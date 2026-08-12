r"""文件轉檔的來源把關與挑選(多檔 / 整個資料夾)。

與 `srcfile.py` 的分工:srcfile 服務「聲音→MD」分頁,本模組服務
「文字、圖像→MD」分頁與命令列。兩者共用 `srcfile.native_dialog` 與
`srcfile.clean_path`,格式白名單則是**單向的超集關係**(2026-08-06):
本模組吃文件 + 錄音錄影,srcfile 只吃錄音錄影。**反過來絕對不行**——
「聲音→MD」那顆「開始轉檔」一旦接受 PDF,使用者會在一個滿是講者命名、
試聽、聲紋的介面裡選了一份試算表。

兩個分頁對**錄音**的行為刻意一致(都走 docaudio 那條批次路徑,標
「講者 N」、不做命名):同一個檔不該因為你把它拖進哪個分頁而得到不一樣的
逐字稿。差別只在「聲音→MD」多了**單檔**那條路(講者命名 + 試聽 + 下載)。

格式分成三組(原生文字 / 需要 OCR / 需要 LibreOffice 或郵件解析),
2026-08-01 起三組都已開通。分組保留下來是為了讓「這個格式為什麼
需要那個相依」一目了然,也方便日後要分階段上下線。

不依賴 gradio(同 srcfile):錯誤一律 UserFacingError,由 app 層翻成 gr.Error。
"""
import logging
import os
import re
import urllib.parse
from pathlib import Path

from meeting_scribe import docmd, srcfile
from meeting_scribe.errors import UserFacingError

logger = logging.getLogger(__name__)

# 階段一:原生文字格式(純 Python 解析,不需要任何模型或外部程式)
NATIVE_TYPES = [
    ".docx", ".pptx", ".xlsx", ".xlsm", ".docm", ".pptm",
    ".csv", ".txt", ".md", ".rtf",
    ".html", ".htm", ".mht", ".mhtml", ".epub", ".pdf",
]
# 階段二:需要 OCR(掃描頁與影像檔)
OCR_TYPES = [
    ".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".gif", ".webp", ".heic",
]
# 階段三:需要 LibreOffice 升級的舊格式,與郵件
LEGACY_TYPES = [
    ".doc", ".ppt", ".xls",
    ".odt", ".ods", ".odp",   # OpenDocument
    ".vsd", ".vsdx",          # Visio(轉成 PDF 再走 docpdf)
    ".msg", ".eml",
]

# 文件類(= 不含錄音錄影)。**這一份的意義在於「聲音→MD 分頁不得碰的東西」**
# ——見 test_audio_tab_never_accepts_documents
DOCUMENT_TYPES = NATIVE_TYPES + OCR_TYPES + LEGACY_TYPES

# 錄音/影片:與文件走同一條批次路徑(docaudio),使用者 2026-08-06 指定
# 「文字、圖像→MD」也收。**清單的唯一出處在 srcfile**,這裡只是引用:
# 兩邊各寫一份的話,會出現「一個分頁收得下、另一個說不支援」的各說各話
AUDIO_TYPES = list(srcfile.SUPPORTED_TYPES)

# 目前實際開通的格式(單一出處:驅動副檔名白名單、選檔對話框的篩選器、
# 給使用者看的格式清單。手寫第二份必定漏改)
SUPPORTED_TYPES = DOCUMENT_TYPES + AUDIO_TYPES

# **只有在命令列上「明確指名」才轉的格式**(2026-08-02 定為介面不提供,
# 2026-08-04 再收緊為資料夾展開也不收)。兩條理由各自獨立:
#
# 1. **介面不提供**:`.md` 在介面模式下必然跳過——輸出擺在原始檔旁邊,
#    目標就是它自己——所以列進選檔對話框只會讓人選了之後看到「跳過」,
#    像壞掉。
# 2. **資料夾展開不收**(expand_folder,**不管呼叫端傳什麼 types**):
#    整棵知識庫的來源樹指過去做 `--out-dir` 攝入時,裡面幾百個既有的 md
#    (網頁剪報、逐字稿)會一起被掃進快取,而它們的產出只是「原樣副本 +
#    標題降一級」,沒有任何轉檔價值。原本只能靠呼叫端自己先 `--dry-run`
#    看一遍再排除,現在由工具本身擋掉。**規則放在 expand_folder 裡面而
#    不是改 doccli 傳進來的值**:那樣下一個呼叫端會再踩一次。
#
# 引擎照樣支援(路由表有它):`doc2md 某檔.md --out-dir <快取>` 仍然會轉
# ——`--out-dir` 才是 md 真正的用途(把 md 連同插圖搬進快取,順便靠
# read_text_auto 救 cp950 / UTF-16 的檔)。指名與展開的差別在
# validate_batch:is_file 分支照舊吃 CLI_ONLY_TYPES。
CLI_ONLY_TYPES = [".md"]
# 介面(選檔對話框、格式提示)認得的格式
GUI_TYPES = [t for t in SUPPORTED_TYPES if t not in CLI_ONLY_TYPES]

# ⚠️ **兩份白名單的關係是「超集」不是「相等」,而且方向只有一邊**:
# 「文字、圖像→MD」與命令列吃 SUPPORTED_TYPES(文件 + 音訊),「聲音→MD」
# 只吃 srcfile.SUPPORTED_TYPES(音訊)。**反過來絕對不行**——那顆「開始
# 轉檔」一旦接受 PDF,使用者會在一個滿是講者命名、試聽、聲紋的介面裡選了
# 一份試算表(測試 test_audio_tab_never_accepts_documents 守著這個方向)。

# (三個階段都開通後,「還在開發中」的專屬訊息機制就沒有對象了。
#  日後若又要分階段上線,把該階段的清單從 SUPPORTED_TYPES 拿掉、
#  在 validate_batch 補一條專屬訊息即可——籠統的「不支援」會讓使用者
#  以為這工具永遠不吃這種檔。)

# 會「引用圖片」的檔案類型:掃它們才認得出哪些圖是別人的插圖(見
# _drop_referenced_images)。mht/mhtml 不在列——它們把資源內嵌在檔案裡,
# 沒有對外的相對路徑。.md 在列是為了保護「文件旁邊就是它的 images/」
# 這種既有成品(例如知識庫的頁面與插圖)
_REFERRING_SUFFIXES = {".html", ".htm", ".md", ".markdown"}
# 單一檔案最多讀這麼多來找引用。這段路徑在 UI 上每改一次路徑欄就會跑,
# 不能讓它被一個超大 HTML 拖住
_REF_SCAN_MAX_BYTES = 4 * 1024 * 1024
# HTML 的 src=/srcset=/href=、Markdown 的 ](路徑),以及**行內程式碼裡的
# 路徑**(`images/某書/圖.jpg`)——最後這種在正文提及插圖時很常見,實測一個
# 知識庫群組裡 310 張圖只有它一張漏網。刻意不動用 bs4:這裡只要「有沒有
# 指到這個檔」、不需要正確的 DOM,而 bs4 會讓選檔那一刻多付數百毫秒
# (docsrc 至今不依賴任何解析器)
#
# **href 必須認**:SVG 的 `<image href="…">`(Calibre 電子書的分篇扉頁)
# 就用它。副作用是 `<a href="photo.jpg">` 這種「HTML 連到圖」也會被當成
# 插圖剔出批次——對「文件+插圖」是對的,對「相簿 index.html + 一堆照片」
# 則是照片只透過 html 進來、不再各自成 md。與現有 `<img src>` 縮圖的行為
# 一致,使用者 2026-08-02 評估後接受
_IMG_REF_RE = re.compile(
    r"""(?:src|srcset|(?:xlink:)?href)\s*=\s*["']([^"']+)["']"""
    r"""|\]\(\s*<?([^)>\s]+)"""
    r"""|`([^`
]+)`""",
    re.IGNORECASE,
)

# 批次上限:使用者可能不小心選到整個 D:\ 或使用者家目錄。一次跑數千個檔
# 只會讓人以為當掉(而且中途停止會留下一半成品)。上限不是效能考量,是
# 「讓人看得懂發生什麼事」
MAX_BATCH_FILES = 500

# 展開資料夾時要跳過的目錄名尾綴:本工具自己產生的圖片資料夾。不跳過的話
# 重轉同一個資料夾會把上次抽出來的圖片當成新的來源檔(每轉一次就多一批
# 由圖片產生的 md)。**取自 docmd 的同一個常數**——命名慣例改了卻只改
# 一處的話,這道防線會安靜失效
_ASSETS_SUFFIX = docmd.ASSETS_SUFFIX


def supported_hint() -> str:
    """**介面**上給使用者看的格式清單(「docx / pptx / xlsx …」,含錄音錄影)。"""
    return srcfile.format_hint(GUI_TYPES)


def cli_supported_hint() -> str:
    """**命令列**的格式清單:比介面多 `.md`(只在明確指名時才轉,見
    CLI_ONLY_TYPES)。音訊/影片則兩邊都吃(見 AUDIO_TYPES)。"""
    return srcfile.format_hint(SUPPORTED_TYPES)


def clean_paths(text) -> list[str]:
    """多行路徑文字 → 路徑清單(實作在 srcfile,兩個分頁共用同一份)。"""
    return srcfile.clean_paths(text)


def expand_folder(
    folder: Path, recursive: bool = True, types: list[str] | None = None,
) -> list[Path]:
    """資料夾 → 其中所有「支援格式」的檔案(排序後回傳)。

    `types` **預設是介面那一份**(GUI_TYPES):忘了指定時退回較保守的行為。

    **`CLI_ONLY_TYPES` 一律不收,不管呼叫端傳什麼進來**:那些格式(目前
    只有 `.md`)要轉必須在命令列上明確指名。把規則寫在這裡而不是讓每個
    呼叫端自己過濾,是因為漏掉的那個呼叫端不會報錯——只會安靜地多轉幾百
    個「原樣副本」出來(見 CLI_ONLY_TYPES 的第 2 點)。

    排序讓批次順序可預測(使用者看進度時能對上檔案總管的順序,中斷後也
    知道做到哪)。跳過:本工具的 .assets 目錄、`.` 開頭的隱藏目錄
    (.git/.venv 之類,裡面的東西不是使用者想轉的文件)。"""
    types = GUI_TYPES if types is None else types
    scan_types = [t for t in types if t not in CLI_ONLY_TYPES]
    files: list[Path] = []
    referrers: list[Path] = []
    cli_only = 0
    for root, dirnames, filenames in os.walk(folder):
        # **就地剪枝**,不是走進去再把結果濾掉:`.assets` 正是這個功能
        # 自己倒圖片的地方(一份 300 頁 PDF 就有近 900 個檔),而主要
        # 用法就是「資料夾陸續加新檔、重跑批次」——不剪枝的話第二次
        # 跑要多 stat 一個數量級的檔案
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") and not d.lower().endswith(_ASSETS_SUFFIX)
        ]
        for name in filenames:
            # `~$報表.docx` 是 Word/Excel 開檔時建的**擁有者暫存檔**(百來
            # bytes 的二進位樁),不是文件——不跳過的話它會進批次、轉檔失敗,
            # 在報告裡冒出一筆看不懂的錯(2026-08-03 全碟稽核實際遇到)
            if name.startswith("~$"):
                continue
            suffix = Path(name).suffix.lower()
            path = Path(root) / name
            if suffix in scan_types:
                files.append(path)
            elif suffix in CLI_ONLY_TYPES and suffix in types:
                # 呼叫端明講吃這個格式、但展開仍不收——留一行紀錄,
                # 「工具幫你少做了幾百個檔」不該完全查不到痕跡
                cli_only += 1
            # `_REFERRING_SUFFIXES` 與 `types` **各自獨立**收集:md 不進
            # 批次,但它仍然是「會引用插圖的文件」,少了這一份
            # _drop_referenced_images 就保護不到「md 旁邊就是它的 images/」
            if suffix in _REFERRING_SUFFIXES:
                referrers.append(path)
        if not recursive:
            break
    if cli_only:
        logger.info(
            "資料夾展開略過 %d 個 %s(要轉請在命令列上直接指名檔案)",
            cli_only, "/".join(CLI_ONLY_TYPES),
        )
    return sorted(
        _drop_referenced_images(files, referrers), key=lambda x: str(x).lower(),
    )


def _refs_in(path: Path) -> list[str]:
    """一份 HTML/Markdown 裡引用到的相對路徑(原樣,尚未解析)。"""
    try:
        raw = path.read_bytes()[:_REF_SCAN_MAX_BYTES]
    except OSError:
        logger.debug("讀不到引用來源:%s", path, exc_info=True)
        return []
    # 只要抓得到路徑就夠,不必正確還原全文:UTF-8 是壓倒性的常態,
    # 而非 UTF-8 的檔案裡,URL 編碼過的路徑(%E7%AC%AC…)本來就是 ASCII
    text = raw.decode("utf-8", "ignore")
    return [g for m in _IMG_REF_RE.finditer(text) for g in m.groups() if g]


def _drop_referenced_images(
    files: list[Path], referrers: list[Path],
) -> list[Path]:
    r"""把「被同一棵樹裡的 HTML/Markdown 引用到的圖片」從批次裡拿掉。

    那些圖是**那份文件的插圖**,不是獨立的來源檔。整包丟一本電子書
    (一個目錄下幾十份 HTML + images/)進來的話,每張插圖都會另外產一份
    只有 OCR 文字的 md,而且同一張圖被 OCR **兩次**(一次隨 HTML 轉、
    一次當成獨立影像)——200 張圖就是十幾分鐘純浪費,外加 200 個垃圾檔
    (使用者 2026-08-02 回報)。「另存為完整網頁」產生的
    `某網頁.html` + `某網頁_files\` 是同一回事,而那個常見得多。

    判準與「跳過 .assets 目錄」是同一個:那是別人的素材,不是待轉的文件。

    **只在批次裡同時有圖片與引用來源時才做**:否則一整批 PDF 也要為此
    讀檔,而選檔那一刻(UI 每改一次路徑欄)就會慢下來。"""
    images = {p for p in files if p.suffix.lower() in OCR_TYPES}
    if not images or not referrers:
        return files
    by_resolved = {}
    for img in images:
        try:
            by_resolved[img.resolve()] = img
        except OSError:  # pragma: no cover - 斷掉的網路磁碟
            continue
    referenced: set[Path] = set()
    for src in referrers:
        base = src.parent
        for ref in _refs_in(src):
            if re.match(r"^[a-zA-Z][\w+.-]*:", ref):  # http: / data: 等,不是本機檔
                continue
            target = Path(urllib.parse.unquote(ref.split("#")[0].split("?")[0]))
            if target.suffix.lower() not in OCR_TYPES:
                continue
            try:
                hit = by_resolved.get((base / target).resolve())
            except OSError:  # pragma: no cover
                continue
            if hit is not None:
                referenced.add(hit)
    if referenced:
        logger.info("略過 %d 張被 HTML/Markdown 引用的插圖", len(referenced))
    return [p for p in files if p not in referenced]


def validate_batch(
    text, recursive: bool = True, types: list[str] | None = None,
    *, what: str = "文件", hint: str | None = None,
) -> tuple[list[Path], list[tuple[Path, str]]]:
    """路徑文字 → (可轉換的檔案清單, 略過的 (路徑, 原因))。

    資料夾會就地展開。整批一個都不可用時才拋 UserFacingError——只要有
    一個能轉就照跑,略過的在批次摘要裡點名,不打斷使用者。

    `types` 同 expand_folder:**預設是介面那一份**。但兩條分支對
    `CLI_ONLY_TYPES` 的態度**刻意不一樣**:資料夾展開一律不收(見
    expand_folder),`is_file` 分支則照舊吃——「明確指名就轉」正是
    `doc2md 某檔.md --out-dir <快取>` 這個用法的全部依據。

    `what`/`hint` 讓「聲音→MD」共用這支(它的批次模式規則一模一樣)。
    **hint 一定要跟著換**:寫死文件那份的話,音訊分頁選錯檔案會被告知
    「支援的格式:docx / pptx / …」,而那份清單它一個都不收。"""
    types = GUI_TYPES if types is None else types
    raw = clean_paths(text)
    if not raw:
        raise UserFacingError(
            f"請先按「選擇檔案…」或「選擇資料夾…」挑選要轉換的{what},"
            "或直接貼上路徑(一行一個)"
        )
    files: list[Path] = []
    skipped: list[tuple[Path, str]] = []
    for item in raw:
        p = Path(item)
        try:
            is_dir, is_file = p.is_dir(), p.is_file()
        except OSError:
            is_dir = is_file = False
        if is_dir:
            found = expand_folder(p, recursive, types)
            if not found:
                skipped.append((p, "這個資料夾裡沒有可以轉換的檔案"))
            files.extend(found)
        elif is_file:
            ext = p.suffix.lower()
            if ext in types:
                files.append(p)
            else:
                skipped.append((p, f"不支援的格式「{ext or '(無副檔名)'}」"))
        else:
            skipped.append((p, "找不到這個檔案或資料夾"))

    # 去重但保序:使用者同時選了資料夾與其中某個檔案時不該轉兩次
    seen: set[str] = set()
    unique: list[Path] = []
    for p in files:
        key = str(p.resolve()).lower()
        if key not in seen:
            seen.add(key)
            unique.append(p)

    if not unique:
        reasons = "、".join(dict.fromkeys(r for _, r in skipped)) or "沒有可以轉換的檔案"
        raise UserFacingError(
            f"沒有可以轉換的檔案:{reasons}。"
            f"支援的格式:{hint or supported_hint()}"
        )
    if len(unique) > MAX_BATCH_FILES:
        raise UserFacingError(
            f"一次選了 {len(unique)} 個檔案,超過上限 {MAX_BATCH_FILES} 個。"
            "請分批處理,或改選範圍小一點的資料夾(也可以取消「包含子資料夾」)"
        )
    return unique, skipped


def summarize(files: list[Path], skipped: list[tuple[Path, str]]) -> str:
    """選檔結果的一行摘要(顯示在選檔區下方,轉檔前就讓使用者確認範圍)。"""
    if not files and not skipped:
        return ""
    parts: list[str] = []
    if files:
        counts: dict[str, int] = {}
        for p in files:
            ext = p.suffix.lower().lstrip(".")
            counts[ext] = counts.get(ext, 0) + 1
        detail = "、".join(
            f"{ext} {n}" for ext, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        parts.append(f"已選 **{len(files)} 個檔案**({detail})")
    if skipped:
        parts.append(f"另有 {len(skipped)} 個項目會略過")
    return ";".join(parts) + "。"


def skipped_lines(skipped: list[tuple[Path, str]]) -> list[str]:
    """略過清單 → 給批次報告逐行列出(摘要只給數量,報告要點名到檔案)。"""
    return [f"- 略過「{p.name}」:{reason}" for p, reason in skipped]


# ---- 原生對話框(共用 srcfile 的 withdraw+topmost+防重複開)----

def pick_files() -> str:
    """「選擇檔案…」:**多選**。回傳一行一個路徑的文字;**取消回空字串**。

    取消時要不要保留現有內容,由呼叫端決定——文件分頁是「累加」不是
    「取代」(使用者 2026-08-01 回報:選了第二個來源就把第一個蓋掉,
    等於永遠只能處理一批)。"""
    picked = srcfile.native_dialog(lambda fd, root: fd.askopenfilenames(
        parent=root, title="選擇要轉成 Markdown 的文件(可多選)",
        filetypes=[
            ("可轉換的文件", " ".join("*" + ext for ext in GUI_TYPES)),
            ("所有檔案", "*.*"),
        ],
    ))
    if not picked:
        return ""
    # tkinter 回傳 C:/x/y 正斜線,轉回與 Explorer 一致的反斜線樣式
    return "\n".join(str(Path(p)) for p in picked)


def pick_folder() -> str:
    """「選擇資料夾…」:整個資料夾的文件一次轉完。取消回空字串(同上)。"""
    picked = srcfile.native_dialog(lambda fd, root: fd.askdirectory(
        parent=root, title="選擇要轉換的資料夾",
    ))
    return str(Path(picked)) if picked else ""
