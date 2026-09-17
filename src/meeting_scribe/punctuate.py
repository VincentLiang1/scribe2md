"""標點還原(FunASR CT-Transformer,中英混合,地端執行)。

Whisper 的中文輸出幾乎無標點,對話式 md 的講者區塊合併後是整片
無標點長文,難以閱讀。以 sherpa-onnx 的 OfflinePunctuation 對
「合併後的講者區塊」補標點——長文脈絡下標點品質最好,也避免對
逐句短片段標點造成每兩秒一個句號的假斷句。

繁體輸入實測可直接使用(詞表 27 萬,含繁體;輸出保留原字元,
僅插入標點),故在 OpenCC 繁化「之後」執行,無需調整管線順序。
"""

import re
import threading
from concurrent.futures import ThreadPoolExecutor

from meeting_scribe import models
from meeting_scribe.errors import UserFacingError

# 惰性載入(_ensure_sherpa,委給 diarize 統一處理 DLL 預載;首次使用
# 才 import,啟動不付原生庫成本)。測試 monkeypatch 本屬性換假貨
sherpa_onnx = None


def _ensure_sherpa():
    global sherpa_onnx
    if sherpa_onnx is None:
        from meeting_scribe import diarize

        sherpa_onnx = diarize._ensure_sherpa()
    return sherpa_onnx


# 單例快取:引擎建構要重讀 onnx,批次多檔/多區塊重用(與 diarize 同準則)
_PUNCT_CACHE: list["sherpa_onnx.OfflinePunctuation"] = []
# ⚠️ 建引擎要上鎖:add_punctuation_many 會從好幾條執行緒同時進 _get_punctuator,
# 快取還空著時不鎖就各建一顆(每顆 ~105MB,而且之後只有一顆留在快取裡)
_BUILD_LOCK = threading.Lock()

# Whisper 有時已在輸出裡帶了句讀,直接餵給標點模型會疊出「是嗎??。」「工作。?我」
# ——標點模型把既有標點當一般 token,又在旁邊插自己的。做法:餵之前先清掉
# Whisper 的句讀,交給模型從乾淨文字統一重下(這正是它的訓練分佈)。
# 保護 token 內的 . - : /(4.8.0 / 7-11 / C-Sharp / 3:1):它們兩側都是英數,
# 不是句讀,清掉會毀掉版本號與產品名。
_STRIP_INTRA = re.compile(r"(?<![0-9A-Za-z])[.\-:/]|[.\-:/](?![0-9A-Za-z])")
_STRIP_MARKS = re.compile(r"[，。、；：！？?!,;…—―～]")
# 標點模型會在英數之間的 . - : / 兩側塞空格(4.8.0 → 4 . 8 . 0、C-Sharp → C - Sharp);
# 用零寬度斷言只刪「英數與符號之間」的空格,連續情況(4 . 8 . 0)也一次黏回。
_REGLUE = re.compile(r"(?<=[0-9A-Za-z]) +(?=[.\-:/])|(?<=[.\-:/]) +(?=[0-9A-Za-z])")


def _strip_existing_punct(text: str) -> str:
    return _STRIP_MARKS.sub("", _STRIP_INTRA.sub("", text))


def _reglue_tokens(text: str) -> str:
    return _REGLUE.sub("", text)


# 引擎內部(ONNX intra-op)的執行緒數。⚠️ **是 1 不是多**,平行放在「區塊之間」
# (2026-09-17 實測,89 分鐘會議的 302 個區塊、2.3 萬字):每個區塊都很短,
# 單次推論裡的 4 緒幾乎沒有東西可分(1 緒依序 5.1 秒、4 緒依序 4.3 秒);
# 改成一顆 1 緒引擎、多條執行緒同時各跑一個區塊,4 條 1.4 秒、8 條 0.8 秒,
# 10 輪輸出與依序**逐字相同**。引擎內 2 緒 × 4 條(1.3 秒)與 1 緒差不多,
# 多開只是與外層搶核心
_ENGINE_THREADS = 1


def _workers() -> int:
    """同時跑幾個區塊:使用者指定的核心數(保留核心準則見 power.cpu_worker_count)。

    ⚠️ **不再有「上限 4」**:那是依序跑、引擎內部分執行緒時代的上限(單區塊
    毫秒級、多給也用不到)。現在每條執行緒各吃一個區塊,核心數就是加速倍數的
    上限;而標點只在轉錄與講者分析**都做完之後**才跑,不與它們搶核心。"""
    from meeting_scribe import power

    return power.cpu_worker_count()


def _get_punctuator() -> "sherpa_onnx.OfflinePunctuation":
    with _BUILD_LOCK:
        if _PUNCT_CACHE:
            return _PUNCT_CACHE[0]
        so = _ensure_sherpa()
        # 模型下載失敗(UserFacingError)在組 config 前就浮出,不會被下方
        # 「載入失敗」訊息誤蓋
        config = so.OfflinePunctuationConfig(
            model=so.OfflinePunctuationModelConfig(
                ct_transformer=str(models.punctuation_model()),
                num_threads=_ENGINE_THREADS,
            )
        )
        try:
            punct = so.OfflinePunctuation(config)
        except Exception as e:
            raise UserFacingError(
                "標點模型載入失敗,模型檔可能已損壞:請刪除 "
                r"%LOCALAPPDATA%\meeting-scribe\models 資料夾後重試(會重新下載)"
            ) from e
        _PUNCT_CACHE.append(punct)
        return punct


def ensure_ready() -> None:
    """下載並載入標點模型。pipeline 在轉檔階段先呼叫:模型問題要在
    最初幾秒浮出,不能等 30 分鐘轉錄完才在輸出階段炸掉。"""
    _get_punctuator()


def clear_engine_cache() -> None:
    """釋放標點引擎快取(transcribe/diarize 同款 API,CPU 核心數變更時一起叫)。

    引擎內部固定 1 緒、平行度在 add_punctuation_many 呼叫當下才讀核心數,所以
    改核心數其實不必重建;留著這支是讓三顆引擎的清快取時機一致。"""
    with _BUILD_LOCK:
        _PUNCT_CACHE.clear()


def add_punctuation(text: str) -> str:
    if not text.strip():
        return text
    # 先清掉 Whisper 既有句讀(避免疊字),重下標點後再把被模型拆開的
    # 英數 token 黏回(4 . 8 . 0 → 4.8.0)
    result = _get_punctuator().add_punctuation(_strip_existing_punct(text))
    return _reglue_tokens(result)


def add_punctuation_many(texts: list[str]) -> list[str]:
    """一批區塊一起補標點,**回傳順序與輸入相同**。

    **同一顆引擎給好幾條執行緒同時用**,不是一條執行緒一顆:sherpa-onnx 的
    `AddPunctuation` 是 const、底下 `Forward` 只讀 session 與名稱表,而
    ONNX Runtime 的 `Session::Run` 本身可併發;呼叫期間放開 GIL(實測平行真的
    變快)。每多一顆引擎要多 ~105MB,8GB 基準機付不起;共用一顆則與改版前
    同樣大(實測 +104MB)。數據見 _ENGINE_THREADS。

    ⚠️ **執行緒裡叫的是模組層的 add_punctuation**(呼叫當下才查名字):測試
    monkeypatch `punctuate.add_punctuation` 換假貨時,這條路也要吃到假貨。
    ⚠️ **順序靠 `Executor.map`**(依輸入順序交回結果),換成 as_completed 之類
    的「誰先好誰先回」,md 裡每位講者的話就會對調,而字一個都沒少、看不出來。"""
    texts = list(texts)
    workers = min(_workers(), len(texts))
    if workers <= 1:
        return [add_punctuation(t) for t in texts]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(add_punctuation, texts))
