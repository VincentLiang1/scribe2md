"""轉檔/錄音期間別讓機器睡著;檔案轉檔另外把本行程降到背景優先權。

**怎麼做是平台的事**(`plat.py`,2026-09-26 全部搬完):Windows 從行程內下
`SetThreadExecutionState` ＋ Power Request、macOS 起一支 `caffeinate` 子行程。
這一支留下的是**領域知識**:兩種模式的差別、旗標由誰持有、什麼時候該警告。

- 轉檔(`keep_awake`):連螢幕一起擋——鎖定螢幕/進入省電會壓抑 CPU 時脈拉長
  轉檔時間,而且轉檔時人就在電腦前。
- 錄音(`stay_awake_begin`/`end`):**只擋系統睡眠,螢幕照常關**(使用者指定
  2026-07-22:一兩小時的會議沒必要整場亮著;螢幕關/鎖定不影響收音)。

⚠️ **拿不到防護絕不能讓轉檔或錄音跑不起來**,但**也絕不可以安靜地跳過**:
`failed`(這台應該做得到卻沒成功)留一行 WARNING、`unsupported`(這個平台
不從這條路做)什麼都不說——2026-09-25 之前 macOS 上的防睡眠就是一個「從來
沒生效過、而且一個字都不會說」的 no-op。
"""

import contextlib
import logging
import os
import subprocess
import threading
from collections.abc import Iterator

from meeting_scribe import plat

logger = logging.getLogger(__name__)

#: 預設留幾顆核心給系統與介面(見 `default_worker_count`)
_RESERVED_CORES = 1
#: 使用者在「進階參數設定」指定的核心數;`None` = 用預設
_worker_count: int | None = None


def max_cpu_cores() -> int:
    """偵測到的最大 CPU 核心數(至少 1)。"""
    return os.cpu_count() or 1


def total_ram_mb() -> int | None:
    """整台機器的實體記憶體(MB);量不到回 None。

    給「該分多少記憶體給某個子系統」這種決策用(目前是 OCR 子行程的引擎
    回收門檻)——同 `max_cpu_cores`,機器能力的偵測集中在這個模組。

    ⚠️ **怎麼問是平台的事**(`plat.total_ram_mb`;Windows 是
    `GlobalMemoryStatusEx`、macOS 是 `sysctlbyname("hw.memsize")`),這裡留一層
    薄殼——它是測試換假貨的接縫,而呼叫端一律問這一支。"""
    return plat.total_ram_mb()


def default_worker_count() -> int:
    """預設使用核心數 = 最大核心數 - _RESERVED_CORES(至少 1)。介面預設值。"""
    return max(1, max_cpu_cores() - _RESERVED_CORES)


def normalize_worker_count(value) -> int:
    """介面「CPU 核心數」欄位的值 → 1..最大核心數;空白/非法回到預設(最大核心數-1)。

    ⚠️ **範圍在這一側夾、不交給控制項**(spec §8:超限讓控制項自己擋會跳出英文訊息)。
    ⚠️ **兩套介面共用這一支**(2026-09-02 從 `app._normalize_cores` 搬來):網頁版的
    `gr.Number` 給的是 float、原生視窗的 `Spinbox` 給的是字串,判準寫兩份就會漂。"""
    try:
        return max(1, min(max_cpu_cores(), int(value)))
    except (TypeError, ValueError):
        return default_worker_count()


def set_worker_count(n: int | None) -> bool:
    """設定使用核心數(夾到 1..最大核心數);None/非正整數回到預設。
    回傳是否有變動(供呼叫端決定是否清引擎快取,讓新值生效)。"""
    global _worker_count
    new = min(int(n), max_cpu_cores()) if isinstance(n, int) and n > 0 else None
    changed = new != _worker_count
    _worker_count = new
    return changed


def cpu_worker_count(cap: int | None = None) -> int:
    """CPU 密集工作的執行緒數:使用者指定值(或預設 最大核心數-1),
    夾到 1..最大核心數,再套用可選上限 cap(如標點模型只需 4)。"""
    n = _worker_count if _worker_count else default_worker_count()
    n = max(1, min(n, max_cpu_cores()))
    return n if cap is None else max(1, min(n, cap))


def system_cpu_ticks() -> tuple[float, float] | None:
    """(閒置, 總計) 的系統 CPU 時間;取不到回 None(呼叫端少一欄,不影響功能)。

    **為什麼要看全機而不是本行程**:講者分析自 2026-08-03 起跑在子行程裡
    (見 diarworker),`time.process_time()` 只算得到自己——工作搬走之後那個
    數字看起來就一直很閒,而機器其實滿載。診斷若答不出「機器有多忙」,就
    分不出「子行程在算」與「子行程掛了」,而那兩種情況的收音數字一模一樣
    (實測真的騙過一輪)。擺在 power 是因為這裡本來就是「這台機器的資源」
    那一層(SetThreadExecutionState、cpu_worker_count)。

    那一次系統呼叫本身 2026-09-25 搬進了 `plat.system_cpu_ticks()`
    ——**兩個平台回的都是累計 tick**(Windows `GetSystemTimes`、macOS
    `host_statistics(HOST_CPU_LOAD_INFO)`),所以呼叫端的 delta 算法與
    記錄檔那一欄在兩邊完全一樣。⚠️ `os.getloadavg()` 實測不合用,理由寫在
    平台層那一支與 `docs/spec/mac/04` §4.3。"""
    return plat.system_cpu_ticks()


def _sleep_guard_begin(*, display: bool) -> plat.Outcome:
    """叫系統別自動睡著。⚠️ **薄殼:測試在這裡換假貨**(同 `record._co_initialize_ex`)。

    ⚠️ **Windows 那份是「綁呼叫執行緒」的狀態**,所以 `_sleep_guard_end()`
    必須在設它的同一條執行緒上呼叫——錄音那條因此要有自家的長駐執行緒。"""
    return plat.prevent_auto_sleep(display=display)


def _sleep_guard_end() -> None:
    """還給系統。⚠️ **只在 begin 回 `ok` 時呼叫**:沒設過就去清,等於把別人
    設的狀態一起清掉。"""
    plat.allow_auto_sleep()


def _execution_request_begin(reason: str) -> tuple[plat.Outcome, object]:
    """要求「待命期間讓這個行程繼續跑」;回 (結果, 還原用的權杖)。

    與 `_sleep_guard_begin` **並用而不是取代**:那一支擋的是「自動」睡著,
    而使用者**主動**闔蓋/鎖屏擋不住(也不該擋)——Windows 的 Modern Standby
    進去之後會把行程整個凍結,實測 63 分鐘的轉檔停擺 40 分鐘。

    ⚠️ **薄殼:測試在這裡換假貨**。拿不到就只記一行,防護拿不到絕不能讓
    轉檔/錄音跑不起來。"""
    return plat.keep_running_in_standby(reason)


def _execution_request_end(token: object) -> None:
    """撤銷上面那個請求(權杖是 `None` 就什麼都不做)。⚠️ **絕不拋**。"""
    plat.stop_keeping_running_in_standby(token)


@contextlib.contextmanager
def keep_awake() -> Iterator[None]:
    """轉檔期間維持系統與螢幕喚醒;離開(含例外)必定解除。

    **兩道防護,管的是不同的事**(2026-08-07 實測補上第二道):
    - `ES_*`:阻止「自動」進入睡眠與關螢幕。
    - Power Request(`ExecutionRequired`):使用者主動闔蓋/鎖屏而系統進入
      **Modern Standby** 時,ES_* 完全攔不住,而進去之後 Win32 程式會被
      整個凍結——實測一支 2 小時 19 分的錄音轉檔,牆鐘 63 分鐘裡有 40
      分鐘行程停擺(見模組上方常數的完整數字)。這一道不阻止待命,
      而是請系統在待命期間讓這個行程繼續跑。
    """
    # ⚠️ **只有 `failed` 才警告**:`unsupported` 是「這個平台不從這條路做」
    #    (macOS 走下面那支 caffeinate),每轉一次檔就喊一句狼來了的話,真的
    #    出問題那一次反而沒有人看見
    guard = _sleep_guard_begin(display=True)
    if guard.state == "failed":
        logger.warning("無法設定防止休眠狀態,轉檔期間螢幕仍可能鎖定:%s",
                       guard.reason)
    keeper = _keeper_begin(display=True)   # macOS:caffeinate -d -i -m -w <pid>
    standby, request = _execution_request_begin("meeting-scribe 正在轉檔")
    if standby.state == "failed":
        logger.warning(
            "無法要求「待命期間繼續執行」:若電腦進入新式待命(闔蓋/鎖屏),"
            "轉檔會暫停,喚醒後才繼續(%s)", standby.reason
        )
    try:
        yield
    finally:
        _keeper_end(keeper)
        _execution_request_end(request)
        if guard.ok:
            _sleep_guard_end()


def _keeper_begin(*, display: bool):
    """起一支「別讓機器睡著」的外部命令(macOS 的 `caffeinate`);沒有就回 None。

    ⚠️ **這一整條 2026-09-25 才補**:在那之前 macOS 上的防睡眠是**完全靜默的
    no-op**——`_set_execution_state()` 非 Windows 直接回 False,而那句 WARNING
    又被 `if sys.platform == "win32"` 擋住。本機 `pmset` 實測是「閒置 1 分鐘
    就睡」:轉檔走開會被打斷,**錄音睡著就是掉音訊,而錄音不能重來**。
    ⚠️ **拿不到不算失敗**(同 Windows 那邊的規矩):防護拿不到絕不能讓轉檔或
    錄音跑不起來——但要留一行,否則又變成一個靜默的 no-op。"""
    argv = plat.keep_awake_argv(display=display)
    if argv is None:
        return None
    try:
        proc = subprocess.Popen(  # noqa: S603 - 固定命令,無 shell
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **plat.spawn_kwargs(low_priority=False),
        )
    except OSError as e:
        logger.warning("擋不住系統睡眠(%s):%s——轉檔或錄音中途若睡著會中斷",
                       argv[0], e)
        return None
    logger.info("已要求系統不要睡著(%s)", " ".join(argv))
    return proc


def _keeper_end(proc) -> None:
    """收掉上面那支。⚠️ **絕不拋**:它是在「工作已經做完」之後跑的。"""
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        logger.debug("收拾防睡眠命令時出錯", exc_info=True)


def _priority_lower() -> tuple[plat.Outcome, object]:
    """把本行程降到背景優先權;回 (結果, 還原用的權杖)。

    ⚠️ **薄殼:測試在這裡換假貨**。⚠️ 這與子行程那兩條路(`plat.spawn_kwargs`
    / `plat.apply_own_low_priority`)是三件不同的事,見 `plat.lower_own_priority`。"""
    return plat.lower_own_priority()


def _priority_restore(token: object) -> None:
    """還原成降之前**那個值**(⚠️ 不可以寫死還原成「一般」,見平台層那一支)。"""
    plat.restore_own_priority(token)


@contextlib.contextmanager
def below_normal_priority() -> Iterator[None]:
    """檔案轉逐字稿期間把**整支程式**降到 below-normal;離開必定還原。

    起因:使用者 2026-08-04 回報「現成檔案轉換時 CPU 會用到 98~99%」,
    要的是「轉檔時電腦還能用」。這是 OCR 子行程 2026-08-03 用過的同一招
    (見 ocr._BELOW_NORMAL_PRIORITY 的實測表:前景被拖慢 +16% → +2%,
    代價是自己慢約兩成),差別在**用法**:那邊是 Popen 的 creationflag,
    這裡的轉錄與講者分析都在主行程的執行緒裡,沒有子行程可以掛旗標,
    只能對已經在跑的自己呼叫 SetPriorityClass。

    ⚠️ **降優先權不會讓工作管理員的 CPU% 變小**——它只決定「搶不搶得贏」,
    機器閒著時照樣跑到 98~99%。要數字下降只能調小「CPU 核心數」
    (見 cpu_worker_count),那是另一個旋鈕、使用者可自行調整。

    ⚠️ **連 UI 一起降**:主行程就是視窗本體,所以前景忙碌時進度更新與
    「停止」的回應也會跟著等(平常滿載就已有數十秒延遲,所以按下停止的
    當下就要先把字樣改掉)。這是 in-process 工作的必然代價;真要避免
    就得把轉錄也搬進子行程,那是另一個量級的改動。

    **只包住檔案轉檔**(run_pipeline)。現場收音不可套用:收音執行緒被排擠
    正是 2026-08-03 掉了 4.6 分鐘音訊的那個災情,錄音期間任何降低自身
    排程權重的動作都是反方向(見 record._CaptureDiag 與 diarworker)。

    還原時寫回**原本那個值**而不是寫死 NORMAL:使用者可能自己用工作管理員
    或捷徑把整支程式設成別的優先權,轉完檔擅自拉回一般是把設定改掉。"""
    lowered, previous = _priority_lower()
    if lowered.state == "failed":
        logger.warning("無法調低程序優先權,轉檔期間電腦操作可能較不順暢:%s",
                       lowered.reason)
    try:
        yield
    finally:
        if lowered.ok:
            _priority_restore(previous)


# 現場收音的防睡眠旗標由這條長駐執行緒持有(見 stay_awake_begin);
# _awake_release 兼任「生效中」判準:非 None = 執行緒在跑
_awake_lock = threading.Lock()
_awake_release: threading.Event | None = None


def _awake_worker(release: threading.Event, keeper) -> None:
    """在自己身上設旗標後守著 release 事件;收到通知清旗標退場。
    不帶 ES_DISPLAY_REQUIRED:錄音只需系統不睡,螢幕照常省電關閉
    (使用者指定 2026-07-22)。

    ⚠️ **而螢幕關閉正是 Modern Standby 的進入條件**——所以錄音這條反而
    比轉檔更需要 Power Request:被凍結就是直接掉音訊,而且掉了不能重來
    (2026-08-07 在轉檔上實測到 40 分鐘凍結,見模組上方常數)。"""
    # 錄音**不擋螢幕**(display=False):使用者 2026-07-22 指定
    guard = _sleep_guard_begin(display=False)
    if guard.state == "failed":
        logger.warning("無法設定防止睡眠狀態,錄音期間系統仍可能睡眠中斷錄音:%s",
                       guard.reason)
    standby, request = _execution_request_begin("meeting-scribe 正在錄音")
    if standby.state == "failed":
        logger.warning(
            "無法要求「待命期間繼續執行」:電腦進入新式待命時錄音會中斷,"
            "而錄音無法重來——建議錄音期間不要闔上螢幕或鎖定(%s)", standby.reason
        )
    # ⚠️ **下面收的是「自己那一支」keeper,不是模組層的全域**(2026-09-26 修):
    #    `stay_awake_end()` 只設事件就返回,這條執行緒要過一下才醒;使用者在那個
    #    空檔按「開始錄音」的話,`stay_awake_begin` 已經換上**新的一支**
    #    `caffeinate`,而醒來的這條再去讀全域就是**把新錄音的防睡眠掐掉**——而且
    #    完全靜默。⚠️ macOS 上那等於「錄到一半機器睡著」,而錄音不能重來。
    try:
        release.wait()
    finally:
        _keeper_end(keeper)
        _execution_request_end(request)
        if guard.ok:
            _sleep_guard_end()


def stay_awake_begin() -> None:
    """現場收音用的「開關式」防睡眠:錄音橫跨多個 UI 事件,無法以單一
    context manager 包住,由開始/停止事件成對呼叫。

    旗標必須由自家長駐執行緒持有:SetThreadExecutionState 的 ES_CONTINUOUS
    是「綁呼叫執行緒」的狀態,執行緒結束旗標即自動消失——gradio 事件跑在
    anyio 執行緒池的短命 worker 上(閒置 10 秒回收,anyio WorkerThread.
    MAX_IDLE_TIME 實查),直接在事件執行緒上設旗標撐不過幾秒、防護即蒸發
    (2026-07-22 使用者回報);end 也落在另一條執行緒,清不到原本那份。
    keep_awake(轉檔用)不受此雷:整段轉檔都在同一條事件執行緒上忙,
    設與清同緒、且執行緒活著——與本執行緒的旗標各自獨立,互不干擾。
    重複 begin 無害(生效中直接返回);daemon=True,硬退出隨行程消滅、
    Windows 自動清掉該執行緒的旗標。"""
    global _awake_release
    with _awake_lock:
        if _awake_release is not None:
            return
        # ⚠️ **錄音那條不帶 `-d`**:螢幕照常關(使用者 2026-07-22 指定,一兩
        # 小時的會議沒必要整場亮著;macOS 的螢幕睡眠不會凍結行程)
        keeper = _keeper_begin(display=False)
        _awake_release = threading.Event()
        # ⚠️ **keeper 用參數交給那條執行緒**,不要放在模組層讓它自己去讀:
        #    理由見 `_awake_worker` 的 finally
        threading.Thread(
            target=_awake_worker, args=(_awake_release, keeper),
            name="stay-awake", daemon=True,
        ).start()


def stay_awake_end() -> None:
    global _awake_release
    with _awake_lock:
        if _awake_release is not None:
            _awake_release.set()
            _awake_release = None
