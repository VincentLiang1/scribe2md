"""平台抽象層的對外介面:同一個意圖,兩個平台各有一套做法。

**動到任何 `sys.platform` 判斷之前先讀 `docs/spec/mac/02-平台抽象層設計.md`。**

三份實作(`plat_win` / `plat_mac` / `plat_null`),選哪一份只在 `_impl()`
一處發生。⚠️ **不在 import 時綁成模組層常數**:那樣測試換不回來,而
「兩份實作的接線都要在任一平台測得到」正是這一層存在的理由之一
(spec/mac/02 §2.6)。

⚠️ **靜默 no-op 是禁止的**(§2.2):每一個能力回的是 `Outcome` 三態
——`ok` / `unsupported`(這個平台沒有這個概念)/ `failed`(有,但這次
沒拿到)。前者只記 log,後者是使用者可以自己補救的那一種,**兩者絕不
可以混成同一句話**:「這台 Mac 沒給權限」與「macOS 不支援」對使用者
來說一個是三十秒能解、一個是只能放棄。

完整的能力表在 spec/mac/02 §2.3(那張表就是契約,加東西先改它)。⚠️ **還沒搬
進來的逐檔列在 `tests/test_plat.py` 的 `_NOT_YET_MIGRATED`,那份清單只准變短**。

⚠️ **游標名、emoji 字型、`appdata_base` 這三支只是轉呼叫 winkit 的 `plat`**(照 A/B
判準它們住那裡:另外兩個下游要的是同一個東西;搬遷見 spec/mac/02 §2.8.1)。**要改
值就去改 winkit 那份**,這邊沒有第二份。⚠️ **winkit 的 `plat` 一律在函式裡 import**:
這一層的模組層只准碰標準函式庫(OCR 子行程也 import 它)。
"""

import logging
import sys
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)

State = Literal["ok", "unsupported", "failed"]


@dataclass(frozen=True)
class Outcome:
    """一個平台能力這次的結果。

    ⚠️ `reason` 在 `unsupported` 與 `failed` 時**必填且是繁體中文**:它會
    進記錄檔,而記錄檔存在的唯一理由就是事後有人要看懂發生什麼事。"""

    state: State
    reason: str = ""

    def __post_init__(self) -> None:
        if self.state != "ok" and not self.reason:
            raise ValueError("unsupported / failed 一定要寫理由(繁中)")

    @property
    def ok(self) -> bool:
        return self.state == "ok"


def _impl():
    """這台機器該用哪一份實作。**每次呼叫都問**,理由見模組 docstring。"""
    from meeting_scribe import plat_mac, plat_null, plat_win

    if sys.platform == "win32":
        return plat_win
    if sys.platform == "darwin":
        return plat_mac
    return plat_null


def name() -> str:
    """目前實作的短名(`windows` / `macos` / `null`),給記錄檔與測試用。"""
    return _impl().NAME


# --- 優先權 ---------------------------------------------------------------
#
# 意圖只有一個:**讓這顆子行程搶不贏前景**,也就是使用者 2026-08-04 要的
# 「轉換時電腦還能用」。兩個平台達成的時機不同,所以拆成三支:
#
# | 平台 | 誰動手 | 什麼時候 |
# | --- | --- | --- |
# | Windows | 父行程 | `Popen` 的 `creationflags`(建立的當下) |
# | macOS | 子行程自己 | 起來之後第一件事 `os.nice()` |
#
# ⚠️ **macOS 不能照抄 Windows 那招**:POSIX 沒有 `creationflags`,而
# `preexec_fn` 在多執行緒行程裡是 Python 自己標註為不安全的(這支程式
# 到處是執行緒)。所以改成「子行程自己降」——nice 值一樣會被孫行程繼承,
# 效果與 Windows 那條相同。


def spawn_kwargs(*, low_priority: bool) -> dict:
    """啟動子行程時要額外餵給 `subprocess.Popen` 的參數。

    ⚠️ **呼叫端不准自己拼 `creationflags`**:那個關鍵字在非 Windows 上只要
    不是 0 就直接 `ValueError`,而 `power.BELOW_NORMAL_PRIORITY_CLASS` 是
    寫死的常數——2026-09-24 在 Mac 上就是這樣讓轉錄與講者分析的子行程
    **兩個都起不來**、整條管線無聲退回主行程跑。AST 掃描反向守著。"""
    return _impl().spawn_kwargs(low_priority=low_priority)


def worker_argv(*, low_priority: bool) -> list[str]:
    """要接在子行程命令列後面的旗標(macOS 用來告訴子行程「自己降」)。

    ⚠️ **這裡回空清單不代表沒做事**:Windows 是父行程在 `spawn_kwargs`
    那一步就做完了。兩支要一起看。"""
    return _impl().worker_argv(low_priority=low_priority)


#: `worker_argv` 會送出的旗標。worker 端解析時用這個常數,不要各自抄字面值
WORKER_LOW_PRIORITY_FLAG = "--below-normal"


def apply_own_low_priority() -> Outcome:
    """子行程自己把優先權降下來(命令列帶了 `WORKER_LOW_PRIORITY_FLAG` 才呼叫)。

    Windows 走的是建立時的 creationflags,所以這裡回 `unsupported`——那不是
    壞掉,是**那個平台不從這條路做**。"""
    return _impl().apply_own_low_priority()


def adopt_low_priority_from_argv(argv: list[str]) -> Outcome | None:
    """worker 的 `main()` 開頭呼叫:命令列有旗標就降,沒有就回 None。

    ⚠️ **收在這裡而不是讓三支 worker 各抄一段**:抄第三份的時候一定會有
    人忘記記 log,而降優先權失敗是**靜默**的——轉檔照樣跑完,只是整台
    機器卡住,而那個症狀指不回這裡。"""
    if WORKER_LOW_PRIORITY_FLAG not in argv:
        return None
    outcome = apply_own_low_priority()
    if outcome.ok:
        logger.info("子行程已降到背景優先權(%s)", name())
    elif outcome.state == "failed":
        logger.warning("子行程降優先權失敗,轉檔期間電腦可能較不順暢:%s",
                       outcome.reason)
    else:
        logger.debug("子行程不從這條路降優先權:%s", outcome.reason)
    return outcome


# --- 介面 -----------------------------------------------------------------


def disabled_cursor() -> str:
    """停用中的按鈕要顯示的 Tk 游標名(網頁版的 `cursor: not-allowed`)。

    ⚠️ **`"no"` 是 Windows 的 Tk 才有的游標名**:2026-09-24 在 macOS 上,
    `HandButton` 一建起來就 `TclError: bad cursor spec "no"`,而它是整個
    `desktop.py` 唯一的按鈕類別——也就是**視窗一個元件都建不出來**
    (`tests/test_desktop.py` 那 198 條全倒在這一行)。

    ⚠️ **Windows 那邊刻意不換成 `notallowed`**:那個名字是 Tk/Aqua 的,
    在 Windows 上會不會過**這台機器驗不到**,而現行的 `"no"` 是已知可用的。
    平台各給各的,不去統一。"""
    from winkit import plat as winkit_plat

    return winkit_plat.disabled_cursor()


# --- 外殼整合 -------------------------------------------------------------
#
# 「讓使用者看到這個檔案/資料夾」。⚠️ **這一組全部回 `Outcome`、不自己丟
# `UserFacingError`**:那是呼叫端的事(它才知道要怎麼跟使用者講)。


def allow_foreground() -> Outcome:
    """把前景權放給即將開起來的檔案管理員。

    不放行的症狀是**按下去只有工作列閃爍**,而使用說明裡已經有一條 FAQ
    在講那個現象——別再製造第二個。macOS 沒有這個概念(`open` 會自己把
    Finder 帶到前面),所以那邊回 `unsupported`。"""
    return _impl().allow_foreground()


def open_folder(path) -> Outcome:
    """在檔案管理員裡**開進**這個資料夾。"""
    return _impl().open_folder(path)


def reveal(path) -> Outcome:
    """在檔案管理員裡**指出**一個檔(選起來)或資料夾(開進去)。

    ⚠️ **檔案那條不可以退化成「用預設程式開啟它」**:記錄檔是 `.log`,
    很多機器上沒有預設開啟程式,那時跳出來的是「你要如何開啟?」的選擇器
    ——而使用者要的只是「讓我看到這個檔」。"""
    return _impl().reveal(path)


# --- 試聽片段的播放 -------------------------------------------------------
#
# 命名時「▶ 試聽」與「🔍 核對」那一列放的都是幾秒的 wav。⚠️ **兩個平台都不接
# 播放函式庫**:內建的就夠(非同步、可隨時停),為了放一句話多一個相依不划算。


def play_sound(path) -> tuple[Outcome, object]:
    """把一個 wav **非同步**放出來;回 (結果, 停止用的權杖)。

    ⚠️ **一定要非同步**:呼叫端是 Tk 的主執行緒,同步播的話整個視窗會凍到
    那一句放完(而核對表的一輪可能有 40 秒)。
    ⚠️ **這一支的狀態機在呼叫端**(按一次播、再按一次停、按別人直接切換,
    使用者 2026-07-18 指定),所以這裡**不記住**現在在放什麼——權杖交出去。
    ⚠️ **回 `unsupported` 與 `failed` 的文案必須分開**(§2.2):前者是「這台
    機器上沒有試聽這回事」,後者是「有,但這次沒放出來」。

    2026-09-29 補進來的:原本 `desktop.py` 直接 `import winsound`,而 macOS
    上那是 `ModuleNotFoundError`——症狀是**按了試聽完全沒有反應**(訊息只有
    一行小字,使用者回報的是「播放鍵按下去沒有反應」),記錄檔裡每按一次
    多一段堆疊。⚠️ **AST 那道守門當時抓不到它**:掃的是 `sys.platform` 這類
    屬性,而 `import winsound` 是另一種形狀(`tests/test_plat.py` 已補)。"""
    return _impl().play_sound(path)


def stop_sound(token: object) -> None:
    """停掉 `play_sound` 正在放的那一段(權杖 `None` 就什麼都不做)。

    ⚠️ **要安靜、而且要能重複呼叫**:切換講者、收起命名區、換下一個檔、
    以及「放完了」的 `after` 排程都會走到這裡,而多數時候那一段**早就自己
    結束了**——把「已經沒在放」當成錯誤記一行,就是在記錄檔裡堆垃圾。"""
    _impl().stop_sound(token)


def mono_family() -> str:
    """說明文字裡的程式碼字要用的等寬字型。

    ⚠️ **缺了不會報錯,只會安靜地不等寬**:Tk 找不到指定的 family 就退到
    預設字型——2026-09-24 在 macOS 上實測,要 `Consolas` 拿回來的是
    `.AppleSystemUIFont`(`Font.actual("family")` 問得出來),而畫面上看起來
    就只是「這幾個字有點怪」。"""
    return _impl().MONO_FAMILY


# --- 給使用者的指路文案 ---------------------------------------------------
# ⚠️ **只收「處方本身就不一樣」的句子**(2026-10-02 使用者要求依平台顯示):Mac 上沒有
# `安裝.bat`、也沒有小畫家,照著 Windows 那句做就是找不到東西。只在 Windows 才會出現
# 的訊息(缺 Visual C++ 那一組)照舊寫死,不必經過這裡。


def reinstall_hint() -> str:
    """「環境沒裝好」時叫人怎麼重裝,以「請」開頭、不帶句號,接在原因後面。

    ⚠️ **Mac 不是「再雙擊一次 App」**:啟動器只在版本戳記對不上、或整個
    site-packages 不見時才重跑安裝,少一個套件時雙擊只會照常開、照樣壞
    (`make_mac_app.LAUNCHER_C`)。刪掉 `VERSION` 才會讓它重跑。"""
    return _impl().REINSTALL_HINT


def heic_workaround() -> str:
    """HEIC 解不開時的替代做法(用系統內建的程式另存成 JPG),以「或」開頭。"""
    return _impl().HEIC_WORKAROUND


# --- 機器能力 -------------------------------------------------------------


def seconds_since_process_start() -> float | None:
    """這個行程建立到現在過了幾秒;問不到回 None。

    ⚠️ **要從行程建立算,不是從某一支模組 import 算**:Python 自己起來、
    套件根與 `desktop` 的 import 也是使用者等的那一段(2026-09-15 量到約
    0.2 秒),拿 `perf_counter` 在模組裡記起點就漏掉了。"""
    return _impl().seconds_since_process_start()


# --- 暫存目錄的存活鎖 -----------------------------------------------------
#
# 意圖:**讓別的實例看得出「這個暫存目錄還有人在用」**。孤兒(上次硬退出
# 留下的)可以刪,活的不行——刪掉活的等於在使用者正在命名、正在試聽、
# 或正在轉一支兩小時的檔時把工作目錄抽走。
#
# ⚠️ **兩個平台的機制完全不同,而且 Windows 那套在 POSIX 上是靜默失效的**
# (2026-09-24 在 Mac 上查出來):Windows 靠「檔案開著就刪不掉」,而 POSIX
# **可以刪掉開啟中的檔**——`unlink` 成功,開著的 fd 照樣能用。所以原本那套
# 在 macOS 上等於沒有鎖:另一個實例一啟動就把第一個的目錄整個掃掉。
# POSIX 的對應物是**勸告鎖**(`flock`):拿不到就代表還有人握著。


def hold_temp_lock(path):
    """在 `path` 建一個存活鎖並持有它。回傳的東西要一直留著(關掉 = 解鎖)。

    ⚠️ **回傳值不可以丟掉**:Windows 那邊靠的就是「這個檔案物件還開著」,
    被垃圾回收就等於鎖沒了,而症狀要等到另一個實例啟動才看得見。"""
    return _impl().hold_temp_lock(path)


def temp_lock_is_held(path) -> bool:
    """別人是不是正握著這個存活鎖?握著 = 那個目錄不准刪。

    ⚠️ **判不出來時一律回 True**(保守):把活的當孤兒刪掉,代價是使用者
    正在做的事當場斷掉;把孤兒當活的留著,代價只是暫存目錄多留一次。"""
    return _impl().temp_lock_is_held(path)


def emoji_font() -> tuple[object, tuple[int, ...]]:
    """彩色 emoji 的字型檔,以及它只吃哪幾個像素尺寸(空 tuple = 任意尺寸)。

    ⚠️ **Tk 自己畫不出「透明底」的彩色 emoji**:Windows 上它拿的是單色字形、
    再染上前景色(所以要繞道用 Pillow 的 `embedded_color=True`);而 **macOS 上
    Tk/Aqua 畫得出彩色,但那塊圖是不透明的**——2026-09-24 使用者一眼看出來:
    分頁列每顆圖示後面都framed 著一塊白方塊。兩個平台都得走 Pillow 那條路。

    ⚠️ **Apple Color Emoji 是點陣字型,只有固定幾個尺寸**(實測 32/96/160 可,
    21 與 137 直接 `OSError: invalid pixel size`)——所以第二個回傳值存在:
    呼叫端要挑最近的那一個畫、再縮到要的大小。Segoe UI Emoji 是向量的,回空。"""
    from winkit import plat as winkit_plat

    return winkit_plat.emoji_font()


def hidpi_image_factor(root) -> int:
    """畫圖片給這個平台時,**一個圖素要畫成幾個實體像素**才銳利;1 = 照實畫。

    ⚠️ **這不是顯示縮放**:Windows 上 Tk 照真實 DPI 畫、資產本來就按 DPI 選檔,
    所以是 1;macOS 上 Tk 一律「1 圖素 = 1 點」,Retina 螢幕於是把整張圖放大兩倍
    ——2026-09-29 使用者回報「畫面的文字有點糊」,而實測**文字是銳利的**(視窗
    真的以 2.0 倍在畫),糊的是**圖示**:它們照點數畫完之後被放大了兩倍。

    ⚠️ **這一支與 `emoji_font` 一樣只是轉呼叫 winkit 的 `plat`**(三個下游要的是
    同一個東西),要改值就去改那邊。"""
    from winkit import plat as winkit_plat

    return winkit_plat.hidpi_image_factor(root)


def make_hidpi_image(root, path, width: int, height: int):
    """把 `path` 那張**高解析度**的圖做成 Tk 影像,而版面尺寸釘死 `width`×`height`
    (點)。回影像名;這個平台辦不到就回 `None`,呼叫端退回普通的 `PhotoImage`。

    ⚠️ **檔案不可以馬上刪掉**:那張影像是**指著那個檔**的(延後載入),刪了就是
    畫面上一塊空白、而且不會有任何錯誤。"""
    from winkit import plat as winkit_plat

    return winkit_plat.make_hidpi_image(root, path, width, height)


def capture_blocksize(seconds: float, rate: int) -> int | None:
    """開收音流時要給 soundcard 的 `blocksize`;`None` = 交給函式庫用裝置預設。

    ⚠️ **兩個平台的 `blocksize` 意思完全不同**(2026-09-25 在 Mac 上炸出來:
    `TypeError: blocksize must be between 15.0 and 512`):

    | | 它是什麼 | 緩衝在哪 |
    | --- | --- | --- |
    | WASAPI | **環形緩衝的容量**(5 秒 = 抗 GIL 阻塞的防線) | 作業系統的緩衝,**C 層、不吃 GIL** |
    | CoreAudio | AudioUnit 的**切片大小**,上限由**裝置**回報(這台 512) | soundcard 自己的 deque,由一支 **Python 回呼**填 |

    所以 macOS 回 `None`:**上限是裝置給的,不可以猜一個數字寫死**。
    ⚠️ **而且那條防線在 macOS 上不成立**,不是「換個數字」就好——
    `docs/dev/recording.md` 整套緩衝策略的立論是「GIL 被鎖住時**音訊沒有
    不見,只是我們太晚去拿**」,那句話在 WASAPI 成立(料躺在 OS 的 C 層
    緩衝裡),在 CoreAudio **不成立**(回呼排不上,CoreAudio 就真的丟了)。
    macOS 這條要靠什麼守,見 `docs/spec/mac/03` 與 `07` 假設 3。"""
    return _impl().capture_blocksize(seconds, rate)


def appdata_base():
    """這個平台放「本機應用程式資料」的目錄(**不含**本專案的資料夾名)。

    ⚠️ **只有在 `LOCALAPPDATA` 沒設的時候才會問到這裡**:那個環境變數同時是
    測試的隔離鉤子(`monkeypatch.setenv`),明設過就必須完全照它——把它換成
    平台分支,整批 `test_models` / `test_pending` 會改去讀**使用者真實的**落地
    目錄,那是假綠燈也是弄髒真實資料。
    ⚠️ **不可以回 `~/.cache`**:那是 Linux 的 XDG 慣例,macOS 上沒有任何系統
    工具或使用者會去那裡找應用程式資料,而第三方清理軟體會(2026-09-25 訂正:
    **macOS 自己清的是 `~/Library/Caches`,不是 `~/.cache`**——所以模型也
    **不該**放進 Caches,那才是系統會在磁碟吃緊時收走的地方,而重下是 1.7 GB、
    還撞到隱私規格那張連網白名單)。"""
    from winkit import plat as winkit_plat

    return winkit_plat.appdata_base()


# --- 電源:別讓機器在轉檔/錄音中途睡著 -------------------------------------
#
# ⚠️ **這一組在 macOS 上原本是「完全靜默的 no-op」**(2026-09-25 查出來):
# `power.keep_awake()` 走 `_set_execution_state()`,非 Windows 直接回 False,
# 而那句 WARNING 又被 `if sys.platform == "win32"` 擋住——也就是防睡眠從來
# 沒有生效過,**而且一個字都不會說**。本機 `pmset` 實測是「閒置 1 分鐘就睡」:
# 使用者按下開始轉檔走開,36 分鐘的轉檔會被打斷;**錄音更嚴重——睡著就是
# 掉音訊,而錄音不能重來**。


def keep_awake_argv(*, display: bool) -> list[str] | None:
    """擋睡眠要下的命令(`None` = 這個平台不從外部命令做)。

    `display=True` 是轉檔那條(連螢幕一起擋,人就在電腦前);`False` 是錄音
    那條——**螢幕照常關**(使用者 2026-07-22 指定:一兩小時的會議沒必要整場
    亮著,而 macOS 的螢幕睡眠不會凍結行程)。

    ⚠️ **`-w <pid>` 不可省**:它讓 `caffeinate` 在**主行程消失時自動退場**,
    正好解掉「硬退出留下殭屍 assertion」——Windows 那邊是靠執行緒退場時旗標
    自動消失達成同一件事(`docs/spec/mac/04` §4.2)。"""
    return _impl().keep_awake_argv(display=display)


def low_power_mode() -> bool | None:
    """這台現在是不是開著「低耗電模式」?問不到(或這個平台沒有這回事)回 `None`。

    起因(2026-09-26 使用者拔掉電源讓我實測):**靠電池本身一毛錢都不花**
    ——單獨的即時轉錄 `fast` RTF 0.275,接電是 0.273。但**低耗電模式讓整台
    機器慢一倍**,於是「錄音中的轉錄 ＋ 講者分析」從 0.605 掉到 **1.080**,
    越過「追得上」那條線:

        爭用下的 fast    接電    電池＋低耗電    電池,低耗電關掉
        各 9 條          0.842      1.178            0.920
        各 5 條(實際)   0.605      1.080            0.571

    ⚠️ **這一支只用來「要不要開錄音中的講者分析」,不要拿去關掉它**
    (使用者 2026-09-26 問過):`pmset` 改設定**一定要 root**(實測
    `'pmset' must be run as root...`),而那是使用者為了續航主動開的開關
    ——程式擅自關掉會在錄音當下更耗電,而且當機/強制結束時**還原不回來**。

    ⚠️ **Windows 回 `None`**:那邊沒有這個開關(省電模式的機制不同,而且
    那條路走 GPU、本來就不受這個判斷影響)。"""
    return _impl().low_power_mode()


def desktop_dir():
    """桌面的實際位置(一定回得出一個 `Path`)。

    ⚠️ **Windows 上不可以寫死 `~/Desktop`**:OneDrive 的「資料夾備份」會把桌面
    整個重導走,而寫死的那條往往**還在、只是沒人看**——捷徑建立成功,使用者卻
    永遠找不到。那邊因此走 `SHGetKnownFolderPath`,問不到才退回猜。"""
    return _impl().desktop_dir()


def start_menu_programs_dir():
    """「開始功能表\程式集」;`None` = **這台機器沒有這個東西**,不是失敗。

    ⚠️ **macOS 回 `None` 是【不成立】不是【待做】**:對應物是 `/Applications`,
    而那是「把 `.app` 放進去」不是「放一個捷徑」,交付路徑完全不同
    (`docs/spec/mac/06` §6.4)。呼叫端(`scripts/make_shortcut.py`)本來就把
    `None` 當成「少了不影響工具能不能用」。"""
    return _impl().start_menu_programs_dir()


def prevent_auto_sleep(*, display: bool) -> Outcome:
    """叫系統**不要自動**睡著(`display=True` 連螢幕一起擋)。

    ⚠️ **這一支與 `keep_awake_argv` 是並用的兩條路,不是二選一**:Windows 從
    行程內下旗標(`SetThreadExecutionState`),macOS 起一支 `caffeinate` 子行程。
    所以非 Windows 回的是 `unsupported`——**那不是壞掉,是那個平台不從這條路做**。

    ⚠️ **Windows 那份是「綁呼叫執行緒」的狀態**:執行緒一退場旗標就沒了。
    錄音那條因此必須由自家長駐執行緒持有(見 `power.stay_awake_begin`),而
    `allow_auto_sleep()` 也**必須在設旗標的同一條執行緒上呼叫**。"""
    return _impl().prevent_auto_sleep(display=display)


def allow_auto_sleep() -> Outcome:
    """把上面那個狀態還給系統(⚠️ 要在設它的那條執行緒上呼叫)。

    ⚠️ **只有在 `prevent_auto_sleep` 回 `ok` 時才呼叫**:沒設過就去清,等於
    把別人設的狀態一起清掉。"""
    return _impl().allow_auto_sleep()


def keep_running_in_standby(reason: str) -> tuple[Outcome, object]:
    """要求「系統進入待命之後,讓**這個行程**繼續跑」;回 (結果, 還原用的權杖)。

    ⚠️ **這與 `prevent_auto_sleep` 管的不是同一件事**:那一支擋的是「自動」
    睡著,而使用者**主動**闔蓋/鎖屏擋不住(也不該擋)。Windows 的 Modern
    Standby 進去之後會把行程**整個凍結**——實測一支轉檔,牆鐘 63.1 分鐘裡有
    40.1 分鐘完全停擺,而症狀長得跟當機一模一樣(`docs/dev/runtime.md`)。

    ⚠️ **macOS 回 `unsupported`,而且那是量過的不是推論**(2026-09-25,
    `scripts/probe_appnap.py`):那邊最接近的東西是 App Nap,而它連「閒著、
    最小化、切到別的 App、四分鐘」都沒有挑中這支程式。睡眠本身由 `caffeinate`
    擋,闔蓋擋不住是硬限制(`docs/spec/mac/04` §4.7)。

    ⚠️ **拿不到絕不能讓轉檔/錄音跑不起來**:呼叫端只記一行就繼續。"""
    return _impl().keep_running_in_standby(reason)


def stop_keeping_running_in_standby(token: object) -> None:
    """撤銷上面那個請求(權杖是 `None` 就什麼都不做)。

    ⚠️ **絕不拋**:它跑在「工作已經做完」之後的 `finally` 裡。"""
    _impl().stop_keeping_running_in_standby(token)


def lower_own_priority() -> tuple[Outcome, object]:
    """把**本行程**降到背景優先權;回 (結果, 還原用的權杖)。

    ⚠️ **這與 `spawn_kwargs(low_priority=True)` / `apply_own_low_priority()`
    是三件不同的事,不准合併**:那兩支管的是**子行程**(建立時下旗標 / 子行程
    自己 nice),這一支是對**已經在跑的自己**下手——用在檔案轉檔那條,因為
    轉錄與講者分析當年都還在主行程的執行緒裡。

    ⚠️ **macOS 回 `unsupported`**:`os.nice()` 降得下去卻**還原不回來**
    (非 root 只能把 nice 值往上加),而這一支的合約是「離開必定還原」。
    而且那邊不需要——檔案轉檔的重活早就在 `transproc` / `diarproc` 子行程裡,
    那兩支起來時自己就 nice 過了。

    ⚠️ **絕不可套用到現場收音**:收音執行緒被排擠正是 2026-08-03 掉了 4.6
    分鐘音訊的那個災情(那是 Windows,而 macOS 的失敗形態更糟——見
    `capture_blocksize`)。守門在 `tests/test_plat.py` 的 AST 掃描。"""
    return _impl().lower_own_priority()


def restore_own_priority(token: object) -> None:
    """還原成降之前**那個值**(權杖是 `None` 就什麼都不做)。

    ⚠️ **不可以寫死還原成「一般」**:使用者可能自己用工作管理員把整支程式
    設成別的優先權,轉完檔擅自拉回一般等於把他的設定改掉。"""
    _impl().restore_own_priority(token)


# --- 系統音訊 -------------------------------------------------------------
#
# ⚠️ **這是整層裡唯一一個必須回結構的能力**(spec/mac/02 §2.3):它要能分出
# 「這台 macOS 版本太舊」「沒給權限」「拿到了」——那三種的**處置完全不同**,
# 而混成同一句話等於把一個三十秒能解的問題講成只能放棄。


def system_audio_capability() -> Outcome:
    """現在能不能錄到系統聲音。

    ⚠️ **`failed` 與 `unsupported` 在這裡的差別特別大**:前者是「去系統設定
    把權限打開、重開程式」(使用者三十秒能自己解),後者是「這台機器做不到」。
    ⚠️ **回 `ok` 不保證真的有聲音**:權限的失敗是**完全靜默的**(tap 建得
    起來、裝置在清單裡、長度正確,就是一整段零),而 TCC 授給的是**發起呼叫
    的那個 App**、執行中的行程拿的是**啟動當下的快照**——所以「說有權限卻
    錄到零」是會發生的,最後一道靠 `record` 那邊的全靜音偵測。"""
    return _impl().system_audio_capability()


def apple_speech_helper() -> tuple[Outcome, object]:
    """Apple 語音辨識小程式(`packaging/mac/apple_asr.swift` 編出來的那支)在哪。

    回 `(Outcome, 路徑)`;不是 `ok` 時路徑是 `None`、理由照原樣給畫面。只回答
    「這一份有沒有附那支、找不找得到」——這台 macOS 夠不夠新、有沒有繁中語音
    資料,要問那支小程式本身(`transcribe_apple.capability`),那是引擎的事
    (spec/mac/02 §2.7:推論引擎不進這一層)。"""
    return _impl().apple_speech_helper()


def open_system_audio():
    """開一個系統聲音來源;`None` = 這個平台不從這條路走(Windows 用 loopback)。

    ⚠️ **回傳的東西一定要 `close()`**:macOS 那邊會在 Core Audio 裡註冊一個
    聚合裝置,它**活得比這個行程久**——不清就一個一個堆在使用者的音訊設定裡。"""
    return _impl().open_system_audio()


# --- 自動更新 -------------------------------------------------------------
#
# 檢查新版是共用的(`update.py`);**換版**兩個平台完全不同:Windows 是「關掉之後由
# 另一支小程式覆蓋工具資料夾、`uv sync`、再開」,macOS 是整個換掉 `.app`、重新打開,
# 之後由 App 自己的升級流程(`make_mac_app.py` 的 setup.zsh)把程式複製出來、`uv sync`。
# 做不到的機器回 `unsupported`/`failed`,畫面改給「前往下載頁」——**不可以假裝換好了**。


def update_asset() -> str | None:
    """這台要下載 Release 上的哪一個附檔(`update.WIN_ASSET` / `MAC_ASSET`);沒有就是 None。"""
    return _impl().update_asset()


def self_update_capability() -> Outcome:
    r"""這個平台能不能在程式裡直接換版(決定對話框的按鈕是「立即更新」還是「前往下載頁」)。

    ⚠️ **`failed` 與 `unsupported` 要分清楚**:Mac 上「App 還在『下載項目』裡直接開」是
    使用者三十秒能自己解的(拖進「應用程式」),理由會照原樣顯示在對話框上。"""
    return _impl().self_update_capability()


def stage_update(zip_path, version: str, into):
    r"""驗包、解壓到 `into`,回傳準備好的新版(Windows 是資料夾、Mac 是 `.app`)。
    形狀或簽章不對就丟 `ValueError`(繁中)。在背景執行緒跑,不碰 Tk。"""
    return _impl().stage_update(zip_path, version, into)


def apply_update(staged, target, version: str) -> Outcome:
    r"""把換版小程式叫起來,**之後呼叫端要立刻關掉本程式**(小程式在等它結束)。

    `staged` 是 `stage_update` 回傳的新版、`target` 是工具資料夾(Mac 用不到:要換的是
    正在跑的那個 `.app`,從執行檔的位置自己找)。
    ⚠️ 回 `ok` 只代表小程式起來了;換不換得成功要等本程式關掉之後才知道,
    由小程式自己跳訊息框講。"""
    return _impl().apply_update(staged, target, version)


def update_restart_note() -> str | None:
    """「立即更新」那一扇底下那一句(換版時畫面上會發生什麼)。None = 用畫面層的預設。"""
    return _impl().update_restart_note()


def update_manual_note() -> str | None:
    """自動更新走不了時,教人手動更新的那一句。None = 用畫面層的預設(Windows 的做法)。"""
    return _impl().update_manual_note()


# --- COM(只有 Windows 有這回事)------------------------------------------


def co_initialize_ex() -> int | None:
    """在**目前這條執行緒**上初始化 COM;回**無號** HRESULT,`None` = 這個
    平台沒有 COM 這回事。

    ⚠️ **這一層只負責那一次系統呼叫**,「哪些 HRESULT 算成功」「每條執行緒
    記一次」「為什麼非做不可」全部留在 `record._ensure_com`——那是錄音領域
    的知識(2026-08-05 那條「第二次按開始錄音就說找不到麥克風」的災難鏈),
    不是平台知識。
    ⚠️ **不回 `Outcome`**:它不是使用者處置得了的東西。失敗時記一行警告、
    讓接下來那個 soundcard 呼叫自己炸——那裡的繁中訊息更貼近使用者當下在
    做的事。"""
    return _impl().co_initialize_ex()


# --- 視窗外觀 -------------------------------------------------------------


def round_window_corners(child_id: int, width: int, height: int,
                         radius: int) -> None:
    """把一個 override-redirect 的 toplevel 切成圓角;做不到就什麼都不做。

    ⚠️ **這是純裝飾,失敗不得有任何後果**——下拉清單照樣能用,只是方角。
    ⚠️ **非 Windows 是「不必做」不是「做不到」**:Aqua 的下拉本來就是圓角,
    所以那兩份實作**安靜地什麼都不做**。這條路每 `<Map>` 一次就走一次(清單
    的高度隨選項數變),留一行記錄就是在每一次點開下拉時往記錄檔倒垃圾。

    `child_id` 是 Tk 那個子視窗的 id(`winfo id`),取真正的視窗控制代碼是
    平台的事。`width`/`height`/`radius` 都是像素、已經乘過 DPI。"""
    return _impl().round_window_corners(child_id, width, height, radius)


def set_window_icon(window, ico_path, png_path):
    """設主視窗的圖示。**回傳呼叫端必須留著參照的東西**(沒有就 `None`)。

    ⚠️ **兩個平台走的是兩支不同的 Tk 介面,而錯的那一支是「靜默」的**
    (2026-09-28 實測,結論寫進 `docs/spec/mac/08` §8.3):

    | | Windows | macOS(Tk 9.0.4 / Aqua) |
    | --- | --- | --- |
    | `iconbitmap(default=…)` | 有效 | **TclError**(`default=` 是 Windows 的簽章) |
    | `iconbitmap(<檔>)` | — | 不拋例外,**完全沒有效果** |
    | `iconphoto(True, <png>)` | — | **有效**,Dock 圖示真的換掉 |

    ⚠️ **中間那列是這支存在的理由**:`iconbitmap(<檔>)` 在 Aqua 上安靜地什麼
    都不做,所以「沒有例外」不等於「設好了」——判準只能是**拍下來看 Dock**。
    ⚠️ **回傳值一定要被留著**:macOS 那條回的是 `PhotoImage`,沒人指著就被 GC
    掉,而畫面上的症狀是圖示變回空白、**不會有任何錯誤**(同 `desktop._icons`
    那條)。
    ⚠️ **純裝飾,失敗不得有任何後果**——呼叫端 `desktop._apply_window_icon`
    本來就有「失敗只記一行」的兜底,所以這三份**不自己吞例外**(同
    `round_window_corners` 那條:兩層都接會讓真正的成因被吃掉一次)。

    ⚠️ **工作列/Dock 那顆不一定歸它管**:Windows 上走的是另一條(見
    `desktop._apply_window_icon`),macOS 上正式交付是 `.app` bundle 裡的
    `.icns`——這支管的是**開發時直接跑 `python -m meeting_scribe.desktop`**
    的那條路。"""
    return _impl().set_window_icon(window, ico_path, png_path)


def strip_default_menubar(window) -> None:
    """把這個平台自己掛上去的、我們用不到的預設選單列清掉。

    ⚠️ **這支存在的理由只在 macOS**(2026-09-28 拍選單列量到):Tk 在 Aqua 上
    會自己掛一排 **`File` / `Edit` / `Window` / `Help`**,全是英文——那違反
    spec §8(使用者可見訊息一律繁體中文),而且**裡面沒有一項是這個工具的
    功能**,使用者不會去那裡找任何東西。

    ⚠️ **不是把快捷鍵一起拿掉**:`Cmd+C/V/X/A` 與 `Cmd+Q` 是 **Tk 自己綁的**,
    不靠那個 `Edit` 選單——三案各真的按一次驗過(貼得上、結束得了)。
    **這件事一定要實測**:「拿掉編輯選單會不會貼不上」用推的兩邊都說得通,
    而猜錯的代價是使用者在詞表編輯區貼不上東西。

    ⚠️ **Windows 不受影響**:那邊 Tk 根本不掛選單列(`plat_win` 什麼都不做)。
    ⚠️ **純外觀,失敗不得有任何後果**,而且要**安靜**(同 `round_window_corners`)。"""
    return _impl().strip_default_menubar(window)


# --- 機器與本行程的記憶體 --------------------------------------------------


def total_ram_mb() -> int | None:
    """整台機器的實體記憶體(MB);量不到回 `None`。

    給「該分多少記憶體給某個子系統」這種決策用(目前是 OCR 子行程的引擎
    回收門檻,`ocr_worker._memory_limit_mb`)。⚠️ **量不到只是「慢一點」不是
    壞掉**:呼叫端退回保守的下限,所以三份實作都可以安靜地回 `None`。"""
    return _impl().total_ram_mb()


def process_memory_mb() -> float | None:
    """**本行程目前**用掉的記憶體(MB);量不到回 `None`。

    ⚠️ **一定要是「目前」不是「歷史最高」**:唯一的消費者是 OCR 的引擎回收
    守衛(`ocr_worker._over_memory_limit`),而它的整個前提是**重建引擎之後
    數字會降下來**。拿 peak RSS(`resource.getrusage` 的 `ru_maxrss`)會讓守衛
    從第一次觸發之後**每一張圖都重建**——而那看起來只是「怎麼變這麼慢」。

    ⚠️ **兩個平台量的東西刻意不同名同義**(2026-09-26 實測後定案):Windows 是
    工作集(`K32GetProcessMemoryInfo` 的 `WorkingSetSize`),macOS 是**活著的
    堆積**(`malloc_zone_statistics` 的 `size_in_use`)。⚠️ 在 macOS 上不能用
    resident/footprint 那一類——libmalloc 放掉的頁面不還給核心,於是那些數字
    在重建引擎之後**不會降**(實測三輪:781 → 1200 → 1268,而 `size_in_use`
    每輪都回到 23.5)。⚠️ **代價是它系統性地小於該行程自己的實體用量,而且不是
    差一個固定值**:OCR 跑起來之後那個差會從 130 MB 長到 2,800 MB(OpenVINO 現編
    的核心走 mmap,不經 malloc),實測它只跟得上 **0.4 倍**的成長。**所以門檻
    必須跟著平台換**——照抄 Windows 的數字等於把守衛關掉,見
    `plat.ocr_memory_anchors_mb` 與 `docs/spec/mac/05` §5.6c。細節見
    `plat_mac.process_memory_mb` 與 `docs/spec/mac/02`。"""
    return _impl().process_memory_mb()


# --- 原生推論庫的預載 -----------------------------------------------------


def preload_onnxruntime() -> None:
    """在 import 任何吃 onnxruntime 的套件之前,先把**正確的那一份**原生庫
    載進這個行程。做不到就什麼都不做。

    Windows System32 內建一份舊版 `onnxruntime.dll`(API 只到 17),依**名稱**
    解析時它會贏——sherpa-onnx 與 rapidocr 的原生模組要的是較新的 ORT C API,
    綁到舊版直接 segfault。先用**完整路徑**載入 pip 版,後續依名稱解析就綁到
    已經在行程裡的那一顆。⚠️ 這件事順帶把「pip 版 wheel 只有 2.2 MB、其實缺
    core」那個洞在 Windows 上蓋了好幾個月(`docs/spec/mac/07`)。

    ⚠️ **這一支不吞任何例外,呼叫端自己決定**:`diarize._ensure_sherpa` 的
    `except (ImportError, OSError)` 就是靠它往上傳,才把「那台電腦缺 Visual C++
    執行階段」翻成繁中的「AI 元件載入失敗」——那句話是使用者唯一的出路。
    `ocr_worker` 那條相反,它自己包一層吞掉、讓後續的 import 自然報錯。
    ⚠️ **兩邊的語意不同不是巧合,別「順手統一」**(spec/mac/08 §8.5)。

    ⚠️ **非 Windows 是「這個問題不存在」而不是「做不到」,而且要安靜**:
    OCR 的引擎回收每重建一次引擎就會走一次這條路(`ocr_worker._recycle`),
    留一行記錄就是在長批次的記錄檔裡堆一疊「macOS 不必預載」。"""
    return _impl().preload_onnxruntime()


# --- 全機 CPU 忙碌度 -------------------------------------------------------


def system_cpu_ticks() -> tuple[float, float] | None:
    """全機累計的 `(閒置, 總計)` CPU 時間;問不到回 `None`。

    ⚠️ **兩個平台回的都是「累計 tick」,所以呼叫端的 delta 算法完全一樣**
    ——`(現值 − 視窗起點) 的閒置 / 總計` 就是這一窗的忙碌度。單位不同不要緊
    (Windows 是 100 奈秒、macOS 是 1/100 秒),因為**只有比值會被讀出來**。

    ⚠️ **`os.getloadavg()` 試過了,不合用**:2026-09-25 實測,把 10 顆核心
    燒滿 3 秒,`loadavg[0]` 從 1.53 紋風不動——它是一分鐘的指數平均,而這裡
    要的是 30 秒的窗。同一輪 `host_statistics` 給的是閒置 6% → 滿載 100%,
    與 `iostat -c 2` 對得上。**這一欄存在的理由正是分辨「子行程在算」與
    「子行程掛了」,而 loadavg 在那個時間尺度上兩者給同一個數字。**

    ⚠️ **不可以退化成「本行程」的 CPU 時間**:講者分析自 2026-08-03 起跑在
    子行程裡,`time.process_time()` 只算得到自己——那個數字在工作搬走之後
    看起來就一直很閒,而機器其實滿載。為什麼非看全機不可留在
    `power.system_cpu_ticks` 與 `record`(那是錄音診斷的領域知識)。"""
    return _impl().system_cpu_ticks()


# --- 純 CPU 路徑值不值得用大模型 -------------------------------------------


def accurate_min_cores() -> int | None:
    """這個平台上「幾核以上,純 CPU 也預設用精準模型」;`None` = 不從核心數判斷。

    ⚠️ **這條存在的理由是「沒有 GPU = 這台很慢」在 Apple Silicon 上不成立**
    (使用者 2026-09-25:「因為 Mac 沒有 GPU,不能以 GPU 做判斷依據」)。
    `transcribe.default_model_key()` 原本只看 `predicted_device()`,而 macOS
    **根本沒有 GPU 那條路**(CTranslate2 在 arm64 只有 CPU,見 spec/mac/05
    §5.1)——於是不論機器多快,每一台 Mac 都只拿得到 `fast`。

    ⚠️ **Windows 回 `None` 是刻意的,那邊的行為完全不動**:那個「精準慢 4 倍」
    是在 Windows 的純 CPU 機器上量的,而交付給非技術同仁的正是 Windows 版
    ——舊的多核文書機(核心多但每核很慢)會因此慢 4 倍,而他們看不出原因。

    ⚠️ **門檻是量出來的,不是猜的**,數字與量法見 `docs/spec/mac/05` §5.6。"""
    return _impl().ACCURATE_MIN_CORES


# --- 麥克風授權 -----------------------------------------------------------
#
# ⚠️ **這一組存在的理由**:全靜音守衛(`desktop.App._rec_confirm_silent`)擋下來
# 之後要不要留「仍要錄」,取決於**成因**,而那兩種成因的處置完全相反——
#
# | 成因 | 會不會自己好 | 繼續錄的結果 | 該給「仍要錄」嗎 |
# | --- | --- | --- | --- |
# | 沒有麥克風授權 | ❌ 不會 | **保證是一整場靜音** | **不該** |
# | 藍牙沒戴上 / 裝置正在重連 | 可能 | 可能沒事 | 該(誤判的代價是錯過一場會議) |
#
# 2026-09-25 使用者裁定:「這樣提示使用者後,就不應該繼續處理」——而那句話
# 成立的前提是**分辨得出是哪一種**,所以才有這一組。


def microphone_permission() -> Outcome:
    """這個行程現在有沒有麥克風授權?`unsupported` = 這個平台問不到。

    ⚠️ **問到的是「這個行程的 TCC 身分」有沒有授權,不是「這台機器」**:
    同一支程式從終端機跑、從 `.app` 跑,答案可以不一樣(`docs/spec/mac/06`
    §6.4 那條血淚:薄殼 `.app` 的請求根本沒被算到 bundle 頭上)。

    ⚠️ **`failed` 的意思是「使用者三十秒能自己解」**,不是「壞掉」——所以
    呼叫端該做的是把他帶到設定頁,而不是說「無法錄音」。"""
    return _impl().microphone_permission()


def open_microphone_settings() -> Outcome:
    """把系統的「麥克風隱私權」設定頁打開。

    ⚠️ **不可以只叫他「去系統設定找」**:2026-09-25 使用者的原話是「有警告,
    但是我不知道要去開權限」——而 macOS 那一頁藏在「隱私權與安全性」底下要
    往下捲,畫面上沒有任何東西指向它。"""
    return _impl().open_microphone_settings()


# --- 錄音中的講者分析 -----------------------------------------------------


def live_diarize_min_cores() -> int | None:
    """這個平台上「幾核以上,錄音中就順便做講者分析」;`None` = 不從核心數判斷。

    ⚠️ **這一條與 `accurate_min_cores` 判的不是同一件事,別合成一支**:那一支
    問「總時間會不會超過音檔長度的 1.5 倍」,這一支問「**錄音中兩個引擎同時跑,
    即時轉錄還追不追得上**」——後者的失敗代價大得多(積壓會讓散會後等很久),
    而且它旁邊就是收音執行緒。

    **原本的閘門是「轉錄走不走 GPU」**(`live.LiveDiarizer.start`),理由寫在
    `live.py`:純 CPU 機兩者同搶 CPU 會讓即時轉錄落後、收尾反而更久。⚠️ **那是
    在 Windows 的純 CPU 機器上量的**,而 macOS **根本沒有 GPU 那條路**——於是
    不論機器多快,整場的講者分析都被推到停止之後(使用者 2026-09-25 回報的
    「按下停止卡住」主因)。

    ⚠️ **Windows 回 `None`、行為完全不動**:那個「同搶 CPU 會落後」是在那個平台
    量出來的,而交付給非技術同仁的正是它。

    ⚠️ **「同時跑會不會追不上」2026-09-25/26 量完了**(`scripts/bench_live_threads.py`):
    這台 10 核在爭用下的即時轉錄 `fast` RTF 0.605(各 5 條),追得上;而
    `accurate` 1.868 追不上(所以錄音固定 `fast`,見 `recording_plan`),
    低耗電模式下連 `fast` 都要 1.080(所以另有 `low_power_mode` 那道閘門)。
    ⚠️ 【待校準】**門檻本身(10)仍然只是「實測過的那一台」**:8 核機型沒有
    量過,所以**不往下放寬**。⚠️ 第一場真實錄音 2026-09-26 驗收過(`docs/spec/mac/07`
    假設 3)。之後每一場要看記錄檔的兩行:「錄音中的講者切分未啟用(…)」——出現
    就是沒開,括號裡是理由;「收音診斷(麥克風軌):… 不連續 N 次 … 累計補零 X 秒」
    ——**不連續不是 0、或補零開始累積,就是收音被餓到了**,那時要立刻把這個門檻
    設回 `None`。"""
    return _impl().LIVE_DIARIZE_MIN_CORES


def live_cpu_threads() -> int | None:
    """錄音中兩個引擎**各**分幾條執行緒;`None` = 不設上限(用使用者指定的核心數)。

    ⚠️ **這一條只在「兩個引擎都在 CPU 上」時才有意義**,所以呼叫端要先確認
    `live_diarize_min_cores()` 那條路真的會走到(見 `live.recording_plan`)。

    起因(2026-09-26 使用者問「可不可以各 5 條執行緒?或各 4 條?」):預設是
    各 `power.cpu_worker_count()` 條,這台就是 9+9=18 條搶 10 顆核。實測同一個
    60 秒片段:

        各幾條   爭用下的 fast    爭用下的 accurate
        9(預設)     0.842            4.133
        5             0.605            1.868
        4             0.621            1.705

    ⚠️ **5 與 4 之間沒有贏家**(差 9%,另一輪名次還顛倒);選 5 是因為**單獨**
    跑時 5 條比 4 條快(0.393 vs 0.425),而錄音中還是有整段沒有講者分析在跑的
    空檔。⚠️ **這救不了「精準」**——理由與執行緒無關,見 `live.recording_plan`。

    ⚠️ **Windows 回 `None`、行為完全不動**:那邊走 GPU,講者分析拿滿執行緒是
    對的(降了只是純粹變慢)。量法見 `scripts/bench_live_threads.py`。"""
    return _impl().LIVE_CPU_THREADS


def ocr_memory_anchors_mb() -> tuple[int, int] | None:
    """OCR 引擎回收門檻的兩個錨點 `(8GB 機器, 32GB 機器)`;`None` = 照
    `ocr_worker` 自己那組(2000 / 4000,也就是 Windows 的值)。

    ⚠️ **這一條為什麼必須平台化**:門檻是「用量超過多少就把引擎丟掉重建」,
    而**「用量」在兩個平台量的根本不是同一種東西**(`process_memory_mb`:
    Windows 是工作集、macOS 是 libmalloc 的 `size_in_use`)。同一個數字放在
    兩邊,守的是不同的東西——⚠️ 而且差得不是一點:實測 macOS 這個數字只跟得上
    **43%** 的實體用量成長,其餘的走 mmap(OpenVINO 現編的核心與模型檔),
    根本不經過 malloc。**維持同一組數字 = 在 Mac 上把守衛實質關掉**。

    ⚠️ **不是「乘一個係數」**(原本 `ocr_worker` 的 docstring 打算這麼做,
    2026-09-27 量完否決):兩邊的錨點各自是在自己的平台上量出來的常數,而
    係數會讓人以為「量一次就能換算任何門檻」——那個比值本身是這一批圖、
    這一版 OpenVINO 的產物。

    ⚠️ **兩個錨點對應的機器規格(8GB / 32GB)不在這一層**:那是使用者
    2026-08-03 對「願意分多少記憶體給 OCR」的決定,跟平台無關,留在
    `ocr_worker._RAM_FLOOR_GB` / `_RAM_CEILING_GB`。這裡只回「在**這個**平台
    上,那兩台機器各該用多少」。

    數字與量法見 `docs/spec/mac/05` §5.6c 與 `scripts/bench_ocr_memory.py`。"""
    return _impl().OCR_MEMORY_ANCHORS_MB
