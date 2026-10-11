r"""純文字類來源(txt / csv / rtf)與**編碼自動判斷**。

`read_text_auto` 是全專案唯一的「位元組 → 文字」判斷處:台灣的 csv/txt
很多是 cp950(Big5)而不是 UTF-8,判錯就是整份亂碼,而且亂碼會安靜地進
知識庫、沒有人會發現。判斷順序與「不確定就標註」的政策見該函式。
"""
import csv
import io
import logging
import re
import unicodedata
import urllib.parse

from pathlib import Path

from meeting_scribe import docmd
from meeting_scribe.docmd import AssetsDir, Block, Note, Para, Raw, Table
from meeting_scribe.errors import UserFacingError

logger = logging.getLogger(__name__)

# BOM → 編碼。utf-32 必須排在 utf-16 前面:UTF-32-LE 的 BOM(FF FE 00 00)
# 前兩個位元組正好是 UTF-16-LE 的 BOM,順序反了會把 utf-32 判成 utf-16
_BOMS = [
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
]

# 候選編碼(順序即同分時的偏好):本工具的使用者在台灣,cp950 優先
_CANDIDATES = ["cp950", "gb18030", "cp1252"]

ENCODING_UNSURE = "(不確定)"

# U+FEFF。以 \N{...} 具名寫法而不是貼字面字元——BOM 在編輯器裡是隱形的,
# 貼進原始碼後沒有人看得出這裡有東西
_BOM_CHAR = "\N{ZERO WIDTH NO-BREAK SPACE}"


def _score(text: str) -> float:
    """解碼結果的「像不像正常中文文件」評分(每字元平均分)。

    解錯編碼的徵狀不是拋例外——cp950 與 gb18030 的位元組分佈高度重疊,
    兩邊多半都「解得出東西」,只是其中一邊解出來的是罕用字堆。所以要看
    解出來的字長什麼樣:

    - CJK 基本區(0x4E00-0x9FFF)= 正常中文,加分
    - CJK 擴充 A(0x3400-0x4DBF)= 罕用字,正常辦公文件幾乎不會出現,扣分
    - 私用區 / 控制字元 / U+FFFD = 幾乎確定解錯,重扣
    """
    if not text:
        return 0.0
    score = 0.0
    for ch in text:
        o = ord(ch)
        if 0x4E00 <= o <= 0x9FFF:
            score += 1.0
        elif 0x3400 <= o <= 0x4DBF or o >= 0x20000:
            score -= 1.0
        elif o == 0xFFFD:
            score -= 5.0
        elif 0xE000 <= o <= 0xF8FF:
            score -= 3.0
        elif ch not in "\r\n\t" and unicodedata.category(ch) in ("Cc", "Cn", "Co", "Cs"):
            score -= 3.0
    return score / len(text)


def read_text_auto(data: bytes) -> tuple[str, str]:
    """位元組 → (文字, 用了哪個編碼)。編碼無法確定時第二個值是 ENCODING_UNSURE。

    判斷順序,把「一定對」的放前面、把不確定性壓到最後一步:

    1. **BOM** —— 有 BOM 就是它,零猜測。
    2. **strict UTF-8** —— UTF-8 自帶結構校驗,Big5/GBK 的中文位元組序列
       幾乎不可能碰巧通過,誤判率趨近 0。
    3. **候選評分**(cp950 / gb18030 / cp1252)—— 這一步才有猜測成分,以
       `_score` 挑最像正常中文的。charset_normalizer 只在同分時當裁判,
       **不當第一判準**:它對 cp950 與 gb18030 的區分實測不穩,而這正好是
       台灣使用者最常遇到的那一組。
    4. 全部失敗 → cp950 + errors='replace',回 ENCODING_UNSURE,由呼叫端
       在 md 裡下 Note——寧可讓使用者看到「這份可能亂碼」,也不要安靜地
       把亂碼送進知識庫。
    """
    if not data:
        return "", "utf-8"
    for bom, enc in _BOMS:
        if data.startswith(bom):
            try:
                text = data.decode(enc)
            except UnicodeDecodeError:  # BOM 對但內容壞,繼續往下猜
                break
            # utf-16-le/-be、utf-32-le/-be 這些「指名位元組序」的 codec 不會
            # 自己剝 BOM(只有不帶 -le/-be 的 utf-16/utf-32 與 utf-8-sig 會),
            # 留著會變成內容開頭的一個 U+FEFF 零寬字元——看不見,但會混進
            # 第一個段落、也可能讓下游的字串比對失準
            return text.removeprefix(_BOM_CHAR), enc
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass

    best: tuple[float, str, str] | None = None
    for enc in _CANDIDATES:
        try:
            text = data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        s = _score(text)
        if best is None or s > best[0]:
            best = (s, text, enc)

    if best is None:
        return data.decode("cp950", errors="replace"), ENCODING_UNSURE

    score, text, enc = best
    # 分數太低 = 每個候選解出來都像亂碼,不要假裝有把握
    if score < 0.0:
        hinted = _charset_normalizer_hint(data)
        if hinted:
            try:
                return data.decode(hinted), hinted
            except (UnicodeDecodeError, LookupError):
                pass
        return text, ENCODING_UNSURE
    return text, enc


def _charset_normalizer_hint(data: bytes) -> str:
    """同分/低分時的裁判。失敗一律回空字串——它只是加分項,不是必需品。"""
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(data).best()
        return best.encoding if best else ""
    except Exception:
        logger.debug("charset_normalizer 判斷失敗,改用評分結果", exc_info=True)
        return ""


def encoding_note(enc: str) -> list[Block]:
    """編碼判不準時的標記(docweb 也用同一份——文案只講一次)。"""
    if enc != ENCODING_UNSURE:
        return []
    return [Note(
        "本檔案的文字編碼無法確定,內容可能有亂碼,請對照原始檔案確認",
        docmd.KIND_ENCODING_GUESS,
    )]


def _paragraphs(text: str) -> list[Block]:
    """以空行切段。純文字檔沒有結構資訊,硬猜標題只會猜錯——保持平坦,
    讓下游切塊器依段落處理(同 render 的規則 1:錯的階層比沒有階層更糟)。"""
    blocks: list[Block] = []
    for chunk in text.replace("\r\n", "\n").replace("\r", "\n").split("\n\n"):
        para = chunk.strip()
        if para:
            blocks.append(Para(para))
    return blocks


def convert_txt(src: Path, assets: AssetsDir, ocr_enabled: bool = True) -> list[Block]:
    """純文字檔 → 段落。

    (`assets` 與 `ocr_enabled` 這幾個純文字 reader 都用不到,但簽章要與
    其他 reader 一致——docpipe 的路由表才能一視同仁地呼叫,不必為每個
    格式寫轉接。)"""
    text, enc = read_text_auto(src.read_bytes())
    return encoding_note(enc) + _paragraphs(text)


def _sniff_delimiter(sample: str) -> str:
    """分隔符偵測。csv.Sniffer 對中文內容常誤判(把全形逗號、頓號當分隔),
    所以只在自家的候選集合裡數數量,不交給 Sniffer 自由發揮。"""
    head = "\n".join(sample.splitlines()[:20])
    counts = {d: head.count(d) for d in [",", "\t", ";", "|"]}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] > 0 else ","


def convert_csv(src: Path, assets: AssetsDir, ocr_enabled: bool = True) -> list[Block]:
    """csv/tsv → 表格(欄多就逐筆區塊)。**資料一列都不截斷。**"""
    text, enc = read_text_auto(src.read_bytes())
    blocks: list[Block] = encoding_note(enc)
    if not text.strip():
        blocks.append(Note("這個檔案是空的", docmd.KIND_BLANK_PAGE, lossy=False))
        return blocks
    delim = _sniff_delimiter(text)
    try:
        rows = [row for row in csv.reader(io.StringIO(text, newline=""), delimiter=delim)]
    except csv.Error as e:
        raise UserFacingError(
            f"CSV 格式有問題,無法解析「{src.name}」:{e}"
        ) from e
    rows = [r for r in rows if any((c or "").strip() for c in r)]
    if not rows:
        blocks.append(Note("這個檔案沒有任何資料列", docmd.KIND_BLANK_PAGE, lossy=False))
        return blocks
    # 寬表要不要改逐筆區塊由 docmd.render 決定(渲染政策只有一個出處)
    table = Table(rows, has_header=True, caption=src.stem)
    blocks.append(table)
    # **列數守恆**:「資料一列都不截斷」是硬規則,這裡把它變成會叫的檢查。
    # csv 沒有 Office 那種「某類節點沒被走訪」的風險(它是平的),所以不做
    # 逐段比對;真正要守的是「哪天有人為了記憶體加了個『大檔只取前 N 列』」
    # ——那時這條會當場擋下來,而不是等使用者發現資料少了
    if len(table.rows) != len(rows):  # pragma: no cover - 回歸防線
        blocks.extend(docmd.extraction_gap_note(
            [f"第 {i + 1} 列" for i in range(len(rows) - len(table.rows))], "資料列",
        ))
    return blocks


# 圍籬式程式區塊的界線(``` 或 ~~~)。裡面的 `# 註解` 不是標題
_FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")
_ATX_RE = re.compile(r"^(#{1,6})(\s)")


def _demote_headings(text: str) -> str:
    """把 ATX 標題整體下推一層,**程式區塊裡的不算**。

    輸出的 H1 一律留給文件標題(`docmd.render` 規則 1:兩個 H1 會被切塊器
    當成兩份文件),而來源 md 自己就有 H1。所有 reader 都是這樣處理的
    ——它們以「文件內視角」給 level,render 統一 +1。這裡等於把那個轉換
    直接做在文字上,階層的**相對關係一格都沒變**。

    Shell 的 `# 註解`、Python 的 `#!` 都在圍籬裡,不能碰。"""
    out: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        marker = _FENCE_RE.match(line)
        if marker:
            token = marker.group(1)
            fence = None if fence == token else (fence or token)
            out.append(line)
            continue
        out.append(line if fence else _ATX_RE.sub(r"#\1\2", line))
    return "\n".join(out)


# md 裡的圖片有兩種寫法:`![說明](路徑)` 與直接寫 HTML 的 `<img src="路徑">`
_MD_IMG_RE = re.compile(r"""(!\[[^\]]*\]\(\s*)([^)\s]+)""")
_MD_HTML_IMG_RE = re.compile(r"""(<img\b[^>]*?\bsrc\s*=\s*["'])([^"']+)""", re.I)
# 有協定的(http:/data:/file:)與絕對路徑都不是「旁邊的檔案」,不要碰
_ABSOLUTE_RE = re.compile(r"^([a-zA-Z][\w+.-]*:|/|\\\\|[a-zA-Z]:[\\/])")


def _copy_local_images(text: str, src: Path, assets: AssetsDir) -> tuple[str, int]:
    """把 md 引用到的**本機相對圖片**搬進 assets 並改寫連結,回傳 (新文字, 失敗數)。

    **非做不可**:`--out-dir` 會把 md 搬到別的資料夾,而旁邊的 `images/`
    不會跟著走——原樣保留連結的話,一份知識庫頁面搬進快取之後圖片全斷,
    而且 `lossy` 還是 0(使用者 2026-08-02 問到這個形狀時實測出來的)。
    每個 reader 都讓輸出自包含,md 沒有理由例外。

    **刻意不對這些圖跑 OCR**:md 本來就是文字,圖是它的插圖;塞一段
    「以下文字由…辨識而來」進去就不是「原樣搬過去」了(使用者 2026-08-02
    指定的正是原樣)。

    搬不動的(檔案不在、讀不到)保留原連結並回報數量,由呼叫端下標記
    ——連結斷掉是實打實的失真,不能安靜。"""
    failed = 0

    def _one(prefix: str, target: str) -> str:
        nonlocal failed
        if _ABSOLUTE_RE.match(target):
            return prefix + target
        rel = urllib.parse.unquote(target.split("#")[0].split("?")[0])
        source = (src.parent / rel).resolve()
        try:
            data = source.read_bytes()
        except OSError:
            failed += 1
            logger.info("md 引用的圖片找不到:%s", target)
            return prefix + target
        link = assets.add_bytes(data, source.suffix or ".png")
        return prefix + (link or target)

    text = _MD_IMG_RE.sub(lambda m: _one(m.group(1), m.group(2)), text)
    text = _MD_HTML_IMG_RE.sub(lambda m: _one(m.group(1), m.group(2)), text)
    return text, failed


def convert_markdown(src: Path, assets: AssetsDir, ocr_enabled: bool = True) -> list[Block]:
    """.md → **原樣搬過去**(使用者 2026-08-02 指定)。

    來源已經是 markdown,再解析一次只會把它重排壞。所以用 `Raw` 整份帶過:
    表格、程式區塊、連結、圖片全部逐字保留。

    **刻意不做簡轉繁**:`Raw` 不經 `docpipe.traditionalize`,而那正是這裡
    要的——markdown 的連結與圖片路徑就寫在正文裡,轉了會把
    `[標題](报表.png)` 變成指向不存在的 `報表.png`(同 `Image.rel_path`
    那條規則)。代價是簡體的 md 來源不會被繁化;那是「原樣」的一部分。

    唯一的改動是標題下推一層,見 `_demote_headings`。

    ⚠️ **預設模式下這條路等於不做事**:輸出目標就是來源檔自己,而
    `docmd.target_md_path` 的規則是「同名 .md 已存在就整份跳過」——所以
    絕不會覆寫掉使用者的原始檔。真正會動作的是 `--out-dir`(知識庫攝入
    正是那個用法)。"""
    text, enc = read_text_auto(src.read_bytes())
    body, lost = _copy_local_images(_demote_headings(text), src, assets)
    blocks: list[Block] = encoding_note(enc)
    if lost:
        blocks.append(Note(
            f"這份 markdown 引用了 {lost} 張找不到的圖片,連結原樣保留",
            docmd.KIND_ASSET_FAILED,
        ))
    return blocks + [Raw(body.strip())]


def convert_rtf(src: Path, assets: AssetsDir, ocr_enabled: bool = True) -> list[Block]:
    """rtf → 純文字。

    階段一只做文字擷取:striprtf 拿不到圖片與版面。階段三 LibreOffice
    就緒後,rtf 改走「升級成 docx」的高保真路徑,這條降為 LibreOffice
    不可用時的退路——所以這裡一定要誠實標註少了什麼。"""
    from striprtf.striprtf import rtf_to_text

    raw, enc = read_text_auto(src.read_bytes())
    try:
        text = rtf_to_text(raw, errors="ignore")
    except Exception as e:
        raise UserFacingError(
            f"無法解析 RTF 檔「{src.name}」:檔案可能已損壞"
        ) from e
    blocks: list[Block] = encoding_note(enc)
    blocks.append(Note(
        "RTF 只擷取了文字,圖片與版面沒有保留", docmd.KIND_IMAGE_ONLY,
    ))
    blocks.extend(_paragraphs(text))
    return blocks
