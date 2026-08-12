r"""文件轉 Markdown 的中介表示、輸出渲染與落檔(doc* 模組群的共用底層)。

各 reader(doctext / docoffice / docpdf / docweb …)只負責把來源檔讀成
`list[Block]`,**不寫檔、不做繁化**——落檔與簡轉繁由 docpipe 統一做一次
(同 pipeline.finalize 對整份逐字稿只呼叫一次 convert.to_taiwan_traditional)。
新增一種來源格式只要多一個 reader,輸出形狀與落檔安全規則不必重寫。

**產物主要餵 RAG / 知識庫**(使用者 2026-08-01 指定),人閱讀是次要用途,
輸出形狀為此設計——三條規則見 `render()` 的 docstring。

本模組也是**唯一一處會寫入(以及 rmtree)使用者文件資料夾**的地方:工具
至今所有輸出都落在 output/ 與 %LOCALAPPDATA%(我們自己的地盤),文件轉檔
是第一次把手伸進使用者的檔案旁邊,而且批次一次做數十個檔。安全規則因此
全部集中在 AssetsDir 與 target_md_path 兩處,不散落到各 reader。
"""
import hashlib
import logging
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from meeting_scribe.errors import UserFacingError

logger = logging.getLogger(__name__)

# ---- 失真標記的種類(frontmatter 的 lossy_kinds)----
# 固定英文 token,不是繁中句子:建 RAG 知識庫時要靠它機器篩選(例如「這批
# 不要含 OCR 辨識的內容」)。給人看的繁中說明留在 inline 的〔〕標記裡。
# 新增種類要同步更新 README 的對照表。
KIND_MERGED_CELLS = "merged_cells"
KIND_CELL_COLOR = "cell_color"
KIND_CHART = "chart"
KIND_PIVOT = "pivot"
KIND_COND_FORMAT = "conditional_format"
KIND_FORMULA_NO_CACHE = "formula_no_cache"
KIND_HUGE_SHEET = "huge_sheet"
KIND_TRACKED_CHANGES = "tracked_changes"
KIND_NESTED_TABLE = "nested_table"
KIND_SMARTART = "smartart"
KIND_REMOTE_IMAGE = "remote_image"
KIND_ENCODING_GUESS = "encoding_guess"
KIND_IMAGE_ONLY = "image_only"
KIND_SCANNED_PAGE = "scanned_page"
KIND_BLANK_PAGE = "blank_page"
KIND_OCR = "ocr"
KIND_ASSET_FAILED = "asset_failed"
# Word 的自動編號:清單的編號與階層沒有進到輸出。**內容本身不打算修**
# ——編號存在 `numbering.xml`、由 Word 算出來,`w:t` 裡根本沒有這串字,
# 所以直接 unzip document.xml 讀也一樣拿不到(自我稽核的對照側同樣看不見,
# 天生偵測不到)。要標記是因為**沉默看起來跟乾淨一樣**:一份 76% 段落
# 帶編號的文件輸出 `lossy: 0`,那是主動宣稱「什麼都沒丟」(使用者
# 2026-08-02 指出,與 SVG `<image>` 同一類問題)
KIND_NUMBERING = "numbering"
# 含巨集(.xlsm/.docm 等):程式碼本身沒有轉出來
KIND_MACRO = "macro"
# 自我稽核抓到的落差:原始檔裡有文字,但沒出現在輸出裡。**這是工具自己的
# bug 的標記**,不是來源檔的問題——但寧可讓使用者看見,也不要安靜地少
KIND_EXTRACTION_GAP = "extraction_gap"
# 舊格式已升級(不是失真,只是告知來源經過一次轉換)
KIND_LEGACY_UPGRADE = "legacy_upgrade"
# 郵件附件(階段三)
KIND_ATTACHMENT = "attachment"
KIND_DEPTH_LIMIT = "depth_limit"
KIND_CYCLE = "cycle"

NOTE_OPEN, NOTE_CLOSE = "〔", "〕"

# frontmatter 的 converter 欄位值,同時是「這份 md 是本工具產生的」的憑據
# (target_md_path 靠它決定覆寫或改名)
GENERATED_MARKER = "meeting-scribe/doc2md"

# assets 目錄的標記檔與命名慣例。**兩者都是單一出處**:docsrc 展開
# 資料夾時要靠尾綴跳過我們自己倒出來的圖片,那份判準必須跟這裡同源
# ——名稱慣例改了卻只改一處,重跑同一個資料夾就會把上次抽出的圖片
# 當成新的來源檔,每張圖再生一份 md
ASSETS_MARKER = ".meeting-scribe-assets"
ASSETS_SUFFIX = ".assets"

# 超寬表改逐筆區塊的門檻(使用者 2026-08-01 決定改逐筆;原為一律維持表格,
# 前提從「給人看」變成「給 AI 看」後翻案)。理由見 render_records
WIDE_TABLE_COLS = 12

# 單一檔名的長度上限。Windows MAX_PATH 是 260,而 <名>.assets\<名>.assets\…
# 在郵件附件遞迴(階段三)下疊得很快,來源檔名又常常是一整句話
_MAX_NAME_CHARS = 60

# Windows 保留裝置名:這些名字(含加副檔名)建不出檔案。附件檔名來自不可
# 信來源(階段三的郵件),一律過 sanitize_name
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


# ---- 中介表示 ----

@dataclass(frozen=True)
class Heading:
    """章節標題。level 由 reader 以「文件內視角」給(1 = 最上層章節);
    render 會整體 +1,讓檔案的 H1 唯一保留給文件標題(見 render)。"""
    level: int
    text: str


@dataclass(frozen=True)
class Para:
    text: str


@dataclass(frozen=True)
class Table:
    """一般表格。caption 非空時渲染成表格前的一行說明——RAG 切塊後
    chunk 可能只剩表格本身,沒有標題就不知道這是什麼表(見 render 規則 2)。"""
    rows: list[list[str]]
    has_header: bool = True
    caption: str = ""


@dataclass(frozen=True)
class Records:
    """超寬表的逐筆區塊表示(欄數 > WIDE_TABLE_COLS 時由 reader 改用此型別)。"""
    header: list[str]
    rows: list[list[str]]
    context: str = ""


@dataclass(frozen=True)
class Image:
    """已寫入 assets 的圖片。rel_path 必須是 AssetsDir.add_bytes 的回傳值
    (已做過 URL 編碼),不可自己拼字串。"""
    rel_path: str
    alt: str = ""


@dataclass(frozen=True)
class Note:
    """標記。text 是給人看的繁中說明,kind 是給機器篩的固定 token。

    無聲失真是本功能最不能接受的事(使用者明講):轉出來的東西少了什麼,
    必須在檔案裡看得見,而不是安靜消失。

    `lossy=False` 用在「有事要說,但沒有東西被丟掉」的註記(空檔案、
    舊格式已升級)。frontmatter 的 `lossy` 只數 True 的那些——RAG 建庫
    端要拿這個數字排序或設門檻,把「這個檔是空的」也算成一次失真會讓
    那個數字失去意義。"""
    text: str
    kind: str = ""
    lossy: bool = True


@dataclass(frozen=True)
class Raw:
    """已經是 markdown 的內容(階段三郵件附件遞迴內嵌用),原樣輸出。

    traditionalised:這份內容**在送進來之前就已經簡轉繁過了**,而且真的
    改到了字。Raw 刻意不經 docpipe.traditionalize(轉了會把
    `[標題](报表.png)` 變成指向不存在的檔案),所以「有沒有繁化過」在
    docpipe 那邊用「轉換前後文字有沒有變」是問不出來的——音訊逐字稿正是
    這種情形(繁化在 pipeline.render_transcript 內、標點之前就做完了)。
    不補這個欄位的話,frontmatter 的 traditionalised 會對每一份逐字稿都
    謊報 false。"""
    text: str
    traditionalised: bool = False


Block = Heading | Para | Table | Records | Image | Note | Raw


# ---- 自我稽核的共用比對(各 reader 都用得到)----

# 一段文字要多長才值得比對。太短的片段(編號、單一字母、表格裡的「-」)
# 在輸出裡本來就會被拆散或合併,比對只會製造雜訊
GAP_MIN_CHARS = 8
# Note 裡最多舉幾個例子:目的是「讓人知道去哪裡看」,不是列清單
_GAP_EXAMPLES = 3
_WS_RE = re.compile(r"\s+")
_ESCAPE_RE = re.compile(r"\\")
# 超連結:`[文字](網址)` → 只留文字。網址那半段可能被包在 <> 裡(網址含
# 空白或括號時的寫法,見 docoffice._as_link)。
# **連結文字裡的 `\[` `\]` 一定要放行**:那是 `_as_link` 對原文本來就有的
# 方括號做的跳脫,而寫成 `[^\]]*` 的話那個跳脫過的 `]` 會提前結束比對、
# 整條規則失效——實測 176 份真實檔有 3 份中招(`[How can merchants avoid
# duplicated [MerchantTradeNo]?](…)`、`[[PDF] Individualized…](…)`),
# 表現出來是「這一段沒轉出來」的假警報
_LINK_RE = re.compile(r"\[((?:\\.|[^\]\n\\])*)\]\((?:<[^>\n]*>|[^)\n]*)\)")
# 自動連結:`<https://…>` → 只留網址本身。文字與網址相同時寫的就是這個形式
# (見 docoffice._as_link),而**原始檔裡是光禿禿的一串網址**——不抹掉這對
# 角括號的話,「本公司 (https://a.com/) 成立於…」這種句子(實測兩份真實檔
# 各兩段)會整段被誤報成沒轉出來
_AUTOLINK_RE = re.compile(r"<([a-z][\w+.-]*:[^>\s]*)>", re.I)
# 註腳記號:`[註3]`(見 note_marker)
_MARK_RE = re.compile(r"\[(?:註|附註)\d+\]")

# 註腳/章節附註在內文裡的記號。**格式定在這裡**是因為 squash 要抹掉它
# ——兩邊各寫一份的話,改了記號樣式就會讓稽核開始誤報整批文件
FOOTNOTE_MARK = "註"
ENDNOTE_MARK = "附註"


def note_marker(mark: str, number: str) -> str:
    """內文裡的註腳記號:`[註3]`。"""
    return f"[{mark}{number}]"


def squash(text: str) -> str:
    r"""稽核比對用的正規化:壓掉所有空白、markdown 跳脫、以及**輸出端自己
    加上去的記號**(超連結網址、註腳記號)。

    輸出會重排、合併、加標點,逐字比對只會製造雜訊,所以空白全壓掉。

    **反斜線也要壓掉**:markdownify 會把 `_` `*` 跳脫成 `\_` `\*`,那個
    反斜線是渲染需要、原始檔裡從來沒有——不壓的話,任何含底線或星號的
    文字(版本號 `3.0.0_01`、變數名、Python 函式)都會被誤報成整段消失
    (2026-08-02 網頁抽樣實測,這是剩下最大的一類誤報)。

    **超連結與註腳記號同理**(2026-08-04):對照側是原始檔的 `w:t` 文字,
    那裡只有「請見官網說明」;輸出側是「請見[官網](https://…)說明」,
    網址是我們**加進去**的,不抹掉的話原文就不再是輸出的子字串——一份
    20% 的文件都有外部連結的語料會整批誤報「整段沒轉出來」。抹掉的是
    兩邊共用的正規化,原始檔裡真的寫著 `[註1]` 的話兩側一起抹,不影響。

    這個函式只服務稽核比對,不碰輸出。"""
    text = _LINK_RE.sub(r"\1", text or "")
    text = _MARK_RE.sub("", _AUTOLINK_RE.sub(r"\1", text))
    return _ESCAPE_RE.sub("", _WS_RE.sub("", text))


def blocks_text(blocks: list) -> str:
    """一批 Block 的所有文字(含表格內容與 caption),壓過空白。

    這是自我稽核的「乾草堆」——**表格與 caption 一定要納進來**,不然
    「內容其實在表格裡」會被誤報成整段消失。"""
    parts = [getattr(b, "text", "") or "" for b in blocks]
    for b in blocks:
        if isinstance(b, Table):
            parts.extend(" ".join(r) for r in b.rows)
            parts.append(b.caption or "")
        elif isinstance(b, Records):
            parts.append(" ".join(b.header))
            parts.extend(" ".join(r) for r in b.rows)
            parts.append(b.context or "")
    return squash("\n".join(parts))


def missing_from(originals: list[str], blocks: list) -> list[str]:
    """原始檔裡有、但沒出現在輸出裡的片段。

    比對只看「整段都不見」:輸出會重排、合併、加標點,而我們要抓的是
    「某一類內容整組沒被走訪」這種**無聲**的漏,不是字元級的差異。"""
    haystack = blocks_text(blocks)
    return [
        o for o in originals
        if len(squash(o)) >= GAP_MIN_CHARS and squash(o) not in haystack
    ]


def extraction_gap_note(missing: list[str], what: str = "文字") -> list:
    """落差 → 給使用者看的標記(進 frontmatter 的 lossy_kinds)。

    **這是工具自己的 bug 的標記**,但寧可讓使用者看見、也不要安靜地少
    ——看得見才修得掉,而「某類節點沒被走訪」這種漏沒有其他症狀。"""
    if not missing:
        return []
    examples = "、".join(
        f"「{_WS_RE.sub(' ', m).strip()[:20]}」" for m in missing[:_GAP_EXAMPLES]
    )
    more = f" 等 {len(missing)} 處" if len(missing) > _GAP_EXAMPLES else ""
    return [Note(
        f"原始檔有 {len(missing)} 段{what}沒有被轉換出來(例如 {examples}{more})"
        "——這是轉檔工具的限制,請開啟原始檔查看那些內容",
        KIND_EXTRACTION_GAP,
    )]


@dataclass(frozen=True)
class DocMeta:
    """一份輸出 md 的檔頭資訊。

    converted_at 由呼叫端(docpipe)傳入而不是在此取現在時間:測試要能
    產出可預期的輸出,且同一批次的所有檔案該用同一個時間戳。"""
    source: Path
    title: str
    converted_at: str
    source_type: str = ""
    ocr_used: bool = False
    extra: dict = field(default_factory=dict)


# ---- 渲染 ----

def lazy_import(name: str, what: str):
    """惰性 import,失敗翻成繁中並指路。

    ImportError 的原文是 cryptic 英文(`No module named 'docx'`),對非
    技術同仁毫無意義;而這個錯真正的成因幾乎一定是「環境沒裝好」,所以
    指路到安裝.bat 而不是叫人去查套件名。

    放在 docmd 是因為每個 reader 都已經 import 它——原本這段在
    docoffice,其他四個模組各自手抄了一份同構的 try/except,同一句
    繁中文案散在 6 個地方。"""
    try:
        return __import__(name)
    except ImportError as e:
        raise UserFacingError(
            f"缺少讀取{what}所需的元件,請重新執行「安裝.bat」把環境裝齊"
        ) from e


def _yaml_scalar(value) -> str:
    """YAML 純量的保守寫法:非單純字串一律加雙引號並跳脫。

    來源檔名什麼字元都可能有(冒號、井號、引號、前導空白),裸寫進
    frontmatter 會讓整份 YAML 解析失敗——而 RAG 建庫端多半是先 parse
    frontmatter 再處理,壞一份就整份進不了知識庫。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    s = str(value)
    if s and re.fullmatch(r"[0-9A-Za-z一-鿿_.\-/\\ ]+", s) and s == s.strip():
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _frontmatter(meta: DocMeta, notes: list[Note]) -> list[str]:
    lossy = [n for n in notes if n.lossy]
    kinds: list[str] = []
    for n in lossy:
        if n.kind and n.kind not in kinds:
            kinds.append(n.kind)
    lines = [
        "---",
        f"source_file: {_yaml_scalar(meta.source.name)}",
        f"source_path: {_yaml_scalar(str(meta.source))}",
        f"source_type: {_yaml_scalar(meta.source_type or meta.source.suffix.lstrip('.').lower())}",
        f"converted_at: {_yaml_scalar(meta.converted_at)}",
        f"converter: {_yaml_scalar(GENERATED_MARKER)}",
        f"lossy: {len(lossy)}",
        "lossy_kinds: [" + ", ".join(kinds) + "]",
        f"ocr_used: {_yaml_scalar(bool(meta.ocr_used))}",
    ]
    for key, value in (meta.extra or {}).items():
        lines.append(f"{key}: {_yaml_scalar(value)}")
    lines.append("---")
    return lines


def _cell(text) -> str:
    """儲存格文字 → 可安插進 markdown 表格的一格。

    `|` 會提前結束欄位、換行會提前結束整列——兩者都會讓表格「錯位」而
    不是「顯示不好看」,AI 讀到的欄位對應就整個歪掉。"""
    s = "" if text is None else str(text)
    return s.replace("\\", "\\\\").replace("|", r"\|").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>").strip()


def render_table(rows: list[list[str]], has_header: bool = True, caption: str = "") -> str:
    """一般表格 → markdown 表格。

    markdown 表格語法**必須有表頭列**,無表頭的資料表補一列空表頭,否則
    第一列會被吃掉當表頭(資料無聲少一列)。"""
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    norm = [[_cell(c) for c in r] + [""] * (width - len(r)) for r in rows]
    if has_header:
        head, body = norm[0], norm[1:]
    else:
        head, body = [""] * width, norm
    out = []
    if caption:
        out.append(f"**{caption}**")
        out.append("")
    out.append("| " + " | ".join(head) + " |")
    out.append("| " + " | ".join(["---"] * width) + " |")
    out.extend("| " + " | ".join(r) + " |" for r in body)
    return "\n".join(out)


def render_records(header: list[str], rows: list[list[str]], context: str = "") -> str:
    """超寬表 → 逐筆區塊(每筆一個 `###` 標題 + 「欄名:值」清單)。

    為什麼寬表不維持 markdown 表格(使用者 2026-08-01 決定):
    1. **不會欄位錯位** —— 40 欄的表格,AI 回答「第 27 欄是什麼」時很容易
       數錯欄;逐筆區塊的欄名與值直接相鄰,沒有數欄位這件事。
    2. **每筆是自包含的 chunk** —— RAG 切塊器攔腰截斷表格後,那個 chunk
       裡**沒有表頭**,整段等於報廢;逐筆區塊每筆都完整可檢索。
    3. 每筆用 `###` 標題,切塊器自然以「一筆」為單位切。

    代價是每列重複欄名(數千列 × 數十欄會多出數十萬 token)。RAG 是檢索後
    只取命中的 chunk、不是整份塞進 context,所以這個代價付得值——**但若
    日後改成「貼進對話」的用法,這個取捨要重新評估**。"""
    if not rows:
        return ""
    head = [str(h).strip() if h is not None else "" for h in (header or [])]
    total = len(rows)
    prefix = f"{context} · " if context else ""
    out: list[str] = []
    for i, row in enumerate(rows, start=1):
        out.append(f"### {prefix}第 {i} 筆(共 {total} 筆)")
        out.append("")
        for j, value in enumerate(row):
            name = head[j] if j < len(head) and head[j] else f"欄位 {j + 1}"
            text = "" if value is None else str(value).strip()
            if text:
                out.append(f"- {name}:{text}")
        out.append("")
    return "\n".join(out).rstrip()


def render_note(note: Note) -> str:
    return f"{NOTE_OPEN}{note.text}{NOTE_CLOSE}"


def render(blocks: list[Block], meta: DocMeta) -> str:
    """Blocks + 檔頭資訊 → 完整的 md 文字。

    三條 RAG 專屬規則(產物主要餵知識庫,使用者 2026-08-01 指定):

    1. **單一 H1**。檔案只有一個 H1(文件標題),內容一律從 H2 起——所以
       reader 給的 Heading.level 在這裡整體 +1。切塊器普遍以標題階層分段,
       兩個 H1 會被當成兩份文件。reader 若推不出階層(例如 PDF 的字級判斷
       失敗),該退回「平坦結構 + 頁碼標題」而不是瞎猜:**錯的階層比沒有
       階層更糟**,它會讓切塊切在錯的地方。
    2. **每個區塊要能獨立理解**。chunk 會脫離上下文,所以表格帶 caption、
       OCR 文字標明來源、逐筆區塊每筆帶工作表名。
    3. 失真標記彙總進 frontmatter 的 lossy / lossy_kinds(機器可讀),繁中
       說明留在 inline 的〔〕(給人看,也讓 AI 知道這裡原本有東西、不要
       腦補)。
    """
    notes = [b for b in blocks if isinstance(b, Note)]
    out = _frontmatter(meta, notes)
    out.append("")
    out.append(f"# {meta.title}")
    out.append("")
    for b in blocks:
        if isinstance(b, Heading):
            level = min(max(b.level, 1) + 1, 6)  # +1 保留 H1 給文件標題
            text = b.text.strip()
            if text:
                out.append(f"{'#' * level} {text}")
                out.append("")
        elif isinstance(b, Para):
            text = b.text.strip()
            if text:
                out.append(text)
                out.append("")
        elif isinstance(b, Table):
            # 超寬表改逐筆區塊的判斷在**這裡**做,不在各 reader:
            # 它是渲染政策,而門檻與理由都住在這個檔。放 reader 裡的
            # 結果是 6 個產生表格的地方只有 2 個記得抄這四行,同一份
            # xlsx 直接轉與匯出成 HTML 再轉會得到不同結果
            width = max((len(r) for r in b.rows), default=0)
            if width > WIDE_TABLE_COLS and len(b.rows) > 1:
                text = render_records(b.rows[0], b.rows[1:], b.caption)
            else:
                text = render_table(b.rows, b.has_header, b.caption)
            if text:
                out.append(text)
                out.append("")
        elif isinstance(b, Records):
            text = render_records(b.header, b.rows, b.context)
            if text:
                out.append(text)
                out.append("")
        elif isinstance(b, Image):
            out.append(f"![{b.alt or '圖片'}]({b.rel_path})")
            out.append("")
        elif isinstance(b, Note):
            out.append(render_note(b))
            out.append("")
        elif isinstance(b, Raw):
            if b.text.strip():
                out.append(b.text.rstrip())
                out.append("")
    return "\n".join(out).rstrip() + "\n"


# ---- 檔名與路徑安全 ----

def sanitize_name(name: str, fallback: str = "檔案") -> str:
    r"""不可信來源的檔名 → 可安全建立的檔名(不含目錄部分)。

    附件檔名(階段三的郵件)是不可信輸入:`..\..\evil.txt` 會把檔案寫到
    assets 之外。剝目錄分隔與 `..`、剝 Windows 保留裝置名、剝結尾的點與
    空白(Windows 會靜默截掉、造成撞名)、裁長度。呼叫端**仍必須**在寫入
    前做 is_relative_to 斷言——消毒是第一道,不是唯一一道。"""
    s = unicodedata.normalize("NFC", str(name or "")).strip()
    s = s.replace("\\", "/").split("/")[-1]
    s = re.sub(r'[<>:"|?*\x00-\x1f]', "_", s)
    s = s.strip(". ")
    if not s or set(s) <= {"."}:
        s = fallback
    stem, dot, suffix = s.rpartition(".")
    if not dot:
        stem, suffix = s, ""
    if stem.upper() in _WIN_RESERVED:
        stem = f"_{stem}"
    if len(stem) > _MAX_NAME_CHARS:
        stem = stem[:_MAX_NAME_CHARS]
    stem = stem.strip(". ") or fallback
    return f"{stem}.{suffix}" if suffix else stem


def asset_link(assets_dir_name: str, filename: str) -> str:
    """assets 內的檔案 → 可放進 md 的相對連結。

    含空白/井號/百分號的檔名不編碼會讓連結在多數 markdown 檢視器裡斷掉
    (`#` 之後被當成錨點)。斜線不編碼,否則目錄分隔會變成 %2F。"""
    return urllib.parse.quote(f"{assets_dir_name}/{filename}", safe="/")


class AssetsDir:
    r"""`<原檔名>.assets\` 目錄:圖片等資產的落地與相對連結。

    **本專案唯一會對使用者文件資料夾做 rmtree 的地方**,規則寫死:
    - 目錄不存在 → 建立並放標記檔
    - 存在且有標記檔 → 清空重建(重轉同一份檔的正常情境,不留舊圖殘渣)
    - 存在但**沒有**標記檔 → **絕不刪**,拋 UserFacingError

    沒有標記就不動,是因為使用者本來就可能有一個叫 `報表.assets` 的資料夾。
    刪錯一次就是同事的檔案沒了,而批次一次跑數十個檔、沒人會逐一確認。

    目錄是**延遲建立**的:沒有圖片的來源檔不該在人家資料夾裡留一個空的
    .assets(批次轉 50 個純文字檔會留 50 個空資料夾,使用者會以為壞了)。
    """

    def __init__(self, base: Path, stem: str):
        self.path = Path(base) / f"{sanitize_name(stem, 'doc')}{ASSETS_SUFFIX}"
        self.name = self.path.name
        self._ready = False
        self._seq = 0
        self._used: set[str] = set()
        # 內容雜湊 → 已經落地的相對連結,同一份文件內重複引用的圖只存一次
        self._by_digest: dict[str, str] = {}
        # 寫失敗的次數。政策不交辦給呼叫端——7 個呼叫端沒有一個會下
        # Note,結果是唯讀資料夾/路徑過長時**所有圖片安靜消失**,而
        # frontmatter 還寫 lossy: 0。由 failure_note() 統一補一顆
        self.failures = 0

    @property
    def created(self) -> bool:
        return self._ready

    def _prepare(self) -> None:
        if self._ready:
            return
        if self.path.exists():
            if not (self.path / ASSETS_MARKER).exists():
                raise UserFacingError(
                    f"資料夾已存在,而且不是本工具建立的,為了安全不會動它:{self.path}"
                    "——請先把它改名或移走,再重新轉檔。"
                )
            import shutil

            shutil.rmtree(self.path, ignore_errors=True)
        self.path.mkdir(parents=True, exist_ok=True)
        (self.path / ASSETS_MARKER).write_text(
            "本資料夾由「AI 文件.MD 轉換器」的文件轉檔功能建立,重新轉檔時會整個重建。\n"
            "請不要把自己的檔案放在這裡。\n",
            encoding="utf-8",
        )
        self._ready = True

    def _unique(self, filename: str) -> str:
        name = sanitize_name(filename, "asset")
        if name not in self._used:
            self._used.add(name)
            return name
        stem, dot, suffix = name.rpartition(".")
        if not dot:
            stem, suffix = name, ""
        i = 2
        while True:
            cand = f"{stem}-{i}.{suffix}" if suffix else f"{stem}-{i}"
            if cand not in self._used:
                self._used.add(cand)
                return cand
            i += 1

    def add_bytes(self, data: bytes, suffix: str = ".png", filename: str = "") -> str:
        """寫入一份資產,回傳可直接放進 `Image.rel_path` 的相對連結。

        寫入失敗(路徑過長、磁碟唯讀)不讓整份轉檔陣亡:回空字串,呼叫端
        改下 Note——少一張圖遠比整個檔案轉不出來好。

        **同樣的位元組只落一次檔**:一份文件裡同一張圖被引用很多次是常態
        (電子書每章的裝飾圖、Word 每頁的公司 logo),各存一份只是把同一
        堆位元組寫進磁碟幾十遍——2026-08-03 實測一本 React Router 電子書
        3,272 個圖檔裡只有 788 張不重複(76% 是副本)。連結指到同一個檔
        完全正確,md 那邊看不出差別。**只對自動命名的圖去重**:呼叫端明
        給 `filename` 的(郵件附件)那個名字本身有意義,兩個附件內容相同
        但檔名不同時,併掉會讓其中一個名字消失。"""
        explicit_name = bool(filename)
        digest = ""
        if not explicit_name:
            digest = hashlib.sha1(data).hexdigest()  # noqa: S324 - 只當識別碼
            if (seen := self._by_digest.get(digest)) is not None:
                return seen
            self._seq += 1
            filename = f"img-{self._seq:04d}{suffix}"
        name = self._unique(filename)
        try:
            self._prepare()
            dest = self.path / name
            # 消毒過仍要驗:symlink、超長路徑被截斷等都可能讓實際落點跑掉
            if not dest.resolve().is_relative_to(self.path.resolve()):
                raise OSError(f"資產路徑逸出 assets 目錄:{dest}")
            dest.write_bytes(data)
        except UserFacingError:
            raise
        except OSError:
            logger.exception("資產寫入失敗,略過這一份:%s", name)
            self.failures += 1
            return ""
        link = asset_link(self.name, name)
        # 只記真的落地成功的:寫失敗時記進去,後面同樣的圖會拿到一個
        # 指向不存在檔案的連結,而 failures 只算了一次
        if not explicit_name:
            self._by_digest[digest] = link
        return link

    def failure_note(self) -> list[Note]:
        """寫不進去的圖片/附件要留下痕跡,由 docpipe 在 render 前補上。

        少一張圖不該讓整份文件轉不出來(所以 add_bytes 只回空字串),
        但**也不能無聲**——那正是本功能宣稱最不能接受的事。"""
        if not self.failures:
            return []
        return [Note(
            f"有 {self.failures} 個圖片或附件無法寫入「{self.name}」資料夾"
            "(可能是資料夾唯讀、磁碟已滿或路徑過長),那些內容沒有保留",
            KIND_ASSET_FAILED,
        )]


# ---- 落檔 ----

# --out-dir 檔名雜湊的長度。8 個十六進位字(32 bit)在知識庫規模的快取裡
# 碰撞機率可以忽略,再長只是讓檔名更難讀
_OUT_DIR_HASH_CHARS = 8


def _content_tag(src: Path) -> str:
    """來源內容的短雜湊,給 `--out-dir` 模式的檔名去重。

    讀不到檔(權限、剛好被移走)時退回用絕對路徑算:這一步跑在轉檔**之前**,
    在這裡拋例外會讓整批陣亡,而真正的失敗自然會在該檔自己的轉檔步驟被記錄
    下來、只影響它一個。"""
    h = hashlib.sha256()
    try:
        with src.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        logger.debug("讀不到內容算雜湊,改用路徑:%s", src, exc_info=True)
        h.update(str(src.resolve()).lower().encode("utf-8", "replace"))
    return h.hexdigest()[:_OUT_DIR_HASH_CHARS]


def md_path_for(src: Path, out_dir: Path | None = None) -> Path:
    """來源檔 → 它的 md 目標路徑(不管那個檔存不存在)。

    **路徑規則的單一出處**。`target_md_path` 存在時回 None 表示跳過,所以
    「這份檔的 md 會叫什麼」問不到它——呼叫端只好自己再算一次,而那份複製
    品漏掉 `out_dir` 就會安靜地指到錯的地方(實際發生過:doccli 回報「已轉
    過的檔」時算成原檔旁邊,`--out-dir` 模式下重跑就回一片空白)。

    `out_dir` 模式的檔名帶一段**來源內容的短雜湊**(`簡報-3f9a2b71.md`,
    使用者 2026-08-01 選定)。集中輸出把所有來源攤平成一層,而「簡報.pdf」
    「會議紀錄.pdf」這種檔名在一個知識庫裡重複是必然的——不區分的話第二份
    會撞上第一份、被「同名 md 已存在就跳過」規則擋掉,而呼叫端拿回去的是
    **完全不相干的另一份文件**。用內容而不是路徑當雜湊來源有兩個好處:攝入
    流程把原檔 `mv` 到別的資料夾之後仍然對得上,而且**來源被改過雜湊就變、
    自動重轉**,不會安靜地拿到舊的 md(原檔旁邊的預設模式沒有這個性質——
    那裡是使用者拍板的「同名就跳過」,代價寫在 target_md_path)。代價是改版
    後的舊 md 會留在快取裡變孤兒,整個快取目錄刪掉重來即可。"""
    if out_dir is None:
        return src.with_suffix(".md")
    return Path(out_dir) / f"{src.stem}-{_content_tag(src)}.md"


def target_md_path(
    src: Path, claimed: set[Path] | None = None, out_dir: Path | None = None,
) -> tuple[Path | None, str]:
    """決定輸出的 .md 路徑。**已經有同名 .md 就回 (None, 原因) 表示跳過**。

    使用者 2026-08-01 指定:同名的 .md 存在就不必再轉一次。這服務的是
    「資料夾裡陸續加新檔案、重跑批次只想處理新的」這個常見用法,也讓
    「不小心對同一個資料夾按兩次」不會白花時間。

    **代價**:程式改版後想重轉,得先自己把舊的 .md 刪掉——這一點寫在
    README 與使用說明,不能只留在程式碼裡。
    (先前的規則是「本工具產生的覆寫、別人的改名成 `(轉檔).md`」,
    2026-08-01 依使用者指示簡化成一律跳過;改名與 `_reads_as_ours`
    的比對邏輯隨之移除,實作見 git 歷史。)

    `claimed` 是本批次已經預定的輸出路徑:同一個資料夾裡的 `報表.xlsx`
    與 `報表.pdf` 都想寫 `報表.md`,而規劃是在**動手之前**做的、那時
    第一個還沒落地,不記帳就會兩個都以為自己可以寫。

    `out_dir` 把整批產出集中到別處(命令列的 `--out-dir`,GUI 不給這個
    選項)。用途是「不想在別人的文件資料夾裡留下 .md 與 .assets」。
    **集中輸出會讓同名衝突變得很常見**(不同資料夾的兩份「報表.pdf」),
    但那正好由上面的 `claimed` 記帳擋下來,不必另寫一套。"""
    taken = claimed or set()
    dest = md_path_for(src, out_dir)
    if dest in taken:
        return None, f"這一批已經有另一個檔案要輸出成「{dest.name}」"
    # **來源就是目標**:.md 檔在預設模式下必然如此(輸出擺在原檔旁邊)。
    # 這時走下面那句會說成「已經存在,要重轉請先刪掉」——聽起來像轉過了,
    # 而使用者刪掉的會是自己的原始檔。要 md 真的被處理請用 `--out-dir`
    # (知識庫攝入正是那個用法)
    if dest == src:
        return None, "本身就是 markdown,不需要轉換"
    if dest.exists():
        return None, f"「{dest.name}」已經存在(要重轉請先把它刪掉)"
    return dest, ""


def write_md(text: str, dest: Path) -> Path:
    """寫出 md:暫名 + 原子改名。

    批次跑到一半當機/斷電時,`.part` 是垃圾但無害;直接寫的話會留下半份
    md 蓋掉上一次的完整成果(而使用者不會知道)。同 models.download 的做法。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(dest)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise UserFacingError(
            f"無法寫出檔案「{dest.name}」:請確認資料夾沒有唯讀、磁碟空間足夠,"
            f"且檔案沒有被其他程式開啟({dest.parent})"
        ) from e
    return dest
