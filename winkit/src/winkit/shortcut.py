r"""桌面 / 開始功能表的捷徑:IShellLink 與 PropertyStore 那一整套 COM。

**為什麼需要它**:程式執行時宣告的 AppUserModelID 只決定「這個視窗歸到哪一隊」,
而**那個身分的圖示登記在一顆 .lnk 上**——沒有捷徑就沒有圖示可用,工作列會沿用啟動鏈
上游那支執行檔的圖示。⚠️ 所以 `write_shortcut()` 一定要把身分寫進去(下面
`_PKEY_AUMID_*` 那一段),而它與程式宣告的**必須同值**。

⚠️ **這裡只有「怎麼寫一顆 .lnk」,沒有「要放在哪、叫什麼名字」**——落點、捷徑名、
給使用者看的每一句話都是下游那支安裝腳本的事(那是它的身分)。

⚠️ **vtable 是照順序呼叫的,不是照名字**:數錯一格就是呼叫到隔壁那個方法,而那多半
是當場 crash 不是回錯誤碼。所以介面宣告的順序整段抄在下面對照。
"""
from __future__ import annotations

import ctypes
import os
from ctypes import POINTER, byref, c_int, c_void_p, c_wchar_p
from pathlib import Path
from uuid import UUID

from winkit import host


_S_OK = 0
_CLSCTX_INPROC_SERVER = 1
_CLSID_SHELL_LINK = "{00021401-0000-0000-C000-000000000046}"
_IID_ISHELL_LINK_W = "{000214F9-0000-0000-C000-000000000046}"
_IID_IPERSIST_FILE = "{0000010B-0000-0000-C000-000000000046}"
_IID_IPROPERTY_STORE = "{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}"
# PKEY_AppUserModel_ID(propkey.h):工作列拿來認「這是哪個應用程式」
_PKEY_AUMID_FMTID = "{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}"
_PKEY_AUMID_PID = 5

# vtable 上的**順序**,不是名字——數錯一格就是呼叫到隔壁那個方法,而那多半是當場
# crash 不是回錯誤碼。所以照介面宣告的順序整段抄在這裡對照:
#   IUnknown:      0 QueryInterface  1 AddRef  2 Release
#   IShellLinkW:   3 GetPath  4 GetIDList  5 SetIDList  6 GetDescription
#                  7 SetDescription  8 GetWorkingDirectory  9 SetWorkingDirectory
#                  10 GetArguments  11 SetArguments  12 GetHotkey  13 SetHotkey
#                  14 GetShowCmd  15 SetShowCmd  16 GetIconLocation
#                  17 SetIconLocation  18 SetRelativePath  19 Resolve  20 SetPath
#   IPersistFile:  3 GetClassID  4 IsDirty  5 Load  6 Save  7 SaveCompleted
_QUERY_INTERFACE, _RELEASE = 0, 2
_SET_DESCRIPTION, _SET_WORKING_DIRECTORY = 7, 9
_SET_ARGUMENTS = 11
_SET_ICON_LOCATION, _SET_PATH = 17, 20
_PERSIST_SAVE = 6
#   IPropertyStore: 3 GetCount  4 GetAt  5 GetValue  6 SetValue  7 Commit
_PS_SET_VALUE, _PS_COMMIT = 6, 7


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", ctypes.c_ulong)]


class _PROPVARIANT(ctypes.Structure):
    """只夠用來裝一個 VT_LPWSTR 的 PROPVARIANT。

    ⚠️ **尾巴那個 `pad` 不可以省**:真正的 PROPVARIANT 是 24 bytes(x64)/16(x86),
    而 `PropVariantClear` 會把整個結構清成零——宣告短了就是寫出界 8 個位元組。
    這裡刻意排成與真實大小相同(8 + 指標 + 指標)。
    ⚠️ 也不要找 `InitPropVariantFromString`:那是 propvarutil.h 的 **inline** 函式,
    propsys.dll 沒有匯出這個符號(NotebookLM_OCR 2026-08-25 實測 not found)。"""
    _fields_ = [("vt", ctypes.c_ushort),
                ("r1", ctypes.c_ushort),
                ("r2", ctypes.c_ushort),
                ("r3", ctypes.c_ushort),
                ("p", c_void_p),
                ("pad", c_void_p)]


_VT_LPWSTR = 31


def _propvariant_str(text: str) -> _PROPVARIANT:
    """把字串包成 VT_LPWSTR 的 PROPVARIANT;字串用 CoTaskMemAlloc 配置,才能由
    `PropVariantClear` 收回去。"""
    ole32 = ctypes.windll.ole32
    ole32.CoTaskMemAlloc.restype = c_void_p          # ⚠️ 不設就會在 x64 被截斷
    ole32.CoTaskMemAlloc.argtypes = [ctypes.c_size_t]
    buf = (text + chr(0)).encode("utf-16-le")
    mem = ole32.CoTaskMemAlloc(len(buf))
    if not mem:
        raise MemoryError("CoTaskMemAlloc 失敗")
    ctypes.memmove(mem, buf, len(buf))
    pv = _PROPVARIANT()
    pv.vt = _VT_LPWSTR
    pv.p = mem
    return pv


def _guid(text: str) -> _GUID:
    u = UUID(text)
    return _GUID(u.time_low, u.time_mid, u.time_hi_version,
                 (ctypes.c_ubyte * 8)(*u.bytes[8:]))


def script_host() -> Path | None:
    """`wscript.exe` 的位置(見模組 docstring 的第一條 ⚠️)。找不到回 None。"""
    host = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "wscript.exe"
    return host if host.is_file() else None


def _call(obj: c_void_p, slot: int, argtypes: tuple, *args) -> int:
    """叫 COM 物件 vtable 上第 slot 個方法,回 HRESULT。

    ⚠️ argtypes 明寫、不用 `type(a)` 推:`byref()` 回的是 CArgObject,推出來的型別
    是錯的,而錯的型別在這一層不會報錯、只會把垃圾推上堆疊。"""
    vtable = ctypes.cast(obj, POINTER(c_void_p))[0]
    method = ctypes.cast(vtable, POINTER(c_void_p))[slot]
    return ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, *argtypes)(method)(obj, *args)


def _check(hr: int, what: str) -> None:
    """⚠️ **COM 的成功判準是 `hr >= 0`,不是 `hr == 0`**(2026-08-27 code review 抓到,
    原本寫的是 `!= _S_OK`)。`S_FALSE`(1)與 `INPLACE_S_TRUNCATED`(0x000401A0)都是
    **成功**碼:shell 的連結處理常式在重導向資料夾、雲端同步的桌面上真的會回它們,
    而把成功當失敗的下場是——.lnk 已經好好地放在桌面上,安裝卻印「桌面圖示建立
    失敗」。`winui.py` 那幾支 COM 呼叫一直都是 `< 0`,這裡跟它一致。"""
    if hr < 0:
        raise OSError(f"{what} 失敗(HRESULT 0x{hr & 0xFFFFFFFF:08X})")


def write_shortcut(dest: Path, target: Path, arguments: str, workdir: Path,
                   icon: Path, description: str) -> None:
    """寫出一個 .lnk;失敗一律拋例外(呼叫端決定要不要當成致命)。"""
    ole32 = ctypes.windll.ole32
    # ⚠️ **`CoUninitialize` 只能配對呼叫**(2026-08-27 code review 抓到):`CoInitialize`
    # 回 `S_OK`/`S_FALSE` 時這條執行緒的計數加了一,要還;回 `RPC_E_CHANGED_MODE`
    # (這條執行緒已經是 MTA)時它**什麼都沒做**,那時還下去就是拆掉呼叫端的
    # apartment——後面每一個 COM 呼叫都會拿到 `CO_E_NOTINITIALIZED`,而且錯在別的
    # 地方爆。今天這支是獨立行程、永遠 S_OK,但 `write_shortcut` 是個公開函式。
    hr_init = ole32.CoInitialize(None)
    try:
        clsid, iid = _guid(_CLSID_SHELL_LINK), _guid(_IID_ISHELL_LINK_W)
        link = c_void_p()
        _check(ole32.CoCreateInstance(byref(clsid), None, _CLSCTX_INPROC_SERVER,
                                      byref(iid), byref(link)),
               "CoCreateInstance(ShellLink)")
        try:
            _check(_call(link, _SET_PATH, (c_wchar_p,), str(target)), "SetPath")
            _check(_call(link, _SET_ARGUMENTS, (c_wchar_p,), arguments),
                   "SetArguments")
            _check(_call(link, _SET_WORKING_DIRECTORY, (c_wchar_p,), str(workdir)),
                   "SetWorkingDirectory")
            _check(_call(link, _SET_DESCRIPTION, (c_wchar_p,), description),
                   "SetDescription")
            # 第二個參數是 .ico 裡的第幾張圖;icon.ico 是同一個圖示的六個尺寸、
            # 不是六張不同的圖,所以固定 0(Windows 自己會挑合適的尺寸)
            _check(_call(link, _SET_ICON_LOCATION, (c_wchar_p, c_int), str(icon), 0),
                   "SetIconLocation")

            # ⚠️ **這一段就是工作列圖示的來源**(見模組 docstring 的第一條理由),
            # 而且要在 IPersistFile::Save **之前**寫:Commit 只改記憶體裡的那個連結
            # 物件,真正落檔的是後面那個 Save。少了它,使用者把捷徑釘到工作列之後,
            # 釘的那顆與執行中的視窗還會是兩個按鈕(釘選那顆的身分是從 wscript.exe
            # 推出來的)。
            # ⚠️ **問不到這個介面就要講一句**(2026-08-27 code review 抓到):原本是
            # 靜靜跳過,而跳過的結果正好是使用者回報的那個 bug——桌面圖示好好的、
            # 工作列還是 wscript 的圖示,安裝卻印「安裝完成」。捷徑本身仍然值得存
            # (雙擊得動),所以不拋例外,只把「重要的那一半沒成功」說出來。
            store_iid = _guid(_IID_IPROPERTY_STORE)
            store = c_void_p()
            hr_store = _call(link, _QUERY_INTERFACE, (c_void_p, c_void_p),
                             byref(store_iid), byref(store))
            if hr_store < 0:
                print(f"[提醒] 這台機器的捷徑寫不進應用程式身分"
                      f"(HRESULT 0x{hr_store & 0xFFFFFFFF:08X}),"
                      "工作列的圖示可能不是本工具的那一顆。")
            else:
                try:
                    key = _PROPERTYKEY(_guid(_PKEY_AUMID_FMTID), _PKEY_AUMID_PID)
                    prop = _propvariant_str(host().app_id)
                    try:
                        _check(_call(store, _PS_SET_VALUE, (c_void_p, c_void_p),
                                     byref(key), byref(prop)),
                               "SetValue(AppUserModelID)")
                        _check(_call(store, _PS_COMMIT, ()), "Commit")
                    finally:
                        ole32.PropVariantClear(byref(prop))
                # ⚠️ **這一段失敗不可以把整顆捷徑拖下水**(理由同上面那條):寫不進身分
                # 的捷徑仍然雙擊得動,而拋出去的話連桌面圖示都不會落地——那比只有
                # 工作列圖示不對更糟。
                except OSError as exc:
                    print(f"[提醒] 應用程式身分沒寫進捷徑({exc}),"
                          "工作列的圖示可能不是本工具的那一顆。")
                finally:
                    _call(store, _RELEASE, ())

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
        if hr_init >= 0:            # 見上面那段:RPC_E_CHANGED_MODE 不可以還
            ole32.CoUninitialize()
