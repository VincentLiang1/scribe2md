r"""在桌面與「開始功能表」放上「AI 文件.MD 轉換器」的捷徑。

由「安裝.bat」在環境建好、而且實際 import 過一次之後呼叫(事後要重建也可以
直接 `uv run python scripts/make_shortcut.py`)。

**存在的理由是那三段路徑只有安裝當下才知道**:同仁把 zip 解壓到哪裡是他的
自由(README 教的是「桌面或 C:\ 底下,資料夾名稱用中文也可以」),所以捷徑
要指的「啟動.bat」、工作目錄、圖示檔**都得從這支腳本自己的位置往上推**——
任何一段寫死,就只有開發那台機器按得動,而別人桌面上會出現一顆指向不存在
路徑的死圖示,比沒有更糟。

**為什麼不叫 PowerShell 的 WScript.Shell**:.lnk 在 Windows 上只有 COM 一條
路,而 PowerShell 正是公司電腦最常被群組原則收走的東西(執行原則、
Constrained Language Mode 都擋得掉)。ctypes 直接叫 IShellLinkW 不經過任何
外部行程,也不必為了一顆捷徑多拉一個相依進來。

**中文不從 .bat 傳進來**:批次檔是 cp950,字串經 cmd 那一層會被重新編碼。
捷徑名稱與所有訊息都寫在這支 UTF-8 的 Python 裡,「安裝.bat」只負責呼叫一行
純 ASCII 的指令(`test_bat.py` 的 test_指令仍為_ascii 正好守著這件事)。

⚠️ **建不出來絕不能擋住安裝**:走到這一步環境已經好了,捷徑只是方便。桌面被
群組原則重導到唯讀的網路磁碟、OneDrive 沒登入、資安軟體擋住寫入,都會讓這裡
失敗——那時該做的是告訴他「雙擊資料夾裡的啟動.bat 一樣能用」,而不是讓他以為
安裝失敗、回頭重跑一次。離開碼只拿來讓「安裝.bat」挑最後那句話該怎麼寫,
**兩種都算安裝成功**:0=桌面那顆放好了,3=沒放成。
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import POINTER, byref, c_int, c_void_p, c_wchar_p
from pathlib import Path
from uuid import UUID

from meeting_scribe.paths import desktop_dir, start_menu_programs_dir

ROOT = Path(__file__).resolve().parents[1]

# 對使用者顯示的名稱(2026-08-09 定名)。⚠️ 這個字串在 repo 裡有好幾處,
# 要改名就 grep -rn 全 repo,別只改這裡——見 CLAUDE.md 開頭那條。
APP_NAME = "AI 文件.MD 轉換器"
LAUNCHER = "啟動.bat"
ICON = Path("src") / "meeting_scribe" / "assets" / "icon.ico"
# 滑鼠停在圖示上會看到這句。寫「不會上傳」是因為那是同仁最常問的第一個問題
DESCRIPTION = "把錄音、影片、文件轉成 Markdown(全程在這台電腦上跑,不會上傳)"

_S_OK = 0
_CLSCTX_INPROC_SERVER = 1
_CLSID_SHELL_LINK = "{00021401-0000-0000-C000-000000000046}"
_IID_ISHELL_LINK_W = "{000214F9-0000-0000-C000-000000000046}"
_IID_IPERSIST_FILE = "{0000010B-0000-0000-C000-000000000046}"

# vtable 上的**順序**,不是名字——數錯一格就是呼叫到隔壁那個方法,而那多半
# 是當場 crash 不是回錯誤碼。所以照介面宣告的順序整段抄在這裡對照:
#   IUnknown:      0 QueryInterface  1 AddRef  2 Release
#   IShellLinkW:   3 GetPath  4 GetIDList  5 SetIDList  6 GetDescription
#                  7 SetDescription  8 GetWorkingDirectory  9 SetWorkingDirectory
#                  10 GetArguments  11 SetArguments  12 GetHotkey  13 SetHotkey
#                  14 GetShowCmd  15 SetShowCmd  16 GetIconLocation
#                  17 SetIconLocation  18 SetRelativePath  19 Resolve  20 SetPath
#   IPersistFile:  3 GetClassID  4 IsDirty  5 Load  6 Save  7 SaveCompleted
_QUERY_INTERFACE, _RELEASE = 0, 2
_SET_DESCRIPTION, _SET_WORKING_DIRECTORY = 7, 9
_SET_ICON_LOCATION, _SET_PATH = 17, 20
_PERSIST_SAVE = 6


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(text: str) -> _GUID:
    u = UUID(text)
    return _GUID(u.time_low, u.time_mid, u.time_hi_version,
                 (ctypes.c_ubyte * 8)(*u.bytes[8:]))


def _call(obj: c_void_p, slot: int, argtypes: tuple, *args) -> int:
    """叫 COM 物件 vtable 上第 slot 個方法,回 HRESULT。

    argtypes 明寫不用 type(a) 推:byref() 回的是 CArgObject,推出來的型別
    是錯的,而錯的型別在這一層不會報錯、只會把垃圾推上堆疊。"""
    vtable = ctypes.cast(obj, POINTER(c_void_p))[0]
    method = ctypes.cast(vtable, POINTER(c_void_p))[slot]
    return ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, *argtypes)(method)(obj, *args)


def _check(hr: int, what: str) -> None:
    if hr != _S_OK:
        raise OSError(f"{what} 失敗(HRESULT 0x{hr & 0xFFFFFFFF:08X})")


def write_shortcut(dest: Path, target: Path, workdir: Path,
                   icon: Path, description: str) -> None:
    """寫出一個 .lnk;失敗一律拋例外(呼叫端決定要不要當成致命)。"""
    ole32 = ctypes.windll.ole32
    ole32.CoInitialize(None)
    try:
        clsid, iid = _guid(_CLSID_SHELL_LINK), _guid(_IID_ISHELL_LINK_W)
        link = c_void_p()
        _check(ole32.CoCreateInstance(byref(clsid), None, _CLSCTX_INPROC_SERVER,
                                      byref(iid), byref(link)),
               "CoCreateInstance(ShellLink)")
        try:
            _check(_call(link, _SET_PATH, (c_wchar_p,), str(target)), "SetPath")
            _check(_call(link, _SET_WORKING_DIRECTORY, (c_wchar_p,), str(workdir)),
                   "SetWorkingDirectory")
            _check(_call(link, _SET_DESCRIPTION, (c_wchar_p,), description),
                   "SetDescription")
            # 第二個參數是 .ico 裡的第幾張圖;我們的 icon.ico 只有一組尺寸,固定 0
            _check(_call(link, _SET_ICON_LOCATION, (c_wchar_p, c_int), str(icon), 0),
                   "SetIconLocation")

            persist_iid = _guid(_IID_IPERSIST_FILE)
            persist = c_void_p()
            _check(_call(link, _QUERY_INTERFACE, (c_void_p, c_void_p),
                         byref(persist_iid), byref(persist)),
                   "QueryInterface(IPersistFile)")
            try:
                # 第二個參數 fRemember=TRUE:把這個路徑記成物件目前的檔案
                _check(_call(persist, _PERSIST_SAVE, (c_wchar_p, c_int),
                             str(dest), 1), "Save")
            finally:
                _call(persist, _RELEASE, ())
        finally:
            _call(link, _RELEASE, ())
    finally:
        ole32.CoUninitialize()


def install_to(folder: Path) -> Path:
    """在 folder 底下建立(或覆寫)捷徑,回傳落地的 .lnk 路徑。

    同名一律覆寫:同仁被教的第一件事就是「搬過資料夾就重跑安裝.bat」,而
    那句話要成立,這裡就得真的把舊捷徑那條指到別處的路徑改回來。"""
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{APP_NAME}.lnk"
    write_shortcut(dest, ROOT / LAUNCHER, ROOT, ROOT / ICON, DESCRIPTION)
    return dest


def main() -> int:
    # 只改 errors,**不**改 encoding,也不呼叫 stdio.force_utf8:
    # 這支的讀者是使用者的眼睛,不是程式。連到真主控台時 PEP 528 已經保證
    # 中文顯示正確(底層走 WriteConsoleW,與黑視窗的 chcp 950 無關),釘不釘
    # UTF-8 都一樣;force_utf8 存在的理由是「輸出被接進 pipe、呼叫端要按
    # UTF-8 讀」,這裡沒有那個呼叫端。
    # 真正要防的是另一件事:輸出被導向檔案時 encoding 會退回 cp950,而工具
    # 資料夾的名字可能有 cp950 表達不了的字(README 說中文甚至 emoji 都可以)
    # ——那時 errors 若是預設的 strict,會在印路徑那一行整支炸掉。
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    launcher = ROOT / LAUNCHER
    if not launcher.is_file():
        print(f"[提醒] 找不到 {LAUNCHER},略過建立捷徑。")
        return 3

    # 只講原因,不講「那你改用啟動.bat」——那句由「安裝.bat」的 :nolnk 統一
    # 印。uv 整個沒跑起來時這支根本不會執行,指引只放在這裡就會漏掉那條路。
    try:
        install_to(desktop_dir())
    except Exception as exc:
        print(f"[提醒] 桌面圖示建立失敗:{exc}")
        return 3

    print(f"已經在桌面放上「{APP_NAME}」,以後雙擊它就能啟動。")

    # 開始功能表是備援,不是主角:桌面被公司政策鎖住時它通常還寫得進去,
    # 而且同仁可以按 Windows 鍵直接搜尋名字。失敗就安靜跳過——為了一個
    # 備援去嚇使用者,只會讓他以為安裝有問題。
    if (programs := start_menu_programs_dir()) is not None:
        try:
            install_to(programs)
            print("「開始功能表」裡也放了一份,按 Windows 鍵打名字就找得到。")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
