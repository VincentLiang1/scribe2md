r"""AI 模型的下載與快取——整個工具唯一會連網的模組,且只在快取不完整時。

- sherpa-onnx 三模型(講者切分 pyannote/聲紋 3D-Speaker/標點 CT-Transformer):
  GitHub release 直下;download()/_extract_onnx_member 附大小驗證與
  「.part 暫名 → 原子改名」,中斷不會留下假快取
- OpenVINO 預轉換 Whisper(Intel GPU 路徑):HF snapshot_download,
  _ov_cache_complete 按名點驗必要檔案組(防「小檔到位、大 bin 沒到」)
- faster-whisper 的 CTranslate2 模型下載在 transcribe._model_dir
  (走 HF hub 原生 tqdm,黑視窗看得到進度)

模型快取都在 %LOCALAPPDATA%\meeting-scribe\models(paths.appdata_root
之下,不進 repo);data_dir() 則是專案 data/——名單/聲紋/詞表等小型
資料檔,隨程式碼版控/複製。
"""
import logging
import shutil
import tarfile
import time
import urllib.request
from pathlib import Path

from meeting_scribe import paths
from meeting_scribe.errors import UserFacingError

logger = logging.getLogger(__name__)

SEGMENTATION_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
# 上游 release tag 拼字即為 recongition,勿「修正」
EMBEDDING_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_eres2netv2_sv_zh-cn_16k-common.onnx"
# sherpa 內部聚類專用的輕量聲紋模型(3D-Speaker CAM++,28MB)。
# 只為了讓 sherpa 切得出 turn 邊界——它抽的聲紋我們全部丟棄,登記進
# 聲紋庫的向量一律仍用上面的 eres2netv2(見 diarize._get_diarizer)
INTERNAL_EMBEDDING_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"
# 標點模型(FunASR CT-Transformer 中英混合,int8 量化 ~61MB 下載);
# 詞表內嵌於 onnx metadata,單檔即可用(實測),不需壓縮包內其餘檔案
PUNCTUATION_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/punctuation-models/sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12-int8.tar.bz2"

_MIN_MODEL_BYTES = 1_000_000
# 進度回報:每 10% 或每 15 秒印一行,兩個條件先到先算。純百分比在大檔案
# 上會有好幾分鐘完全沒輸出(見 download 的 _hook)
_PROGRESS_PCT_STEP = 10
_PROGRESS_MIN_SEC = 15.0

_OV_REPOS = {
    "fast": "OpenVINO/whisper-large-v3-turbo-int8-ov",
    "accurate": "OpenVINO/whisper-large-v3-int8-ov",
}

# OV 預轉換 repo 的必要檔案組:四組 xml(結構)+ bin(權重)缺一不可。
# 8 執行緒平行下載被中斷時常見「小檔(tokenizer 等)已落地、大 bin 未完成」,
# 籠統的 any(xml)+any(bin) 會把這種殘缺快取誤判為完整 → WhisperPipeline
# 載入失敗且被降級鏈吞掉,使用者永遠看不到真正原因——必須按名成對點驗
_OV_REQUIRED_STEMS = (
    "openvino_encoder_model",
    "openvino_decoder_model",
    "openvino_tokenizer",
    "openvino_detokenizer",
)

# in-process memo:批次多檔時免重複做完整性點驗與(潛在的)連網解析
_ov_dirs: dict[str, Path] = {}


def cache_dir() -> Path:
    d = paths.appdata_root() / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def data_dir() -> Path:
    """專案 data/ 子目錄:放聲紋庫、與會名單等小型資料,隨程式碼一起版控/複製
    (不同於 models 快取放在使用者目錄、不進版控)。"""
    d = paths.repo_root() / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def seed_dir() -> Path:
    r"""出廠預設的資料檔。**只有交付給使用者的那一份裡才有這個目錄**——
    開發用的 repo 沒有它(`data\` 本身就是本尊),所以這裡回傳的路徑
    在開發機上一律不存在,seed_missing() 會直接空手而回。"""
    return paths.repo_root() / "data-default"


def seed_missing() -> list[str]:
    r"""把 `data\` 裡**缺少**的檔案從出廠預設補上,回傳補了哪些檔名。

    這是「交付只出一個 zip」的整個機制(見 docs/spec/12 的打包規格)。
    打包時 `data\` 改名成 `data-default\` 放進 zip,使用者解壓後:

    - 第一次安裝:`data\` 是空的 → 四個檔在這裡就位,體驗與開發機一致
    - 解壓覆蓋更新:`data\` 裡的檔都在 → **一個都不碰**

    ⚠️ **已存在就絕不覆蓋,這是整支函式唯一重要的性質。** 那四個檔是
    使用者在自己機器上會寫的東西(替講者命名會登記聲紋,三個維護分頁
    都能編輯存檔),蓋掉等於清空他一次一次累積出來的聲紋庫——而且他
    不會立刻發現:症狀是「以前認得出來的人現在認不出來了」,查起來會
    往分群的方向走,離真正的原因很遠。

    代價要知道:反過來說,**產品端更新了 `replace.txt`/`hotwords.txt`
    也不會下發給已經在用的人**(他的檔已存在)。要更新那兩個檔就單獨
    傳、請他自己放進去。

    先寫 `.part` 再原子改名,與 download() 同樣的理由:中途失敗(磁碟滿、
    防毒鎖檔)只會留下一個 .part,不會讓半個 `voiceprints.npz` 落地
    ——那會讓 np.load 直接拋例外,而檔案「看起來是在的」。
    """
    src = seed_dir()
    if not src.is_dir():
        return []
    dst = data_dir()
    filled: list[str] = []
    for f in sorted(src.iterdir()):
        if not f.is_file():
            continue
        target = dst / f.name
        if target.exists():
            continue
        tmp = target.with_name(target.name + ".part")
        try:
            shutil.copy2(f, tmp)
            tmp.replace(target)
        except OSError as e:
            # 不擋啟動:少一個資料檔只是那項功能等同關閉(四個載入函式
            # 都對「檔案不存在」有優雅退化),擋掉啟動則是整個工具不能用
            logger.warning("補不上出廠預設的「%s」:%s", f.name, e)
            tmp.unlink(missing_ok=True)
            continue
        filled.append(f.name)
    if filled:
        logger.info("已從出廠預設補上資料檔:%s", "、".join(filled))
    return filled


def download(
    url: str, dest: Path, min_bytes: int = _MIN_MODEL_BYTES, what: str = "模型",
) -> Path:
    """下載單一檔案(已存在且過大小門檻直接沿用);.part 暫名+原子改名,
    中斷只殘留 .part、不會產生假快取;進度印到黑視窗。

    `what` 只影響給使用者看的字。這支函式也被 soffice 拿去抓 LibreOffice
    安裝檔,那時一句「模型下載失敗」會讓人完全找不到方向。"""
    if dest.exists() and dest.stat().st_size >= min_bytes:
        return dest
    tmp = dest.with_suffix(dest.suffix + ".part")
    last_pct = -_PROGRESS_PCT_STEP
    last_at = 0.0

    def _hook(blocks: int, block_size: int, total: int) -> None:
        # 進度印到主控台(黑色視窗),兌現 README「下載進度顯示在黑色視窗」。
        # **每 N 秒也要印一次,不能只按百分比**:LibreOffice 是 372MB,在慢的
        # 鏡像站上(實測約 150 KB/s)光是跑到 10% 就要四分鐘——第一行「0%」
        # 瞬間出現、之後四分鐘鴉雀無聲,使用者合理地判斷成當掉了
        # (2026-08-01 回報「都卡在 0%」,而 .part 檔其實一直在長)。
        # 同理帶上 MB 數:大檔案時「10%」給不出「還要多久」的感覺
        nonlocal last_pct, last_at
        if total <= 0:
            return
        done = blocks * block_size
        pct = min(done * 100 // total, 100)
        now = time.monotonic()
        if pct < last_pct + _PROGRESS_PCT_STEP and now - last_at < _PROGRESS_MIN_SEC:
            return
        last_pct, last_at = pct, now
        try:
            print(  # noqa: T201 - 黑視窗的進度回饋
                f"下載{what} {dest.name}:{pct}%"
                f"({min(done, total) / 1048576:.0f}/{total / 1048576:.0f} MB)",
                flush=True,
            )
        except Exception:
            # 進度顯示是 best-effort:主控台編碼等顯示問題
            # 絕不能讓 urlretrieve 中止下載、誤報成下載失敗
            pass

    try:
        urllib.request.urlretrieve(url, tmp, reporthook=_hook)
        if tmp.stat().st_size < min_bytes:
            raise UserFacingError(f"下載的檔案過小,可能下載失敗,請重試:{url}")
    except UserFacingError:
        tmp.unlink(missing_ok=True)
        raise
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise UserFacingError(f"{what}下載失敗,請確認網路連線後重試:{url}") from e
    tmp.replace(dest)
    return dest


def segmentation_model() -> Path:
    """講者切分模型(pyannote segmentation,sherpa-onnx 用)。"""
    return _extract_onnx_member(
        SEGMENTATION_URL, "model.onnx", cache_dir() / "pyannote-segmentation-3-0.onnx"
    )


def embedding_model() -> Path:
    """聲紋模型(3D-Speaker eres2netv2,中文 16k)。**聲紋庫的向量都出自
    這顆**——換模型等於讓 data/voiceprints.npz 整個失效,勿輕易更動。"""
    return download(EMBEDDING_URL, cache_dir() / "3dspeaker-eres2netv2-zh.onnx")


def internal_embedding_model() -> Path:
    """sherpa 內部聚類用的輕量聲紋模型(3D-Speaker CAM++,中文 16k)。

    只影響「turn 邊界怎麼切」,抽出的聲紋一律丟棄(誰是誰由本專案自己
    用 embedding_model() 重抽+重聚決定)——所以這裡換輕的,聲紋庫不受
    影響。2026-07-29 三份真實錄音實測:切分+抽聲紋快 2.3~2.7 倍,指定
    人數模式下講者群數完全相同、標籤一致率 92~99%,切分純度判準
    (加權平均信心)持平,詳見 scripts/bench_diarize.py 檔頭。"""
    return download(INTERNAL_EMBEDDING_URL, cache_dir() / "3dspeaker-campplus-zh.onnx")


def _extract_onnx_member(archive_url: str, member_suffix: str, target: Path) -> Path:
    """從 tar.bz2 抽出單一 onnx(暫名寫入 → 原子改名;與 download() 同法)。

    target 檔名存在且過大小門檻即代表解壓完整;大小門檻是第二道防線,
    不是唯一防線。"""
    if target.exists() and target.stat().st_size >= _MIN_MODEL_BYTES:
        return target
    # 壓縮檔案大小不可預期(取決於壓縮率),不能拿來套用模型最小檔案大小門檻;
    # 真正需要驗證的是解壓後的 onnx 檔案大小,見下方檢查。
    archive = download(archive_url, target.with_suffix(".tar.bz2"), min_bytes=1)
    part = target.with_suffix(target.suffix + ".part")
    try:
        with tarfile.open(archive, "r:bz2") as tar:
            member = next(
                (m for m in tar.getmembers() if m.name.endswith(member_suffix)), None
            )
            if member is None:
                raise UserFacingError(
                    f"下載的壓縮檔內容不符預期(缺少 {member_suffix}),請重試:{archive_url}"
                )
            member.name = part.name
            tar.extract(member, target.parent, filter="data")
        if part.stat().st_size < _MIN_MODEL_BYTES:
            raise UserFacingError(
                f"解壓後的模型檔案過小,可能下載失敗,請重試:{archive_url}"
            )
        part.replace(target)  # 原子改名:中斷只會殘留 .part,不會產生假快取
    finally:
        archive.unlink(missing_ok=True)
        part.unlink(missing_ok=True)
    return target


def punctuation_model() -> Path:
    """標點模型(FunASR CT-Transformer 中英混合,int8)。"""
    return _extract_onnx_member(
        PUNCTUATION_URL, "model.int8.onnx", cache_dir() / "punct-ct-transformer-zh-en-int8.onnx"
    )


def ov_compile_cache() -> Path:
    """OpenVINO 編譯 blob 快取目錄(CACHE_DIR):讓每次程序啟動後的
    首次轉檔免付數十秒 GPU 重編譯成本。"""
    d = cache_dir() / "ov" / "compile_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def whisper_cache() -> Path:
    d = cache_dir() / "whisper"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ov_cache_complete(target: Path) -> bool:
    return all(
        (target / f"{stem}.xml").exists() and (target / f"{stem}.bin").exists()
        for stem in _OV_REQUIRED_STEMS
    )


def ov_whisper_dir(model_key: str) -> Path:
    """下載(或沿用快取)OpenVINO 預轉換 Whisper 模型目錄。

    快取完整時完全離線、不連網——兌現 README「唯一的網路行為是第一次
    下載模型」(snapshot_download 線上時每次都會打 HF 做 revision 檢查),
    也消除批次多檔的每檔連網延遲與慢網路 stall。
    下載失敗以繁中訊息浮出;呼叫端(transcribe)不得把它安靜降級成
    「改抓另一個模型」(spec §8)。
    """
    if model_key in _ov_dirs:
        return _ov_dirs[model_key]
    repo = _OV_REPOS[model_key]
    target = cache_dir() / "ov" / repo.split("/")[-1]
    if not _ov_cache_complete(target):
        from huggingface_hub import snapshot_download

        try:
            snapshot_download(repo, local_dir=target)  # 原生支援中斷續傳
        except Exception as e:
            raise UserFacingError(f"模型下載失敗,請確認網路連線後重試:{repo}") from e
        if not _ov_cache_complete(target):
            # 防呆:下載「成功」但必要檔案組不齊(上游 repo 結構改變等)
            raise UserFacingError(f"模型下載內容不完整,請重試:{repo}")
    _ov_dirs[model_key] = target
    return target
