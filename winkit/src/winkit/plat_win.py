"""平台層的 Windows 實作。只由 `winkit.plat._impl()` 選用,不要直接 import。

⚠️ **模組層只准 import 標準函式庫,而且不准碰 `ctypes.windll`**:`plat._impl()` 每次
都會把三份實作一起載進來,這一份在 macOS 上也會被 import——模組層碰到 `windll` 或
`winreg` 就是在那邊直接炸掉。`ctypes` 本身(`Structure`、`c_long`…)是跨平台的,可以
在模組層用;真的呼叫 Win32 的那一行一律放進函式裡。

⚠️ **這一份只負責「怎麼跟 Windows 講話」**:何時呼叫、失敗了怎麼辦、為什麼要做,
都寫在 `winui` 對應的那一支上(2026-09-27 從那邊搬來的,本體原樣照抄)。**全份同一條
原則:純外觀,任何一步失敗就回 `failed`、絕不拋例外**——這些呼叫沒有一個值得讓轉檔
停下來。
"""

import ctypes
from pathlib import Path

from winkit.plat import Outcome

NAME = "windows"

#: 停用中的按鈕用的 Tk 游標名。Windows 的 Tk 內建的禁止游標
DISABLED_CURSOR = "no"

#: 彩色 emoji 的字型。⚠️ **Windows 7 以後都有**;真的缺了,呼叫端退回文字圖示,
#: 不是壞掉。
EMOJI_FONT = Path(r"C:\Windows\Fonts\seguiemj.ttf")
#: 向量字型,任意尺寸都畫得出來
EMOJI_STRIKES: tuple[int, ...] = ()


def appdata_base() -> Path:
    """`%LOCALAPPDATA%` 的實際位置。⚠️ 只是退路——正常的 Windows 一定有那個環境
    變數,呼叫端會先用它;會走到這裡的是被剝掉環境的服務帳號。"""
    return Path.home() / "AppData" / "Local"


# --- 啟動期:DPI 與工作列身分 -------------------------------------------------


def enable_dpi_awareness() -> Outcome:
    """`SetProcessDpiAwareness(1)`(Win8.1+),舊系統退回 `SetProcessDPIAware`。"""
    try:                                   # Win8.1+:PROCESS_SYSTEM_DPI_AWARE
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return Outcome("ok")
    except Exception:
        try:                               # Win7/8 的舊 API
            ctypes.windll.user32.SetProcessDPIAware()
            return Outcome("ok")
        except Exception as e:             # 沒有就算了:只是回到會鋸齒的舊行為
            return Outcome("failed", f"DPI 感知設不上:{type(e).__name__}: {e}")


def set_app_user_model_id(app_id: str) -> Outcome:
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        return Outcome("ok")
    except Exception as e:                 # 純外觀,失敗就回到舊行為
        return Outcome("failed", f"工作列身分設不上:{type(e).__name__}: {e}")


# --- 視窗本身 -------------------------------------------------------------------


def window_handle(root) -> int:
    """`GetParent(winfo_id())`,拿不到回 0。

    ⚠️ **`restype` 一定要設**:`ctypes.windll` 的預設回傳型別是 `c_int`(32 位元),
    而 64 位元 Windows 的 HWND 是指標寬。值小的時候看起來完全正常,一旦某次配到
    高位元有值的 handle 就會被**靜默截斷**成另一個視窗的號碼——那種 bug 只會偶爾
    發生一次,查起來毫無線索。"""
    try:
        user32 = ctypes.windll.user32
        user32.GetParent.restype = ctypes.c_void_p
        user32.GetParent.argtypes = [ctypes.c_void_p]
        return user32.GetParent(root.winfo_id()) or 0
    except Exception:
        return 0


def use_dark_titlebar(root, hwnd_of) -> Outcome:
    """DWM 屬性 20(`DWMWA_USE_IMMERSIVE_DARK_MODE`,Windows 10 20H1+)。

    ⚠️ `hwnd_of` 由 `winui` 傳進來(就是 `winui.window_handle`):下游的測試樁的是
    那一支,在這裡自己問就繞過了它們的樁。"""
    try:
        root.update_idletasks()          # 先讓 HWND 真的存在
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd_of(root)), 20,
            ctypes.byref(ctypes.c_int(1)), ctypes.sizeof(ctypes.c_int))
        return Outcome("ok")
    except Exception as e:
        return Outcome("failed", f"深色標題列設不上:{type(e).__name__}: {e}")


# --- 輸入法 ---------------------------------------------------------------------


def ime_needs_help() -> bool:
    """Windows 的 Tk 只做了一半:它會交代組字**位置**(而且會被自己的快取吃掉),但
    從不交代組字**字型**——所以 `winui` 那兩支 `follow_ime_*` 在這個平台要補。"""
    return True


def set_ime_composition_font(hwnd: int, logfont) -> Outcome:
    """`ImmSetCompositionFontW`。`logfont` 是 `winui._LOGFONTW`(填法的理由在那邊)。

    ⚠️ **視窗還藏著的時候拿不到那顆 context**(`ImmGetContext` 回 NULL,2026-09-12
    實測)——那是 `failed` 不是錯,`<FocusIn>` 那條會再補一次。"""
    try:
        imm = ctypes.windll.imm32
        imm.ImmGetContext.restype = ctypes.c_void_p
        imm.ImmGetContext.argtypes = [ctypes.c_void_p]
        imm.ImmSetCompositionFontW.argtypes = [ctypes.c_void_p,
                                               ctypes.POINTER(type(logfont))]
        imm.ImmReleaseContext.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        himc = imm.ImmGetContext(ctypes.c_void_p(hwnd))
        if not himc:
            return Outcome("failed", "拿不到 IME context(視窗可能還藏著)")
        try:
            imm.ImmSetCompositionFontW(ctypes.c_void_p(himc), ctypes.byref(logfont))
        finally:
            imm.ImmReleaseContext(ctypes.c_void_p(hwnd), ctypes.c_void_p(himc))
        return Outcome("ok")
    except Exception as e:
        return Outcome("failed", f"組字字型設不上:{type(e).__name__}: {e}")


# --- 還原那一幀的底色(成因與兩條否決見 `winui` 那一段)---------------------

_GCLP_HBRBACKGROUND = -10

# 同一個顏色只建一支 brush。⚠️ **不可以 `DeleteObject`**:handle 掛在 window class 上,
# 而 class 比視窗長壽(行程內所有 Tk 視窗共用它);刪掉之後下一次擦背景畫的是未定義的
# 東西,而那正好又是「只在還原那一瞬間露臉」的那種錯。
_BRUSHES: dict[tuple[int, int, int], int] = {}


def _colorref(r: int, g: int, b: int) -> int:
    """Win32 的 COLORREF 是 `0x00BBGGRR`——**位元組序跟 `#rrggbb` 相反**。

    寫反了沒有錯誤訊息,只是底色變成另一個顏色,而它只在還原那一瞬間看得到。"""
    return (b << 16) | (g << 8) | r


def set_backdrop(root, colour: str, hwnd_of) -> Outcome:
    """把 Tk 的兩個 window class 的背景 brush 設成 `colour`。"""
    try:
        root.update_idletasks()          # 先讓 HWND 真的存在
        key = tuple(v >> 8 for v in root.winfo_rgb(colour))
        brush = _BRUSHES.get(key)
        if brush is None:
            gdi32 = ctypes.windll.gdi32
            gdi32.CreateSolidBrush.restype = ctypes.c_void_p
            brush = gdi32.CreateSolidBrush(_colorref(*key))
            _BRUSHES[key] = brush
        user32 = ctypes.windll.user32
        # ⚠️ 32 位元的 user32 裡沒有 `SetClassLongPtrW`(那邊它是映射到 `SetClassLongW`
        # 的巨集),而 handle 在 64 位元是指標寬——兩邊都要接得住
        setter = getattr(user32, "SetClassLongPtrW", None) or user32.SetClassLongW
        setter.restype = ctypes.c_void_p
        setter.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        # ⚠️ **兩個 class 都要設**:`TkTopLevel` 是工作列認得的那個、`TkChild` 是
        # widget 真正畫上去的那個(見 `window_handle`),而黑的是哪一層,要看 Windows
        # 那一次走的是哪條擦除路徑
        for hwnd in (hwnd_of(root), root.winfo_id()):
            if hwnd:
                setter(ctypes.c_void_p(hwnd), _GCLP_HBRBACKGROUND,
                       ctypes.c_void_p(brush))
        return Outcome("ok")
    except Exception as e:
        return Outcome("failed", f"視窗底色設不上:{type(e).__name__}: {e}")


# --- 工作區 ---------------------------------------------------------------------

_MONITOR_DEFAULTTONEAREST = 2


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", _RECT),
                ("rcWork", _RECT), ("dwFlags", ctypes.c_ulong)]


def work_area(root, hwnd_of) -> tuple[int, int, int, int] | None:
    """`MonitorFromWindow` ＋ `GetMonitorInfoW` 的 `rcWork`,取不到回 None。"""
    try:
        user32 = ctypes.windll.user32
        # ⚠️ `restype` / `argtypes` 一定要設,理由與 `window_handle` 同一條:
        # HMONITOR 跟 HWND 一樣是**指標寬**,而 ctypes 的預設回傳型別是 32 位元的
        # `c_int`。被截斷的 handle 不會當場爆炸——`GetMonitorInfoW` 只是回 0,
        # 症狀是「這台機器讀不到工作區」,而且要配到高位元有值才偶爾發生一次。
        user32.MonitorFromWindow.restype = ctypes.c_void_p
        user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        mon = user32.MonitorFromWindow(
            ctypes.c_void_p(hwnd_of(root) or root.winfo_id()),
            _MONITOR_DEFAULTTONEAREST)
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if not user32.GetMonitorInfoW(ctypes.c_void_p(mon), ctypes.byref(info)):
            return None
        r = info.rcWork
        return r.left, r.top, r.right, r.bottom
    except Exception:
        return None


# --- 工作列(為什麼只閃、不搶前景見 `winui` 那一段)--------------------------

# ITaskbarList3(shell32 內建,Win7 起)。⚠️ vtable 的位置是介面定義的一部分、
# 不會變動:IUnknown 佔 0-2、ITaskbarList 佔 3-7、ITaskbarList2 佔 8,
# ITaskbarList3 自己的方法從 9 開始算。
_CLSID_TASKBARLIST = "{56FDF344-FD6D-11D0-958A-006097C9A090}"
_IID_ITASKBARLIST3 = "{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}"
_VT_HRINIT = 3
_VT_SETPROGRESSVALUE = 9
_VT_SETPROGRESSSTATE = 10
# TBPFLAG(shellapi.h)。⚠️ 這是**位元旗標**不是序號。對外的那一份在 `winui.TBPF_*`
# (下游讀的是那邊),兩份同值由測試釘著。
_TBPF_NOPROGRESS = 0x0
_TBPF_INDETERMINATE = 0x1
_TBPF_NORMAL = 0x2

# FlashWindowEx 的旗標。只閃**工作列按鈕**(TRAY),不閃標題列(CAPTION):視窗
# 如果只是被蓋住一半,標題列閃起來很吵而且沒有多給任何資訊。
# TIMERNOFG＝一直閃到使用者把視窗切到前景為止,不必自己算次數。
_FLASHW_TRAY = 0x2
_FLASHW_TIMERNOFG = 0xC

_taskbar_ptr = None
_taskbar_dead = False        # 建過一次失敗就不再重試(每則進度都重試會很吵)


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]


class _FLASHWINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("hwnd", ctypes.c_void_p),
                ("dwFlags", ctypes.c_uint), ("uCount", ctypes.c_uint),
                ("dwTimeout", ctypes.c_uint)]


def _com_call(ptr, index: int, *argtypes):
    """取 COM 物件 vtable 上第 `index` 個方法,回傳可直接呼叫的函式。

    呼叫慣例是 `fn(ptr, 其餘參數…)` —— COM 的 this 指標要自己帶。"""
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p)).contents.value
    slot = ctypes.cast(vtbl, ctypes.POINTER(ctypes.c_void_p))[index]
    return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(slot)


def _taskbar():
    """取得 ITaskbarList3(第一次呼叫時建立並 `HrInit`)。失敗一律回 None。

    ⚠️ **只能在主執行緒呼叫**:COM 物件綁在建立它的 apartment 上,而
    `CoInitialize` 給的是 STA。下游所有呼叫點都在 Tk 的主執行緒上(視窗是唯一
    的呼叫端,而它的事件迴圈就是主執行緒),讀子行程輸出的那條執行緒不可以碰它。
    """
    global _taskbar_ptr, _taskbar_dead
    if _taskbar_ptr is not None or _taskbar_dead:
        return _taskbar_ptr
    try:
        ole32 = ctypes.windll.ole32
        # 已經初始化過會回 S_FALSE(1),那不是錯誤,不必也不該去 CoUninitialize
        ole32.CoInitialize(None)
        clsid, iid = _GUID(), _GUID()
        if ole32.CLSIDFromString(_CLSID_TASKBARLIST, ctypes.byref(clsid)) < 0:
            raise OSError("CLSIDFromString")
        if ole32.IIDFromString(_IID_ITASKBARLIST3, ctypes.byref(iid)) < 0:
            raise OSError("IIDFromString")
        ptr = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(
            ctypes.byref(clsid), None, 1,          # CLSCTX_INPROC_SERVER
            ctypes.byref(iid), ctypes.byref(ptr))
        if hr < 0 or not ptr:
            raise OSError(f"CoCreateInstance hr=0x{hr & 0xFFFFFFFF:08X}")
        if _com_call(ptr, _VT_HRINIT)(ptr) < 0:
            raise OSError("HrInit")
        _taskbar_ptr = ptr
    except Exception:
        _taskbar_dead = True             # 純外觀:這台機器沒有就算了
    return _taskbar_ptr


def taskbar_progress(hwnd: int, done: int, total: int) -> Outcome:
    """工作列按鈕畫成進度條;`total <= 0` 畫跑馬燈。"""
    try:
        tb = _taskbar()
        if tb is None or not hwnd:
            return Outcome("failed", "沒有工作列物件,或視窗還不在")
        state = _TBPF_NORMAL if total > 0 else _TBPF_INDETERMINATE
        _com_call(tb, _VT_SETPROGRESSSTATE, ctypes.c_void_p, ctypes.c_int)(
            tb, hwnd, state)
        if total > 0:
            _com_call(tb, _VT_SETPROGRESSVALUE, ctypes.c_void_p,
                      ctypes.c_ulonglong, ctypes.c_ulonglong)(
                tb, hwnd, done, total)
        return Outcome("ok")
    except Exception as e:
        return Outcome("failed", f"工作列進度設不上:{type(e).__name__}: {e}")


def taskbar_finish(hwnd: int, flag: int) -> Outcome:
    """收工時的工作列狀態。⚠️ **`ERROR` / `PAUSED` 要先有長度才看得到顏色**:那兩個
    狀態只換色、不動數值,前一刻若停在 0% 就等於畫了一條看不見的紅線。"""
    try:
        tb = _taskbar()
        if tb is None or not hwnd:
            return Outcome("failed", "沒有工作列物件,或視窗還不在")
        if flag != _TBPF_NOPROGRESS:
            _com_call(tb, _VT_SETPROGRESSVALUE, ctypes.c_void_p,
                      ctypes.c_ulonglong, ctypes.c_ulonglong)(tb, hwnd, 1, 1)
        _com_call(tb, _VT_SETPROGRESSSTATE, ctypes.c_void_p, ctypes.c_int)(
            tb, hwnd, flag)
        return Outcome("ok")
    except Exception as e:
        return Outcome("failed", f"工作列收尾設不上:{type(e).__name__}: {e}")


def _flash_hwnd(hwnd: int) -> None:
    """閃某個 HWND 的工作列按鈕,一直閃到它被切到前景為止。

    ⚠️ **例外自己收掉,不可以讓它冒到呼叫端**:在 `raise_existing_window()` 裡這一支
    是包在那個大 `try` 內的,漏出去的話「閃工作列失敗」會被寫成「**沒找到視窗**」
    ——視窗明明找到了、也 restore 過了,答案卻反過來,而下一步就是多開一個視窗。"""
    try:
        user32 = ctypes.windll.user32
        info = _FLASHWINFO(ctypes.sizeof(_FLASHWINFO), hwnd,
                           _FLASHW_TRAY | _FLASHW_TIMERNOFG, 0, 0)
        user32.FlashWindowEx(ctypes.byref(info))
    except Exception:
        pass


def flash_taskbar(hwnd: int) -> Outcome:
    """閃工作列按鈕;⚠️ **視窗已經在前景就什麼都不做**(前景視窗閃自己看不出來)。"""
    try:
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        if user32.GetForegroundWindow() == hwnd:
            return Outcome("ok")
        _flash_hwnd(hwnd)
        return Outcome("ok")
    except Exception as e:
        return Outcome("failed", f"閃工作列失敗:{type(e).__name__}: {e}")


# --- 單一實例 -------------------------------------------------------------------
# ⚠️ **`Local\` 而不是 `Global\`**:這是使用者桌面上的 app,「只開一個」的真正意思
# 是「同一個登入 session 只開一個」——切換使用者、遠端桌面各自開一個才是對的,而
# `Global\` 會把那些也一起擋掉。
_MUTEX_SCOPE = "Local\\"
_ERROR_ALREADY_EXISTS = 183
_SW_RESTORE = 9


def claim_single_instance(name: str):
    """具名互斥鎖。回 `(我是第一個嗎, 要一直留著的 handle 或 None)`。

    ⚠️ **拿不到 Windows API 就一律放行**:這是便利、不是安全邊界。"""
    try:
        # ⚠️ `use_last_error=True` ＋ `ctypes.get_last_error()`:直接呼叫
        # `kernel32.GetLastError()` 讀到的可能是**中間任何一次 ctypes 呼叫**留下的
        # 值,而這裡整個判斷就靠 ERROR_ALREADY_EXISTS 這一個碼。
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        handle = k32.CreateMutexW(None, False, _MUTEX_SCOPE + name)
        if not handle:
            return True, None
        if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
            # ⚠️ **搶輸了就把自己這個 handle 關掉**:同名的互斥鎖每開一次就多一個
            # handle,而它要等**最後一個**關掉才真的消失——留著的話,第一個實例收工
            # 之後那把鎖還在,下一次啟動會被自己的殘骸擋在門外。
            k32.CloseHandle(ctypes.c_void_p(handle))
            return False, None
        return True, handle
    except Exception:
        return True, None


def release_single_instance(lock) -> None:
    """把 `claim_single_instance` 拿到的 handle 放掉(只給測試的復位用)。"""
    try:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(lock))
    except Exception:
        pass


def raise_existing_window(class_name: str, title: str) -> bool:
    """找 class ＋ 標題都對得上的頂層視窗,還原並叫到前景(叫不動就閃工作列)。

    回**有沒有找到它**。為什麼叫到前景不違反「不搶前景」、為什麼要認 class ＋ 標題,
    見 `winui` 那一支。"""
    try:
        user32 = ctypes.windll.user32
        found: list[int] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def _visit(hwnd, _lparam):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(ctypes.c_void_p(hwnd), buf, 256)
            if buf.value != class_name:
                return True
            user32.GetWindowTextW(ctypes.c_void_p(hwnd), buf, 256)
            if buf.value == title:
                found.append(hwnd)
                return False          # 找到就不必再走完整個桌面
            return True

        user32.EnumWindows(_visit, None)
        if not found:
            return False
        # ⚠️ **兩個拼法都要留著,不可以「順手統一」**:`raw` 是給 `_flash_hwnd()`
        # 塞進 `_FLASHWINFO.hwnd`(那個欄位自己是 `c_void_p`)用的,而 `hwnd` 包成
        # `c_void_p` 是因為底下那幾支 user32 沒設 `argtypes`——`ctypes` 預設把 int
        # 當 32 位元傳,HWND 是指標寬,值大的時候會被**靜默截斷**成另一個視窗的號碼。
        raw = found[0]
        hwnd = ctypes.c_void_p(raw)
        # ⚠️ **「最小化」與「隱藏」兩種都要先 restore**(2026-08-28 更正:原本只判
        # `IsIconic`)。下游的視窗在 `__init__` 全程是 `withdraw()` 的(量好高度才
        # `deiconify()`,否則會先閃一個小畫面),而 `EnumWindows` **會列舉隱藏視窗**
        # ——於是那段期間標題對得上、`IsIconic` 是 0,對隱藏的 HWND 呼叫
        # `SetForegroundWindow` 實測回 1(成功)卻什麼都沒顯示,這一支回報 `True`
        # 而畫面全黑。顯示縮放不在出貨清單裡、皮膚要當場畫時那段可以長達數秒。
        if user32.IsIconic(hwnd) or not user32.IsWindowVisible(hwnd):
            user32.ShowWindow(hwnd, _SW_RESTORE)
        # ⚠️ **`SetForegroundWindow` 會失敗,而且在出貨的那條啟動鏈上是常態**
        # (2026-08-28 實測):Windows 只把前景權給「最後收到輸入事件」的那個行程,
        # 而我們是使用者雙擊之後隔了 `wscript` → `cmd` → `pythonw` 兩層
        # `CreateProcess` 才起來的,沒有人呼叫過 `AllowSetForegroundWindow`。本機
        # `SPI_GETFOREGROUNDLOCKTIMEOUT` 讀到 2147483647(＝前景鎖等於永久)。
        # ⚠️ **四種「用力一點」的做法全部量過而否決**(同日,對一個標題正確的 Tk
        # 視窗):`SetForegroundWindow` 回 0 前景不動;`SW_MINIMIZE`+`SW_RESTORE`
        # 之後再叫仍然回 0;`AttachThreadInput` **本身**就回 0(接不上前景執行緒),
        # 而且它會把兩條輸入佇列綁在一起——前景執行緒卡住時我們跟著卡;
        # `SwitchToThisWindow`(Alt+Tab 用的那支)前景也不動。
        # ⚠️ **`HWND_TOPMOST` → `HWND_NOTOPMOST` 那個 Z 序把戲不用**:它確實抬得動
        # (實測 rank 1 → 0),但行程若在兩次呼叫之間死掉,**別人的視窗就永久卡在
        # 最上層**,而且使用者沒有辦法把它弄回去。
        # 所以叫得動就叫,叫不動就退到閃工作列——那本來就是 Windows 自己在前景鎖
        # 擋下來時做的事,差別只在系統選的閃法比我們吵。
        if not user32.SetForegroundWindow(hwnd):
            _flash_hwnd(raw)
        return True
    except Exception:
        return False


# --- 防睡眠與系統佈景(2026-09-27 從 `power` 與 `winui` 搬來)----------------------


def has_execution_state() -> bool:
    """這個平台擋睡眠是不是走 `SetThreadExecutionState`(`power` 用來判斷要不要喊警告)。"""
    return True


def set_execution_state(flags: int) -> Outcome:
    """`SetThreadExecutionState(flags)`。⚠️ **旗標綁呼叫執行緒**(理由在 `power` 檔頭)。"""
    try:
        # 回傳 0 代表失敗(旗標無效等);非 0 為前一個狀態值
        if ctypes.windll.kernel32.SetThreadExecutionState(flags) != 0:
            return Outcome("ok")
        return Outcome("failed", "SetThreadExecutionState 回 0")
    except Exception as e:
        return Outcome("failed", f"擋睡眠失敗:{type(e).__name__}: {e}")


def system_theme() -> str | None:
    """「應用程式模式」是亮是暗(`"light"` / `"dark"`);讀不到回 None。

    讀 registry 而不是加一個 darkdetect 依賴——就這一個值,而且它正是 darkdetect
    在 Windows 上讀的那一個。⚠️ `winreg` 在函式裡 import:它只有 Windows 有。"""
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        ) as key:
            return "light" if winreg.QueryValueEx(key, "AppsUseLightTheme")[0] else "dark"
    except Exception:
        return None


def hidpi_image_factor(root) -> int:
    """1:Windows 上 Tk 是照**實體像素**畫的,而資產本來就按顯示縮放選檔
    (`make_skin.SCALES` 那八檔),再乘一次就是把每張圖畫成兩倍大。"""
    return 1


def make_hidpi_image(root, path, width: int, height: int) -> str | None:
    """沒有這個概念(見 `hidpi_image_factor`),一律回 None。"""
    return None


def set_hidpi_image(root, name: str, path, width: int, height: int) -> bool:
    """沒有高解析影像這回事(見 `hidpi_image_factor`),一律回 False。"""
    return False


def restore_on_reopen(root) -> Outcome:
    """Windows 沒有 reopen 事件:再開一次是新的行程,由 `raise_existing_window` 接手。"""
    return Outcome("unsupported", "Windows 沒有「再開一次 App」的事件,由單一實例那條路處理")
