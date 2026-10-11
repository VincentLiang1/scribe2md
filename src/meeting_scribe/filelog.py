r"""執行記錄落地成檔(2026-08-03 加,使用者要求:「之後進行程式改善分析之用」)。

起因是現場收音的掉幀問題:黑視窗只看得到節流過的累計次數,關掉視窗就什麼
都不剩,而真正該問的「掉了幾秒」「間隔是均勻還是叢發」「是誰在吃 CPU」
從來沒有被記下來過。作法照 MP4-2-SRT 的 filelog(使用者指定),那邊已經
驗證過的幾件事直接沿用:

- **一次執行一個檔**(`logs\2026-08-03_143020-12345.log`):一場錄音正好等於
  一次執行,不必另外定義邊界;跨午夜也不會被切成兩半。⚠️ 尾巴那個是**行程
  編號**,少了它「一次執行一個檔」在同一秒開兩次時就不成立(見 `new_path`)。
- **檔頭記程式版本**:三週後拿一份 log 出來分析,沒有這行就只能從訊息長相
  反推是哪一版跑的,而那正是每次分析都要先做一遍的事。直接讀 `.git` 不叫
  `git` 指令——啟動路徑不該多開一個行程,機器上也不見得有 git。⚠️ **交付版
  沒有 `.git`**,所以另外吃套件版號與 `scripts/package.py` 戳的 `VERSION`(見
  `code_version`);那一群人正是最需要報得出版本的。
- **逐行 flush**:使用者是直接關視窗收工的,留在緩衝區的會整段蒸發,而那
  正好是出事的那一段。
- **寫檔失敗一律靜靜關掉**,不重試也不拋:記錄檔不該有辦法讓錄音停下來。

與 MP4-2-SRT 的差別:那邊是外層 launch 與子行程兩條流要合併,這裡是單一
行程,掛一個 FileHandler 就收得到全部。

**檔案收 DEBUG、黑視窗維持原樣**:收音診斷(每 30 秒一筆的掉幀/裝置停擺/
落後統計)量大而且只有事後分析才有意義,印進黑視窗只會蓋掉使用者真正該看
的訊息。故 `meeting_scribe` logger 降到 DEBUG、同時把既有的主控台 handler
釘在 INFO——**少了後面這一步,DEBUG 會照樣往上傳給 root 的 StreamHandler
灌進黑視窗**,正好是這個設計要避免的。

handler 掛在 **root** 而不是 `meeting_scribe`:自家的 DEBUG 靠自家 logger
的等級放行,而第三方的 WARNING/例外 traceback(gradio、soundcard、驅動層)
也要收——那些正是最難重現的問題留下的唯一線索。第三方的 INFO 仍被 root 的
WARNING 等級擋掉,不會洗版。
"""
import datetime
import logging
import os
import sys
import time
from pathlib import Path

from meeting_scribe import paths

# 目錄覆寫(測試用):不設就寫進專案底下的 logs\。沒有這個開關的話,每跑
# 一次測試就會在原始碼樹裡長出記錄檔。
LOG_DIR_ENV = "MEETING_SCRIBE_LOG_DIR"

KEEP_DAYS = 30

# 檔內時間格式:診斷行密度高(每 30 秒一筆、同一秒內可能好幾筆),帶毫秒
# 才對得起來;要跟黑視窗的訊息對齊時取到秒即可。
_TS_FMT = "%m-%d %H:%M:%S"

_attached: Path | None = None
_handler: logging.Handler | None = None

# 黑視窗上「不是經由 logging 印出來的」那些(gradio 的網址、自家 print、
# 未攔截的 traceback)改由這兩個 logger 代收,名字直接標示來源
_OUT_LOGGER = "meeting_scribe.console.out"
_ERR_LOGGER = "meeting_scribe.console.err"
# 沒有換行的內容累積到這個長度就先落地:進度條之類的東西可能很久才換行,
# 不設上限的話出事那一刻的內容還躺在緩衝區裡
_MAX_PENDING = 4096

_teed: list = []  # 被換掉的原始 (名稱, 串流),detach 用


def log_dir() -> Path:
    override = os.environ.get(LOG_DIR_ENV)
    return Path(override) if override else paths.repo_root() / "logs"


def new_path(now: datetime.datetime | None = None) -> Path:
    r"""這一趟的記錄檔路徑。

    ⚠️ **檔名要帶行程編號,不然「一次執行一個檔」在同一秒開兩次時就不成立**
    (2026-08-29 補,來自 NotebookLM_OCR 那邊已經驗過的形狀):兩個行程在同一秒
    走到這裡就會拿到**同一個路徑**,而 `attach()` 用的是 `"a"` ——兩份記錄疊在
    一個檔裡、檔頭出現兩次,**沒有任何錯誤訊息**,而分析的時候會把甲的訊息算到
    乙頭上(這個檔存在的理由正是事後分析)。
    ⚠️ **同一秒真的到得了**:網頁版沒有「只准開一個」的把關(它自動找空埠,兩份
    本來就跑得起來),而使用者是連點兩下的;原生視窗那邊雖然有把關,但它的政策
    第三格是「搶輸了卻找不到那個視窗就照常開」——所以兩邊都到得了。
    ⚠️ `purge_old()` 不受影響:它掃 `*.log` 看 mtime,不看檔名格式。"""
    base = (now or datetime.datetime.now()).strftime("%Y-%m-%d_%H%M%S")
    return log_dir() / f"{base}-{os.getpid()}.log"


def current_path() -> Path | None:
    r"""這一趟的記錄檔(還沒 `attach()` 或開檔失敗就回 None)。

    介面上那顆「📂 記錄檔…」問的就是它(`desktop.App._open_log`)。⚠️ **不要讓呼叫端
    去讀 `_attached`**:那是模組私有的狀態,而「還沒落地時退到 `log_dir()`」這條退路
    要有一個固定的地方寫著——沒有它,每個呼叫端都得自己記得寫一次。"""
    return _attached


def purge_old(keep_days: int = KEEP_DAYS) -> None:
    """清掉太舊的記錄檔。擺在開 App 時做,與 cleanup_stale_temp 同一個位置。"""
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


def _package_version() -> str:
    """套件版號(pyproject 的 version)。取不到回空字串——不要假裝有。"""
    try:
        from importlib.metadata import version

        return version("meeting-scribe")
    except Exception:
        return ""


def _git_sha() -> str:
    """開發機與 git clone 來的副本:直接讀 `.git`。取不到回空字串。"""
    git = paths.repo_root() / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not head.startswith("ref: "):
        return head[:7]  # detached HEAD:HEAD 本身就是 sha
    ref = head[5:].strip()
    try:
        return (git / ref).read_text(encoding="utf-8").strip()[:7]
    except OSError:
        pass
    try:  # 鬆散檔不在,查打包過的 ref
        packed = (git / "packed-refs").read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in packed.splitlines():
        sha, _, name = line.partition(" ")
        if name.strip() == ref:
            return sha[:7]
    return ""


def _stamped() -> str:
    """交付版:`scripts/package.py` 在打包當下戳進去的 `VERSION`(格式 `<sha> <日期>`)。

    **為什麼光有套件版號不夠**:交付版沒有 `.git`,而兩次 Release 之間本來
    就會重打包給同仁——只靠版號的話那幾份長得一模一樣,回報問題時分不出
    是哪一份。⚠️ **檔案內容必須是純 ASCII**,中文一律由這裡補上、不從那邊
    寫進去(沿革:寫它的原本是 cp950 的「打包.bat」,混中文必成亂碼)。"""
    try:
        raw = (paths.repo_root() / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    parts = raw.split()
    if not parts:
        return ""
    return f"{parts[0]}, 打包於 {parts[1]}" if len(parts) > 1 else parts[0]


def code_version() -> str:
    """跑出這份記錄的是哪一版的碼(取不到就明說,不要假裝有)。

    三種情境都要答得出來,因為讀者是同一個人——收到問題回報的維護者:

    - 開發機 / `git clone` 來的副本(有 `.git`)→ `0.5.0 (32f3b66)`
    - 交付版(zip 解壓,沒有 `.git`,但打包時戳過)→ `0.5.0 (32f3b66, 打包於 2026-08-12)`
    - 兩者皆無 → 至少還有 `0.5.0`

    ⚠️ 舊版只讀 `.git`,於是**交付版一律印「(不在 git 工作區)」**——正好是
    最需要知道版本的那一群人什麼都拿不到。"""
    pkg = _package_version()
    detail = _git_sha() or _stamped()
    if pkg and detail:
        return f"{pkg} ({detail})"
    return pkg or detail or "(取不到)"


def _device_hint() -> str:
    """轉錄會走哪顆晶片。

    ⚠️ **偵測本身不便宜**(`import openvino` ＋ 列裝置,暖的時候約 0.34 秒,結果整個
    行程快取一份,見 `transcribe._intel_gpu_available`),所以原生視窗不在檔頭問它
    (`header` 的 `device`)。收音掉幀這類問題的成因高度綁機器,少了這行事後就對不
    起來,所以其他進入點照舊寫在檔頭。偵測失敗(驅動壞掉)絕不能讓記錄檔擋住啟動,
    故整段兜底。"""
    try:
        from meeting_scribe import transcribe

        return transcribe.predicted_device()
    except Exception:
        return "(偵測失敗)"


# 檔頭那一行不偵測時寫什麼(原生視窗:偵測挪到開窗之後在背景做,結果另起一行)。
DEVICE_LATER = "開窗後在背景偵測,結果見下方「轉錄裝置(偵測)」那一行"
# 命令列(doccli)用的說法。⚠️ **那條路不能沿用 DEVICE_LATER**:它沒有「開窗
# 後補一行」這回事,照抄等於在檔頭寫一句永遠不會兌現的承諾——三週後分析的人
# (或 AI)會往下找那一行,而它不存在,只能重看一遍才確定不是自己漏掉
DEVICE_SKIPPED = "未偵測(命令列不載入 openvino:純文件批次不值得付那 0.34 秒與 106 MB DLL)"


def header(
    path: Path, *, device: bool | str = True, console_level: int = logging.INFO,
) -> list[str]:
    """檔頭。分析一份 log 要先知道「哪一版的碼、在什麼機器上跑的」——
    收音掉幀這類問題的成因高度綁機器,少了這幾行就對不起來。

    `device=False` 不在這裡偵測轉錄裝置(原生視窗用,2026-09-15):偵測要
    `import openvino` 再列裝置,暖的時候 0.34 秒、而且載入約 106 MB 的 DLL,
    寫在檔頭就等於**每次開窗都先等它**。那一行改由視窗在背景偵測完再補
    (`desktop.App._probe_done`),機器資訊一樣留得下來。
    給**字串**則直接當那一行的內容(`DEVICE_SKIPPED`:命令列那條路根本不會
    回頭補,得把「為什麼沒有」講在當場)。"""
    now = datetime.datetime.now()
    if device is True:
        hint = _device_hint()
    elif device is False:
        hint = DEVICE_LATER
    else:
        hint = device
    return [
        "=" * 72,
        f"AI 文件.MD 轉換器 執行記錄  開始 {now:%Y-%m-%d %H:%M:%S}",
        f"  程式版本:{code_version()}",
        f"  記錄檔:{path}",
        f"  Python:{sys.version.split()[0]}  平台:{sys.platform}",
        f"  CPU 核心數:{os.cpu_count()}  轉錄裝置(偵測):{hint}",
        # ⚠️ 門檻要照實寫:讀這個檔的人多半手上另有一份「畫面上看到的」
        # (同事貼來的訊息、AI 接走的 stderr),兩邊對不起來時第一個要問的
        # 就是「是不是那邊本來就看不到」。命令列那條是 WARNING,不是 INFO
        f"  畫面上看得到的是 {logging.getLevelName(console_level)} 以上;"
        "這個檔另收 DEBUG(收音診斷等細節)。",
        "=" * 72,
    ]


def start(
    tee: bool = True, *, device: bool | str = True, stream=None,
    console_level: int = logging.INFO,
) -> Path | None:
    """開一份執行記錄:清舊檔 → 掛檔案 handler →(可選)代收 print。

    **順序有三個約束,寫成程式碼而不是註解**:清舊檔要在開檔之前(否則
    剛建好的這一份會被自己的規則掃到)、tee 要在 attach 之後(handler 還
    不存在就沒有地方收)、路徑要印出來(同事回報問題時得找得到那個檔)。
    先前這三條只寫在 app.main 的註解裡,第二個進入點(scripts/repro_live)
    就只做了一半——沒 tee,於是它 print 的收尾比較數字**不在它自己叫人去
    讀的那個記錄檔裡**,而那正是規定要前後對比的數字。

    tee=False 給「stdout 是機器可讀契約」的進入點(如 doccli:一行一個 md
    絕對路徑),那種通道不能被攔截。⚠️ **那種進入點連這裡的 print 都要改道**
    (`stream=sys.stderr`)——路徑那一行本身就會污染契約,而呼叫端是照著
    stdout 逐行去 Read 的,多一行就是多一個讀不到的檔。`device` 見 `header`。"""
    purge_old()
    path = attach(device=device, console_level=console_level)
    if path is not None:
        if tee:
            tee_console()
        print(f"執行記錄:{path}", file=stream or sys.stdout)
    return path


def attach(
    path: Path | None = None, *, device: bool | str = True,
    console_level: int = logging.INFO,
) -> Path | None:
    """把往後所有 log 同時寫進檔案;回傳實際路徑(失敗回 None)。

    冪等:重複呼叫直接回上次的路徑,不會疊出第二個 handler。`device` 見 `header`。

    `console_level` 是**主控台**往後的門檻(檔案一律收 DEBUG)。黑視窗要看得到
    INFO 的進度,所以預設 INFO;⚠️ **doccli 要傳 WARNING**——那條路的 stderr 是
    「給人看的摘要」而不是診斷流,沿用 INFO 會讓每一趟都多出「啟動」與批次摘要
    兩行,而後者與它自己印的摘要一字不差(2026-09-18 接上記錄檔時實測到)。"""
    global _attached, _handler
    if _attached is not None:
        return _attached
    p = path or new_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(
                header(p, device=device, console_level=console_level),
            ) + "\n")
        handler = logging.FileHandler(p, encoding="utf-8")
    except OSError:
        return None
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s [%(threadName)s] "
        "%(message)s", _TS_FMT,
    ))
    root = logging.getLogger()
    # 黑視窗維持原樣:既有的主控台 handler 釘在 `console_level`,自家的 DEBUG
    # 不外流。⚠️ **這一步是必要的**:下一行把 `meeting_scribe` 降到 DEBUG,
    # 而 handler 多半是 NOTSET(`basicConfig` 的 `level` 設的是 **logger** 的
    # 門檻、不是 handler 的)——不擋的話,原本被 root 等級攔住的每一行都會
    # 突然湧上主控台。⚠️ **只收緊不放寬**(`max`):已經比它嚴的不要被放寬
    for existing in root.handlers:
        existing.setLevel(max(existing.level, console_level))
    root.addHandler(handler)
    logging.getLogger("meeting_scribe").setLevel(logging.DEBUG)
    _attached, _handler = p, handler
    return p


class _LineTee:
    """代理 stdout/stderr:照樣印到黑視窗,同時逐行送進記錄檔。

    **為什麼不直接把 handler 掛上去就好**:黑視窗上有一部分內容根本不經過
    logging(gradio 的「Running on local URL」、自家的提示、未攔截的
    traceback),而使用者要的是「不用再貼黑視窗」(2026-08-03 指定)——
    收不到那些就名不副實。

    三個要點:
    - **逐行轉成 log 記錄,不是直接寫檔**:與 FileHandler 共寫同一個檔會
      在行中交錯、把兩邊的內容都弄糊。走 logging 就只有一個寫入者。
    - **`propagate = False` 且 handler 直接掛在自己身上**:不然這些記錄會
      往上傳給 root 的主控台 handler,黑視窗上每一行都印兩遍。
    - **`\\r` 只留最後一段**:下載模型的進度條是原地重寫,整串收下來會在
      記錄檔裡堆出上萬行,而這個檔要保持「貼得進對話」。

    ⚠️ **stderr 那條記成 WARNING 不是 INFO**(2026-08-15 改):這個檔存在
    的理由是事後分析,而先前**未攔截的例外在檔裡長得跟一般訊息一模一樣**
    ——259 份記錄裡有 35 次 traceback(一晚就 33 次)全掛在 INFO,用等級
    篩選會整批漏掉,只能靠 grep「Traceback」去撈。查過那 1178 行 stderr
    輸出**全部**是 traceback 內容、沒有一行是正常訊息,所以整條提級不會
    製造新的雜訊(否則就是另一種狼來了)。

    寫入端有任何閃失都吞掉:記錄檔不該有辦法讓程式的輸出壞掉。"""

    def __init__(self, stream, logger_name: str, level: int = logging.INFO):
        self._stream = stream
        self._log = logging.getLogger(logger_name)
        self._level = level
        self._buf = ""

    def write(self, text) -> int:
        n = self._stream.write(text)
        try:
            self._absorb(text)
        except Exception:
            pass
        return n

    def _absorb(self, text: str) -> None:
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._emit(line)
        if "\r" in self._buf:  # 進度條原地重寫:只留最後一次的狀態
            self._buf = self._buf.rsplit("\r", 1)[1]
        if len(self._buf) > _MAX_PENDING:
            self._emit(self._buf)
            self._buf = ""

    def _emit(self, line: str) -> None:
        if "\r" in line:
            line = line.rsplit("\r", 1)[1]
        if line.strip():
            self._log.log(self._level, "%s", line.rstrip())

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def tee_console() -> bool:
    """把 stdout/stderr 上的內容也收進記錄檔;回傳是否接上。

    必須在 attach() 之後、且在 logging.basicConfig() 之後呼叫:主控台
    handler 在建構時就抓住了「當時的」sys.stderr 物件,所以它之後仍直接
    寫真正的 stderr、不會繞回這裡被收第二次。"""
    if _handler is None or _teed:
        return False
    for name, logger_name, level in (
        ("stdout", _OUT_LOGGER, logging.INFO),
        ("stderr", _ERR_LOGGER, logging.WARNING),  # 見 _LineTee 的 ⚠️
    ):
        stream = getattr(sys, name)
        log = logging.getLogger(logger_name)
        log.setLevel(logging.INFO)
        log.propagate = False  # 不往上傳,否則黑視窗每行印兩遍
        log.addHandler(_handler)
        _teed.append((name, stream))
        setattr(sys, name, _LineTee(stream, logger_name, level))
    return True


def detach() -> None:
    """卸下自己那個 handler(測試用:logging 是行程級全域狀態,不還原會跨
    測試洩漏)。只動自己掛上去的那一個——照 isinstance(FileHandler) 掃的話
    會連呼叫端自己掛的檔案輸出一起拆掉。"""
    global _attached, _handler
    while _teed:
        name, stream = _teed.pop()
        setattr(sys, name, stream)
    for logger_name in (_OUT_LOGGER, _ERR_LOGGER):
        log = logging.getLogger(logger_name)
        log.handlers.clear()
        log.propagate = True
    if _handler is not None:
        logging.getLogger().removeHandler(_handler)
        _handler.close()
    _attached = _handler = None
