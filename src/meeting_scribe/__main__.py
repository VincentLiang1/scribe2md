r"""進入點(`pythonw -m meeting_scribe`,由「啟動.bat」呼叫)。

同一支檔案有兩個角色,靠環境變數分辨(見 `console` 模組檔頭):

- **門面**——持有黑視窗那一支。生下本體、轉印它的話、等它把網頁開起來就退場。
- **本體**——`CREATE_NO_WINDOW` 的獨立行程,真正跑整個工具。

**為什麼不沿用 `meeting-scribe` 那個入口**:兩件事必須發生在**任何重量級 import
之前**,而 `[project.scripts]` 產生的入口點會先把整個 `app` 模組拉進來(gradio、
torch 那一串):

1. **分角色**——門面要在幾十毫秒內就把本體生出來,不能等 gradio 載完。
2. **接住啟動失敗**——依賴缺一半、防毒把某個 .pyd 隔離掉,症狀都是 import 當場
   拋例外。⚠️ 那時**紀錄檔還沒接手**,唯一看得到的地方就是門面那個黑視窗,所以
   import 本身必須包在 try 裡,錯誤要印得出來。

⚠️ **這支檔案自己不可以 import 任何重東西**:它的價值全在「早」。
"""
import logging
import sys

from meeting_scribe import console

# ⚠️ **第一件事**:該當門面就當門面,回來的是退出碼——本體那一支拿到的是 None,
# 直接往下跑。沒設環境變數時(開發時直接執行、逃生門)也是 None,行為與從前一樣。
_code = console.run_as_launcher()
if _code is not None:
    raise SystemExit(_code)

# 以下是本體。⚠️ **stdout 一定要先釘成 UTF-8**:它是一條 pipe(通往門面),而過
# pipe 的 stdout 會退回地區設定的編碼(台灣機器是 cp950)——少了這一行,黑視窗上
# 的中文全變成替代字元(同 `stdio` 模組檔頭記的那兩次)。
from meeting_scribe import stdio  # noqa: E402 - 必須在角色分流之後

stdio.force_utf8()

logger = logging.getLogger(__name__)

_TITLE = "AI 文件.MD 轉換器"


def _explain(exc: BaseException) -> str:
    r"""把例外翻成使用者看得懂的一段話(spec §8)。

    ⚠️ **兩種來源要分開**:自家精心寫過的繁中訊息原樣顯示(例如「代理把連自己
    擋掉了」那一段,它連該找誰都講了);第三方的 cryptic 英文則走通用文案,原文
    降級成一行「回報時附上」的技術細節——把 `ImportError: DLL load failed` 端
    到非技術同仁面前,只會讓他覺得自己弄壞了什麼。"""
    try:
        from meeting_scribe.errors import UserFacingError

        if isinstance(exc, UserFacingError):
            return str(exc)
    except Exception:  # noqa: BLE001 - 連 errors 都 import 不進來就走通用文案
        pass
    return (
        f"「{_TITLE}」啟動失敗,沒有辦法開啟網頁。\n"
        f"最常見的原因是安裝沒有完成,或防毒軟體把程式檔案隔離了。\n"
        f"請雙擊「安裝.bat」重新安裝一次。\n"
        f"(技術細節,回報問題時附上即可:{type(exc).__name__}: {exc})"
    )


def _report_fatal(exc: BaseException) -> None:
    r"""啟動失敗時,想盡辦法留下線索。

    ⚠️ **每一段都要能單獨失敗**:會走到這裡的情形本來就是「環境壞掉了」,連
    `filelog` 都可能 import 不進來。收線索的程式碼自己拋例外,只會把原本的錯誤
    蓋掉——而那才是使用者需要我們回答的問題。"""
    path = None
    try:
        from meeting_scribe import filelog

        path = filelog.attach()  # 冪等:app 已經開過就回同一個檔
        logger.exception("啟動失敗")
    except Exception:  # noqa: BLE001 - 記不下來也要繼續把話說完
        pass
    text = _explain(exc)
    where = f"\n詳細訊息記在:{path}" if path else ""
    try:
        # 黑視窗還在的話,門面會把這幾行轉印出來並停下來等按鍵(見 console)
        print(f"\n{text}{where}", flush=True)
        # 只有「網頁已經開起來、視窗已經沒了」才輪到訊息框
        console.message_box(f"{text}{where}", _TITLE, warning=True)
    except Exception:  # noqa: BLE001 - 同上,不得把原本那個錯誤蓋掉
        logger.debug("連把話說出來都失敗了", exc_info=True)


def main() -> int:
    try:
        from meeting_scribe.app import main as app_main

        app_main()
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - 這裡是最後一道說話的機會
        _report_fatal(exc)
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
