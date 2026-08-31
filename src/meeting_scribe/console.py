r"""黑視窗:出現、講話,然後在網頁真的開起來的那一刻自己關上。

**規格**(使用者 2026-08-30 指定藏視窗、08-31 兩度修正):雙擊之後黑視窗**只出現
一次**,期間照常顯示啟動訊息(網址、下載進度、失敗原因),**等瀏覽器真的把頁面開
起來才消失**——瀏覽器要幾秒才起得來,那幾秒空著反而讓人以為沒反應。

## 走過的兩條死路(都別再試)

**① `ShowWindow(SW_HIDE)` 把視窗藏掉**(2026-08-30 的做法,一天都沒生效過)。
Windows 11 的預設終端機是 Windows Terminal,`GetConsoleWindow()` 拿到的是 ConPTY
的**偽視窗**:藏它不拋例外、事後 `IsWindowVisible` 也回報「不可見」,而畫面上那個
屬於 `WindowsTerminal` **另一個行程**——**每一道自我檢查都通過,只有使用者的眼睛
沒被騙過**。⚠️ 反過來去藏 Windows Terminal 那個視窗更不行:同一個行程裝著使用者
其他分頁。`test_console.py` 有 AST 反向鎖擋著,不准再用 `ShowWindow`。

**② 自己 `FreeConsole()` + 終止那個還掛著的 trampoline**。uv 放在
`.venv\Scripts\` 的 `pythonw.exe` **不是直譯器,是 trampoline**:它另開一支真的
`python.exe` 跑我們、自己留在同一個主控台上等,`GetConsoleProcessList()` 實測是
**2 個行程**。主控台要等最後一個使用者離開才銷毀,所以光自己脫離沒有用。而**終止
它會在一秒內把我們一起帶走**(實測:「父行程已終止,而我還活著」寫得出來,一秒後
那行就再也寫不出來了——uv 用 Job Object 綁著整組)。

## 現在的做法:門面 + 本體

- **門面**(第一支,由「啟動.bat」用 `pythonw` 丟出來):持有黑視窗。它生下本體、
  把本體的輸出**逐行轉印**到視窗上,然後等。
- **本體**(`CREATE_NO_WINDOW` 的獨立行程):真正跑整個工具,自己那個主控台從建立
  的那一刻就沒有視窗。
- **收工要送一個明確的信號**(`dismiss()`,在頁面真的連上來時呼叫):本體把 fd 1/2
  重導到 `os.devnull`,並在同一個動作裡用一份保留的寫端送出 `DISMISS_MARK`;門面
  看到它就退場,trampoline 跟著走,主控台於是真的銷毀、視窗消失。

⚠️ **不可以改用「等 pipe 的 EOF」**(實測過,不會發生):**本體的 trampoline 也
握著同一支 pipe 的寫端**——本體關掉自己那份,門面照樣等不到 EOF,視窗永遠不關,
而紀錄檔裡「輸出通道已收掉」「頁面連上」全都好端端地寫著。
⚠️ **順序是「先導掉、再送信號」**:反過來的話,萬一 `dup2` 失敗,門面已經退場、
pipe 讀端關閉,本體下一次 `print` 就會炸在 broken pipe 上。現在最壞的情況只是
信號送不出去 → 視窗留著,那是安全的失敗方向。

⚠️ **本體先死掉的話,門面要把視窗留著**(印出原因並等按鍵):那是使用者唯一看得到
的線索,而這一段**還沒有紀錄檔**(`filelog` 要程式起來才接手)。於是「環境沒建好」
「代理擋掉本機」「已經有一個在跑」這三種情況,視窗全都自動留著——不必為它們各寫
一條補救,這正是把時機挑在「頁面連上來」的用意。

⚠️ **使用者中途把黑視窗關掉,只會殺掉門面**:本體有自己的主控台,照常跑完。
"""
import io
import logging
import os
import subprocess
import sys

logger = logging.getLogger(__name__)

# 「這個行程是啟動器丟出來的」——只有「啟動.bat」會設。
DETACH_ENV = "MEETING_SCRIBE_DETACH_CONSOLE"
# 逃生門:設了就一切照舊(要盯著黑視窗查問題時用)。⚠️ 比 DETACH_ENV 優先。
KEEP_ENV = "MEETING_SCRIBE_KEEP_CONSOLE"
# 「我是本體,不是門面」——防無限遞迴,由門面自己設給子行程。
CHILD_ENV = "MEETING_SCRIBE_WINDOWLESS_CHILD"

CREATE_NO_WINDOW = 0x08000000

# 本體對門面說「網頁開起來了,你可以走了」。⚠️ 用控制字元包起來:它會流過同一條
# stdout,而那條 stdout 上跑的是要印給使用者看的中文——夾在裡面必須絕對不可能
# 與正常內容相撞,也不可以在門面漏接時顯示成一行怪東西。
DISMISS_MARK = "\x00meeting-scribe:window-can-go\x00\n"

# gradio 的供應快取目錄(由套件根 `__init__.py` 設定,名字帶行程編號)。
# ⚠️ 前綴要與那裡一致,對不上的話門面就清不掉它,而症狀只是紀錄檔對不上人。
SERVE_DIR_ENV = "GRADIO_TEMP_DIR"
_SERVE_DIR_MARK = "meeting-scribe-serve-"

_dismissed = False


def may_detach() -> bool:
    """現在該不該走「門面 + 本體」這一套?(抽成純函式才測得到判斷本身)"""
    if os.environ.get(KEEP_ENV):
        return False
    return bool(os.environ.get(DETACH_ENV))


def is_body() -> bool:
    """我是本體(無視窗那一支)嗎?"""
    return bool(os.environ.get(CHILD_ENV))


def dismissed() -> bool:
    return _dismissed


def _child_env() -> dict:
    env = dict(os.environ)
    env[CHILD_ENV] = "1"
    # ⚠️ **供應快取的目錄名帶著行程編號,而它在「門面」匯入套件時就定好了**
    # (見 `meeting_scribe/__init__.py`):原樣傳下去,本體會用門面的編號當目錄名,
    # 而門面幾秒後就退場了——清掃孤兒靠的是鎖檔所以不會誤刪,但事後看紀錄檔會對
    # 不上人。清掉讓本體自己重設一次;使用者自己設的那種不含我們的前綴,原樣留著。
    if _SERVE_DIR_MARK in env.get(SERVE_DIR_ENV, ""):
        env.pop(SERVE_DIR_ENV, None)
    return env


def run_as_launcher() -> int | None:
    r"""門面的工作:生下本體 → 轉印它的話 → 等它說「網頁開起來了」→ 退場。

    回傳退出碼(呼叫端要照著結束);**不該由這一支扮演門面時回 `None`**,呼叫端
    就自己往下跑(開發時直接執行、逃生門、以及本體自己都走這條)。

    ⚠️ **轉印是必要的不是裝飾**:黑視窗留著卻不說話,跟沒有一樣——網址、首次下載
    模型的進度、失敗原因,全都在本體那一邊。
    """
    if sys.platform != "win32" or not may_detach() or is_body():
        return None
    _greet()
    try:
        proc = subprocess.Popen(
            # ⚠️ **`-u` 不可省**:本體的 stdout 是一條 pipe,而 Python 對 pipe 用的是
            # **8KB 區塊緩衝**——每一行都會卡在它自己的緩衝區裡,門面一行都收不到,
            # 黑視窗於是整片空白站在那裡好幾秒,直到收工那一刻才一次吐出、而視窗同時
            # 就關了(2026-08-31 使用者回報「沒文字」的真正原因)。
            [sys.executable, "-u", "-m", "meeting_scribe", *sys.argv[1:]],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW, env=_child_env(), close_fds=True,
        )
    except Exception:  # noqa: BLE001 - 見下
        # 生不出本體就讓這一支自己跑完:留一個黑視窗,遠比「按了圖示什麼都沒發生」好
        logger.debug("生不出無視窗的本體,改由這一支自己跑", exc_info=True)
        return None
    if _relay(proc):
        return 0  # 收到收工信號 = 網頁開起來了,門面功成身退
    # 沒收到信號就沒話說了 = 本體提早結束(環境壞掉、已經有一個在跑、代理擋住…)
    return _explain_early_exit(proc.wait())


def _greet() -> None:
    r"""視窗一出現就先說話,不要讓使用者對著一片空白等。

    ⚠️ **這句話非得由門面自己印不可**(2026-08-31):`啟動.bat` 的 `echo` 印在
    **cmd 自己那個視窗**上,而 `start` 會另外給 pythonw 一個**新的主控台**——那
    才是使用者看著的視窗,`.bat` 印的東西一個字都不會出現在上面(cmd 隨即退出,
    它那個視窗一閃即逝)。

    ⚠️ **要講「接下來會發生什麼」**:黑視窗停在那裡好幾秒,使用者需要知道那是
    正常的、以及它什麼時候會不見。"""
    print("正在啟動「AI 文件.MD 轉換器」,請稍候……")
    print("瀏覽器待會兒會自動打開工具頁面,這個視窗屆時就會自己關掉。")
    print("(第一次轉檔會先下載 AI 模型,約 2-3 GB、只有那一次,進度會顯示在網頁上。)")
    print()


def _relay(proc) -> bool:
    """把本體說的話逐行印到黑視窗上;回傳**有沒有收到收工信號**。

    ⚠️ **判準是「有沒有收到信號」,不可以改回「本體還活著嗎」**(2026-08-31
    使用者回報「重複開啟時視窗沒有暫停」的根因):`_relay` 讀到管線關閉的那一
    瞬間,`proc.poll()` 未必已經反映出行程結束——那是**時序競爭**,而且它偏向
    錯的那一邊(誤判成「還活著 = 網頁開起來了」→ 門面直接退場 → 使用者連
    「已經在執行中了」都沒看到,視窗一閃就關)。信號是本體**主動送**的,與時序
    無關。⚠️ **這種 bug 用假的行程物件測不出來**:測試裡「還活著沒」是明確設定
    的,而真實情況下那正是會飄的那一格(假引擎測得到接線、測不到時序)。

    ⚠️ **要按 UTF-8 解碼**:過 pipe 的 stdout 會退回地區設定的編碼(台灣機器是
    cp950),而本體開頭就把自己那一端釘成 UTF-8 了(`stdio.force_utf8`)——兩邊
    對不上的話,黑視窗上的中文會變成一片替代字元。"""
    mark = DISMISS_MARK.strip("\n")
    try:
        with io.TextIOWrapper(proc.stdout, encoding="utf-8", errors="replace") as text:
            for line in text:
                if mark in line:
                    # 信號可能與同一行的內容黏在一起,前半段照印
                    head = line.split(mark, 1)[0]
                    if head:
                        print(head, end="", flush=True)
                    return True
                print(line, end="", flush=True)
    except Exception:  # noqa: BLE001 - 轉印失敗不該連帶弄死本體
        logger.debug("轉印本體的輸出時出錯", exc_info=True)
    return False


def _explain_early_exit(code: int) -> int:
    r"""本體還沒把網頁開起來就結束了 → 把視窗留著,讓使用者看得到原因。

    ⚠️ **一定要等按鍵**:不等的話門面立刻退出、視窗跟著關掉,剛剛印出來的原因
    就只是閃了一下——而這一段**還沒有紀錄檔**,那是他唯一的線索。"""
    print()
    if code != 0:
        # ⚠️ 退出碼 0 的情形不印錯誤字樣:「已經有一個在跑」走的正是這條,而那
        # 不是失敗——它前面已經把該說的話說完了,這裡再喊一次「沒有正常啟動」
        # 只會讓人以為又出了別的事。
        print("[錯誤] 工具沒有正常啟動,原因請看上面的訊息。")
        print("       如果看不出來,請把整個視窗的內容複製給維護者。")
        print()
    print("請按 Enter 關閉這個視窗。")
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass
    return code


def dismiss() -> bool:
    r"""網頁真的開起來了 → 收掉輸出通道並通知門面退場,黑視窗跟著關上。

    回傳這一次有沒有真的收(冪等:F5 重連會再觸發一次)。三個動作,順序是規格:

    1. **flush**——緩衝區裡通常正好是最後那句「網頁介面網址…」。
    2. **留一份寫端、把 fd 1/2 導去 `os.devnull`**——之後任何 `print` 都還有地方
       去。⚠️ 動的是**檔案描述符**不是 `sys.stdout` 這個物件:只換物件的話,底下
       那條 pipe 還開著,而且下一次寫入就炸在 broken pipe 上。
    3. **用那份保留的寫端送出信號**——⚠️ **不可以改成「關掉 pipe 讓門面讀到
       EOF」**:本體的 trampoline 也握著同一支 pipe 的寫端(實測),EOF 永遠不會
       發生,視窗就永遠不關。

    ⚠️ **順序不可顛倒**:先送信號再導的話,萬一導不成功,門面已經退場、pipe 讀端
    關閉,本體下一次 `print` 就會炸。現在最壞的情況只是信號送不出去 → 視窗留著。"""
    global _dismissed
    if _dismissed or not is_body():
        return False
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001 - 收不乾淨也要繼續往下收
            logger.debug("收輸出通道前 flush 失敗", exc_info=True)
    try:
        keep = os.dup(1)  # 留一份寫端,等一下用它送信號
    except OSError:
        keep = -1
        logger.debug("留不下寫端,黑視窗大概會留著", exc_info=True)
    try:
        null = os.open(os.devnull, os.O_RDWR)
        os.dup2(null, 1)
        os.dup2(null, 2)
        os.close(null)
    except OSError:
        if keep >= 0:
            os.close(keep)
        logger.debug("收不掉輸出通道,黑視窗會留著", exc_info=True)
        return False
    _dismissed = True
    if keep >= 0:
        try:
            os.write(keep, DISMISS_MARK.encode("utf-8"))
        except OSError:
            logger.debug("送不出收工信號,黑視窗會留著", exc_info=True)
        finally:
            os.close(keep)
    logger.debug("輸出通道已收掉、收工信號已送出,黑視窗會關上")
    return True


MB_OK = 0x0
MB_ICONINFORMATION = 0x40
MB_ICONWARNING = 0x30
MB_SETFOREGROUND = 0x10000
MB_TOPMOST = 0x40000


def message_box(text: str, title: str, warning: bool = False) -> bool:
    r"""跳一個訊息框;回傳有沒有真的跳出來。

    ⚠️ **只在黑視窗已經收掉之後才跳**(`dismissed()`):在那之前,話講在視窗上
    使用者就看得到,而一個會卡住的對話框比一行字討厭得多——開發時更是會擋住整批
    測試(2026-08-31 真的發生過:`GetConsoleWindow()` 在輸出被導進 pipe 時也回 0,
    判準少了這一半,pytest 就彈出一個沒有人會去按的對話框,整批停在 325 秒)。

    所以它現在只服務「網頁已經開起來、視窗已經沒了,之後才出事」的那一種。"""
    import ctypes

    if sys.platform != "win32" or not _dismissed:
        return False
    icon = MB_ICONWARNING if warning else MB_ICONINFORMATION
    try:
        ctypes.windll.user32.MessageBoxW(
            0, text, title, MB_OK | icon | MB_SETFOREGROUND | MB_TOPMOST)
        return True
    except Exception:  # noqa: BLE001 - 跳不出來也只能認了,不得因此讓程式失敗
        logger.debug("訊息框跳不出來", exc_info=True)
        return False
