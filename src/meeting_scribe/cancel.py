"""協作式取消:「停止」按鈕設旗標,耗時迴圈在檢查點察覺後拋 Cancelled。

為什麼要協作式:Gradio 的 cancels= 只能取消「排隊中」的工作,執行中的
執行緒無法被強制終止;各引擎在自己的迴圈邊界(塊/窗/段)呼叫 check(),
數秒內即可停下,並沿正常例外路徑收尾——暫存目錄、防休眠鎖都由既有的
context manager 自動釋放,不需要額外清理碼。

旗標為模組層級單例:轉檔事件在 gradio 佇列上同時只跑一批(事件預設
併發上限 1),不會有兩批互搶;每批開始時 reset(),前一批按過的停止
不得波及本批。
"""
import threading


class Cancelled(BaseException):
    """使用者按下停止。繼承 BaseException(比照 asyncio.CancelledError):
    取消是控制流訊號、不是錯誤,不得被引擎降級等 except Exception
    的通用兜底安靜吞掉(否則按停止反而觸發 CUDA→CPU 重跑)。"""


_flag = threading.Event()


def request() -> None:
    """要求停止(停止按鈕呼叫;執行緒安全、可重複呼叫)。"""
    _flag.set()


def reset() -> None:
    """開始新批次前清旗標。"""
    _flag.clear()


def requested() -> bool:
    return _flag.is_set()


def check() -> None:
    """檢查點:已要求停止則拋 Cancelled。"""
    if _flag.is_set():
        raise Cancelled()
