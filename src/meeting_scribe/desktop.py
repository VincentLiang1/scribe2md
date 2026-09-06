r"""桌面視窗的**外殼**:標題列、四個頂層分頁、「聲音→MD」底下的三個子分頁。

2026-08-29 新增(原生介面遷移,決策記錄見 `docs/dev/native-ui.md`)。階段 3 只做
殼(視窗開得起來、工作列身分與圖示、`啟動.vbs` 的守門會說話——那三件事與頁面內容
完全無關,混在一起做的話任何一個症狀都會有兩打嫌疑犯);階段 4 一頁一頁接內容,
**六頁現在都接上了**(⚠️ 還缺的是頁面裡的兩塊:轉檔那頁的**命名區**與**現場收音**)。

⚠️ **這一支不 import 任何模型或引擎相依**(同姊妹專案的 `gui.py`):視窗的啟動
要是毫秒級的,而 faster-whisper / sherpa / OpenCC 那一整串本來就是惰性載入(見
CLAUDE.md)。階段 4 接內容時**照這條線走**——要跑的東西交給既有的 `pipeline` /
`docpipe`,不要為了方便在模組層 import 進來。

版面(1000 × 760)
------------------
  頂層四分頁:**置中**、選中的那個底下一條 2px 的主色底線(使用者 2026-08-29 在
    三個模擬圖裡選的第三案「頂層置中 ＋ 子分頁靠左」,理由是他看 apple.com 這類
    現代網站的主選單都置中,靠左是舊式應用程式的樣子)
  「聲音→MD」再分三個子分頁(轉檔 / 名單與聲紋 / 領域詞表):**靠左**、膠囊底
  內容區:`PAGE_PAD` 的外距,裡面是圓角卡片(`CARD_PAD` 24 / `CARD_GAP` 20)

⚠️ **間距那三個數字完全照抄 MP4-2-SRT**(使用者 2026-08-29 指定「完全照抄
24/24/20」):那組數字是他自己在截圖上逐像素量出來的(卡片內緣 42 實體 px
@150%,換算回邏輯像素約 27,取既有尺規的 24),第一版寫 16 被他當場說太窄。
⚠️ **不要為這裡新開一把尺**——三支程式在同一台電腦的桌面上並排,長相要是一套。

⚠️ **搬文案過來時不可以整串照抄**,`tests/test_desktop.py` 三面釘著:
  **VS16**(`U+FE0F`)要拿掉——Tk 對「本身沒有 emoji 樣式」的字符會把它畫成一個
    30px 寬的方塊(2026-08-29 實測,一般空白是 6px),而受害的正好是這裡要用的
    🎙 ▶ ⌨ ✏ 🖥 🖱 🎚(⚠ ⏹ ⚙ ⏱ 不受影響,所以「有幾個看起來正常」不是反證)。
  **Markdown 的記號要拿掉**(`**…**`、反引號)——Tk 不渲染,會原樣印出來。
  **圖示要與文字分成兩個 Label**,因為它們的基線不同(見 `NAV_LIFT`)。
"""
from __future__ import annotations

import collections
import contextlib
import logging
import shutil
import time
from dataclasses import dataclass
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import font as tkfont
from tkinter import ttk
from types import SimpleNamespace

from winkit import winui

from meeting_scribe import (attendees, audit, cancel, diarize, docpipe, docsrc,
                            doctab, filelog, help_text,
                            helpmd, pending, relabel, roster, skin, srcfile,
                            transcribe, wordlists)
from meeting_scribe import export
from meeting_scribe.types import SpeechBlock
from meeting_scribe import live as live_scribe
from meeting_scribe import models
from meeting_scribe import naming as naming_core
from meeting_scribe import pipeline, power, record
from meeting_scribe import voiceprints as voiceprints_store
from meeting_scribe.cancel import Cancelled
from meeting_scribe.errors import UserFacingError
from meeting_scribe.brand import APP_TITLE
from meeting_scribe.paths import appdata_root, assets_dir, repo_root
from meeting_scribe.pipeline import run_pipeline
from meeting_scribe.types import (DEVICE_NAMES, MAX_SPEAKERS,
                                  UNKNOWN_SPEAKER)

logger = logging.getLogger(__name__)

# 視窗尺寸。⚠️ **比姊妹專案高**:這裡最長的一頁是命名區,一場會議可能有 20 多位
# 講者(使用者 2026-08-29 特別點名這一頁要注意),而那一頁是往下捲的——⚠️ **下方
# 不放 1 2 3 4 那排分頁鈕**(他看到模擬圖的第一句話是「那看起來像分頁功能」),
# 就是一路捲到底。
# ⚠️ **2026-09-01 從 1000×760 放大**:寬度 = `MAX_CONTENT` + 兩側 `PAGE_PAD`
# (20×2),這樣預設開起來就正好是那個上限、不必先拉一次視窗;高度多出來的 60 給
# 新的頁首(大標＋副標)。⚠️ **2026-09-04 再從 1280 收成 1216**:那是 `MAX_CONTENT`
# 跟著網頁版改成 1176 的連帶,兩者的關係不變(改一個就要改另一個)。
WIN_W, WIN_H = 1216, 820

# 內容區的**最大**寬度。⚠️ **這是 2026-09-01「照網頁版重排」的第一刀**:網頁版的
# 容器寫死 `width: 1240px` 並置中(`ui_style.py` 的 `.gradio-container`),而原生
# 視窗原本沒有上限——最大化時內容就一路撐到螢幕邊。使用者 2026-09-01 兩張全螢幕
# 截圖量出來的差距是 **1175 對 1665 邏輯 px(1.42 倍)**,而那正是他說的「比例
# 不對」:卡片被螢幕撐寬,裡面的下拉與按鈕卻只有「文字那麼寬」,於是每張卡右邊
# 三分之二是空的。
# ⚠️ **要照的是 1176 不是 1240**(2026-09-04 使用者再拿兩張截圖對照:「調整 AP
# 左右的寬度跟 WEB 一致」):`.gradio-container` 寫的 1240 是**外框**,gradio 自己的
# `.app` 另有 `padding: var(--size-4) var(--size-8)`(`--size-8` = 32px),所以卡片
# **看得到**的那一段是 1240 − 32×2 = **1176**。⚠️ 上一版註解寫「1175 是 Chrome 的
# 頁面縮放」——**那句是錯的**,那 5% 就是這一圈內距;1176 一樣有出處,在
# `gradio/templates/frontend/assets/i18n-*.css` 的 `.app`。實測對照(兩張 150% DPI
# 的截圖逐列掃卡片邊界):網頁 1764 實體 px、原生 1856,而中縫都是 30 = `CARD_GAP`
# ——**先用中縫確認兩張同一個縮放,再比卡片寬**,不然量到的是 DPI 不是版面。
# ⚠️ **上限不是固定寬**:視窗拉得比它窄時內容跟著縮,不會冒出橫向捲軸。
MAX_CONTENT = 1176

# 共用的間距尺規(三個 repo 同一組值,理由見檔頭)。
SP_XS, SP_SM, SP_MD, SP_LG, SP_XL = 4, 8, 12, 16, 24
PAGE_PAD, CARD_PAD, CARD_GAP = 20, SP_XL, 20

# 分頁列那顆 emoji 要往上推幾個邏輯像素,才會與旁邊的中文字對齊。
# ⚠️ **它不是憑感覺調的**:emoji 走 Segoe UI Emoji 的後備字型,基線與
# Microsoft JhengHei UI 不同,同一行排出來圖示會**沉在下面**(2026-08-29 使用者
# 回報「選單上的字跟前面的圖示沒有對齊」)。Tk 沒有「調整單一字符基線」這種東西,
# 所以圖示要自己一個 Label、用不對稱的內距把它抬起來。
# ⚠️ **兩排的值不同,不是筆誤**:頂層 11pt、子層 10pt,而且兩排用的 emoji 字形也
# 不同。2026-08-29 實測(墨跡中線差,正值 = 圖示偏低):
#     推 0 → 頂層 +3.5、子層 +5.0/+6.0
#     推 3 → 頂層 **+0.5**、子層 +2.0/+3.0
#     推 4 → 頂層 −1.5、子層 **+0.0/+1.0**
# ⚠️ **不要再往下追那 ±1**:量的是**墨跡**的中線,而每顆 emoji 的字形高低本來就
# 不一樣(👥 兩顆頭、🎙 一支細長的麥克風),追到 0 是在對單一字形過擬合。
# ⚠️ **改了字級或換了圖示就要重新量一次**,做法見 `docs/dev/verification.md`
# 第 6 節(開真的視窗、逐個 widget 量墨跡中線)。
NAV_LIFT, SUBNAV_LIFT = 3, 4

# segmented control 的三個尺寸(邏輯 px)。⚠️ **`SEG_H_ON` 必須與 `scripts/make_skin.py`
# 的 `SQ_H_SEG_ON` 一模一樣**:膠囊底板的圖高與元件高度不相等時,Tk 不是垂直重複貼
# (下緣長出第二段圓角)就是裁切(下半個圓被削平)——兩種都不報錯、`reqheight` 也看
# 不出來,只有截圖看得到。`tests/test_desktop.py` 兩邊釘在一起。
# ⚠️ **2026-09-06 拿掉了灰槽**(使用者選案 H,四案對照見 `docs/dev/native-ui.md`):未選
# 中的那幾格改成直接坐在卡片白上,選中的那格是白膠囊 ＋ 一圈 `accent` 描邊。成因是
# **對比不是好看**——`muted` 坐在 `btn` 上淺色 8.85、深色 7.47,兩個模式都過不了共用包
# 那條 9:1,而 `btn` 從來就不在 `winkit.palette.MUTED_SURFACES` 裡。
# ⚠️ **`SEG_H` 因此只剩「那一列多高」,沒有對應的底板了**:它不再向皮膚問高度(沒有圖
# 要對齊),直接 `px()`。選中段仍然靠 `rowconfigure` 的 weight 垂直置中,差出來的一圈
# 現在單純是留白;`SEG_PAD` 也一樣,只是那一列左右各留的一圈。
# ⚠️ **膠囊同日從 25 調回 31**(使用者選案,四個高度開真視窗逐一截圖比過):25 是灰槽
# 時代那條外弧條件逼出來的,槽沒了就沒有理由留著。31 與一般按鈕/子分頁膠囊的 30 同一
# 個量級,而**刻意低於主要動作鈕的 40**——它只是個選項,比主要動作鈕還重就本末倒置。
# ⚠️ **一併消失的是「選中段的方角不可壓到槽的外弧」那條幾何條件**(2026-09-01 使用者
# 第二次圈出來的那件事:槽的四個角上各有一小塊灰)。沒有槽就沒有外弧,選中段的圓角外
# 側現在畫的是卡片白,所以那條式子與 `test_the_selected_segment_never_covers_the_track_corner`
# 一起移除了——沿革留在 `docs/dev/native-ui.md`,不要照著舊註解把它推導回來。
SEG_H, SEG_H_ON, SEG_PAD = 37, 31, 6

# 兩排分頁選中那一格的膠囊高度。⚠️ **量網頁版得到的**:兩排都是 **45 實體 px**
# @150%(頂層 y 339~383、子層 417~461),÷1.5 = 30——兩排一樣高不是巧合,它們在網頁版
# 是同一組 CSS。⚠️ **第一次量成 41 是掃描步進 4 列漏掉了上下邊緣**:逐列掃才對得上,
# 而 45 正好等於一般按鈕的高度(`make_skin.SQ_H`),那也是它們在畫面上的量級。
# ⚠️ **元件高度不要拿這個值去 `px()`**,要問 `_plate_h()`(理由見那一支)。
TAB_PILL_H = 30

# 兩排分頁選中那一格的藍條厚度(邏輯 px)。⚠️ **量網頁版得到的**(150%,x=1920 逐列
# 掃):藍條 y 384~386 **3 實體 px**,底下那條灰線 385~386 與 463~464 各 **2 px**——也就
# 是 CSS 的 2px 與 1px。⚠️ **灰線不是另一個常數,是「藍條減一列」**(`_tab_rule_h`):
# 量到的關係正是「藍條比灰線高**一列**、底邊對齊」,而 `px(1)` 在這台算出來是 **1**
# 不是 2——`scale` 量到 1.4985,`round` 落在 1.5 的另一側(同 `_plate_h` 那條 55 對 56
# 的問題);從藍條已經 round 過的高度減,就不會各自漂。⚠️ **兩者不是同一個數**:上一版
# 把它們做成同一條(都 `px(2)`),灰線就比網頁版粗一截(2026-09-02 使用者:「灰色的兩
# 條線比較粗」)。所以每一格的橫條是兩層:外層藍條的高度、內層貼底一段灰線,見
# `_tab_cell`。
TAB_BAR_H = 2

# 標題列與 Alt+Tab 的圖示。⚠️ 工作列那顆不歸它管,見 `_apply_window_icon`。
ICON_PATH = assets_dir() / "icon.ico"

# 頁首那顆程式圖示的字級(跟著大標的字級走,見 `_brand_image`)。
BRAND_ICON_PT = 20

# 頁首的第二行。⚠️ **與網頁版同一句**(`app.build_ui` 的 `#app-header`),搬過來時
# 只動了兩處:拿掉 Markdown 的 `#` 標題記號(Tk 不渲染,會原樣印出來),以及把
# 「❓ 使用說明」的引號內容縮成分頁名——原生版的分頁圖示與文字本來就是分開的兩個
# Label,把 emoji 寫進句子裡反而對不上那一排。
# ⚠️ **「檔案不外傳」不可省**:那是隱私規格對使用者的承諾(spec §7),也是他敢把
# 會議錄音丟進來的唯一理由;網頁版那邊測試守著,這邊由 `tests/test_desktop.py` 守。
APP_SUBTITLE = ("本系統係將聲音、文字、圖像轉換成適合 AI 閱讀的 Markdown 格式"
                "(以節省 Token)。全部在本機轉換,檔案不外傳;"
                "第一次使用請先看「使用說明」(首次轉檔需下載模型)。")

# 頁首三行(大標、副標、頂層分頁列)之間的行距(邏輯 px),**量網頁版得到的**
# (2026-09-03 使用者:「AP 最上面幾行字的行距,請依照 WEB 的行距調整」;@150% 逐列掃
# 他的兩張全螢幕截圖,實體 px,量的是**墨跡與墨跡之間的空白**):
#
#   大標 → 副標          網頁 37   原生當時 18(pady SP_XS = 6)  → 差 19 = px(13) → 4 + 13
#   副標 → 分頁列的字    網頁 43   原生當時 25(pady SP_SM = 12) → 差 18 = px(12) → 8 + 12
#
# 字高兩邊一致(大標 41 對 40,副標與分頁字都是 21),分頁字→灰線 10 對 12、視窗頂→大標
# 29 對 25,差在 2~4 實體 px 內、沒動。Tk 的 Label 沒有內距,墨跡之間的空白 = `pack` 的
# pady + 字型自己的上下留白,所以 pady 是唯一能調的那一項。⚠️ **這兩個數字量自網頁版,
# 不是尺規上的刻度**(同 `HELP_GAP`):尺規上最近的 `SP_LG`/`SP_XL` 各差 3~6 實體 px,
# 而使用者比對的正是這種差距。要改就重量(`verification.md` §6),不要照感覺調。
HEAD_GAP_SUB, HEAD_GAP_NAV = 17, 20

# 說明文字裡的程式碼字(`_icon_text` 的 code tag)。Consolas 從 Vista 起隨 Windows 出貨;
# 缺了 Tk 會自己退到預設字型,只是不等寬,不是壞掉。
MONO_FAMILY = "Consolas"

# 彩色 emoji 的來源。⚠️ **Windows 7 以後都有這個字型**,而本專案本來就是 Windows
# 專用的;真的缺了就退回文字圖示(見 `App._nav_icon`),不是壞掉。
EMOJI_FONT = Path(r"C:\Windows\Fonts\seguiemj.ttf")

# 逐字稿的落地位置(與網頁版同一個資料夾:兩套介面轉出來的東西不該分家)。
OUTPUT_DIR = repo_root() / "output"

# 模型的介面標籤 ↔ 引擎鍵。⚠️ **這個選擇必須保留**(2026-07-26 曾移除、同日裁定
# 還原):快速 = large-v3-turbo、精準 = large-v3。
MODEL_LABELS = {"快速": "fast", "精準": "accurate"}
MODEL_KEYS = {v: k for k, v in MODEL_LABELS.items()}

# 核對表最多列幾列。⚠️ **限的是列數不是長度**(使用者 2026-08-13 指定改成逐列
# 播放):逐列播放一次只聽一段,限制長度反而是把東西藏起來;而幾百列光是排版就
# 卡得有感,那個成本與「聽多久」無關。
AUDIT_MAX_ROWS = 100
# 核對視窗(2026-09-02 使用者選案 C:另開視窗、表格式;三案實拍見 `docs/dev/native-ui.md`)。
# 主視窗還沒 map(測試)或右欄太小時的最小尺寸(邏輯 px)、表格列高(照網頁版核對表的
# 列)、格子上的記號。⚠️ 記號都選 Microsoft JhengHei UI 畫得出來的字(VS16 那條坑不碰)。
AUDIT_WIN_MIN_W, AUDIT_WIN_MIN_H = 720, 520
AUDIT_ROW_H = 28
AUDIT_PICKED, AUDIT_UNPICKED, AUDIT_STOP = "☑", "☐", "■"
AUDIT_COLS = ("pick", "play", "time", "secs", "text")
AUDIT_COL_SPEC = (("pick", "", 36, "center"), ("play", "", 36, "center"),
                  ("time", "時間", 72, "center"), ("secs", "長度", 64, "e"),
                  ("text", "摘錄", 0, "w"))
# 一行灰一行白(使用者 2026-09-02 拿網頁版核對表對照):列多時眼睛才跟得住同一列的
# 時間與摘錄。⚠️ **白那一半要自己設**(`_styles` 把 `Audit.Treeview` 的底色設成卡片的白):
# 共用包給 Treeview 的底色是紀錄框那個淺灰(`log_bg` = #f0f0f3),第一版的斑馬灰 #f4f4f6
# 疊在上面幾乎同色,使用者當場問「灰及白的差異太小,你是不是設錯了」。灰要看得出來、
# 又不跟 hover/選中那一列(`row_sel`)與琥珀底打架。
AUDIT_ZEBRA_LIGHT, AUDIT_ZEBRA_DARK = "#ececf0", "#2c2c31"
AUDIT_HINT = ("一列一輪發言:點「▶」逐列聽(播放中會變成「■」,再點一下停止)、點第一格"
              "勾選(鍵盤:上下選列、空白鍵勾選、Enter 播放)。要問的是「這一群裡面是不是"
              "混了別人」——混進來的通常是很短的插話,所以短的那幾列特別值得聽。")

# 現場收音的三個情境。⚠️ **三個錄的東西不同**,不是同一件事的三個名字:現場會議
# 只錄麥克風;線上會議**麥克風與系統聲音分兩軌**(回音在文字層去重);只錄電腦
# 聲音走 WASAPI loopback。⚠️ **每次啟動一律回到第一項**(使用者 2026-08-09 指定
# 拿掉「記住上次選擇」,推翻 2026-07-21 的規格)。
SCENARIOS = {"現場會議": "onsite", "線上會議": "online", "只錄電腦聲音": "playback"}

# 情境那一排 segmented 的 (鍵, 圖示, 文字)。⚠️ **鍵就是 `SCENARIOS` 的鍵**:
# `_rec_scene` 存的是它,而開始錄音時查的也是它——兩份對不起來的症狀是「選了線上
# 會議,錄到的卻是麥克風」,而畫面上完全看不出來。
# ⚠️ 圖示照網頁版的 `SCENARIO_LABELS`,**但拿掉 VS16**(見檔頭)。
SCENARIO_OPTIONS = (
    ("現場會議", "\U0001F399", "現場會議"),
    ("線上會議", "\U0001F4BB", "線上會議"),
    ("只錄電腦聲音", "\U0001F50A", "只錄電腦聲音"),
)

# 情境的說明**跟著選中的那一個換**(使用者 2026-08-07 指定,同網頁版的
# `_SCENARIO_INFO`):三段一起攤在上面時,只有一段是當下有用的。
SCENARIO_INFO = {
    "現場會議": "大家在同一個房間,只錄麥克風收到的聲音",
    "線上會議": "如 Teams / Meet,麥克風+喇叭都錄,回音自動剔除",
    "只錄電腦聲音": "只錄電腦放出來的聲音,適合純旁聽、線上課程",
}

# 模型那一排 segmented。⚠️ **沒有圖示**(空字串):兩個選項是同一件事的兩個檔位,
# 給它們各配一顆圖示只會讓人以為那是兩種不同的功能。
MODEL_OPTIONS = tuple((k, "", k) for k in MODEL_LABELS)

# 模型的說明,依這台機器**有沒有 GPU** 換一句(同網頁版 `build_ui` 的 `model_info`;
# 完整理由收在「使用說明」)。
# ⚠️ **兩句都縮成半欄排得進一行的長度(≈ 16 個中文字)**(2026-09-02 使用者選案 C):
# 摺疊區排成「模型｜CPU 核心數」兩欄之後左欄餘裕只剩 36 實體 px,任何一句多出一行
# (+22)就只剩 14——`test_the_advanced_card_keeps_to_two_columns_and_one_line_hints`
# 拿真的字型量。網頁版的長版(「文字更準,總時間和快速差不多」「改選精準會慢約 4 倍」)
# 在 `app.py` 與使用說明裡都還在,這裡只留結論。
# ⚠️ **餘裕只剩 5 實體 px**(2026-09-04,`MAX_CONTENT` 收成 1176 之後半欄是 291,而
# 「沒有 GPU」那句 286):**再多一個字就折行**——要改這兩句就先拿那條測試量,不要
# 照「≈ 16 個中文字」估(GPU、4 這種半形字比中文窄,字數算不出寬度)。
MODEL_INFO = {
    True: "有 GPU:已選「精準」,時間差不多",
    False: "沒有 GPU:已選「快速」,精準慢 4 倍",
}

# 「CPU 核心數」底下那句(文案重點同網頁版、使用者 2026-07-26 指定的:講清楚「預設
# 已自動留 1 核」與「太卡就調小」,舊電腦的使用者才知道這裡是逃生口)。⚠️ **縮成半欄
# 排得進一行**(理由見 `MODEL_INFO`),而且**核心數兩位數的機器也要排得進**(測試用
# 64 核量過)。⚠️ **2026-09-04 又少掉「電腦」兩個字**:內容區照網頁版從 1240 收成
# `MAX_CONTENT` 1176,半欄的換行寬度跟著從 311 掉到 291 實體 px,而原句 295——**差
# 4 px**。留下的正好是使用者當初指定的那兩個重點,一個字都沒少。
# ⚠️ **是函式不是常數**:核心數是這台機器的事,import 當下不該定死
# (測試會換掉 `cpu_count`)。
def cores_info() -> str:
    return f"預設 {power.default_worker_count()}(自動留 1 核);太卡就調小"


# 還沒開始錄音時狀態列那一句(同網頁版的 `_REC_IDLE_MD`)。
REC_IDLE = "尚未開始錄音。"

# 按了「停止並轉檔」之後、收尾跑完之前那一句(同網頁版的 `_REC_FINISHING_MD`,
# 只把方位詞換成這一版的版面:網頁版預覽在右邊,這裡在下面)。
# ⚠️ **一定要有這句、而且計時器要停**:先前那行字整個收尾期間都還寫著「● 錄音中・
# 已錄 62:05」而且**數字繼續往上跳**——收音其實早就停了(`_rec_tick` 只看
# `_rec` 裡的 recorder,而它要到 `_rec_done` 才清掉)。長會議的收尾要跑幾十分鐘,
# 那段時間畫面等於在說謊,而使用者唯一的判讀是「它還在錄」。
# ⚠️ **不寫 Markdown 的粗體記號**:狀態行是普通的 ttk.Label,`**` 會原樣畫出來。
REC_FINISHING = "收尾中:完成剩餘轉錄與講者分析,進度見下方的預覽區…"

# 「進階參數設定」收合的記號。⚠️ **不帶 VS16**(見檔頭):▶ 正是那七個受害字元之一,
# 帶了就在後面多出一個 30px 寬的方塊。
ADV_CLOSED, ADV_OPEN = "▶", "▼"

# 使用說明裡那張 Claude 隱私設定截圖(全篇唯一一張圖)。
PRIVACY_IMG = repo_root() / "docs" / "claude-privacy-setting.jpg"

# 使用說明的行距與段距(邏輯 px),**量網頁版得到的**(2026-09-03 使用者:「說明文字太擠,
# 請參考圖片 WEB 的行距調整,內文及選單都要調整」;@150% 逐列掃他的截圖,實體 px):
#
#   同一段裡換行      網頁 30   Tk 10pt 的行高 25 → 多留 3 邏輯 px(`HELP_LINE_GAP`)
#   段落 → 段落        57      → 差額 31 = px(21)
#   段落 → 清單項      41      → 15 = px(10)(項與項之間 40,同一個數)
#   任何東西 → 小標    54      → px(18)(小標的墨跡在行裡比內文低 2px,所以不是 16)
#   小標 → 段落        57      → px(19)(小標 12pt 行高 30)
#   大標 → 段落        68      → px(18)(大標 16pt 行高 41;2026-09-04 大標從 17pt 改成 16pt,
#                                        行高跟著少 2 實體 px,段距就得多還回去)
#   目錄每一列          56      → 見底下 `TOC_ROW`(2026-09-03 目錄重排成網頁版的樣子)
#
# ⚠️ **Tk 沒有 CSS 那種相鄰邊界折疊**:段落→清單項是 41、清單項→段落卻是 57,同一個
# 段落的 spacing3 給不出兩個值——所以段距**看這一塊與下一塊是什麼**,由 `_render_help`
# 逐塊挑 tag 掛上去(`_help_gap`),而不是每種塊一個固定值。⚠️ 這些數字量自網頁版,不是
# 尺規上的刻度;要改就重量(`verification.md` §6 的做法:截圖、逐列掃墨跡),不要照感覺調
# ——第一版就是照「差額」算的,沒算到各級字的墨跡在行裡的位置不同,量出來差 2~14px。
HELP_GAP = {"para": 21, "bullet": 10, "heading": 18, "h2": 18, "h3": 19}
HELP_LINE_GAP = 3
# 使用說明的字級(pt):大標、小標、內文。⚠️ **只有這一份**——`tag_configure` 的字型表與
# `_help_insert`(把 emoji 換成彩色圖片)都從這裡取。⚠️ **兩邊必須同號**:圖是照 pt 算出
# 畫布大小的,對不上就是「圖示比旁邊的字小一號」,而那正是使用者 2026-09-03 要求「圖示的
# 高度與文字要相同」的反面。2026-09-03 真的漂開過一次:字型表改成大標 17pt/小標 12pt,畫圖
# 那段留在 13/11(舊字級),十篇篇名的圖示全部只有旁邊的字四分之三高,而測試只驗字型表、
# 量不到圖。
# ⚠️ **大標 2026-09-04 從 17pt 改成 16pt**(使用者:「使用說明 最大的標題 字體有點太大,
# 這是 WEB 的參考,應該要小一點」,並補「只咬最大的標題,其他的字體大小都 OK」)。⚠️ 這一級
# 是**照網頁版重量出來的、不是照感覺調小**:@150% 的截圖上量漢字的**起點間距**(那就是
# em),網頁版大標 33、小標 24、內文 21 實體 px → 邏輯 22 / 16 / 14 px → pt(×0.75)是
# 16.5 / 12 / 10.5;小標的 12 本來就正中(所以使用者說它 OK),大標取 16(同內文 10.5 取
# 10 的取法),而舊的 17pt 在 150% 下畫出來是 34 實體 px、比網頁版整整大一級的觀感。
# ⚠️ **要再改就重量**:量的是 em(字的起點間距)不是墨跡高度——墨跡高度隨字形變動,
# 「錄」與「一」差好幾 px,拿它當基準會量出兩套數字。
HELP_PT = {"h2": 16, "h3": 12, "": 10}
# 目錄(2026-09-03 使用者:「使用說明左邊選單,樣式請做成與 WEB 相同」;照網頁版 `#help-nav`
# 的 CSS 定義值,邏輯 px):灰槽寬 194(`gr.Column(min_width=248)` 扣掉卡片內距)、內距 4、
# 圓角 14(`make_skin.SQ_R_TOC`);每一條 35 高(`padding: 7px 12px` + 一行字)、圓角 10、
# 左右內距 12、條與條之間 2。原生把那 2px 併進每一條的高度(`TOC_ROW` = 37):選中那條
# 白膠囊的影子(`0 1px 3px`)正好落在那 2px 裡——那一圈是 `TOC_INSET`,**與
# `make_skin.SQ_TOC_INSET` 綁死**(測試守著)。⚠️ 裡面的 Label 只能蓋在膠囊的直邊段:左右各
# 縮進 `TOC_INSET + TOC_R_ON`、高度只到 `TOC_PILL_H`,Label 的方角底色才不會壓在圓角與
# 影子上(`TOC_R_ON` 也與 `make_skin.SQ_R_TOC_ON` 綁死)。網頁版量到列距 55.5 實體 px
# @150%,px(37) = 55。
TOC_W, TOC_PAD, TOC_ROW, TOC_PILL_H = 194, 4, 37, 35
TOC_INSET, TOC_R_ON, TOC_TEXT_PAD = 2, 10, 12

# 頂層四分頁。⚠️ 順序與網頁版一致(使用者對「第幾個分頁」是有肌肉記憶的);
# ⚠️ 字串少了 VS16、與網頁版不同,理由見檔頭。
TABS = (
    ("audio", "\U0001F399", "聲音→MD"),
    ("doc", "\U0001F4C4", "文字、圖像→MD"),
    ("lexicon", "\U0001F4DA", "用詞替換表"),
    ("help", "❓", "使用說明"),
)

# 「聲音→MD」底下的三個子分頁(同上,順序照網頁版)。
SUBTABS = (
    ("run", "\U0001F3A7", "轉檔"),
    ("roster", "\U0001F465", "名單與聲紋"),
    ("hotwords", "\U0001F524", "領域詞表"),
)

# 三條互斥的工作路徑,以及被擋下來時要說的話(見 `App._busy_reason`)。
# ⚠️ **互斥是正確性需求、不只是體驗**(CLAUDE.md 的硬規則):`cancel` 的旗標是全域
# 單例,兩邊同時跑時任一顆停止鈕會把另一邊也殺掉。
# ⚠️ **話要指出去哪一頁按停止**:三條路徑分屬兩個頂層分頁,只說「還有工作在跑」的
# 話,使用者會在眼前這一頁找那顆根本不在這裡的鈕。
# ⚠️ **分頁名不帶 emoji**(同 `TABS`:圖示與文字本來就是分開的兩欄):照抄網頁版那
# 串會把 VS16 一起抄進來,而 Tk 把它畫成一個 30px 寬的方塊。
# 「要做什麼」:(鍵, 圖示, 名稱, 說明小字)。三種模式**互斥**(同網頁版的
# `app._MODE_*`):一次只顯示一組控制項——把收音與選檔同時攤開,正是 2026-08-30
# 「預覽框沒露出來、命名區被擠成 1px」的成因。
# ⚠️ **「🔄 重設講者」2026-09-01 才接上**(使用者在版面提案裡選定「這次一起接上」):
# 在那之前它刻意不放——接不上的選項與壞掉的選項長得一模一樣。它與轉檔收尾**共用同
# 一套命名區**,差別只在資料從哪裡來(那邊是剛跑完的 `PipelineResult`,這邊是解析既
# 有的 md),所以 `_naming_show` 多開了兩個參數收「已經算好的」核對資料與試聽片段。
RUN_MODES = (
    ("rec", "🎙", "現場收音", "開會時直接收音,邊開會邊轉,散會幾乎不用等"),
    ("file", "🎧", "轉錄音檔", "挑已經錄好的錄音/錄影檔,轉成逐字稿"),
    ("relabel", "\U0001F504", "重設講者",
     "拿一份已經轉好的逐字稿,重新命名裡面的講者(不會重轉)"),
)

# 「文字、圖像→MD」上方那段說明、備註與路徑框的提示字(2026-09-03 照網頁版重排;三段都
# 與網頁版 `app.build_ui` 的 `tab-doc` 同一句)。
# ⚠️ **這一段照網頁版一字不改,連 `**粗體**` 與 `` `程式碼` `` 的記號都留著**(同日使用者:
# 「請依照 WEB 為準,不要自己發明」):它不是 Label,是 `App._icon_text`(`tk.Text`),記號由
# `parse_inline` 解成粗體／等寬字的 tag、emoji 畫成彩色圖片(與分頁列那顆同一張)。先前兩版
# 各自拿掉了 emoji 與 Markdown 記號,理由都是 Label 畫不出來——現在畫得出來,就不必改字。
# ⚠️ **不帶 VS16**(同 `TABS` 那條,測試守著);⚠️ 純 Label 的文案(`DOC_NOTE` 等)仍然不可以
# 帶記號(`test_the_wordlist_pages_carry_no_markdown` 那一族)。
DOC_INTRO = ("把 Word、PowerPoint、Excel、PDF、網頁、掃描件、照片與**錄音錄影**轉成 Markdown"
             "(`.md`),方便交給 AI 閱讀。轉好的檔案放在**原始檔案的旁邊**(同名、副檔名換成 `.md`),"
             "文件裡的圖片會存進同名的 `.assets` 資料夾。共支援 {n} 種格式(完整清單見"
             "「❓ 使用說明」);錄音錄影會轉成逐字稿,要**替講者取真實姓名**請改用「🎙 聲音→MD」分頁。")
DOC_NOTE = ("轉出的內容裡,「〔 〕」標的是無法完整呈現的地方(圖表、儲存格底色等),"
            "檔頭也有一份清單——寧可讓你看到少了什麼,也不要安靜地少掉。")
# 路徑框空著時的提示(網頁版 Textbox 的 placeholder)。⚠️ `tk.Text` 沒有 placeholder,
# 自己畫:空的時候塞這一句(灰字、`ph` tag),游標一進來就拿掉(`_doc_ph_show/_hide`)。
DOC_PLACEHOLDER = "按下方按鈕挑選,或直接把路徑貼進來"
# 三段說明(文件頁、用詞替換表、領域詞表)的行距(邏輯 px),**量網頁版得到的**(2026-09-03
# 使用者:「說明文字的行距太擠,
# 請參考 WEB 頁面」;@150% 逐列掃截圖,實體 px):網頁版兩行的墨跡頂到墨跡頂 37,原生
# 10pt 的行高 25 → 多留 12 = px(8)。⚠️ **上下各半**(`_icon_text` 的 spacing1/spacing3):
# 那正是瀏覽器的 line-height 做法(半行距在字的上下),整段的上下邊也跟著對上網頁版
# (灰線→第一行墨跡 46 對 45)。⚠️ 與說明頁的 `HELP_LINE_GAP`(3)不是同一個數:網頁版
# 那兩處的 CSS 行高本來就不同,各量各的。
INTRO_LINE_GAP = 8
# 結果框右上角那顆「複製」(2026-09-03 使用者:「請放 WEB 頁面的圖示即可,不要複製的文
# 字」):量網頁版截圖 @150%,整顆 26×26 實體 px、中間的線稿 18×18 → 邏輯 17 與 12。
COPY_BTN, COPY_GLYPH = 17, 12
# 聲紋庫那排「把 ___ 的聲紋改掛到 ___」兩格的寬(字元數;2026-09-03 使用者:「都請拉長一點」,
# 原本 14)。⚠️ 2026-09-04 排成三欄之後那兩格改成 `fill="x"` 撐滿欄寬,這個數字只當
# **請求寬度的下限**——給大了欄就被撐開(見 `_roster_page` 的「不准過度請求」)。
VP_FIELD_W = 12

# 「名單與聲紋」三欄各佔內容區的幾成:**照網頁版的 scale 3 / 4 / 3**(`app.py` 那一頁的
# 註解寫著理由:中欄裝的控件最多——摘要、已登記的人、三顆鈕、會就地展開的健檢結果,
# 而兩側各只有一份清單或兩個欄位)。⚠️ **改任何一欄都要重算中欄**,判準同網頁版。
ROSTER_SHARES = (0.3, 0.4, 0.3)
# 捲軸自己有多寬(sv_ttk;`_two_columns` 的註解裡量過的那 12)。⚠️ **這一頁把它的位置
# 一直留著、不管捲軸現在有沒有出現**:留一格看不出來(卡片自己就有 24 的內距),而不留
# 的話健檢一有結果、捲軸一冒出來,三欄就整組被擠窄——說明小字的句尾當場被右邊界切掉。
SCROLL_W = 12
# 名單編輯框、已登記的人清單各幾列。⚠️ **兩個都要寫死**:`tk.Text` 不給就是 24 列、
# `tk.Listbox` 是 10 列,而這一頁的欄寬只有 292/406——請求高度一大,整頁就開始捲。
# ⚠️ **已登記的人只給名單框的一半、而且不跟著卡片長高**(2026-09-04 使用者:「聲紋健檢
# 沒有考慮到,如果有多人時,畫面會放不下,所以中間的那個不能拉那麼長…已登記的人那個
# 選取框,可以只有一半高」;同日再指定 4 列改 6 列):中欄那張卡是這一頁唯一會長高的
# 一張,而**健檢結果與這份清單在搶同一段高度**——清單佔走多少,可疑樣本就少幾筆放得
# 下(實測不必捲的筆數:8 列時 3 筆、6 列時 4 筆、4 列時 5 筆)。清單自己會捲,矮一點
# 只是多捲兩下;健檢結果矮不了。
ROSTER_LINES, VP_LIST_LINES = 17, 6
# 勾選格連同它右邊那點空白佔多寬——健檢那幾列的文字要從可用寬度裡扣掉它才不會壓到
# 卡片邊界(同命名區線索旁邊那兩顆鈕的 `wrap_minus`)。
CHECK_W = 28

# 預覽框固定幾行。⚠️ **一定要寫死**(同網頁版的 `lines=20`,它的註解是「不讓預覽
# 把頁面撐滿」):`tk.Text` 不給 height 就是預設 24 行、實測請求 564px,它一個人
# 就能把命名區擠成 1px。
PREVIEW_LINES = 20

# 轉檔頁左欄佔內容區的幾成(`grid` 的權重 5:7)。⚠️ **說明小字的換行寬度靠這個
# 算,不准去量欄位自己有多寬**——理由見 `_wrap_width`。
RUN_LEFT = 5 / 12

# 命名下拉裡「聲紋分不開的候選」的底色。⚠️ 網頁版用的是半透明琥珀
# `rgba(255,199,0,.18)`,疊在淺色卡片上正好是使用者 2026-08-16 選定的 #FFF4D1;
# Tk 的 listbox 不吃 alpha,所以兩種佈景各給一個「已經疊好」的值。
RIVAL_AMBER_LIGHT, RIVAL_AMBER_DARK = "#fff4d1", "#4a3d12"

# 使用說明搜尋的標記色(2026-09-03):每一筆命中淡黃、目前那一筆深一階——照瀏覽器 Ctrl+F
# 的慣例(全部黃、目前橘)。⚠️ 淡黃**就是**命名下拉那個琥珀,不是另外量的一個相近色:
# 寫成兩組獨立的字面值時,調其中一邊另一邊不會跟,而兩處相隔四行、看起來像各自量出來的
# (2026-09-03 code review 抓到)。目前那一筆深一階,那個才是這裡自己的顏色。
FIND_HIT_LIGHT, FIND_HIT_DARK = RIVAL_AMBER_LIGHT, RIVAL_AMBER_DARK
FIND_NOW_LIGHT, FIND_NOW_DARK = "#ffcf4d", "#8a6a12"

# 兩欄的「請求高度」——只是為了讓 Tk 願意 map(高度 0 的 widget 不會 map),
# 實際高度由 `sticky="nsew"` 拉滿。⚠️ **要比實際可用高度小**,理由見
# `_refit_columns`(拿量到的高度回填會把視窗一路推大)。
COL_SEED_H = 400

# 即時預覽的開頭那句。⚠️ **要講明講者名字還沒標**:不講的話使用者會以為講者分離
# 壞掉了,而它本來就要等收尾才算得出來。
LIVE_PREVIEW_HEAD = "(錄音中即時預覽;講者名字會在停止錄音、完成分析後標註。)" \
                    "\n\n"

# 一位講者都沒有時,預覽最前面那一段(同網頁版 `app._NO_SPEECH_NOTE`;模式名照
# `RUN_MODES`、不帶 emoji)。⚠️ **一定要明講**:不講的話畫面上只有一份空的逐字稿,而
# 使用者(2026-08-15 錄了一段沒有人聲的電腦聲音)第一個念頭是「程式壞了」。
NO_SPEECH_NOTE = (
    "這一段聲音裡沒有聽到任何人說話,所以逐字稿是空的,也沒有講者可以命名。\n"
    "常見原因:收音時電腦其實沒有在出聲、麥克風被靜音或選錯裝置。"
    "音檔已經存起來了——確認聲音沒問題之後,可以用「轉錄音檔」把它重轉一次。"
)

# 命名卡的標題與說明:同網頁版 `#name-box` 頂端那一句,拆成標題＋說明兩個 Label,
# 「套用」寫成這裡按鈕的字樣。⚠️ 2026-09-02 照網頁版對齊(使用者:「UI 的樣式要比照
# WEB」),先前多出來的那句「認不出來的人不要硬填…」拿掉了——網頁版 2026-08-18 精簡
# 面板時就沒有它,那句話在使用說明裡。
NAMING_TITLE = "為講者命名(選填)"
# ⚠️ 字數壓在 1280 寬時左欄一行排得下的長度:多兩三個字,句尾那個「。」就自己掉到第二行
# (Tk 不做避頭尾)。
NAMING_HINT = ("看摘錄、按「▶ 試聽」或「🔍 核對」認人;"
               "按「套用名字」寫回,留白維持講者 N。")

# 「改成幾位講者」底下那句限制說明(同網頁版 `app._RECLUSTER_HINT`)。⚠️ **一定要寫**:
# md 的區塊是原子的,往多的方向改只能在現有段落之間重新分配,而使用者填了 5 卻安靜地
# 只拿到 3 是最糟的一種——他不會知道。
RECLUSTER_HINT = "往少改一定準;當初就在同一段裡的兩個人拆不開,要重轉。"

# 「未知」命名框線索前面那半句(同網頁版):它只改逐字稿文字,絕不登記聲紋。
UNKNOWN_NO_ENROLL = "只改逐字稿文字、不會登記聲紋"

# 開頁還原未完成命名時,預覽最前面那一句(同網頁版 `_restore_pending`)。
RESTORED_NOTE = "(已還原上次未完成的講者命名,可直接接續;不需要重新轉檔。)"

# 「跳過命名」之後預覽留的那一段(同網頁版 `_discard_naming`):成品**不**刪,講清楚它
# 在哪,不讓使用者以為檔案不見了。
# 「轉錄音檔」的兩種模式(照網頁版 `app._SRC_MODE_HINT`;Tk 不渲染 Markdown,所以是純文字)。
# 第二句一定要講「不做命名」:使用者是在這裡決定要不要一次丟一批的,等 30 分鐘後才發現
# 沒有命名就白花了(使用者 2026-08-06 指定摘要要當場講清楚,同一個理由)。
# ⚠️ 第二句**自己斷行**:Tk 的 Label 對中文逐字斷行,整句(量到 797 px)在左欄的換行寬度
# (671 px @1280)裡一定折,而它自己折的位置落在「原檔名.md」或「同名」中間(實拍看到的)。
# 拆成 467 ＋ 330 兩段各一行、斷在逗號上;高度與自動折行一樣是三行,視窗再寬也是三行
# (內容區上限 1240,換行寬度不會再變大)。
FILE_MODE_HINT = ("選 1 個檔案:轉完可替講者命名(記住聲紋),成品在 output 資料夾。\n"
                  "選多檔或資料夾:整批連轉、不做命名(只標「講者 1/2/3」),\n"
                  "成品放在原檔旁邊,已有同名 md 就跳過。")
SKIPPED_NOTE = ("已跳過講者命名,畫面已清空。\n"
                "已完成的逐字稿(講者以「講者 1、講者 2…」標示,現場收音含錄音檔)"
                "仍在 output 資料夾。")

# 命名區塊右側那一欄(「▶ 試聽」「🔍 核對」兩顆鈕疊著)的寬度,邏輯 px。⚠️ 照網頁版
# `.name-btns` 的 `min_width=100`:那一欄**每一塊都佔位**,有沒有鈕都一樣,下拉的右
# 邊界才會每一塊對齊;線索那行的換行寬度也要扣掉它(見 `App._naming_block`)。
NAME_BTN_COL = 100
# 命名與改掛的下拉清單是**自畫的**(2026-09-02 使用者三案實拍後選 ②):Tk 內建的 popdown
# 是 Listbox,它沒有列內距、hover 那一列一定帶 3D 邊(`selectborderwidth` 同時是行距與陰影),
# 「灰底、沒有陰影、行距有空間」三個一起要,它給不了;字型那條路也量過(候選字型在 10pt
# 的行高全是 17)。清單的樣子照網頁版:列高 28、hover 是平的灰底、目前那一筆前面打 ✓。
PICK_ROW_H = 28                   # 列高(邏輯 px)
PICK_MAX_ROWS = 10                # 一次露幾列,多的捲
PICK_MARK = "✓"                   # 目前那一筆前面的記號——只畫在清單上,絕不進字串
PICK_HOVER_LIGHT, PICK_HOVER_DARK = "#e8e8ed", "#3a3a3f"   # hover/選中列(網頁版量到的灰)

WORK_BUSY_TEXT = {
    "doc": "文件轉檔還在跑,請先等它結束,或到「文字、圖像→MD」按停止。",
    "run": "檔案轉檔還在跑,請先等它結束,或到「聲音→MD」按停止。",
    "rec": "現在正在錄音,請先按「停止並轉檔」。",
}

# 工作列那條進度的刻度數(見 `App._taskbar`)。⚠️ **與畫面上的進度條無關**:那條吃
# 0~100 的百分比,而 `SetProgressValue` 吃的是自己給的分母。千分之一在工作列按鈕那
# 麼寬的一格上遠小於一個像素,夠細了——而刻度愈粗,「同樣的值不重送」那道守衛擋掉的
# COM 往返愈多。
TASKBAR_STEPS = 1000

# 收工時工作列按鈕上留下的顏色(使用者 2026-09-05 逐項選定,同 `MP4-2-SRT` 的
# `gui.OUTCOMES`)。⚠️ **旗標在這裡查完才給 `winui`**:那一層只管「怎麼跟 Windows
# 講話」,不該認得本專案的收場種類(反過來也不行)。
# ⚠️ **黃與紅的界線是「壞掉了」還是「沒做成但你知道為什麼」**,不是「成功/失敗」:
# 紅色如果連「錄音太短」都算進去,它就會常常出現在其實沒壞的情況,而看久了就沒有人
# 再看它了——那等於把最重的那個訊號用掉。
# ⚠️ **「轉完了但還沒命名」是這支程式才有的一格**(`MP4-2-SRT` 沒有):完成不等於
# 收工,而命名進度會落地存檔這件事本身,就證明使用者確實會離開再回來。
WORK_OUTCOME = {
    "naming": winui.TBPF_PAUSED,     # 轉完了,但還等你回來填講者名字
    "stopped": winui.TBPF_PAUSED,    # 你自己按的停止
    "known": winui.TBPF_PAUSED,      # 看得懂原因的失敗(錄音太短、格式不支援)
    "partial": winui.TBPF_PAUSED,    # 批次有檔案**轉失敗**(略過的不算,見 `_job_say`)
    "broken": winui.TBPF_ERROR,      # 未預期的錯誤、錄音收尾失敗
    "done": winui.TBPF_NOPROGRESS,   # 真的收乾淨了
}

# 兩份可以直接編輯的詞表檔。⚠️ **兩頁長得一樣是刻意的**(同一個 builder):它們
# 對使用者是同一件事——「改一份純文字檔,存檔後下一個檔案生效」,而長得不一樣只
# 會讓人以為規矩不同。⚠️ **卡片上沒有標題**(2026-09-03 使用者圈出「領域詞表」那個
# 標題:「紅框多餘,請刪除」):分頁列已經寫著同一個名字,網頁版的卡片上也沒有;兩頁
# 同一個 builder,用詞替換表那頁的一起拿掉。⚠️ **但文案不可以互相照抄**,兩份檔的規矩真的不同(一個有
# 長度預算、一個沒有;一個只影響轉錄、一個兩條路徑都吃),見 `wordlists` docstring。
# ⚠️ **`note` 照網頁版 `app.build_ui` 一字不改**(2026-09-03 使用者:「行距與斷行請比照
# WEB,寫兩行即可」→「請依照 WEB 為準,不要自己發明」):粗體與反引號的記號留著,由
# `App._icon_text` 渲染;網頁版 Markdown 裡那個軟換行(`\n`)瀏覽器畫成一個空白,這裡
# 也寫成空白。先前那版三句各自硬斷行、第三句拿 ⚠ 代替粗體,都是 Label 畫不出來的權宜。
WORDLIST_PAGES = {
    "lexicon": dict(
        note=("輸出前把偶發的大陸用詞換成台灣用詞(軟件→軟體)。"
              "一行一條「原詞 新詞」(空格或 Tab 分隔)、`#` 開頭是註解,"
              "在這裡編輯等同用記事本改 `data/replace.txt`,"
              "**存檔後下一個轉換的檔案生效**(進行中的那個不受影響)。 "
              "**加詞前先讀檔頭的收詞原則**:台灣也在用的詞"
              "(如「優化、用戶」)收了反而會改壞原文。"),
        save="儲存替換表",
        lines=24,                    # 編輯框幾行(tk.Text 的預設值)
        read=wordlists.replace_file,
        status=wordlists.replace_status,
        write=wordlists.save_replace,
    ),
    "hotwords": dict(
        note=("轉錄時提示 AI 優先選這些詞,減少同音錯字"
              "(金控≠監控、壽險≠受險)。一行一詞、`#` 開頭是註解,"
              "在這裡編輯等同用記事本改 `data/hotwords.txt`,"
              "**存檔後下一個轉錄的檔案生效**(進行中的那個不受影響)。 "
              "**順序即優先序**:詞表有長度預算,超出的尾端會被靜默"
              "忽略——重要的詞放前面。"),
        save="儲存詞表",
        # ⚠️ 比用詞替換表少兩行(2026-09-03 使用者:「領域詞表,維護內容的格子請減少兩行,
        # 因為剛剛調整說明的行距,導致下面的訊息會看不到」):這一頁在「聲音→MD」的子分頁
        # 底下,比頂層那頁少一排分頁列的高度;說明段照網頁版拉開行距之後,預設 1280×820 的
        # 視窗裡卡片只到底邊上 18px,按鈕列底下那行狀態(「目前 N 條…」與超出預算的警告)
        # 被 pack 判定放不下、直接不畫(量到 `winfo_ismapped()` = 0,畫面上就是少一行)。
        lines=22,
        read=wordlists.hotwords_file,
        status=wordlists.hotwords_status,
        write=wordlists.save_hotwords,
    ),
}


# 已經接好內容的分頁。⚠️ **它要蓋滿每一頁**——漏掉一頁就是點過去一片空白,而那與
# 「這一頁本來就沒東西」長得一模一樣。`tests/test_desktop.py` 釘著,而 `_build_ui`
# 也是讀這一份、不另外抄一份 if/elif。
# ⚠️ **2026-08-29 起這裡就是全部六頁**:先前還有一份 `TODO`(還沒接的那幾頁放一段
# 「還沒接上」的說明),六頁接完之後那份與 `_placeholder` 一起刪掉了——沒有人用的
# 東西跟刻意留的長得一模一樣(CLAUDE.md 的殘骸那條)。要再加頁就照這份補。
BUILT_PAGES = (*WORDLIST_PAGES, "help", "doc", "run", "roster")


# 本檔自己要用的 ttk 樣式:(樣式名, 底色鍵, 字色鍵, 字級, 粗體)。
# ⚠️ **按鈕與卡片那些不在這裡**,它們由 `skin.apply()` 給(那是共用包的事);這裡只
# 有版面自己長出來的那幾個標籤。
# ⚠️ **不要在這裡宣告共用包已經設過的樣式**:`CardBody.TFrame` 曾經在這裡重複設過
# 一次同樣的值(2026-08-29 移除)。重複宣告本身不會壞,壞的是它讓人以為那個樣式歸
# 這裡管——姊妹專案 2026-08-29 就是在那種「看起來還在用」的重複段落裡加了一行新
# 顏色,而整段其實是死碼,於是那個顏色從來沒生效過,截圖與對比度都看不出來。
# ⚠️ **宣告了就要有人用**:每一個都由 `tests/test_desktop.py` 兩面釘著(真的生效
# 了嗎、真的有 widget 在用嗎)。
STYLES = (
    # 頁首(2026-09-01 照網頁版加回來,見 `App._brand_head`)。⚠️ **字級是量網頁版
    # 得到的**:大標那行在他的截圖上是 32 CSS px,而 Tk 的字級單位是 point
    # (1pt = 4/3 px @96dpi),32 ÷ 4×3 = 24——取 22 是因為原生視窗的標題列已經寫著
    # 同一個名字,照抄 24 會讓「重複的那一份」比正本還搶眼。
    ("Brand.TLabel", "page", "ink", 22, True),       # 程式名(頁首大標)
    ("BrandSub.TLabel", "page", "muted", 10, False),  # 底下那句用途與隱私
    ("Nav.TLabel", "page", "muted", 11, False),      # 未選中的頂層分頁
    # ⚠️ **選中的那一個是「灰膠囊 ＋ 黑字」,不是藍字**(2026-09-01 量網頁版才發現:
    # 底 #e8e8ed、字 #1d1d1f)。先前寫成 accent 藍,是照著底下那條藍線的印象推的。
    # ⚠️ 底色也要跟著改成 `btn`——它現在坐在膠囊上,給 `page` 就是一塊淺色方塊。
    ("NavOn.TLabel", "btn", "ink", 11, True),        # 選中的那一個
    ("Sub.TLabel", "page", "muted", 10, False),      # 未選中的子分頁
    ("SubOn.TLabel", "btn", "ink", 10, True),        # 選中的子分頁(膠囊底)
    # segmented control 的兩種格子(2026-09-01,見 `App._segmented`)。⚠️ **底色要
    # 跟著各自坐的那一層**——`ttk.Label` 是實色底、不是透明的,給錯就是一塊色差方塊
    # 壓在膠囊上。⚠️ **2026-09-06 起兩種都坐在卡片白上**(拿掉灰槽,使用者選案 H):
    # 沒選中的直接畫在卡片上,選中的那格是白膠囊 ＋ 一圈 accent 描邊、底色也是白。
    # ⚠️ **沒選中那格不可以改回 `btn`**:`muted` 坐在 `btn` 上深色只有 7.47:1,而共用包
    # 承諾的只有 `page` / `card` / `field` 三階——`test_every_muted_style_sits_on_a_
    # surface_the_shared_package_promises` 擋著,突變 M584 守著。
    ("Seg.TLabel", "card", "muted", 10, False),      # 沒選中的那幾格
    ("SegOn.TLabel", "card", "ink", 10, True),       # 選中的那一格
    ("CardH.TLabel", "card", "ink", 11, True),       # 卡片標題
    ("Hint.TLabel", "card", "muted", 9, False),      # 卡片裡的說明小字
    # 命名卡裡的欄位名(「講者 N 的名字」「改成幾位講者」),2026-09-02 照網頁版:那邊
    # 的欄位名是深色粗體的小標,比卡片標題小一號、比說明小字深一階。
    ("Field.TLabel", "card", "ink", 10, True),
    ("Status.TLabel", "card", "muted", 9, False),    # 存檔之後那一句
    # 卡片**外面**那一句(錄音狀態列,2026-09-01 隨主要動作鈕一起搬出卡片)。
    # ⚠️ **底色是 `page` 不是 `card`**:`ttk.Label` 是實色底,坐在視窗底上卻塗卡片白,
    # 就是一塊白矩形浮在灰底上——那與「這一行的背景髒了」長得一模一樣。
    ("PageStatus.TLabel", "page", "muted", 9, False),
    # ⚠️ **警告要有自己的顏色,不是把字加粗**:網頁版是 `**…**`,而 Tk 畫不出
    # Markdown(會原樣印出四個星號)。兩種警告(詞表超出預算、替換規則缺新詞)
    # 的共同點是**使用者不會自己發現**,所以它要跳出來。
    ("Warn.TLabel", "card", "warn", 9, True),        # 同上,但出問題那一句
    # 使用說明的目錄(2026-09-03 照網頁版 `#help-nav`):沒選中的坐在灰槽上、選中的坐在
    # 白膠囊上(粗體;網頁版 `font-weight: 600`)。⚠️ 底色要跟著各自坐的那一層(同 segmented
    # 那條):`ttk.Label` 是實色底,給錯就是一塊色差方塊。
    ("Toc.TLabel", "btn", "ink", 10, False),         # 使用說明的目錄
    ("TocOn.TLabel", "card", "ink", 10, True),       # 目錄上正在看的那一篇
)


@contextlib.contextmanager
def unlocked(box: tk.Text):
    """暫時把鎖住的 `tk.Text` 打開來寫,寫完放回原本的狀態。

    ⚠️ **`tk.Text` 在 `state="disabled"` 時是靜默吃掉插入的**——不拋錯、也不留痕跡,
    症狀只是「路徑框裡什麼都沒有」,而那與「使用者還沒選檔」長得一模一樣。工作進行中
    路徑框是鎖著的(見 `App._data_lock`),而清空路徑、放提示字這幾件事照樣會在那段
    時間發生。"""
    was = str(box.cget("state") or "normal")
    box.configure(state="normal")
    try:
        yield
    finally:
        box.configure(state=was)


def configure_styles(root: tk.Misc, fam: str, pal: dict) -> None:
    """把 `STYLES` 設進 ttk。

    ⚠️ **抽成模組層函式是為了測得到**(2026-08-29):要問「這個樣式最後**生效**的值
    是什麼」只能拿真的 `ttk.Style.lookup()` 問,而那需要一個 Tk root——抽出來之後
    測試建一個**藏起來的** root 就夠,不必開整個視窗。
    ⚠️ **掃原始碼問不出這件事**:宣告寫在檔案裡、`in` 一比就過,但那段程式碼有沒有
    被執行是另一回事;而 ttk 會照後綴繼承退到 `TLabel` 的預設值,所以**畫得出東西、
    截圖正常、連對比度都可能更漂亮**(姊妹專案 2026-08-29 的實例:壞掉那個是
    #1c1c1c on #fafafa,14.98:1,比正確值還「好看」)。"""
    st = ttk.Style(root)
    for name, bg, fg, size, bold in STYLES:
        st.configure(name, background=pal[bg], foreground=pal[fg],
                     font=(fam, size, "bold" if bold else "normal"))
    # 選中的子分頁那塊膠囊底。⚠️ **底色要給整個 cell,不能只給兩個 Label**:圖示
    # 與文字現在是各自的 Label(理由見 `App._icon_pad`),中間那道 padding 屬於
    # cell——只染 Label 的話,膠囊中間會裂出一條頁面底色的縫。
    # ⚠️ **`SubOnCell.TFrame` 不在這裡宣告**(2026-09-01 移除):它現在是一張膠囊底板
    # (`skin.SKIN_FRAMES`),而共用包已經替每一張底板設好了「沒有皮膚時的後備底色」
    # ——在這裡再設一次,會讓人以為那個樣式歸這裡管。
    # segmented 的格子**裡面**那一層(見 `App._segmented`)。⚠️ **不可以直接用
    # `SegOn.TFrame`**:那一支的 layout 被換成了一張膠囊底板,每用一次就多畫一顆膠
    # 囊——框中框,而症狀是文字被一條白邊切掉(看起來像沒對齊,不像樣式錯了)。
    # ⚠️ **`Seg.TFrame` 從 2026-09-06 起要在這裡設**:灰槽拿掉之後它沒有底板了,也就
    # 不再進 `skin.SKIN_FRAMES`——那份名單才是「誰的底色由共用包設」的來源,不在裡面
    # 就沒有人設它,而 ttk 會照後綴退到 `TFrame` 的預設灰:白卡片上一條說不出理由的
    # 灰帶,而且不當掉、不報錯。
    st.configure("Seg.TFrame", background=pal["card"])
    st.configure("SegCell.TFrame", background=pal["card"])
    st.configure("SegOnBody.TFrame", background=pal["card"])
    # segmented 的**停用**外觀(2026-09-03 使用者:「現場錄音時,進階參數設定應該要
    # DISABLE」)。⚠️ **它本來就按不動,缺的是看得出來**:`_model_show` / `_scene_show`
    # 開頭就 `if self._busy[...]: return`,所以錄音中點它一直是沒反應——而「按了沒反應」
    # 與「壞掉」在畫面上長得一模一樣。字色用 `run_off_fg`,那是整個視窗停用態共用的那個
    # (`skin.ACCENT_STYLES` 的 map 也是它),不另外發明一個顏色。
    for _seg in ("Seg.TLabel", "SegOn.TLabel"):
        st.map(_seg, foreground=[("disabled", pal["run_off_fg"])])
    # 子分頁選中時,膠囊**裡面**那一層(同上,見 `App._build_ui`)。
    st.configure("SubOnBody.TFrame", background=pal["btn"])
    # 卡片上的勾選項(2026-09-03 使用者:「進階選項有灰底,應該是白底」)。sv_ttk 的
    # `TCheckbutton` 沒有自己的底色、退到主題的視窗底(那個灰),坐在白卡片上就是一塊
    # 灰矩形框著字——不當掉、不報錯,只有截圖看得到。⚠️ **每一顆坐在卡片上的
    # Checkbutton 都要穿它**,不只文件頁那三顆(轉檔頁的「包含子資料夾」、聲紋健檢的
    # 勾選欄同病;測試用 AST 掃全檔守著)。
    st.configure("Card.TCheckbutton", background=pal["card"])
    # 使用說明目錄裡**沒選中**的那幾條(2026-09-03):只有灰槽的底色、沒有底板;選中的那條
    # 才換成 `TocOn.TFrame`(白膠囊底板,`skin.SKIN_FRAMES`)。
    st.configure("TocItem.TFrame", background=pal["btn"])


# 按鈕字樣開頭、**照文字畫**的兩個記號:網頁版的「● 開始錄音」「■ 停止錄音並完成逐字稿」
# 是瀏覽器拿文字字型畫的單色符號(這兩個在 Segoe UI Emoji 裡沒有彩色字形),照抄。
# 其餘開頭的符號(▶ ⏹ 🔍 🔀)在網頁版都是彩色 emoji,`HandButton` 會畫成彩色圖片。
BUTTON_TEXT_MARKS = "●■"

# `configured()` 的「這一次沒動到」哨兵。⚠️ 不能用 `None`:`configure(text=None)` 與
# 「沒有給 text」是兩件事,而前者在 Tk 裡是合法的(等於清空)。
_UNSET = object()


def configured(cnf, kw, key: str = "text"):
    r"""這一次 `configure` 給 `key` 的新值;沒動到就回 `_UNSET`。

    ⚠️ **兩條路都要看**:`widget[key] = …` 走的是 `Misc.__setitem__`,而它的實作是
    `self.configure({key: value})`——那個字典是**位置參數**,不在 `**kw` 裡。只看 `kw`
    的包裝會漏掉那一條,而症狀是「值換了、跟著它跑的那件事沒跟」:2026-09-03
    `HandButton` 就是這樣「字換了、圖示停在舊的那一顆」,連 `cget()` 都不再與畫面一致。
    ⚠️ **這支存在的理由是那條 Tk 知識只該寫一份**:本檔有兩個攔 `configure` 的包裝
    (`HandButton.configure` 與 `_wrap` 裡那個實例層的),先前各寫各的、而且寫法不同
    ——下一個包裝一定是照最近的一份抄。"""
    if cnf and key in cnf:
        return cnf[key]
    return kw.get(key, _UNSET)


def is_symbol(ch: str) -> bool:
    """這個字元**可能**是 emoji(碼位落在符號／emoji 那兩段)。

    只是粗篩:「─」「☐」也在範圍裡,而 Segoe UI Emoji 沒有它們——真正的判定在
    `App._icon_pil`(拿字型畫一次、對照 .notdef)。中文從 U+4E00 起、英數更前面,都不在
    這兩段裡。⚠️ **箭頭那一區(U+2190~21FF)刻意不算**:Segoe UI Emoji 有「→」,畫出來
    卻是一根細細的線稿,而網頁版的「聲音→MD」是拿文字字型畫的——要留在文字裡。"""
    o = ord(ch)
    return 0x2300 <= o <= 0x2BFF or 0x1F000 <= o <= 0x1FAFF


def split_icon(text: str) -> tuple[str, str]:
    """「🔍 核對」→ `("🔍", "核對")`;開頭不是圖示 → `("", text)`。

    判準是「第一個字是符號／emoji、第二個字是空白」,所以「選擇檔案…」「只存名單」
    一律原樣回來。"""
    if (len(text) >= 3 and text[1] == " " and text[0] not in BUTTON_TEXT_MARKS
            and is_symbol(text[0])):
        return text[0], text[2:]
    return "", text


# 中文排版的避頭尾:這些不能落在行首／行尾(`breakable_after`)。⚠️ 半形的 `,.;:!?` 也算:
# 說明文字裡的逗號句號是半形的(全 repo 的慣例),行首一個「,」跟全形的一樣難看。
NO_BREAK_BEFORE = "、。,.;:!?%)]}」』】〉》—…・"
NO_BREAK_AFTER = "「『【〈《([{"


def is_cjk(ch: str) -> bool:
    """中文、全形標點(U+2E80 起:部首、標點、假名、漢字、全形字母都在後面)。"""
    return ord(ch) >= 0x2E80


def breakable_after(a: str, b: str) -> bool:
    """`a` 與 `b` 之間可不可以斷行——瀏覽器規則的近似:至少一邊是中文(英文單字內部不拆),
    兩邊都不是空白(空白本來就是斷點),而且不落在標點的錯邊(避頭尾)。"""
    if a.isspace() or b.isspace():
        return False
    if not (is_cjk(a) or is_cjk(b)):
        return False
    return b not in NO_BREAK_BEFORE and a not in NO_BREAK_AFTER


def break_lines(text: str, width: int, measure, widths: list[int] | None = None) -> str:
    r"""照瀏覽器的規則把一段文字斷成幾行(給 `ttk.Label` 用;`measure` 量一段字幾像素寬)。

    (2026-09-03 文件頁上方那段說明把「.assets」拆成「.a / ssets」。)⚠️ **Label 的
    `wraplength` 只認 ASCII 空白**:留著空白它就只在空白處斷(一整串中文當一個單字,前面
    那半行空著——2026-08-30 那次的病);把空白換成 U+00A0(2026-08-30 的解法)它就逐字斷,
    英文單字也照拆。Label 又沒有 `tk.Text` 那種塞得進看不見斷點的地方(U+200B 量過,寬度
    0 但**不算斷點**),所以斷點自己算、塞真正的換行;`wraplength` 留著只當量錯時的安全網。
    規則同 `breakable_after`:中文字之間可以斷、英文單字內部不拆、標點避頭尾;**空白也是
    斷點**(斷在空白後面,那個空白不進任何一行——瀏覽器也是這樣;「docx / pptx / …」那種
    清單少了這條就被硬切成「mht / ml」);原文裡的換行照留(那是刻意的硬斷行)。一整個
    「單字」都放不進一行時退回逐字斷(不然它永遠放不進去)。
    `widths` 是**每一個字**的像素寬(與 `text` 等長):給了就不叫 `measure`,改用前綴和
    ——`_icon_text` 那種一段裡混著粗體、等寬字與圖片的文字,沒有一個字型量得了整段,
    只能逐字各量各的(不計字距,差在 1px 內)。"""
    out = []
    base = 0                                   # 這一段在 text 裡的起點(widths 用)
    for para in text.split("\n"):
        if widths is None:
            def span(a: int, b: int, para=para) -> int:
                return measure(para[a:b])
        else:
            acc = [0]
            for i in range(len(para)):
                acc.append(acc[-1] + widths[base + i])

            def span(a: int, b: int, acc=acc) -> int:
                return acc[b] - acc[a]
        base += len(para) + 1
        # ⚠️ **空段落要照留一行**:底下那個迴圈對空字串一次都不跑,不補這一行的話
        # `out` 就少一項,而原文裡的空白行是**刻意的段落分隔**(`\n\n`)。2026-09-03
        # 從 Label 的 `wraplength` 換成自己算斷點時漏掉,症狀是文件頁摘要的「⚠ 其中
        # N 個是錄音/影片……」直接黏在「已選 N 個檔案」下面(Tk 自己排版時是留著的)。
        if not para:
            out.append("")
            continue
        start = 0
        while start < len(para):
            n, last_ok = start, None
            while n < len(para) and span(start, n + 1) <= width:
                n += 1
                if n < len(para) and (para[n - 1] == " " or breakable_after(para[n - 1], para[n])):
                    last_ok = n
            if n >= len(para):
                out.append(para[start:].rstrip(" "))
                break
            cut = last_ok if last_ok is not None and last_ok > start else max(n, start + 1)
            out.append(para[start:cut].rstrip(" "))
            start = cut
            while start < len(para) and para[start] == " ":   # 硬切時別讓下一行以空白開頭
                start += 1
    return "\n".join(out)


def parse_inline(raw: str) -> tuple[str, list[str]]:
    r"""`**粗體**` 與 `` `程式碼` `` 兩種記號 → (純文字, 每個字的樣式 "" / "b" / "code")。

    只做網頁版那三段說明用到的兩種(`gr.Markdown` 的行內語法),不是 Markdown 引擎:
    反引號裡的 `**` 不算記號(照 Markdown 的規矩),其餘一律原樣。記號本身不進純文字,
    所以斷行與量寬看到的都是使用者會看到的字。

    ⚠️ **這是全 repo 第二支行內記號解析器**(另一支是 `helpmd.spans`,使用說明那邊用的),
    2026-09-03 code review 記下來的:兩支對**現有出貨文案**的結果完全相同,分歧只在原文
    寫壞的時候——未閉合的 `**` 或反引號,這支會把記號吃掉、樣式套到該行結尾,`helpmd`
    那支留成字面值;反引號裡的 `**`,這支照 Markdown 的規矩不當記號,而 `helpmd` 的
    regex 會當成粗體(與它自己的 docstring 相反)。要合併的話**以這支的語意為準**,但那
    會動到使用說明的渲染,不是順手做得完的事——在那之前,**新文案兩種記號都要成對**。"""
    plain, style = [], []
    bold = code = False
    i = 0
    while i < len(raw):
        if raw.startswith("**", i) and not code:
            bold = not bold
            i += 2
            continue
        if raw[i] == "`":
            code = not code
            i += 1
            continue
        plain.append(raw[i])
        style.append("code" if code else "b" if bold else "")
        i += 1
    return "".join(plain), style


@dataclass
class ScrollBox:
    r"""一個「內容比視野高就捲」的容器:`canvas` ＋ 裡面那張 `inner`,捲軸要用時才出現。

    ⚠️ **捲軸一律 `grid(row=0, column=1)`**(`_scroll_refit` 寫死):兩欄頁把它放進中間
    那條溝裡,整頁捲動的頁面則是「內容一欄、捲軸一欄」——兩種都對得上這個座標,所以
    捲動那一套(`_scroll_refit` / `_scroll_top` / `_wheel`)兩邊共用一份。"""

    canvas: tk.Canvas
    inner: ttk.Frame
    win: int
    scrollbar: ttk.Scrollbar
    # 內容比視野**矮**的時候要不要把它撐滿(見 `_scroll_refit`)。⚠️ 預設 False:
    # 兩欄頁的左欄是一疊由上而下的卡,撐開只會讓最後一張莫名其妙地長高。
    stretch: bool = False


@dataclass
class TwoCols(ScrollBox):
    """一頁的兩欄(見 `App._two_columns`):**左欄**就是那個可捲動的 `ScrollBox`,捲軸
    住在中間那條溝裡。"""

    left: ttk.Frame = None
    right: ttk.Frame = None


@dataclass
class Fold:
    """一張可摺疊的卡(「進階參數設定」,見 `App._fold`):標題列點了展開/收起。"""

    card: ttk.Frame
    body: ttk.Frame
    mark: ttk.Label
    open: bool = False


class HandButton(ttk.Button):
    r"""會換游標、而且把字樣開頭的 emoji 畫成**彩色圖片**的按鈕——兩件事都是照網頁版。

    **游標**(2026-09-02 使用者:「游標移到開始錄音,不會出現手指的符號」):網頁版的
    按鈕是 `cursor: pointer`、停用時 gradio 自己給 `cursor: not-allowed`;Tk 的按鈕預設
    兩種狀態都是箭頭。⚠️ **游標是 widget 的選項、不是樣式的**(`ttk.Style` 設不到),
    所以只能逐顆設——而「逐顆設」在三十幾顆按鈕上一定會漏,於是整個 `desktop.py`
    一律用這一支,`tests/test_desktop.py` 反向釘著「檔案裡不准再出現裸的 `ttk.Button(`」。
    ⚠️ **狀態翻過去,游標要跟著翻**,而狀態有三種寫法(`configure(state=)`、
    `["state"] =`、`state([...])`),三條都要接住——`__setitem__` 走的是 `configure`,
    所以蓋前兩支就夠。

    **圖示**(2026-09-03 使用者:「所有頁面上的圖示 ICON,例如試聽及核對等按鈕上都請
    修正」):Tk 把 emoji 當文字畫是單色的,網頁版的「▶ 試聽」「🔍 核對」「🔀 重新分群」
    卻是彩色的。所以字樣開頭的圖示(`split_icon`)拆出來交給 `App.button_icon` 畫成
    圖片、`compound="left"` 擺回字前面;**停用時換灰階那張**(ttk 的 `image=` 吃狀態表)。
    ⚠️ **`cget("text")` 從此不含那個圖示字元**(它成了圖);畫不出來的機器(沒有那顆
    字型)整段原樣留在文字裡,退回單色——不是壞掉。⚠️ 也走 `configure(text=)`,所以
    試聽鈕在「▶ 試聽」與「⏹ 停止」之間翻的時候圖會跟著換。"""

    def __init__(self, master=None, **kw) -> None:
        super().__init__(master, **kw)
        self._refit_icon(kw.get("text"))
        self._refit_cursor()

    def configure(self, cnf=None, **kw):
        # ⚠️ **兩種寫法都要接住**(`configure(text=…)` 與 `btn["text"] = …`),那條 Tk
        # 知識收在 `configured()` 一份裡——先前這裡拆成兩個 `if`,兩者同時給還會把圖
        # 重畫兩次。
        out = super().configure(cnf, **kw)
        text = configured(cnf, kw)
        if text is not _UNSET:
            self._refit_icon(text)
        self._refit_cursor()
        return out

    def _refit_icon(self, text) -> None:
        if text is None:
            return
        icon, rest = split_icon(text)
        render = getattr(self.nametowidget("."), "button_icon", None)
        photos = render(icon, str(self.cget("style"))) if icon and render else None
        if photos is None:                     # 沒有圖示,或這台機器畫不出來
            ttk.Button.configure(self, image="", compound="none")
            return
        ttk.Button.configure(self, text=rest, image=photos, compound="left")

    config = configure

    def state(self, statespec=None):
        out = super().state(statespec)
        if statespec:
            self._refit_cursor()
        return out

    def _refit_cursor(self) -> None:
        # ⚠️ 直接叫 `ttk.Button.configure`,不然會回到上面那支、無限遞迴
        ttk.Button.configure(
            self, cursor="no" if self.instate(["disabled"]) else "hand2")


class App(tk.Tk):
    """主視窗。"""

    def __init__(self) -> None:
        super().__init__()
        # ⚠️ **整段建介面都藏在螢幕外**,最後一行才 `deiconify()`(做法與理由同姊妹
        # 專案):根視窗是在**第一次 `update_idletasks()` 當下**被貼到螢幕上的,而
        # `skin.apply` 與 `winui.set_backdrop` 都會呼叫它——不藏的話使用者看到的是
        # 「小畫面 → 擴大」那兩幀。
        # ⚠️ **所以 `withdraw()` 必須排在那兩支之前**,順序本身就是這個機制
        # (2026-08-29 量到證據:`App()` 剛回來時截圖 96.26% 是純黑、一次
        # `update_idletasks()` 之後降到 2.32% 就再也不動——那一次 update 正是視窗
        # 上螢幕的時刻。見 `docs/dev/verification.md` 第 6 節)。⚠️ 排錯了**不會
        # 當掉、截圖也看不出來**(`PrintWindow` 經過 DWM 會補齊),使用者看到的只是
        # 開窗時多閃一幀,所以 `tests/test_desktop.py` 釘著這個順序。
        self.withdraw()
        self.title(APP_TITLE)
        # ⚠️ 縮放倍率要問 Tk 自己(DPI-aware 之後它量得到真實 DPI):寫死的像素一律
        # 過 `self.px()`,否則 150% 下整個版面會縮成 2/3。
        self.scale = self.winfo_fpixels("1i") / 96.0
        self.fam, self.pal, self.skin = skin.apply(self, self.scale)
        self.configure(background=self.pal["page"])
        # ⚠️ **底色要給兩邊**:上面那一行只管 Tk 自己畫的那一份,視窗最小化再還原時
        # 露出來的是 Windows 那一層(預設是黑的)。成因與量測在 `winui.set_backdrop`。
        winui.set_backdrop(self, self.pal["page"])
        self._apply_window_icon()

        self.tab = TABS[0][0]             # 現在停在哪個頂層分頁
        self.subtab = SUBTABS[0][0]       # 「聲音→MD」停在哪個子分頁
        self._nav: dict[str, tuple[ttk.Label, tk.Frame]] = {}
        self._subnav: dict[str, ttk.Label] = {}
        self._pages: dict[str, ttk.Frame] = {}
        # 每一頁的兩欄(見 `_two_columns`);沒建過的頁不在裡面。
        self._cols: dict[str, TwoCols] = {}
        # 每一頁的可捲動容器。⚠️ **兩欄頁登記的就是同一個 `TwoCols` 物件**(它本身
        # 就是 `ScrollBox`),不是另一份拷貝——兩份參考同一組 widget 遲早會分岔。
        # 整頁捲動的頁面(名單與聲紋)只在這裡,`_cols` 沒有它。
        self._scrolls: dict[str, ScrollBox] = {}
        # 「這個容器佔內容區的幾成、以及整頁要先扣掉幾像素」(見 `_wrap_share`)。
        self._shares: dict[tk.Misc, tuple[float, int]] = {}
        # 自畫的下拉清單:同時最多開一張;候選筆數照下拉的 Tk 路徑記(`_mark_rivals`)
        self._pick: dict | None = None
        self._combo_rivals: dict[str, int] = {}
        # 三條工作路徑「正在跑」的旗標(見 `_busy_reason`)。⚠️ **記在視窗上、不是
        # 回頭去問按鈕**:分頁是「用到才建」的,那一頁沒被點過時按鈕根本不存在。
        self._busy = dict.fromkeys(WORK_BUSY_TEXT, False)
        # 「工作進行中不該能動」的元件,以及它們**閒著時**該是什麼狀態(見
        # `_lockable` / `_data_lock`)。⚠️ **要記原本的狀態**:`ttk.Combobox` 閒著時
        # 是 `readonly`,一律放回 `normal` 會把它變成可以自由打字的欄位。
        self._locked: list[tuple[tk.Misc, str]] = []
        # 工作列按鈕上那條**最後送出去的東西**(見 `_taskbar`):`("end", 旗標)` =
        # 收場、None = 跑馬燈、0~`TASKBAR_STEPS` = 進度。⚠️ 記著是為了「一樣就不重送」。
        self._tb = ("end", winui.TBPF_NOPROGRESS)
        # 這一趟工作有沒有「跑完了但不乾淨」(目前只有批次的 `partial`,見 `_job_say`)。
        # ⚠️ **由工作執行緒回報、每趟開頭歸零**(`_run_job`):收工那一刻才問得到的東西
        # 只有錯誤,而「12 個檔裡有 3 個轉失敗」不是錯誤——它成功跑完了。
        self._job_outcome = ""
        # 現在這一場錄音(recorder / live / dir / stem / scene)與它的起算時刻。
        # ⚠️ **放在這裡而不是 `_rec_build`**:關視窗時要問「還在錄音嗎」,而那是在
        # 「使用者從沒點過轉檔那一頁」也成立的問題(同 `_busy` 那條)。
        self._rec: dict = {}
        # `_rec_t0` 開錄的時刻;`_rec_t1` 是**收音停止**的時刻(0 = 還在錄)。
        # ⚠️ 兩個都要:收尾期間問「已錄多久」不能拿現在的時間去減 `_rec_t0`,那個
        # 數字會跟著收尾一路往上跳(收尾比錄音還久是常態)。
        self._rec_t0 = 0.0
        self._rec_t1 = 0.0
        # 即時逐字稿:已經畫到第幾段、以及逐段的繁化快取(見 `_rec_live_preview`)
        self._live_n = -1
        self._live_conv: dict = {}
        # 畫好的圖示。⚠️ **一定要自己留著參照**:`PhotoImage` 沒人指著就被 GC 掉,
        # 而畫面上的症狀是圖示變成空白——不會有任何錯誤。
        self._icons: dict[tuple, object] = {}
        # 正在跑的那個長工作的訊息佇列(見 `_run_job` / `_model_progress`)
        self._job_box = None
        # 預覽框裡那段文字的**原文**(Markdown 還在):核對改掛要改它、命名進度落地要存它,
        # 而畫在框裡的是 `helpmd.flatten` 剝過記號的版本(見 `_run_set_preview`)。
        self._preview_md = ""
        # 命名卡的幾個跨頁狀態(見 `_naming_show`);那一頁沒建過時也要存在。
        self._naming_src: Path | None = None       # 試聽片段從哪個音檔剪
        self._recluster_features: Path | None = None
        self._naming_draft_after = None            # 排好的「把草稿名字寫進落地」
        # 要跟著視窗寬度換行的 Label(見 `_wrap`),以及量它們寬度用的字型(每種樣式一個)
        self._wrapped: list[ttk.Label] = []
        self._fonts: dict[str, tkfont.Font] = {}
        # 每一頁詞表的三個元件:(編輯區, 狀態行, 警告行)
        self._wordlists: dict[str, tuple] = {}

        self._styles()
        self._build_ui()
        self._show(self.tab)
        # ⚠️ 綁在 `self` 而不是內容區:內容區換頁時會 pack/forget,而換行寬度只跟
        # **視窗**寬度有關。
        self.bind("<Configure>", self._refit_wraps)
        # 左欄長過視窗時的滾輪(見 `_wheel`;綁在視窗上的理由也在那裡)
        self.bind("<MouseWheel>", self._wheel)
        # ⚠️ **標題列的 X 要自己接**:預設行為是直接 destroy,錄音中按下去就是一場
        # 會議無聲無息地中斷(見 `_on_close`)。
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # ⚠️ **尺寸與位置一起設**:只給 `WxH` 的話落點不是「置中」也不是「上次那
        # 裡」——Tk 把位置交還給 Windows,而 Windows 走 `CW_USEDEFAULT` 的層疊規則
        # (每開一次往右下推一格)。⚠️ 擺法在共用包裡、**不開參數**:兩支 app 在同
        # 一台電腦上輪流開,落點的規矩不一樣就是「這台電腦的程式各有各的脾氣」。
        winui.place_window(self, self.px(WIN_W), self.px(WIN_H))
        # ⚠️ 最小尺寸擋的是「拉到看不見內容」,不是版面的目標尺寸。
        self.minsize(self.px(760), self.px(520))
        # ⚠️ **最後一行才現身**(見 `__init__` 開頭那段):尺寸、最小尺寸、停在哪一頁
        # 全部定了才 map,使用者看到的就只有最終那一幀。
        self.deiconify()

    # ---- 基礎 ----------------------------------------------------------- #
    def px(self, n: float) -> int:
        return max(1, int(round(n * self.scale)))

    def _wrap(self, label: ttk.Label, cols: int = 1, minus: int = 0) -> ttk.Label:
        r"""登記一個「要跟著視窗寬度換行」的 Label,回傳它自己(好接著 `.pack()`)。

        ⚠️ **Tk 的 Label 不會自動換行**:沒給 `wraplength` 的話,一行長字就直接被
        視窗邊緣裁掉——而且**裁得沒有任何跡象**,看起來就像那句話本來就這麼短
        (2026-08-29 文件分頁的摘要行就是這樣少講了半句「支援的格式…」)。
        ⚠️ **要跟著寬度重算**,不能給一個固定值:使用者會拉視窗,而 `wraplength`
        是像素、不是「填滿父容器」。
        ⚠️ 斷點是自己算的(`break_lines`,2026-09-03):中文字之間可以斷、英文單字不拆、
        標點避頭尾,`wraplength` 只當量錯時的安全網;換字會當場重算(見 `_rewrap`)。"""
        # 卡片裡再分成幾欄(進階參數設定那張是「模型｜CPU 核心數」兩欄,2026-09-02):
        # 記在 Label 身上,`_refit_wraps` 重算時照著除。⚠️ 不記的話兩欄裡的說明會照整張
        # 卡的寬度換行,句尾直接壓到隔壁那一欄上。
        label.wrap_cols = cols
        # 同一列右邊還有別的東西時(命名區塊的線索旁邊是那兩顆鈕),要從可用寬度裡扣掉
        # 那一段;不扣的話線索照整張卡換行,會壓到鈕上、把它擠出卡片右緣。
        label.wrap_minus = minus
        self._wrapped.append(label)
        label.wrap_out = None
        # 換字的時候要**當場**重新斷行(狀態列、線索、摘要都是後來才換字的),而斷行是
        # 我們自己算的(`break_lines`),不是 Tk——所以把這一顆的 `configure` 換成會順手
        # 重算的版本。⚠️ 是實例層的替換,只影響登記過的這幾顆;`label["text"] = …` 走
        # `__setitem__` → `configure`,一樣接得到。
        real = label.configure

        def configure(cnf=None, **kw):
            out = real(cnf, **kw)
            if configured(cnf, kw) is not _UNSET:   # 兩種寫法都要接住,見那一支
                self._rewrap(label)
            return out

        label.configure = label.config = configure
        # ⚠️ **當場先給一個值**:視窗還沒 map 時量不到寬度,而沒有 `wraplength` 的
        # Label 是「一行到底」——第一幀會是一個被自己撐爆的版面,然後才縮回來。
        # 這裡用視窗的標稱寬度先算,`_refit_wraps` 之後再依實際寬度校正。
        share, reserve = self._wrap_share(label)
        self._rewrap(label, self._wrap_width(
            self.px(MAX_CONTENT), share, cols, reserve) - minus)
        return label

    def _rewrap(self, label: ttk.Label, width: int | None = None) -> None:
        r"""把一顆登記過的 Label 照現在的寬度重新斷行(見 `break_lines`)。

        原文記在 `wrap_raw`:`cget("text")` 拿到的是斷過行的版本,跟上次寫進去的
        (`wrap_out`)不一樣就代表有人換了字,那才是新的原文。斷點由 `break_lines` 決定,
        空白不必再換成 U+00A0(2026-08-30 那個 `nbsp` 的工法是為了騙過 Tk 的 word 換行,
        現在斷點是自己算的,空白照樣是空白——複製出來的也是正常的字)。⚠️ 寫回去要走
        `ttk.Label.configure`,不能走實例上那個會重算的版本——那是無窮遞迴。"""
        if isinstance(label, tk.Text):       # `_icon_text` 那種:字與圖一起重排
            self._rewrap_text(label, width)
            return
        now = label.cget("text")
        if now != label.wrap_out:
            label.wrap_raw = now
        if width is None:
            width = getattr(label, "wrap_w", 0)
        label.wrap_w = width
        if width <= 0:
            return
        out = break_lines(label.wrap_raw, width, self._label_font(label).measure)
        label.wrap_out = out
        ttk.Label.configure(label, text=out, wraplength=width)

    def _icon_text(self, parent, text: str, *, bg: str, fg: str, pt: int = 10,
                   line_gap: int = 0) -> tk.Text:
        r"""一段會跟著視窗寬度換行、**裡面的 emoji 畫成彩色圖片**的說明文字。

        (2026-09-03 使用者圈出文件頁說明裡的「使用說明」「聲音→MD」:「上方說明文字請加上
        ICON」。)`ttk.Label` 塞不進行內圖片,Tk 畫的 emoji 又是單色線稿(見 `_icon_pil`),
        所以這一段改用 `tk.Text`:字照樣是字,emoji 用 `image_create` 換成分頁列那一張
        彩色圖(`_icon_image`,同字級)。外觀對齊 Label:沒有邊框、沒有內距、底色與字色
        照它坐的那一層給(`bg`/`fg` 是色票的鍵)、唯讀、箭頭游標。
        ⚠️ **斷行仍然是自己算的**(`break_lines`,`wrap="none"`),與其他 `_wrap` 的 Label
        同一套規則、同一個寬度來源(視窗寬度),圖的寬度算進量尺裡;高度 = 行數,由斷出來
        的文字決定,**不量 widget 自己**(那條回授的規矩見 `_wrap_width`)。
        ⚠️ `width=1`:Text 預設請求 80 個字元寬,`fill="x"` 撐開是靠父容器,但**請求寬度
        會擋住視窗縮小**——縮到比 80 個字元窄時整頁就被它撐住。
        `line_gap`(邏輯 px)是行與行之間多留的距離,**上下各半**放在 spacing1/spacing3
        (瀏覽器的 line-height 就是這樣分);⚠️ **要設在 widget 上、不能用 tag**:Tk 只把
        widget 層的 spacing 算進請求高度(實測:tag 層設 13,`reqheight` 一動也不動),用 tag
        的話最後一行會被裁掉。"""
        gap = self.px(line_gap) if line_gap else 0
        box = tk.Text(parent, font=(self.fam, pt), relief="flat", bd=0,
                      highlightthickness=0, wrap="none", cursor="arrow", width=1,
                      height=1, padx=0, pady=0, takefocus=0, state="disabled",
                      bg=self.pal[bg], fg=self.pal[fg],
                      selectbackground=self.pal["row_sel"],
                      spacing1=gap // 2, spacing3=gap - gap // 2)
        box.wrap_raw = text
        box.wrap_font = tkfont.Font(root=self, font=(self.fam, pt))
        box.wrap_bold = tkfont.Font(root=self, font=(self.fam, pt, "bold"))
        # 程式碼字:等寬、小一號、淡灰底——量網頁版:墨跡 18 對一般字 21(約 0.85em)、底
        # `#f3f4f6`(這裡給色票的 `field`)。網頁版還有一圈 `#e5e7eb` 的細框,Text 的 tag
        # 畫不出指定顏色的 1px 框(`relief` 的顏色是從底色算的陰影),沒畫。
        box.wrap_code = tkfont.Font(root=self, font=(MONO_FAMILY, max(1, pt - 1)))
        box.wrap_pt = pt
        box.tag_configure("b", font=box.wrap_bold)
        box.tag_configure("code", font=box.wrap_code, background=self.pal["field"])
        return self._wrap(box)

    def _rewrap_text(self, box: tk.Text, width: int | None = None) -> None:
        r"""`_icon_text` 那種的重排:解掉記號、照寬度斷行,再逐字塞回去——粗體與程式碼掛
        tag、emoji 換成圖。

        ⚠️ **寬度逐字量**(`break_lines` 的 `widths`):一段裡混著三種字型與圖片,粗體的
        英數比一般字寬、等寬字更寬,拿一種字型量整段就會在行尾裁掉幾個字(`wrap="none"`
        的 Text 裁得沒有任何跡象)。⚠️ 斷完行要把樣式對回去:`break_lines` 只會**多**換行、
        **少**斷點上的空白,所以兩邊用兩個指標走一遍就對得上(遇到不相等就是被拿掉的空白)。"""
        if width is None:
            width = getattr(box, "wrap_w", 0)
        box.wrap_w = width
        if width <= 0:
            return
        icons: dict = {}

        def icon(ch: str):
            if ch not in icons:
                icons[ch] = self._icon_image(ch, box.wrap_pt) if is_symbol(ch) else None
            return icons[ch]

        plain, styles = parse_inline(box.wrap_raw)
        fonts = {"": box.wrap_font, "b": box.wrap_bold, "code": box.wrap_code}
        widths = [icon(ch).width() if icon(ch) is not None else fonts[st].measure(ch)
                  for ch, st in zip(plain, styles)]
        out = break_lines(plain, width, None, widths)
        pieces: list[tuple[str, str]] = []   # (字, 樣式)
        j = 0
        for ch in out:
            if ch == "\n":
                while j < len(plain) and plain[j] == " ":
                    j += 1
                if j < len(plain) and plain[j] == "\n":
                    j += 1
                pieces.append((ch, ""))
                continue
            while j < len(plain) and plain[j] == " " and ch != " ":
                j += 1
            pieces.append((ch, styles[j] if j < len(plain) and plain[j] == ch else ""))
            j += 1
        box.configure(state="normal")
        box.delete("1.0", "end")
        run, run_st = "", ""

        def flush() -> None:
            nonlocal run
            if run:
                box.insert("end", run, (run_st,) if run_st else ())
                run = ""

        for ch, st in pieces:
            photo = icon(ch)
            if photo is not None:
                flush()
                box.image_create("end", image=photo, align="center")
                continue
            if st != run_st:
                flush()
                run_st = st
            run += ch
        flush()
        box.wrap_out = out
        box.configure(state="disabled", height=out.count("\n") + 1)

    def _label_font(self, label: ttk.Label):
        """一顆 Label 實際用的字型(照樣式查,`Hint.TLabel` 9pt、`Field.TLabel` 10pt 粗體…),
        量寬度用;同一種樣式只建一次。"""
        style = str(label.cget("style") or "TLabel")
        font = self._fonts.get(style)
        if font is None:
            spec = ttk.Style(self).lookup(style, "font") or "TkDefaultFont"
            font = self._fonts[style] = tkfont.Font(root=self, font=spec)
        return font

    def _col_width(self, body: int, share: float,
                   reserve: int | None = None) -> int:
        r"""一欄有多寬(`body` = 內容區寬度,`reserve` = 分欄前先扣掉的溝與捲軸)。

        ⚠️ **版面與換行寬度只能有這一條式子**:`_wrap_width` 與真的 grid 出來的欄寬
        各算各的話,差的那幾像素只有「最後一個字被切掉一半」看得出來,而那正是
        2026-08-30 修過兩次的症狀。"""
        if reserve is None:
            reserve = self.px(CARD_GAP) if share < 1.0 else 0
        return int((body - reserve) * share)

    def _wrap_width(self, body: int, share: float, cols: int = 1,
                    reserve: int | None = None) -> int:
        r"""一個 Label 該在幾像素處換行(`body` = 內容區寬度)。

        ⚠️ **只能由視窗寬度決定,不准由 Label 所在的容器決定**(2026-08-30 實測到的
        災情):量容器寬度會形成**回授**——換行寬度 ← 欄寬 ← Label 的請求寬度 ←
        換行寬度,每一次 `<Configure>` 就往內縮一格。使用者看到的是**開窗之後預覽框
        自己一格一格變大**;實測從開窗到收斂要 2.2 秒(左欄 967→513、預覽 346→800),
        而且每一步都看得見。
        ⚠️ 單欄的頁面(share = 1)要退回**原本那條式子**:它本來就是穩定的,這次
        壞掉的只有兩欄那條路。
        ⚠️ **`reserve` 是「整頁先扣掉幾像素才分欄」,不能從 `share` 推**(2026-09-04
        排三欄時發現):兩欄要扣一條溝,三欄要扣**兩條**、而且名單與聲紋那一頁還要扣
        捲軸那一格。先前是「share < 1 就扣一條溝」,照那條算三欄的每一欄都會多算——
        說明小字的句尾就壓到隔壁欄上。⚠️ **分欄寬度與這裡必須同一條式子**
        (`_split`),兩邊各算各的就是那種「差幾像素、只有最後一個字看得出來」的錯。
        不給 `reserve` 就沿用舊行為。"""
        inner = self._col_width(body, share, reserve) - self.px(CARD_PAD) * 2
        # 卡片裡再分成 `cols` 欄時,欄與欄之間留 `CARD_GAP`、每一欄各扣一份鬆度
        # (`cols` = 1 就是原本那條式子,一個數字都沒變)
        return (inner - self.px(CARD_GAP) * (cols - 1)) // cols - self.px(SP_MD)

    def _two_columns(self, page: ttk.Frame, key: str) -> "TwoCols":
        r"""把一頁排成**左操作、右結果**的兩欄(5:7),左欄可捲動;轉檔頁與文件頁共用。

        (2026-08-30 轉檔頁照網頁版重排時做的;2026-09-03 文件頁也照網頁版排成兩欄,抽出來
        共用——網頁版兩頁都是 `Column(scale=5) / Column(scale=7)`。)
        ⚠️ **用 grid 不用 pack**:兩欄要的是 5:7 的**權重**,而 pack 只有「平分」或「照
        請求大小」,給不了比例。
        ⚠️ **中間那條溝是獨立的一欄**(2026-09-02):它平常空著(寬度就是 `CARD_GAP`),
        左欄長過視窗時**捲軸住在裡面**。捲軸放進左欄的話會把卡片擠窄 12px,而說明小字的
        換行寬度是照欄寬的**常數**算的(`_wrap_width`,刻意不量),窄了那 12px 就是句尾被
        右邊界裁掉——那正是 2026-08-30 修過的那顆。
        ⚠️ **兩欄的寬度要寫死,不准讓內容決定**(2026-08-30 使用者回報):grid 的欄寬取決
        於**內容的請求寬度**,而命名區那張卡比「要做什麼」寬——它一出現,左欄就撐大、整個
        版面往右跳一次。關掉 propagate 之後欄寬只由視窗決定(`_refit_columns`)。
        ⚠️ **是 `pack_propagate` 不是 `grid_propagate`**(2026-08-30 實測):兩欄自己是用
        `grid` 放進 page 的,但**它們的子元件是用 `pack` 排的**——傳播開關跟著子元件那一側
        的 geometry manager。關錯的症狀不是「沒效果」而是「看起來有效但其實沒有」:
        `cget("width")` 是我設的 587,而 `winfo_reqwidth()` 照樣跟著內容跑,兩欄互相搶,
        畫面持續抖動。
        ⚠️ **左欄是可捲動的**(2026-09-02,照網頁版「頁面長過視窗就捲」):它有會長過視窗的
        東西(命名區 20 多位、展開的「進階參數設定」)。先前長過的那一段是**無聲地被裁掉**:
        pack 分不到空間就把最後那張卡縮短,卡片的圓角照畫,看起來像那張卡本來就只有這麼長
        (2026-09-02 截圖才看出「CPU 核心數」整段不在畫面上)。`ttk` 沒有可捲動的 Frame,
        自己用 Canvas ＋ 內層 Frame 兜;捲軸只在內容比欄高時才出現。
        ⚠️ **`yscrollincrement` 不可以設**(2026-09-02 量到的):它不只是滾輪的步幅,Tk 會
        把捲動**原點**一律取整成它的倍數——`yview_moveto(1.0)` 之後原點被往回推到上一個
        倍數,最底下永遠差 0~15 px 露不出來(實測內容 596、可見 264:原點該是 332,被取整成
        320)。而「展開進階參數設定之後捲到底」要的正是最底下那一段。滾輪的固定步幅改由
        `_wheel` 自己用像素算。
        ⚠️ **東西要放進 `inner` 不是 `left`**:`_wrap_share` 往上找的是 `left`,而
        inner → canvas → left 這條鏈仍然會經過它。"""
        page.columnconfigure(0, weight=5)
        page.columnconfigure(1, minsize=self.px(CARD_GAP))
        page.columnconfigure(2, weight=7)
        page.rowconfigure(0, weight=1)
        left = ttk.Frame(page, style="Page.TFrame")
        left.grid(row=0, column=0, sticky="nsew")
        right = ttk.Frame(page, style="Page.TFrame")
        right.grid(row=0, column=2, sticky="nsew")
        left.pack_propagate(False)
        right.pack_propagate(False)
        canvas = tk.Canvas(left, bg=self.pal["page"], highlightthickness=0, bd=0)
        canvas.pack(fill="both", expand=True)
        inner = ttk.Frame(canvas, style="Page.TFrame")
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        bar = ttk.Scrollbar(page, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        cols = TwoCols(canvas=canvas, inner=inner, win=win, scrollbar=bar,
                       left=left, right=right)
        self._cols[key] = cols
        self._scrolls[key] = cols
        self._shares[left] = (RUN_LEFT, self.px(CARD_GAP))
        canvas.bind("<Configure>", lambda _e: self._scroll_refit(cols))
        inner.bind("<Configure>", lambda _e: self._scroll_refit(cols))
        self._refit_columns(self.px(WIN_W) - self.px(PAGE_PAD) * 2)
        return cols

    def _refit_columns(self, body: int) -> None:
        r"""把每一頁兩欄的寬度釘成 5:7——**與 `_wrap_share` 同一個來源**。

        ⚠️ 不釘的話欄寬由內容決定,而命名區那張卡比其他卡寬:轉完檔它一出現,左欄
        就撐大、右邊的預覽跟著位移。使用者看到的是「畫面自己動了一下」。"""
        wide = int((body - self.px(CARD_GAP)) * RUN_LEFT)
        # ⚠️ **高度要給一個非零、而且很小的值**,兩邊都是實測換來的:
        #   **①** 高度 0 的 widget Tk **不會 map**(`ismapped=0`、`winfo_width()`
        #     永遠回 1)——`grid_propagate(False)` 把寬**與高**都交給 `configure()`。
        #   **②** 但也**不能給整個視窗高**:那會讓頁面 → body → 視窗的請求高度被一路
        #     推大,視窗一變大就再觸發一次 `<Configure>`,於是又是一個回授迴圈
        #     ——實測是切到「名單與聲紋」再切回「轉檔」時**整個卡住**。
        # ⚠️ **而且必須是固定常數,不准從量到的高度推導**:設成「`_body` 現在的高度」
        #   會讓 body 的**請求**高度變成自己當下的高度,視窗因此再長高一點 → 又一次
        #   `<Configure>` → 再長高……實測是切到「名單與聲紋」再切回「轉檔」時整個卡住。
        # 只要這個常數比實際可用高度小,視窗就不會被推大;實際高度照樣由
        # `sticky="nsew"` 拉滿整格。
        for cols in self._cols.values():
            if not cols.left.winfo_exists():
                continue
            for col, w in ((cols.left, wide),
                           (cols.right, body - self.px(CARD_GAP) - wide)):
                col.configure(width=w, height=self.px(COL_SEED_H))

    def _scroll_refit(self, cols: "ScrollBox") -> None:
        r"""可捲動的容器(兩欄頁的左欄、整頁捲動那一頁的內容區):內層寬度跟著欄寬、scrollregion 跟著內容,捲軸按需要出現。

        ⚠️ **不可以從量到的高度回填任何「請求」尺寸**(同 `_refit_columns` 那條回授):
        這裡改的只有 Canvas item 的寬度與 scrollregion,兩者都不進尺寸傳播。
        ⚠️ **捲軸 grid 在溝裡、不在左欄裡**:左欄的寬度是釘死的,捲軸擠進去就是卡片
        變窄(見 `_two_columns`);溝是 `CARD_GAP` 寬(30 實體 px @150%),捲軸 12px 住得下。
        ⚠️ 內容縮回去(收起進階參數設定、命名區收掉)時要**捲回頂端**:不捲回去的話
        scrollregion 變小、視野卻停在原來的偏移,畫面上是一片空白。
        ⚠️ **`stretch` 那一支要連高度一起釘**(2026-09-04 使用者:「三張卡片請做成一樣
        高…高度請以拉到最下面」):Canvas 的 window item 預設用內層的**請求**高度,而
        那是「最高的那張卡有多高」——三張卡於是各長各的。把 item 撐到可視高度,裡面的
        `rowconfigure(weight=1)` ＋ `sticky="nsew"` 才拉得到底。⚠️ **取的是與內容的
        較大者**:內容更高時撐成可視高度就是把它裁掉,而那正是這一頁本來的病。
        ⚠️ 這**不算**上面那條「不可以回填請求尺寸」的例外:item 的高度不會回頭變成
        `inner` 的請求高度(它仍然由裡面的卡片決定),所以沒有回授。"""
        canvas, inner = cols.canvas, cols.inner
        w, h = canvas.winfo_width(), canvas.winfo_height()
        if w > 1:
            canvas.itemconfigure(cols.win, width=w)
        need = inner.winfo_reqheight()
        if cols.stretch and h > 1:
            canvas.itemconfigure(cols.win, height=max(h, need))
        canvas.configure(scrollregion=(0, 0, max(w, 1), max(need, 1)))
        if need > h > 1:
            cols.scrollbar.grid(row=0, column=1, sticky="ns")
        else:
            cols.scrollbar.grid_remove()
            canvas.yview_moveto(0)

    def _scroll_top(self, key: str) -> None:
        r"""某一頁的左欄**整批換過內容**之後:重算 scrollregion,並捲回頂端。

        ⚠️ **不能只靠 `<Configure>`**(2026-09-04 使用者回報「設定完講者,左邊的功能選單
        不見了」,實測重現):內層 Frame 是 Canvas 的 window item,而 Tk **不會**在它的
        請求高度變小時把 item 縮回去——量到的是 `winfo_reqheight()` 497、
        `winfo_height()` 還是 1180,於是 `<Configure>` 從頭到尾沒送出來,
        `_scroll_refit` 也就沒被叫到。症狀:命名時左欄長到 1180、使用者捲到底按「套用
        名字」,套用後內容縮回 497,scrollregion 卻還停在 1180、視野還停在 y=592——
        大卡與動作列全在視野**上方**(Tk 連 map 都不 map 它們了),畫面上只剩最底下那張
        「進階參數設定」,看起來就是功能選單整組消失。
        ⚠️ **`update_idletasks()` 少不得**(同 `_fold_toggle`):`pack`/`pack_forget` 之後
        請求高度要等閒置佇列跑完才是新的,拿舊值算出來的 scrollregion 一樣是錯的。
        ⚠️ **`yview_moveto(0)` 要自己補一次**:`_scroll_refit` 只在「內容放得下」那一支
        捲回頂端,而換上來的內容也可能比視窗高(命名卡二十幾位就是),那時同樣得從頭看。"""
        cols = self._scrolls.get(key)
        if cols is None or not cols.canvas.winfo_exists():
            return
        self.update_idletasks()
        self._scroll_refit(cols)
        cols.canvas.yview_moveto(0)

    def _wheel(self, event) -> None:
        r"""滾輪捲左欄——**只在指標落在某一頁的左欄裡、而且那一欄真的長過視窗時**。

        ⚠️ **綁在視窗上,不是綁在 Canvas 上**:Tk 把滾輪送給**指標底下**的 widget,而那是
        卡片裡的某個 Label、不是 Canvas;每個 widget 的 bindtags 都含所在 toplevel,所以
        綁在視窗上一次就接得到整個左欄(class binding 先跑、這一支後跑)。
        ⚠️ **自己會捲(或會吃滾輪)的東西讓路**:預覽框、名單表、下拉、Spinbox 的 class
        binding 已經處理過這一下(Spinbox 是拿滾輪**改數字**),再捲一次欄位就是「滾一格
        動兩個東西」。⚠️ 判斷用 `grid_info()` 不用 `winfo_ismapped()`:測試那個藏起來的
        視窗什麼都沒 map。"""
        canvases = {box.canvas: box for box in self._scrolls.values()}
        node = self.winfo_containing(event.x_root, event.y_root)
        while node is not None and node not in canvases:
            if isinstance(node, (tk.Text, tk.Listbox, ttk.Treeview, ttk.Spinbox,
                                 ttk.Combobox, ttk.Scrollbar)):
                return
            node = node.master
        if node is None:
            return
        cols = canvases[node]
        if not cols.scrollbar.grid_info():
            return
        # 固定步幅(三格尺規)用像素換算成 scrollregion 的比例來捲。⚠️ 不用 `yview_scroll(n,
        # "units")`:沒有 `yscrollincrement` 時一格是「可見高度的十分之一」,滾一下跳多遠
        # 跟著視窗高度變;而那個增量本身又不能設(理由見 `_two_columns`)。
        region = str(cols.canvas.cget("scrollregion")).split()
        total = float(region[3]) if len(region) == 4 else 0.0
        if total <= 0:
            return
        step = 3 * self.px(SP_LG) / total
        top = cols.canvas.yview()[0]
        cols.canvas.yview_moveto(max(0.0, top - step if event.delta > 0 else top + step))

    def _dark(self) -> bool:
        """現在是不是深色佈景(拿欄位底色的亮度判斷——palette 沒有給旗標)。"""
        try:
            r, g, b = self.winfo_rgb(self.pal["field"])
        except tk.TclError:                 # 佈景還沒套上
            return False
        return (r + g + b) / 3 < 32768

    def _mark_rivals(self, combo: ttk.Combobox, count: int) -> None:
        r"""下拉展開後最前面 `count` 筆標成琥珀底(= 聲紋分不開的候選 / 這場會議裡的人)。

        ⚠️ **只改順序不標色是不夠的**(網頁版 2026-08-15 第一版就是那樣,使用者當場
        回報):提到最前面而沒有視覺區隔,他會以為那是名單本來的順序——名單一長
        (實測 28 人)反而更難找。
        ⚠️ **標示只能做在視覺層**:選項字串**就是**寫進逐字稿與聲紋庫的名字,把記號
        寫進字串裡,那個記號就會變成人名的一部分(見 `naming.rival_order`)。
        清單是自畫的(`_pick_open`),展開時照這裡記的筆數給 tag;筆數與順序必須出自
        同一次計算,否則底色落在別人身上。"""
        self._combo_rivals[str(combo)] = max(0, int(count))

    # ---- 下拉選單:自畫的清單(案 ②)------------------------------------ #
    def _combo_setup(self, combo: ttk.Combobox) -> None:
        r"""把 `ttk.Combobox` 的清單換成自畫的(2026-09-02 使用者三案實拍後選 ②)。

        輸入格仍是 ttk 的(圓角底板 `Sq.field`、textvariable、打字都不變),**只有展開的
        清單換掉**:Tk 內建的 popdown 是 Listbox,沒有列內距,hover 那一列一定帶 3D 邊——
        使用者要的「灰底、沒有陰影、行距有空間」它給不了(見 `PICK_*` 的註解)。自畫的清單
        (`_pick_open`)照網頁版:列高 28、hover 是平的灰底、目前那一筆前面打 ✓、圓角、捲軸
        在框裡;先前補的三件(點整條就展開、展開後直接打字、圓角)與候選的琥珀底都保留。
        ⚠️ **Tk 自己的展開要擋掉**:`<ButtonPress-1>` 與 `<Down>` 的 widget 綁定回
        `break`,類別綁定(`ttk::combobox::Press` / `Post`)就不會跑——否則兩張清單一起開。
        代價是文字區的游標位置要自己放(`icursor @x`),不然打字永遠接在最後面。
        ⚠️ **下拉被銷毀(換檔、重新分群、關核對視窗)時清單要跟著關**:清單是另一個
        toplevel、不會跟著死,還握著 grab——那就是「整個程式點不動」。"""
        combo.bind("<ButtonPress-1>", lambda e, c=combo: self._pick_press(c, e))
        combo.bind("<Down>", lambda _e, c=combo: self._pick_toggle(c))
        combo.bind("<Destroy>", lambda _e, c=combo: self._pick_close_for(c))

    def _pick_press(self, combo: ttk.Combobox, event) -> str:
        """按在輸入格的任何地方 → 展開清單;已經開著 → 收起來(同 Tk 內建的切換)。"""
        if combo.instate(["disabled"]):
            return "break"
        if self._pick is not None and self._pick["combo"] is combo:
            self._pick_close()
            return "break"
        if str(combo.identify(event.x, event.y)).endswith("textarea"):
            combo.focus_set()
            combo.icursor(f"@{event.x}")
        self._pick_open(combo)
        return "break"

    def _pick_toggle(self, combo: ttk.Combobox) -> str:
        """鍵盤 ↓:開;開著就關。回 `break` 擋掉 Tk 自己的 Post。"""
        if self._pick is not None and self._pick["combo"] is combo:
            self._pick_close()
        elif not combo.instate(["disabled"]):
            self._pick_open(combo)
        return "break"

    def _pick_open(self, combo: ttk.Combobox) -> None:
        r"""把清單開在輸入格正下方(放不下就放上面,同 Tk 自己的 PlacePopdown)。

        ⚠️ `overrideredirect` + `-topmost`(同 ttk 的 popdown):沒有標題列、不進工作列、
        浮在主視窗上。⚠️ **要 grab**:按在清單以外的任何地方都要收起來,而且那一下**不能**
        再傳給底下的元件(否則按輸入格會關了又開)——Tk 內建的也是靠 grab 做到的。
        ⚠️ **焦點給清單**:↑↓ Enter Esc 才有反應;可列印的鍵轉回輸入格(`_combo_typed`)。
        ⚠️ 焦點跑到別的程式(grab 管不到那裡)時也要收:`<FocusOut>` 之後下一拍看焦點還在
        不在清單上。"""
        self._pick_close()
        values = [str(v) for v in combo.tk.splitlist(combo.cget("values"))]
        if not values:
            return
        win = tk.Toplevel(combo)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(background=self.pal["card"])
        rows = min(len(values), PICK_MAX_ROWS)
        tree = ttk.Treeview(win, columns=("mark", "name"), show="tree",
                            style="Pick.Treeview", selectmode="browse", height=rows)
        tree.column("#0", width=0, stretch=False)
        tree.column("mark", width=self.px(26), anchor="center", stretch=False)
        tree.column("name", anchor="w")
        tree.tag_configure("rival", background=(RIVAL_AMBER_DARK if self._dark()
                                                else RIVAL_AMBER_LIGHT))
        current = combo.get().strip()
        n_rivals = self._combo_rivals.get(str(combo), 0)
        for i, name in enumerate(values):
            tree.insert("", "end", iid=str(i),
                        values=(PICK_MARK if name == current else "", name),
                        tags=("rival",) if i < n_rivals else ())
        pad = self.px(SP_XS + 2)
        scrolls = len(values) > rows
        if scrolls:
            sb = ttk.Scrollbar(win, orient="vertical", command=tree.yview)
            tree.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y", pady=pad)
        tree.pack(side="left", fill="both", expand=True,
                  padx=(pad, 0 if scrolls else pad), pady=pad)
        # 開起來停在目前的值那一列(捲到看得見);沒有就第一列
        start = values.index(current) if current in values else 0
        tree.selection_set(str(start))
        tree.focus(str(start))
        tree.see(str(start))
        x = combo.winfo_rootx()
        y = combo.winfo_rooty() + combo.winfo_height() + self.px(2)
        w, h = max(combo.winfo_width(), 1), rows * self.px(PICK_ROW_H) + pad * 2
        if y + h > combo.winfo_screenheight():
            y = combo.winfo_rooty() - h - self.px(2)
        win.geometry(f"{w}x{h}+{x}+{y}")
        tree.bind("<Motion>", self._pick_hover)
        tree.bind("<ButtonRelease-1>", self._pick_click)
        tree.bind("<Return>", lambda _e: self._pick_choose())
        tree.bind("<Escape>", lambda _e: self._pick_close())
        tree.bind("<KeyPress>", lambda e, c=combo: "break" if self._combo_typed(c, e.char)
                  else None)
        tree.bind("<FocusOut>", lambda _e: self.after_idle(self._pick_focus_lost))
        win.bind("<ButtonPress-1>", self._pick_outside)                # 按在清單以外
        win.bind("<Map>", lambda _e, p=str(win): self._round_popup(p))
        self._pick = dict(win=win, tree=tree, combo=combo, values=values)
        win.update_idletasks()
        try:
            win.grab_set()
        except tk.TclError:                  # 視窗還沒 viewable(測試那個藏起來的視窗)
            logger.debug("下拉清單 grab 失敗", exc_info=True)
        tree.focus_set()

    def _pick_hover(self, event) -> None:
        """滑鼠移到哪一列,那一列就是選中(灰底)——同網頁版的 hover。"""
        pick = self._pick
        if pick is None:
            return
        iid = pick["tree"].identify_row(event.y)
        if iid:
            pick["tree"].selection_set(iid)

    def _pick_click(self, event) -> None:
        """放開滑鼠在哪一列,就選那一筆(用 Release 不用 Press:同 Tk 內建,拖過去不算)。"""
        pick = self._pick
        if pick is None:
            return
        iid = pick["tree"].identify_row(event.y)
        if iid:
            pick["tree"].selection_set(iid)
            self._pick_choose()

    def _pick_outside(self, event) -> None:
        r"""按在清單**以外** → 收;按在清單自己的列或捲軸上不收。

        (2026-09-03 使用者:「聲紋庫『把』後的下拉式選單,可以下拉選擇,但是選不進欄位」。)
        ⚠️ **Tk 的 toplevel 綁定對它裡面每一個 widget 都會跑**(bindtags 裡有 toplevel):先前
        這條綁定一律 `_pick_close()`,於是按在列上清單先被收掉、放開那一下(`_pick_click`)
        就沒東西可選——真視窗用滑鼠事件重現:按下去 `_pick` 已經是 None。grab 把清單以外的
        按下送到 toplevel **自己**身上(`event.widget` 就是它),按在列上 `widget` 是 Treeview,
        看這一點就分得出來。"""
        if self._pick is not None and event.widget is self._pick["win"]:
            self._pick_close()

    def _pick_choose(self) -> None:
        """把選中那一筆填進輸入格、收清單、焦點回輸入格。"""
        pick = self._pick
        if pick is None:
            return
        sel = pick["tree"].selection()
        self._pick_close()
        if not sel:
            return
        combo = pick["combo"]
        combo.set(pick["values"][int(sel[0])])   # textvariable 一起變,trace 照常跑
        combo.icursor("end")
        combo.focus_set()
        combo.event_generate("<<ComboboxSelected>>")

    def _pick_focus_lost(self) -> None:
        pick = self._pick
        if pick is None:
            return
        try:
            focused = self.focus_get()
        except (KeyError, tk.TclError):      # 焦點在別的程式或已銷毀的視窗上
            focused = None
        if focused is not pick["tree"]:
            self._pick_close()

    def _pick_close(self) -> None:
        pick, self._pick = self._pick, None
        if pick is None:
            return
        win = pick["win"]
        try:
            win.grab_release()
        except tk.TclError:
            pass
        if win.winfo_exists():
            win.destroy()

    def _pick_close_for(self, combo: ttk.Combobox) -> None:
        """某個下拉被銷毀:它的清單跟著關、候選筆數忘掉。"""
        self._combo_rivals.pop(str(combo), None)
        if self._pick is not None and self._pick["combo"] is combo:
            self._pick_close()

    def _combo_typed(self, combo: ttk.Combobox, ch: str) -> int:
        """清單展開中按了一個可列印的鍵:收清單、焦點回輸入格、把字打進去。回 1 表示
        吃掉了這個鍵(清單那邊據此 `break`);控制鍵回 0 放行給清單自己(↑↓ Enter Esc)。"""
        if len(ch) != 1 or not ch.isprintable():
            return 0
        try:
            self._pick_close()
            combo.focus_set()
            combo.insert("insert", ch)
        except tk.TclError:
            logger.debug("把按鍵轉給下拉輸入格失敗", exc_info=True)
        return 1

    def _round_popup(self, pop: str) -> None:
        r"""把下拉展開的那個清單視窗切成圓角(它是 override-redirect 的 toplevel)。

        Windows 11 用 DWM 的 `DWMWA_WINDOW_CORNER_PREFERENCE`(33)= `DWMWCP_ROUND`(2),
        角是反鋸齒的、還帶系統那圈細邊;Windows 10 沒有這個屬性(回 E_INVALIDARG),退回
        `SetWindowRgn` 硬切一個圓角矩形(角會鋸齒,但至少是圓的)。
        ⚠️ **HWND 要往上取一層**(`GetParent`,同 `winui.window_handle` 那條):`winfo id`
        給的是 Tk 自己那個子視窗,DWM 不認。⚠️ `restype` 一定要設成指標寬,理由也在那裡。
        ⚠️ 每次 `<Map>` 都做:清單的高度隨選項數變,`SetWindowRgn` 那條路的區域要跟著重算;
        DWM 那條重複設定沒有代價。任何一步失敗只記 log——清單照樣能用,只是方角。"""
        import ctypes

        try:
            user32 = ctypes.windll.user32
            user32.GetParent.restype = ctypes.c_void_p
            user32.GetParent.argtypes = [ctypes.c_void_p]
            # ⚠️ `winfo id` 回的是 "0x240efe" 這種十六進位字串,`int(x, 0)` 才讀得懂
            # (Tkinter 的 `winfo_id()` 也是這樣做的);第一版寫 `int(x)` 當場 ValueError,
            # 而它只在紀錄檔留一行 DEBUG,畫面上就是清單照舊方角。
            child = int(str(self.tk.call("winfo", "id", pop)), 0)
            hwnd = user32.GetParent(child) or child
            pref = ctypes.c_int(2)                        # DWMWCP_ROUND
            hr = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd), 33, ctypes.byref(pref), ctypes.sizeof(pref))
            if hr == 0:
                return
            w = int(self.tk.call("winfo", "width", pop))
            h = int(self.tk.call("winfo", "height", pop))
            r = self.px(10) * 2                           # 直徑;半徑照 `SQ_R_BOX`
            gdi32 = ctypes.windll.gdi32
            gdi32.CreateRoundRectRgn.restype = ctypes.c_void_p
            rgn = gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, r, r)
            user32.SetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
            user32.SetWindowRgn(ctypes.c_void_p(hwnd), rgn, 1)
        except Exception:
            logger.debug("下拉清單切圓角失敗(清單照樣能用)", exc_info=True)

    def _wrap_share(self, label) -> tuple[float, int]:
        r"""這個 Label 所在的那一欄佔內容區的幾成、整頁要先扣掉幾像素——**只看它在
        哪一欄,不量任何寬度**(見上)。

        欄是在建版面時登記進 `_shares` 的(兩欄頁的左欄、名單與聲紋那三欄);往上找到
        的**第一個**登記過的容器說了算,所以巢狀時裡層優先。都沒登記就是單欄整頁。"""
        node = label
        while node is not None:
            got = self._shares.get(node)
            if got is not None:
                return got
            node = node.master
        return (1.0, 0)

    def _refit_wraps(self, event=None) -> None:
        r"""版面寬度變了,重算每一個登記過的 Label 該在幾像素處換行。

        ⚠️ **要問「自己的父容器」有多寬,不能問視窗**(2026-08-30 改):先前是拿
        `self._body` 的寬度給每一個 Label 用,那在**單欄**時剛好成立;兩欄之後左欄
        只有整頁的 5/12,而 Label 還照整頁的寬度換行——結果是**句子被右邊界裁掉**,
        而且裁得沒有任何跡象(命名區那句「填錯會把聲紋存到別人名下」只剩半句)。
        ⚠️ 內距要自己扣掉:`winfo_width()` 給的是**含 padding 的外框寬**,而 Label
        能用的只有內側。"""
        # ⚠️ **只理會視窗自己的 Configure**(2026-08-30 實測到的根):Tk 裡每個
        # widget 的 bindtags 都含**所在 toplevel 的路徑**,所以綁在視窗上的
        # `<Configure>` 會收到**每一個子元件**的 Configure——實測靜止畫面三秒收到
        # 72 次。而這一支自己又會 `configure()` 別的元件 → 再生 Configure → 再進來
        # 一次,那就是「版面持續抖動」的來源。⚠️ 先前那幾顆(換行寬度回授、欄寬跳
        # 動、切分頁卡死)全是這一個根長出來的,別只修表面。
        if event is not None and event.widget is not self:
            return
        # ⚠️ **修剪要在提早 return 之前**:它與寬度無關,而死掉的 widget 留著就是
        # 下一次還會再炸一次。
        # ⚠️ **先把死掉的清掉**:命名區每轉一檔就整批重建(`_naming_show` 會
        # `destroy()` 舊的那幾列),而那些 Label 還留在這張清單上。碰到它就是
        # `TclError: bad window path name`,而且**迴圈當場中斷**——後面所有 Label
        # 從此不再重算換行,拉視窗也不會變。⚠️ 它只在紀錄檔留一行:Tk 把
        # `<Configure>` 的 traceback 吞掉,畫面上什麼都沒有。
        self._wrapped = [w for w in self._wrapped if w.winfo_exists()]
        # ⚠️ **寬度由這一支算、不去問 `winfo_width()`**:內容區的寬度現在是「視窗
        # 放得下的」與 `MAX_CONTENT` 的較小者(見 `_refit_shell`),而剛設完的那一刻
        # `winfo_width()` 還是舊值。
        body = self._refit_shell()
        if body < self.px(200):             # 還沒排版完,等下一次 Configure
            return
        self._refit_columns(body)
        # 換字的那些不必等這裡(`_wrap` 把每一顆的 `configure` 換成會當場重算的版本),
        # 這裡只管寬度變了;寬度沒變的那幾顆 `_rewrap` 會算出同一個結果,便宜。
        for label in self._wrapped:
            share, reserve = self._wrap_share(label)
            width = self._wrap_width(body, share,
                                     getattr(label, "wrap_cols", 1), reserve
                                     ) - getattr(label, "wrap_minus", 0)
            if width >= self.px(160) and width != getattr(label, "wrap_w", None):
                self._rewrap(label, width)

    def _icon_pil(self, char: str, size: int):
        r"""把一顆 emoji 用 Segoe UI Emoji 的**彩色**字形畫成 Pillow 圖(裁到墨跡)。

        ⚠️ **Tk 的文字繪製畫不出彩色 emoji**:它拿的是 Segoe UI Emoji 的**單色**
        字形,再染上 widget 的前景色——所以先前選中時那顆是「藍色的麥克風」,不是
        彩色的。彩色字形(COLR/CPAL)只有 Pillow 的 `embedded_color=True` 畫得出來,
        所以繞道畫成圖片再塞回 widget。
        ⚠️ **`size` 就是旁邊文字的 em,emoji 照那個 em 畫、不縮**(使用者 2026-09-03:
        「圖示的高度與文字要相同」→「跟 WEB 做成相同」,於是量了他的網頁版截圖 @150%):
        分頁列 emoji 21px、中文字面 19px;試聽鈕 emoji 20px、字 18px——瀏覽器本來就把
        emoji 畫滿 em,而中文字面只佔 em 的九成,**emoji 比字高約 2px 正是網頁版的樣子**。
        本機同字級畫出來是同一組數字(20 對 18)。⚠️ 曾試過把 emoji 縮到中文字面的高度,
        那反而與網頁版不同,不要再做。天生矮一截的 emoji(📄 在網頁版只有 14px)照它
        自己的字形,同樣與網頁版一致。
        ⚠️ 回 `None` 代表這台機器畫不出來(沒有那個字型、或 Pillow 出問題),呼叫端
        要退回文字。"""
        try:
            from PIL import Image, ImageDraw, ImageFont

            font = ImageFont.truetype(str(EMOJI_FONT), size)
            pad = size // 4

            def draw(text: str):
                im = Image.new("RGBA", (size * 2, size * 2), (0, 0, 0, 0))
                ImageDraw.Draw(im).text((pad, pad), text, font=font,
                                        embedded_color=True)
                # ⚠️ **裁到墨跡本身**(2026-08-29 使用者回報「圖示比文字高一點」):
                # 畫布底下留白、字形卻貼著上緣的話,Label 置中的是**畫布**、不是圖案
                # ——看起來就是整顆往上跑。裁掉之後圖片就是圖案,置中才是真的置中。
                box = im.getbbox()
                return im.crop(box) if box else None

            # ⚠️ **字型裡沒有這個字時 FreeType 不報錯,畫的是 .notdef**(一個空框):
            # 「→」「─」這類符號落在 emoji 的碼位範圍裡、Segoe UI Emoji 卻沒有它們,
            # 直接畫就是說明內文裡冒出一排方框。拿一個保證不存在的碼位(U+FFFF 是
            # 非字元)畫一次當對照,長得一模一樣就是「沒有這個字」,回 `None` 讓呼叫端
            # 留在文字。
            key = ("notdef", size)
            if key not in self._icons:
                blank = draw("\uffff")
                self._icons[key] = blank.tobytes() if blank is not None else b""
            im = draw(char)
            if im is None or im.tobytes() == self._icons[key]:
                return None
            return im
        except Exception:                # 沒有那個字型的機器:退回文字
            logger.debug("彩色圖示畫不出來,退回文字", exc_info=True)
            return None

    def _icon_image(self, char: str, pt: int, *, grey: bool = False, gap: int = 0):
        r"""把一顆 emoji 畫成圖片:**一律彩色**,`grey` 只給停用的按鈕。

        (使用者 2026-09-03 拿網頁版對照:「所有選單上的 ICON……在還沒有選取的時候,
        就顯示顏色」——網頁版的 emoji 是瀏覽器畫的彩色字形,選不選中都一樣;先前這裡
        沒選中就轉灰階,是 2026-08-29「選到的時候變彩色」那一版的做法,已作廢。)

        ⚠️ **兩種狀態都用圖片**,不是「選中才換成圖片」:混用圖片與文字的話,兩者
        寬度不同,切分頁時整排會左右跳一下。
        ⚠️ **順帶解掉了基線那件事**:圖片是自己一個 Label、上下由 `anchor` 置中,
        不再需要 `NAV_LIFT` 那種補償——但**文字後備那條路還是要**(見 `_nav_icon`)。
        `gap` 是右側多留的透明實體像素——按鈕與目錄把圖示和文字塞在**同一個**
        widget 裡(`compound="left"`),ttk 不在兩者之間留縫,只能畫進圖裡。
        ⚠️ 回 `None` 代表這台機器畫不出來,呼叫端要退回文字。"""
        key = (char, pt, grey, gap)
        if key in self._icons:
            return self._icons[key]
        photo = None
        im = self._icon_pil(char, self.px(pt) * 4 // 3)   # pt → px(72dpi → 96dpi)
        if im is not None:
            try:
                from PIL import Image, ImageTk

                if grey:
                    # 停用的按鈕:轉灰階再壓淡,與旁邊變灰的字同一個份量(網頁版是
                    # 整顆鈕 opacity .5)。⚠️ **alpha 要留著**:整張轉成 "L" 會連透明區
                    # 一起變成黑色方塊。
                    g = im.convert("L").point(lambda v: 96 + v * 96 // 255)
                    im = Image.merge("RGBA", (g, g, g, im.getchannel("A")))
                if gap:
                    wide = Image.new("RGBA", (im.width + gap, im.height), (0, 0, 0, 0))
                    wide.paste(im, (0, 0))
                    im = wide
                photo = ImageTk.PhotoImage(im)
            except Exception:
                logger.debug("彩色圖示畫不出來,退回文字", exc_info=True)
        self._icons[key] = photo
        return photo

    def button_icon(self, char: str, style: str):
        r"""給 `HandButton` 的:字樣開頭那顆圖示的兩張圖——能按時彩色、停用時灰階。

        回 ttk 的狀態表 `(彩色, "disabled", 灰階)`,直接餵 `image=`;`None` = 畫不出來。
        字級跟著那顆鈕的字型走(`skin.BUTTONS`),圖示才與字同一個量級。"""
        spec = skin.BUTTONS.get(style)
        pt = spec.size if spec is not None else 10
        gap = self.px(SP_XS)
        on = self._icon_image(char, pt, gap=gap)
        off = self._icon_image(char, pt, grey=True, gap=gap)
        if on is None or off is None:
            return None
        return (on, "disabled", off)

    def _nav_icon(self, parent, char: str, pt: int, left: int, vert: int,
                  lift: int) -> ttk.Label:
        """分頁列那顆圖示——畫得出彩色就用圖片,畫不出來就退回文字。"""
        photo = self._icon_image(char, pt)
        if photo is not None:
            return ttk.Label(parent, image=photo, style="Nav.TLabel",
                             padding=(self.px(left), self.px(vert),
                                      self.px(SP_XS), self.px(vert)))
        return ttk.Label(parent, text=char, style="Nav.TLabel",
                         padding=self._icon_pad(left, vert, lift))

    def _paint_icon(self, lab: ttk.Label, style: str) -> None:
        """切換一顆圖示坐的底色(選中/沒選中)。⚠️ 圖不換:兩種狀態都是彩色那一張。"""
        lab.configure(style=style)

    def _icon_pad(self, left: int, vert: int, lift: int) -> tuple[int, int, int, int]:
        r"""分頁列那顆圖示的內距——**上下不對稱,把它往上推 `lift` 個邏輯像素**。

        為什麼要推、推多少是量出來的,見 `NAV_LIFT` 上面那段。
        ⚠️ **抬的量跟著 `px()` 走**:那個差是字型度量的比例,不是固定的實體像素。"""
        return (self.px(left), self.px(vert) - self.px(lift),
                self.px(SP_XS), self.px(vert) + self.px(lift))

    def _apply_window_icon(self) -> None:
        """標題列 / Alt+Tab 的圖示。

        ⚠️ **工作列那顆不歸這裡管**,它走的是另一條路而且有兩段:
        `winui.set_app_user_model_id()`(必須在建視窗**之前**呼叫)只把視窗歸到一個
        身分,那個身分的圖示登記在 `scripts/make_shortcut.py` 建的 .lnk 上——缺任一
        段,工作列上出現的就是啟動鏈上游那支執行檔的圖示。"""
        try:
            self.iconbitmap(default=str(ICON_PATH))
        except Exception:               # 純外觀,失敗就用預設的
            logger.debug("視窗圖示載入失敗", exc_info=True)

    def _styles(self) -> None:
        configure_styles(self, self.fam, self.pal)
        # ⚠️ **要跟著顯示縮放走的那幾個留在這裡**:`configure_styles` 是模組層函式
        # (為了測得到「樣式最後生效的值」而抽出去的),拿不到 `self.px()`。
        # 講者人數那個輸入框:預設 38 實體 px,而網頁版量到的是 **55**——它與上面兩排
        # segmented 是同一組「填一件事」的控制項,矮一截看起來像另一個層級的東西
        # (使用者 2026-09-01 指出)。⚠️ **垂直內距 2026-09-02 從 `SP_SM` 降到 `SP_XS`**:
        # 換上圓角底板(`Sq.field`)之後,底板自帶的內距從 sv_ttk 的 5 變成 `px(5)`,
        # 同樣的 `SP_SM` 量到 **65**;`SP_XS` 量到 53,差網頁版 2px(先前那版差 4px)。
        # 兩個都是尺規上的值、不是為了湊數字新編的——改了任一邊要開真視窗重量一次。
        ttk.Style(self).configure(
            "Tall.TSpinbox", padding=(self.px(SP_SM), self.px(SP_XS)))
        # 命名區的下拉與講者人數同一個高度(網頁版兩者是同一種輸入格、同高 55 實體 px);
        # 圓角底板由 `skin.SKIN_SWAPS` 的 `TCombobox` 那一列給,這裡只補內距。
        ttk.Style(self).configure(
            "Tall.TCombobox", padding=(self.px(SP_SM), self.px(SP_XS)))
        # 使用說明的搜尋框:同一種輸入格、同一個高度(圓角底板由 `skin.SKIN_SWAPS` 的
        # `TEntry` 那一列給)。
        ttk.Style(self).configure(
            "Tall.TEntry", padding=(self.px(SP_SM), self.px(SP_XS)))
        # 坐在視窗底上的線框鈕(文件頁的選檔鈕):字色與 hover 翻白照共用包給 `CTA_STYLE` 的
        # 那一份(`winkit.skin.apply` 只設 `spec.CTA_STYLE`,樣式名不同就繼承不到)。
        st = ttk.Style(self)
        st.configure(skin.CTA_PAGE_STYLE, foreground=self.pal["cta_fg"])
        st.map(skin.CTA_PAGE_STYLE,
               foreground=[("disabled", self.pal["run_off_fg"]),
                           ("pressed", self.pal["on_accent"]),
                           ("active", self.pal["on_accent"])])
        # 核對視窗的表格(2026-09-02 選案 C):字型同輸入格,列高照網頁版核對表的列
        # (`AUDIT_ROW_H`);`rowheight` 是像素,要跟著縮放走,所以留在這裡。
        # ⚠️ 底色要明寫成卡片的白:共用包給 Treeview 的是紀錄框的淺灰,斑馬紋的白那一半
        # 會跟灰那一半糊成一片(見 `AUDIT_ZEBRA_*` 的註解)。
        ttk.Style(self).configure("Audit.Treeview", font=(self.fam, 10),
                                  rowheight=self.px(AUDIT_ROW_H),
                                  background=self.pal["card"],
                                  fieldbackground=self.pal["card"])
        ttk.Style(self).configure("Audit.Treeview.Heading", font=(self.fam, 10))
        # 自畫的下拉清單(案 ②):字同輸入格、列高同網頁版、hover 是平的灰底、沒有外框
        # (框與圓角由 toplevel 那一層畫,見 `_pick_open`)。
        hover = PICK_HOVER_DARK if self._dark() else PICK_HOVER_LIGHT
        st = ttk.Style(self)
        st.configure("Pick.Treeview", font=(self.fam, 10),
                     rowheight=self.px(PICK_ROW_H),
                     background=self.pal["card"], fieldbackground=self.pal["card"],
                     foreground=self.pal["ink"], borderwidth=0, relief="flat")
        st.map("Pick.Treeview", background=[("selected", hover)],
               foreground=[("selected", self.pal["ink"])])
        st.layout("Pick.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

    # ---- 版面 ----------------------------------------------------------- #
    def _tab_cell(self, parent, icon: str, text: str, pt: int, lift: int,
                  off: str, command) -> tuple:
        r"""一顆分頁鈕:**灰膠囊底 ＋ 底下一條藍線**——兩排分頁共用同一個做法。

        (2026-09-01 使用者第三次比對網頁版:「第一層少了膠囊,第二層少了藍線」。)
        ⚠️ **兩排真的長得一樣**,那是量出來的、不是猜的:網頁版兩排選中的那一格都是
        `#e8e8ed` 的膠囊 ＋ 黑字(`#1d1d1f`)＋ 下方一條藍線,連膠囊高度都同樣是 41
        實體 px。⚠️ **選中的字是黑的不是藍的**——先前頂層寫成 accent 藍,那是照著
        「藍條」的印象推的,量了才發現不是。
        ⚠️ **膠囊要貼著那條灰線**:網頁版膠囊底 y=382、藍條 384~386、灰線 385~386
        ——中間只有 1px。所以藍條**緊接在膠囊下面**(`pady` 給 0),而整排的下緣就是
        那條灰線。

        回 `(lab, ico, pill, body, bar, seg)`,狀態切換交給 `_paint_tab`。"""
        cell = ttk.Frame(parent, style="Page.TFrame")
        # ⚠️ **格子之間不留 padx**(2026-09-01):每一格底下那條橫線**兼任灰線的那一
        # 段**(見 `bar`),留了間隙灰線就在那裡斷掉。⚠️ 視覺上的間距不必靠 padx——
        # 膠囊自己左右各留了一個半徑(見下面 `pill.configure`),相鄰的兩顆之間本來
        # 就有 45 實體 px 的空白。
        cell.pack(side="left")
        # 膠囊那一層。⚠️ **圖示與文字要包一層、用 `place` 置中**:`ttk.Label` 是
        # **實色底**的,直接 pack 進去就佔滿整個寬度——它的方角背景會把膠囊**圓角
        # 外側**那四塊也塗成膠囊色,圓角當場被填平(2026-09-01 實撞,畫面上是一個
        # 四角斜切的八角形)。⚠️ **這與皮膚無關**:同一張圖貼在空的 Frame 上,
        # 60~500px 六種寬度的圓角全部正確(最小重現驗過)。
        pill = ttk.Frame(cell, style="Page.TFrame")
        pill.pack()
        body = ttk.Frame(pill, style="Page.TFrame")
        body.place(relx=0.5, rely=0.5, anchor="center")
        ico = self._nav_icon(body, icon, pt, 0, 0, lift)
        ico.configure(style=off)
        ico.pack(side="left")
        lab = ttk.Label(body, text=text, style=off,
                        padding=(self.px(SP_XS), 0, 0, 0))
        lab.pack(side="left")
        # 選中的那一條藍線,**兩層**:外層 `bar` 是藍條的高度,內層 `seg` 貼底一段灰線。
        # ⚠️ **沒選中時 `seg` 是灰線色,不是頁面色**——那條貫穿整排的灰線被每一格的
        # 實色背景蓋住了(`ttk.Frame` 不透明),所以**由每一格自己接出那一段**;兩側
        # 沒有格子的地方才由 `_tab_rule` 那條補(2026-09-01 使用者:「灰線被擋住了」)。
        # ⚠️ **為什麼要兩層**:灰線比藍條薄一列(`_tab_rule_h` 對 `TAB_BAR_H`),做成
        # 同一條的話灰線就跟藍條一樣厚,比網頁版粗一截(2026-09-02 使用者:「灰色的兩
        # 條線比較粗」)。選中時兩層一起翻藍,藍條就自然蓋住灰線、而且底邊對齊。
        # ⚠️ 用 `tk.Frame` 而不是 ttk:它要的只是一塊純色,而 ttk 的 Frame 顏色歸樣式
        # 管,為了兩種狀態各開一個樣式並不划算。
        # ⚠️ **`pack_propagate(False)` 少不得**:不關的話外層會縮成內層的高度,藍條
        # 就只剩灰線那麼薄(探針量到 3 → 2)——而 `cget("height")` 照樣回原值,而且
        # 要等視窗 map 之後才發生(`_paint_tab` 的 `configure(bg=)` 會把設定的高度
        # 重新要一次,下一次 ConfigureNotify 才又傳播),測試那個藏起來的視窗量不到。
        bar = tk.Frame(cell, bg=self.pal["page"], height=self.px(TAB_BAR_H))
        bar.pack(fill="x")
        bar.pack_propagate(False)
        seg = tk.Frame(bar, bg=self.pal["line_off"], height=self._tab_rule_h())
        seg.pack(side="bottom", fill="x")
        for w in (cell, pill, body, ico, lab):
            w.bind("<Button-1>", lambda _e: command())
            w.configure(cursor="hand2")
        # ⚠️ **高度釘死成膠囊那張圖的高度**:膠囊垂直方向不切九宮格,圖高與元件高度
        # 差一列就被裁或重複貼(見 `_plate_h`)。
        # ⚠️ **寬度要留出左右各一個半徑**,否則內容仍然壓在圓角上;而 `place` 的子
        # 元件不參與尺寸傳播,所以要問 `body` 的請求寬度、不是 `pill` 的。
        h = self._plate_h("Sq.subtab", TAB_PILL_H)
        body.update_idletasks()
        # ⚠️ **多留 `SP_XS`**:`+ h` 已經是「左右各一個半徑」,但那是**邊界**(方角
        # 剛好落在弧上),而 `winfo_reqwidth()` 在字型度量上還有一兩像素的誤差——
        # 邊界值一漂就又壓到圓角上了。
        pill.configure(width=body.winfo_reqwidth() + h + self.px(SP_XS), height=h)
        pill.pack_propagate(False)
        return lab, ico, pill, body, bar, seg

    def _tab_rule(self, row) -> tk.Frame:
        r"""一排分頁下面那條細灰線——⚠️ **畫在藍條的同一列,而且被藍條蓋住**。

        (2026-09-01 使用者:「藍色的線,應該壓在灰線上」。)網頁版量到的正是這樣:
        頂層的灰線在 y 385~386,而選中那一格的藍條是 384~386——**同一個位置**,穿過
        選中格的那一條直線上根本看不到灰線。先前把灰線 `pack` 在整排下面,結果是
        「3px 藍 ＋ 2px 灰」疊成 5px,底下多一條說不出理由的灰。
        ⚠️ **做法是 `place` 貼在那一排的底邊再 `lower()`**:`place` 的元件不佔父容器
        的尺寸(所以整排不會因此變高),而藍條是每一格的最後一列、正好在同一個水平
        ——降到底層之後,選中那一格的藍條就蓋住它,其餘那幾格露出灰線。
        ⚠️ 用 `tk.Frame` 不是 ttk:它要的只是一塊純色(同藍條的理由)。"""
        # ⚠️ **要與每一格那段灰線(`seg`)同高,不是與藍條**:整條灰線是「兩側這一段
        # ＋ 每一格自己那一段」拼起來的,高度不一致就會在 navin 的左右兩端各出現一個
        # 階梯;而它比藍條薄一列是量網頁版得到的(見 `TAB_BAR_H` 上面那段)。
        rule = tk.Frame(row, bg=self.pal["line_off"], height=self._tab_rule_h())
        rule.place(relx=0, rely=1.0, anchor="sw", relwidth=1.0)
        rule.lower()
        return rule

    def _tab_rule_h(self) -> int:
        """分頁底下那條灰線的厚度:**藍條減一列**,而不是 `px(1)`(理由見 `TAB_BAR_H`)。"""
        return max(1, self.px(TAB_BAR_H) - 1)

    def _btn_col_w(self) -> int:
        r"""命名卡右側鈕欄的欄寬:**鈕本身 `NAME_BTN_COL` ＋ 與輸入格之間那道縫 `SP_MD`**。

        「試聽」「核對」「重新分群」三顆都撐滿 `NAME_BTN_COL`(使用者 2026-09-03:「試聽與
        核對按鈕的長度,請與重新分群按鈕一樣長」),縫是鈕左邊的 `padx`,兩者都算進欄寬;
        線索小字的換行寬度也扣同一個數,句尾才不會壓進鈕的那一欄。⚠️ 兩段各自 `px()`
        再相加,與 `padx` 用的 `px(SP_MD)` 是同一個 round,不會差一像素。"""
        return self.px(NAME_BTN_COL) + self.px(SP_MD)

    def _paint_tab(self, entry: tuple, icon: str, pt: int, on: bool,
                   on_style: str, off_style: str) -> None:
        """把一顆分頁鈕切成選中/沒選中(兩排共用,見 `_tab_cell`)。"""
        lab, ico, pill, body, bar, seg = entry
        style = on_style if on else off_style
        lab.configure(style=style)
        self._paint_icon(ico, style)
        # ⚠️ **膠囊與它裡面那一層要分開切**:內層同樣是實色底,留在頁面底色上就是
        # 一塊淺色方塊壓在膠囊中間(理由同 `_segmented` 的 `SegOnBody`)。
        pill.configure(style="SubOnCell.TFrame" if on else "Page.TFrame")
        body.configure(style="SubOnBody.TFrame" if on else "Page.TFrame")
        # ⚠️ **兩層一起翻**:只翻外層,選中那一格的藍條底下會露出一條灰;只翻內層,
        # 藍條就只剩灰線那麼薄。
        bar.configure(bg=self.pal["accent"] if on else self.pal["page"])
        seg.configure(bg=self.pal["accent"] if on else self.pal["line_off"])

    def _brand_image(self, pt: int):
        """頁首那顆程式圖示(讀出貨的 PNG,縮到要的大小)。畫不出來就回 `None`。

        ⚠️ **不要拿 `icon.ico` 直接餵 `tk.PhotoImage`**:Tk 8.6 讀得了 PNG 但讀不了
        ICO,而失敗是靜默的(圖示位置就是一片空白)。所以走 `assets/png` 那組。
        ⚠️ **縮放交給 Pillow**:`PhotoImage.subsample` 只能整數倍,150% 縮放要的
        尺寸它給不出來——硬用就是鋸齒。"""
        key = ("brand", pt)
        if key in self._icons:
            return self._icons[key]
        try:
            from PIL import Image, ImageTk

            size = self.px(pt) * 4 // 3      # pt → px(同 `_icon_image`)
            im = Image.open(assets_dir() / "png" / "icon-256.png").convert("RGBA")
            im = im.resize((size, size), Image.LANCZOS)
            photo = ImageTk.PhotoImage(im)
        except Exception:
            logger.debug("頁首圖示畫不出來", exc_info=True)
            photo = None
        self._icons[key] = photo
        return photo

    def _brand_head(self, parent) -> ttk.Frame:
        r"""頁首:程式圖示＋名稱,底下一句「這是什麼、會不會外傳」。

        ⚠️ **2026-09-01 使用者裁定加回來的**(照網頁版重排那一批),而這**推翻了**
        2026-08-29 的「Windows 標題列已經提供程式身分,所以不必再有頁首」。理由是那
        句副標:**「全部在本機轉換,檔案不外傳」在原生版沒有別的地方講**,而那是他
        敢把會議錄音丟進來的唯一理由(spec §7 對使用者的承諾)。程式名重複一次是這
        個決定的代價,不是疏忽。
        ⚠️ **不放網頁版右上角那顆齒輪**:那是 gradio 自己的深淺色切換,原生視窗的
        佈景跟著 Windows 走、沒有對應的功能——放一顆按了沒反應的鈕,跟壞掉長得一模
        一樣(同 `RUN_MODES` 那條)。"""
        head = ttk.Frame(parent, style="Page.TFrame")
        head.pack(fill="x", pady=(self.px(SP_MD), 0))
        # ⚠️ 置中同 `nav`:外層 fill=x、內層什麼都不 fill(見 `_build_ui`)。
        line = ttk.Frame(head, style="Page.TFrame")
        line.pack()
        photo = self._brand_image(BRAND_ICON_PT)
        if photo is not None:
            ico = ttk.Label(line, image=photo, style="Brand.TLabel")
            ico.pack(side="left", padx=(0, self.px(SP_SM)))
        ttk.Label(line, text=APP_TITLE, style="Brand.TLabel").pack(side="left")
        # ⚠️ 上 pady 是量網頁版得到的 `HEAD_GAP_SUB`,不是尺規上的 SP_XS(見那個常數)。
        self._wrap(ttk.Label(head, text=APP_SUBTITLE, style="BrandSub.TLabel",
                             justify="center")).pack(pady=(self.px(HEAD_GAP_SUB), 0))
        return head

    def _plate_h(self, elem: str, fallback: int) -> int:
        r"""某張底板**實際載入的那張圖**有多高(實體 px)。

        ⚠️ **膠囊的高度一定要問這一支,不可以自己再 `px()` 算一次**(2026-09-01 使用者
        圈出來的那顆):`App.px()` 用的是**真實**倍率,而皮膚挑的是最接近的**檔位**
        ——這台量到 `scale = 1.4985`,於是 `px(37) = 55` 而資產走 1.5 檔畫的是 **56**。
        差一個像素,Tk 就把圖裁掉一列,圓角當場變形(而它不報錯、`reqheight` 也看不
        出來,只有截圖看得到)。
        ⚠️ **既有那幾顆剛好沒中**:30 與 40 在兩種倍率下都算出同一個值,37 不是——
        所以這不是「新皮特有的問題」,是任何新高度都可能踩到的。
        ⚠️ 沒有皮膚時(裝不起來)退回自己算,那時本來就是實色的方角矩形。"""
        if self.skin is not None:
            got = self.skin.plate.get(elem)
            if got:
                return got[0]
        return self.px(fallback)

    def _segmented(self, parent, options, command) -> tuple[ttk.Frame, dict]:
        r"""一排幾選一的膠囊 segmented(照網頁版的 `.seg-radio`)。

        `options` 是 `((鍵, 圖示, 文字), …)`,回傳 `(整條槽, {鍵: (格子, 內層,
        圖示, 文字)})`——狀態切換交給呼叫端的 `_segment_show`。

        (2026-09-01 使用者選案:網頁版那三排是灰底膠囊槽 + 白色的選中段,而原生版
        原本是方角小標籤與下拉選單。他的原話是「沒有膠囊圓角效果」「選單也不直覺」。)
        ⚠️ **2026-09-06 又改過一次**(使用者選案 H,四案對照見 `docs/dev/native-ui.md`):
        灰槽拿掉,未選中的那幾格直接坐在卡片白上,選中的那格是白膠囊 ＋ 一圈 `accent`
        描邊。**成因是對比不是好看**——`muted` 坐在 `btn` 上兩個模式都過不了 9:1
        (見 `STYLES` 那段),而那條路壓深色票已經算過是死的。

        ⚠️ **每一格等分、右緣齊頭**靠的是 `columnconfigure(uniform=)`:少了 uniform
        就只是「平分剩餘空間」,而「只錄電腦聲音」比「線上會議」多兩個字,那兩個字
        就是落差(網頁版當初也是為同一件事改過一次)。
        ⚠️ **兩層高度都要自己釘死**:那一列用 `grid_propagate(False)` 關掉傳播,格子
        則因為選中態是一張膠囊底板、圖高必須精確等於元件高度(見 `SEG_H_ON`)。格子用
        `height=` 加**不垂直拉伸的 sticky**——`sticky="ew"` 只拉寬度,高度就留在它
        自己的 31,再靠 `rowconfigure(weight=1)` 垂直置中。
        ⚠️ **格子裡面那一層用 `place` 置中**:圖示與文字是兩個 Label(基線不同,見
        `_icon_pad`),要當成一個整體擺在格子正中間;`place` 的子元件不參與尺寸傳播,
        所以格子的高度不會被內容撐開——那正是這裡最怕的一件事。"""
        track = ttk.Frame(parent, style="Seg.TFrame",
                          padding=(self.px(SEG_PAD), 0))
        # ⚠️ 這一列**沒有底板**(2026-09-06 拿掉灰槽),所以高度直接 `px()`:沒有圖要
        # 對齊,`_plate_h` 那條「要問皮膚不要自己算」只適用於穿著膠囊的那一層。
        track.configure(height=self.px(SEG_H))
        track.grid_propagate(False)
        track.rowconfigure(0, weight=1)
        cells: dict[str, tuple] = {}
        for i, (key, icon, text) in enumerate(options):
            track.columnconfigure(i, weight=1, uniform="seg")
            cell = ttk.Frame(track, style="SegCell.TFrame")
            cell.configure(height=self._plate_h("Sq.seg_on", SEG_H_ON))
            cell.grid(row=0, column=i, sticky="ew")
            body = ttk.Frame(cell, style="SegCell.TFrame")
            body.place(relx=0.5, rely=0.5, anchor="center")
            # ⚠️ **圖示可以沒有**(模型那排就沒有):空字串時整個 Label 不建,而不是
            # 建一個空的——空 Label 照樣佔一份 padding,那一排就會比別排偏右。
            ico = None
            if icon:
                ico = self._nav_icon(body, icon, 10, 0, 0, SUBNAV_LIFT)
                ico.configure(style="Seg.TLabel")
                ico.pack(side="left")
            lab = ttk.Label(body, text=text, style="Seg.TLabel",
                            padding=(self.px(SP_XS) if icon else 0, 0, 0, 0))
            lab.pack(side="left")
            # ⚠️ 停用時**連點都不要進到 command**:那幾支開頭雖然有 `_busy` 的把關,
            # 但那是「正在跑」的把關,而停用可能有別的理由(見 `_segment_enable`)。
            for w in (cell, body, lab) + ((ico,) if ico is not None else ()):
                w.bind("<Button-1>",
                       lambda _e, k=key, t=lab: None if t.instate(["disabled"]) else command(k))
                w.configure(cursor="hand2")
            cells[key] = (cell, body, ico, lab)
        return track, cells

    def _segment_enable(self, cells: dict, on: bool) -> None:
        r"""整條 segmented 停用/放開(2026-09-03 使用者:錄音中進階參數要 DISABLE)。

        ⚠️ **這是「看得出來」那一半**:`_model_show` / `_scene_show` 開頭本來就擋著
        `_busy`,所以錄音中它一直按不動——但畫面上完全看不出來,游標還是手形、字色照舊,
        使用者按了沒反應會當成壞掉。字色由 `configure_styles` 的 `map` 換成停用色。
        ⚠️ **狀態設在 Label 上**(`Frame` 沒有前景色),而 `_segment_show` 只換 style、
        不動 state,所以停用期間切換選中格也不會把它放開。"""
        for cell, body, ico, lab in cells.values():
            for w in (lab,) + ((ico,) if ico is not None else ()):
                w.state(["!disabled"] if on else ["disabled"])
            # ⚠️ **游標四個都要換**:它設在 cell/body/lab/ico 上(見 `_segmented`),
            # 漏掉哪一個,滑過那一塊就還是手形——那正好是「看起來按得動」的來源。
            for w in (cell, body, lab) + ((ico,) if ico is not None else ()):
                w.configure(cursor="hand2" if on else "arrow")

    def _segment_show(self, cells: dict, key: str) -> None:
        """把 segmented 切到某一格(選中的那格翻成白膠囊;圖示兩種狀態都是彩色)。"""
        for k, (cell, body, ico, lab) in cells.items():
            on = k == key
            cell.configure(style="SegOn.TFrame" if on else "SegCell.TFrame")
            body.configure(style="SegOnBody.TFrame" if on else "SegCell.TFrame")
            style = "SegOn.TLabel" if on else "Seg.TLabel"
            lab.configure(style=style)
            if ico is not None:
                self._paint_icon(ico, style)

    @staticmethod
    def _say(label, prog, before, text: str) -> None:
        r"""某一頁那一行階段文字。⚠️ **空字串 = 整組進度收起來**。

        (2026-09-01 決策 D1:閒置時的畫面要跟網頁版一樣乾淨,而網頁版根本沒有一條
        常駐的空進度槽——使用者截圖裡預覽框上方那條灰線就是它。)
        ⚠️ **收起來的只有「還沒開始跑」那一段**:原生版沒有黑視窗,第一次轉檔那
        2-3 GB 的模型下載進度與長階段的心跳訊息只剩這裡出得來(`native-ui.md` §4)。
        所以判準是**有沒有話要說**,不是「哪個模式」——訊息一來它就自己出現。
        ⚠️ **兩頁共用這一支**(`_stage` 是轉檔頁與錄音、`_doc_say` 是文件頁):先前是
        逐行抄過去的兩份,而上面這兩條硬規則只寫在其中一份上,下一次動它另一邊不會跟
        (2026-09-03 code review 抓到)。各頁不同的只有那三個 widget。"""
        label.configure(text=text)
        if text:
            # ⚠️ `before=` 不能省:pack 的順序是呼叫的先後,不指定的話它會掉到預覽
            # 框**下面**——而那正是使用者最不會去看的位置。
            prog.pack(fill="x", before=before)
        else:
            prog.pack_forget()

    def _stage(self, text: str) -> None:
        """轉檔頁(與錄音)那一行階段文字。規矩全在 `_say`。"""
        self._say(self._run_stage, self._run_prog, self._run_pwrap, text)

    def _content_width(self, window: int) -> int:
        """視窗這麼寬(實體 px)的時候,內容區該多寬。

        ⚠️ **抽成純算的一支是為了測得到**:測試裡那個視窗沒有 map,`winfo_width()`
        量到的一律是 1(同檔其他幾條的理由)。"""
        return min(window - self.px(PAGE_PAD) * 2, self.px(MAX_CONTENT))

    def _refit_shell(self) -> int:
        """把內容殼的寬度設成「視窗放得下的」與 `MAX_CONTENT` 的**較小者**,回傳它。

        ⚠️ **回傳值就是後面每一個換行寬度與欄寬的來源**,不要再去問 `winfo_width()`
        ——剛 `configure()` 完的那一刻它還是舊值,而拿舊值算出來的欄寬會慢一幀,
        使用者看到的就是拉視窗時內容「跟不上」。"""
        width = self._content_width(self.winfo_width())
        if width > self.px(200):
            self._shell.configure(width=width)
        return width

    def _build_ui(self) -> None:
        # ⚠️ **殼的每一個 Frame 都要指名 `Page.TFrame`**(共用包給的):不指名的話吃
        # 到的是 sv_ttk 的 `TFrame` 底色(實測 #fafafa),而視窗底是 #f5f5f7——兩者
        # 只差 5 階,螢幕上是一塊說不出理由的淡色方塊(左右那 20px 外距正好把它框出
        # 來)。卡片是純白,同樣只差 5 階、會整個融進背景。
        # 整個內容欄(頁首、兩排分頁、內容區)住在一個**寬度有上限、而且置中**的殼
        # 裡。⚠️ **2026-09-01 加的,理由見 `MAX_CONTENT`**:先前這幾層直接 pack 在
        # 視窗上,最大化時就一路撐到螢幕邊。
        # ⚠️ **置中不能靠「內層不 fill」那一招**(頂層分頁列用的那個):那招要求內層
        # 的寬度由**內容**決定,而這裡要的正好相反——寬度由視窗決定(取 min),內容
        # 反過來填滿它。所以走 `grid` + `sticky="ns"`:垂直方向拉滿整格、水平方向不
        # 拉,grid 就把它擺在正中間,而寬度由 `_refit_shell` 每次 Configure 時設。
        outer = ttk.Frame(self, style="Page.TFrame")
        outer.pack(fill="both", expand=True, padx=self.px(PAGE_PAD))
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)
        shell = ttk.Frame(outer, style="Page.TFrame")
        shell.grid(row=0, column=0, sticky="ns")
        # ⚠️ **是 `pack_propagate` 不是 `grid_propagate`**(同 `_run_page` 那兩欄踩
        # 過的):傳播開關跟著**子元件那一側**的 geometry manager,而殼裡面的東西是
        # 用 `pack` 排的。關錯的症狀不是「沒效果」而是「看起來有效但其實沒有」。
        shell.pack_propagate(False)
        shell.configure(width=self.px(MAX_CONTENT), height=self.px(WIN_H))
        self._shell = shell

        self._head = self._brand_head(shell)

        nav = ttk.Frame(shell, style="Page.TFrame")
        # ⚠️ 上 pady 是量網頁版得到的 `HEAD_GAP_NAV`(副標→分頁列的字空 43 實體 px)。
        nav.pack(fill="x", pady=(self.px(HEAD_GAP_NAV), 0))
        # ⚠️ 置中靠的是「外層 fill=x、內層什麼都不 fill」:`pack` 會把撐不滿的子元件
        # 放在父容器正中間,不必自己算偏移(算的話字型一換就歪掉)。
        navin = ttk.Frame(nav, style="Page.TFrame")
        navin.pack()
        for key, icon, text in TABS:
            self._nav[key] = self._tab_cell(
                navin, icon, text, 11, NAV_LIFT, "Nav.TLabel",
                lambda k=key: self._show(k))

        self._rule = self._tab_rule(nav)

        # 「聲音→MD」的子分頁列。⚠️ **整條列跟著頂層分頁一起收起來**(只有第一頁有
        # 子分頁),而不是留一條空列:留著的話另外三頁上方會多一道說不出理由的空白。
        # ⚠️ **它自己那條線要包在同一個 Frame 裡**:兩者要一起出現、一起收起來,而
        # `_show` 每次都會重新 pack 這一整塊(pack 的順序是呼叫的先後)。
        self._subbar = ttk.Frame(shell, style="Page.TFrame")
        subin = ttk.Frame(self._subbar, style="Page.TFrame")
        subin.pack(fill="x")
        for key, icon, text in SUBTABS:
            self._subnav[key] = self._tab_cell(
                subin, icon, text, 10, SUBNAV_LIFT, "Sub.TLabel",
                lambda k=key: self._show_sub(k))
        self._subrule = self._tab_rule(subin)

        # ⚠️ **內容區不再自己留左右外距**:那一圈現在由外層的 `outer` 給,而殼的寬
        # 度就是內容的寬度(`MAX_CONTENT`)——兩邊都留的話,內容會比網頁版窄 40px。
        self._body = ttk.Frame(shell, style="Page.TFrame")
        self._pack_body(SP_MD)

    def _page(self, key: str) -> ttk.Frame:
        r"""要哪一頁就建哪一頁,**第一次切過去才建**。

        ⚠️ **不要在啟動時全部建好**(2026-08-29 量到才改的):六頁一起建要 2,601ms,
        而第一幀從 861ms 掉到 3,184ms——那正是整個原生介面遷移要換掉的東西。逐頁量到
        的成本是 `_run_page` 915、`_help_page` 697、兩頁詞表 282、`_doc_page` 153、
        `_roster_page` 67,而使用者開起來只看得到其中一頁。
        ⚠️ **建好要留著**:每次切頁都重建的話,使用者填到一半的東西會消失。"""
        if key not in self._pages:
            if key in WORDLIST_PAGES:
                self._pages[key] = self._wordlist_page(key)
            elif key == "help":
                self._pages[key] = self._help_page()
            elif key == "doc":
                self._pages[key] = self._doc_page()
            elif key == "run":
                self._pages[key] = self._run_page()
            else:
                self._pages[key] = self._roster_page()
        return self._pages[key]

    def _wordlist_page(self, key: str) -> ttk.Frame:
        r"""一份詞表檔的編輯頁(兩頁共用,規格見 `WORDLIST_PAGES`)。

        ⚠️ **純文字原樣編輯,不做成表格**:兩份檔的註解(收詞原則、預算說明)與
        行序都有意義,而表格 round-trip 會把兩者一起弄丟。
        ⚠️ **框由外層的 `Sunken.TFrame` 畫,不由 `tk.Text` 自己畫**:`tk.Text` 是
        classic 控制項、做不出圓角,而捲軸是另一個 widget——框畫在 Text 身上,捲軸
        就只能貼在框外面(共用包 `SKIN_FRAMES` 那段記著這條的來歷)。"""
        spec = WORDLIST_PAGES[key]
        page = ttk.Frame(self._body, style="Page.TFrame")
        card = ttk.Frame(page, style="Card.TFrame", padding=self.px(CARD_PAD))
        card.pack(fill="both", expand=True)
        # 說明照網頁版(2026-09-03 使用者:「行距請比照 WEB,斷行也請依照 WEB,寫兩行即可」
        # →「請依照 WEB 為準,不要自己發明」):同一段自然折行、粗體與程式碼字照記號畫、
        # 深色 10pt、行距同文件頁那段(`INTRO_LINE_GAP`;網頁版三段說明是同一個樣式)。
        # 先前是 9pt 灰的 Hint、三句各自硬斷行、第三句拿 ⚠ 代替粗體——全是 Label 的權宜。
        self._icon_text(card, spec["note"], bg="card", fg="ink", pt=10,
                        line_gap=INTRO_LINE_GAP).pack(
            anchor="w", fill="x", pady=(0, self.px(SP_LG)))

        wrap = ttk.Frame(card, style="Sunken.TFrame", padding=self.px(SP_XS))
        wrap.pack(fill="both", expand=True)
        box = tk.Text(wrap, font=(self.fam, 10), relief="flat", bd=0, undo=True,
                      highlightthickness=0, wrap="none", height=spec["lines"],
                      bg=self.pal["field"], fg=self.pal["ink"],
                      insertbackground=self.pal["ink"],
                      selectbackground=self.pal["row_sel"],
                      padx=self.px(SP_SM), pady=self.px(SP_XS))
        bar = ttk.Scrollbar(wrap, orient="vertical", command=box.yview)
        box.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self._lockable(box).pack(side="left", fill="both", expand=True)

        # 那一對鈕(2026-09-04 使用者:「重新載入按鈕樣式,修改為儲存詞表的樣式;另外
        # 儲存詞表的樣式,請改用實心橢圓的樣式」):儲存 = **實心藍膠囊**、重新載入 =
        # 線框鈕(儲存原本那張皮)。⚠️ **這正是網頁版的那一對**:`app.py` 那兩顆是
        # `gr.Button("儲存詞表", variant="primary")` ＋ 不帶 variant 的次要鈕,先前
        # 原生版把主要那顆做成線框、次要那顆做成實心灰,主次剛好反過來。
        # ⚠️ **實心那張皮用現成的 `RUN_SMALL_STYLE`**(「聲紋健檢」那顆):它的字級、
        # 內距與 `CTA_STYLE` 一模一樣、高度同為 `SQ_H`——同一列的一對只差底板,不必
        # 為此再產一張新的膠囊(每加一張皮都要跳 `SKIN_SCHEMA` 並重跑 `make_skin`)。
        # ⚠️ **兩頁詞表共用這一支**,所以「用詞替換表」跟著一起換——網頁版那兩頁本來
        # 就是同一對樣式。
        row = ttk.Frame(card, style="CardBody.TFrame")
        row.pack(fill="x", pady=(self.px(SP_MD), 0))
        self._lockable(HandButton(row, text=spec["save"], style=skin.RUN_SMALL_STYLE,
                                  command=lambda: self._wordlist_save(key))).pack(
                                      side="left")
        HandButton(row, text="重新載入", style=skin.CTA_STYLE,
                   command=lambda: self._wordlist_load(key)).pack(
                       side="left", padx=(self.px(SP_SM), 0))

        # 狀態那兩行:平常只有第一行,出問題才多一行警告色的。⚠️ **兩個 Label 而
        # 不是一個**——一個的話,警告只能靠字面(「已超出…」)與正常訊息區分,而
        # 那正是網頁版用粗體在解的問題。
        st = self._wrap(ttk.Label(card, style="Status.TLabel", justify="left"))
        st.pack(anchor="w", pady=(self.px(SP_SM), 0))
        warn = self._wrap(ttk.Label(card, style="Warn.TLabel", justify="left"))
        self._wordlists[key] = (box, st, warn)
        self._wordlist_load(key)
        return page

    def _wordlist_show(self, key: str, status: wordlists.Status) -> None:
        """把 `Status` 畫上去。⚠️ 沒有警告時**整個 Label 收起來**,不是設成空字串
        ——空的 Label 照樣佔一行高,而那一行的有無正是使用者判斷「這次有沒有問題」
        最快的線索。"""
        _, st, warn = self._wordlists[key]
        st.configure(text=status.summary)
        if status.warning:
            warn.configure(text=status.warning)
            warn.pack(anchor="w", pady=(self.px(SP_XS), 0))
        else:
            warn.pack_forget()

    def _wordlist_load(self, key: str) -> None:
        box, _, _ = self._wordlists[key]
        spec = WORDLIST_PAGES[key]
        was = str(box.cget("state") or "normal")   # 工作進行中是鎖著的,見 `_roster_load`
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("1.0", wordlists.read(spec["read"]()))
        box.edit_reset()          # 重新載入之後不該還能「復原」回上一份的內容
        box.configure(state=was)
        self._wordlist_show(key, spec["status"]())

    def _wordlist_save(self, key: str) -> None:
        box, _, _ = self._wordlists[key]
        spec = WORDLIST_PAGES[key]
        # ⚠️ `end-1c` 去掉 Tk 自己補的那個換行:不去掉的話,每存一次就多一個空行。
        self._wordlist_show(key, spec["write"](box.get("1.0", "end-1c")))

    # ---- 長工作:在工作執行緒跑,訊息回主執行緒 ------------------------- #
    def _taskbar(self, frac: float | None) -> None:
        r"""把工作列按鈕底下那條畫成進度。`None` = **還不知道要跑多久**(跑馬燈)。

        使用者 2026-09-05 指定,對齊姊妹專案 `MP4-2-SRT`(`winui.taskbar_progress`
        是同一支):「開始錄音時,如果最小化或是退到背後,工具列下的 icon 底部能出現
        在流動的效果……像是轉換檔案,可先知道還剩多少時間的,改為類似進度的方式顯示」。

        ⚠️ **這是視窗被縮小之後唯一還看得見的進度**:錄音與轉檔動輒一兩個小時,而
        winkit 版沒有黑視窗——切走的人在工作列上看不到任何動靜,只能猜它是不是死了
        (同「靜默的失敗」那一族)。所以兩種狀態都要給:**算得出剩多少就畫進度,算不
        出來(錄音、準備中、模型下載前)就流動**——「有在動」本身就是答案。
        ⚠️ **只能在主執行緒呼叫**:COM 物件綁在建立它的 STA 上(`winui._taskbar` 的
        檔頭)。進度是工作執行緒算的,但畫它的 `_job_say` 已經被幫浦搬到主執行緒了。
        ⚠️ **一樣的值不重送**:跑馬燈那條每秒被推一次的話,Windows 會把動畫從頭跑起
        ——看起來反而像卡住。"""
        # ⚠️ **0 也算「還不知道」**:`SetProgressValue(0)` 畫出來的空槽跟沒有進度長
        # 得一模一樣,而每一條管線的第一則進度都是 0——照著畫的話,使用者按下去看到的
        # 是工作列閃一下就沒了(而那正是「它是不是死了」那個問題)。
        steps = (None if frac is None or frac <= 0
                 else round(min(1.0, frac) * TASKBAR_STEPS))
        if steps == self._tb:
            return
        self._tb = steps
        if steps is None:
            winui.taskbar_progress(self, 0, 0)      # total <= 0 = 跑馬燈
        else:
            winui.taskbar_progress(self, steps, TASKBAR_STEPS)

    def _taskbar_end(self, flag: int = winui.TBPF_NOPROGRESS) -> None:
        r"""收工:工作列按鈕上留下這一趟的收場(`NOPROGRESS` = 清乾淨)。

        ⚠️ **成功、報錯、按停止、關視窗四條路都要走到**:漏掉哪一條,工作列就會在
        沒有任何工作在跑的時候一直流動——那比沒有進度更糟,它在說一件不是真的事。
        ⚠️ **顏色留著到下一趟按開始為止**(使用者 2026-09-05 選定):那條顏色正是給
        「還沒切回來的人」看的,一切回視窗就抹掉等於只有已經知道的人看得到。下一趟
        開始時 `_taskbar(None)` 自己會蓋過去,關掉視窗也就沒了。
        ⚠️ **哪一種收場配哪個旗標由呼叫端查表**(`WORK_OUTCOME` + `_work_outcome`):
        這一支只管送,不猜——`MP4-2-SRT` 記著的教訓是兩邊各有一個相反的預設值時,一個
        打錯的字串就會得到「紅色的結果列 ＋ 乾淨的工作列」。"""
        if self._tb == ("end", flag):
            return
        self._tb = ("end", flag)
        winui.taskbar_finish(self, flag)

    def _work_outcome(self, error, *, naming: bool = False) -> int:
        r"""這一趟的收場配哪個工作列旗標(定義在 `WORK_OUTCOME`)。

        ⚠️ **`naming` 只有轉逐字稿那兩條路傳 True**:批次(多檔/資料夾)刻意不做講者
        命名,文件轉檔更沒有命名這回事——不分的話,它們每一趟都會留下一個沒有人要回來
        處理的黃色。⚠️ 而且要問 `_naming_result`、不是「有沒有講者」:命名卡真的長出來
        了才算(沒有人可命名時 `_naming_show` 根本不會被呼叫)。"""
        if error is None:
            if naming and self._naming_result is not None:
                return WORK_OUTCOME["naming"]
            if self._job_outcome == "partial":
                return WORK_OUTCOME["partial"]
            return WORK_OUTCOME["done"]
        if isinstance(error, Cancelled):
            return WORK_OUTCOME["stopped"]
        if isinstance(error, UserFacingError):
            return WORK_OUTCOME["known"]
        return WORK_OUTCOME["broken"]

    def _busy_reason(self, mine: str) -> str | None:
        r"""另外兩條工作路徑有沒有在跑?有就回一句繁中的話,沒有回 `None`。

        ⚠️ **一律問旗標,不准跨頁去摸按鈕的 `state`**(2026-08-30 修掉的那顆):分頁
        是「用到才建」的(見 `_page`),使用者還沒點過「文字、圖像→MD」時
        `self._doc_stop` **根本不存在**——而 `self.` 取不到就落到 Tk 自己的
        `__getattr__`,丟出來的是
        `AttributeError: '_tkinter.tkapp' object has no attribute '_doc_stop'`。
        ⚠️ **它在畫面上一個字都沒有**:Tk 把 callback 的 traceback 吞進紀錄檔,使用者
        看到的只是「按了『開始錄音』沒反應」,而先去點一下別的分頁反而就好了——
        「先點過別頁才會動」這種症狀沒有人猜得到成因。`tests/test_desktop.py` 以 AST
        反向釘著「三個 start 都要走這一支」。
        ⚠️ **三條路徑各要問另外兩條、一條都不能少**:`cancel` 的旗標是全域單例,兩邊
        同時跑時任一顆停止鈕會把另一邊也殺掉(CLAUDE.md 的硬規則),所以這是正確性
        需求。先前錄音問兩條、轉逐字稿問一條,而**文件轉檔誰都沒問**。"""
        for key, text in WORK_BUSY_TEXT.items():
            if key != mine and self._busy[key]:
                return text
        return None

    def _run_job(self, work, done) -> None:
        r"""把 `work(say)` 丟到工作執行緒,`say(...)` 的東西回到主執行緒。

        ⚠️ **Tk 不是執行緒安全的**:工作執行緒直接碰 widget 有時能跑、有時整個當掉
        ——那種當機沒有規律、也重現不出來。所以中間隔一個 queue,由主執行緒的
        `after` 幫浦取出來再畫。
        ⚠️ **例外要從工作執行緒帶回來**:不接的話它死在那條執行緒裡,畫面停在「轉檔
        中」不動,而黑視窗(如果有的話)才看得到 traceback。"""
        import queue
        import threading

        self._job_outcome = ""      # ⚠️ 上一趟的收場不得殘留到這一趟
        box: "queue.Queue" = queue.Queue()
        # ⚠️ 讓「深在管線裡面」的東西也丟得回主執行緒(目前是模型下載的進度):
        # 它在工作執行緒上跑,而 `queue` 是執行緒安全的,`pump` 照樣收得到。
        self._job_box = box

        def worker() -> None:
            try:
                work(box.put)
            except BaseException as exc:     # `Cancelled` 繼承 BaseException
                box.put(("!", exc))
            finally:
                box.put((".", None))

        def pump() -> None:
            while True:
                try:
                    kind, payload = box.get_nowait()
                except queue.Empty:
                    break
                if kind == ".":
                    self._job_box = None
                    done(None)
                    return
                if kind == "!":
                    self._job_box = None
                    done(payload)
                    return
                self._job_say(kind, payload)
            self.after(80, pump)

        threading.Thread(target=worker, daemon=True).start()
        self.after(80, pump)

    def _model_progress(self, line: str, frac: float) -> None:
        r"""模型下載的進度(**從工作執行緒被呼叫**)。

        ⚠️ **第一次轉檔要下載 2~3 GB**,而原生視窗沒有黑視窗——這段進度掉在地上的
        話,使用者看到的是一個十幾二十分鐘不動的畫面,合理地判斷成當掉了。
        ⚠️ **這裡不准碰任何 widget**:丟進 queue,由主執行緒的幫浦畫(Tk 不是執行緒
        安全的)。⚠️ 沒有工作在跑就丟掉——那時沒有人在等這個訊息。
        ⚠️ **要送給正在跑的那一頁,不能一律送轉檔頁**:文件頁轉錄音錄影時,標點模型的
        下載跑在主行程的工作執行緒裡、這支真的會被呼叫,而使用者眼前是文件頁。一律送
        `run_stage` 的話,他看到的是文件頁停在 0% 十幾二十分鐘不動,那 2-3 GB 的進度
        畫在另一個頂層分頁上;而且文件頁收尾只叫 `_doc_say`,沒有人清掉轉檔頁那一行,
        它會一直掛著。三條路徑裡只有 `doc` 用另一組 widget(錄音與檔案轉檔共用轉檔頁的
        那一組,見 `_stage`)。⚠️ 讀 `_busy` 是跨執行緒的,但那只是一個 bool 的讀取。"""
        box = self._job_box
        if box is not None:
            box.put(("stage" if self._busy["doc"] else "run_stage", (line, frac)))

    def _job_say(self, kind: str, payload) -> None:
        """工作執行緒送回來的一則訊息,已經在主執行緒上了。"""
        if kind == "text":
            self._doc_set_result(payload)
        elif kind == "stage":
            stage, frac = payload
            self._doc_say(stage)
            self._doc_bar.configure(value=max(0.0, min(1.0, frac)) * 100)
            self._taskbar(frac)
        elif kind == "run_stage":
            stage, frac = payload
            self._stage(stage)
            self._run_bar.configure(value=max(0.0, min(1.0, frac)) * 100)
            self._taskbar(frac)
        elif kind == "run_preview":
            self._run_set_preview(payload)
        elif kind == "outcome":
            # 「跑完了,但不乾淨」(目前只有批次:有檔案**轉失敗**)。⚠️ **略過的不算**:
            # `BatchReport.skipped` 混了「格式不支援」與「已經有同名 md、不重做」兩種,
            # 而後者是重跑同一個資料夾必然發生的事——拿它判黃的話,每跑第二次都會亮黃。
            self._job_outcome = payload
        elif kind == "clips":
            self._naming_clips = payload
        elif kind == "audit_clip":
            self._audit_sound(*payload)
        elif kind == "named":
            # ⚠️ 命名區要在**主執行緒**長出來:它要建一整排 widget。四條路(檔案轉檔、
            # 現場收音、重設講者／重新分群)送的都是 `_naming_show` 的關鍵字參數——
            # 算好的核對資料與試聽片段(現場收音在刪錄音目錄之前就剪好了;重設講者本來
            # 就已經讀了整份音訊)直接交過來,不必再剪一次。
            self._naming_show(**payload)
        elif kind == "naming_status":
            self._naming_say(payload)
        elif kind == "dirs":
            # 成品所在的資料夾。⚠️ **批次可能跨多個資料夾**,所以存的是清單。
            self._doc_dirs = payload
        elif kind == "run_dirs":
            # 「聲音→MD」批次的成品所在(同上;單檔那條不送,留 None = output)
            self._run_dirs = payload

    # ---- 聲音→MD:名單與聲紋 --------------------------------------------- #
    def _roster_page(self) -> ttk.Frame:
        r"""三欄:與會人員名單 ｜ 聲紋資料管理 ｜ 修改名稱作業。

        ⚠️ **三欄是照網頁版排的**(2026-09-04 使用者出三案設計稿後選定 A;實拍與量到的
        數字見 `docs/dev/native-ui.md`)。先前是「名單」「聲紋庫」兩張卡上下疊,而
        **健檢的結果掛在整頁最底下**——按鈕在那一排的最右邊、結果回到左下角,量到水平
        572px;更糟的是真的抓到可疑樣本時那份清單**整個看不到**:那時這一頁要 748px 高、
        可用只有 588,而它不會捲,`pack` 分不到空間就把結果區壓成 6px,卡片的圓角照畫。
        ⚠️ **所以這一頁改成整頁可捲**(同網頁版:內容長過視窗就捲)。捲軸那一格
        **一直留著**(`SCROLL_W`),理由見那個常數。
        ⚠️ **欄寬寫死、不准由內容決定**(同 `_two_columns`):`tk.Text` 不給 `width`
        就是 80 個字元寬、`tk.Listbox` 是 20——放進 292px 的欄裡,那一欄會被自己的
        內容撐開,而畫面上看起來只是「右邊那欄被切掉了」。三個清單都給了 `width=1`
        再靠 `fill` 撐滿;`test_the_roster_page_columns_never_overflow` 守著。
        ⚠️ **一行一個名字的純文字編輯**,不是表格:那份檔案本來就是純文字,而**順序
        有意義**(命名時下拉選單照它排,使用者是照部門排的)。
        ⚠️ **改名字時聲紋要跟著走**——不跟的話那個人下次開會**認不出來**,而那個症狀
        長得像分群壞掉。所以存檔前先算一份計畫給使用者看,由他決定要不要一起改
        (判準與失效方式見 `roster` 的 docstring)。"""
        page = ttk.Frame(self._body, style="Page.TFrame")
        page.columnconfigure(0, weight=1)
        # ⚠️ 捲軸那一格的寬度**與捲軸在不在無關**(`_scroll_refit` 只 `grid_remove`,
        # 欄的 minsize 留著),所以三欄不會因為健檢有沒有結果而變寬變窄。
        page.columnconfigure(1, minsize=self.px(SCROLL_W))
        page.rowconfigure(0, weight=1)
        canvas = tk.Canvas(page, bg=self.pal["page"], highlightthickness=0, bd=0)
        canvas.grid(row=0, column=0, sticky="nsew")
        inner = ttk.Frame(canvas, style="Page.TFrame")
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        bar = ttk.Scrollbar(page, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        # ⚠️ `stretch=True`:三張卡要一樣高、而且拉到底(見 `_scroll_refit`)。
        box = ScrollBox(canvas, inner, win, bar, stretch=True)
        self._scrolls["roster"] = box
        canvas.bind("<Configure>", lambda _e: self._scroll_refit(box))
        inner.bind("<Configure>", lambda _e: self._scroll_refit(box))

        cards = []
        for i, share in enumerate(ROSTER_SHARES):
            col = i * 2
            if i:
                inner.columnconfigure(col - 1, minsize=self.px(CARD_GAP))
            inner.columnconfigure(col, minsize=self._roster_col(share))
            inner.rowconfigure(0, weight=1)
            card = ttk.Frame(inner, style="Card.TFrame", padding=self.px(CARD_PAD))
            # ⚠️ **三張卡一樣高、而且拉到底**(2026-09-04 使用者:「請做成一樣高,
            # 看來較一致,高度請以拉到最下面,留下灰色邊緣」):底下那圈灰是內容區的
            # 下 `pady`,與左右同寬(`_pack_body`,那條 2026-09-03 就定了)。
            # ⚠️ 光靠 `sticky` 不夠——Canvas 的 window item 用的是**請求**高度,所以
            # `_scroll_refit` 那邊還要把 item 撐到可視高度(`ScrollBox.stretch`)。
            card.grid(row=0, column=col, sticky="nsew")
            self._shares[card] = (share, self._roster_reserve())
            cards.append(card)
        self._roster_build(cards[0])
        self._vp_manage_build(cards[1])
        self._vp_move_build(cards[2])
        self._roster_load()
        self._vp_refresh()
        return page

    def _roster_reserve(self) -> int:
        """名單與聲紋那一頁分欄前要先扣掉的:兩條溝 ＋ 捲軸那一格(見 `_col_width`)。"""
        return self.px(CARD_GAP) * 2 + self.px(SCROLL_W)

    def _roster_col(self, share: float) -> int:
        """那一頁某一欄的寬度——與說明小字的換行寬度同一條式子(見 `_col_width`)。"""
        return self._col_width(self.px(MAX_CONTENT), share, self._roster_reserve())

    # ---- 第一欄:與會人員名單 -------------------------------------------- #
    def _roster_build(self, card: ttk.Frame) -> None:
        """與會人員名單(命名時下拉選單的來源):編輯、儲存、以及改名時的追問。"""
        ttk.Label(card, text="與會人員名單", style="CardH.TLabel").pack(anchor="w")
        self._wrap(ttk.Label(
            card, text="一行一個名字。這份名單是替講者命名時那個下拉選單的來源,"
                       "順序就是選單的順序。改了名字,工具會問你要不要把那個人的"
                       "聲紋一起改掛過去。",
            style="Hint.TLabel", justify="left")).pack(
                anchor="w", pady=(self.px(SP_SM), self.px(SP_LG)))
        wrap = ttk.Frame(card, style="Sunken.TFrame", padding=self.px(SP_XS))
        # ⚠️ **不給 `expand`**(2026-09-04 使用者:「與會人員名單,不需要那麼多列,請
        # 減少 5 列」):卡片是撐滿高度的,讓這一框跟著長就是 22 列——比它需要的多。
        # 多的空白留在卡片底下,三張白框照樣對齊(同中欄那一框)。
        wrap.pack(fill="x")
        # ⚠️ `width=1`:不給的話是 80 個字元寬,這一欄只有 292(見 `_roster_page`)。
        self._roster_box = tk.Text(wrap, font=(self.fam, 10), relief="flat", bd=0,
                                   undo=True, highlightthickness=0, wrap="none",
                                   width=1, height=ROSTER_LINES,
                                   bg=self.pal["field"], fg=self.pal["ink"],
                                   insertbackground=self.pal["ink"],
                                   selectbackground=self.pal["row_sel"],
                                   padx=self.px(SP_SM), pady=self.px(SP_XS))
        bar = ttk.Scrollbar(wrap, orient="vertical", command=self._roster_box.yview)
        self._roster_box.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self._lockable(self._roster_box).pack(side="left", fill="both", expand=True)

        row = ttk.Frame(card, style="CardBody.TFrame")
        row.pack(fill="x", pady=(self.px(SP_MD), 0))
        self._lockable(HandButton(row, text="儲存名單", style=skin.CTA_STYLE,
                                  command=self._roster_save)).pack(side="left")
        HandButton(row, text="重新載入", style="Small.TButton",
                   command=self._roster_load).pack(side="left",
                                                   padx=(self.px(SP_SM), 0))
        self._roster_status = self._wrap(
            ttk.Label(card, style="Status.TLabel", justify="left"))
        self._roster_status.pack(anchor="w", pady=(self.px(SP_SM), 0))
        self._roster_ask = self._wrap(
            ttk.Label(card, style="Warn.TLabel", justify="left"))
        self._roster_confirm = ttk.Frame(card, style="CardBody.TFrame")
        self._roster_plan = None

    # ---- 第二欄:聲紋資料管理 -------------------------------------------- #
    def _vp_manage_build(self, card: ttk.Frame) -> None:
        r"""聲紋庫:登記了誰、刪掉誰的聲紋、以及健檢。

        ⚠️ **這一區的每一個動作都會影響「下次開會認不認得出人」**,而錯誤的症狀都
        長得像分群壞掉——所以每一顆鈕按下去之前都先講清楚會發生什麼。
        ⚠️ **「已登記的人」用看得見的清單、不是下拉**(2026-09-04):網頁版那個下拉
        兼兩件事——多選要刪的人、以及「聲紋庫裡到底有誰」的完整檢視,而 Tk 的
        `ttk.Combobox` **沒有多選**。攤開來反而兩件事都做得更好:不必先拉開就看得到
        名單,而且這一欄本來就空著。
        ⚠️ **順序照與會名單,不是字典序**(同網頁版 `vp_names_in_roster_order`):左邊
        名單是使用者自己排的(照部門),兩邊順序不一樣的話,要在七十幾個人裡找同一個
        人就得重新掃一遍。"""
        ttk.Label(card, text="聲紋資料管理", style="CardH.TLabel").pack(anchor="w")
        self._vp_summary = self._wrap(
            ttk.Label(card, style="Hint.TLabel", justify="left"))
        self._vp_summary.pack(anchor="w", pady=(self.px(SP_SM), self.px(SP_LG)))
        ttk.Label(card, text="已登記的人(要刪除聲紋時在此選取,可多選)",
                  style="Field.TLabel").pack(anchor="w")
        wrap = ttk.Frame(card, style="Sunken.TFrame", padding=self.px(SP_XS))
        # ⚠️ **不給 `expand`**(見 `VP_LIST_LINES`):卡片是撐滿高度的,一旦讓這一框
        # 跟著長,多出來的高度全被它吃掉,健檢結果就被推到視野外——而那是這一整刀
        # 要修的病。多的空白留在卡片底下,三張白框照樣對齊。
        wrap.pack(fill="x", pady=(self.px(SP_XS), 0))
        # ⚠️ `width=1` 同名單那一框;`exportselection=0` 是因為 Tk 預設會把選取項送進
        # X selection,而點別的地方(名單框、下拉)就會把這裡的選取**清掉**——使用者
        # 看到的是「勾好的人自己不見了」。
        self._vp_list = tk.Listbox(
            wrap, font=(self.fam, 10), relief="flat", bd=0, activestyle="none",
            selectmode="extended", exportselection=0, highlightthickness=0,
            width=1, height=VP_LIST_LINES, bg=self.pal["field"],
            fg=self.pal["ink"], selectbackground=self.pal["row_sel"],
            selectforeground=self.pal["ink"])
        bar = ttk.Scrollbar(wrap, orient="vertical", command=self._vp_list.yview)
        self._vp_list.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self._vp_list.pack(side="left", fill="both", expand=True)

        row = ttk.Frame(card, style="CardBody.TFrame")
        row.pack(fill="x", pady=(self.px(SP_MD), 0))
        # ⚠️ **實心藍只給「聲紋健檢」**(2026-09-03 使用者指定的主次:主要動作實心、
        # 次要線框或灰):健檢是這一欄唯一**只讀不寫**的動作,擺在最顯眼的位置正好,
        # 而「刪除選取」不該比它搶眼。
        self._lockable(HandButton(row, text="刪除選取", style="Small.TButton",
                                  command=self._vp_delete_people)).pack(side="left")
        self._lockable(HandButton(row, text="清除全部", style="Small.TButton",
                                  command=self._vp_clear_ask)).pack(
                                      side="left", padx=(self.px(SP_SM), 0))
        HandButton(row, text="重新載入", style="Small.TButton",
                   command=self._vp_reload).pack(side="left",
                                                 padx=(self.px(SP_SM), 0))
        HandButton(row, text="聲紋健檢", style=skin.RUN_SMALL_STYLE,
                   command=self._vp_check).pack(side="left",
                                                padx=(self.px(SP_SM), 0))
        self._vp_status = self._wrap(
            ttk.Label(card, style="Status.TLabel", justify="left"))
        self._vp_status.pack(anchor="w", pady=(self.px(SP_SM), 0))
        # 「清除全部」的頁內追問(⚠️ **不用 modal**:本 repo 的確認一律是頁內問句,
        # 唯一的例外是標題列的 X——那個沒有頁內等價物,見 `_ask_close`)。
        self._vp_clear_confirm = ttk.Frame(card, style="CardBody.TFrame")
        self._vp_found = ttk.Frame(card, style="CardBody.TFrame")
        self._vp_picks = {}

    # ---- 第三欄:修改名稱作業 -------------------------------------------- #
    def _vp_move_build(self, card: ttk.Frame) -> None:
        r"""改掉一個人的名字:**聲紋庫、與會名單、落地的命名草稿一起改**。

        ⚠️ **三處缺一不可**(`roster.rename_everywhere` 的 docstring 記著每一處漏掉
        會怎樣)。2026-09-05 之前這一欄只動聲紋庫、標題也因此叫「改掛聲紋」,而使用
        說明寫的是網頁版那一套(兩邊一起改)——那條落差正是這次補齊的東西。
        ⚠️ **合併要先問**:新名字已經有樣本時,這個動作實質上是「把兩個人併成一個」
        (`voiceprints.rename_plan().merges`)。如果它們本來就是同一個人的兩種寫法,
        那正是使用者要的;如果不是,就等於親手製造「一個名字底下裝了兩個人」,而那
        是最難發現的一種錯——成品看起來只是少了一個人。
        ⚠️ **追問裡要有數字**:「3 個併進 2 個、共 5 個」才判斷得了,只說「名字重複,
        確定嗎?」等於沒問。"""
        ttk.Label(card, text="修改名稱作業", style="CardH.TLabel").pack(anchor="w")
        self._wrap(ttk.Label(
            card, text="改掉一個人的名字(打錯字、或想補上部門時用)。"
                       "聲紋庫與左邊的與會名單會一起改——只改一邊的話,"
                       "下次開會就認不出這個人了。",
            style="Hint.TLabel", justify="left")).pack(
                anchor="w", pady=(self.px(SP_SM), self.px(SP_LG)))
        self._vp_old = tk.StringVar(value="")
        self._vp_new = tk.StringVar(value="")
        self._vp_merge_pending: tuple[str, str] | None = None
        ttk.Label(card, text="要改名的人", style="Field.TLabel").pack(anchor="w")
        self._vp_old_pick = ttk.Combobox(card, textvariable=self._vp_old,
                                         state="readonly", width=VP_FIELD_W)
        self._lockable(self._vp_old_pick).pack(fill="x", pady=(self.px(SP_XS), 0))
        self._combo_setup(self._vp_old_pick)
        # 選了舊名字,新名字那格**同時帶入同一個名字**、直接改就好(2026-09-03 使用者:「正常
        # 前面選了,後面格子名字要同時帶進去,後面的格子不用下拉式選單,可以直接修改即可」)。
        # ⚠️ 選完焦點**直接跳到新名字那格**、游標在最後(2026-09-03 使用者:「選擇完後,請直接
        # FOCUS 到輸入的下一個欄位」):`_pick_choose` 先把焦點放回下拉,這個綁定在它之後跑。
        self._vp_old_pick.bind("<<ComboboxSelected>>", lambda _e: self._vp_carry())
        ttk.Label(card, text="改成", style="Field.TLabel").pack(
            anchor="w", pady=(self.px(SP_MD), 0))
        self._vp_new_entry = ttk.Entry(card, textvariable=self._vp_new,
                                       width=VP_FIELD_W)
        self._lockable(self._vp_new_entry).pack(fill="x", pady=(self.px(SP_XS), 0))
        self._lockable(HandButton(card, text="改名", style=skin.CTA_STYLE,
                                  command=self._vp_rename)).pack(
                                      anchor="w", pady=(self.px(SP_MD), 0))
        self._vp_move_status = self._wrap(
            ttk.Label(card, style="Status.TLabel", justify="left"))
        self._vp_move_status.pack(anchor="w", pady=(self.px(SP_SM), 0))
        # 合併的追問(平常收著,只有「新名字已經有聲紋」那條路才長出來)。
        self._vp_merge_ask = self._wrap(
            ttk.Label(card, style="Warn.TLabel", justify="left"))
        self._vp_merge_row = ttk.Frame(card, style="CardBody.TFrame")

    def _vp_carry(self) -> None:
        """「要改掛的人」選了誰:改掛到那格填同一個名字,焦點過去、游標放在最後。"""
        self._vp_new.set(self._vp_old.get())
        self._vp_new_entry.focus_set()
        self._vp_new_entry.icursor("end")

    def _vp_refresh(self) -> None:
        """摘要、已登記的人、以及改掛那個下拉——動過聲紋庫就三個一起重畫。

        ⚠️ **這是唯一的產生點**(同網頁版 `_name_dropdowns` 的那條註解):任何動到
        聲紋庫名字的動作都要回頭叫它,漏一個就是「刪掉的人還在清單上」。"""
        names = roster.voiceprint_names()
        total = len(voiceprints_store.load()[0])
        self._vp_summary.configure(text=self._vp_summary_text(names, total))
        self._vp_list.delete(0, "end")
        for name in names:
            self._vp_list.insert("end", name)
        self._vp_old_pick.configure(values=names)

    def _vp_clear_ask(self) -> None:
        r"""按下「清除全部」:**先問**,一個位元組都還沒動。

        ⚠️ **這件事收不回來**,而且代價與「刪除選取」不是同一個量級:每個人的聲紋
        都是一次一次替講者命名累積出來的,清掉之後只能從頭再來,而症狀(每個人都
        認不出來)看起來會像工具壞了。⚠️ **預設沒有任何鈕是「確定」**——追問區平常
        收著,按過才長出來,而長出來的那顆鈕上寫的是**數字**,不是「確定」二字。"""
        names = roster.voiceprint_names()
        if not names:
            self._vp_status.configure(text="聲紋庫本來就是空的,沒有東西可以清。")
            return
        total = len(voiceprints_store.load()[0])
        for w in self._vp_clear_confirm.winfo_children():
            w.destroy()
        self._wrap(ttk.Label(
            self._vp_clear_confirm,
            text=f"確定要清掉 {len(names)} 個人、共 {total} 個聲紋樣本嗎?"
                 "這件事收不回來,之後每個人都要重新命名一次才會再被認出來。"
                 "只想清掉某幾個人的話,用上面的清單選取、按「刪除選取」。",
            style="Warn.TLabel", justify="left")).pack(anchor="w")
        row = ttk.Frame(self._vp_clear_confirm, style="CardBody.TFrame")
        row.pack(anchor="w", pady=(self.px(SP_SM), 0))
        HandButton(row, text=f"清掉全部 {total} 個樣本", style="Small.TButton",
                   command=self._vp_clear_all).pack(side="left")
        HandButton(row, text="取消", style="Small.TButton",
                   command=self._vp_clear_hide).pack(
                       side="left", padx=(self.px(SP_SM), 0))
        self._vp_clear_confirm.pack(anchor="w", fill="x",
                                    pady=(self.px(SP_SM), 0))

    def _vp_clear_hide(self) -> None:
        """把「清除全部」的追問收起來(聲紋庫一個位元組都沒動)。"""
        self._vp_clear_confirm.pack_forget()

    def _vp_clear_all(self) -> None:
        """真的清掉整個聲紋庫(追問裡按過那顆帶數字的鈕才走得到這裡)。"""
        gone = len(voiceprints_store.load()[0])
        voiceprints_store.clear()
        self._vp_clear_hide()
        self._vp_check_clear()
        self._vp_refresh()
        self._vp_status.configure(
            text=f"已清除全部聲紋({gone} 個樣本)。與會名單沒有被動到,"
                 "已經轉好的逐字稿也不受影響——只有「下次開會自動認人」要從頭累積。")

    def _vp_summary_text(self, names: list[str], total: int) -> str:
        r"""中間欄那行摘要。**兩句安全網掛在這一行**(同網頁版 `data_tabs.vp_summary`)。

        ⚠️ **「只在聲紋庫、不在名單上」要主動講**(見 `roster.orphan_names`):它是
        改名只改了一半的**唯一**收尾線索,而失聯本身沒有任何症狀——要等下次開會那
        個人認不出來才會發現,那時沒有人會聯想到幾週前改過名字。
        ⚠️ **健檢也要主動講**,不能做成「要自己想到去按」:整件事的教訓就是「錯誤
        沒有被看見」,健檢等人想到才按等於把同一個問題再犯一次。掃描是純矩陣運算
        (實測 138 個樣本 < 0.1 秒),付得起每次重畫都掃一遍。
        ⚠️ **健檢算不出來就當作沒事**:它是附加資訊,絕不能連坐這一行本來要講的話。
        ⚠️ **不寫 Markdown 的粗體記號**:這是普通的 ttk.Label,`**` 會原樣畫出來
        (網頁版那一份有記號,兩邊因此不是同一個字串)。"""
        if not names:
            return "聲紋庫是空的。替講者命名之後,他的聲紋就會被記起來。"
        line = (f"目前 {len(names)} 個人、{total} 個聲紋樣本。"
                "聲紋是「下次開會自動認出是誰」的依據——動它之前先想清楚。")
        orphans = roster.orphan_names()
        if orphans:
            shown = "、".join(f"「{o}」" for o in orphans[:roster.ORPHANS_SHOWN])
            rest = len(orphans) - roster.ORPHANS_SHOWN
            if rest > 0:
                shown += f"、等 {rest} 人"
            line += (f"\n⚠ 有 {len(orphans)} 個名字只在聲紋庫、不在名單上:{shown}"
                     "——多半是改名時只改了一邊,用右邊的「修改名稱作業」"
                     "改成名單上的名字即可。")
        try:
            found = len({s.name for s in voiceprints_store.suspects()})
        except Exception:               # noqa: BLE001 - 健檢不得連坐主要內容
            logger.debug("聲紋健檢摘要失敗", exc_info=True)
            return line
        if found:
            line += (f"\n⚠ 健檢發現 {found} 個人的聲紋可能混到別人,"
                     "按下面的「聲紋健檢」看看。")
        return line

    def _vp_selected_people(self) -> list[str]:
        """「已登記的人」現在選了誰。"""
        return [self._vp_list.get(i) for i in self._vp_list.curselection()]

    def _vp_delete_people(self) -> None:
        r"""刪掉選取那幾個人的**全部**聲紋樣本。

        ⚠️ **這件事收不回來**,而症狀看起來會像分群壞掉:那個人下次開會不會被自動
        認出來,得重新命名一次才會重新累積。所以講清楚刪了誰、幾個樣本。"""
        picked = self._vp_selected_people()
        if not picked:
            self._vp_status.configure(text="請先在上面的清單裡選要刪掉聲紋的人。")
            return
        counts = collections.Counter(voiceprints_store.load()[0])
        gone = sum(counts.get(name, 0) for name in picked)
        for name in picked:
            voiceprints_store.delete(name)
        self._vp_refresh()
        self._vp_check_clear()
        self._vp_status.configure(
            text=f"已刪掉 {'、'.join(picked)} 的聲紋,共 {gone} 個樣本。"
                 "⚠ 這幾位下次開會不會被自動認出來,要再命名一次才會重新累積。")

    def _vp_reload(self) -> None:
        """重讀聲紋庫(別的地方動過檔案、或剛轉完一場會議之後)。"""
        self._vp_refresh()
        self._vp_check_clear()
        self._vp_status.configure(text="已重新載入聲紋庫。")

    def _vp_check_clear(self) -> None:
        """把健檢的結果收掉(聲紋庫一動,上一輪的結果就對不上了)。"""
        for child in self._vp_found.winfo_children():
            child.destroy()
        self._vp_picks = {}
        self._vp_found.pack_forget()

    def _vp_rename(self) -> None:
        r"""按下「改名」:單純改名就直接做,**合併則先問**。

        ⚠️ **合併是同一個動作、不同的後果**,所以介面必須問得出差別(見
        `voiceprints.RenamePlan` 的 docstring)。⚠️ **被擋下來時畫面原封不動**:
        名字都還在,只在底下多一段話。"""
        self._vp_merge_hide()
        old, new = self._vp_old.get().strip(), self._vp_new.get().strip()
        if not old or not new:
            self._vp_move_status.configure(
                text="請選一個要改名的人,並填入新名字。")
            return
        if old == new:
            self._vp_move_status.configure(text="新名字與原本相同,沒有變更。")
            return
        plan = voiceprints_store.rename_plan(old, new)
        if plan.merges:
            self._vp_merge_pending = (old, new)
            self._vp_merge_ask.configure(text=self._merge_question(old, new, plan))
            self._vp_merge_ask.pack(anchor="w", fill="x",
                                    pady=(self.px(SP_SM), 0))
            for w in self._vp_merge_row.winfo_children():
                w.destroy()
            HandButton(self._vp_merge_row, text="確認合併", style="Small.TButton",
                       command=self._vp_merge_go).pack(side="left")
            HandButton(self._vp_merge_row, text="暫不修改", style="Small.TButton",
                       command=self._vp_merge_cancel).pack(
                           side="left", padx=(self.px(SP_SM), 0))
            self._vp_merge_row.pack(anchor="w", pady=(self.px(SP_SM), 0))
            self._vp_move_status.configure(text="")
            return
        self._vp_rename_do(old, new)

    @staticmethod
    def _merge_question(old: str, new: str, plan) -> str:
        r"""合併的追問。⚠️ **一定要把數字講出來**:「3 個併進 2 個、共 5 個」才判斷
        得了,只說「名字重複,確定嗎?」等於沒問。⚠️ **不寫 Markdown 記號**(這是
        ttk.Label,`**` 會原樣畫出來)。"""
        lines = [
            f"⚠ 「{new}」已經有聲紋了——這個動作會把兩個名字合併。",
            f"・「{old}」的 {plan.moving} 個樣本會併進「{new}」"
            f"(現有 {plan.existing} 個)",
            f"・合併後共 {plan.total} 個",
        ]
        if plan.dropped:
            lines.append(
                f"・⚠ 超過每人的上限,會淘汰 {plan.dropped} 個最舊的樣本"
                "(最舊的往往是別場次、別麥克風錄的,那種最難再取得)")
        lines.append(
            "只有在它們本來就是同一個人(兩種寫法)時才該按下去。如果是兩個不同的"
            "人,合併之後會變成「一個名字底下裝了兩個人」,那種錯在逐字稿裡看起來"
            "只是「少了一個人」,很難發現。")
        return "\n".join(lines)

    def _vp_merge_hide(self) -> None:
        """把合併追問收起來。"""
        self._vp_merge_pending = None
        self._vp_merge_ask.pack_forget()
        self._vp_merge_row.pack_forget()

    def _vp_merge_go(self) -> None:
        """「確認合併」:看過數字才走得到這裡。"""
        if self._vp_merge_pending is None:
            return
        old, new = self._vp_merge_pending
        self._vp_merge_hide()
        self._vp_rename_do(old, new)

    def _vp_merge_cancel(self) -> None:
        r"""「暫不修改」:聲紋庫與名單**一個位元組都不動**。

        ⚠️ **這顆鈕少不得**:沒有它的話,不想合併的人只能不理那段追問(它會一直掛
        在那裡),於是「他決定不改」與「他根本沒看到」在畫面上長得一模一樣。"""
        if self._vp_merge_pending is None:
            return
        old, _new = self._vp_merge_pending
        self._vp_merge_hide()
        self._vp_move_status.configure(
            text=f"沒有動任何東西:「{old}」的名字與聲紋都維持原樣。")

    def _vp_rename_do(self, old: str, new: str) -> None:
        """真的改名:聲紋庫、與會名單、落地的命名草稿一起改。"""
        moved, total = roster.rename_everywhere(old, new)
        if not moved:
            # 聲紋庫裡沒有他的樣本:名單那一半照樣改掉了,講清楚免得看起來像沒反應
            self._vp_move_status.configure(
                text=f"「{old}」在聲紋庫裡沒有樣本;與會名單已改成「{new}」。")
        else:
            self._vp_move_status.configure(
                text=f"已把「{old}」改名為「{new}」:{moved} 個聲紋樣本"
                     f"(目前共 {total} 個),與會名單也一併更新。")
        self._vp_new.set("")            # ⚠️ 清空:留著上一次的字,下次改別人時很容易直接按下去
        self._vp_refresh()
        self._vp_check_clear()
        self._roster_load()             # 名單那一半也改了,左欄要跟著重畫

    def _vp_check(self) -> None:
        r"""健檢:找出「可能存錯名字」的樣本。

        ⚠️ **兩道關卡不得只留一道**(2026-08-08 真的刪錯過才補的第二道):**差距**
        (跟某個別人比跟自己人還像多少)＋ **孤例度**(那個別人要領先第二個名字夠
        多)。通道效應(共用麥克風、遠端連線)會讓**一整群**人一起變像、第一道必然
        成立,真的存錯人才會只對**某一個**名字突出。
        ⚠️ **代價不對稱**:留著頂多偶爾認錯(當場可改),刪錯了那個人的遠端發言就
        **再也認不出來**,而症狀長得像分群壞掉。所以這裡只列出來、由使用者逐筆勾,
        預設一個都不勾。
        ⚠️ **每一列都要會斷行**(2026-09-04 排成三欄之後):這一欄只有 401px,而一列
        是「某某某 某部某處 的第 N 個樣本 —— 比較像「另一個某某某 某部某處」」——
        不斷行的話後面那半直接被卡片邊界切掉,而畫面上看起來只是名字比較短。"""
        found = voiceprints_store.suspects()
        self._vp_check_clear()
        if not found:
            self._vp_status.configure(text="健檢沒有發現可疑的樣本。")
            return
        self._wrap(ttk.Label(
            self._vp_found,
            text=f"這 {len(found)} 個樣本聽起來比較像別人。⚠ 刪之前先想一下:留著"
                 "頂多偶爾認錯(當場可以改),刪錯了那個人以後就再也認不出來——"
                 "而那個症狀看起來會像「講者分不出來」。共用麥克風或遠端連線會讓"
                 "一整群人一起變像,那種不是存錯人。",
            style="Hint.TLabel", justify="left")).pack(
                anchor="w", pady=(0, self.px(SP_SM)))
        for i, s in enumerate(found):
            row = ttk.Frame(self._vp_found, style="CardBody.TFrame")
            row.pack(fill="x", pady=(0, self.px(SP_XS)))
            var = tk.BooleanVar(value=False)      # ⚠️ 預設一個都不勾
            self._vp_picks[i] = (var, s)
            ttk.Checkbutton(row, variable=var, style="Card.TCheckbutton").pack(
                side="left", anchor="n")
            self._wrap(ttk.Label(
                row, text=f"{s.name} 的第 {s.index + 1} 個樣本 —— "
                          f"比較像「{s.like_name}」",
                style="Hint.TLabel", justify="left"),
                minus=self.px(CHECK_W)).pack(side="left", anchor="w",
                                             padx=(self.px(SP_XS), 0))
        # ⚠️ 這一顆是**工作進行中要鎖的**(它會寫聲紋庫),而它是按了健檢才長出來的
        # ——所以只能在這裡登記,不能在建頁時一次收齊(見 `_lockable`)。
        self._lockable(HandButton(self._vp_found, text="刪掉勾選的樣本",
                                  style="Small.TButton",
                                  command=self._vp_delete)).pack(
                                      anchor="w", pady=(self.px(SP_SM), 0))
        self._vp_found.pack(fill="x", pady=(self.px(SP_MD), 0))
        self._vp_status.configure(text="")

    def _vp_delete(self) -> None:
        """刪掉勾選的那幾個樣本(整個名字的樣本都被勾時要特別講一句)。"""
        items = [(s.name, s.index) for _i, (var, s) in self._vp_picks.items()
                 if var.get()]
        if not items:
            self._vp_status.configure(text="一個都沒勾。")
            return
        gone = voiceprints_store.delete_samples(items)
        left = [n for n, _ in items if n not in voiceprints_store.known_names()]
        note = (f"⚠ {'、'.join(dict.fromkeys(left))} 在聲紋庫裡已經一個樣本都不剩,"
                "下次開會不會被自動認出來。" if left else "")
        self._vp_check()
        self._vp_status.configure(text=f"已刪掉 {gone} 個樣本。" + note)
        self._vp_refresh()

    def _roster_load(self) -> None:
        # ⚠️ **先解鎖再寫**:工作進行中這一框是 `disabled` 的(見 `_data_lock`),而
        # `tk.Text` 在那個狀態下**吃不進任何插入**——症狀是「按了重新載入,名單一片
        # 空白」。寫完放回原本的狀態,不是一律打開。
        was = str(self._roster_box.cget("state") or "normal")
        self._roster_box.configure(state="normal")
        self._roster_box.delete("1.0", "end")
        self._roster_box.insert("1.0", "\n".join(roster.current()))
        self._roster_box.edit_reset()
        self._roster_box.configure(state=was)
        self._roster_hide_ask()
        self._roster_status.configure(
            text=f"目前 {len(roster.current())} 人。")

    def _roster_hide_ask(self) -> None:
        self._roster_plan = None
        self._roster_ask.pack_forget()
        self._roster_confirm.pack_forget()
        for child in self._roster_confirm.winfo_children():
            child.destroy()

    def _roster_save(self) -> None:
        r"""按下「儲存名單」。

        ⚠️ **要動聲紋庫的時候先問**:改名字而聲紋沒跟著走,那個人下次開會就認不
        出來,而症狀長得像分群壞掉。所以這裡先算一份計畫;沒有要問的事才直接存。"""
        before = roster.current()
        plan = roster.plan(before, self._roster_box.get("1.0", "end-1c").split("\n"))
        if not plan.needs_confirming:
            self._roster_hide_ask()
            self._roster_status.configure(text=roster.save(plan))
            return
        self._roster_plan = plan
        self._roster_ask.configure(text=self._roster_question(plan))
        self._roster_ask.pack(anchor="w", pady=(self.px(SP_SM), 0))
        self._roster_confirm.pack(anchor="w", pady=(self.px(SP_SM), 0))
        if plan.rename:
            HandButton(self._roster_confirm, text="名單與聲紋一起改",
                       style=skin.CTA_STYLE,
                       command=lambda: self._roster_commit(True)).pack(side="left")
        HandButton(self._roster_confirm, text="只存名單", style="Small.TButton",
                   command=lambda: self._roster_commit(False)).pack(
                       side="left", padx=(self.px(SP_SM), 0))
        HandButton(self._roster_confirm, text="取消", style="Small.TButton",
                   command=self._roster_hide_ask).pack(
                       side="left", padx=(self.px(SP_SM), 0))

    @staticmethod
    def _roster_question(plan) -> str:
        """要問使用者的那句話。⚠️ **數字一定要講出來**:只說「有些人對不上」的話,
        使用者沒辦法判斷這是不是他要的(他可能剛整批補了 28 個人)。"""
        parts = []
        if plan.rename:
            old, new = plan.rename
            parts.append(f"看起來你把「{old}」改成了「{new}」。"
                         f"要不要把「{old}」的聲紋樣本一起改掛到「{new}」?")
        if plan.stranded:
            parts.append("這幾個人會從名單上消失,但聲紋庫裡還有他們的樣本:"
                         + "、".join(f"{n}({c} 個)" for n, c in plan.stranded)
                         + "。")
        return "".join(parts)

    def _roster_commit(self, rename_voiceprints: bool) -> None:
        plan = self._roster_plan
        if plan is None:                 # 已經被「取消」收掉了
            return
        text = roster.save(plan, rename_voiceprints=rename_voiceprints)
        self._roster_hide_ask()
        self._roster_load()
        self._roster_status.configure(text=text)

    # ---- 聲音→MD:轉檔 --------------------------------------------------- #
    def _run_page(self) -> ttk.Frame:
        r"""把錄音/錄影轉成繁體中文逐字稿(含講者分離與聲紋辨識)。

        ⚠️ **兩欄:左操作、右結果**(2026-08-30 照網頁版重排,`app.py` 的
        `with gr.Row(): Column(scale=5) / Column(scale=7)`)。先前是單欄五張卡直向
        堆疊,而內容區只有 997px、五張卡要 1832px——**預覽框幾乎沒露出來、命名區被
        擠成 1px**(使用者實測回報「錄音中沒有逐字稿」「錄完沒進命名畫面」,那是同
        一個病)。
        ⚠️ **三種模式互斥,一次只顯示一組控制項**(見 `RUN_MODES`):把收音與選檔
        同時攤開正是上面那件事的成因。
        ⚠️ **「一次一檔」是裁決過的**(使用者 2026-07-26):講者編號每檔獨立分群,
        命名是一檔一檔當場做的事。多檔要批次轉就走「文字、圖像→MD」那一頁。
        ⚠️ **模型的快速/精準選擇必須保留**(2026-07-26 曾移除、同日裁定還原)。"""
        page = ttk.Frame(self._body, style="Page.TFrame")
        cols = self._two_columns(page, "run")   # 兩欄、左欄可捲(理由都在那一支)
        inner, right = cols.inner, cols.right

        # 命名區:轉檔前收起來、不佔位(轉完才 pack 回來,見 `_naming_show`)。
        # ⚠️ **在左欄最上方**,不是頁尾——網頁版的註解寫著「放在設定區下方的話,
        # 講者一多,欄位起點就掉到預覽結尾之後,又回到上下捲動(使用者實測回報)」,
        # 而原生版先前正好做了那件被否決的事。
        # ⚠️ 「整張命名卡搬進獨立視窗、主畫面只留一顆『替講者命名…』」那個 2026-08-30
        # 的案子,**使用者 2026-09-02 裁定不做**;另開視窗的只有核對那一張(`_audit_window`)。
        # ⚠️ **只先建一張空卡,裡面的東西用到才建**(2026-09-04,使用者:「查一下啟動
        # 的速度」):量到 `_naming_build` 要 **758ms**,是開窗第二貴的一段——而這張卡
        # **轉檔完成之前根本看不見**。真的要建的那一刻使用者已經等了一小時(同
        # `native-ui.md` 對 22 位那 0.8 秒的判斷),0.75 秒在那裡是無感的。
        # ⚠️ **空卡本身要先建**:它的 `pack` / `pack_forget` 在還沒命名過的時候就會被
        # 呼叫(`_naming_hide` 掛在整頁復位那條路上),而分頁「用到才建」踩過的那個坑
        # 正是 `AttributeError` 被 Tk 吞進紀錄檔、畫面上一個字都沒有。
        self._naming_box = ttk.Frame(inner, style="Card.TFrame",
                                     padding=self.px(CARD_PAD))
        self._naming_ready = False
        # 核對表那張視窗的狀態:⚠️ **跟著空卡一起先建**,`_naming_hide` 會叫
        # `_audit_close()`,而那條路在命名卡建起來之前就走得到。
        self._audit_win: tk.Toplevel | None = None
        self._audit_body: ttk.Frame | None = None
        self._audit_tree: ttk.Treeview | None = None
        self._audit_rows = []
        self._audit_picks: dict[int, tk.BooleanVar] = {}
        self._audit_spk = None
        self._audit_src = ""
        self._naming_result = None
        self._naming_audit: dict = {}
        self._naming_vars: dict[int, tk.StringVar] = {}
        self._naming_btns: dict[int, ttk.Button] = {}
        self._naming_audit_btns: dict[int, ttk.Button] = {}
        self._naming_clues: dict[int, ttk.Label] = {}   # 每一塊的線索(填了名字就收合)
        self._naming_next_row = 0
        self._naming_clips: dict | None = None
        self._playing: int | None = None      # 正在試聽哪一位(None = 沒在放)
        self._naming_after = None             # 排好的「播完把字換回來」

        # ---- 左欄:一張大卡裝三段 ------------------------------------------ #
        # ⚠️ **2026-09-01 照網頁版合併**(使用者選案 C1):三段是同一件事的三個問題
        # (要做什麼 → 這個模式要什麼 → 幾位講者),所以是同一張卡。先前拆成三張,
        # 而每張各有一圈 `CARD_PAD` 內距——左欄因此比網頁版高約 90px,「設定」那張
        # 更是只有兩個控制項卻佔掉一整張卡的份量。
        # ⚠️ **主要動作鈕不在卡裡**(同網頁版):它管的是整個左欄,不是某一張卡。
        card = ttk.Frame(inner, style="Card.TFrame", padding=self.px(CARD_PAD))
        card.pack(fill="x")
        self._run_card = card           # 命名區要 pack 在它前面(見 `_naming_show`)

        # 段一:要做什麼
        ttk.Label(card, text="要做什麼", style="CardH.TLabel").pack(anchor="w")
        # ⚠️ **說明排在控制項「上面」**(2026-09-01 照網頁版):先講這是要做什麼,
        # 再給選項。原生版原本印在下面——讀到選項的當下那句話還沒出現。
        self._mode_info = self._wrap(ttk.Label(card, style="Hint.TLabel",
                                               justify="left"))
        self._mode_info.pack(anchor="w", pady=(self.px(SP_XS), 0))
        self._mode = RUN_MODES[0][0]
        # ⚠️ `RUN_MODES` 每一列是四欄(多一欄說明小字),而 segmented 只吃前三欄。
        track, self._mode_cells = self._segmented(
            card, [(m[0], m[1], m[2]) for m in RUN_MODES], self._mode_show)
        track.pack(fill="x", pady=(self.px(SP_SM), 0))

        # 段二:這個模式自己的東西(收音情境 / 選檔),一次只出現一組
        self._mode_body = ttk.Frame(card, style="CardBody.TFrame")
        self._mode_body.pack(fill="x")
        self._rec_box = self._rec_build(self._mode_body)
        self._file_box = self._file_build(self._mode_body)
        self._relabel_box = self._relabel_build(self._mode_body)

        # 段三:講者人數(兩個模式共用,同網頁版)
        spk = ttk.Frame(card, style="CardBody.TFrame")
        spk.pack(fill="x", pady=(self.px(SP_XL), 0))
        # ⚠️ **「重設講者」那條路要把它收起來**(2026-09-01,這裡與網頁版**刻意
        # 不同**):那條路不重跑分群——講者數在當初轉檔時就定了,md 的段落是那次切
        # 出來的。網頁版把這個欄位一直留在畫面上(它與收音/轉檔共用),而擺一個在
        # 這個模式下按了不會有任何作用的輸入框,跟壞掉長得一模一樣。
        self._speakers_box = spk
        ttk.Label(spk, text="講者人數(0 = 自動偵測)",
                  style="CardH.TLabel").pack(anchor="w")
        self._wrap(ttk.Label(
            spk, text="人少又確定(如 1~3 人)才填;其餘留 0 自動判斷,"
                      f"最多 {MAX_SPEAKERS}",
            style="Hint.TLabel", justify="left")).pack(
                anchor="w", pady=(self.px(SP_XS), 0))
        self._run_speakers = tk.StringVar(value="0")
        # ⚠️ **不設 min/max**(同網頁版那條):超限的輸入一律在這一側 clamp,
        # 讓控制項自己擋會跳出英文錯誤(spec §8:使用者可見訊息一律繁中)。
        # ⚠️ **撐滿整欄**(2026-09-01 照網頁版):`width=5` 的小框擺在一張被螢幕撐寬
        # 的卡片上,右邊就是一整片空白——那正是使用者說「比例不對」的那一類。
        # ⚠️ **要留住它**:檔案轉檔與收尾時要停用(見 `_params_lock`);錄音中則刻意
        # 保持可輸入——那是網頁版明訂的唯一例外。
        self._speakers_spin = ttk.Spinbox(
            spk, textvariable=self._run_speakers, from_=0, to=MAX_SPEAKERS,
            style="Tall.TSpinbox")
        self._speakers_spin.pack(fill="x", pady=(self.px(SP_SM), 0))

        # ---- 卡外:狀態一行,與兩顆等寬的主要動作鈕(照網頁版)-------------- #
        self._rec_bar = self._rec_actions(inner)
        self._file_bar = self._file_actions(inner)
        self._relabel_bar = self._relabel_actions(inner)
        self._adv_card = self._adv_build(inner)

        # ---- 右欄:結果 --------------------------------------------------- #
        run = ttk.Frame(right, style="Card.TFrame", padding=self.px(CARD_PAD))
        # ⚠️ **不要 `expand=True`**(2026-09-01):撐滿的話這張卡會一路長到視窗底,而
        # 裡面只有 20 行的預覽框——多出來的全是空灰。網頁版量到的右欄卡片是 475 邏輯
        # px、**比左欄還短一點**,使用者滿意的正是那個比例;原生版先前是 784。
        run.pack(fill="x")
        head = ttk.Frame(run, style="CardBody.TFrame")
        head.pack(fill="x")
        # ⚠️ 標題括號裡那句照網頁版:進度就畫在這張卡上,不講的話使用者按下開始之後
        # 會盯著左欄等一個不會出現的進度條。
        ttk.Label(head, text="逐字稿預覽(轉檔進度顯示於此)",
                  style="CardH.TLabel").pack(side="left")
        # ⚠️ 開的是**這一趟**的成品所在:單檔/收音/重設講者都在 output,但批次的成品在
        # 原檔旁邊、可能跨好幾個資料夾(`_run_dirs`:None = output,清單 = 批次報告裡的
        # 那幾個)。每一趟開始時歸零(`_run_lock`),否則轉完一批再轉單檔,這顆鈕開的還
        # 是上一批的資料夾——「狀態機的驗收要連跑兩趟」那一族。
        self._run_dirs: list[str] | None = None
        # ⚠️ 穿線框藍(`CTA_STYLE`)而不是一般的灰底小鈕(2026-09-04 使用者圈出這一顆:
        # 「請改為藍色框的按鈕樣式」):它與同一張卡上的「選擇檔案…」是同一款,而停用
        # 時邊框會自己換成灰的(`Sq.cta` 的 `cta-dis`),還沒轉完照樣看得出按不動。
        self._run_open = HandButton(head, text="輸出資料夾…", style=skin.CTA_STYLE,
                                    state="disabled",
                                    command=lambda: doctab.open_output_dirs(
                                        self._run_dirs if self._run_dirs is not None
                                        else [str(OUTPUT_DIR)]))
        self._run_open.pack(side="right")
        # 階段文字與進度條:**閒置時整組收起來**(2026-09-01,對齊網頁版的乾淨畫面)。
        # ⚠️ **不是刪掉**——原生版沒有黑視窗,第一次轉檔那 2-3 GB 的模型下載進度與長
        # 階段的心跳訊息**只剩這裡出得來**(`docs/dev/native-ui.md` §4,那是硬需求)。
        # 收起來的只有「還沒開始跑」那一段時間,見 `_run_progress_show`。
        self._run_prog = ttk.Frame(run, style="CardBody.TFrame")
        self._run_stage = ttk.Label(self._run_prog, text="", style="Status.TLabel")
        self._run_stage.pack(anchor="w", pady=(self.px(SP_MD), self.px(SP_XS)))
        self._run_bar = ttk.Progressbar(self._run_prog, mode="determinate",
                                        maximum=100)
        self._run_bar.pack(fill="x")
        pwrap = ttk.Frame(run, style="Sunken.TFrame", padding=self.px(SP_XS))
        pwrap.pack(fill="both", expand=True, pady=(self.px(SP_SM), 0))
        self._run_pwrap = pwrap         # 進度那組要 pack 在它**前面**(見 `_stage`)
        # ⚠️ **高度要寫死**(同網頁版的 `lines=20`,它的註解:「不讓預覽把頁面
        # 撐滿」):`tk.Text` 不給 height 就是**預設 24 行**,實測請求 564px——
        # 單欄時它一個人就把命名區擠成 1px。行數寫死之後,多的空間由 `expand`
        # 給它,少的時候它自己縮,而不是把別人擠掉。
        self._run_preview = tk.Text(pwrap, font=(self.fam, 9), relief="flat", bd=0,
                                    highlightthickness=0, wrap="word", state="disabled",
                                    height=PREVIEW_LINES,
                                    bg=self.pal["field"], fg=self.pal["ink"],
                                    selectbackground=self.pal["row_sel"],
                                    padx=self.px(SP_SM), pady=self.px(SP_XS))
        pbar = ttk.Scrollbar(pwrap, orient="vertical", command=self._run_preview.yview)
        self._run_preview.configure(yscrollcommand=pbar.set)
        pbar.pack(side="right", fill="y")
        self._run_preview.pack(side="left", fill="both", expand=True)

        self._mode_show(RUN_MODES[0][0])
        # 上次沒做完的命名(關視窗、當機、隔天再開)在這裡接回來——同網頁版的
        # `demo.load → _restore_pending`;這一頁建好才有地方長命名卡。
        self._naming_restore()
        return page

    # ---- 「要做什麼」:三種模式互斥 -------------------------------------- #
    def _mode_show(self, key: str) -> None:
        r"""切到某個模式:只有那一組控制項留在畫面上。

        ⚠️ **模式互斥不是為了好看**:先前收音卡與選檔卡同時攤開,左欄光是輸入就吃掉
        711px,而整個內容區只有 997px——命名區與預覽框都被擠掉。
        ⚠️ **工作進行中不准切**:切走等於把正在跑的那組鈕從畫面上拿掉,而工作還在
        跑——停止鈕就找不到了。
        ⚠️ **卡內那段與卡外的動作列要一起切**(2026-09-01 卡片合併之後):兩者現在
        是分開的兩個容器,只切一邊的症狀是「選了轉錄音檔,底下卻還是開始錄音」。"""
        if self._busy["rec"] or self._busy["run"]:
            return
        self._mode = key
        self._segment_show(self._mode_cells, key)
        self._mode_info.configure(
            text=dict((m[0], m[3]) for m in RUN_MODES)[key])
        # ⚠️ 命名中(`_naming_focus(True)`)動作列與摺疊卡都收著,對沒 pack 的摺疊卡說
        # `before=` 是 TclError(2026-09-02 開頁還原一份命名進度之後接著切模式就撞到;
        # 使用者按不到——那時整張大卡都收著——但程式自己會)。那時只記模式,動作列由
        # `_naming_focus(False)` 收工時照 `_mode` 放回來。
        adv_packed = bool(self._adv_card.winfo_manager())
        for k, box, bar in (("rec", self._rec_box, self._rec_bar),
                            ("file", self._file_box, self._file_bar),
                            ("relabel", self._relabel_box, self._relabel_bar)):
            if k == key:
                box.pack(fill="x", pady=(self.px(SP_XL), 0))
                if adv_packed:
                    bar.pack(fill="x", pady=(self.px(CARD_GAP), 0),
                             before=self._adv_card)
            else:
                box.pack_forget()
                bar.pack_forget()
        if key == "relabel":
            self._speakers_box.pack_forget()
        else:
            self._speakers_box.pack(fill="x", pady=(self.px(SP_XL), 0))

    # ---- 轉現成的錄音、錄影 ---------------------------------------------- #
    def _file_build(self, parent) -> ttk.Frame:
        r"""「轉錄音檔」模式在卡片裡的那一段:挑檔案或資料夾。

        **兩種模式,由輸入的形狀決定**(網頁版 2026-08-06 起;判準是
        `srcfile.looks_like_batch`):剛好一個檔案 → 轉完替講者命名、成品在 output;
        多檔或任何資料夾 → 整批連轉、**不做命名**、成品在原檔旁(`_run_start_batch`)。
        ⚠️ 原生版第一版只做了單檔(2026-08-30 那一刀寫的是「一次一個檔」,照的是更早的
        規則),使用者 2026-09-02 拿網頁版截圖問「為何沒有選擇目錄的按鈕」才補上——
        **版面照網頁版**,只有「包含子資料夾」的位置另外選過案(見下)。
        ⚠️ **選檔是累加不是取代**(同網頁版與文件分頁;2026-08-01 使用者回報:選第二
        個資料夾把第一個蓋掉,等於永遠只能處理一批),所以要有「清空」。"""
        box = ttk.Frame(parent, style="CardBody.TFrame")
        ttk.Label(box, text="要轉哪個檔", style="CardH.TLabel").pack(anchor="w")
        self._wrap(ttk.Label(box, text=FILE_MODE_HINT, style="Hint.TLabel",
                             justify="left")).pack(
                                 anchor="w", pady=(self.px(SP_XS), 0))
        row = ttk.Frame(box, style="CardBody.TFrame")
        row.pack(fill="x", pady=(self.px(SP_SM), self.px(SP_SM)))
        # ⚠️ 三顆都要鎖(使用說明寫著「轉檔中選檔的三顆鈕會鎖住」):選檔是**累加**
        # 的,轉檔中按下去只會把下一趟的路徑混進正在跑的這一趟的欄位裡。
        self._lockable(HandButton(row, text="選擇檔案…", style=skin.CTA_STYLE,
                                  command=self._run_pick)).pack(side="left")
        self._lockable(HandButton(row, text="選擇資料夾…", style=skin.CTA_STYLE,
                                  command=self._run_pick_folder)).pack(
                                      side="left", padx=(self.px(SP_SM), 0))
        self._lockable(HandButton(row, text="清空", style="Small.TButton",
                                  command=self._run_clear)).pack(
                                      side="left", padx=(self.px(SP_SM), 0))
        wrap = ttk.Frame(box, style="Sunken.TFrame", padding=self.px(SP_XS))
        wrap.pack(fill="x")
        self._run_src = tk.Text(wrap, font=(self.fam, 10), height=2, relief="flat",
                                bd=0, highlightthickness=0, wrap="none",
                                bg=self.pal["field"], fg=self.pal["ink"],
                                insertbackground=self.pal["ink"],
                                selectbackground=self.pal["row_sel"],
                                padx=self.px(SP_SM), pady=self.px(SP_XS))
        self._lockable(self._run_src).pack(fill="x")
        # 「包含子資料夾」:網頁版放在「進階參數設定」裡;原生版的摺疊卡 2026-09-02 選案 C
        # 之後餘裕只剩 36 實體 px(`_adv_build`),所以另外出過三案(這裡／摺疊卡先量／
        # 這一版不做),使用者同日選定放在路徑欄下方。⚠️ **預設不勾**(同網頁版;與文件
        # 分頁那顆刻意相反):錄音檔轉一份要數十分鐘,不小心掃到整顆磁碟會跑上好幾天。
        # ⚠️ 它住在 `_file_box` 裡,所以切到收音/重設講者時跟著整段收起來——那兩個模式
        # 沒有來源資料夾,擺一個勾了沒作用的框跟壞掉長得一模一樣。
        self._run_recursive = tk.BooleanVar(value=False)
        opt = ttk.Frame(box, style="CardBody.TFrame")
        opt.pack(fill="x", pady=(self.px(SP_SM), 0))
        ttk.Checkbutton(opt, text="包含子資料夾", variable=self._run_recursive,
                        command=self._run_refresh_summary,
                        style="Card.TCheckbutton").pack(side="left")
        ttk.Label(opt, text="只在選了資料夾時有作用", style="Hint.TLabel").pack(
            side="left", padx=(self.px(SP_SM), 0))
        # 選了什麼的即時摘要(同網頁版,緊接在選檔區之下):**空的時候整行收起來**,
        # 不留一行空白(同文件分頁的 `_doc_summary`)。
        self._run_summary = self._wrap(
            ttk.Label(box, style="Status.TLabel", justify="left"))
        return box

    def _file_actions(self, parent) -> ttk.Frame:
        """「轉錄音檔」的動作列(卡片**外面**,同網頁版)。"""
        bar = ttk.Frame(parent, style="Page.TFrame")
        self._run_btn = HandButton(bar, text="開始轉檔", style=skin.RUN_PAGE_STYLE,
                                   command=self._run_start)
        self._run_stop = HandButton(bar, text="停止", style=skin.STOP_PAGE_STYLE,
                                    state="disabled", command=self._run_stop_click)
        self._pair(bar, self._run_btn, self._run_stop)
        return bar

    def _pair(self, bar: ttk.Frame, left: ttk.Button, right: ttk.Button) -> None:
        r"""把兩顆主要動作鈕排成**等寬、平分整欄**(照網頁版)。

        ⚠️ **不是靠 `side="left"` 加固定內距**:那樣兩顆鈕只有「文字那麼寬」,在一張
        被螢幕撐寬的卡片旁邊就是一片空白——使用者 2026-09-01 圈的正是這個。
        ⚠️ **`uniform` 少不得**:沒有它 `weight=1` 只平分**剩餘**空間,而「停止錄音
        並完成逐字稿」比「開始錄音」長得多,兩顆就不等寬。
        ⚠️ 膠囊底板在水平方向是九宮格(中段可以拉伸),所以拉寬不會把圓角拉變形;
        **高度**則由圖高釘死,不受 `sticky` 影響。"""
        bar.columnconfigure(0, weight=1, uniform="run")
        bar.columnconfigure(1, weight=1, uniform="run")
        left.grid(row=0, column=0, sticky="ew", padx=(0, self.px(CARD_GAP) // 2))
        right.grid(row=0, column=1, sticky="ew", padx=(self.px(CARD_GAP) // 2, 0))

    # ---- 重設講者 -------------------------------------------------------- #
    def _relabel_build(self, parent) -> ttk.Frame:
        r"""「重設講者」模式在卡片裡的那一段:挑一份已經轉好的逐字稿。

        (使用者 2026-08-06 指定這個功能,2026-09-01 接進原生版。)解決的是三件**事
        後**才發現的事:批次轉出來的 md 只有「講者 1/2/3」、當初命名打錯字、當初
        跳過了命名。⚠️ **不重跑轉檔**——分群早在當初就做完了,md 裡有講者標籤與時間
        戳,這條路只改名字。"""
        box = ttk.Frame(parent, style="CardBody.TFrame")
        ttk.Label(box, text="要改哪一份逐字稿", style="CardH.TLabel").pack(anchor="w")
        self._wrap(ttk.Label(
            box, text="挑一份轉好的 .md。同一層有同名的錄音檔時,還可以試聽原音、"
                      "命名後記住聲紋;沒有的話就只改名字。",
            style="Hint.TLabel", justify="left")).pack(
                anchor="w", pady=(self.px(SP_XS), 0))
        row = ttk.Frame(box, style="CardBody.TFrame")
        row.pack(fill="x", pady=(self.px(SP_SM), self.px(SP_SM)))
        self._lockable(HandButton(row, text="選擇逐字稿…", style=skin.CTA_STYLE,
                                  command=self._relabel_pick)).pack(side="left")
        self._lockable(HandButton(row, text="清空", style="Small.TButton",
                                  command=lambda: self._relabel_set_path(""))).pack(
                                      side="left", padx=(self.px(SP_SM), 0))
        wrap = ttk.Frame(box, style="Sunken.TFrame", padding=self.px(SP_XS))
        wrap.pack(fill="x")
        self._relabel_src = tk.Text(wrap, font=(self.fam, 10), height=2,
                                    relief="flat", bd=0, highlightthickness=0,
                                    wrap="none", bg=self.pal["field"],
                                    fg=self.pal["ink"],
                                    insertbackground=self.pal["ink"],
                                    selectbackground=self.pal["row_sel"],
                                    padx=self.px(SP_SM), pady=self.px(SP_XS))
        self._lockable(self._relabel_src).pack(fill="x")
        return box

    def _relabel_actions(self, parent) -> ttk.Frame:
        """「重設講者」的動作列(卡片外面,同另外兩個模式)。"""
        bar = ttk.Frame(parent, style="Page.TFrame")
        self._relabel_btn = HandButton(bar, text="讀取並開始命名",
                                       style=skin.RUN_PAGE_STYLE,
                                       command=self._relabel_start)
        self._relabel_stop = HandButton(bar, text="停止", style=skin.STOP_PAGE_STYLE,
                                        state="disabled",
                                        command=self._run_stop_click)
        self._pair(bar, self._relabel_btn, self._relabel_stop)
        return bar

    def _relabel_pick(self) -> None:
        """「選擇逐字稿…」:原生對話框,只挑一份 md。"""
        picked = relabel.pick_md()
        if picked:
            self._relabel_set_path(picked)

    def _relabel_set_path(self, text: str) -> None:
        with unlocked(self._relabel_src):
            self._relabel_src.delete("1.0", "end")
            if text:
                self._relabel_src.insert("1.0", text)

    def _relabel_start(self) -> None:
        r"""按下「讀取並開始命名」。

        ⚠️ **清畫面要在把關之前**(同 `_run_start` 那條):被擋下來時畫面還掛著上一趟
        的「完成。」與亮著的「輸出資料夾…」,而那顆鈕會打開上一趟的資料夾。
        ⚠️ **分析失敗只是「少了試聽與聲紋」**,命名本身照樣做得到——為了它擋掉整個
        功能不划算(同網頁版 `_run_relabel` 的取捨)。"""
        self._stage("")
        self._run_bar.configure(value=0)
        self._run_open.configure(state="disabled")
        self._naming_hide()
        busy = self._busy_reason("run")
        if busy:
            self._stage(busy)
            return
        try:
            md_path = relabel.validate(self._relabel_src.get("1.0", "end-1c"))
        except UserFacingError as e:
            self._run_set_preview(str(e))
            return
        cancel.reset()
        self._run_lock(True)
        # ⚠️ **這條路的成品不在 `output`**:改寫的就是使用者自己資料夾裡的那份 md
        # (使用說明也是這樣寫的)。不設的話 `_run_dirs` 是 None = `output`,而
        # 「輸出資料夾…」就開到一個**沒有那份檔案**的資料夾去——比按了沒反應更糟。
        # ⚠️ 要設在 `_run_lock(True)` **之後**:它每一趟開頭把 `_run_dirs` 歸零。
        self._run_dirs = [str(md_path.parent)]
        self._stage(f"{md_path.name}:讀取中…")
        self._run_set_preview("")
        cores = self._run_cores.get()
        self._run_job(lambda say: self._relabel_work(md_path, say, cores),
                      self._relabel_done)

    def _relabel_work(self, md_path: Path, say, cores) -> None:
        r"""「重設講者」的主體(**工作執行緒**上;「重新分群」改寫完 md 之後也走這一支)。

        ⚠️ **有分群檔就從它算**(同網頁版 `_run_relabel`):聲紋與每一輪發言的相似度都
        直接從 npz 取,一個字節的音訊都不讀——否則一份 417 輪的逐字稿要把每一輪各抽
        一次聲紋(使用者 2026-08-18 回報「計算每段的相似度跑得較久」)。⚠️ **存在不等於
        用得動**:換過聲紋模型或格式舊了的檔要當成沒有,而且要把原因講出來——擺一顆
        按下去才報錯的鈕,比不擺更糟。
        ⚠️ **分析失敗只是「少了試聽與聲紋」**,命名本身照樣做得到——為了它擋掉整個
        功能不划算(同網頁版的取捨)。
        ⚠️ 有同名媒體檔時要抽聲紋(走 CPU),核心數在這條路上一樣有效(同網頁版)。"""
        pipeline.apply_worker_count(cores)
        md_text = relabel.read(md_path)
        transcript = relabel.parse(md_text)
        # 「未知」不佔講者編號:命名框那邊它是獨立的一塊,而且**絕不登記聲紋**
        # (多人零碎語音的混合,登記會污染聲紋庫)。改鍵成哨兵,`_naming_show`
        # 那一整套就原封不動地認得它。
        named = [n for n in transcript.order if n != "未知"]
        hints = {
            (UNKNOWN_SPEAKER if transcript.order[i] == "未知"
             else named.index(transcript.order[i])): h
            for i, h in transcript.hints().items()
        }
        media = relabel.find_media(md_path)
        # 分群檔在不在,決定第三種狀態——**而且要在分析之前讀**:有它的話聲紋與每段
        # 相似度都直接從裡面算(見 `naming._from_features`)。
        features = relabel.find_features(md_path)
        feat, feat_note, origin = None, "", ""
        if features is not None:
            try:
                feat = diarize.load_features(features)
                origin = ("自動判斷" if feat.num_speakers == 0
                          else f"當初指定 {feat.num_speakers} 位")
            except UserFacingError as e:
                features = None
                feat_note = f"\n{e}"
        # ⚠️ **不可以叫 `audit`**:模組層有一支同名的 `audit`(核對音檔那支),
        # 遮住之後 `audit.get(...)` 讀到的是這個 dict——而那一族要真的按下去
        # 才會發現,Tk 把 `AttributeError` 吞進紀錄檔、畫面上一個字都沒有。
        # `tests/test_desktop.py` 的靜態網守著。
        payload = naming_core._audit_payload_from_transcript(
            transcript, named, media)
        voiceprints: dict = {}
        clips: dict = {}
        if media is not None:
            try:
                voiceprints, clips, cohesion = \
                    naming_core._analyse_for_relabel(
                        md_path, media, transcript, named,
                        lambda f, desc="": say(
                            ("run_stage", (f"{md_path.name}:{desc}", f))),
                        blocks=payload.get("blocks"), feat=feat)
                for b, c in zip(payload.get("blocks") or [], cohesion):
                    b["cohesion"] = float(c)
            except Cancelled:
                raise
            except Exception:
                logger.exception("重設講者:媒體檔分析失敗,只提供命名")
        have_media = media is not None and bool(voiceprints)
        # 三種狀態各講一句(同網頁版;Tk 不渲染 Markdown,粗體記號拿掉)
        if not have_media:
            tail = ("同一層沒有同名的錄音檔——這次只能改名字:沒有試聽、"
                    "不會記住聲紋,也不能改人數。名字照樣寫得回逐字稿。")
        elif features is None:
            tail = (f"同一層找到「{media.name}」:可以試聽原音,命名後也會記住"
                    "聲紋、下次開會自動認人。"
                    + (feat_note or
                       f"\n沒有找到分群檔「{diarize.features_path(md_path).name}」,"
                       "所以這份改不了講者人數——分群檔是新版轉檔才會留下的,"
                       "舊的逐字稿沒有。要改人數只能重轉一次。"))
        else:
            tail = (f"同一層找到「{media.name}」與分群檔「{features.name}」:"
                    "可以試聽、命名後記住聲紋,而且可以直接改講者人數重新分群,"
                    "幾秒鐘就好、不必重轉。")
        note = (f"已讀取「{md_path.name}」,共 {len(named)} 位講者"
                + (f"(當初是{origin}分出來的)" if origin else "") + "。" + tail)
        say(("run_preview", f"{note}\n\n{md_text}"))
        # ⚠️ **這個 result 只是「命名區要的那幾個欄位」**,不是 `PipelineResult`:
        # 這條路沒有跑過管線,裝置、耗時、品質診斷通通不存在。
        # ⚠️ `clips` 一律給 dict(空的也給):它代表「已經剪過了」——沒有媒體檔時就
        # 沒有任何試聽鈕(同網頁版:有片段的講者才亮鈕),而不是留 None 讓命名區以為
        # 「還沒剪」、第一次按試聽再去剪一份 md。
        say(("named", dict(
            result=SimpleNamespace(
                outputs=[str(md_path)], speakers=len(named),
                speaker_hints=hints, voiceprints=voiceprints, quality=None),
            src=media, audit=payload, clips=clips)))

    def _relabel_done(self, error) -> None:
        """重設講者收尾。⚠️ **只解鎖、不碰命名區**:那是 `_job_say` 已經長好的。"""
        self._run_lock(False)
        self._run_bar.configure(value=0)
        if error is None:
            self._stage("已讀取,請在左邊替講者命名。")
        elif isinstance(error, Cancelled):
            self._stage("已依要求停止。")
        else:
            self._stage("")
            self._run_set_preview(
                error.args[0] if isinstance(error, UserFacingError)
                else "未預期的錯誤,詳見紀錄檔。")

    # ---- 現場收音 -------------------------------------------------------- #
    def _rec_build(self, parent) -> ttk.Frame:
        r"""「現場收音」模式在卡片裡的那一段:選收音情境。

        ⚠️ **三個情境錄的東西不同**,不是同一件事的三個名字:現場會議只錄麥克風;
        線上會議**麥克風與系統聲音分兩軌**(回音在文字層去重);只錄電腦聲音走
        WASAPI loopback。選錯了會錄到一場空(或只錄到自己那半)。
        ⚠️ **2026-09-01 從下拉選單改成 segmented**(照網頁版):三個選項藏在下拉裡,
        使用者要點開才知道有幾種——而選錯的代價是一整場會議。"""
        box = ttk.Frame(parent, style="CardBody.TFrame")
        ttk.Label(box, text="收音情境", style="CardH.TLabel").pack(anchor="w")
        self._scene_info = self._wrap(ttk.Label(box, style="Hint.TLabel",
                                                justify="left"))
        self._scene_info.pack(anchor="w", pady=(self.px(SP_XS), 0))
        # ⚠️ **每次啟動一律回到第一項**(使用者 2026-08-09 指定拿掉「記住上次選擇」,
        # 推翻 2026-07-21 的規格):那個記憶實際用了兩週就被要求拿掉。
        self._rec_scene = tk.StringVar(value=next(iter(SCENARIOS)))
        track, self._scene_cells = self._segmented(
            box, SCENARIO_OPTIONS, self._scene_show)
        track.pack(fill="x", pady=(self.px(SP_SM), 0))
        self._scene_show(SCENARIO_OPTIONS[0][0])
        return box

    def _scene_show(self, key: str) -> None:
        """切收音情境。⚠️ **錄音中不准切**:錄的東西當場就換了,而檔案已經在寫。"""
        if self._busy["rec"]:
            return
        self._rec_scene.set(key)
        self._segment_show(self._scene_cells, key)
        self._scene_info.configure(text=SCENARIO_INFO[key])

    def _rec_actions(self, parent) -> ttk.Frame:
        r"""「現場收音」的狀態列與雙鈕(卡片**外面**,同網頁版)。

        ⚠️ **錄音不能重來**——所以工作目錄在 `%LOCALAPPDATA%\meeting-scribe\
        recordings`,**絕不能放系統暫存的 `meeting-scribe-*` 前綴下**:啟動時的
        `cleanup_stale_temp` 會把那裡的殘留當孤兒掃掉。
        ⚠️ **錄音中不得睡眠**:螢幕關閉正是新式待命的進入條件,而進去之後行程會被
        整個凍結——那就是掉音訊。`power.stay_awake_begin()` 在開始時舉旗、收尾解除。"""
        bar = ttk.Frame(parent, style="Page.TFrame")
        self._rec_status = self._wrap(
            ttk.Label(bar, text=REC_IDLE, style="PageStatus.TLabel",
                      justify="left"))
        # ⚠️ 狀態列自己一列、跨滿兩欄:它是這兩顆鈕的說明,不是第三顆鈕。
        self._rec_status.grid(row=0, column=0, columnspan=2, sticky="w",
                              pady=(0, self.px(SP_SM)))
        self._rec_go = HandButton(bar, text="● 開始錄音", style=skin.RUN_PAGE_STYLE,
                                  command=self._rec_start)
        # ⚠️ 記號照網頁版用 `■`(文字符號、單色),不是 `⏹`:`⏹` 會被 `HandButton` 當
        # 圖示畫成彩色 emoji,而網頁版這兩顆主要動作鈕上的 ●／■ 是白色的文字符號。
        self._rec_end = HandButton(bar, text="■ 停止錄音並完成逐字稿",
                                   style=skin.STOP_PAGE_STYLE,
                                   state="disabled", command=self._rec_stop)
        self._rec_go.grid(row=1, column=0, sticky="ew",
                          padx=(0, self.px(CARD_GAP) // 2))
        self._rec_end.grid(row=1, column=1, sticky="ew",
                           padx=(self.px(CARD_GAP) // 2, 0))
        bar.columnconfigure(0, weight=1, uniform="run")
        bar.columnconfigure(1, weight=1, uniform="run")
        return bar

    def _fold(self, parent, title: str, on_toggle) -> "Fold":
        r"""一張可摺疊的卡:標題列 ＋ 右邊的 ▶／▼ 記號,點了就叫 `on_toggle`(通常是
        `_fold_toggle`)。內容自己往 `body` 裡放;卡片跟前一張之間留 `CARD_GAP`。

        轉檔頁與文件頁的「進階參數設定」共用(2026-09-03 文件頁照網頁版重排時抽出來的)。
        ⚠️ 整條標題列都能點(標題、記號、列本身),不是只有記號——網頁版的摺疊區也是。"""
        card = ttk.Frame(parent, style="Card.TFrame",
                         padding=(self.px(CARD_PAD), self.px(SP_LG)))
        card.pack(fill="x", pady=(self.px(CARD_GAP), 0))
        head = ttk.Frame(card, style="CardBody.TFrame")
        head.pack(fill="x")
        label = ttk.Label(head, text=title, style="CardH.TLabel")
        label.pack(side="left")
        mark = ttk.Label(head, text=ADV_CLOSED, style="Hint.TLabel")
        mark.pack(side="right")
        body = ttk.Frame(card, style="CardBody.TFrame")
        for w in (head, label, mark):
            w.bind("<Button-1>", lambda _e: on_toggle())
            w.configure(cursor="hand2")
        return Fold(card, body, mark)

    def _fold_toggle(self, fold: "Fold", cols: "TwoCols") -> None:
        """把摺疊卡展開或收起來;展開時把它所在的左欄捲到底,整張卡才露得出來。"""
        fold.open = not fold.open
        fold.mark.configure(text=ADV_OPEN if fold.open else ADV_CLOSED)
        if fold.open:
            fold.body.pack(fill="x", pady=(self.px(SP_LG), 0))
            # 展開之後把左欄捲到底,整張卡才露得出來(它是左欄最後一張;網頁版的摺疊區
            # 展開時也是往下長、使用者要往下捲)。⚠️ **先 `update_idletasks()` 再自己
            # 呼叫一次 refit**:內層的 `<Configure>` 要等這個 handler 結束才送到,而捲到
            # 底要的是**新的** scrollregion。
            self.update_idletasks()
            self._scroll_refit(cols)
            cols.canvas.yview_moveto(1.0)
        else:
            fold.body.pack_forget()

    def _adv_build(self, parent) -> ttk.Frame:
        r"""「進階參數設定」:平常收起來的那張卡(同網頁版的 `gr.Accordion`)。

        ⚠️ **模型的快速/精準選擇必須保留**(2026-07-26 曾移除、同日裁定還原),但它
        **平常不會改**——收起來之後左欄少一整段,而那是 2026-09-01 對齊網頁版時
        「一張大卡」放得下的原因之一。
        ⚠️ **摺疊區裡排成「模型｜CPU 核心數」兩欄、說明各一行**(2026-09-02 使用者選案
        C):直向排的話展開後左欄在 1280×820 差 133 實體 px 放不下——pack 分不到空間就
        把最後那張卡縮短,卡片的圓角照畫,看起來像那張卡本來就只有這麼長。出過三案實拍
        (搬到右欄 +71、對話框不受限、留在左欄兩欄式 +36),他選了位置不動的這一案。
        ⚠️ **代價是餘裕只有 36 實體 px**:任何一段說明多出一行(+22)就只剩 14,所以
        `MODEL_INFO`、`cores_info` 都是照半欄 ≈ 16 個中文字收的,**這張卡不要再加東西,
        要加就先量**。放不下時左欄會長出捲軸(`_two_columns`),那是安全網不是版面。"""
        self._adv = self._fold(parent, "進階參數設定", self._adv_toggle)
        card = self._adv.card

        # 兩欄等分、中間留 `CARD_GAP`(與卡片之間的距離同一把尺)。⚠️ **`uniform` 少不得**
        # (同 segmented 那條):沒有它只是平分「剩餘」空間,哪一欄的內容寬就哪一欄大。
        body = self._adv.body
        body.columnconfigure(0, weight=1, uniform="adv")
        body.columnconfigure(1, weight=1, uniform="adv")
        gap = self.px(CARD_GAP)
        col_model = ttk.Frame(body, style="CardBody.TFrame")
        col_model.grid(row=0, column=0, sticky="new", padx=(0, gap // 2))
        col_cores = ttk.Frame(body, style="CardBody.TFrame")
        col_cores.grid(row=0, column=1, sticky="new", padx=(gap - gap // 2, 0))

        # ---- 左:模型 ----
        ttk.Label(col_model, text="模型", style="CardH.TLabel").pack(anchor="w")
        self._wrap(ttk.Label(
            col_model, text=MODEL_INFO[transcribe.predicted_device() != "cpu"],
            style="Hint.TLabel", justify="left"), cols=2).pack(
                anchor="w", pady=(self.px(SP_XS), 0))
        self._run_model = tk.StringVar(
            value=MODEL_KEYS[transcribe.default_model_key()])
        track, self._model_cells = self._segmented(
            col_model, MODEL_OPTIONS, self._model_show)
        track.pack(fill="x", pady=(self.px(SP_SM), 0))
        self._segment_show(self._model_cells, self._run_model.get())

        # ---- 右:CPU 核心數 ----
        # (2026-09-02 使用者:「進階參數設定 CPU 核心數不見了,是 AP 沒辦法設定嗎?」
        # ——不是做不到,是先前沒接上。)⚠️ **它管的是三條路徑**:轉錄音檔、現場收音
        # (開始與收尾各套一次,同網頁版)、重設講者(有同名媒體檔時要抽聲紋,走 CPU)。
        # 值在按下「開始」那一刻讀、經 `pipeline.apply_worker_count` 套用;引擎的執行緒數
        # 在建構時就定死,所以是「下一趟才生效」——而那正是網頁版的行為。
        ttk.Label(col_cores, text="CPU 核心數", style="CardH.TLabel").pack(anchor="w")
        self._wrap(ttk.Label(
            col_cores, text=cores_info(), style="Hint.TLabel",
            justify="left"), cols=2).pack(anchor="w", pady=(self.px(SP_XS), 0))
        self._run_cores = tk.StringVar(value=str(power.default_worker_count()))
        # ⚠️ **不設 min/max**(同「講者人數」那條):超限一律在 `power.normalize_worker_count`
        # 夾,讓控制項自己擋會跳出英文錯誤(spec §8)。
        # ⚠️ **要留住這個 widget**:錄音／轉檔進行中它得停用(見 `_params_lock`)。模型
        # 那排點了本來就沒作用,而這一格**是真的會生效的**——收尾時才讀,所以錄音中改
        # 它會悄悄改掉這一場的收尾行為,那正是網頁版把它鎖起來的理由。
        self._cores_spin = ttk.Spinbox(
            col_cores, textvariable=self._run_cores, from_=1,
            to=power.max_cpu_cores(), style="Tall.TSpinbox")
        self._cores_spin.pack(fill="x", pady=(self.px(SP_SM), 0))
        return card

    def _lockable(self, widget):
        r"""登記一個「工作進行中不該能動」的元件,回傳它自己(方便接著 `.pack()`)。

        ⚠️ **分頁是「用到才建」的**,所以這份清單只能在各頁建起來的當下長出來,而且
        新建的那一頁要**立刻**吃到現在的狀態——否則「錄音錄到一半才第一次點開名單
        頁」那一次就是沒鎖的,而那正是最需要鎖的時候。
        ⚠️ **不准回頭去摸別頁的 widget**(CLAUDE.md 那條):這一支只收自己被建出來時
        傳進來的東西,誰都不必知道別的頁建了沒有。"""
        rest = str(widget.cget("state") or "normal")
        self._locked.append((widget, rest))
        if any(self._busy.values()):
            widget.configure(state="disabled")
        return widget

    def _data_lock(self) -> None:
        r"""依 `_busy` 的現況鎖住/放開三個資料分頁與選檔那幾樣東西。

        (使用者 2026-09-05 裁定:使用說明本來就寫著「錄音中/轉檔中,『進階參數
        設定』與『名單與聲紋』『領域詞表』『用詞替換表』分頁會鎖住」,而原生版與網頁
        版都只鎖了進階參數那幾格——他選擇補上行為,不改說明。)

        ⚠️ **理由不只是「說明這樣寫」**:詞表在**開始那一刻**就被引擎讀走了,轉檔中
        改它不會生效,而畫面上完全看不出這件事——改完存檔、看著轉出來的稿子還是舊
        的詞,只會判斷成「詞表沒有用」。
        ⚠️ **被銷毀的元件要跳過**:命名卡那類會整批重建,清單裡留著的是死掉的參照。"""
        busy = any(self._busy.values())
        alive: list[tuple[tk.Misc, str]] = []
        for widget, rest in self._locked:
            try:
                widget.configure(state="disabled" if busy else rest)
            except tk.TclError:         # 已經被銷毀,從清單裡淘汰掉
                continue
            alive.append((widget, rest))
        self._locked = alive

    def _params_lock(self) -> None:
        r"""依 `_busy` 的現況鎖住/放開「進階參數設定」裡的控制項。

        (2026-09-03 使用者:「現場錄音時,進階參數設定應該要 DISABLE」。網頁版本來就
        這樣做:`app._param_updates`,鎖模型、CPU 核心數、包含子資料夾,而**講者人數
        在錄音中是唯一的例外**。)

        ⚠️ **兩個控制項的病不一樣,不要只修看得到的那個**:模型那排點了本來就沒作用
        (`_model_show` 開頭擋著 `_busy`),缺的只是看得出來;而 **CPU 核心數是真的會
        生效的**——它在收尾時才讀(`_rec_stop` 那行 `self._run_cores.get()`),錄音中
        改它會悄悄改掉這一場的收尾行為,而使用者以為自己什麼都沒改。
        ⚠️ **講者人數不鎖**(使用者 2026-07-24 對網頁版指定,這裡照抄):它同樣在按
        「停止並轉檔」那一刻才讀,而**開會中數清人數、停止前才填**是正當用法。檔案
        轉檔沒有這個例外——那條路的人數在按「開始」當下就定案,中途改不生效,開著
        只會誤導,所以那時一起鎖。
        ⚠️ **一律從 `_busy` 算,不要在各個呼叫端各記一次**:錄音與收尾是**兩個**旗標
        接力(`rec` 放掉的同一刻 `run` 才亮),各記一次就會在交棒的瞬間放開一拍。
        ⚠️ **「包含子資料夾」不在這裡**:它住在 `_file_box`,切到收音模式時整段收起來
        ——網頁版要鎖它是因為那邊一直看得到。"""
        busy = self._busy["rec"] or self._busy["run"]
        self._segment_enable(self._model_cells, not busy)
        # 「要做什麼」與「收音情境」:`_mode_show` / `_scene_show` 開頭本來就擋著
        # `_busy`,這裡補的是**看得出來**那一半(同 `_segment_enable` 的 docstring)。
        # ⚠️ 兩組都在轉檔頁裡建,而這一支只從 `_run_lock` 走得到——那時頁一定在。
        self._segment_enable(self._mode_cells, not busy)
        self._segment_enable(self._scene_cells, not busy)
        self._cores_spin.configure(state="disabled" if busy else "normal")
        # 講者人數:只有「錄音中」放行,收尾(`run`)與檔案轉檔一起鎖
        self._speakers_spin.configure(
            state="disabled" if self._busy["run"] else "normal")

    def _adv_toggle(self) -> None:
        """把轉檔頁的「進階參數設定」展開或收起來(見 `_fold_toggle`)。"""
        self._fold_toggle(self._adv, self._cols["run"])

    def _model_show(self, key: str) -> None:
        """切模型。⚠️ **轉檔中不准切**:引擎的參數在開始那一刻就定死了。"""
        if self._busy["rec"] or self._busy["run"]:
            return
        self._run_model.set(key)
        self._segment_show(self._model_cells, key)

    def _rec_tick(self) -> None:
        """錄音中每秒更新一次:計時、背景轉錄進度,以及即時逐字稿。

        ⚠️ 沒有這個的話,畫面上看不出它還在錄。
        ⚠️ **按過「停止並轉檔」就不要再跳**(2026-09-04;網頁版 `_lock_for_rec_finish`
        的第一件事正是 `gr.Timer(active=False)`):`_rec` 裡的 recorder 要到
        `_rec_done` 才清,不擋的話這一行會在整個收尾期間把 `REC_FINISHING` 蓋回
        「● 錄音中・已錄 …」,而且數字繼續加——收音早就停了。"""
        if not self._rec.get("recorder") or self._rec_t1:
            return
        secs = int(time.monotonic() - self._rec_t0)
        live = self._rec.get("live")
        done = live.transcribed_until() if live is not None else 0.0
        tail = (f"背景轉錄:已完成至 {int(done) // 60:02d}:{int(done) % 60:02d}"
                if done > 0 else "背景轉錄暖機中…")
        self._rec_status.configure(
            text=f"● 錄音中({self._rec.get('scene')})・已錄 "
                 f"{secs // 60:02d}:{secs % 60:02d}。{tail}")
        if live is not None:
            self._rec_live_preview(live)
        self.after(1000, self._rec_tick)

    def _rec_live_preview(self, live) -> None:
        r"""錄音中的即時逐字稿(2026-08-30 從網頁版 `app._rec_tick` 移植)。

        ⚠️ **使用者回報「錄音中沒有逐字稿預覽」就是缺這一段**:原生版先前只更新計時,
        狀態列還寫著「轉出來的字會在停止之後才出現」——而背景其實一直在轉,只是沒有
        人把它畫出來。
        ⚠️ **段數沒變就不要重畫**:每秒重寫整個 Text 會把使用者的捲動位置與選取狀態
        洗掉,而長會議每秒都在洗。
        ⚠️ **逐段快取繁化結果、只轉新增的那幾段**(同網頁版):整份重轉是每秒一次的
        OpenCC 全文轉換,而且錄音中途改 `replace.txt` 也不該回頭重寫已經轉過的段落。
        ⚠️ **這裡不標講者**:講者要等收尾的分群才知道,預覽只求「看得到內容在長」。"""
        # 局部 import:本檔刻意不在模組層背這些(見檔頭)
        from meeting_scribe import convert
        snap = live.snapshot()
        if len(snap) == self._live_n:
            return
        lines = []
        for seg in snap:
            key = (seg.start, seg.end, seg.text)
            if key not in self._live_conv:
                self._live_conv[key] = convert.to_taiwan_traditional(seg.text)
            lines.append(f"[{pipeline.mmss(seg.start)}] {self._live_conv[key]}")
        self._live_n = len(snap)
        self._run_set_preview(LIVE_PREVIEW_HEAD + "\n".join(lines))

    def _rec_start(self) -> None:
        r"""按下「開始錄音」。

        ⚠️ **裝置缺失要當場講**(沒有麥克風、沒有播放裝置),**絕不錄一場空**。
        ⚠️ **`cancel.reset()` 要在這裡做**:上一批檔案轉檔按過的停止不清掉的話,
        錄音中的增量講者切分一開工就自我了斷(而且只在紀錄檔裡留一行)。"""
        if self._rec.get("recorder"):
            return
        busy = self._busy_reason("rec")
        if busy:
            self._rec_status.configure(text=busy)
            return
        cancel.reset()
        # ⚠️ 開始收音時就套(同網頁版 `_start_recording`):邊錄邊轉的增量轉錄與講者
        # 切分從這一刻起就在用執行緒,不在這裡套就要等到收尾才生效
        pipeline.apply_worker_count(self._run_cores.get())
        scene_label = self._rec_scene.get()
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        rec_dir = appdata_root() / "recordings" / stamp
        stem = f"錄音_{stamp}"
        try:
            recorder = record.Recorder(rec_dir, SCENARIOS[scene_label], stem=stem)
            recorder.start()
        except UserFacingError as e:
            self._rec_status.configure(text=str(e))
            return
        except Exception as e:
            logger.exception("開始錄音失敗")
            self._rec_status.configure(
                text="無法開始錄音:發生未預期的錯誤,詳情見紀錄檔(logs 資料夾)。")
            return
        # ⚠️ **增量轉錄起不來也要收乾淨再回報**:`LiveTranscriber` 一 `__init__` 就開
        # 了暫存目錄與鎖檔,`start()` 又會拉起講者分析子行程。
        # ⚠️ 這一段先前**完全沒有把關**(上面那個 `Recorder` 有、它沒有):炸掉就是
        # traceback 進紀錄檔、畫面上一個字都沒有,而收音其實已經開始了——使用者會
        # 對著一個看起來沒反應的畫面講完一整場會議(同「靜默的失敗」那一族)。
        live = live_scribe.LiveTranscriber(
            model_key=MODEL_LABELS[self._run_model.get()])
        try:
            for kind, path in recorder.track_files().items():
                live.add_track(kind, path)
            live.start()
        except Exception:
            logger.exception("錄音中的增量轉錄啟動失敗")
            live.close()
            try:
                recorder.stop()       # 收音已經開始了,不收就是一直錄下去
            except Exception:
                logger.debug("收拾失敗的錄音時出錯", exc_info=True)
            self._rec_status.configure(
                text="無法開始錄音:發生未預期的錯誤,詳情見紀錄檔(logs 資料夾)。")
            return
        power.stay_awake_begin()      # ⚠️ 錄音中不得睡眠(收尾時解除)
        self._rec.update(recorder=recorder, live=live, dir=rec_dir, stem=stem,
                         scene=scene_label)
        self._busy["rec"] = True
        # ⚠️ **錄音是唯一不經 `_run_lock` 的工作**,所以它的跑馬燈要自己點起來(清掉
        # 的那一側則共用:按下「停止並轉檔」會 `_run_lock(True)`,收尾結束時
        # `_rec_done` 的 `_run_lock(False)` 一起把它熄掉)。⚠️ **一定是跑馬燈**:
        # 散會時間不是這支程式算得出來的東西。
        self._taskbar(None)
        self._params_lock()           # 錄音中鎖進階參數(講者人數例外,見那一支)
        self._data_lock()             # 三個資料分頁與選檔那幾樣(見 `_data_lock`)
        # ⚠️ **每一場都要歸零**:不歸零的話這一場的預覽會接在上一場的字後面,
        # 而 `_live_n` 還停在上一場的段數 → 新的段落根本畫不出來。
        self._live_n = -1
        self._live_conv.clear()
        self._run_set_preview("")
        self._rec_t0 = time.monotonic()
        self._rec_t1 = 0.0           # ⚠️ 不歸零的話這一場的計時器一開始就是停的
        self._rec_go.configure(state="disabled")
        self._rec_end.configure(state="normal")
        self._naming_hide()
        self._rec_tick()

    def _rec_stop(self) -> None:
        r"""按下「停止並轉檔」:停止收音,然後把尾巴算完。

        ⚠️ **收尾是最慢的一段**(講者分析的積壓要在這裡補完),而在此之前畫面
        一行字都沒有的話,使用者無從判斷是還在算、還是卡住了——2026-08-04 那場
        64 分鐘的錄音停止後 40 幾分鐘一片空白,就是這個。"""
        if not self._rec.get("recorder"):
            return
        self._rec_end.configure(state="disabled")
        self._rec_status.configure(text="正在停止收音…")
        self.update_idletasks()      # `stop()` 會擋住主執行緒(補零/修剪),先把這句畫出來
        tracks = self._rec["recorder"].stop()
        # ⚠️ **收音停止的時刻要記下來**:計時器靠它停(見 `_rec_tick`),關視窗時要問
        # 的「錄了多久」也靠它——收尾期間拿現在的時間去減開錄時刻會越報越長。
        self._rec_t1 = time.monotonic()
        self._rec_status.configure(text=REC_FINISHING)
        live, out_dir, stem = (self._rec["live"], OUTPUT_DIR, self._rec["stem"])
        rec_dir = self._rec.get("dir")
        speakers = self._normalize_speakers(self._run_speakers.get())
        cores = self._run_cores.get()
        self._run_lock(True)
        self._run_bar.configure(value=0)

        def work(say) -> None:
            try:
                if not tracks or max((t.duration for t in tracks), default=0.0) < 2.0:
                    raise UserFacingError("錄音太短(不到 2 秒),沒有可轉的內容")
                # 收尾再套一次(同網頁版 `_finish_recording`):錄音中使用者可能改過
                pipeline.apply_worker_count(cores)
                result = live_scribe.run_live_finish(
                    tracks, live, out_dir, stem, num_speakers=speakers,
                    on_stage=lambda stage, frac: say(("run_stage", (stage, frac))),
                )
                wavs = [Path(p) for p in result.outputs
                        if str(p).lower().endswith(".wav")]
                # 首行講錄音檔存在哪(同網頁版 `_finish_recording`),**不講執行裝置**:
                # 收尾不是一次跑完整條管線,`run_live_finish` 給的 device 是空字串——
                # 先前照抄檔案轉檔那句,畫面上就是「執行裝置:・」(2026-09-02 使用者
                # 截圖對照網頁版抓到的)。
                preview = (f"(錄音檔已存於 output 資料夾:"
                           f"{'、'.join(p.name for p in wavs)})\n\n{result.preview}")
                if not pending.anyone_to_name(result.speakers,
                                              result.speaker_hints or {}):
                    preview = f"{NO_SPEECH_NOTE}\n\n{preview}"
                say(("run_preview", preview))
                # 試聽剪**對應的音軌**(`speaker_sources`:線上會議的現場講者剪麥克風軌、
                # 遠端講者剪系統軌,剪錯軌只會聽到回音版或無聲)。⚠️ **要在刪錄音工作目錄
                # 之前剪**——軌檔就住在那裡。先前一律從合成後的立體聲檔剪、而且是第一次
                # 按試聽才剪,雙軌合成失敗那條(`stem.wav` 根本不存在)就整批靜默失敗。
                clips = (naming_core._cut_speaker_clips(
                             Path(tracks[0].path), result.speaker_hints or {},
                             sources=result.speaker_sources)
                         if result.speaker_hints else {})
                # 核對的來源只能用 output\ 裡的成品音檔:`speaker_sources` 指的是錄音
                # 工作目錄裡的軌檔,而那個目錄底下幾行就整個刪掉了(同網頁版)。
                audit_src = wavs[0] if wavs else None
                say(("named", dict(
                    result=result, src=audit_src,
                    audit=(naming_core._audit_payload(result, audit_src, sources={})
                           if audit_src else {}),
                    clips=clips)))
                # 音檔已進 output/、試聽片段已剪出:錄音工作目錄功成身退(同網頁版)。放
                # 最後:前面任何一步炸掉都還留著原始素材。⚠️ **先前這裡沒刪**——每一場
                # 都把整場的原始 wav 留在 `recordings` 底下(2026-09-02 查到八場),而
                # 啟動時的 `cleanup_stale_temp` 刻意不掃那裡(錄音不能重來)。
                if rec_dir is not None:
                    shutil.rmtree(rec_dir, ignore_errors=True)
            finally:
                # ⚠️ **收尾走哪條路都要 close**(成功、報錯、按停止都算):它收的
                # 是講者分析**子行程**、暫存目錄與那個鎖檔。漏收的症狀有三層,而
                # 且沒有一層會出聲——① 子行程一直活著(下一場錄音會與它併用同一
                # 批模型檔、白佔 CPU,理由見 `diarproc.close` 的檔頭);②
                # `TemporaryDirectory` 的 finalizer 要等 GC 才跑,那時鎖檔還開著、
                # Windows 不准刪,只在紀錄檔留一行 `Exception ignored …
                # PermissionError: [WinError 32]`;③ 於是那個
                # `meeting-scribe-live-*` 目錄整場都留著(要等下次啟動的
                # `cleanup_stale_temp` 才掃得掉——那時鎖檔的持有者已經不在了)。
                # ⚠️ `close()` 刻意做成冪等,放 `finally` 兜底不怕重複呼叫。
                live.close()

        self._run_job(work, self._rec_done)

    def _rec_done(self, error) -> None:
        """收尾結束(成功、報錯、停止都走這裡),所以防睡眠一定會解除。"""
        power.stay_awake_end()
        flag = self._work_outcome(error, naming=True)
        self._rec.clear()
        self._busy["rec"] = False
        self._rec_go.configure(state="normal")
        self._rec_end.configure(state="disabled")
        self._run_lock(False, flag)
        self._run_bar.configure(value=0)
        if error is None:
            self._rec_status.configure(text="收尾完成,逐字稿在下面。")
            self._stage("完成。")
            self._run_open.configure(state="normal")
        elif isinstance(error, UserFacingError):
            # 自家的錯誤(錄音太短之類)照原話講;原始音檔沒有被刪
            self._rec_status.configure(
                text=f"{error}。原始音檔留在 recordings 資料夾。")
        else:
            logger.exception("錄音收尾失敗", exc_info=error)
            self._rec_status.configure(
                text="收尾時出錯,詳情見紀錄檔(logs 資料夾)。"
                     "⚠ 原始音檔還在,可以用「轉錄音檔」重轉一次。")
        winui.flash_taskbar(self)       # 理由見 `_run_done` 最後那一行

    # ---- 命名區(轉完之後才出現)------------------------------------------ #
    def _naming_ensure(self) -> None:
        """命名卡的內容用到才建(見 `_run_page`);建過就不再建。"""
        if not self._naming_ready:
            self._naming_ready = True          # ⚠️ 先立旗標,建到一半出錯也不重進
            self._naming_build(self._naming_box)

    def _naming_build(self, box: ttk.Frame) -> ttk.Frame:
        r"""把替講者取名字那張卡的內容填進 `box`。轉檔前整張收起來、不佔位。

        ⚠️ **不是在這裡建卡片本身**(2026-09-04):卡片是 `_run_page` 開窗時就建好的
        空殼,這一支只填內容、而且只在第一次要顯示時被叫到(`_naming_ensure`)。

        ⚠️ **結構照網頁版**(使用者 2026-09-02:「UI 的樣式要比照 WEB」),由上而下:
        標題與說明 → 「改成幾位講者」(只在同層有分群檔時出現,見 `_naming_show`)→
        每位講者一個區塊(欄位名、認人線索、滿寬的下拉,右側疊著「▶ 試聽」「🔍 核對」)
        → 「未知」那一塊 → 「套用名字到逐字稿」與「跳過命名」兩顆鈕(3:1)。
        ⚠️ **名字用下拉不是自由輸入**:打錯一個字就是聲紋庫裡多一個人,而那個人
        下次會被當成新面孔(選單同時收「與會名單」與**只在聲紋庫裡的**那些人,
        見 `naming.._all_names`)。
        ⚠️ **留白就維持「講者 N」**,不強迫命名——沒認出來的人硬填會把聲紋存到
        別人名下,而那個錯誤看不見還會自我複製。
        ⚠️ **是一張卡,不是一塊區域**(2026-08-30 兩欄化):它是左欄最上面那張卡,用
        無內距的 `CardBody.TFrame` 會讓它貼著視窗邊、跟底下的卡對不齊。"""
        ttk.Label(box, text=NAMING_TITLE, style="CardH.TLabel").pack(anchor="w")
        self._wrap(ttk.Label(box, text=NAMING_HINT, style="Hint.TLabel",
                             justify="left")).pack(
            anchor="w", pady=(self.px(SP_SM), self.px(SP_LG)))
        # 「改成幾位講者」＋「🔀 重新分群」(網頁版 2026-08-18 使用者選定的 B 案:標題、
        # 說明、輸入列各一行,最後一條分隔線)。⚠️ **平常收起來**,只在這份逐字稿同層
        # 真的有分群檔時才 pack(第三種狀態,見 `_naming_show`)。
        rc = ttk.Frame(box, style="CardBody.TFrame")
        self._recluster_box = rc
        ttk.Label(rc, text="改成幾位講者", style="Field.TLabel").pack(anchor="w")
        self._wrap(ttk.Label(rc, text=RECLUSTER_HINT, style="Hint.TLabel",
                             justify="left")).pack(anchor="w", pady=(self.px(SP_XS), 0))
        row = ttk.Frame(rc, style="CardBody.TFrame")
        row.pack(fill="x", pady=(self.px(SP_SM), 0))
        row.columnconfigure(0, weight=1)
        # 鈕那一欄與底下命名列的鈕欄**同一個寬度、同一道縫**(`_btn_col_w`;網頁版的
        # 註解寫著「輸入列的幾何照抄命名列」):輸入框的右緣才會與下拉齊,而「重新分群」
        # 撐滿 `NAME_BTN_COL`、與「試聽」「核對」一樣長(使用者 2026-09-03)。
        row.columnconfigure(1, weight=0, minsize=self._btn_col_w())
        # ⚠️ **不設 min/max 擋輸入**(同「講者人數」那條):範圍在 `_naming_recluster`
        # 那一側夾,讓控制項自己擋會跳出英文訊息;`from_=2` 只管上下箭頭的起點。
        self._recluster_n = tk.StringVar(value="2")
        ttk.Spinbox(row, textvariable=self._recluster_n, from_=2, to=MAX_SPEAKERS,
                    style="Tall.TSpinbox").grid(row=0, column=0, sticky="ew")
        self._recluster_btn = HandButton(row, text="🔀 重新分群", style="Small.TButton",
                                         command=self._naming_recluster)
        self._recluster_btn.grid(row=0, column=1, sticky="ew", padx=(self.px(SP_MD), 0))
        tk.Frame(rc, bg=self.pal["line_off"], height=max(1, self.px(1))).pack(
            fill="x", pady=(self.px(SP_LG), 0))
        # 每位講者一個區塊,全部放進**同一個** grid(欄位只在同一個 grid 裡才跨列對齊,
        # 2026-08-30 使用者拿網頁版對照過)。第 0 欄是欄位名／線索／下拉,第 1 欄是右側
        # 那兩顆鈕——⚠️ **第 1 欄一律佔位**(`minsize`,照網頁版 `.name-btns` 的
        # `min_width=100`):有沒有鈕都佔,下拉的右邊界才會每一塊都一樣;而且**只有第 0
        # 欄給 weight**,被壓縮的一定是下拉(它有捲動、縮了還能用),鈕永遠完整。
        self._naming_rows = ttk.Frame(box, style="CardBody.TFrame")
        self._naming_rows.pack(fill="x")
        self._naming_rows.columnconfigure(0, weight=1)
        # ⚠️ 欄寬 = 鈕的寬度 + 與下拉之間那道縫(`_btn_col_w`):鈕要**撐滿** `NAME_BTN_COL`
        # (2026-09-03 使用者:「試聽與核對按鈕的長度,請與重新分群按鈕一樣長」),縫另外算,
        # 縫算在欄寬裡的話鈕就只剩 88。「重新分群」那一列用同一個數(`_recluster_box`)。
        self._naming_rows.columnconfigure(1, weight=0, minsize=self._btn_col_w())
        # 套用／跳過同一列、3:1(照網頁版 `.apply-row` 的 scale=3 / scale=1)。
        # ⚠️ **兩顆要同高**:「跳過命名」是一般按鈕的灰底、但要主要動作鈕的高度,所以
        # 有自己的一張皮(`skin.SKIP_STYLE`);拿 `Small.TButton` 就是一高一矮(網頁版
        # 2026-07-24 使用者截圖回報過同一件事)。
        foot = ttk.Frame(box, style="CardBody.TFrame")
        foot.pack(fill="x", pady=(self.px(SP_XL), 0))
        self._naming_foot = foot
        foot.columnconfigure(0, weight=3, uniform="apply")
        foot.columnconfigure(1, weight=1, uniform="apply")
        self._apply_btn = HandButton(foot, text="套用名字到逐字稿", style=skin.RUN_STYLE,
                                     command=self._naming_apply)
        self._apply_btn.grid(row=0, column=0, sticky="ew", padx=(0, self.px(SP_SM)))
        self._skip_btn = HandButton(foot, text="跳過命名", style=skin.SKIP_STYLE,
                                    command=self._naming_skip)
        self._skip_btn.grid(row=0, column=1, sticky="ew")
        # 底下那一行狀態(重複的名字、剪不出片段之類)。⚠️ **沒話講就整行收起來**
        # (見 `_naming_say`):空的 Label 照樣佔一行高,而網頁版這裡什麼都沒有。
        self._naming_status = self._wrap(
            ttk.Label(box, style="Status.TLabel", justify="left"))
        # ⚠️ 核對表那張視窗的狀態**不在這裡**:它跟著空卡一起在 `_run_page` 就建好了
        # (`_naming_hide` 會叫 `_audit_close()`,而那條路在這一支跑過之前就走得到)。
        return box

    def _naming_say(self, text: str) -> None:
        """命名卡底下那一行狀態。⚠️ 空字串 = 整行收起來(空的 Label 照樣佔一行高)。"""
        self._naming_status.configure(text=text)
        if text:
            self._naming_status.pack(anchor="w", pady=(self.px(SP_SM), 0),
                                     after=self._naming_foot)
        else:
            self._naming_status.pack_forget()

    def _naming_show(self, result, src, audit=None, clips=None, names=None,
                     audit_flags=None, restored=False) -> None:
        r"""轉完一檔:把命名卡長出來(每位講者一個區塊)。

        四條路都走這裡(同網頁版 `_present_result` 的地位):檔案轉檔、現場收音、
        重設講者／重新分群、開頁還原。
        ⚠️ **`audit` / `clips` 是給現場收音與「重設講者」那兩條路的**:核對資料與試聽
        片段在工作執行緒就算好了(現場收音要趕在刪錄音目錄之前剪;重設講者本來就已經
        讀了整份音訊)。`clips` 給 dict 就是「已經剪過了」——有片段的講者才亮試聽鈕
        (同網頁版);給 None 才是「第一次按試聽再整批剪」(檔案轉檔那條路)。
        ⚠️ **`names` / `audit_flags` / `restored` 是給開頁還原的**:草稿名字要蓋過自動
        辨識的預填、核對鈕要亮在轉完當下亮的那幾顆(不是重算),而且**不再落地一次**。
        ⚠️ **一位講者都沒有時整張卡不出現**(`pending.anyone_to_name`;使用者 2026-08-15
        錄了一段沒有人聲的電腦聲音踩到的):長一張空卡、寫「辨識到 0 位講者」再給一顆
        套用鈕,比什麼都不長更糟。那一段的話由呼叫端寫進預覽(`NO_SPEECH_NOTE`)。"""
        self._naming_stop()
        # ⚠️ **卡片的內容在這裡才建**(第一次而已,見 `_naming_ensure`):底下每一行都
        # 碰得到卡片裡的東西,所以要排在最前面——連「一位講者都沒有就收起來」那條
        # 提早 return 的路也在它後面(那條會叫 `_naming_hide`,而它自己擋得住沒建過)。
        self._naming_ensure()
        for child in self._naming_rows.winfo_children():
            child.destroy()
        self._naming_vars = {}
        self._naming_btns = {}
        self._naming_audit_btns = {}
        self._naming_clues = {}
        self._naming_next_row = 0
        count = int(getattr(result, "speakers", 0) or 0)
        hints = result.speaker_hints or {}
        if not pending.anyone_to_name(count, hints):
            self._naming_hide()
            return
        self._naming_clips = clips
        self._naming_src = Path(src) if src else None
        # 核對要的東西(每一輪發言 ＋ 從哪個音檔剪)。⚠️ **沒有 blocks 就是空的**
        # ——那時每一塊不長「🔍 核對」,而不是長一顆按了會說「沒東西」的鈕。
        self._naming_audit = (dict(audit) if audit is not None
                              else naming_core._audit_payload(result, src))
        self._audit_close()
        self._naming_result = (result, src)
        known = naming_core._all_names()
        # ⚠️ **整場一起辨識**,不逐位各自 recognize:同一個名字只能給一位講者,
        # 否則兩群拿到同一個名字,而那在成品裡看起來是「少了一個人」不是「認錯
        # 人」——使用者看不到任何異狀,自然也不會去改(見
        # `voiceprints.recognize_batch`)。`rivals` 是聲紋分不開的候選,並列在
        # 線索裡、**不寫分數**(0.86 對 0.85 會讓那 0.01 看起來像一種依據)。
        # ⚠️ **「🔍 核對」只給該核對的那幾位,不是每一位**(同網頁版;2026-08-15 使用者
        # 截圖抓到過反過來的版本)。`flags` 合併兩個來源、缺一不可:**群內一致性最低**
        # 的那幾位(這一群是不是混了人,來自檔尾診斷的 `export.check_first`)與**聲紋
        # 分不開**最難的前三位(這一群到底是誰)。兩個判準問的是不同問題,實測名單幾乎
        # 不重疊。⚠️ **有核對表才算數**:沒有 blocks 時整批不亮。
        has_audit = bool(self._naming_audit)
        seed = (set(audit_flags) if audit_flags is not None
                else naming_core._audit_flags(getattr(result, "quality", None)))
        guesses, rivals, flags = naming_core._naming_clues(
            count, result.voiceprints or {}, seed, has_audit)
        prefill = dict(guesses) if names is None else dict(names)
        # 「改成幾位講者」:只在同層真的有分群檔時出現(第三種狀態,同網頁版
        # `_naming_page_updates`)。⚠️ **人數欄預設填「現在幾位」,不是猜一個建議值**
        # (使用者 2026-08-18 選定:兩種標準做法都推算不出人數,欄位就該顯示事實)。
        features = naming_core._features_for(list(result.outputs or ()))
        self._recluster_features = features
        if features is not None:
            self._recluster_n.set(str(count))
            self._recluster_box.pack(fill="x", pady=(0, self.px(SP_LG)),
                                     before=self._naming_rows)
        else:
            self._recluster_box.pack_forget()
        for spk in range(count):
            # ⚠️ **顯示名稱走 `export.speaker_label`**(全 repo 唯一那一份):它是
            # **1-based**(講者 0 顯示成「講者 1」),而 md 裡寫的也是那個。
            mine = rivals.get(spk) or []
            choices, n_rivals = naming_core.rival_order(known, mine)
            self._naming_block(
                spk, f"{export.speaker_label(spk)} 的名字",
                naming_core._hint_text(hints.get(spk), mine),
                prefill.get(spk, ""), choices, n_rivals,
                play=(clips is None or spk in clips),
                audit_btn=has_audit and spk in flags)
        # 「未知」那一塊(逐字稿有未知段落才有;同網頁版 `_name_section_updates`):
        # 有核對可用時**只留核對鈕**——給整批未知一個名字正是檔尾診斷勸阻的事(它常是
        # 多人混合),而試聽只播最長一句、混合群裡那一句是誰的都不知道(使用者 2026-08-13
        # 指定,理由是他自己的經驗:「未知我常聽,裡面通常都混著好幾個人」);沒有核對可
        # 用時保留命名框,那時它是唯一能處理未知的路。⚠️ 那個框只改字、**絕不登記聲紋**。
        unknown = hints.get(UNKNOWN_SPEAKER)
        if unknown is not None:
            if has_audit:
                self._naming_block(
                    UNKNOWN_SPEAKER, export.speaker_label(UNKNOWN_SPEAKER),
                    naming_core._hint_text(unknown), None, [], 0,
                    play=False, audit_btn=True)
            else:
                self._naming_block(
                    UNKNOWN_SPEAKER,
                    f"「{export.speaker_label(UNKNOWN_SPEAKER)}」的名字",
                    f"{UNKNOWN_NO_ENROLL}・{naming_core._hint_text(unknown)}",
                    prefill.get(UNKNOWN_SPEAKER, ""), known, 0,
                    play=(clips is None or UNKNOWN_SPEAKER in clips), audit_btn=False)
        self._naming_say("")
        # 命名中左欄**只剩這張卡**(`_naming_focus` 把大卡、動作列、摺疊卡全收了),所以
        # 直接 pack 就是最上面,不必 `before=`——而且不能用:重新分群那條路會在命名中再進
        # 來一次,那時其他卡都沒有 pack,對它們說 `before=` 是 TclError。
        self._naming_box.pack(fill="x", pady=(0, self.px(CARD_GAP)))
        self._naming_focus(True)
        if not restored:
            self._naming_persist(result, hints, prefill, flags, rivals)

    def _naming_block(self, spk: int, label: str, clue, value, choices, n_rivals: int,
                      play: bool, audit_btn: bool) -> None:
        r"""命名卡裡的一個區塊(一位講者,或「未知」)。

        第 0 欄由上而下三列:欄位名、認人線索、滿寬的下拉;第 1 欄是疊著的兩顆鈕,
        `rowspan` 跨整個區塊、垂直置中(照網頁版 `.name-row` ＋ `.name-btns`)。
        `value=None` 表示這一塊不給命名框(有核對可用時的「未知」)。
        ⚠️ **線索的換行寬度要扣掉右邊那一欄**(`minus`):它跟鈕同一列,照整張卡的寬度
        換行會把鈕擠出卡片右緣——而那正是 2026-08-30 修過的「鈕被壓成一顆灰點」。
        ⚠️ **填好名字的那一塊收起線索**(使用者 2026-08-18 選定,同網頁版):線索是拿來
        「認出這是誰」的,認完就只剩佔位——實測每列 84px,十位講者填到第八位時畫面有
        672px 是用不到的東西。清空名字就放回來,所以是收合不是丟掉(`_naming_changed`)。"""
        rows = self._naming_rows
        r = self._naming_next_row
        # 區塊之間一條灰線(使用者 2026-09-02 拿網頁版對照:「每一位講者間應該要有一條
        # 灰線做區隔」),跨兩欄、與「重新分群」底下那條同款;第一塊上面沒有。
        if r > 0:
            tk.Frame(rows, bg=self.pal["line_off"], height=max(1, self.px(1))).grid(
                row=r, column=0, columnspan=2, sticky="ew", pady=(self.px(SP_LG), 0))
            r += 1
        start = r
        top = 0 if start == 0 else self.px(SP_LG)
        ttk.Label(rows, text=label, style="Field.TLabel").grid(
            row=r, column=0, sticky="w", pady=(top, 0))
        r += 1
        if clue:
            lab = self._wrap(ttk.Label(rows, text=clue, style="Hint.TLabel",
                                       justify="left"),
                             minus=self._btn_col_w())
            lab.grid(row=r, column=0, sticky="w", pady=(self.px(SP_XS), 0))
            self._naming_clues[spk] = lab
            r += 1
        if value is not None:
            var = tk.StringVar(value=value)
            self._naming_vars[spk] = var
            combo = ttk.Combobox(rows, textvariable=var, values=choices, width=8,
                                 style="Tall.TCombobox")
            combo.grid(row=r, column=0, sticky="ew", pady=(self.px(SP_SM), 0))
            self._combo_setup(combo)          # 點整條就展開、展開後可打字、清單圓角
            # 聲紋分不開的那幾位:候選排到選單最前面、標成琥珀底(同網頁版的 3 案,
            # 使用者 2026-08-15 選定)。認得出來的人 `n_rivals` 是 0,這一塊就與先前一樣。
            self._mark_rivals(combo, n_rivals)
            var.trace_add("write", lambda *_a, s=spk: self._naming_changed(s))
            if value.strip() and spk in self._naming_clues:
                self._naming_clues[spk].grid_remove()
            r += 1
        if play or audit_btn:
            # ⚠️ `sticky="ew"` 是鈕的寬度的來源:小框撐滿欄寬扣掉左邊那道縫 = 剛好
            # `NAME_BTN_COL`,兩顆鈕再 `fill="x"` 撐滿小框——與「重新分群」同寬(網頁版
            # `.name-btns` 的 `min_width=100` 也是這個數)。垂直不拉,鈕維持在區塊正中。
            col = ttk.Frame(rows, style="CardBody.TFrame")
            col.grid(row=start, column=1, rowspan=r - start, sticky="ew",
                     padx=(self.px(SP_MD), 0), pady=(top, 0))
            if play:
                btn = HandButton(col, text=f"{naming_core._ROW_PLAY} 試聽",
                                 style="Small.TButton",
                                 command=lambda s=spk: self._naming_play(s))
                btn.pack(fill="x")
                self._naming_btns[spk] = btn
            if audit_btn:
                btn = HandButton(col, text="🔍 核對", style="Small.TButton",
                                 command=lambda s=spk: self._audit_open(s))
                btn.pack(fill="x", pady=(self.px(SP_XS) if play else 0, 0))
                self._naming_audit_btns[spk] = btn
        self._naming_next_row = r

    def _naming_changed(self, spk: int) -> None:
        """某一格的名字變了:線索收合／放回,草稿名字寫進落地的命名進度(打字即存)。"""
        var = self._naming_vars.get(spk)
        lab = self._naming_clues.get(spk)
        if var is not None and lab is not None and lab.winfo_exists():
            if var.get().strip():
                lab.grid_remove()
            else:
                lab.grid()
        # 草稿延後一拍再寫:每敲一個字就重寫一次 JSON 沒有意義,`after` 把連續的輸入
        # 合併成一次(同網頁版掛在 `.input` 上的 `_save_draft_names`)。
        if self._naming_draft_after is not None:
            self.after_cancel(self._naming_draft_after)
        self._naming_draft_after = self.after(300, self._naming_draft_save)

    def _naming_draft_save(self) -> None:
        self._naming_draft_after = None
        if self._naming_result is None:
            return
        pending.update_names({spk: var.get().strip()
                              for spk, var in self._naming_vars.items()})

    def _naming_persist(self, result, hints, prefill, flags, rivals) -> None:
        r"""把命名要的一切落地(同網頁版 `_present_result` 裡那一段):關視窗、當機、
        隔天再開,命名卡都接得回來,不必重轉音檔(使用者選定 2026-07-18)。

        ⚠️ **落地的核對資料要含「哪幾顆鈕該亮」與候選聯集**:還原時亮的鈕要跟轉完當下
        一模一樣,否則使用者會以為自己記錯了。⚠️ 試聽片段還沒剪(檔案轉檔是第一次按
        試聽才剪)時落地的是空的——還原後從音檔重剪一次,音檔路徑跟著核對資料一起落地
        (`audit["src"]`)。⚠️ 落地是輔助功能,`pending.persist` 任何失敗只記 log。"""
        audit_disk = dict(self._naming_audit)
        if audit_disk:
            audit_disk["flags"] = sorted(flags)
            audit_disk["rivals"] = naming_core._rival_pool(rivals)
        stored = pending.persist(
            [str(p) for p in result.outputs or ()], self._preview_md,
            int(result.speakers or 0), result.voiceprints or {}, hints,
            self._naming_clips or {}, prefill, audit=audit_disk)
        if self._naming_clips is not None:
            # 改指落地副本(同網頁版):暫存副本會被下次啟動清掉,落地副本活到套用完成
            self._naming_clips = stored

    def _naming_restore(self) -> None:
        r"""開頁(建轉檔頁時)還原未完成的命名(同網頁版 `_restore_pending`):關視窗、
        當機、隔天再開,命名卡直接接回來,不必重轉音檔(使用者選定 2026-07-18)。

        ⚠️ **候選不落地、在這裡重算**(`_naming_show` 本來就會算):落地的話,中途改過
        名單或聲紋庫之後開起來,會看到一份與現況對不上的舊候選。核對鈕的旗標則用落地
        那份——轉完當下亮哪幾顆,重開後就要是哪幾顆。
        ⚠️ **音檔來源跟著核對資料走**(`audit["src"]`);沒有它就退回成品裡的錄音檔
        (現場收音)或同層同名的媒體檔(重設講者),都沒有就只能改名字、不能試聽。
        ⚠️ `pending` 與網頁版是**同一份**(`%LOCALAPPDATA%` 共用),兩邊落地的格式相同、
        互相接得回來;也因此兩邊不要同時開(CLAUDE.md)。"""
        data = pending.load()
        if data is None:
            return
        audit_saved = data.get("audit") or {}
        outputs = list(data["outputs"])
        if audit_saved.get("src"):
            src = Path(audit_saved["src"])
        else:
            wav = next((p for p in outputs if p.lower().endswith(".wav")), None)
            md = next((p for p in outputs if p.lower().endswith(".md")), None)
            src = (Path(wav) if wav
                   else relabel.find_media(Path(md)) if md else None)
        self._run_set_preview(f"{RESTORED_NOTE}\n\n{data['preview']}")
        self._run_open.configure(state="normal")
        self._naming_show(
            SimpleNamespace(outputs=outputs, speakers=data["count"],
                            speaker_hints=data["hints"],
                            voiceprints=data["voiceprints"], quality=None),
            src, audit=audit_saved, clips=data["clips"] or None,
            names=data["names"], audit_flags=audit_saved.get("flags") or (),
            restored=True)

    def _naming_focus(self, naming: bool) -> None:
        r"""命名進行中,把「開始下一份工作」那整組收起來(使用者 2026-08-08 選定 A 案,
        同網頁版 `_naming_focus`)。

        收的是左欄那張大卡(要做什麼／收音情境或選檔／講者人數)、卡外的動作列,**連
        「進階參數設定」也收**——網頁版也是這樣(`SOURCE_SWITCH_SPEC` 裡有 `model`、
        `recursive`),第一版漏收,使用者 2026-09-02 看截圖指出:「設定講者時已經錄音完成,
        進階參數本來也沒有設定的意義」。理由與「轉檔中鎖住那整組」同一個:工作沒收工就
        不開新的——套用或跳過就是收工,那時整組放回來。
        ⚠️ **放回來時要照原本的順序擺**:大卡 → 動作列 → 摺疊卡。這時命名卡已經收掉、
        左欄是空的,所以直接依序 pack 到尾端就是對的順序,不必 `before=`。
        ⚠️ **兩個方向都要 `_scroll_top`**:左欄是可捲的,而這一支換掉的是它的**全部**
        內容——不重算 scrollregion、不捲回頂端的話,視野會停在上一批內容的偏移上。
        使用者 2026-09-04 回報的「設定完講者,左邊的功能選單不見了」就是這個(病因與
        實測見 `_scroll_top`)。"""
        bars = {"rec": self._rec_bar, "file": self._file_bar,
                "relabel": self._relabel_bar}
        if naming:
            self._run_card.pack_forget()
            for bar in bars.values():
                bar.pack_forget()
            self._adv_card.pack_forget()
            self._scroll_top("run")
            return
        self._run_card.pack(fill="x")
        bars[self._mode].pack(fill="x", pady=(self.px(CARD_GAP), 0))
        self._adv_card.pack(fill="x", pady=(self.px(CARD_GAP), 0))
        self._scroll_top("run")

    def _naming_hide(self) -> None:
        r"""命名卡收起來。

        ⚠️ **這條路在命名卡建起來之前就走得到**(整頁復位、開始下一份工作都會叫它),
        所以卡片內部的東西要問過 `_naming_ready` 才碰——空殼的 `pack_forget` 本身
        永遠是安全的。"""
        self._pick_close()
        self._naming_stop()
        self._audit_close()
        self._naming_box.pack_forget()
        if self._naming_ready:
            self._recluster_box.pack_forget()
        self._naming_result = None
        self._recluster_features = None
        self._naming_focus(False)

    def _naming_finish(self, note: str) -> None:
        r"""收工(套用或跳過都走這裡;同網頁版 `_page_reset_view` ＋ `_end_of_job_view`):
        清掉落地的命名進度、命名卡收起、「開始下一份工作」那組放回來、路徑欄清空、
        講者人數歸零(使用者 2026-07-24:收工沒歸零,上一場填的人數會殘留到下一場、
        被拿去強制分群)、預覽只留一段指路。

        ⚠️ **預覽不留舊內容**(網頁版的理由,使用者回報過:「畫面留著舊內容會讓人誤以為
        還沒套用完」);成品在哪一句話講清楚,右上角那顆「輸出資料夾…」照亮。"""
        pending.clear()
        # ⚠️ **工作列那個黃的到這裡才熄**:它問的是「你回來把名字填完了嗎」,而套用與
        # 跳過都算填完了(這是那顆黃色唯一的收工點——別的收場沒有人會回來「收掉」它,
        # 所以只留到下一趟按開始為止,見 `_taskbar_end`)。
        self._taskbar_end()
        self._naming_hide()
        self._run_set_preview(note)
        self._run_set_path("")
        self._relabel_set_path("")
        self._run_speakers.set("0")
        self._stage("")
        if not self._busy["rec"]:
            self._rec_status.configure(text=REC_IDLE)

    def _naming_skip(self) -> None:
        """「跳過命名」(使用者指定 2026-07-24:有時不想改名):成品**不**刪,只清畫面
        與落地的命名進度;預覽留一段指路,不讓使用者以為檔案不見了。"""
        if self._naming_result is None:
            return
        self._naming_finish(SKIPPED_NOTE)

    def _naming_recluster(self) -> None:
        r"""「🔀 重新分群」:拿分群檔重算 → 改寫 md 的講者標籤 → 回到命名流程(同網頁版
        `_check_recluster` ＋ `_run_recluster`)。

        **不重轉、也不重讀音訊**:貴的那一段(切分＋抽聲紋)轉檔當下就存下來了,這裡
        只做重聚——實測 0.3 秒。之後照「重設講者」那條路重建命名卡(`_relabel_work`),
        試聽、聲紋登記、核對整套共用。
        ⚠️ **把關在動手之前、而且擋下來時畫面原封不動**(網頁版 2026-08-18 使用者回報
        「每次還要按 F5」的教訓):名字都還在、卡片也還在,只在底下多一句話。
        ⚠️ **比對的是「試算出來的位數」,不是使用者填的數字**(2026-08-19 實機:填 13~17
        全都得到 10 位):md 的段落是原子的,新分出來的一兩秒碎群拿不到任何段落,就不會
        出現在逐字稿上——拿填的數字去比,那五次全部放行,每一次都白跑一趟。
        ⚠️ **重分群等於推翻上一次的命名,所以一律重新命名**(使用者 2026-08-18 指定):
        講者編號整個換過了,舊名字對應的那一位已經不存在。走 `_relabel_work` 天然就是
        這個行為。⚠️ 上一輪已經登記進聲紋庫的樣本**不會自動撤銷**——那要使用者自己去
        聲紋健檢看;程式沒有立場判斷哪一次的命名才是對的(使用說明裡寫著)。"""
        if self._naming_result is None:
            return
        busy = self._busy_reason("run")
        if busy:
            self._naming_say(busy)
            return
        result, _src = self._naming_result
        features = self._recluster_features
        md_path = next((Path(p) for p in result.outputs
                        if str(p).lower().endswith(".md")), None)
        if features is None or md_path is None:
            self._naming_say("找不到這份逐字稿的分群檔,不能改人數。")
            return
        n = self._normalize_speakers(self._recluster_n.get())
        if n <= 1:
            # 0 是自動偵測——而自動判斷正是要改掉的那個結果;1 則是把整份逐字稿
            # 都掛到同一位名下,那不是「分講者」而是「取消分講者」
            self._naming_say("請填 2 以上的講者人數。0 是自動偵測(那正是你要改掉的"
                             "結果);1 等於把整份逐字稿都算成同一個人,不必經過重新分群。")
            return
        try:
            md_text = relabel.read(md_path)
            now = len([x for x in relabel.parse(md_text).order if x != "未知"])
            feat = diarize.load_features(features)
            turns, _vps, quality = diarize.recluster(feat, n)
        except UserFacingError as e:
            self._naming_say(str(e))
            return
        if relabel.count_after_recluster(md_text, turns) == now:
            if n == now:
                self._naming_say(f"這份逐字稿現在就是 {now} 位講者,不必重新分群。"
                                 "要改成別的位數再按一次。")
            else:
                self._naming_say(
                    f"填 {n} 位,在這份逐字稿上分出來還是 {now} 位——跟現在一樣,所以"
                    "沒有重跑,已經填好的名字都還在。逐字稿的段落是當初那次分群切出來"
                    "的,一個段落只能歸一位;多分出來的都是一兩秒的碎段,會被併回鄰近"
                    "的講者。要再分出更多人只能重轉一次。")
            return
        md_path.write_text(relabel.recluster_md(md_text, turns, quality),
                           encoding="utf-8")
        logger.info("重新分群:%s → %d 位講者", md_path.name, n)
        # 之後就是「重設講者」那條路:從改寫後的 md 重建命名卡(有分群檔,所以聲紋與
        # 相似度都從 npz 算、一個字節的音訊都不讀)。
        cancel.reset()
        self._run_lock(True)
        self._stage(f"{md_path.name}:重新分群…")
        cores = self._run_cores.get()
        self._run_job(lambda say: self._relabel_work(md_path, say, cores),
                      self._relabel_done)

    def _naming_play(self, spk: int) -> None:
        r"""試聽某一位講者最長的那一句。

        ⚠️ **聽到的就是看到的那一句**(與線索裡的摘錄同一句):兩邊不一致的話,
        使用者會以為自己聽錯了。片段是從**原始檔**剪的——管線的暫存 wav 已經清掉,
        而兩者時間軸一致(ffmpeg 轉檔不平移時間)。
        ⚠️ **不要播放器介面**(使用者 2026-07-18 指定):按試聽即從頭播、**再按一次
        停止**、按另一位**直接切換**。所以這顆鈕是切換鈕,不是「播放」。
        ⚠️ 剪片段要花幾秒(ffmpeg),所以丟到工作執行緒,不然視窗會凍住;而**整批
        一次剪完**(那支本來就是整批做的),第二次按別人就不必再等。"""
        if self._naming_result is None:
            return
        if self._playing == spk:                 # 再按一次 = 停
            self._naming_stop()
            return
        self._naming_stop()                      # 按別人 = 直接切換
        if self._naming_clips is not None:
            self._naming_sound(spk)
            return
        result, _src = self._naming_result
        src = self._naming_src
        if src is None:
            self._naming_say("這一段沒有可以試聽的片段。")
            return
        self._naming_say("正在剪試聽片段…")

        def work(say) -> None:
            say(("clips",
                 naming_core._cut_speaker_clips(src, result.speaker_hints or {})))

        self._run_job(work, lambda err: self._naming_cut_done(spk, err))

    def _naming_cut_done(self, spk: int, error) -> None:
        if error is not None:
            logger.exception("試聽片段剪不出來", exc_info=error)
            self._naming_clips = {}
            self._naming_say("這一段剪不出來,可以改看摘錄認人。")
            return
        self._naming_say("")
        self._naming_sound(spk)

    def _naming_sound(self, spk) -> None:
        r"""真的把那一段放出來。

        ⚠️ **用 `winsound` 而不是自己接播放器**:片段是 wav、只放幾秒,而
        `PlaySound` 是 Windows 內建的、非同步、可隨時 purge 掉——接一個播放函式庫
        只為了播一句話並不划算(本專案本來就是 Windows 專用的)。
        ⚠️ **播完要自己把鈕的字換回來**:`PlaySound` 不會通知播完了,所以照那一句的
        長度排一個 `after`——⚠️ 排程要記下來,切換時取消掉,否則前一句的排程會把新
        那顆鈕的字提早改回去。"""
        path = (self._naming_clips or {}).get(spk)
        if not path:
            self._naming_say("這一段沒有可以試聽的片段。")
            return
        try:
            import winsound

            winsound.PlaySound(str(path), winsound.SND_FILENAME
                               | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        except Exception:
            logger.exception("試聽播不出來")
            self._naming_say("這一段播不出來,可以改看摘錄認人。")
            return
        self._playing = spk
        btn = self._naming_btns.get(spk)
        if btn is not None:
            btn.configure(text="⏹ 停止")
        if isinstance(spk, tuple) and spk[0] == "audit":
            # 核對表的一列:播放中那一格換成 ■,而且**排程要照那一輪的長度**——先前這裡
            # 對核對列拿不到 hint 就退回 5 秒,40 秒的一輪放到第 5.3 秒就被 `_naming_stop`
            # 的 purge 切掉(2026-09-02 換成視窗時才看出來)。
            self._audit_paint_play(spk[1])
            secs = (self._audit_rows[spk[1]].seconds
                    if spk[1] < len(self._audit_rows) else 5.0)
        else:
            hint = (self._naming_result[0].speaker_hints or {}).get(spk)
            secs = (hint[3] - hint[2]) if hint else 5.0
        self._naming_after = self.after(int(max(secs, 0.5) * 1000) + 300, self._naming_stop)

    # ---- 🔍 核對:整群一次聽完 -------------------------------------------- #
    def _audit_open(self, spk: int) -> None:
        r"""打開某一位講者的核對表:他這一場的每一輪發言,逐列可播。

        ⚠️ **這顆鈕存在的理由是「▶ 試聽」回答不了那個問題**:試聽只放**一段**
        (該標籤最長的那一句),回答的是「這聽起來像誰」;它回答不了「**這一群裡面
        是不是混了別人**」。2026-08-12 資訊月會實跡:某個標籤的 38 段裡有 13 段共
        20 秒分屬另外七個人,而試聽播的那 14.1 秒**真的是本人**——聽完它任何人都會
        確認「就是他」然後命名。
        ⚠️ **單位是「一輪發言」不是講者分離的原始區段**:md 的每個區塊是整段跑過
        標點模型的產物,區塊內部的句界已經不存在。而與前後鄰居不同講者的插話本來
        就自成一個區塊,所以要查的那些都查得到。
        ⚠️ **不抽樣、只限列數**(使用者 2026-08-13 指定改成逐列播放):逐列播放一次
        只聽一段,限制長度反而是把東西藏起來。"""
        payload = self._naming_audit or {}
        blocks = [SpeechBlock(**b) if not isinstance(b, SpeechBlock) else b
                  for b in payload.get("blocks", ())]
        mine = audit.blocks_of(blocks, spk)
        if not mine:
            self._naming_say("這一位沒有可以核對的段落。")
            return
        src = (payload.get("sources") or {}).get(str(spk)) or payload.get("src") or ""
        try:
            audit.ensure_wav16k(Path(src), naming_core._audit_dir())
        except UserFacingError as e:
            self._naming_say(str(e))
            return
        picks = audit.plan(mine, cap_sec=float("inf"), max_rows=AUDIT_MAX_ROWS)
        self._audit_rows = audit.rows_for(mine, picks)
        # ⚠️ **`AuditRow.index` 是「在 picks 裡的第幾個」**,不是「在該講者所有區塊
        # 裡的序號」——列數超過上限而抽樣過時,兩者對不上,拿它去索引 `mine` 會改掛
        # 到**別輪發言**。所以自己把 picks 留著。
        self._audit_picked_blocks = [mine[i] for i in picks]
        self._audit_spk = spk
        self._audit_src = src
        self._audit_fill()

    def _audit_window(self) -> tk.Toplevel:
        r"""核對用的獨立視窗(2026-09-02 使用者選案 C):建一次、換講者時重用、關閉就 destroy。

        ⚠️ **為什麼另開視窗**(使用者原話):面板長在命名卡最底下,「有時候講者很多位,
        不論是下移或是上移都不方便操作」——網頁版放在右欄也得把畫面捲上去。獨立視窗讓
        命名卡不捲不動,拖到哪裡都行(含第二個螢幕)。⚠️ **三案實拍後選的是表格式**:另外
        兩案(浮動小視窗、貼齊右欄)一位講者二、三十輪時都得在視窗內捲,改掛那一列會跟著
        捲走;表格的表頭固定、清單自己捲、改掛列永遠看得到(`docs/dev/native-ui.md`)。
        ⚠️ **位置放在右欄上方**(網頁版核對表的位置:蓋住預覽框、關掉就回來),只在建視窗
        時擺一次——換講者不再搬,使用者拖過就留在他放的地方。主視窗還沒 map(測試)或右欄
        太小時退回 `AUDIT_WIN_MIN_*`。
        ⚠️ `transient`:跟著主視窗最小化、不在工作列另外佔一格;X 與 Esc 都是「關閉」
        (= 先前的「收起來」),**必須走 `_audit_close`** 才會把播放停掉、狀態清掉。"""
        win = self._audit_win
        if win is not None and win.winfo_exists():
            return win
        win = tk.Toplevel(self)
        win.configure(background=self.pal["page"])
        win.transient(self)
        win.protocol("WM_DELETE_WINDOW", self._audit_close)
        win.bind("<Escape>", lambda _e: self._audit_close())
        self._audit_body = ttk.Frame(win, style="Card.TFrame", padding=self.px(CARD_PAD))
        self._audit_body.pack(fill="both", expand=True,
                              padx=self.px(SP_LG), pady=self.px(SP_LG))
        w = h = x = y = 0
        cols = self._cols.get("run")
        right = cols.right if cols is not None else None
        if right is not None and right.winfo_exists():
            w, h = right.winfo_width(), right.winfo_height()
            x, y = right.winfo_rootx(), right.winfo_rooty()
        w = max(w, self.px(AUDIT_WIN_MIN_W))
        h = max(h, self.px(AUDIT_WIN_MIN_H))
        win.geometry(f"{w}x{h}+{max(x, 0)}+{max(y, 0)}")
        # 說明文字的換行寬度:視窗寬扣掉邊距與卡片內距(視窗還沒 map,量不到,所以用算的)
        self._audit_inner = w - self.px(SP_LG) * 2 - self.px(CARD_PAD) * 2
        self._audit_win = win
        return win

    def _audit_fill(self) -> None:
        r"""把核對視窗畫出來:表格一列一輪發言(勾選、▶、時間、長度、摘錄),底下是改掛列。

        ⚠️ **勾選與播放都是「點那一格」**:Treeview 沒有可嵌的按鈕,第一欄畫 ☐/☑、第二欄
        畫 ▶/■,`<Button-1>` 靠 `identify_column` 分辨點到哪一格;鍵盤上下之後空白鍵勾選、
        Enter 播放。⚠️ 勾選的真值仍是 `BooleanVar`(`_audit_picks`,同先前的面板),
        `_audit_reassign` 與測試都照那份讀;格子只是它的投影(trace)。
        ⚠️ 換講者時**整張重畫**(同一個視窗),不留上一位的勾選與播放。"""
        win = self._audit_window()
        self._naming_stop()
        card = self._audit_body
        for child in card.winfo_children():
            child.destroy()
        label = export.speaker_label(self._audit_spk)
        title = f"核對「{label}」的 {len(self._audit_rows)} 輪發言"
        win.title(title)
        head = ttk.Frame(card, style="CardBody.TFrame")
        head.pack(fill="x")
        ttk.Label(head, text=title, style="CardH.TLabel").pack(side="left")
        HandButton(head, text="關閉", style="Small.TButton",
                   command=self._audit_close).pack(side="right")
        ttk.Label(card, text=AUDIT_HINT, style="Hint.TLabel", justify="left",
                  wraplength=self._audit_inner).pack(
                      anchor="w", pady=(self.px(SP_XS), self.px(SP_SM)))
        wrap = ttk.Frame(card, style="Sunken.TFrame", padding=self.px(SP_XS))
        wrap.pack(fill="both", expand=True)
        tree = ttk.Treeview(wrap, columns=AUDIT_COLS, show="headings",
                            style="Audit.Treeview", selectmode="browse", height=8)
        self._audit_tree = tree
        for col, head_text, width, anchor in AUDIT_COL_SPEC:
            tree.heading(col, text=head_text)
            if width:
                tree.column(col, width=self.px(width), minwidth=self.px(width),
                            anchor=anchor, stretch=False)
            else:
                tree.column(col, anchor=anchor)
        self._audit_picks = {}
        tree.tag_configure("odd", background=(AUDIT_ZEBRA_DARK if self._dark()
                                              else AUDIT_ZEBRA_LIGHT))
        for i, row in enumerate(self._audit_rows):
            mins, secs = divmod(int(row.start), 60)
            tree.insert("", "end", iid=str(i), tags=("odd",) if i % 2 else (),
                        values=(AUDIT_UNPICKED, naming_core._ROW_PLAY,
                                f"{mins:02d}:{secs:02d}", f"{row.seconds:.0f} 秒", row.text))
            var = tk.BooleanVar(value=False)
            var.trace_add("write", lambda *_a, n=i: self._audit_paint_pick(n))
            self._audit_picks[i] = var
        tree.bind("<Button-1>", self._audit_click)
        tree.bind("<space>", lambda _e: self._audit_key("pick"))
        tree.bind("<Return>", lambda _e: self._audit_key("play"))
        sb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)

        # 改掛:勾起來的那幾輪其實不是這一位。⚠️ **名單只排序不限縮**——插話的人
        # 常常只講一兩句、根本沒有自成一群,限縮的話他們只能靠打字,而打錯一個字
        # 就是聲紋庫裡多一個人(見 `naming.._reassign_choices`)。
        # ⚠️ 傳的是**照講者編號排**的名字清單(那支照位置對講者),「未知」那一格不在裡面。
        move = ttk.Frame(card, style="CardBody.TFrame")
        move.pack(fill="x", pady=(self.px(SP_MD), 0))
        self._audit_move_label = ttk.Label(move, text="把勾選的改掛給", style="Hint.TLabel")
        self._audit_move_label.pack(side="left")
        self._audit_to = tk.StringVar(value="")
        count = int(getattr(self._naming_result[0], "speakers", 0) or 0)
        names, n_head = naming_core.reassign_order(
            self._audit_spk,
            [self._naming_vars[i].get() if i in self._naming_vars else ""
             for i in range(count)],
            self._naming_audit)
        to = ttk.Combobox(move, textvariable=self._audit_to, values=names, width=18,
                          style="Tall.TCombobox")
        to.pack(side="left", padx=(self.px(SP_SM), 0))
        self._combo_setup(to)
        # 這場會議裡的人(已填好名字的、聲紋候選)排最前面**並塗琥珀底**,同命名欄那個下拉
        # (使用者 2026-09-02:「相同的邏輯,將可能的放最前面,並標示黃色」)。筆數與順序
        # 出自同一次計算(`reassign_order`),否則底色會落在別人身上。
        self._mark_rivals(to, n_head)
        self._audit_to_combo = to
        HandButton(move, text="改掛", style=skin.CTA_STYLE,
                   command=self._audit_reassign).pack(
                       side="left", padx=(self.px(SP_SM), 0))
        # 視窗自己的狀態行(勾都沒勾就按改掛、片段剪不出來):寫在命名卡底下的話,
        # 使用者正看著這個視窗,根本看不到。⚠️ 沒話講就整行收起來(`_audit_say`)。
        self._audit_status = ttk.Label(card, style="Status.TLabel", justify="left",
                                       wraplength=self._audit_inner)
        win.deiconify()
        win.lift()
        tree.focus_set()

    def _audit_click(self, event) -> None:
        """表格上點了一下:第一格切換勾選、第二格播放,其他格交給 Treeview 自己(選列)。"""
        tree = self._audit_tree
        if tree is None or tree.identify("region", event.x, event.y) != "cell":
            return
        iid = tree.identify_row(event.y)
        if not iid:
            return
        col = tree.identify_column(event.x)
        if col == "#1":
            var = self._audit_picks[int(iid)]
            var.set(not var.get())
        elif col == "#2":
            self._audit_play(int(iid))

    def _audit_key(self, what: str) -> None:
        """鍵盤:選中的那一列,空白鍵勾選、Enter 播放。"""
        tree = self._audit_tree
        sel = tree.selection() if tree is not None else ()
        if not sel:
            return
        i = int(sel[0])
        if what == "pick":
            var = self._audit_picks[i]
            var.set(not var.get())
        else:
            self._audit_play(i)

    def _audit_paint_pick(self, i: int) -> None:
        """把某一列的勾選狀態畫到第一格,改掛列的字順便報「勾了幾輪」。"""
        tree = self._audit_tree
        if tree is None or not tree.winfo_exists() or not tree.exists(str(i)):
            return
        tree.set(str(i), "pick", AUDIT_PICKED if self._audit_picks[i].get() else AUDIT_UNPICKED)
        n = sum(1 for v in self._audit_picks.values() if v.get())
        lab = getattr(self, "_audit_move_label", None)
        if lab is not None and lab.winfo_exists():
            lab.configure(text=f"把勾選的 {n} 輪改掛給" if n else "把勾選的改掛給")

    def _audit_paint_play(self, playing: int | None) -> None:
        """播放中那一列的 ▶ 換成 ■(`None` = 全部換回 ▶);與試聽鈕的字同一個狀態機。"""
        tree = self._audit_tree
        if tree is None or not tree.winfo_exists():
            return
        for iid in tree.get_children(""):
            on = playing is not None and int(iid) == playing
            tree.set(iid, "play", AUDIT_STOP if on else naming_core._ROW_PLAY)

    def _audit_say(self, text: str) -> None:
        """核對視窗底下那一行(視窗不在時退回命名卡的狀態行)。空字串 = 整行收起來。"""
        lab = getattr(self, "_audit_status", None)
        if lab is None or not lab.winfo_exists():
            self._naming_say(text)
            return
        lab.configure(text=text)
        if text:
            lab.pack(anchor="w", pady=(self.px(SP_SM), 0))
        else:
            lab.pack_forget()

    def _audit_reassign(self) -> None:
        r"""把勾選的那幾輪改掛給另一位。

        ⚠️ **靠講者行的時間戳定位**,不是行號(`audit.reassign` 做的):命名套用、
        繁化、標點都可能動過內文,唯一穩定的錨是 `**名字** (00:12:34)` 那一行。
        ⚠️ **一定要跟著 `note_reassigned`**:`reassign` 刻意不動檔尾的診斷表(那張
        表的意義是「機器分成這樣」),而不加註的話,出貨的 md 會寫著「講者 3 有 38
        輪」、內文卻只剩 25 輪——這份 md 的既定消費者是 RAG,等於餵進一段自我矛盾
        的診斷。2026-08-15 的 code review 抓到那半根本不存在。
        ⚠️ **加註不是改數字**:改成人工修正後的樣子,下游就再也看不出哪些是機器
        判的、哪些是人改的。
        ⚠️ **記下「這一群被改掛過」**(同網頁版 `_audit_apply`):改掛這個動作本身就是
        使用者親口說「這一群不只一個人」,而那是**他給的證據**,不是工具的猜測。套用
        名字時據此跳過聲紋登記(見 `_naming_apply`)——先前原生版沒記,不純的群照樣
        登記,聲紋庫就把別人的聲音學進這個人名下。
        ⚠️ **預覽也要跟著改**(同網頁版):右邊還寫著舊講者,使用者會以為改掛沒生效。"""
        name = self._audit_to.get().strip()
        picked = [i for i, var in self._audit_picks.items() if var.get()]
        if not name or not picked:
            self._audit_say("請先勾選要改掛的那幾輪,並選一個要改掛給誰。")
            return
        chosen = [self._audit_picked_blocks[i] for i in picked]
        # 加註用的是**顯示名稱**(「講者 3」／「未知」),不是內部編號:先前這裡傳的是
        # 0-based 的整數,出貨的 md 就寫著「「2」的 13 段已改掛給…」。
        label = export.speaker_label(self._audit_spk)
        moved_total = 0
        for path in [Path(p) for p in (self._naming_result[0].outputs or ())]:
            if path.suffix.lower() != ".md":
                continue
            text = path.read_text(encoding="utf-8")
            text, moved = audit.reassign(text, chosen, name)
            if moved:
                text = audit.note_reassigned(text, [label], name, moved)
                path.write_text(text, encoding="utf-8")
                moved_total += moved
        if not moved_total:
            # 改不到就要出聲:錨定的是 md 的講者行格式,改不到多半是格式對不上——
            # 那是工具的 bug,不是使用者的(同 `_naming_apply` 的同一條規矩)
            logger.warning("改掛沒有改到任何一行(%d 段、名字「%s」)", len(chosen), name)
            self._naming_say("沒有改到任何一行,請把紀錄檔提供給維護者。")
            self._audit_close()
            return
        done = set(int(i) for i in self._naming_audit.get("reassigned") or ())
        done.add(int(self._audit_spk))
        self._naming_audit["reassigned"] = sorted(done)
        preview, n = audit.reassign(self._preview_md, chosen, name)
        if n:
            self._run_set_preview(audit.note_reassigned(preview, [label], name, n))
        self._naming_say(f"已把 {moved_total} 輪發言改掛給「{name}」。"
                         "⚠ 這一位這次不會被登記進聲紋庫——你改掛過,表示這一群不只一個人。")
        self._audit_close()

    def _audit_close(self) -> None:
        """關閉核對視窗(關閉鈕、X、Esc、改掛完、收起命名卡都走這裡):停播、清狀態、destroy。"""
        self._naming_stop()                  # 表格還在,播放中那一格才換得回 ▶
        win = self._audit_win
        self._audit_win = None
        self._audit_tree = None
        self._audit_body = None
        self._audit_rows = []
        self._audit_picks = {}
        if win is not None and win.winfo_exists():
            win.destroy()

    def _audit_play(self, index: int) -> None:
        r"""播核對表上的某一列。

        ⚠️ **從管線留下的 16k wav 剪**:從原始 m4a/mp4 剪要先整檔解碼(長錄音數十
        秒),而 16k wav 是隨機存取、實測 0.01 秒。
        ⚠️ **這一格是切換鈕不是播放鈕**(同 `_naming_play` 那條使用者 2026-07-18 指定的
        規矩):正在播的那一列已經畫成 ■,再點一下**必須是停止**。少了下面這一段的話,
        點 ■ 走的是「停掉 → 重剪 → 從頭再播一次」——畫面上那一格閃回 ▶ 又變 ■,而聲音
        根本沒停(使用者 2026-09-04 回報「按下正方形要停止播放,但是無效」)。
        ⚠️ 鍵值是 `("audit", index)`,與 `_audit_sound` 放進 `_naming_clips` 的那把同一支
        ——用整數 `index` 比對會與試聽的講者編號撞在一起。"""
        if self._playing == ("audit", index):    # 再按一次 = 停
            self._naming_stop()
            return
        self._naming_stop()                      # 按別一列 = 直接切換
        row = self._audit_rows[index]
        dest = naming_core._audit_dir() / f"audit_{self._audit_spk}_{index}.wav"

        def work(say) -> None:
            say(("audit_clip", (index, str(audit.cut_one(Path(self._audit_src),
                                                         row, dest)))))

        self._run_job(work, lambda err: self._audit_played(err))

    def _audit_played(self, error) -> None:
        if error is not None:
            logger.exception("核對片段剪不出來", exc_info=error)
            self._audit_say("這一段剪不出來。")

    def _audit_sound(self, index: int, path: str) -> None:
        """核對那一列剪好了,放出來。⚠️ 與試聽共用同一組停止/切換的狀態。"""
        self._naming_clips = dict(self._naming_clips or {})
        key = ("audit", index)
        self._naming_clips[key] = path
        self._naming_sound(key)

    def _naming_stop(self) -> None:
        """停掉目前這一段(切換、收起命名區、換下一檔都會走到)。"""
        if self._naming_after is not None:
            self.after_cancel(self._naming_after)
            self._naming_after = None
        if self._playing is None:
            return
        try:
            import winsound

            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            logger.debug("停止試聽失敗", exc_info=True)
        btn = self._naming_btns.get(self._playing)
        if btn is not None:
            btn.configure(text=f"{naming_core._ROW_PLAY} 試聽")
        if isinstance(self._playing, tuple):
            self._audit_paint_play(None)
        self._playing = None

    def _naming_apply(self) -> None:
        r"""按下「套用名字到逐字稿」:改寫 md 的講者標籤,填過的名字存進聲紋庫、加進與會
        名單,然後收工。

        ⚠️ **同一個名字可以給不只一位講者,而且每一位的聲紋都登記**(2026-09-03 使用者
        兩次指定:先說「網頁版是允許的」,我回報「網頁版對兩位都跑 `enroll`、與你說的
        『第二次不更新聲紋』不同」之後,他裁定**以網頁版為準**)。⚠️ **不要改成擋下來、
        也不要改成只記第一位**——兩種都做過又被推翻了:
        - **擋下來**(原生版第一版):講者分離把同一個人切成兩群是常有的事(那正是
          「重新分群」與「核對」存在的理由),那時使用者手上唯一正確的答案就是同一個
          名字,擋掉他等於逼他在逐字稿裡留一個假名字。
        - **只記第一位**(中間那一版):行為與 `..\meeting-scribe` 的 `_apply_names`
          不一致,而兩套介面對同一件事給不同結果比風險本身更難查。
        ⚠️ **已知的代價**(使用者知道並接受):兩群的質心不會一樣,萬一那兩群其實是兩個
        人,別人的聲音就進了這個人名下,而那個錯誤看不見又會自我複製(下次自動認名 →
        再登記一次)。真的發生時的處方在 `docs/spec/09` §9.6 與 `scripts/audit_voiceprints.py`。
        ⚠️ 這與「自動填名一個名字只能給一位」是**兩件事**:那條講的是
        `voiceprints.recognize_batch`,現在照舊(見 `_naming_show`)。
        ⚠️ **留白的維持「講者 N」、而且不記聲紋**;「未知」只改字、**絕不登記聲紋、也
        不進與會名單**(同網頁版 `_apply_names`):未知是與每位講者都不夠像的零碎語音
        (常是多人重疊)的集合,聲紋混雜,登記會污染聲紋庫;使用者給它的稱呼也常非人名
        (如「其他」),不該進名單下拉。
        ⚠️ **在「🔍 核對」裡改掛過的講者不登記聲紋**(使用者 2026-08-13 選定):改掛等於
        他親口說「這一群不只一個人」,不純的群一旦登記,聲紋庫就把別人的聲音學進這個人
        名下——下次認得更錯,而使用者看不出來。逐字稿照樣改名:那是他確認過的事實。
        ⚠️ **套用即收工**(同網頁版):卡片收起、畫面復位。先前套用後卡片留著、按鈕還能
        按——第二次按名字改不到任何行、`enroll` 卻照跑,同一份聲紋重複進庫,而同名上限
        會把那個人最舊的真樣本擠掉。"""
        if self._naming_result is None:
            return
        result, _src = self._naming_result
        # ⚠️ **鍵要 +1**:`_rename_speakers` 預設找的是「講者 N」,而那個 N 是
        # `export.speaker_label` 的 **1-based** 編號。用 0-based 的話它一行都改不到
        # ——**而且完全不會出聲**(檔案還在、畫面照樣復位)。2026-08-29 第一版就是
        # 這樣,測試之所以是綠的,是因為我把 fixture 寫成配合那個 bug。
        picked = {spk + 1: var.get().strip()
                  for spk, var in self._naming_vars.items()
                  if spk != UNKNOWN_SPEAKER and var.get().strip()}
        unknown_var = self._naming_vars.get(UNKNOWN_SPEAKER)
        unknown_name = unknown_var.get().strip() if unknown_var is not None else ""
        outputs = [Path(p) for p in result.outputs]
        mds = [p for p in outputs if p.suffix.lower() == ".md"]
        changed = 0
        if picked or unknown_name:
            for path in mds:
                text = path.read_text(encoding="utf-8")
                # ⚠️ **標籤現場從檔案讀**:一般逐字稿讀到的就是「講者 N」;而重設講者
                # 那條路讀到的是當初命名過的真名,那正是這次要換掉的東西。
                after = naming_core._rename_speakers(
                    text, picked, unknown_name or None, naming_core._labels_in(text))
                # ⚠️ **改不到要出聲**:套用成功與否使用者看不出來(畫面照樣復位、檔案
                # 照樣在),真的發生時多半是格式對不上,那是工具的 bug 不是使用者的。
                if after == text:
                    logger.warning("套用名字沒改到任何一行:%s", path.name)
                else:
                    path.write_text(after, encoding="utf-8")
                    changed += 1
        # ⚠️ `result.voiceprints` 是**每位講者的聲紋向量**({講者: 質心}),不是名字
        # ——把它當成預填名用會塞一個 numpy 陣列進下拉選單(第一版就寫錯了)。
        impure = {int(i) for i in self._naming_audit.get("reassigned") or ()}
        roster_before = roster.current()
        enrolled, held = 0, []
        # ⚠️ **照講者編號排**:同一個名字給了不只一位時,聲紋進庫的先後由編號決定,
        # 而 dict 的插入順序取決於命名區怎麼建,不該由它決定聲紋庫的內容(`enroll` 的
        # 同名上限會淘汰最舊的一筆,所以「誰先誰後」不是完全沒有差別的)。
        for spk1, name in sorted(picked.items()):
            attendees.add(name)        # 輸入的新名字自動加入與會名單(下次下拉可選)
            vec = (result.voiceprints or {}).get(spk1 - 1)
            if vec is None:
                continue
            if (spk1 - 1) in impure:
                logger.info("「%s」核對時改掛過段落,這次不登記聲紋", name)
                held.append(name)
                continue
            voiceprints_store.enroll(name, vec)
            enrolled += 1
        # 名單頁開著的話,新名字要看得到——但只在那一頁的內容還是「上次載入的樣子」時
        # 重載,使用者改到一半沒存的東西不能被洗掉。
        if picked and "roster" in self._pages and \
                self._roster_box.get("1.0", "end-1c") == "\n".join(roster_before):
            self._roster_load()
        # ⚠️ **「聲紋記起來了」只有真的記了才能講**(2026-09-01 端到端跑「重設講者」
        # 時抓到):那條路在**同一層沒有錄音檔**時只改名字、一個聲紋都不抽,而原本的
        # 文案是「填了名字就一定有聲紋」。謊報的代價不是文案不準——使用者會以為下次
        # 開會認得出這個人,於是不再回來補登記。
        total = len(picked) + (1 if unknown_name else 0)
        note = f"已套用 {total} 個名字到 {changed} 份逐字稿。"
        if enrolled:
            note += f"其中 {enrolled} 位的聲紋也記起來了——下次開會會自動認出來。"
        if held:
            note += ("\n" + "、".join(held)
                     + ":核對時改掛過段落(表示那一群不只一個人),這次不登記聲紋。")
        note += "\n改好的逐字稿:" + "、".join(str(p) for p in mds)
        self._naming_finish(note)

    def _run_paths(self) -> str:
        return self._run_src.get("1.0", "end-1c")

    def _run_set_path(self, text: str) -> None:
        with unlocked(self._run_src):
            self._run_src.delete("1.0", "end")
            self._run_src.insert("1.0", text or "")
        self._run_refresh_summary()

    def _run_refresh_summary(self) -> None:
        """選了幾個、會走哪種模式(文字在 `doctab.audio_summary`)。⚠️ 空的時候整行收起來。"""
        text = doctab.audio_summary(self._run_paths(), bool(self._run_recursive.get()))
        if text:
            self._run_summary.configure(text=helpmd.flatten(text))
            self._run_summary.pack(anchor="w", pady=(self.px(SP_SM), 0))
        else:
            self._run_summary.pack_forget()

    def _run_pick(self) -> None:
        r"""「選擇檔案…」:原生對話框,**可多選、累加**(選一個仍走單檔模式,見
        `srcfile.looks_like_batch`;對話框本身在 `srcfile.pick_files`,兩套介面共用)。

        ⚠️ 先前這裡寫的是 `srcfile.native_dialog(srcfile.OPEN_FILE)`,而 `OPEN_FILE`
        **根本不存在**、`native_dialog` 收的也是一個 callable——那一行從來沒被執行
        過,直到使用者 2026-08-30 真的按下去。症狀又是「按了沒反應」:Tk 把
        `AttributeError` 吞進紀錄檔,畫面上一個字都沒有。"""
        self._run_set_path(srcfile.append_paths(self._run_paths(), srcfile.pick_files()))

    def _run_pick_folder(self) -> None:
        """「選擇資料夾…」:整個資料夾丟進來(= 批次模式),一樣累加。"""
        self._run_set_path(srcfile.append_paths(self._run_paths(), srcfile.pick_folder()))

    def _run_clear(self) -> None:
        """「清空」:選檔是累加的,要重來得有這顆。"""
        self._run_set_path("")

    def _run_set_preview(self, text: str) -> None:
        # 原文留一份(核對改掛與命名進度落地都要它),框裡畫的是剝過記號的版本
        self._preview_md = text or ""
        self._run_preview.configure(state="normal")
        self._run_preview.delete("1.0", "end")
        self._run_preview.insert("1.0", helpmd.flatten(text))
        self._run_preview.configure(state="disabled")

    def _run_stop_click(self) -> None:
        cancel.request()
        self._stage("正在停止…(引擎會在下一個段落邊界停下)")

    def _run_lock(self, running: bool, outcome: int = winui.TBPF_NOPROGRESS) -> None:
        r"""鎖住/放開「檔案轉檔」這條路的按鈕;`outcome` 是收工時留在工作列上的顏色。

        ⚠️ **三個模式的鈕都要一起管**(2026-09-01 加上重設講者之後):它們共用
        `_busy["run"]` 這一個旗標與同一顆停止鈕的語意,漏掉哪一組,那一組就會在
        工作進行中還亮著——按下去是第二趟,而第一趟還在跑。"""
        self._busy["run"] = running
        self._params_lock()             # 進行中鎖進階參數(見那一支;錄音收尾也走這裡)
        self._data_lock()               # 三個資料分頁與選檔那幾樣(見 `_data_lock`)
        # 工作列上的進度掛在這裡(而不是各個 start):`_run_lock` 是這條路唯一的總開關
        # ——五個進入點(單檔、批次、重設講者、重新分群、錄音收尾)全走它,掛在那五支
        # 上遲早漏掉一支,而漏掉的症狀是那一種工作在工作列上完全看不出來。
        # ⚠️ **開頭一律是跑馬燈**:這一刻連檔案清單都還沒展開,不可能算得出剩多久;
        # 第一則帶 frac 的進度一來,`_taskbar` 自己就換成進度條。
        if running:
            self._taskbar(None)
            self._run_dirs = None       # 這一趟的成品位置由這一趟自己說
        else:
            self._taskbar_end(outcome)
        for go, stop in ((self._run_btn, self._run_stop),
                         (self._relabel_btn, self._relabel_stop)):
            go.configure(state="disabled" if running else "normal")
            stop.configure(state="normal" if running else "disabled")

    def _run_start(self) -> None:
        r"""按下「開始轉檔」。

        ⚠️ **三條工作路徑互斥是正確性需求、不只是體驗**:`cancel` 的旗標是全域單例,
        兩邊同時跑時任一顆停止鈕會把另一邊也殺掉(CLAUDE.md 的硬規則)。"""
        # ⚠️ 同 `_doc_reset_view` 那條:上一趟的成果要在把關**之前**清掉,否則被擋
        # 下來時畫面還掛著上一趟的「完成。」與亮著的「輸出資料夾…」。
        self._stage("")
        self._run_bar.configure(value=0)
        self._run_open.configure(state="disabled")
        self._naming_hide()
        busy = self._busy_reason("run")
        if busy:
            self._stage(busy)
            return
        # 兩種模式,由輸入的**形狀**決定(不看展開後有幾個檔;`srcfile.looks_like_batch`
        # 的 docstring 寫著為什麼):多檔或任何資料夾 → 批次,剛好一個檔案 → 這條
        text = self._run_paths()
        if srcfile.looks_like_batch(text):
            self._run_start_batch(text)
            return
        try:
            src = srcfile.validate(text)
        except UserFacingError as e:
            self._run_set_preview(str(e))
            return
        cancel.reset()
        self._run_lock(True)
        self._stage(f"{src.name}:準備中…")
        self._run_set_preview("")
        # ⚠️ 值要在主執行緒先讀好(同文件分頁那條:Tk 不是執行緒安全的)
        model_key = MODEL_LABELS[self._run_model.get()]
        speakers = self._normalize_speakers(self._run_speakers.get())
        cores = self._run_cores.get()

        def work(say) -> None:
            # ⚠️ 核心數要在引擎建起來**之前**套(執行緒數是建構參數);放在工作執行緒
            # 是因為有變動時會收掉轉錄子行程,那一下不該卡住視窗
            pipeline.apply_worker_count(cores)
            result = run_pipeline(
                src, OUTPUT_DIR, model_key=model_key, num_speakers=speakers,
                on_stage=lambda stage, frac: say(
                    ("run_stage", (f"{src.name}:{stage}", frac))),
            )
            device = DEVICE_NAMES.get(result.device, result.device)
            preview = f"(執行裝置:{device})\n\n{result.preview}"
            # 有成品、卻連一位講者都沒有 = 整段沒聽到人說話。**一定要明講**(同網頁版
            # `_present_result`):命名卡這時不會出現,不講的話畫面上只有一份空稿子。
            if not pending.anyone_to_name(result.speakers, result.speaker_hints or {}):
                preview = f"{NO_SPEECH_NOTE}\n\n{preview}"
            say(("run_preview", preview))
            say(("named", dict(result=result, src=src)))

        self._run_job(work, self._run_done)

    def _run_start_batch(self, text: str) -> None:
        r"""多檔/資料夾:整批連續轉,**不做講者命名**(網頁版 `_run_batch`,使用者
        2026-08-06 指定:一批幾十個檔沒有「當場」可言,硬做只會把這一檔的名字寫進別檔)。

        **走的是「文字、圖像→MD」那條批次路徑**(`docpipe.convert_batch`),音訊只是
        路由表上多幾個副檔名(`docaudio`)。不另寫一條批次迴圈:「原地輸出」「同名 md
        就跳過」「先整批規劃再動手」「單檔失敗不中斷」這些規則每重寫一次就是一次走樣
        的機會,而它們在那邊都有測試守著。
        ⚠️ **白名單是音訊自己那份**(`srcfile.SUPPORTED_TYPES`):混用會讓這顆「開始
        轉檔」開始接受 PDF。⚠️ 模型與講者人數照畫面上填的套用到整批(填 0 = 自動偵測)。
        ⚠️ 成品在原檔旁邊、可能跨好幾個資料夾,所以「輸出資料夾…」開的是報告裡的那幾個
        (`run_dirs`),不是 output。⚠️ 按停止時 `convert_batch` 自己接住 `Cancelled`、
        回一份 `cancelled` 的報告(已完成的檔案留著),這裡把它再丟成 `Cancelled` 讓
        收尾講「已依要求停止」,而不是「完成。」。"""
        recursive = bool(self._run_recursive.get())
        try:
            files, skipped = docsrc.validate_batch(
                text, recursive=recursive, types=srcfile.SUPPORTED_TYPES,
                what="錄音或錄影檔", hint=srcfile.supported_hint(),
            )
        except UserFacingError as e:
            self._run_set_preview(str(e))
            return
        cancel.reset()
        self._run_lock(True)
        self._stage(f"{len(files)} 個檔案:準備中…")
        # 先乾跑再動手(同文件分頁):批次一次動使用者資料夾裡數十個檔,開跑前讓人
        # 看得到「將寫出/將略過」比事後報告有用得多
        plan = docpipe.plan_outputs(files)
        self._run_set_preview("即將轉換以下檔案(輸出會放在原始檔案旁邊):\n\n"
                              + "\n".join(docpipe.dry_run_lines(plan)))
        # ⚠️ 值要在主執行緒先讀好(Tk 不是執行緒安全的)
        model_key = MODEL_LABELS[self._run_model.get()]
        speakers = self._normalize_speakers(self._run_speakers.get())
        cores = self._run_cores.get()

        def work(say) -> None:
            pipeline.apply_worker_count(cores)
            # 逐檔進度直接沿用 docpipe 給的那句話——它已經把「第幾個/共幾個 + 檔名」
            # 組好了(`(3/12) 月會.m4a`),不必在這裡重組一份會走樣的
            report = docpipe.convert_batch(
                files, skipped,
                on_stage=lambda stage, frac: say(("run_stage", (stage, frac))),
                options={"model_key": model_key, "num_speakers": speakers},
            )
            say(("run_preview", docpipe.report_markdown(report)))
            say(("run_dirs", [str(d) for d in report.out_dirs]))
            if report.failed:
                say(("outcome", "partial"))
            if report.cancelled:
                raise Cancelled()

        self._run_job(work, self._run_done)

    @staticmethod
    def _normalize_speakers(value) -> int:
        """欄位清空或亂填時一律安全歸零(0 = 自動偵測),超過上限就夾住。

        ⚠️ **範圍在這一側夾**,不交給控制項:讓 `Spinbox` 自己擋會跳出英文訊息。"""
        try:
            return max(0, min(MAX_SPEAKERS, int(str(value).strip())))
        except (TypeError, ValueError):
            return 0

    def _run_done(self, error) -> None:
        # ⚠️ **旗標要在 `_run_lock` 之前算**:那一支收工時就把顏色送出去了,先清再上色
        # 的話工作列會閃一下白的(而且多一趟 COM)。`naming=True`:這條路轉完之後真的
        # 會冒出命名卡,而那時「完成」還不等於「收工」。
        flag = self._work_outcome(error, naming=True)
        self._run_lock(False, flag)
        self._run_bar.configure(value=0)
        if error is None:
            self._stage("完成。")
            # 批次一個都沒轉出來(全部略過)就沒有資料夾可開;None 是單檔那條 = output
            self._run_open.configure(
                state="disabled" if self._run_dirs == [] else "normal")
        elif isinstance(error, Cancelled):
            # 半成品已由 pipeline 自己清掉(批次那條:已完成的檔案留著,報告在預覽裡)
            self._stage("已依要求停止,本檔案尚未轉完。")
        elif isinstance(error, UserFacingError):
            self._stage("")
            self._run_set_preview(str(error))
        else:
            logger.exception("處理失敗", exc_info=error)
            self._stage("")
            self._run_set_preview(
                "發生未預期的錯誤,詳情見紀錄檔(logs 資料夾);"
                "若為記憶體不足,請改選「快速」模型再試。")
        # ⚠️ **閃工作列、但絕不把視窗搶到前景**(使用者 2026-09-05 選定,同
        # `MP4-2-SRT`):切走兩小時的人需要有人叫他一聲,而搶焦點會把他正在打的字吃掉。
        # `winui.flash_taskbar` 自己擋掉「視窗本來就在前景」那一種(人就坐在畫面前,
        # 結果已經寫在他眼前了),所以這裡三條路都無條件叫。
        winui.flash_taskbar(self)

    # ---- 文字、圖像→MD --------------------------------------------------- #
    def _doc_page(self) -> ttk.Frame:
        r"""批次把 Office / PDF / 網頁 / 影像 / 郵件 / 錄音錄影轉成 Markdown。

        ⚠️ **產物的主要消費者是 AI 不是人**(使用者 2026-08-01 指定),所以轉不出來的
        東西一律在原地留繁中〔〕標記並彙總進 frontmatter——**無聲失真是唯一不可接受
        的結果**。那一整套在 `docpipe`,這裡只負責接線。
        ⚠️ **這一頁沒有「模型」與「講者人數」**(使用者 2026-08-06 指定):它的用法是
        「一次丟一堆混合檔案」,為其中一種格式加兩個旋鈕,會讓另外三十種格式的使用者
        每次都看到它們。要調參數就去「聲音→MD」。
        ⚠️ **版面照網頁版**(2026-09-03 使用者:「AP 的功能請比照 WEB 做正確」;先前是三張
        滿寬的卡直向堆疊):上方一段說明,底下兩欄 5:7——左欄由上而下是檔案卡、三顆選檔鈕、
        「已選幾個」、三顆動作鈕、「進階參數設定」摺疊卡(三個選項在裡面)、一句備註;右欄
        一張「轉檔進度與結果」卡,標題旁有「複製」。選檔鈕與動作列都在卡片**外面**(同轉檔
        頁的主要動作鈕),所以它們穿的是坐在視窗底上的那三張皮(`skin.*_PAGE_STYLE`)。"""
        page = ttk.Frame(self._body, style="Page.TFrame")
        # 上方那段說明(網頁版 `#doc-intro`):整個內容區寬,坐在視窗底上。⚠️ 走 `_icon_text`
        # 不是 Label:句子裡的「❓ 使用說明」「🎙 聲音→MD」要畫成彩色圖示(使用者 2026-09-03
        # 圈出來的),而 `ttk.Label` 塞不進行內圖片。⚠️ **字色是深色 `ink` 不是灰**:量網頁版
        # 截圖,這三段說明的字全是 #1d1d1f(灰的只有頁首那句副標);先前那版沿用 PageInfo
        # 的灰,是自己發明的。
        self._doc_intro = self._icon_text(
            page, DOC_INTRO.format(n=len(docsrc.GUI_TYPES)), bg="page", fg="ink", pt=10,
            line_gap=INTRO_LINE_GAP)
        # ⚠️ 底下留 `CARD_GAP` 不是 `SP_LG`:量網頁版,最後一行墨跡到卡頂 38 實體 px,
        # SP_LG 只到 32(行距補了半行之後);CARD_GAP 正好 38,而它的意思本來就是「到下一張卡」。
        self._doc_intro.pack(anchor="w", fill="x", pady=(0, self.px(CARD_GAP)))
        grid = ttk.Frame(page, style="Page.TFrame")
        grid.pack(fill="both", expand=True)
        cols = self._two_columns(grid, "doc")
        inner, right = cols.inner, cols.right
        gap = self.px(CARD_GAP)

        # ---- 左:檔案卡 ----
        src = ttk.Frame(inner, style="Card.TFrame", padding=self.px(CARD_PAD))
        src.pack(fill="x")
        ttk.Label(src, text="要轉換的檔案或資料夾(一行一個)",
                  style="CardH.TLabel").pack(anchor="w")
        wrap = ttk.Frame(src, style="Sunken.TFrame", padding=self.px(SP_XS))
        wrap.pack(fill="x", pady=(self.px(SP_SM), 0))
        self._doc_src = tk.Text(wrap, font=(self.fam, 10), height=6, relief="flat",
                                bd=0, highlightthickness=0, wrap="none",
                                bg=self.pal["field"], fg=self.pal["ink"],
                                insertbackground=self.pal["ink"],
                                selectbackground=self.pal["row_sel"],
                                padx=self.px(SP_SM), pady=self.px(SP_XS))
        self._lockable(self._doc_src).pack(fill="x")
        self._doc_src.tag_configure("ph", foreground=self.pal["muted"])
        self._doc_ph = False
        self._doc_src.bind("<FocusIn>", lambda _e: self._doc_ph_hide())
        self._doc_src.bind("<FocusOut>", lambda _e: self._doc_ph_show())
        self._doc_ph_show()

        # ---- 選檔鈕列(卡片外;網頁版:兩顆等寬、「清空」照內容寬)----
        row = ttk.Frame(inner, style="Page.TFrame")
        row.pack(fill="x", pady=(gap, 0))
        row.columnconfigure(0, weight=1, uniform="pick")
        row.columnconfigure(1, weight=1, uniform="pick")
        self._doc_pick_btns = (
            HandButton(row, text="選擇檔案…", style=skin.CTA_PAGE_STYLE,
                       command=self._doc_pick_files),
            HandButton(row, text="選擇資料夾…", style=skin.CTA_PAGE_STYLE,
                       command=self._doc_pick_folder),
            HandButton(row, text="清空", style=skin.SMALL_PAGE_STYLE,
                       command=self._doc_clear),
        )
        for _btn in self._doc_pick_btns:        # 轉檔中一併鎖住(見 `_data_lock`)
            self._lockable(_btn)
        self._doc_pick_btns[0].grid(row=0, column=0, sticky="ew", padx=(0, gap // 2))
        self._doc_pick_btns[1].grid(row=0, column=1, sticky="ew",
                                    padx=(gap - gap // 2, 0))
        self._doc_pick_btns[2].grid(row=0, column=2, padx=(gap, 0))
        # 「已選 N 個檔案」:空的時候整行收起來(見 `_doc_refresh_summary`),擠在選檔鈕與
        # 動作列之間,所以要 `before=` 動作列
        self._doc_summary = self._wrap(
            ttk.Label(inner, style="PageStatus.TLabel", justify="left"))

        # ---- 動作列(卡片外,三顆等寬同高;同網頁版)----
        bar = ttk.Frame(inner, style="Page.TFrame")
        bar.pack(fill="x", pady=(gap, 0))
        self._doc_actions = bar
        for i in range(3):
            bar.columnconfigure(i, weight=1, uniform="act")
        # 「開始轉檔」**一律可按**(使用者 2026-08-01 指定,與逐字稿那條刻意不同):
        # 貼上路徑時不一定觸發任何事件,鈕不亮會讓人以為工具壞了;按下去才把關,
        # 錯誤訊息會講清楚是空的、找不到、還是格式不支援。
        self._doc_run = HandButton(bar, text="開始轉檔", style=skin.RUN_PAGE_STYLE,
                                   command=self._doc_start)
        self._doc_run.grid(row=0, column=0, sticky="ew", padx=(0, gap // 2))
        self._doc_stop = HandButton(bar, text="停止", style=skin.STOP_PAGE_STYLE,
                                    state="disabled", command=self._doc_stop_click)
        self._doc_stop.grid(row=0, column=1, sticky="ew",
                            padx=(gap - gap // 2, gap // 2))
        self._doc_open = HandButton(bar, text="輸出資料夾…", style=skin.SKIP_PAGE_STYLE,
                                    state="disabled", command=self._doc_open_dirs)
        self._doc_open.grid(row=0, column=2, sticky="ew", padx=(gap - gap // 2, 0))

        # ---- 進階參數設定:三個選項(網頁版的 Accordion,預設收著)----
        self._doc_adv = self._fold(
            inner, "進階參數設定", lambda: self._fold_toggle(self._doc_adv, cols))
        self._doc_opts = {}
        # ⚠️ **三個預設都是開的**,而且每一個都要有說明:關掉的後果不對稱(OCR 關掉
        # 圖裡的字就整片不見),而使用者看不到那件事發生。
        for n, (key, label, note) in enumerate((
            ("recursive", "包含子資料夾",
             "選整個資料夾時,連裡面的子資料夾一起找;關掉只轉最上層那一層"),
            ("ocr", "辨識圖片裡的文字(OCR)",
             "掃描檔、照片,以及文件裡的內嵌圖都會辨識;關掉會快很多,但那些圖只會留下連結"),
            ("mail", "郵件附件一併轉檔",
             "Outlook 郵件(msg/eml)的附件會一起轉成 Markdown 並在信件本文連結;"
             "關掉只轉信件本身"),
        )):
            var = tk.BooleanVar(value=True)
            self._doc_opts[key] = var
            line = ttk.Frame(self._doc_adv.body, style="CardBody.TFrame")
            line.pack(fill="x", pady=(self.px(SP_SM) if n else 0, 0))
            ttk.Checkbutton(line, text=label, variable=var,
                            style="Card.TCheckbutton").pack(anchor="w")
            self._wrap(ttk.Label(line, text=note, style="Hint.TLabel",
                                 justify="left")).pack(
                                     anchor="w", padx=(self.px(SP_LG), 0))

        # ---- 備註(網頁版 `.hint`)----
        self._wrap(ttk.Label(inner, text=DOC_NOTE, style="PageStatus.TLabel",
                             justify="left")).pack(anchor="w", pady=(gap, 0))

        # ---- 右:進度與結果 ----
        run = ttk.Frame(right, style="Card.TFrame", padding=self.px(CARD_PAD))
        run.pack(fill="both", expand=True)
        head = ttk.Frame(run, style="CardBody.TFrame")
        head.pack(fill="x")
        ttk.Label(head, text="轉檔進度與結果", style="CardH.TLabel").pack(side="left")
        # 網頁版的結果框右上角有一顆複製鈕(`buttons=["copy"]`):**只有圖示、沒有字**
        self._doc_copy_btn = self._copy_button(head, self._doc_copy_result)
        self._doc_copy_btn.pack(side="right")
        # 階段文字 ＋ 進度條:閒置時整組收起來(同轉檔頁的 `_stage`;網頁版閒置時沒有進度槽)
        self._doc_prog = ttk.Frame(run, style="CardBody.TFrame")
        self._doc_stage = ttk.Label(self._doc_prog, text="", style="Status.TLabel")
        self._doc_stage.pack(anchor="w", pady=(self.px(SP_MD), self.px(SP_XS)))
        self._doc_bar = ttk.Progressbar(self._doc_prog, mode="determinate", maximum=100)
        self._doc_bar.pack(fill="x")
        rwrap = ttk.Frame(run, style="Sunken.TFrame", padding=self.px(SP_XS))
        rwrap.pack(fill="both", expand=True, pady=(self.px(SP_SM), 0))
        self._doc_rwrap = rwrap
        self._doc_result = tk.Text(rwrap, font=(self.fam, 10), height=PREVIEW_LINES,
                                   relief="flat", bd=0, highlightthickness=0,
                                   wrap="word", state="disabled",
                                   bg=self.pal["field"], fg=self.pal["ink"],
                                   selectbackground=self.pal["row_sel"],
                                   padx=self.px(SP_SM), pady=self.px(SP_XS))
        rbar = ttk.Scrollbar(rwrap, orient="vertical", command=self._doc_result.yview)
        self._doc_result.configure(yscrollcommand=rbar.set)
        rbar.pack(side="right", fill="y")
        self._doc_result.pack(side="left", fill="both", expand=True)
        self._doc_dirs: list[str] = []
        return page

    def _doc_ph_show(self) -> None:
        """路徑框空著、游標也不在裡面:放提示字(灰)。"""
        if not self._doc_ph and not self._doc_src.get("1.0", "end-1c").strip():
            with unlocked(self._doc_src):
                self._doc_src.delete("1.0", "end")
                self._doc_src.insert("1.0", DOC_PLACEHOLDER, "ph")
            self._doc_ph = True

    def _doc_ph_hide(self) -> None:
        if self._doc_ph:
            with unlocked(self._doc_src):
                self._doc_src.delete("1.0", "end")
            self._doc_ph = False

    def _doc_paths(self) -> str:
        """路徑框裡的字——提示字不算(`_doc_ph`)。"""
        return "" if self._doc_ph else self._doc_src.get("1.0", "end-1c")

    def _doc_set_paths(self, text: str) -> None:
        self._doc_ph_hide()
        with unlocked(self._doc_src):
            self._doc_src.delete("1.0", "end")
            self._doc_src.insert("1.0", text or "")
        if not text and self.focus_get() is not self._doc_src:
            self._doc_ph_show()
        self._doc_refresh_summary()

    def _doc_say(self, text: str) -> None:
        """文件頁右卡那一行階段文字。規矩全在 `_say`。"""
        self._say(self._doc_stage, self._doc_prog, self._doc_rwrap, text)

    def _copy_button(self, parent, command) -> tk.Widget:
        r"""結果框右上角那顆「複製」——**只有圖示、沒有字**,照網頁版(gradio 的 copy 鈕)。

        (2026-09-03 使用者:「複製的按鈕,請放 WEB 頁面的圖示即可,不要複製的文字」。)
        量網頁版截圖 @150%:26×26 實體 px 的圓角方塊(底 `btn` #e8e8ed、圓角約 4),中間
        18×18 的「兩張紙」線稿——前面一張 14×14 的圓角方框、左上角露出後面那張的 L 形一角
        (兩邊各 10)、線寬約 1.5、墨色 `ink`。整顆用 Pillow 畫成圖、放在 Label 上(同分頁
        格的做法):共用包沒有這麼小的方形底板,為一顆鈕多養一張皮不划算。hover 換 `btn_hi`
        那張(gradio 的 icon-button hover 也是把底色加深一階)。畫不出來就退回文字鈕,不是
        壞掉。⚠️ Label 要穿卡片底色的樣式(`Hint.TLabel` 的字級與它無關,只借它的底色):
        圖的四個圓角外面是透明的,底下露出來的必須是卡片白。"""
        try:
            normal, hover = self._copy_icon(self.pal["btn"]), self._copy_icon(self.pal["btn_hi"])
        except Exception:
            logger.debug("複製鈕的圖示畫不出來,退回文字鈕", exc_info=True)
            return HandButton(parent, text="複製", style="Small.TButton", command=command)
        lab = ttk.Label(parent, image=normal, style="Hint.TLabel", cursor="hand2")
        lab.bind("<Enter>", lambda _e: lab.configure(image=hover))
        lab.bind("<Leave>", lambda _e: lab.configure(image=normal))
        lab.bind("<Button-1>", lambda _e: command())
        return lab

    def _copy_icon(self, bg: str):
        """把那顆複製鈕畫成圖(尺寸見 `COPY_BTN`/`COPY_GLYPH`);同一個底色只畫一次。"""
        key = ("copy", bg)
        if key in self._icons:
            return self._icons[key]
        from PIL import Image, ImageDraw, ImageTk

        k = 4                                     # 放大 4 倍畫、再縮回來,線才有抗鋸齒
        size = self.px(COPY_BTN)
        big = size * k
        im = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.rounded_rectangle((0, 0, big - 1, big - 1), radius=self.px(3) * k, fill=bg)
        g = self.px(COPY_GLYPH) * k               # 線稿的外框(18/18)
        o = (big - g) // 2                        # 線稿在鈕裡置中
        w = max(k, round(g * 1.5 / 18))           # 線寬 1.5/18
        f = round(g * 4 / 18)                     # 前面那張從 4/18 起、到右下角
        d.rounded_rectangle((o + f, o + f, o + g - 1, o + g - 1),
                            radius=max(k, round(g * 1.5 / 18)), outline=self.pal["ink"], width=w)
        leg = round(g * 10 / 18)                  # 後面那張只露出左上角的 L,兩邊各 10/18
        half = w // 2
        d.line([(o + half, o + leg), (o + half, o + half), (o + leg, o + half)],
               fill=self.pal["ink"], width=w, joint="curve")
        photo = ImageTk.PhotoImage(im.resize((size, size), Image.LANCZOS))
        self._icons[key] = photo
        return photo

    def _doc_copy_result(self) -> None:
        """「複製」:結果框整份進剪貼簿(網頁版結果框右上角那顆)。"""
        self.clipboard_clear()
        self.clipboard_append(self._doc_result.get("1.0", "end-1c"))

    def _doc_refresh_summary(self) -> None:
        """選了幾個、會轉幾份。⚠️ 空的時候整行收起來,不留一行空白。"""
        text = doctab.preview_summary(self._doc_paths(),
                                      bool(self._doc_opts["recursive"].get()))
        if text:
            self._doc_summary.configure(text=helpmd.flatten(text))
            self._doc_summary.pack(anchor="w", pady=(self.px(CARD_GAP), 0),
                                   before=self._doc_actions)
        else:
            self._doc_summary.pack_forget()

    def _doc_pick_files(self) -> None:
        self._doc_set_paths(doctab.pick_files(
            self._doc_paths(), bool(self._doc_opts["recursive"].get()))[0])

    def _doc_pick_folder(self) -> None:
        self._doc_set_paths(doctab.pick_folder(
            self._doc_paths(), bool(self._doc_opts["recursive"].get()))[0])

    def _doc_clear(self) -> None:
        self._doc_set_paths("")

    def _doc_set_result(self, text: str) -> None:
        self._doc_result.configure(state="normal")
        self._doc_result.delete("1.0", "end")
        self._doc_result.insert("1.0", helpmd.flatten(text))
        self._doc_result.configure(state="disabled")

    def _doc_open_dirs(self) -> None:
        doctab.open_output_dirs(self._doc_dirs)

    def _doc_stop_click(self) -> None:
        cancel.request()
        self._doc_say("正在停止…(會先把手上這一份做完)")

    def _doc_lock(self, running: bool,
                  outcome: int = winui.TBPF_NOPROGRESS) -> None:
        self._busy["doc"] = running
        self._data_lock()               # 三個資料分頁與選檔那幾樣(見 `_data_lock`)
        if running:                     # 同 `_run_lock`:開頭沒有分母,先流動
            self._taskbar(None)
        else:
            self._taskbar_end(outcome)
        self._doc_run.configure(state="disabled" if running else "normal")
        self._doc_stop.configure(state="normal" if running else "disabled")

    def _doc_reset_view(self) -> None:
        r"""把「上一趟的成果」從畫面上清掉。

        ⚠️ **要在**把關**之前**做,不是在開始跑之後**(2026-08-29,姊妹專案回報
        「狀態機的驗收要連跑兩趟」之後照著查出來的):第二趟被把關擋下來時會提早
        `return`,而那時畫面上還掛著第一趟的「完成。」與亮著的「輸出資料夾…」——
        使用者看到的是「錯誤訊息」與「完成」同時在畫面上,而那顆鈕會打開上一趟的
        資料夾。⚠️ **這種 bug 只有連跑兩趟才看得到**:測試全綠、截圖正常、真的轉過
        一次都不會發現,因為每一項都只跑了一趟。"""
        self._doc_say("")
        self._doc_bar.configure(value=0)
        self._doc_dirs = []
        self._doc_open.configure(state="disabled")

    def _doc_start(self) -> None:
        r"""按下「開始轉檔」。

        ⚠️ **先乾跑再動手**:批次一次動使用者資料夾裡數十個檔,開跑前讓人看得到
        「將寫出/將覆寫/將改名」比事後報告有用得多。
        ⚠️ **`cancel.reset()` 要在每一批開頭**:上一批按過的停止不得波及這一批。
        ⚠️ **要問另外兩條路徑在不在跑**(2026-08-30 補;先前這一頁誰都沒問,於是錄音
        中或轉逐字稿中照樣開得起來,而任一顆停止鈕會把另一邊一起殺掉)。"""
        self._doc_reset_view()
        busy = self._busy_reason("doc")
        if busy:
            self._doc_say(busy)
            return
        try:
            files, skipped = docsrc.validate_batch(
                self._doc_paths(), bool(self._doc_opts["recursive"].get()))
        except UserFacingError as e:
            self._doc_set_result(str(e))
            return
        cancel.reset()
        self._doc_lock(True)
        self._doc_say("準備中…")
        plan = docpipe.plan_outputs(files)
        self._doc_set_result("即將轉換以下檔案(輸出會放在原始文件旁邊):\n\n"
                             + "\n".join(docpipe.dry_run_lines(plan)))

        # ⚠️ **選項要在主執行緒先讀好再進工作執行緒**:`BooleanVar.get()` 是一次 Tk
        # 呼叫,從別條執行緒問會丟 `RuntimeError: main thread is not in main loop`
        # ——2026-08-29 第一版就是這樣,而它**只有真的跑一次轉檔才看得到**(建視窗、
        # 切分頁、跑測試都碰不到)。
        ocr = bool(self._doc_opts["ocr"].get())
        mail = bool(self._doc_opts["mail"].get())

        def work(say) -> None:
            report = docpipe.convert_batch(
                files, skipped,
                on_stage=lambda stage, frac: say(("stage", (stage, frac))),
                ocr_enabled=ocr,
                mail_attachments=mail,
                options={"model_key": transcribe.default_model_key(),
                         "num_speakers": 0},
            )
            say(("text", docpipe.report_markdown(report)))
            say(("dirs", [str(d) for d in report.out_dirs]))
            if report.failed:
                say(("outcome", "partial"))

        self._run_job(work, self._doc_done)

    def _doc_done(self, error) -> None:
        """收尾:完成、報錯、按停止都走這裡(所以鎖一定會解開)。"""
        # ⚠️ **這條路沒有 `naming=True`**:文件轉檔沒有講者命名這回事,傳了的話
        # 上一趟逐字稿留在 `_naming_result` 的那份會讓這裡誤判成「還等你來命名」。
        self._doc_lock(False, self._work_outcome(error))
        self._doc_bar.configure(value=0)
        if error is None:
            self._doc_say("完成。")
        elif isinstance(error, Cancelled):
            self._doc_say("已停止。")
        elif isinstance(error, UserFacingError):
            self._doc_say("")
            self._doc_set_result(str(error))
        else:
            logger.exception("文件轉檔失敗(未預期)", exc_info=error)
            self._doc_say("")
            self._doc_set_result("發生未預期的錯誤,詳情見紀錄檔(logs 資料夾)。")
        self._doc_open.configure(state="normal" if self._doc_dirs else "disabled")
        winui.flash_taskbar(self)       # 理由見 `_run_done` 最後那一行

    # ---- 使用說明 -------------------------------------------------------- #
    def _help_page(self) -> ttk.Frame:
        r"""使用說明:左目錄、右內容(版面沿用網頁版 2026-08-08 的設計稿方案 A)。

        ⚠️ **十篇的分法是「使用需求」不是功能**(見 `help_text` 的 docstring),而
        **有四段刻意在兩處各出現一次**(疑難排解那篇與原本的功能篇)——看到重複不要
        「順手」合併。
        ⚠️ **原生版沒有「全部內容(可搜尋)」那一篇**(2026-09-03 使用者:網頁版有那一篇
        是因為它沒有別的搜尋辦法)——這裡整本是**一份連續的內容**,目錄只負責捲到那一章,
        搜尋列搜的本來就是整本。詳見底下濾掉 `help_text.EVERYTHING` 那一段。"""
        page = ttk.Frame(self._body, style="Page.TFrame")
        card = ttk.Frame(page, style="Card.TFrame", padding=self.px(CARD_PAD))
        card.pack(fill="both", expand=True)
        card.columnconfigure(1, weight=1)
        card.rowconfigure(1, weight=1)          # 第 0 列是搜尋列,內容框在第 1 列撐滿

        # 目錄照網頁版 `#help-nav`(2026-09-03 使用者:「左邊選單,樣式請做成與 WEB 相同」):
        # 一條灰槽,每一篇一條,正在看的那條浮起成白膠囊(粗體、帶影子)。尺寸與圓角全是網頁版
        # CSS 的定義值,見 `TOC_*`;兩張底板在 `make_skin`(`Sq.toc` / `Sq.toc_on`)。
        # ⚠️ **上面那行「你現在想做什麼」2026-09-05 拿掉了**(使用者指定):網頁版有它是因為
        # 那是 `gr.Radio` 的 label,而原生版少了它之後,灰槽的頂端才有東西可以對齊——留著的話
        # 灰槽頂端卡在搜尋列(336)與內容框(401)之間的 371,跟誰都不齊。**不要照網頁版加回來。**
        # ⚠️ **原生版沒有「全部內容(可搜尋)」那一篇**(2026-09-03 使用者:「說明文字改為可以
        # 一直向下捲動,且跨越章節……因此全部內容(可搜尋)最後一個選項請去掉」):整本本來就接成
        # 一份連續的內容(`_help_render_all`),目錄是跳章與跟著捲動亮的索引,再放一篇「全部」
        # 就是同一份內容出現兩次。網頁版仍然是一篇一篇的,那一篇留給它用。
        pages = [page_ for page_ in help_text.help_pages(
                     DEVICE_NAMES[transcribe.predicted_device()], MAX_SPEAKERS)
                 if page_.label != help_text.EVERYTHING]
        # 灰槽:寬高都釘死、關掉傳播(同 segmented 那條規矩)。高度由**篇數**算,不量 widget
        # (`_wrap_width` 那條回授的規矩);槽的左右內距扣掉 `TOC_INSET`——那一圈畫在每一條
        # 的圖裡,加起來才是網頁版的 4。
        wrap = ttk.Frame(card, style="Toc.TFrame",
                         padding=(self.px(TOC_PAD) - self.px(TOC_INSET), self.px(TOC_PAD)))
        wrap.configure(width=self.px(TOC_W),
                       height=len(pages) * self.px(TOC_ROW) + 2 * self.px(TOC_PAD))
        wrap.pack_propagate(False)
        # 灰槽**兩端都對齊**:頂端跟搜尋列、底端跟內容框(使用者 2026-09-05 在三案裡選的 C)。
        # ⚠️ **靠 `sticky="ns"` 撐,不要去量內容框有多高**:量了就要跟著 `<Configure>` 重算,
        # 而那是一條回授(改高度會讓卡片重排、又觸發一次)——版面該由 geometry manager 算。
        # ⚠️ **上面那個 `height` 沒有變成死碼**:`rowspan=2` 的請求高度會被 grid 拿去當
        # 兩列的下限,所以視窗壓到最小時列會被撐開,選單不會被切掉幾條(而 `sticky` 只在
        # 空間**有餘**時才把它拉長)。⚠️ 水平不給 `e`:欄寬就是灰槽的寬,拉伸它會變形。
        wrap.grid(row=0, column=0, rowspan=2, sticky="nsw", padx=(0, self.px(SP_LG)))
        self._help_toc = {}
        self._help_cell = {}
        for page_ in pages:
            # 篇名開頭的 emoji 畫成彩色圖片(同分頁列與按鈕;2026-09-03 使用者:「所有
            # 頁面上的圖示 ICON」)。⚠️ **圖示與字塞在同一個 Label 裡**(`compound`),
            # 不像分頁列拆成兩個:選中那一篇的底要是一整塊,拆開就是兩塊中間一道縫。
            # 縫由圖右側的透明邊(`gap`)補,ttk 不在圖與字之間留空。畫不出來就原樣
            # 留在文字裡(單色)。
            icon, rest = split_icon(helpmd.plain(page_.label))
            photo = self._icon_image(icon, 10, gap=self.px(SP_XS)) if icon else None
            # 每一篇一條:沒選中是灰槽底色的平面 Frame,選中換成白膠囊底板(`_help_highlight`)。
            # 高度釘死在 `TOC_ROW`(膠囊 35 + 影子那 2px),裡面的東西用 `place`——它不參與
            # 尺寸傳播,格子的高度不會被內容撐開。
            cell = ttk.Frame(wrap, style="TocItem.TFrame", height=self.px(TOC_ROW))
            cell.pack(fill="x")
            cell.pack_propagate(False)
            # ⚠️ Label 的方角底色只能蓋在膠囊的**直邊段**:左右各縮進 `TOC_INSET + TOC_R_ON`
            # (影子那一圈 + 圓角),高度只到 `TOC_PILL_H`(底下那 2px 是影子)。少縮就是白色
            # 方角壓在圓角上——不當掉、不報錯,只有截圖看得到(同 segmented 那顆)。文字離
            # 膠囊左緣照網頁版 12(`TOC_TEXT_PAD`),扣掉已經縮進的圓角。
            side = self.px(TOC_INSET) + self.px(TOC_R_ON)
            lab = ttk.Label(cell, style="Toc.TLabel", anchor="w",
                            padding=(self.px(TOC_TEXT_PAD) - self.px(TOC_R_ON), 0),
                            **(dict(text=rest, image=photo, compound="left")
                               if photo is not None
                               else dict(text=helpmd.plain(page_.label))))
            lab.place(x=side, y=0, relwidth=1.0, width=-2 * side,
                      height=self.px(TOC_PILL_H))
            for w in (cell, lab):
                w.bind("<Button-1>", lambda _e, t=page_.label: self._show_help(t))
                w.configure(cursor="hand2")
            self._help_toc[page_.label] = (lab, page_)
            self._help_cell[page_.label] = cell

        # 搜尋列(2026-09-03 使用者:「全部內容 因為網頁才可以搜尋 AP 沒有這個功能」,兩案
        # 裡選了做搜尋、「(可搜尋)」留著):搜的是**現在顯示的這一篇**——在「全部內容」上就是
        # 整本,與網頁版 Ctrl+F 的行為一樣。打字即標(淡黃)、Enter／「下一個」逐筆跳(深黃、
        # 捲到看得見)、Shift+Enter／「上一個」倒著跳、Esc 清掉;Ctrl+F 在這一頁把游標送進框。
        find = ttk.Frame(card, style="CardBody.TFrame")
        find.grid(row=0, column=1, sticky="ew", pady=(0, self.px(SP_SM)))
        find.columnconfigure(1, weight=1)
        ttk.Label(find, text="搜尋內文", style="Field.TLabel").grid(
            row=0, column=0, padx=(0, self.px(SP_SM)))
        self._help_query = tk.StringVar()
        self._help_pos: int | None = None       # 目前跳到第幾筆(None = 還沒跳)
        entry = ttk.Entry(find, textvariable=self._help_query, style="Tall.TEntry")
        entry.grid(row=0, column=1, sticky="ew")
        self._help_entry = entry
        HandButton(find, text="上一個", style="Small.TButton",
                   command=lambda: self._help_find(-1)).grid(
            row=0, column=2, padx=(self.px(SP_SM), 0))
        HandButton(find, text="下一個", style="Small.TButton",
                   command=lambda: self._help_find(+1)).grid(
            row=0, column=3, padx=(self.px(SP_XS), 0))
        self._help_hits = ttk.Label(find, text="", style="Hint.TLabel")
        self._help_hits.grid(row=0, column=4, padx=(self.px(SP_SM), 0))
        entry.bind("<Return>", lambda _e: self._help_find(+1))
        entry.bind("<Shift-Return>", lambda _e: self._help_find(-1))
        entry.bind("<Escape>", lambda _e: self._help_query.set(""))
        self._help_query.trace_add("write", lambda *_a: self._help_mark())
        # ⚠️ `bind_all` 是整個視窗的:handler 自己看現在是不是說明頁,其他頁按了不動。
        self.bind_all("<Control-f>", self._help_focus_find)

        wrap = ttk.Frame(card, style="Sunken.TFrame", padding=self.px(SP_XS))
        wrap.grid(row=1, column=1, sticky="nsew")
        body = tk.Text(wrap, font=(self.fam, HELP_PT[""]), relief="flat", bd=0,
                       highlightthickness=0, wrap="word", cursor="arrow",
                       bg=self.pal["field"], fg=self.pal["ink"],
                       selectbackground=self.pal["row_sel"],
                       padx=self.px(SP_MD), pady=self.px(SP_SM),
                       spacing2=self.px(HELP_LINE_GAP))
        bar = ttk.Scrollbar(wrap, orient="vertical", command=body.yview)
        # 捲到哪一章,目錄就亮哪一章(`_help_spy`):接在捲軸的回呼上,滾輪、拖捲軸、搜尋
        # 跳到命中處都會經過這裡。
        body.configure(yscrollcommand=lambda first, last: (bar.set(first, last),
                                                           self._help_spy(first, last)))
        bar.pack(side="right", fill="y")
        body.pack(side="left", fill="both", expand=True)
        self._help_body = body
        body.bind("<<Copy>>", self._help_copy)   # 複製時剝掉斷點用的空白(見 `_help_run`)
        self._help_photo = None          # ⚠️ 圖片的參照要自己留著,否則被 GC 掉就不見了
        for name, opts in (
            # 字級照網頁版量的(@150% 墨跡高:大標 33、小標 23~24、內文 21 實體 px;Tk 17pt
            # 的墨跡約 31、12pt 約 22、10pt 約 21)。大標的 spacing1 是整頁最上面那段留白;
            # 標題底下、段落之間的距離都不在這裡,在 `gap_*`(見 `HELP_GAP`)。
            # ⚠️ 字級只有 `HELP_PT` 一份,`_help_insert` 畫 emoji 也從它取。
            ("h2", dict(font=(self.fam, HELP_PT["h2"], "bold"), spacing1=self.px(SP_MD))),
            ("h3", dict(font=(self.fam, HELP_PT["h3"], "bold"))),
            *((f"gap_{kind}", dict(spacing3=self.px(gap)))
              for kind, gap in HELP_GAP.items()),
            # 搜尋的標記:全部命中淡黃、目前那一筆深黃。⚠️ 要排在 `code` 後面——後建的
            # tag 優先,命中落在行內 code 上時才蓋得過它的灰底。
            ("find", dict(background=(FIND_HIT_DARK if self._dark() else FIND_HIT_LIGHT))),
            ("find_now", dict(background=(FIND_NOW_DARK if self._dark()
                                          else FIND_NOW_LIGHT))),
            # 中文字之間的斷點:看不見的空白(見 `_help_run`)。⚠️ 是 `elide` 不是 1px 字型:
            # 1px 的空白每個字多 1px(整行 5%),elide 是 0px 而且 Tk 照樣在那裡斷行。
            ("zw", dict(elide=True)),
            # ⚠️ 內文那一級的字級一律取 `HELP_PT[""]`、等寬字型取 `MONO_FAMILY`:這幾個
            # tag 與 Text 的基底字型是**同一級**,寫成各自的字面值就是 M495 那顆蟲的形狀
            # (當時是「字型表改了、畫圖那段沒跟」),下一次調內文字級會變成「內文換了、
            # 行內粗體與表格沒跟」。
            ("bold", dict(font=(self.fam, HELP_PT[""], "bold"))),
            ("code", dict(font=(MONO_FAMILY, HELP_PT[""]), background=self.pal["btn_off"])),
            ("block", dict(font=(MONO_FAMILY, HELP_PT[""]), background=self.pal["btn_off"],
                           lmargin1=self.px(SP_MD), lmargin2=self.px(SP_MD),
                           spacing1=self.px(SP_XS), spacing3=self.px(SP_XS))),
            ("bullet", dict(lmargin1=self.px(SP_SM), lmargin2=self.px(SP_LG))),
            ("th", dict(font=(self.fam, HELP_PT[""], "bold"))),
        ):
            body.tag_configure(name, **opts)
        # ⚠️ 位置那一格**可以是 None**:`_show_help` 先卡住(還沒量到位置)再捲,
        # `_help_spy` 那道保護存在的理由就是它(見那兩支的時序那段)。
        self._help_pin: tuple[str, float | None] | None = None
        self._help_current = ""
        self._help_render_all()
        self._show_help(next(iter(self._help_toc)))
        return page

    def _help_render_all(self) -> None:
        r"""整本畫進內容區——章節依序接起來,記下每一章標題在第幾行(`_help_anchor`)。

        (2026-09-03 使用者:「說明文字改為可以一直向下捲動,且跨越章節,跨越時左邊選單可以
        自動移動正確的章節,使用者可以直接按下左邊的選單跳到有興趣的章節」。)先前是一篇一篇
        換內容,與網頁版一樣;現在**刻意與網頁版不同**:目錄是索引不是分頁。

        ⚠️ **先畫第一章就交差,其餘九章排到畫完第一幀之後**(2026-09-05 使用者選定;先前是
        一口氣畫完整本,而那是「第一次點使用說明有點慢」的**全部**成因——538ms 裡 439 在這
        一支)。⚠️ **那 439ms 沒有便宜的優化**,四個方向都量過(絕對索引無差、`insert` 交替
        形式慢 9 倍、渲染時把 Text 藏起來更慢、批次 `tag_add` 無差):成本是三萬個 elide
        range 讓**可見的** Text 每次重算換行,而那正是中文每字可斷所需要的(同樣三萬個 range
        在看不見的 Text 上只要 23ms)。所以改的是**什麼時候畫**,不是怎麼畫。
        ⚠️ `state` 要先開再關:唯讀是靠 `disabled`,而 `disabled` 的 Text 連程式自己都寫
        不進去(不是只有使用者打不了字)。⚠️ 錨點要在**插入之前**問 `end-1c`:那時它就是
        下一章標題會落在的那一行(每一章結尾都有 `\n`)。"""
        body = self._help_body
        body.configure(state="normal")
        body.delete("1.0", "end")
        self._help_anchor: dict[str, int] = {}
        self._help_todo = list(self._help_toc.items())
        self._help_render_one()
        body.configure(state="disabled")
        if self._help_todo:
            self.after_idle(self._help_kick_rest)

    def _help_kick_rest(self) -> None:
        r"""把畫面**先追上**,再排補完那一刀。

        ⚠️ **這一層不是多餘的**(2026-09-05 量事件順序才發現):Tcl 的迴圈是「事件 → 計時器
        → idle」,而 Tk 的重繪是 idle——所以 `after(1)` 排的補完**永遠搶在畫面更新前面**,
        分段等於沒做(量到:`_show` 86ms 回來、補完 87→310ms、畫面 310ms 才畫好,跟不分段
        一樣)。⚠️ **`after_idle` 單獨也不行**:它在建頁時就排進佇列,而切頁的重繪 idle 是
        之後 `pack` 才排的,先排的先跑。所以這裡自己 `update_idletasks()` 把佇列裡**剩下的**
        (含那次重繪)做完,畫面出來了才交棒給計時器。
        ⚠️ **不逐章補**:一章一刀的話捲軸滑塊會一格一格縮,那正是使用者說的「眼睛看得出來
        在產生」;寧可一刀做完(約 220ms),那時他正在讀第一章。"""
        if not getattr(self, "_help_todo", None) or not self.winfo_exists():
            return
        self.update_idletasks()
        self.after(1, self._help_render_rest)

    def _help_render_one(self) -> None:
        """畫掉待畫清單最前面那一章(`state` 與迴圈由呼叫端管)。"""
        label, (_lab, page) = self._help_todo.pop(0)
        body = self._help_body
        self._help_anchor[label] = int(str(body.index("end-1c")).split(".")[0])
        self._render_help(page.top)
        if page.image:
            self._insert_privacy_image()
            self._render_help(page.bottom)

    def _help_render_rest(self) -> None:
        r"""把還沒畫的章節補完:空檔時自己來,或被跳章/搜尋**拉著提前做完**。

        ⚠️ **每一條會用到「後面那幾章」的路都要先叫它**(`_show_help`、`_help_mark`):
        錨點與內文在補完之前只有第一章,不叫就是點了目錄跳不動、搜尋搜不到後半本——而那
        只有在補完前的那 250ms 內點下去才會發生,測試不特意搶那個時機就永遠是綠的。
        ⚠️ **視窗關掉時 `after_idle` 可能已經排著了**:那時 widget 都沒了,碰它就是
        `TclError` 被 Tk 吞進紀錄檔。"""
        # ⚠️ `getattr`:`_help_mark` 掛在搜尋框的 trace 上,而那是**建頁的過程中**就接好的
        if not getattr(self, "_help_todo", None) or not self._help_body.winfo_exists():
            return
        body = self._help_body
        body.configure(state="normal")
        while self._help_todo:
            self._help_render_one()
        body.configure(state="disabled")

    def _show_help(self, label: str) -> None:
        r"""目錄點了某一章:捲到那一章的標題、目錄亮那一章。

        ⚠️ **靠近文末的章捲不到頂**(底下沒有那麼多內容),`@0,0` 會落在前一章——這時目錄
        要聽點的那一下、不聽捲動:記住捲到的位置(`_help_pin`),`_help_spy` 看到位置沒變
        就不動;使用者一捲動位置就變了,回到「捲到哪亮哪」。

        ⚠️ **`_help_pin` 要在 `yview` 之前就設好**:Tk 的 `yscrollcommand` 是在**那一次
        `update_idletasks()` 當中**回呼的,而且視野沒變就不再回呼(實測:`yview` 之後
        還沒回呼、`update_idletasks()` 當中回呼一次、再叫一次就不回呼了)。把 pin 設在
        後面的話 `_help_spy` 拿到的永遠是上一次的值、必然不相等,於是它清掉 pin 並用
        `_help_chapter_at` 重算、**覆蓋掉剛設好的高亮**——上面那道保護等於從來沒生效。
        先設 pin 就沒有這個時序問題;位置用 `_help_anchor` 那一行的實際捲動比例,所以
        仍然要 `update_idletasks()` 之後才量得到,那一份留在後面**校正**。"""
        body = self._help_body
        # ⚠️ **只在那一章還沒有錨點時才拉**:建頁的最後一行就是 `_show_help(第一章)`,
        # 無條件叫的話整本又在使用者等的那一段裡同步畫完了(2026-09-05 量到 314ms 才發現)。
        if label not in self._help_anchor:
            self._help_render_rest()
        self._help_highlight(label)
        self._help_pin = (label, None)          # 先卡住,擋掉 yview 引發的那一次回呼
        body.yview(f"{self._help_anchor[label]}.0")
        body.update_idletasks()
        self._help_pin = (label, float(body.yview()[0]))

    def _help_highlight(self, label: str) -> None:
        """目錄上亮某一章(只換樣式,不捲)。"""
        self._help_current = label
        for name, (lab, _) in self._help_toc.items():
            on = name == label
            lab.configure(style="TocOn.TLabel" if on else "Toc.TLabel")
            # 格子跟著換:選中的穿白膠囊底板(帶影子),其餘是灰槽底色的平面
            self._help_cell[name].configure(style="TocOn.TFrame" if on else "TocItem.TFrame")

    def _help_chapter_at(self, top_line: int, at_bottom: bool = False) -> str:
        """內容區頂端在第 `top_line` 行時,算是在哪一章:標題在那一行或更上面的最後一章;
        捲到底就是最後一章(它太短、標題永遠到不了頂端也一樣)。"""
        labels = list(self._help_anchor)
        if at_bottom:
            return labels[-1]
        current = labels[0]
        for label, line in self._help_anchor.items():
            if line <= top_line:
                current = label
        return current

    def _help_spy(self, first=None, last=None, *, top_line: int | None = None) -> None:
        """捲軸的回呼:內容區捲到哪一章,目錄就亮哪一章(`top_line` 給測試用,平常自己問)。"""
        body = self._help_body
        pin = self._help_pin
        if pin is not None:
            # `pin[1] is None` = `_show_help` 正在捲、位置還沒量到(見那支的時序那段):
            # 這一次回呼就是它引發的,一律不動目錄。
            if pin[1] is None:
                return
            if first is not None and abs(float(first) - pin[1]) < 1e-9:
                return                            # 還停在點目錄捲到的位置
            self._help_pin = None
        if top_line is None:
            top_line = int(str(body.index("@0,0")).split(".")[0])
        at_bottom = last is not None and float(last) >= 1.0
        self._help_highlight(self._help_chapter_at(top_line, at_bottom))

    # ---- 使用說明:搜尋 ------------------------------------------------------ #
    def _help_mark(self) -> None:
        r"""打字即標:把現在這一篇裡每一個符合的地方標成淡黃;目前那一筆的深黃由 `_help_find`。

        不分大小寫(`nocase`);長度用 `count` 拿,不拿 `len()`——Text 的 index 算的是
        字元數,而嵌進去的圖也算一個字元。⚠️ Text 是唯讀(`disabled`)的,`tag_add` 照樣
        做得到,只有 insert/delete 被擋。"""
        body = getattr(self, "_help_body", None)
        if body is None:
            return
        body.tag_remove("find", "1.0", "end")
        body.tag_remove("find_now", "1.0", "end")
        self._help_pos = None
        query = self._help_query.get()
        n = 0
        if query:
            self._help_render_rest()   # 搜的是整本,補完之前後半本還不在裡面
            count = tk.IntVar(self)
            idx = "1.0"
            while True:
                idx = body.search(query, idx, stopindex="end", nocase=True, count=count)
                if not idx or not count.get():
                    break
                end = f"{idx}+{count.get()}c"
                body.tag_add("find", idx, end)
                idx = end
                n += 1
        self._help_hits.configure(text=("" if not query else f"共 {n} 筆" if n else "找不到"))

    def _help_find(self, step: int) -> None:
        """Enter／「下一個」:跳到下一筆(`step=-1` 上一筆),捲到看得見、標成深黃;到底就繞回。"""
        body = self._help_body
        ranges = body.tag_ranges("find")
        hits = [(ranges[i], ranges[i + 1]) for i in range(0, len(ranges), 2)]
        if not hits:
            return
        if self._help_pos is None:
            pos = 0 if step > 0 else len(hits) - 1
        else:
            pos = (self._help_pos + step) % len(hits)
        self._help_pos = pos
        start, end = hits[pos]
        body.tag_remove("find_now", "1.0", "end")
        body.tag_add("find_now", start, end)
        body.see(start)
        self._help_hits.configure(text=f"第 {pos + 1} / {len(hits)} 筆")

    def _help_focus_find(self, _event=None) -> None:
        """Ctrl+F:只在說明頁上有作用,把游標送進搜尋框、全選,直接打字就取代。"""
        if self.tab != "help":
            return
        self._help_entry.focus_set()
        self._help_entry.select_range(0, "end")

    def _help_copy(self, _event=None):
        """Ctrl+C:複製選取的字,**不含**斷點用的那些看不見的空白(`_help_run`)。

        Tk 自己的 `<<Copy>>` 連 elided 的字元一起抄,貼出來就是「逐 字 稿」;`get` 的
        `-displaychars` 只拿畫出來的字。"""
        body = self._help_body
        try:
            start, end = body.index("sel.first"), body.index("sel.last")
        except tk.TclError:                       # 沒有選取
            return "break"
        text = body.tk.call(body._w, "get", "-displaychars", "--", start, end)
        self.clipboard_clear()
        self.clipboard_append(text)
        return "break"

    def _render_help(self, md: str) -> None:
        """把一段 Markdown 畫進內容區(支援的七種語法見 `helpmd`)。"""
        body = self._help_body
        blocks = list(helpmd.parse(md))
        for block, nxt in zip(blocks, blocks[1:] + [None]):
            if block.kind == "code":
                body.insert("end", block.text + "\n", "block")
            elif block.kind == "table":
                self._render_table(block)
            else:
                tag = {"heading": f"h{block.level}", "bullet": "bullet"}.get(
                    block.kind, "")
                # 段距看**下一塊**是什麼(`HELP_GAP` 上面那段);掛在整塊上——Tk 的行距
                # 選項讀的是該行第一個字(或圖)的 tag,只掛尾巴等於沒掛。
                gap = self._help_gap(block, nxt)
                if block.kind == "bullet":
                    body.insert("end", "・", (tag, gap))
                for span in block.spans:
                    tags = tuple(t for t in (tag, gap, span.style) if t)
                    self._help_insert(span.text, tags)
            body.insert("end", "\n")

    @staticmethod
    def _help_gap(block, nxt) -> str:
        """這一塊底下要留哪一種段距(tag 名,對應 `HELP_GAP`):標題各自一個,其餘看下一塊。"""
        if block.kind == "heading":
            return "gap_h2" if block.level == 2 else "gap_h3"
        kind = getattr(nxt, "kind", None)
        return {"bullet": "gap_bullet", "heading": "gap_heading"}.get(kind, "gap_para")

    def _help_insert(self, text: str, tags: tuple) -> None:
        r"""把一段內文塞進說明區,**emoji 換成彩色圖片**。

        (2026-09-03 使用者:「所有頁面上的圖示 ICON……都請修正」。)`tk.Text` 畫 emoji
        同樣是單色字形,但它收得下行內圖片(`image_create`),所以逐字掃:字型裡有的
        字元換成圖、其餘原樣插入。字級跟著標題層級走(`HELP_PT`,**與字型表同一份**),
        圖才與旁邊的字同一個量級;圖用 `align="center"` 對齊行的中線,照瀏覽器畫 emoji
        的位置。⚠️ 這裡**不可以自己寫一份字級**:2026-09-03 兩邊各寫一份而字型表改了、
        這裡沒改,十篇篇名的圖示全部只有旁邊的字四分之三高。
        ⚠️ 換的只有 Segoe UI Emoji 真的有的字(`_icon_pil` 對照 .notdef):「→」「─」
        碼位在範圍裡、字型裡沒有,留在文字裡。
        ⚠️ **傳進來的字要先經過 `helpmd.spans`**(兩個呼叫端都是):VS16(`U+FE0F`)在
        Tk 裡是一個 30px 的方塊,而剝掉它是 `helpmd.plain` 的事、`test_helpmd` 守著。
        先前這裡自己又逐字剝了一次,兩層各自宣稱負責、而其中一份是死碼(2026-09-03
        code review 清掉)——要新增呼叫端的話,原文一樣得先過 `spans`。"""
        body = self._help_body
        pt = HELP_PT["h2"] if "h2" in tags else HELP_PT["h3"] if "h3" in tags else HELP_PT[""]
        run = ""
        for ch in text:
            photo = self._icon_image(ch, pt) if is_symbol(ch) else None
            if photo is None:
                run += ch
                continue
            if run:
                self._help_run(run, tags)
                run = ""
            body.image_create("end", image=photo, align="center")
            # ⚠️ **圖也要掛同一組 tag**:`spacing1`(標題上方那段留白)看的是**該行第一個
            # 東西**的 tag,而標題正好都以 emoji 開頭——圖沒掛 tag 的話,標題與上一段之間
            # 的距離當場消失(實拍看到的)。圖插在 `end` 之前,位置是 `end-2c`(`end-1c`
            # 是 Text 自己那個永遠存在的換行)。
            for t in tags:
                body.tag_add(t, "end-2c")
        if run:
            self._help_run(run, tags)

    def _help_run(self, run: str, tags: tuple) -> None:
        r"""插入一段文字,並在中文字與字之間放**可斷行的空白**(`breakable_after`)。

        (使用者 2026-09-03:「工具資料夾裡的「logs」為何後面就斷字?」)⚠️ `tk.Text` 的
        `wrap="word"` **只認 ASCII 空白**(Tk 原始碼逐位元組 `isspace`):一整串沒有空白的
        中文是一個「單字」,那一行塞不下它就整個搬到下一行、斷在更前面的某個空白——「logs」
        前後那兩個空白是 Markdown 粗體語法留的,於是斷在那裡。`wrap="char"` 又會把
        Markdown、PowerPoint 這種英文單字從中間拆開,瀏覽器不會。
        所以照瀏覽器的規則自己給斷點:中文字之間、中文與英文的交界放一個 **elided** 的
        空白——看不見、0px,但 Tk 的換行認得(實測),`search` 預設又略過它們,所以搜
        「逐字稿」照樣找得到「逐 字 稿」;標點避頭尾見 `NO_BREAK_BEFORE`／`NO_BREAK_AFTER`。
        ⚠️ 複製會把這些空白一起帶走,`<<Copy>>` 另外接(`_help_copy`)。⚠️ 一段一次插入、
        `tag add` 一次掛所有空白,不逐字插(整本兩萬字)。⚠️ 段落開頭也要看前一個字:粗體
        與一般字是兩次插入,交界處不看就是「把**聲音**」之間永遠不能斷。"""
        body = self._help_body
        start = body.index("end-1c")
        prev = body.get(f"{start}-1c", start)
        parts, spots, at = [], [], 0
        if prev and breakable_after(prev, run[0]):
            parts.append(" ")
            spots.append(0)
            at = 1
        for a, b in zip(run, run[1:] + "\n"):
            parts.append(a)
            at += 1
            if breakable_after(a, b):
                parts.append(" ")
                spots.append(at)
                at += 1
        body.insert("end", "".join(parts), tags)
        if spots:
            body.tag_add("zw", *(f"{start}+{i + k}c" for i in spots for k in (0, 1)))

    def _render_table(self, block) -> None:
        r"""表格:用 Text 的定位點對欄,不是補空白。

        ⚠️ **補空白對不齊**:中文是全形、數字英文是半形,而這裡的字型不是等寬的
        ——同樣的字數畫出來寬度不同。定位點是照**量出來的像素**排的。"""
        body = self._help_body
        # ⚠️ 量寬的字型要與畫表頭的 `th` tag 同一級(見那張 tag 表):寫成字面值的話,
        # 內文字級一調,欄寬還是照舊字級量的,欄位當場對不齊。
        font = tkfont.Font(font=(self.fam, HELP_PT[""], "bold"))
        widths = [max(font.measure("".join(s.text for s in row[i]))
                      for row in block.rows if i < len(row))
                  for i in range(max(len(r) for r in block.rows))]
        stops, x = [], self.px(SP_SM)
        for w in widths[:-1]:
            x += w + self.px(SP_LG)
            stops.append(x)
        tag = f"tbl{body.index('end')}"
        body.tag_configure(tag, tabs=tuple(stops), lmargin1=self.px(SP_SM),
                           lmargin2=self.px(SP_SM))
        for n, row in enumerate(block.rows):
            for i, cell in enumerate(row):
                if i:
                    body.insert("end", "\t", tag)
                for span in cell:
                    extra = "th" if n == 0 else span.style
                    self._help_insert(span.text, tuple(t for t in (tag, extra) if t))
            body.insert("end", "\n", tag)

    def _insert_privacy_image(self) -> None:
        """那張 Claude 隱私設定截圖。⚠️ 找不到就**整張跳過**,不放破圖——它只是
        佐證,而說明的文字自己講得完整。"""
        if not PRIVACY_IMG.is_file():
            return
        try:
            from PIL import Image, ImageTk

            img = Image.open(PRIVACY_IMG)
            wide = self.px(560)
            if img.width > wide:
                img = img.resize((wide, round(img.height * wide / img.width)),
                                 Image.LANCZOS)
            self._help_photo = ImageTk.PhotoImage(img)
            self._help_body.image_create("end", image=self._help_photo)
            self._help_body.insert("end", "\n\n")
        except Exception:                # 純佐證,載不起來不該讓整篇打不開
            logger.debug("隱私截圖載入失敗", exc_info=True)

    # ---- 關視窗 ---------------------------------------------------------- #
    def _on_close(self) -> None:
        r"""按下標題列的 X。

        ⚠️ **工作進行中要先問**(使用者 2026-08-30 選案「攔下來問,比照網頁版」,
        與網頁版 `ui_style.UNLOAD_GUARD_HEAD` 那個 beforeunload 是同一個政策):
        一次手滑就中斷一場開了兩小時的會,而**錄音不能重來**。
        ⚠️ **問完之後一定要關得掉**:`_shutdown` 每一步各自 try,收不乾淨也照關
        ——一個關不掉的視窗比沒收乾淨更糟(而且那正是「按了沒反應」那一族)。
        ⚠️ **這裡不負責收子行程**:2026-08-30 實測過,父行程一死,講者分析子行程
        從 stdin 收到 EOF 就自己走了(`diarproc` 的 worker 讀 stdin)。真正會漏的
        是**行程還活著的時候**沒 close,那條在 `_rec_stop` 的 `finally` 裡。"""
        question = self._close_question()
        if question:
            try:
                if not self._ask_close(question):
                    return
            except Exception:
                # ⚠️ 對話框自己壞掉時**照關**:使用者按了 X 就是要關,而音檔本來
                # 就是邊錄邊落地的(`record._WavWriter` 每 2 秒回填標頭),不會因
                # 為少問一句就毀掉。卡在這裡不關才是更糟的那一邊。
                logger.exception("關閉前的確認框開不起來,直接關閉")
        self._shutdown()
        self.destroy()

    def _close_question(self) -> str:
        """工作進行中要問的那句話(三條路徑都閒著就回空字串)。

        ⚠️ **錄音與轉檔要分開講**:代價不一樣。錄音是「這一場沒了、而且不能重來」,
        轉檔是「時間白花了、檔案還在」——講同一句話會讓人用錯的心態按下去。
        ⚠️ **錄音的收尾是第三種,不能混進前兩種**(2026-09-04 使用者指定補上):按過
        「停止並轉檔」之後 `rec` 與 `run` 兩個旗標**同時**舉著(見 `_rec_stop`),而
        那時收音早就停了——照舊那句講就是三句話全錯:「還在錄音中」(沒有)、「已錄
        62:05」(數字還跟著收尾一路往上跳)、「現在關閉會停止收音」(沒有收音可停)。
        真正的代價是**這一趟的逐字稿沒了,而聲音檔還在**。"""
        if self._busy["rec"]:
            # 收尾中就算到收音停止那一刻為止(見 `_rec_t1`)
            secs = int((self._rec_t1 or time.monotonic()) - self._rec_t0)
            clock = f"{secs // 60:02d}:{secs % 60:02d}"
            if self._rec_t1:
                return (f"錄音已經停止(共 {clock}),正在轉成逐字稿。\n\n"
                        "現在關閉會中斷,這一趟的逐字稿不會產生。聲音檔留在 recordings "
                        "資料夾,之後要用「轉錄音檔」自己轉一次。\n\n"
                        "確定要關閉嗎?")
            return (f"還在錄音中(已錄 {clock})。\n\n"
                    "現在關閉會停止收音。已經錄到的聲音會存進 recordings 資料夾,"
                    "但不會轉成逐字稿——之後要用「轉錄音檔」自己轉一次。\n\n"
                    "確定要關閉嗎?")
        if self._busy["run"] or self._busy["doc"]:
            return ("還在轉檔中,現在關閉會中斷,已經跑掉的時間拿不回來。\n\n"
                    "確定要關閉嗎?")
        return ""

    def _ask_close(self, question: str) -> bool:
        r"""關閉前的確認框。按「關閉」回 True,其餘一律 False。

        ⚠️ **自己畫一個,不用 `tkinter.messagebox`**:原生對話框的按鈕字走
        `::tk::mc`,而這台機器的 Tcl locale 雖然是 `zh_tw`、msgcat 裡卻**沒有**翻譯
        (2026-08-30 實測:`::tk::mc OK` 原樣回 `OK`,把翻譯 `mcset` 進 `::tk` 也
        不生效)——那會在使用者看得見的地方冒出英文按鈕,違反 spec §8。
        ⚠️ **本 repo 先前一個 modal 都沒有**(既有的確認都是頁內問句,像「名單與
        聲紋」那個),這裡破例是因為**標題列的 X 沒有頁內等價物**:它必須擋在關閉
        之前,而頁內問句攔不住視窗。
        ⚠️ **預設焦點在「取消」**:Enter 是最容易被順手按下去的鍵,而這個對話框問
        的是一件不能反悔的事。"""
        win = tk.Toplevel(self)
        win.title(APP_TITLE)
        win.configure(background=self.pal["page"])
        win.resizable(False, False)
        win.transient(self)          # 跟著主視窗,不在工作列另外佔一格
        answer = {"go": False}

        def close_now() -> None:
            answer["go"] = True
            win.destroy()

        body = ttk.Frame(win, style="Card.TFrame", padding=self.px(CARD_PAD))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=question, style="Status.TLabel", justify="left",
                  wraplength=self.px(360)).pack(anchor="w")
        row = ttk.Frame(body, style="CardBody.TFrame")
        row.pack(anchor="e", pady=(self.px(SP_LG), 0))
        HandButton(row, text="關閉", style=skin.CTA_STYLE,
                   command=close_now).pack(side="left")
        cancel_btn = HandButton(row, text="取消", style="Small.TButton",
                                command=win.destroy)
        cancel_btn.pack(side="left", padx=(self.px(SP_SM), 0))

        # 對話框自己的 X 與 Esc 都當成「取消」——它們的意思是「我不要做這件事」
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        win.bind("<Escape>", lambda _e: win.destroy())
        win.update_idletasks()       # 先算出真實大小才擺得準
        x = self.winfo_rootx() + (self.winfo_width() - win.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - win.winfo_height()) // 3
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        cancel_btn.focus_set()
        win.grab_set()               # ⚠️ 沒有 grab 的話主視窗還點得動
        win.wait_window()
        return answer["go"]

    def _shutdown(self) -> None:
        r"""關閉前把還在跑的東西收乾淨。

        ⚠️ **每一步各自 try**:這一支是在「使用者已經決定要關」之後跑的,任何一步
        丟例外都會讓視窗關不掉,而那比沒收乾淨嚴重得多。
        ⚠️ **`recorder.stop()` 要叫**:收音的收尾(補零/修剪、標頭最後一次回填、
        那行「錄音收檔」的紀錄)都在裡面。不叫也不會壞掉(標頭每 2 秒回填一次,
        檔案任何時刻都是合法 WAV),但會白白丟掉最後那不到兩秒。"""
        cancel.request()             # 轉檔那兩條會在下一個段落邊界停下來
        recorder, live = self._rec.get("recorder"), self._rec.get("live")
        if recorder is not None:
            try:
                recorder.stop()
            except Exception:
                logger.exception("關閉時停止收音失敗")
        if live is not None:
            try:
                live.close()
            except Exception:
                logger.exception("關閉時收拾增量轉錄失敗")
        try:
            power.stay_awake_end()
        except Exception:
            logger.debug("關閉時解除防睡眠失敗", exc_info=True)
        # ⚠️ **工作列要自己清**:視窗關掉那一格本來就跟著不見,但錄音中關閉時
        # Windows 有機會先把進度畫到別的地方去(同 `MP4-2-SRT` 關窗前那一句)。
        try:
            self._taskbar_end()
        except Exception:
            logger.debug("關閉時清除工作列進度失敗", exc_info=True)

    # ---- 切頁 ----------------------------------------------------------- #
    def _show(self, key: str) -> None:
        self.tab = key
        for (k, _icon, _text), entry in zip(
                [(t[0], t[1], t[2]) for t in TABS], self._nav.values()):
            self._paint_tab(entry, _icon, 11, k == key,
                            "NavOn.TLabel", "Nav.TLabel")
        if key == "audio":
            # ⚠️ 子分頁列要塞在內容區**之前**,所以先把內容區收起來再 pack 回去:
            # `pack` 的順序就是由上而下的順序,而子分頁列是後建的。
            self._body.pack_forget()
            # ⚠️ **這兩個間距是量網頁版得到的**(實體 px @150%):頂層那條灰線的
            # 底(386)到子分頁膠囊的頂(417)是 **31**,子分頁那條線的底(464)到卡片
            # 頂(501)是 **37**。⚠️ 先前給的是 `SP_SM`(12 實體)與 `SP_MD`(18),
            # 兩排分頁與卡片因此擠成一團——使用者 2026-09-01 說的「膠囊應該下移」。
            self._subbar.pack(fill="x", pady=(self.px(SP_LG + SP_XS), 0))
            self._pack_body(SP_XL)
            self._show_sub(self.subtab)
            return
        self._subbar.pack_forget()
        # ⚠️ **也要 pack 回來**(2026-09-03 補的):先前只有「聲音→MD」那條路重新 pack,切回其他
        # 分頁時內容區還留著那一路的上 pady(`SP_XL`)——去過聲音頁之後,其他頁的分頁列到卡片
        # 就變成 36 實體 px 而不是 18,而且只有「先點過聲音頁」才會發生,沒人猜得到成因。
        self._pack_body(SP_MD)
        self._raise(key)

    def _pack_body(self, top: int) -> None:
        r"""內容區 pack 回殼裡:上 pady 看有沒有子分頁列(`top`),**下 pady 一律 `PAGE_PAD`**。

        底邊那段灰與左右兩側同一個數(2026-09-03 使用者:「下面灰色的高度,要跟左右兩邊相同」;
        量到底邊 18 實體 px、兩側 30)。⚠️ 撐到底的卡片(詞表兩頁、使用說明、文件頁右欄)都是
        `fill="both"`,裡面的框再撐滿卡片,**框的行數決定不了這一段**——只有這裡的下 pady。
        ⚠️ 三條路(建視窗、切到聲音頁、切到其他頁)都走這一支,底邊才不會各自漂。"""
        self._body.pack(fill="both", expand=True, pady=(self.px(top), self.px(PAGE_PAD)))

    def _show_sub(self, key: str) -> None:
        self.subtab = key
        for (k, _icon, _text), entry in zip(
                [(t[0], t[1], t[2]) for t in SUBTABS], self._subnav.values()):
            self._paint_tab(entry, _icon, 10, k == key,
                            "SubOn.TLabel", "Sub.TLabel")
        self._raise(key)

    def _raise(self, key: str) -> None:
        want = self._page(key)           # 還沒建過就在這裡建(見 `_page`)
        for k, page in self._pages.items():
            if page is want:
                page.pack(fill="both", expand=True)
            else:
                page.pack_forget()


def main() -> None:
    r"""視窗的進站。`啟動.vbs` 跑的是這一支。"""
    # ⚠️ **已經開著就不要再開第二個**:兩個實例會搶同一顆 GPU 與同一份聲紋庫,而畫
    # 面上只看得到各自那份進度。找得到既有的就把它叫到前景——什麼都不做的話,使用者
    # 會以為程式壞了、再點兩三次。整條政策(含「搶輸了卻找不到那個視窗就照常開」那
    # 一格)在共用包裡。
    # ⚠️ **擋在最前面**:紀錄檔還沒開、視窗還沒建,退出時什麼都不必收;而且離開碼是
    # **0**,啟動器因此不跳訊息框、也不會觸發它那道「非正常結束又不到 5 秒就用 uv 再
    # 跑一次」的退路。
    if not winui.single_instance_or_raise():
        return

    # ⚠️ 這兩支都必須在建立第一個 Tk 視窗**之前**(DPI 決定 Windows 用不用真實像素
    # 畫、AppUserModelID 決定工作列拿誰的圖示)。
    winui.enable_dpi_awareness()
    winui.set_app_user_model_id()

    # 紀錄檔:黑視窗被藏起來之後,這是唯一看得到啟動過程的地方。
    # ⚠️ **走本專案的 `filelog`,不是 `winkit.filelog`**:兩份都寫進 `<repo>\logs`、
    # 檔名格式也一樣(`2026-08-29_170028.log`),所以同一秒內開的兩個寫入者會**疊在
    # 同一個檔上**——而那沒有任何錯誤訊息,只是紀錄檔的內容交錯。另外二十支模組用的
    # 都是這一份,視窗這一支沒有理由不一樣。⚠️ 哪一份最後成為唯一真值還沒定案
    # (winkit 的 `CLAUDE.md` 記著),在那之前**不要把這裡換過去**。
    filelog.start()
    # ⚠️ **硬退出殘留的暫存目錄要在這裡清**(同網頁版 `app._launch` 的位置):關視窗
    # /當機來不及自清的 `meeting-scribe-*` 留在系統暫存裡,沒有人會回頭刪它。
    # ⚠️ **先前這一支完全沒清**——網頁版清、命令列的 `doccli` 清,就原生視窗不清,
    # 而它正要變成唯一的入口(到那時就再也沒有人清了)。
    # 局部 import:本檔刻意不在模組層背 `pipeline`(見檔頭)。
    from meeting_scribe import pipeline
    pipeline.cleanup_stale_temp()
    # ⚠️ **交付版的 `data\` 是空的**:出貨帶的是 `data-default\`(種子檔),第一次
    # 啟動要把缺的補進 `data\`——少了這一步,用詞替換表、領域詞表與與會名單通通
    # 不存在,而**畫面上完全看不出來**:轉檔照跑,只是大陸詞沒換、專有名詞沒提示。
    # ⚠️ **2026-09-05 才補上這一行**:先前只寫在 gradio 那條路的 `app.main()` 裡,
    # 而原生視窗正要變成唯一的入口——`test_models.py::test_both_entry_points_seed_
    # before_doing_work` 是移除 gradio 那一刀才讓它現形的(那條測試本來就在守
    # 「補檔只寫在其中一邊 = 另一邊靜默失真」,只是先前它比對的是 app.py)。
    models.seed_missing()
    # ⚠️ 模型下載的進度要進視窗(見 `App._model_progress`):掛在這裡而不是
    # `App.__init__` 裡,是因為它是**行程層級**的單一插槽——一個行程只有一個視窗。
    app = App()
    models.set_progress_hook(app._model_progress)
    try:
        app.mainloop()
    finally:
        models.set_progress_hook(None)


if __name__ == "__main__":
    main()
