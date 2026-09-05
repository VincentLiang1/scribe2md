r"""皮膚資產的**幾何**:超橢圓取樣、九宮格切法、膠囊、抗鋸齒、sprite 打包。

**這裡只有「怎麼畫」,沒有「畫什麼」。** 要產哪幾張底板、每一張多高多圓、出貨哪
幾檔縮放,全部是下游那支 `scripts/make_skin.py` 的事(那是它的長相);本檔提供的是
兩支程式**都該一樣**的那一半——同一種曲線、同一種九宮格、同一種抗鋸齒。

⚠️ **這條界線就是三個 repo 漂開的成因**:拆開之前,「要產哪幾張皮」與「超橢圓怎麼
取樣」寫在同一支檔案裡,所以只能整支複製、然後各自演化——量到的差距是 763 行。

⚠️ **本檔在執行期也會被 import**:顯示縮放對不上出貨資產時,皮膚載入器就地畫一份
(`skin._drawn()`),所以 **Pillow 是執行期相依,不是開發相依**。

為什麼是圖
----------
ttk 內建的繪圖能力只有矩形、3D 浮雕邊框、直線——**沒有圓角、沒有抗鋸齒、沒有
任意路徑**。Windows 原生的 vista 佈景不必用圖,是因為它把繪圖整個交給作業系統的
UxTheme API,拿到的是系統長什麼樣就什麼樣、形狀不能自訂。所以在 Tk 上要一個自訂
形狀的圓角,只有兩條路:**預先渲染成圖片**,或換掉整個 GUI 框架。

sv_ttk 自己就是這樣做的(一張 `spritesheet_light.png` 切成一堆小圖,再用
`ttk::style element create ... image` 掛上去);本檔產出的東西與它同構,換掉的
也正是它的 `Button.button` / `AccentButton.button` / `Entry.field` / `Treeview.field`
/ 進度條那幾個元件。

形狀:四分之一超橢圓,直邊保持直的
----------------------------------
圓角矩形的角是一段**圓弧**,弧與直邊接得上位置、接不上曲率,交界處看得出一個
轉折;squircle 的角是超橢圓 `|x|^n + |y|^n = r^n` 的四分之一,曲率從邊上的 0
連續長到角落的最大值——同樣的半徑看起來更飽滿、轉角更長一段。

⚠️ **按鈕全部是膠囊(2026-08-27,使用者指定「參考 NotebookLM_OCR 的按鈕圓角效果」)**:
指數維持 2.0(正圓弧,見 `SQ_N`),但半徑改成「那一類按鈕**自己高度的一半**」——弧走
完 90 度、切線已經水平了才碰到上下那條直線,交界處沒有轉折。那邊當天先做的,起因是
使用者圈住停止鈕說「這個按鈕的圓角效果,感覺還不是很平順」:那顆鈕自然高 40 邏輯
px,而當時的半徑只有 12(佔高度 30%),弧走不到一半就接上直邊,轉折在整片深紅上特別
讀得出來。做法是 `pill()`——**垂直方向不切九宮格**,高度改由底板釘死。
⚠️ **卡片、清單框、拖放條、訊息區的框不跟**:它們沒有「一種高度」(會被內容撐到
幾百 px),而膠囊在一整塊內容區上讀起來是藥丸不是容器。曲線本身還是同一種,所以
「畫面上不要有兩種曲線」仍然成立。

⚠️ 五個一踩就壞的地方(1~3 是 NotebookLM_OCR 2026-08-26 實際撞到的,完整記述在那個
repo 的 `docs/dev/windows-環境與入口.md` §5.9、§5.11;第 1 點的垂直版是本專案同日撞
到的,4~5 是那邊 2026-08-27 膠囊化時撞到的):

1. **底板中段要夠寬**(`SQ_MID`)。九宮格的中段是 Tk **一格一格重複貼**滿的,
   不是拉伸;中段留 1px 的話,填一顆 840px 寬的鈕就是幾百次繪製呼叫,重畫整個
   視窗要 2.5 秒(看起來就像當掉)。對照組:sv_ttk 的按鈕 sprite 是 20×20 配
   `-border 4`,中段 12px。
   ⚠️ **這一條有垂直版,而且更痛**——本專案 2026-08-26 卡片化時實際撞到:卡片與
   清單框會被撐到 600px 高,而當時那些底板的垂直中段只有 1px,於是垂直 556 次 ×
   水平 10 次 = 5,560 次繪製,**視窗第一次畫出來要 1,996 ms**(使用者回報「頁面出
   現得有點慢」)。會被撐得又寬又高的東西改用 `block()`(兩個方向都留中段)之後是
   **173 ms**。
2. **`border` 不可超過圖片邊長的一半——而且是「逐軸」比。** 切不出九宮格時 ttk 會
   在幾何計算裡原地打轉,事件迴圈當場卡死——沒有例外、沒有訊息。⚠️ 膠囊化之後
   `border` 是四元組 `[左, 上, 右, 下]`,要比的是**左＋右對圖寬、上＋下對圖高**,
   兩軸各自獨立。⚠️ **不可以拿水平的 border 去比圖高**:膠囊的圖又扁又寬
   (NotebookLM_OCR 的 `Sq.subtle` @1x 是 142×45、左右各 23),照「邊長」的字面理解
   會在現行資產上驗出三十幾筆假違規,而最順手的「修法」——把 `br` 降成 `H/2`——正好
   踩到 `pill()` 明文禁止的那件事(中段第一欄不是純色、水平重複貼透出細紋)。
3. **先畫成不透明的 RGB,最後才把遮罩放進 alpha 通道。** 拿遮罩去 `paste` 一張
   RGB 到透明畫布上的話,角落抗鋸齒帶的 RGB 會先跟畫布的黑色混一次,而 Tk 合成
   時又依 alpha 混第二次,四個角就各浮出一圈比底色深的邊。
4. **第 2 點有第二個上限:`border` 也不可超過「用它的那個元件」高度的一半。**
   第 2 點講的是圖片自己切不切得開(切不開會卡死),這一點講的是切得開、但畫不下:
   `2(r+1)` 超過元件實際高度時,Tk 把上下兩個圓角**畫到框外**,形狀變成兩個半圓疊
   在一起、左右各鼓出一塊(NotebookLM_OCR 2026-08-27 實測 r=29 配 47px 高的按鈕)。
   ⚠️ 不會當掉、不會報錯,連 `reqheight` 都不變,只有截圖看得出來。
5. **膠囊的圖高必須精確等於元件高度**(見 `pill()`)。矮了 Tk 垂直**重複貼**、底部
   長出第二段圓角,高了直接**裁切**、下半個圓被削平——兩種都是靜默的。所以動任何
   一個 `SQ_H_*`、任何一顆按鈕的垂直 `padding` 或字級之前,要把那一類元件的自然
   高度重量一次(量法與驗算表在下游各自的 UI 文件裡)。
   ⚠️ **這一條綁的不是第 4 點**(2026-08-28 更正,依據是 NotebookLM_OCR
   2026-08-27 下午的實測):本檔一度寫著「膠囊化把第 4 點的上限從很遠變成只差
   幾 px」,那描述的是**當天上午**那一版——半徑改成 H/2、但**還在切四邊**。同日下午
   改成垂直不切之後方向就反了:**膠囊的垂直 border 是 0,第 4 點對它們永遠觸發不
   了**;真正卡住膠囊的是這一條(內容需求必須矮於圖高)。第 4 點今天只管得到還有
   垂直 border 的那幾張(卡片、清單框、核取方塊)。
"""


from __future__ import annotations

import math

from PIL import Image, ImageColor, ImageDraw


# `sprites.json` 的格式版號。⚠️ **2026-08-27 從 `main()` 裡的字面值提上來**:當場畫
# 的那條路(`skin._save_cache`)寫的快取跟出貨資產是**同一種格式、同一支讀取器**,
# 所以那個號碼要有一份共用的來源。⚠️ 改格式(欄位增減、`border` 從 int 改成四元組
# 那一類)就要進號——舊資產的每個 key 都還在,不進號的話它會**成功**載入,把不相容
# 的元件定義裝上去(使用者換電腦是複製專案資料夾,只覆蓋 `.py` 而留著舊 `assets/`
# 完全做得到)。快取那邊不必靠這個號:它的相容性由 `skin.skin_cache_key()` 的指紋
# 顧著,而指紋雜湊的正是這支檔案的原始碼。
SCHEMA_VERSION = 1


# 角落的曲線指數(`|x|^n + |y|^n = r^n`)。**2.0 就是正圓弧。**
#
# ⚠️ **這裡曾經是 5.0(squircle),2026-08-26 使用者指定全部改成正圓**:他先要卡片
# 比照 meeting-scribe 那張網頁截圖,接著「所有按鈕圓角弧度也跟它做成相同,灰色塊的
# 圓角弧度也請比照」。中間試過一版「容器正圓、按鈕留 squircle」,被這句話推翻——
# **畫面上不要有兩種曲線**。
#
# 為什麼 5.0 撐不住:超橢圓幾乎填滿整個角落方框、只在最角落切掉一點,半徑 21 時
# **視覺上的圓角只有 7px**(量 sprite 的 alpha 量出來的)。小按鈕看不出來,卡片那麼
# 大一塊就是「幾乎直角」——所以光加大半徑沒有用,指數才是那個旋鈕。
#
# 半徑則是逐像素量他那張參考圖得到的:卡片 30 實體 ÷ 150% = 20,按鈕 12 ÷ 1.5 = 8,
# 卡片內的灰塊 11 ÷ 1.5 ≈ 8(我們維持 10,差 3 實體像素,肉眼分不出來)。
SQ_N = 2.0
SQ_SS = 4             # 遮罩超取樣倍率(畫 4× 再縮回來,這就是抗鋸齒)
SQ_STEPS = 24         # 每個角取樣幾個點(再多肉眼看不出來,只是變慢)

# 虛線描邊:一個週期裡實線佔多少(拖放條那一圈)。⚠️ **週期本身不在這裡**——它由
# 下游算(中段寬度 ÷ 要切幾段),因為「一段的長度必須整除中段」取決於那支自己的
# 尺寸;這裡定的是虛線**長什麼樣**,而那是兩支程式該一樣的東西。
SQ_DASH_ON = 0.58


def px(n: float, scale: float) -> int:
    return max(1, int(round(n * scale)))


def _sq_points(w: float, h: float, r: float) -> list[tuple[float, float]]:
    """連續圓角的輪廓點:四個角各是四分之一超橢圓,直邊保持直的。

    指數一律取 `SQ_N`(2.0,正圓弧)。⚠️ 這裡曾經是個參數,撐著「容器正圓、按鈕
    squircle」那一版——**那一版 2026-08-26 被使用者一句話推翻**(畫面上不要有兩種
    曲線,見檔頭與 `SQ_N`),而參數沒有跟著收:四支簽名串了它一路,卻沒有任何呼叫端
    傳過非預設值,讀的人會以為畫面上支援兩種曲線。被推翻的**理由**留在 `SQ_N`
    上方,那才是要留的東西。"""
    r = min(r, w / 2.0, h / 2.0)
    k = 2.0 / SQ_N
    q = [(r - r * math.cos(t) ** k, r - r * math.sin(t) ** k)
         for t in (i / SQ_STEPS * (math.pi / 2) for i in range(SQ_STEPS + 1))]
    rev = list(reversed(q))
    return (q                                     # 左上:(0,r) → (r,0)
            + [(w - x, y) for x, y in rev]        # 右上:(w-r,0) → (w,r)
            + [(w - x, h - y) for x, y in q]      # 右下:(w,h-r) → (w-r,h)
            + [(x, h - y) for x, y in rev])       # 左下:(r,h) → (0,h-r)


def _sq_mask(w: int, h: int, r: float) -> Image.Image:
    m = Image.new("L", (w * SQ_SS, h * SQ_SS), 0)
    ImageDraw.Draw(m).polygon(
        [(x * SQ_SS, y * SQ_SS) for x, y in _sq_points(w, h, r)], fill=255)
    return m.resize((w, h), Image.LANCZOS)


def _dash_mask(w: int, h: int, r: float, lw: int,
               period: float) -> Image.Image:
    """虛線描邊的遮罩:沿著輪廓走,每個週期畫 `SQ_DASH_ON` 那一段。

    ⚠️ 相位是**沿周長累積**的,而週期由中段整除算出來(見 SQ_DASHES),所以九宮格
    重複貼中段時接得上,不會冒出半截虛線。"""
    m = Image.new("L", (w * SQ_SS, h * SQ_SS), 0)
    d = ImageDraw.Draw(m)
    pts = _sq_points(w, h, r)
    walked = 0.0
    for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]):
        seg = math.hypot(x1 - x0, y1 - y0)
        if seg <= 0:
            continue
        step = min(0.5, seg)          # 沿線細切,再逐小段決定畫不畫
        steps = max(1, int(seg / step))
        for k in range(steps):
            a, b = k / steps, (k + 1) / steps
            if (walked + seg * a) % period < period * SQ_DASH_ON:
                d.line([((x0 + (x1 - x0) * a) * SQ_SS,
                         (y0 + (y1 - y0) * a) * SQ_SS),
                        ((x0 + (x1 - x0) * b) * SQ_SS,
                         (y0 + (y1 - y0) * b) * SQ_SS)],
                       fill=255, width=lw * SQ_SS)
        walked += seg
    return m.resize((w, h), Image.LANCZOS)


def plate(w: int, h: int, r: float, fill: str,
          line: str | None = None, lw: int = 1,
          dash: float | None = None,
          on: str | None = None) -> Image.Image:
    """一張圓角底板:實色填滿,可選描邊(`dash` 給週期就畫成虛線)。

    ⚠️ 先畫成不透明的 RGB、**最後**才把遮罩放進 alpha 通道(理由見檔頭第 3 點)。

    `on` = **這張底板坐在什麼顏色上**。給了就把圓角外側直接畫成那個色、整張圖
    不透明;不給就留透明。

    ⚠️ **一定要給**(除了色點那種本來就該透明的)。理由是 ttk 的繪製順序:它先用
    樣式的 `background` 把整塊填滿,**再**把九宮格的圖畫上去——圓角外側那圈透明區
    露出來的是那個 background,不是父容器。2026-08-26 使用者兩次圈著截圖說「顏色
    有跑出去」,兩次都是這件事(先是卡片的白方角,再是清單框的灰方角)。

    ⚠️ **不能改用「把樣式的 background 設成外側色」那條捷徑**,雖然它一度做出來了:
    `Treeview` 的 `background` 是**列的底色**、`fieldbackground` 是空白區的底色,
    兩個都另有用途,設成卡片白就等於把清單的底色改掉。把外側畫進圖裡與那些值無關,
    而代價只是「同一張底板不能重複用在兩種背景上」——本專案的每個控制項都只坐在
    一種底色上(按鈕/框/進度條在卡片上、卡片在視窗底上)。"""
    w, h = max(1, w), max(1, h)
    outer = _sq_mask(w, h, r)
    img = Image.new("RGB", (w, h), fill)
    if line and lw > 0:
        if dash:
            ring = Image.composite(outer, Image.new("L", (w, h), 0),
                                   _dash_mask(w, h, r, lw, dash))
        else:
            inner = Image.new("L", (w, h), 0)
            inner.paste(_sq_mask(max(1, w - 2 * lw), max(1, h - 2 * lw),
                                 max(1.0, r - lw)), (lw, lw))
            ring = Image.composite(outer, Image.new("L", (w, h), 0),
                                   Image.eval(inner, lambda v: 255 - v))
        # 這一次 paste 是**在不透明的圖層裡**混色,描邊與填色混得對
        img.paste(Image.new("RGB", (w, h), line), (0, 0), ring)
    if on is not None:
        # 圓角外側直接畫成它坐在的那個顏色,整張不透明(見 docstring)。
        # ⚠️ 用 `outer` 當遮罩貼上去:抗鋸齒那一圈於是與外側色**混得對**,
        # 而不是先跟黑色混一次再由 Tk 混第二次(檔頭第 3 點的同一個坑)
        base = Image.new("RGB", (w, h), on)
        base.paste(img, (0, 0), outer)
        return base.convert("RGBA")
    img = img.convert("RGBA")
    img.putalpha(outer)
    return img


def shade(color: str, amt: float) -> str:
    """把**色碼**往白(amt>0)或黑(amt<0)拉,回傳新的色碼。

    ⚠️ **對顏色做,不是對圖片做。** 原本這支是整張圖去 blend,那在底板還透明的
    年代沒問題;底板改成不透明(見 `plate` 的 `on`)之後,整張 blend 會把圓角外側
    那圈「它坐在的顏色」一起壓暗——按下按鈕時,卡片上會浮出一圈比周圍暗的方框。

    hover 有明確色碼、pressed 沒有,所以 pressed 那一階一律由這支從同一個底色推
    出來,免得再手配一組沒有人記得該差多少的色碼。"""
    rgb = ImageColor.getrgb(color)
    tone = 255 if amt >= 0 else 0
    return "#%02x%02x%02x" % tuple(
        round(c + (tone - c) * abs(amt)) for c in rgb)


def dot(size: int, fill: str) -> Image.Image:
    """一顆實心圓。

    ⚠️ **色點是唯一該留透明的底板**:它是 Treeview 的 item image,由 Tk 自己合成
    到列的底色上,而那個底色會變(選取的列是淡藍)。其餘每一張都要指定 `on`。

    歷史:這支函式原本存在的理由是「n=5 的超橢圓在 r=w/2 時不是圓,是一顆明顯方的
    圓角方塊」(2026-08-26 截圖看出來的,8px 下四個角看得一清二楚)。指數改成正圓
    之後那個理由消失了,但它留著——畫一顆實心圓不必繞道九宮格那條路。"""
    m = Image.new("L", (size * SQ_SS, size * SQ_SS), 0)
    ImageDraw.Draw(m).ellipse((0, 0, size * SQ_SS - 1, size * SQ_SS - 1), fill=255)
    img = Image.new("RGB", (size, size), fill).convert("RGBA")
    img.putalpha(m.resize((size, size), Image.LANCZOS))
    return img


def pad_right(img: Image.Image, gap: int) -> Image.Image:
    """右邊補一段透明——色點與文字之間的縫,Treeview 裡沒有地方塞內距。"""
    out = Image.new("RGBA", (img.width + gap, img.height), (0, 0, 0, 0))
    out.paste(img, (0, 0))
    return out


def pack(imgs: dict[str, Image.Image],
         *, dedup: bool = False) -> tuple[Image.Image, dict]:
    """把底板疊成一張 sprite sheet(單欄,最省事也最好對)。

    `dedup=True` 時**逐位元組相同的底板只鋪一次**(NotebookLM_OCR 2026-08-27 做的,
    2026-08-28 隨這支搬進共用包):好幾組狀態本來就是同一張圖——低調皮的 `rest` 與
    `dis` 都是卡片色(那一顆停用時本來就不該變色)、`accent-dis` 與 `stop-dis` 都是
    `run_off`。各出貨一份純粹是浪費(那邊量到 415KB → 401KB),而讓兩個 key 指到同一
    個 rect 對消費端完全透明——`skin._from_assets` 是照 rect 去裁的,裁兩次同一塊區域
    沒有任何差別(而且它自己也去重,同一塊只裁一次)。
    ⚠️ **不要改成「把重複的 key 從 `states` 拿掉」**:狀態表要完整列出來,ttk 才知道
    每個狀態該用哪張圖。

    ⚠️ **預設是關的,而且那是刻意的**(2026-08-28,`check_downstreams` 當場抓到):
    開關一改,產出的 sprite sheet 就變了,而下游有「出貨的資產 == 現在的產生器」那種
    逐位元組比對的測試——共用包這邊一改預設,**另一個下游當場變紅,而在這裡看不到**。
    這正是「API 只准加、不准改語意」那條規則要擋的東西:要打開就在**那個下游**的
    產生器裡傳 `dedup=True`,同一筆改動重跑產生器、把資產一起提交。
    """
    if dedup:
        return _pack_dedup(imgs)
    order = sorted(imgs)
    sheet = Image.new("RGBA", (max(im.width for im in imgs.values()),
                               sum(imgs[k].height for k in order)), (0, 0, 0, 0))
    rects, y = {}, 0
    for k in order:
        im = imgs[k]
        sheet.paste(im, (0, y))
        rects[k] = [0, y, im.width, im.height]
        y += im.height
    return sheet, rects


def _pack_dedup(imgs: dict[str, Image.Image]) -> tuple[Image.Image, dict]:
    """`pack(dedup=True)` 的本體(理由見那邊)。"""
    sheet_w = max(im.width for im in imgs.values())
    rects, top, y = {}, {}, 0
    for k in sorted(imgs):
        im = imgs[k]
        sig = (im.width, im.height, im.tobytes())
        if sig not in top:
            top[sig] = y
            y += im.height
        rects[k] = [0, top[sig], im.width, im.height]
    sheet = Image.new("RGBA", (sheet_w, y), (0, 0, 0, 0))
    for k, (_x, t, _w, _h) in rects.items():
        sheet.paste(imgs[k], (0, t))
    return sheet, rects


def pill(h_logical: int, scale: float, mid: int) -> tuple[int, int, float, int]:
    """膠囊底板:左右兩端各**一個完整半圓**,中間一段純色。

    回傳 `(圖寬, 圖高, 半徑, 水平 border)`。半徑就是圖高的一半——弧走完 90 度、
    切線已經水平了才碰到上下那條直線,所以沒有交界(見檔頭那一段)。

    ⚠️ **垂直方向不切九宮格**(`border` 的上下兩格給 0),因為切了就等於在圖裡留
    一段直邊,半徑再也大不過 `H/2 − 1`。代價是**圖高必須精確等於元件高度**,三種
    情況 NotebookLM_OCR 2026-08-27 都實測過:圖比元件矮 → 垂直**重複貼**,底部長
    出第二段圓角;圖比元件高 → **裁切**,下緣被削平;相等 → 完美膠囊。

    ⚠️ 所以元件的 `height` 要給圖高、把元件**釘死**在這個高度——進度條從第一天
    就是這樣做的(`border=[edge, 0, edge, 0]` 配釘死的 `thickness`),只是當時沒
    意識到那是一個可以推廣的模式。那一類按鈕自己的內容(字 + 樣式的垂直 padding)
    **必須矮於圖高**,不然元件高度取「內容需求」與 `height` 的較大者,一撐過頭就
    變成上面那個「裁切」。

    ⚠️ `border` 取 `ceil(H/2)` 不是 `H/2`:H 是奇數時半圓會多佔半欄,border 少
    一欄的話中段第一欄不是純色,水平重複貼就會透出一條細紋。"""
    h = px(h_logical, scale)
    br = (h + 1) // 2
    return 2 * br + mid, h, h / 2.0, br


def pill_elem(states, h: int, br: int, on: str, *, padding) -> dict:
    """膠囊底板的元件定義。⚠️ 九宮格那幾個數字要**跟圖對得起來**(上下不切、
    `height` 精確等於圖高、左右各一欄),而那三條規則原本在四張膠囊各抄一次
    ——踩到的症狀是垂直重複貼或被裁,兩種都不報錯,只有截圖看得出來。

    ⚠️ **`padding` 的垂直那一份在膠囊上換不到任何東西**,可以(也建議)給 0:高度被
    `height` 釘死、標籤在裡面本來就置中,所以扣掉之後還是置中——它只會把「內容需求」
    灌高 2n,而那正是「內容不可以撐過圖高」那條規則唯一的安全邊界(NotebookLM_OCR
    2026-08-27 把四張膠囊的內距改成 `[左, 0, 右, 0]`,餘裕從 2~12 變成 10~27 實體
    像素,而畫面**逐像素相同**)。水平那一份必須留著,寬度是內容決定的。
    ⚠️ 所以這個參數吃純量也吃 `[左, 上, 右, 下]`;讀取端一律經過 `skin._pad_v`。"""
    return dict(states=states, border=[br, 0, br, 0], width=2 * br + 1,
                height=h, padding=padding, sticky="nswe", on=on)


def block_elem(states, r: int, pad: int, on: str) -> dict:
    """兩個方向都留中段的那一類(卡片、清單框、拖放區)的元件定義。"""
    return dict(states=states, border=r + 1, width=2 * (r + 1) + 1,
                height=2 * (r + 1) + 1, padding=pad, sticky="nswe", on=on)


def block(r: int, mid: int) -> tuple[int, int]:
    """兩個方向都留中段。給**會被撐得又寬又高**的東西用(卡片、清單框、拖放區)。

    ⚠️ **這是檔頭第 1 點的垂直版,而且它比水平版痛得多。** 九宮格的中段是 Tk
    一格一格**重複貼**滿的:垂直中段只留 1px 的話,填一張 600px 高的卡片就是垂直
    556 次、再乘水平 10 次 = **5,560 次繪製呼叫**,而畫面上有四張卡片加清單框、
    拖放區、訊息區。2026-08-26 實測:視窗第一次畫出來要 **1,996 ms**(App 建構
    本身只有 237 ms、皮膚載入只有 9.5 ms),使用者回報「頁面出現得有點慢」。改成
    兩個方向都留中段之後同一台機器是 **173 ms**——**11.5 倍**,而資產只從 133KB
    長到 161KB。

    ⚠️ 這一條與膠囊(`pill()`)不衝突,兩者管的是不同的東西:**會長高**的用這支,
    **高度釘死**的用膠囊,而膠囊那一路垂直根本不切。

    代價是圖片面積變 4 倍(當年那版是 188×45 → 188×188),但那是實色圓角,PNG 壓得掉。
    ⚠️ 元件的 `width`/`height` **不可以跟著改成圖片邊長**——那兩個是「最小
    尺寸」,給了 188 的話 62px 高的拖放區會被硬撐成 125。"""
    return 2 * (r + 1) + mid, 2 * (r + 1) + mid
