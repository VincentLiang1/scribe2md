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
import ctypes
import os
from pathlib import Path
from uuid import UUID

# Windows 的「已知資料夾」GUID(shlobj_core.h 的 FOLDERID_*)
_DESKTOP_GUID = "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"
_PROGRAMS_GUID = "{A77F5D77-2E2B-44C3-A6A2-ABA601054A51}"   # 開始功能表\程式集


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


def appdata_root() -> Path:
    r"""%LOCALAPPDATA%\meeting-scribe;無 LOCALAPPDATA 的環境退回 ~/.cache。

    每次呼叫重讀環境變數:測試以 monkeypatch.setenv 隔離,不能在 import
    時定死。"""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".cache")
    return Path(base) / "meeting-scribe"


def known_folder(guid: str) -> Path | None:
    r"""問 Windows 要「已知資料夾」的實際位置,問不到回 None。

    ⚠️ 不用 ctypes.wintypes 湊 GUID 結構:那個模組在非 Windows 上 import
    就會炸,而 paths 是全專案都會載到的 leaf 模組。改用 ctypes 的基本
    型別自己排,欄位寬度是一樣的。"""

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

    不寫死 `~/Desktop`:OneDrive 的「資料夾備份」會把桌面整個重導到
    `%USERPROFILE%\OneDrive\Desktop`,而寫死的那條路徑往往還在、只是沒人看——
    檔案產出成功,使用者卻永遠找不到。所以先問 Windows,問不到才退回猜。"""
    return known_folder(_DESKTOP_GUID) or next(
        (p for p in (Path.home() / "OneDrive" / "Desktop", Path.home() / "Desktop")
         if p.is_dir()), Path.home())


def start_menu_programs_dir() -> Path | None:
    r"""這個使用者的「開始功能表\程式集」;問不到才退回 %APPDATA% 那條。

    回 None 代表連退路都不成立(非 Windows、或 APPDATA 不在)——呼叫端要
    當成「這台機器沒有開始功能表」處理,不是當成錯誤:它只是桌面捷徑的
    備援,少了不影響工具能不能用。"""
    if found := known_folder(_PROGRAMS_GUID):
        return found
    if base := os.environ.get("APPDATA"):
        return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    return None
