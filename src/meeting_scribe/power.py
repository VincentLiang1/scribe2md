"""轉檔/錄音期間阻止 Windows 進入睡眠;轉檔另擋螢幕關閉。

以 SetThreadExecutionState 告訴系統「持續有工作」,兩種模式旗標不同:
- 轉檔(keep_awake):ES_SYSTEM_REQUIRED + ES_DISPLAY_REQUIRED——鎖定
  螢幕/進入省電會壓抑 CPU 時脈拉長轉檔時間,且轉檔時人就在電腦前。
- 錄音(stay_awake_begin/end):只設 ES_SYSTEM_REQUIRED——擋系統睡眠/
  休眠讓錄音與背景轉錄持續,但螢幕照常省電關閉(使用者指定 2026-07-22:
  一兩小時的會議沒必要整場亮著螢幕;螢幕關閉/鎖定不影響 WASAPI 收音)。
結束後解除,恢復正常省電。非 Windows 或呼叫失敗時安靜略過,
絕不影響轉檔/錄音本身。
"""

import contextlib
import ctypes
import logging
import os
import sys
import threading
from collections.abc import Iterator

logger = logging.getLogger(__name__)

class _FILETIME(ctypes.Structure):
    """GetSystemTimes 的輸出格式。在模組層定義一次:每次呼叫重新建
    Structure 子類要走 metaclass 建 field descriptor(數十 µs),而這是
    診斷路徑上會反覆呼叫的東西。"""

    _fields_ = [("lo", ctypes.c_uint32), ("hi", ctypes.c_uint32)]


ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

# --- Modern Standby(新式待命)下的「請讓我繼續執行」 ---
#
# ⚠️ **SetThreadExecutionState 在 Modern Standby 機器上擋不住待命**
# (2026-08-07 實測到,使用者回報「轉檔跑到一半網頁連不上」才查出來):
# 那組旗標是為傳統 S3 睡眠設計的,而 Modern Standby(S0 低耗電待命,
# 現代筆電幾乎都是)是「使用者一闔蓋/鎖屏就進入」,ES_* 攔不住;進去
# 之後 Desktop Activity Moderator 會把 Win32 程式**整個凍結**。
#
# 實測數字(8339 秒音訊的轉檔,每 5 秒取樣一次記憶體):牆鐘 63.1 分鐘
# 裡有 40.1 分鐘行程完全停擺,單次最長 32.7 分——取樣中斷的起訖與系統
# 日誌的 Modern Standby 進出時間**逐秒吻合**(21:43:39 進入 / 22:16:16
# 離開)。使用者看到的是「網頁連不上、F5 也沒用」,而程式其實還在,
# 只是被凍結了。
#
# PowerRequestExecutionRequired 正是為此設計的:它**不阻止系統進入待命**
# (那是使用者的決定,也擋不住),而是讓**這個行程**在待命期間繼續執行。
# Windows 8 起支援;MSDN 明言能跑多久仍取決於作業系統與電源原則,所以
# 這是「盡力而為」不是保證——但沒有它就是必定被凍結。
POWER_REQUEST_CONTEXT_VERSION = 0
POWER_REQUEST_CONTEXT_SIMPLE_STRING = 0x1
PowerRequestSystemRequired = 1
PowerRequestExecutionRequired = 3


class _REASON_DETAILED(ctypes.Structure):
    _fields_ = [
        ("LocalizedReasonModule", ctypes.c_void_p),
        ("LocalizedReasonId", ctypes.c_uint32),
        ("ReasonStringCount", ctypes.c_uint32),
        ("ReasonStrings", ctypes.POINTER(ctypes.c_wchar_p)),
    ]


class _REASON_UNION(ctypes.Union):
    # **兩個分支都要宣告**,即使只用 SimpleReasonString:union 的大小要
    # 夠大,只宣告字串指標會讓結構短少十幾個位元組,而 API 是照完整長度
    # 讀的(越界讀取,症狀隨機)
    _fields_ = [("Detailed", _REASON_DETAILED),
                ("SimpleReasonString", ctypes.c_wchar_p)]


class _REASON_CONTEXT(ctypes.Structure):
    _fields_ = [("Version", ctypes.c_uint32), ("Flags", ctypes.c_uint32),
                ("Reason", _REASON_UNION)]

# 檔案轉逐字稿期間用的程序優先權(見 below_normal_priority)。數值與
# ocr._BELOW_NORMAL_PRIORITY 相同但**不共用常數**:那邊是 CreateProcess 的
# creationflag(建立子行程時給),這裡是 SetPriorityClass 的參數(對自己下),
# 兩者剛好同值是 Win32 的巧合,綁在一起會讓「改一邊」意外動到另一邊
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000

# 預設保留給系統/其他程式的 CPU 核心數:轉檔本來會用滿所有核心求快,
# 預設保留 1 核(預設使用核心數 = 最大核心數 - 1),使用者可在介面調整。
_RESERVED_CORES = 1
# 使用者於介面指定的核心數;None = 用預設(最大核心數 - _RESERVED_CORES)
_worker_count: int | None = None


def max_cpu_cores() -> int:
    """偵測到的最大 CPU 核心數(至少 1)。"""
    return os.cpu_count() or 1


class _MEMORYSTATUSEX(ctypes.Structure):
    """GlobalMemoryStatusEx 的輸出格式(同 _FILETIME,在模組層定義一次)。"""

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


def total_ram_mb() -> int | None:
    """整台機器的實體記憶體(MB);量不到回 None。

    給「該分多少記憶體給某個子系統」這種決策用(目前是 OCR 子行程的引擎
    回收門檻)——同 `max_cpu_cores`,機器能力的偵測集中在這個模組。

    **用 GlobalMemoryStatusEx 不用 `os` 那邊的東西**:標準函式庫沒有跨版本
    可靠的實體記憶體查詢,而這支 Win32 API 一次呼叫就給總量與可用量。"""
    if sys.platform != "win32":
        return None
    try:
        status = _MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return int(status.ullTotalPhys / (1024 * 1024))
    except Exception:  # noqa: BLE001 - 量不到就讓呼叫端用保守預設
        logger.debug("讀取實體記憶體失敗", exc_info=True)
        return None


def default_worker_count() -> int:
    """預設使用核心數 = 最大核心數 - _RESERVED_CORES(至少 1)。介面預設值。"""
    return max(1, max_cpu_cores() - _RESERVED_CORES)


def set_worker_count(n: int | None) -> bool:
    """設定使用核心數(夾到 1..最大核心數);None/非正整數回到預設。
    回傳是否有變動(供呼叫端決定是否清引擎快取,讓新值生效)。"""
    global _worker_count
    new = min(int(n), max_cpu_cores()) if isinstance(n, int) and n > 0 else None
    changed = new != _worker_count
    _worker_count = new
    return changed


def cpu_worker_count(cap: int | None = None) -> int:
    """CPU 密集工作的執行緒數:使用者指定值(或預設 最大核心數-1),
    夾到 1..最大核心數,再套用可選上限 cap(如標點模型只需 4)。"""
    n = _worker_count if _worker_count else default_worker_count()
    n = max(1, min(n, max_cpu_cores()))
    return n if cap is None else max(1, min(n, cap))


def system_cpu_ticks() -> tuple[float, float] | None:
    """(閒置, 總計) 的系統 CPU 時間;取不到回 None(呼叫端少一欄,不影響功能)。

    **為什麼要看全機而不是本行程**:講者分析自 2026-08-03 起跑在子行程裡
    (見 diarworker),`time.process_time()` 只算得到自己——工作搬走之後那個
    數字看起來就一直很閒,而機器其實滿載。診斷若答不出「機器有多忙」,就
    分不出「子行程在算」與「子行程掛了」,而那兩種情況的收音數字一模一樣
    (實測真的騙過一輪)。擺在 power 是因為這裡本來就是「這台機器的資源」
    那一層(ctypes、SetThreadExecutionState、cpu_worker_count)。"""
    if sys.platform != "win32":
        return None
    try:
        idle, kern, user = _FILETIME(), _FILETIME(), _FILETIME()
        if not ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user)
        ):
            return None
        val = (lambda f: (f.hi << 32) | f.lo)
        # kernel 時間**包含** idle,總計 = kernel + user
        return float(val(idle)), float(val(kern) + val(user))
    except Exception:
        return None


def _set_execution_state(flags: int) -> bool:
    """呼叫 Win32 SetThreadExecutionState;成功回傳 True。非 Windows 回 False。"""
    if sys.platform != "win32":
        return False
    try:
        # 回傳 0 代表失敗(旗標無效等);非 0 為前一個狀態值
        return ctypes.windll.kernel32.SetThreadExecutionState(flags) != 0
    except Exception:
        return False


def _execution_request_begin(reason: str):
    """要求「待命期間讓這個行程繼續跑」;回傳控制代碼(失敗回 None)。

    與 `_set_execution_state` **並用而不是取代**:兩者管的是不同的事——
    ES_* 阻止「自動」進入睡眠/關螢幕(傳統 S3 仍然只有它有效),
    這一支則是 Modern Standby 已經進去之後不要凍結我(見上方常數註解)。

    取不到就安靜回 None:防護拿不到絕不能讓轉檔/錄音跑不起來。"""
    if sys.platform != "win32":
        return None
    try:
        k32 = ctypes.windll.kernel32
        ctx = _REASON_CONTEXT()
        ctx.Version = POWER_REQUEST_CONTEXT_VERSION
        ctx.Flags = POWER_REQUEST_CONTEXT_SIMPLE_STRING
        ctx.Reason.SimpleReasonString = reason
        k32.PowerCreateRequest.restype = ctypes.c_void_p
        handle = k32.PowerCreateRequest(ctypes.byref(ctx))
        # 失敗是 INVALID_HANDLE_VALUE(-1)不是 0——照 0 判斷會拿著 -1
        # 當控制代碼往下傳,後面每一支 API 都靜默失敗
        if not handle or handle == ctypes.c_void_p(-1).value:
            return None
        ok = False
        for kind in (PowerRequestExecutionRequired, PowerRequestSystemRequired):
            # 兩種都設:ExecutionRequired 只在 Modern Standby 機器上有意義,
            # 傳統 S3 的機器要靠 SystemRequired。任一成功就算數
            if k32.PowerSetRequest(ctypes.c_void_p(handle), kind):
                ok = True
        if not ok:
            k32.CloseHandle(ctypes.c_void_p(handle))
            return None
        return handle
    except Exception:  # noqa: BLE001 - 舊版 Windows 沒有這組 API
        logger.debug("PowerCreateRequest 失敗", exc_info=True)
        return None


def _execution_request_end(handle) -> None:
    """撤銷上面那個請求(拿不到控制代碼時什麼都不做)。"""
    if handle is None or sys.platform != "win32":
        return
    try:
        k32 = ctypes.windll.kernel32
        for kind in (PowerRequestExecutionRequired, PowerRequestSystemRequired):
            k32.PowerClearRequest(ctypes.c_void_p(handle), kind)
        k32.CloseHandle(ctypes.c_void_p(handle))
    except Exception:  # noqa: BLE001
        logger.debug("PowerClearRequest 失敗", exc_info=True)


@contextlib.contextmanager
def keep_awake() -> Iterator[None]:
    """轉檔期間維持系統與螢幕喚醒;離開(含例外)必定解除。

    **兩道防護,管的是不同的事**(2026-08-07 實測補上第二道):
    - `ES_*`:阻止「自動」進入睡眠與關螢幕。
    - Power Request(`ExecutionRequired`):使用者主動闔蓋/鎖屏而系統進入
      **Modern Standby** 時,ES_* 完全攔不住,而進去之後 Win32 程式會被
      整個凍結——實測一支 2 小時 19 分的錄音轉檔,牆鐘 63 分鐘裡有 40
      分鐘行程停擺(見模組上方常數的完整數字)。這一道不阻止待命,
      而是請系統在待命期間讓這個行程繼續跑。
    """
    active = _set_execution_state(
        ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
    )
    if not active and sys.platform == "win32":
        logger.warning("無法設定防止休眠狀態,轉檔期間螢幕仍可能鎖定")
    request = _execution_request_begin("meeting-scribe 正在轉檔")
    if request is None and sys.platform == "win32":
        logger.warning(
            "無法要求「待命期間繼續執行」:若電腦進入新式待命(闔蓋/鎖屏),"
            "轉檔會暫停,喚醒後才繼續"
        )
    try:
        yield
    finally:
        _execution_request_end(request)
        if active:
            # 只留 ES_CONTINUOUS = 清除先前旗標,交還系統正常省電排程
            _set_execution_state(ES_CONTINUOUS)


def _current_process() -> "ctypes.c_void_p":
    """GetCurrentProcess() 的偽控制代碼(固定是 -1),包成指標大小再傳。

    **不可直接把 `k32.GetCurrentProcess()` 的回傳值餵給下一個 API**:ctypes
    預設把回傳當 c_int,拿到的是 Python 的 -1,再當引數傳出去時只送 32 位元
    ——64 位元的 Windows 收到的控制代碼是錯的,`GetPriorityClass` 直接回 0
    (失敗)。實測:傳 int -1 回 0、傳 c_void_p(-1) 回 32(NORMAL)。
    這種錯誤是**靜默**的:優先權沒降成功,而轉檔照樣跑完。"""
    return ctypes.c_void_p(-1)


def _get_priority_class() -> int | None:
    """本行程目前的優先權類別;取不到回 None。"""
    if sys.platform != "win32":
        return None
    try:
        got = ctypes.windll.kernel32.GetPriorityClass(_current_process())
        return int(got) or None  # 0 = 失敗
    except Exception:
        return None


def _set_priority_class(value: int) -> bool:
    """設定本行程的優先權類別;成功回 True。非 Windows 一律 False。"""
    if sys.platform != "win32":
        return False
    try:
        return bool(
            ctypes.windll.kernel32.SetPriorityClass(_current_process(), value)
        )
    except Exception:
        return False


@contextlib.contextmanager
def below_normal_priority() -> Iterator[None]:
    """檔案轉逐字稿期間把**整支程式**降到 below-normal;離開必定還原。

    起因:使用者 2026-08-04 回報「現成檔案轉換時 CPU 會用到 98~99%」,
    要的是「轉檔時電腦還能用」。這是 OCR 子行程 2026-08-03 用過的同一招
    (見 ocr._BELOW_NORMAL_PRIORITY 的實測表:前景被拖慢 +16% → +2%,
    代價是自己慢約兩成),差別在**用法**:那邊是 Popen 的 creationflag,
    這裡的轉錄與講者分析都在主行程的執行緒裡,沒有子行程可以掛旗標,
    只能對已經在跑的自己呼叫 SetPriorityClass。

    ⚠️ **降優先權不會讓工作管理員的 CPU% 變小**——它只決定「搶不搶得贏」,
    機器閒著時照樣跑到 98~99%。要數字下降只能調小「CPU 核心數」
    (見 cpu_worker_count),那是另一個旋鈕、使用者可自行調整。

    ⚠️ **連 UI 一起降**:主行程同時是 gradio 的網頁伺服器,所以前景忙碌時
    進度更新與「停止」的回應也會跟著等(平常滿載就已有數十秒延遲,見
    ui_style 的停止鈕即時回饋)。這是 in-process 工作的必然代價;真要避免
    就得把轉錄也搬進子行程,那是另一個量級的改動。

    **只包住檔案轉檔**(run_pipeline)。現場收音不可套用:收音執行緒被排擠
    正是 2026-08-03 掉了 4.6 分鐘音訊的那個災情,錄音期間任何降低自身
    排程權重的動作都是反方向(見 record._CaptureDiag 與 diarworker)。

    還原時寫回**原本那個值**而不是寫死 NORMAL:使用者可能自己用工作管理員
    或捷徑把整支程式設成別的優先權,轉完檔擅自拉回一般是把設定改掉。"""
    previous = _get_priority_class()
    lowered = _set_priority_class(BELOW_NORMAL_PRIORITY_CLASS)
    if not lowered and sys.platform == "win32":
        logger.warning("無法調低程序優先權,轉檔期間電腦操作可能較不順暢")
    try:
        yield
    finally:
        if lowered and previous:
            _set_priority_class(previous)


# 現場收音的防睡眠旗標由這條長駐執行緒持有(見 stay_awake_begin);
# _awake_release 兼任「生效中」判準:非 None = 執行緒在跑
_awake_lock = threading.Lock()
_awake_release: threading.Event | None = None


def _awake_worker(release: threading.Event) -> None:
    """在自己身上設旗標後守著 release 事件;收到通知清旗標退場。
    不帶 ES_DISPLAY_REQUIRED:錄音只需系統不睡,螢幕照常省電關閉
    (使用者指定 2026-07-22)。

    ⚠️ **而螢幕關閉正是 Modern Standby 的進入條件**——所以錄音這條反而
    比轉檔更需要 Power Request:被凍結就是直接掉音訊,而且掉了不能重來
    (2026-08-07 在轉檔上實測到 40 分鐘凍結,見模組上方常數)。"""
    active = _set_execution_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    if not active and sys.platform == "win32":
        logger.warning("無法設定防止睡眠狀態,錄音期間系統仍可能睡眠中斷錄音")
    request = _execution_request_begin("meeting-scribe 正在錄音")
    if request is None and sys.platform == "win32":
        logger.warning(
            "無法要求「待命期間繼續執行」:電腦進入新式待命時錄音會中斷,"
            "而錄音無法重來——建議錄音期間不要闔上螢幕或鎖定"
        )
    try:
        release.wait()
    finally:
        _execution_request_end(request)
        if active:
            _set_execution_state(ES_CONTINUOUS)


def stay_awake_begin() -> None:
    """現場收音用的「開關式」防睡眠:錄音橫跨多個 UI 事件,無法以單一
    context manager 包住,由開始/停止事件成對呼叫。

    旗標必須由自家長駐執行緒持有:SetThreadExecutionState 的 ES_CONTINUOUS
    是「綁呼叫執行緒」的狀態,執行緒結束旗標即自動消失——gradio 事件跑在
    anyio 執行緒池的短命 worker 上(閒置 10 秒回收,anyio WorkerThread.
    MAX_IDLE_TIME 實查),直接在事件執行緒上設旗標撐不過幾秒、防護即蒸發
    (2026-07-22 使用者回報);end 也落在另一條執行緒,清不到原本那份。
    keep_awake(轉檔用)不受此雷:整段轉檔都在同一條事件執行緒上忙,
    設與清同緒、且執行緒活著——與本執行緒的旗標各自獨立,互不干擾。
    重複 begin 無害(生效中直接返回);daemon=True,硬退出隨行程消滅、
    Windows 自動清掉該執行緒的旗標。"""
    global _awake_release
    with _awake_lock:
        if _awake_release is not None:
            return
        _awake_release = threading.Event()
        threading.Thread(
            target=_awake_worker, args=(_awake_release,),
            name="stay-awake", daemon=True,
        ).start()


def stay_awake_end() -> None:
    global _awake_release
    with _awake_lock:
        if _awake_release is not None:
            _awake_release.set()
            _awake_release = None
