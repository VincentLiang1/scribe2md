"""平台抽象層的 Windows 實作。介面與規矩見 `plat.py`。"""

# ⚠️ **模組層這支只給 `_FILETIME` 的類別本體用,函式內那幾支 `import ctypes`
# 不要清掉**:它們現在是測試的接縫——`test_windows_asks_for_the_foreground…`
# 那幾條靠把假貨塞進 `sys.modules["ctypes"]` 換掉 `windll`,而那**只對「呼叫
# 當下才解析」的 import 有效**(2026-09-25 把它們當成多餘的清掉,當場紅兩條)。
# (它們原本只是因為這一支模組層沒有 ctypes 才寫在函式內,是後來才變成接縫的。)
import ctypes
import logging

from meeting_scribe.plat import Outcome

logger = logging.getLogger(__name__)

NAME = "windows"

#: ⚠️ **Windows 不從核心數判斷預設模型**(見 `plat.accurate_min_cores`):
#: 那邊有 GPU 那條路,而「純 CPU 就用 fast」是在這個平台上量出來的
#: (精準約慢 4 倍)。改成看核心數會讓舊的多核文書機慢 4 倍,而使用者
#: 看不出原因——**交付給非技術同仁的正是這個平台**。
ACCURATE_MIN_CORES = None

#: ⚠️ **Windows 不從核心數判斷「錄音中要不要順便做講者分析」**:那條
#: 「兩者同搶 CPU 會讓即時轉錄落後」是在這個平台量出來的,而交付給非技術
#: 同仁的正是它。維持原本的「看轉錄走不走 GPU」。
LIVE_DIARIZE_MIN_CORES = None

#: 錄音中不限制執行緒數:這個平台走 GPU,講者分析拿滿執行緒是對的。
LIVE_CPU_THREADS = None

#: OCR 引擎回收門檻的兩個錨點(見 `plat.ocr_memory_anchors_mb`)。
#: ⚠️ **`None` = 用 `ocr_worker` 自己那組 2000 / 4000**,也就是**行為完全不動**:
#: 那條曲線本來就是在這個平台、拿這個平台的工作集(`WorkingSetSize`)校準的
#: (120 張真實影像)。搬進平台層是為了讓 macOS 有地方放它自己的數字,
#: **不是為了在這裡改什麼**。
OCR_MEMORY_ANCHORS_MB = None


class _FILETIME(ctypes.Structure):
    """`GetSystemTimes` 的輸出格式。⚠️ **在模組層定義一次**:每次呼叫重新建
    Structure 子類要走 metaclass 建 field descriptor(數十 µs),而這是診斷
    路徑上會反覆呼叫的東西(2026-09-25 從 `power.py` 原樣搬過來)。"""

    _fields_ = [("lo", ctypes.c_uint32), ("hi", ctypes.c_uint32)]

# 建立子行程時不要閃一個主控台黑視窗。⚠️ 這個值先前在 transproc / diarproc /
# ocr 各抄了一份(三處字面值都是 0x08000000),搬進來之後只剩這一份
CREATE_NO_WINDOW = 0x08000000
# 子行程的優先權類別。⚠️ 數值與 `power.BELOW_NORMAL_PRIORITY_CLASS` 相同不是
# 巧合:那一支管的是**本行程**(SetPriorityClass),這一個是建立子行程時的
# creationflag,兩條路不同、常數同源
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000


def spawn_kwargs(*, low_priority: bool) -> dict:
    """優先權在**建立的當下**就決定,而且會被孫行程繼承(uv 環境下
    `sys.executable` 是跳板,真正載模型的是它的子行程——ocr.py 實測兩層
    的 Priority 都從 8 降到 6)。"""
    flags = CREATE_NO_WINDOW
    if low_priority:
        flags |= BELOW_NORMAL_PRIORITY_CLASS
    return {"creationflags": flags}


def worker_argv(*, low_priority: bool) -> list[str]:
    """Windows 不從命令列走:父行程在 `spawn_kwargs` 那一步就做完了。"""
    return []


def apply_own_low_priority() -> Outcome:
    return Outcome("unsupported",
                   "Windows 的子行程優先權在建立時就由 creationflags 決定了")


# --- 外殼整合 -------------------------------------------------------------

def allow_foreground() -> Outcome:
    """ASFW_ANY:放行給任何行程。⚠️ 拿不到不是壞掉(這台機器的前景規則),
    所以是 `failed` 而不是例外——開資料夾照樣要試。"""
    import ctypes

    try:
        if ctypes.windll.user32.AllowSetForegroundWindow(-1):
            return Outcome("ok")
    except Exception as e:
        return Outcome("failed", f"放行前景權失敗:{type(e).__name__}: {e}")
    return Outcome("failed", "本程序沒有前景權,檔案總管可能只會在工作列閃爍")


def open_folder(path) -> Outcome:
    import os

    allow_foreground()
    try:
        os.startfile(str(path))  # noqa: S606 - 路徑來自本程式自己的輸出
    except OSError as e:
        return Outcome("failed", f"開不了資料夾:{e}")
    return Outcome("ok")


def reveal(path) -> Outcome:
    r"""走 `explorer.exe` 而不是 `os.startfile`(做法取自 MP4-2-SRT,那邊踩過)。
    ⚠️ `/select,` 與路徑**分成兩個參數**——explorer 的老怪癖。"""
    import subprocess
    from pathlib import Path

    p = Path(path)
    allow_foreground()
    args = (["explorer.exe", "/select,", str(p)] if p.is_file()
            else ["explorer.exe", str(p)])
    try:
        subprocess.Popen(args)  # noqa: S603 - 固定命令,無 shell
    except OSError as e:
        return Outcome("failed", f"開不了檔案總管:{e}")
    return Outcome("ok")


def play_sound(path) -> tuple[Outcome, object]:
    r"""`winsound.PlaySound`:Windows 內建、非同步、可隨時 purge 掉。

    ⚠️ **`SND_ASYNC` 少了就是整個視窗凍到那一句放完**;`SND_NODEFAULT` 是
    「檔案有問題時不要改放系統的預設嗶聲」(放錯的東西比沒放出來更難懂)。
    ⚠️ **權杖是 `None`**:這條路停的方式是「purge 掉目前所有的」,沒有
    個別的控制柄——`stop_sound` 因此不看權杖。"""
    try:
        import winsound

        winsound.PlaySound(str(path), winsound.SND_FILENAME
                           | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except Exception as e:  # noqa: BLE001 - 試聽是輔助功能,絕不往上炸
        return Outcome("failed", f"這一段放不出來({type(e).__name__})"), None
    return Outcome("ok"), None


def stop_sound(token: object) -> None:
    """purge 掉目前在放的(與權杖無關,見 `play_sound`)。"""
    try:
        import winsound

        winsound.PlaySound(None, winsound.SND_PURGE)
    except Exception:  # noqa: BLE001 - 停不下來也只是多放幾秒,不值得吵
        logger.debug("停止試聽失敗", exc_info=True)


#: 等寬字型。Consolas 從 Vista 起隨 Windows 出貨
MONO_FAMILY = "Consolas"

#: 見 `plat.reinstall_hint` / `plat.heic_workaround`
REINSTALL_HINT = "請重新執行「安裝.bat」把環境裝齊"
HEIC_WORKAROUND = "或用「小畫家」另存為 JPG 再轉"


def seconds_since_process_start() -> float | None:
    """`GetProcessTimes` 的 creation time 對上 `GetSystemTimePreciseAsFileTime`。

    ⚠️ **不含 `啟動.vbs` 那一層**(wscript ＋ cmd,約 0.14 秒):那是父行程
    的時間,這裡問不到。"""
    import ctypes
    from ctypes import wintypes

    try:
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        created, _exit, _kernel, _user, now = (wintypes.FILETIME() for _ in range(5))
        if not k32.GetProcessTimes(wintypes.HANDLE(k32.GetCurrentProcess()),
                                   ctypes.byref(created), ctypes.byref(_exit),
                                   ctypes.byref(_kernel), ctypes.byref(_user)):
            return None
        k32.GetSystemTimePreciseAsFileTime(ctypes.byref(now))

        def ticks(ft) -> int:
            return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

        return (ticks(now) - ticks(created)) / 10_000_000     # FILETIME 是 100ns
    except Exception:
        return None


# --- 暫存目錄的存活鎖 -----------------------------------------------------

def hold_temp_lock(path):
    """Windows 的獨占是**開著就刪不掉**,所以持有 = 把檔案物件留著。"""
    from pathlib import Path

    return Path(path).open("wb")


def temp_lock_is_held(path) -> bool:
    """刪得掉 = 沒有實例持開(孤兒);刪不掉(`PermissionError`)= 還有人在用。

    ⚠️ **這個判法會把鎖檔刪掉**,所以只能由「正在清掃孤兒」的那一方呼叫。"""
    from pathlib import Path

    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        return True
    return False


def capture_blocksize(seconds: float, rate: int) -> int | None:
    """WASAPI 的 blocksize 就是環形緩衝的容量——這是 2026-07-22 掉幀那條的防線。"""
    return int(seconds * rate)


def keep_awake_argv(*, display: bool) -> list[str] | None:
    """Windows 走 `SetThreadExecutionState` + Power Request,不是外部命令。"""
    return None


# --- 已知資料夾(2026-09-26 從 `paths.py` 原樣搬來)-------------------------

#: 桌面 / 開始功能表\程式集 的 KNOWNFOLDERID
_DESKTOP_GUID = "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"
_PROGRAMS_GUID = "{A77F5D77-2E2B-44C3-A6A2-ABA601054A51}"


def known_folder(guid: str):
    r"""問 Windows 要「已知資料夾」的實際位置,問不到回 `None`。

    ⚠️ **不用 `ctypes.wintypes` 湊 GUID 結構**:那個模組在非 Windows 上 import
    就會炸,而這一層在每個平台都會被載到。改用 ctypes 的基本型別自己排,
    欄位寬度是一樣的。"""
    import ctypes          # ⚠️ 測試接縫,見本檔開頭
    from pathlib import Path
    from uuid import UUID

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


def desktop_dir():
    r"""桌面的實際位置。

    ⚠️ **不寫死 `~/Desktop`**:OneDrive 的「資料夾備份」會把桌面整個重導到
    `%USERPROFILE%\OneDrive\Desktop`,而寫死的那條路徑往往**還在、只是沒人看**
    ——檔案產出成功,使用者卻永遠找不到。所以先問 Windows,問不到才退回猜。"""
    import os
    from pathlib import Path

    if found := known_folder(_DESKTOP_GUID):
        return found
    home = Path(os.path.expanduser("~"))
    return next((p for p in (home / "OneDrive" / "Desktop", home / "Desktop")
                 if p.is_dir()), home)


def start_menu_programs_dir():
    r"""這個使用者的「開始功能表\程式集」;問不到才退回 `%APPDATA%` 那條。

    ⚠️ **回 `None` 代表連退路都不成立**——呼叫端要當成「這台機器沒有開始
    功能表」處理,不是當成錯誤:它只是桌面捷徑的備援,少了不影響工具能不能用。"""
    import os
    from pathlib import Path

    if found := known_folder(_PROGRAMS_GUID):
        return found
    if base := os.environ.get("APPDATA"):
        return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    return None


def low_power_mode() -> bool | None:
    """Windows 沒有這個開關(省電模式的機制不同),而且那邊走 GPU、
    本來就不受「錄音中兩個引擎搶 CPU」那個判斷影響。"""
    return None


# --- 防睡眠與本行程優先權(2026-09-26 從 `power.py` 原樣搬來)---------------

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

# ⚠️ **`SetThreadExecutionState` 擋不住 Modern Standby**(2026-08-07 實測):
# 那組 `ES_*` 是為傳統 S3 睡眠設計的,而 Modern Standby(S0 低耗電待命,現代
# 筆電幾乎都是)是「使用者一闔蓋/鎖屏就進入」,進去之後 Desktop Activity
# Moderator 會把 Win32 程式**整個凍結**。完整數字與災情見 `docs/dev/runtime.md`。
# `PowerRequestExecutionRequired` 正是為此設計:它**不阻止**系統進入待命
# (那是使用者的決定,也擋不住),而是請系統在待命期間讓這個行程繼續跑。
_POWER_REQUEST_CONTEXT_VERSION = 0
_POWER_REQUEST_CONTEXT_SIMPLE_STRING = 0x1
_PowerRequestSystemRequired = 1
_PowerRequestExecutionRequired = 3

#: 檔案轉逐字稿期間**本行程**的優先權類別(`SetPriorityClass` 的參數)。
#: ⚠️ **與上面的 `BELOW_NORMAL_PRIORITY_CLASS` 同值,但刻意分成兩個常數**:
#: 那一個是建立子行程時的 creationflag,這一個是對自己下的。兩者剛好同值是
#: Win32 的巧合,**綁在一起會讓「改一邊」意外動到另一邊**。
_SET_PRIORITY_BELOW_NORMAL = 0x00004000


class _REASON_DETAILED(ctypes.Structure):
    _fields_ = [
        ("LocalizedReasonModule", ctypes.c_void_p),
        ("LocalizedReasonId", ctypes.c_uint32),
        ("ReasonStringCount", ctypes.c_uint32),
        ("ReasonStrings", ctypes.POINTER(ctypes.c_wchar_p)),
    ]


class _REASON_UNION(ctypes.Union):
    # ⚠️ **兩個分支都要宣告**,即使只用 `SimpleReasonString`:union 的大小要
    # 夠大,只宣告字串指標會讓結構短少十幾個位元組,而 API 是照完整長度讀的
    # (越界讀取,症狀隨機)。`coreaudio.py` 那邊踩過同一種。
    _fields_ = [("Detailed", _REASON_DETAILED),
                ("SimpleReasonString", ctypes.c_wchar_p)]


class _REASON_CONTEXT(ctypes.Structure):
    _fields_ = [("Version", ctypes.c_uint32), ("Flags", ctypes.c_uint32),
                ("Reason", _REASON_UNION)]


def _current_process() -> "ctypes.c_void_p":
    """`GetCurrentProcess()` 的偽控制代碼(固定是 -1),包成指標大小再傳。

    ⚠️ **不可直接把 `k32.GetCurrentProcess()` 的回傳值餵給下一個 API**:
    ctypes 預設把回傳當 `c_int`,拿到的是 Python 的 -1,再當引數傳出去時只送
    32 位元——64 位元的 Windows 收到的控制代碼是錯的,`GetPriorityClass` 直接
    回 0(失敗)。實測:傳 int -1 回 0、傳 `c_void_p(-1)` 回 32(NORMAL)。
    **這種錯誤是靜默的**:優先權沒降成功,而轉檔照樣跑完。"""
    return ctypes.c_void_p(-1)


def _set_execution_state(flags: int) -> bool:
    import ctypes          # ⚠️ 測試接縫,見本檔開頭

    try:
        # 回傳 0 代表失敗(旗標無效等);非 0 為前一個狀態值
        return ctypes.windll.kernel32.SetThreadExecutionState(flags) != 0
    except Exception:
        return False


def prevent_auto_sleep(*, display: bool) -> Outcome:
    flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    if display:
        flags |= ES_DISPLAY_REQUIRED
    if _set_execution_state(flags):
        return Outcome("ok")
    return Outcome("failed", "SetThreadExecutionState 沒有設成功")


def allow_auto_sleep() -> Outcome:
    # 只留 ES_CONTINUOUS = 清除先前旗標,交還系統正常省電排程
    if _set_execution_state(ES_CONTINUOUS):
        return Outcome("ok")
    return Outcome("failed", "SetThreadExecutionState 沒有清成功")


def keep_running_in_standby(reason: str) -> tuple[Outcome, object]:
    import ctypes          # ⚠️ 測試接縫,見本檔開頭

    try:
        k32 = ctypes.windll.kernel32
        ctx = _REASON_CONTEXT()
        ctx.Version = _POWER_REQUEST_CONTEXT_VERSION
        ctx.Flags = _POWER_REQUEST_CONTEXT_SIMPLE_STRING
        ctx.Reason.SimpleReasonString = reason
        k32.PowerCreateRequest.restype = ctypes.c_void_p
        handle = k32.PowerCreateRequest(ctypes.byref(ctx))
        # ⚠️ 失敗是 INVALID_HANDLE_VALUE(-1)**不是 0**——照 0 判斷會拿著 -1
        # 當控制代碼往下傳,後面每一支 API 都靜默失敗
        if not handle or handle == ctypes.c_void_p(-1).value:
            return Outcome("failed", "PowerCreateRequest 沒有給控制代碼"), None
        ok = False
        for kind in (_PowerRequestExecutionRequired, _PowerRequestSystemRequired):
            # 兩種都設:ExecutionRequired 只在 Modern Standby 機器上有意義,
            # 傳統 S3 的機器要靠 SystemRequired。任一成功就算數
            if k32.PowerSetRequest(ctypes.c_void_p(handle), kind):
                ok = True
        if not ok:
            k32.CloseHandle(ctypes.c_void_p(handle))
            return Outcome("failed", "PowerSetRequest 兩種都沒設成功"), None
        return Outcome("ok"), handle
    except Exception as e:  # noqa: BLE001 - 舊版 Windows 沒有這組 API
        logger.debug("PowerCreateRequest 失敗", exc_info=True)
        return Outcome("failed", f"這台的 Windows 沒有這組 API({e})"), None


def stop_keeping_running_in_standby(token: object) -> None:
    if token is None:
        return
    import ctypes          # ⚠️ 測試接縫,見本檔開頭

    try:
        k32 = ctypes.windll.kernel32
        for kind in (_PowerRequestExecutionRequired, _PowerRequestSystemRequired):
            k32.PowerClearRequest(ctypes.c_void_p(token), kind)
        k32.CloseHandle(ctypes.c_void_p(token))
    except Exception:  # noqa: BLE001
        logger.debug("PowerClearRequest 失敗", exc_info=True)


def _get_priority_class() -> int | None:
    import ctypes          # ⚠️ 測試接縫,見本檔開頭

    try:
        got = ctypes.windll.kernel32.GetPriorityClass(_current_process())
        return int(got) or None  # 0 = 失敗
    except Exception:
        return None


def _set_priority_class(value: int) -> bool:
    import ctypes          # ⚠️ 測試接縫,見本檔開頭

    try:
        return bool(
            ctypes.windll.kernel32.SetPriorityClass(_current_process(), value)
        )
    except Exception:
        return False


def lower_own_priority() -> tuple[Outcome, object]:
    previous = _get_priority_class()
    if not _set_priority_class(_SET_PRIORITY_BELOW_NORMAL):
        return Outcome("failed", "SetPriorityClass 沒有設成功"), None
    return Outcome("ok"), previous


def restore_own_priority(token: object) -> None:
    if token:
        _set_priority_class(int(token))


def system_audio_capability() -> Outcome:
    """Windows 用 WASAPI loopback,不必先建什麼——真正的把關在
    `record.find_loopback_mic()`(有沒有播放裝置)。"""
    return Outcome("ok")


def apple_speech_helper() -> tuple[Outcome, object]:
    """Apple 的語音辨識只在 macOS 上有;這邊的「模型」選項本來就不會出現它。"""
    return Outcome("unsupported", "Apple 的語音辨識只在 Mac 上有"), None


def open_system_audio():
    """`None` = 走既有的 loopback 那條路。"""
    return None


def update_asset() -> str | None:
    from meeting_scribe import update

    return update.WIN_ASSET


def self_update_capability() -> Outcome:
    return Outcome("ok")


def stage_update(zip_path, version: str, into):
    """Windows 的包是純檔案,`zipfile` 解就對了(驗包在 `update.verify_zip`)。"""
    from pathlib import Path

    from meeting_scribe import update

    update.verify_zip(Path(zip_path), version)
    return update.stage(Path(zip_path), Path(into))


def update_restart_note() -> str | None:
    return None


def update_manual_note() -> str | None:
    return None


# 換版小程式要活得比本程式久:不掛主控台、自成一個行程群組、盡量脫離工作物件
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def apply_update(staged, target, version: str) -> Outcome:
    r"""把 `update_helper.py` 複製到工作目錄,用 uv 的**基底**直譯器叫起來。

    ⚠️ **不用 `.venv` 裡那支 pythonw**:`uv sync` 可能換掉它(見 `update_helper` 檔頭)。
    `sys._base_executable` 在 uv 的環境裡指向 `%APPDATA%\uv\python\...\python.exe`,
    旁邊就有 pythonw.exe(2026-10-07 在開發機上確認過)。
    ⚠️ **`CREATE_BREAKAWAY_FROM_JOB` 可能被拒**(父行程所在的工作物件不准脫離時是
    存取被拒):那就不帶它再試一次——啟動器與 cmd 平常都不建工作物件。"""
    import os
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    from meeting_scribe import update

    staged, target = Path(staged), Path(target)
    base = Path(getattr(sys, "_base_executable", "") or sys.executable)
    exe = base.with_name("pythonw.exe")
    if not exe.exists():
        exe = base
    if not exe.exists():
        return Outcome("failed", f"找不到可以執行換版小程式的 Python({base})")
    helper = staged.parent / "update_helper.py"
    try:
        shutil.copy2(update.helper_source(), helper)
    except OSError as e:
        return Outcome("failed", f"換版小程式複製不過去({e})")
    argv = [str(exe), "-I", str(helper), "--staged", str(staged),
            "--target", str(target), "--pid", str(os.getpid()), "--version", version]
    flags = CREATE_NO_WINDOW | DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    for extra in (CREATE_BREAKAWAY_FROM_JOB, 0):
        try:
            subprocess.Popen(argv, cwd=str(staged.parent), close_fds=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, creationflags=flags | extra)
            return Outcome("ok")
        except OSError as e:
            err = e
    return Outcome("failed", f"換版小程式起不來({err})")


# COM 的 apartment 模型。MTA:誰都可以從任何執行緒呼叫,不必有訊息迴圈
COINIT_MULTITHREADED = 0x0


def set_window_icon(window, ico_path, png_path):
    r"""Windows 走 `iconbitmap(default=…)`:那個 `-default` 讓它同時成為**這個
    行程之後所有 toplevel** 的預設圖示,標題列與 Alt+Tab 都吃得到。

    ⚠️ **不要「順手統一」成 `iconphoto`**:工作列那顆走的是另一條(見
    `desktop._apply_window_icon`),而這條路上 `.ico` 自帶多種尺寸,換成單一
    PNG 會讓小尺寸變成縮圖、糊一階。⚠️ **不吞例外**,理由見 `plat` 那一支。
    ⚠️ 回 `None`:這條路沒有需要留著參照的東西。"""
    window.iconbitmap(default=str(ico_path))
    return None


def strip_default_menubar(window) -> None:
    """Windows 的 Tk 不掛選單列,沒有東西要清。⚠️ **而且要安靜**。"""
    return None


def co_initialize_ex() -> int | None:
    """⚠️ **一律 `& 0xFFFFFFFF` 轉無號再回**:ctypes 預設把回傳當有號
    `c_int`,少了這一步 `RPC_E_CHANGED_MODE`(0x80010106)會是負的,於是
    「本執行緒已在別的 apartment」被誤判成失敗。呼叫端那份可接受清單
    (`record._COINIT_OK`)比的是無號值。"""
    import ctypes

    try:
        return ctypes.windll.ole32.CoInitializeEx(
            None, COINIT_MULTITHREADED
        ) & 0xFFFFFFFF
    except Exception:
        # ole32 是 KnownDLL,理論上不會發生;真發生時不得靜靜略過
        logger.warning("叫不到 ole32.CoInitializeEx", exc_info=True)
        return None


def round_window_corners(child_id: int, width: int, height: int,
                         radius: int) -> None:
    r"""Windows 11 用 DWM 的 `DWMWA_WINDOW_CORNER_PREFERENCE`(33)=
    `DWMWCP_ROUND`(2),角是反鋸齒的、還帶系統那圈細邊;Windows 10 沒有這個
    屬性(回 `E_INVALIDARG`),退回 `SetWindowRgn` 硬切一個圓角矩形(角會
    鋸齒,但至少是圓的)。

    ⚠️ **HWND 要往上取一層**(`GetParent`,同 `winui.window_handle` 那條):
    `winfo id` 給的是 Tk 自己那個子視窗,DWM 不認。
    ⚠️ **`restype` 一定要設成指標寬**,理由也在那裡。
    ⚠️ **這一支刻意不自己吞例外**:呼叫端(`desktop._round_popup`)本來就有
    一個「失敗只記一行 DEBUG」的兜底,兩層都接會讓真正的成因被吃掉一次。"""
    import ctypes

    user32 = ctypes.windll.user32
    user32.GetParent.restype = ctypes.c_void_p
    user32.GetParent.argtypes = [ctypes.c_void_p]
    hwnd = user32.GetParent(child_id) or child_id
    pref = ctypes.c_int(2)                        # DWMWCP_ROUND
    hr = ctypes.windll.dwmapi.DwmSetWindowAttribute(
        ctypes.c_void_p(hwnd), 33, ctypes.byref(pref), ctypes.sizeof(pref))
    if hr == 0:
        return
    gdi32 = ctypes.windll.gdi32
    gdi32.CreateRoundRectRgn.restype = ctypes.c_void_p
    rgn = gdi32.CreateRoundRectRgn(0, 0, width + 1, height + 1, radius, radius)
    user32.SetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
    user32.SetWindowRgn(ctypes.c_void_p(hwnd), rgn, 1)


def total_ram_mb() -> int | None:
    """**用 `GlobalMemoryStatusEx` 不用 `os` 那邊的東西**:標準函式庫沒有跨
    版本可靠的實體記憶體查詢,而這支 Win32 API 一次呼叫就給總量與可用量。"""
    import ctypes

    class _MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_uint32),
            ("dwMemoryLoad", ctypes.c_uint32),
            ("ullTotalPhys", ctypes.c_uint64),
            ("ullAvailPhys", ctypes.c_uint64),
            ("ullTotalPageFile", ctypes.c_uint64),
            ("ullAvailPageFile", ctypes.c_uint64),
            ("ullTotalVirtual", ctypes.c_uint64),
            ("ullAvailVirtual", ctypes.c_uint64),
            ("ullAvailExtendedVirtual", ctypes.c_uint64),
        ]

    try:
        status = _MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return int(status.ullTotalPhys / (1024 * 1024))
    except Exception:  # noqa: BLE001 - 量不到就讓呼叫端用保守預設
        logger.debug("讀取實體記憶體失敗", exc_info=True)
        return None


def process_memory_mb() -> float | None:
    """**用 `K32GetProcessMemoryInfo`(kernel32)不用 psapi.dll 那個**:後者在
    新版 Windows 上是轉發樁,直接呼叫常常回 0 而且不報錯——那會讓守衛看起來
    有在跑、實際永遠不觸發。"""
    import ctypes
    import ctypes.wintypes as wintypes

    try:
        class _Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in (
                    "PeakWorkingSetSize", "WorkingSetSize",
                    "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                    "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                    "PagefileUsage", "PeakPagefileUsage")]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.K32GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(_Counters), wintypes.DWORD]
        k32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        counters = _Counters()
        counters.cb = ctypes.sizeof(counters)
        if not k32.K32GetProcessMemoryInfo(
                k32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return None
        return counters.WorkingSetSize / 1e6
    except Exception:  # noqa: BLE001 - 量不到就當守衛不存在,不影響辨識
        logger.debug("讀取記憶體用量失敗", exc_info=True)
        return None


def preload_onnxruntime() -> None:
    """System32 那份舊版 `onnxruntime.dll`(API 只到 17)會被依名稱優先解析,
    sherpa-onnx 的原生模組需要較新的 ORT C API,綁到舊版會直接 segfault;
    先以完整路徑載入 pip 版 DLL,讓後續依名稱解析綁到正確版本。

    ⚠️ **這裡刻意不包 `try`**:`import onnxruntime` 的 `ImportError` 與
    `ctypes.WinDLL` 的 `OSError` 都必須往上傳——`diarize._ensure_sherpa`
    接住它們、翻成繁中的「AI 元件載入失敗」,而那是「這台電腦缺 Visual C++
    執行階段」唯一的出路。要吞的那一邊(`ocr_worker`)自己包一層。"""
    import ctypes
    from pathlib import Path

    import onnxruntime

    dll = Path(onnxruntime.__file__).parent / "capi" / "onnxruntime.dll"
    if dll.exists():
        ctypes.WinDLL(str(dll))


def system_cpu_ticks() -> tuple[float, float] | None:
    """`GetSystemTimes`:回 `(閒置, 總計)`,單位是 100 奈秒。"""
    import ctypes   # ⚠️ 這支要在函式內,理由見檔頭

    try:
        idle, kern, user = _FILETIME(), _FILETIME(), _FILETIME()
        if not ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user)
        ):
            return None
        val = (lambda f: (f.hi << 32) | f.lo)
        # kernel 時間**包含** idle,總計 = kernel + user
        return float(val(idle)), float(val(kern) + val(user))
    except Exception:  # noqa: BLE001 - 診斷少一欄而已,絕不影響錄音
        logger.debug("讀取全機 CPU 時間失敗", exc_info=True)
        return None


def microphone_permission() -> Outcome:
    """⚠️ **Windows 也有逐 App 的麥克風隱私設定,但這裡刻意回 `unsupported`**:
    問不到就不要猜。回 `unsupported` 讓呼叫端維持原本的行為(留「仍要錄」),
    那正是 2026-09-25 之前在這個平台上驗過的形狀——**行為完全不動**。"""
    return Outcome("unsupported", "Windows 這邊問不到逐 App 的麥克風授權狀態")


def open_microphone_settings() -> Outcome:
    """沒有對應的深層連結(而且上面那支問不到狀態,開了也沒有前後文)。"""
    return Outcome("unsupported", "Windows 這邊沒有要開的麥克風權限頁")
