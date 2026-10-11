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

from meeting_scribe import brand, paths

logger = logging.getLogger(__name__)

REPO = "VincentLiang1/scribe2md"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
#: 使用者手動更新時要去的頁面(Mac 版、或自動更新走不了的時候)
DOWNLOAD_PAGE = f"https://github.com/{REPO}/releases/latest"
#: ⚠️ 與 `scripts/release.py` 的 `WIN_ASSET` 是同一個名字(測試釘著)
WIN_ASSET = "scribe2md-win.zip"
#: ⚠️ 與 `scripts/release.py` 的 `MAC_ASSET` 是同一個名字(測試釘著)
MAC_ASSET = "scribe2md-mac.zip"
#: Mac 壓縮包的頂層:`make_mac_app.py` 打出來的 bundle 名(`ditto --keepParent`)。
#: ⚠️ **所以 App 改名就是改契約**:已裝出去的 Mac 版會照舊名找,找不到就拒收。
MAC_APP = f"{brand.APP_TITLE}.app"
#: bundle 裡那份程式(首次啟動時被複製到落地根目錄,見 `make_mac_app.py`)
MAC_PAYLOAD = f"{MAC_APP}/Contents/Resources/payload"
#: ⚠️ 與 `scripts/make_public.py` 寫進 zip 的頂層資料夾是同一個名字(測試釘著)
TOP = "scribe2md"
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
    worktree,這條線就是)或資料夾都算——與 `安裝.bat` 略過捷徑的判準同一個。"""
    root = root or paths.repo_root()
    return (root / ".git").exists()


def notes_from_body(body: str | None) -> str:
    r"""從 Release 內文取出「這一版改了什麼」。

    內文的形狀由 `release.build_notes` 決定:`## v1.2.0 更新內容`、空行、本次更新、
    `---`、固定不變的產品介紹。只要前一半,標題也拿掉(對話框自己會寫版號);
    `**粗體**` 的星號在 Tk 的純文字框裡只會是雜訊,一併拿掉。"""
    text = (body or "").replace("\r\n", "\n")
    text = text.split("\n---\n", 1)[0]
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
    data = models.with_tls_rescue(lambda: _get_json(LATEST_API, timeout), "檢查新版")
    tag = str(data.get("tag_name") or "")
    if parse_version(tag) is None:
        raise ValueError(f"GitHub 回的版號認不得:{tag!r}")
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
            return f"工具資料夾沒有寫入權限({d})"
    return None


def download(rel: Release, dest: Path, progress=None,
             stop: threading.Event | None = None) -> Path:
    r"""把壓縮包下載到 `dest`,邊下邊報進度 `progress(已下載, 總數)`。

    ⚠️ **自己寫迴圈,不用 `urlretrieve`**:那個停不下來,而這一趟要有「取消」。
    ⚠️ **大小與摘要都要對得上**:斷線時 urllib 不一定丟例外,只是檔案短了一截——
    那份 zip 解得開一半,換到一半才炸是最糟的時機。"""
    from meeting_scribe import models

    if not rel.asset_url:
        raise ValueError("這一版沒有附這台電腦用的壓縮包")
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
            raise ValueError(f"下載不完整({done} / {rel.asset_size} 位元組)")
        if rel.asset_sha256 and sha.hexdigest() != rel.asset_sha256:
            raise ValueError("下載的檔案摘要對不上,可能在傳輸中損毀")

    try:
        models.with_tls_rescue(fetch, "新版程式")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(dest)
    return dest


def verify_zip(path: Path, version: str) -> None:
    r"""打開之前先驗:形狀不對就丟 `ValueError`,一個檔都不寫。

    ⚠️ **含 `data/` 的包一律拒收**:解開覆蓋下去就是把使用者的聲紋與名單換成別人
    的(或清空),而那在覆蓋的那一刻就已經造成損失(`docs/spec/12` §12.3)。
    ⚠️ **路徑一律要在 `scribe2md/` 底下**,`..` 與絕對路徑拒收(zip slip)。
    ⚠️ **版號要跟 GitHub 說的那一版對得上**:附錯檔的 Release 不該把人換到另一版去。"""
    with zipfile.ZipFile(path) as z:
        bad = z.testzip()
        if bad is not None:
            raise ValueError(f"壓縮包損毀({bad})")
        names = z.namelist()
        prefix = TOP + "/"
        for n in names:
            parts = n.split("/")
            if (not n.startswith(prefix) or ".." in parts or ":" in n
                    or n.startswith("/") or "\\" in n):
                raise ValueError(f"壓縮包裡有不該出現的路徑:{n}")
            if len(parts) > 2 and parts[1] in NEVER_TOUCH:
                raise ValueError(f"壓縮包裡帶著 {parts[1]} 資料夾,拒絕安裝")
        for need in ("pyproject.toml", "VERSION", "src/meeting_scribe/desktop.py"):
            if prefix + need not in names:
                raise ValueError(f"壓縮包裡少了 {need}")
        pyproject = z.read(prefix + "pyproject.toml").decode("utf-8")
    got = _pyproject_version(pyproject)
    if parse_version(got or "") != parse_version(version):
        raise ValueError(f"壓縮包裡的版號是 {got},不是 {version}")


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
    ⚠️ **頂層只能是 `MAC_APP` 那一個 App**;⚠️ **payload 裡出現 `data/` 一律拒收**——
    升級時那一份本來就不會覆蓋(`make_mac_app.py` 的 rsync 排除了它),但帶著它出貨就是
    把某個人的聲紋與名單公開出去,在這裡擋第二次;⚠️ **版號讀 payload 的 `pyproject.toml`**,
    不讀 `Info.plist`——那裡的兩個欄位放的是日期與 commit(`make_mac_app.py`)。
    簽章與符號連結這兩件 zipfile 驗不到,由 `plat_mac.stage_update` 解開之後驗。"""
    with zipfile.ZipFile(path) as z:
        bad = z.testzip()
        if bad is not None:
            raise ValueError(f"壓縮包損毀({bad})")
        infos = {_zip_name(i): i for i in z.infolist()}
        prefix = MAC_APP + "/"
        payload = MAC_PAYLOAD + "/"
        for n in infos:
            outside = (not n.startswith(prefix) or n.startswith("/")
                       or "\\" in n or ":" in n)
            if outside or ".." in n.split("/"):
                raise ValueError(f"壓縮包裡有不該出現的路徑:{n}")
            if n.startswith(payload):
                inner = n[len(payload):].split("/")
                if len(inner) > 1 and inner[0] in NEVER_TOUCH:
                    raise ValueError(f"壓縮包裡帶著 {inner[0]} 資料夾,拒絕安裝")
        for need in ("Contents/Info.plist", "Contents/MacOS/"):
            if not any(n.startswith(prefix + need) for n in infos):
                raise ValueError(f"壓縮包裡少了 {need}")
        for need in ("pyproject.toml", "VERSION", "src/meeting_scribe/desktop.py"):
            if payload + need not in infos:
                raise ValueError(f"壓縮包裡少了 {need}")
        pyproject = z.read(infos[payload + "pyproject.toml"]).decode("utf-8")
    got = _pyproject_version(pyproject)
    if got is None or parse_version(got) != parse_version(version):
        raise ValueError(f"壓縮包裡的版號是 {got},不是 {version}")


def _pyproject_version(text: str) -> str | None:
    import tomllib

    try:
        return tomllib.loads(text)["project"]["version"]
    except Exception:  # noqa: BLE001
        return None


def stage(zip_path: Path, into: Path) -> Path:
    """解壓到 `into`,回傳解出來的 `scribe2md` 資料夾(換版小程式拿它去覆蓋)。"""
    if into.exists():
        shutil.rmtree(into)
    into.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(into)
    return into / TOP


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


def helper_source() -> Path:
    """換版小程式的原始碼(會被複製到工作目錄再跑,見 `plat_win.apply_update`)。"""
    return Path(__file__).with_name("update_helper.py")
