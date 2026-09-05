r"""**這支 app 的皮膚長什麼樣**:有哪幾種按鈕、配哪張皮、什麼字級與內距。

⚠️ **載入器不在這裡**(2026-08-28 搬進 `winkit.skin`):四條來源的順序、sprite 的切
與貼、膠囊內距的兩道收口、快取指紋——那些是**兩支程式都該一樣**的機制。這裡只留
「這一支要哪些樣式」,而那正是它跟另一支必須不一樣的地方。

⚠️ 所以 `apply()` 在這裡是一層轉呼叫,它把**這份規格**交給共用的載入器。桌面版
的用法(`skin.apply`、`skin.RUN_STYLE`、`skin.CTA_STYLE`…)。

⚠️ **顏色也不在這裡**(`winkit.palette`):色票整份是刻意跨專案共用的設計系統
——「兩支程式在桌面上是一套」(使用者 2026-08-26)。這裡只指名要用色票裡的哪個鍵。
"""
import sys
import tkinter as tk

from winkit import skin as _loader
from winkit.skin import Button

# `sprites.json` 的 schema 版本,要與 `scripts/make_skin.py` 的 `SCHEMA_VERSION` 同號
# (`tests/test_desktop.py` 兩邊對釘;那支的檔頭有完整理由)。⚠️ **不從 make_skin import**:
# 讀資產那條路的重點就是**不必有 Pillow**,為了一個整數把相依拉進來等於把那條路廢掉。
# ⚠️ **沒宣告的話共用包就整個不比對**(`getattr(spec, "SKIN_SCHEMA", None)`),而「舊資產
# 配新程式」是可達的、失敗是完全無聲的——底下 `SKIN_FRAMES` / `SKIN_SWAPS` 每加一個
# `Sq.*` 都要跳號,否則舊資產照樣載得起來、那幾顆的底板整個不畫。
SKIN_SCHEMA = 2

# 主要動作鈕的兩張皮(「開始轉檔」「開始錄音」「套用名字並下載」與「停止」)。⚠️ 樣式名**必須以 `.Accent.TButton` 結尾**才繼承得到那組
# 圖片元件——ttk 是照後綴一層層往上找的
RUN_STYLE = "Run.Accent.TButton"
STOP_STYLE = "Stop.Run.Accent.TButton"

# 同樣兩顆,但**坐在視窗底上**那一份(2026-09-01「照網頁版重排」把「聲音→MD」的主要
# 動作鈕搬到了卡片外面)。⚠️ **底板是不透明的**,圓角外側那四個角畫的就是它被畫出來
# 時指定的底色——用錯的症狀是每顆鈕的四個角各有一塊白色方角,看起來像圓角壞掉
# (使用者 2026-09-01 圈出來的)。⚠️ 樣式名一樣**必須以 `.Accent.TButton` 結尾**。
RUN_PAGE_STYLE = "RunPage.Accent.TButton"
STOP_PAGE_STYLE = "StopPage.Run.Accent.TButton"
# 小的實心藍(2026-09-03):「聲紋健檢」——使用者要實心藍,又要與同排的「改掛」(一般按鈕的
# 高度)同高;`RUN_STYLE` 是主要動作鈕的高度,擺在那一排就是一高一矮。字級與內距照
# `CTA_STYLE`(同一排的那一對),底板另一張(`Sq.accent_small`)。⚠️ 樣式名一樣**必須以
# `.Accent.TButton` 結尾**,而且要進 `ACCENT_STYLES`(文字翻白)。
RUN_SMALL_STYLE = "RunSmall.Accent.TButton"

# 線框鈕:「選擇檔案…」與「選擇資料夾…」(兩個分頁各一組)。靜止是白底藍框藍字,滑鼠經過整顆翻成
# 主色藍、文字同一刻翻白(使用者 2026-08-27 分兩次指定,比照 apple.com 那顆「查看
# 價格」;做法照姊妹專案 NotebookLM_OCR 的「瀏覽…」)。⚠️ **只有挑選那兩顆**——
# 哪幾顆刻意不套、以及這裡為什麼與那個專案不同,都在 `scripts/make_skin.py` 的那一段。
# ⚠️ 兩顆共用**同一個樣式**就夠了:它們的內距一樣(那邊要兩份是因為「瀏覽…」與
# 「變更…」大小不同)。內距要自己再設一次——`Cta.TButton` 繼承得到的是 `TButton`,
# **不是** `Small.TButton`(ttk 剝的是後綴不是前綴)。
CTA_STYLE = "Cta.TButton"

# 「跳過命名」:一般按鈕的灰底、主要動作鈕的高度(2026-09-02)。網頁版的「套用名字」與
# 「跳過命名」是同一列、同高的一對(primary / secondary);拿 `Small.TButton` 擺在
# `RUN_STYLE` 旁邊就是一高一矮(使用者 2026-07-24 在網頁版截圖回報過同一件事)。
# ⚠️ 不是強調色(不進 `ACCENT_STYLES`):文字維持深色,停用時照一般按鈕的規矩變灰。
SKIP_STYLE = "Skip.TButton"

# 坐在**視窗底**上的三顆(2026-09-03「文字、圖像→MD」照網頁版重排:選檔鈕列與動作列都在
# 卡片外面):一般鈕(「清空」)、線框鈕(「選擇檔案…」「選擇資料夾…」)、灰底高鈕(「輸出
# 資料夾…」,與「開始轉檔」「停止」同一列同高)。⚠️ 每一種底色各要一份皮——底板不透明,
# 圓角外側畫的是它被畫出來時指定的底色(見 `RUN_PAGE_STYLE` 那段)。
SMALL_PAGE_STYLE = "SmallPage.TButton"
CTA_PAGE_STYLE = "CtaPage.TButton"
SKIP_PAGE_STYLE = "SkipPage.TButton"

# 視窗第一句(程式用途那句副標)。⚠️ 樣式名**必須以 `.Muted.TLabel` 結尾**才繼承
# 得到說明文字的前景色,理由同下面 `Hint.Muted.TLabel` 那段。做法照姊妹專案
# NotebookLM_OCR(使用者 2026-08-27 指定「樣式及大小學那邊即可」):10pt 粗體。
SUB_STYLE = "Sub.Muted.TLabel"

# 把 sv_ttk layout 裡的背景元件換成我們的。第二欄是**要從哪個樣式抄 layout**
# ——主要動作鈕的兩張皮都是從 `Accent.TButton` 複製出來的(兩份各要一份自己的
# layout 才分得開,字級與內距則靠樣式名的後綴繼承)。
#
# ⚠️ **`Treeview` 不在這裡**(2026-08-27 從這份移走):清單的框改由外面那層 Frame
# 畫(`Sunken.TFrame`),捲軸才進得了框裡;它自己那顆 `Treeview.field` 反而要拆掉,
# 否則是框中框——見 `_strip_field`。
#
# 底下那份是自己開 layout 的那幾個 Frame(不從 sv_ttk 抄,因為要的就只有一張底板)。
#
# ⚠️ 三欄綁在一起:誰換 layout、換成哪張圖、以及**沒有皮膚時它自己是什麼顏色**。
# 最後一欄只在 squircle 裝不起來時用得到(那時退回實色的方角矩形)。
#
# ⚠️ **圓角外側的顏色不在這裡**,它畫進圖片本身了(`skingen.plate` 的 `on`)。
# 一度走過另一條路:把樣式的 `background` 設成外側色——ttk 先用那個值填滿整塊、
# 再把九宮格圖畫上去,所以透明的圓角外側露出來的正是它。那條路對 Frame 有效,
# 但**救不了 `Treeview`**:它的 `background` 是列的底色、`fieldbackground` 是空白
# 區的底色,兩個都另有用途。使用者 2026-08-26 第二次圈截圖(清單框的灰方角)就是
# 那條路的漏網之魚,於是整批改成不透明底板。
SKIN_FRAMES = (
    # (樣式, 底板元件, 沒有皮膚時它自己的底色)
    ("Card.TFrame", "Sq.card", "card"),
    ("Drop.TFrame", "Sq.drop", "field"),
    # 命名區的捲動框與名單表共用的那個框(它們在畫面上本來就該長得一樣)。⚠️ **框一律
    # 由這一層畫,不由裡面的控制項自己畫**:`tk.Text` 是 classic 控制項、根本做不
    # 到圓角,而 `Treeview` 做得到卻**不該**做——捲軸是另一個 widget,塞不進
    # Treeview 裡面,框畫在它身上捲軸就只能貼在框外(使用者 2026-08-27 指出清單的
    # 捲軸沒被包進去,對照組正是這個訊息區)。框在外層,兩者就都住得進去。
    # 內距 ≥ 圓角半徑的 0.29 倍,方角才不會伸進弧裡(圖片自帶 SQ_PAD_FIELD = 5px)
    ("Sunken.TFrame", "Sq.tree", "field"),
    # segmented control(2026-09-01「照網頁版重排」):外面一條灰槽、裡面一顆白色的
    # 選中段,兩張都是膠囊。⚠️ **是 Frame 不是 Button**——每一格裡面要放圖示與文字
    # 兩個 Label(它們的基線不同,理由見 `desktop.App._icon_pad`),而 ttk 的 Button
    # 只吃得下一個 image + 一段文字。
    # ⚠️ **膠囊底板的圖高必須精確等於元件高度**,所以用它的 Frame 一定要 `height=`
    # 釘死並關掉傳播(見 `desktop.App._segmented`);漏掉就是垂直重複貼或被裁,兩種
    # 都不報錯、`reqheight` 也看不出來,只有截圖看得到。
    ("Seg.TFrame", "Sq.seg", "btn"),
    ("SegOn.TFrame", "Sq.seg_on", "card"),
    # 子分頁選中的那一顆。⚠️ **2026-09-01 從純色的方角 Frame 換過來**:整個畫面其他
    # 東西都是圓角,只有它是方角灰塊,使用者當場圈出來。同 segmented,用它的 cell
    # 一定要 `height=` 釘死(見 `desktop.App._build_ui`)。
    ("SubOnCell.TFrame", "Sq.subtab", "btn"),
    # 使用說明的目錄(2026-09-03 照網頁版 `#help-nav`):灰槽,與槽裡選中那篇的白膠囊(帶
    # 影子,`make_skin.shadowed_plate`)。⚠️ 用它們的 Frame 寬高都要釘死、關掉傳播(見
    # `desktop.App._help_page`):膠囊那張的影子畫在圖的下緣那一圈,高度跟著內容跑就會
    # 把那一圈拉開;裡面的 Label 只能蓋在膠囊的直邊段(`TOC_INSET` + 圓角以內)。
    ("Toc.TFrame", "Sq.toc", "btn"),
    ("TocOn.TFrame", "Sq.toc_on", "card"),
)


SKIN_SWAPS = (
    ("TButton", None, {"Button.button": "Sq.button"}),
    # 輸入框:「講者人數」與「CPU 核心數」那兩個 Spinbox(2026-09-02 使用者:「講者
    # 人數輸入格子也要有圓角效果,請參考 WEB 介面」)。⚠️ **換的是 `TSpinbox` 那一層**,
    # `Tall.TSpinbox` 靠後綴繼承拿到同一份 layout——ttk 的 layout 與樣式一樣是逐段剝
    # 前綴往上找的。⚠️ 底板是 `block()` 不是膠囊(網頁版量到的是 10px 圓角的方框,不是
    # 藥丸),所以高度不必釘死、由樣式的內距決定(`desktop.App._styles` 那一行)。
    ("TSpinbox", None, {"Spinbox.field": "Sq.field"}),
    # 下拉選單(命名區的名字、核對的改掛給誰、聲紋改名):同一張 `Sq.field`(2026-09-02
    # 使用者:「下拉式選單,也請做圓角效果」——網頁版的下拉輸入格與數字框是同一種 10px
    # 圓角方框)。展開後那個清單是另一個 toplevel,圓角在 `desktop.App._round_popup`。
    ("TCombobox", None, {"Combobox.field": "Sq.field"}),
    # 使用說明的搜尋框(2026-09-03):同一張 `Sq.field`——它與講者人數、命名下拉是同一種
    # 「填一件事」的輸入格,方角的話會是整個視窗唯一一個沒圓角的框。
    ("TEntry", None, {"Entry.field": "Sq.field"}),
    ("Horizontal.TProgressbar", None,
     {"Horizontal.Progressbar.trough": "Sq.trough",
      "Horizontal.Progressbar.pbar": "Sq.pbar"}),
    (CTA_STYLE, "TButton", {"Button.button": "Sq.cta"}),
    (SKIP_STYLE, "TButton", {"Button.button": "Sq.button_run"}),
    (SMALL_PAGE_STYLE, "TButton", {"Button.button": "Sq.button_page"}),
    (CTA_PAGE_STYLE, "TButton", {"Button.button": "Sq.cta_page"}),
    (SKIP_PAGE_STYLE, "TButton", {"Button.button": "Sq.button_run_page"}),
    (RUN_STYLE, "Accent.TButton", {"AccentButton.button": "Sq.accent"}),
    (STOP_STYLE, "Accent.TButton", {"AccentButton.button": "Sq.stop"}),
    (RUN_PAGE_STYLE, "Accent.TButton", {"AccentButton.button": "Sq.accent_page"}),
    (STOP_PAGE_STYLE, "Accent.TButton", {"AccentButton.button": "Sq.stop_page"}),
    (RUN_SMALL_STYLE, "Accent.TButton", {"AccentButton.button": "Sq.accent_small"}),
)


# 每一顆按鈕的**唯一一份**規格。
# ⚠️ 字型原本在三個地方各寫一次:`apply()` 真的設上去的那份、收底板自帶內距的那份
# (`_fit_pill_plates`)、量行高反推樣式內距的那份(`_button_padding`)。三份漂開的
# 症狀是高 DPI 下那顆的膠囊被削平——不報錯、`reqheight` 也看不出來,只有截圖看得到。
# ⚠️ **底板也綁在同一列**:上一版量線框鈕時量的是 `Sq.button`(那時兩張圖同高,所以
# 剛好對),`scripts/make_skin.py` 只要給線框鈕另一個高度,那道收口就會靜默地量錯板子。
# ⚠️ 加一種按鈕皮就在這裡補一列(`tests/test_desktop.py` 與 `PILL_PLATES` 雙向釘著)。
BUTTONS = {
    RUN_STYLE: Button("Sq.accent", 11, "bold", 20, 4, 7),
    STOP_STYLE: Button("Sq.stop", 11, "bold", 20, 4, 7),
    RUN_PAGE_STYLE: Button("Sq.accent_page", 11, "bold", 20, 4, 7),
    STOP_PAGE_STYLE: Button("Sq.stop_page", 11, "bold", 20, 4, 7),
    "Small.TButton": Button("Sq.button", 10, "normal", 10, 1, 3),
    CTA_STYLE: Button("Sq.cta", 10, "normal", 10, 1, 3),
    # 字級、粗細、內距與 `RUN_STYLE` 一模一樣——它們是同一列的一對,差的只有底板顏色
    SKIP_STYLE: Button("Sq.button_run", 11, "bold", 20, 4, 7),
    # 視窗底上的三顆:規格各照卡片上那一顆,只有底板不同
    SMALL_PAGE_STYLE: Button("Sq.button_page", 10, "normal", 10, 1, 3),
    CTA_PAGE_STYLE: Button("Sq.cta_page", 10, "normal", 10, 1, 3),
    SKIP_PAGE_STYLE: Button("Sq.button_run_page", 11, "bold", 20, 4, 7),
    # 字級、粗細、內距與 `CTA_STYLE` 一模一樣——同一排的一對,差的只有底板
    RUN_SMALL_STYLE: Button("Sq.accent_small", 10, "normal", 10, 1, 3),
}

# 這支 app 有哪幾種按鈕皮是**強調色**的(文字要跟著底板翻白 / 停用時翻灰)。
# ⚠️ 名單住在這裡而不是載入器裡:另一支程式可能只有一顆、也可能一顆都沒有。
ACCENT_STYLES = (RUN_STYLE, STOP_STYLE, RUN_PAGE_STYLE, STOP_PAGE_STYLE, RUN_SMALL_STYLE)


def apply(root: tk.Misc, scale: float) -> tuple[str, dict, "_loader.SquircleSkin | None"]:
    """設定字型與佈景,回傳 (字型家族名, 色票, squircle 皮膚)。

    ⚠️ 機制在 `winkit.skin.apply`——這一層只負責把**這支 app 的規格**交出去。
    ⚠️ 規格就是**本模組自己**(上面那幾個常數),所以直接把模組交出去,不必再開一個
    只為了轉手的資料類別。"""
    return _loader.apply(root, scale, spec=sys.modules[__name__])
