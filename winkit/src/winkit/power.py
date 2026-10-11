"""轉檔期間阻止 Windows 睡眠/休眠(螢幕照常省電關閉)。

機制沿用 meeting-scribe power.py:以 SetThreadExecutionState 告訴系統
「持續有工作」。只設 ES_SYSTEM_REQUIRED、不設 ES_DISPLAY_REQUIRED——
使用者 2026-07-26 選定:長時間工作中允許螢幕關閉省電,但不得睡眠/休眠中斷
工作(對應 meeting-scribe「錄音」模式的旗標組合,非其轉檔模式)。

⚠️ **這一支沒有任何下游相依**(不需要 `bind()`):它只跟 Windows 講話。

旗標綁「呼叫執行緒」:必須在整段轉檔期間都活著的執行緒上設與清——
下游那條做事的 worker 執行緒正是;絕不可在只負責回報進度的那條上設(那在網頁時代是
框架的短命 worker,閒置 10 秒就回收、旗標幾秒就蒸發,meeting-scribe 2026-07-22
踩過。載體換了規則不變:旗標要跟做事的那條同生共死)。
非 Windows 或呼叫失敗時安靜略過,絕不影響轉檔本身。

⚠️ **Win32 那一行住在 `plat_win.set_execution_state`**(2026-09-27 搬過去):這裡留
的是政策(哪幾個旗標、何時設何時清、什麼時候該喊)。⚠️ **`_set_execution_state` 這
個名字要留著**:下游的測試樁的就是它。⚠️ macOS 目前沒有擋睡眠(`plat_mac` 回
`unsupported`);要做就接 `caffeinate`,meeting-scribe 那邊有一套。"""
import contextlib
import logging
from collections.abc import Iterator

from winkit import plat

logger = logging.getLogger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _set_execution_state(flags: int) -> bool:
    """設 SetThreadExecutionState 的旗標;成功回傳 True。這個平台沒有這套機制回 False。"""
    return plat.set_execution_state(flags).ok


@contextlib.contextmanager
def keep_awake() -> Iterator[None]:
    """轉檔期間擋系統睡眠/休眠(螢幕不擋);離開(含例外)必定解除。"""
    active = _set_execution_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    # ⚠️ 只在「這個平台有這套機制、這次卻沒擋住」時喊:沒有機制的平台每次都喊,就是
    # 把一句要人處理的警告變成背景雜訊
    if not active and plat.has_execution_state():
        logger.warning("無法設定防止睡眠狀態,轉檔期間系統仍可能睡眠中斷")
    try:
        yield
    finally:
        if active:
            # 只留 ES_CONTINUOUS = 清除先前旗標,交還系統正常省電排程
            _set_execution_state(ES_CONTINUOUS)
