"""外觀層:字型、Sun Valley 佈景、squircle 皮膚、色票。

畫面上的形狀與顏色分工:**顏色**全在 `palette.py`(唯一真值,產生器與本檔共用),
**形狀**在 `scripts/make_skin.py`(它把顏色畫進 `assets/skin/` 的 PNG),本檔負責
把那些圖掛上 ttk、把文字色設進樣式。

⚠️ **只換互動控制項的外觀**(按鈕、框、進度條);視窗外框不碰(使用者 2026-08-26
在 NotebookLM_OCR 拍板,本專案沿用)。那不是省事,是量過的:視窗四角的圓角是
Windows 11 的 DWM 畫的,app 改不了形狀——要改只能走 layered window ＋逐像素
alpha,代價是失去原生標題列,而最小化、貼齊、工作列預覽全部得自己重寫,
`winui.taskbar_progress` 那一套也綁在原生視窗上。

⚠️ **沒有 sv_ttk 也必須開得起來**:這支是使用者唯一的入口,為了外觀讓它開不了
完全不划算。缺套件就留在系統原生佈景(vista),只換字型;squircle 皮膚同一條
原則——裝不起來就回 None,畫面留在原本的長相。

⚠️ **資產拿不到就現畫一份**(`_drawn()`:import `scripts/make_skin.py` 就地畫,順手
存進磁碟快取)。做法照姊妹專案 NotebookLM_OCR。⚠️ 這條路要 Pillow,所以它是**執行期
相依**(見 pyproject 那段註解)。

四條路,由快到慢,**順序不可調換**(見 `SquircleSkin.install`):

  1. 出貨資產、且顯示縮放**精確匹配**(`SKIN_SCALE_TOL`)——最快,而且逐位元組驗過
     (`tests/test_gui.py::test_the_shipped_skin_is_what_the_generator_draws_today`)
  2. 這台機器上畫過並存起來的快取(同一種格式、共用同一支讀取器)
  3. 當場畫(照**真實**縮放,任何 DPI 都畫得對)＋存快取
  4. 都不行(沒有 Pillow、`scripts/` 不在):退回「貼最近那一檔資產」

⚠️ **第 4 條是安全網,不是裝飾**:少了它,第 1 條的精確匹配把關就會把「縮放對不上」
從「按鈕矮一點點」升級成「整個沒有皮膚」。
"""
import base64
import hashlib
import io
import json
import os
import shutil
import sys
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import ttk
from typing import NamedTuple

from winkit import env_var, host, palette, paths, skingen, winui
from winkit.palette import DOTS, PALETTES


def skin_dir() -> Path:
    r"""出貨資產的位置:下游套件的 `assets/skin/`。

    ⚠️ **是函式不是常數**:位置要等下游 `bind()` 之後才算得出來,而模組層常數會在
    import 當下就定死——那時還沒有人告訴我們下游在哪。"""
    return paths.assets_dir() / "skin"

def _generator() -> Path:
    """下游那支 `make_skin.py`。

    ⚠️ **產生器留在下游是刻意的**:它決定「要產哪幾張皮、每一張多大」——那是那支
    app 的長相。共用的只有幾何(`skingen`)。
    ⚠️ **路徑由 `bind()` 給,本包不猜**:兩個下游放的層不一樣(`scripts/` vs
    `tools/`),而猜錯是安靜降級(見 `bind()` 的 `skin_generator`)。"""
    return host().skin_generator

# 出貨資產的縮放檔與這台機器的實際縮放要**幾乎相等**才算數(見 `_variant`)。
SKIN_SCALE_TOL = 0.01

# 「當場畫」的成果存到哪(落點與那幾條「為什麼是 LOCALAPPDATA」的理由都在
# `paths.appdata_root`)。這一支只覆寫**皮膚**那一格,給測試用。
def skin_cache_env() -> str:
    """「當場畫」的成果存到哪(給測試覆寫用)。⚠️ 是函式不是常數:前綴要等下游
    `bind()` 之後才算得出來。"""
    return env_var("SKIN_CACHE")

UI_FONT = "Microsoft JhengHei UI"
UI_FONT_FALLBACK = "Microsoft JhengHei"   # 舊版 Windows 沒有 UI 版

# `apply()` 直接 `configure` 說明文字前景色的那幾個樣式,**其餘全靠後綴繼承**。
# ⚠️ **這是給下游 import 的**(2026-09-06 加):下游要列舉「哪些樣式吃 muted 前景」時
# 判準是**後綴**,而後綴要比對的那份根名單是 **winkit 的實作事實**、不是下游的身分
# ——過一次 A/B 判準:三個下游吃的是同一支 `apply()`,答案對三個都一樣,所以是 B 類。
# ⚠️ 這一格是踩出來才加的:MP4-2-SRT 的 `tests/test_gui.py` 2026-09-03 手抄了一份
# `_MUTED_SUFFIXES = ("Muted.TLabel", "CardHint.Card.TLabel")`,而兩份之間**沒有任何
# 連結**——這裡多 `configure` 一個吃 muted 前景的樣式(或改掉其中一個名字),那份就
# 過期,它那條檢查變成**部分 no-op**。⚠️ 失效方式與 2026-09-03 那次同型:**測試是
# 綠的、抓法看起來也對**,而那次的實測代價是「低於門檻連續三天沒有人發現」。
# ⚠️ **這是「winkit 那半」**:下游可以在自己的 `configure_styles` 掛勾裡再加幾個
# (meeting-scribe-native 的 `Seg.TLabel` 就是),那半是它自己的長相、要 union 上去
# ——與 `palette.MUTED_SURFACES`(承諾)對 `muted_fits()`(現況)是同一種切法。
MUTED_FG_STYLES = ("Muted.TLabel", "CardHint.Card.TLabel")

class Button(NamedTuple):
    """一顆按鈕的完整規格。`vpad` 是**想要的**垂直內距,實際值由圖高反推收口
    (見 `_button_padding`);`vpad_bare` 是沒有皮膚那條路要還回去的那份。"""

    plate: str        # 膠囊底板(SKIN_SWAPS 換上去的那張)
    size: int         # 字級
    weight: str
    hpad: int         # 水平內距(過 px() 之前的尺規值)
    vpad: int
    vpad_bare: int


class SquircleSkin:
    """把 ttk 互動控制項的背景換成 squircle 圖片。

    ⚠️ **只在 sv_ttk 載得起來時才安裝**:這裡是把 sv_ttk 的 layout 複製一份、只換
    掉背景元件的名字,換不到 layout(原生 vista 佈景的結構不一樣)就整個放棄,讓
    畫面留在原本的長相——外觀不值得賠上「開得起來」。

    ⚠️ **不要用 Pillow 的 ImageTk**:它要再多一個 C 擴充模組才 import 得起來,而
    Tk 8.6 的 `PhotoImage` 本來就吃 base64 的 PNG(含 alpha)。走 base64 也順手
    避開了「專案放在非 ASCII 路徑時 `-file` 讀不到」的那一類麻煩。
    """

    def __init__(self, root: tk.Misc, scale: float, mode: str, fam: str,
                 spec) -> None:
        self.root, self.scale, self.mode = root, scale, mode
        # 這支 app 的皮膚規格(要換哪幾個 layout、哪顆鈕配哪張皮…)。
        # ⚠️ **規格不住這裡**:載入器是兩支程式共用的,而「有哪幾種按鈕」正是它們
        # 必須不一樣的地方。下游把自己的 `skin` 模組整個交進來(見它的 `apply`)。
        self.spec = spec
        self.fam = fam             # 量字型用(見 `_fit_pill_plates`)
        self.st = ttk.Style(root)
        self._keep: list = []      # ⚠️ Tk 不持有 PhotoImage 的參考,放掉就變空白
        # 狀態色點(見 DOTS)。它們跟底板同一張 sprite sheet,但**不是** ttk 元件
        # ——gui 直接拿去當清單那一列的 image。⚠️ **有那幾張才有**,見 `_adopt`。
        self.dots: dict[str, tk.PhotoImage] = {}
        # 切出來的全部底板(下游要拿非 ttk 元件的那幾張時查這裡,見 `_adopt`)
        self.images: dict[str, tk.PhotoImage] = {}
        # 底板的 `(圖高, 自帶內距)`,裝起來之後才有。⚠️ 這是**膠囊按鈕的內距算得
        # 出來的唯一來源**(見 `_pill_pad`):資產是照某一檔顯示縮放畫死的,而字型
        # 跟著真實 DPI 走,兩者對不上時要靠這兩個數字把內容壓回圖高以內。
        self.plate: dict[str, tuple[int, int]] = {}
        # 四條路裡的哪一條成的。⚠️ **驗收時要分得出來**:畫面上「精確匹配的資產」
        # 與「當場畫的」長得幾乎一樣(那正是重點),出了事只有這個欄位講得清楚是哪
        # 一條路。空字串＝還沒裝或整個失敗。
        self.source = ""

    def install(self) -> bool:
        """四條路,由快到慢。⚠️ **順序不可調換**,理由在檔頭與各自的 docstring。

        ⚠️ 最後那條 `exact=False` 是**安全網**:沒有 Pillow、或 `scripts/` 不在
        (打包成 wheel)時,前三條全部走不通,而那時貼最近那一檔仍然比沒有皮膚好。"""
        elems = (self._from_assets(skin_dir(), "assets", exact=True)
                 or self._from_cache()
                 or self._drawn()
                 or self._from_assets(skin_dir(), "nearest"))
        if elems is None:
            return False
        # ⚠️ 前景色**直接查色票**,不從資產繞一圈:上一版把這兩個顏色也烘進
        # `sprites.json`,而 `palette` 這邊改了卻忘了重跑 `make_skin.py` 的話,停用態
        # 的字色會停在舊值、畫面其他地方都更新了(那正是無聲漂移)。順帶修掉一個已經
        # 漂開的名字:資產裡那個鍵叫 `run_off`,而色票的 `run_off` 是**另一個顏色**
        # (停用態的底色),讀起來像同一個其實不是。
        pal = PALETTES[self.mode]
        self._fit_pill_plates(elems)
        # ⚠️ 存的是**垂直**那一份內距(見 `_pad_v`):`_pill_fits` 拿它去算「還剩多少
        # 高度」,而水平那一份與圖高無關。
        self.plate = {name: (e["height"], _pad_v(e["padding"]))
                      for name, e in elems.items()}
        try:
            for name, e in elems.items():
                states = e["states"]
                args = [str(states[0][1])] + [(s, str(i)) for s, i in states[1:]]
                self.st.element_create(
                    name, "image", *args,
                    border=e["border"], padding=e["padding"],
                    sticky=e["sticky"], width=e["width"], height=e["height"])
            # ⚠️ **先把來源 layout 全部抓下來,再統一寫回去。** 邊抄邊寫的話後面
            # 那幾條會抄到**已經換過**的版本:`Cta.TButton` 從 `TButton` 抄的時候,
            # `Button.button` 早就被前一圈改名成 `Sq.button` 了,於是替換表對不上
            # 任何一個元件名——那顆鈕安靜地留在一般按鈕的灰底皮上,沒有例外也沒有
            # 訊息(2026-08-27 加線框鈕時當場踩到,截圖才看出來:文字是藍的、底板
            # 還是灰的)。姊妹專案 NotebookLM_OCR 2026-08-26 踩過同一個。
            src_layouts = [(style, self.st.layout(src or style), table)
                           for style, src, table in self.spec.SKIN_SWAPS]
            for style, layout, table in src_layouts:
                self.st.layout(style, self._swap(layout, table))
            # 卡片與拖放區:自己開一個**只有底板**的 layout,不從 TEntry 抄。
            # ⚠️ 抄 TEntry 會把 `Entry.textarea` 一起帶進 Frame 裡,而那個元件在
            # 非輸入框的控制項上沒有定義的行為;這裡要的本來就只有一張底板。
            # ⚠️ 卡片的**內距不在這裡**——它是版面的尺規(gui.CARD_PAD,要跟著顯示
            # 縮放走),由 Frame 自己的 padding 給
            for style, elem, _own in self.spec.SKIN_FRAMES:
                self.st.layout(style, [(elem, {"sticky": "nswe"})])
            self.st.configure("Horizontal.TProgressbar",
                              thickness=elems["Sq.trough"]["height"])
            for style in self.spec.ACCENT_STYLES:
                self.st.map(style,
                            foreground=[("disabled", pal["run_off_fg"]),
                                        ("!disabled", pal["on_accent"])])
        except Exception:
            # 佈景結構跟預期不一樣(換了 sv_ttk 版本、Tcl 版本不合):畫面留在
            # 原本的長相就好,不要讓外觀把整支程式帶下水
            # ⚠️ `source` 要跟著清掉:上面那幾條路已經把它設成走通的那一條了,留著
            # 會變成「說自己裝好了、畫面上卻沒有皮膚」——不變式是**非空 ⟺ 真的裝上去**
            self.source = ""
            return False
        return True

    def _fit_pill_plates(self, elems: dict) -> None:
        """把膠囊底板**自帶的內距**收到「這個 DPI 下塞得下」為止。

        ⚠️ **這是資產與真實 DPI 對不上時的第一道收口**(2026-08-27 code review 抓到)。
        資產有八檔(`make_skin.SCALES`),`_variant` 挑最接近的那一檔;顯示縮放卻不只
        那八種(Windows 11 還可以自訂)。字型跟著真實 DPI 長大、底板停在
        200% 那一檔,於是內容撐過圖高,Tk 把膠囊**裁切**——不報錯、`reqheight` 也看
        不出來,只有截圖看得到下半個圓被削平。

        ⚠️ 順序很重要:**先收這一份、再收樣式那一份**(`_button_padding`)。底板自帶的
        內距是兩份裡比較大的那一份(`make_skin.SQ_PAD` 照資產的縮放畫死,200% 那一檔
        就是 8px 上下各一),樣式那份在 300% 下先歸零都還差 6px,非收它不可。逐檔實測
        (`(圖高, 內容)`,一般按鈕):3.0 收之前 60/66 撐爆、收之後 60/58;3.5 收之前
        60/76、收之後 60/60。100%~225% 這一份完全不會被動到(`min` 取不到)。"""
        for b in self.spec.BUTTONS.values():
            e = elems.get(b.plate)
            if not e:                # 資產舊了、沒有這張底板:讓它照原樣走
                continue
            line_h = tkfont.Font(
                self.root, font=(self.fam, b.size, b.weight)).metrics("linespace")
            # 底板自帶的內距**就是**這裡要收的那一份,所以 `inner` 給 0
            # (算式與第二道收口共用一支,見 `_pill_fits`)
            e["padding"] = _with_pad_v(
                e["padding"],
                _pill_fits(e["height"], line_h, 0, _pad_v(e["padding"])))

    def _swap(self, layout, table: dict):
        """複製一份 layout,把背景元件換成我們的(其餘結構原封不動)。"""
        out = []
        for elem, opts in layout:
            opts = dict(opts)
            if "children" in opts:
                opts["children"] = self._swap(opts["children"], table)
            out.append((table.get(elem, elem), opts))
        return out

    def _photo(self, **kw) -> tk.PhotoImage:
        ph = tk.PhotoImage(master=self.root, **kw)
        self._keep.append(ph)
        return ph

    def _adopt(self, cut: dict) -> None:
        """把切好的底板收下來:`images` 是全部,`dots` 是其中的狀態色點。

        ⚠️ **色點是「有才拿」,不是必需品**(2026-08-28 接第二個下游時改的):它們是
        清單那一欄的圖片,而 NotebookLM_OCR 的畫面上根本沒有清單、資產裡也就沒有那四
        張。原本寫死 `cut[f"dot-{k}"]` 會 `KeyError`,而這一段整個包在 `except` 裡
        ——**四條來源會全部落空、畫面整個掉皮膚**,沒有任何訊息。缺一顆色點的正確症狀
        寫在 `palette.DOTS` 上:那一列沒有色點,不是整支程式沒有皮膚。

        ⚠️ `images` 讓下游拿到**不是 ttk 元件**的那幾張圖(色點、
        NotebookLM_OCR 收合鈕的三角形)。它們與 `elems` 指向同一批 PhotoImage,不佔
        額外記憶體;Tk 不持有參考,放掉就變空白,所以一律經過 `_keep`。"""
        self.images = cut
        self.dots = {k: cut[f"dot-{k}"] for k in DOTS if f"dot-{k}" in cut}

    def _from_assets(self, root: Path, tag: str, *, exact: bool = False):
        """讀一個**資產目錄**(`assets/skin/` 或這台機器上畫過的快取)。

        ⚠️ 任何一步不對就回 None 交給下一條路,不要丟例外。
        ⚠️ **快取刻意做成同一種格式、共用這一支讀取器**(2026-08-27):兩份格式就是
        兩份會漂的程式,而漂掉的症狀(元件定義對不上)是靜默的。快取那邊的
        `sprites.json` 只裝一個 variant、`scales` 只有一格,其餘欄位一模一樣。

        ⚠️ **整張 sprite sheet 不進 `_keep`**:元件拿的是切出來的 28 張小圖,sheet
        只在切的那一段用得到。留著它比真正在用的還多(實測常駐 @150% 是 1.65 MB、
        @300% 6.5 MB,而 28 張切片才 1.26 / 5.58 MB),而這支程式是長時間開著的桌面
        視窗。區域變數在這裡回收得掉——`PhotoImage.__del__` 會去 Tcl 那頭刪。"""
        # ⚠️ **每一條路開頭都要清空**(2026-08-27 多路之後才需要):上一條路走到一半
        # 才失敗(PNG 半截、鍵名對不上)時,已經切出來的那幾張會永遠留在 `_keep` 裡
        # ——沒有人用、也沒有人回收得掉。單一來源的年代不會發生,因為失敗就整個放棄。
        self._keep.clear()
        try:
            var = _variant(self.mode, self.scale, root, exact=exact,
                           schema=getattr(self.spec, "SKIN_SCHEMA", None))
            if var is None:              # 縮放差太多:交給下一條路
                return None
            sheet = tk.PhotoImage(
                master=self.root,
                data=base64.b64encode((root / var["file"]).read_bytes()))
            # ⚠️ **同一塊 rect 只裁一次**:`skingen.pack()` 去重之後好幾個 key 指到
            # 同一塊區域(低調皮的 rest/dis 都是卡片色、accent-dis 與 stop-dis 都是
            # run_off)。逐 key 裁等於多做幾次 blit,又把幾張一模一樣的 PhotoImage
            # 永久留在 `_keep` 裡(NotebookLM_OCR @2x 量到約 252KB)。對 `elems` 與
            # `images` 完全透明——兩邊都只是查 `cut`。
            cut: dict[str, tk.PhotoImage] = {}
            same: dict[tuple, tk.PhotoImage] = {}
            for key, (x, y, w, h) in var["sprites"].items():
                if (x, y, w, h) in same:
                    cut[key] = same[(x, y, w, h)]
                    continue
                sub = self._photo(width=w, height=h)
                # ⚠️ `-compositingrule set` 不可省:預設是 overlay,會把來源**疊**
                # 上去而不是覆蓋,半透明的角落會被疊成不透明
                self.root.tk.call(sub, "copy", sheet, "-from", x, y,
                                  x + w, y + h, "-compositingrule", "set")
                cut[key] = same[(x, y, w, h)] = sub
            elems = {name: dict(e, states=[(s, cut[k]) for s, k in e["states"]])
                     for name, e in var["elements"].items()}
            self._adopt(cut)
        except Exception:
            return None
        self.source = tag
        return elems

    def _cache_entry(self) -> Path:
        """這一組(指紋 × 亮暗 × 縮放)的快取資料夾。

        ⚠️ **一組一個資料夾、各自帶一份 `sprites.json`**,不是共用一份總表:共用的話
        每加一種縮放都要 read-modify-write 那份總表,而兩個視窗同時開起來就會互相蓋掉。
        ⚠️ 縮放用 `.3f` 而不是 `%g`:`winfo_fpixels` 回的是浮點數(見 `SKIN_SCALE_TOL`),
        檔名要能穩定重現同一個字串。"""
        return (skin_cache_root() / skin_cache_key()
                / f"{self.mode}@{self.scale:.3f}")

    def _from_cache(self):
        """讀快取。⚠️ 讀不到、壞了、指紋算不出來都只是回 None,一路交給當場畫。"""
        try:
            entry = self._cache_entry()
        except Exception:
            return None          # 連指紋都算不出來(`scripts/` 不在):當作沒有快取
        return self._from_assets(entry, "cache") if entry.is_dir() else None

    def _save_cache(self, make_skin, imgs, elems) -> None:
        """把剛畫好的那一組存成**與出貨資產同格式**的一個快取資料夾。

        ⚠️ **整支包在自己的 try 裡、失敗完全不作聲**:快取是純加速,家目錄唯讀、磁碟
        滿、防毒擋寫入都不該讓畫面掉皮膚——呼叫端已經拿到圖了。

        ⚠️ **`sprites.json` 最後寫,而且要原子換上**:讀取器是以它為入口的,先寫它就
        會出現「總表指到一張還沒寫完的 PNG」那個窗口。⚠️ 同理 PNG 也走 `os.replace`,
        不要就地開檔寫——半截的 PNG 會被下一次啟動讀進來(`_from_assets` 雖然接得住,
        但那是把一次可以避免的失敗留給例外處理)。

        ⚠️ 存的是**還沒收口**的 `elems`:`_fit_pill_plates` 那道收口跟真實 DPI 有關,
        烘進快取就變成「別台機器的字型度量」。它跑在 `install()` 裡、這一支之後。"""
        try:
            entry = self._cache_entry()
            entry.mkdir(parents=True, exist_ok=True)
            sheet, rects = make_skin.pack(imgs)
            name = f"skin-{self.mode}@{self.scale:.3f}x.png"
            # ⚠️ 暫存檔名要帶 pid:兩個視窗同時開起來、又剛好是同一個縮放時,共用一個
            # `.part` 會讓 A 把 B 寫到一半的內容 `replace` 成正本(讀取器接得住——壞掉
            # 的 JSON 回 None 交給當場畫——但那是本來就避得掉的一次浪費)。
            tmp = entry / f"{name}.{os.getpid()}.part"
            # ⚠️ `format="PNG"` 不可省:暫存檔的副檔名是 `.part`,Pillow 認不出來就丟
            # `ValueError`——而這一支的例外是**整支吞掉**的,症狀會是「快取永遠是空的、
            # 每次啟動照樣重畫」,沒有任何訊息(NotebookLM_OCR 第一版就是這樣)。
            sheet.save(tmp, format="PNG", optimize=True)
            os.replace(tmp, entry / name)
            meta = {
                "version": make_skin.SCHEMA_VERSION,
                # ⚠️ 只有一格:讀取器照 `SKIN_SCALE_TOL` 比對,對不上就回 None,所以
                # 快取不必也不該假裝自己蓋得住別的縮放
                "scales": [self.scale],
                "variants": {f"{self.mode}@{self.scale:g}": {
                    "file": name, "sprites": rects, "elements": elems}},
            }
            tmp = entry / f"sprites.json.{os.getpid()}.part"
            tmp.write_text(json.dumps(meta, ensure_ascii=False, sort_keys=True),
                           encoding="utf-8")
            os.replace(tmp, entry / "sprites.json")
            self._prune_cache()
        except Exception:
            pass

    def _prune_cache(self) -> None:
        """把**別的指紋**那幾個資料夾整個刪掉。

        ⚠️ 這是快取有沒有上限的唯一一道門:指紋跟著 `make_skin.py` / `palette.py` 走,
        改一次就換一個目錄名,不清的話每次改皮膚都在使用者的家目錄裡多留一份再也不會
        被讀到的舊圖。同一個指紋底下那幾組(亮/暗 × 這台機器的縮放)是有界的,留著。"""
        keep = skin_cache_key()
        for d in skin_cache_root().iterdir():
            if d.is_dir() and d.name != keep:
                shutil.rmtree(d, ignore_errors=True)

    def _drawn(self):
        """資產的縮放對不上(或壞了)就 import 產生器現畫一份。

        ⚠️ 這裡用的是**實際的**縮放倍率,不必貼齊資產那八檔——當場畫本來就沒有「只有
        幾種尺寸」的限制,所以任何 DPI(含 Windows 11 的自訂縮放)畫出來都是對的。

        ⚠️ 畫完**順手存進快取**:這一趟比讀資產慢一個量級,而同一台機器的縮放不會天天
        變。存的時機在轉成 `PhotoImage` **之前**——那時手上還是 PIL 影像,正好是
        `pack()` 吃的東西。

        ⚠️ 走不通的兩種情況都在 `except` 裡安靜收掉,交給 `install()` 的最後一條路:
        沒有 Pillow(使用者沒重跑 `uv sync`)、`scripts/` 不在(打包成 wheel)。"""
        try:
            make_skin = import_make_skin()
            imgs, elems = make_skin.build_variant(self.mode, self.scale)
            self._save_cache(make_skin, imgs, elems)
            self._keep.clear()
            cut: dict[str, tk.PhotoImage] = {}
            for key, im in imgs.items():
                buf = io.BytesIO()
                im.save(buf, "PNG")
                cut[key] = self._photo(data=base64.b64encode(buf.getvalue()))
            elems = {name: dict(e, states=[(s, cut[k]) for s, k in e["states"]])
                     for name, e in elems.items()}
            self._adopt(cut)
        except Exception:
            return None
        self.source = "drawn"
        return elems


def skin_cache_root() -> Path:
    r"""當場畫出來的皮膚存在哪:`%LOCALAPPDATA%\MP4-2-SRT\skin`。

    ⚠️ 走 `paths.appdata_root()`,與模型快取並排(models / skin),使用者要清東西時
    只有一個地方——而**那個資料夾名只有 `paths.APP_DIR_NAME` 一份**。
    ⚠️ 環境變數那條是**給測試用**的(慣例同 `filelog.LOG_DIR_ENV`):測試不可以在
    使用者真正的家目錄裡長出東西,更不可以把它 `_prune_cache` 掉。
    ⚠️ 這一支**不可以丟例外**:呼叫端全都在「純加速」的路徑上。`appdata_root()`
    在沒有 `LOCALAPPDATA` 的環境會自己退到 `~/.cache`,不會丟。"""
    override = os.environ.get(skin_cache_env())
    return Path(override) if override else paths.appdata_root() / "skin"


def skin_cache_key() -> str:
    """快取的指紋:**畫出來的像素只要可能不一樣,這個字串就要不一樣。**

    影響像素的有**三**份原始碼:下游的 `scripts/make_skin.py`(要產哪幾張、每一張多大)、
    `winkit.skingen`(超橢圓取樣、九宮格、膠囊、sprite 佈局、格式版號)與 `winkit.palette`
    (顏色)。⚠️ **2026-08-28 幾何搬進共用包時多了第二支**——漏掉它的話,改了超橢圓或
    九宮格,快取卻**不會失效**,於是「改了程式、畫面沒變」。
    ⚠️ **直接雜湊那幾支的位元組**,不要自己列一份「有哪些常數
    會影響輸出」的清單——那份清單一定會漏(`SQ_N`、`SQ_H_RUN`、`SQ_PAD`、`pill()` 的
    畫法、`pack()` 的排列…),而漏掉的那一項就是「改了程式、畫面沒變」這種查不出成因
    的災情。多雜湊一點的代價只是**改過那兩支之後第一次啟動重畫一次**。

    ⚠️ `skingen.SCHEMA_VERSION` 不必另外雜湊:它就住在被雜湊的那幾支裡。"""
    h = hashlib.sha256()
    for path in _pixel_sources():
        h.update(path.read_bytes())
    return h.hexdigest()[:16]


def _pixel_sources() -> tuple[Path, ...]:
    """**畫出來的像素由這幾支原始碼決定。**

    ⚠️ 抽成一支函式不只是為了整齊——它讓測試驗得到「**每一支**都真的進了指紋」:
    寫死在 `skin_cache_key()` 裡的話,漏掉其中一支照樣綠(指紋仍會跟著被驗的那幾支
    變),而使用者付的代價是「改了程式、畫面沒變」。
    ⚠️ **2026-08-28 從兩支變三支**:幾何搬進 `skingen` 之後,只雜湊「下游的產生器 +
    色票」就會漏掉超橢圓與九宮格的改動。哪天再多一支,補在這裡、測試自動涵蓋。"""
    return (_generator(), Path(skingen.__file__), Path(palette.__file__))


def import_make_skin():
    """import `scripts/make_skin.py`(產生器不在套件裡,要先把 `scripts/` 放進路徑)。

    ⚠️ **只有「當場畫」那條路會用到**:讀資產不需要產生器、也不需要 Pillow。
    ⚠️ `finally` 裡的 `sys.path.pop(0)` 不可省,也不可改成 `remove()`——插在最前面就
    從最前面拿掉,才不會在重複呼叫時愈積愈長、或誤刪別人放的同名路徑。
    ⚠️ 測試也走這一支,這樣驗到的才是**真正會出貨的**匯入路徑,而不是另一份抄本。"""
    sys.path.insert(0, str(_generator().parent))
    try:
        import make_skin
    finally:
        sys.path.pop(0)
    return make_skin


def _meta(root: Path | None = None) -> dict:
    """讀一個資產目錄的總表。`root` 預設是出貨資產,快取目錄走的是同一支。

    ⚠️ 預設值走 `None` 而不是直接寫 `skin_dir()`:預設參數在 **def 執行當下**求值,
    而那是 import 的時候——那時下游還沒 `bind()`。"""
    root = root or skin_dir()
    return json.loads((root / "sprites.json").read_text("utf-8"))


def _variant(mode: str, scale: float, root: Path | None = None,
             *, exact: bool = False, schema: int | None = None) -> dict | None:
    """挑最接近這個顯示縮放的那一檔。`exact` 時差太多就回 None。

    ⚠️ **`schema` 對不上就當作沒有這份資產**(2026-08-28 補;下游沒宣告
    `spec.SKIN_SCHEMA` 就不驗,行為與以前一樣)。理由是「**舊資產配新程式**」是可達
    的:使用者換電腦的方式是複製專案資料夾,只覆蓋 `.py` 而留著舊的 `assets/` 完全做
    得到。舊檔的每一個 key 都還在,不擋的話這裡會**成功**回傳、`source` 還報
    `assets`,把不相容的元件定義裝上去——沒有例外、沒有 log(NotebookLM_OCR
    2026-08-27 把 `border` 從 int 改成四元組時就是這種變更)。⚠️ 那支「資產＝現在的
    產生器」測試看不到這件事:它只比本機這一份。
    ⚠️ **版號是下游各自的**,不是共用包的:兩個下游的資產格式今天真的不同(一個純量
    內距、一個四元組),而這個號碼的用途是讓**那一支**的舊資產失效。

    ⚠️ 那八檔正好對上 Windows 給得出來的 100~300%,所以實務上幾乎永遠是精確匹配
    (⚠️ 只是「幾乎」:Tk 問到的 DPI **本質上帶小數**,150% 的機器實測是
    `1.4985250737463127` 而不是 1.5——`SKIN_SCALE_TOL` 就是為這個留的)。

    ⚠️ **`exact` 只在有後路時才准開。** 這道把關回 None 之後必須有人接得住——現在接
    的是「照真實縮放當場畫一份」(`install()` 的第 3 條路)。⚠️ 而且 `install()` 的第 4
    條仍然用 `exact=False` 再問一次:連 Pillow 都沒有時,貼最近那一檔還是比**整個沒有
    皮膚**好——為了幾種罕見縮放下按鈕矮一點點,賠掉全畫面的圓角與色票,不划算。

    膠囊化之後為什麼非有這道不可:底板的圖高就是元件的高度,而樣式內距與點數字型走的
    是**真實 DPI**。貼到隔壁那一檔等於讓元件比底板高,下半個圓被裁掉——實測 250% 貼
    2.0x 檔時 Run 83 對底板 80、一般按鈕 65 對 60,300% 更差。`_fit_pill_plates` 與
    `_pill_fits` 那兩道收口救得回「不被裁」,但救不回「按鈕矮一階」。"""
    meta = _meta(root)
    if schema is not None and meta.get("version") != schema:
        return None
    best = min(meta["scales"], key=lambda s: abs(s - scale))
    if exact and abs(best - scale) > SKIN_SCALE_TOL:
        return None
    return meta["variants"][f"{mode}@{best:g}"]


def ui_font_family(root: tk.Misc) -> str:
    """介面要用的字型家族名(挑不到就讓 Tk 用系統預設,不要硬塞不存在的名字)。"""
    fams = set(tkfont.families(root))
    for fam in (UI_FONT, UI_FONT_FALLBACK):
        if fam in fams:
            return fam
    return tkfont.nametofont("TkDefaultFont").actual("family")


def _strip_field(st: ttk.Style, style: str) -> None:
    """把最外層那顆背景元件拆掉,只留它包著的結構(其餘 layout 原封不動)。

    給 `Treeview` 用的。清單的框 2026-08-27 改由外層 Frame 畫(見 SKIN_FRAMES 的
    `Sunken.TFrame`),它自己那顆 `Treeview.field` 就變成**框中框**了——有皮膚時是
    兩圈圓角疊在一起,沒皮膚時是 sv_ttk 那張方角的輸入框圖戳在圓角框裡。拆掉之後
    空白區露出來的是 style 的 `background`(那是 Tk 視窗本身的底,與列的底色同一
    個選項),所以它必須跟框的底色一致。

    ⚠️ 一定要在 `SquircleSkin.install()` **之後**呼叫:那邊會照 SKIN_SWAPS 重寫
    layout,先拆會被蓋回去。
    ⚠️ **有沒有皮膚都要拆**:外層那個框在兩種情況下都在(沒皮膚時是實色方框)。
    """
    try:
        lay = st.layout(style)
        if len(lay) == 1 and lay[0][1].get("children"):
            st.layout(style, lay[0][1]["children"])
    except tk.TclError:
        pass          # 佈景結構跟預期不一樣:純外觀,不值得把程式帶下水


def apply(root: tk.Misc, scale: float,
          spec) -> tuple[str, dict, SquircleSkin | None]:
    """設定字型與佈景,回傳 (字型家族名, 色票, squircle 皮膚)。

    字型走 **Tk 的具名字型**(TkDefaultFont…):所有 ttk 控制項預設就吃這幾個,
    改一次全部跟著換,不必逐個 widget 設 font。

    ⚠️ `spec` 是**下游那支 app 的皮膚規格**(有哪幾種按鈕、配哪張皮、什麼字級)。
    機制在這裡、規格在那裡——那正是兩支程式該一樣與該不一樣的分界。"""
    def px(n: float) -> int:
        return max(1, int(round(n * scale)))

    fam = ui_font_family(root)
    for name, size in (("TkDefaultFont", 10), ("TkTextFont", 10),
                       ("TkMenuFont", 10), ("TkHeadingFont", 10),
                       ("TkIconFont", 10), ("TkTooltipFont", 9),
                       ("TkCaptionFont", 10), ("TkSmallCaptionFont", 9),
                       ("TkFixedFont", 10)):
        try:
            tkfont.nametofont(name, root).configure(family=fam, size=size)
        except tk.TclError:
            pass

    mode = winui.preferred_theme_mode(PALETTES)
    pal = dict(PALETTES[mode])
    st = ttk.Style(root)
    try:
        import sv_ttk
        sv_ttk.set_theme(mode, root)
    except Exception:
        # 佈景載不起來(沒跑過 uv sync、Tcl 版本不合):字型已經換好了,版面照舊,
        # 只是回到 Windows 原生長相
        pal["page"] = st.lookup("TFrame", "background") or pal["page"]
        return fam, pal, None

    # ⚠️ **切完佈景要自己補一發 `<<ThemeChanged>>`**:sv-ttk 的顏色不是寫在佈景
    # 定義裡,而是掛在那個事件上的 configure_colors 設的,而 Tk 8.6.15 在
    # `ttk::style theme use` 時**不會**把事件送到根視窗(NotebookLM_OCR 實測:
    # set_theme 之後 `ttk::style configure .` 仍是空字串,補一發才有值)。少了這
    # 一行,ttk 控制項會沿用母佈景 clam 的淺灰——深色模式下就是一堆白底黑字的
    # 標籤散在深色視窗上。
    # ⚠️ **要先 update_idletasks()**:視窗還沒實體化之前,事件送到根視窗也不會
    # 觸發 class binding,而這支正好跑在整支程式最早的地方。
    root.update_idletasks()
    root.event_generate("<<ThemeChanged>>", when="now")

    # 佈景自帶的字型是 Segoe UI Variable、而且用**像素**指定(-14px):既不是要用
    # 的字型,在 DPI-aware 的 150% 下也會小一號(點數才會跟著 DPI 換算)
    for name, size, bold in (("SunValleyCaptionFont", 9, False),
                             ("SunValleyBodyFont", 10, False),
                             ("SunValleyBodyStrongFont", 10, True),
                             ("SunValleyBodyLargeFont", 12, False),
                             ("SunValleySubtitleFont", 14, True),
                             ("SunValleyTitleFont", 20, True)):
        try:
            tkfont.nametofont(name, root).configure(
                family=fam, size=size, weight="bold" if bold else "normal")
        except tk.TclError:
            pass
    st.configure(".", font=(fam, 10))

    # Sun Valley 沒有涵蓋到、或本專案要調整的幾處。⚠️ **垂直內距不在這裡設**,它要
    # 看皮膚裝不裝得起來(見底下 `_button_padding`)。字級與粗細跟收底板內距那兩處
    # 共用同一份規格(見 `BUTTONS`)——三份寫在三個地方時,漏改一處只有截圖看得出來。
    for style, b in spec.BUTTONS.items():
        st.configure(style, font=(fam, b.size, b.weight))
    # 「選擇檔案…」:靜止是白底藍框,所以字也要是藍的;滑過去整顆翻藍,**文字要在
    # 同一刻翻白**。
    st.configure(spec.CTA_STYLE, foreground=pal["cta_fg"])
    # ⚠️ `map` 不會與 `TButton` 那一份合併、是整個取代,所以 `disabled` 也要自己
    # 列——漏掉的話轉檔中被鎖起來的那顆會是一般的黑字,看起來還能按。
    # ⚠️ `pressed` 要排在 `active` 前面:按住不放時兩個狀態同時成立,而 ttk 取的
    # 是第一個對上的。
    st.map(spec.CTA_STYLE,
           foreground=[("disabled", pal["run_off_fg"]),
                       ("pressed", pal["on_accent"]),
                       ("active", pal["on_accent"])])
    # ⚠️ **由 `MUTED_FG_STYLES` 驅動,不要在別處另寫一行 `foreground=pal["muted"]`**
    # ——那個常數是下游拿去比對後綴的根名單,這裡多設一個而常數沒跟上,就等於讓下游
    # 那份檢查靜默漏一格(`tests/test_skin.py` 有一條在守這件事)。
    for _style in MUTED_FG_STYLES:
        st.configure(_style, foreground=pal["muted"])
    # ⚠️ 樣式名**必須以 `.Muted.TLabel` 結尾**才繼承得到說明文字的前景色:ttk 是
    # 照後綴一層層往上找的(`Hint.Muted.TLabel` → `Muted.TLabel` → `TLabel`)。
    # 取名成 `Hint.TLabel` 就只會繼承到 `TLabel`,顏色會掉回預設的黑。
    # ⚠️ **這條同時是寫「檢查」的人的坑,不只是寫樣式的人的**(2026-09-03 由 MP4-2-SRT
    # 那邊踩出來):要在下游列舉「哪些樣式吃 muted 前景」時,**判準要用後綴、不可以用
    # 字面比對**那幾個被 `configure` 的名字——直接設的只有 `MUTED_FG_STYLES` 那兩個,
    # 其餘全靠後綴繼承。照字面抓那次只抓到 2 個,而**漏掉的正好是拖放框那兩句**,也就是
    # 真的出過事的那兩個。⚠️ 失效方式很惡劣:測試是綠的、抓法看起來也對。
    # ⚠️ 2026-09-06 補:那份根名單現在是模組層的 `MUTED_FG_STYLES`,**下游改 import
    # 它、不要再手抄**——`str.endswith` 本來就吃 tuple,是一行的事。
    st.configure("Hint.Muted.TLabel", font=(fam, 9))
    # 視窗第一句。⚠️ 它坐在**視窗底**上(卡片之外),所以底色要跟著 `page`:
    # `ttk.Label` 是實色底、不是透明的,不指定就吃佈景的 #fafafa,那一行會是一塊
    # 淺色矩形壓在視窗底色上(兩者只差幾階,看起來像那一行「髒了」)。
    st.configure(spec.SUB_STYLE, font=(fam, 10, "bold"), background=pal["page"])
    # 卡片**裡面**的文字。⚠️ `ttk.Label` 是**實色底**的,不是透明:不指定的話它會
    # 吃佈景的視窗底色,坐在白卡上就是一塊塊淺灰矩形浮在白色裡。所以卡片上的每一種
    # 文字都要繼承得到 `Card.TLabel` 這一層的 background——樣式名的後綴一定要留
    # `.Card.TLabel`(ttk 是逐段剝前綴往上找的)。
    st.configure("Card.TLabel", background=pal["card"])
    st.configure("CardTitle.Card.TLabel", font=(fam, 10, "bold"))
    # ⚠️ 前景色**不在這裡設**,由上面 `MUTED_FG_STYLES` 那個迴圈統一設掉(ttk 的
    # `configure` 是累加的:同一個樣式分兩次設不同的選項不會互相清掉),這裡只剩字級。
    st.configure("CardHint.Card.TLabel", font=(fam, 9))
    # ⚠️ 詳情那行**檔名沒有自己的樣式**:它 2026-08-27 之前是 `CardName.Card.TLabel`
    # (10pt 粗體),使用者指定拿掉粗體之後那支就只剩「跟 `Card.TLabel` 一模一樣」,
    # 留著是空樣式——所以整支刪掉,`gui` 那邊直接用 `Card.TLabel`。⚠️ 它的字級因此
    # 是 `.` 的預設 10pt,而 `gui._detail_font` 拿那個數字去量省略,兩邊要一起改。
    # 卡片之間露出來的視窗底(見 gui._build_ui:不設就是 sv_ttk 的 #fafafa)
    st.configure("Page.TFrame", background=pal["page"])
    # ⚠️ **這是沒有皮膚時的後備**(squircle 裝不起來就退回實色的方角矩形)。有皮膚
    # 時這個值看不到——底板是不透明的,圓角外側已經畫進圖裡了(見 SKIN_FRAMES)。
    for style, _elem, own in spec.SKIN_FRAMES:
        st.configure(style, background=pal[own])
    # ⚠️ 卡片**裡面**的 Frame 要用這一支,不是 `Card.TFrame`:那一支的 layout 被
    # 換成了一張底板圖(`Sq.card`),每用一次就多畫一張帶邊框的小卡片。第一版
    # 把標題列、按鈕列、捲軸容器全設成 Card.TFrame,截圖上就是四條莫名其妙的
    # 橫線橫過卡片。這一支只換背景色、layout 照 ttk 原本的
    st.configure("CardBody.TFrame", background=pal["card"])
    # 清單:列高要塞得下色點與中文,標題列不要粗到搶戲。⚠️ **列高住在這裡不是
    # 版面那邊**:`rowheight` 是 style 層的**全表**設定,Treeview 給不了單列。
    # 28 是卡片化那次從 24 調上來的——舊版沒有留白,24 看起來剛好;放進有 24px
    # 內距的卡片裡就顯得密
    st.configure("Treeview", font=(fam, 9), rowheight=px(28),
                 fieldbackground=pal["log_bg"], background=pal["log_bg"],
                 foreground=pal["ink"], borderwidth=0)
    st.configure("Treeview.Heading", font=(fam, 9))
    # ⚠️ `map` 的 selected 要同時給前景:不給的話 sv_ttk 會把選取列的字翻成白,
    # 而我們的選取底色是淡藍(見 PALETTES 的 row_sel),白字在上面讀不到
    st.map("Treeview", background=[("selected", pal["row_sel"])],
           foreground=[("selected", pal["ink"])])
    # ⚠️ **下游自己的樣式掛在這裡**(2026-08-28 接第二個下游時加的):上面那一批是
    # 兩支程式共有的(說明文字、卡片、線框鈕、主要動作鈕),而各自多出來的那幾種
    # (核取方塊的底色、卡片上的狀態字、收合鈕的 `anchor`…)本來就該留在下游——它們
    # 正是「這一支長什麼樣」。
    # ⚠️ **位置在這裡是刻意的**:要在上面那批 `st.configure` **之後**(下游才蓋得掉
    # 共用的預設,例如兩支的提示文字字級不同),又要在 `_button_padding` **之前**——
    # 那一支算出來的垂直內距是膠囊的安全邊界,被下游事後蓋掉就是「下半個圓被削平」,
    # 而那不報錯、`reqheight` 也看不出來。所以掛勾裡**不要碰 padding**。
    if (extra := getattr(spec, "configure_styles", None)) is not None:
        extra(st, fam, pal, px)
    if mode == "dark":
        winui.use_dark_titlebar(root)
    # ⚠️ 一定要在 sv_ttk 切完佈景**之後**:image element 是建在「當下這個佈景」
    # 裡的,`ttk::style theme use` 一換就整批不見了。
    skin = SquircleSkin(root, scale, mode, fam, spec)
    ok = skin.install()
    _button_padding(st, root, fam, px, skin if ok else None, spec)
    # ⚠️ 拆殼要在 install 之後(它會照 SKIN_SWAPS 重寫 layout),而且不管有沒有
    # 皮膚都要拆:清單的框由外層那個 Frame 畫,見 `_strip_field`
    _strip_field(st, "Treeview")
    return fam, pal, (skin if ok else None)


def _pad_v(pad) -> int:
    """ttk `padding` 的**垂直**那一份(純量、二/三/四元組都吃得下)。

    ⚠️ **兩個下游的資產真的不同形**(2026-08-28 接第二個下游時撞到):MP4-2-SRT 的
    底板寫純量 `4`(四邊都套),NotebookLM_OCR 寫 `[左, 上, 右, 下]` 且上下是 0——那邊
    2026-08-27 發現膠囊的高度被 `height` 釘死之後,垂直內距換不到任何東西(字在裡面
    本來就置中),只會把「內容需求」灌高 2n、把餘裕吃掉。
    ⚠️ **沒有這一支的話,四元組會讓 `_pill_fits` 當場 TypeError**(`2 * [..]` 是串接、
    再拿去跟 int 相減),而它跑在 `install()` 的 try **之外**——症狀不是掉皮膚,是視窗
    整個開不起來。

    ttk 的規則:1 個值＝四邊、2 個＝(水平, 垂直)、3 個＝(左, 垂直, 右)、
    4 個＝(左, 上, 右, 下)。"""
    if not isinstance(pad, (list, tuple)):
        return int(pad)
    vals = [int(v) for v in pad]
    if len(vals) == 1:
        return vals[0]
    if len(vals) >= 4:
        return max(vals[1], vals[3])
    return vals[1]


def _with_pad_v(pad, v: int):
    """把 `pad` 的垂直那一份換成 `v`,形狀維持原樣(見 `_pad_v`)。"""
    if not isinstance(pad, (list, tuple)):
        return v
    vals = [int(x) for x in pad]
    if len(vals) == 1:
        return v
    if len(vals) >= 4:
        return [vals[0], v, vals[2], v]
    return [vals[0], v] + vals[2:]


# 膠囊內距的安全邊界(實體像素)。⚠️ 內容需求不只「字 + 兩份內距」:ttk 的按鈕
# layout 中間還隔著 `Button.focus`,而它的厚度是佈景給的、這裡問不到。留 2px 讓
# 下面那條算式站在保守的那一邊——內距少 1px 沒有人看得出來,多 1px 就是被削平的
# 半個圓。
_PILL_SAFETY = 2

def _pill_fits(plate_h: int, line_h: int, inner: int, want: int) -> int:
    """膠囊按鈕的垂直內距:`want` 與「塞得下的上限」取**較小者**(下限 0)。

    純算術,故意不碰 Tk——量字型那半在 `_pill_pad`,這一半才測得動(見
    `tests/test_gui.py::test_the_pill_padding_never_lets_content_outgrow_the_plate`)。
    `plate_h` 是底板圖高、`inner` 是底板自帶的內距(`make_skin.SQ_PAD`),兩個都來自
    **資產**;`line_h` 是字型的行高,跟著**真實 DPI** 走。"""
    return max(0, min(want, (plate_h - line_h - 2 * inner - _PILL_SAFETY) // 2))


def _pill_pad(skin: "SquircleSkin", elem: str, root: tk.Misc, font, want: int) -> int:
    """`_pill_fits` 的量測版:去問這個字型的行高與那張底板的尺寸。"""
    plate_h, inner = skin.plate.get(elem, (0, 0))
    if not plate_h:                  # 沒有這張底板(資產舊了):照原本的值走
        return want
    line_h = tkfont.Font(root, font=font).metrics("linespace")
    return _pill_fits(plate_h, line_h, inner, want)


def _button_padding(st: ttk.Style, root: tk.Misc, fam: str, px,
                    skin: "SquircleSkin | None", spec) -> None:
    """三顆按鈕的內距。⚠️ **垂直那一半要等 `install()` 跑完才算得出來。**

    有皮膚時按鈕的高度由膠囊底板**釘死**(`make_skin.SQ_H` / `SQ_H_RUN`),而元件高度
    取「內容需求」與底板圖高的**較大者**——內容(字 + 這裡的垂直內距 + 底板自帶的
    `SQ_PAD`)一旦撐過圖高,Tk 就把膠囊**裁切**,下半個圓當場被削平(不報錯、
    `reqheight` 也看不出來)。

    ⚠️ **所以這個值不可以寫死**(2026-08-27 code review 抓到)。膠囊化那一版把它從
    `px(7)`/`px(3)` 手調成 `px(4)`/`px(1)`,是拿五個縮放檔逐檔量出來的——而**資產的檔數再多也
    蓋不完顯示縮放**(當時只有五檔,現在八檔):`_variant` 挑的是**最接近**的那一檔,於是 225%/250%/
    300%/350%(Windows 11 顯示設定就有)與任何自訂縮放下,字型跟著真實 DPI 長大、底板
    卻停在 200% 那一檔。實測 App.scale=2.5 時內容比圖高多 2~3px、3.0 時多 16px,膠囊
    當場被裁掉下緣。改成由圖高反推之後,**每一種縮放都塞得下**:五個標準檔算出來的值
    與手調那組相同(按鈕高度本來就由底板釘死,內距在餘裕內怎麼給都不影響外觀),對不上
    的那幾檔則自動收到 0。⚠️ 殘留的一條:≥350% 連 0 都不夠(差 1px),那時膠囊會少掉
    大約一個像素——**已知、可接受**,對照組是改版前的 16px。
    ⚠️ **這是兩道防線裡的第二道**(使用者 2026-08-27 選定第一道):`make_skin.SCALES`
    同一天從五檔補到 **八檔**(加 225%/250%/300%,4K 面板上那三個都是 Windows 11 的
    標準選項),資產 350KB → 730KB(⚠️ 同日稍後移除三張沒有人觸發得了的皮之後是 **622KB**,見 `make_skin` 的「清單那個框」那一段)。第一道讓那三種縮放**精確匹配**——按鈕的大小是對的、
    圓角也是對的;這一道則保證**任何**縮放(350%、自訂縮放、將來的新檔位)都不會被裁,
    代價只是那幾種罕見縮放下按鈕看起來矮一點點。⚠️ 兩道都要留:少了第一道,225% 的
    按鈕會照 200% 那張圖來、視覺上矮一階;少了第二道,清單外的縮放就是被削平的半個圓。

    ⚠️ **沒有皮膚時要把降掉的那幾像素還回去**(同一次 review 抓到):`install()` 失敗
    (sv_ttk 裝不起來、資產不在、Tcl 版本不合——那幾條路本檔的 docstring 明講要撐得住)
    時沒有底板、也就沒有那個釘死的高度,按鈕的高度回到「內容需求」,照著膠囊的值走就是
    **整排按鈕矮 4~6 px**(實測 @100%:有皮膚 30/30/40、沒皮膚配新值 27/27/35、沒皮膚
    配舊值 31/31/41)。"""
    # ⚠️ 每一顆都**照自己那張底板**量(見 `BUTTONS`):線框鈕與一般按鈕今天同高,
    # 但那是 `make_skin.pill()` 給的巧合,不是保證。⚠️ 線框鈕也繼承不到
    # `Small.TButton`(ttk 剝的是後綴不是前綴),本來就要各設各的。
    for style, b in spec.BUTTONS.items():
        vpad = (px(b.vpad_bare) if skin is None
                else _pill_pad(skin, b.plate, root,
                               (fam, b.size, b.weight), px(b.vpad)))
        st.configure(style, padding=(px(b.hpad), vpad))
