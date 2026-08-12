r"""網頁類來源(html / htm / mht / mhtml)→ Block 清單。

`.mht/.mhtml` 是 Edge/IE 的「另存為單一檔案」格式,本質就是
multipart/related,用標準函式庫的 `email` 就能拆——零額外相依。內嵌的
圖片以 Content-Location / Content-ID 對映回 HTML 裡的 src。

**遠端圖片絕不下載**(隱私規格 spec §7:唯一的連網行為是首次下載模型)。
連結原樣保留並下標記,讓使用者知道那裡有東西沒帶進來。
"""
import base64
import logging
import html as html_mod
import re
import urllib.parse
from email import message_from_bytes, policy
from pathlib import Path

from meeting_scribe import docimage, doctext, docmd
from meeting_scribe.docmd import AssetsDir, Block, Heading, Image, Note, Para, Table
from meeting_scribe.errors import UserFacingError

logger = logging.getLogger(__name__)

# 惰性載入的佔位
bs4 = None
markdownify = None
_CONVERTER = None  # 共用的 MarkdownConverter(見 _converter)

# `image` 是 **SVG** 的貼圖元素(`<svg><image href="…"/></svg>`),不是筆誤:
# Calibre 匯出的 HTMLZ 電子書就用它放分部/分篇的扉頁。少了它,那些頁面在
# md 裡整個消失而且 lossy 仍是 0(使用者 2026-08-02 回報:一本書 65 張圖漏
# 5 張,PART 1-5 的分篇結構全沒了,還不給任何警告)
_BLOCK_TAGS = [
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "table",
    "img", "image", "pre", "blockquote", "li",
]
# **容器類標籤也要走訪**:真實網頁常把整段文字直接放在 <div> 裡、根本不用
# <p>,只認 _BLOCK_TAGS 的話那些頁面的內文**無聲**整份消失(2026-08-02
# 抽樣 25 個 .html 就有一份掉了 480 段內文)。但它們會巢狀(div 套 div 套
# p 是網頁常態),所以只能取「自己的文字」——見 _own_md
_CONTAINER_TAGS = [
    "div", "section", "article", "aside", "main", "header", "footer", "nav",
    "dd", "dt", "figcaption", "caption", "address", "body",
]
_ALL_BLOCK = frozenset(_BLOCK_TAGS + _CONTAINER_TAGS)
# rowspan/colspan 的展開上限:壞掉或惡意的 HTML 會寫 rowspan="999999"
_MAX_SPAN = 200


def _ensure_bs4():
    global bs4
    if bs4 is None:
        bs4 = docmd.lazy_import("bs4", "網頁檔")
        # epub 的內文是 XHTML,帶著 XML 宣告——bs4 會對「用 HTML 解析器讀
        # XML」印一大段英文警告。**用 HTML 解析器是刻意的**(XHTML 本來就
        # 是給瀏覽器讀的,而 `_html_blocks` 整套都建立在那個行為上),而黑
        # 視窗不得出現裸英文(spec §8)
        import warnings

        warnings.filterwarnings("ignore", category=bs4.XMLParsedAsHTMLWarning)
    return bs4


def _ensure_markdownify():
    global markdownify
    if markdownify is None:
        markdownify = docmd.lazy_import("markdownify", "網頁檔")
    return markdownify


def ensure_ready() -> None:
    """預熱網頁解析的兩個相依(給 docpipe 的路由表用)。"""
    _ensure_bs4()
    _ensure_markdownify()


def _decode_html(raw: bytes) -> tuple[str, str]:
    """HTML 位元組 → 文字。**先看 HTML 自己宣告的 charset**。

    網頁自報的編碼比任何統計猜測都權威(台灣不少舊網頁是 Big5 且明寫在
    meta 裡)。宣告缺漏或宣告錯了才退回 doctext 的通用判斷。

    C1 數值實體的修正在這裡做一次,convert_html 與 convert_mht 就都吃得到
    (見 _fix_c1_entities)。"""
    m = re.search(rb'charset\s*=\s*["\']?\s*([\w\-]+)', raw[:4096], re.IGNORECASE)
    if m:
        enc = m.group(1).decode("ascii", errors="ignore").lower()
        try:
            return _fix_c1_entities(raw.decode(enc)), enc
        except (UnicodeDecodeError, LookupError):
            logger.debug("HTML 宣告的編碼 %s 解不開,改用自動判斷", enc)
    text, enc = doctext.read_text_auto(raw)
    return _fix_c1_entities(text), enc


# `&#147;` `&#153;` 這種指向 C1 控制碼區(0x80-0x9F)的數值實體,在舊的
# 微軟/Dell 網頁裡俯拾即是。**HTML5 規定要照 cp1252 對照表解**(瀏覽器
# 就是這樣做的,`&#153;` 顯示成 ™),但 bs4/lxml 直接照碼位給你一個
# `\x99` 控制字元——md 裡就存進一個看不見的亂碼。在交給 bs4 之前先改寫成
# 真正的字元,輸出才對得上瀏覽器所見(順帶讓自我稽核兩側一致:對照那側
# 走的是 Python 的 html.unescape,它有做這個對照)
_NUM_ENTITY_RE = re.compile(r"&#(\d+);|&#[xX]([0-9a-fA-F]+);")


def _fix_c1_entities(html: str) -> str:
    def sub(m: re.Match) -> str:
        code = int(m.group(1)) if m.group(1) else int(m.group(2), 16)
        if not 0x80 <= code <= 0x9F:
            return m.group(0)
        try:
            return bytes([code]).decode("cp1252")
        except UnicodeDecodeError:  # cp1252 有五個未定義碼位,原樣留著
            return m.group(0)
    return _NUM_ENTITY_RE.sub(sub, html)


def _converter():
    """共用一個 MarkdownConverter,而且直接吃現成的 soup 節點。

    `markdownify.markdownify(str(tag))` 每次呼叫都做三件重工:把子樹
    序列化回 HTML、新建一個 converter、再用**純 Python 的 html.parser**
    重新解析——而整份文件在 _html_blocks 早就用 lxml 解析過了。實測
    800 個段落 165ms → 30ms(5.4 倍),輸出逐字相同。"""
    global _CONVERTER
    if _CONVERTER is None:
        mod = _ensure_markdownify()
        _CONVERTER = mod.MarkdownConverter(heading_style="ATX", strip=["img"])
    return _CONVERTER


def _inline_md(tag) -> str:
    """行內格式(粗體/斜體/連結)交給 markdownify,區塊結構自己走。

    markdownify 的表格處理弱、也不知道我們要把圖片抽進 assets,所以只讓它
    處理行內——區塊層級由本模組控制。"""
    try:
        text = _converter().convert_soup(tag)
    except Exception:
        logger.debug("markdownify 轉換失敗,退回純文字", exc_info=True)
        return tag.get_text(" ", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _own_md(tag, mod) -> str:
    """這個標籤**自己**的文字,不含巢狀區塊元素裡的。

    容器會巢狀,而每一層都會被 find_all 走訪到——直接 get_text 的話同一段
    話會在外層每一層各出現一次(一份三層 div 的頁面就是三份)。所以只收
    「行內子節點」,巢狀的區塊元素留給它們自己那一輪出場。

    先用便宜的字串檢查擋掉「純容器」(絕大多數 div 沒有自己的文字),
    有文字才付重新解析一小段 HTML 的成本——那是為了讓行內格式
    (粗體/連結)仍然走 markdownify,與其他區塊一致。"""
    parts = [
        str(c) for c in tag.children
        if getattr(c, "name", None) not in _ALL_BLOCK
    ]
    inner = "".join(parts)
    if not html_mod.unescape(_TAG_RE.sub("", inner)).strip():
        return ""
    return _inline_md(mod.BeautifulSoup(f"<div>{inner}</div>", "lxml"))


def _table_rows(table_tag) -> tuple[list[list[str]], bool]:
    """HTML 表格 → 方形的列(rowspan/colspan 展開成重複值)。

    markdown 表格沒有跨欄跨列,只能把值填滿被合併的每一格。回傳的第二個
    值表示有沒有發生展開,呼叫端據此下標記——展開後讀的人分不出
    「本來就重複」與「本來是合併」,這是實打實的失真。"""
    grid: dict[tuple[int, int], str] = {}
    spanned = False
    max_col = 0
    rows_tags = table_tag.find_all("tr")
    for r, tr in enumerate(rows_tags):
        c = 0
        for cell in tr.find_all(["td", "th"], recursive=False) or tr.find_all(["td", "th"]):
            while (r, c) in grid:
                c += 1
            try:
                rs = min(max(int(cell.get("rowspan", 1) or 1), 1), _MAX_SPAN)
                cs = min(max(int(cell.get("colspan", 1) or 1), 1), _MAX_SPAN)
            except (TypeError, ValueError):
                rs = cs = 1
            if rs > 1 or cs > 1:
                spanned = True
            text = cell.get_text(" ", strip=True)
            for dr in range(rs):
                for dc in range(cs):
                    grid[(r + dr, c + dc)] = text
            c += cs
            max_col = max(max_col, c)
    if not grid:
        return [], False
    n_rows = max(r for r, _ in grid) + 1
    rows = [[grid.get((r, c), "") for c in range(max_col)] for r in range(n_rows)]
    return [r for r in rows if any(v.strip() for v in r)], spanned


def _img_block(
    tag, assets: AssetsDir, resources: dict[str, bytes], base: Path,
    ocr_enabled: bool = True,
) -> tuple[list[Block], bool]:
    """`<img>` / SVG 的 `<image>` → Image block。回傳 (blocks, 是否為未下載的遠端圖)。

    三種來源:mht 內嵌資源(已解出的位元組)、data: URI、本機相對路徑。
    http(s) 一律不碰——隱私規格 spec §7。

    位址屬性有三種寫法:`<img src>`、SVG 1.1 的 `xlink:href`、SVG 2 的
    `href`(Calibre 匯出的電子書用後者)。bs4 對 `xlink:href` 給的鍵是
    去掉命名空間的 `href`,但原樣寫著 `xlink:href` 的檔案也有,兩個都試。"""
    src = next(
        (v.strip() for v in (
            tag.get("src"), tag.get("href"), tag.get("xlink:href"),
        ) if (v or "").strip()),
        "",
    )
    alt = (tag.get("alt") or "").strip() or "圖片"
    if not src:
        return [], False

    def _store(data: bytes, suffix: str) -> tuple[list[Block], bool]:
        """存進 assets 並把圖裡的文字也撈出來(給 AI 用時那才是重點)。"""
        link = assets.add_bytes(data, suffix)
        out: list[Block] = [Image(link, alt)] if link else []
        out.extend(docimage.ocr_image_bytes(data, f"圖片「{alt}」", ocr_enabled))
        return out, False

    if src in resources:
        return _store(
            resources[src], Path(urllib.parse.urlparse(src).path).suffix or ".png",
        )
    if src.lower().startswith("data:"):
        try:
            header, _, payload = src.partition(",")
            data = base64.b64decode(payload) if ";base64" in header else urllib.parse.unquote_to_bytes(payload)
        except Exception:
            logger.debug("data: URI 解碼失敗,略過", exc_info=True)
            return [], False
        ext = ".png"
        m = re.search(r"image/([\w.+-]+)", header)
        if m:
            ext = "." + m.group(1).split("+")[0]
        return _store(data, ext)
    if re.match(r"^[a-zA-Z][\w+.-]*:", src):  # http:/https:/ftp: … 一律不連外
        return [], True
    local = (base / urllib.parse.unquote(src)).resolve()
    try:
        if local.is_file() and local.stat().st_size < 64 * 1024 * 1024:
            return _store(local.read_bytes(), local.suffix or ".png")
    except OSError:
        logger.debug("本機圖片讀取失敗:%s", local, exc_info=True)
    return [], False


def _html_blocks(
    html: str, assets: AssetsDir, resources: dict[str, bytes], base: Path,
    ocr_enabled: bool = True,
) -> list[Block]:
    mod = _ensure_bs4()
    soup = mod.BeautifulSoup(html, "lxml")
    for junk in soup(["script", "style", "noscript"]):
        junk.decompose()

    blocks: list[Block] = []
    remote = 0
    spanned_any = False
    root = soup.body or soup
    # root 自己也要算一份:文字直接掛在 <body> 底下的頁面 find_all 看不到
    for tag in [root] + root.find_all(_BLOCK_TAGS + _CONTAINER_TAGS):
        # 祖先有 table 就跳過:格內的段落已經在表格裡了,巢狀表格則由
        # 外層的 get_text 攤平——兩種情況同一條規則
        if tag.find_parent("table") is not None:
            continue
        name = tag.name.lower()
        if name == "table":
            rows, spanned = _table_rows(tag)
            spanned_any = spanned_any or spanned
            if rows:
                blocks.append(Table(rows, has_header=True))
        elif name in ("img", "image"):
            imgs, is_remote = _img_block(tag, assets, resources, base, ocr_enabled)
            blocks.extend(imgs)
            remote += int(is_remote)
        elif name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            text = tag.get_text(" ", strip=True)
            if text:
                blocks.append(Heading(int(name[1]), text))
        elif name in ("p", "pre"):
            # 這兩個不會包住區塊元素(lxml 會把誤寫的結構拆掉),整段交給
            # markdownify 才留得住 pre 的換行與 p 的行內格式
            text = _inline_md(tag)
            if text:
                blocks.append(Para(text))
        else:
            # 容器、li、blockquote:只取自己的文字。li 套 ul、blockquote
            # 包 p 都會巢狀,整段收下的話同一句會重複出現
            text = _own_md(tag, mod)
            if text:
                blocks.append(Para(text))

    notes: list[Block] = []
    if remote:
        notes.append(Note(
            f"本文件有 {remote} 張圖片來自網路,基於隱私原則沒有下載"
            "(本工具除了首次下載 AI 模型之外不連外)",
            docmd.KIND_REMOTE_IMAGE,
        ))
    if spanned_any:
        notes.append(Note(
            "表格中的跨欄/跨列儲存格已展開成重複值", docmd.KIND_MERGED_CELLS,
        ))
    return notes + blocks


# 這些的內容本來就不該出現在輸出裡(_html_blocks 也是先 decompose 掉)。
# **註解一定要一起剝**:`<!-- 區塊說明 -->` 在 regex 眼裡是看得見的文字,
# 不剝的話每份用心寫註解的樣板都會被誤報
_NON_CONTENT_RE = re.compile(
    r"<!--.*?-->|<(script|style|noscript|head)\b[^>]*>.*?</\1\s*>", re.S | re.I,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _visible_texts(html: str) -> list[str]:
    """網頁上「看得到的文字」,一段連續的文字算一筆。

    **刻意用 regex 剝標籤、不用 bs4**:自我稽核要的是一份**與 reader 無關**
    的對照——bs4 走的正是 reader 走的那條路,用它比對等於自己跟自己對答案,
    reader 漏掉哪個節點它就一起漏。

    切在**每個標籤邊界**上、而不是切「區塊」:HTML 會巢狀,
    `<div>甲<p>乙</p>丙</div>` 的 div 這一塊文字是「甲丙」,而輸出裡
    甲丙(div 自己的)與乙(p 的)是兩個 Block、順序還是甲乙丙——拿整塊去
    比對必定找不到。實測一份正常轉出來的電子書噴 2,312 筆假落差,逐筆查
    全部都在輸出裡。切到葉節點就沒有這個對不齊的問題(代價是被行內標籤
    切碎的短句會低於 GAP_MIN_CHARS 而不列入比對,那是可接受的靈敏度損失)。
    """
    body = _NON_CONTENT_RE.sub(" ", html)
    out: list[str] = []
    for chunk in _TAG_RE.split(body):
        text = html_mod.unescape(chunk).strip()
        if text:
            out.append(text)
    return out


# epub 的入口:規格規定一定在這個位置,指向真正的 OPF
_EPUB_CONTAINER = "META-INF/container.xml"


def _epub_spine(opf_xml: bytes, opf_dir: str) -> tuple[list[str], dict[str, str]]:
    """OPF → (依閱讀順序排好的內文檔路徑, 書籍中繼資料)。

    **一定要照 spine 的順序**:zip 裡的檔名排序與閱讀順序常常對不上
    (`chapter10` 會排在 `chapter2` 前面),照檔名走會把整本書打亂。"""
    mod = _ensure_bs4()
    soup = mod.BeautifulSoup(opf_xml, "xml")
    manifest = {
        item.get("id"): item.get("href")
        for item in soup.find_all("item") if item.get("id") and item.get("href")
    }
    order: list[str] = []
    for ref in soup.find_all("itemref"):
        href = manifest.get(ref.get("idref"))
        if href:
            order.append(f"{opf_dir}{href}" if opf_dir else href)
    meta: dict[str, str] = {}
    for tag, label in (("title", "書名"), ("creator", "作者"),
                       ("publisher", "出版"), ("date", "日期")):
        node = soup.find(tag)
        text = (node.get_text(" ", strip=True) if node else "").strip()
        if text:
            meta[label] = text
    return order, meta


def convert_epub(src: Path, assets: AssetsDir, ocr_enabled: bool = True) -> list[Block]:
    """epub → Blocks。

    epub 就是「一包 XHTML + 一份閱讀順序」,所以**整包解到暫存目錄、
    再逐篇走 `_html_blocks`**——相對路徑的插圖就自然從磁碟解得到,不必
    另外做一套 zip 內的資源查表(mht 那條 `resources` 是為內嵌資源設計的,
    對 epub 反而繞遠路)。

    順序一定照 OPF 的 spine,不照 zip 的檔名(見 `_epub_spine`)。"""
    import tempfile
    import zipfile

    from meeting_scribe import pipeline

    try:
        with zipfile.ZipFile(src) as z:
            container = z.read(_EPUB_CONTAINER)
            rootfile = re.search(rb'full-path="([^"]+)"', container)
            if not rootfile:
                raise ValueError("container.xml 沒有 full-path")
            opf_path = rootfile.group(1).decode("utf-8", "replace")
            opf_dir = opf_path.rpartition("/")[0]
            order, meta = _epub_spine(z.read(opf_path), f"{opf_dir}/" if opf_dir else "")
            with tempfile.TemporaryDirectory(
                prefix=pipeline.TMP_PREFIX + "epub-", ignore_cleanup_errors=True,
            ) as tmp:
                root = Path(tmp)
                z.extractall(root)
                return _epub_blocks(root, order, meta, assets, ocr_enabled)
    except UserFacingError:
        raise
    except Exception as e:
        raise UserFacingError(
            f"無法解析電子書「{src.name}」:檔案可能已損壞,或不是有效的 epub"
        ) from e


def _epub_blocks(
    root: Path, order: list[str], meta: dict[str, str],
    assets: AssetsDir, ocr_enabled: bool,
) -> list[Block]:
    """解開後的 epub → Blocks(書籍資訊 + 逐篇內文)。"""
    blocks: list[Block] = []
    if meta:
        # 書名/作者要在**內容裡**,不能只放 frontmatter:RAG 切塊後的
        # chunk 會脫離上下文(同 docmail._header_blocks 的理由)
        blocks.append(Para("\n".join(f"- {k}:{v}" for k, v in meta.items())))
    for rel in order:
        doc = root / rel
        if not doc.is_file():
            logger.debug("epub 的 spine 指到不存在的檔案:%s", rel)
            continue
        try:
            html, _ = _decode_html(doc.read_bytes())
            blocks.extend(_html_blocks(html, assets, {}, doc.parent, ocr_enabled))
        except Exception:  # noqa: BLE001 - 一篇壞掉不該讓整本書轉不出來
            logger.info("epub 的 %s 解析失敗,略過", rel, exc_info=True)
            blocks.append(Note(
                f"電子書的「{rel}」這一篇解析失敗,內容沒有取出",
                docmd.KIND_EXTRACTION_GAP,
            ))
    if not order:
        blocks.append(Note("這本電子書沒有可讀的內文", docmd.KIND_BLANK_PAGE, lossy=False))
    return blocks


def convert_html(src: Path, assets: AssetsDir, ocr_enabled: bool = True) -> list[Block]:
    text, enc = _decode_html(src.read_bytes())
    blocks = (
        doctext.encoding_note(enc)
        + _html_blocks(text, assets, {}, src.parent, ocr_enabled)
    )
    return blocks + docmd.extraction_gap_note(
        docmd.missing_from(_visible_texts(text), blocks),
    )


def convert_mht(src: Path, assets: AssetsDir, ocr_enabled: bool = True) -> list[Block]:
    """mht/mhtml → Blocks(內嵌資源一併取出)。"""
    try:
        msg = message_from_bytes(src.read_bytes(), policy=policy.default)
    except Exception as e:
        raise UserFacingError(
            f"無法解析網頁封存檔「{src.name}」:檔案可能已損壞"
        ) from e

    html = ""
    html_enc = "utf-8"
    resources: dict[str, bytes] = {}
    for part in msg.walk():
        ctype = (part.get_content_type() or "").lower()
        if ctype == "multipart/related":
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            continue
        if payload is None:
            continue
        if ctype == "text/html" and not html:
            html, html_enc = _decode_html(payload)
            continue
        # 圖片等資源:HTML 裡的 src 可能寫 Content-Location 的完整網址,
        # 也可能寫 cid:<Content-ID>,兩種索引都放進去
        location = (part.get("Content-Location") or "").strip()
        cid = (part.get("Content-ID") or "").strip().strip("<>")
        if location:
            resources[location] = payload
        if cid:
            resources[f"cid:{cid}"] = payload

    if not html:
        raise UserFacingError(
            f"網頁封存檔「{src.name}」裡找不到網頁內容,可能不是有效的 .mht 檔"
        )
    # 編碼標記與 convert_html 走同一份:mht 也可能是 Big5,判錯時
    # 一聲不吭正是本功能最不能接受的「無聲失真」
    blocks = (
        doctext.encoding_note(html_enc)
        + _html_blocks(html, assets, resources, src.parent, ocr_enabled)
    )
    # 自我稽核與 convert_html 同一份:mht 的內容就是那份 html
    return blocks + docmd.extraction_gap_note(
        docmd.missing_from(_visible_texts(html), blocks),
    )
