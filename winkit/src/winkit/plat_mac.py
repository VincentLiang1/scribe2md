"""平台層的 macOS 實作。只由 `winkit.plat._impl()` 選用,不要直接 import。

⚠️ **上半段(游標名、emoji 字型、`appdata_base`)的數字是在 Mac 上實測過的**
(2026-09-24~25,`meeting-scribe` 的 `plat_mac` 先跑過),搬過來時原樣照抄——改任何
一個值之前,先在 Mac 上重量一次。

⚠️ **下半段(視窗整合那一批)2026-09-27 在 Mac 上補完了兩支**:`work_area`(NSScreen
的 `visibleFrame`)與 `raise_existing_window`(訊號 ＋ 自我叫到前景),兩支的數字與
行為都是當天在這台機器上量的。其餘仍是 `unsupported`,理由各自寫在那一支上。
"""

import ctypes
import os
from pathlib import Path

from winkit.plat import Outcome

NAME = "macos"

#: 停用中的按鈕用的 Tk 游標名。Tk/Aqua 的名字(`"no"` 在這裡是 TclError)
DISABLED_CURSOR = "notallowed"

#: 彩色 emoji 的字型。隨每一版 macOS 出貨
EMOJI_FONT = Path("/System/Library/Fonts/Apple Color Emoji.ttc")
#: ⚠️ **點陣字型,只有這幾個尺寸畫得出來**(2026-09-24 實測:21 與 137 直接
#: `OSError: invalid pixel size`)。這一串是 8~199 逐個試出來的,不是猜的。
EMOJI_STRIKES: tuple[int, ...] = (20, 26, 32, 40, 48, 52, 64, 96, 160)


def appdata_base() -> Path:
    """`~/Library/Application Support`。

    ⚠️ **不是 `~/Library/Caches`**:下游在底下放的有錄音、工作進度與幾 GB 的模型,
    而 Caches 正是 macOS 會在磁碟吃緊時收走的地方。⚠️ **也不是 `~/.cache`**(那是
    Linux 的慣例;本包 2026-09-27 之前的 `local_appdata` 就落在那裡)。"""
    return Path.home() / "Library" / "Application Support"


# =========================================================================== #
#  視窗整合(對應 `plat_win` 那一批;⚠️ 2026-09-27 在 Windows 上盲寫,見檔頭)
# =========================================================================== #


# --- ObjC 直呼:不裝 PyObjC ---------------------------------------------------
#
# ⚠️ **為了兩支函式多一個相依不划算**(同 `meeting-scribe` 的 `coreaudio.py` /
# `plat_mac.microphone_permission`,那一套 2026-09-25 起在用):`ctypes` 載 `libobjc`
# 直接發訊息就夠了。
# ⚠️ **`objc_msgSend` 在 arm64 上不可以當 variadic 用**:AAPCS64 對可變參數與具名
# 參數的傳遞方式不同,那是已知的當機來源——每一次呼叫都把簽章宣告成**那一次的
# 真實形狀**,不共用一份。


class _NSPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class _NSSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class _NSRect(ctypes.Structure):
    _fields_ = [("origin", _NSPoint), ("size", _NSSize)]


def _appkit():
    """載 `libobjc` ＋ AppKit,回 `(objc, send)`。

    ⚠️ **AppKit 一定要載**:`objc_getClass(b"NSScreen")` 在沒載那個 framework 的
    行程裡回的是 NULL,而 NULL 發訊息在 ObjC 是**合法的、回 0**——症狀不是例外,
    是每一個值都變成 0。"""
    import ctypes.util

    objc = ctypes.CDLL(ctypes.util.find_library("objc"))
    ctypes.CDLL("/System/Library/Frameworks/AppKit.framework/AppKit")
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]

    def send(restype, argtypes, obj, sel, *args):
        proto = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)
        fn = proto(ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value)
        return fn(ctypes.c_void_p(obj), objc.sel_registerName(sel), *args)

    return objc, send


def enable_dpi_awareness() -> Outcome:
    return Outcome("unsupported", "macOS 的 Retina 縮放由 Tk/Aqua 自己處理,沒有要行程宣告的開關")


def set_app_user_model_id(app_id: str) -> Outcome:
    return Outcome("unsupported", "Dock 認的是 .app bundle 的身分,不是行程自己宣告的 ID")


def window_handle(root) -> int:
    """macOS 沒有 HWND;回 0 = 「拿不到」,所有吃它的地方都已經當成失敗處理。"""
    return 0


def use_dark_titlebar(root, hwnd_of) -> Outcome:
    return Outcome("unsupported", "Aqua 的標題列跟著系統外觀走,不必另外設")


def ime_needs_help() -> bool:
    """Aqua 自己擺組字視窗、自己挑字型(2026-09-26 使用者在 Mac 上打注音確認過),
    `winui` 那兩支 `follow_ime_*` 在這裡什麼都不必做。"""
    return False


def set_ime_composition_font(hwnd: int, logfont) -> Outcome:
    return Outcome("unsupported", "組字字型由 Aqua 自己決定")


def set_backdrop(root, colour: str, hwnd_of) -> Outcome:
    return Outcome("unsupported",
                   "還原時露黑的成因是 Win32 window class 沒有背景 brush,macOS 沒有這個機制")


def _screen_rects():
    """每個螢幕的 `(frame, visibleFrame)`,各是 `(x, y, w, h)`。拿不到回 None。

    ⚠️ **單位是「點」不是實體像素,原點在螢幕的左下角**——兩件都與 Tk 相反,換算
    在 `work_area` 裡做。點這一邊剛好對得上:Tk 在 Aqua 上問到的也是點(2026-09-27
    實測,`winfo_screenwidth()` 1280 = `frame.size.width` 1280,而
    `backingScaleFactor` 是 2.0),所以**不必自己乘縮放**。

    ⚠️ **只在 arm64 上做**:`-[NSScreen visibleFrame]` 回的是 32 bytes 的結構,
    arm64 用暫存器回(`CFUNCTYPE` 的 struct restype 就是對的),而 x86_64 走的是
    `objc_msgSend_stret`——**手上沒有 Intel Mac 可以驗**,盲寫一條 ABI 不同的路等於
    猜。那邊回 None,呼叫端維持 2026-09-27 之前的行為(退回整個螢幕矩形)。"""
    import platform

    if platform.machine() != "arm64":
        return None
    try:
        objc, send = _appkit()
        screens = send(ctypes.c_void_p, [], objc.objc_getClass(b"NSScreen"), b"screens")
        if not screens:
            return None
        out = []
        for i in range(send(ctypes.c_long, [], screens, b"count")):
            scr = send(ctypes.c_void_p, [ctypes.c_long], screens, b"objectAtIndex:", i)
            f = send(_NSRect, [], scr, b"frame")
            v = send(_NSRect, [], scr, b"visibleFrame")
            out.append(((f.origin.x, f.origin.y, f.size.width, f.size.height),
                        (v.origin.x, v.origin.y, v.size.width, v.size.height)))
        return out or None
    except Exception:
        return None


def work_area(root, hwnd_of):
    """`NSScreen` 的 `visibleFrame`(已扣掉選單列與 Dock),翻成 Tk 的座標系。

    2026-09-27 在這台機器上實測:`frame` 1280×832、`visibleFrame` 在 (0, 62) 起算
    741 高,也就是**上面 29 點是選單列、下面 62 點是 Dock**。⚠️ **那個 29 正是
    「y=0 擺不進去」的成因**:逐格試過,要求 0~28 一律被 Aqua 推到 29,而呼叫端
    退回整個螢幕矩形(0, 0, 1280, 832)時算出來的就是 y=0。補上這一支之後,
    下游要的 1119×819 會先被 `_fit` 縮成 1119×709、落點算成 (80, 29) ——**高度不再
    是 Aqua 鉗出來的**(實測:補這一支之前拿到 713、之後是自己算的 709)。

    ⚠️ **Stage Manager 的縮圖帶這一支看不到,而且沒有別的路問得到**:那台開著
    Stage Manager,左邊 193 點是它的縮圖帶,要求 x=0~192 的視窗一律被推到 193、
    寬度再被切到剩下的 1087(2026-09-27 逐格實測;`wm_maxsize()` 回的寬度也是
    整個 1280,Tk 同樣不知道)。所以**水平置中在開著 Stage Manager 的機器上仍然
    做不到**——那是已知的殘留,不是這一支算錯。

    ⚠️ 【待驗】**多螢幕**:這裡照 `plat_win` 的 `MONITOR_DEFAULTTONEAREST` 學,
    先找視窗左上角落在哪一個螢幕、找不到才退回主螢幕,但手上只有一台單螢幕的機器,
    真的接第二個螢幕時要重驗一次。"""
    rects = _screen_rects()
    if not rects:
        return None
    # 主螢幕的上緣 = Tk 座標的 y=0。⚠️ 主螢幕的 frame 原點照定義是 (0, 0),但這裡
    # 仍然照 `y + h` 算——第二個螢幕擺在上面時,那些座標是負的,而假設 0 會整批位移。
    top_of_world = rects[0][0][1] + rects[0][0][3]

    def to_tk(r):
        x, y, w, h = r
        return int(x), int(top_of_world - (y + h)), int(x + w), int(top_of_world - y)

    areas = [(to_tk(frame), to_tk(vis)) for frame, vis in rects]
    try:
        wx, wy = root.winfo_x(), root.winfo_y()
    except Exception:
        wx = wy = 0
    if wx or wy:
        for frame, vis in areas:
            if frame[0] <= wx < frame[2] and frame[1] <= wy < frame[3]:
                return vis
    return areas[0][1]


def taskbar_progress(hwnd: int, done: int, total: int) -> Outcome:
    return Outcome("unsupported", "Dock 圖示上的進度要 NSDockTile(PyObjC),先不做")


def taskbar_finish(hwnd: int, flag: int) -> Outcome:
    return Outcome("unsupported", "Dock 圖示上的進度要 NSDockTile(PyObjC),先不做")


def flash_taskbar(hwnd: int) -> Outcome:
    return Outcome("unsupported", "Dock 的彈跳要 NSApp.requestUserAttention(PyObjC),先不做")


#: 這個行程這一輪的鎖檔位置。⚠️ **`raise_existing_window()` 只拿得到 class 名與
#: 標題,而要叫的那個行程的 pid 寫在鎖檔裡**——`plat` 這一層問不到下游的 `APP_ID`
#: (它不准 import 下游),所以由 `claim_single_instance()` 在同一個行程裡記下來。
#: 搶輸的那一份也走過 `claim`,所以它也有這個值(政策層固定是先 claim 再 raise)。
_lock_path: Path | None = None
#: 裝上 SIGUSR1 之前原本的處理器,`release_single_instance()` 要還回去(只給測試;
#: 正式執行靠行程結束)
_prev_sigusr1 = None

#: `NSApplicationActivationOptions`:全部視窗 ＋ 不管誰在前景
_ACTIVATE_ALL_WINDOWS = 1 << 0
_ACTIVATE_IGNORING_OTHERS = 1 << 1


def claim_single_instance(name: str):
    """勸告鎖(`flock`)。回 `(我是第一個嗎, 要一直留著的檔案物件或 None)`。

    ⚠️ **與 `meeting-scribe` 的暫存目錄鎖同一個推理**:Windows 靠具名互斥鎖,POSIX
    的對應物是勸告鎖——行程死掉時核心會自己放掉,不會留下擋住下一次啟動的殘骸。
    ⚠️ **鎖檔放在 `tempfile.gettempdir()`**:macOS 上那是**每個使用者各一份**的
    `$TMPDIR`,正好對應 Windows 那邊 `Local\\` 的「同一個登入 session」。
    ⚠️ **拿不到鎖以外的任何失敗都放行**:這是便利、不是安全邊界。

    ⚠️ **搶到鎖之後還做兩件事**(2026-09-27 加,為的是 `raise_existing_window()`):
    把自己的 pid 寫進鎖檔、裝上 SIGUSR1 的處理器。兩件都是 best-effort——失敗的
    後果只是「下一次啟動叫不回這個視窗」,不值得讓程式開不起來。
    ⚠️ **pid 要在 `flock` 成功之後才寫**:寫在前面的話,搶輸的那一份會蓋掉持有者
    的 pid,而那正是要讀的東西。"""
    import fcntl
    import tempfile

    global _lock_path
    _lock_path = Path(tempfile.gettempdir()) / f"{name}.instance.lock"
    try:
        # ⚠️ **`a+b` 不是 `ab`**:要能回頭讀(同一個行程稍後可能是搶輸的那一份)。
        fh = open(_lock_path, "a+b")
    except OSError:
        return True, None
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return False, None
    except OSError:
        fh.close()
        return True, None
    _write_holder_pid(fh)
    _listen_for_raise()
    return True, fh


def _write_holder_pid(fh) -> None:
    """把自己的 pid 寫進鎖檔(蓋掉上一任的)。

    ⚠️ **`truncate(0)` 之後那個 `write` 才會落在位移 0**:檔案是用 `a+` 開的,
    寫入永遠追加到檔尾——先砍成 0 長度,檔尾就是開頭。"""
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()).encode("ascii"))
        fh.flush()
    except OSError:
        pass


def _listen_for_raise() -> None:
    """裝上 SIGUSR1 的處理器:第二個實例用它把我們叫到前景(見 `raise_existing_window`)。

    ⚠️ **非裝不可,不裝比沒做更糟**:SIGUSR1 的預設動作是**結束行程**——第二次啟動
    會變成「把第一個實例殺掉」,而使用者看到的是視窗自己消失。
    ⚠️ **裝不上就算了**:不在主執行緒(`signal.signal` 只准主執行緒裝)時丟 ValueError,
    那一份就是叫不回來而已。"""
    import signal

    global _prev_sigusr1
    sig = getattr(signal, "SIGUSR1", None)
    if sig is None:
        return
    try:
        previous = signal.signal(sig, _on_raise_request)
    except (ValueError, OSError):
        return
    # ⚠️ **裝第二次時不要把「原本的」蓋成自己**:同一個行程可以呼叫 `claim` 不只
    # 一次(測試就是),那時 `previous` 已經是我們自己,存下去等於永遠還不回去。
    if previous is not _on_raise_request:
        _prev_sigusr1 = previous


def _on_raise_request(signum, frame) -> None:
    """⚠️ **只碰 ObjC、一行 Tk 都不碰**:這是訊號處理器,而 Tk 不是可重入的——
    在 `mainloop` 中間插一句 `lift()` 是拿整個介面去賭。自我叫到前景本來就會把
    這支 app 的視窗一起帶上來,不必經過 Tk。"""
    _activate_self()


def _activate_self() -> bool:
    """`[[NSRunningApplication currentApplication] activateWithOptions:]`。

    ⚠️ **只有「自己叫自己」這個方向做得到**(2026-09-27 實測,見
    `raise_existing_window`);⚠️ 而 `activateWithOptions:` 雖然從 macOS 14 起標成
    deprecated,當天在這台機器上量到的是**真的生效**(`isActive` False → True)。

    ⚠️ **前提是這支 app 至少拿過一次前景**(同一天量到的第二件事):探針視窗在
    「從頭到尾沒被啟用過」的那幾輪,這一行一律回 False、`isActive` 紋風不動;
    先讓它拿過一次前景、被別的 app 蓋掉之後再叫,才是 False → True。正常啟動
    (使用者自己雙擊)必定滿足這個前提,所以不另外補救——**但這也是「開機自動
    啟動」那種情境不能假設它會動的理由**。"""
    try:
        objc, send = _appkit()
        app = send(ctypes.c_void_p, [],
                   objc.objc_getClass(b"NSRunningApplication"), b"currentApplication")
        if not app:
            return False
        return bool(send(ctypes.c_bool, [ctypes.c_ulong], app, b"activateWithOptions:",
                         _ACTIVATE_ALL_WINDOWS | _ACTIVATE_IGNORING_OTHERS))
    except Exception:
        return False


def release_single_instance(lock) -> None:
    import signal

    global _prev_sigusr1
    try:
        lock.close()
    except Exception:
        pass
    sig = getattr(signal, "SIGUSR1", None)
    if _prev_sigusr1 is not None and sig is not None:
        try:
            signal.signal(sig, _prev_sigusr1)
        except (ValueError, OSError):
            pass
        _prev_sigusr1 = None


def _holder_pid() -> int | None:
    """鎖檔裡那個 pid(持有者自己寫的)。讀不到、或不是數字就回 None。"""
    if _lock_path is None:
        return None
    try:
        raw = Path(_lock_path).read_bytes()[:32].strip()
    except OSError:
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    return pid if pid > 0 else None


def raise_existing_window(class_name: str, title: str) -> bool:
    """送 SIGUSR1 給鎖檔裡那個 pid,由**它自己**把視窗叫到前景。

    ⚠️ **`class_name` 與 `title` 在這個平台上用不到,而且不是忘了接**:macOS 沒有
    「照 class ＋ 標題找別人的視窗」這回事,那要走輔助取用(Accessibility)的
    AXUIElement,而那是一顆使用者要自己去系統設定裡打開的權限——這台機器上的
    osascript 就是被它擋的(`-25211`)。⚠️ **叫得回來的唯一一條路是繞過視窗系統**:
    第一個實例在 `claim_single_instance()` 裡裝好處理器,我們只負責按鈴。

    ⚠️ **為什麼不是「第二個實例直接叫對方到前景」**(2026-09-27 實測否決):
    `[NSRunningApplication activateWithOptions:]` 對**別的行程**回 True、卻什麼都
    沒發生——A 被 E 蓋住之後,B 在 t≈6 秒叫它,A 的 `isActive` 一直到 t≈17 秒
    E 自己結束才變 True。新版 macOS 的 cooperative activation 只讓**前景那一支**
    轉讓啟用權,而第二個實例連視窗都還沒有。同一輪量到「自己叫自己」是立刻生效的
    (0.0 秒內 False → True),所以改成請對方自己叫。

    ⚠️ **回 True 的意思是「鈴按到了」**,不是「對方真的浮上來了」:訊號送到一個
    確實持著鎖的行程就當成找到了。對方若把視窗縮到 Dock 或 `withdraw()` 著,自我
    啟用帶不回來——那是已知的殘留,而政策層在這裡的代價只是「多開一個視窗」。
    ⚠️ **pid 是上一行 `claim_single_instance()` 讀到的那個鎖檔寫的**,所以呼叫順序
    (先 claim、搶輸才 raise)是這一支的前提。"""
    import signal

    pid = _holder_pid()
    sig = getattr(signal, "SIGUSR1", None)
    if pid is None or sig is None or pid == os.getpid():
        return False
    try:
        os.kill(pid, sig)
    except OSError:
        return False
    return True


# --- 防睡眠與系統佈景 ---------------------------------------------------------------


def has_execution_state() -> bool:
    """沒有 `SetThreadExecutionState` 這套機制(macOS 的對應物是 `caffeinate`)。"""
    return False


def set_execution_state(flags: int) -> Outcome:
    return Outcome("unsupported",
                   "macOS 擋睡眠要走 caffeinate(meeting-scribe 有一套),本包還沒接")


#: `CFStringGetCString` / `CFStringCreateWithCString` 的編碼常數
_kCFStringEncodingUTF8 = 0x08000100


def _interface_style() -> str | None:
    """全域偏好設定裡的 `AppleInterfaceStyle`。深色時是 `"Dark"`;⚠️ **亮色時整個鍵
    不存在**(回 None 的意思是「亮色」,不是「問不到」——問不到會拋例外)。

    ⚠️ **走 CoreFoundation 不 fork `defaults`**:這支跑在開窗的關鍵路徑上(佈景要在
    建第一個 widget 之前定案),為了一個布林去開一支子行程是本末倒置(同
    `meeting-scribe` 的 `seconds_since_process_start`)。
    ⚠️ **只在啟動時讀一次就夠**:CFPreferences 會快取,行程開著的時候使用者去切
    系統外觀,這一支不會跟著變——那與 Windows 那份的行為一致(兩邊都是重開才換)。"""
    import ctypes
    import ctypes.util

    cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
    cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                             ctypes.c_uint32]
    cf.CFPreferencesCopyAppValue.restype = ctypes.c_void_p
    cf.CFPreferencesCopyAppValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                      ctypes.c_long, ctypes.c_uint32]
    cf.CFRelease.argtypes = [ctypes.c_void_p]

    key = cf.CFStringCreateWithCString(None, b"AppleInterfaceStyle",
                                       _kCFStringEncodingUTF8)
    try:
        value = cf.CFPreferencesCopyAppValue(
            ctypes.c_void_p(key),
            ctypes.c_void_p.in_dll(cf, "kCFPreferencesAnyApplication"))
    finally:
        cf.CFRelease(ctypes.c_void_p(key))
    if not value:
        return None
    try:
        buf = ctypes.create_string_buffer(64)
        if not cf.CFStringGetCString(ctypes.c_void_p(value), buf, 64,
                                     _kCFStringEncodingUTF8):
            return ""
        return buf.value.decode("utf-8", "replace")
    finally:
        cf.CFRelease(ctypes.c_void_p(value))


def system_theme() -> str | None:
    """跟隨系統「外觀」的亮暗(`"light"` / `"dark"`);問不到回 None。

    ⚠️ **鍵不存在 = 亮色,不是「問不到」**:macOS 只在深色時才寫 `AppleInterfaceStyle`
    (2026-09-27 在這台上實測,亮色時 `defaults read -g AppleInterfaceStyle` 直接說
    「does not exist」)。把它當成「問不到」而回 None 的話結果**剛好一樣**(呼叫端
    退回亮色),但那是誤打誤撞——「自動」模式下它會隨日落切換,語意要對。
    ⚠️ **`Dark` 以外的值一律當亮色**:比對用開頭而不是全等,Apple 這個鍵歷史上出現
    過 `Dark` 與 `DarkAqua` 兩種寫法。"""
    try:
        style = _interface_style()
    except Exception:
        return None
    return "dark" if (style or "").lower().startswith("dark") else "light"


# --- 高解析度的皮膚 -----------------------------------------------------------


def hidpi_image_factor(root) -> int:
    """`[[NSScreen mainScreen] backingScaleFactor]`,取整數;問不到回 1。

    ⚠️ **Tk 在 Aqua 上「1 圖素 = 1 點」**(2026-09-27 實測:`skin-light@1x.png` 是
    138 像素寬,`image width` 回的也是 138),所以 2 倍螢幕會把整張皮膚放大兩倍畫
    ——所有邊緣、圓角與虛線都比原生控制項軟一階。⚠️ **最顯眼的是弧**:直邊是實心
    1 像素、弧上同一條線被抗鋸齒攤成兩三格,放大之後直邊銳利而弧糊成一片,使用者
    的原話是「左右那個弧形粗細不同」。⚠️ **回 1 的意思是「照實畫就好」**,不是失敗。"""
    try:
        objc, send = _appkit()
        screen = send(ctypes.c_void_p, [], objc.objc_getClass(b"NSScreen"), b"mainScreen")
        if not screen:
            return 1
        f = send(ctypes.c_double, [], screen, b"backingScaleFactor")
    except Exception:
        return 1
    # ⚠️ 只收 2 與 3:那是 Apple 出過的兩檔。非整數(外接螢幕的縮放模式會出現
    # 1.5 之類)一律無條件捨去——餵 1.5 倍的 bitmap 只會讓 NSImage 再縮一次。
    return int(f) if 2 <= f <= 3 else 1


def make_hidpi_image(root, path, width: int, height: int) -> str | None:
    """`image create nsimage`:bitmap 是檔案裡那張(高解析),版面尺寸釘死 `width`×`height`。

    ⚠️ **`-as file` 不可省**:`-source` 預設當成**系統圖示的名字**,給檔案路徑會丟
    「Unknown named NSImage」。⚠️ **`photo` 換不了這件事**:它沒有密度概念,餵 2 倍的
    圖給它,版面就真的變成兩倍大(2026-09-27 實測)。"""
    try:
        return str(root.tk.call("image", "create", "nsimage",
                                "-source", str(path), "-as", "file",
                                "-width", int(width), "-height", int(height)))
    except Exception:
        return None


def set_hidpi_image(root, name: str, path, width: int, height: int) -> bool:
    """`<影像名> configure -source <檔> -width/-height`。

    ⚠️ **影像名不變是重點**:元件與樣式在啟動時就配好了,執行期只換這張圖的內容
    ——那不會發 `<<ThemeChanged>>`,整棵樹的顏色才不會被重刷。"""
    try:
        root.tk.call(name, "configure", "-source", str(path), "-as", "file",
                     "-width", int(width), "-height", int(height))
    except Exception:
        return False
    return True


# --- 再開一次 App(2026-10-03)--------------------------------------------------

#: Tk/Aqua 收到 reopen(`kAEReopenApplication`)時會呼叫的 Tcl 指令名(有定義的話)
REOPEN_COMMAND = "::tk::mac::ReopenApplication"


def restore_on_reopen(root) -> Outcome:
    """定義 `::tk::mac::ReopenApplication`:縮在 Dock 裡就 `deiconify()`。

    ⚠️ **沒有它的話視窗不會回來**(2026-10-03 實測,幕前調度關著、黃燈真的縮進
    Dock 之後 `open -a`):Tk 只把 App 叫到前景,`AXMinimized` 仍是 `true`。
    ⚠️ **只動 `iconic`、不動 `withdrawn`**:下游在建介面的那一兩秒是刻意
    `withdraw()` 著的(量好尺寸才放出來,否則先閃一個小畫面),那時候 reopen 進來
    就放出來,等於把那個機制拆掉。
    ⚠️ **這是 Tk 主執行緒上的普通回呼**,不是訊號處理器——所以碰 Tk 是安全的,
    與 `_on_raise_request` 那條「一行 Tk 都不碰」不衝突。"""
    def _reopen() -> None:
        try:
            if root.wm_state() == "iconic":
                root.deiconify()
        except Exception:
            pass        # 視窗正在關:什麼都不做就是對的

    try:
        root.createcommand(REOPEN_COMMAND, _reopen)
    except Exception as exc:
        return Outcome("failed", f"註冊「再開一次 App」的回呼失敗:{exc}")
    return Outcome("ok")
