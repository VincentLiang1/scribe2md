"""平台抽象層的「其他平台」實作(Linux、CI、開發者的任何機器)。

**這一份不是湊數的**(spec/mac/02 §2.4):沒有它,測試在非 Windows/macOS
的環境上就得靠 `try/except` 兜,而那正是「靜默 no-op」的溫床。這裡每一支
都明確回 `unsupported` **而且附繁中理由**,測試可以直接斷言這件事。
"""

from meeting_scribe.plat import Outcome

NAME = "null"

#: 不知道這個平台的 CPU 跑不跑得動大模型;照 Windows 的保守做法
ACCURATE_MIN_CORES = None

#: 不知道這個平台撐不撐得住兩個引擎並行;照保守的做
LIVE_DIARIZE_MIN_CORES = None

#: 錄音中不限制執行緒數(這個平台本來就不走錄音中的講者分析)。
LIVE_CPU_THREADS = None

#: 不知道這個平台的 `process_memory_mb` 量的是什麼(這一層根本量不到,
#: 回 `None`),也就無從換算 OCR 的回收門檻;照 `ocr_worker` 的預設。
OCR_MEMORY_ANCHORS_MB = None

_WHY = "這個平台沒有對應的做法(只有 Windows 與 macOS 有實作)"


def spawn_kwargs(*, low_priority: bool) -> dict:
    return {}


def worker_argv(*, low_priority: bool) -> list[str]:
    return []


def apply_own_low_priority() -> Outcome:
    return Outcome("unsupported", _WHY)


# --- 外殼整合 -------------------------------------------------------------

def allow_foreground() -> Outcome:
    return Outcome("unsupported", _WHY)


def open_folder(path) -> Outcome:
    return Outcome("unsupported", _WHY)


def reveal(path) -> Outcome:
    return Outcome("unsupported", _WHY)


#: 等寬字型。Courier 是 X11 時代就有的泛用名,哪裡都找得到
MONO_FAMILY = "Courier"

#: 見 `plat.reinstall_hint` / `plat.heic_workaround`(這邊只有開發者會看到)
REINSTALL_HINT = "請在工具資料夾執行 uv sync 把環境裝齊"
HEIC_WORKAROUND = "或先另存為 JPG 再轉"


def seconds_since_process_start() -> float | None:
    """問不到。⚠️ 這裡回 None 是**刻意**的:呼叫端只拿它記一行診斷,
    編一個「從 import 算起」的數字回去比沒有更糟——那會讓人以為啟動
    真的只花了那麼久。"""
    return None


# --- 暫存目錄的存活鎖 -----------------------------------------------------

def hold_temp_lock(path):
    """沒有鎖,但**檔案還是要建出來**:清掃那一側靠它的存在與否判斷。"""
    from pathlib import Path

    return Path(path).open("wb")


def temp_lock_is_held(path) -> bool:
    """⚠️ **判不出來就回 True**(保守):把活的當孤兒刪掉,使用者正在做的事
    當場斷掉;把孤兒當活的留著,只是暫存目錄多留一次。"""
    from pathlib import Path

    return Path(path).exists()


def capture_blocksize(seconds: float, rate: int) -> int | None:
    """不知道這個平台的緩衝模型:交給函式庫決定,別猜。"""
    return None


def keep_awake_argv(*, display: bool) -> list[str] | None:
    """不知道這個平台怎麼擋睡眠。"""
    return None


def desktop_dir():
    """猜一個:`~/Desktop`,不在就回家目錄。"""
    from pathlib import Path

    home = Path.home()
    desk = home / "Desktop"
    return desk if desk.is_dir() else home


def start_menu_programs_dir():
    """不知道這個平台有沒有這種東西。"""
    return None


def play_sound(path) -> tuple[Outcome, object]:
    """不知道這個平台怎麼放聲音。⚠️ **回 `unsupported` 不是 `failed`**:
    使用者在這台機器上該做的事是「改看摘錄認人」,不是去修什麼。"""
    return Outcome("unsupported", _WHY), None


def stop_sound(token: object) -> None:
    """沒有東西在放(而且要安靜:收起命名區就會走一次)。"""


def low_power_mode() -> bool | None:
    """不知道這個平台怎麼問。"""
    return None


def prevent_auto_sleep(*, display: bool) -> Outcome:
    return Outcome("unsupported", _WHY)


def allow_auto_sleep() -> Outcome:
    return Outcome("unsupported", _WHY)


def keep_running_in_standby(reason: str) -> tuple[Outcome, object]:
    return Outcome("unsupported", _WHY), None


def stop_keeping_running_in_standby(token: object) -> None:
    """沒有請求可以撤(而且要安靜:每次轉檔與錄音的 `finally` 都會走一次)。"""


def lower_own_priority() -> tuple[Outcome, object]:
    return Outcome("unsupported", _WHY), None


def restore_own_priority(token: object) -> None:
    """沒有降過,所以沒有東西要還原。"""


def system_audio_capability() -> Outcome:
    return Outcome("unsupported", _WHY)


def apple_speech_helper() -> tuple[Outcome, object]:
    return Outcome("unsupported", _WHY), None


def open_system_audio():
    return None


def update_asset() -> str | None:
    return None


def self_update_capability() -> Outcome:
    return Outcome("unsupported", _WHY)


def stage_update(zip_path, version: str, into):
    raise ValueError(_WHY)


def update_restart_note() -> str | None:
    return None


def update_manual_note() -> str | None:
    return None


def apply_update(staged, target, version: str) -> Outcome:
    return Outcome("unsupported", _WHY)


def total_ram_mb() -> int | None:
    """不知道這個平台怎麼問;呼叫端會退回保守的下限。"""
    return None


def process_memory_mb() -> float | None:
    """⚠️ **回 `None` 不是 `0.0`**:`None` 的意思是「量不到、當守衛不存在」,
    而 `0.0` 會讓 `記憶體用量 0 MB` 這種話出現在記錄檔裡,看起來像真的量到了。"""
    return None


def round_window_corners(child_id: int, width: int, height: int,
                         radius: int) -> None:
    """不知道這個平台怎麼切圓角;方角也能用。"""
    return None


def set_window_icon(window, ico_path, png_path):
    """不知道這個平台該走哪一支 Tk 介面;圖示是純裝飾,安靜地不設。"""
    return None


def strip_default_menubar(window) -> None:
    """不知道這個平台會不會自己掛選單列;什麼都不做(有的話也只是外觀)。"""
    return None


def co_initialize_ex() -> int | None:
    """不知道這個平台有沒有 COM;當成沒有(Windows 以外目前都沒有)。"""
    return None


def preload_onnxruntime() -> None:
    """不知道這個平台要不要預載;當成不必(目前只有 Windows 有那個問題)。"""
    return None


def system_cpu_ticks() -> tuple[float, float] | None:
    """不知道這個平台怎麼問;診斷少一欄,錄音照跑。"""
    return None


def microphone_permission() -> Outcome:
    """不知道這個平台怎麼問;維持原本的行為。"""
    return Outcome("unsupported", "這個平台問不到麥克風授權狀態")


def open_microphone_settings() -> Outcome:
    return Outcome("unsupported", "這個平台沒有要開的麥克風權限頁")
