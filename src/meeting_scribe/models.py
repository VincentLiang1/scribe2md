r"""AI 模型的下載與快取——整個工具唯一會連網的模組,且只在快取不完整時。

- sherpa-onnx 三模型(講者切分 pyannote/聲紋 3D-Speaker/標點 CT-Transformer):
  GitHub release 直下;download()/_extract_onnx_member 附大小驗證與
  「.part 暫名 → 原子改名」,中斷不會留下假快取
- OpenVINO 預轉換 Whisper(Intel GPU 路徑):HF snapshot_download,
  _ov_cache_complete 按名點驗必要檔案組(防「小檔到位、大 bin 沒到」)
- faster-whisper 的 CTranslate2 模型下載在 transcribe._model_dir
  (進度經 hub_tqdm 接到 _PROGRESS_HOOK,畫面上看得到)

模型快取都在 %LOCALAPPDATA%\meeting-scribe\models(paths.appdata_root
之下,不進 repo);data_dir() 則是專案 data/——名單/聲紋/詞表等小型
資料檔,隨程式碼版控/複製。
"""
import logging
import shutil
import ssl
import tarfile
import time
import urllib.request
from pathlib import Path

from meeting_scribe import paths
from meeting_scribe.errors import UserFacingError
from meeting_scribe.i18n import t

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


def redownload_hint() -> str:
    r"""「模型檔可能壞了,刪掉重下」那句話——**全 repo 只有一份**(同
    `desktop.LOG_HINT` 的理由)。

    ⚠️ **印真實路徑,不可以寫死 `%LOCALAPPDATA%\meeting-scribe\models`**
    (2026-09-25 訂正):那個資料夾在 macOS 上根本不存在,而使用者看到的是
    一句「請刪除某個找不到的資料夾」——照著做的人會先困惑一輪,然後放棄。
    ⚠️ **不要在這裡 `mkdir`**:這是在講一個「請你去刪掉」的位置,順手把它
    建出來只會讓那句話更奇怪。"""
    return t("請刪除 {v} 資料夾後重試(會重新下載)", v=paths.appdata_root() / 'models')


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


# ---- HTTPS 憑證:公司網路有兩種壞法,各有各的解 ----
# 第一層(套件 __init__ 的 _trust_bundled_certificates)已把 certifi 疊上去,
# 治的是「Windows 的根憑證用到才補、Python 看不到」。這裡是第二層:公司代理
# 做 TLS 攔截(中間人)時,對方憑證由**公司自己的 CA** 簽——certifi 裡不可能
# 有,但那張 CA 一定裝在 Windows 憑證存放區(否則員工的瀏覽器也上不了網)。
# truststore 把驗證整個交給 Windows 原生 API,那種網路才連得出去。
# ⚠️ **不預先切換,只在真的撞到憑證錯誤時才換**:OS 原生驗證反過來會讓
# 「根憑證沒補到 + 群組原則關掉自動更新」的機器失去 certifi 這條活路,無條件
# 套用等於拿一種壞法換另一種。換完再失敗就還原,不留下比原本更糟的狀態。
_CERT_HINT = (
    t("這台電腦不信任下載網站的安全憑證(公司網路做連線檢查時最常見)。"
    "請把這句話轉給資訊人員:需要把公司的根憑證匯入 Windows 的"
    "「受信任的根憑證授權單位」。")
)

# None=還沒試過、True=已改用系統憑證、False=試過但沒用(不再重試)
_os_certs: bool | None = None


def _is_cert_error(e: BaseException) -> bool:
    """這個例外是不是「憑證驗不過」?

    ⚠️ **要走整條例外鏈**:原始的 ssl.SSLCertVerificationError 會被 urllib
    包成 URLError(藏在 args)、被 requests 包成自家的 SSLError(藏在 __cause__/
    __context__),直接 isinstance 判一層一定漏。字串比對是最後一道——第三方
    有時已經把例外換成純自家型別,只剩訊息還帶著 CERTIFICATE_VERIFY_FAILED。
    """
    seen: set[int] = set()
    cur: BaseException | None = e
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, ssl.SSLCertVerificationError):
            return True
        if any(isinstance(a, ssl.SSLCertVerificationError) for a in getattr(cur, "args", ())):
            return True
        cur = cur.__cause__ or cur.__context__
    return "CERTIFICATE_VERIFY_FAILED" in str(e)


def _use_os_certificates() -> bool:
    """改用作業系統原生的憑證驗證,回傳有沒有換成功。

    truststore.inject_into_ssl() 是行程級的:urllib(本模組與 soffice)、
    requests(huggingface_hub 下載 Whisper)、httpx 一起受惠,所以只要換一次。
    ⚠️ **兩個平台都成立**:truststore 在 Windows 走憑證存放區、在 macOS 走
    Security.framework,所以這一層不必分平台(只有訊息要講得通用)。
    """
    global _os_certs
    if _os_certs is not None:
        return _os_certs
    _os_certs = False
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:
        logger.warning("憑證驗證失敗,而且改不成系統憑證驗證", exc_info=True)
        return False
    _os_certs = True
    logger.info("改用作業系統的憑證存放區驗證連線(公司網路做連線檢查時需要)")
    return True


def _revert_os_certificates() -> None:
    """換了系統憑證還是驗不過:還原成原本的驗證方式。

    這個行程後面還有別的下載(Whisper 模型、LibreOffice),不還原等於讓
    certifi 本來走得通的路一起賠進去。"""
    global _os_certs
    try:
        import truststore

        truststore.extract_from_ssl()
    except Exception:
        logger.debug("還原憑證驗證方式失敗", exc_info=True)
    _os_certs = False  # 標記成「試過沒用」,不再重試


def with_tls_rescue(fn, what: str = ""):
    """跑 fn();若敗在「憑證驗不過」,改用系統憑證驗證再跑一次。

    ⚠️ **只重試憑證那一種錯**:網路不通、404、逾時重跑一次只是讓使用者
    多等一倍時間。"""
    try:
        return fn()
    except Exception as e:
        if not _is_cert_error(e) or not _use_os_certificates():
            raise
        logger.warning("%s憑證驗證失敗,改用系統憑證重試一次", what)
        try:
            return fn()
        except Exception as e2:
            if _is_cert_error(e2):
                _revert_os_certificates()
            raise


def download_failed(what: str, target: str, e: Exception) -> UserFacingError:
    """把下載例外翻成使用者看得懂的繁中訊息(spec §8)。

    ⚠️ **憑證錯誤不可混進「請確認網路連線」**:那台電腦的網路好得很,
    瀏覽器開得了同一個網址,照著訊息去檢查網路只會白花一小時——而真正
    要做的事(請 IT 匯入根憑證)完全不在那個方向上。"""
    if _is_cert_error(e):
        return UserFacingError(t("{what}下載失敗:{CERT_HINT}({target})", what=what, CERT_HINT=_CERT_HINT, target=target))
    return UserFacingError(t("{what}下載失敗,請確認網路連線後重試:{target}", what=what, target=target))


# 進度的第二個出口。⚠️ **原生視窗沒有黑視窗**(`Launch.vbs` 用 pythonw 起,主控台
# 整個藏起來),而模型第一次下載是 2~3 GB——只 `print` 的話那段進度**掉在地上**,
# 使用者看到的是一個十幾二十分鐘不動的畫面,合理地判斷成當掉了。
# ⚠️ **這是加一個出口、不是換掉 print**:除錯用的「Launch-DEBUG.bat」與命令列那條路
# 靠的正是主控台那一份。
# ⚠️ **會從工作執行緒被呼叫**(下載跑在轉檔那條執行緒上),所以掛進來的東西
# **不准直接碰 UI**——要自己丟回主執行緒(見 `desktop.App._model_progress`)。
_PROGRESS_HOOK = None


def set_progress_hook(fn) -> None:
    """讓下載進度多一個出口(`None` = 取消)。理由見 `_PROGRESS_HOOK`。"""
    global _PROGRESS_HOOK
    _PROGRESS_HOOK = fn


def report_progress(line: str, frac: float) -> None:
    """把一行下載進度交給 `_PROGRESS_HOOK`(沒掛就什麼都不做)。

    子行程轉回來的進度也走這裡(`transproc`/`diarproc`),所以畫面只有一個入口。
    ⚠️ **出任何差錯都不能中斷下載**:進度是 best-effort,下載才是正事。"""
    hook = _PROGRESS_HOOK
    if hook is None:
        return
    try:
        hook(line, frac)
    except Exception:  # noqa: BLE001
        logger.debug("下載進度回報失敗", exc_info=True)


def hub_tqdm(what: str):
    r"""給 `snapshot_download(tqdm_class=...)` 用的進度條:不印,改送 `report_progress`。

    ⚠️ **為什麼要有它**:Hugging Face 的下載進度原本只印在主控台,而原生視窗沒有
    主控台——2026-10-02 一台全新的 Mac 首次錄音,畫面停在**上一個檔案**(標點模型)
    的「100%」,而轉錄模型 1.6 GB 在背後下載了十幾分鐘,看起來就是當掉了。
    ⚠️ **只認位元組那一條,而且只認第一條**:hub 1.23 實測會建四條 `unit="B"` 的
    (Downloading bytes、Reconstructing…、兩條 complete)和一條「幾個檔案」的;
    Whisper 是「幾個小檔、其中一個 1.6 GB」,用檔案數回報會盯著 1/5 看十幾分鐘。
    最先建立的是 Downloading bytes,而它的 total 一開始是 0、下載開始後才填上。
    ⚠️ **覆寫 `display` 而不是 `update`**:節流與 `n`/`total` 的維護都在 tqdm 自己
    那一套裡;回傳 True 而不呼叫 super,那一行就不會被印出去。
    每次呼叫都回一個**新的類別**:狀態(誰是代表、上次報到哪)跟著這一次下載走。"""
    from tqdm.auto import tqdm as _base

    state = {"owner": None, "last_pct": -_PROGRESS_PCT_STEP, "last_at": 0.0}

    class _HubProgress(_base):
        def display(self, msg=None, pos=None):  # noqa: A002 - 對齊 tqdm 的簽章
            try:
                self._report()
            except Exception:  # noqa: BLE001 - 進度絕不能弄掉下載
                logger.debug("hub 進度回報失敗", exc_info=True)
            return True

        def _report(self) -> None:
            if getattr(self, "unit", None) != "B":
                return
            if state["owner"] is None:
                state["owner"] = self
            if state["owner"] is not self:
                return
            total, done = self.total or 0, self.n or 0
            if total <= 0:
                return
            pct = min(int(done * 100 // total), 100)
            now = time.monotonic()
            # 同 download():每 10% 或每 15 秒報一次,大檔案在慢網路上也不會鴉雀無聲
            if (pct < state["last_pct"] + _PROGRESS_PCT_STEP
                    and now - state["last_at"] < _PROGRESS_MIN_SEC):
                return
            state["last_pct"], state["last_at"] = pct, now
            line = (t("下載{what}:{pct}%({v:.0f}/{v2:.0f} MB)", what=what, pct=pct, v=min(done, total) / 1048576, v2=total / 1048576))
            # 記錄檔也留一份:使用者回報「卡住」時,這是判斷「還在下載」的唯一依據
            logger.info("%s", line)
            report_progress(line, pct / 100)

    return _HubProgress


def download(
    url: str, dest: Path, min_bytes: int = _MIN_MODEL_BYTES, what: str = t("模型"),
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
        line = (t("下載{what} {name}:{pct}%({v:.0f}/{v2:.0f} MB)", what=what, name=dest.name, pct=pct, v=min(done, total) / 1048576, v2=total / 1048576))
        try:
            logger.info("%s", line)
            # ⚠️ **有掛鉤就不 print**:子行程(diarworker/transworker)掛了掛鉤,
            #    而那裡的 stdout 只准 NDJSON——印出去的那一行會被父端當壞掉的
            #    JSON 丟掉。沒掛鉤的是命令列那條路,主控台就是它的畫面
            if _PROGRESS_HOOK is None:
                print(line, flush=True)  # noqa: T201 - 命令列的進度回饋
            report_progress(line, pct / 100)
        except Exception:
            # 進度顯示是 best-effort:主控台編碼等顯示問題
            # 絕不能讓 urlretrieve 中止下載、誤報成下載失敗
            pass

    def _fetch() -> None:
        nonlocal last_pct, last_at
        last_pct, last_at = -_PROGRESS_PCT_STEP, 0.0  # 重試時進度要從頭數起
        urllib.request.urlretrieve(url, tmp, reporthook=_hook)

    try:
        with_tls_rescue(_fetch, what)
        if tmp.stat().st_size < min_bytes:
            raise UserFacingError(t("下載的檔案過小,可能下載失敗,請重試:{url}", url=url))
    except UserFacingError:
        tmp.unlink(missing_ok=True)
        raise
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise download_failed(what, url, e) from e
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
                    t("下載的壓縮檔內容不符預期(缺少 {member_suffix}),請重試:{archive_url}", member_suffix=member_suffix, archive_url=archive_url)
                )
            member.name = part.name
            tar.extract(member, target.parent, filter="data")
        if part.stat().st_size < _MIN_MODEL_BYTES:
            raise UserFacingError(
                t("解壓後的模型檔案過小,可能下載失敗,請重試:{archive_url}", archive_url=archive_url)
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


# 會議紀錄草稿的語言模型(Windows 的 OpenVINO 版;Mac 的 MLX 版在下面 `mlx_llm_dir`)。
# 選哪顆見 `minutes._ov_plan`;數字見 docs/spec/mac/05 §5.4b 的 Windows 那段。
# ⚠️ Qwen3.5 是多模態架構:LLMPipeline 載得起來、一推論就炸,只能走 VLMPipeline,
# 而它建構時要讀視覺那幾顆——所以視覺檔也在必要清單裡(少了就是載入失敗)
_LLM_REPOS = {
    "4b": "OpenVINO/Qwen3.5-4B-int4-ov",
    "2b": "OpenVINO/Qwen3.5-2B-int4-ov",
}
_LLM_REQUIRED_STEMS = (
    "openvino_language_model",
    "openvino_text_embeddings_model",
    "openvino_vision_embeddings_model",
    "openvino_vision_embeddings_merger_model",
    "openvino_vision_embeddings_pos_model",
    "openvino_tokenizer",
    "openvino_detokenizer",
)
#: 下載量(GB,給「第一次要等多久」那句話用;實際檔案大小)
LLM_DOWNLOAD_GB = {"4b": 3.4, "2b": 2.0}
_llm_dirs: dict[str, Path] = {}


def _llm_target(size: str) -> Path:
    return cache_dir() / "llm" / _LLM_REPOS[size].split("/")[-1]


def llm_cached(size: str) -> bool:
    """這顆已經下載好了沒(不連網)。"""
    return size in _llm_dirs or _llm_cache_complete(_llm_target(size))


def _llm_cache_complete(target: Path) -> bool:
    return (target / "config.json").exists() and all(
        (target / f"{stem}.xml").exists() and (target / f"{stem}.bin").exists()
        for stem in _LLM_REQUIRED_STEMS
    )


def minutes_llm_dir(size: str) -> Path:
    """下載(或沿用快取)會議紀錄草稿用的語言模型(`4b` 3.4 GB / `2b` 2.0 GB)。

    同 `ov_whisper_dir`:快取完整就不連網;進度走 `hub_tqdm`(原生視窗沒有主控台)。"""
    if size in _llm_dirs:
        return _llm_dirs[size]
    repo = _LLM_REPOS[size]
    target = _llm_target(size)
    if not _llm_cache_complete(target):
        from huggingface_hub import snapshot_download

        try:
            with_tls_rescue(lambda: snapshot_download(
                repo, local_dir=target, tqdm_class=hub_tqdm(t("AI 模型 {v}", v=repo.split('/')[-1]))),
                t("AI 模型"))
        except Exception as e:
            raise download_failed(t("AI 模型"), repo, e) from e
        if not _llm_cache_complete(target):
            raise UserFacingError(t("AI 模型下載內容不完整,請重試:{repo}", repo=repo))
    _llm_dirs[size] = target
    return target


# 會議紀錄草稿的 Gemma 4 E4B,Windows 那一份(OpenVINO,只跑在內顯;2026-10-28 使用者要
# 「Gemma 4 E4B 也放進 Mac 與 Windows」,見 docs/spec/mac/05 §5.4b)。⚠️ **釘版本、逐檔核
# sha256**(同 Mac 的 Qwen):上游重傳權重,兩台的草稿就不再是同一顆模型寫的。⚠️ **看圖用的那幾個
# 檔也要**:Gemma 4 在 OpenVINO 上一樣走 VLMPipeline,建構時會讀;大檔的雜湊跟 Hugging Face 的
# LFS 對過(2026-10-29)
GEMMA_OV_REPO = "OpenVINO/gemma-4-E4B-it-int4-ov"
GEMMA_OV_REVISION = "9e0659b961752791d2d1325ddb1edf35057c3d2d"
#: 檔名 → (位元組數, sha256)
GEMMA_OV_FILES = {
    "chat_template.jinja": (18569, "0a2c8073c878ab1da004bee933a998606537bbb62016310352c7285c3f01c5b5"),
    "config.json": (5174, "4861dd6c98eeea978f97d871ffc5313eafd456d663b2c4f723b5fa9373914c4d"),
    "generation_config.json": (203, "4c2d18c072dc54cc2ee8f4a29e63525f56bc97a14a0e93c9a30755ba7b6b9014"),
    "openvino_config.json": (1160, "451fc3603d08c3a1fb00bb42ffab4e00fc8e6e6b6621e60be883e8113712ea66"),
    "openvino_detokenizer.bin": (4413547, "461919bda7b546453e903052588c578e3e94e9e8215b7cec07cb00cb97459d13"),
    "openvino_detokenizer.xml": (29792, "fd0aa647e2acce4e54c01128a03f99e4a2840f52efe8a3ab5454aa5803c745ab"),
    "openvino_language_model.bin": (2815657513, "ad62540973a8d53e853e68bfb3f6bf6d917893d1612b9c67b2612d8d6e55067d"),
    "openvino_language_model.xml": (5142009, "754b7572bbec879afb3098423ddf021ebbf292f45b75cb3fe10ac87c90d9811d"),
    "openvino_text_embeddings_model.bin": (671612936, "f4ba107d403f58e4190d0bfef396c9e4eda6762021e4f429c10a53bb4f87aee2"),
    "openvino_text_embeddings_model.xml": (7079, "03ffa3e546653e344394a04688456c7fc3036919d0340ad07ff2e0463c8d7a54"),
    "openvino_text_embeddings_per_layer_model.bin": (2819096656, "e666691fa271c8b6e26aa6eff33f3030148c0bfe43d3a1514b9b997a9a594c5e"),
    "openvino_text_embeddings_per_layer_model.xml": (17900, "310e3a42d76f726145871fed9ff8ed8c1bc6ff85e3ba8d205a3351b7a9a5ee7b"),
    "openvino_tokenizer.bin": (17270350, "72cc61385a142c29a1ea37125b9d7a4c5775fe8386a5f6db1dd1108ea5ec1361"),
    "openvino_tokenizer.xml": (46761, "fdd24f36dd1604fd646586edee1daee4f62916dadcdf2d267446f93a707d4b47"),
    "openvino_vision_embeddings_model.bin": (169812532, "6c7609d984db75c39461f74b418cf3eac65b90da8678a08bc4e158e2f4524c3d"),
    "openvino_vision_embeddings_model.xml": (1932063, "4120821825d31bb690c2ddeb4e39c961a4a1b0a395160a326bbaf03e67cc3c17"),
    "preprocessor_config.json": (403, "12476d626b89e273a9b2c6a493a6319ab02427ebfcd3635d92b29584b27c7249"),
    "processor_config.json": (1689, "32bdf45d2ad4cc29a0822ddd157a182de76644f0419a6228d151495256e9813c"),
    "tokenizer.json": (32169626, "cc8d3a0ce36466ccc1278bf987df5f71db1719b9ca6b4118264f45cb627bfe0f"),
    "tokenizer_config.json": (3700, "aa2f4089111e7c2f23ee3064153267ad8d72bf67378bed3d89c62b20a0c96f88"),
}
#: 下載量(GB,給確認框與等待提示那幾句話用)
GEMMA_OV_DOWNLOAD_GB = round(sum(n for n, _ in GEMMA_OV_FILES.values()) / 1e9, 1)


def gemma_ov_target() -> Path:
    return cache_dir() / "llm" / "gemma-4-E4B-it-int4-ov"


def gemma_ov_cached() -> bool:
    """下載好了沒(不連網、不核 sha256;判準含大小,理由同 `mlx_llm_cached`)。"""
    target = gemma_ov_target()
    return all((target / name).is_file() and (target / name).stat().st_size == size
               for name, (size, _) in GEMMA_OV_FILES.items())


def gemma_ov_size() -> int:
    """模型資料夾現在佔多少位元組(含下載到一半的;「進階參數設定」顯示用)。"""
    target = gemma_ov_target()
    if not target.is_dir():
        return 0
    return sum(f.stat().st_size for f in target.rglob("*") if f.is_file())


def delete_gemma_ov() -> None:
    """刪掉 Windows 的 Gemma 模型(使用者在「進階參數設定」按的;下次用時重新下載)。"""
    target = gemma_ov_target()
    if target.is_dir():
        shutil.rmtree(target)


def gemma_ov_dir() -> Path:
    """下載(或沿用)Windows 的 Gemma 4 E4B,回模型資料夾;下載完逐檔核 sha256,對不上的那個檔
    刪掉、叫使用者再按一次(同 `mlx_llm_dir`)。"""
    target = gemma_ov_target()
    if gemma_ov_cached():
        return target
    from huggingface_hub import snapshot_download

    name = t("AI 模型 {v}", v=GEMMA_OV_REPO.split('/')[-1])
    try:
        with_tls_rescue(lambda: snapshot_download(
            GEMMA_OV_REPO, revision=GEMMA_OV_REVISION, allow_patterns=list(GEMMA_OV_FILES),
            local_dir=target, tqdm_class=hub_tqdm(name)), t("AI 模型"))
    except Exception as e:
        raise download_failed(t("AI 模型"), GEMMA_OV_REPO, e) from e
    report_progress(t("核對下載的 AI 模型檔案中…"), 1.0)
    for fname, (size, digest) in GEMMA_OV_FILES.items():
        path = target / fname
        if not path.is_file() or path.stat().st_size != size or _sha256(path) != digest:
            path.unlink(missing_ok=True)
            logger.warning("Gemma 模型檔核對不符,已刪除:%s", fname)
            raise UserFacingError(t("下載的 AI 模型檔案不完整,請再按一次重新下載。"))
    return target


# 會議紀錄草稿的 Qwen3.5-4B,Mac 那一份(MLX,跑在 GPU;使用者 2026-10-14 選定,見
# docs/spec/mac/05 §5.4b)。⚠️ **跟手機版 `Qwen.swift` 是同一個固定版本、同一份 sha256**:
# 版本不釘,上游哪天重傳權重,兩邊的草稿就不再是同一顆模型寫的,而那正是兩邊實測對照的前提。
# 只下文字要用的 7 個檔(repo 裡另有視覺那一半,MLX 只問文字用不到)。
MLX_LLM_REPO = "mlx-community/Qwen3.5-4B-4bit"
MLX_LLM_REVISION = "0e7ffd5c629ef7719d4cbc04069232580bfa9d9c"
#: 檔名 → (位元組數, sha256)
MLX_LLM_FILES = {
    "config.json": (3366, "f3efc81b2ea8d96a45301037d3ccccbcccdef44a961845c87f286aaddbc6eaaa"),
    "chat_template.jinja": (7756, "a4aee8afcf2e0711942cf848899be66016f8d14a889ff9ede07bca099c28f715"),
    "tokenizer_config.json": (1139, "e98f1901ac6f0adff67b1d540bfa0c36ac1a0cf59eb72ed78146ef89aafa1182"),
    "vocab.json": (6722759, "ce99b4cb2983d118806ce0a8b777a35b093e2000a503ebde25853284c9dfa003"),
    "tokenizer.json": (19989343, "87a7830d63fcf43bf241c3c5242e96e62dd3fdc29224ca26fed8ea333db72de4"),
    "model.safetensors.index.json": (101944, "52e534c41f7b97708329c85f762e5882bf48bd5955a422c6ae74eba321e6048a"),
    "model.safetensors": (3034300695, "5fb9acd0246866381cf8c5c354c6db1019f6498eec4ccb4f5edcc71ffeacb2db"),
}
#: 下載量(GB,給確認框與等待提示那幾句話用)
MLX_LLM_DOWNLOAD_GB = round(sum(n for n, _ in MLX_LLM_FILES.values()) / 1e9, 1)


def mlx_llm_target() -> Path:
    return cache_dir() / "llm" / "Qwen3.5-4B-4bit-mlx"


def mlx_llm_cached() -> bool:
    """下載好了沒(不連網、不核 sha256:3 GB 核一次要十幾秒,只在剛下載完核)。

    ⚠️ **判準含大小**:下載中斷時留下的是不完整的檔,只看「在不在」會把它當成好的、
    載入時才炸——而那時給的是「模型載入失敗」,看不出是下載沒完。"""
    target = mlx_llm_target()
    return all((target / name).is_file() and (target / name).stat().st_size == size
               for name, (size, _) in MLX_LLM_FILES.items())


def mlx_llm_size() -> int:
    """模型資料夾現在佔多少位元組(含下載到一半的;「進階參數設定」顯示用)。"""
    target = mlx_llm_target()
    if not target.is_dir():
        return 0
    return sum(f.stat().st_size for f in target.rglob("*") if f.is_file())


def delete_mlx_llm() -> None:
    """刪掉 Mac 的 Qwen 模型(使用者在「進階參數設定」按的;下次用時重新下載)。"""
    target = mlx_llm_target()
    if target.is_dir():
        shutil.rmtree(target)


def _sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def mlx_llm_dir() -> Path:
    """下載(或沿用)Mac 的 Qwen3.5-4B,回模型資料夾。

    下載完逐檔核 sha256,對不上的那個檔刪掉、叫使用者再按一次(同手機版:中斷了再按一次
    會從沒下完的檔重來——`snapshot_download` 本來就會略過已經在的檔)。"""
    target = mlx_llm_target()
    if mlx_llm_cached():
        return target
    from huggingface_hub import snapshot_download

    name = t("AI 模型 {v}", v=MLX_LLM_REPO.split('/')[-1])
    try:
        with_tls_rescue(lambda: snapshot_download(
            MLX_LLM_REPO, revision=MLX_LLM_REVISION, allow_patterns=list(MLX_LLM_FILES),
            local_dir=target, tqdm_class=hub_tqdm(name)), t("AI 模型"))
    except Exception as e:
        raise download_failed(t("AI 模型"), MLX_LLM_REPO, e) from e
    report_progress(t("核對下載的 AI 模型檔案中…"), 1.0)
    for fname, (size, digest) in MLX_LLM_FILES.items():
        path = target / fname
        if not path.is_file() or path.stat().st_size != size or _sha256(path) != digest:
            path.unlink(missing_ok=True)
            logger.warning("Qwen 模型檔核對不符,已刪除:%s", fname)
            raise UserFacingError(t("下載的 AI 模型檔案不完整,請再按一次重新下載。"))
    return target



# 會議紀錄草稿的 Gemma 4 E4B,Mac 那一份(MLX;2026-10-28 使用者要「Gemma 4 E4B 也放進 Mac 與
# Windows」,docs/spec/mac/05 §5.4b)。⚠️ **跟手機版 `OfflineModel.gemma` 是同一個固定版本、同一份
# sha256**(理由同 Qwen)。只下文字要用的 7 個檔:**不下 `processor_config.json`**(看圖、聽音用的;
# 手機版同樣不下),`mlx_lm` 只問文字用不到
GEMMA_MLX_REPO = "mlx-community/gemma-4-e4b-it-4bit"
GEMMA_MLX_REVISION = "475b9088d29754a3379866cf5aeb6b41acd313c2"
#: 檔名 → (位元組數, sha256)
GEMMA_MLX_FILES = {
    "config.json": (6628, "780ccb3a514a5f1ced161d383f948fc22eca9b84b752ca19494f625bd9bad7a6"),
    "generation_config.json": (208, "d4226bbe3117d2d253ba4609720ba82c6c4ce4627a9a6ae05387c78983ac03de"),
    "chat_template.jinja": (17336, "2f1b4d75d067bae3fe44e676721c7f077d243bc007156cb9c2f8b5836613d082"),
    "tokenizer_config.json": (2740, "080d9e1aff284e2f6043889cd05367966f7c7b80e025fbc0b06745e218158656"),
    "tokenizer.json": (32169626, "cc8d3a0ce36466ccc1278bf987df5f71db1719b9ca6b4118264f45cb627bfe0f"),
    "model.safetensors.index.json": (240961, "f8accac59ee7efe87e0c298c854610b262c3cadd477407503147c71209ff0093"),
    "model.safetensors": (5146800534, "932b8271fc3fe65adcc78b96c10c6268bbfb13e8f67d1358727c0d6ee97e1eff"),
}
#: 下載量(GB,給確認框與等待提示那幾句話用)
GEMMA_MLX_DOWNLOAD_GB = round(sum(n for n, _ in GEMMA_MLX_FILES.values()) / 1e9, 1)


def gemma_mlx_target() -> Path:
    return cache_dir() / "llm" / "gemma-4-e4b-it-4bit-mlx"


def gemma_mlx_cached() -> bool:
    """下載好了沒(不連網、不核 sha256;判準含大小,理由同 `mlx_llm_cached`)。"""
    target = gemma_mlx_target()
    return all((target / name).is_file() and (target / name).stat().st_size == size
               for name, (size, _) in GEMMA_MLX_FILES.items())


def gemma_mlx_size() -> int:
    """模型資料夾現在佔多少位元組(含下載到一半的)。"""
    target = gemma_mlx_target()
    if not target.is_dir():
        return 0
    return sum(f.stat().st_size for f in target.rglob("*") if f.is_file())


def delete_gemma_mlx() -> None:
    """刪掉 Mac 的 Gemma 模型(下次用時重新下載)。"""
    target = gemma_mlx_target()
    if target.is_dir():
        shutil.rmtree(target)


def gemma_mlx_dir() -> Path:
    """下載(或沿用)Mac 的 Gemma 4 E4B,回模型資料夾;下載完逐檔核 sha256,對不上的那個檔
    刪掉、叫使用者再按一次(同 `mlx_llm_dir`)。"""
    target = gemma_mlx_target()
    if gemma_mlx_cached():
        return target
    from huggingface_hub import snapshot_download

    name = t("AI 模型 {v}", v=GEMMA_MLX_REPO.split('/')[-1])
    try:
        with_tls_rescue(lambda: snapshot_download(
            GEMMA_MLX_REPO, revision=GEMMA_MLX_REVISION, allow_patterns=list(GEMMA_MLX_FILES),
            local_dir=target, tqdm_class=hub_tqdm(name)), t("AI 模型"))
    except Exception as e:
        raise download_failed(t("AI 模型"), GEMMA_MLX_REPO, e) from e
    report_progress(t("核對下載的 AI 模型檔案中…"), 1.0)
    for fname, (size, digest) in GEMMA_MLX_FILES.items():
        path = target / fname
        if not path.is_file() or path.stat().st_size != size or _sha256(path) != digest:
            path.unlink(missing_ok=True)
            logger.warning("Gemma(MLX)模型檔核對不符,已刪除:%s", fname)
            raise UserFacingError(t("下載的 AI 模型檔案不完整,請再按一次重新下載。"))
    return target

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
            # 原生支援中斷續傳;with_tls_rescue 兜公司網路的 TLS 攔截
            with_tls_rescue(lambda: snapshot_download(
                repo, local_dir=target, tqdm_class=hub_tqdm(t("模型 {v}", v=repo.split('/')[-1]))),
                t("模型"))
        except Exception as e:
            raise download_failed(t("模型"), repo, e) from e
        if not _ov_cache_complete(target):
            # 防呆:下載「成功」但必要檔案組不齊(上游 repo 結構改變等)
            raise UserFacingError(t("模型下載內容不完整,請重試:{repo}", repo=repo))
    _ov_dirs[model_key] = target
    return target
