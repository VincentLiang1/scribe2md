r"""PDF → Block 清單(逐頁判斷文字型 / 掃描型)。

**同一份 PDF 裡文字頁與掃描頁混雜是常態**(電子文件夾帶掃描附件),所以
判斷是逐頁做的、不是逐檔。階段一只處理文字頁,掃描頁留下標記;階段二
接上 OCR 之後,掃描頁會改成「OCR 文字 + 來源標註」。

標題階層以字級推斷,**推不出來就退回平坦結構 + 頁碼標題**——PDF 沒有
語意結構可用,硬猜出來的錯階層會讓 RAG 切在錯的地方,比沒有階層更糟
(docmd.render 規則 1)。
"""
import hashlib
import logging
import re
from collections.abc import Callable
from pathlib import Path

from meeting_scribe import docimage, docmd
from meeting_scribe.docmd import AssetsDir, Block, Heading, Image, Note, Para, Table
from meeting_scribe.errors import UserFacingError

logger = logging.getLogger(__name__)

# 惰性載入的佔位(測試 monkeypatch 這個屬性換假貨)
pymupdf = None

# 一頁要有這麼多字才算「有文字層」。太低會把「只有頁碼與浮水印」的掃描頁
# 誤判成文字頁,結果整頁只轉出一個頁碼
_MIN_TEXT_CHARS = 40
# 單張圖覆蓋頁面這個比例以上 = 掃描頁的特徵
_BIG_IMAGE_AREA_RATIO = 0.5
# 沒有文字層、也沒有大圖,但畫了這麼多向量圖形 = **文字被轉成外框曲線**,
# 那一頁的內容全在筆畫裡(常見於電子公文與某些 PDF 印表機的輸出)。這種
# 頁面要當掃描頁處理:渲染整頁 + OCR,否則整頁內容無聲消失。
#
# 門檻取 50 是因為實測分佈是**乾淨的雙峰**(2026-08-03,400 份真實 PDF、
# 10,200 頁,`scripts/audit_ocr.py` 的姊妹探針):判成 blank 的 392 頁裡,
# 繪圖數 0~9 個的有 256 頁(真的空白,那幾筆是裝訂線與頁碼框)、200 個
# 以上的有 126 頁(全是外框化文字),中間 10~199 只有 10 頁。取在谷底,
# 兩邊都離門檻很遠。
_OUTLINED_TEXT_DRAWINGS = 50
# 字級要比內文大這個倍數才算標題
_HEADING_SIZE_RATIO = 1.15
# 標題字元數不會超過全文這個比例——超過就代表那個字級其實是內文
# (常見於整份都是大字的簡報式 PDF),不能拿來當標題
_HEADING_MAX_SHARE = 0.2
# 最多認幾階標題
_MAX_HEADING_LEVELS = 3
# 文字區塊與表格重疊超過這個比例就視為「表格內的文字」,不重複輸出
_TABLE_OVERLAP = 0.5

# ---- 內嵌圖的抽取(見 _page_images:聚類 + 區域渲染,不逐個抽 XObject)----
# 渲染解析度:150 dpi 對「給 AI 讀 + 人核對」夠用,又不會讓 assets 爆掉
_IMG_RENDER_DPI = 150
# 小於這個邊長(PDF point,約 1/72 吋)的繪製區域視為遮罩/占位/裝飾線。
# 實際案例裡混進來的是一堆 2x2 px 的 png
_MIN_IMG_PT = 24.0
# bbox 間距在這個範圍內就視為同一張圖。條帶的接縫通常剛好對齊(間距 0),
# 留一點餘裕吸收浮點誤差與 1px 的縫
_CLUSTER_GAP_PT = 3.0
# 佔頁面這個比例以上的圖**可能**是背景底圖(見下一個常數)
_FULL_PAGE_RATIO = 0.95
# 版型裝飾的判準:同一個位置在這麼多頁重複出現 = 母片上的裝飾,不是內容。
# 真正的內容圖不會在多頁的**同一座標**重複出現
_TEMPLATE_MIN_PAGES = 3
# 但大圖不適用:一份簡報每頁都有整版圖是正常的(那正是內容)。沒有這條
# 上限的話,「17 頁都有整版圖」會被誤判成版型、整份簡報的內容全部消失
_TEMPLATE_MAX_AREA_RATIO = 0.25

# 整版圖到底是「背景」還是「這一頁的內容」,取決於它上面有沒有東西:
# 文字夠多 → 那張圖是襯底,渲染它等於把整頁連文字再存一份(重複);
# 文字很少 → **那張圖就是這一頁的內容**,跳過它等於整頁消失。
#
# 一開始只有比例判斷、整版圖一律跳過,結果 39 頁的簡報 PDF 有 17 頁的
# 主圖被丟掉(使用者 2026-08-02 回報);其中 12 頁還因為有標題文字而被
# 判成 text 頁、連整頁 OCR 都不會跑,那些投影片的內容就這樣**無聲**消失
# ——那正是本功能宣稱最不能接受的事。實測那些頁的文字量是 1~196 字
# (一行標題),而真正的襯底背景頁通常整頁都是字。
_BACKDROP_MIN_TEXT_CHARS = 400
# 掃描頁送 OCR 前的渲染解析度。掃描件本身多半是 150~300 dpi,渲染再高
# 只是把同一批像素放大、白付時間;200 是準確度與速度的平衡點
_OCR_PAGE_DPI = 200


def _ensure_pymupdf():
    global pymupdf
    if pymupdf is None:
        _real = docmd.lazy_import("pymupdf", "PDF")
        # 黑視窗是使用者看得到的地方,不能有裸英文(spec §8)。PyMuPDF 有
        # 兩條各自獨立的訊息路徑,兩條都要處理:
        # (1) find_tables 首次呼叫會 **直接 print** 一句推薦安裝
        #     pymupdf_layout 的英文提示(pymupdf/__init__.py:_warn_layout_once),
        #     它不走 message 機制,只能用官方開關關掉
        # (2) MuPDF 本身的訊息走 set_messages,導進 logging 交給我們的 logger
        # 兩者都用 try 包住:舊版沒有這些 API,壓不掉也不該讓轉檔失敗
        try:
            _real.no_recommend_layout()
        except Exception:
            logger.debug("PyMuPDF 版面分析推薦訊息關不掉", exc_info=True)
        try:
            _real.set_messages(pylogging=True)
        except Exception:
            logger.debug("PyMuPDF 訊息導向設定失敗", exc_info=True)
        pymupdf = _real
    return pymupdf


def _open(src: Path):
    mod = _ensure_pymupdf()
    try:
        doc = mod.open(str(src))
    except Exception as e:
        raise UserFacingError(
            f"無法開啟 PDF「{src.name}」:檔案可能已損壞或不是真正的 PDF"
        ) from e
    if doc.needs_pass:
        doc.close()
        raise UserFacingError(
            f"PDF「{src.name}」有密碼保護,請先用 PDF 軟體移除密碼再轉換"
        )
    return doc


def _text_dict(page) -> dict:
    """取頁面的文字結構。

    **一定要帶 `TEXTFLAGS_TEXT`**:`get_text("dict")` 的預設旗標含
    `TEXT_PRESERVE_IMAGES`,PyMuPDF 會把該頁**每張圖解碼成 bytes 塞進
    回傳的 dict**——而這裡從頭到尾只讀文字 block。實測 50 頁、每頁一張
    圖的 PDF:2140ms → 58ms(37 倍);單頁 dict 少夾帶 1.4MB 影像位元組
    (200dpi 掃描頁是 5~10MB,那才是 8GB 機的記憶體尖峰)。"""
    mod = _ensure_pymupdf()
    return page.get_text("dict", flags=mod.TEXTFLAGS_TEXT)


def _size_histogram(doc) -> dict[float, int]:
    """全文的字級分布(以**字元數**加權,不是 span 數)。

    用 span 數會讓「很多個短標題」蓋過「少數幾段長內文」,內文字級判錯,
    整份文件的階層就全歪了。"""
    hist: dict[float, int] = {}
    for page in doc:
        for block in _text_dict(page).get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    size = round(float(span.get("size", 0)), 1)
                    hist[size] = hist.get(size, 0) + len(span.get("text", ""))
    return hist


def heading_sizes(hist: dict[float, int]) -> list[float]:
    """字級分布 → 可當標題的字級(由大到小)。空清單 = 推不出階層。

    判準:比內文字級大 15% 以上,而且該字級的字量不超過全文兩成——後者
    擋掉「整份都是大字」的簡報式 PDF,那種文件沒有階層可言。"""
    if not hist:
        return []
    total = sum(hist.values())
    if total <= 0:
        return []
    body = max(hist, key=lambda s: hist[s])
    bigger = [
        s for s in hist
        if s > body * _HEADING_SIZE_RATIO and 0 < hist[s] <= total * _HEADING_MAX_SHARE
    ]
    return sorted(bigger, reverse=True)[:_MAX_HEADING_LEVELS]


def _block_text(block) -> tuple[str, float]:
    """文字區塊 → (文字, 最大字級)。"""
    parts: list[str] = []
    max_size = 0.0
    for line in block.get("lines", []):
        chunk = "".join(span.get("text", "") for span in line.get("spans", []))
        for span in line.get("spans", []):
            max_size = max(max_size, round(float(span.get("size", 0)), 1))
        if chunk.strip():
            parts.append(chunk.strip())
    return " ".join(parts).strip(), max_size


def _overlap_ratio(inner, outer) -> float:
    """inner 有多少比例落在 outer 內(兩者都是 (x0, y0, x1, y1))。"""
    ix0, iy0, ix1, iy1 = inner
    ox0, oy0, ox1, oy1 = outer
    w = max(0.0, min(ix1, ox1) - max(ix0, ox0))
    h = max(0.0, min(iy1, oy1) - max(iy0, oy0))
    area = (ix1 - ix0) * (iy1 - iy0)
    return (w * h / area) if area > 0 else 0.0


def page_kind(page) -> str:
    """單頁的種類:'text'(有文字層)/ 'scan'(掃描影像)/ 'blank'(空白)。

    「掃描」在這裡是**處理方式**不是來源:凡是「內容看得見、但抽不出文字」
    的頁面都算,因為對它們唯一的辦法都是渲染整頁再 OCR。除了真正的掃描
    影像,還有**文字被轉成外框曲線**的頁(見 `_OUTLINED_TEXT_DRAWINGS`)。"""
    text = page.get_text("text").strip()
    if len(text) >= _MIN_TEXT_CHARS:
        return "text"
    rect = page.rect
    page_area = abs(float(rect.width) * float(rect.height))
    if page_area > 0:
        try:
            infos = page.get_image_info()
        except Exception:
            infos = []
        for info in infos:
            bbox = info.get("bbox")
            if not bbox:
                continue
            area = abs((bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))
            if area / page_area >= _BIG_IMAGE_AREA_RATIO:
                return "scan"
    if text:
        return "text"
    # 沒有文字層也沒有大圖,但畫滿了向量圖形:內容是外框化的文字。
    # **只在這個分支才數繪圖**——`get_drawings()` 對複雜頁面很慢,而走到
    # 這裡的頁面本來就要被判成空白,數一次的成本換的是「整頁不要消失」
    try:
        if len(page.get_drawings()) >= _OUTLINED_TEXT_DRAWINGS:
            return "scan"
    except Exception:  # noqa: BLE001 - 數不出來就維持原本的判斷
        logger.debug("向量圖形數不出來,沿用空白頁判定", exc_info=True)
    return "blank"


def _near(a, b, gap: float) -> bool:
    """兩個 bbox 是否重疊或相鄰(間距在 gap 以內)。"""
    return not (
        a[2] + gap < b[0] or b[2] + gap < a[0]
        or a[3] + gap < b[1] or b[3] + gap < a[1]
    )


def _cluster_boxes(boxes: list[tuple], gap: float) -> list[tuple]:
    """把重疊或相鄰的 bbox 合併成「視覺上的一張圖」。

    反覆合併到收斂:條帶是一條接一條的,單趟掃描只會兩兩合併,合不出
    完整的一整塊。合併具匯流性,所以**先把全部放進去、再收斂到不動**
    就夠了——早期版本是「每插入一個 box 就把整輪收斂重跑」,那是
    O(n³):實測 n=200 要 120ms(n 加倍、時間 8 倍)。這個函式存在的
    理由正是「PowerPoint 匯出的 PDF 會把圖打成碎片」(實例:39 頁
    215 片),n 大得起來。改成單趟收斂後 n=200 只要 1.7ms。"""
    groups = list(boxes)
    changed = True
    while changed:
        changed = False
        merged: list[tuple] = []
        for box in groups:
            for i, g in enumerate(merged):
                if _near(g, box, gap):
                    merged[i] = (
                        min(g[0], box[0]), min(g[1], box[1]),
                        max(g[2], box[2]), max(g[3], box[3]),
                    )
                    changed = True
                    break
            else:
                merged.append(box)
        groups = merged
    return groups


def template_boxes(doc) -> set[tuple[int, int, int, int]]:
    """整份文件的「版型裝飾」座標集合(母片上的角落圖形、分隔色塊等)。

    判準是**同一個位置在多頁重複出現**:內容圖不會在第 3、4、22、23、36、38
    頁的同一座標各出現一次,而母片的裝飾一定會。實際案例:一份簡報的兩個
    角落幾何圖形各出現在 7 頁,被當成內容抽出來,而且渲染時把壓在上面的
    標題文字一起切進去,產出「半個字」的怪圖(使用者 2026-08-02 回報)。

    **大圖不適用**(`_TEMPLATE_MAX_AREA_RATIO`):每頁都有整版圖的簡報,
    那些整版圖正是內容——沒有這條上限會把整份簡報的內容全部誤刪。

    比對用四捨五入到整點的座標:同一個母片元素在各頁的座標會有浮點誤差。"""
    seen: dict[tuple[int, int, int, int], set[int]] = {}
    for page in doc:
        area = page.rect.get_area()
        if area <= 0:
            continue
        try:
            infos = page.get_image_info()
        except Exception:  # pragma: no cover - 壞頁跳過即可
            continue
        for info in infos:
            bbox = info.get("bbox")
            if not bbox:
                continue
            x0, y0, x1, y1 = (float(v) for v in bbox)
            if abs((x1 - x0) * (y1 - y0)) / area > _TEMPLATE_MAX_AREA_RATIO:
                continue
            key = (round(x0), round(y0), round(x1), round(y1))
            seen.setdefault(key, set()).add(page.number)
    return {k for k, pages in seen.items() if len(pages) >= _TEMPLATE_MIN_PAGES}


def _is_backdrop(page, clip) -> bool:
    """這張整版圖是「襯在內容底下的背景」還是「這一頁的內容本身」?

    只看面積會判錯:簡報匯出的 PDF 裡「整頁一張大圖 + 一行標題」是常態,
    那張圖就是內容。改看它上面有多少字(理由與實測見 _BACKDROP_MIN_TEXT_CHARS)。"""
    if clip.get_area() < page.rect.get_area() * _FULL_PAGE_RATIO:
        return False
    try:
        return len(page.get_text("text").strip()) >= _BACKDROP_MIN_TEXT_CHARS
    except Exception:  # pragma: no cover - 取不到文字就當它是內容,寧可多存
        logger.debug("第 %d 頁取不到文字量", page.number + 1, exc_info=True)
        return False


def _page_images(
    page, assets: AssetsDir, seen: set[str], ocr_enabled: bool = True,
    templates: "set[tuple[int, int, int, int]] | None" = None,
) -> list[Block]:
    """頁面內嵌圖 → assets。**依視覺區域聚類後整塊渲染**,不逐個抽 XObject。

    為什麼不用 `get_images` + `extract_image`(原本的做法,2026-08-01 使用者
    回報後改掉):PDF 產生器(尤其 PowerPoint 匯出)常把一張大圖**切成水平
    條帶**分開存放,一張視覺上完整的投影片圖會散成幾十片「一條一條」的
    圖片;同一條還常常被繪製兩次(圖 + 遮罩),外加一堆 2x2 的占位小圖。
    真實案例:39 頁的簡報 PDF 抽出 215 張碎片。

    改成「把相鄰/重疊的繪製區域聚成群,每群用 clip 渲染一張」之後:
    - 條帶自動接回一張(它們垂直相鄰 → 同一群)
    - 重複繪製自動去重(bbox 相同 → 同一群)
    - 遮罩與占位小圖不會單獨輸出(尺寸門檻擋掉)
    - **所見即所得**:裁切、旋轉、透明度都已套用,而 XObject 是套用前的
      原圖(階段二要 OCR 這些圖時,這一點尤其重要)

    代價是點陣重繪、失去原圖的最高解析度;對「給 AI 讀、人偶爾核對」的
    用途划算,DPI 也壓在 `_IMG_RENDER_DPI` 控制檔案大小。

    去重改用**渲染結果的雜湊**(原本用 xref):每頁重複出現的頁首 logo
    渲染出來位元組相同,一樣抓得到,而且跨「不同 xref 但畫面相同」也有效。"""
    mod = _ensure_pymupdf()
    out: list[Block] = []
    try:
        infos = page.get_image_info()
    except Exception:
        logger.debug("第 %d 頁取不到圖片資訊", page.number + 1, exc_info=True)
        return out

    boxes = []
    for info in infos:
        bbox = info.get("bbox")
        if not bbox:
            continue
        x0, y0, x1, y1 = (float(v) for v in bbox)
        # 太小的多半是遮罩、圓角占位或裝飾線,不是內容
        if (x1 - x0) < _MIN_IMG_PT or (y1 - y0) < _MIN_IMG_PT:
            continue
        # 母片上的裝飾:在聚類**之前**濾掉,否則它會跟旁邊的真圖併成一群
        if templates and (round(x0), round(y0), round(x1), round(y1)) in templates:
            continue
        boxes.append((x0, y0, x1, y1))
    if not boxes:
        return out

    page_rect = page.rect
    for group in _cluster_boxes(boxes, _CLUSTER_GAP_PT):
        clip = mod.Rect(*group) & page_rect
        if clip.is_empty or clip.width < _MIN_IMG_PT or clip.height < _MIN_IMG_PT:
            continue
        # 佔滿整頁**而且上面有大量文字** = 背景底圖:渲染它等於把整頁
        # (含文字)再存一份。文字很少的話那張圖就是內容,絕不能跳過
        if _is_backdrop(page, clip):
            continue
        try:
            pix = page.get_pixmap(clip=clip, dpi=_IMG_RENDER_DPI)
            data = pix.tobytes("png")
        except Exception:
            logger.debug("第 %d 頁的圖片區域渲染失敗", page.number + 1, exc_info=True)
            continue
        digest = hashlib.sha256(data).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        link = assets.add_bytes(data, ".png")
        if link:
            out.append(Image(link, f"第 {page.number + 1} 頁的圖片"))
        # 內嵌圖的文字才是給 AI 用的重點:只留一個連結等於什麼都沒給
        out.extend(docimage.ocr_image_bytes(
            data, f"第 {page.number + 1} 頁的圖片", ocr_enabled,
        ))
    return out


def _clean_text(text: str) -> str:
    """收掉連續空行(PDF 抽出來的文字常常一行一空行)。"""
    return re.sub(r"\n{3,}", "\n\n", str(text or "").strip())


def _ocr_page(page, assets: AssetsDir, ocr_enabled: bool) -> list[Block]:
    """掃描頁 → 渲染整頁 → 存進 assets → OCR。

    渲染而不是抽出頁面裡那張圖:掃描件常被切成條帶存放(見 _page_images),
    而且渲染出來的是「所見即所得」——旋轉、裁切都已套用。

    **整頁的圖也要存**(使用者 2026-08-02 回報第 12、13 頁沒有圖):這一頁的
    內容就是那張圖,而 OCR 出來的字**會有錯**——要人回頭核對時,沒有圖等於
    沒有原件可對。原本這裡只 OCR 不存檔(頁面迴圈另有 `kind != "scan"` 才抽圖
    的條件),於是掃描頁的視覺內容整個消失、而且無聲。存的就是剛才 OCR 用的
    那份像素,不必再渲染一次。

    代價是**整份掃描的 PDF 會一頁一張圖**(300 頁的掃描合約就是 300 張,
    200dpi 下上看百來 MB)。真的礙事再加開關,不要為此讓一般情況少東西。"""
    no = page.number + 1
    try:
        data = page.get_pixmap(dpi=_OCR_PAGE_DPI).tobytes("png")
    except Exception:
        logger.debug("第 %d 頁渲染失敗", no, exc_info=True)
        return [Note(f"第 {no} 頁抽不出文字,而且無法渲染內容", docmd.KIND_SCANNED_PAGE)]
    out: list[Block] = []
    link = assets.add_bytes(data, ".png")
    if link:
        out.append(Image(link, f"第 {no} 頁(整頁影像)"))
    # **文字層也要留**:被判成掃描頁只代表「字少又有大圖」,不代表那幾個字
    # 不是內容(常見的是圖說或頁碼標題)。OCR 關著時它更是這一頁唯一的文字
    # ——自我稽核 2026-08-02 就是先抓到這個落差,才發現這條路會無聲吃掉它
    layer = _clean_text(page.get_text("text"))
    if layer:
        out.append(Para(layer))
    if not ocr_enabled:
        out.append(Note(
            f"第 {no} 頁抽不出文字層,而文字辨識(OCR)沒有啟用,這一頁的文字尚未取出",
            docmd.KIND_SCANNED_PAGE,
        ))
        return out
    found = docimage.ocr_image_bytes(data, f"第 {no} 頁的整頁影像", True)
    out.extend(found or [
        Note(f"第 {no} 頁抽不出文字層,也沒有辨識出文字", docmd.KIND_SCANNED_PAGE),
    ])
    return out


def _table_leftovers(page, table, rows: list[list[str]]) -> str:
    """表格範圍內、但 `extract()` 沒抽到的文字。

    **兩頭落空是真的會發生的**:我們跳過與表格重疊的文字區塊(它們的內容
    「已經在表格裡了」),但 PyMuPDF 的 `extract()` 不保證抽到 bbox 內的
    每一行——實測一份 3,574 頁的程式碼掃描報告,單頁 bbox 內有 116 行、
    只抽出 25 列,**12 行兩邊都沒有、整段無聲消失**(2026-08-02 全碟掃描
    揪出來的最大一筆:9,914 行)。

    比對用與自我稽核同一套(壓空白後看整行在不在),所以「被拆進不同
    儲存格」「換行接起來」這些正常情形不會誤判成漏。"""
    try:
        inside = page.get_text("text", clip=table.bbox)
    except Exception:  # pragma: no cover - 壞頁跳過即可
        logger.debug("表格區域取文字失敗", exc_info=True)
        return ""
    cells = docmd.squash(" ".join(c for row in rows for c in row))
    lost = [
        line for line in inside.splitlines()
        if len(docmd.squash(line)) >= docmd.GAP_MIN_CHARS
        and docmd.squash(line) not in cells
    ]
    return "\n".join(lost)


def _text_page_blocks(page, levels: list[float], flat: bool) -> list[Block]:
    """單一文字頁 → Blocks(文字與表格依版面位置交錯排好)。"""
    try:
        found = page.find_tables()
        tables = list(getattr(found, "tables", []) or [])
    except Exception:
        logger.debug("第 %d 頁的表格偵測失敗", page.number + 1, exc_info=True)
        tables = []

    items: list[tuple[float, float, Block]] = []
    table_boxes = []
    for t in tables:
        try:
            rows = [[("" if c is None else str(c)) for c in row] for row in t.extract()]
        except Exception:
            continue
        bbox = tuple(float(v) for v in t.bbox)
        table_boxes.append(bbox)
        if rows:
            items.append((bbox[1], bbox[0], Table(
                rows, has_header=True, caption=f"第 {page.number + 1} 頁的表格",
            )))
        # 表格吞掉但沒抽到的行要補回來,否則兩頭落空(見 _table_leftovers)。
        # 擺在表格正下方:它本來就是這張表裡的內容
        leftover = _table_leftovers(page, t, rows)
        if leftover:
            items.append((bbox[3], bbox[0], Para(leftover)))

    for block in _text_dict(page).get("blocks", []):
        if block.get("type") != 0:  # 0 = 文字;圖片另外由 _page_images 處理
            continue
        bbox = tuple(float(v) for v in block.get("bbox", (0, 0, 0, 0)))
        # 表格範圍內的文字已經在表格裡了,再輸出一次會整段重複
        if any(_overlap_ratio(bbox, tb) > _TABLE_OVERLAP for tb in table_boxes):
            continue
        text, size = _block_text(block)
        if not text:
            continue
        level = 0
        if not flat and size in levels:
            level = levels.index(size) + 1
        items.append((bbox[1], bbox[0], Heading(level, text) if level else Para(text)))

    items.sort(key=lambda it: (round(it[0], 1), round(it[1], 1)))
    return [b for _, _, b in items]


def _page_text_gaps(doc, blocks: list[Block]) -> list[Block]:
    """自我稽核:**每一頁的文字層,有沒有出現在輸出裡?**

    PDF 的漏法跟 Office 不同——不是「某種節點沒被走訪」,而是**頁面分類
    走錯分支**:一頁只要文字少於 `_MIN_TEXT_CHARS`(40)又有大圖,就會被
    判成 `scan`;那時如果 OCR 關著,`_ocr_page` 只留一張圖與一句標記,
    **那一頁原本的文字層就跟著消失**(踩過兩次類似的:整版圖被當背景、
    掃描頁沒存圖)。

    比對用 `page.get_text()` 的原始輸出——它與我們的處理無關,只跟
    「這一頁到底有沒有字」有關。

    **但要逐行比,不能拿整頁當一個單位**(2026-08-02 修):輸出是逐塊的,
    表格會被抽出來另外擺、註記會插在中間,整頁的字串因此幾乎不可能原封
    不動出現在輸出裡——全碟掃描 3,416 個 PDF 只有 18.7% 乾淨,抽樣逐頁
    查證後 **458 頁的落差裡有 399 頁(87%)是這種對不齊**,字其實都在。
    改成逐行之後同一批檔從「36/38 份有落差」降到 9/38,而那 9 份都只差
    零星幾行(5/797、2/291、1/140)。這與 `docweb._visible_texts` 切到
    葉節點是同一個修法、同一個道理。

    切細**不會**削弱本來要抓的東西:一頁被誤判成 scan 時,那一頁的每一行
    都對不到,照樣整批報出來。"""
    originals: list[str] = []
    for page in doc:
        try:
            text = page.get_text("text")
        except Exception:  # pragma: no cover - 壞頁跳過即可
            continue
        originals.extend(line for line in text.splitlines() if line.strip())
    return docmd.extraction_gap_note(docmd.missing_from(originals, blocks))


def convert_pdf(
    src: Path,
    assets: AssetsDir,
    ocr_enabled: bool = True,
    on_page: Callable[[int, int], None] | None = None,
) -> list[Block]:
    """PDF → Blocks。**逐頁**判斷文字型/掃描型。

    ocr_enabled=False 時掃描頁只留標記、不辨識(給「這批不要 OCR、
    要快」的情境)。不論開關,掃描頁與空白頁都會留下標記——安靜地
    產生空白內容是最糟的結果,使用者會以為那幾頁本來就沒東西。"""
    doc = _open(src)
    try:
        hist = _size_histogram(doc)
        levels = heading_sizes(hist)
        flat = not levels
        blocks: list[Block] = []
        if flat and len(doc) > 0:
            logger.info("%s:字級分布推不出標題階層,改用平坦結構+頁碼", src.name)
        scanned = 0
        seen_images: set[str] = set()  # 渲染結果的雜湊,跨頁去重(見 _page_images)
        templates = template_boxes(doc)  # 母片裝飾的座標(見 template_boxes)
        total = len(doc)
        for page in doc:
            no = page.number + 1
            kind = page_kind(page)
            # 平坦結構每頁都要標題;掃描頁即使推得出階層也需要一個錨點
            if flat or kind == "scan":
                blocks.append(Heading(1, f"第 {no} 頁"))
            if kind == "text":
                blocks.extend(_text_page_blocks(page, levels, flat))
            elif kind == "scan":
                scanned += 1
                blocks.extend(_ocr_page(page, assets, ocr_enabled))
            else:
                blocks.append(Note(f"第 {no} 頁沒有可擷取的內容", docmd.KIND_BLANK_PAGE))
            # 掃描頁已經整頁 OCR 過,不必再對頁內的圖重跑一次
            if kind != "scan":
                blocks.extend(_page_images(page, assets, seen_images, ocr_enabled, templates))
            if on_page:
                on_page(no, total)
        blocks.extend(_page_text_gaps(doc, blocks))
        if scanned:
            blocks.insert(0, Note(
                f"本文件共 {total} 頁,其中 {scanned} 頁抽不出文字層,已改用整頁文字辨識(OCR)",
                docmd.KIND_SCANNED_PAGE,
            ))
        return blocks
    finally:
        doc.close()
