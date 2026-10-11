r"""路徑的單一出處:我們自己的落地位置,加上要問 Windows 才算得準的那幾個。

%LOCALAPPDATA%\meeting-scribe 底下的所有落地——AI 模型快取(models)、
命名進度(pending)、錄音工作目錄(recordings)——都從
appdata_root() 出發:基底路徑邏輯只此一份,要盤點「工具在使用者機器上
寫了什麼」看這裡即可。專案 data/(隨 repo 版控)不在此列,見
models.data_dir。

桌面與「開始功能表」(desktop_dir / start_menu_programs_dir)也收在這裡。
它們不是我們的落地位置,擺進來的理由只有一個:**那兩條路徑都不可以用
`~/Desktop` 猜**(原因見 desktop_dir),而這件事原本只寫在
scripts/md2fb.py 裡——建捷徑的腳本要是自己再寫一份,那個教訓就只會有
一邊記得。
"""
import logging
import os
from pathlib import Path

from meeting_scribe import plat

logger = logging.getLogger(__name__)

def repo_root() -> Path:
    r"""專案根目錄(output\、data\、docs\、logs\ 都掛在它底下)。

    src/meeting_scribe/paths.py → 往上兩層。editable 安裝(本專案的交付
    方式)直接跑 src,這條成立;真打成 wheel 裝進 site-packages 就不成立,
    而那正是要一處改、不是四處各自壞掉的理由。"""
    return Path(__file__).resolve().parents[2]


def assets_dir() -> Path:
    r"""套件自帶的靜態資產(程式圖示;隨 wheel 與交付副本走)。

    ⚠️ **不可以走 repo_root()**:那條在真打成 wheel 時不成立(見上),而圖示
    要跟著套件本身移動。內容由 `scripts/make_icon.py` 產生,不是手寫的。"""
    return Path(__file__).resolve().parent / "assets"


#: 2026-09-25 之前,非 Windows 的落地位置。⚠️ **不要拿它當「舊平台」的同義詞**
#: ——它是一個**歷史值**,新的機器不會再建這個目錄;留著只為了「已經有資料的
#: 那幾台」不要憑空再下載 1.7 GB 的模型。
_LEGACY_BASE = ".cache"


def legacy_appdata_root() -> Path:
    """2026-09-25 之前的落地位置(`~/.cache/meeting-scribe`)。"""
    return Path.home() / _LEGACY_BASE / "meeting-scribe"


def appdata_root() -> Path:
    r"""本專案在這台機器上的落地根目錄。

    ⚠️ **`LOCALAPPDATA` 明設過就完全照它**:那同時是測試的隔離鉤子
    (`monkeypatch.setenv`),換成平台分支的話整批 `test_models`/`test_pending`
    會改去讀**使用者真實的**落地目錄——那是假綠燈,也是弄髒真實資料。
    **每次呼叫重讀環境變數**,不能在 import 時定死。

    ⚠️ **沒設就問平台**(2026-09-25 改;先前一律退回 `~/.cache`):macOS 的
    慣例是 `~/Library/Application Support`,而 `~/.cache` 是 Linux 的 XDG 慣例
    ——macOS 上沒有任何系統工具或使用者會去那裡找應用程式資料,第三方清理
    軟體卻會。規格見 `docs/spec/mac/01` §1.6。

    ⚠️ **新位置還不存在、而舊位置有東西時,回舊的**——這一段是整個搬移敢做的
    理由:①命令列那條路**刻意不搬**(它可能在背景跑批次,不該順手動使用者
    幾 GB 的資料),但它照樣找得到模型;②視窗那邊的搬移**萬一失敗**,程式
    會繼續用舊位置,而不是對著一個空目錄重下 1.7 GB。
    ⚠️ **兩邊都在就用新的、不合併**:那是兩份狀態,猜錯比留著糟。"""
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "meeting-scribe"
    root = Path(plat.appdata_base()) / "meeting-scribe"
    if not root.exists():
        legacy = legacy_appdata_root()
        if legacy != root and legacy.is_dir():
            return legacy
    return root


def migrate_legacy_appdata() -> Path | None:
    """把舊位置整個搬到現在的位置;搬了回新位置,不必搬或搬不動回 None。

    ⚠️ **只在開窗那條路呼叫**(見 `appdata_root` 的第三條):命令列可能在
    背景跑批次,不該順手動使用者幾 GB 的資料。
    ⚠️ **搬不動只記一行、絕不拋**:搬移失敗不是「不能用」——`appdata_root`
    會繼續回舊位置,程式照常跑。因為搬不動而開不了程式,比不搬糟得多。
    ⚠️ **同一顆磁碟所以用 `os.rename`**:1.7 GB 也是瞬間完成的原子操作;
    跨磁碟時它會丟 `OSError`,而那正好走到上面那條「搬不動」。"""
    legacy = legacy_appdata_root()
    base = os.environ.get("LOCALAPPDATA")
    root = (Path(base) / "meeting-scribe" if base
            else Path(plat.appdata_base()) / "meeting-scribe")
    if root == legacy or not legacy.is_dir() or root.exists():
        return None
    try:
        root.parent.mkdir(parents=True, exist_ok=True)
        os.rename(legacy, root)
    except OSError as e:
        logger.warning("工具資料夾搬不動,繼續用原本的位置(%s):%s", legacy, e)
        return None
    logger.info("工具資料夾已搬到 %s(原本在 %s)", root, legacy)
    return root


def desktop_dir() -> Path:
    r"""桌面的實際位置(一定回得出一個 `Path`)。

    ⚠️ **不寫死 `~/Desktop`**:OneDrive 的「資料夾備份」會把桌面整個重導到
    `%USERPROFILE%\OneDrive\Desktop`,而寫死的那條往往還在、只是沒人看——
    檔案產出成功,使用者卻永遠找不到。怎麼問是平台的事(`plat.desktop_dir`)。"""
    return plat.desktop_dir()


def start_menu_programs_dir() -> Path | None:
    r"""這個使用者的「開始功能表\程式集」;`None` = 這台機器沒有這個東西。

    ⚠️ **`None` 不是錯誤**:它只是桌面捷徑的備援,少了不影響工具能不能用
    (macOS 的對應物是 `/Applications`,而那條走的是完全不同的交付路徑)。"""
    return plat.start_menu_programs_dir()
