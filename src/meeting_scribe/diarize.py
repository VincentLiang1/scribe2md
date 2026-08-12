"""講者分離:找出「誰、在哪些時間段說話」,並抽出每位講者的聲紋。

diarize() 是唯一入口,三步:
1. _segment_and_embed:sherpa-onnx segmentation(pyannote)切出說話
   區段(其內部聚類的講者標籤一律丟棄)+ 3D-Speaker embedding 逐段
   抽聲紋;超長檔案分 15 分鐘塊處理(控記憶體、進度全程可見)。
2. _speech_blocks + _cluster:先把「時間相鄰且聲紋夠像」的區段聚成
   **發言塊**(分群真正的單位——2 秒的區段聲紋近乎雜訊),再以自家的
   「質心式階層重聚」決定誰是誰(sherpa 的 complete-linkage 在長錄音上
   會塌縮);自動模式(人數填 0)另有兩層碎屑吸收與「未知」判定。
   實測案例與門檻校準見下方常數區。
3. 依首次發言時間重編號(講者 0 = 最先開口);太短沒抽聲紋的區段
   繼承時間上最近區段的講者。

回傳 (turns, voiceprints, quality):turns 交給 merge.assign_speakers 掛
講者,voiceprints(原始 embedding 質心)交給聲紋庫做跨會議自動辨識,
quality 是每位講者的分群品質(段數/時長/群內一致性),寫進逐字稿檔尾的
診斷區塊——**合併是單行道**,把好幾個人塌成一群時成品裡看起來只是
「少了一個人」,不主動把數字攤開就沒有人會發現。

現場收音走 IncrementalDiarizer(錄音中就逐塊做完 1、停止後只剩尾巴與
2、3);兩條路共用同一份塊界/擁有權(_ChunkAccum)與重聚邏輯。
"""
import ctypes
import sys
from collections.abc import Callable
from pathlib import Path

import numpy as np

from meeting_scribe import audio, cancel, models
from meeting_scribe.errors import UserFacingError
from meeting_scribe.types import (
    MAX_SPEAKERS,
    UNKNOWN_SPEAKER,
    SpeakerQuality,
    SpeakerTurn,
)

ProgressFn = Callable[[float], None]


def _preload_pip_onnxruntime() -> None:
    """Windows System32 內建舊版 onnxruntime.dll(API 只到 17)會被依名稱優先解析,
    sherpa-onnx 的原生模組需要較新的 ORT C API,綁到舊版會直接 segfault;
    先以完整路徑載入 pip 版 DLL,讓後續依名稱解析綁到正確版本。"""
    if sys.platform != "win32":
        return
    import onnxruntime

    dll = Path(onnxruntime.__file__).parent / "capi" / "onnxruntime.dll"
    if dll.exists():
        ctypes.WinDLL(str(dll))


# 惰性載入(_ensure_sherpa 首次使用才 import):原生庫+DLL 預載付在啟動
# 路徑上會拖慢「開程式到瀏覽器可用」,冷啟動(開機後首次/防毒掃描)更放大;
# 首次轉檔才付,相對轉檔本身可忽略。測試 monkeypatch 本屬性換假貨
sherpa_onnx = None


def _ensure_sherpa():
    """取得 sherpa_onnx 模組(首次呼叫才真正 import)。任何 import
    sherpa_onnx 之前必須先預載 pip 版 onnxruntime DLL(見
    _preload_pip_onnxruntime);測試 monkeypatch 過的假貨原樣回傳。"""
    global sherpa_onnx
    if sherpa_onnx is None:
        _preload_pip_onnxruntime()
        import sherpa_onnx as _real

        sherpa_onnx = _real
    return sherpa_onnx


# sherpa 內部聚類僅用於產生細粒度說話區段(local turn 邊界),其講者標籤
# 一律丟棄、由下方 _cluster() 重聚——FastClustering 是 complete-linkage
# 階層聚類(fast-clustering.cc),在長錄音上千段的規模下對離群段極度敏感:
# 指定人數時 cutree_k 的最後一刀常是「全部 vs 少數離群段」(實測 56 分鐘
# 會議 k=2 塌縮成 1788/4 行),自動模式則爆出數百「講者」。閾值 0.5 取其
# 標籤破碎=區段細,粒度對重聚有利。
_SHERPA_THRESHOLD = 0.5

# --- 重聚參數(56 分鐘真實會議實測校準,見 tests/test_diarize.py)---
# 聲紋可靠的最短區段;更短的區段不抽聲紋,改繼承時間上最近區段的講者
_MIN_EMBED_SEC = 0.5
# 每段聲紋最多取前 N 秒:嵌入品質數秒即飽和,長段全算只是白付 CPU
_MAX_EMBED_SEC = 10.0
# 「重設講者」時每位講者最多抽幾段聲紋(voiceprints_for_spans)。質心是
# 平均,取最長的幾段就夠;再多只是每段都真的跑一次模型換不到什麼
_RELABEL_MAX_SPANS = 8
# 分段處理長度:每 15 分鐘一段逐段切分+抽聲紋。超長檔案(3 小時以上)整檔
# 一次丟給 sherpa 會記憶體吃緊、且切分階段長時間不回報進度(進度條像卡住);
# 分段後每段約數分鐘、逐段回報進度(全程可見),聚類仍在全部聲紋上全域進行。
_CHUNK_SEC = 15 * 60
# 錄音期間增量處理(IncrementalDiarizer)的塊長,比離線的 _CHUNK_SEC 短:
# 按下「停止錄音」時若正好有一塊在跑,收尾必須等它跑完(引擎不可併用),
# 塊越長這段死等越久(15 分鐘塊 ≈ 4~11 分鐘 CPU,標準機實測 RTF 0.24~0.77)。
# 5 分鐘把最壞等待壓到數分鐘;代價是重疊佔比由 3% 升到 10%(多做一成
# 切分),換得「散會即拿稿」的體感。離線路徑不受影響、仍用 _CHUNK_SEC
_LIVE_CHUNK_SEC = 5 * 60
# 相鄰段的重疊秒數:讓「橫跨切點的發言」被某一段完整收錄(不被切斷),
# 再以「turn 中點落在哪一段的擁有區間(重疊區中點為界)」去重——不重複、
# 不遺漏。重疊 ≥ 單一 turn 長度即完全消除交界效應;30 秒涵蓋絕大多數發言段。
_CHUNK_OVERLAP_SEC = 30
# 塊內進度的兩相分配:sd.process(切分+sherpa 內部聲紋)與本模組抽聲紋
# 耗時粗估 7:3。比例只影響塊內視覺速度,不影響正確性(單調即可)。
_SEG_PHASE_FRAC = 0.7
# --- 發言塊(2026-08-08 加,校準見下方與 tests/test_diarize.py)---
#
# **分群的單位是「發言塊」不是「區段」**。sherpa 切出來的區段中位數只有
# 2.8 秒(3 小時 59 分的月會實測:2325 段裡 25% 短於 1.4 秒),而 2 秒的
# 語音抽出來的聲紋近乎雜訊——它跟誰都有點像,在質心式階層合併裡就成了
# 把兩個真實講者串起來的**橋**。一段 8 分鐘的報告其實是**一個**很可靠的
# 樣本,不是 55 個很爛的樣本。
#
# 聚塊的條件是「時間相鄰**且**聲紋夠像」,兩個都要:只看時間會在一問一答
# 時把主席併進報告人;只看聲紋就退化成分群本身。
#
# **發言塊與下面調高的門檻是一組的,兩個都需要**(2026-08-08 消融實驗;
# 月會 2325 段 → 649 塊、對照組 1129 段 → 345 塊):
#             月會(20+ 人)        對照組(5 人,已確認正確)
#   舊參數+舊單位   17 群、遠端 1/4     5 群、最低集中度 96%   ← 使用者回報的災情
#   新參數+舊單位   26 群、遠端 4/4     9 群、最低集中度 49%   ← 修好了大會、打壞小會
#   舊參數+發言塊   19 群、遠端 1/4     —                    ← 只換單位不夠
#   新參數+發言塊   25 群、遠端 4/4     6 群、最低集中度 91%   ← 取這個
# 也就是:**門檻負責把大型會議分開,發言塊負責讓小型會議不被那個門檻打碎**。
# 少任何一半都會壞一邊,別把其中一項當成「順手可以拿掉的」。
_BLOCK_MAX_GAP_SEC = 5.0
_BLOCK_MIN_SIM = 0.60
# 自動偵測人數:兩群質心 cosine 相似度低於此值即停止合併。
# 校準基準(扣全域均值後):同講者質心對 ~0.7+,不同講者 ~0.4 以下
#
# ⚠️ **0.60 是窄窗,不要往上調**(2026-08-08 兩份真實錄音實測):
#   門檻   月會(20+ 人)              對照組(5 人,已確認分群正確)
#   0.58   遠端 3/4 分開             6 群、最低集中度 90%
#   0.60   遠端 4/4 分開 ← 取這個     6 群、最低集中度 91%
#   0.62   遠端 4/4 分開             10 群、最低集中度 49%(L 被劈成兩半)
# 0.62 在月會沒有更好,卻把一場使用者親自確認過的錄音打碎。往下(0.58)則
# 分不開兩個遠端據點。這個窗口這麼窄,正說明**單一絕對門檻本來就撐不住**
# 各種規模的會議——所以逐字稿檔尾另有一整節診斷把每個標籤的數字攤開
# (export._speaker_diagnostics),不要指望門檻自己解決全部問題。
_AUTO_STOP_SIM = 0.60
# 自動模式碎屑吸收改為「兩層」(取代舊的比例門檻——比例門檻以總語音量計,
# 會議越長門檻越高,3.9 小時月會實測把 22 位講者塌成 4,見 tests)。
# (1) 極短碎段:總時長 < _TINY_FRAGMENT_SEC 者判為雜訊/重疊語音,
#     無條件併入最近群(講者聲紋在此規模下也不可靠)。
# (2) 中等小群:_TINY_FRAGMENT_SEC ≤ 時長 < _MINOR_SPEAKER_SEC 者用相似度
#     把關——與最近大群(≥ _MINOR_SPEAKER_SEC)相似度 > _ABSORB_SIM 才吸收
#     (判為該講者的音量/距離漂移碎段);否則保留(判為發言少的獨立講者)。
# 關鍵:漂移碎段仍與本人大群「有點像」,真.少發言者則與所有大群都不像——
# 用相似度而非時長區分兩者,才能同時服務小會議(2 人)與大會議(20+ 人)。
# ⚠️ 這兩個時長門檻在 2026-08-08 從 30/120 調成 20/60,理由是**大型會議的
# 「小講者」根本不小**:3 小時 59 分的月會裡,兩個遠端據點的與會者各只講了
# 100~135 秒——120 秒的門檻把他們判成「碎段」,再用 0.30 的相似度(那已經
# 是不同人的水準)把他們吸收進別人名下。實測 120→60 是四位遠端能分開的
# 必要條件之一,而對照組(5 人)的最低集中度反而從 86% 升到 91%。
_TINY_FRAGMENT_SEC = 20.0
_MINOR_SPEAKER_SEC = 60.0
_ABSORB_SIM = 0.30
# 「未知」門檻:某段聲紋與所屬講者群質心的相似度低於此值,判為未知
# (跟任何已辨識講者都不夠像,通常是很短/模糊/重疊的碎片,如一兩秒的搭腔)。
# 真實 2 人訪談(recording 27)校準:好段落 ~0.5+,模糊碎段 <0.1。
_UNKNOWN_SIM = 0.10


# 單例快取:diarizer/embedder 建構要重讀 onnx(實測 ~0.45 秒),批次多檔
# 重用;設定不再依講者人數而變(重聚在本模組做),永遠只需一份
_SD_CACHE: list["sherpa_onnx.OfflineSpeakerDiarization"] = []
_EMBED_CACHE: list["sherpa_onnx.SpeakerEmbeddingExtractor"] = []


def _num_threads() -> int:
    # sherpa-onnx 預設單執行緒,會讓講者分離慢於即時。執行緒數用使用者於
    # 介面指定的核心數(預設 最大核心數-1),見 power.cpu_worker_count。
    # 錄音期間的隔離不靠調小這個數字(2026-08-03 實測 7→5 反而更差),
    # 而是整支跑進子行程(見 diarworker)——問題是 GIL 不是核心數。
    from meeting_scribe import power

    return power.cpu_worker_count()


def clear_engine_cache() -> None:
    """釋放 diarizer/embedder 快取。CPU 核心數變更時由 app 呼叫:執行緒數
    在引擎建構時定死,不清快取新值不會生效(transcribe/punctuate 同款 API)。"""
    _SD_CACHE.clear()
    _EMBED_CACHE.clear()


def _get_diarizer() -> "sherpa_onnx.OfflineSpeakerDiarization":
    if _SD_CACHE:
        return _SD_CACHE[0]
    so = _ensure_sherpa()
    # 模型下載失敗(UserFacingError)在組 config 前就浮出,不會被下方
    # 「載入失敗」訊息誤蓋
    config = so.OfflineSpeakerDiarizationConfig(
        segmentation=so.OfflineSpeakerSegmentationModelConfig(
            pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=str(models.segmentation_model())
            ),
            num_threads=_num_threads(),
        ),
        # 這裡刻意用「輕量」的內部聲紋模型:sherpa 對每個滑窗的每位活躍
        # 講者都要抽一次聲紋(整場重複抽 7 倍以上,佔講者分析八成時間),
        # 而那些聲紋我們全部丟棄——只需要它切得出 turn 邊界。誰是誰由
        # 下面的 _get_embedder(eres2netv2)重抽+重聚決定,聲紋庫不受影響
        # (實測數據見 models.internal_embedding_model)
        embedding=so.SpeakerEmbeddingExtractorConfig(
            model=str(models.internal_embedding_model()),
            num_threads=_num_threads(),
        ),
        clustering=so.FastClusteringConfig(
            num_clusters=-1,
            threshold=_SHERPA_THRESHOLD,
        ),
    )
    try:
        sd = so.OfflineSpeakerDiarization(config)
    except Exception as e:
        # sherpa 原生錯誤是 cryptic 英文 RuntimeError(常見:快取模型檔損壞
        # 但大小過門檻),不得直出給使用者
        raise UserFacingError(
            "講者分析模型載入失敗,模型檔可能已損壞:請刪除 "
            r"%LOCALAPPDATA%\meeting-scribe\models 資料夾後重試(會重新下載)"
        ) from e
    _SD_CACHE.append(sd)
    return sd


def _get_embedder() -> "sherpa_onnx.SpeakerEmbeddingExtractor":
    if _EMBED_CACHE:
        return _EMBED_CACHE[0]
    so = _ensure_sherpa()
    config = so.SpeakerEmbeddingExtractorConfig(
        model=str(models.embedding_model()),
        num_threads=_num_threads(),
    )
    try:
        ex = so.SpeakerEmbeddingExtractor(config)
    except Exception as e:
        raise UserFacingError(
            "講者分析模型載入失敗,模型檔可能已損壞:請刪除 "
            r"%LOCALAPPDATA%\meeting-scribe\models 資料夾後重試(會重新下載)"
        ) from e
    _EMBED_CACHE.append(ex)
    return ex


def _embed_chunks(
    chunks, n: int, progress: ProgressFn | None = None,
) -> np.ndarray:
    """逐段音訊 → L2 正規化聲紋向量,shape (n, dim)。

    收「音訊片段的可迭代物」而非整段波形:塊內抽取直接切陣列,退化路徑
    (所有 turn 都短於門檻)則逐段回頭讀檔——錄音中的增量處理不持有整份
    波形,兩條路才共用同一份正規化/取消/進度邏輯。"""
    ex = _get_embedder()
    vecs = []
    for i, chunk in enumerate(chunks):
        cancel.check()  # 停止響應點:逐段之間(單段 ≤10 秒音訊)
        stream = ex.create_stream()
        stream.accept_waveform(16000, chunk)
        stream.input_finished()
        v = np.asarray(ex.compute(stream), dtype=np.float64)
        norm = np.linalg.norm(v)
        vecs.append(v / norm if norm > 0 else v)
        if progress is not None:
            progress((i + 1) / n)
    return np.stack(vecs)


def _extract_embeddings(
    samples: np.ndarray,
    spans: list[tuple[float, float]],
    progress: ProgressFn | None = None,
) -> np.ndarray:
    """抽取每個區段的 L2 正規化聲紋向量,shape (len(spans), dim)。"""
    return _embed_chunks(
        (
            samples[int(s * 16000): int(min(e, s + _MAX_EMBED_SEC) * 16000)]
            for s, e in spans
        ),
        len(spans), progress,
    )


def _wcentroid(vecs: np.ndarray, weights: np.ndarray) -> np.ndarray:
    """時長加權質心(L2 正規化)。分群各處共用一份,算法才不會走鐘。"""
    s = (vecs * weights[:, None]).sum(axis=0)
    return s / max(float(np.linalg.norm(s)), 1e-12)


def _speech_blocks(
    spans: np.ndarray, vecs: np.ndarray, weights: np.ndarray,
) -> list[list[int]]:
    """把「時間相鄰**且**聲紋夠像」的區段聚成發言塊(回傳每塊的區段索引)。

    這是分群真正的單位,理由見 _BLOCK_MAX_GAP_SEC 的註解。兩個條件缺一
    不可:

    - 只看時間 → 一問一答時會把主席併進報告人那一塊(而那正是大型會議
      的常態:報告人講一段、主席插一句、報告人再接下去)。
    - 只看聲紋 → 就是分群本身,沒有解決任何事。

    比對的是「新區段 vs 這一塊目前的質心」而不是 vs 前一段:前一段可能
    剛好是這塊裡最爛的那一段(換氣、咳嗽),拿它當門檻會無謂地切斷。
    用**原始 embedding**(未扣均值)比對:聚塊發生在扣均值之前,而且
    這裡問的是「像不像同一個人」,那正是原始空間的問題。"""
    order = np.argsort(spans[:, 0], kind="stable")
    blocks: list[list[int]] = []
    cur = [int(order[0])]
    cur_sum = vecs[cur[0]] * weights[cur[0]]
    for k in order[1:]:
        k = int(k)
        gap = spans[k, 0] - spans[cur[-1], 1]
        cent = cur_sum / max(float(np.linalg.norm(cur_sum)), 1e-12)
        if gap <= _BLOCK_MAX_GAP_SEC and float(vecs[k] @ cent) >= _BLOCK_MIN_SIM:
            cur.append(k)
            cur_sum = cur_sum + vecs[k] * weights[k]
        else:
            blocks.append(cur)
            cur = [k]
            cur_sum = vecs[k] * weights[k]
    blocks.append(cur)
    return blocks


def _cluster(
    vecs: np.ndarray, weights: np.ndarray, num_speakers: int,
    blocks: list[list[int]] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """把聲紋向量分群,回傳 (每列的群標籤, 每列的信心度)。

    質心式階層合併(centroid agglomerative):每步合併質心 cosine 相似度
    最高的兩群,質心 = 時長加權和再正規化——離群段只會稀釋質心,不像
    complete-linkage 一票否決整群合併。分群前扣除全域(時長加權)均值:
    同房間同麥克風的通道成分會把所有相似度整體墊高、壓縮同人/異人間距,
    扣均值後間距顯著拉開(真實會議實測 異人 0.66 → <0.3)。

    **blocks = 合併的起點**(每個元素是一組 vecs 的索引,見 _speech_blocks):
    給了就從「發言塊」開始合併而不是從單一區段——2 秒的區段聲紋近乎雜訊,
    會在階層合併裡當橋把兩個真實講者串起來。不給則每段自成一塊(等同舊
    行為,單元測試走這條)。

    num_speakers>0 合併到指定群數為止;=0(自動)合併到最佳相似度低於
    _AUTO_STOP_SIM,再做兩層碎屑吸收(見上方常數)、以 MAX_SPEAKERS 封頂
    (types.MAX_SPEAKERS,與 UI 的人數上限同源)。

    ⚠️ **合併是單行道,這裡沒有「拆回來」的機制**,而且不是沒試過:
    2026-08-08 實作過一道分裂修復(對每個最終群做 2-means、夠分得開就拆),
    在兩份真實錄音上實測**沒有任何錨點因此被分對**,卻讓月會從 25 群變成
    30 群(撞上 MAX_SPEAKERS 上限)、讓已確認正確的對照組從 6 群變 7 群,
    所以移除了(實作見 git 歷史)。真正有效的是把合併的**起點**換成發言塊
    ——與其事後補救,不如一開始就不要拿雜訊當樣本。
    """
    n = len(vecs)
    if n == 0:
        return np.zeros(0, dtype=int), np.zeros(0, dtype=float)
    if blocks is None:
        blocks = [[i] for i in range(n)]
    m = len(blocks)
    # k 以**塊數**為上限:指定人數多過塊數時合併不到那麼多群
    k = min(num_speakers, m) if num_speakers > 0 else 0
    if k == 1 or m == 1:
        # 只有一位講者:全體屬同一群,信心度視為滿分(不判未知)
        return np.zeros(n, dtype=int), np.ones(n, dtype=float)

    mean = (vecs * weights[:, None]).sum(axis=0) / weights.sum()
    centered = vecs - mean
    norms = np.linalg.norm(centered, axis=1, keepdims=True)
    centered = centered / np.maximum(norms, 1e-12)

    # 活躍群狀態:加權向量和(質心 = 正規化後的和)、總時長、成員。
    # 起點是「塊」不是「段」——members 裝的仍是段的索引,所以下游
    # (信心度、標籤指派)完全不必知道塊的存在
    members: list[list[int]] = [list(b) for b in blocks]
    sums = np.stack([(centered[b] * weights[b, None]).sum(axis=0) for b in members])
    durs = np.array([weights[b].sum() for b in members], dtype=float)
    active = np.ones(m, dtype=bool)

    def centroid(idx: int) -> np.ndarray:
        s = sums[idx]
        return s / max(np.linalg.norm(s), 1e-12)

    # 相似度矩陣(僅活躍列有效);自身與非活躍設 -inf 避免被選中
    cents = np.stack([centroid(i) for i in range(m)])
    sim = cents @ cents.T
    np.fill_diagonal(sim, -np.inf)
    # 每列極值快取:找「全域最相似對」只掃 row_max(O(n)),不整片 argmax
    # ——整片掃是 O(n²),乘上 ~n 次合併就是 O(n³)(56 分鐘會議 n≈1788,
    # 更長會議聚類會拖到分鐘級)。合併只改一列一行,其他列僅在「舊極值
    # 指向被改的兩列」時才重掃;平手時的選擇與整片 argmax 一致(最小列
    # 優先、列內最小行優先=攤平後首個極值),合併順序與改版前相同
    row_max = sim.max(axis=1)
    row_arg = sim.argmax(axis=1)

    def merge_pair(i: int, j: int) -> None:
        """把群 j 併入群 i,更新質心、相似度矩陣與每列極值快取。"""
        sums[i] += sums[j]
        durs[i] += durs[j]
        members[i] += members[j]
        active[j] = False
        sim[j, :] = -np.inf
        sim[:, j] = -np.inf
        row_max[j] = -np.inf
        ci = centroid(i)
        cents[i] = ci
        row = cents @ ci
        row[~active] = -np.inf
        row[i] = -np.inf
        sim[i, :] = row
        sim[:, i] = row
        row_max[i] = row.max()
        row_arg[i] = row.argmax()
        # 其他列只需局部修正:舊極值指向 i 或 j 的已失效 → 重掃該列;
        # 第 i 行的新值比原極值大的,直接以之為新極值(變小且非舊極值
        # 所在行則整列不受影響)
        stale = active & ((row_arg == i) | (row_arg == j))
        stale[i] = False
        for s in np.flatnonzero(stale):
            row_max[s] = sim[s].max()
            row_arg[s] = sim[s].argmax()
        better = active & (row > row_max)
        better[i] = False
        row_max[better] = row[better]
        row_arg[better] = i

    def merge_best() -> float:
        """合併目前最相似的兩群,回傳合併前的相似度;無可合併回 -inf。"""
        best = float(row_max.max())
        if not np.isfinite(best):
            return -np.inf
        i = int(np.argmax(row_max))
        j = int(row_arg[i])
        merge_pair(i, j)
        return best

    if k > 0:
        while active.sum() > k:
            if merge_best() == -np.inf:
                break
    else:
        while active.sum() > 1:
            if float(row_max.max()) < _AUTO_STOP_SIM:
                break
            merge_best()
        # (1) 極短碎段:無條件併入最近群(雜訊/重疊語音)
        while active.sum() > 1:
            alive = np.flatnonzero(active)
            tiny = [i for i in alive if durs[i] < _TINY_FRAGMENT_SEC]
            if not tiny:
                break
            s = tiny[0]
            target = max(
                (a for a in alive if a != s), key=lambda b: float(cents[b] @ cents[s])
            )
            merge_pair(target, s)
        # (2) 中等小群:與最近大群相似度 > _ABSORB_SIM 才吸收(漂移碎段);
        #     都不夠像就停(剩下的判為發言少的獨立講者,予以保留)
        while active.sum() > 1:
            alive = np.flatnonzero(active)
            small = [i for i in alive if durs[i] < _MINOR_SPEAKER_SEC]
            big = [i for i in alive if durs[i] >= _MINOR_SPEAKER_SEC]
            if not small or not big:
                break
            best_sim, s = max(
                (float(max(cents[b] @ cents[i] for b in big)), i) for i in small
            )
            if best_sim < _ABSORB_SIM:
                break
            target = max(big, key=lambda b: float(cents[b] @ cents[s]))
            merge_pair(target, s)
        while active.sum() > MAX_SPEAKERS:  # 自動模式講者數上限(types.MAX_SPEAKERS,與 UI 同源)
            if merge_best() == -np.inf:
                break

    labels = np.zeros(n, dtype=int)
    conf = np.zeros(n, dtype=float)
    for lab, i in enumerate(np.flatnonzero(active)):
        g = members[i]
        labels[g] = lab
        # 信心度 = 該段(扣均值正規化後)聲紋與所屬群質心的 cosine 相似度;
        # 低 = 這段其實跟本群不像(常是被硬併進來的模糊/重疊短碎段)
        conf[g] = centered[g] @ _wcentroid(centered[g], weights[g])
    return labels, conf


def _relabel_by_first_appearance(labels: list[int]) -> list[int]:
    """講者編號依首次發言時間排序(講者 0 = 最先開口),輸出穩定可讀;
    UNKNOWN_SPEAKER(未知)保持不變、不參與編號。"""
    order: dict[int, int] = {}
    for lab in labels:
        if lab != UNKNOWN_SPEAKER and lab not in order:
            order[lab] = len(order)
    return [lab if lab == UNKNOWN_SPEAKER else order[lab] for lab in labels]


def _window_step(chunk_sec: float) -> tuple[int, int]:
    """(單塊樣本數, 塊間位移樣本數)。塊界算式只有這一份:離線 diarize 與
    錄音中的增量處理共用,兩邊的擁有權邊界才不會對不起來。"""
    window = int(chunk_sec * 16000)
    overlap = int(_CHUNK_OVERLAP_SEC * 16000)
    return window, max(1, window - overlap)


def _chunk_count(total_samples: int, chunk_sec: float) -> int:
    window, step = _window_step(chunk_sec)
    if total_samples <= window:
        return 1
    return 1 + (total_samples - window + step - 1) // step


class _ChunkAccum:
    """逐塊累積「切分結果 + 聲紋」的狀態。

    相鄰塊重疊 _CHUNK_OVERLAP_SEC 秒,橫跨切點的發言由某塊完整收錄;以
    turn 中點的擁有權(重疊區中點為界)去重——不重複、不遺漏。分塊讓
    超長檔案控制記憶體,也讓錄音中就能邊錄邊做(塊內獨立,真正需要全場
    資料的只有最後的重聚)。

    process_chunk 對外原子:中途拋例外(引擎失敗、按下停止)不留半套
    狀態,該塊之後重跑即可——增量處理的失敗退回正是靠這點。"""

    def __init__(self, chunk_sec: float):
        self.chunk_sec = chunk_sec
        self.spans: list[tuple[float, float]] = []  # 全域時間的 (起, 迄) 秒
        self.emb_idx: list[int] = []  # spans 中「可抽聲紋」者的索引
        self.vec_blocks: list[np.ndarray] = []  # 對應 emb_idx 的聲紋(順序一致)
        self.next_chunk = 0  # 下一個待處理的塊序號

    def vectors(self) -> np.ndarray:
        return np.vstack(self.vec_blocks) if self.vec_blocks else np.zeros((0, 0))

    def process_chunk(
        self,
        sd: "sherpa_onnx.OfflineSpeakerDiarization",
        seg: np.ndarray,
        c: int,
        *,
        is_last: bool,
        progress: ProgressFn | None = None,
    ) -> None:
        """處理第 c 塊(seg = 該塊音訊)。progress 以「本塊完成度 0~1」回報
        ——塊內就要連續回報(切分逐窗、抽聲紋逐段):單塊要跑數分鐘,
        只在塊完成時回報會讓進度條長時間定格。"""
        sr = 16000
        window, step = _window_step(self.chunk_sec)
        s0 = c * step
        offset = s0 / sr
        half = _CHUNK_OVERLAP_SEC / 2  # 擁有權邊界設在重疊區中點(秒)
        # 此塊「擁有」的全域時間區間;turn 中點落在此區間才保留(相鄰塊去重)
        own_lo = 0.0 if c == 0 else offset + half
        own_hi = float("inf") if is_last else (s0 + step) / sr + half
        cancel.check()  # 停止響應點:塊與塊之間

        # seg_cb 無論有沒有 progress 都要掛:取消時直接在 callback 內拋
        # Cancelled,經 pybind 讓 sd.process 立即中止並把例外帶回——取消
        # 才不用等單塊跑完。sherpa 文件宣稱「回傳非零即中止」,1.13.4
        # 實測會忽略回傳值,故不可倚賴;拋例外實測有效、例外型別完整
        # 浮出、且中止後同一 diarizer 重用結果一致。
        def seg_cb(done: int, total: int) -> int:
            cancel.check()
            if progress is not None and total > 0:
                progress(_SEG_PHASE_FRAC * min(done / total, 1.0))
            return 0

        emb_cb = None
        if progress is not None:
            def emb_cb(f: float) -> None:
                progress(_SEG_PHASE_FRAC + (1 - _SEG_PHASE_FRAC) * f)

        raw = sd.process(seg, seg_cb).sort_by_start_time()
        cancel.check()  # 最後一次 callback 之後才按停止的窗口,在此補接
        new_spans: list[tuple[float, float]] = []
        local_emb: list[int] = []  # new_spans 內可抽聲紋者的索引
        local_spans: list[tuple[float, float]] = []  # 塊內(chunk-local)秒
        for r in raw:
            gstart, gend = r.start + offset, r.end + offset
            if not (own_lo <= (gstart + gend) / 2 < own_hi):
                continue  # 中點不在本塊擁有區間 → 由相鄰塊負責,去重
            new_spans.append((gstart, gend))
            if r.end - r.start >= _MIN_EMBED_SEC:
                local_emb.append(len(new_spans) - 1)
                local_spans.append((r.start, r.end))
        vecs = (
            _extract_embeddings(seg, local_spans, progress=emb_cb)
            if local_spans else None
        )
        base = len(self.spans)  # 全部成功才提交(見類別 docstring)
        self.spans.extend(new_spans)
        self.emb_idx.extend(base + i for i in local_emb)
        if vecs is not None:
            self.vec_blocks.append(vecs)
        self.next_chunk = c + 1
        if progress is not None:
            progress(1.0)  # 塊完成(塊內無語音時也要推進)


def _segment_and_embed(
    sd: "sherpa_onnx.OfflineSpeakerDiarization",
    samples: np.ndarray,
    progress: ProgressFn | None,
) -> _ChunkAccum:
    """整份音訊逐塊切分 + 抽聲紋(離線路徑)。"""
    accum = _ChunkAccum(_CHUNK_SEC)
    window, step = _window_step(_CHUNK_SEC)
    n_chunks = _chunk_count(len(samples), _CHUNK_SEC)
    for c in range(n_chunks):
        # c 以預設參數固化,防 late-binding 錯亂(同 app.on_stage)
        sub = None
        if progress is not None:
            def sub(f: float, c: int = c) -> None:
                progress((c + f) / n_chunks)
        accum.process_chunk(
            sd, samples[c * step: c * step + window], c,
            is_last=(c == n_chunks - 1), progress=sub,
        )
    return accum


def _speaker_voiceprints(
    emb_idx: list[int], vecs: np.ndarray, labels: list[int]
) -> dict[int, np.ndarray]:
    """每位講者的原始聲紋質心(L2 正規化);未知不計。供跨會議辨識——
    用原始 embedding(講者驗證原生空間),非會議內分群的扣均值向量。"""
    if vecs.size == 0:
        return {}
    sums: dict[int, np.ndarray] = {}
    for k, idx in enumerate(emb_idx):
        lab = labels[idx]
        if lab == UNKNOWN_SPEAKER:
            continue
        sums[lab] = sums.get(lab, np.zeros(vecs.shape[1])) + vecs[k]
    out: dict[int, np.ndarray] = {}
    for lab, s in sums.items():
        norm = np.linalg.norm(s)
        out[lab] = (s / norm).astype(np.float32) if norm > 0 else s.astype(np.float32)
    return out


def _quality(
    turns: list[SpeakerTurn], conf: np.ndarray, emb_labels: list[int],
) -> list[SpeakerQuality]:
    """每位講者的分群品質(見 types.SpeakerQuality)。

    段數/時長取 **turns**(使用者在逐字稿裡看得到的那一份),cohesion 只能
    取有抽聲紋的那些段——沒抽聲紋的短碎段是「繼承時間上最近鄰」來的,
    拿它算一致性等於拿自己的推論當證據。"""
    out: list[SpeakerQuality] = []
    for lab in sorted({t.speaker for t in turns if t.speaker != UNKNOWN_SPEAKER}):
        mine = [t for t in turns if t.speaker == lab]
        sims = [c for c, m in zip(conf, emb_labels) if m == lab]
        out.append(SpeakerQuality(
            speaker=lab,
            segments=len(mine),
            seconds=float(sum(t.end - t.start for t in mine)),
            cohesion=float(np.mean(sims)) if sims else 0.0,
        ))
    return out


def _labels_and_voiceprints(
    accum: _ChunkAccum,
    num_speakers: int,
    read: Callable[[float, float], np.ndarray],
) -> tuple[list[SpeakerTurn], dict[int, np.ndarray], list[SpeakerQuality]]:
    """累積的切分/聲紋 → (turns, voiceprints, quality):全域重聚決定
    「誰是誰」,再依首次發言時間重編號。離線與錄音增量共用(重聚必須看
    全場,見 _cluster docstring)。read(起秒, 迄秒) 只在退化路徑用到。

    quality 是每位講者的「這個標籤有多可信」,帶到輸出的診斷區塊——分群
    把幾個人塌成一群時,成品裡看起來只是「少了一個人」,不主動講就沒有人
    會發現。"""
    all_spans = accum.spans
    emb_idx = accum.emb_idx
    vecs = accum.vectors()
    if not all_spans:
        return [], {}, []

    if not emb_idx:  # 極端:所有 turn 都短於門檻 → 退化用全部區段抽聲紋
        emb_idx = list(range(len(all_spans)))
        vecs = _embed_chunks(
            (read(s, min(e, s + _MAX_EMBED_SEC)) for s, e in all_spans),
            len(all_spans),
        )

    est = np.array([all_spans[i] for i in emb_idx], dtype=float)
    weights = est[:, 1] - est[:, 0]
    # 分群的單位是「發言塊」不是「區段」(見 _speech_blocks)
    blocks = _speech_blocks(est, vecs, weights)
    labels_emb, conf_emb = _cluster(vecs, weights, num_speakers, blocks)
    labels_emb = labels_emb.copy()
    # 只有自動模式(num_speakers=0)才判「未知」:信心度過低者(與所屬講者群
    # 都不夠像)不硬塞給某講者。指定人數時尊重使用者、全數歸給指定的講者。
    if num_speakers == 0:
        labels_emb[conf_emb < _UNKNOWN_SIM] = UNKNOWN_SPEAKER

    # 未抽聲紋的短區段:繼承時間中點最近的已分群區段的講者(可能是未知)
    mids_emb = np.array([(all_spans[i][0] + all_spans[i][1]) / 2 for i in emb_idx])
    label_by_idx = {idx: int(lab) for idx, lab in zip(emb_idx, labels_emb)}
    labels = []
    for i, (s, e) in enumerate(all_spans):
        if i in label_by_idx:
            labels.append(label_by_idx[i])
        else:
            mid = (s + e) / 2
            labels.append(int(labels_emb[int(np.argmin(np.abs(mids_emb - mid)))]))

    labels = _relabel_by_first_appearance(labels)
    # 品質指標吃的是**重編號後**的標籤:兩邊用同一份 labels,警告才不會
    # 掛到別人頭上(那比不警告更糟)
    emb_labels = [labels[i] for i in emb_idx]

    voiceprints = _speaker_voiceprints(emb_idx, vecs, labels)
    turns = [SpeakerTurn(s, e, lab) for (s, e), lab in zip(all_spans, labels)]
    return turns, voiceprints, _quality(turns, conf_emb, emb_labels)


def diarize(
    wav_path: str | Path,
    num_speakers: int = 0,
    progress: ProgressFn | None = None,
) -> tuple[list[SpeakerTurn], dict[int, np.ndarray], list[SpeakerQuality]]:
    """num_speakers=0 表示自動偵測人數。回傳 (turns, voiceprints, quality):
    voiceprints = {講者標籤: 原始聲紋質心},供聲紋庫登記/辨識;
    quality = 每位講者的分群品質(見 types.SpeakerQuality),供拒絕自動
    命名與輸出診斷用。

    逐段(每 15 分鐘)切分 + 抽聲紋(進度 0~0.9,超長檔案也全程可見進度、
    控制記憶體),再對全部聲紋做全域重聚(0.9~1.0)決定「誰是誰」,
    見 _ChunkAccum / _cluster docstring。現場收音走增量版
    (IncrementalDiarizer),兩者共用同一份塊界與重聚邏輯。
    """
    sd = _get_diarizer()
    samples = audio.read_wav16k(wav_path)

    seg_progress = (lambda f: progress(0.9 * f)) if progress is not None else None  # noqa: E731
    accum = _segment_and_embed(sd, samples, seg_progress)
    turns, voiceprints, quality = _labels_and_voiceprints(
        accum, num_speakers,
        lambda a, b: samples[int(a * 16000): int(b * 16000)],
    )
    if progress is not None:
        progress(1.0)
    return turns, voiceprints, quality


def voiceprints_for_spans(
    wav_path: str | Path,
    spans_by_speaker: dict[int, list[tuple[float, float]]],
    progress: ProgressFn | None = None,
) -> dict[int, np.ndarray]:
    """已知「誰在哪幾段講話」時,直接算每位講者的聲紋質心(**不做分群**)。

    「重設講者」用(app._run_relabel):現成逐字稿裡已經有講者標籤與時間戳
    ——分群早在當初轉檔時就做完了,缺的只是聲紋向量。重跑一次完整的
    `diarize()` 要一小時上下(切分 + 每窗每位講者各抽一次聲紋,見模組
    docstring 的耗時結構),而這裡只對每位講者的幾個區段各抽一次,幾分鐘。

    **公開這支而不是讓呼叫端去拿 `_extract_embeddings`**:那是私有的,
    上游一改名就會安靜壞掉(今天才在 record._ensure_com 踩過同一類),
    而且質心的算法(L2 正規化、未知不計)必須與 diarize 走同一份——
    兩邊各算一次的話,同一個人在「轉檔時登記」與「重設時登記」會得到
    不一樣的向量,聲紋庫就開始自相矛盾。

    每位講者最多取 `_RELABEL_MAX_SPANS` 段:質心是平均,再多的邊際效益
    很低,而每一段都要真的跑一次模型。"""
    samples = audio.read_wav16k(wav_path)
    flat: list[tuple[float, float]] = []
    owners: list[int] = []
    for speaker, spans in spans_by_speaker.items():
        if speaker == UNKNOWN_SPEAKER:
            continue  # 未知是多人零碎語音的混合,登記會污染聲紋庫
        for s, e in sorted(spans, key=lambda x: x[1] - x[0], reverse=True)[
            :_RELABEL_MAX_SPANS
        ]:
            flat.append((s, e))
            owners.append(speaker)
    if not flat:
        return {}
    vecs = _extract_embeddings(samples, flat, progress)
    # 借用同一份質心邏輯:emb_idx 是「第 k 個向量對應 labels 的哪一格」,
    # 這裡一對一,所以 labels 直接就是 owners
    return _speaker_voiceprints(list(range(len(owners))), vecs, owners)


class IncrementalDiarizer:
    """錄音期間就把「切分 + 抽聲紋」做掉,停止錄音後只剩尾巴與全域重聚。

    痛點:講者分析是整個工具最慢的一段(標準機實測 RTF 0.24~0.77——
    2 小時的會議要跑 1 小時上下),而它整段都壓在「散會後」。切分與抽聲紋其實是
    塊內獨立的(塊界+中點擁有權見 _ChunkAccum),錄音中就能一塊一塊做;
    真正需要全場資料的只有兩階段重聚(_cluster,18 分鐘會議實測 0.3 秒)。

    read(起秒, 迄秒) → float32 波形,由呼叫端提供:錄音中的軌檔標頭
    落後於實際資料,不能用 wave 讀尾端(見 live._read_span)。

    用法:錄音中反覆 poll(),停止後 finish()。poll 中途失敗(引擎、
    取消)不推進狀態,該塊由 finish 重跑——最壞退回等同離線行為。"""

    def __init__(self, chunk_sec: float = _LIVE_CHUNK_SEC):
        self._accum = _ChunkAccum(chunk_sec)

    @property
    def done_sec(self) -> float:
        """已完成切分的音訊秒數(狀態顯示用;塊的擁有權上界)。"""
        _window, step = _window_step(self._accum.chunk_sec)
        if self._accum.next_chunk == 0:
            return 0.0
        return self._accum.next_chunk * step / 16000 + _CHUNK_OVERLAP_SEC / 2

    def poll(
        self, read: Callable[[float, float], np.ndarray], avail_sec: float,
        progress: ProgressFn | None = None,
        should_continue: Callable[[], bool] | None = None,
    ) -> bool:
        """把「音訊已寫滿」的塊都處理掉(尾塊留給 finish:它的擁有權要
        開到無限大,錄音還沒停就不知道尾巴在哪)。回傳是否有進展。

        should_continue 在每塊之前問一次:積壓多塊時(線上會議雙軌合計
        追不上即時)呼叫端要能在塊界收手,否則「停止錄音」會被迫等整批
        清完,而不只是等進行中的那一塊。"""
        sd = _get_diarizer()
        window, step = _window_step(self._accum.chunk_sec)
        avail = int(avail_sec * 16000)
        did = False
        while self._accum.next_chunk * step + window <= avail:
            if should_continue is not None and not should_continue():
                return did
            c = self._accum.next_chunk
            seg = read(c * step / 16000, (c * step + window) / 16000)
            if len(seg) < window:
                return did  # 讀不足(檔案還在長):狀態不推進,下一輪再來
            self._accum.process_chunk(sd, seg, c, is_last=False, progress=progress)
            did = True
        return did

    def finish(
        self,
        read: Callable[[float, float], np.ndarray],
        total_sec: float,
        num_speakers: int = 0,
        progress: ProgressFn | None = None,
    ) -> tuple[list[SpeakerTurn], dict[int, np.ndarray], list[SpeakerQuality]]:
        """收尾:補完還沒做的塊(含尾塊)+ 全域重聚 → (turns, voiceprints, quality)。

        錄音中若已把所有「整塊」做完,最後一塊當時是以「非尾塊」處理的
        (擁有權止於 own_hi),尾巴那段還沒人認領——補一塊 is_last=True
        接管 [own_hi, 結尾]。錄音中一塊都沒做成(無 GPU 不啟用、或引擎
        失敗)時,這裡就等同離線走法。"""
        sd = _get_diarizer()
        window, step = _window_step(self._accum.chunk_sec)
        total = int(total_sec * 16000)
        n_chunks = _chunk_count(total, self._accum.chunk_sec)
        start = self._accum.next_chunk
        if start >= n_chunks:
            tail_lo = start * step / 16000 + _CHUNK_OVERLAP_SEC / 2
            todo = [(start, True)] if total_sec > tail_lo else []
        else:
            todo = [(c, c == n_chunks - 1) for c in range(start, n_chunks)]
        for i, (c, is_last) in enumerate(todo):
            sub = None
            if progress is not None:
                def sub(f: float, i: int = i) -> None:
                    progress(0.9 * (i + f) / len(todo))
            seg = read(
                c * step / 16000, min((c * step + window) / 16000, total_sec),
            )
            self._accum.process_chunk(sd, seg, c, is_last=is_last, progress=sub)
        out = _labels_and_voiceprints(self._accum, num_speakers, read)
        if progress is not None:
            progress(1.0)
        return out
