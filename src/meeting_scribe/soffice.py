r"""LibreOffice 的取得,以及舊格式的「升級器」。

策略是**先升級成新格式,再走同一條路**:.doc → .docx、.ppt → .pptx、
.xls → .xlsx、.rtf → .docx,升級完就交給既有的 docoffice reader。升級器
只有這一份——為每個舊格式各寫一套二進位讀取器是另一個量級的工作,而且
永遠追不上 Office 的格式細節。

取得順序(使用者 2026-08-01 選定「內附 LibreOffice」而非 Office COM:
不必要求每台機器都裝 Office、也不會撞上使用者正開著的 Word):

1. **先找本機既有安裝**(登錄檔 + 常見安裝路徑)——很多公司電腦本來就有,
   找到就用,**完全不連網**。
2. 沒有才下載官方 MSI,以 `msiexec /a`(administrative install)解開到
   `%LOCALAPPDATA%\meeting-scribe\libreoffice`。那只是解壓,不寫登錄檔、
   不需要提權。**絕不放系統暫存**——`cleanup_stale_temp` 會把它當孤兒
   掃掉,每次啟動都要重下 300MB。
3. 兩條都失敗就給指路的繁中訊息(自己裝 LibreOffice,或用 Office 另存
   為新格式),而不是把使用者丟在一句 cryptic 英文裡。

下載那條路 2026-08-01 在一台沒有 LibreOffice 的機器上實測過,**第一版就壞在
寫死的版本號**(HTTP 404):TDF 的 stable/ 只留當前的幾個版本、舊的整個移走。
現在改成執行時去問目錄索引(見 `_resolve_msi_url`)。**`msiexec /a /qn` 是否
真的不需要提權仍未驗到底**——第一次跑到那一步時要盯著,失敗時的指路訊息
(手動裝 LibreOffice / 用 Office 另存新格式)就是為此準備的。
"""
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from meeting_scribe import cancel, models, paths
from meeting_scribe.errors import UserFacingError

logger = logging.getLogger(__name__)

# 舊格式 → (新副檔名, LibreOffice 的輸出篩選器名稱)
_FILTERS = {
    ".doc": ("docx", "MS Word 2007 XML"),
    ".rtf": ("docx", "MS Word 2007 XML"),
    ".ppt": ("pptx", "Impress MS PowerPoint 2007 XML"),
    ".xls": ("xlsx", "Calc MS Excel 2007 XML"),
    # OpenDocument:LibreOffice 的原生格式,轉成 OOXML 之後就走 docoffice
    # 那條路(閱讀順序、失真標註全部沿用)
    ".odt": ("docx", "MS Word 2007 XML"),
    ".ods": ("xlsx", "Calc MS Excel 2007 XML"),
    ".odp": ("pptx", "Impress MS PowerPoint 2007 XML"),
    # **Visio 只能轉成 PDF**:它是圖,沒有對應的「文件」格式可以轉——
    # 轉成 pptx/docx 只會得到一張貼上去的圖。轉 PDF 之後走 docpdf,那條
    # 路本來就會抽文字層、抽圖、必要時 OCR,正是圖說需要的處理
    ".vsd": ("pdf", "draw_pdf_Export"),
    ".vsdx": ("pdf", "draw_pdf_Export"),
}

# 單一檔案的轉換上限。soffice 卡死(等不到 UNO、profile 鎖)是常態,
# 沒有 timeout 的話整批會永遠停在那一個檔
_CONVERT_TIMEOUT = 180.0
# 等待子行程時的輪詢間隔,也是停止鈕的反應時間上限
_POLL_SEC = 0.25
_LO_INDEX = "https://download.documentfoundation.org/libreoffice/stable/"
_MSI_URL = "{index}{ver}/win/x86_64/LibreOffice_{ver}_Win_x86-64.msi"
# 讀不到目錄索引時的退路。**只是退路、不是預設**:寫死的版本遲早會從
# stable/ 消失(見 _stable_versions)
_LO_FALLBACK_VERSION = "25.8.7"
# 最多回頭試幾個版本(見 _resolve_msi_url)
_URL_PROBE_LIMIT = 3
# MSI 約 370MB;低於這個大小一定是下載壞了(HTML 錯誤頁之類)
_MIN_MSI_BYTES = 200 * 1024 * 1024

_PROGRAM_REL = Path("program") / "soffice.exe"

# soffice 不能併發:同一份使用者 profile 被兩個行程開啟會直接失敗。
# 批次本來就是序列的,這把鎖是防止日後有人改成平行時安靜壞掉
_lock = threading.Lock()
_resolved: Path | None = None


def _install_root() -> Path:
    """走 paths.appdata_root:CLAUDE.md 明訂「%LOCALAPPDATA%\\meeting-scribe
    基底路徑統一走 appdata_root()」,盤點「工具在使用者機器上寫了什麼」
    要看得到這個 320MB 的最大落地物。(原本用 models.cache_dir().parent
    反推,models 的目錄佈局一改就會靜默跑掉、舊安裝變孤兒。)"""
    return paths.appdata_root() / "libreoffice"


def _registry_paths() -> list[Path]:
    """登錄檔裡的安裝位置(InstallPath 直接指向 program 目錄)。"""
    if sys.platform != "win32":
        return []
    import winreg

    out: list[Path] = []
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (0, getattr(winreg, "KEY_WOW64_64KEY", 0)):
            try:
                with winreg.OpenKey(
                    root, r"SOFTWARE\LibreOffice\UNO\InstallPath",
                    0, winreg.KEY_READ | view,
                ) as key:
                    value, _ = winreg.QueryValueEx(key, "")
                    out.append(Path(str(value)) / "soffice.exe")
            except OSError:
                continue
    return out


def find_existing() -> Path | None:
    """本機既有的 LibreOffice。找得到就完全不必連網。"""
    candidates: list[Path] = list(_registry_paths())
    for env in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            candidates.append(Path(base) / "LibreOffice" / _PROGRAM_REL)
    candidates.append(_install_root() / _PROGRAM_REL)
    for path in candidates:
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def _console_launcher(soffice: Path) -> Path:
    r"""Windows 上一律改用同目錄的 `soffice.com`。

    `soffice.exe` 是 **GUI 子系統**的執行檔:從沒有主控台的行程叫它,它會
    自己 AllocConsole **彈出一個黑視窗**印版本號、還停在「Press Enter to
    continue...」等人按鍵(使用者 2026-08-01 回報)。`CREATE_NO_WINDOW` 擋不
    住——那個旗標只管「要不要繼承呼叫端的主控台」,管不到程式自己開的。

    `soffice.com` 是同一套的**主控台子系統**包裝。本機實測差距不只是視窗:
    `--version` 用 .com 是 0.5 秒且 stdout 拿得到版本字串,用 .exe 要 8.8 秒
    而且 **stdout 是空的**(輸出跑進它自己開的那個視窗)——也就是說原本的
    `_smoke_test` 只是靠 returncode 僥倖過關,真的壞掉也驗不出來。"""
    if sys.platform != "win32":
        return soffice
    console = soffice.with_suffix(".com")
    return console if console.is_file() else soffice


def _smoke_test(soffice: Path) -> bool:
    """`--version` 冒煙:檔案在不代表跑得起來(解壓不完整、缺 VC 執行期)。

    抄 models._ov_cache_complete 的精神——「按名點驗」不夠,要真的碰一下。"""
    try:
        proc = subprocess.run(  # noqa: S603
            [str(soffice), "--version"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, creationflags=_no_window(),
        )
    except Exception:
        logger.debug("soffice --version 執行失敗", exc_info=True)
        return False
    if proc.returncode != 0:
        return False
    # 主控台版一定印得出版本字串;拿不到就是解壓不完整之類的真問題。
    # 只在 .com 上要求——.exe 的輸出會跑進它自己開的視窗(見 _console_launcher),
    # 對它要求 stdout 會把「其實可用」誤判成壞掉
    if soffice.suffix.lower() == ".com" and not proc.stdout.strip():
        logger.debug("soffice.com --version 沒有任何輸出")
        return False
    return True


def _no_window() -> int:
    return 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW


def _extract_msi(msi: Path, target: Path) -> None:
    """`msiexec /a` = administrative install:只把內容解開到 TARGETDIR,
    不寫登錄檔、不建立捷徑,也**不需要提權**(這是給網路部署用的模式)。"""
    target.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(  # noqa: S603
        ["msiexec", "/a", str(msi), "/qn", f"TARGETDIR={target}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=1800, creationflags=_no_window(),
    )
    if proc.returncode != 0:
        logger.error("msiexec 解壓失敗(%s):%s", proc.returncode, proc.stderr)
        raise UserFacingError(
            "LibreOffice 安裝檔解壓失敗。請改用手動方式:"
            "到 https://zh-tw.libreoffice.org/ 下載安裝(免費),裝好後再轉一次;"
            "或用 Word/Excel/PowerPoint 開啟後另存為新格式(.docx/.xlsx/.pptx)。"
        )


def _parse_versions(html: str) -> list[str]:
    """目錄索引 → 版本號清單,新到舊。

    **依數字排序而不是字串**:字串比較會把 25.8.7 排在 26.2.5 之後
    (逐字元比,「8」>「2」),結果挑到舊版。"""
    found = {
        tuple(int(part) for part in m)
        for m in re.findall(r'href="(\d+)\.(\d+)\.(\d+)/"', html)
    }
    return [".".join(str(p) for p in v) for v in sorted(found, reverse=True)]


def _stable_versions() -> list[str]:
    """TDF 的 stable/ 目錄現在有哪些版本,新到舊。讀不到就回退路那一個。"""
    try:
        with urllib.request.urlopen(_LO_INDEX, timeout=30) as resp:
            html = resp.read(1 << 20).decode("utf-8", "replace")
    except Exception:
        logger.warning("讀不到 LibreOffice 版本清單,改用內建版本", exc_info=True)
        return [_LO_FALLBACK_VERSION]
    versions = _parse_versions(html)
    if not versions:
        logger.warning("LibreOffice 版本清單解析不出版本號,改用內建版本")
        return [_LO_FALLBACK_VERSION]
    return versions


def _url_exists(url: str) -> bool:
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return 200 <= resp.status < 300
    except Exception:
        logger.debug("LibreOffice 安裝檔不存在:%s", url, exc_info=True)
        return False


def _resolve_msi_url() -> str:
    """問出一個**現在真的存在**的 Windows MSI 網址。

    **版本號絕不寫死**:stable/ 只留當前的幾個版本、舊的整個移走——第一版
    寫死 25.2.5,幾個月後就 404(使用者 2026-08-01 實際踩到,而且是在「已經
    印出下載提示」之後才炸,看起來像網路壞掉)。原本的理由是「下載頁 HTML
    隨時會改,寫死至少壞得明確」,但實情是寫死**必然**會壞、只是時間問題,
    而目錄索引要解析的只是 `href="26.2.5/"` 這種版本目錄名,比整頁 HTML 穩。

    挑法是「新到舊試,第一個 HEAD 得到 200 的就用」:版本目錄可能先建立、
    Windows 的 build 稍後才上傳,只認最新的會撲空。"""
    for ver in _stable_versions()[:_URL_PROBE_LIMIT]:
        url = _MSI_URL.format(index=_LO_INDEX, ver=ver)
        if _url_exists(url):
            return url
    raise UserFacingError(
        "找不到可下載的 LibreOffice 安裝檔(官方網站的版本可能剛好在更新)。"
        "請改用手動方式:到 https://zh-tw.libreoffice.org/ 下載安裝(免費),"
        "裝好後再轉一次;或用 Word/Excel/PowerPoint 開啟後另存為新格式"
        "(.docx/.xlsx/.pptx)。"
    )


def _download_and_install() -> Path:
    root = _install_root()
    url = _resolve_msi_url()
    msi = root / url.rsplit("/", 1)[-1]
    print(  # noqa: T201 - 黑視窗的進度回饋,同 models.download
        "首次轉換舊版 Office 檔(副檔名沒有 x 的 doc/xls/ppt 等舊格式)需要下載 "
        "LibreOffice(約 370MB,只需一次);docx/xlsx/pptx 這類新格式不必下載。"
        "進度如下:", flush=True,
    )
    root.mkdir(parents=True, exist_ok=True)
    models.download(url, msi, min_bytes=_MIN_MSI_BYTES, what="LibreOffice 安裝檔")
    try:
        _extract_msi(msi, root)
    finally:
        # 解壓完就不需要安裝檔了,留著白佔 320MB
        msi.unlink(missing_ok=True)
    found = root / _PROGRAM_REL
    if not found.is_file():
        # 有些版本會多包一層目錄,掃一下再放棄
        for cand in root.rglob("soffice.exe"):
            found = cand
            break
    if not found.is_file():
        shutil.rmtree(root, ignore_errors=True)
        raise UserFacingError(
            "LibreOffice 解壓後找不到主程式,安裝可能不完整。"
            "請改用手動方式:到 https://zh-tw.libreoffice.org/ 下載安裝,"
            "或用 Office 另存為新格式(.docx/.xlsx/.pptx)再轉。"
        )
    return found


def ensure_ready(*, allow_install: bool = True) -> Path:
    """取得可用的 soffice.exe(必要時下載安裝)。結果會被記住,只做一次。

    **`allow_install=False` 是「本機有就用,沒有就算了」**:.rtf 走這條。
    它有 striprtf 的降級路徑,為了一份 rtf 下載 320MB 不合理——而且使用者
    完全沒料到,他只是轉了一個看起來很普通的檔(隱私規格把連網行為列成
    白名單,這種默默觸發的下載不在使用者的預期裡)。"""
    global _resolved
    with _lock:
        if _resolved is not None and _resolved.is_file():
            return _resolved
        found = find_existing()
        if found is not None:
            # 換成主控台版再驗:黑視窗與 8.8 秒的空轉都出在 .exe 上
            found = _console_launcher(found)
            if _smoke_test(found):
                _resolved = found
                return found
        if not allow_install:
            raise UserFacingError("本機沒有安裝 LibreOffice")
        if sys.platform != "win32":
            raise UserFacingError(
                "舊版 Office 格式的轉換目前只支援 Windows,"
                "請用 Office 另存為新格式(.docx/.xlsx/.pptx)再轉。"
            )
        installed = _console_launcher(_download_and_install())
        if not _smoke_test(installed):
            raise UserFacingError(
                "LibreOffice 安裝完成但無法執行。請改用手動方式:"
                "到 https://zh-tw.libreoffice.org/ 下載安裝,"
                "或用 Office 另存為新格式(.docx/.xlsx/.pptx)再轉。"
            )
        _resolved = installed
        return installed


def _profile_uri(work: Path) -> str:
    """`-env:UserInstallation` 要吃 file:// URI。

    **必須指定**:不指定的話會用使用者預設的 profile,而那個 profile 在
    使用者自己開著 LibreOffice 時是被鎖住的,轉檔會直接失敗。"""
    profile = work / "lo-profile"
    profile.mkdir(parents=True, exist_ok=True)
    return profile.as_uri()


def _run_convert(cmd: list[str], expect: Path, src_name: str) -> Path:
    """跑 soffice 並確認成品落地。

    兩件事跟 `audio._run_ffmpeg` 一樣重要,一件是它沒有的:
    - `encoding="utf-8", errors="replace"`:外部工具吐 cp950 不能讓 decode 炸掉
    - **雙重成功判準**:returncode == 0 **且** 成品存在——soffice 惡名昭彰,
      轉檔失敗時常常靜靜回 0
    - (新增)`Popen` + 輪詢:`subprocess.run` 不可中斷,而 soffice 卡死是
      常態;沒有這一層,按停止會完全沒反應直到 timeout"""
    proc = subprocess.Popen(  # noqa: S603
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        creationflags=_no_window(),
    )
    deadline = time.monotonic() + _CONVERT_TIMEOUT
    try:
        while proc.poll() is None:
            cancel.check()
            if time.monotonic() > deadline:
                proc.kill()
                raise UserFacingError(
                    f"轉換「{src_name}」超過 {_CONVERT_TIMEOUT:.0f} 秒沒有完成,已中止。"
                    "檔案可能有問題,或 LibreOffice 正被其他程式佔用。"
                )
            time.sleep(_POLL_SEC)
    except cancel.Cancelled:
        proc.kill()
        raise
    _, err = proc.communicate(timeout=30)
    if proc.returncode != 0 or not expect.is_file():
        logger.error("LibreOffice 轉換失敗(%s):%s", proc.returncode, err)
        raise UserFacingError(
            f"無法轉換舊版格式的「{src_name}」:檔案可能已損壞或有密碼保護。"
            "可以先用 Office 開啟後另存為新格式(.docx/.xlsx/.pptx)再試。"
        )
    return expect


def upgrade(
    src: Path, work: Path, *, allow_install: bool = True,
    source_ext: str | None = None,
) -> Path:
    """舊格式 → 新格式,回傳新檔路徑(落在 work 目錄裡)。

    **只做格式升級,不做內容解析**:升級完的 .docx/.pptx/.xlsx 交回給
    docoffice 的既有 reader,那條路已經處理好閱讀順序、失真標註等等。"""
    # **`source_ext` 優先於副檔名**:公司文件裡「把 .xls 改名成 .xlsx」很
    # 常見,呼叫端用 magic bytes 認出真格式之後要能覆寫這裡——不然偵測只做
    # 了一半(路由挑對了、升級器仍照著錯的副檔名查表,報「不支援的舊格式
    # 「.xlsx」」)。2026-08-03 全量稽核主要工作目錄時,85 個打不開的 xlsx
    # 裡有 77 個是這種
    ext = (source_ext or src.suffix).lower()
    if ext not in _FILTERS:
        raise UserFacingError(f"不支援的舊格式「{ext}」:{src.name}")
    target_ext, filter_name = _FILTERS[ext]
    soffice = ensure_ready(allow_install=allow_install)
    work.mkdir(parents=True, exist_ok=True)
    expect = work / f"{src.stem}.{target_ext}"
    cmd = build_command(soffice, src, work, target_ext, filter_name)
    with _lock:  # soffice 不能併發:同一份 profile 被兩個行程開會直接失敗
        return _run_convert(cmd, expect, src.name)


def build_command(
    soffice: Path, src: Path, work: Path, target_ext: str, filter_name: str,
) -> list[str]:
    """組出 headless 轉檔的命令列。抽成函式是為了讓測試驗得到參數——
    少一個 `-env:UserInstallation` 就會在使用者剛好開著 LibreOffice 時
    無聲失敗,而那種情境在測試環境裡重現不了。"""
    return [
        str(soffice), "--headless", "--norestore", "--invisible",
        "--nolockcheck", "--nodefault", "--nofirststartwizard",
        f"-env:UserInstallation={_profile_uri(work)}",
        "--convert-to", f"{target_ext}:{filter_name}",
        "--outdir", str(work), str(src),
    ]


def reset_cache() -> None:
    """忘掉找到的 soffice 位置(測試用;正式流程中位置不會變)。"""
    global _resolved
    with _lock:
        _resolved = None
