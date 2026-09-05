r"""路徑的單一出處:下游自己的落地位置,加上要問 Windows 才算得準的那幾個。

`%LOCALAPPDATA%\<下游的資料夾名>` 底下的所有落地——模型快取、皮膚快取、編譯快取
——都從 `appdata_root()` 出發:基底路徑邏輯只此一份,要盤點「這支工具在使用者機器
上寫了什麼」看這裡即可。⚠️ 專案底下的 `logs\`(隨專案走、可以整包刪)不在此列,
它在 `filelog`。

桌面與「開始功能表」(`desktop_dir` / `start_menu_programs_dir`)也收在這裡。它們
不是落地位置,擺進來的理由只有一個:**那兩條路徑都不可以用 `~/Desktop` 猜**
(原因見 `desktop_dir`),而這件事原本只寫在某個專案的捷徑腳本裡——別的地方哪天也
要用,那個教訓就只會有一邊記得。

⚠️ **本檔的祖本是 `C:\SOURCE5\Python\meeting-scribe\src\meeting_scribe\paths.py`**
(450 筆提交,實際踩過 OneDrive 重導與非 Windows 匯入那兩個坑),2026-08-27 抄進
MP4-2-SRT、2026-08-28 搬進本包。⚠️ **meeting-scribe 不接 winkit**(它正往 mac 走),
所以那邊維持自己那一份,不要回頭改它。

⚠️ **這裡沒有任何身分值,也不准用自己的 `__file__` 推位置**。住在下游的時候,
「我在哪」是 `Path(__file__).resolve().parents[2]` 算出來的;搬進共用包之後那條會
指到 **winkit 自己**——紀錄檔寫進 `winkit\logs`、版本號讀成 winkit 的 `.git`、
資產在那邊找不到,而**三個症狀都沒有錯誤訊息、看起來全都像「東西不見了」**。位置
一律走 `winkit.host()`(由下游在 `bind()` 時給)。

⚠️ **`appdata_root()` 每次呼叫重讀環境變數,不可以在 import 時定死**:測試靠
`monkeypatch.setenv("LOCALAPPDATA", ...)` 把落地導進 tmp,定死就導不動了,而那會
讓測試在**開發者自己的家目錄**裡長出東西。
"""
import ctypes
import os
from pathlib import Path
from uuid import UUID

from winkit import host

# Windows 的「已知資料夾」GUID(shlobj_core.h 的 FOLDERID_*)
_DESKTOP_GUID = "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"
_PROGRAMS_GUID = "{A77F5D77-2E2B-44C3-A6A2-ABA601054A51}"   # 開始功能表\程式集


def repo_root() -> Path:
    r"""下游的專案根(`scripts\`、`docs\`、`logs\`、那幾個 `.bat` 掛在它底下)。

    ⚠️ **值由下游注入,本包不准自己推**:src layout 要從套件目錄往上兩層、flat
    layout 只有一層,推的那個版本會在其中一邊安靜地算錯。⚠️ 這條在真打成 wheel 裝
    進 site-packages 時本來就不成立——而那正是要一處給、不是散在各處各自壞掉的
    理由。"""
    return host().repo_root


def package_dir() -> Path:
    """下游套件自己的目錄。⚠️ 與 `repo_root()` 分開的理由見 `assets_dir()`。"""
    return host().package_dir


def assets_dir() -> Path:
    r"""下游套件自帶的靜態資產(程式圖示與皮膚;隨 wheel 與交付副本走)。

    ⚠️ **不可以走 `repo_root()`**:那條在真打成 wheel 時不成立(見上),而資產要跟著
    套件本身移動。內容由產生器腳本產出,不是手寫的。"""
    return package_dir() / "assets"


def appdata_root() -> Path:
    r"""`%LOCALAPPDATA%\<下游的資料夾名>`;沒有 LOCALAPPDATA 的環境退回 `~/.cache`。

    ⚠️ **每次呼叫重讀環境變數**:測試以 `monkeypatch.setenv` 隔離,不能在 import 時
    定死(見模組 docstring)。
    ⚠️ **不可以寫回專案資料夾**:那裡可能是唯讀(Program Files、公司政策掛的網路
    磁碟),而且使用者換電腦是複製整個資料夾——把機器專屬的東西(某台機器的 DPI 畫
    出來的皮膚、幾 GB 的模型)一起複製過去,不是慢就是錯。
    ⚠️ **走 `LOCALAPPDATA` 不是 `APPDATA`**:這底下全是**衍生自這台機器**的產物
    ——照本機顯示設定畫出來的皮膚、為這顆晶片編譯的快取、幾 GB 的模型。漫遊設定檔
    會跟著使用者跑到另一台機器上,而那台的 DPI 與晶片都不一樣(指紋擋得住不相容,
    但那就變成每台機器互相把對方的快取洗掉),模型則是根本不該漫遊。"""
    return local_appdata(host().app_dir_name)


def local_appdata(*parts: str) -> Path:
    r"""`%LOCALAPPDATA%` 底下的任意路徑(**不**自動套用下游的資料夾名)。

    ⚠️ 存在的理由是**姊妹專案的快取**:同一顆模型別的專案下載過就直接沿用、不重下
    幾 GB。自己的落地一律走 `appdata_root()`,不要拿這一支去拼。"""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".cache")
    return Path(base).joinpath(*parts)


def known_folder(guid: str) -> Path | None:
    r"""問 Windows 要「已知資料夾」的實際位置,問不到回 None。

    ⚠️ **不用 `ctypes.wintypes` 湊 GUID 結構**:那個模組在非 Windows 上 import 就會
    炸,而這裡是誰都會載到的 leaf 模組。改用 ctypes 的基本型別自己排,欄位寬度
    是一樣的。"""

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_ulong),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    try:
        u = UUID(guid)
        g = _GUID(u.time_low, u.time_mid, u.time_hi_version,
                  (ctypes.c_ubyte * 8)(*u.bytes[8:]))
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(
                ctypes.byref(g), 0, None, ctypes.byref(out)) != 0:
            return None
        try:
            return Path(out.value)
        finally:
            ctypes.windll.ole32.CoTaskMemFree(out)
    except Exception:
        return None


def desktop_dir() -> Path:
    r"""桌面的實際位置。

    ⚠️ **不寫死 `~/Desktop`**:OneDrive 的「資料夾備份」會把桌面整個重導到
    `%USERPROFILE%\OneDrive\Desktop`,而寫死的那條路徑往往還在、只是沒人看——捷徑
    建立成功,使用者卻永遠看不到。所以先問 Windows,問不到才退回猜。"""
    return known_folder(_DESKTOP_GUID) or next(
        (p for p in (Path.home() / "OneDrive" / "Desktop", Path.home() / "Desktop")
         if p.is_dir()), Path.home())


def start_menu_programs_dir() -> Path | None:
    r"""這個使用者的「開始功能表\程式集」;問不到才退回 `%APPDATA%` 那條。

    回 None 代表連退路都不成立(非 Windows、或 APPDATA 不在)——呼叫端要當成「這台
    機器沒有開始功能表」處理,不是當成錯誤:它只是桌面捷徑的備援,少了不影響工具
    能不能用。"""
    if found := known_folder(_PROGRAMS_GUID):
        return found
    if base := os.environ.get("APPDATA"):
        return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    return None
