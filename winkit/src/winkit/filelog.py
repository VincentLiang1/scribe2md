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
    git = host().repo_root / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return "(不在 git 工作區)"
    if not head.startswith("ref: "):
        return head[:7] or "(取不到)"       # detached HEAD:HEAD 本身就是 sha
    ref = head[5:].strip()
    try:
        return (git / ref).read_text(encoding="utf-8").strip()[:7]
    except OSError:
        pass
    try:                                    # 鬆散檔不在,查打包過的 ref
        packed = (git / "packed-refs").read_text(encoding="utf-8")
    except OSError:
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
    """紀錄檔的寫入端;開不起來或寫壞了就靜靜關掉,不影響轉檔。"""

    def __init__(self, path: Path):
        self.path = path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._f = path.open("a", encoding="utf-8", newline="\n")
        except OSError:
            self._f = None

    @property
    def live(self) -> bool:
        return self._f is not None

    def raw(self, line: str) -> None:
        """不帶時間戳與標籤的原樣行(檔頭用)。"""
        self._put(line)

    def write(self, tag: str, line: str) -> None:
        self._put(f"{stamp()} {_pad(tag)}{line}")

    def _put(self, text: str) -> None:
        if self._f is None:
            return
        try:
            self._f.write(text + "\n")
            # 逐行 flush:使用者是直接關視窗收工的(CTRL_CLOSE 把兩個行程一起
            # 帶走),留在緩衝區的會整段蒸發,而那正是出事那一段
            self._f.flush()
        except (OSError, ValueError):
            self._f = None

    def close(self) -> None:
        if self._f is not None:
            try:
                self._f.close()
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
    管線、再經由外層灌進主檔——正好是這個設計要避免的。"""
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
        handler = logging.FileHandler(p, encoding="utf-8", delay=True)
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
