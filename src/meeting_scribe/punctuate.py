"""標點還原(FunASR CT-Transformer,中英混合,地端執行)。

Whisper 的中文輸出幾乎無標點,對話式 md 的講者區塊合併後是整片
無標點長文,難以閱讀。以 sherpa-onnx 的 OfflinePunctuation 對
「合併後的講者區塊」補標點——長文脈絡下標點品質最好,也避免對
逐句短片段標點造成每兩秒一個句號的假斷句。

繁體輸入實測可直接使用(詞表 27 萬,含繁體;輸出保留原字元,
僅插入標點),故在 OpenCC 繁化「之後」執行,無需調整管線順序。
"""

import re

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


def _num_threads() -> int:
    # 標點模型小、單區塊毫秒級,4 緒已綽綽有餘(不與轉錄/講者分析搶滿核心);
    # 一併套用保留核心準則(見 power.cpu_worker_count)
    from meeting_scribe import power

    return power.cpu_worker_count(4)


def _get_punctuator() -> "sherpa_onnx.OfflinePunctuation":
    if _PUNCT_CACHE:
        return _PUNCT_CACHE[0]
    so = _ensure_sherpa()
    # 模型下載失敗(UserFacingError)在組 config 前就浮出,不會被下方
    # 「載入失敗」訊息誤蓋
    config = so.OfflinePunctuationConfig(
        model=so.OfflinePunctuationModelConfig(
            ct_transformer=str(models.punctuation_model()),
            num_threads=_num_threads(),
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
    """釋放標點引擎快取。CPU 核心數變更時由 app 呼叫:執行緒數在建構時
    定死,不清快取新值不會生效(transcribe/diarize 同款 API)。"""
    _PUNCT_CACHE.clear()


def add_punctuation(text: str) -> str:
    if not text.strip():
        return text
    # 先清掉 Whisper 既有句讀(避免疊字),重下標點後再把被模型拆開的
    # 英數 token 黏回(4 . 8 . 0 → 4.8.0)
    result = _get_punctuator().add_punctuation(_strip_existing_punct(text))
    return _reglue_tokens(result)
