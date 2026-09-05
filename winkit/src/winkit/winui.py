r"""Windows 平台整合:DPI、工作列身分、單一實例、視窗落點、工作列進度、深色標題列、
還原那一幀的底色。

**原始出處是 `C:\SOURCE5\Python\NotebookLM_OCR\pdf2ppt_gui_2.py`**(2026-08-26
抄進 MP4-2-SRT,使用者指定「工作列圖示與工作列進度也一起做出來」),2026-08-28 搬進
本包。⚠️ 那時的規則是「**兩支的這一段要一起改**,它們在同一台電腦的工作列上並排,
行為不一致比兩邊都沒做還糟」——**現在它是唯一真值,那條規則自動成立**,不必再靠人
記得同步。

全模組共用一條原則:**純外觀,任何一步失敗就安靜回到舊行為**。這些呼叫沒有一個
值得讓轉檔停下來——一批片跑五小時,不該因為工作列的 COM 物件建不起來而收工。
"""
import ctypes
import sys

from winkit import env_var, host


def theme_env() -> str:
    """佈景覆寫用的環境變數名(`<前綴>_THEME`);不設就跟隨 Windows 的應用程式模式。

    ⚠️ **是函式不是常數**:值要等下游 `bind()` 之後才算得出來,而模組層常數會在
    import 當下就定死——那時前綴還不知道。⚠️ 兩支 app 在同一台機器上不可以同前綴,
    否則覆寫佈景會互相牽動(理由在下游 `brand.ENV_PREFIX` 上方)。"""
    return env_var("THEME")


def enable_dpi_awareness() -> None:
    """讓 Windows 用真實像素畫這個視窗。⚠️ **必須在建立 Tk 之前呼叫**。

    不設的話,Windows 會把整個視窗當成 96dpi 畫完、再**點陣放大**到顯示縮放
    (本機 150%,就是 1.5×),字的邊緣全是鋸齒——那跟字型無關。

    設了之後 Tk 量到的是真實 DPI(NotebookLM_OCR 實測 95.9 → 143.9),**用點數
    指定的字型會自己換算成正確的像素高**,但**寫死的像素數字不會**(geometry、
    padx、欄寬…)——那些一律要過縮放倍率,否則 150% 下整個版面會縮成 2/3。
    """
    if not sys.platform.startswith("win"):
        return
    try:                                   # Win8.1+:PROCESS_SYSTEM_DPI_AWARE
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:                               # Win7/8 的舊 API
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass                           # 沒有就算了:只是回到會鋸齒的舊行為


def set_app_user_model_id() -> None:
    """讓工作列用「視窗自己的圖示」,不要沿用啟動鏈上游那支執行檔的。

    ⚠️ **必須在建立第一個視窗之前呼叫**(和 enable_dpi_awareness 一樣):視窗一旦
    建出來,工作列就已經把它歸好隊了。

    NotebookLM_OCR 2026-08-25 實際踩到的症狀是「工作列上的圖示不是我設計的那顆」
    ——標題列那顆是對的,**工作列那顆是 wscript 的**。兩顆走的是不同的路:標題列與
    Alt+Tab 讀的是視窗身上的圖示(`gui._apply_window_icon` 那支 `iconbitmap` 設的),
    工作列則是先把視窗歸到某個 AppUserModelID、再用**那個身分**的圖示。行程沒有
    自己宣告身分時,Windows 會沿用啟動鏈上游的執行檔,而這條鏈是「捷徑 →
    wscript.exe → cmd → uv → pythonw」,於是拿到 wscript 的圖示。

    ⚠️ **這一支只做了一半,另一半是那顆捷徑**(2026-08-27 本專案實測更正)。宣告身分
    只決定「歸到哪一隊」,那個身分的**圖示登記在一顆 .lnk 上**——沒有捷徑就沒有圖示
    可用,使用者看到的就是「ICON 圖像沒有在工具列呈現」。同一天量的證據:視窗的
    `WM_GETICON`(BIG/SMALL/SMALL2)三個都是空的,圖示只掛在**視窗類別**上
    (`GCLP_HICON`/`GCLP_HICONSM`,48×48)——那是 Tk 的 `wm iconbitmap -default` 放的
    位置,標題列讀得到、工作列讀不到。姊妹專案的工作列圖示一直是對的,差別就是它的
    「安裝.bat」會跑 `make_shortcut.py`;本專案 2026-08-27 才補上同一支
    (下游的捷徑腳本,身分值由它讀走寫進 .lnk)。⚠️ **本模組不認得那個值是誰的**
    ——它只從 `winkit.host()` 拿,而那是下游 `bind()` 時給的。
    """
    if not sys.platform.startswith("win"):
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(host().app_id)
    except Exception:
        pass                               # 純外觀,失敗就回到舊行為


def preferred_theme_mode(palettes) -> str:
    """"light" 或 "dark":環境變數優先,否則跟隨 Windows 的「應用程式模式」。

    讀 registry 而不是加一個 darkdetect 依賴——就這一個值,而且它正是 darkdetect
    在 Windows 上讀的那一個。讀不到就當亮色(絕大多數的情況)。"""
    import os

    override = (os.environ.get(theme_env()) or "").strip().lower()
    if override in palettes:
        return override
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        ) as key:
            return "light" if winreg.QueryValueEx(
                key, "AppsUseLightTheme")[0] else "dark"
    except Exception:
        return "light"


def window_handle(root) -> int:
    """視窗真正的 top-level HWND(拿不到回 0)。

    ⚠️ **`winfo_id()` 不是它**:那是 Tk 自己那個**子**視窗,DWM 與工作列都不認
    ——要往上取一層才是工作列上那顆按鈕對應的視窗。

    ⚠️ **`restype` 一定要設**:`ctypes.windll` 的預設回傳型別是 `c_int`(32 位元),
    而 64 位元 Windows 的 HWND 是指標寬。值小的時候看起來完全正常,一旦某次配到
    高位元有值的 handle 就會被**靜默截斷**成另一個視窗的號碼——那種 bug 只會偶爾
    發生一次,查起來毫無線索。
    """
    if not sys.platform.startswith("win"):
        return 0
    try:
        user32 = ctypes.windll.user32
        user32.GetParent.restype = ctypes.c_void_p
        user32.GetParent.argtypes = [ctypes.c_void_p]
        return user32.GetParent(root.winfo_id()) or 0
    except Exception:
        return 0


def use_dark_titlebar(root) -> None:
    """把視窗標題列也換成深色(Windows 10 20H1+ 的 DWM 屬性)。

    不做的話深色介面會頂著一條白色標題列,比整片亮色還醜。失敗就算了。"""
    if not sys.platform.startswith("win"):
        return
    try:
        root.update_idletasks()          # 先讓 HWND 真的存在
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(window_handle(root)), 20,
            ctypes.byref(ctypes.c_int(1)), ctypes.sizeof(ctypes.c_int))
    except Exception:
        pass


# --------------------------------------------------------------------------- #
#  還原那一幀:沒畫到的地方要是底色,不是黑
# --------------------------------------------------------------------------- #
# ⚠️ **Tk 的兩個 window class 都沒有背景 brush**(`TkTopLevel` 與 `TkChild`,2026-08-29
# 實測 `GetClassLongPtrW(..., GCLP_HBRBACKGROUND)` 兩個都回 NULL),而 Tk 又把
# `WM_ERASEBKGND` 攔下來回 1(「背景我自己畫」)。平常沒事——畫面本來就是 Tk 一路畫滿
# 的;但**視窗最小化過一趟之後,client area 的內容是未定義的**,而 Tk 沒有雙緩衝、是
# 一個 widget 一個 widget 畫上去的,於是「還沒輪到的那幾塊」在還原的那 200~300ms 裡
# 是**純黑**,一塊一塊被內容填掉。使用者 2026-08-29 回報的「按工作列把程式叫起來時
# 看得見它重新繪製畫面」就是這個,而**視窗愈大、widget 愈多,黑得愈久**(同日實測:
# MP4-2-SRT 1499×1149、41 個 widget,還原後主執行緒忙 ~150ms;NotebookLM_OCR
# 1139×757、34 個,~79ms——所以那邊也有,只是短到沒被發現)。
#
# 實測(MP4-2-SRT,1521×1205 @150%,連拍螢幕比對相鄰兩幀):還原後 249ms 那一幀,
# 拖放區、清單與兩條卡片標題整片是黑的(單次變化 72% 的畫面);設了 brush 之後同一條
# 路的峰值降到 38%,而且**看不到黑**——沒畫到的地方是底色,像內容還沒進來的骨架。
#
# ⚠️ **量過而否決:`WS_EX_COMPOSITED`**(Windows 對整個視窗做雙緩衝繪製,本來就是為了
# 消除這種逐塊繪製而存在的)。2026-08-29 交錯量 6 次:還原從 244ms 變成 **1162ms**、
# 畫面更新從 4 段變成 16 段——它疊在 DWM 上會讓每次重繪都整片重跑。
#
# ⚠️ **量過而否決:在 `<Map>` 時 `update_idletasks()` 把重繪併成一次。** 主執行緒的
# 忙碌段數確實從 4 降到 2.5,但**連拍到的畫面變化次數反而從 6 次變成 13 次**——那個
# 指標量的是「主執行緒忙不忙」,不是「使用者看到幾次半成品」。⚠️ 這條的教訓比結論
# 值錢:**黑色才是問題,分幾段不是。**
_GCLP_HBRBACKGROUND = -10

# 同一個顏色只建一支 brush。⚠️ **不可以 `DeleteObject`**:handle 掛在 window class 上,
# 而 class 比視窗長壽(行程內所有 Tk 視窗共用它);刪掉之後下一次擦背景畫的是未定義的
# 東西,而那正好又是「只在還原那一瞬間露臉」的那種錯。
_BRUSHES: dict[tuple[int, int, int], int] = {}


def _colorref(r: int, g: int, b: int) -> int:
    """Win32 的 COLORREF 是 `0x00BBGGRR`——**位元組序跟 `#rrggbb` 相反**。

    寫反了沒有錯誤訊息,只是底色變成另一個顏色,而它只在還原那一瞬間看得到。"""
    return (b << 16) | (g << 8) | r


def set_backdrop(root, colour: str) -> None:
    """把 Tk 的 window class 底色設成 `colour`(給的就是視窗自己的背景色)。

    ⚠️ **改的是 window class、不是這一個視窗**:行程內所有 Tk 視窗(含之後才開的
    `Toplevel`)一起生效。這正是要的——它們的底色本來就該是同一個。原生的檔案對話框
    與訊息框是另一個 class(`#32770`),不受影響。

    ⚠️ **要排在 `withdraw()` 之後**:這一支會 `update_idletasks()`(HWND 得先存在),
    而根視窗正是在**第一次 `update_idletasks()` 當下**被貼上螢幕的。

    失敗就安靜回到舊行為(同本模組其餘各支):底色不對只是難看一點。"""
    if not sys.platform.startswith("win"):
        return
    try:
        root.update_idletasks()          # 先讓 HWND 真的存在
        key = tuple(v >> 8 for v in root.winfo_rgb(colour))
        brush = _BRUSHES.get(key)
        if brush is None:
            gdi32 = ctypes.windll.gdi32
            gdi32.CreateSolidBrush.restype = ctypes.c_void_p
            brush = gdi32.CreateSolidBrush(_colorref(*key))
            _BRUSHES[key] = brush
        user32 = ctypes.windll.user32
        # ⚠️ 32 位元的 user32 裡沒有 `SetClassLongPtrW`(那邊它是映射到 `SetClassLongW`
        # 的巨集),而 handle 在 64 位元是指標寬——兩邊都要接得住
        setter = getattr(user32, "SetClassLongPtrW", None) or user32.SetClassLongW
        setter.restype = ctypes.c_void_p
        setter.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        # ⚠️ **兩個 class 都要設**:`TkTopLevel` 是工作列認得的那個、`TkChild` 是
        # widget 真正畫上去的那個(見 `window_handle`),而黑的是哪一層,要看 Windows
        # 那一次走的是哪條擦除路徑
        for hwnd in (window_handle(root), root.winfo_id()):
            if hwnd:
                setter(ctypes.c_void_p(hwnd), _GCLP_HBRBACKGROUND,
                       ctypes.c_void_p(brush))
    except Exception:
        pass


# --------------------------------------------------------------------------- #
#  視窗落點:每次啟動都在同一個地方
# --------------------------------------------------------------------------- #
# ⚠️ **`geometry()` 只給 `WxH`、不給 `+x+y`,落點就不是「置中」也不是「上次那裡」**
# ——Tk 把位置交還給 Windows,而 Windows 走的是 `CW_USEDEFAULT` 的**層疊**規則:每
# 開一次往右下推一格,推到螢幕邊再從左上重來。使用者 2026-08-28 回報的「啟動位置
# 每次都不同」就是這個,而在那之前**兩支 app 都只設了尺寸**(MP4-2-SRT 的
# `gui.App.__init__`、NotebookLM_OCR 的 `pdf2ppt_gui_2.App.__init__`)。
#
# ⚠️ **策略寫死在這裡、不開參數**:兩支在同一台電腦上輪流開,落點的規矩不一樣就是
# 「這台電腦的兩支程式各有各的脾氣」(同一位使用者 2026-08-26 對色票說過同一句話:
# 「兩支程式在桌面上是一套」)。呼叫端只給尺寸——**視窗多大**才是那支 app 自己的事。

_MONITOR_DEFAULTTONEAREST = 2

# 垂直落點:工作區扣掉視窗以後,**上方分這麼多**。⚠️ **不是 0.5**:幾何正中在眼睛
# 看起來偏低,而且「視窗比工作區小得多」時最明顯——本機 2880×1800 工作區 2880×1704
# (200%)上量到 NotebookLM_OCR 置中 y=271、偏上 y=206。⚠️ 反過來,視窗把工作區塞
# 滿時這個係數幾乎不起作用,而那是對的:MP4-2-SRT 在同一台機器上外框高 1598、工作
# 區只有 1704,兩種算法差 13px。
_TOP_BIAS = 0.38

# 視窗外緣到內容區頂端(標題列 + 上外框),以 96dpi 為單位的**估計值**。
# ⚠️ **只能估**:真值是 `winfo_rooty() - winfo_y()`,而那條要**視窗 map 之後**才
# 算得準(NotebookLM_OCR 實測 map 後是 31,還 `withdraw()` 著的時候讀到的是殘值
# 135)——而我們非在 map 之前擺不可(理由見 `place_window`)。取 32 是往「高估一點」
# 那一邊靠:估多了視窗往上一點點,估少了底端會壓到工作列。
_CHROME_96 = 32


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", _RECT),
                ("rcWork", _RECT), ("dwFlags", ctypes.c_ulong)]


def work_area(root) -> tuple[int, int, int, int] | None:
    """視窗所在螢幕的**工作區**(左, 上, 右, 下;實體像素),取不到回 None。

    工作區 = 螢幕矩形扣掉工作列。⚠️ **要問視窗待的那一個螢幕,不是主螢幕**:接了
    外接螢幕的機器上兩者可以差好幾百像素,而拿它去算「視窗擺哪、最高能多高」時,
    算錯的方向是**視窗有一塊掉到螢幕外**。

    ⚠️ **座標可以是負的**:(0, 0) 是主螢幕的左上角,擺在它左邊或上面的第二螢幕整個
    都在負座標裡。所有算式一律照 `left`/`top` 走,不可以假設它們是 0。
    """
    if not sys.platform.startswith("win"):
        return None
    try:
        user32 = ctypes.windll.user32
        # ⚠️ `restype` / `argtypes` 一定要設,理由與 `window_handle` 同一條:
        # HMONITOR 跟 HWND 一樣是**指標寬**,而 ctypes 的預設回傳型別是 32 位元的
        # `c_int`。被截斷的 handle 不會當場爆炸——`GetMonitorInfoW` 只是回 0,
        # 症狀是「這台機器讀不到工作區」,而且要配到高位元有值才偶爾發生一次。
        user32.MonitorFromWindow.restype = ctypes.c_void_p
        user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        mon = user32.MonitorFromWindow(
            ctypes.c_void_p(window_handle(root) or root.winfo_id()),
            _MONITOR_DEFAULTTONEAREST)
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if not user32.GetMonitorInfoW(ctypes.c_void_p(mon), ctypes.byref(info)):
            return None
        r = info.rcWork
        return r.left, r.top, r.right, r.bottom
    except Exception:
        return None


def _placement(work, w: int, h: int, chrome: int) -> tuple[int, int]:
    """工作區 `work` 裡,內容 `w`×`h`、頂上再加 `chrome` 的視窗該擺在哪。"""
    left, top, right, bottom = work
    outer = h + chrome                   # 使用者看到的視窗總高
    x = left + (right - left - w) // 2
    y = top + int(round((bottom - top - outer) * _TOP_BIAS))
    # ⚠️ 鉗的方向是**保住左上角**:視窗比工作區大時,被切掉的是右下角。反過來(貼齊
    # 右下)會把標題列推出工作區上緣,而**標題列一旦看不見,這個視窗就再也搬不動了**
    # ——那比看不到最下面一列嚴重得多。
    # ⚠️ **右下不必再鉗**:`_TOP_BIAS` 在 0~1 之間,所以裝得下的時候 `y` 本來就
    # ≤ `bottom - outer`(水平置中同理),裝不下的時候下面這個 `max()` 又會蓋過去。
    # 補一道 `min(y, bottom - outer)` 在這條算式底下**永遠不會觸發**,只會讓讀的人
    # 以為它在守某種情況。
    return max(left, x), max(top, y)


def place_window(root, w: int, h: int) -> None:
    """把視窗設成 `w`×`h`(**實體像素**)並擺到工作區的水平正中、垂直偏上。

    取代下游那句只給尺寸的 `geometry(f"{w}x{h}")`(位置沒給就是層疊,見本段開頭)。

    ⚠️ **呼叫時機是 `deiconify()` 之前、而且視窗高度已經定案。** 兩個都會錯:

    - **擺在 `deiconify()` 之後**就是「先出現在 A、再跳到 B」。兩支 app 都刻意把整
      段建介面藏在 `withdraw()` 底下、只讓使用者看到最終那一幀,那個不變式是它們
      花力氣建立的,不要在這裡打破它。
    - **拿還沒定案的高度擺**會讓偏上係數算在錯的數字上:`geometry()` 事後改高度時
      Tk **不動左上角**(視窗只往下長),所以擺完再長高,視窗就整個偏上了
      (NotebookLM_OCR 的初值 460 與 `_fit_window()` 量完的 549 差 89 邏輯像素)。
      那邊的順序要是 `_fit_window()` → `place_window()` → `deiconify()`。

    ⚠️ 拿不到工作區時退回 **Tk 問到的螢幕矩形**,那是**主螢幕、而且沒扣工作列**——
    降級是刻意的(純外觀,不值得讓程式開不起來),而偏上的落點讓它在實務上還是看得
    到整個視窗。真的連這條都炸掉就只設尺寸,位置交還給 Windows。
    """
    try:
        chrome = max(1, int(round(_CHROME_96 * root.winfo_fpixels("1i") / 96.0)))
        work = work_area(root) or (0, 0, root.winfo_screenwidth(),
                                   root.winfo_screenheight())
        x, y = _placement(work, w, h, chrome)
        root.geometry(f"{w}x{h}+{x}+{y}")
    except Exception:
        root.geometry(f"{w}x{h}")


# --------------------------------------------------------------------------- #
#  工作列:使用者切走之後,唯一還看得見的東西
# --------------------------------------------------------------------------- #
# ⚠️ 這一整段的存在理由是「**使用者不會盯著這個視窗看**」,而本專案比姊妹專案更
# 極端:一批 300 部片要跑五、六個小時,是半夜掛著跑的(見 app.note_eta 的
# 「使用者半夜要決定跑不跑得完」)。那段時間視窗裡的清單、進度條、結果列**全部
# 看不見**。Windows 對這件事有兩個原生答案,這裡兩個都用——轉檔中把工作列按鈕
# 本身畫成進度條,收工時閃那顆按鈕。
#
# ⚠️ **絕對不要改成把視窗搶到前景**(`focus_force` / `deiconify` / `-topmost`):
# 使用者這時正在別的視窗打字,搶焦點會把他的按鍵吃掉。而且 Windows 本來就有前景
# 鎖擋著,擋下來的結果**還是閃工作列**——差別只在系統選的閃法比我們吵。

# ITaskbarList3(shell32 內建,Win7 起)。⚠️ vtable 的位置是介面定義的一部分、
# 不會變動:IUnknown 佔 0-2、ITaskbarList 佔 3-7、ITaskbarList2 佔 8,
# ITaskbarList3 自己的方法從 9 開始算。
_CLSID_TASKBARLIST = "{56FDF344-FD6D-11D0-958A-006097C9A090}"
_IID_ITASKBARLIST3 = "{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}"
_VT_HRINIT = 3
_VT_SETPROGRESSVALUE = 9
_VT_SETPROGRESSSTATE = 10
# TBPFLAG(shellapi.h)。⚠️ 這是**位元旗標**不是序號,別自己重排。
TBPF_NOPROGRESS = 0x0
TBPF_INDETERMINATE = 0x1
TBPF_NORMAL = 0x2
TBPF_ERROR = 0x4
TBPF_PAUSED = 0x8

# FlashWindowEx 的旗標。只閃**工作列按鈕**(TRAY),不閃標題列(CAPTION):視窗
# 如果只是被蓋住一半,標題列閃起來很吵而且沒有多給任何資訊。
# TIMERNOFG＝一直閃到使用者把視窗切到前景為止,不必自己算次數。
FLASHW_TRAY = 0x2
FLASHW_TIMERNOFG = 0xC

_taskbar_ptr = None
_taskbar_dead = False        # 建過一次失敗就不再重試(每則進度都重試會很吵)


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]


class _FLASHWINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("hwnd", ctypes.c_void_p),
                ("dwFlags", ctypes.c_uint), ("uCount", ctypes.c_uint),
                ("dwTimeout", ctypes.c_uint)]


def _com_call(ptr, index: int, *argtypes):
    """取 COM 物件 vtable 上第 `index` 個方法,回傳可直接呼叫的函式。

    呼叫慣例是 `fn(ptr, 其餘參數…)` —— COM 的 this 指標要自己帶。"""
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p)).contents.value
    slot = ctypes.cast(vtbl, ctypes.POINTER(ctypes.c_void_p))[index]
    return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(slot)


def _taskbar():
    """取得 ITaskbarList3(第一次呼叫時建立並 `HrInit`)。失敗一律回 None。

    ⚠️ **只能在主執行緒呼叫**:COM 物件綁在建立它的 apartment 上,而
    `CoInitialize` 給的是 STA。本專案所有呼叫點都在 Tk 的主執行緒上(視窗是唯一
    的呼叫端,而它的事件迴圈就是主執行緒),讀子行程輸出的那條執行緒不可以碰它。
    """
    global _taskbar_ptr, _taskbar_dead
    if _taskbar_ptr is not None or _taskbar_dead:
        return _taskbar_ptr
    try:
        ole32 = ctypes.windll.ole32
        # 已經初始化過會回 S_FALSE(1),那不是錯誤,不必也不該去 CoUninitialize
        ole32.CoInitialize(None)
        clsid, iid = _GUID(), _GUID()
        if ole32.CLSIDFromString(_CLSID_TASKBARLIST, ctypes.byref(clsid)) < 0:
            raise OSError("CLSIDFromString")
        if ole32.IIDFromString(_IID_ITASKBARLIST3, ctypes.byref(iid)) < 0:
            raise OSError("IIDFromString")
        ptr = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(
            ctypes.byref(clsid), None, 1,          # CLSCTX_INPROC_SERVER
            ctypes.byref(iid), ctypes.byref(ptr))
        if hr < 0 or not ptr:
            raise OSError(f"CoCreateInstance hr=0x{hr & 0xFFFFFFFF:08X}")
        if _com_call(ptr, _VT_HRINIT)(ptr) < 0:
            raise OSError("HrInit")
        _taskbar_ptr = ptr
    except Exception:
        _taskbar_dead = True             # 純外觀:這台機器沒有就算了
    return _taskbar_ptr


def taskbar_progress(root, done: int, total: int) -> None:
    """把工作列按鈕畫成進度條。`total <= 0` 代表「還不知道總量」(跑馬燈)。

    對應視窗底部那條整批進度條的兩個階段:按下開始到第一支片交貨之間沒有分母
    (第一階段可能要聽二十分鐘),那段用 indeterminate;收到第一則 `deliver`
    之後才有「第幾部 / 共幾部」。"""
    if not sys.platform.startswith("win"):
        return
    try:
        tb = _taskbar()
        hwnd = window_handle(root)
        if tb is None or not hwnd:
            return
        state = TBPF_NORMAL if total > 0 else TBPF_INDETERMINATE
        _com_call(tb, _VT_SETPROGRESSSTATE, ctypes.c_void_p, ctypes.c_int)(
            tb, hwnd, state)
        if total > 0:
            _com_call(tb, _VT_SETPROGRESSVALUE, ctypes.c_void_p,
                      ctypes.c_ulonglong, ctypes.c_ulonglong)(
                tb, hwnd, done, total)
    except Exception:
        pass


def taskbar_finish(root, flag: int) -> None:
    """收工時的工作列狀態。`flag` 是 TBPF 旗標:`NOPROGRESS` 清掉、`PAUSED` 留黃的、
    `ERROR` 留紅的。

    ⚠️ **收的是旗標,不是本專案的收場字串**(2026-08-27 改):哪一種收場配哪個旗標
    是畫面那一層的事(見 `gui.OUTCOMES`),而這裡只管「怎麼跟 Windows 講話」。上一版
    在這裡自己 `.get(state, NOPROGRESS)` 對一次,而結果列那邊的預設是「未知 = 紅」
    ——兩個相反的預設,一個打錯的字串就會得到「紅色結果列 ＋ 乾淨的工作列」。

    ⚠️ **有失敗或中止要「留在那裡」**,不是清掉:那條顏色正是給「還沒切回來的
    人」看的——工作列上一眼就知道這趟不是乾淨完成,不必先切回視窗才發現。下一趟
    按開始會把它蓋掉,關掉視窗也就沒了。

    ⚠️ **`ERROR` / `PAUSED` 要先有長度才看得到顏色**:那兩個狀態只換色、不動
    數值,前一刻若停在 0% 就等於畫了一條看不見的紅線。所以先推到滿格再換色。"""
    if not sys.platform.startswith("win"):
        return
    try:
        tb = _taskbar()
        hwnd = window_handle(root)
        if tb is None or not hwnd:
            return
        if flag != TBPF_NOPROGRESS:
            _com_call(tb, _VT_SETPROGRESSVALUE, ctypes.c_void_p,
                      ctypes.c_ulonglong, ctypes.c_ulonglong)(tb, hwnd, 1, 1)
        _com_call(tb, _VT_SETPROGRESSSTATE, ctypes.c_void_p, ctypes.c_int)(
            tb, hwnd, flag)
    except Exception:
        pass


def _flash_hwnd(hwnd: int) -> None:
    """閃某個 HWND 的工作列按鈕,一直閃到它被切到前景為止。

    ⚠️ **呼叫端負責「已經在前景就不要閃」**:這一支只認 HWND,拿不到那個判斷所需的
    上下文(`raise_existing_window` 找到的是**別的行程**的視窗)。

    ⚠️ **例外自己收掉,不可以讓它冒到呼叫端**:在 `raise_existing_window()` 裡這一支
    是包在那個大 `try` 內的,漏出去的話「閃工作列失敗」會被寫成「**沒找到視窗**」
    ——視窗明明找到了、也 restore 過了,答案卻反過來,而下一步就是多開一個視窗。
    純外觀的失敗只能回到舊行為,不可以改寫別人要吃的語意(全模組同一條原則)。"""
    try:
        user32 = ctypes.windll.user32
        info = _FLASHWINFO(ctypes.sizeof(_FLASHWINFO), hwnd,
                           FLASHW_TRAY | FLASHW_TIMERNOFG, 0, 0)
        user32.FlashWindowEx(ctypes.byref(info))
    except Exception:
        pass


def flash_taskbar(root) -> None:
    """閃工作列按鈕,直到使用者把視窗切回前景。

    ⚠️ **視窗已經在前景就什麼都不做**:人就坐在這個畫面前面,結果列已經把話講完
    了,再閃一次只是噪音(而且前景視窗閃自己在 Windows 上根本看不出來)。"""
    if not sys.platform.startswith("win"):
        return
    try:
        hwnd = window_handle(root)
        if not hwnd:
            return
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        if user32.GetForegroundWindow() == hwnd:
            return
        _flash_hwnd(hwnd)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
#  單一實例(同一個登入 session 只開一個)
# --------------------------------------------------------------------------- #
# ⚠️ **`Local\` 而不是 `Global\`**:這是使用者桌面上的 app,「只開一個」的真正意思
# 是「同一個登入 session 只開一個」——切換使用者、遠端桌面各自開一個才是對的,而
# `Global\` 會把那些也一起擋掉。
_MUTEX_SCOPE = "Local\\"
_ERROR_ALREADY_EXISTS = 183
_SW_RESTORE = 9

# ⚠️ **這個 handle 要活到行程結束**:被 GC 收掉的話互斥鎖跟著消失,下一個實例就會
# 以為自己是第一個。所以存在模組層,不交給呼叫端保管——回傳 handle 的 API 遲早有人
# 寫成 `if claim_single_instance(): ...` 而把它丟掉,而那種壞法**沒有任何徵狀**
# (開發時開一個視窗永遠是對的)。
_instance_lock = None


def claim_single_instance() -> bool:
    r"""搶下「這個登入 session 只有我一個」。回 `True` = 我是第一個,照常開視窗。

    ⚠️ **判斷靠具名互斥鎖,不是「有沒有同名的視窗」**:專案資料夾往往與程式同名
    (下游的 `APP_DIR_NAME`),使用者開著那個資料夾時,檔案總管那個視窗的標題就是
    同一串字——只認標題會把它誤判成「程式已經開著」,於是程式再也啟動不了,而畫面上
    什麼都不會說。名字取自 `host().app_id`:那本來就是「這支程式是誰」的唯一真值,
    與工作列的身分同一份,不會有第二個地方要跟著改。

    ⚠️ **拿不到 Windows API 就一律放行(回 `True`)**:這是便利、不是安全邊界——寧可
    開出兩個視窗,也不要讓程式因為它而開不起來(同本模組「任何一步失敗就回到舊行為」
    那條原則)。
    """
    global _instance_lock
    if not sys.platform.startswith("win"):
        return True
    try:
        # ⚠️ `use_last_error=True` ＋ `ctypes.get_last_error()`:直接呼叫
        # `kernel32.GetLastError()` 讀到的可能是**中間任何一次 ctypes 呼叫**留下的
        # 值,而這裡整個判斷就靠 ERROR_ALREADY_EXISTS 這一個碼。
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        handle = k32.CreateMutexW(None, False, _MUTEX_SCOPE + host().app_id)
        if not handle:
            return True
        if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
            # ⚠️ **搶輸了就把自己這個 handle 關掉**:同名的互斥鎖每開一次就多一個
            # handle,而它要等**最後一個**關掉才真的消失——留著的話,第一個實例收工
            # 之後那把鎖還在,下一次啟動會被自己的殘骸擋在門外。
            k32.CloseHandle(ctypes.c_void_p(handle))
            return False
        _instance_lock = handle
        return True
    except Exception:
        return True


def _release_single_instance() -> None:
    """把互斥鎖放掉。⚠️ **只給測試的復位用**——正常的執行路徑靠行程結束來釋放,
    而 `_instance_lock` 是模組級全域:忘了清會變成跨檔案、依順序才重現的偽失敗。"""
    global _instance_lock
    if _instance_lock is not None:
        try:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(_instance_lock))
        except Exception:
            pass
        _instance_lock = None


def raise_existing_window(class_name: str = "TkTopLevel") -> bool:
    r"""把已經開著的那個視窗給使用者看(最小化或隱藏的話先還原)。回**有沒有找到
    它**——找到了就一定給過使用者看得見的回應(叫到前景,或前景鎖擋下來時閃工作列)。

    ⚠️ **這不牴觸「絕對不要把視窗搶到前景」**:那條管的是**轉檔中與收工時**不可以
    打斷使用者手上的事(見 `flash_taskbar`);這裡是他自己剛按下桌面圖示,而唯一
    合理的回應就是把已經開著的那個視窗給他看——什麼都不做的話,他會以為程式壞了、
    再點兩三次。

    ⚠️ **認的是 class ＋ 標題兩個條件**:同上,只認標題會撈到同名資料夾的檔案總管
    視窗(它的 class 是 `CabinetWClass`,Tk 的頂層視窗是 `TkTopLevel`)。⚠️ class
    名開參數是因為它綁的是**工具包**不是這個包——哪天下游換掉 Tk,改的是呼叫端。
    """
    if not sys.platform.startswith("win"):
        return False
    try:
        user32 = ctypes.windll.user32
        found: list[int] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def _visit(hwnd, _lparam):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(ctypes.c_void_p(hwnd), buf, 256)
            if buf.value != class_name:
                return True
            user32.GetWindowTextW(ctypes.c_void_p(hwnd), buf, 256)
            if buf.value == host().app_title:
                found.append(hwnd)
                return False          # 找到就不必再走完整個桌面
            return True

        user32.EnumWindows(_visit, None)
        if not found:
            return False
        # ⚠️ **兩個拼法都要留著,不可以「順手統一」**:`raw` 是給 `_flash_hwnd()`
        # 塞進 `_FLASHWINFO.hwnd`(那個欄位自己是 `c_void_p`)用的,而 `hwnd` 包成
        # `c_void_p` 是因為底下那幾支 user32 沒設 `argtypes`——`ctypes` 預設把 int
        # 當 32 位元傳,HWND 是指標寬,值大的時候會被**靜默截斷**成另一個視窗的號碼。
        raw = found[0]
        hwnd = ctypes.c_void_p(raw)
        # ⚠️ **「最小化」與「隱藏」兩種都要先 restore**(2026-08-28 更正:原本只判
        # `IsIconic`)。下游的視窗在 `__init__` 全程是 `withdraw()` 的(量好高度才
        # `deiconify()`,否則會先閃一個小畫面),而 `EnumWindows` **會列舉隱藏視窗**
        # ——於是那段期間標題對得上、`IsIconic` 是 0,對隱藏的 HWND 呼叫
        # `SetForegroundWindow` 實測回 1(成功)卻什麼都沒顯示,這一支回報 `True`
        # 而畫面全黑。顯示縮放不在出貨清單裡、皮膚要當場畫時那段可以長達數秒。
        if user32.IsIconic(hwnd) or not user32.IsWindowVisible(hwnd):
            user32.ShowWindow(hwnd, _SW_RESTORE)
        # ⚠️ **`SetForegroundWindow` 會失敗,而且在出貨的那條啟動鏈上是常態**
        # (2026-08-28 實測):Windows 只把前景權給「最後收到輸入事件」的那個行程,
        # 而我們是使用者雙擊之後隔了 `wscript` → `cmd` → `pythonw` 兩層
        # `CreateProcess` 才起來的,沒有人呼叫過 `AllowSetForegroundWindow`。本機
        # `SPI_GETFOREGROUNDLOCKTIMEOUT` 讀到 2147483647(＝前景鎖等於永久)。
        # ⚠️ **四種「用力一點」的做法全部量過而否決**(同日,對一個標題正確的 Tk
        # 視窗):`SetForegroundWindow` 回 0 前景不動;`SW_MINIMIZE`+`SW_RESTORE`
        # 之後再叫仍然回 0;`AttachThreadInput` **本身**就回 0(接不上前景執行緒),
        # 而且它會把兩條輸入佇列綁在一起——前景執行緒卡住時我們跟著卡;
        # `SwitchToThisWindow`(Alt+Tab 用的那支)前景也不動。
        # ⚠️ **`HWND_TOPMOST` → `HWND_NOTOPMOST` 那個 Z 序把戲不用**:它確實抬得動
        # (實測 rank 1 → 0),但行程若在兩次呼叫之間死掉,**別人的視窗就永久卡在
        # 最上層**,而且使用者沒有辦法把它弄回去。
        # 所以叫得動就叫,叫不動就退到閃工作列——那本來就是 Windows 自己在前景鎖
        # 擋下來時做的事,差別只在系統選的閃法比我們吵。
        if not user32.SetForegroundWindow(hwnd):
            _flash_hwnd(raw)
        return True
    except Exception:
        return False


def single_instance_or_raise(class_name: str = "TkTopLevel") -> bool:
    r"""「我該開視窗嗎?」回 `True` = 開;回 `False` = 已經有一個了,而且**已經給
    使用者看過**(叫到前景,或至少閃了工作列)。

    ⚠️ **這一支存在的理由是「找不到視窗」那一格**。`claim_single_instance()` 與
    `raise_existing_window()` 各自只回答一半,而把它們接起來的那三行政策——搶輸之後
    **找不到**既有視窗時該怎麼辦——原本是手抄在每個下游的 `main()` 裡的,兩邊逐字
    相同、卻沒有任何機制偵測分歧(共用包的 `check_downstreams` 只跑各下游自己的
    測試,而那些測試又各自把這兩支樁掉)。2026-08-28 從下游收上來。

    ⚠️ **失效方向往「放行」倒**:鎖被拿走、卻找不到那個視窗時,回 `True` 讓這一份
    照常開。那個狀態是真的到得了的——第一個實例還在 `__init__`、卡在某個訊息框、
    正在 `destroy()` 與行程結束之間,或者根本是個已經沒有視窗的殘留行程(下游在
    daemon thread 裡跑 onnxruntime,直譯器關不掉時就是這一種)。這時候安靜地
    `return 0` 等於「按了圖示什麼都沒發生」,而使用者只會以為程式壞了、再點兩三次;
    多開一個視窗雖然也不對,但他**看得見**、關得掉。這是便利、不是安全邊界。

    呼叫端要守的三件事(2026-08-28 補;三件都是被下游各自獨立踩出來的):

    ⚠️ **一、有資格持鎖的行程,必須保證接下來會開出一個「找得到」的視窗。** 所以
    這一支要排在「這份安裝跑不跑得起來」的檢查**之後**——跑不起來的那一份若先搶到
    鎖、接著跳一個**留在螢幕上的** modal(訊息框的 class 與標題都不是主視窗那一組,
    比對不到),另一份好的副本就從此既搶不到鎖、也找不到視窗可以叫。⚠️ 今天只有一個
    下游有那種路徑,另一個下游安全**是碰巧**(它的啟動期失敗都是行程直接死掉,
    Windows 順手收走 handle)——哪天那邊加一個啟動期的訊息框就是同一顆地雷。

    ⚠️ **二、收到 `False` 要用「正常結束」收工(離開碼 0)。** 這條的成因在本包:
    `winkit.launcher` 產的 `.vbs` 有「非 0 就跳訊息框」與「非正常結束又不到 5 秒
    就用 uv 再跑一次」兩道,踩到第二道會**真的開出第二個視窗**——正好是這一支要
    擋的事。

    ⚠️ **三、內部組成也是契約。** 兩個下游的測試樁的是底下 `claim_single_instance()`
    與 `raise_existing_window()` **兩支零件**,不是樁這一支——只有那樣才走得到第三格
    (樁掉這一支的話,「找不到視窗」那一格在下游永遠測不到),而且有人把那三行抄回
    下游時測試會紅。代價是這一支的**內部**換掉時三個 repo 的測試會一起紅,所以要
    連同下游測試一起改。
    """
    if claim_single_instance():
        return True
    # 用關鍵字傳純粹是可讀性(呼叫點看得出那個字串是什麼)。⚠️ **它不是一道保護**:
    # 這裡原本寫著「替身只吃位置參數時會當場 TypeError」,但兩個下游的替身這一輪
    # 都放寬成 `lambda *a, **k:` 了,那個機制一次都使不出來。要擋「參數換了名字
    # 沒人發現」,守的是本包自己的 `test_the_window_class_is_passed_through`。
    return not raise_existing_window(class_name=class_name)
