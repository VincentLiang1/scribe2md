"""平台層的保底實作:不認得的平台(Linux 等)。只由 `winkit.plat._impl()` 選用。

原則:**沒有把握就選「畫面醜一點但建得起來」的那一邊**——名字錯的游標會讓整個
視窗建不起來,而一個普通箭頭只是不好看。視窗整合那一批一律 `unsupported`、單一實例
一律放行。
"""

from pathlib import Path

from winkit.plat import Outcome

NAME = "null"

#: 沒有把握的平台一律用最普通的箭頭
DISABLED_CURSOR = "arrow"

#: 不知道這個平台的彩色 emoji 字型在哪:回 None,呼叫端退回文字圖示
EMOJI_FONT = None
EMOJI_STRIKES: tuple[int, ...] = ()

_WHY = "不認得的平台,這一項沒有實作"


def appdata_base() -> Path:
    """不知道這個平台的慣例:退回 XDG 的 `~/.cache`。"""
    return Path.home() / ".cache"


def enable_dpi_awareness() -> Outcome:
    return Outcome("unsupported", _WHY)


def set_app_user_model_id(app_id: str) -> Outcome:
    return Outcome("unsupported", _WHY)


def window_handle(root) -> int:
    return 0


def use_dark_titlebar(root, hwnd_of) -> Outcome:
    return Outcome("unsupported", _WHY)


def ime_needs_help() -> bool:
    return False


def set_ime_composition_font(hwnd: int, logfont) -> Outcome:
    return Outcome("unsupported", _WHY)


def set_backdrop(root, colour: str, hwnd_of) -> Outcome:
    return Outcome("unsupported", _WHY)


def work_area(root, hwnd_of):
    return None


def taskbar_progress(hwnd: int, done: int, total: int) -> Outcome:
    return Outcome("unsupported", _WHY)


def taskbar_finish(hwnd: int, flag: int) -> Outcome:
    return Outcome("unsupported", _WHY)


def flash_taskbar(hwnd: int) -> Outcome:
    return Outcome("unsupported", _WHY)


def claim_single_instance(name: str):
    """一律放行:寧可開出兩個視窗,也不要讓程式因為它而開不起來。"""
    return True, None


def release_single_instance(lock) -> None:
    return None


def raise_existing_window(class_name: str, title: str) -> bool:
    return False


def has_execution_state() -> bool:
    return False


def set_execution_state(flags: int) -> Outcome:
    return Outcome("unsupported", _WHY)


def system_theme() -> str | None:
    return None


def hidpi_image_factor(root) -> int:
    """1:不知道這個平台怎麼問,照實畫。"""
    return 1


def make_hidpi_image(root, path, width: int, height: int) -> str | None:
    return None


def set_hidpi_image(root, name: str, path, width: int, height: int) -> bool:
    return False


def restore_on_reopen(root) -> Outcome:
    return Outcome("unsupported", "這個平台沒有「再開一次 App」的事件")
