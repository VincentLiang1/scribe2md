r"""兩層行程的紀錄檔:外層當唯一的寫入者,子行程把逐步訊息吐到 stdout 當標記。

(2026-07-31 為 MP4-2-SRT 而生,使用者要求:「以後分析就不用手動貼兩邊」;2026-08-28 搬進共用包。)

**為什麼主檔由外層 launch 寫**——兩條 log 流的存在位置不同,誰寫決定收得到什麼:
  逐步訊息那條只活在引擎行程的記憶體裡(app.run_batch 的 log() 閉包),從來沒
    離開過那個行程
  引擎的「KVcache is full」是 C++ 直接寫 fd 的(見 launch 模組 docstring),只有
    外層看得到;而「沒解完就停 N 次」更是外層自己數出來的,子行程根本不知道
子行程寫檔會漏掉後者,外層寫檔會漏掉前者。解法是外層當**唯一**的寫入者,子行程
把逐步訊息也吐到 stdout、走 UI_LOGGER 這個 logger 名字當標記,外層認出來就只寫
檔、不重複顯示。單一寫入者=單一時序,不必跨行程鎖檔。

**為什麼合成一個檔而不是兩個**:使用者要的正是「不用貼兩邊」,拆兩檔還是得貼兩份。
而且最難查的問題卡在兩條流的交界——`✘ 檔名 失敗:訊息`(逐步訊息)與它的
traceback(引擎那條)是同一件事的兩半,同一個檔裡它們相隔幾毫秒、自然黏在一起。
原本擔心的「交錯會把敘事切碎」不成立:逐步訊息是整支片一次補播的(見
app.run_batch.produce),合併後每支片的區塊仍然連續。

**DEBUG 另存一檔**,而且由子行程自己寫:那些訊息(固定窗每丟一個窗一行)從來沒被
emit 過,不進管線也就不影響主檔大小。主檔要守住「還貼得進對話」這個用途
(343 部約 3000 行 / 400KB),DEBUG 那份會大好幾倍。

寫檔失敗一律靜靜關掉,不重試也不拋:紀錄檔不該有辦法讓轉檔停下來。
"""
import datetime
import functools
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

from winkit import env_var, host


@functools.cache
def ui_logger() -> str:
    """子行程用哪個 logger 名字標記「這行是逐步訊息」。

    ⚠️ **名字綁著下游的套件名**(`<套件>.ui`):兩層行程的分流靠它,而下游的 logging
    設定(`basicConfig` 的 format)組出來的前綴要跟 `ui_mark()` 對得起來——**三邊一
    起改才成立**,漏一邊不是紀錄檔的來源標籤全錯,就是視窗一則事件都收不到。

    ⚠️ **是函式不是常數**:值要等 `bind()` 之後才算得出來。⚠️ 快取是因為外層對
    **每一行**子行程輸出都要比對一次(一批五小時幾萬行)——雖然那個成本本來就在
    微秒級,但這裡沒有理由每次重算。"""
    return f"{host().package_dir.name}.ui"


@functools.cache
def ui_mark() -> str:
    """外層認出「這行是逐步訊息」的前綴。⚠️ 與下游 `LOG_FORMAT` 的格式對齊。"""
    return f"INFO {ui_logger()}: "


def log_dir_env() -> str:
    """目錄覆寫(測試用):不設就寫進專案底下的 logs/。

    ⚠️ 沒有這個開關的話,每跑一次測試就會在**原始碼樹裡**長出紀錄檔。"""
    return env_var("LOG_DIR")


def debug_log_env() -> str:
    """外層把 DEBUG 檔的路徑用這個環境變數告訴子行程(見 `attach_debug`)。"""
    return env_var("DEBUG_LOG")

KEEP_DAYS = 30


def log_dir() -> Path:
    r"""紀錄檔放哪:下游專案底下的 `logs\`(隨專案走、可以整包刪)。

    ⚠️ **位置由 `bind()` 給,不可以用本檔的 `__file__` 推**:搬進共用包之後那條會
    指到 winkit 自己,於是紀錄檔寫進 `winkit\logs`——而那**沒有任何錯誤訊息**,只是
    使用者再也找不到自己的紀錄。"""
    override = os.environ.get(log_dir_env())
    return Path(override) if override else host().repo_root / "logs"


def ensure_log_dir() -> Path | None:
    r"""把 `logs\` 先建出來,回傳它(建不起來就回 None,不拋)。

    ⚠️ **為什麼需要一個獨立的入口**(2026-09-19 加,為了 MP4-2-SRT):那個資料夾
    同時是「紀錄檔還沒落地時,『開啟紀錄檔』那顆鈕退到哪裡」的落點,而那顆鈕**在
    第一批跑起來之前就按得到**。`Writer.__init__` 也會建它,但呼叫端**不一定在開
    App 那一刻就建 Writer**——MP4-2-SRT 2026-09-19 起把建構搬進 `Engine.start()`
    (一批一個檔),於是全新安裝(`logs\` 不進版控)上那顆鈕在第一批之前完全沒
    反應,而那正是延後開檔那條要救的症狀本身。"""
    d = log_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return d


def new_paths(now: datetime.datetime | None = None) -> tuple[Path, Path]:
    """這一趟的 (主檔, DEBUG 檔)。

    一次執行一個檔(使用者 2026-07-31 選定):批次跨午夜不會被切成兩半——他的
    批次都是凌晨跑到早上,一天一檔剛好斷在中間。而「一次執行」正好等於 launch
    子行程的生命週期,不必另外定義邊界。"""
    base = (now or datetime.datetime.now()).strftime("%Y-%m-%d_%H%M%S")
    main = log_dir() / f"{base}.log"
    return main, debug_path_for(main)


def purge_old(keep_days: int = KEEP_DAYS) -> None:
    """清掉太舊的紀錄檔。擺在開 App 時做,與 tmpdir.cleanup_stale()、
    asr.cleanup_stale_cache() 同一個位置(見 app.main)。"""
    cutoff = time.time() - keep_days * 86400
    try:
        old = [p for p in log_dir().glob("*.log") if p.stat().st_mtime < cutoff]
    except OSError:
        return
    for p in old:
        try:
            p.unlink()
        except OSError:
            pass


def code_version() -> str:
    """跑出這份 log 的是哪一版的碼。

    這是檔頭最重要的一行:三週後拿一份 log 出來分析時,沒有它就只能從訊息長相
    反推版本(「這行有序號,所以是 583aa4c 之後」),而那正是先前每次分析都要
    先做一遍的事。

    直接讀 .git 而不是叫 git 指令:開 App 的路徑上不該多開一個行程、機器上不見
    得有 git 可執行檔,而且**不會跟別人的 subprocess 攔截打架**——launch 的測試
    就是全域換掉 subprocess.Popen 來塞假 app 的,第一版用 subprocess.run 問 git
    當場被導去跑那個假 app,整組 launcher 測試一起垮。"""
    # ⚠️ **三個 read_text 都要連 `ValueError` 一起接**(2026-09-19 code review 抓到):
    # `encoding="utf-8"` 讀到非 UTF-8 位元組是 `UnicodeDecodeError`,而它是
    # `ValueError` 的子類、**不是** `OSError`。這一支跑在**開 App 的路徑上**
    # (`header()` 一進去就呼叫它,而 `header()` 是建 Writer 的參數),漏接的症狀
    # 不是「版本號取不到」而是**整支 App 打不開**,還帶著一段 traceback
    git = host().repo_root / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return "(不在 git 工作區)"
    if not head.startswith("ref: "):
        return head[:7] or "(取不到)"       # detached HEAD:HEAD 本身就是 sha
    ref = head[5:].strip()
    try:
        return (git / ref).read_text(encoding="utf-8").strip()[:7]
    except (OSError, ValueError):
        pass
    try:                                    # 鬆散檔不在,查打包過的 ref
        packed = (git / "packed-refs").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return "(取不到)"
    for line in packed.splitlines():
        sha, _, name = line.partition(" ")
        if name.strip() == ref:
            return sha[:7]
    return "(取不到)"


# 兩個檔共用一份時間格式,否則對不起來。DEBUG 那份多帶毫秒(它逐窗一行、密度
# 高,同一秒內好幾筆是常態);要跟主檔對齊時取到秒即可。
_TS_FMT = "%m-%d %H:%M:%S"


def debug_path_for(main: Path) -> Path:
    """主檔 ↔ DEBUG 檔的配對。只有 new_paths 與這裡定義這個關係,兩邊要一致。"""
    return main.with_name(main.stem + ".debug.log")


def main_path_for(debug: Path) -> Path:
    """DEBUG 檔 → 它的主檔(子行程只拿得到 DEBUG 那個路徑,靠這個回推)。"""
    return debug.with_name(debug.name[: -len(".debug.log")] + ".log")


def _pad(tag: str) -> str:
    """來源標籤補到同寬。CJK 一字兩欄,補到 7 欄才對得齊(現行的兩個標籤
    「訊息」「引擎」都是 2 字,留著這個算式是為了日後加第三種);補出來的空白
    同時就是與內容之間的分隔。"""
    return tag + " " * max(1, 7 - 2 * len(tag))


def stamp(now: datetime.datetime | None = None) -> str:
    """帶月日:批次跨午夜是常態,只有時分秒就算不出真實間隔。"""
    return (now or datetime.datetime.now()).strftime(_TS_FMT)


class Writer:
    r"""紀錄檔的寫入端;開不起來或寫壞了就靜靜關掉,不影響轉檔。

    **`header` 有給就是延後開檔**:檔案等到第一次真的有東西要寫才建立,那幾行
    檔頭在那一刻先落地(2026-09-19 加,為了 MP4-2-SRT 的空殼紀錄檔)。

    為什麼需要它:這些 AP 的視窗開著不一定在做事——調個設定、看一眼上次的結果、
    拖錯檔案又關掉,每一次都留下一個**只有檔頭的檔**。MP4-2-SRT 2026-09-19 實測:
    `logs\` 裡 310 份主檔有約 250 份(81%)是這種空殼,而它們**還會害人挑錯檔**
    ——「排序最後的就是最新的」對分析批次結果是最自然的挑法,而最新的那幾份
    往往正是開了沒轉的空殼(那天實際就要往回翻四份才找到有內容的)。

    ⚠️ **不能改成「收工時把沒內容的刪掉」**:使用者是直接關視窗收工的(CTRL_CLOSE
    把整個行程帶走),`finally` 那條路根本不會跑——這與逐行 flush 是同一個前提
    (見 `_put`)。延後開檔不依賴收尾,所以它在那種收場下照樣成立。

    ⚠️ 檔頭的「開始」時間是**呼叫 `header()` 那一刻**(也就是開 App 那一刻),
    不是檔案落地的時刻:拿落地時刻的話,檔名與內容會各說一個時間。⚠️ 但**兩者
    並不是同一次 `now()`**(2026-09-19 code review 量過):檔名來自 `new_paths()`、
    檔頭來自 `header()`,中間隔著呼叫端的 `purge_old()`(實測 390 個檔 glob+stat
    3.4ms、刪 300 個檔 20.7ms,最壞約 24ms),所以開 App 剛好落在整秒邊界前那
    24ms 時,兩者會差一秒。沒有修成「餵同一個 `now` 進兩邊」是因為代價落在呼叫端
    (`header()` 要多收一個參數、三個下游都要改),而症狀只是檔頭比檔名晚一秒。

    ⚠️ **延後的是檔案,不是資料夾**(見 `__init__`):兩者都延後的話,「紀錄檔
    還沒落地」這個狀態在全新安裝上會退化成「連 `logs\` 都不存在」。

    ⚠️ **開場白與收尾行各有自己的入口**(`preamble()` / `footer()`):前者是「先寫
    在這裡、但不算數」,後者是「沒落地就別寫」。少了它們,一支程式的開場提示或
    一句「---- 結束 ----」就會自己把檔案生出來,延後機制在最常走的那條路上等於
    沒有(實例見 `preamble`)。

    不給 `header` 就是原本的行為(當場開檔),`attach_debug` 那條路就是這樣用的。
    ⚠️ **但那個理由是有條件的**(2026-09-19 code review 訂正):「子行程會開檔就
    一定有東西要寫」只有在**呼叫端已經知道有事要做之後才接上**時才成立。接在
    「還沒讀到這一批要做什麼」之前的話,使用者按下開始隨即關視窗那條路上,子行程
    會建出一個只有檔頭的 DEBUG 檔,而它的檔頭寫著「對應主檔:<某檔>」——指著一個
    **從來沒被建立**的主檔(主檔那半已經延後了)。比空殼更糟:那是孤兒。所以
    `attach_debug` 的位置是呼叫端的責任,見它自己的 docstring。"""

    def __init__(self, path: Path, header: list[str] | str | None = None):
        self.path = path
        self._f = None
        # ⚠️ **一把鎖,因為寫入與收工本來就在不同執行緒**(2026-09-19 code review
        # 抓到四條競爭,這是它們共同的根)。下游的形狀是:所有 `write` 都跑在讀
        # 子行程輸出那條 daemon 執行緒,而 `close()` 跑在主執行緒的 finally;
        # `kill()` **不 join** 那條執行緒,它正好因此解開 finally 再寫幾行——
        # 所以那個交錯**不是窄窗,是每一次收工都必然發生**。量過的四種收場:
        #   ① `_emit` 在 `if self._f is None` 之後重讀 `self._f` → `AttributeError`
        #      (`ValueError`/`OSError` 都接不住,會炸穿呼叫端那條執行緒)
        #   ② `close()` 落在 `_open` 的 `path.open()` 裡面 → 檔案在收工後才建出來、
        #      handle 沒有人關得到
        #   ③ `close()` 的兩個 store 之間被切走 → 建出一個**沒有檔頭**的紀錄檔
        #   ④ 兩條執行緒同時進 `_open()` → 兩個 handle、檔頭寫兩遍、第一個洩漏
        # ⚠️ **紀律**:公開入口(`raw`/`write`/`preamble`/`footer`/`close`)自己取鎖,
        # 內部的 `_put`/`_open`/`_emit` 一律**假設已經持有**——內部互相呼叫
        # (`_put`→`_open`→`_emit`)所以不能用可重入以外的方式再取一次。
        # ⚠️ 成本不是問題:未競爭的 `Lock` 是奈秒級,而這裡最密的用法是一批幾萬行。
        self._lock = threading.Lock()
        # ⚠️ **只有 `close()` 設它,只有 `_open()` 讀它**(2026-09-19 第二輪 code
        # review 簡化;原本叫 `_dead`、被寫四次)。那三個多出來的寫入點
        # (建構子失敗、`_open` 開不起來、`_emit` 寫壞了)**永遠觀察不到**:三種
        # 收場都把 `_pending` 留成 None,而 `_put` 只在 `_pending is not None` 時
        # 才走進 `_open`。代價是讀者每次都要重推一次四個寫入點,而且逼出兩段
        # 「它其實不是它看起來那樣」的訂正註解。它真正的語意就是「收工了」。
        self._closed = False
        # 還沒落地的檔頭(None = 已經開過檔、不延後,或建構子就認輸了)
        self._pending = None
        # ⚠️ **資料夾照舊當場建,延後的只有檔案**(2026-09-19 code review 抓到):
        # 那個資料夾是「紀錄檔還沒落地時那顆『開啟紀錄檔』的鈕退到哪裡」的落點
        # (見 MP4-2-SRT 的 `gui._open_log`)。連資料夾一起延後的話,全新安裝
        # (`logs\` 不進版控)上那顆鈕在第一批跑起來之前完全沒反應——正是延後那條
        # 要救的症狀本身。⚠️ **但這裡不是唯一建得出它的地方**(第二輪 code review
        # 訂正):呼叫端不一定在開 App 那一刻就建 Writer,那一格走 `ensure_log_dir()`。
        try:
            # ⚠️ **`str` 當成一行,不要拆開**(2026-09-19 第二輪 code review):
            # `raw()` 收的就是單一 `str`,所以 `Writer(p, "=== 檔頭 ===")` 是最
            # 自然的筆誤,而 `list("…")` 會**靜靜地**把它拆成一個字一行(實測那
            # 一句是 10 行,開頭是 `['=', '=', '=', ' ', '檔']`)。型別註記守不住
            # 它:本包只配了 pytest,沒有 mypy/pyright/ruff。
            if header is not None:
                self._pending = [header] if isinstance(header, str) else list(header)
            path.parent.mkdir(parents=True, exist_ok=True)
            if header is not None:
                # ⚠️ **延後模式要自己問一次「這個資料夾寫得進去嗎」**(2026-09-19
                # 第二輪 code review 抓到):上面那道 `mkdir(exist_ok=True)` 對
                # **已經存在**的資料夾是 no-op,它只答得出「建得起來嗎」。於是
                # 裝在 `%ProgramFiles%`、被 ACL 拒寫、被 Controlled Folder Access
                # 或 OneDrive 鎖住的機器上,`live` 會回 True(相對於當場開檔那條
                # 是**迴歸**),而呼叫端拿它決定要不要把檔名擺上畫面——那個判斷
                # **只做一次、沒有第二次機會改口**,於是狀態列從此掛著一個永遠
                # 不會出現的檔名,而第一行真的要寫時整批紀錄靜靜丟光。
                # ⚠️ 用 `TemporaryFile` 而不是開一下真正的那個檔:後者若那個檔名
                # 已經有人用(附加模式是刻意的)就會誤刪別人的紀錄,而前者在
                # Windows 上帶 `O_TEMPORARY`——連行程被硬殺掉都由 OS 收走。
                # ⚠️ 成本實測 **0.126 ms**(本機 50 次平均),而它一次執行只付一次。
                # ⚠️ **它問的是資料夾、不是那個檔名**:超過 MAX_PATH 的長檔名這裡
                # 看不出來(那一格留給 `_open`,見下面那段不留 0 byte 的處理)。
                tempfile.TemporaryFile(dir=path.parent).close()
        except (OSError, ValueError, TypeError):
            # ⚠️ **三種例外都要接,而且一個都不准漏出去**(2026-09-19 第二輪 code
            # review):`OSError` 是落點建不起來/寫不進去;`ValueError` 是路徑的
            # 目錄段含 NUL(`mkdir` 丟的是 `ValueError: embedded null character`,
            # 不是 `OSError`);`TypeError` 是 `header` 根本不是 iterable
            # (`Writer(p, 0)`)。下游是在**開 App 那一刻、沒有任何 try** 的情況下
            # 建這個物件的,漏一種就是整支 App 帶著 traceback 打不開。
            self._pending = None
            return
        if header is None:
            self._open()

    def _open(self) -> None:
        # 先把待落地的檔頭取下來:開檔失敗時它也不該留著,否則 `live` 會一直說
        # 「還沒開,應該還行」,而每一次寫入都要再試一次開檔
        pending, self._pending = self._pending, None
        # ⚠️ **已經收工就不要再建檔**:收工之後被喚醒的話,Writer 會復活並建出一個
        # **沒有檔頭**的紀錄檔(比空殼更難解讀:沒有版本、沒有開始時間),`live` 跟著
        # 從 False 翻回 True,而那個 handle 沒有人關得到。⚠️ 這道早退**只在持有鎖時
        # 才真的擋得住**(2026-09-19 code review 量到:在它之後的 `path.open()` 是
        # 窗口最寬的一段,close() 落在那裡面時檔案照樣被建出來)——鎖見 `__init__`,
        # 這一行守的是「鎖到手之前 close() 已經跑完」那一種
        if self._closed:
            return
        # 這個檔是我們建出來的嗎——檔頭寫壞時要據此決定收不收拾(見下面)
        try:
            fresh = not self.path.exists()
        except OSError:
            fresh = False
        try:
            # ⚠️ **`errors="replace"` 不是防禦性程式設計,是這條路唯一的出口**
            # (2026-09-19 code review 抓到):Windows 檔名**合法地**含得了落單的
            # surrogate,它經由片名走進 `write()` 就是 `UnicodeEncodeError`——而那
            # 是 `ValueError` 的子類,會被 `_emit` 接住、把整個寫入端**永久**關掉。
            # 延後模式下順序最糟:檔頭剛落地、下一行就死,於是留下的正好是這個模式
            # 要消滅的那種空殼,而且一趟幾小時的批次從那一行起完全沒有紀錄。
            # ⚠️ 用 `replace` 而不是 `ignore`:看到 `?` 才知道這裡原本有個字。
            self._f = self.path.open("a", encoding="utf-8", errors="replace",
                                     newline="\n")
        except OSError:
            return
        for line in pending or ():
            self._emit(line)
        # ⚠️ **檔頭第一行就寫失敗時,不要留下一個 0 byte 的檔**(2026-09-19 第二輪
        # code review 抓到):`open()` 那一刻檔案就落地了,而磁碟寫滿、隨身碟被拔、
        # 配額踩線都會讓下一行炸掉——留下的東西**比這個模式要消滅的空殼更糟**:沒有
        # app 名、沒有版本、沒有開始時間,而且它排序在最後,正好是「排序最後的就是
        # 最新的」那個挑法第一個挑到的,然後在 `logs\` 裡待滿 KEEP_DAYS 天。
        # ⚠️ **只收拾「我們建的」而且「真的是空的」那一種**:附加模式下那個檔可能
        # 本來就有別人的內容,而檔頭寫到一半才壞的那種至少還講得出自己是誰。
        if self._f is None and fresh:
            try:
                if self.path.stat().st_size == 0:
                    self.path.unlink()
            except OSError:
                pass

    @property
    def live(self) -> bool:
        """還寫得出東西嗎。

        ⚠️ **延後模式下「檔案還沒建立」要回 True**:呼叫端拿這個答案去決定要不要
        把紀錄檔的路徑擺到畫面上(MP4-2-SRT 的狀態列與「開啟紀錄檔」那顆鈕),而
        那個判斷在第一支片開跑之前就做了。回 False 的話畫面上那個檔名整個不見,
        而它明明等一下就會有。⚠️ 但**落點建不起來、或建得起來卻寫不進去時仍然要回
        False**:那是建構子那道 mkdir **加上那次寫入探測**的用途(2026-09-19 第二輪
        code review 補上探測——只有 mkdir 的話,對已經存在但不可寫的資料夾會說謊)。
        不然畫面上會掛著一個永遠不會出現的檔名,而那個判斷沒有第二次機會。

        ⚠️ **這裡不必看 `_closed`**:三種放棄的收場(建構子失敗、`_open` 開不起來、
        `_emit` 寫壞了)都把 `_pending` 與 `_f` 一起留成 None,所以這兩個欄位自己就
        答得出來;而 `close()` 兩個都清掉,答案一樣是 False。`_closed` 守的是另一件
        事(見 `_open`)。"""
        return self._f is not None or self._pending is not None

    def raw(self, line: str) -> None:
        """不帶時間戳與標籤的原樣行。

        ⚠️ **延後模式下它會讓檔案當場落地**(2026-09-19 起;原本的說明寫「檔頭用」,
        而檔頭現在走建構子那條)。所以**舊的用法要清掉**:先 `Writer(p, hdr)` 再
        `for line in hdr: raw(line)` 會把檔頭寫兩遍(沒有錯誤訊息),而拿它寫分隔線
        或「session started」這類橫幅會讓延後機制當場失效、又回到空殼。不想讓它
        落地的開場白走 `preamble()`。"""
        self._put(line)

    def preamble(self, line: str) -> None:
        """開場白:算不算「真的有東西要寫」? 不算。

        ⚠️ **這是延後開檔的另一半**(2026-09-19 加,為了 NotebookLM_OCR):那支
        程式的開場白不在檔頭裡——「執行紀錄:<檔名>」與首次轉檔的提示是介面建好
        之後才由顯示漏斗寫進來的,而它們用 `raw()` 就會把檔案生出來,於是**每一次
        開了沒轉的執行照樣留下一個空殼**(實測 307 份主檔裡 253 份、82%,長相正是
        六行檔頭 + 兩行開場白 + 一行收尾)。

        檔案已經落地之後它就只是普通的一行(位置仍然對:那時開場白早就寫過了)。"""
        with self._lock:
            if self._pending is not None:
                self._pending.append(line)
                return
            self._emit(line)

    def footer(self, line: str) -> None:
        """收尾行:檔案**沒落地就不寫**(`preamble` 的對稱物)。

        ⚠️ 少了它,收尾那句「---- 結束 ----」自己就會把檔案生出來——那會讓延後
        開檔在「正常關視窗」這條最常走的路上完全失效。"""
        with self._lock:
            self._emit(line)      # `_f is None` 時 `_emit` 自己就不做事

    def write(self, tag: str, line: str) -> None:
        self._put(f"{stamp()} {_pad(tag)}{line}")

    def _put(self, text: str) -> None:
        with self._lock:
            if self._pending is not None:
                self._open()      # 第一次真的有東西要寫:檔案連檔頭一起落地
            self._emit(text)

    def _emit(self, text: str) -> None:
        # ⚠️ **先綁進區域變數**(2026-09-19 code review 抓到):直接寫
        # `self._f.write(...)` 的話,檢查與使用之間插進一次 `close()` 就是
        # `AttributeError: 'NoneType' object has no attribute 'write'`,而下面那個
        # except **接不住它**(它不是 OSError 也不是 ValueError),呼叫端那條執行緒
        # 會被它炸穿。鎖已經擋掉這個交錯,綁區域變數是第二道:這一支的契約是
        # 「紀錄檔不該有辦法讓轉檔停下來」,那條線上不留只靠鎖的地方
        f = self._f
        if f is None:
            return
        try:
            f.write(text + "\n")
            # 逐行 flush:使用者是直接關視窗收工的(CTRL_CLOSE 把兩個行程一起
            # 帶走),留在緩衝區的會整段蒸發,而那正是出事那一段
            f.flush()
        except (OSError, ValueError):
            self._f = None
            # ⚠️ **順手把 handle 關掉**(2026-09-19 第二輪 code review):不關的話
            # 它要等 GC 才收,而 `_open` 那段「不留 0 byte 的檔」在 Windows 上會
            # 因此 unlink 失敗(不准刪開啟中的檔案)。關不起來就算了,這條路上
            # 不准有任何東西漏出去
            try:
                f.close()
            except (OSError, ValueError):
                pass

    def close(self) -> None:
        with self._lock:
            # 延後模式下一個字都沒寫過:檔案根本沒建立,沒有東西要關(而且檔頭
            # 就此作廢——這正是這個模式要的結果)。⚠️ **同時標成終局**:收工之後
            # 不可以再被一條慢半拍的執行緒喚醒去建檔(理由見 `_open`),而且
            # `close()` 也因此是冪等的。⚠️ 這是 `_closed` **唯一**的寫入點
            self._closed, self._pending = True, None
            if self._f is None:
                return
            try:
                self._f.close()
            except OSError:
                # ⚠️ **收尾這一行是整個寫入路徑上唯一漏出去的失敗**(2026-09-19
                # code review 實測):紀錄檔在網路碟/隨身碟上而它消失了(或磁碟滿)
                # 時,`TextIOWrapper.close()` 會驅動底層的 `os.close(fd)`,**即使
                # 緩衝區是空的照樣丟 OSError**(Errno 9)。它從下游 `main()` 的
                # `finally: out.close()` 逃出去 = 行程離開碼 1,而「啟動.vbs」看到
                # 非 0 就跳紅色訊息框——於是一趟**已經跑完**的批次,收工時跳一個
                # 「沒有正常結束」加一段 traceback。紀錄檔不該有辦法做到這件事
                pass
            finally:
                self._f = None


def header(main: Path, debug: Path) -> list[str]:
    now = datetime.datetime.now()
    return [
        "=" * 72,
        f"{host().app_title} 轉檔紀錄  開始 {now:%Y-%m-%d %H:%M:%S}",
        f"  程式版本:{code_version()}",
        f"  主檔:{main}",
        f"  DEBUG:{debug.name}(固定窗逐窗判定等細節,平常不用看)",
        "  「訊息」= 視窗上那條逐步訊息;「引擎」= 引擎自己的輸出(含 traceback)",
        "=" * 72,
    ]


def debug_header(debug: Path) -> list[str]:
    """DEBUG 檔的檔頭。

    這個檔不是給人讀的,但它必須自己講得出「我是誰、跟哪個主檔是一對、哪一版
    的碼跑的」——不然日後單獨拿到它就無從對起。檔名同 stem 已經配好對了,但
    檔案會被複製、改名、單獨貼出來,寫在內容裡才靠得住。"""
    now = datetime.datetime.now()
    return [
        "=" * 72,
        f"{host().app_title} DEBUG 紀錄  開始 {now:%Y-%m-%d %H:%M:%S}",
        f"  程式版本:{code_version()}",
        f"  對應主檔:{main_path_for(debug).name}",
        "  格式:月-日 時:分:秒.毫秒 等級 logger [執行緒] [片名 @晶片] 訊息",
        "  主檔到秒、這裡到毫秒;要對齊取到秒即可。",
        f"  含 INFO 以上全部(逐步訊息以 {ui_logger()} 出現),所以單獨讀也成立;",
        "  只缺外層才看得到的引擎雜訊統計(見 launch)。",
        "=" * 72,
    ]


def attach_debug(logger: logging.Logger) -> bool:
    """把 DEBUG 級訊息導進另一個檔(路徑由外層經環境變數指定)。

    收的是現在被壓掉的那些:固定窗「這個窗撐不起有對白、丟棄」逐窗一行、翻譯
    「可疑譯文,重試」。查「覆蓋率為什麼只有 12%」最直接,但量大,所以不進主檔。

    **刻意收 INFO 以上而不是只收 DEBUG**:只有 DEBUG 的檔沒有上下文,每一行都要
    回主檔查它屬於哪支片的哪個階段。收全的話這個檔自己就串得起來(片名與晶片由
    logtag 貼在每一行,執行緒名分得出兩條生產端),而要跟主檔合起來看時,兩邊
    共用同一份時間格式(見 _TS_FMT)。

    同時把 root 的 handler 壓到 INFO:logger 的等級一放到 DEBUG,記錄會照樣往上
    傳給 root 那個 StreamHandler(它的等級是 NOTSET,不擋),於是 DEBUG 會灌進
    管線、再經由外層灌進主檔——正好是這個設計要避免的。

    ⚠️ **呼叫的位置是契約的一部分:要等到「確定有事要做」之後**(2026-09-19 code
    review 抓到)。這個檔是**當場開**的(不像主檔那半會延後),所以接得太早,使用者
    按下開始隨即關視窗那條路上就會留下一個只有檔頭的 DEBUG 檔——而主檔那半因為
    延後了、從來沒被建立,於是那個檔頭裡的「對應主檔」指向一個不存在的檔。⚠️ 代價
    寫明:接在後面就收不到這之前那幾行 DEBUG(下游的暫存清理那類),它們照樣走
    stdout 進主檔,只是不進這個檔——拿一個孤兒換它,划得來。"""
    path = os.environ.get(debug_log_env())
    if not path:
        return False
    p = Path(path)
    head = Writer(p)          # 檔頭直接寫,handler 之後以附加模式接上去
    if not head.live:
        return False
    for line in debug_header(p):
        head.raw(line)
    head.close()
    try:
        # ⚠️ **不可以帶 `delay=True`**(2026-09-19 code review 抓到,原本帶著):
        # 那樣建構子只做 `os.fspath` 與 `abspath`、**從不開檔**,所以下面這個
        # `except OSError` 是死碼,而真正的失敗會延到第一次 `emit()`——`emit` 的
        # `self.stream = self._open()` 坐在 `StreamHandler.emit` 的 try/except
        # **外面**,`Handler.handle` 與 `callHandlers` 也都沒有 except,於是**子行程
        # 裡第一個記 DEBUG 的那一行直接丟例外**。而那時 handler 已經加上去、logger
        # 已經放到 DEBUG、root 的 handler 已經全被壓到 INFO——正是下面那個「半套」
        # 最壞收場,再加一個會炸的 logger。不延後就當場拋,這道 except 才名副其實
        # (檔案反正在上面幾行就已經被建出來、寫過檔頭了)
        handler = logging.FileHandler(p, encoding="utf-8")
    except OSError:
        return False
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s [%(threadName)s] "
        "%(message)s", _TS_FMT))
    for existing in logging.getLogger().handlers:
        existing.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    return True
