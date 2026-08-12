r"""語音轉文字:引擎路由(CUDA → Intel GPU → CPU)+ faster-whisper 本體。

transcribe() 是唯一入口:依偵測結果挑路——CUDA/CPU 用本檔的
faster-whisper(內建 Silero VAD、condition_on_previous_text=False 防
幻覺),Intel GPU 轉交 transcribe_ov.py(openvino-genai)。「執行」失敗
安靜降級下一路;模型「下載」失敗(UserFacingError)直接浮出,分流理由
見 transcribe() docstring。

模型鍵 fast=large-v3-turbo、accurate=large-v3;首次使用從 HF 下載到
%LOCALAPPDATA%\meeting-scribe\models\whisper,快取完整後完全離線
(local-first,見 _model_dir)。每個解碼視窗都注入 data/hotwords.txt
領域詞(見 hotwords 模組 docstring)。

**轉錄語言固定中文**(LANGUAGE):曾有「語音語言」中/英選擇貫穿兩條
引擎路徑,使用者 2026-07-26 指定移除(代價:英文音檔會被強制往中文解碼
成品質不穩的「翻譯」;要恢復多語就是把 LANGUAGE 改回參數,兩條路徑都
從這個常數取值,不必再考古各自寫死的字面值)。
"""
import logging
import os
from collections.abc import Callable
from pathlib import Path

from meeting_scribe import cancel, hotwords, models
from meeting_scribe.errors import UserFacingError
from meeting_scribe.types import TranscriptSegment

# 轉錄語言(產品決定,非引擎細節):faster-whisper 直接吃,OV 路徑包成
# `<|zh|>` 特殊 token——兩條路徑必須一致,故只有這一個出處
LANGUAGE = "zh"

logger = logging.getLogger(__name__)

# 惰性載入(_ensure_whisper 首次使用才 import faster_whisper,連帶
# ctranslate2/tokenizers 原生庫——啟動路徑不付,首次轉檔才付,相對
# 轉檔本身可忽略)。測試 monkeypatch 本屬性換假引擎
WhisperModel = None


def _ensure_whisper():
    global WhisperModel
    if WhisperModel is None:
        from faster_whisper import WhisperModel as _real

        WhisperModel = _real
    return WhisperModel

MODEL_CHOICES = {"fast": "large-v3-turbo", "accurate": "large-v3"}

# faster-whisper download_model() 用的 HF repo 對應與檔案清單(1.2.1 實查)。
# 不經 download_model 而直呼 snapshot_download 的原因:download_model 寫死
# tqdm_class=disabled_tqdm,首次下載 1.5~3GB 期間黑視窗全程無進度,README
# 「下載進度顯示在黑色視窗」直接落空;直呼可用 hub 原生 tqdm 進度條。
# cache_dir 與 download_model 相同,既有使用者快取無縫沿用。
_HF_REPOS = {
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "large-v3": "Systran/faster-whisper-large-v3",
}
_ALLOW_PATTERNS = [
    "config.json",
    "preprocessor_config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.*",
]

# in-process memo / 快取:批次多檔免重複連網解析、免每檔重建模型
# (CPU 模型建構實測 ~2.5 秒/檔;OV 路徑另有 transcribe_ov._PIPE_CACHE)。
# _MODEL_CACHE 為單槽:快速↔精準切換時淘汰舊模型,CPU int8 兩顆同時
# 常駐是 1.5+3GB,基準機(8GB)吃不消
_model_dirs: dict[str, str] = {}
_MODEL_CACHE: dict[tuple[str, str], "WhisperModel"] = {}

ProgressFn = Callable[[float], None]

# 引擎前置成本(音檔解碼、全檔 VAD 掃描)在本引擎進度中的固定佔比:
# 這些工作沒有細粒度進度,但完成時要讓進度條動一下,否則長檔開頭
# 會長時間停在階段起點(transcribe_ov._PREP_FRAC 同義,OV 路徑用)
_PREP_FRAC = 0.05


def _cuda_available() -> bool:
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def _intel_gpu_available() -> bool:
    # 只看「OpenVINO 列得出 GPU 裝置」,不分強弱:效能驗證僅涵蓋標準機
    # Arc 140V;老款弱 iGPU(UHD 6xx 等)也會被路由到 OV-GPU,可能慢於
    # faster-whisper CPU 而不會自動降級(不拋錯就不降級)——已列 spec §11
    # 已知限制。不做 GPU 型號白名單:清單必然過時且誤殺(YAGNI)。
    #
    # 不問 NPU:標準機列得出 NPU(AI Boost)但本專案沒有任何運算跑在上面,
    # 偵測了也只能拿去顯示,而「本機偵測」那行講的是工作跑在哪
    # (2026-08-03 實測結案,見 scripts/bench_npu.py)。
    try:
        import openvino

        return any(d.startswith("GPU") for d in openvino.Core().available_devices)
    except Exception:
        return False


def gpu_available() -> bool:
    """轉錄是否會走 GPU 引擎(CUDA 或 Intel GPU)。

    pipeline 據此決定轉錄與講者分析要平行(異質資源,GPU+CPU)
    或依序(兩者同吃 CPU,平行只會過度訂閱且推高峰值記憶體)。"""
    return _cuda_available() or _intel_gpu_available()


def predicted_device() -> str:
    """啟動時預告轉錄將走的裝置(偵測結果,非執行保證;中途降級以實際為準)。"""
    if _cuda_available():
        return "cuda"
    if _intel_gpu_available():
        return "intel-gpu"
    return "cpu"


def default_model_key() -> str:
    """依偵測到的裝置挑預設模型:有 GPU 用 accurate、純 CPU 用 fast。

    判準只有這一份:介面(app.build_ui 的「模型」預設)與命令列
    (doccli 的 --model 預設)各寫一次的話,同一台機器會因為你從哪個入口
    進來而拿到不同品質的逐字稿,而且完全沒有跡象。理由是實測的——無 GPU
    機器上「精準」約慢 4 倍,有 GPU 時兩者總時間差不多(轉錄與講者分析
    平行,總時間 = 講者分析)。

    問的是 predicted_device 而不是 gpu_available:app 的「本機偵測」與
    欄位說明都由前者算出,兩邊問不同的函式就有機會出現「說明寫著沒有
    GPU、卻幫你選了精準」。"""
    return "accurate" if predicted_device() != "cpu" else "fast"


def _cpu_threads() -> int:
    # ctranslate2 預設不吃滿核心;執行緒數用使用者指定的核心數(與 diarize 同準則)
    from meeting_scribe import power

    return power.cpu_worker_count()


def _model_dir(name: str) -> str:
    """解析(必要時下載)CTranslate2 模型,回傳本地目錄路徑。

    local-first:快取完整時以 local_files_only 解析、完全不連網——兌現
    README「唯一的網路行為是第一次下載」,並消除批次每檔的 HF revision
    檢查延遲;快取不完整才連網下載(hub 原生 tqdm 進度條顯示於黑視窗)。
    下載失敗以繁中訊息浮出(spec §8),呼叫端不得安靜降級改抓其他模型。"""
    if name in _model_dirs:
        return _model_dirs[name]
    from huggingface_hub import snapshot_download

    repo = _HF_REPOS[name]
    kwargs = {"allow_patterns": _ALLOW_PATTERNS, "cache_dir": str(models.whisper_cache())}
    try:
        path = snapshot_download(repo, local_files_only=True, **kwargs)
    except Exception:
        try:
            print(f"首次使用需下載轉錄模型 {name}(約 1.5~3GB),進度如下:", flush=True)
        except Exception:
            pass  # 主控台顯示問題絕不能中止下載
        try:
            path = snapshot_download(repo, **kwargs)
        except Exception as e:
            raise UserFacingError(f"模型下載失敗,請確認網路連線後重試:{name}") from e
    _model_dirs[name] = path
    return path


def clear_engine_cache() -> None:
    """釋放已建構的模型實例。CPU 核心數變更時由 app 呼叫:執行緒數在
    引擎建構時定死,不清快取新值不會生效(diarize/punctuate 同款 API)。"""
    _MODEL_CACHE.clear()


def _load(model_dir: str, device: str) -> "WhisperModel":
    """建構(或沿用快取的)WhisperModel;單槽快取,換模型/裝置即淘汰舊的。"""
    key = (model_dir, device)
    if key not in _MODEL_CACHE:
        _MODEL_CACHE.clear()
        compute = "int8_float16" if device == "cuda" else "int8"
        kwargs = {"cpu_threads": _cpu_threads()} if device == "cpu" else {}
        _MODEL_CACHE[key] = _ensure_whisper()(
            model_dir, device=device, compute_type=compute, **kwargs
        )
    return _MODEL_CACHE[key]


def _run(
    model: "WhisperModel", wav_path: str | Path, progress: ProgressFn | None,
) -> list[TranscriptSegment]:
    """faster-whisper 實際解碼:逐窗產出帶時間戳的句子(空句略過)。"""
    segments, info = model.transcribe(
        str(wav_path),
        language=LANGUAGE,
        vad_filter=True,
        condition_on_previous_text=False,
        # 領域詞表逐窗注入,修正金融詞同音誤譯;用 hotwords 而非
        # initial_prompt——condition_on_previous_text=False 下,
        # initial_prompt 只影響第一個 30 秒視窗(見 hotwords 模組 docstring)
        hotwords=hotwords.as_string() or None,
    )
    # transcribe() 返回時音檔解碼與全檔 VAD 已完成(惰性的只有逐窗解碼):
    # 回報前置完成,長檔開頭進度條才不會長時間紋絲不動
    if progress:
        progress(_PREP_FRAC)
    out = []
    for s in segments:
        cancel.check()  # 停止響應點:逐窗解碼之間(生成器就地棄置)
        text = s.text.strip()
        if text:  # 空字串不輸出(與 transcribe_ov 的輸出契約一致)
            out.append(TranscriptSegment(s.start, s.end, text))
        if progress and info.duration:
            progress(_PREP_FRAC + (1 - _PREP_FRAC) * min(s.end / info.duration, 1.0))
    return out


def _transcribe_intel(
    wav_path: str | Path, model_dir: str, progress: ProgressFn | None,
) -> list[TranscriptSegment]:
    """Intel GPU 路徑的注入點(看似可內聯,實際有兩個理由):openvino 的
    惰性 import 隔離在此(不進啟動路徑);測試也 monkeypatch 這個名字來
    假裝有 Intel GPU。"""
    from meeting_scribe.transcribe_ov import transcribe_ov

    return transcribe_ov(wav_path, model_dir, progress=progress, device="GPU")


def transcribe(
    wav_path: str | Path,
    model_key: str = "fast",
    progress: ProgressFn | None = None,
) -> tuple[list[TranscriptSegment], str]:
    """回傳 (轉錄結果, 實際裝置)。三路自動選擇:CUDA → Intel GPU → CPU。

    失敗處理分流(spec §8):
    - 引擎「執行」失敗(CUDA DLL 缺失、OV 編譯失敗等)→ 安靜降級下一路;
    - 模型「下載」失敗(UserFacingError)→ 直接浮出明確提示重試——網路
      問題降級只會默默觸發另一場 1.5~3GB 下載再跑慢速路,不符降級初衷。
      模型解析置於 try 之外即為此分流的實作點。"""
    name = MODEL_CHOICES[model_key]
    if _cuda_available():
        model_dir = _model_dir(name)
        try:
            return _run(_load(model_dir, "cuda"), wav_path, progress), "cuda"
        except Exception:
            # 失敗的實例不得留在快取被下一檔重用(狀態可能已壞)
            _MODEL_CACHE.pop((model_dir, "cuda"), None)
            logger.warning("CUDA 執行失敗,嘗試下一個引擎", exc_info=True)
    if _intel_gpu_available():
        ov_dir = str(models.ov_whisper_dir(model_key))
        try:
            return _transcribe_intel(wav_path, ov_dir, progress), "intel-gpu"
        except Exception:
            logger.warning("Intel GPU(OpenVINO)執行失敗,降級 CPU", exc_info=True)
    model_dir = _model_dir(name)
    return _run(_load(model_dir, "cpu"), wav_path, progress), "cpu"
