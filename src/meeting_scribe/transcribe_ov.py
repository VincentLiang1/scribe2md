"""OpenVINO 轉錄引擎(同仁標準機 Arc GPU 路徑)。

幻覺防治 parity(spec §5):openvino-genai 無內建 VAD,
沿用 faster-whisper 內建 Silero VAD 先切出語音塊,靜音不送模型;
各 VAD 塊獨立解碼,塊與塊之間等效 condition_on_previous_text=False;
語言取 transcribe.LANGUAGE(單一出處,與 faster-whisper 路徑必然一致)。
parity 缺口與其緩解:單一塊超過 30 秒(長段連續發言)時,
WhisperPipeline 內部 sliding window 的跨窗上下文行為不可設定
(WhisperGenerationConfig 無 condition_on_prev 對應欄位,2026.2.1 實測),
實際踩到跳針(42 分鐘會議的 334 秒單塊解碼出 418 秒「包括資料,」×百次
迴圈)——防護:每塊輸出過 loopdetect 檢查,中招就在最安靜處切半遞迴
重轉(_generate_block),切到 _RETRY_MIN_SEC 仍中招才放棄,由 pipeline
輸出端以繁中標記交代(spec §11 的缺口從「未驗證」改為「已緩解」)。

API 備註(依 openvino-genai 2026.2.1.0 實測 introspect,見 task report):
- WhisperPipeline.generate() 回傳 WhisperDecodedResults,其 .chunks 為
  WhisperDecodedResultChunk 列表,欄位為 start_ts / end_ts / text(非
  start/end)。end_ts 可能為 -1(窗在句中切斷、無收尾時間戳的上游
  sentinel,型別 stub 未記載,timestamps.cpp 實查)——必須 clamp。
- 實測 WhisperPipeline 對 >30 秒輸入會自行做長音檔分段(sliding window),
  一次 generate() 呼叫即可回傳跨越全長、時間戳遞增的多個 chunks,故不需
  額外的「單塊上限秒數」二次切分。
"""

import logging
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np

from meeting_scribe import audio, cancel, hotwords, loopdetect, models
from meeting_scribe.transcribe import LANGUAGE
from meeting_scribe.types import TranscriptSegment

logger = logging.getLogger(__name__)

ProgressFn = Callable[[float], None]

# 心跳訊息的間隔。這個迴圈可以跑數十分鐘,不出聲就會被當成當機
# (2026-08-07 使用者實跡,見迴圈內註解);每分鐘一筆,369 塊也只有數十行
_HEARTBEAT_SEC = 60.0
# 剩餘時間只看「最近這段時間」跑多快。⚠️ **不可改回從頭到現在的平均**
# (2026-08-15,理由與 runstate._ETA_WINDOW_SHARE 同一件事):whisper 前幾塊
# 特別慢(GPU kernel 首次編譯、pipeline 暖機),把那段算進速率會讓開頭的
# 預估離譜到沒有意義——一支 3 小時 59 分的月會實測報「還要 214.5 分」,
# 而整段轉錄只花了 65 分。視窗 5 分鐘 = 5 筆心跳,夠平掉單塊的長短差異
_ETA_WINDOW_SEC = 300.0

# --- 跳針防護 ---
# 塊輸出中招(loopdetect)→ 切半重轉;短於此秒數×2 就不再切(再切窗太短、
# 且此規模的殘餘跳針交給 pipeline 輸出端標記)
_RETRY_MIN_SEC = 30.0
# 切點搜尋:中點 ± 此秒數內找 RMS 最低的 0.2 秒窗,避免正砍在字中間
_CUT_SEARCH_SEC = 5.0

_PAD_SEC = 0.2
# 打包參數:每次 generate() 都付一趟與音訊長短無關的固定 encoder 成本
# (開發機實測 ~2.4 秒/趟),破碎對話的 chunk 數是轉錄時間的直接乘數。
# gap 3 秒內合併(短靜音是 Whisper 的正常工作域,遠低於幻覺風險門檻)、
# 合併後不超過 28 秒(低於 30 秒視窗,打包不引入內部 sliding window)。
_MAX_GAP_SEC = 3.0
_MAX_CHUNK_SEC = 28.0

# 前置成本(全檔 VAD 掃描、pipeline 載入/GPU 編譯——首次編譯數十秒)在本
# 引擎進度中的固定佔比:都發生在第一塊完成前,不回報的話長檔開頭進度條
# 會長時間紋絲不動。VAD 完成回報一半、pipeline 就緒回報全額。
# (transcribe._PREP_FRAC 同義,CUDA/CPU 路徑用)
_PREP_FRAC = 0.05

# GPU 上 WhisperPipeline 編譯成本高(數十秒),批次多檔時以 (model_dir, device)
# 快取重用;單槽淘汰:快速↔精準切換時釋放舊模型(GPU 共享記憶體 0.8~1.7GB)。
# 程序運行中解構 pipeline 屬正常使用範疇;spec §11 已知的解構卡死
# 只發生在直譯器關閉期,與此無關。
_PIPE_CACHE: dict[tuple[str, str], object] = {}


def _get_pipe(model_dir: str, device: str):
    import openvino_genai

    key = (model_dir, device)
    if key not in _PIPE_CACHE:
        _PIPE_CACHE.clear()
        # CACHE_DIR:編譯 blob 落地重用,跨程序啟動免重編譯(_PIPE_CACHE 僅程序內有效)
        _PIPE_CACHE[key] = openvino_genai.WhisperPipeline(
            model_dir, device, CACHE_DIR=str(models.ov_compile_cache()),
        )
    return _PIPE_CACHE[key]


def _merge_speech_chunks(
    speech: list[dict], total_dur: float, pad: float, max_gap: float, max_len: float
) -> list[tuple[float, float]]:
    """VAD 區段加 padding 並打包合併,回傳 (start, end) 秒。

    合併條件:與前塊間隙 ≤ max_gap 且合併後長度 ≤ max_len。

    單一 VAD 區段本身超過 max_len(長段連續發言)在這裡不切——這個函式
    看不到取樣點、切不出好切點,由 `_split_long_chunks` 接手(它會在最
    安靜處切)。"""
    chunks: list[list[float]] = []
    for seg in speech:
        start = max(0.0, seg["start"] - pad)
        end = min(total_dur, seg["end"] + pad)
        if (
            chunks
            and start - chunks[-1][1] <= max_gap
            and end - chunks[-1][0] <= max_len
        ):
            chunks[-1][1] = max(chunks[-1][1], end)
        else:
            chunks.append([start, end])
    return [(s, e) for s, e in chunks]


def _quietest_near(samples: np.ndarray, lo: float, hi: float) -> float:
    """lo..hi(秒)之間 RMS 最低的 0.2 秒窗中心。

    切點不能亂砍:正砍在字中間會把該字轉壞。回傳值必定落在 (lo, hi) 之內,
    呼叫端可以據此保證迴圈會前進。"""
    sr = 16000
    seg = samples[int(lo * sr): int(hi * sr)]
    win, hop = int(0.2 * sr), int(0.05 * sr)
    if len(seg) <= win:
        return (lo + hi) / 2
    best_i, best_rms = 0, float("inf")
    for i in range(0, len(seg) - win, hop):
        rms = float(np.sqrt(np.mean(np.square(seg[i: i + win]))))
        if rms < best_rms:
            best_i, best_rms = i, rms
    return lo + (best_i + win / 2) / sr


def _quietest_cut(samples: np.ndarray, start: float, end: float) -> float:
    """start..end 的**中點**附近最安靜的切點(跳針重轉切半用)。"""
    mid = (start + end) / 2
    return _quietest_near(
        samples, max(start, mid - _CUT_SEARCH_SEC), min(end, mid + _CUT_SEARCH_SEC),
    )


def _split_long_chunks(
    samples: np.ndarray, chunks: list[tuple[float, float]], max_len: float,
) -> list[tuple[float, float]]:
    """把單塊超過 max_len 的切開,切點取最安靜處(2026-08-04)。

    **這是跳針的根因處理**。`_merge_speech_chunks` 的合併上限只管「合併後
    不超過 max_len」,單一 VAD 區段本身超長時整塊原樣送進 generate()
    ——會議的連續發言正是這樣:實測一場 64 分鐘的真實會議,VAD 併出
    243.9 / 183.7 / 157.9 秒的單塊,而 >30 秒的塊會走進 WhisperPipeline
    內部的 sliding window,跨窗上下文把重複帶著走,那正是跳針的溫床
    (同一場觸發 7 次,其中 3 段連切半重轉都救不回、只能在逐字稿留標記,
    合計約 92 秒的內容變成一句「此段轉錄異常」)。

    原本刻意不切的理由是「句中硬切會斬斷語音」,但那個顧慮 `_quietest_near`
    已經解掉了——A 層(`_generate_block`)的切半重轉用的就是同一招,而且
    是已經在生產路徑上驗證過的。差別只在:**與其等跳針了再切,不如一開始
    就別送超過 30 秒的塊進去**。

    **上限就用 `_MAX_CHUNK_SEC`(28 秒),而且千萬不要「覺得太碎」調大**。
    拿那場會議真的出過事的六段(共 21 分鐘)實測四種上限:

        上限    救不回      乾淨字   耗時
        不切    2 段 36 秒   4883    269 秒
        90 秒   2 段 30 秒   4945    238 秒
        60 秒   4 段 109 秒  4652    195 秒   ← 最糟
        28 秒   1 段 28 秒   5264    198 秒   ← 最好

    28 秒在六段裡有五段不輸,而且**比不切還快**(198 vs 269 秒)——切碎多付
    的 encoder 固定成本,遠少於省下來的「跳針後整塊重轉」。

    ⚠️ **60 秒是個陷阱**:塊長 59 秒剛好卡在 A 層的重轉門檻
    (`end - start >= _RETRY_MIN_SEC * 2` = 60 秒)之下——長到會跳針、又短到
    救不回,所以救不回的秒數反而是不切的三倍。要調的話只能往「明顯低於
    30 秒」或「明顯高於 60 秒」兩邊走,中間那段別碰。

    **往回找切點**(搜尋窗是 `[max_len - 2×_CUT_SEARCH_SEC, max_len]`)而不是
    以目標點為中心:以中心搜尋的話切點可能落在 max_len + 搜尋窗,又超過
    30 秒、等於白切。代價是每塊平均短一點(18~28 秒)。"""
    out: list[tuple[float, float]] = []
    for start, end in chunks:
        s = start
        while end - s > max_len:
            cut = _quietest_near(
                samples, s + max_len - 2 * _CUT_SEARCH_SEC, s + max_len,
            )
            out.append((s, cut))
            s = cut  # _quietest_near 必定回傳 > lo,迴圈保證前進
        out.append((s, end))
    return out


def _generate_block(
    pipe, config, samples: np.ndarray, start: float, end: float
) -> list[TranscriptSegment]:
    """解碼單一 VAD 塊;輸出跳針(重複迴圈)時於最安靜處切半、遞迴重轉。

    切半有效的原因:跳針好發於 >30 秒單塊的內部 sliding window(跨窗上下文
    把重複帶著走),切短後各半獨立解碼、上下文歸零;實際案例中前半的正常
    內容也因此救得回來(跳針段常是「前段正常、中途開始迴圈」)。"""
    cancel.check()  # 停止響應點:每次(重)解碼之前
    block = samples[int(start * 16000): int(end * 16000)]
    result = pipe.generate(block, config)
    segs: list[TranscriptSegment] = []
    for c in result.chunks or []:
        text = c.text.strip()
        if not text:
            continue
        seg_start = start + max(c.start_ts, 0.0)
        # end_ts == -1 sentinel:clamp 到該塊終點,否則產出 end < start 的
        # 壞區段(SRT 垃圾時間戳、講者配對掛錯人)
        seg_end = start + c.end_ts if c.end_ts >= 0.0 else end
        segs.append(TranscriptSegment(seg_start, max(seg_end, seg_start), text))
    joined = "".join(s.text for s in segs)
    degenerate = (
        any(loopdetect.is_degenerate(s.text) for s in segs)
        or loopdetect.is_degenerate(joined)  # 迴圈散在多個 chunk 時合併才看得出
    )
    if degenerate and end - start >= _RETRY_MIN_SEC * 2:
        cut = _quietest_cut(samples, start, end)
        logger.info(
            "轉錄跳針偵測(%.0f~%.0f 秒):於 %.1f 秒切半重轉", start, end, cut,
        )
        return (
            _generate_block(pipe, config, samples, start, cut)
            + _generate_block(pipe, config, samples, cut, end)
        )
    return segs


def transcribe_ov(
    wav_path: str | Path,
    model_dir: str,
    progress: ProgressFn | None = None,
    device: str = "GPU",
) -> list[TranscriptSegment]:
    """model_dir:已解析的本地模型目錄(下載由呼叫端負責——下載失敗與
    引擎執行失敗的錯誤處理不同,見 transcribe.transcribe docstring)。"""
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    samples = audio.read_wav16k(wav_path)
    total_dur = len(samples) / 16000.0
    speech = get_speech_timestamps(samples, VadOptions())
    if progress:
        progress(_PREP_FRAC / 2)  # 全檔 VAD 掃描完成
    # get_speech_timestamps 回傳 start/end 為 sample index(非秒),須換算
    speech_sec = [{"start": s["start"] / 16000.0, "end": s["end"] / 16000.0} for s in speech]
    chunks = _merge_speech_chunks(speech_sec, total_dur, _PAD_SEC, _MAX_GAP_SEC, _MAX_CHUNK_SEC)
    # 超長的單塊在最安靜處切開:同一個 30 秒門檻,合併與切分兩邊都要守
    chunks = _split_long_chunks(samples, chunks, _MAX_CHUNK_SEC)
    if not chunks:
        return []
    speech_total = sum(e - s for s, e in chunks)
    # 268V 打包參數驗證用:真實會議跑一次即可從主控台 log 讀到分佈
    logger.info(
        "VAD 切塊統計:%d 塊,語音共 %.0f 秒(全長 %.0f 秒),最長單塊 %.1f 秒",
        len(chunks), speech_total, total_dur, max(e - s for s, e in chunks),
    )

    pipe = _get_pipe(model_dir, device)
    if progress:
        progress(_PREP_FRAC)  # pipeline 載入/編譯完成
    config = pipe.get_generation_config()
    config.language = f"<|{LANGUAGE}|>"  # OV 吃特殊 token 形式,值同 faster-whisper
    config.task = "transcribe"
    config.return_timestamps = True
    # 領域詞表逐窗注入:OV 的 hotwords 與 faster-whisper 語意一致
    # (<|startofprev|> 前文、all processing windows,API 文件實查),
    # 此為 parity 項目而非缺口。空詞表不動 config(維持引擎預設 None)
    hw = hotwords.as_string()
    if hw:
        config.hotwords = hw

    out: list[TranscriptSegment] = []
    done = 0.0
    t0 = last_beat = time.monotonic()
    marks: list[tuple[float, float]] = [(t0, 0.0)]  # 速率取樣,見 _remaining_sec
    for i, (start, end) in enumerate(chunks):
        # 單塊解碼(官方 chunks 型別為 list | None,None 已在內處理);
        # 跳針時塊內自行切半重轉,進度仍以原始塊回報(重轉是罕見路徑,
        # 期間進度短暫停格可接受,黑視窗有 log)
        out.extend(_generate_block(pipe, config, samples, start, end))
        done += end - start
        if progress:
            # 時長加權(非塊數):塊長度差異大時塊數進度嚴重失真
            progress(_PREP_FRAC + (1 - _PREP_FRAC) * done / speech_total)
        # 心跳:整支 2 小時錄音的轉錄要跑數十分鐘,而這個迴圈原本**一行
        # log 都不寫**——黑視窗最後停在「VAD 切塊統計」那行不動,看起來
        # 就是當機。使用者 2026-08-07 因此連關了兩次程式重來(實際上它
        # 一直好好地在跑)。現場收音那條路一直有「背景轉錄…耗時 X 秒」
        # 的訊息,所以從來沒有人覺得它死掉。
        # 每分鐘一筆(不是每塊):369 塊逐塊印會把紀錄檔洗掉
        now = time.monotonic()
        if now - last_beat >= _HEARTBEAT_SEC:
            last_beat = now
            spent = now - t0
            marks.append((now, done))
            cutoff = now - _ETA_WINDOW_SEC
            # 留住視窗外的最後一筆當基準(同 runstate._sample_locked 的 ⚠️)
            while len(marks) > 1 and marks[1][0] <= cutoff:
                marks.pop(0)
            left = _remaining_sec(marks, now, done, speech_total)
            logger.info(
                "轉錄進行中:%d/%d 塊、已處理 %.0f/%.0f 秒音訊,"
                "已花 %.1f 分,預估還要 %.1f 分",
                i + 1, len(chunks), done, speech_total, spent / 60, left / 60,
            )
    logger.info(
        "轉錄完成:%d 塊、%.0f 秒音訊,耗時 %.1f 分",
        len(chunks), done, (time.monotonic() - t0) / 60,
    )
    return out


def _remaining_sec(
    marks: list[tuple[float, float]], now: float, done: float, total: float,
) -> float:
    """依取樣視窗內的速率估剩餘秒數;算不出來回 0.0(不猜)。

    `marks` 是 [(時刻, 已處理音訊秒數)],呼叫端已按 `_ETA_WINDOW_SEC`
    修剪。**用音訊秒數而不是塊數**:塊長差異大,塊數進度嚴重失真。"""
    t_ref, done_ref = marks[0]
    gained, span = done - done_ref, now - t_ref
    if gained <= 0 or span <= 0:
        return 0.0
    return max(total - done, 0.0) * span / gained
