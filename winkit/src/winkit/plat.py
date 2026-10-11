"""平台抽象層的對外介面:同一個意圖,每個平台各有一套做法。

**2026-09-24 使用者裁定本包要做成跨平台**(選項 B)。這一層是那件事的落腳處,
設計照抄 `meeting-scribe` 那邊已經跑過、驗過的那一層(它的 `docs/spec/mac/02`
§2.8 寫著怎麼搬):三份實作(`plat_win` / `plat_mac` / `plat_null`),選哪一份
**只在 `_impl()` 一處發生**。

⚠️ **不在 import 時綁成模組層常數**:那樣測試換不回來,而「每一份實作的接線都要在
任何一台機器上測得到」正是這一層存在的理由——Windows 那條路要在 Mac 上驗得到,
反過來也一樣。**不准用 `skipif` 把另一個平台的分支跳掉**(2026-09-27 使用者裁定)。

⚠️ **判準:本包自己的模組會不會 dispatch 它?** 會的住這裡(DPI、游標名、emoji
字型、`local_appdata`……);只有某一個下游需要的(錄音、推論門檻)留在那個下游
自己的 `plat`。整包搬過來等於把領域知識塞進一個 UI 殼。

⚠️ **還沒搬進來的 `sys.platform` 判斷逐檔列在 `tests/test_plat.py` 的
`_NOT_YET_MIGRATED`,那份清單只准變短。**

收了兩批(都是 2026-09-27):**游標名、emoji 字型、`appdata_base`**(從 `meeting-scribe`
的 `plat` 搬來,那邊已經拆掉、只剩轉呼叫)與 **`winui` 的視窗整合**(DPI、工作列、
輸入法、單一實例……;對外的口子仍是 `winui`,這裡只收「怎麼跟作業系統講話」)。
"""

import sys
from dataclasses import dataclass
from typing import Literal

State = Literal["ok", "unsupported", "failed"]


@dataclass(frozen=True)
class Outcome:
    """一個平台能力這次的結果。

    ⚠️ **`failed` 與 `unsupported` 絕不可以混成同一句話**:「這台沒給權限」是
    使用者三十秒能自己解的,「這個平台沒有這個概念」是只能放棄的。兩者都放行,
    但記錄檔要說的話不一樣。`reason` 在這兩種情況下**必填且是繁體中文**——它會
    進記錄檔,而記錄檔存在的唯一理由就是事後有人要看懂發生什麼事。

    ⚠️ 第一階段收的三條都是常數,還用不到它;先放進來是因為下一批(`winui` 的
    DPI、單一實例……)全都要,而從第一天就有名字,比事後再把布林改成三態便宜。"""

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
    from winkit import plat_mac, plat_null, plat_win

    if sys.platform == "win32":
        return plat_win
    if sys.platform == "darwin":
        return plat_mac
    return plat_null


def name() -> str:
    """目前實作的短名(`windows` / `macos` / `null`),給記錄檔與測試用。"""
    return _impl().NAME


def disabled_cursor() -> str:
    """停用中的按鈕要顯示的 Tk 游標名(網頁的 `cursor: not-allowed`)。

    ⚠️ **`"no"` 是 Windows 的 Tk 才有的游標名**:2026-09-24 在 macOS 上,按鈕一建
    起來就 `TclError: bad cursor spec "no"`——而那是整個視窗唯一的按鈕類別,也就是
    **視窗一個元件都建不出來**。
    ⚠️ **Windows 那邊刻意不換成 `notallowed`**:那個名字是 Tk/Aqua 的,現行的 `"no"`
    是已知可用的。平台各給各的,不去統一。"""
    return _impl().DISABLED_CURSOR


def emoji_font() -> tuple[object, tuple[int, ...]]:
    """彩色 emoji 的字型檔(`Path`;`None` = 畫不出來),以及它只吃哪幾個像素尺寸
    (空 tuple = 任意尺寸)。

    ⚠️ **Tk 自己畫不出「透明底」的彩色 emoji**:Windows 上它拿的是單色字形、再染上
    前景色(所以要繞道 Pillow 的 `embedded_color=True`);macOS 上 Tk/Aqua 畫得出
    彩色,**但那塊圖是不透明的**(2026-09-24:分頁列每顆圖示後面都有一塊白方塊)。
    兩個平台都得走 Pillow 那條路。
    ⚠️ **Apple Color Emoji 是點陣字型,只有固定幾個尺寸**(21 與 137 直接
    `OSError: invalid pixel size`)——所以有第二個回傳值:呼叫端挑**大於等於**想要
    那個尺寸的最小一檔畫、再往下縮。Segoe UI Emoji 是向量的,回空。"""
    impl = _impl()
    return impl.EMOJI_FONT, impl.EMOJI_STRIKES


def appdata_base():
    """這個平台放「本機應用程式資料」的目錄(**不含**下游的資料夾名)。

    ⚠️ **只有在 `LOCALAPPDATA` 沒設的時候才會問到這裡**(`paths.local_appdata`):
    那個環境變數同時是測試的隔離鉤子,明設過就必須完全照它。
    ⚠️ **macOS 不可以回 `~/.cache`**:那是 Linux 的 XDG 慣例,macOS 上沒有任何系統
    工具或使用者會去那裡找;**也不可以回 `~/Library/Caches`**——那正是系統在磁碟
    吃緊時會收走的地方,而底下放的是幾 GB 的模型(重下一次還撞到下游的隱私規格)。"""
    return _impl().appdata_base()


# --- 視窗整合(2026-09-27 從 `winui` 搬來)-------------------------------------
#
# ⚠️ **對外的口子仍然是 `winui`**:下游呼叫、測試樁的都是那邊的名字,回傳型別也照舊
# (`bool` / `None` / tuple)。這一層只收「怎麼跟作業系統講話」,`Outcome` 不往外漏。
# ⚠️ **吃 `hwnd_of` 的那幾支**:`winui` 把它自己的 `window_handle` 傳進來,不在這裡
# 另外問——下游的測試樁的是 `winui.window_handle`,自己問就繞過了它們的樁。
# 理由與政策(何時呼叫、為什麼不搶前景、四種否決的做法)一律寫在 `winui`。


def enable_dpi_awareness() -> Outcome:
    return _impl().enable_dpi_awareness()


def set_app_user_model_id(app_id: str) -> Outcome:
    return _impl().set_app_user_model_id(app_id)


def window_handle(root) -> int:
    """工作列與 DWM 認得的那個頂層視窗代號;這個平台沒有、或拿不到都回 0。"""
    return _impl().window_handle(root)


def use_dark_titlebar(root, hwnd_of) -> Outcome:
    return _impl().use_dark_titlebar(root, hwnd_of)


def ime_needs_help() -> bool:
    """這個平台的 Tk 會不會漏交代組字的字型與位置(會 = `winui` 要補)。"""
    return _impl().ime_needs_help()


def set_ime_composition_font(hwnd: int, logfont) -> Outcome:
    return _impl().set_ime_composition_font(hwnd, logfont)


def set_backdrop(root, colour: str, hwnd_of) -> Outcome:
    return _impl().set_backdrop(root, colour, hwnd_of)


def work_area(root, hwnd_of):
    """視窗所在螢幕的工作區 `(左, 上, 右, 下)`;問不到回 None。"""
    return _impl().work_area(root, hwnd_of)


def taskbar_progress(hwnd: int, done: int, total: int) -> Outcome:
    return _impl().taskbar_progress(hwnd, done, total)


def taskbar_finish(hwnd: int, flag: int) -> Outcome:
    return _impl().taskbar_finish(hwnd, flag)


def flash_taskbar(hwnd: int) -> Outcome:
    return _impl().flash_taskbar(hwnd)


def claim_single_instance(name: str):
    """回 `(我是第一個嗎, 要活到行程結束的鎖或 None)`。⚠️ 失敗一律放行。"""
    return _impl().claim_single_instance(name)


def release_single_instance(lock) -> None:
    return _impl().release_single_instance(lock)


def raise_existing_window(class_name: str, title: str) -> bool:
    return _impl().raise_existing_window(class_name, title)


# --- 防睡眠與系統佈景(2026-09-27 從 `power` 與 `winui` 搬來)----------------------


def has_execution_state() -> bool:
    """這個平台擋睡眠是不是走 `SetThreadExecutionState`。⚠️ 給 `power` 分辨「失敗」與
    「這個平台本來就沒有」:前者要喊警告,後者安靜——混成一句,每一台 Mac 開轉檔都會
    在記錄檔裡喊一次「擋不住睡眠」。"""
    return _impl().has_execution_state()


def set_execution_state(flags: int) -> Outcome:
    return _impl().set_execution_state(flags)


def system_theme() -> str | None:
    """作業系統的亮暗模式(`"light"` / `"dark"`);讀不到或這個平台不知道怎麼讀,回 None。"""
    return _impl().system_theme()


# --- 高解析度的皮膚(2026-09-27)------------------------------------------------


def hidpi_image_factor(root) -> int:
    """這個平台把圖片畫在螢幕上時,**一個圖素佔幾個實體像素**;1 = 照實畫。

    ⚠️ **這不是顯示縮放**:Windows 上 Tk 照真實 DPI 畫,而資產本來就按 DPI 選檔,
    所以是 1;macOS 上 Tk 一律「1 圖素 = 1 點」,Retina 螢幕於是把整張皮膚放大兩倍
    (見 `plat_mac`)。回 >1 就表示「這台要餵它 N 倍的 bitmap 才會銳利」。"""
    return _impl().hidpi_image_factor(root)


def make_hidpi_image(root, path, width: int, height: int) -> str | None:
    """把 `path` 那張**高解析度**的圖做成一個 Tk 影像,但**版面尺寸釘死 `width`×`height`**
    (點)。回影像名;這個平台辦不到就回 None,呼叫端退回普通的 `photo`。"""
    return _impl().make_hidpi_image(root, path, width, height)


def set_hidpi_image(root, name: str, path, width: int, height: int) -> bool:
    """把 `make_hidpi_image()` 建出來的那張影像**就地**換成 `path`、版面尺寸
    `width`×`height`(點)。回成不成功。

    ⚠️ **存在的理由是「執行期不可以碰樣式」**:`ttk::style layout` 會發
    `<<ThemeChanged>>`,而那個事件會把整棵樹的顏色重刷(見 `skin.install_sharpener`)。
    換影像的內容則不會,所以樣式一律先配好、之後只走這一支。"""
    return _impl().set_hidpi_image(root, name, path, width, height)


# --- 再開一次 App 時把視窗放回來(2026-10-03)------------------------------------


def restore_on_reopen(root) -> Outcome:
    """使用者**再開一次這支 App**(點 Dock 上的圖示、在「應用程式」裡雙擊)時,
    把縮在 Dock 裡的主視窗放回來。

    ⚠️ **不放回來的症狀是「點了沒反應」**(2026-10-03 在 Mac 上量的,`meeting-scribe`
    的 `docs/spec/mac/07` 假設 4c):App 拿到了前景、選單列換成它的名字,但視窗
    留在 Dock 裡——一般 Mac App 這時會把它放回來,所以使用者會以為壞了。
    Windows 沒有這個事件(再開一次走的是單一實例那條路),回 `unsupported`。"""
    return _impl().restore_on_reopen(root)
