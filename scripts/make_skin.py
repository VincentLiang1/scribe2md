#!/usr/bin/env python3
r"""產生視窗介面的 squircle 皮膚資產(`src/meeting_scribe/assets/skin/` 底下那一組)。

    uv run python scripts/make_skin.py

這支是皮膚的**唯一真值**:形狀、圓角半徑、色票、內距全寫在這裡,`assets/skin/`
的 PNG 與 `sprites.json` 都是它的產物。改了顏色或半徑就重跑一次,然後把
`assets/skin/` 整個目錄跟著程式碼一起提交。⚠️ **不要手改產物**——下一次重跑就被
蓋掉(與 `scripts/make_icon.py` 同一條原則)。

⚠️ **這支是可以 import 的,而且執行期真的會被 import**(2026-08-27):顯示縮放對不上
出貨資產的那八檔時,`skin._drawn()` 走 `skin.import_make_skin()` 拿到這支、呼叫
`build_variant()` 就地畫一份,再把成果存進磁碟快取。做法照姊妹專案 NotebookLM_OCR。
⚠️ 所以 **`build_variant()` 與 `pack()` 是對外的介面**,改它們的簽名要一起改
`skin._drawn()` 與 `skin._save_cache()`。⚠️ 也因此 **Pillow 是執行期相依**(理由見 `pyproject.toml`)。

本檔整份**移植自 `C:\SOURCE5\Python\MP4-2-SRT\scripts\make_skin.py`**
(2026-08-29,原生介面遷移的階段 3;那一支自己也是從 NotebookLM_OCR 移植的)。形狀、
超取樣、九宮格那三條地雷、五種縮放的理由全部照搬——**三支程式在同一台電腦的桌面上
並排,長相要是一套**(winkit 的 README 寫的)。這邊自己的差異:
  **拿掉狀態色點**——姊妹專案是清單那一欄的圖片,這裡的名單表沒有那一欄,而定義了
    沒人用的元件跟刻意留的東西長得一模一樣(CLAUDE.md 的殘骸那條)。
  **保留 `Sq.drop`**——「文字、圖像→MD」那一頁要拖放(階段 4),先留著。

形狀、九宮格、膠囊那幾條規則在哪
--------------------------------
**全部搬進 `winkit.skingen`**(2026-08-28):超橢圓為什麼是 n=2.0 的正圓弧、為什麼要
預先渲染成圖、九宮格的三條地雷(中段要夠寬、border 不可超過邊長的一半、先畫 RGB
最後才放 alpha)、膠囊的兩條(border 也不可超過元件高度的一半、圖高必須精確等於元件
高度)——那些是**兩支程式都該一樣**的東西,所以它們是共用包的內容。

⚠️ **動任何一個 `SQ_H_*`、任何一顆按鈕的垂直 padding 或字級之前,先讀 `skingen.pill`
那段**:膠囊把「border 不可超過元件高度一半」從「很遠」變成「只差幾 px」,而超過的
症狀是下半個圓被削平——不當掉、不報錯、`reqheight` 也看不出來,只有截圖看得到。

為什麼要產生八種縮放
--------------------
資產是固定像素,而顯示縮放不是:Windows 給的是 100%/125%/150%/175%/200%,4K 面板上
還有 225%/250%/300%。每一檔各出一組,載入時挑最接近的——⚠️ 這幾檔要**精確**對上那些
設定值,不然內距與圓角會互相錯開,而**膠囊還會被裁**(見 `SCALES` 上方那段:圖高釘死
之後,不匹配從良性變成看得見的瑕疵)。

⚠️ **那八檔不是唯一的來源**:對不上的縮放(自訂縮放、將來的新檔位)走「當場畫」那條
路,照**真實**倍率畫,所以任何 DPI 都畫得對。八檔存在的理由是它最快(實測 11~16ms,
當場畫要 61~118ms)、而且逐位元組驗過。⚠️ **不要因此把 SCALES
砍回五檔**:砍掉的每一檔都是把「讀資產」換成「第一次啟動慢 100ms + 家目錄多一份
快取」,而那三檔正好是 4K 面板上的標準選項。
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

from PIL import Image

from winkit import skingen
from winkit.palette import PALETTES as SKINS
from winkit.skingen import pack, plate, px, shade

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "src" / "meeting_scribe" / "assets" / "skin"

# `sprites.json` 的 schema 版本。⚠️ **這一支自己的號碼,不是共用包的**:共用包那個
# `skingen.SCHEMA_VERSION` 講的是「畫法的格式」,兩個下游共用;而這裡要擋的是**這一支**
# 的舊資產,所以少一個 sprite 就得跳號。`src/meeting_scribe/skin.py` 的 `SKIN_SCHEMA`
# 要同號(`tests/test_desktop.py` 兩邊對釘)。
#
# ⚠️ **理由是「舊資產配新程式」是可達的,而且無聲**:使用者換電腦的方式是複製專案
# 資料夾,只覆蓋 `.py` 而留著舊的 `assets/skin/` 完全做得到。舊檔的每一個 key 都還在、
# `scales` 也一樣,不擋的話 `_from_assets` 會**成功**回傳、`source` 還報 `assets`,接著
# 那幾個 layout 指到不存在的元件——實測 ttk 對這件事毫無反應:不丟例外、widget 照建、
# 記錄檔一行都沒有,畫面上就是那幾顆的底板整個不畫。而那支資產一致性測試只比「資產 ==
# 現在的產生器」,看不到別人機器上的舊資產。
#
# 2 = 2026-09-03 照網頁版收尾:新增 `Sq.toc`、`Sq.toc_on`、`Sq.accent_small`、
#     `Sq.button_page`、`Sq.cta_page`、`Sq.button_run_page` 六個元件。
# 4 = 2026-09-15「要做什麼」那排改成分頁的長相:新增 `Sq.segtab`。
# 5 = 2026-09-15 狀態選擇那三排改成灰槽 ＋ 白膠囊 ＋ 淡影:`Sq.seg` 回來、`Sq.seg_on` 換畫法。
# 6 = 2026-10-07 錄音中的「‖ 暫停錄音」:新增 `Sq.pause_page`。
SCHEMA_VERSION = 6

# 顯示縮放。⚠️ 這幾個值要對上 Windows 顯示設定給得出來的那幾檔。
# ⚠️ **2026-08-27 從五檔加到八檔**(code review 抓到,使用者選定):膠囊的圖高是**釘死**
# 的(見 `pill()`),而 `skin._variant` 挑的是**最接近**的一檔——縮放不在這份清單裡時,
# 實際的字型度量與烘好的圖高就對不上,實測 225%/250% 差 2~3px、300% 差 12px,Tk 當場
# 把下半個圓削平(檔頭第 5 點,不報錯、`reqheight` 也看不出來)。膠囊化之前那個不匹配
# 是良性的:四角九宮格的中段會自己拉伸。4K 面板上 225% 與 250% 是 Windows 11 的標準
# 選項,300% 也是。
SCALES = (1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 3.0)

         # 每個角取樣幾個點(再多肉眼看不出來,只是變慢)

# ---------------------------------------------------------------------------
# 按鈕:**高度**(邏輯 px),半徑不再由這裡指定
# ---------------------------------------------------------------------------
# ⚠️ 膠囊的半徑恆等於圖高的一半(見 `pill()` 與檔頭),所以這裡定的是**高度**,而
# 高度會透過元件的 `height` 把控制項**釘死**。
#
# ⚠️ **釘死不是副作用,是這條路的入場費。** 垂直方向不切九宮格才拿得到完整半圓,
# 而不切的代價是圖高必須精確等於元件高度(檔頭第 5 點)。好處是按鈕高度從此**跨
# DPI 與字型一致**,不再被字型度量牽著走——改版前同一顆鈕的邏輯高度在五個縮放檔
# 之間是漂的(下面那張表)。
#
# ⚠️ **配套:那幾顆按鈕的垂直 padding 必須讓內容矮於這裡的高度**(元件高度取「內容
# 需求」與 `height` 的較大者,內容一旦撐過頭就變成檔頭第 5 點的「裁切」)。
# `skin.apply` 那三行 `st.configure(..., padding=(水平, 垂直))` 因此跟著這裡一起
# 降過,逐檔驗算的餘裕是 3~11 實體像素。**動任何一邊都要重跑那個驗算。**
#
# 取值貼著改版前的自然高度,版面才不會跑掉(2026-08-27 用 `tk scaling` 模擬五個
# 縮放檔各量一次,邏輯 px;⚠️ `tk scaling` 是 dpi/72 而 `App.scale` 是 dpi/96,
# 混用會量到另一個縮放檔的數字):
#   主要動作鈕 41.0 / 40.8 / 40.0 / 40.6 / 40.5  → 40
#   線框鈕與一般按鈕 31.0 / 32.0 / 30.0 / 30.3 / 31.5 → 30
SQ_H = 30             # 線框鈕(挑選那兩顆)與一般按鈕(開啟輸出資料夾/記錄…)
SQ_H_RUN = 40         # 主要動作鈕(開始/停止合成的那一顆)

# segmented control 的狀態選擇(「收音情境」與進階參數對話框的「運算裝置」「模型」那三排)。
# ⚠️ **2026-09-15 灰槽回來了**(使用者選案 C,五案實拍見 `docs/dev/native-ui.md` §15):
# 灰槽 ＋ 選中那格一顆**帶淡影的白膠囊**(iPhone/Mac 內建的分段控制,也是「使用說明」目錄
# 那一套)。2026-09-06 拿掉槽的理由是 `muted` 坐在 `btn` 上過不了 9:1——**這次沒選中的字
# 改用 `ink`**(淺 13.78、深 10.42),那個理由就不成立了,色票一個位元組都沒動。
# ⚠️ **連帶回來的是「選中段的方角不可壓到槽的外弧」那條幾何條件**:Tk 疊不了兩個透明圓角,
# 選中段的圓角外側畫的是槽色,那一塊只要伸出槽的弧就把槽的圓角填平。條件由
# `desktop.SEG_PAD` 撐著、`test_the_selected_segment_never_covers_the_track_corner` 逐檔驗算。
# ⚠️ **兩個高度 37 / 31 都不動**(2026-09-06 選的值):整列高度不變,畫面其他東西一格都
# 不移;31 刻意低於主要動作鈕的 `SQ_H_RUN` 40——選中的那一格只是個選項。⚠️ **兩張都是
# 膠囊**,圖高必須精確等於元件高度(見檔頭第 5 點),元件高度由 `desktop._segmented` 向
# 皮膚問(`_plate_h`)、不是自己算;兩個數字與 `desktop.SEG_H` / `SEG_H_ON` 測試釘著。
SQ_H_SEG = 37         # 灰槽(整列)
SQ_H_SEG_ON = 31      # 選中的那一格(白膠囊 ＋ 淡影)
# 選中那顆膠囊四周留給影子的一圈(邏輯 px):膠囊本體上下左右各縮進這麼多,影子往下偏
# 1、模糊落在那一圈裡。⚠️ 影子的偏移不可以大於它,不然被圖的下緣切掉。
SQ_SEG_INSET = 2
# 「聲音→MD」底下那排子分頁,選中的那一顆(2026-09-01:先前是純色的方角 Frame,
# 而整個畫面其他東西都是圓角——使用者當場圈出來)。⚠️ 高度沿用一般按鈕的 `SQ_H`:
# 它們在畫面上本來就是同一個量級,共用一個數字就是把這件事寫死。
SQ_H_SUBTAB = SQ_H    # 量網頁版得到的(兩排都是 45 實體 px @150%)
# 「要做什麼」那一排選中的那一格(2026-09-15 使用者選案 C:「改成上面兩層的選單樣式」)。
# ⚠️ **高度就是分頁那顆**:使用者要的是「長得跟分頁一樣」,兩個數字各自存在就會各自漂。
# ⚠️ **只給那一排**:「收音情境」與進階參數對話框裡那兩排是另一種長相(`SQ_H_SEG` 那組,
# 同日稍後使用者另外選案)。
SQ_H_SEGTAB = SQ_H_SUBTAB

# 圓角半徑(邏輯 px)。⚠️ 半徑是**新的一把尺**,不要拿版面的 SP_* 那把來湊——
# 間距與圓角在版面上管的是兩件事。⚠️ 只剩**不走膠囊**的那幾張還用得到它(理由見
# 檔頭:它們沒有一種高度)。
SQ_R_BOX = 10         # 清單框、拖放條、訊息區的框
# 使用說明的目錄(2026-09-03 照網頁版 `#help-nav` 的 CSS 定義值):灰槽 `border-radius: 14px`、
# 選中那篇的白膠囊 `10px`。⚠️ `SQ_TOC_INSET` 是膠囊圖左右與下方留給影子的一圈(邏輯 px),
# **必須與 `desktop.TOC_INSET` 一模一樣**(測試釘著):Label 放的位置照它算,對不上就是
# 白色方角壓在圓角上、或影子被 Label 蓋掉。
SQ_R_TOC = 14
SQ_R_TOC_ON = 10
SQ_TOC_INSET = 2
# 卡片(2026-08-26 版面卡片化時加的)。**比它裡面的框大一號**:圓角一層套一層時
# 內外同半徑會看起來內圈比較胖(外圈的曲率被更長的直邊稀釋掉),轉角對不齊。
SQ_R_CARD = 20        # 量他那張參考圖得到的(30 實體 ÷ 150%)
SQ_PB_TH = 7          # 進度條厚度(圓頭)
SQ_MID = 96           # 底板中段的寬度(見檔頭第 1 點,不可以縮回 1px)
# ⚠️ **膠囊的中段要窄得多**(2026-09-01,使用者圈出子分頁那顆的圓角才查出來的):
# 九宮格的中段是 Tk **一格一格重複貼**的,而**元件比整張圖窄的時候,整張圖會被壓縮
# ——圓角跟著縮小**。實測:子分頁「轉檔」那顆只有 106 實體 px 寬,配上 190px 寬的圖
# (中段 96 邏輯 = 144 實體),半徑 22.5 的膠囊被壓成大約 7,看起來就是「四個角有一點
# 點弧的方塊」。⚠️ **同一個坑對按鈕也成立**:視窗拉到最小(760)時,兩顆等寬平分的
# 主要動作鈕各只有 140 實體 px,而它們的圖寬是 200。
# ⚠️ **不可以為此縮到 1px**(檔頭第 1 點:那是幾百次繪製呼叫、視窗要 2.5 秒才畫完)。
# 16 邏輯 px 是照 sv_ttk 自己的做法取的(它的按鈕 sprite 是 20×20 配 border 4,中段
# 12px):一顆 840px 寬的鈕重複貼 32 次,而最窄的膠囊圖寬只剩 84 實體 px。
# ⚠️ **卡片、清單框、拖放條不跟**:它們會被內容撐到幾百 px 高,兩軸都要留大中段
# (那正是 2026-08-26 把第一次繪製從 1,996ms 降到 173ms 的那一條)。
SQ_MID_PILL = 16

# ⚠️ **底板自己要撐出來的內距**:sv_ttk 的按鈕/輸入框圖片是**自帶內距的**,換掉
# 圖片就得把那一份補回來,否則全畫面的控制項一起矮 8px(NotebookLM_OCR 2026-08-26
# 用皮膚開/關逐個量 requested size 量出來的:按鈕差 8×8,輸入框差 10 寬 7 高)。
SQ_PAD = 4            # 按鈕
SQ_PAD_FIELD = 5      # 框類。⚠️ 「比按鈕多 1px 才一樣高」那個理由 2026-08-27
                      # 起不成立了(按鈕的高度改由膠囊釘死);這個值留著是因為
                      # 框類自己需要那一圈內距,不是為了跟按鈕對齊

# 拖放條的虛線。⚠️ **一段的長度必須整除中段**,不然九宮格重複貼的時候接縫處會
# 出現半截的虛線:所以這裡定的是「中段切幾段」而不是「一段幾 px」,週期由
# `mid / SQ_DASHES` 算出來(浮點數沒關係,畫的時候才取整)。
# ⚠️ **一個週期裡實線佔多少**(`SQ_DASH_ON`)不在這裡,它是「虛線長什麼樣」、
# 兩支程式該一樣,所以住在 `skingen`。
SQ_DASHES = 16

# 清單那一欄的狀態色點。⚠️ **為什麼要畫成圖**:ttk 的 Treeview 只認得「整列一個
# 前景色」(tag_configure 是列層級的),做不到「檔名用一般字色、色點用狀態色」。
# 唯一能在列裡放一個獨立顏色的東西是 item 的 `image`,所以色點是圖片,掛在檔名
# 那一欄的最前面(狀態的**文字**還是留在狀態欄)。8px 是量過的:6px 在 100%
# 縮放下看起來像髒點,10px 就開始跟文字搶高度。

















def build_variant(theme: str, scale: float) -> tuple[dict, dict]:
    """畫出一組(某個佈景 × 某個縮放)的所有底板,回傳 (圖片, 元件定義)。

    ⚠️ **每一個元件都要在 `elems` 裡記下自己的 `on`**(它坐在什麼顏色上,見
    `plate`)。那個欄位 ttk 用不到——它進資產是為了讓**測試**驗得到「新增一張底板
    時忘了指定外側色」,而那個漏法的症狀(圓角外側一塊實心方角)只有截圖看得出來。
    `tests/test_desktop.py::test_every_skin_plate_declares_what_it_sits_on` 釘著。"""
    p = SKINS[theme]
    mid = px(SQ_MID, scale)
    imgs: dict[str, Image.Image] = {}
    elems: dict[str, dict] = {}

    def pill(h_logical: int) -> tuple[int, int, float, int]:
        """膠囊的尺寸,綁上這一支的縮放與中段寬度。幾何與那三條地雷見
        `skingen.pill`(垂直不切九宮格、圖高必須精確等於元件高度、border 取 ceil)。

        ⚠️ **中段用 `SQ_MID_PILL` 不是 `SQ_MID`**:元件比整張圖窄的時候整張圖會被
        壓縮、圓角跟著縮小(見那個常數上面那段)。"""
        return skingen.pill(h_logical, scale, px(SQ_MID_PILL, scale))

    def pill_elem(states, h: int, br: int, on: str) -> dict:
        """膠囊的元件定義,補上這一支的內距(`SQ_PAD`)。見 `skingen.pill_elem`。"""
        return skingen.pill_elem(states, h, br, on, padding=px(SQ_PAD, scale))

    def block_elem(states, r: int, pad: int, on: str) -> dict:
        """兩個方向都留中段的那一類的元件定義。見 `skingen.block_elem`。"""
        return skingen.block_elem(states, r, pad, on)

    def block(r: int) -> tuple[int, int]:
        """兩個方向都留中段(卡片、清單框、拖放區),綁上這一支的中段寬度。
        ⚠️ 那筆 1,996 ms → 173 ms 的實測在 `skingen.block`。"""
        return skingen.block(r, mid)

    # ---- 一般按鈕:開啟輸出資料夾/記錄…(線框鈕沿用同一組尺寸,見下一段)----
    # ⚠️ **不描邊**:灰底本身就跟背景分得開,再加一圈線就變成「框中框」
    # ⚠️ **膠囊**(見 `pill()`):`w/h/r/br_` 這一組線框鈕直接接著用——那兩類在畫面上
    # 本來就該一樣高(它們是同一排的四顆鈕),共用一組尺寸就是把這件事寫死。
    w, h, r, br_ = pill(SQ_H)
    for key, fill in (("button-rest", p["btn"]), ("button-dis", p["btn_off"]),
                      ("button-pressed", p["btn_lo"]), ("button-hover", p["btn_hi"])):
        imgs[key] = plate(w, h, r, fill, on=p["card"])
    elems["Sq.button"] = pill_elem(
        [["", "button-rest"], ["disabled", "button-dis"],
         ["pressed", "button-pressed"], ["active", "button-hover"]],
        h, br_, p["card"])
    # 同一顆、但坐在**視窗底**上的那一份(2026-09-03「文字、圖像→MD」照網頁版重排:「清空」
    # 與選檔鈕搬到卡片外面)。⚠️ 底板不透明,圓角外側畫的是 `on`——用卡片那張就是四個白角
    # (2026-09-01 主要動作鈕踩過同一顆)。
    for key, fill in (("buttonpage-rest", p["btn"]), ("buttonpage-dis", p["btn_off"]),
                      ("buttonpage-pressed", p["btn_lo"]), ("buttonpage-hover", p["btn_hi"])):
        imgs[key] = plate(w, h, r, fill, on=p["page"])
    elems["Sq.button_page"] = pill_elem(
        [["", "buttonpage-rest"], ["disabled", "buttonpage-dis"],
         ["pressed", "buttonpage-pressed"], ["active", "buttonpage-hover"]],
        h, br_, p["page"])

    # ---- 線框鈕:「選擇檔案…」與「選擇資料夾…」----
    # ⚠️ **只有挑選那兩顆**(使用者 2026-08-27 分兩次指定,做法整套照姊妹專案
    # NotebookLM_OCR 的「瀏覽…」搬過來):它們是「要轉哪些影片」這條主線的兩個入口,
    # 值得在滑鼠經過時明確地說「按這裡」。⚠️ **「開啟輸出資料夾／記錄…」不要一起
    # 套**——那是跑完之後的分岔,不是主線;同一種強調用在每一顆上就等於沒有強調。
    # ⚠️ 這裡與 NotebookLM_OCR **刻意不同**:那邊只有「瀏覽…」一顆、「變更…」套過
    # 之後被要求還原(「主要焦點只有一個」),但那兩顆在版面上不對稱;這邊是**並排
    # 的一對**,對稱地回答同一個問題(單檔還是整夾),只亮一顆反而像另一顆比較不能
    # 按。別照那邊「還原」回去。
    # ⚠️ **靜止是「白底藍框」不是灰底實心**:底色就是卡片本身,只有一圈線與字是
    # 藍的,滑過去才整顆翻成實心藍、文字同一刻翻白(那是 apple.com 上「查看價格」
    # 那顆的行為,不是「進一步了解」那顆)。
    # ⚠️ 三個藍不可互換:線框走 `cta_fg`(深色要亮一階才讀得到)、翻過去的底走
    # `cta_hi`(兩模式同值),理由都在色票。
    # ⚠️ **2026-09-27 一度把線與字分成兩個鍵**(`cta_line`,深色壓暗一階),為的是
    # Retina 上那圈線看起來比 Windows 重一倍;同一天 winkit 的皮膚改成餵 2 倍的
    # bitmap(`skin._retina`)之後,前提消失、整個收回來了。**不要再分一次**。
    # ⚠️ 線寬跟著顯示縮放走:這一圈是**強調**,200% 下留 1 實體像素會細到看不出
    # 它是個按鈕。
    # ⚠️ **200% 以上改用 1.5 邏輯像素**(2026-09-29 使用者選案 B:「我是想讓線粗
    # 一點」):`round(1 * scale)` 的四捨五入讓各檔位本來就不一致——125% 與 250%
    # 只有 **0.80** 邏輯像素、150% 卻有 **1.33**,而 macOS 的 Retina 走 200% 那一檔、
    # 拿到的是 1.00。使用者在 150% 的 Windows 上看慣了 1.33,換到 Mac 就覺得細。
    # ⚠️ **只動 200% 以上**:150% 那一檔維持 1.33(他的 Windows 是那一檔,不能動),
    # 而 125% 若跟著拉齊會從 0.80 跳到 1.60(一倍粗),用那個縮放的同仁會當場發現。
    # ⚠️ **這一檔 macOS 與 Windows 共用**:高解析的 Windows 筆電(200%↑)也會一起
    # 變粗——那是使用者 2026-09-29 明白選的範圍,不是副作用。
    #
    #     檔位   改前(邏輯像素)   改後
    #     125%   0.80             0.80
    #     150%   1.33             1.33   ← 不動
    #     200%   1.00             1.50   ← Mac 走這一檔
    #     250%   0.80             1.60
    # ⚠️ **判準是 1.9 不是 2.0**:macOS 上 Tk 報的縮放是 **0.999** 不是 1.0
    # (皮膚快取的目錄名就寫著 `light@0.999-sharp2x`),乘上 2 倍 bitmap 之後是
    # **1.998**——寫 `>= 2` 就差這一點點沒進來,而症狀是「資產重產了、快取也清了,
    # 線卻一點都沒變」(2026-09-29 真的卡在這裡一輪)。
    lw = max(1, px(1.5 if scale >= 1.9 else 1, scale))
    imgs["cta-rest"] = plate(w, h, r, p["card"], p["cta_fg"], lw=lw, on=p["card"])
    # 停用:淡框淡字,看得出「這裡本來有顆鈕,但現在按不動」(轉檔中它是鎖著的)
    imgs["cta-dis"] = plate(w, h, r, p["card"], p["line_off"], lw=lw, on=p["card"])
    imgs["cta-pressed"] = plate(w, h, r, shade(p["cta_hi"], -0.12), on=p["card"])
    imgs["cta-hover"] = plate(w, h, r, p["cta_hi"], on=p["card"])
    elems["Sq.cta"] = dict(
        states=[["", "cta-rest"], ["disabled", "cta-dis"],
                ["pressed", "cta-pressed"], ["active", "cta-hover"]],
        border=[br_, 0, br_, 0], width=2 * br_ + 1, height=h,
        padding=px(SQ_PAD, scale), sticky="nswe", on=p["card"])
    # 坐在視窗底上的那一份(2026-09-03,「文字、圖像→MD」的選檔鈕在卡片外面):靜止仍是
    # 白底藍框(白是鈕自己的底,不是卡片的),只有圓角外側換成視窗底色。
    imgs["ctapage-rest"] = plate(w, h, r, p["card"], p["cta_fg"], lw=lw, on=p["page"])
    imgs["ctapage-dis"] = plate(w, h, r, p["card"], p["line_off"], lw=lw, on=p["page"])
    imgs["ctapage-pressed"] = plate(w, h, r, shade(p["cta_hi"], -0.12), on=p["page"])
    imgs["ctapage-hover"] = plate(w, h, r, p["cta_hi"], on=p["page"])
    elems["Sq.cta_page"] = dict(
        states=[["", "ctapage-rest"], ["disabled", "ctapage-dis"],
                ["pressed", "ctapage-pressed"], ["active", "ctapage-hover"]],
        border=[br_, 0, br_, 0], width=2 * br_ + 1, height=h,
        padding=px(SQ_PAD, scale), sticky="nswe", on=p["page"])

    # ---- 版面的卡片 ----
    # ⚠️ **內距給 0**:卡片的內距是版面的一把尺(gui 的 SP_XL,要過 App.px() 跟著
    # 顯示縮放走),不是底板自帶的。按鈕/框類那幾張要自帶內距,是因為它們換掉的
    # sv_ttk 圖片本來就帶著一份(見 SQ_PAD);卡片沒有前身,不必補。
    cr = px(SQ_R_CARD, scale)
    cw, ch = block(cr)      # ⚠️ 卡片很高,見 block() 的說明
    imgs["card-rest"] = plate(cw, ch, cr, p["card"], p["card_line"],
                              on=p["page"])
    elems["Sq.card"] = block_elem([["", "card-rest"]], cr, 0, p["page"])

    # ---- 清單那個框 ----
    # 不吃 focus 那一階(輸入框才需要「游標在我這裡」的提示;清單的焦點由選取列
    # 自己表示,再閃一圈藍邊只是兩個東西同時在講同一件事)
    #
    # ⚠️ **這個框與拖放條各只有一張皮**(2026-08-27 使用者裁決,移除了三張)。上一版
    # 畫了 `tree-dis` / `drop-dis` / `drop-over`,而「拖曳經過時整圈變藍」需要 GUI 在
    # `<<DropEnter>>` / `<<DropLeave>>` 手動 `state(["active"])` 切換——**那幾行從來
    # 沒有寫進 `gui.py`**(全 repo 零命中),而 `_refresh_actions` 只鎖三顆按鈕、不碰
    # 這兩個容器(ttk 的 state 不會從父容器往下傳)。於是那三張圖 × 8 縮放 × 2 佈景
    # = **48 張永遠畫不出來的 sprite** 進了每一份資產。
    # ⚠️ 要加回那個回饋的話,**圖與綁定必須同一批進來**:少了綁定,圖就只是資產裡
    # 的死重量,而且它跟「刻意留著的東西」長得一模一樣(CLAUDE.md 的殘骸那條)。
    br = px(SQ_R_BOX, scale)
    bw, bh = block(br)      # 同上:清單框與拖放區都會被撐高
    imgs["tree-rest"] = plate(bw, bh, br, p["field"], p["line"], on=p["card"])
    elems["Sq.tree"] = block_elem(
        [["", "tree-rest"]], br, px(SQ_PAD_FIELD, scale), p["card"])

    # ---- 拖放條:虛線描邊 ----
    imgs["drop-rest"] = plate(bw, bh, br, p["field"], p["line"],
                              dash=mid / SQ_DASHES, on=p["card"])
    elems["Sq.drop"] = block_elem(
        [["", "drop-rest"]], br, px(SQ_PAD_FIELD, scale), p["card"])

    # ---- 輸入框:「講者人數」與「CPU 核心數」那兩個 Spinbox(2026-09-02)----
    # (使用者:「講者人數輸入格子也要有圓角效果,請參考 WEB 介面」。)網頁版那個框是
    # `gr.Number`,量到的是 10px 圓角的方框、**不是膠囊**——所以跟清單框共用 `SQ_R_BOX`
    # 與 `block()`(兩個方向都留中段,高度由樣式內距決定、不必釘死),外觀也與同一張
    # 卡片上的路徑框(`Sunken.TFrame`)一致:同樣的凹陷底、同樣的一圈線。
    # ⚠️ **兩張皮:靜止與取得焦點。** 焦點那一階是使用者每次點進去都會觸發的(ttk 自己
    # 設 `focus` 狀態),做法照姊妹專案 NotebookLM_OCR 的 `Sq.field`:描邊換成 accent 並
    # 加粗到 2px。停用態**不畫**——這兩個框從不鎖(值在按下「開始」那一刻才讀),照
    # 上面「圖與綁定必須同一批進來」那條,沒有人觸發得了的皮不進資產。
    imgs["field-rest"] = plate(bw, bh, br, p["field"], p["line"], on=p["card"])
    imgs["field-focus"] = plate(bw, bh, br, p["field"], p["accent"],
                                lw=max(2, px(2, scale)), on=p["card"])
    elems["Sq.field"] = block_elem(
        [["", "field-rest"], ["focus", "field-focus"]], br,
        px(SQ_PAD_FIELD, scale), p["card"])

    # ---- 進度條:圓頭的軌道與填充條 ----
    # ⚠️ 高度是**釘死**的(thickness ＝ 圖高),所以九宮格的左右兩塊不會被垂直
    # 拉伸、圓頭不會變形;會被拉開的只有中段那一欄純色。
    th = px(SQ_PB_TH, scale)
    pr = th / 2.0
    pw = int(2 * (pr + 1) + mid)
    imgs["trough"] = plate(pw, th, pr, p["trough"], on=p["card"])
    imgs["pbar"] = plate(pw, th, pr, p["accent"], on=p["trough"])
    edge = int(pr) + 1
    for name, key, on in (("Sq.trough", "trough", p["card"]),
                          ("Sq.pbar", "pbar", p["trough"])):
        elems[name] = dict(states=[["", key]], border=[edge, 0, edge, 0],
                           width=int(2 * (pr + 1) + 1), height=th,
                           padding=0, sticky="nswe", on=on)

    # ---- 主要動作鈕的兩張皮:開始是 Apple 藍、停止是深紅 ----
    # ⚠️ **自己一種高度、自己一張膠囊**(`SQ_H_RUN`):它的字級大一號(11pt 粗體),
    # 而膠囊的半徑恆等於**自己**圖高的一半、圖高又必須等於元件高度——跟一般按鈕
    # 共用一張的話,這顆會把那張圖垂直重複貼、下緣長出第二段圓角。
    # ⚠️ **每一種底色各要一份**(2026-09-01 使用者圈出來的):底板是**不透明**的,
    # 圓角外側那四個角畫的就是 `on` 那個顏色。這顆鈕原本只坐在卡片上,而「照網頁版
    # 重排」把它搬到了卡片**外面**(坐在視窗底 `page` 上)——外側還是白的,畫面上就是
    # 每顆鈕的四個角各有一塊白色方角,看起來像圓角壞掉。
    # ⚠️ **不能改用「把樣式的 background 設成外側色」那條捷徑**,理由見 `plate` 的
    # `on`(那條路救不了 `Treeview`,而且同一張底板從此不能重複用在兩種背景上)。
    # ⚠️ **新增一種擺法就要在這裡多一份**,而漏掉的症狀只有截圖看得到——
    # `tests/test_desktop.py::test_every_plate_sits_on_the_colour_it_was_drawn_for`
    # 走訪真視窗的每一顆 widget、比對底板的 `on` 與它父容器的實際底色,反向釘著。
    rw, rh, rr, rbr = pill(SQ_H_RUN)
    for kind, hover in (("accent", p["accent_hi"]), ("stop", None)):
        for suffix, on in (("", p["card"]), ("-page", p["page"])):
            name = f"{kind}{suffix}"
            imgs[f"{name}-rest"] = plate(rw, rh, rr, p[kind], on=on)
            imgs[f"{name}-dis"] = plate(rw, rh, rr, p["run_off"], on=on)
            imgs[f"{name}-pressed"] = plate(rw, rh, rr, shade(p[kind], -0.12),
                                            on=on)
            imgs[f"{name}-hover"] = plate(rw, rh, rr,
                                          hover or shade(p[kind], 0.10), on=on)
            elems[f"Sq.{name.replace('-', '_')}"] = pill_elem(
                [["", f"{name}-rest"], ["disabled", f"{name}-dis"],
                 ["pressed", f"{name}-pressed"], ["active", f"{name}-hover"]],
                rh, rbr, on)

    # ---- 小的實心藍(2026-09-03):「聲紋健檢」要與同排的「改掛」同高 ----
    # (使用者:「聲紋健檢按鈕改成藍色實心橢圓效果」→「高度請跟前面的改掛相同」。)`Sq.accent`
    # 是 `SQ_H_RUN`(40),擺在一排 `SQ_H`(30)的控制項旁邊就是一高一矮(同「跳過命名」那
    # 段的病);四種狀態的顏色照 `Sq.accent`,只坐在卡片上,所以一份就夠。
    sw, sh, sr, sbr = pill(SQ_H)
    for key, fill in (("accentsmall-rest", p["accent"]), ("accentsmall-dis", p["run_off"]),
                      ("accentsmall-pressed", shade(p["accent"], -0.12)),
                      ("accentsmall-hover", p["accent_hi"])):
        imgs[key] = plate(sw, sh, sr, fill, on=p["card"])
    elems["Sq.accent_small"] = pill_elem(
        [["", "accentsmall-rest"], ["disabled", "accentsmall-dis"],
         ["pressed", "accentsmall-pressed"], ["active", "accentsmall-hover"]],
        sh, sbr, p["card"])

    # ---- 「跳過命名」:一般按鈕的灰底,但要主要動作鈕的高度(2026-09-02)----
    # 網頁版的「套用名字」與「跳過命名」是同一列、同高的一對(primary / secondary),
    # 而 `Sq.button` 是 `SQ_H`、`Sq.accent` 是 `SQ_H_RUN`——同一列一高一矮,正是使用者
    # 2026-07-24 在網頁版截圖回報過的「兩顆一高一低」。它只坐在卡片上,所以一份就夠;
    # 四種狀態的顏色照 `Sq.button`。
    for key, fill in (("buttonrun-rest", p["btn"]), ("buttonrun-dis", p["btn_off"]),
                      ("buttonrun-pressed", p["btn_lo"]), ("buttonrun-hover", p["btn_hi"])):
        imgs[key] = plate(rw, rh, rr, fill, on=p["card"])
    elems["Sq.button_run"] = pill_elem(
        [["", "buttonrun-rest"], ["disabled", "buttonrun-dis"],
         ["pressed", "buttonrun-pressed"], ["active", "buttonrun-hover"]],
        rh, rbr, p["card"])
    # 坐在視窗底上的那一份(2026-09-03):「文字、圖像→MD」的動作列在卡片外面,第三顆
    # 「輸出資料夾…」是灰底、與「開始轉檔」「停止」同高的一顆(網頁版三顆同一列同高)。
    for key, fill in (("buttonrunpage-rest", p["btn"]), ("buttonrunpage-dis", p["btn_off"]),
                      ("buttonrunpage-pressed", p["btn_lo"]),
                      ("buttonrunpage-hover", p["btn_hi"])):
        imgs[key] = plate(rw, rh, rr, fill, on=p["page"])
    elems["Sq.button_run_page"] = pill_elem(
        [["", "buttonrunpage-rest"], ["disabled", "buttonrunpage-dis"],
         ["pressed", "buttonrunpage-pressed"], ["active", "buttonrunpage-hover"]],
        rh, rbr, p["page"])
    # 「‖ 暫停錄音」:**線框鈕、主要動作鈕的高度**(2026-10-07 使用者:灰底「太不明顯」→
    # 黃、橘實心都試過 → 定案「不要橘色的底色,改成有藍色線框的版本」)。坐在視窗底上,
    # 與「■ 停止錄音」同高。四種狀態的畫法照 `Sq.cta_page`(白底藍框、滑過翻實心藍、
    # 停用淡框),線寬也共用那個 `lw`。⚠️ 暫停之後那顆「● 繼續錄音」是**實心**藍
    # (`Sq.accent_page`):線框 → 實心,一眼分得出現在是錄音中還是暫停中。
    imgs["pausepage-rest"] = plate(rw, rh, rr, p["card"], p["cta_fg"], lw=lw, on=p["page"])
    imgs["pausepage-dis"] = plate(rw, rh, rr, p["card"], p["line_off"], lw=lw, on=p["page"])
    imgs["pausepage-pressed"] = plate(rw, rh, rr, shade(p["cta_hi"], -0.12), on=p["page"])
    imgs["pausepage-hover"] = plate(rw, rh, rr, p["cta_hi"], on=p["page"])
    elems["Sq.pause_page"] = pill_elem(
        [["", "pausepage-rest"], ["disabled", "pausepage-dis"],
         ["pressed", "pausepage-pressed"], ["active", "pausepage-hover"]],
        rh, rbr, p["page"])

    # ---- 子分頁選中的那一顆(灰膠囊,坐在視窗底上)----
    uw, uh, ur, ubr = pill(SQ_H_SUBTAB)
    imgs["subtab"] = plate(uw, uh, ur, p["btn"], on=p["page"])
    elems["Sq.subtab"] = skingen.pill_elem(
        [["", "subtab"]], uh, ubr, p["page"], padding=0)

    # ---- segmented control 的狀態選擇:灰槽 ＋ 選中那格的白膠囊(帶淡影)----
    # 2026-09-01 這一排是「照網頁版重排」那一批的核心(原生版原本是方角小標籤與下拉
    # 選單,使用者說「沒有膠囊圓角效果」「選單也不直覺」),當時做成灰槽 + 白色選中段;
    # 2026-09-06 為了 `muted` 在灰槽上的對比拿掉槽、改成白膠囊 ＋ `accent` 描邊(選案 H)。
    # ⚠️ **2026-09-15 灰槽回來**(使用者選案 C):沒選中的字改用 `ink`,拿掉槽的理由就不
    # 成立了;描邊換成影子——槽是灰的,白膠囊本身就分得出來,不必再靠那一圈線。
    # ⚠️ **兩張的 `on` 不同**:槽坐在卡片上(外側畫卡片白),膠囊坐在**槽**上(外側畫槽色)。
    # 弄錯的症狀是一圈白/灰方角,不當掉、不報錯。膠囊外側那一塊能不能伸出槽的弧,是
    # `desktop.SEG_PAD` 的事(見 `SQ_H_SEG` 上面那段)。
    # ⚠️ **內距一律 0**(不走 `pill_elem` 那個帶 `SQ_PAD` 的區域 helper):這兩張貼的是
    # **Frame** 不是按鈕,而 Frame 裡面的東西自己排——底板再自帶一份內距,只會把
    # 「內容需求」灌高、把膠囊那條「內容不可以撐過圖高」的餘裕吃掉。
    tw, th, tr, tbr = pill(SQ_H_SEG)
    imgs["seg"] = plate(tw, th, tr, p["btn"], on=p["card"])
    elems["Sq.seg"] = skingen.pill_elem([["", "seg"]], th, tbr, p["card"], padding=0)
    # 影子照「使用說明」目錄那顆(往下 1、模糊 3),濃度 16% 是出案時使用者看的那一版。
    # ⚠️ **上下左右都縮進**(`top=inset`,目錄那顆上緣貼齊):膠囊要在槽裡垂直置中,
    # 只縮下緣的話上窄下寬,差一圈看得出來。九宮格的邊仍是 `ceil(H/2)`——本體縮進
    # `inset`、半徑少 `inset`,弧的右端正好還在 `H/2` 以內。
    ow, oh, or_, obr = pill(SQ_H_SEG_ON)
    inset = px(SQ_SEG_INSET, scale)
    imgs["seg-on"] = shadowed_plate(ow, oh, oh / 2.0 - inset, p["card"], p["btn"], inset,
                                    dy=px(1, scale), blur=px(3, scale) / 2, alpha=0.16,
                                    top=inset)
    elems["Sq.seg_on"] = skingen.pill_elem(
        [["", "seg-on"]], oh, obr, p["btn"], padding=0)

    # ---- 「要做什麼」選中的那一格:分頁那顆灰膠囊,但坐在**卡片**上 ----
    # (2026-09-15 使用者選案 C。)形狀與顏色同 `subtab`,差別只有 `on`:分頁坐在視窗底,
    # 這一排坐在白卡片上——拿 `Sq.subtab` 直接用的話,四個角各露出一塊視窗底的淺灰方角。
    # ⚠️ **圖與 `button-rest` 逐像素相同,但元件要自己一個**:`Sq.button` 帶著按鈕的
    # `SQ_PAD` 內距與四種狀態,貼在 Frame 上只會把內容需求灌高、吃掉膠囊那條「內容不可以
    # 撐過圖高」的餘裕(同 `seg-on` 那段的內距 0)。
    gw, gh, gr, gbr = pill(SQ_H_SEGTAB)
    imgs["segtab"] = plate(gw, gh, gr, p["btn"], on=p["card"])
    elems["Sq.segtab"] = skingen.pill_elem(
        [["", "segtab"]], gh, gbr, p["card"], padding=0)

    # ---- 使用說明的目錄(2026-09-03 照網頁版 `#help-nav`):灰槽 + 選中那篇的白膠囊 ----
    # 網頁版的 CSS 定義值:槽是 `--background-fill-secondary`(= `btn`)、圓角 14、內距 4;
    # 選中那條是 `--block-background-fill`(白)、圓角 10、`box-shadow: 0 1px 3px rgba(0,0,0,.12)`。
    # ⚠️ 兩張都是 `block_elem`(兩個方向都留中段):槽的高度由篇數決定、膠囊的高度由字級
    # 與內距決定,都不釘死在圖上(那是膠囊那一族的規矩,這兩張不是膠囊)。
    wr = px(SQ_R_TOC, scale)
    ww, wh = block(wr)
    imgs["toc-wrap"] = plate(ww, wh, wr, p["btn"], on=p["card"])
    elems["Sq.toc"] = block_elem([["", "toc-wrap"]], wr, 0, p["card"])
    # 影子是自己合成的(`plate` 沒有影子):往下 1、模糊 3、12%,畫在灰槽色上;膠囊本身左右
    # 與下方各縮進 `SQ_TOC_INSET` 讓影子有地方落(上方貼齊:它只往下投)。九宮格的邊要含
    # 這一圈,所以 `block_elem` 的 r 給 `ir + inset`。
    ir, inset = px(SQ_R_TOC_ON, scale), px(SQ_TOC_INSET, scale)
    ow, oh = block(ir + inset)
    imgs["toc-on"] = shadowed_plate(ow, oh, ir, p["card"], p["btn"], inset,
                                    dy=px(1, scale), blur=px(3, scale) / 2, alpha=0.12)
    elems["Sq.toc_on"] = block_elem([["", "toc-on"]], ir + inset, 0, p["btn"])

    return imgs, elems


def shadowed_plate(w: int, h: int, r: float, fill: str, on: str, inset: int,
                   *, dy: int, blur: float, alpha: float, top: int = 0) -> Image.Image:
    """一張帶柔影的圓角底板,坐在 `on` 上(不透明,同 `plate` 的 `on`)。

    影子:形狀同底板、往下 `dy`、高斯模糊 `blur`(σ,實體 px)、黑色 `alpha`;底板本身左右
    與下方各縮進 `inset`、上方縮進 `top`(預設 0 = 貼齊,目錄那顆),影子就落在那一圈裡。
    ⚠️ `dy` 不可以大於 `inset`,不然影子被圖的下緣切掉。

    ⚠️ **形狀一律從公開的 `plate()` 取**(它的 alpha 通道就是那張遮罩,實測逐位元組相同),
    **不要去叫 `skingen._sq_mask`**:那是共用包的私有函式,「只准加、不准改語意」的約定只
    涵蓋公開名稱,而它 2026-08-26 就換過一次簽章。⚠️ 而且這不只是重跑產生器時才會炸——
    共用包的 `skin.install()` 第三條路 `_drawn()` 會在**執行期** import 這支檔案(顯示縮放
    不在出貨那八檔內時,例如 Windows 11 的自訂縮放),而那條路的例外是整個吞掉的,症狀是
    按鈕矮一階、記錄檔一行都沒有(2026-09-03 code review 抓到)。"""
    from PIL import ImageFilter

    body = plate(max(1, w - 2 * inset), max(1, h - inset - top), r, "white").getchannel("A")
    base = Image.new("RGB", (w, h), on)
    shade = Image.new("L", (w, h), 0)
    shade.paste(body, (inset, top + min(dy, inset)))
    shade = shade.filter(ImageFilter.GaussianBlur(blur)).point(lambda v: int(v * alpha))
    base.paste(Image.new("RGB", (w, h), "black"), mask=shade)
    mask = Image.new("L", (w, h), 0)
    mask.paste(body, (inset, top))
    base.paste(Image.new("RGB", (w, h), fill), mask=mask)
    base.putalpha(255)
    return base




def variant(theme: str, scale: float) -> tuple[Image.Image, dict]:
    """一組(佈景 × 縮放)的成品:sprite sheet,加上它在 `sprites.json` 裡的那一節。

    ⚠️ **`main()` 與測試都要走這一支。** 資產是否為「現在這份程式的產物」由
    `tests/test_desktop.py::test_the_shipped_skin_is_what_the_generator_draws_today`
    逐位元組比對,而那條測試一旦自己組一份 meta,那份就變成第二個真值——這支檔案
    存在的理由正好是反過來的(見檔頭:形狀、色票、內距只寫在這裡一份)。"""
    imgs, elems = build_variant(theme, scale)
    sheet, rects = pack(imgs)
    # ⚠️ **前景色不烘進資產**(2026-08-27 移除):`skin.install()` 直接查色票就拿得到
    # (它握著 mode、也已經 import `palette`),而烘一份進來就是第二份真值——改了
    # `palette` 卻忘了重跑這支,停用態的字色會停在舊值而畫面其他地方都更新了。
    return sheet, {
        "file": f"skin-{theme}@{scale:g}x.png",
        "sprites": rects,
        "elements": elems,
    }


def _shrink(path: Path) -> None:
    r"""把剛存好的 PNG 再無損壓一次(oxipng)。

    ⚠️ **Pillow 的 `optimize=True` 對這批圖一個位元組都沒省**(2026-09-05 實測:
    1,339 KB 進、1,339 KB 出),而 oxipng 省 **24.6%**(→ 1,009 KB)——它重試各種
    掃描線濾波器與 zlib 策略,Pillow 只做其中一種。這批圖佔交付 zip 的一半,所以
    那 330 KB 是整包最划算的一刀。

    ⚠️ **無損**:`optimize_from_memory` 不動像素,只換編碼方式——這批是 RGBA 而且
    **alpha 是 0~255 的連續值**(圓角的抗鋸齒),任何量化(轉 P 模式、pngquant)
    都會讓圓角變鋸齒,所以那條路不能走。
    ⚠️ **`level=4` 不是 6、更不是 zopfli**(2026-09-05 三種都實測過,同一批 16 張):
    lv4 與 lv6 **產出一模一樣**(都是 1,009 KB),而 lv6 多花 56 秒;zopfli 再省
    43 KB(966 KB,27.8%)卻要 **1,603 秒**。這支是「改個顏色就重跑」的產生器,
    慢到那個程度沒有人會再跑它——而那 43 KB 換不到任何東西。
    ⚠️ **`strip=safe()`**:只丟掉與顯示無關的中繼資料(時間戳之類),色彩描述檔
    與 gAMA 會留著。⚠️ 這也讓輸出**穩定**:帶時間戳的話,同樣的輸入每次跑出來的
    位元組都不同,而 `tests/test_desktop.py` 是逐位元組比對產物的。
    ⚠️ **裝不了就跳過**(只印一句):它是 dev 相依,而這支腳本在沒有它的環境裡
    仍然要產得出東西——差別只是檔案大一點,不是壞掉。
    """
    try:
        import oxipng
    except ImportError:
        print(f"  (沒有 oxipng,{path.name} 未再壓縮;uv sync 之後重跑可省約 25%)")
        return
    raw = path.read_bytes()
    out = oxipng.optimize_from_memory(raw, level=4, strip=oxipng.StripChunks.safe())
    if len(out) < len(raw):
        path.write_bytes(out)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    meta = {"version": SCHEMA_VERSION, "scales": list(SCALES), "variants": {}}
    for theme in ("light", "dark"):
        for scale in SCALES:
            sheet, node = variant(theme, scale)
            dest = OUT / node["file"]
            sheet.save(dest, optimize=True)
            _shrink(dest)
            meta["variants"][f"{theme}@{scale:g}"] = node
    (OUT / "sprites.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1, sort_keys=True),
        encoding="utf-8")

    total = 0
    for p in sorted(OUT.iterdir()):
        total += p.stat().st_size
        print(f"{p.relative_to(ROOT).as_posix()}  {p.stat().st_size:,} bytes")
    print(f"合計 {total:,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
