r"""檢查新版與自動更新的純邏輯(問 GitHub、下載、驗包、交給換版小程式)。

使用者 2026-10-07 指定:「每次啟動後自動檢查程式的版本跟 GitHub 的版本,如果 GitHub
比較新,問使用者是否要下載、自動更新;勾了『不要再提醒』就不要檢查;也可以主動按
『檢查更新』」——四張設計稿裡選了案 B(使用說明頁底下一列),更新做到「真正自動換版」。

**這是第三種連網**(`docs/spec/01` §1.3,同日使用者同意):送出去的只有一個「最新版是
哪一版」的詢問,收回來的是版號、這一版的更新說明與壓縮包的網址;**沒有任何檔案內容
或使用資料離開這台電腦**。

⚠️ **這個模組不准 import 任何 UI 模組**(同 `naming.py` 的判準:換一套 UI 要不要改)。
畫面在 `desktop.py`,真正動手換檔的是換版小程式(Windows 是 `update_helper.py`、Mac 是
`plat_mac` 裡那支 zsh)——它在本程式**關掉之後**才跑,所以不能是本行程裡的任何東西。
⚠️ **兩個平台不一樣的地方一律走 `plat`**(附檔名、解壓與驗簽、換版),這裡只放共用的
檢查、下載與「壓縮包裡該有什麼」的判準。

⚠️ **發佈的形狀是一份契約,每一份已經裝出去的副本都在讀它**(`docs/dev/publish.md`):
tag 是 `v{版號}`、Windows 的附檔叫 `scribe2md-win.zip`、壓縮包頂層是 `scribe2md/`;
Mac 的附檔叫 `scribe2md-mac.zip`、頂層是 `{顯示名稱}.app`;內文在 `---` 之前是這一版的
更新說明。改了 `release.py`/`make_public.py` 那一邊,
**已經在同仁手上的舊版就再也更新不了**——而那不會有任何人立刻發現。
`tests/test_update.py` 把這幾個名字跟另一邊釘在一起。
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import threading
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from meeting_scribe import brand, i18n, paths
from meeting_scribe.i18n import t

logger = logging.getLogger(__name__)

# ⚠️ **2026-11-07 改名:完整 App(會議助手電腦版)搬到新 repo**。舊 repo `scribe2md` 停在「橋接版」:
# 已裝的舊版只會問舊 repo(那段程式在它們手上、改不到),橋接版照舊形狀發在舊 repo 讓它們收得下,
# 而橋接版自己的程式問的是這裡——更新過一次之後就都從新 repo 來(`docs/dev/publish.md` §4.7)。
REPO = "VincentLiang1/notetaker-desktop"
LEGACY_REPO = "VincentLiang1/scribe2md"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
#: 使用者手動更新時要去的頁面(Mac 版、或自動更新走不了的時候)
DOWNLOAD_PAGE = f"https://github.com/{REPO}/releases/latest"
#: Release 內文裡分開兩種語言的小標(⚠️ 與 `scripts/release.py` 的同名常數是同一個字,
#: 測試釘著),都在 `---` 之前:v1.7.3 起英文在前、`ZH_HEADING` 之後是中文;
#: v1.7.2 以前中文在前、`EN_HEADING` 之後是英文
EN_HEADING = "### English"
ZH_HEADING = "### 中文"
#: ⚠️ 與 `scripts/release.py` 的 `WIN_ASSET` 是同一個名字(測試釘著)
WIN_ASSET = "notetaker-desktop-win.zip"
#: ⚠️ 與 `scripts/release.py` 的 `MAC_ASSET` 是同一個名字(測試釘著)
MAC_ASSET = "notetaker-desktop-mac.zip"
#: 舊 repo 的附檔名:**只有橋接版**用它發(已裝的舊版照這兩個名字找)
LEGACY_WIN_ASSET = "scribe2md-win.zip"
LEGACY_MAC_ASSET = "scribe2md-mac.zip"
#: Mac 壓縮包的頂層:`make_mac_app.py` 打出來的 bundle 名(`ditto --keepParent`)。
#: ⚠️ **所以 App 改名就是改契約**:已裝出去的 Mac 版會照舊名找,找不到就拒收——2026-11-07 改名成
#: `Notetaker Desktop.app` 之後,**新版兩種都收**(`MAC_APPS`),橋接版發在舊 repo 的那一包是舊名
MAC_APP = f"{brand.APP_BUNDLE}.app"
LEGACY_MAC_APP = f"{brand.APP_TITLE}.app"
MAC_APPS = (MAC_APP, LEGACY_MAC_APP)
#: bundle 裡那份程式(首次啟動時被複製到落地根目錄,見 `make_mac_app.py`)的相對位置
MAC_PAYLOAD_REL = "Contents/Resources/payload"
MAC_PAYLOAD = f"{MAC_APP}/{MAC_PAYLOAD_REL}"
#: ⚠️ 與 `scripts/make_public.py` 寫進 zip 的頂層資料夾是同一個名字(測試釘著)。使用者手動解壓
#: 時看得到它,所以跟著新名字;**舊的 `scribe2md/` 也收**(橋接版發在舊 repo 的那一包是舊形狀)
TOP = "notetaker-desktop"
TOPS = (TOP, "scribe2md")
#: 啟動時那一趟要快:連不上就算了,下一次開再問(手動按的那一趟也用同一個數)
CHECK_TIMEOUT_SEC = 8
#: 下載本身:大檔慢網路要的是「每一塊之間」的逾時,不是整趟的
DOWNLOAD_TIMEOUT_SEC = 30
_CHUNK = 256 * 1024

#: 這幾個資料夾**絕不可以出現在更新包裡、也絕不可以被換版覆蓋**:`data` 是使用者
#: 的聲紋與名單(`docs/spec/12` §12.3「已存在就絕不覆蓋」那一整節的理由),其餘是
#: 這台電腦自己長出來的東西。⚠️ `update_helper.py` 有一份同樣的清單(它只准用標準
#: 函式庫、import 不到這裡),測試釘著兩份一致。
NEVER_TOUCH = ("data", ".venv", "logs", "output", ".git")


@dataclass(frozen=True)
class Release:
    """GitHub 上最新的那一版。"""

    version: str          # 不帶 v,例如 "1.2.0"
    notes: str            # 這一版的更新說明(已去掉產品介紹那一半)
    page: str             # Release 頁面的網址(自動更新走不了時開它)
    asset_url: str | None  # 這個平台的壓縮包的下載網址;沒附就是 None
    asset_size: int       # 位元組數(下載完要對得上)
    asset_sha256: str | None  # GitHub 給的摘要(有給才驗)


def current_version() -> str:
    """這一份程式的版號。⚠️ 與記錄檔檔頭同一個來源(`filelog`),不另起一份。"""
    from meeting_scribe import filelog

    return filelog._package_version()


def parse_version(text: str) -> tuple[int, ...] | None:
    """`"v1.2.0"` → `(1, 2, 0)`;認不得回 None。

    ⚠️ **一律比 tuple、絕不比字串**:字串比的話 `"1.10.0" < "1.9.0"`。"""
    text = (text or "").strip().lstrip("vV")
    try:
        nums = tuple(int(p) for p in text.split("."))
    except ValueError:
        return None
    return nums or None


def is_newer(remote: str, local: str) -> bool:
    """GitHub 那一版是不是比這一份新?任一邊認不得就當作「不是」(寧可不問)。"""
    r, mine = parse_version(remote), parse_version(local)
    if r is None or mine is None:
        return False
    return r > mine


def is_dev_tree(root: Path | None = None) -> bool:
    r"""這是不是開發用的資料夾(有 `.git`)?

    ⚠️ **是的話絕不自動換版**:那會拿公開版把 repo 整個蓋過去。`.git` 是檔案(git
    worktree,這條線就是)或資料夾都算——與 `Install.bat` 略過捷徑的判準同一個。"""
    root = root or paths.repo_root()
    return (root / ".git").exists()


def notes_from_body(body: str | None, lang: str | None = None) -> str:
    r"""從 Release 內文取出「這一版改了什麼」。

    內文的形狀由 `release.build_notes` 決定:`## v1.2.0 What's new`、英文的本次更新、
    `### 中文` 與中文的本次更新、`---`、固定不變的產品介紹(v1.7.2 以前是中文在前、
    `### English` 底下接英文,舊 Release 照樣認得)。只要前一半,
    標題也拿掉(對話框自己會寫版號);`**粗體**` 的星號在 Tk 的純文字框裡只會是雜訊,一併拿掉。
    英文介面只列英文那段、其他介面只列中文那段(`lang` 不給就問介面語言);沒有英文那段的
    舊 Release 一律列中文。"""
    text = (body or "").replace("\r\n", "\n")
    text = text.split("\n---\n", 1)[0]
    if f"\n{ZH_HEADING}\n" in text:
        en, sep, zh = text.partition(f"\n{ZH_HEADING}\n")
    else:
        zh, sep, en = text.partition(f"\n{EN_HEADING}\n")
    text = en if sep and (lang or i18n.current()) == "en" else zh
    lines = [ln for ln in text.split("\n") if not ln.startswith("## ")]
    return "\n".join(lines).replace("**", "").strip()


def _get_json(url: str, timeout: float) -> dict:
    # ⚠️ GitHub API 沒帶 User-Agent 會回 403
    req = urllib.request.Request(url, headers={
        "User-Agent": "scribe2md-update-check",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 固定網址
        return json.loads(resp.read().decode("utf-8"))


def fetch_latest(timeout: float = CHECK_TIMEOUT_SEC, asset_name: str | None = None) -> Release:
    r"""問 GitHub 最新的正式版。失敗就丟例外,由呼叫端決定要不要出聲。

    `asset_name` 不給就問平台層這台要哪一個附檔(`plat.update_asset`)。

    ⚠️ **`/releases/latest` 不含草稿**:Mac 那一步先上傳的同版號草稿(`release.py`
    的兩條路)不會讓 Windows 這邊誤報有新版。
    ⚠️ **走 `with_tls_rescue`**:公司電腦驗不過憑證時改用系統憑證再試一次,同模型下載。"""
    from meeting_scribe import models, plat

    if asset_name is None:
        asset_name = plat.update_asset()
    data = models.with_tls_rescue(lambda: _get_json(LATEST_API, timeout), t("檢查新版"))
    tag = str(data.get("tag_name") or "")
    if parse_version(tag) is None:
        raise ValueError(t("GitHub 回的版號認不得:{tag!r}", tag=tag))
    asset = next((a for a in data.get("assets") or []
                  if asset_name and a.get("name") == asset_name), None)
    digest = (asset or {}).get("digest") or ""
    return Release(
        version=tag.lstrip("vV"),
        notes=notes_from_body(data.get("body")),
        page=str(data.get("html_url") or DOWNLOAD_PAGE),
        asset_url=(asset or {}).get("browser_download_url"),
        asset_size=int((asset or {}).get("size") or 0),
        asset_sha256=digest[7:].lower() if digest.startswith("sha256:") else None,
    )


def check(timeout: float = CHECK_TIMEOUT_SEC) -> tuple[Release | None, str | None]:
    r"""比對一次:回 `(有新版就是那一版、否則 None, 失敗時的原因)`。

    ⚠️ **失敗只記一行、不附堆疊**:連不上是環境問題(公司代理、離線),不是程式壞掉
    (同 CLAUDE.md「環境問題的記錄檔只留一行」)。啟動那一趟由呼叫端吞掉不出聲。"""
    try:
        rel = fetch_latest(timeout)
    except Exception as e:  # noqa: BLE001
        logger.info("檢查新版失敗(%s: %s)", type(e).__name__, e)
        return None, str(e) or type(e).__name__
    mine = current_version()
    logger.info("檢查新版:GitHub 上是 %s,這一份是 %s", rel.version, mine or "(不明)")
    return (rel if is_newer(rel.version, mine) else None), None


# --- 下載與驗包 --------------------------------------------------------------


class UpdateCancelled(Exception):
    """使用者在下載途中按了「取消」。⚠️ **不走 `cancel.py`**:那是三條工作路徑共用
    的全域旗標,拿它來停下載會把正在跑的轉檔一起停掉。"""


def work_dir() -> Path:
    r"""下載與解壓的地方(`%LOCALAPPDATA%\meeting-scribe\update`)。

    ⚠️ **不放系統暫存的 `meeting-scribe-*` 前綴下**:`cleanup_stale_temp` 會把它當孤兒
    掃掉,而換版小程式要在本程式關掉之後才讀它。"""
    return paths.appdata_root() / "update"


def writable(root: Path) -> str | None:
    r"""工具資料夾寫不寫得進去?寫得進去回 None,否則回一句繁中原因。

    ⚠️ **下載之前就要問**:裝在「Program Files」或被群組原則鎖住的資料夾,下載完
    才發現換不了,等於讓人白等一百多 MB。兩處都試(根目錄與程式碼那一層)。"""
    for d in (root, root / "src" / "meeting_scribe"):
        probe = d / ".update-write-test"
        try:
            probe.write_bytes(b"")
            probe.unlink()
        except OSError:
            return t("工具資料夾沒有寫入權限({d})", d=d)
    return None


def download(rel: Release, dest: Path, progress=None,
             stop: threading.Event | None = None) -> Path:
    r"""把壓縮包下載到 `dest`,邊下邊報進度 `progress(已下載, 總數)`。

    ⚠️ **自己寫迴圈,不用 `urlretrieve`**:那個停不下來,而這一趟要有「取消」。
    ⚠️ **大小與摘要都要對得上**:斷線時 urllib 不一定丟例外,只是檔案短了一截——
    那份 zip 解得開一半,換到一半才炸是最糟的時機。"""
    from meeting_scribe import models

    if not rel.asset_url:
        raise ValueError(t("這一版沒有附這台電腦用的壓縮包"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")

    def fetch() -> None:
        req = urllib.request.Request(rel.asset_url,
                                     headers={"User-Agent": "scribe2md-update"})
        sha = hashlib.sha256()
        done = 0
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_SEC) as resp, \
                open(tmp, "wb") as f:
            total = int(resp.headers.get("Content-Length") or rel.asset_size or 0)
            while True:
                if stop is not None and stop.is_set():
                    raise UpdateCancelled()
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                sha.update(chunk)
                done += len(chunk)
                if progress is not None:
                    try:
                        progress(done, total)
                    except Exception:  # noqa: BLE001 - 進度是 best-effort
                        logger.debug("更新下載的進度回報失敗", exc_info=True)
        if rel.asset_size and done != rel.asset_size:
            raise ValueError(t("下載不完整({done} / {asset_size} 位元組)", done=done, asset_size=rel.asset_size))
        if rel.asset_sha256 and sha.hexdigest() != rel.asset_sha256:
            raise ValueError(t("下載的檔案摘要對不上,可能在傳輸中損毀"))

    try:
        models.with_tls_rescue(fetch, t("新版程式"))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(dest)
    return dest


def verify_zip(path: Path, version: str) -> None:
    r"""打開之前先驗:形狀不對就丟 `ValueError`,一個檔都不寫。

    ⚠️ **含 `data/` 的包一律拒收**:解開覆蓋下去就是把使用者的聲紋與名單換成別人
    的(或清空),而那在覆蓋的那一刻就已經造成損失(`docs/spec/12` §12.3)。
    ⚠️ **路徑一律要在同一個頂層資料夾底下**(`TOPS` 其中之一),`..` 與絕對路徑拒收(zip slip)。
    ⚠️ **版號要跟 GitHub 說的那一版對得上**:附錯檔的 Release 不該把人換到另一版去。"""
    with zipfile.ZipFile(path) as z:
        bad = z.testzip()
        if bad is not None:
            raise ValueError(t("壓縮包損毀({bad})", bad=bad))
        names = z.namelist()
        prefix = _zip_top(names) + "/"
        for n in names:
            parts = n.split("/")
            if (not n.startswith(prefix) or ".." in parts or ":" in n
                    or n.startswith("/") or "\\" in n):
                raise ValueError(t("壓縮包裡有不該出現的路徑:{n}", n=n))
            if len(parts) > 2 and parts[1] in NEVER_TOUCH:
                raise ValueError(t("壓縮包裡帶著 {v} 資料夾,拒絕安裝", v=parts[1]))
        for need in ("pyproject.toml", "VERSION", "src/meeting_scribe/desktop.py"):
            if prefix + need not in names:
                raise ValueError(t("壓縮包裡少了 {need}", need=need))
        pyproject = z.read(prefix + "pyproject.toml").decode("utf-8")
    got = _pyproject_version(pyproject)
    if parse_version(got or "") != parse_version(version):
        raise ValueError(t("壓縮包裡的版號是 {got},不是 {version}", got=got, version=version))


def _zip_top(names: list[str]) -> str:
    """壓縮包的頂層資料夾(`TOPS` 其中之一);第一個名字不在任何一個底下就回 `TOP`(之後的檢查會拒收)。"""
    first = names[0].split("/", 1)[0] if names else ""
    return first if first in TOPS else TOP


def _zip_name(info: zipfile.ZipInfo) -> str:
    r"""zip 裡的檔名。⚠️ **`ditto` 打的包沒有標 UTF-8**(flag bit 11),`zipfile` 就照規格
    當成 cp437 解,中文的 App 名變成一串亂碼(2026-10-07 拿 v1.1.0 的 Mac 包實測)——
    不還原的話「頂層是不是那個 App」永遠答不對。"""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


def verify_mac_zip(path: Path, version: str) -> None:
    r"""Mac 版的壓縮包:打開之前先驗,形狀不對就丟 `ValueError`,一個檔都不寫。

    判準與 Windows 那支同一套,只是位置換到 bundle 裡那份程式(`MAC_PAYLOAD`):
    ⚠️ **頂層只能是一個 App**(新名或舊名,`MAC_APPS`);⚠️ **payload 裡出現 `data/` 一律拒收**——
    升級時那一份本來就不會覆蓋(`make_mac_app.py` 的 rsync 排除了它),但帶著它出貨就是
    把某個人的聲紋與名單公開出去,在這裡擋第二次;⚠️ **版號讀 payload 的 `pyproject.toml`**,
    不讀 `Info.plist`——那裡的兩個欄位放的是日期與 commit(`make_mac_app.py`)。
    簽章與符號連結這兩件 zipfile 驗不到,由 `plat_mac.stage_update` 解開之後驗。"""
    with zipfile.ZipFile(path) as z:
        bad = z.testzip()
        if bad is not None:
            raise ValueError(t("壓縮包損毀({bad})", bad=bad))
        infos = {_zip_name(i): i for i in z.infolist()}
        first = next(iter(infos), "").split("/", 1)[0]
        top = first if first in MAC_APPS else MAC_APP
        prefix = top + "/"
        payload = f"{top}/{MAC_PAYLOAD_REL}/"
        for n in infos:
            outside = (not n.startswith(prefix) or n.startswith("/")
                       or "\\" in n or ":" in n)
            if outside or ".." in n.split("/"):
                raise ValueError(t("壓縮包裡有不該出現的路徑:{n}", n=n))
            if n.startswith(payload):
                inner = n[len(payload):].split("/")
                if len(inner) > 1 and inner[0] in NEVER_TOUCH:
                    raise ValueError(t("壓縮包裡帶著 {v} 資料夾,拒絕安裝", v=inner[0]))
        for need in ("Contents/Info.plist", "Contents/MacOS/"):
            if not any(n.startswith(prefix + need) for n in infos):
                raise ValueError(t("壓縮包裡少了 {need}", need=need))
        for need in ("pyproject.toml", "VERSION", "src/meeting_scribe/desktop.py"):
            if payload + need not in infos:
                raise ValueError(t("壓縮包裡少了 {need}", need=need))
        pyproject = z.read(infos[payload + "pyproject.toml"]).decode("utf-8")
    got = _pyproject_version(pyproject)
    if got is None or parse_version(got) != parse_version(version):
        raise ValueError(t("壓縮包裡的版號是 {got},不是 {version}", got=got, version=version))


def _pyproject_version(text: str) -> str | None:
    import tomllib

    try:
        return tomllib.loads(text)["project"]["version"]
    except Exception:  # noqa: BLE001
        return None


def stage(zip_path: Path, into: Path) -> Path:
    """解壓到 `into`,回傳解出來的頂層資料夾(換版小程式拿它去覆蓋;新舊兩種名字都認,見 `TOPS`)。"""
    if into.exists():
        shutil.rmtree(into)
    into.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(into)
        top = _zip_top(z.namelist())
    return into / top


def prepare(rel: Release, progress=None, stop: threading.Event | None = None) -> Path:
    r"""下載 → 驗包 → 解壓。回傳準備好的新版(Windows 是資料夾、Mac 是 `.app`)。
    每一步失敗都丟例外、不留半成品。

    ⚠️ **驗包與解壓交給平台層**(`plat.stage_update`):Mac 一定要用 `ditto` 解、解完還要
    驗簽章,`zipfile` 解出來的 App 符號連結全變成普通檔,簽章當場失效。"""
    from meeting_scribe import plat

    base = work_dir()
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)   # 上一次沒換成的殘骸(含 Mac 的舊版備份)
    zip_path = download(rel, base / "update.zip", progress, stop)
    staged = plat.stage_update(zip_path, rel.version, base / "staged")
    zip_path.unlink(missing_ok=True)
    return staged


#: 2026-10-22 之前的入口檔名(使用者:「支援多國語言,安裝檔案應該是英文檔名,目錄也不應該
#: 有中文」)。⚠️ **換版只覆蓋、不刪檔**(`update_helper` 檔頭),所以舊版自動更新上來的副本
#: 兩套都在——見 `migrate_legacy_entries`。
LEGACY_ENTRIES = ("安裝.bat", "安裝Skill.bat", "安裝.command", "安裝Skill.command",
                  "啟動.command", "疑難排解/啟動DEBUG.bat")
LEGACY_LAUNCHER = "啟動.vbs"
LEGACY_DIR = "疑難排解"
LAUNCHER = "Launch.vbs"


def _load_make_shortcut(root: Path):
    """交付包裡的 `scripts/make_shortcut.py`(`package.SCRIPT_FILES` 帶著它)。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_make_shortcut", root / "scripts" / "make_shortcut.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def migrate_legacy_entries(root: Path | None = None) -> list[str]:
    r"""舊版更新上來的副本:把捷徑改指 `Launch.vbs`、刪掉中文檔名的舊入口。回傳刪掉了哪些。

    ⚠️ **只在「新舊兩支啟動器都在」時動手**:那個組合只會出現在「改名前裝的、自動更新上來」
    的副本;全新安裝與 Mac(`rsync --delete` 自己就清乾淨了)都碰不到。開發用的資料夾
    一律不動(那些檔是版控的)。
    ⚠️ **只改原本就在的捷徑、不新建**:使用者自己刪掉的那顆不該被變回來。
    ⚠️ **捷徑沒改成就留著 `啟動.vbs`**:桌面那顆還指著它,刪了就是一顆點不開的死圖示;下一次
    啟動再試。其他舊檔沒有人指著,照刪。
    ⚠️ **絕不拋例外**:這是開窗路徑上的順手清理,任何一步失敗都只記一行。"""
    from meeting_scribe import plat

    root = root or paths.repo_root()
    if is_dev_tree(root) or not (root / LAUNCHER).is_file():
        return []
    removed: list[str] = []
    try:
        if (root / LEGACY_LAUNCHER).exists():
            retargeted = True
            folders = (plat.desktop_dir(), plat.start_menu_programs_dir())
            # 不論那一顆叫什麼(現在的三種語言或以前的名字 `brand.LEGACY_NAMES`)都認;重建時換成
            # 現在介面語言的名字,舊名那一顆由 `install_to` 刪掉(2026-11-07 改名)
            names = (brand.APP_NAME, brand.APP_NAME_EN, brand.APP_NAME_ZH_CN, *brand.LEGACY_NAMES)
            for folder in folders:
                if folder is None or not any((Path(folder) / f"{n}.lnk").exists() for n in names):
                    continue
                try:
                    _load_make_shortcut(root).install_to(Path(folder))
                    logger.info("捷徑改指 %s:%s", LAUNCHER, folder)
                except Exception as e:  # noqa: BLE001
                    retargeted = False
                    logger.warning("捷徑沒能改指 %s(%s):%s", LAUNCHER, folder, e)
            if retargeted:
                removed += _unlink(root, (LEGACY_LAUNCHER,))
        else:
            _rename_legacy_shortcuts(root)
        removed += _unlink(root, LEGACY_ENTRIES)
        try:
            (root / LEGACY_DIR).rmdir()      # 只刪空的:使用者可能放了自己的東西
        except OSError:
            pass
    except Exception as e:  # noqa: BLE001
        logger.warning("舊入口清理失敗:%s", e)
    if removed:
        logger.info("刪掉舊的中文檔名入口:%s", ", ".join(removed))
    return removed


def _rename_legacy_shortcuts(root: Path) -> None:
    r"""捷徑還叫舊名(「AI 文件.MD 轉換器」、Scribe2MD)而且沒有新名的那一顆:照現在的介面語言重建、
    舊的刪掉(2026-11-07 改名為「會議助手電腦版」)。

    ⚠️ **v1.10.1 以前裝的副本換版上來時沒有 `啟動.vbs`**,不會走上面「改指 Launch.vbs」那條路——不另外做
    的話,桌面那顆一直叫舊名。⚠️ **已經有新名的那一顆就不重建,只把舊名那顆刪掉**(兩顆一樣的圖示,
    2026-11-07 Windows 實測);**沒有舊名的也不新建**(使用者自己刪掉的那顆不該被變回來)。"""
    from meeting_scribe import plat

    current = (brand.APP_NAME, brand.APP_NAME_EN, brand.APP_NAME_ZH_CN)
    for folder in (plat.desktop_dir(), plat.start_menu_programs_dir()):
        if folder is None:
            continue
        folder = Path(folder)
        legacy = [folder / f"{n}.lnk" for n in brand.LEGACY_NAMES if (folder / f"{n}.lnk").exists()]
        if not legacy:
            continue
        if any((folder / f"{n}.lnk").exists() for n in current):
            # 新名的已經有了:舊名那顆只是多出來的一顆一樣的圖示,刪掉就好(不重建,新名那顆不動)
            for lnk in legacy:
                try:
                    lnk.unlink()
                except OSError as e:
                    logger.warning("舊名捷徑刪不掉(%s):%s", lnk, e)
            continue
        try:
            _load_make_shortcut(root).install_to(folder)
            logger.info("捷徑改成新名字:%s", folder)
        except Exception as e:  # noqa: BLE001 - 開窗路徑上的順手清理,絕不拋例外
            logger.warning("捷徑沒能改名(%s):%s", folder, e)


def _unlink(root: Path, rels) -> list[str]:
    done = []
    for rel in rels:
        p = root / rel
        if not p.is_file():
            continue
        try:
            p.unlink()
            done.append(rel)
        except OSError as e:
            # 舊的 `啟動.vbs` 若正被 wscript 開著,就下次再刪
            logger.info("舊入口 %s 這次刪不掉:%s", rel, e)
    return done


def helper_source() -> Path:
    """換版小程式的原始碼(會被複製到工作目錄再跑,見 `plat_win.apply_update`)。"""
    return Path(__file__).with_name("update_helper.py")


# 換版小程式的訊息(Windows `update_helper.py`、Mac `plat_mac._HELPER_ZSH`)跟介面語言(使用者 2026-11-07:
# 英文介面不出現中文字)。小程式 import 不到 `i18n`(它跑在本程式關掉之後,`meeting_scribe` 正要被換掉),
# 所以由本程式翻好、放進環境變數 `UPD_<鍵>` 帶過去;小程式自己留一份繁中當預設——⚠️ **讀不到就用預設**,
# 新舊版本互相叫起來(舊版叫新版的小程式、或反過來)時照樣有話可說。繁中預設與這裡的原文由
# `tests/test_update.py` 釘成同一份。佔位 `{version}` `{reason}` `{n}` `{e}` `{title}` `{manual}` 由小程式填,
# Mac 的 `{backup}` 由 zsh 填
def helper_texts() -> dict[str, str]:
    """換版小程式要用的訊息(介面語言),鍵是環境變數 `UPD_` 後面那一段。"""
    manual = t("請到 GitHub 下載最新版,解壓縮後覆蓋到工具資料夾,再雙擊「Install.bat」:\n"
               "https://github.com/VincentLiang1/notetaker-desktop/releases/latest")
    return {
        "TITLE": t("會議助手電腦版"),   # 畫面上的程式名(同 `desktop.WINDOW_TITLE`)
        "PROGRESS": t("正在換上 v{version},完成後會自動重新開啟…"),
        "RELAUNCH_FAILED": t("更新完成,但沒能自動重新開啟。請自己開啟「{title}」。"),
        "MANUAL": manual,
        "NOT_CLOSED": t("更新沒有進行:程式沒有關掉。請關掉它之後,再從「❓ 使用說明」"
                        "最底下按一次「檢查更新」。"),
        "SYNC_FAILED": t("執行環境更新失敗(多半是網路或防毒軟體)"),
        "SWAP_FAILED": t("換檔失敗({e})"),
        "ROLLED_BACK": t("更新到 v{version} 沒有成功:{reason}。\n\n已經還原成原本的版本,"
                         "可以照常使用。詳情在工具資料夾的 logs 裡(update_ 開頭的那份)。"),
        "STUCK": t("更新到 v{version} 沒有成功,而且有 {n} 個檔沒能還原。\n\n{manual}"),
        "UNEXPECTED": t("更新時發生未預期的錯誤。\n\n{manual}"),
        "MAC_TIMEOUT": t("舊版沒有在兩分鐘內關閉,這次沒有更新,原本的版本都還在。請重新打開工具再試一次。"),
        "MAC_NOPERM": t("沒辦法換掉原本的 App(可能沒有權限),這次沒有更新,原本的版本都還在。"
                        "可以到下載頁手動更新。"),
        "MAC_PUT_RESTORED": t("新版放不上去,已還原成原本的版本。可以到下載頁手動更新。"),
        "MAC_PUT_STUCK": t("新版放不上去,原本的 App 也搬不回去,它現在在:{backup} "
                           "請把它拖回「應用程式」資料夾,或到下載頁重新下載。"),
        "MAC_OPEN_RESTORED": t("新版打不開,已還原成原本的版本。可以到下載頁手動更新。"),
        "MAC_OPEN_STUCK": t("新版打不開,原本的 App 也搬不回去,它現在在:{backup} "
                            "請把它拖回「應用程式」資料夾,或到下載頁重新下載。"),
    }


def helper_env() -> dict[str, str]:
    """`helper_texts` 包成環境變數(`UPD_<鍵>`),疊在本行程的環境上交給 `Popen(env=…)`。"""
    import os

    return {**os.environ, **{f"UPD_{k}": v for k, v in helper_texts().items()}}
