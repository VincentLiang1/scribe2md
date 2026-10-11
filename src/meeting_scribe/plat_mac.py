"""平台抽象層的 macOS 實作。介面與規矩見 `plat.py`。"""

import ctypes
import logging
import os
import re

from meeting_scribe.plat import Outcome

logger = logging.getLogger(__name__)

NAME = "macos"

# 子行程要加多少 nice。
#
# ⚠️ **只准用 `os.nice`,不准用 `taskpolicy -b` 或 background QoS**
# (spec/mac/04 §4.4):後者在 Apple Silicon 上不只是「排程權重低」,它會把
# 工作**綁到效率核心**。M 系列的 E-core 與 P-core 差距很大,結果不是
# Windows 那種「慢兩成」,而是可能慢數倍——而一場兩小時的會議本來就要
# 跑很久。nice 是傳統 Unix 排程權重,概念上才是 BELOW_NORMAL 的對應物。
#
# 10 是 Unix 慣例的「背景工作」。機器閒著時 nice 完全不影響速度(它只決定
# 搶不搶得贏),所以這個值偏高沒有代價。⚠️ 【待校準】真正要量的是
# 「前景被拖慢多少」,對照 ocr.py 那張 Windows 的實測表。
_NICE = 10

#: 幾核以上,純 CPU 也預設用精準模型(`plat.accurate_min_cores`)。
#:
#: ⚠️ **2026-09-25 在 10 核 M5 上實測,8 是推算的**(使用者選定,涵蓋所有
#: Apple Silicon——基本款 M 晶片就是 8 核):同一段 180 秒音訊,排除模型
#: 載入之後 `fast` RTF 0.397、`accurate` RTF 1.265,**精準是快速的 3.19 倍**
#: (Windows 上記的是約 4 倍,同一個量級)。
#: ⚠️ **絕對值要用整場的基準去推,不可以拿片段的 RTF 乘上去**:實測四個
#: 不同位置的 180 秒片段 RTF 在 0.35~0.68 之間跳,而整場是 0.216——片段
#: 一律高估(整場有大量廉價的靜音)。照整場基準推:
#:     fast 轉錄 24.4 分 × 3.19 ＝ accurate 約 78 分
#:     ＋ 講者分析 11.4 分(純 CPU 依序跑)→ 端到端約 89 分
#:     驗收線是音檔長度的 1.5 倍 = 170 分,餘裕約 1.9 倍
#: ⚠️ 【待校準】**8 核機型沒有量過**(記憶體頻寬也只有一半),推算最壞
#: 約 120~135 分、餘裕剩約 1.3 倍。要敲定就在一台 8 核 Mac 上跑同一段。
#:
#: ⚠️ **2026-10-05 起是 `None`(使用者指定 Mac 也預設快速,試用中)**:10/02 整段
#: 30 分鐘實測 accurate 是 fast 的 5.2 倍,還會吞掉整段話(見 `docs/spec/mac/05`
#: §5.6)。⚠️ 當時另一個理由「md 給 LLM 讀,零星錯字它能靠上下文補回來」在
#: 2026-10-08 被推翻(補不回聽錯的人名與數字,見 `docs/spec/08` 的模型表)——有
#: GPU 的機器因此改回精準,Mac 沒有跟著改,靠的是上面那個 5.2 倍。上面的實測留著:
#: **要改回就把這裡改回 `8`**。
ACCURATE_MIN_CORES = None

#: 幾核以上,錄音中就順便做講者分析(`plat.live_diarize_min_cores`)。
#:
#: ⚠️ **比 `ACCURATE_MIN_CORES` 保守,而且刻意不共用**:這一條旁邊就是收音
#: 執行緒,而**錄音不能重來**。
#:
#: ⚠️ **而且在 macOS 上,收音執行緒被餓到的代價比 Windows 更嚴重**
#: (使用者 2026-09-25 訂正我引錯來源時釐清的):2026-08-03 掉 4.6 分鐘音訊
#: 那場是 **Windows/WASAPI**,根因是講者分析把 GIL 佔死、拉取執行緒排不上
#: (`docs/dev/recording.md`)。那次之後它被搬進子行程、GIL 那條路移除了。
#: **但兩個平台的失敗形態不同**——見 `capture_blocksize` 的註解:WASAPI 上
#: 「音訊**沒有不見**,只是我們太晚去拿」(料躺在 OS 的 C 層緩衝),而
#: **CoreAudio 是一支 Python 回呼在填 deque:回呼排不上,音訊就真的丟了**。
#: 所以這邊的爭用風險不是「照抄 Windows 的結論」,是**更不能賭**。
#:
#: 現在仍然存在的爭用:兩個引擎各開 `cpu_worker_count()` 條執行緒
#: (這台 9+9=18 條 / 10 核),而錄音時那個子行程**刻意不降優先權**
#: (降它正是當年餓死收音執行緒的做法之一)。
#: ⚠️ **只設成「實測過的那一台」的核心數,不往下放寬**——2026-09-25 量到爭用
#: 下的真實數字,而餘裕比單獨量的時候薄得多:
#:     fast      單獨 RTF 0.259 → 同時跑講者分析 0.719(拖慢 2.78 倍)
#:     accurate  單獨 RTF 0.924 → 同時跑 3.704(拖慢 4.01 倍)
#: ⚠️ **兩個引擎不是相加,是互相拖垮**——先前把兩個 RTF 相加估成 1.11,
#: 實測是 3.70。⚠️ **而拖垮的一大半是執行緒超額訂閱**(2026-09-26 補量,使用者
#: 問「各 5 條或各 4 條呢」):兩邊都降到 5 條,爭用下的 RTF 變成
#: `fast` 0.605、`accurate` 1.868(各 9 條時是 0.842 / 4.133)。
#: ⚠️ **但「精準」仍然做不到**,而且理由與執行緒無關:`accurate` 單獨跑
#: 就已經吃掉幾乎整台機器(整場換算 RTF 約 0.92),加上講者分析的約 0.10
#: 就 ≥ 1.0,**分配方式不會讓工作總量變少**。
#: 所以 `fast` 的餘裕只剩 **1.2~1.4 倍**(不是 3.4 倍;兩個片段各量一次,
#: 0.719 與 0.842,差的是片段密度——**保守要用 1.2**),在電池的低耗電模式、熱節流
#: 之後、或 8 核機型上都可能超過 1。量法見 `docs/dev/recording.md` 與
#: `scripts/bench_live_threads.py`。
LIVE_DIARIZE_MIN_CORES = 10

#: 錄音中兩個引擎**各**分幾條執行緒(2026-09-26 實測,理由見 `plat.live_cpu_threads`)。
#: ⚠️ **只在講者分析真的會跑的時候才套**:核心數不夠、閘門沒放行的機器上,
#: 限制執行緒只會讓單獨跑的轉錄白白變慢(單獨 fast 0.273 → 0.393)。
LIVE_CPU_THREADS = 5

#: OCR 引擎回收門檻的兩個錨點 `(8GB 機器, 32GB 機器)`,見
#: `plat.ocr_memory_anchors_mb`。⚠️ **不是把 Windows 的 2000 / 4000 抄過來**:
#: 守衛量的是 `process_memory_mb`,而那一支在這個平台回的是 libmalloc 的
#: `size_in_use`——實測它只跟得上 **0.4 倍**的實體用量成長(120 張合成圖、
#: 關掉守衛:堆積 123 → 2,182 MB,同時 rss 252 → 4,957 MB)。⚠️ **不是碎片**:
#: 同時量 `size_allocated` 對照,與 `size_in_use` 始終差不到 20 MB——看不到的
#: 那六成是 **OpenVINO 現編的核心走 mmap**,根本不經過 malloc。
#:
#: 2026-09-27 在 10 核 M5 / 32GB / macOS 26.6 上掃的(每個門檻各一個乾淨行程,
#: `scripts/bench_ocr_memory.py`;⚠️ rss 不會在兩輪之間回到原點,同一個行程
#: 連跑好幾輪量到的峰值是假的):
#:
#:     門檻   回收   OCR ms/張   牆鐘 ms/張   堆積峰值   rss 峰值
#:      關      0       327         328        2182      4957
#:      800    81       334         504        2166      3361
#:     1200    36       365         448        2008      3742
#:     1750     9       330         350        1973      3909
#:     2500     0       338         339        2187      4642
#:     4000     0       343         344        2185      4740   ← 原本的值
#:
#: ⚠️ **頭一條結論是「原本的 4000 在這台機器上一次都不會觸發」**:堆積峰值
#: 只有 2,185,而那時行程已經佔了 4,740 MB。照抄 = 守衛實質關掉。
#: **1750**:0.4 × 4000 的換算值(1,697)附近,而且正好是掃描裡的拐點——
#: 9 次回收、牆鐘只多 7%,rss 峰值就從 4,957 壓到 3,909。再往下買不划算
#: (1200 要多花 37% 才多壓 167 MB,800 要多花 54% 才多壓 548 MB)。
#: **800**:0.4 × 2000 的換算值(857)往保守方向取到掃描裡實際量過的那一點。
#: ⚠️ 【待校準】**8 GB 的 Mac 沒有量過**——這個值是從 32GB 機器上的換算率推出來
#: 的,不是量出來的。⚠️ **要動就往上動、不要往下**:代價不對稱,慢是看得見的
#: (使用者會抱怨「OCR 怎麼變慢」),而 8GB 機器 OOM 的表現是**子行程無聲消失**。
#: ✅ **Windows 那一半 2026-09-27 對照過**:同一批合成圖,Windows 的守衛(工作集)
#: 看得到的成長與這邊的 rss 同一個量級——「0.4」是這個平台量法的差異,換算站得住
#: (`docs/spec/mac/05` §5.6c)。
OCR_MEMORY_ANCHORS_MB = (800, 1750)


def spawn_kwargs(*, low_priority: bool) -> dict:
    """POSIX 沒有 creationflags,也沒有要隱藏的主控台視窗。

    ⚠️ **這裡刻意不回 `preexec_fn`**:Python 自己標註它在多執行緒行程裡
    不安全(fork 之後只能呼叫 async-signal-safe 的東西),而這支程式到處
    是執行緒;而且它會讓 Popen 走不到 `posix_spawn` 那條快路。改成
    「子行程自己降」(`apply_own_low_priority`),效果一樣、風險沒有。"""
    return {}


def worker_argv(*, low_priority: bool) -> list[str]:
    from meeting_scribe import plat

    return [plat.WORKER_LOW_PRIORITY_FLAG] if low_priority else []


def apply_own_low_priority() -> Outcome:
    """`os.nice` 是**加**上去的,而且非 root 只能往高調(調不回來)。

    對子行程而言這不是問題——它做完就結束了。⚠️ **但正因為調不回來,
    這支絕不可以套用在主行程上**:視窗本體降下去就再也升不回來。"""
    try:
        os.nice(_NICE)
    except OSError as e:          # 權限或 RLIMIT_NICE 擋下來
        return Outcome("failed", f"os.nice 被作業系統拒絕:{e}")
    return Outcome("ok")


# --- 外殼整合 -------------------------------------------------------------
#
# 兩支都走 `open`(macOS 內建的命令列開啟器)。⚠️ **不要改用 `os.startfile`**
# ——那支只有 Windows 有;也不要用 AppleScript 叫 Finder,那會觸發「自動化」
# 權限對話框,而使用者按下的只是「開啟資料夾」。

def allow_foreground() -> Outcome:
    return Outcome("unsupported",
                   "macOS 沒有前景權這個概念(open 會自己把 Finder 帶到前面)")


def _run_open(args: list[str], what: str) -> Outcome:
    import subprocess

    try:
        subprocess.Popen(["open", *args])  # noqa: S603,S607 - 固定命令,無 shell
    except OSError as e:
        return Outcome("failed", f"開不了{what}:{e}")
    return Outcome("ok")


def open_folder(path) -> Outcome:
    return _run_open([str(path)], "資料夾")


def reveal(path) -> Outcome:
    """`-R` = reveal:在 Finder 裡把那個項目**選起來**,而不是拿預設程式開它。"""
    return _run_open(["-R", str(path)], "Finder")


#: 停的時候等它收攤多久。⚠️ **一定要 `wait`**:`after` 的收尾排程與切換講者
#: 都會走 `stop_sound`,而那時候多半**早就自己結束了**——不收屍的話一整場命名
#: 下來就是一串殭屍行程掛在視窗底下(命名一場會議按十幾次試聽是常態)
_AFPLAY_STOP_TIMEOUT_SEC = 2.0


def play_sound(path) -> tuple[Outcome, object]:
    """`afplay`(macOS 內建):丟一支子行程去放,權杖就是那支行程。

    ⚠️ **不接播放函式庫也不用 PyObjC 的 `NSSound`**:片段是 wav、只放幾秒,
    而 `afplay` 隨每一版 macOS 出貨、非同步、`terminate()` 就停(2026-09-29
    實測 16k 單聲道 wav:播放中 `poll()` 是 None、`terminate()` 後 `wait()`
    回 -15、自然結束回 0)。
    ⚠️ **輸出兩條都丟掉**:`afplay` 遇到放不了的檔會往 stderr 印英文,而這裡
    的呼叫端已經有自己的繁中文案。⚠️ **但也因此「檔案有問題」這一種在這裡
    看不出來**(它是 exit code 1,而我們不等它)——那是刻意的:等它就等於
    同步播。"""
    import subprocess

    try:
        proc = subprocess.Popen(  # noqa: S603 - 固定命令,無 shell
            ["afplay", str(path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **spawn_kwargs(low_priority=False))
    except Exception as e:  # noqa: BLE001 - 試聽是輔助功能,絕不往上炸
        return Outcome("failed", f"這一段放不出來(afplay 起不來:{type(e).__name__})"), None
    return Outcome("ok"), proc


def stop_sound(token: object) -> None:
    """殺掉那支 `afplay`(權杖是 `None` 就什麼都不做),並**收屍**。

    ⚠️ **已經結束的行程再 `terminate()` 不會拋例外**(2026-09-29 實測),所以
    不必先問 `poll()`——問了反而多一條只在賽局裡才走到的分支。"""
    if token is None:
        return
    try:
        token.terminate()
        token.wait(timeout=_AFPLAY_STOP_TIMEOUT_SEC)
    except Exception:  # noqa: BLE001 - 停不下來也只是多放幾秒,不值得吵
        logger.debug("停止試聽失敗", exc_info=True)


#: 等寬字型。⚠️ **Menlo 不是 SF Mono**:後者要另外安裝(Xcode/開發者下載),
# Tk 問得到的 family 清單裡沒有它;Menlo 隨每一版 macOS 出貨,而且實測
# `measure("i") == measure("W")`(真的等寬)
MONO_FAMILY = "Menlo"

#: 見 `plat.reinstall_hint`。⚠️ 刪 `VERSION` 是讓啟動器重跑安裝的唯一一步(雙擊不會),
#: 而 `setup.zsh` 不碰 `data/`,聲紋與名單不受影響
REINSTALL_HINT = ("請到工具資料夾刪掉「VERSION」這個檔,再雙擊一次 App,它會重新建置執行環境"
                  "(工具資料夾的位置見「❓ 使用說明」的「📂 工具資料夾在哪」)")
#: 見 `plat.heic_workaround`
HEIC_WORKAROUND = "或用「預覽程式」打開,從「檔案 › 輸出…」存成 JPEG 再轉"


# kinfo_proc 裡 `kp_proc.p_starttime` 的位移。⚠️ **這個 0 是實測出來的、不是推的**
# (2026-09-24,macOS 26 / arm64):`extern_proc` 開頭那個 union 的第一個成員正好就是
# `__p_starttime`,而整段 648 bytes 裡**只有這一個位置**解得出「近一天內的 timeval」,
# 時間也與 `ps -o lstart` 逐秒吻合。⚠️ 下面仍然做合理性檢查——Apple 哪天改了佈局,
# 要的是回 None(那一行診斷不見),不是印出一個荒謬的秒數。
_STARTTIME_OFFSET = 0
_CTL_KERN, _KERN_PROC, _KERN_PROC_PID = 1, 14, 1


def seconds_since_process_start() -> float | None:
    """`sysctl(KERN_PROC_PID)` 的 `p_starttime`。

    ⚠️ **不要改用 `ps` 子行程**:這支跑在開窗的關鍵路徑上,為了一行診斷
    去 fork 一支行程是本末倒置。"""
    import ctypes
    import ctypes.util
    import os
    import struct
    import time

    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        mib = (ctypes.c_int * 4)(_CTL_KERN, _KERN_PROC, _KERN_PROC_PID, os.getpid())
        size = ctypes.c_size_t(0)
        if libc.sysctl(mib, 4, None, ctypes.byref(size), None, 0) != 0:
            return None
        buf = (ctypes.c_char * size.value)()
        if libc.sysctl(mib, 4, buf, ctypes.byref(size), None, 0) != 0:
            return None
        sec, usec = struct.unpack_from("<qq", bytes(buf), _STARTTIME_OFFSET)
    except Exception:
        return None
    started = sec + usec / 1e6
    elapsed = time.time() - started
    # 合理性檢查:負數或超過一天都代表解錯了位置,寧可不報
    if not 0 <= elapsed < 86400 or not 0 <= usec < 1_000_000:
        return None
    return elapsed


# --- 暫存目錄的存活鎖 -----------------------------------------------------
#
# ⚠️ **不可以照抄 Windows 的「開著就刪不掉」**:POSIX 上 `unlink` 一個開啟中的
# 檔是**合法而且會成功**的(開著的 fd 照樣讀得到,只是名字從目錄裡消失)。
# 照抄的結果是鎖完全失效、而且一句話都不會說。用勸告鎖(flock)。

def hold_temp_lock(path):
    import fcntl
    from pathlib import Path

    fh = Path(path).open("wb")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise
    return fh


def temp_lock_is_held(path) -> bool:
    """試著搶同一把鎖:搶得到 = 沒人握著(孤兒)。

    ⚠️ **搶到之後要馬上放掉**:這裡只是在問問題,不是要接管那個目錄。
    ⚠️ **鎖檔不存在也算沒人握著**——那是上次硬退出留下的空殼。"""
    import fcntl
    from pathlib import Path

    p = Path(path)
    if not p.exists():
        return False
    try:
        with p.open("ab") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        return True                       # 搶不到 = 還有人握著
    return False


def capture_blocksize(seconds: float, rate: int) -> int | None:
    """⚠️ **回 None,不要猜一個數字**:CoreAudio 的 blocksize 上限是**裝置**
    回報的(`blocksizerange` 與 `maxblocksize` 取小,本機實測 512),寫死
    任何值都可能在別台機器上直接 `TypeError`。交給 soundcard 用裝置預設。"""
    return None


def keep_awake_argv(*, display: bool) -> list[str] | None:
    """`caffeinate`(macOS 內建,不必 PyObjC、不必自己排 C 結構)。

    ⚠️ **刻意不用 IOKit 的 assertion API**(`docs/spec/mac/04` §4.2):那要
    PyObjC 或自排結構,而且硬退出會留下殭屍 assertion——`-w <pid>` 沒有
    這個問題。⚠️ **`-s` 只在接電時有效**,所以不放進來:靠電池時擋不住
    系統睡眠是作業系統的決定,硬要反而讓行為在兩種電源下不一致。"""
    import os

    return ["caffeinate", *(["-d"] if display else []), "-i",
            "-m", "-w", str(os.getpid())]


#: `pmset -g` 裡那一行長這樣:` lowpowermode         1`
_LOW_POWER_RE = re.compile(r"^\s*lowpowermode\s+(\d+)\s*$", re.MULTILINE)
#: 問一次要多久(實測約 15 ms)。⚠️ **只在「開始錄音」那一刻問一次**,
#: 不要放進每秒跑的迴圈
_PMSET_TIMEOUT_SEC = 3.0


def desktop_dir():
    """macOS 的桌面就是 `~/Desktop`。

    ⚠️ **不學 Windows 去問系統**:那邊要問是因為 OneDrive 的資料夾備份會把
    桌面整個重導走,而這裡的消費者(`make_shortcut.py`)在這個平台上**沒有
    對應物**(見 `start_menu_programs_dir`)——多一層問法只是多一個沒人走的
    分支。真的遇到有人把桌面重導了再說。"""
    from pathlib import Path

    return Path.home() / "Desktop"


def start_menu_programs_dir():
    """macOS 沒有「開始功能表」這個東西。

    ⚠️ **這是【不成立】不是【待做】**:對應物是 `/Applications`,而那是「把
    `.app` 放進去」不是「放一個捷徑」——交付路徑完全不同(見
    `docs/spec/mac/06` §6.4)。回 `None`,呼叫端本來就當成「這台機器沒有」。"""
    return None


def low_power_mode() -> bool | None:
    """問 `pmset -g`。⚠️ **問不到一律回 `None`(不是 False)**:
    「沒開」與「問不到」的處置不同,而把問不到當成沒開,等於在一台可能
    追不上的機器上把錄音中的講者分析打開——那條路上就是收音執行緒。"""
    import subprocess

    try:
        out = subprocess.run(
            ["pmset", "-g"], capture_output=True, text=True,
            timeout=_PMSET_TIMEOUT_SEC, **spawn_kwargs(low_priority=False),
        ).stdout
    except Exception:
        logger.debug("問不到低耗電模式", exc_info=True)
        return None
    m = _LOW_POWER_RE.search(out)
    return bool(int(m.group(1))) if m else None


def prevent_auto_sleep(*, display: bool) -> Outcome:
    """這個平台不從行程內下旗標,睡眠由上面那支 `caffeinate` 擋。

    ⚠️ **這是 `unsupported` 不是 `failed`**:防睡眠在這台**是有的**,只是走
    另一條路——混成同一句話會讓看記錄檔的人去找一個不存在的問題。"""
    return Outcome("unsupported", "這個平台的防睡眠走 caffeinate 子行程")


def allow_auto_sleep() -> Outcome:
    return Outcome("unsupported", "這個平台的防睡眠走 caffeinate 子行程")


def keep_running_in_standby(reason: str) -> tuple[Outcome, object]:
    """⚠️ **這是【不成立】不是【待做】,而且是量過的**(2026-09-25,
    `scripts/probe_appnap.py`):macOS 最接近 Modern Standby 凍結的東西是
    App Nap,而它連「閒著、最小化、切到別的 App、四分鐘、醒來節奏放慢到
    5 秒」都沒有挑中這支程式(`darwin_bg` 全程 0、計時器一次都沒被合併);
    同一套探針在 `taskpolicy -b` 底下量得到,所以不是探針沒在量。

    睡眠本身由 `caffeinate` 擋,**闔蓋擋不住是硬限制**(`docs/spec/mac/04`
    §4.7)。⚠️ **換打包方式時要重跑那支探針**:唯一沒被推翻的解釋是「這個
    行程沒有 bundle id」,而 App Nap 的政策正是以它為鍵。"""
    return Outcome("unsupported", "這個平台沒有會凍結行程的待命狀態"), None


def stop_keeping_running_in_standby(token: object) -> None:
    """沒有請求可以撤。⚠️ **而且一個字都不說**——它在每一次轉檔與錄音的
    `finally` 裡都會走一次。"""


def lower_own_priority() -> tuple[Outcome, object]:
    """⚠️ **這台不做**:`os.nice()` 降得下去卻**還原不回來**(非 root 只能把
    nice 值往上加),而這一支的合約是「離開必定還原」——降了回不來的話,
    **下一場錄音的收音執行緒就一直被排擠**,而那正是掉音訊的路。

    而且這裡不需要:檔案轉檔的重活早就在 `transproc` / `diarproc` 子行程裡,
    那兩支起來時自己就 `nice` 過了(見 `apply_own_low_priority`)。"""
    return Outcome("unsupported", "這個平台只降子行程的優先權(nice 還原不回來)"), None


def restore_own_priority(token: object) -> None:
    """沒有降過,所以沒有東西要還原(而且要安靜)。"""


# --- 系統音訊 -------------------------------------------------------------

def system_audio_capability() -> Outcome:
    """三態各自對應一種完全不同的處置(見 `plat.system_audio_capability`)。"""
    from meeting_scribe import coreaudio

    if not coreaudio.available():
        return Outcome("unsupported",
                       "這台 macOS 沒有錄製系統聲音需要的功能(需要 macOS 14.2 以上)")
    if not coreaudio.has_permission():
        return Outcome(
            "failed",
            "還沒有「螢幕與系統音訊錄製」的權限:請到「系統設定 › 隱私權與安全性 › "
            "螢幕與系統音訊錄製」把終端機(或本程式)打開,**然後重新啟動程式**")
    return Outcome("ok")


#: Apple 語音辨識小程式在 bundle 裡的名字(`scripts/make_mac_app.py` 編進 Contents/MacOS/)
APPLE_ASR_NAME = "apple-asr"


def apple_speech_helper() -> tuple[Outcome, object]:
    """從 App 開的:用 bundle 裡那支(跟主執行檔一起簽過,TCC 認的是這個 App)。
    開發時(`uv run`,不在 bundle 裡):把 repo 的原始碼編進 appdata 底下的快取,
    原始碼比較新才重編;沒有 swiftc 就照實說。"""
    import shutil
    import subprocess

    from meeting_scribe import paths

    app = _running_bundle()
    if app is not None:
        exe = app / "Contents" / "MacOS" / APPLE_ASR_NAME
        if exe.is_file():
            return Outcome("ok"), exe
        return Outcome("unsupported", "這一版的 App 沒有附 Apple 語音辨識元件"), None
    src = paths.repo_root() / "packaging" / "mac" / "apple_asr.swift"
    if not src.is_file():
        return Outcome("unsupported", "這一份沒有附 Apple 語音辨識元件"), None
    exe = paths.appdata_root() / "helpers" / APPLE_ASR_NAME
    if exe.is_file() and exe.stat().st_mtime >= src.stat().st_mtime:
        return Outcome("ok"), exe
    swiftc = shutil.which("swiftc")
    if swiftc is None:
        return Outcome("failed", "編譯 Apple 語音辨識元件需要 Xcode 命令列工具(swiftc)"), None
    exe.parent.mkdir(parents=True, exist_ok=True)
    # ⚠️ 部署目標與 App 的 LSMinimumSystemVersion 一致(見原始碼檔頭)
    done = subprocess.run([swiftc, *APPLE_ASR_SWIFTC_FLAGS, "-o", str(exe), str(src)],
                          capture_output=True, text=True)
    if done.returncode != 0 or not exe.is_file():
        return Outcome("failed", f"Apple 語音辨識元件編譯失敗:{done.stderr.strip()[:200]}"), None
    return Outcome("ok"), exe


#: 開發時與 `make_mac_app` 共用的編譯旗標(兩邊編出來的要是同一種東西)
APPLE_ASR_SWIFTC_FLAGS = ("-O", "-parse-as-library", "-target", "arm64-apple-macos13.0")


def open_system_audio():
    """建一個全系統的 tap 並包成聚合裝置(見 `coreaudio.open_source`)。"""
    from meeting_scribe import coreaudio

    return coreaudio.open_source()


# --- 自動更新 -------------------------------------------------------------
#
# Mac 的「換版」只做一件事:**用新的 `.app` 換掉正在跑的那一個、再打開它**。之後的事
# (把 bundle 裡那份程式複製到落地根目錄、`uv sync`、寫版本戳記)本來就是 App 啟動器
# 發現版本戳記對不上時會做的(`make_mac_app.py` 的 setup.zsh,手動更新走的也是它),
# 所以不另寫一套——兩套就會有一套在某天悄悄壞掉。
# ⚠️ **`data/` 不會被碰到**:它在落地根目錄底下,setup.zsh 的 rsync 排除了它;新的
# bundle 裡也不准有(`update.verify_mac_zip` 擋)。

#: 不是從 App 開的(開發時 `uv run`)。⚠️ 這是 `unsupported`:那條路本來就不該自動換版
_NOT_FROM_APP = "這一份不是從 App 開的(開發用的執行方式),不能在程式裡直接換版"
#: App 沒有正式簽章(自己打的 ad-hoc 包):新版的簽章無從比對
_UNSIGNED = "這一份 App 沒有正式簽章,沒辦法確認新版是同一個發行者,不能在程式裡直接換版"
#: ⚠️ App Translocation:從「下載項目」直接打開、還沒搬進「應用程式」的 App,macOS 會把它
#: 放在一個**唯讀**的隨機位置執行。換那個位置等於什麼都沒換(下次開的還是原本那一個)
_TRANSLOCATED = ("這個 App 是直接從下載的位置打開的,macOS 讓它在唯讀的暫存位置執行。"
                 "請先把它拖進「應用程式」資料夾、從那裡重新打開,就能自動更新")

_MAC_RESTART = ("更新時會先關閉本程式、換上新版;重新開啟時會先跳出一個終端機視窗,"
                "跑約一分鐘建置新版的執行環境,跑完會自動開回本程式(那個視窗之後可以直接關掉)。"
                "你的聲紋、名單與用詞替換表都會保留。")
_MAC_MANUAL = ("要手動更新:到下載頁下載 scribe2md-mac.zip,解壓縮出來的 App 取代「應用程式」"
               "裡原本那一個,再雙擊它即可,你的聲紋與名單不會被蓋掉。")


def update_asset() -> str | None:
    from meeting_scribe import update

    return update.MAC_ASSET


def update_restart_note() -> str | None:
    return _MAC_RESTART


def update_manual_note() -> str | None:
    return _MAC_MANUAL


def _running_bundle(exe: str | None = None):
    r"""正在跑的這一份屬於哪個 `.app`;不是從 App 開的回 None。

    App 啟動器用的是 bundle 裡那顆 python(`…/X.app/Contents/Resources/python/bin/…`,
    見 `make_mac_app.py`),所以往上找第一個 `.app` 就是它。⚠️ **不可以拿工具資料夾
    (`paths.repo_root()`)推**:那是落地根目錄底下的副本,換它等於什麼都沒換。"""
    import sys
    from pathlib import Path

    path = Path(exe or sys.executable)
    for parent in path.parents:
        if parent.suffix == ".app" and (parent / "Contents" / "Info.plist").is_file():
            return parent
    return None


def _team_id(app) -> str | None:
    """`codesign -dv` 讀出的 TeamIdentifier;沒簽、ad-hoc 或讀不到都回 None。"""
    import subprocess

    try:
        r = subprocess.run(["codesign", "-dv", str(app)],  # noqa: S603,S607 - 固定命令
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"^TeamIdentifier=(\S+)", r.stderr, re.M)
    team = m.group(1) if m else None
    return None if team in (None, "not", "not set") else team


def self_update_capability() -> Outcome:
    r"""能不能直接換掉正在跑的這個 App。順序就是三個問題:從 App 開的嗎、有正式簽章嗎、
    換得掉嗎(位置可寫、與下載的地方在同一顆磁碟)。"""
    from meeting_scribe import update

    app = _running_bundle()
    if app is None:
        return Outcome("unsupported", _NOT_FROM_APP)
    # ⚠️ 比對路徑元件、不比對字串:`str()` 的分隔符跟著執行平台走,測試在 Windows 上跑時
    # 是反斜線,寫成 `"/AppTranslocation/" in str(app)` 就永遠比不到
    if "AppTranslocation" in app.parts:
        return Outcome("failed", _TRANSLOCATED)
    if _team_id(app) is None:
        return Outcome("unsupported", _UNSIGNED)
    parent = app.parent
    # ⚠️ 兩個都要能寫:改名是改 parent 的目錄項,而把 bundle 搬到別的資料夾還要改它自己的 `..`
    if not (os.access(parent, os.W_OK) and os.access(app, os.W_OK)):
        return Outcome("failed", f"「{parent}」沒有寫入權限(這台電腦的帳號可能不是管理員),"
                                 "沒辦法換掉原本的 App")
    work = update.work_dir()
    probe = work if work.exists() else work.parent
    try:
        same_disk = os.stat(probe).st_dev == os.stat(parent).st_dev
    except OSError:
        same_disk = False
    if not same_disk:
        # 跨磁碟的 `mv` 是複製再刪除,不是一步完成——換到一半就是半個 App
        return Outcome("failed", f"這個 App 放在另一顆磁碟上({parent}),"
                                 "請把它搬進這台 Mac 的「應用程式」資料夾再更新")
    return Outcome("ok")


def stage_update(zip_path, version: str, into):
    r"""驗包 → `ditto` 解壓 → 驗簽章,回傳解出來的新版 `.app`。

    ⚠️ **一定要用 `ditto -x -k`,不可以用 `zipfile`**:bundle 裡的 python 有符號連結
    (v1.1.0 的包裡有 9 個),`zipfile` 會把它們解成普通檔、也不留執行權限,簽章當場
    失效——同 `make_mac_app._zip` 打包時那一條,只是方向相反。
    ⚠️ **簽章要過、發行者要與正在跑的這一份相同**(Team ID):這是 Mac 版取代 Windows
    「只能比 SHA-256」的那一道,保證換上去的是同一個人簽的 App。"""
    import shutil
    import subprocess
    from pathlib import Path

    from meeting_scribe import update

    zip_path, into = Path(zip_path), Path(into)
    update.verify_mac_zip(zip_path, version)
    if into.exists():
        shutil.rmtree(into)
    into.mkdir(parents=True)
    r = subprocess.run(["ditto", "-x", "-k", str(zip_path), str(into)],  # noqa: S603,S607
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise ValueError(f"解壓縮失敗({r.stderr.strip() or r.returncode})")
    app = into / update.MAC_APP
    if not app.is_dir():
        raise ValueError(f"解壓縮之後找不到 {update.MAC_APP}")
    r = subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)],  # noqa: S603,S607
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise ValueError(f"新版的簽章驗不過({r.stderr.strip() or r.returncode})")
    mine = _running_bundle()
    want = _team_id(mine) if mine is not None else None
    got = _team_id(app)
    if want is None or got != want:
        raise ValueError(f"新版的發行者({got or '沒有簽章'})與目前這一份({want or '不明'})不同")
    return app


#: 換版小程式(macOS)。⚠️ **是 zsh 不是 Python**:使用者的 Mac 不一定有系統 Python
#: (`/usr/bin/python3` 可能只是叫你裝開發工具的空殼),而 bundle 裡那顆正是要被換掉的。
#: ⚠️ **寫成字串、執行時才落地**:交付白名單不必多管一個檔(`package.py` 那一類漏帶
#: 的災情見 CLAUDE.md)。參數:新版 App、要換掉的 App、備份位置、記錄檔、App 名稱、要等的 pid…
_HELPER_ZSH = r"""#!/bin/zsh
set -u
NEW="$1"; TARGET="$2"; BACKUP="$3"; LOG="$4"; TITLE="$5"; shift 5

log() { print -r -- "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG" }
alert() {
    log "訊息框:$1"
    osascript - "$TITLE" "$1" >/dev/null 2>&1 <<'OSA'
on run argv
    display alert (item 1 of argv) message (item 2 of argv) as critical
end run
OSA
}

log "換版開始:$NEW → $TARGET"
# 等本程式與 App 啟動器都結束(啟動器要等 python 結束才會走,`open` 一個還活著的
# App 只會把它叫到前景、不會開新的)
for pid in "$@"; do
    n=0
    while kill -0 "$pid" 2>/dev/null; do
        sleep 0.2
        n=$((n + 1))
        if (( n > 600 )); then
            alert "舊版沒有在兩分鐘內關閉,這次沒有更新,原本的版本都還在。請重新打開工具再試一次。"
            exit 1
        fi
    done
done
log "本程式已結束"

if [[ -e "$BACKUP" ]]; then rm -rf "$BACKUP"; fi
mkdir -p "${BACKUP:h}"
if ! mv "$TARGET" "$BACKUP" 2>>"$LOG"; then
    alert "沒辦法換掉原本的 App(可能沒有權限),這次沒有更新,原本的版本都還在。可以到下載頁手動更新。"
    open "$TARGET"
    exit 1
fi
if [[ -e "$TARGET" ]] || ! mv "$NEW" "$TARGET" 2>>"$LOG"; then
    log "放上新版失敗,還原"
    if [[ ! -e "$TARGET" ]] && mv "$BACKUP" "$TARGET" 2>>"$LOG"; then
        alert "新版放不上去,已還原成原本的版本。可以到下載頁手動更新。"
        open "$TARGET"
    else
        alert "新版放不上去,原本的 App 也搬不回去,它現在在:$BACKUP 請把它拖回「應用程式」資料夾,或到下載頁重新下載。"
    fi
    exit 1
fi
log "已換上新版,重新開啟"
if ! open "$TARGET" 2>>"$LOG"; then
    log "新版打不開,還原"
    rm -rf "$TARGET"
    if mv "$BACKUP" "$TARGET" 2>>"$LOG"; then
        open "$TARGET"
        alert "新版打不開,已還原成原本的版本。可以到下載頁手動更新。"
    else
        alert "新版打不開,原本的 App 也搬不回去,它現在在:$BACKUP 請把它拖回「應用程式」資料夾,或到下載頁重新下載。"
    fi
    exit 1
fi
log "完成(舊版留在 $BACKUP,下一次更新時清掉)"
"""


def apply_update(staged, target, version: str) -> Outcome:
    r"""把換版小程式寫進工作目錄、脫離本行程叫起來,它等本程式結束才換 App。

    ⚠️ **要等兩個行程**:本程式(python)與它的父行程 App 啟動器——啟動器是編譯過的 C,
    `waitpid` 等到 python 結束才走;它還活著時 `open` 那個 App 只會叫它到前景、不會開新的。
    ⚠️ **`start_new_session`**:自成一個 session,本程式關掉時不會被一起帶走。
    ⚠️ 舊版**搬到工作目錄當備份**而不是刪掉:換到一半出錯才有東西可以搬回去;它在
    下一次更新的 `update.prepare` 開頭被清掉。"""
    import datetime
    import subprocess
    from pathlib import Path

    from meeting_scribe import brand, filelog

    app = _running_bundle()
    if app is None:
        return Outcome("unsupported", _NOT_FROM_APP)
    staged = Path(staged)
    work = staged.parent.parent
    helper = work / "update_helper_mac.zsh"
    backup = work / "backup" / app.name
    logs = filelog.log_dir()
    try:
        logs.mkdir(parents=True, exist_ok=True)
        helper.write_text(_HELPER_ZSH, encoding="utf-8")
        helper.chmod(0o755)
    except OSError as e:
        return Outcome("failed", f"換版小程式寫不進工作目錄({e})")
    log = logs / f"update_{datetime.datetime.now():%Y-%m-%d_%H%M%S}.log"
    pids = [str(os.getpid())]
    if os.getppid() > 1:
        pids.append(str(os.getppid()))
    argv = ["/bin/zsh", str(helper), str(staged), str(app), str(backup), str(log),
            brand.APP_TITLE, *pids]
    try:
        subprocess.Popen(argv, cwd=str(work), close_fds=True,  # noqa: S603 - 固定命令
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        return Outcome("failed", f"換版小程式起不來({e})")
    return Outcome("ok")


# --- 機器與本行程的記憶體 --------------------------------------------------
#
# 兩支都走 libSystem 的 C API,**不 fork `sysctl` / `ps` 子行程**:其中一支
# (`total_ram_mb`)是在 OCR 子行程的模組層被呼叫的,那裡不該再長出一個行程。

#: `host_statistics` 的 flavor:全機累計 CPU tick(`host_cpu_load_info`)
_HOST_CPU_LOAD_INFO = 3
#: `cpu_ticks[]` 有幾格(CPU_STATE_MAX),也就是 count 要填的值
_CPU_STATE_MAX = 4
_CPU_STATE_USER, _CPU_STATE_SYSTEM, _CPU_STATE_IDLE, _CPU_STATE_NICE = 0, 1, 2, 3
#: `mach_port_deallocate` 要的權利種類(MACH_PORT_RIGHT_SEND)
_MACH_PORT_RIGHT_SEND = 0


class _CpuLoadInfo(ctypes.Structure):
    """`host_statistics(HOST_CPU_LOAD_INFO)` 的輸出。⚠️ **在模組層定義一次**
    (同 `plat_win._FILETIME`):這是錄音診斷每 30 秒走一次的路。

    ⚠️ **`natural_t` 是 32 位元、會繞回去**:這台機器整機約 1,000 tick/秒,
    也就是約 49 天繞一圈。繞的那一窗 delta 會是負的,而呼叫端本來就有
    `d_all > 0` 那道守門會把它丟掉——不必在這裡多做事。"""

    _fields_ = [("cpu_ticks", ctypes.c_uint * _CPU_STATE_MAX)]


class _MallocStatistics(ctypes.Structure):
    """`<malloc/malloc.h>` 的 `malloc_statistics_t`。⚠️ 欄位順序不可調。

    ⚠️ **`size_in_use` 與 `size_allocated` 是兩件事**:前者是「現在真的有人在用
    的位元組」,後者含 libmalloc 向核心要來、還沒還回去的整塊。守衛要的是前者。"""

    _fields_ = [
        ("blocks_in_use", ctypes.c_uint),
        ("size_in_use", ctypes.c_size_t),
        ("max_size_in_use", ctypes.c_size_t),      # 歷史最高,刻意不用
        ("size_allocated", ctypes.c_size_t),
    ]


def _libsystem():
    return ctypes.CDLL("/usr/lib/libSystem.B.dylib")


def total_ram_mb() -> int | None:
    """`hw.memsize`。⚠️ **走 `sysctlbyname` 不 fork `sysctl`**:這一支會在 OCR
    子行程的模組層被呼叫到。Apple Silicon 上回的是**統一記憶體的總量**,而
    呼叫端要的正是「這台機器有多大」,不必扣掉給 GPU 的部分。"""

    try:
        val = ctypes.c_uint64(0)
        size = ctypes.c_size_t(ctypes.sizeof(val))
        if _libsystem().sysctlbyname(b"hw.memsize", ctypes.byref(val),
                                     ctypes.byref(size), None, 0) != 0:
            return None
        return int(val.value / (1024 * 1024))
    except Exception:  # noqa: BLE001 - 量不到就讓呼叫端用保守預設
        logger.debug("讀取實體記憶體失敗", exc_info=True)
        return None


def process_memory_mb() -> float | None:
    """`malloc_zone_statistics(NULL, …)` 的 `size_in_use`(所有 zone 的總和)。

    ⚠️ **問 libmalloc 而不是問核心,是 2026-09-26 實測後改的**:唯一的消費者是
    OCR 的引擎回收守衛,而它的前提是**重建引擎之後數字會降下來**——在 macOS 上
    只有這個數字做得到。原本讀 `task_info` 的 `resident_size`,而那一版讓守衛
    從第一次觸發之後**每一張圖都重建**(症狀只是「怎麼變這麼慢」)。

    真的建 OpenVINO 的 OCR 引擎、跑四張不同尺寸的圖、丟掉引擎,連做三輪
    (macOS 26.6 / 25G72,32 GB;`scripts/` 外的一次性量測):

    | 丟掉引擎之後 | rss | phys_footprint | size_in_use |
    |---|---|---|---|
    | 第 1 輪 | 781.3 | 638.0 | **23.5** |
    | 第 2 輪 | 1200.8 | 1057.4 | **23.5** |
    | 第 3 輪 | 1268.9 | 1125.4 | **23.5** |

    前兩個**只升不降**(第一輪收回 408.7 MB,第二、三輪各只收回 1.0),
    `size_in_use` 每一輪都回到同一個數字。⚠️ **`phys_footprint` 一起否決掉了**
    ——那是 Activity Monitor 的「記憶體」欄,很容易以為它比 rss 精確。

    ⚠️ **成因是 libmalloc 留著頁面不還給核心,而不是「量錯了」**:同一台機器上
    `bytearray(300 MB)` 放掉之後 rss 一毫不動(10 MB~1.2 GB、單塊與多塊、
    `bytearray` 與裸 `malloc/free`、Python 3.12 與系統 3.9 全都如此;
    `malloc_zone_pressure_relief` 回報釋放 0,`reusable` 也沒動)。而**換成
    `mmap` 就會降**(331.8 → 17.3,三輪一致)——所以 rss 本身是對的,是
    「malloc 放掉的東西不算釋放」。

    ⚠️ **刻意不用 `resource.getrusage(...).ru_maxrss`**(它在 macOS 上是位元組、
    很容易誤以為可以直接用)也不用 `max_size_in_use`:兩者都是**歷史最高**,
    病症與上面那條一樣。

    ⚠️ **這一支比原本那版貴得多,而且成本跟著「存活區塊數」走**(2026-09-26 實測,
    `task_info` 那版是 O(1) 的核心查詢,這版要走一遍堆積):4 千塊時 17 µs、
    2 萬塊 28、12 萬塊 81、32 萬塊 193(舊版一律 0.75)。⚠️ **與位元組數無關**
    ——1.8 GB 裝在 60 個大塊裡量到的仍是 194 µs。**仍然不值得改**:唯一的呼叫端
    是 OCR 每張圖問一次,而一張圖是數百毫秒(`ocr_worker._memory_limit_mb` 記的
    是 670~714 ms),最壞的 0.19 ms 佔 0.03%。⚠️ 但**哪天想把這個問法搬到更內層
    的迴圈**(每一段、每一個 box),這張表就是判準——那時要改的是「隔幾次才問
    一次」,不是快取 ctypes(實測快取只省 4 µs,80% 的成本在 C 呼叫本身)。
    ⚠️ **這個數字系統性地小於本行程的實體用量,而且那個差會隨著 OCR 跑而長大**
    (2026-09-27 掃了 120 張合成圖才看清楚):引擎剛建好時差 130 MB,跑完差
    2,800 MB——**它只跟得上 0.4 倍的成長**,其餘的是 OpenVINO 現編的核心,走
    mmap 不經 malloc(⚠️ **不是碎片**:`size_allocated` 與 `size_in_use` 始終
    差不到 20 MB)。⚠️ **所以 `ocr_worker` 的門檻不可以照抄 Windows 那一組**:
    2000 / 4000 是拿那邊的工作集校準的,搬過來就是「一次都不會觸發」——
    這個平台自己的錨點在 `OCR_MEMORY_ANCHORS_MB`,量法見
    `scripts/bench_ocr_memory.py`。"""

    try:
        libc = _libsystem()
        libc.malloc_zone_statistics.argtypes = [ctypes.c_void_p,
                                                ctypes.POINTER(_MallocStatistics)]
        libc.malloc_zone_statistics.restype = None
        stats = _MallocStatistics()
        # ⚠️ 第一個參數給 NULL 是「所有 zone 的總和」,給 `malloc_default_zone()`
        # 只有預設那一個。實測載入 OCR 引擎前後兩者都一樣(5.9 / 370.8,差 0.0
        # ——OpenVINO 沒有自己開 zone),但要的是總和,不該賭這件事不變
        libc.malloc_zone_statistics(None, ctypes.byref(stats))
        return stats.size_in_use / 1e6
    except Exception:  # noqa: BLE001 - 量不到就當守衛不存在,不影響辨識
        logger.debug("讀取記憶體用量失敗", exc_info=True)
        return None


def round_window_corners(child_id: int, width: int, height: int,
                         radius: int) -> None:
    """Aqua 的下拉清單本來就是圓角,不必做也做不到。⚠️ **而且要安靜**:
    每 `<Map>` 一次就走一次,留一行就是每次點開下拉都往記錄檔倒垃圾。"""
    return None


def set_window_icon(window, ico_path, png_path):
    """Aqua 只有 `iconphoto` 這一條會真的生效(2026-09-28 拍 Dock 實測,對照
    表在 `plat.set_window_icon`)。

    ⚠️ **`.ico` 在這裡完全沒有用**:`iconbitmap(default=…)` 直接 TclError,
    而不帶 `default=` 的那個簽章不拋例外卻**什麼都沒改**——所以這份實作連試
    都不試,直接走 PNG。
    ⚠️ **回傳的 `PhotoImage` 一定要被呼叫端留著**:沒人指著就被 GC 掉,症狀
    是圖示變回空白而且沒有任何錯誤。"""
    import tkinter as tk

    img = tk.PhotoImage(master=window, file=str(png_path))
    window.iconphoto(True, img)
    return img


def strip_default_menubar(window) -> None:
    """掛一個**空的** menubar 把 Tk 自己那排 `File`/`Edit`/`Window`/`Help` 頂掉。

    ⚠️ **判準是「有沒有設 menubar」不是「設了什麼」**(2026-09-28 拍選單列實測):
    只要 `configure(menu=…)` 指到任何一個 `tk.Menu`(哪怕一項都沒有),那四個
    預設項目就整排消失,只剩應用程式選單(顯示的是 App 名稱)。
    ⚠️ **參照要掛在視窗上**:`tk.Menu` 沒人指著會被 GC 掉,而症狀是那四個英文
    項目自己長回來(同 `set_window_icon` 那顆 `PhotoImage` 的坑)。"""
    import tkinter as tk

    window._plat_menubar = tk.Menu(window)
    window.configure(menu=window._plat_menubar)
    return None


def co_initialize_ex() -> int | None:
    """沒有 COM 這回事。⚠️ **而且要安靜**:這條路每開一條錄音執行緒就會走
    一次,吐任何一行警告都會在使用者的記錄檔裡堆成一片,而那一片說的是一件
    根本不存在的問題。"""
    return None


def preload_onnxruntime() -> None:
    """macOS 沒有「系統目錄裡躺著一份舊 onnxruntime」這回事,不必預載。
    ⚠️ **而且要安靜**:OCR 每回收一次引擎就會走一次這條路。"""
    return None


def system_cpu_ticks() -> tuple[float, float] | None:
    """`host_statistics(HOST_CPU_LOAD_INFO)`:回 `(閒置, 總計)`,單位 1/100 秒。

    ⚠️ **`os.getloadavg()` 試過了,不合用**(2026-09-25 實測,`spec/mac/04` §4.3
    原本寫的就是它):把 10 顆核心燒滿 3 秒,`loadavg[0]` 從 1.53 **紋風不動**
    ——它是一分鐘的指數平均,而這一欄要答的是 30 秒的窗裡機器忙不忙。同一輪
    `host_statistics` 給閒置 6% → 滿載 100%,與 `iostat -c 2` 的 id 欄對得上。

    ⚠️ **`mach_host_self()` 每呼叫一次就多留一個送出權參照,一定要還**
    (2026-09-25 用 `mach_port_get_refs` 量到:不還的話呼叫 1000 次 urefs
    2 → 1002,還了就停在原地)。這支程式可以開好幾天、錄音每 30 秒問一次,
    不還就是一條只會在「開很久之後」才現形的洩漏。所以 `finally` 不能省。"""

    host = None
    try:
        libc = _libsystem()
        libc.mach_host_self.restype = ctypes.c_uint
        libc.mach_task_self.restype = ctypes.c_uint
        libc.mach_port_deallocate.argtypes = [ctypes.c_uint, ctypes.c_uint]
        libc.host_statistics.argtypes = [ctypes.c_uint, ctypes.c_int,
                                         ctypes.POINTER(_CpuLoadInfo),
                                         ctypes.POINTER(ctypes.c_uint)]
        libc.host_statistics.restype = ctypes.c_int
        info = _CpuLoadInfo()
        count = ctypes.c_uint(_CPU_STATE_MAX)
        host = libc.mach_host_self()
        if libc.host_statistics(host, _HOST_CPU_LOAD_INFO,
                                ctypes.byref(info), ctypes.byref(count)) != 0:
            return None
        t = info.cpu_ticks
        # ⚠️ 總計要**四格全加**:nice 那格不加的話,機器上只要有背景的低優先權
        # 工作(而我們自己的子行程正是 nice 過的)忙碌度就會被高估
        return float(t[_CPU_STATE_IDLE]), float(
            t[_CPU_STATE_USER] + t[_CPU_STATE_SYSTEM]
            + t[_CPU_STATE_IDLE] + t[_CPU_STATE_NICE])
    except Exception:  # noqa: BLE001 - 診斷少一欄而已,絕不影響錄音
        logger.debug("讀取全機 CPU 時間失敗", exc_info=True)
        return None
    finally:
        if host is not None:
            libc.mach_port_deallocate(libc.mach_task_self(), host)


#: `AVAuthorizationStatus`。⚠️ **0(尚未詢問)也算沒授權**:那時候繼續錄,
#: TCC 要嘛跳提示(使用者可能沒看到)、要嘛靜默拒絕——兩種都不該讓他往下走
_AV_NOT_DETERMINED, _AV_RESTRICTED, _AV_DENIED, _AV_AUTHORIZED = 0, 1, 2, 3
_AV_WHY = {
    _AV_NOT_DETERMINED: "這台電腦還沒有把麥克風權限給這個工具",
    _AV_RESTRICTED: "這台電腦的管理原則不允許使用麥克風",
    _AV_DENIED: "麥克風權限被拒絕了",
}
#: 系統設定的深層連結。⚠️ **那一頁要往下捲才看得到**,所以非得直接開它不可
_MIC_PANE = "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"


def microphone_permission() -> Outcome:
    """問 `[AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeAudio]`。

    ⚠️ **用 ctypes 不裝 PyObjC**:為了一個三態查詢多一個相依不划算,而這一段
    只有三個 objc 呼叫(2026-09-25 實測可行,回 3 = 已授權)。
    ⚠️ **問不到就回 `unsupported` 不要猜**:猜「沒授權」會把一場本來錄得成的
    會議擋掉,而那個代價比錄到一場靜音更大。"""
    import ctypes
    import ctypes.util

    try:
        objc = ctypes.CDLL(ctypes.util.find_library("objc"))
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        av = ctypes.CDLL("/System/Library/Frameworks/AVFoundation.framework/"
                         "AVFoundation")
        media = ctypes.c_void_p.in_dll(av, "AVMediaTypeAudio")
        send = objc.objc_msgSend
        send.restype = ctypes.c_long
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        status = send(ctypes.c_void_p(objc.objc_getClass(b"AVCaptureDevice")),
                      ctypes.c_void_p(objc.sel_registerName(
                          b"authorizationStatusForMediaType:")), media)
    except Exception:  # noqa: BLE001 - 問不到就讓呼叫端維持原本的行為
        logger.debug("查詢麥克風授權狀態失敗", exc_info=True)
        return Outcome("unsupported", "這台機器問不到麥克風授權狀態")

    if status == _AV_AUTHORIZED:
        return Outcome("ok")
    return Outcome("failed", _AV_WHY.get(status, f"麥克風授權狀態異常({status})"))


def open_microphone_settings() -> Outcome:
    """打開「系統設定 → 隱私權與安全性 → 麥克風」。

    ⚠️ **`import subprocess` 那一行不可以省**:這一支模組的模組層**沒有**它
    (2026-09-29 修:原本這裡裸用 `subprocess.Popen`,拿到的是 `NameError`
    被下面那個 `except` 收成 `failed`——按鈕按了沒反應,而理由那一句寫的是
    「打不開系統設定:NameError」,看起來像系統設定的問題)。"""
    import subprocess

    try:
        subprocess.Popen(["open", _MIC_PANE])
    except Exception as e:  # noqa: BLE001
        return Outcome("failed", f"打不開系統設定:{type(e).__name__}")
    return Outcome("ok")
