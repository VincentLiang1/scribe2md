r"""把黑視窗藏起來(使用者 2026-08-30 指定:「讓使用者看到黑視窗並不好」)。

**為什麼是「程式自己藏」而不是換一支無視窗的啟動器**(當天列了四案由使用者選定):
`啟動.vbs`(姊妹專案那一套)確實一秒都不會出現,但它把黑視窗現在兼的差事一次
全砍掉,得逐項補回來;而在 Python 裡藏,可以**挑時機**——藏在「第一個網頁真的
連上來」那一刻(見 `app._viewer_arrived`),於是這三種情況視窗**自動留著**,
一行補救都不必寫:

- 「安裝.bat」沒跑過 / Python 被防毒隔離 → 走不到 Python,`啟動.bat` 的錯誤
  分流照樣印得出來(**那一段還沒有紀錄檔**,filelog 要 Python 起來才接手,
  藏掉就等於「雙擊沒反應」)。
- 瀏覽器沒有自動開起來(公司電腦把預設瀏覽器鎖住)→ 沒有人連上來,視窗留著
  顯示網址,那是唯一的退路。
- 公司代理把「連自己」擋掉 → gradio 的自我健檢當場拋例外,連 launch 都沒完成。

⚠️ **只藏「為這個工具開的」那個主控台**(`HIDE_ENV`,由「啟動.bat」設):
`GetConsoleWindow()` 拿到的是**整個主控台視窗**——開發時在 Windows Terminal 或
VS Code 裡跑 `uv run meeting-scribe`,藏下去會把那個終端機**連同裡面別的分頁**
一起關進看不見的地方。判準不能用「主控台上掛著幾個行程」猜(手動跑也是 shell
+uv+python 三個,分不開),所以改由啟動器明說。

⚠️ **藏了之後,每一條自己走得掉的退出路徑都要先叫回來**(`show()`):
「啟動.bat」失敗時會 `pause` 等人按鍵,而**隱形視窗上的 pause 是按不到的**
——行程會永遠掛在那裡,使用者連工作管理員裡該殺哪一個都認不出來。硬殺
python.exe 那種我們沒機會執行任何程式碼的情形,由 `HIDDEN_FLAG` 兜底(見下)。
"""
import ctypes
import logging
import os
import sys
import threading

logger = logging.getLogger(__name__)

# 「這個主控台是為工具開的,藏了不會妨礙別人」——只有「啟動.bat」會設。
HIDE_ENV = "MEETING_SCRIBE_HIDE_CONSOLE"
# 逃生門:設了就永遠不藏(要盯著黑視窗查問題時用)。⚠️ 比 HIDE_ENV 優先。
KEEP_ENV = "MEETING_SCRIBE_KEEP_CONSOLE"

SW_HIDE = 0
SW_SHOW = 5

_state = {"hidden": False}
_lock = threading.Lock()


def flag_path():
    r"""「現在有一個藏起來的主控台」記號檔。

    ⚠️ **這個檔是給「啟動.bat」看的,不是給我們自己看的**:python.exe 被外力
    硬殺(工作管理員、防毒)時,我們沒有機會跑 `show()`,而 cmd 會照常走到
    `:failed` 的 `pause` ——那個 pause 在隱形視窗上按不到,於是留下一個看不見
    也關不掉的 cmd。`.bat` 看到這個記號就知道「視窗是藏起來的,印訊息和等按鍵
    都沒有意義」,直接退出。

    ⚠️ **「啟動.bat」每次開頭都會先刪掉它**:視窗剛開時本來就是可見的,殘留的
    記號會讓真正該讓人看到的失敗訊息被跳過(而那正是它要保護的那條路)。"""
    from meeting_scribe import paths

    return paths.appdata_root() / "console-hidden.flag"


def _window() -> int:
    """這個行程的主控台視窗;沒有主控台(pythonw / 被導向)時回 0。"""
    if sys.platform != "win32":
        return 0
    try:
        return int(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:  # noqa: BLE001 - 取不到就當作沒有,絕不影響啟動
        logger.debug("取不到主控台視窗", exc_info=True)
        return 0


def _show_window(hwnd: int, cmd: int) -> bool:
    try:
        ctypes.windll.user32.ShowWindow(hwnd, cmd)
        return True
    except Exception:  # noqa: BLE001 - 顯示與否是外觀,不得讓程式停下來
        logger.debug("ShowWindow 呼叫失敗", exc_info=True)
        return False


def may_hide() -> bool:
    """現在該不該藏?(抽成純函式才測得到判斷本身,不只是接線)"""
    if os.environ.get(KEEP_ENV):
        return False
    return bool(os.environ.get(HIDE_ENV))


def hidden() -> bool:
    return _state["hidden"]


def hide() -> bool:
    """把主控台視窗藏起來;回傳這一次有沒有真的藏到。

    冪等:已經藏著就直接回 False,不重複寫記號檔。"""
    with _lock:
        if _state["hidden"] or not may_hide():
            return False
        hwnd = _window()
        if not hwnd or not _show_window(hwnd, SW_HIDE):
            return False
        _state["hidden"] = True
    # 記號檔失敗不算失敗:視窗已經藏好了,而它只影響「硬殺之後那個 pause」
    try:
        path = flag_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        logger.debug("寫不進隱藏記號檔", exc_info=True)
    logger.debug("主控台視窗已藏起來")
    return True


def show() -> bool:
    """把藏起來的主控台叫回來;回傳這一次有沒有真的叫回來。

    ⚠️ **每一條退出路徑都要先呼叫它**:見模組檔頭最後一段。"""
    with _lock:
        if not _state["hidden"]:
            return False
        hwnd = _window()
        if hwnd:
            _show_window(hwnd, SW_SHOW)
        _state["hidden"] = False
    try:
        flag_path().unlink(missing_ok=True)
    except OSError:
        logger.debug("刪不掉隱藏記號檔", exc_info=True)
    logger.debug("主控台視窗已叫回來")
    return True


MB_OK = 0x0
MB_ICONINFORMATION = 0x40
MB_SETFOREGROUND = 0x10000
MB_TOPMOST = 0x40000


def message_box(text: str, title: str) -> bool:
    r"""跳一個訊息框;回傳有沒有真的跳出來。

    ⚠️ **只在「由啟動器開的」那條路上用**(`may_hide()`):開發時直接跑
    `uv run meeting-scribe`,一個會卡住的對話框比一行字討厭得多。

    存在的理由是**黑視窗不再是使用者的資訊來源之後,唯一還需要當場說給他聽
    的那句話**——工具已經在跑、不會開第二個。那句話原本印在黑視窗裡,而
    `啟動.bat` 在程式回傳 0 之後**立刻**關掉視窗:使用者按了桌面圖示,畫面上
    只會閃一下,他會再按第二次、第三次(2026-08-30 查到,不是這次改出來的)。

    ⚠️ **會擋住直到使用者按確定**,所以只給「接下來就要退出」的路徑用。"""
    if sys.platform != "win32" or not may_hide():
        return False
    try:
        ctypes.windll.user32.MessageBoxW(
            0, text, title,
            MB_OK | MB_ICONINFORMATION | MB_SETFOREGROUND | MB_TOPMOST)
        return True
    except Exception:  # noqa: BLE001 - 跳不出來就退回「印一行字」,不能因此失敗
        logger.debug("訊息框跳不出來", exc_info=True)
        return False


def forget() -> None:
    """收掉記號檔,但**不**把視窗叫回來。

    給「正常結束」那條路用(關掉瀏覽器 → 自己結束):那時退出碼是 0、
    「啟動.bat」不會 `pause`,叫回來只會讓視窗閃現一格再消失。"""
    _state["hidden"] = False
    try:
        flag_path().unlink(missing_ok=True)
    except OSError:
        logger.debug("刪不掉隱藏記號檔", exc_info=True)
