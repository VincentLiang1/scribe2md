"""Apple 風視覺層與前端腳本(使用者指定 apple.com 風格;只有視覺與
js 常數,不含任何事件邏輯——接線都在 app.build_ui)。

色票對照設計稿:淺色 #f5f5f7 底/白卡片/#1d1d1f 文字/#0071e3 藍;
深色 #000 底/#1d1d1f 卡片/#0a84ff 藍。

注意:gradio 6 的 theme/css 必須在 launch() 傳入(app.main 負責),
放 Blocks 建構子只會被忽略(僅 deprecation warning、樣式悄悄落空,
測試守著)。遙測環境變數由套件 __init__(與 app.py 開頭)在任何
import gradio 之前設定,本模組可安全 import gradio。
"""
import gradio as gr

_APPLE_BLUE = gr.themes.Color(
    c50="#eaf3ff", c100="#d5e8fe", c200="#abcffd", c300="#77b0fa",
    c400="#3f8ef4", c500="#0071e3", c600="#0071e3", c700="#005bb8",
    c800="#00468e", c900="#003165", c950="#001f40",
)


def apple_theme() -> gr.themes.Base:
    """佈景只用 gradio 公開的 theme 變數(升版不破);膠囊按鈕、卡片陰影
    等 theme 管不到的細節由 APPLE_CSS 補。"""
    return gr.themes.Base(
        primary_hue=_APPLE_BLUE,
        neutral_hue=gr.themes.colors.gray,
        # 系統字體堆疊:mac 上是 SF Pro/PingFang,Windows 落到 Segoe UI+正黑
        font=["-apple-system", "BlinkMacSystemFont", "SF Pro TC", "SF Pro Text",
              "PingFang TC", "Segoe UI", "Microsoft JhengHei", "sans-serif"],
    ).set(
        body_background_fill="#f5f5f7",
        body_background_fill_dark="#000000",
        body_text_color="#1d1d1f",
        body_text_color_dark="#f5f5f7",
        body_text_color_subdued="#6e6e73",
        body_text_color_subdued_dark="#a1a1a6",
        background_fill_secondary="#e8e8ed",
        background_fill_secondary_dark="#2c2c2e",
        # 卡片:白底、大圓角、無邊框、極淡陰影;內距與卡片間距加大
        # (對齊 apple.com 卡片的留白比例,使用者截圖回報預設太擠)
        block_background_fill="#ffffff",
        block_background_fill_dark="#1d1d1f",
        block_border_width="0px",
        block_radius="22px",
        block_padding="24px 28px",
        block_shadow="0 1px 3px rgba(0,0,0,0.06)",
        block_shadow_dark="0 1px 3px rgba(0,0,0,0.5)",
        block_title_text_weight="600",
        layout_gap="20px",
        panel_background_fill="#ffffff",
        panel_background_fill_dark="#1d1d1f",
        panel_border_width="0px",
        input_background_fill="#f5f5f7",
        input_background_fill_dark="#2c2c2e",
        input_border_color="rgba(0,0,0,0.10)",
        input_border_color_dark="rgba(255,255,255,0.14)",
        input_radius="10px",
        # 膠囊按鈕;主=Apple 藍、次=灰底藍字、停止=灰底紅字(低調破壞性)
        button_large_radius="999px",
        button_medium_radius="999px",
        button_small_radius="999px",
        button_primary_background_fill="#0071e3",
        button_primary_background_fill_hover="#0077ed",
        button_primary_background_fill_dark="#0a84ff",
        button_primary_background_fill_hover_dark="#3395ff",
        button_primary_text_color="#ffffff",
        button_secondary_background_fill="#e8e8ed",
        button_secondary_background_fill_hover="#dcdce1",
        button_secondary_background_fill_dark="#2c2c2e",
        button_secondary_background_fill_hover_dark="#3a3a3c",
        button_secondary_text_color="#0071e3",
        button_secondary_text_color_dark="#0a84ff",
        button_cancel_background_fill="#e8e8ed",
        button_cancel_background_fill_hover="#dcdce1",
        button_cancel_background_fill_dark="#2c2c2e",
        button_cancel_background_fill_hover_dark="#3a3a3c",
        button_cancel_text_color="#d70015",
        button_cancel_text_color_dark="#ff453a",
        checkbox_background_color_selected="#0071e3",
        checkbox_background_color_selected_dark="#0a84ff",
        checkbox_border_color_selected="#0071e3",
        checkbox_border_color_selected_dark="#0a84ff",
        loader_color="#0071e3",
        loader_color_dark="#0a84ff",
        # 角落標籤(File/Files 等的 BlockLabel 徽章)改成「卡片內標題」樣式,
        # 與 Textbox 的標題(如「逐字稿預覽」)一致——使用者截圖回報徽章
        # 擠在角落很怪;圖示另由 APPLE_CSS 的 .titled-label 規則隱藏
        block_label_background_fill="transparent",
        block_label_background_fill_dark="transparent",
        block_label_border_width="0px",
        block_label_shadow="none",
        block_label_text_color="#1d1d1f",
        block_label_text_color_dark="#f5f5f7",
        block_label_text_weight="600",
        block_label_text_size="*text_md",
        block_label_margin="16px",
        # 表格(與會名單)圓角:對齊輸入框的圓角感,角落不再是直角
        table_radius="14px",
    )


# theme 變數搆不到的部分。選擇器策略:錨定自家 elem_id(#app-header/#main-tabs)
# 與 gradio 跨版本沿用的 .selected;內容區按鈕以 variant class(.primary/.stop/
# .secondary)排除。若日後 gradio 改內部結構,這些規則只會失效回預設樣式,
# 不影響任何功能。
APPLE_CSS = """
/* 捲軸的位置永遠留著,不管當下需不需要(2026-08-12 使用者回報「點『重設
   講者』整個頁面會移位,感覺像抖一下」)。成因與分段控制項無關:三種模式的
   內容高度差很多,而「重設講者」最短(一個路徑欄 + 一顆按鈕),撐不出一頁
   → 垂直捲軸消失 → 可用寬度多出捲軸那 15px → 1240px 的置中版面重算,
   整頁橫移約 7px。**只有那一個模式會**,所以它看起來像那顆按鈕的毛病。
   `scrollbar-gutter: stable` 是為此而生的標準屬性(Chrome 94+):永遠保留
   那道槽、但不畫軌道。⚠️ 不要改用 `overflow-y: scroll` 達成同樣效果——
   那會讓 Windows 上的灰色捲軸軌道**一直**顯示在畫面右緣。
   ⚠️ 必須下在 `html` 上:捲的是文件本身(.gradio-container 另有一條把
   overflow 改成 clip 的規則,那是 sticky 目錄用的,與這裡無關)。
   實測(Playwright,1600x900,切四次模式量 .gradio-container 的 left):
   修正前 left 在 **180 ↔ 173** 之間跳(捲軸 15px、橫移 7px),修正後恆為 173;
   而該有捲軸的模式仍然是 15px——沒有變成「永遠畫一條軌道」。
   ⚠️ **這條規則在 headless 瀏覽器下驗不出來**:headless chromium 用的是不佔
   寬度的 overlay 捲軸,拿掉這條規則也照樣不橫移(對照組實測),`--disable-
   features=OverlayScrollbar` 與 `::-webkit-scrollbar{width}` 兩種強制法都無效
   ——**只有真視窗(headless=False)重現得出來**。要改動這一帶時記得這件事,
   否則會拿到一個「怎麼測都是綠的」的結論。 */
html { scrollbar-gutter: stable; }

/* 版面「固定寬度」1240px、置中(視窗更窄才跟著縮)。gradio 6 的容器是
   .fillable,原生上限是響應式階梯(1280/1536/1920px 隨視窗跳級)——寬螢幕上
   版面會突然變寬,使用者回報「被撐大」的感覺即來自於此;鎖定值蓋過階梯 */
.gradio-container, .fillable {
  width: 1240px !important;
  max-width: min(1240px, 100%) !important;
  margin: 0 auto !important;
}

/* 標題置中,像 Apple 產品頁開場;資訊列縮成一行小灰字 */
#app-header { text-align: center; box-shadow: none; background: transparent; }
#app-header h1 { font-weight: 700; letter-spacing: -0.015em; margin-bottom: 4px; }
#app-header p { color: var(--body-text-color-subdued); }

/* 分頁列:去底線、選中頁籤膠囊化(仿 segmented control)。
   「基底 + 差異」兩層,主分頁列與「聲音→MD」內的子分頁列共用基底:
   錨定 **tablist 底下的按鈕**(`.tab-nav` 與 `[role="tablist"]` 兩種寫法
   都列,對沖 gradio 改版——任一還在,樣式就還在),而 #audio-tabs 是
   #main-tabs 的後代,所以一條就同時蓋到兩層。
   這個錨法順帶除掉兩份硬編清單:gradio 的 variant class 名
   (.primary/.secondary/.stop)與 :not([role="tabpanel"] *)——內容區的按鈕
   不可能是 tablist 的後代,不必逐一排除。也因為兩層走同一條,shared 的值
   只有一份:改圓角/間距/subdued 色票時不會漏掉其中一層。
   代價是按鈕的膠囊樣式跟著 tablist 錨點走了(先前只錨 #main-tabs button);
   兩個錨點都消失時整排退回 gradio 預設樣式——純外觀降級,不影響功能,
   與本檔其餘規則同一個取捨。 */
#main-tabs :is(.tab-nav, [role="tablist"]) {
  border: none !important; justify-content: center; gap: 4px;
}
#main-tabs :is(.tab-nav, [role="tablist"]) button {
  border: none !important; border-radius: 999px !important;
  padding: 6px 18px !important; color: var(--body-text-color-subdued);
}
#main-tabs :is(.tab-nav, [role="tablist"]) button.selected {
  background: var(--background-fill-secondary) !important;
  color: var(--body-text-color) !important; font-weight: 600;
  box-shadow: 0 1px 4px rgba(0,0,0,0.10);
}

/* 子分頁列(#audio-tabs)只寫「與主分頁列不同」的那幾個值:靠左、字小一號、
   內距小一圈、選中不浮起。差異必須有 !important:兩條選擇器特異度相同,
   單靠「後寫的贏」在規則被搬動時會靜默失效。
   為什麼要有差異:再來一排等寬置中的膠囊,會讓人分不出哪一排是主層級。 */
#audio-tabs :is(.tab-nav, [role="tablist"]) {
  justify-content: flex-start !important;
}
#audio-tabs :is(.tab-nav, [role="tablist"]) button {
  padding: 4px 14px !important; font-size: 13px !important;
}
#audio-tabs :is(.tab-nav, [role="tablist"]) button.selected {
  box-shadow: none !important;
}

/* 模型「快速/精準」:仿 segmented control(灰膠囊底、白色浮起的選中段) */
.seg-radio .wrap {
  background: var(--background-fill-secondary); border: none;
  border-radius: 999px; padding: 3px; display: inline-flex; gap: 2px;
}
.seg-radio label {
  border: none !important; background: transparent !important;
  border-radius: 999px !important; box-shadow: none !important;
  padding: 4px 16px !important;
}
.seg-radio label:has(input:checked) {
  background: var(--block-background-fill) !important;
  box-shadow: 0 1px 3px rgba(0,0,0,0.12) !important; font-weight: 600;
}
.seg-radio input[type="radio"] { display: none; }

/* 「要做什麼」與「收音情境」撐滿卡片寬、每段等分(2026-08-12 設計稿方案 A)。
   起因是使用者圈出兩排灰槽的右緣對不齊:`.seg-radio .wrap` 是 inline-flex,
   寬度由內容決定,而「只錄電腦聲音」比「重設講者」多兩個字,那兩個字就是
   落差。等分之後齊頭是**自然結果**——沒有任何寫死的寬度要維護,換字型、
   改瀏覽器縮放、Windows 顯示比例變動都不會再歪(當時的 C 案是硬編
   min-width,那條路歪掉時沒有任何測試會紅)。
   ⚠️ **錨在這兩個 elem_id 上,不是 .seg-radio**:第三顆同樣掛 .seg-radio 的
   是「進階參數設定」裡的模型 快速/精準,它只有兩段,撐滿整張卡會空得離譜。
   日後新增的 seg-radio 也不該被這條無聲波及。
   ⚠️ **`justify-content` 與 `text-align` 兩個都要寫**:gradio 6.20 的 radio
   label 是不是 flex 容器隨版本而異,寫錯那個文字就會靠左而不是置中——兩條
   互不干擾,兩種結構都蓋得到。
   ⚠️ **`white-space: nowrap` 少不得**(2026-08-12 使用者當場截圖回報「只錄
   電腦聲音」折成兩行):`flex: 1` 是 `flex-grow:1 flex-shrink:1 flex-basis:0`,
   而 flex item 的 `min-width: auto` 只保得住 **min-content** 寬度——中文
   **可以在任意字元之間斷行**,所以它的 min-content 只有一個字寬,那一段就被
   壓到 1/3 卡片寬並折行。加上 nowrap 之後 min-content = 整行寬,三段各自
   保住自己的內容寬、只平分**剩餘**空間:字數相同的那排仍是等分,「只錄電腦
   聲音」那排則是它稍寬一些——齊頭不受影響(灰槽仍撐滿卡片),而那正是要的。
   ⚠️ **`:has(label)` 是必要的**:`#source-mode .wrap` 會命中**兩個**元素
   (Playwright 實測 `wraps: 2`)——gradio 的進度追蹤器
   (`div.wrap[data-testid=status-tracker]`)也叫 .wrap,而且排在 radio 容器
   **前面**,所以不加條件時 `querySelector` 與 CSS 都會先抓到它。**這次它
   剛好沒出事**(它自己被 gradio 設成 flex + opacity:0 + position:absolute,
   我們寫什麼都看不出來),但把版面屬性套在一個不相干的絕對定位元素上沒有
   任何理由,而那種錯誤浮現時會長得跟這兩排完全無關。同 id/同 class 兩層是
   gradio 的常態,見 docs/dev/ui.md 第 1 節的 DOM 實勘。
   實測(最小重現 + Playwright,視窗 520~1400px 逐段掃):兩排右緣永遠相等、
   零溢出、無折行;1400px 下每段各 370px(真的等分)。 */
#source-mode .wrap:has(label), #rec-scenario .wrap:has(label) { display: flex; }
#source-mode label, #rec-scenario label {
  flex: 1; text-align: center; justify-content: center; white-space: nowrap;
}

/* 「使用說明」的目錄(2026-08-08 設計稿方案 A):.seg-radio 的直排版本,
   同一套語彙(灰底槽、選中那條浮起成白卡)換個方向,使用者才不必再學一種。
   ⚠️ 目錄**不編號**(使用者指定):八篇不是先後順序,標上 1234 等於謊稱
   它是個流程。認篇靠的是每一項前面的 emoji。 */
#help-nav .wrap {
  background: var(--background-fill-secondary); border: none;
  border-radius: 14px; padding: 4px; display: flex;
  flex-direction: column; align-items: stretch; gap: 2px;
}
#help-nav label {
  border: none !important; background: transparent !important;
  border-radius: 10px !important; box-shadow: none !important;
  padding: 7px 12px !important; width: 100%;
}
#help-nav label:has(input:checked) {
  background: var(--block-background-fill) !important;
  box-shadow: 0 1px 3px rgba(0,0,0,0.12) !important; font-weight: 600;
}
#help-nav input[type="radio"] { display: none; }

/* 目錄跟著捲動黏在視窗上緣:**這是方案 A 之所以贏過另外兩案的那一點**
   ——內文捲到中段時還換得了篇。少了 sticky 就退化成「捲回最上面才能換」,
   那正是這次改版要修掉的毛病。
   align-self 是必要的:gradio 的 Row 是 flex 且預設 stretch,欄位被撐到
   與內文等高時 sticky 永遠不會觸發(沒有可捲的餘裕)。 */
#help-nav-col {
  position: sticky; top: 12px; align-self: flex-start;
}
/* ⚠️ 上面那條光靠自己是**無效**的(2026-08-08 Playwright 實測:捲 1400px
   後目錄的 y = -105,整個捲出視窗)。原因是 gradio 6.20 在
   .gradio-container 上寫了 overflow:hidden——那讓它成為 sticky 的
   scrollport,而它自己永遠不捲,於是 sticky 永遠不觸發(真正在捲的是
   document)。改成 overflow:clip 即可:clip 一樣裁切、但**不建立捲動容器**,
   sticky 於是回頭錨定 document。
   只在「使用說明」分頁生效,不動其他分頁的裁切行為;.selected 是既有
   CSS 早就在錨的跨版本 class,data-tab-id 由 gr.Tab(id=…) 明給。 */
.gradio-container:has(button[data-tab-id="tab-help"].selected) {
  overflow: clip;
}

/* gradio 頁尾整個藏掉:「設定」齒輪打開的是 gradio 內建設定頁,主題之外
   還有語言、通知等一堆選項(使用者指定:只要顏色,其他都不要);
   外觀設定改由自家 #theme-menu 提供(見 THEME_MENU_HTML) */
footer { display: none !important; }

/* 外觀設定(2026-07 設計稿選案 A):齒輪在「頁面」右上角,跟著頁面捲動
   (使用者指定:不要 fixed 一直黏在視窗上)。點開小浮窗,內含
   系統/深色/淺色 三段式切換(與 .seg-radio 同語彙),開合由 head 腳本切
   .open。position:absolute 讓這個 gr.HTML 區塊脫離版面流,放在 Blocks
   尾端也不佔空間、又錨定在頁面頂端。
   注意:區塊外觀必須整組 !important——gradio 6 在 .block 上寫了行內
   border-style:solid,一般規則的 border:none 會被行內樣式蓋過,只留下
   border-width 被 shorthand 重設成 medium(3px)的邊框(踩過:使用者
   回報右上冒出藍色粗框) */
#theme-menu {
  position: absolute !important; top: 14px; right: 18px; z-index: 100;
  width: auto !important; min-width: 0 !important;
  background: transparent !important; border: none !important;
  box-shadow: none !important; padding: 0 !important;
}
/* 內層容器自帶卡片內距(--block-padding 24/28px),不歸零的話透明區塊比
   齒輪大一圈,齒輪也會被推離角落 */
#theme-menu .html-container { padding: 0 !important; }
#theme-menu #theme-gear {
  width: 34px; height: 34px; border-radius: 999px; border: none; padding: 0;
  cursor: pointer; display: flex; align-items: center; justify-content: center;
  background: transparent; color: var(--body-text-color-subdued);
}
#theme-menu #theme-gear:hover {
  background: var(--background-fill-secondary); color: var(--body-text-color);
}
#theme-menu .theme-pop {
  position: absolute; top: 42px; right: 0; display: none; text-align: left;
  background: var(--block-background-fill); border-radius: 14px;
  border: 1px solid var(--input-border-color);
  box-shadow: 0 12px 32px rgba(0,0,0,0.22); padding: 14px 16px;
}
#theme-menu.open .theme-pop { display: block; }
#theme-menu .theme-pop-title {
  font-size: 12px; font-weight: 600; margin-bottom: 10px;
  color: var(--body-text-color-subdued);
}
#theme-menu .theme-seg {
  display: inline-flex; gap: 2px; padding: 3px;
  background: var(--background-fill-secondary); border-radius: 999px;
}
#theme-menu .theme-seg button {
  border: none; background: transparent; cursor: pointer; white-space: nowrap;
  border-radius: 999px; padding: 4px 16px; font-size: 13px;
  color: var(--body-text-color-subdued);
}
/* 「目前選擇」的打亮錨定 html 的 data-ms-theme——head 腳本在任何渲染前就
   設好,不必等 gr.HTML 掛載;三選項含「系統」,光看 body 的 dark class
   分不出「深色」與「系統剛好是深色」 */
html[data-ms-theme="system"] #theme-menu button[data-theme-choice="system"],
html[data-ms-theme="dark"] #theme-menu button[data-theme-choice="dark"],
html[data-ms-theme="light"] #theme-menu button[data-theme-choice="light"] {
  background: var(--block-background-fill); color: var(--body-text-color);
  font-weight: 600; box-shadow: 0 1px 3px rgba(0,0,0,0.12);
}

/* 說明文字與卡片/欄位邊緣留空隙(使用者截圖回報:貼邊的字被裁到) */
.pad-x { padding: 0 8px !important; }

/* 「進階參數設定」摺疊列:轉檔/錄音進行中連標題列(三角形)都不可按
   (使用者指定 2026-07-22)。訊號不能用區內 input:disabled——gradio 6.20
   的 Accordion「收合時把內容整個卸載」(Playwright 實測收合狀態區內
   input 數=0),摺疊收著按開始時 :has(input:disabled) 必失效。改錨
   「停止鈕亮著=有工作進行中」:#stop-btn(上傳轉檔;錄音收尾也亮它)與
   #rec-stop-btn(錄音中;F5 接回由 _restore_recording 重亮)可按時鎖標題,
   兩顆鈕的亮/滅與 _param_updates 的鎖定同一批訊息落地,免另接事件。
   已按「停止」的收尾空窗(停止鈕已灰、參數尚未解鎖)標題會先恢復可按,
   但區內控件仍鎖著,無實害;gradio 結構改版時本規則只會降級成
   「標題可按但內容仍鎖」,不影響功能。
   訊號鈕有三顆(#stop-btn 檔案轉逐字稿、#rec-stop-btn 錄音、#doc-stop-btn
   文字、圖像→MD),以 :is() 分組成一條規則,加第四顆時只改一個 token。
   摺疊區也有兩個(#adv-params 逐字稿、#doc-adv-params 文字、圖像→MD),
   同樣以 :is() 分組——照笛卡兒積展開會是六條。
   刻意**不**改成共用的 elem_classes:elem_id 落在 <button> 本體是既有 CSS
   與 js getElementById(...).disabled 雙重證實過的,而 elem_classes 落在
   button 本體還是外層 wrapper 沒有同等證據——一旦落在 div,
   :not([disabled]) 永遠成立,整條鎖定會**靜默**失效(本專案最怕的失敗
   方向)。:is() 的瀏覽器支援(2021)比這裡已在用的 :has()(2023)更保守 */
body:has(:is(#stop-btn, #rec-stop-btn, #doc-stop-btn):not([disabled]))
  :is(#adv-params, #doc-adv-params) > .label-wrap {
  pointer-events: none; opacity: 0.5;
}

/* 三個資料維護分頁(名單與聲紋 / 領域詞表 / 用詞替換表)任何工作進行中都
   不可按(使用者指定 2026-07-22;工作分頁與「使用說明」照常)——進行中改
   名單/聲紋/詞表,這一批轉檔也吃不到,徒生「即時生效」誤解。訊號同上
   (停止鈕亮著);錨點是 gr.Tab(id=...) 落在分頁鈕上的 data-tab-id
   (預設值是隨元件增減漂移的數字流水號,不可依賴,故各分頁都在建構時
   明給 id)。
   **新增資料分頁時這份清單要跟著加**,漏掉是**靜默**失效——分頁照樣可按,
   使用者是改完詞表才發現這批轉檔沒吃到。把關落在
   tests/test_app.py::test_tabs_locked_during_run_css:那裡要求每個分頁 id
   都被歸類(鎖 / 只在逐字稿工作時鎖 / 明列為不鎖),新分頁不分類就 fail。
   選擇器用後代不是直屬,所以「聲音→MD」底下的子分頁鈕照樣咬得到;而外層
   本身維持可按,才進得去(同下方「可看不可做」) */
body:has(:is(#stop-btn, #rec-stop-btn, #doc-stop-btn):not([disabled]))
  #main-tabs button:is([data-tab-id="tab-roster"], [data-tab-id="tab-hotwords"],
                       [data-tab-id="tab-lexicon"]) {
  pointer-events: none; opacity: 0.5;
}

/* 「文字、圖像→MD」分頁:逐字稿的轉檔/錄音進行中要鎖(兩邊都吃 CPU,
   而且 cancel.py 的取消旗標是全域單例——同時跑的話任一顆停止鈕會把兩邊
   一起殺掉,這是正確性問題不只是體驗問題)。反向則不鎖「聲音→MD」分頁:
   文件轉檔中仍可切過去看,真按下開始會被 app._run 的 _converting 檢查
   擋下並給繁中提醒(同 tab-run 一貫「可看不可做」的處理) */
body:has(:is(#stop-btn, #rec-stop-btn):not([disabled]))
  #main-tabs button[data-tab-id="tab-doc"] {
  pointer-events: none; opacity: 0.5;
}

/* 角落標籤改標題樣式的配套:隱藏標籤前的小圖示(標題不該有圖示)。
   titled-label 現在只掛在唯讀的下載區(上傳元件 2026-07-26 隨路徑輸入
   移除,只服務拖放區/上傳進度畫面的規則已一併刪除)。
   注意:不要動 File 內部的 min-width——檔案列的「大小」欄原生
   min-width:8rem,壓掉會截字並冒出水平捲軸(踩過) */
.titled-label label > span:first-child { display: none; }

/* 檔名列:File 元件不吃卡片內距,檔名原生只有 10px 邊距、貼著卡片邊
   ——首格要與角落標題的「文字起點」切齊:標題內縮 = 標籤 margin(16px)
   + 標籤自身 padding(--spacing-lg 8px)= 24px。之前寫死 16px 只對到標籤
   外框、檔名仍凸出一截(使用者截圖回報);改用變數計算,日後調
   block_label_margin 也不會再歪。尾格對稱 */
.titled-label .file-preview td:first-child {
  padding-left: calc(var(--block-label-margin) + var(--spacing-lg)) !important;
}
.titled-label .file-preview td:last-child {
  padding-right: calc(var(--block-label-margin) + var(--spacing-lg)) !important;
}

/* 元件下方的補充小字 */
.hint { color: var(--body-text-color-subdued) !important; font-size: 13px; padding: 0 8px !important; }

/* 下載區空的整塊藏起來、有檔案才出現(使用者指定 2026-07-23:套用後
   自動下載上線,閒置時的空框只是干擾)。錨 .empty(gr.Files 只在無檔案
   時渲染的佔位圖示區),方向刻意選「看到 .empty 才藏」而非「沒看到檔案
   列表就藏」:gradio 改版把類別改名時,前者只是退回「空框照常顯示」,
   後者會把有檔案的列表永遠藏死、使用者拿不到檔案。元件本身必須永遠
   掛載——它是套用後自動下載 js 的取址來源(gradio 6 對 visible=False
   整個不渲染、前端沒有值,同出聲載體鐵則),故只能 CSS 藏 */
#download-box:has(.empty) { display: none !important; }
/* 上一條的退路(:has 不支援的舊瀏覽器)空狀態瘦身:佔位圖示區
   (.empty.large)原生 min-height 約 236px(calc(var(--size-64) - 20px)),
   整張卡被撐到約 300px;gr.Files 的 height 參數只管「有檔案時」的列表,
   管不到這個佔位(踩過)。壓到 --size-28(112px)後卡片總高約 170px */
#download-box .empty { min-height: var(--size-28) !important; }

/* 命名列(下拉+試聽鈕同列):鈕與欄位垂直置中。整列的顯示跟著「命名框」
   走——gradio 6 對 visible=False 的元件是「整個不渲染」(Playwright 實測:
   隱藏列只剩一個空的 .form,無 .hidden 之類的標記 class),但包住它們的
   Row 仍是零高的可見空盒,會各吃一份 Column 的 layout_gap(20px×30 列
   疊出半頁空白)。列內渲染出 .block(=命名框可見)才顯示整列;
   :has 不支援的舊瀏覽器只是多出空白,功能不受影響 */
.name-row { align-items: center; }
.name-row:not(:has(.block)) { display: none !important; }
/* 按鈕列整列空(該列的鈕全隱藏)時收掉,不留 layout_gap 疊出的空白
   (同 .name-row 的 :has 技巧)。判斷用 <button> 而非 .block:
   gradio 6 的 Button 沒有 .block 類別,用 .block 判斷整列永遠被藏
   (實際踩過:切到現場收音後錄音鈕整排不見,使用者回報)。
   三列各在不同模式下會整列空,分組成一條規則(同三顆停止鈕的寫法):
   .rec-row 錄音雙鈕(檔案/重設講者模式空)、.src-pick-row 選檔三鈕、
   .run-row 開始轉檔/停止(後兩者在收音模式空)、.att-btn-row 合併追問的
   兩顆鈕(2026-08-09 加:預告在顯示、追問還沒出現時那一列是空的,
   不收就在決策區卡片裡多留 20px)。原本只收 .rec-row,
   另兩列各自靜靜吃掉 20px——使用者 2026-08-07 截圖回報「開始錄音」
   被擠出畫面,Playwright 實測那 40px 的間隔裡剛好有 20px 是空的
   .src-pick-row(按鈕底 759px vs 可視高度 736px,差 23px 就得捲)。
   ⚠️ 判準是「有沒有**可見的**鈕」(`button:not(.hidden)`)而不是「有沒有鈕」:
   gradio 6.20 對 visible=False 有兩種做法(Playwright 實測),**開頁**時
   整個不渲染(列內只剩註解節點),但**執行中切 visible**是把 <button> 留在
   DOM、只加一個 .hidden class(display:none)。只看 :has(button) 的話,
   開頁那次收得掉、切模式再切回來就收不掉了——舊的 .rec-row 規則正是
   如此(切到「轉錄音檔」後錄音列高 0 卻照吃 20px,沒人發現是因為它不影響
   任何按鈕的可見性)。gradio 改版若不再用 .hidden,退化成「切換後不收」
   =今天的行為,純外觀降級 */
:is(.rec-row, .src-pick-row, .run-row, .att-btn-row):not(:has(button:not(.hidden))) {
  display: none !important;
}
#rec-status { font-size: 16px; }

/* 收音模式:「要做什麼」與「收音情境+講者人數」兩張白卡貼成一張,兩段
   之間**只靠留白分開、不畫線**(使用者 2026-08-07 選案 A 合併、2026-08-08
   選案 C 定下無線的收法;目的是把「開始錄音」鈕拉進第一屏)。
   Playwright 實測:合併後按鈕底 759 → 710px,離 736px 的可視底端有 26px
   餘裕;字級與卡內留白一律不動(選案時的取捨:方案 C 再收留白能到 678,
   使用者選了保住 Apple 風的呼吸感)。
   ⚠️ **不要再加分隔線**(2026-08-08 使用者實際用過之後的指定,他先選了
   內縮線的 D、隔一輪就改回 C):那條線只有**現場收音**畫得出來——切到
   「轉錄音檔」「重設講者」時 #rec-scenario 根本不存在,同一個位置什麼都
   沒有。使用者的話是「只有現場收音有灰線,有點奇怪」。分段訊號在這裡是
   模式相依的,而模式是他一直在切的東西;留白到哪個模式都一樣。
   ⚠️ 貼合必須有條件:切到「轉錄音檔」時兩張卡中間會冒出路徑欄與選檔鈕,
   無條件貼合會讓下面那張卡用負 margin 壓到路徑欄上(三個模式都實測過)。
   判準是**四者相鄰**——上卡的 .form、.src-pick-row、.run-row、含
   #rec-scenario 的 .form 直接連在一起(2026-08-08 改成相鄰判準;
   2026-08-15 補上 .run-row,見下)。這個判準直接描述了「下一張卡真的貼在
   下面」這件事,而且在實測的三個模式下都對:切到轉錄音檔時 #rec-scenario
   **整個不渲染**(gradio 6.20 對 Radio 的 visible=False 是移除 DOM,不是加
   .hidden),而 .src-pick-row 後面接的是選檔摘要那個 .block.pad-x、
   不是 .run-row,兩邊都匹配不到 → 自動退回兩張卡。
   ⚠️ **中間夾著幾列,這裡就要寫幾列**(2026-08-15,「開始轉檔/停止」上移到
   「講者人數」之前時踩到):那一列在收音模式是整列空、被上面那條規則
   `display:none` 收掉——但**相鄰兄弟選擇器不看 display**,它照樣把 .form 與
   .form 隔開,漏寫一段 `+ .run-row` 整組貼合就**靜默**失效(症狀是收音模式
   變回兩張卡、「開始錄音」被擠下去,而沒有任何錯誤)。app.py 那一列的註解
   反向指回這裡,test_recording_mode_merges_two_cards_css 把「中間有哪幾列」
   與「規則裡寫了哪幾列」綁在一起守,新增一列就會紅。
   ⚠️ **三條規則的條件必須等價**(不可能「字面」一致——CSS 沒有前向兄弟
   選擇器,上卡那條只能寫成 `:has(+ …)`、下面兩條只能寫成 `+ …`):上卡
   歸零圓角、下卡貼上來,只成立一半就是「上卡下緣變直角卻沒人接」。
   ⚠️ 判準要帶 `:not(.hidden)`,理由**不是**今天會出錯,而是**猜錯的代價
   不對稱**:今天 #rec-scenario 在別的模式是整個不渲染(不會匹配,沒事),
   但 gradio 哪天改成「留在 DOM 加 .hidden」(50 行前那條空列規則面對的
   正是這種元件),`.form:has(> #rec-scenario)` 就會在**轉錄音檔模式**命中
   ——而那張卡在那個模式是**看得見的**(裡面裝著「講者人數」),負 margin
   會把它壓到路徑欄上。同一份 CSS 裡兩種隱藏心智模型並存,判準的詞彙就
   得統一,否則下一個讀者分不出哪條是家規。
   ⚠️ **`:has()` 不能巢狀在 `:has()` 裡**(規範禁止,含透過 `:not()` 間接),
   整條規則會被瀏覽器**靜默丟棄**——這正是舊判準的死因:
   `.form:has(> #source-mode):has(+ .src-pick-row:not(:has(button:not(.hidden))))`
   從 2026-08-07 寫下起就沒生效過(實測 #source-mode 的 padding-bottom
   一直是主題預設的 24px,不是規則裡的 14px),而同一份 CSS 裡
   `.src-pick-row:not(:has(...)) + .form` 這種**沒有巢狀**的寫法是好的,
   兩者長得很像,肉眼掃過去分不出來。
   ⚠️ **圓角歸零要改在 `.form`,內距才改在 fieldset**(2026-08-08 更正:
   原本兩者都寫在 #source-mode/#rec-scenario 上,圓角那半**完全沒作用**,
   使用者截圖回報「還是像兩張卡片」)。Playwright 逐列取像素才看清楚:
   卡片**中央**從頭到尾都是白的、交界只有分隔線那一列,但貼著卡片**左緣**
   量,交界上下 18px 是 (245,245,247)=頁面背景——兩張卡各自的圓角把交界
   咬出一個腰身,人眼讀到的「兩張卡」就是這個。原因是白卡的圓角根本不在
   fieldset 上(它的 border-radius 實測 0px):`.form` 才是 border-radius
   22px + **overflow: hidden** 的那一層,fieldset 只是被它裁圓的白底。
   padding 則相反,得改在 fieldset(.form 的 padding 是 0,改了不但沒用
   還把卡撐高——這是原註解唯一沒說錯的一半)。
   交界的留白 = 上卡的 padding-bottom(主題預設 24px,刻意不動)+ 下卡的
   padding-top(收到 20px)= 44px,與使用者選案時看到的那張圖一致;兩段
   之間沒有線,靠的就是這 44px。⚠️ 別為了省高度再往下收:沒有線的時候
   留白是**唯一**的分段訊號,收窄等於把兩組選項糊成一組。
   ⚠️ 覆寫 gradio 自家樣式時別只靠 !important 比大小:gradio 6.20 會**自動**
   給 css= 參數裡的每條規則加上 `.gradio-container.gradio-container-6-20-0
   .contain ` 前綴(前綴後的規則同樣帶 !important),特異度整整多三個
   class——臨時在瀏覽器 devtools/injected style 裡試覆寫時,不補上同樣的
   前綴會靜靜地不生效,看起來像「這條 CSS 沒用」(驗證方案時踩過)。
   :has 不支援的舊瀏覽器退回兩張卡,功能不受影響 */
.form:has(> #source-mode):has(+ .src-pick-row + .run-row + .form > #rec-scenario:not(.hidden)) {
  border-bottom-left-radius: 0 !important;
  border-bottom-right-radius: 0 !important;
}
.form:has(> #source-mode) + .src-pick-row + .run-row + .form:has(> #rec-scenario:not(.hidden)) {
  margin-top: calc(-1 * var(--layout-gap)) !important;
  border-top-left-radius: 0 !important;
  border-top-right-radius: 0 !important;
}
.form:has(> #source-mode) + .src-pick-row + .run-row + .form > #rec-scenario:not(.hidden) {
  padding-top: 20px !important;
}

/* 收音模式:把「開始轉檔/停止」那一列**在視覺上**排到錄音雙鈕之後
   (2026-08-15 使用者回報的 bug:「按下停止錄音並完成逐字稿後,停止按鈕
   出現的位置不對…將卡片上下擠開」,截圖圈的正是兩張卡中間多出來的那顆)。
   ⚠️ **那一列在收音模式並非永遠隱藏**——按下「停止錄音」之後的收尾期間,
   `_lock_for_rec_finish` 會把裡面的「停止」**臨時亮回來**(收尾要中止得有
   東西可按,見 app.py 該處),而它 2026-08-15 起的 DOM 位置正好在兩張卡
   中間,一亮出來就把貼合撐開 60px。⚠️ 這是**狀態相依**的破圖:三個模式
   的靜態畫面全部正常,只有「按下停止錄音之後」那一段才看得到——設計稿
   與驗收都只走靜態模式,所以漏掉了(同 2026-08-08「只有現場收音有灰線」
   那一條的形狀:跨狀態的元素要把**每一個狀態**都走過)。
   ⚠️ **修法刻意用 `order` 而不是搬 DOM**:搬回去等於把「開始轉檔」推回
   第一屏外(那正是這次要解決的問題),而收尾期間那顆鈕只是**借用**這一列。
   `order` 只改 flex 的視覺排序、不動 DOM,所以上面三條貼合規則的相鄰兄弟
   判準完全不受影響(實測:收尾期間 gap 仍是 0、圓角仍歸零)。
   ⚠️ **`#adv-params` 必須跟著設更大的 order**:order 的預設值是 0,只給
   .run-row 設 1 會讓它排到**所有** order:0 的後面——連「進階參數設定」
   摺疊區都在它前面。兩條要一起看,少一條就是換個地方錯位。
   ⚠️ 判準是「**後面**還有含 #rec-scenario 的 .form」= 收音模式(別的模式
   那顆 Radio 整個不渲染);寫成後續兄弟 `~` 而不是相鄰 `+`,因為中間隔著
   收音狀態列等元素。`:not(.hidden)` 的理由同上面三條。
   實測(視窗 1307×797):收尾期間「停止」從 360px(夾在兩張卡中間)回到
   718px(錄音雙鈕 658-698 的下一列),兩張卡 gap 60 → 0;切到「轉錄音檔」
   時判準不成立,「開始轉檔」照樣在 665px、「講者人數」之前。 */
.run-row:has(~ .form > #rec-scenario:not(.hidden)) { order: 1 !important; }
#adv-params { order: 2 !important; }

/* 命名區塊整體:容器永遠掛載(gradio 6.20 對 visible 會切換的容器有
   children 帶舊 props 重生的地雷,見 build_ui 註解),沒有任何渲染中的
   命名框(=轉檔前/套用後/跳過命名後)就整塊藏起。:has 不支援的舊瀏覽器
   退化為常駐顯示說明文字,不影響功能 */
#name-box:not(:has(.name-row .block)) { display: none !important; }

/* 命名進行中,連「進階參數設定」也收起來(使用者 2026-08-08 指出:那同樣
   是**下一份工作**的設定)。左欄其餘那一組由 `_naming_focus` 在伺服器端切
   visible,唯獨這個摺疊區走 CSS——它是**容器**,而 gradio 6.20 對「visible
   會切換的容器」會讓 children 帶舊 props 重生、同一批訊息裡的 children
   更新只有部分生效(見上方 #name-box 註解)。摺疊區裡正好裝著模型/CPU
   核心數,那些的可見性又由 _switch_source 依模式在管,撞上去就是一個
   只在某條路上出現的怪事。
   兩個錨點都不是新造的:#adv-params 是「轉檔中鎖住標題列」那條already
   在用的錨,#name-box:has(.name-row .block) 則是命名區自己的顯示判準,
   兩者同源才不會出現「命名區顯示了、摺疊區卻沒收」。
   ⚠️ 只錨 #adv-params:文件分頁的 #doc-adv-params 是另一條路,那邊沒有
   命名這回事。:has 不支援的舊瀏覽器退化成「摺疊區照常顯示」,不影響功能 */
body:has(#name-box .name-row .block) #adv-params { display: none !important; }

/* 名單表格要跟旁邊兩欄一樣是一張白卡(使用者 2026-08-09 回報「外觀不像
   其他卡片」,看過三案截圖後選 **A:白卡包著表格**)。
   ⚠️ **差的不是圓角,是白底與陰影**——Playwright 看真實 DOM 才問得清楚:
   旁邊「已登記的人」那張卡是 `.form`(圓角 22px + 陰影)包著
   `.block.padded`(白底 + padding 24/28);而 gr.Dataframe **沒有 .form
   那一層**,它的 `.block` 還帶 `hide-container`(背景 transparent、
   padding 0),所以只剩表格自己畫的 14px 灰框浮在頁面底色上。
   補的就是那張卡缺的三件事:白底、陰影、內距。
   ⚠️ **顏色與陰影一律走主題變數,不可寫死 #fff**:寫死的話深色模式會
   出現一張白卡(測試守著,突變 M110)。
   ⚠️ **內距 24/28 是抄隔壁那張卡的數字,不是隨手挑的**(2026-08-10 使用者
   截圖圈出表格「與卡片外圍留下的空白不夠」,看過四張實拍後選 C 案):原本
   12px 是 A 案「刻意留著內外兩層框」的產物,而實測隔壁 `.block.padded`
   正是 24px 28px——兩張卡並排時差一倍就看得出來。改這裡要連 `app.py` 的
   `max_height` 一起看,見下一條。 */
#att-table {
  background: var(--block-background-fill) !important;
  border-radius: 22px !important;
  box-shadow: var(--block-shadow) !important;
  padding: 24px 28px !important;
}

/* 表格自己那圈 1px 灰框拿掉(同上,C 案)。⚠️ 這是 gr.Dataframe 內建的
   **拖放區按鈕**在畫的(`.table-wrap > button`,圓角 14px),不是表格框線
   ——白卡把它包起來之後就成了「框中框」,而使用者圈的「擠」正是兩層框
   之間只剩 12px 的那道縫。留白補寬 + 內框拿掉是同一件事的兩面,只做一半
   都還看得出原來的毛病(四張實拍並排比過)。
   ⚠️ **改 border-color 不是 border-width**:那顆按鈕是拖放目標,拿掉框寬
   會讓 hover/拖檔時的高亮位移;透明只是不畫出來。
   ⚠️ 選擇器只錨結構(`.table-wrap > button`),**不可錨 svelte 的 class
   雜湊**(`.svelte-8prmba` 那種每次改版都會變)。突變 M122 守著。 */
#att-table .table-wrap > button { border-color: transparent !important; }

/* 改名的兩格(「要改名的人」/「改成」)是同一件事,中間不該有一條灰線
   (使用者截圖圈出 2026-08-08)。`gr.Group` 本來就是要讓內部貼合的,但
   Apple 風的白卡樣式畫在 **.block** 上(卡片外觀來自 block 背景+圓角,
   見上方註解),於是兩格各自成了一張白卡,中間露出底色就是那條線。
   解法同 .name-row 那條:把 Group 內的卡片感取消,讓 Group 自己當那張卡。
   規則只錨自家 elem_id,gradio 改內部結構時退回預設樣貌、不影響功能 */
/* ⚠️ **這一條是 Playwright 看真實 DOM 才修對的**(盲改來回三次都沒中,
   見 docs/dev/verification.md)。實測的結構是:

     #vp-rename-fields (.gr-group)   ← elem_id 落在這裡(而且有兩層同 id)
       └ .styler                     bg = 灰
           └ .form   gap: 1px        bg = 灰
               ├ .block              ← 兩格各自的白卡
               └ .block

   兩個教訓:**`.form` 不是 Group 的直接子元素**(中間隔著 `.styler`),
   寫成 `> .form` 從來沒中;而那條「灰線」其實就是 `.form` 的 **1px gap**
   ——縫隙露出底下 `.styler` 的灰。先前把 `.block` 設成 transparent 想
   「讓 Group 當那張卡」,反而是主動把整片灰露出來,於是整塊變灰底。

   正解只有一件事:**把 gap 收掉、讓兩張白卡直接貼合**。背景就用 block
   自己的(與旁邊「已登記的人」同一種白),不必也不該由 Group 代勞。 */
#vp-rename-fields .form { gap: 0 !important; }
/* 貼合處切平:圓角由外層 .gr-group 接手,否則兩張卡的圓角會在接縫處
   互相咬出缺口 */
#vp-rename-fields .block {
  border-radius: 0 !important;
  box-shadow: none !important;
}

/* 名單那一欄的「決策區」(改名預告 + 一起改聲紋的勾選;撞名時換成合併
   追問與確認鈕)。它必須**看起來跟旁邊的說明文字不一樣**:那是使用者
   唯一會被問到「聲紋要不要跟著改」的地方,而不改的後果(名單與聲紋庫
   從此對不上)要等下次開會那個人認不出來才會發現。
   容器**永遠掛載**、四個孩子都藏起來時整塊收掉——同 #name-box 那條:
   gradio 6.20 對「visible 會切換的容器」有 children 帶舊 props 重生的
   地雷,而空的容器仍會吃掉一份 layout_gap。
   ⚠️ 判準要帶 `:not(.hidden)`:gradio 6.20 對 visible=False 有兩種做法
   ——開頁時整個不渲染(選不到 .att-cue),執行中切換則是留在 DOM 加上
   `.hidden`。少了它,開頁那次收得掉、按過儲存之後就收不掉(那正是
   .rec-row 那條規則半失效過的原因)。 */
#att-decision:not(:has(.att-cue:not(.hidden))) { display: none !important; }
#att-decision {
  background: var(--block-background-fill) !important;
  border-left: 3px solid var(--button-primary-background-fill) !important;
  border-radius: 12px !important;
  box-shadow: var(--block-shadow) !important;
  padding: 12px 6px 14px 10px !important;
  gap: 6px !important;
}
/* 區塊內的元件不再各自是一張白卡:整個決策區才是那張卡(同 .name-row
   那條)。checkbox 自帶的底線也是這樣來的 */
#att-decision .block, #att-decision .form {
  background: transparent !important;
  box-shadow: none !important;
  border: 0 !important;
}

/* 命名區塊版面(2026-07-18 設計稿選案 A:單卡緊湊列表):所有講者收進
   「一張大卡片」——#name-box 本體就是卡片,列與列之間細分隔線;去掉
   每列自帶的卡片感與列間 gap(一人一卡時 7 位講者疊出兩個螢幕高,
   使用者回報)。列內的 form/block 轉為透明,試聽鈕才會貼在欄位右側
   (對齊設計稿的欄內 chip)。規則只錨定 #name-box/.name-row 之內,
   gradio 改內部結構時退回預設樣貌,不影響功能 */
#name-box {
  background: var(--block-background-fill) !important;
  border-radius: 22px !important;
  box-shadow: var(--block-shadow) !important;
  padding: 18px 22px !important;
  gap: 0 !important;
}
#name-box .pad-x { margin-bottom: 10px; }
/* 套用/跳過命名同列:與上方欄位的留白掛在「整列」上——掛在
   button.primary 上會只推下套用鈕,同列的次要鈕不動,兩顆一高一低
   (使用者截圖回報 2026-07-24) */
#name-box .apply-row { margin-top: 14px; align-items: center; }
/* 核對面板的那一列(改掛下拉 + 兩顆鈕)。⚠️ **置中規則要自己寫一份**:
   上面那條錨在 #name-box 底下,而核對面板在右欄、不在那棵樹裡——只掛
   同一個 class 不會生效,按鈕就會貼著下拉的頂端(使用者 2026-08-13 截圖
   圈出的第二處)。下拉有標籤、比按鈕高,不置中會歪得很明顯 */
#audit-panel .apply-row { margin-top: 4px; align-items: center; gap: 10px; }
/* 核對面板下半部收成一張卡(使用者 2026-08-14:「現在是分好幾塊,合起來,
   行距跟旁邊卡片一樣」)。做法同命名區的 .name-row:**內部的 block 透明、
   外框由這一層給**——每個元件各自帶白底圓角時,看起來就是散開的好幾塊。 */
.audit-actions {
  background: var(--block-background-fill);
  border-radius: 18px; padding: 12px 16px; gap: 4px !important;
}
.audit-actions :is(.block, .form, .styler) {
  background: transparent !important; box-shadow: none !important;
  border: none !important; padding: 2px 6px !important;
}
/* 元件的標籤首字被左上圓角切掉(使用者 2026-08-13 截圖:「逐段對照」的
   「逐」缺一角)——與 .name-row 那條**同一個坑**:水平內距歸零時,標籤
   就貼在圓角上,而圓角會把那個角落的像素切掉(那次是「講」剩半邊)。
   留 8px 水平內距即可,不必去動圓角本身 */
#audit-panel .block { padding-left: 8px; padding-right: 8px; }
/* 核對的出聲載體:與 #audition-player 同一套——移出畫面但保持渲染
   (gradio 對 visible=False 整個不渲染,前端沒有元件就不會出聲) */
#audit-player {
  position: absolute !important; left: -9999px !important; top: 0;
  width: 480px !important; opacity: 0; pointer-events: none;
}
/* 逐段對照表:字級調小一級(使用者 2026-08-13:「內容欄字體小一點,
   可以看到更多內容,也不用一直捲動」)。時間與長度用等寬數字對齊,
   一整欄掃過去才不會跳 */
#audit-panel :is(td, th, .body-cell, .header-cell) { font-size: 12.5px; padding: 5px 8px; }
/* 點下某一格時 gradio 會畫一個藍框、還冒出一顆小三角選單鈕(使用者
   2026-08-13 截圖:「那個藍色框框無法接受」)。核對表是拿來聽與勾的,
   不是試算表——那兩個都只是干擾。
   ⚠️ **第一版寫 `box-shadow: none` 沒有中**(使用者回報照樣有框):那些框
   是**用 `--ring-color` 這個變數**畫出來的,而且畫在不只一層元素上,
   逐條蓋永遠會漏。**把變數本身改成透明**才一次到位——五案並排的最小
   重現頁上使用者選的就是這一案(2026-08-14)。
   `.selection-button` 是那顆小三角旁邊的選取把手,一起收掉。 */
/* 前四欄(聽、#、相似度、長度)置中——內容那一欄照舊靠左(使用者
   2026-08-14)。⚠️ **兩次沒中的原因**:① 只設 td 的 text-align 不夠,
   gradio 把儲存格內容包在 .cell-wrap 裡而它是 flex;② **少了
   `!important`**,自己的規則被 gradio 的蓋掉。所以這裡連同所有子元素
   一起指定,而且兩種對齊(text-align 給文字流、justify-content 給 flex)
   都給。 */
/* ⚠️ **選擇器不要求 `table` 這個祖先**(2026-08-14 第三次才修對):gradio
   6.20 的表格是**虛擬化**的(捲動時才產生列),資料列不一定掛在那個
   `<table>` 底下——我先前的診斷量到的是**表頭**那一格(它確實置中了),
   而使用者看到沒置中的是資料列。兩種渲染的類名也不同(`td.svelte-…` 與
   `.body-cell`),所以兩套都列進來。 */
#audit-panel :is(td, th, .body-cell, .header-cell),
#audit-panel :is(td, th, .body-cell, .header-cell) * {
  text-align: center !important;
  justify-content: center !important;
}
/* 只有最後一欄(內容)靠左——**不數欄位**:用 :nth-child(-n+4) 就得跟著
   欄數改,而欄位這幾天已經改過三次(拿掉核對檔位置、拿掉勾選、換相似度)。
   「除了最後一欄以外都置中」與欄數無關,改欄位時不必回來動它。 */
#audit-panel :is(td, th, .body-cell, .header-cell):last-child,
#audit-panel :is(td, th, .body-cell, .header-cell):last-child * {
  text-align: left !important;
  justify-content: flex-start !important;
}
#audit-panel :is(td, .body-cell):first-child { cursor: pointer; font-size: 15px; }
/* 播放中的那一格改顯示 ■。⚠️ **不去改文字**:那一格的內容是 svelte 管的
   (診斷實測「字樣節點 = DIV.cell-wrap」),直接寫 textContent 會被它在下
   一次渲染蓋回去——第一版就是這樣沒作用。改成加一個 class、用 ::after
   疊上去,原本的 ▶ 藏起來即可,完全不碰它的資料。 */
#audit-panel :is(td, .body-cell).ms-playing { position: relative; }
#audit-panel :is(td, .body-cell).ms-playing > * { visibility: hidden; }
#audit-panel :is(td, .body-cell).ms-playing::after {
  content: "■"; position: absolute; inset: 0;
  display: flex; align-items: center; justify-content: center;
  font-size: 15px; color: var(--body-text-color);
}
#audit-panel { --ring-color: transparent !important; }
/* ⚠️ **變數要設在儲存格自己身上**(2026-08-14 使用者截圖:點某一格還是有
   藍框):gradio 的 `.body-cell` **自己就宣告了 `--ring-color`**,而元素上
   的宣告贏過從祖先繼承來的——所以只在面板層設透明是沒有用的。
   ⚠️ **只中和變數、不要碰 box-shadow**:那些格線本身也是 box-shadow 畫的
   (`inset 1px 0 0 var(--border-color-primary)`),一起關掉會讓整張表的
   直線消失。 */
#audit-panel :is(td, th, .body-cell, .header-cell, .cell-wrap) {
  --ring-color: transparent !important;
}
/* ⚠️ **選取框有好幾種畫法**(2026-08-14,第三次才收乾淨):`--ring-color`
   合成的 box-shadow、`.selected td` 的 border-color、還有 focus 的 outline
   ——一條一條追太慢,而且每追一條就要使用者再截一次圖。改成**在表格容器
   上把 accent 那組顏色整個中和**:不論哪一條規則畫的,顏色都是透明的。
   ⚠️ **只在表格容器內**,不要下在整個面板上——面板裡的下拉還需要 focus
   時的顏色回饋(那是鍵盤操作唯一的線索)。 */
#audit-panel :is(.table-wrap, .virtual-table-viewport, .header-table, table) {
  --ring-color: transparent !important;
  --border-color-accent: transparent !important;
  --color-accent: transparent !important;
}
#audit-panel :is(td, th, .body-cell, .header-cell) :is(:focus, :focus-visible) {
  outline: none !important;
}
#audit-panel :is(.cell-menu-button, .selection-button) { display: none !important; }
#audit-panel :is(td, .body-cell) { font-variant-numeric: tabular-nums; }
/* 面板頂端那句說明:比內文小一級、顏色收斂——它是操作說明不是內容,
   不該跟逐段對照表搶注意力 */
.audit-note p { font-size: 13px; color: var(--body-text-color-subdued); margin: 0; }
.name-row { padding: 10px 2px; }
.name-row + .name-row { border-top: 1px solid var(--input-border-color); }
.name-row .form, .name-row .block {
  background: transparent !important; box-shadow: none !important;
  /* 水平留 6px:歸零會讓標籤首字貼邊被裁(「講」剩半邊,同 pad-x 地雷) */
  padding: 2px 6px !important; border: none !important;
}

/* 試聽鈕:比一般膠囊再小一號,像設計稿的 chip;不隨 Row 伸縮 */
/* 命名列右側的按鈕欄(試聽在上、核對在下)。⚠️ **不能讓它有自己的白底**
   ——它在命名卡片裡面,多一層底色看起來就是「卡中卡」 */
.name-btns { gap: 4px !important; background: transparent !important; }
.name-btns :is(.block, .form, .styler) {
  background: transparent !important; box-shadow: none !important;
  border: none !important; padding: 0 !important;
}
.aud-btn, .audit-btn {
  flex: 0 0 auto !important; align-self: center;
  font-size: 13px !important; padding: 4px 14px !important;
}
/* 「🔍 核對」只亮在該核對的那幾列(未知＋診斷點名的前三名),所以它天生
   稀疏——樣式與試聽鈕一致,靠字樣區分,不另外給顏色:整排鈕五顏六色
   會讓人以為那是三種不同性質的操作 */

/* 聲紋分不開時,那幾位候選在**選單裡**的標示(設計稿 D 案,使用者
   2026-08-16 選定):淺琥珀底 + 「聲音接近的 / 全部名單」兩個分組小標;
   **收起來之後不留任何記號**(同日他指定:欄位裡不要多一個符號)。
   之前只是安靜地把候選排到最前面,打開選單完全看不出哪幾筆是它。

   ⚠️ **標示只能做在 CSS,絕不能寫進選項字串**:這個欄位允許自由輸入,
   選項字串就是最後寫進逐字稿與 `data/voiceprints.npz` 的名字(見
   app._choice_layout,突變 M185)。欄位掛的 class 由 `rival_classes()`
   產生:`rivals`(有候選就掛,與筆數無關的規則錨它)+ `rivals-N`。

   改這段之前,兩條非知不可的實測結論(其餘沿革見 docs/dev/ui.md
   「下拉選單的標示」,那裡是 gradio 地雷的家):
   ⚠️ **選 li 一律用 `data-index`,不可用 `nth-child`**:打字過濾時
   gradio 把不符的 li 整個移除,但**留下來的 data-index 保持原始索引**
   ——`nth-child` 在過濾後會把底色落到別人身上,而**標錯人比不標更糟**。
   ⚠️ **小標要 `flex-basis:100%` 才會自成一行**:li 是 flex 容器,
   `display:block` 的 ::before 會被排成 flex item、跟名字擠在同一行。

   底色用**半透明琥珀**而不是寫死 #FFF4D1:同一個值疊在淺色卡片上正好
   是使用者選定的 #FFF4D1,疊在深色卡片(#1d1d1f)上自動變成暗琥珀
   ——這裡沒有現成的主題變數可用,寫死的話深色模式會亮得刺眼。alpha 走
   `--rival-bg` 是為了讓 hover 只換一個數字,不必把整串選擇器再抄一遍
   (非候選的 li 拿到這個變數也沒有規則會用)。⚠️ `!important` 不可省:
   gradio 自己會給選中/滑過的那一列掛 `bg-gray-100`。

   ⚠️ **三種失效都是「安靜地退回改動前」,這是刻意的**:gradio 改 DOM、
   候選超過 `voiceprints._MAX_RIVALS`(CSS 只寫到那個數)、過濾把第 0 筆
   濾掉——三者都只是琥珀底不出現,而「聲音同時像:A、B」那行 info 是
   **另一條獨立的通道**、仍然在。所以壞掉不會誤導,只會少一個提示 */
#name-box .rivals li { flex-wrap: wrap; }
#name-box :is(
  .rivals-1 li[data-index="0"],
  .rivals-2 li[data-index="0"], .rivals-2 li[data-index="1"],
  .rivals-3 li[data-index="0"], .rivals-3 li[data-index="1"],
  .rivals-3 li[data-index="2"]
) { background: rgba(255, 199, 0, var(--rival-bg, 0.18)) !important; }
#name-box .rivals li:hover { --rival-bg: 0.30; }
/* 兩個分組小標:第一筆候選(永遠是 data-index=0)與第一筆非候選
   (= 第 N 筆)。「請聽過再選」這半句是重點——底色只說得出「這幾筆
   不一樣」,說不出哪裡不一樣,而使用者看到名字就填正是要防的事 */
#name-box .rivals li[data-index="0"]::before {
  content: "聲音接近的 ・ 請聽過再選";
}
#name-box :is(
  .rivals-1 li[data-index="1"],
  .rivals-2 li[data-index="2"],
  .rivals-3 li[data-index="3"]
) {
  border-top: 1px solid var(--input-border-color) !important;
  margin-top: 4px; padding-top: 8px !important;
}
#name-box :is(
  .rivals-1 li[data-index="1"],
  .rivals-2 li[data-index="2"],
  .rivals-3 li[data-index="3"]
)::before { content: "全部名單"; }
#name-box .rivals li::before {
  flex-basis: 100%; font-size: 11px; margin-bottom: 1px;
  color: var(--body-text-color-subdued); letter-spacing: 0.04em;
}

/* 轉檔進度文字(「XXX.m4a:轉錄與講者分析 - 5.0%」,gradio 畫在預覽框上
   的 .progress-level-inner)原生只有 12px,使用者反映太小;先放 16px
   後改 14px(使用者微調)。錨定自家 #preview-box,gradio 改內部結構時
   只是退回小字。
   左右內距+折行(使用者截圖回報 2026-07-25:長檔名頂到卡片邊):覆蓋層
   .wrap 原生雖寫 padding:0 var(--size-6),gradio 6.20 實測計算後是 0
   (--size-6 有值 24px,規則本身沒落地)——文字貼死卡片左緣;無空格長段
   (底線相連無折行機會)超寬時再被 .wrap 的 overflow:hidden 從右緣裁掉。
   改在文字本體補 24px 內距、overflow-wrap:anywhere 讓長段折行(檔名折行
   無妨、左右要留空,使用者指定)。Playwright A/B 實測(900px 窄視窗):
   注入前 textLeft=blockX 貼邊,注入後左右各留 24px、無右緣裁切。
   #doc-result-box(「文字、圖像→MD」分頁的結果框)同理,合成一條規則 */
:is(#preview-box, #doc-result-box) .progress-level-inner {
  font-size: 14px !important;
  max-width: 100%; box-sizing: border-box;
  padding: 0 24px; overflow-wrap: anywhere;
}

/* 文件轉檔的結果框:等寬字讓檔名清單對齊,並保留換行 */
#doc-result-box textarea {
  font-family: var(--font-mono); font-size: 13px; line-height: 1.7;
}

/* 試聽的出聲載體:移出畫面但「必須保持渲染」——gradio 層 visible=False
   會整個不渲染、前端沒有元件就不會出聲(地雷實測),所以用 CSS 挪走而
   不是藏 visible。position:absolute 脫離版面流(不佔 Column 的 gap)、
   左移出視窗;保留實際寬度讓內部播放器正常初始化。負 left 不會撐出
   水平捲軸(LTR 下向左溢出不產生捲動範圍) */
#audition-player {
  position: absolute !important; left: -9999px !important; top: 0;
  width: 480px !important; opacity: 0; pointer-events: none;
}

/* 斷線提示橫幅(使用者回報 2026-07-31):電腦睡眠會讓 gradio 的 SSE 斷線、
   session 被伺服器判死,而頁面看起來與正常時一模一樣——每顆按鈕都沒反應、
   gradio 前端不給任何提示(使用者以為程式壞了)。橫幅由 RECONNECT_HEAD
   偵測到「時間跳躍」時建出來並加 .show。
   position:fixed 是刻意的(與只錨頁面頂端的 #theme-menu 不同):提示不該
   被捲走,使用者在畫面任何位置都要看得到。掛在 .gradio-container 內才吃得
   到 gradio 的 CSS 變數(深淺色自動跟著走);fixed 不受父層影響 */
#reconnect-bar {
  display: none; position: fixed; top: 12px; left: 50%;
  transform: translateX(-50%); z-index: 1000;
  width: min(680px, calc(100% - 24px));
  gap: 14px; align-items: flex-start;
  padding: 14px 16px; border-radius: 14px;
  background: var(--block-background-fill);
  border: 1px solid var(--input-border-color);
  box-shadow: 0 12px 32px rgba(0,0,0,0.24);
}
#reconnect-bar.show { display: flex; }
#reconnect-bar .rc-text {
  flex: 1; display: flex; flex-direction: column; gap: 4px;
}
/* 標題色要明寫:gradio 對 strong 有自己的著色,不指定會渲染成藍色、
   看起來像可以點的連結(Playwright 截圖回看發現) */
#reconnect-bar .rc-text strong {
  font-size: 14px; color: var(--body-text-color);
}
#reconnect-bar .rc-text span {
  font-size: 12.5px; line-height: 1.5; color: var(--body-text-color-subdued);
}
#reconnect-bar .rc-actions { display: flex; align-items: center; gap: 6px; }
#reconnect-bar .rc-go {
  border: none; cursor: pointer; white-space: nowrap;
  border-radius: 999px; padding: 8px 18px; font-size: 13px; font-weight: 600;
  background: var(--button-primary-background-fill);
  color: var(--button-primary-text-color);
}
#reconnect-bar .rc-dismiss {
  border: none; background: transparent; cursor: pointer;
  color: var(--body-text-color-subdued); font-size: 14px;
  padding: 6px 8px; border-radius: 999px; line-height: 1;
}
#reconnect-bar .rc-dismiss:hover { background: var(--background-fill-secondary); }
"""


def rival_classes(count: int) -> list[str]:
    """命名欄位的 class:有 `count` 位聲紋候選就掛 `rivals` + `rivals-N`。

    ⚠️ **這串是只有上面那段 CSS 看得懂的私有編碼**,所以產生器住在視覺層
    ——`app.py` 只負責把它接到 `gr.update(elem_classes=…)` 上,不必知道
    拼法。`rivals` 那個不帶數字的給「與筆數無關」的規則錨(小標的外觀、
    flex-wrap),`rivals-N` 給「前 N 筆」那幾條;分開之後上限改大時,
    只有列舉 `data-index` 的那兩條要跟著長。

    ⚠️ **沒有候選時一定要回空清單,不能省略不送**:gradio 的
    `elem_classes` 更新是**整組取代**,上一檔留下的 class 不清掉,下一檔
    認得出來的那幾位會繼續黃著——那正好是「看起來還沒確認、其實已經
    認出來」的反向誤導。復位路徑(`app._page_reset_updates`)同理。"""
    return ["rivals", f"rivals-{count}"] if count else []


# 外觀(深淺色)設定:gradio 6 對主題選擇無任何落地(重啟即失),而且它切主題是
# 「整頁導向 ?__theme=」——class 變化發生在下一頁的初始化期間,靠監聽
# class 變動來記錄注定存不到(踩過)。故不再依賴內建設定頁,改由自家
# #theme-menu(右上齒輪)設定:點選當下就寫 localStorage,「深色/淺色」帶
# __theme 參數重載讓 gradio 原生套用(官方支援、優先於系統偏好),「系統」
# 移除參數、交還給系統偏好;下次開頁網址沒帶參數時,自動用存檔值補上還原。
THEME_PERSIST_HEAD = """
<script>
(function () {
  var KEY = "ms-theme";
  function save(mode) { try { localStorage.setItem(KEY, mode); } catch (e) {} }
  var current = "system";  /* 預設跟隨系統 */
  try {
    var url = new URL(window.location);
    var mode = url.searchParams.get("__theme");
    if (mode === "dark" || mode === "light") {
      save(mode);  /* 網址上的參數就是使用者的選擇,順手記下(冪等) */
      current = mode;
    } else {
      var saved = localStorage.getItem(KEY);
      if (saved === "dark" || saved === "light") {
        url.searchParams.set("__theme", saved);
        window.location.replace(url);  /* 帶參數重載,由 gradio 原生套用 */
        return;
      }
    }
  } catch (e) { /* localStorage 不可用:不記憶,切換本身仍可用 */ }
  /* 「目前選擇」的打亮錨點:head 在任何渲染前執行,CSS 直接依此判斷,
     不必等 gr.HTML 掛載(渲染時機不定) */
  document.documentElement.setAttribute("data-ms-theme", current);
  function closeMenu() {
    var menu = document.getElementById("theme-menu");
    if (menu) menu.classList.remove("open");
    var gear = document.getElementById("theme-gear");
    if (gear) gear.setAttribute("aria-expanded", "false");
  }
  /* 齒輪開合與選項點擊:gr.HTML 渲染時機不定,掛 document 事件委派最穩 */
  document.addEventListener("click", function (e) {
    var gear = e.target && e.target.closest("#theme-gear");
    if (gear) {
      var menu = document.getElementById("theme-menu");
      var open = menu && menu.classList.toggle("open");
      gear.setAttribute("aria-expanded", open ? "true" : "false");
      return;
    }
    var btn = e.target && e.target.closest("[data-theme-choice]");
    if (btn) {
      var pick = btn.getAttribute("data-theme-choice");
      save(pick);
      var u = new URL(window.location);
      if (pick === "system") u.searchParams.delete("__theme");
      else u.searchParams.set("__theme", pick);
      window.location.replace(u);
      return;
    }
    closeMenu();  /* 點浮窗外任意處收起 */
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closeMenu();
  });
})();
</script>
"""

# 轉檔中誤關瀏覽器的守門:beforeunload 只在 window.__msBusy 舉旗時攔,
# 旗標由 build_ui 的轉檔事件鏈開關(點「開始」當下前端舉旗、事件結束放下)。
# 對話框文字無法自訂——現代瀏覽器基於反釣魚一律顯示自家的通用訊息,
# returnValue 設空字串只是「要攔」的訊號(舊版 Chrome/Edge 必須設值)。
# 沒在轉檔時旗標為假,關閉一如往常不多問。
#
# abort 壓制(第二段)是這個守門能用的前提:gradio 前端自己也掛了
# beforeunload → client.close() → abort 掉 /queue/data 的 SSE 串流,完全沒料
# 到會被 preventDefault 留下——按「取消」後頁面雖在、連線已死,伺服器隨即
# 把 session 判死(clean_events、重連 404),前端的「重連」又只是整頁
# reload(還會再被守門攔一次),結果整頁報廢、按什麼都沒反應(實測重現)。
# gradio 的清理監聽比 head= 腳本先註冊(head 內容是前端 mount 時才注入,
# 攔 addEventListener 來不及),client 實例也沒掛在 window 上,唯一搆得到
# 的下游就是 AbortController.prototype.abort:只在「beforeunload 派發中
# (window.event)+轉檔中」這個窄窗口吞掉 abort——該時刻會呼叫 abort 的
# 只有 gradio 的 unload 清理。真的離開時瀏覽器自會斷線,行為與原本無異;
# Playwright 實測:取消留下後進度照跑、完成照送、UI 可繼續操作。
UNLOAD_GUARD_HEAD = """
<script>
(function () {
  window.addEventListener("beforeunload", function (e) {
    if (window.__msBusy) {
      e.preventDefault();
      e.returnValue = "";
    }
  });
  var origAbort = AbortController.prototype.abort;
  AbortController.prototype.abort = function (reason) {
    var evt = window.event;
    if (window.__msBusy && evt && evt.type === "beforeunload") {
      return;
    }
    return origAbort.call(this, reason);
  };
})();
</script>
"""

# 斷線提示 +「重新連線」(使用者選定 2026-07-31,實際踩到後):電腦睡眠/
# 長時間閒置會讓 gradio 的 SSE 斷線、session 被伺服器判死,頁面卻與正常時
# 看起來一模一樣——按鈕全部沒反應,gradio 前端毫無提示。使用者實際災情:
# 睡醒後在死頁面上點了一下,整份講者命名的落地進度就沒了(死 session 的
# 點擊「照樣在伺服器端執行」,見 app._stale_click_guard),只能重轉。
#
# 偵測方式=計時器漂移:睡眠/凍結期間 setInterval 不會準時觸發,醒來時的
# 時間跳躍即是訊號。不碰 gradio 內部——client 實例不掛在 window 上、監聽
# 註冊順序也搶不到(同 UNLOAD_GUARD_HEAD 的實測結論),版本改版時這段
# 完全不受影響。門檻 90 秒是為了避開瀏覽器對「背景分頁」的計時器節流
# (Chrome 最慢降到每分鐘一次,那是正常現象、連線好好的),睡眠中斷則
# 動輒數分鐘起跳。
#
# 只提示、不自動重整(使用者選定):主導權留給使用者,轉檔中的畫面不該
# 自己刷掉。措辭是「可能已中斷」——短暫睡眠有機會連線還活著,寧可講得
# 保守也不謊報;真按下去也不會有損失(重新整理本來就安全)。
# 開頁過場:蓋掉 gradio 還沒把畫面畫出來的那幾秒(使用者 2026-08-08 回報
# 「切回去 Chrome 頁面時,會有 2~3 秒整個畫面是空白的,有點怪」)。
#
# 白畫面的來源不是我們的重連(那是手動的),是 **Chrome 把背景分頁整個丟棄**
# (記憶體壓力下的 tab discard)之後重新載入整頁——而 gradio 是 SPA,HTML
# 到手還要靠 JS 把畫面畫出來,這個 app 元件又特別多(30 個命名框+31 顆
# 試聽鈕),冷渲染就是這個量級。期間 .gradio-container 是空的。
#
# ⚠️ **CSS 必須 inline 在 head**:APPLE_CSS 是 launch(css=) 交給 gradio 的,
# 要等它載入才套得上,那時白畫面早就閃完了。
#
# ⚠️ **一定要有逾時保險**:偵測不到「畫面好了」的話,這一層會永遠蓋著——
# 那不是「有點怪」,是整個程式看起來當掉。寧可讓它閃一下,不可鎖死畫面。
#
# 使用者選了「只要一個轉圈圖示」(不放文字):最安靜的一種。轉圈用灰階畫,
# 不依賴 gradio 的主題變數(那時還沒載入),深淺色底下都看得見。
SPLASH_HEAD = """
<style>
#ms-splash {
  position: fixed; inset: 0; z-index: 2000;
  display: flex; align-items: center; justify-content: center;
  background: #ffffff;
}
/* 底色要自己判斷:gradio 的變數這時還不存在。深色的判準有兩個——
   系統偏好,以及 THEME_PERSIST_HEAD 依 localStorage\網址參數蓋上的
   data-ms-theme(它比這裡晚跑,但 CSS 是宣告式的,屬性一上就套用) */
@media (prefers-color-scheme: dark) { #ms-splash { background: #0f1012; } }
html[data-ms-theme="dark"] #ms-splash { background: #0f1012; }
html[data-ms-theme="light"] #ms-splash { background: #ffffff; }
#ms-splash i {
  width: 34px; height: 34px; border-radius: 50%;
  border: 3px solid rgba(140, 140, 150, 0.22);
  border-top-color: rgba(140, 140, 150, 0.72);
  animation: ms-spin 0.8s linear infinite;
}
@keyframes ms-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) {
  #ms-splash i { animation-duration: 2.4s; }
}
</style>
<script>
(function () {
  var GIVE_UP_MS = 15000;   // 逾時保險,見上方註解
  var SETTLE_MS = 150;      // 主結構出現後再等一下,免得撤掉時看到半成品
  var el = null;

  function drop() {
    if (el && el.parentNode) { el.parentNode.removeChild(el); }
    el = null;
  }

  function mount() {
    if (el || !document.body) { return; }
    el = document.createElement("div");
    el.id = "ms-splash";
    el.innerHTML = "<i></i>";
    document.body.appendChild(el);
    // #main-tabs 是我們自己的 elem_id,gradio 把主結構畫出來才會有
    var timer = setInterval(function () {
      if (document.getElementById("main-tabs")) {
        clearInterval(timer);
        setTimeout(drop, SETTLE_MS);
      }
    }, 80);
    setTimeout(function () { clearInterval(timer); drop(); }, GIVE_UP_MS);
  }

  if (document.body) {
    mount();
  } else {
    // 比 DOMContentLoaded 早:body 節點一出現就蓋上,白的那一段才夠短
    new MutationObserver(function (_recs, obs) {
      if (document.body) { obs.disconnect(); mount(); }
    }).observe(document.documentElement, { childList: true });
  }
})();
</script>
"""

RECONNECT_HEAD = """
<script>
(function () {
  var TICK_MS = 5000;    // 檢查間隔
  var GAP_MS = 90000;    // 判定「睡過/凍結過」的時間跳躍門檻(見上方註解)
  var last = Date.now();
  var bar = null;

  function build() {
    // 掛進 .gradio-container 才吃得到 gradio 的 CSS 變數(深淺色跟著走);
    // 建構時機是「第一次要顯示時」,不必等 DOM 就緒也不必輪詢
    var host = document.querySelector(".gradio-container") || document.body;
    var el = document.createElement("div");
    el.id = "reconnect-bar";
    el.innerHTML =
      '<div class="rc-text"><strong>畫面暫停更新了</strong>' +
      "<span>切換到其他程式或電腦睡眠時,這個頁面會停止接收畫面更新," +
      "按鈕也會按了沒反應。轉檔與錄音都在背景繼續進行、不受影響," +
      "按「重新整理」即可接回目前進度;若重新整理後仍是空白畫面," +
      "請確認執行程式的黑色視窗還開著。</span></div>" +
      '<div class="rc-actions">' +
      '<button type="button" class="rc-go">重新整理</button>' +
      '<button type="button" class="rc-dismiss" aria-label="關閉提示">✕</button>' +
      "</div>";
    el.querySelector(".rc-go").addEventListener("click", function () {
      // 關頁守門(UNLOAD_GUARD_HEAD)在轉檔中會攔下離開:這裡是使用者
      // 刻意要重新整理,先放下旗標免得多跳一個確認框。重新整理是安全的
      // ——轉檔/錄音都在伺服器端跑,命名進度也已落地,demo.load 會把
      // 錄音狀態(_restore_recording)與命名(_restore_pending)接回來
      window.__msBusy = false;
      location.reload();
    });
    el.querySelector(".rc-dismiss").addEventListener("click", function () {
      el.classList.remove("show");
    });
    host.appendChild(el);
    return el;
  }

  setInterval(function () {
    var now = Date.now();
    var gap = now - last;
    last = now;
    if (gap <= GAP_MS) { return; }
    /* 漂移只代表「這個分頁停過」,**不代表程式那邊出事**:瀏覽器對背景
       分頁本來就會節流計時器,切去別的軟體工作十來分鐘必定超標。所以先
       確認伺服器還在不在,問不到才提示(使用者選定 2026-08-08)。

       為什麼值得多這一趟:2026-08-08 使用者切去別的軟體、回來就看到
       「連線可能已中斷」,照著提示按下去,一支 3 小時 59 分的月會錄音
       轉了 43 分鐘的進度就從畫面上消失了——而連線當時很可能好好的。
       誤報本身不只是噪音,它會把人推去做那個弄丟東西的動作。

       任何 HTTP 回應都算「有人回話」(404/405 也是);只有網路層失敗才
       進 catch。本機服務連不上時 fetch 會立刻失敗,不必另外設逾時。

       ⚠️ 誠實的限制:這問得到「伺服器在不在」,問不到「這個分頁的 SSE
       還通不通」——gradio 的 client 不掛在 window 上(實測結論同關頁
       守門)。SSE 死了但伺服器活著時因此不再提示,代價是使用者要自己
       察覺畫面不動;換來的是不再誤導他。而且現在按重新整理是安全的:
       轉檔會由 _restore_transcribing 接回畫面(含停止鈕) */
    fetch(window.location.href, { method: "HEAD", cache: "no-store" })
      .catch(function () {
        if (!bar) { bar = build(); }
        bar.classList.add("show");
      });
  }, TICK_MS);
})();
</script>
"""

# 右上角外觀設定:齒輪+「外觀」浮窗,三選項 系統/深色/淺色(取代 gradio
# 頁尾的設定齒輪,只做「顏色」一件事)。點擊行為在 THEME_PERSIST_HEAD
# (事件委派)、樣式與「目前選擇」的打亮在 APPLE_CSS(依 html 的
# data-ms-theme)——#theme-gear 與 data-theme-choice 是兩者的錨點。
THEME_MENU_HTML = (
    '<button type="button" id="theme-gear" aria-label="外觀設定"'
    ' aria-expanded="false" aria-haspopup="true">'
    # 齒輪造型(Material Icons settings):細線圈+放射短線畫出來像太陽,
    # 會被誤讀成「亮度」;實心齒輪才有「設定」的既有印象
    '<svg viewBox="0 0 24 24" width="19" height="19" fill="currentColor"'
    ' aria-hidden="true"><path d="M19.14 12.94c.04-.3.06-.61.06-.94'
    " 0-.32-.02-.64-.07-.94l2.03-1.58c.18-.14.23-.41.12-.61l-1.92-3.32"
    "c-.12-.22-.37-.29-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94"
    "l-.36-2.54c-.04-.24-.24-.41-.48-.41h-3.84c-.24 0-.43.17-.47.41"
    "l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22"
    "L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94"
    "s.02.64.07.94l-2.03 1.58c-.18.14-.23.41-.12.61l1.92 3.32"
    "c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54"
    "c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54"
    "c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32"
    "c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6"
    's1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z"></path></svg>'
    "</button>"
    '<div class="theme-pop">'
    '<div class="theme-pop-title">外觀</div>'
    '<div class="theme-seg">'
    '<button type="button" data-theme-choice="system">系統</button>'
    '<button type="button" data-theme-choice="dark">深色</button>'
    '<button type="button" data-theme-choice="light">淺色</button>'
    "</div></div>"
)

# 停止鈕的即時回饋:轉檔把 CPU 吃滿時,停止事件連同其 toast 可能延遲數十秒
# 才輪到(Playwright 實測 15~35 秒、暖機期更久),看起來就像按了沒反應
# (使用者回報)。js-only 不進佇列,點下當下就變「停止中…」;只在轉檔中
# (__msBusy)生效,平時誤按不變字。復原掛在轉檔主事件的 .then
# (RUN_DONE_JS)——真正停下來之前都該維持「停止中…」,不能自己復原。
# gr.Button 的 elem_id 掛在 <button> 本體;找不到就靜默不動(gradio 內部
# 結構改版時退化成無回饋,不壞功能)
# 停止鈕不只一顆(逐字稿的 #stop-btn、文件轉檔的 #doc-stop-btn),所以做成
# 工廠函式而不是寫死 id 的常數。以 __BTN__ 佔位 + replace 產生,不用
# f-string——js 本體滿是大括號,f-string 要逐個跳脫,反而容易改壞
_STOP_FEEDBACK_TMPL = """
() => {
  const b = document.getElementById('__BTN__');
  if (b && window.__msBusy && !b.dataset.msStopping) {
    b.dataset.msStopping = '1';
    b.dataset.msLabel = b.textContent;
    b.textContent = '停止中…';
  }
}
"""

# 合併追問是**唯一**還留在「按下儲存之後」的追問(純改名 2026-08-09 起隨
# 勾選一次做完,見 data_tabs.save_attendees),而它比預告長——要把「幾個併進
# 幾個、合併後共幾個」講清楚。實測(Playwright,1240×736、64 人名單):
# 確認鈕底落在 y=744、**超出視窗 8px**。
#
# 這一段只在「真的有東西看不到」時捲,而且**只捲差額**——整頁跳走會把
# 使用者剛編輯的表格帶離視線,那正是設計稿方案 A(自動捲動+高亮)被否掉
# 的理由。⚠️ 不用 `scrollIntoView({block:'nearest'})`:它捲完鈕會**貼著**
# 視窗底(實測 bottom 剛好落在 736),看起來像被切掉;差額要另加一段餘裕。
#
# ⚠️ 掛成 click 的 `.then` 尾巴:gradio 6.20 的 js-only 步之後的環節一律
# 不觸發,所以它只能當鏈尾(這裡本來就沒有後續)。requestAnimationFrame
# 是等 svelte 把那塊畫出來——事件回來的當下 DOM 還可能是舊的。
ROSTER_CONFIRM_SCROLL_JS = """
() => {
  requestAnimationFrame(() => {
    const btn = document.querySelector('#att-decision button:not(.hidden)');
    if (!btn) { return; }
    const over = btn.getBoundingClientRect().bottom + 24 - window.innerHeight;
    if (over > 0) { window.scrollBy(0, over); }
  });
}
"""

_RUN_DONE_TMPL = """
() => {
  window.__msBusy = false;
  const b = document.getElementById('__BTN__');
  if (b && b.dataset.msStopping) {
    b.textContent = b.dataset.msLabel || '停止';
    delete b.dataset.msStopping;
    delete b.dataset.msLabel;
  }
}
"""


def stop_feedback_js(btn_id: str) -> str:
    """指定停止鈕的「按下即改字」腳本。"""
    return _STOP_FEEDBACK_TMPL.replace("__BTN__", btn_id)


def run_done_js(btn_id: str) -> str:
    """指定停止鈕的收尾腳本(放下關頁守門旗標 + 鈕字樣復原)。

    `__msBusy` 是**共用**旗標:逐字稿轉檔、錄音、文件批次三者互斥
    (app 層的 _transcribing / _converting 雙向擋),同時只會有一個舉旗,
    共用一個旗標關頁守門才不會有人漏放。"""
    return _RUN_DONE_TMPL.replace("__BTN__", btn_id)


STOP_FEEDBACK_JS = stop_feedback_js("stop-btn")

# 轉檔主事件結束(完成/報錯/停止皆然,故 .then/.failure 都要掛):
# 放下關頁守門旗標+停止鈕復原
RUN_DONE_JS = run_done_js("stop-btn")

# 「文字、圖像→MD」分頁的同一組(批次跑幾十個檔一樣不能讓人以為當掉)
DOC_STOP_FEEDBACK_JS = stop_feedback_js("doc-stop-btn")
DOC_RUN_DONE_JS = run_done_js("doc-stop-btn")

# 套用後的「自動下載」:瀏覽器端逐一點擊下載區各檔的快取網址(gr.Files 的
# 前端值是 FileData 物件,url 指向 gradio 快取)。伺服器端無從觸發下載,
# 只能走 js。輸出固定 md、實際上永遠只有一個檔,迴圈是無害的通用寫法
APPLY_DOWNLOAD_JS = """
(files) => {
  for (const f of files || []) {
    if (!f || !f.url) continue;
    const a = document.createElement('a');
    a.href = f.url;
    a.download = f.orig_name || '';
    document.body.appendChild(a);
    a.click();
    a.remove();
  }
}
"""


# 「聽」那一欄的播放/停止字樣切換。⚠️ **純前端**:切字樣本來要把整張表送回
# 伺服器再送回來重畫,而那正是「勾選改掛很慢」的成因(見 app._audit_play_row)
# ——所以改成點下去當場在 DOM 上換,零往返。
# 對不到節點就什麼都不做(字樣不變,但播放照樣正常):這是錦上添花的回饋,
# 不能因為 gradio 改版就讓核對壞掉。
AUDIT_PLAYING_HEAD = """
<script>
(function () {
  var MARK = "ms-playing";
  function cellOf(e) {
    return e.target && e.target.closest
      ? e.target.closest("#audit-panel :is(td, .body-cell):first-child") : null;
  }
  function clearAll() {
    document.querySelectorAll("#audit-panel ." + MARK).forEach(function (td) {
      td.classList.remove(MARK);
    });
  }
  document.addEventListener("click", function (e) {
    var td = cellOf(e);
    if (!td) { return; }
    var was = td.classList.contains(MARK);
    clearAll();
    if (!was) { td.classList.add(MARK); }
  }, true);
  // 播完的還原由 gradio 的 `stop` 事件呼叫(見 AUDIT_PLAY_ENDED_JS):
  // 這裡只把清除函式掛出去,類別名稱與選擇器才不會有第二份
  window.msAuditClearPlaying = clearAll;
})();
</script>
"""


# 播完自然結束 → 還原成 ▶。**接在 gradio 的 `Audio.stop` 上**(app.py 接線)。
#
# ⚠️ **不要再回頭去收 <audio> 的 ended/pause/emptied**(2026-08-14 第一版就是
# 那樣寫的,使用者回報「播完沒有還原」):Playwright 實測的事實是**真正在播的
# 那顆 <audio> 不在文件樹上**——播放器是 wavesurfer,它自己 createElement 一個
# 載體來播,所以 document 上的監聽(連捕獲階段也一樣)**一個事件都收不到**;
# 頁面上找得到的那顆 `#audit-player audio` 是另一顆、從來不會播。同一次實測
# 也確認 gradio 的 `play` / `stop` 兩個事件都準時派送(wavesurfer 的 `finish`
# 就接在那裡),所以正解是走元件的事件、不是走 DOM。
#
# `fn=None` = 純前端,零往返(不得把表格牽進來,見 app._audit_play_row)。
# 對不到函式就什麼都不做:這是錦上添花的回饋,不能讓核對本身壞掉。
AUDIT_PLAY_ENDED_JS = """
() => { if (window.msAuditClearPlaying) { window.msAuditClearPlaying(); } }
"""

# 按下「🔍 核對」時把頁面捲回最上面(使用者 2026-08-15:講者多的時候那顆鈕
# 在螢幕下方,面板長在右欄上方,不捲的話還要自己捲上去)。
#
# ⚠️ **捲的是 window,不是 `.gradio-container`**(2026-08-15 Playwright 實測):
# 這個 app 的捲軸就在 window / document.scrollingElement / html 那一組上,
# `.gradio-container`、`.main`、`body` 三者的 clientHeight 都等於 scrollHeight
# ——它們沒有自己的捲軸。只對 window 下 `scrollTo(0)`,實測全部歸零。
# (別直接套 ui.md 裡「`.gradio-container` 有 overflow:hidden」那條就假設是它:
#  那條講的是 sticky 的包含塊,與誰在捲是兩回事。)
#
# ⚠️ 掛成**獨立的一顆** `.click(None, js=...)`:gradio 的 `js=` 是「先跑前端、
# 回傳值當成 fn 的 inputs」,併進開面板那顆的話,這支回 undefined 會把
# audit_state 整個洗掉(同 AUDIT_PLAY_ENDED_JS 那條的理由)。
AUDIT_SCROLL_TOP_JS = """
() => { window.scrollTo({top: 0, behavior: 'smooth'}); }
"""
