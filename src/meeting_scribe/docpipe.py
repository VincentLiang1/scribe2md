r"""文件轉 Markdown 的批次總管:路由、繁化、落檔、逐檔容錯、進度、報告。

對照 `pipeline.run_pipeline`(逐字稿那條)的角色,但差異很大:那條是「一個
檔案跑一條長管線」,這條是「一批檔案各跑一條短管線」,所以容錯與進度都
是以「檔」為單位。

本模組**不認識 gradio**(同 pipeline):進度只透過 `StageFn` 回呼,由 app
層翻成 `gr.Progress`。
"""
import logging
import tempfile
from collections.abc import Callable
from typing import NamedTuple
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from meeting_scribe import (
    cancel,
    convert,
    docaudio,
    docimage,
    docmail,
    docmd,
    docoffice,
    docpdf,
    doctext,
    docweb,
    ocr,
    pipeline,
    power,
    soffice,
)
from meeting_scribe.docmd import AssetsDir, Block, DocMeta, Records, Table
from meeting_scribe.errors import UserFacingError

logger = logging.getLogger(__name__)

# 同 pipeline.StageFn:(階段描述, 0~1 的整體進度)
StageFn = Callable[[str, float], None]

# OLE 複合檔的簽章:.doc/.ppt/.xls 都是這個開頭。新格式(OOXML)是 ZIP
_OLE_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")
# 副檔名被改過時,對應到真正該走的舊格式
_MISLABELLED = {".docx": ".doc", ".pptx": ".ppt", ".xlsx": ".xls", ".xlsm": ".xls"}


def _actually_legacy(src: Path) -> str | None:
    """副檔名寫著新格式、內容其實是舊格式時,回傳真正的副檔名。

    公司文件裡「把 .xls 直接改名成 .xlsx」很常見(通常是為了通過某個只認
    副檔名的上傳檢查)。不認的話使用者只會看到「檔案可能已損壞」——而檔案
    好得很,只是我們用錯讀取器(全碟稽核 2026-08-02:233 個讀不了的檔裡
    有一批是這種)。判斷靠**內容的 magic bytes**,不靠副檔名。"""
    target = _MISLABELLED.get(src.suffix.lower())
    if target is None:
        return None
    try:
        with src.open("rb") as f:
            return target if f.read(8) == _OLE_MAGIC else None
    except OSError:
        return None


def _is_encrypted_office(src: Path) -> bool:
    """這份 Office 檔是不是設了開啟密碼?

    加密的 OOXML/OLE 外層仍是合法的 OLE 複合檔,內容整包塞在
    `EncryptedPackage` 串流裡——所以 openpyxl 與 LibreOffice 都會失敗,
    但失敗理由完全不同於「檔案損壞」。**分清楚很重要**:一個叫使用者
    去輸密碼,另一個叫他去找備份(2026-08-03 全量稽核主要工作目錄:
    85 個打不開的 xlsx 裡有 77 個是加密的薪資/獎金檔)。"""
    try:
        import olefile

        if not olefile.isOleFile(str(src)):
            return False
        with olefile.OleFileIO(str(src)) as ole:
            return any(
                "EncryptedPackage" in part or "EncryptionInfo" in part
                for entry in ole.listdir() for part in entry
            )
    except Exception:  # pragma: no cover - 讀不到就當作不是加密,走原本的訊息
        logger.debug("判斷 %s 是否加密時失敗", src, exc_info=True)
        return False


def _convert_legacy(
    src: Path, assets: AssetsDir, ocr_enabled: bool = True,
    *, allow_install: bool = True, source_ext: str | None = None,
) -> list[Block]:
    """LibreOffice 轉得動的格式(.doc/.ppt/.xls/.odt/.ods/.odp/.vsd)
    → **先轉成我們讀得懂的格式,再走同一條路**。

    升級器只有 soffice.upgrade 一份;升級完就交給既有的 docoffice reader,
    閱讀順序、失真標註那些都不必重寫一遍。升級產物落在暫存目錄,
    使用者資料夾裡只會出現最後的 .md(與 .assets)。

    **`ignore_cleanup_errors` 是必要的**:soffice 是另一個行程,它退出後
    Windows 有時還按著剛寫出來的檔案不放,清暫存目錄就吃 WinError 32。
    那發生在**轉檔已經成功之後**——不吞掉的話,一份轉得好好的 .doc 會在
    最後一刻炸成「未預期的錯誤」(2026-08-02 抽樣掃描實際踩到)。殘留的
    目錄帶 TMP_PREFIX,下次啟動的 cleanup_stale_temp 會掃掉。"""
    if _is_encrypted_office(src):
        raise UserFacingError(
            f"「{src.name}」有開啟密碼,請先用 Office 開啟後另存一份沒有密碼的再轉換"
        )
    with tempfile.TemporaryDirectory(
        prefix=pipeline.TMP_PREFIX + "lo-", ignore_cleanup_errors=True,
    ) as tmp:
        upgraded = soffice.upgrade(
            src, Path(tmp), allow_install=allow_install, source_ext=source_ext,
        )
        route = _ROUTES.get(upgraded.suffix.lower())
        if route is None:  # pragma: no cover - _FILTERS 與 _ROUTES 對不上才會發生
            raise UserFacingError(f"升級後的格式無法解析:{upgraded.name}")
        blocks = list(route.read(upgraded, assets, ocr_enabled=ocr_enabled))
    # 提示要說出**實際的**來源與目標:這條路現在也走 ODF 與 Visio,
    # 寫死「舊版 Office」會讓一份 .odt 的說明變成假的
    return [docmd.Note(
        f"這是 {(source_ext or src.suffix).lstrip('.')} 格式,已先用 LibreOffice 轉成 "
        f"{upgraded.suffix.lstrip('.')} 再解析,版面可能與原檔略有出入",
        docmd.KIND_LEGACY_UPGRADE, lossy=False,
    ), *blocks]


def _convert_rtf(src: Path, assets: AssetsDir, ocr_enabled: bool = True) -> list[Block]:
    """rtf 優先走 LibreOffice(保得住圖片與版面),不可用時退回純文字擷取。

    兩條路的差別很大,所以退回時 doctext.convert_rtf 會自己標註
    「只擷取了文字」——使用者要看得出這一份的品質為什麼比較差。

    **`allow_install=False` 不可少**:路由表給 .rtf 的 `ensure` 是空的
    (刻意不預熱 LibreOffice),但那只擋掉預熱——真的轉檔時 `ensure_ready`
    照樣會去下載安裝 320MB。使用者只是轉了一份看起來很普通的 .rtf,
    不該換來一次沒預期的大下載,何況我們本來就有退路。"""
    try:
        return _convert_legacy(src, assets, ocr_enabled, allow_install=False)
    except UserFacingError as e:
        logger.info("LibreOffice 不可用(%s),rtf 改用純文字擷取", e)
        return doctext.convert_rtf(src, assets, ocr_enabled)


class Route(NamedTuple):
    """一種格式要「怎麼讀」與「怎麼預熱」。

    兩件事綁在一起是刻意的:分成兩張以副檔名為鍵的表時,新增格式
    漏改第二張**不會有任何症狀**——只是安靜地退回「跑到第 47 個檔
    才發現環境缺元件」,而那正是預熱機制存在的理由。(實際發生過:
    階段二加了 9 種影像格式,預熱名單完全沒跟上。)"""
    read: Callable[..., list[Block]]
    ensure: Callable[[], object] | None = None
    # 「檔案內部」的進度怎麼交給 reader:收一個 0~1 的回報函式,回傳要
    # 併進 reader 呼叫的具名參數。**多數格式沒有**——一個檔就是一個單位,
    # 硬套只會讓進度條在 0 與 1 之間跳。有的格式逐頁(PDF 吃
    # `on_page(已完成, 總數)`)、有的逐階段(音訊吃 `on_inner(比例)`),
    # 差別包在這裡:convert_file 只管「這條路由有沒有」,不必為每個
    # reader 各寫一條 `is` 判斷(第二個特例就是該把機制長出來的時候)
    progress: Callable[[Callable[[float], None]], dict] | None = None
    # 這條路由額外要吃的執行選項(音訊:模型與講者人數)。那些是使用者
    # 在介面上選的、不是格式的性質,所以由 convert_batch 一路傳進來
    wants_options: bool = False


# 副檔名 → Route。所有 reader 的簽章都是 (src, assets, ocr_enabled),
# 路由才能一視同仁地呼叫;純文字那幾個用不到後兩個參數,但一樣收下
# (見 doctext)——為少數格式寫轉接反而更容易漏
_ROUTES: dict[str, Route] = {
    ".docx": Route(docoffice.convert_docx, docoffice._ensure_docx),
    ".pptx": Route(docoffice.convert_pptx, docoffice._ensure_pptx),
    ".xlsx": Route(docoffice.convert_xlsx, docoffice._ensure_openpyxl),
    ".xlsm": Route(docoffice.convert_xlsx, docoffice._ensure_openpyxl),
    # 含巨集的 Word/PowerPoint:與 .docx/.pptx **格式完全相同**,zip 裡只多
    # 一個 vbaProject.bin(同 .xlsm 之於 .xlsx),所以共用同一個 reader。
    # 少了這兩條的話,一份含巨集的 Word 會得到「不支援的格式」——而它其實
    # 讀得好好的(2026-08-02 盤點磁碟時發現的一致性缺口)
    ".docm": Route(docoffice.convert_docx, docoffice._ensure_docx),
    ".pptm": Route(docoffice.convert_pptx, docoffice._ensure_pptx),
    ".csv": Route(doctext.convert_csv),
    ".txt": Route(doctext.convert_txt),
    # 來源已經是 markdown:原樣帶過,不再解析一次(見 convert_markdown)
    ".md": Route(doctext.convert_markdown),
    # rtf 的預熱刻意留空:它有 striprtf 的降級路徑,為它下載 320MB
    # 的 LibreOffice 不合理(使用者也可能根本不需要那份保真度)
    ".rtf": Route(_convert_rtf),
    ".html": Route(docweb.convert_html, docweb.ensure_ready),
    ".htm": Route(docweb.convert_html, docweb.ensure_ready),
    ".mht": Route(docweb.convert_mht, docweb.ensure_ready),
    ".mhtml": Route(docweb.convert_mht, docweb.ensure_ready),
    ".pdf": Route(
        docpdf.convert_pdf, docpdf._ensure_pymupdf,
        progress=lambda f: {
            "on_page": lambda done, total: f(done / total if total else 1.0),
        },
    ),
    # epub 是「一包 XHTML + 一份閱讀順序」,解開後走同一個 html reader
    ".epub": Route(docweb.convert_epub, docweb.ensure_ready),
    # 階段二:影像檔全部走同一條 OCR 路徑(heic 由 pillow-heif 解碼)
    ".jpg": Route(docimage.convert_image, docimage.ensure_ready),
    ".jpeg": Route(docimage.convert_image, docimage.ensure_ready),
    ".png": Route(docimage.convert_image, docimage.ensure_ready),
    ".tiff": Route(docimage.convert_image, docimage.ensure_ready),
    ".tif": Route(docimage.convert_image, docimage.ensure_ready),
    ".bmp": Route(docimage.convert_image, docimage.ensure_ready),
    ".gif": Route(docimage.convert_image, docimage.ensure_ready),
    ".webp": Route(docimage.convert_image, docimage.ensure_ready),
    ".heic": Route(docimage.convert_image, docimage.ensure_ready),
    # 階段三:舊格式先升級、郵件另有一套(附件會遞迴回到這張表)
    ".doc": Route(_convert_legacy, soffice.ensure_ready),
    ".ppt": Route(_convert_legacy, soffice.ensure_ready),
    ".xls": Route(_convert_legacy, soffice.ensure_ready),
    # OpenDocument 與 Visio:同一條升級路徑,只是目標格式不同(見 _FILTERS)
    ".odt": Route(_convert_legacy, soffice.ensure_ready),
    ".ods": Route(_convert_legacy, soffice.ensure_ready),
    ".odp": Route(_convert_legacy, soffice.ensure_ready),
    ".vsd": Route(_convert_legacy, soffice.ensure_ready),
    ".vsdx": Route(_convert_legacy, soffice.ensure_ready),
    ".msg": Route(docmail.convert_mail, docmail._ensure_extract_msg),
    ".eml": Route(docmail.convert_mail),
}

# 音訊/影片 → 逐字稿(使用者 2026-08-06 指定併進 doc2md)。**這一條與上面
# 那些不同**:一個檔可能要跑幾十分鐘,所以檔內進度是必需品不是裝飾;而
# 「模型」與「講者人數」是使用者選的執行選項,由呼叫端傳進來。
# 副檔名清單的唯一出處在 srcfile(見 docaudio.supported_types)——在這裡
# 再抄一份,就會出現「分頁收得下、批次不認得」這種各說各話的狀況
for _ext in docaudio.supported_types():
    _ROUTES[_ext] = Route(
        docaudio.convert_audio, docaudio.ensure_ready,
        progress=lambda f: {"on_inner": f},
        wants_options=True,
    )


def route_for(suffix: str):
    """副檔名 → reader(沒有就 None)。給 docmail 的附件遞迴用——
    它需要知道「這個附件轉得動嗎」,但不該自己複製一份路由表。"""
    route = _ROUTES.get(suffix.lower())
    return route.read if route else None


@dataclass(frozen=True)
class FileResult:
    """單一檔案的轉換結果。error 非空即失敗(但整批不會因此中斷)。"""
    src: Path
    out: Path | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.out is not None and not self.error


@dataclass(frozen=True)
class BatchReport:
    results: list[FileResult] = field(default_factory=list)
    skipped: list[tuple[Path, str]] = field(default_factory=list)
    cancelled: bool = False
    out_dirs: list[Path] = field(default_factory=list)

    @property
    def ok(self) -> list[FileResult]:
        return [r for r in self.results if r.ok]

    @property
    def failed(self) -> list[FileResult]:
        return [r for r in self.results if not r.ok]


def _tw(text: str) -> str:
    return convert.to_taiwan_traditional(text) if text else text


def _text_of(blocks: list[Block]) -> str:
    """所有 Block 的文字,用來比對簡轉繁有沒有真的動到內容。

    只看文字欄位:`Image.rel_path` 之類的路徑本來就不參與轉換(見
    traditionalize),把它們算進來只會讓比對永遠相等。"""
    parts: list[str] = []
    for b in blocks:
        parts.append(getattr(b, "text", "") or "")
        parts.append(getattr(b, "alt", "") or "")
        parts.append(getattr(b, "caption", "") or "")
        if isinstance(b, Table):
            parts.extend(cell for row in b.rows for cell in row)
        if isinstance(b, Records):
            parts.extend(b.header)
            parts.extend(cell for row in b.rows for cell in row)
    return "\n".join(parts)


def traditionalize(blocks: list[Block]) -> list[Block]:
    """簡轉繁,**做在 Block 上而不是渲染完的文字上**。

    對 RAG 來說簡轉繁不是禮貌而是功能:知識庫的查詢是繁中,內容留簡中
    就檢索不到(使用者 2026-08-01 指定產物主要餵 RAG)。

    為什麼不在文字層做(原本的作法):那需要靠字串手術把 frontmatter
    挖掉(`source_path` 是真實路徑,轉了就再也對不回原始檔案),而
    **`Image.rel_path` 挖不到**——來源檔名是簡體字時,磁碟上的資料夾
    叫 `报表.assets`,md 裡的連結卻被繁化成 `報表.assets`,圖片全斷。
    在 Block 層做就沒有這個問題:路徑欄位天生不在範圍內,frontmatter
    更是 render 之後才產生的。"""
    out: list[Block] = []
    for b in blocks:
        if isinstance(b, docmd.Heading):
            out.append(docmd.Heading(b.level, _tw(b.text)))
        elif isinstance(b, docmd.Para):
            out.append(docmd.Para(_tw(b.text)))
        elif isinstance(b, docmd.Note):
            out.append(docmd.Note(_tw(b.text), b.kind, b.lossy))
        elif isinstance(b, docmd.Table):
            out.append(docmd.Table(
                [[_tw(c) for c in row] for row in b.rows],
                b.has_header, _tw(b.caption),
            ))
        elif isinstance(b, docmd.Records):
            out.append(docmd.Records(
                [_tw(h) for h in b.header],
                [[_tw(c) for c in row] for row in b.rows],
                _tw(b.context),
            ))
        elif isinstance(b, docmd.Image):
            # rel_path 絕不轉:它必須對得上磁碟上真實的資料夾名
            out.append(docmd.Image(b.rel_path, _tw(b.alt)))
        else:  # Raw:已經是成形的 markdown,原樣保留
            out.append(b)
    return out


def plan_outputs(
    files: list[Path], out_dir: Path | None = None,
) -> list[tuple[Path, Path | None, str]]:
    """先算好整批的輸出路徑,回傳 [(來源, 目標, 說明)];**目標為 None
    表示這一份要跳過**(已經有同名 .md,見 docmd.target_md_path)。

    先規劃再執行有兩個好處:(1) 可以在開跑前把「將寫出/將跳過」列給
    使用者看(50 檔批次下很有價值);(2) 批次內的同名衝突在這裡就用
    `claimed` 記帳解決掉——`報表.xlsx` 與 `報表.pdf` 都想要 `報表.md`,
    邊跑邊算的話第二個會覆蓋第一個剛寫好的成果。"""
    claimed: set[Path] = set()
    plan: list[tuple[Path, Path | None, str]] = []
    for src in files:
        dest, note = docmd.target_md_path(src, claimed, out_dir)
        if dest is not None:
            claimed.add(dest)
        plan.append((src, dest, note))
    return plan


def dry_run_lines(plan: list[tuple[Path, Path | None, str]]) -> list[str]:
    """開跑前的預告清單(給結果框顯示)。"""
    lines: list[str] = []
    for src, dest, note in plan:
        if dest is None:
            lines.append(f"- 「{src.name}」→ 跳過({note})")
        else:
            lines.append(f"- 「{src.name}」→ 「{dest.name}」")
    return lines


def ensure_engines_ready(files: list[Path], ocr_enabled: bool = True) -> None:
    """把這批用得到的解析器先載入(依路由表,不另維護一份名單)。

    抄 `pipeline.py` 裡 `punctuate.ensure_ready()` 的擺位:環境缺元件這種
    錯要在**第一秒**炸出來,不能跑到第 47 個檔才發現——那時前面 46 個檔
    的時間已經花掉了,而使用者以為整批都會成功。"""
    exts = {p.suffix.lower() for p in files}
    seen: list = []
    for ext in exts:
        route = _ROUTES.get(ext)
        if route and route.ensure and route.ensure not in seen:
            seen.append(route.ensure)
            route.ensure()
    # OCR 子行程要起來 + 載三顆模型(約 2 秒):整批共用一支,而且
    # 「引擎起不來」這種錯要在第一秒炸出來,不是跑到第 47 個檔才發現。
    # 掃描頁與影像檔以外的格式也可能有內嵌圖,所以只要開了 OCR 就預熱
    # 純文字格式不可能有內嵌圖:為它們起一支子行程要 2 秒、佔數百 MB
    if ocr_enabled and (exts - {".txt", ".csv"}):
        ocr.ensure_ready()


def convert_file(
    src: Path,
    dest: Path,
    converted_at: str,
    ocr_enabled: bool = True,
    mail_attachments: bool = True,
    on_inner: Callable[[float], None] | None = None,
    options: dict | None = None,
) -> FileResult:
    """單一檔案 → 寫出 md。**不吞例外**:容錯由 convert_batch 決定。

    options 是「使用者選的執行選項」(目前只有音訊的 model_key/
    num_speakers),只會交給宣告 wants_options 的路由。"""
    # 副檔名被改過的舊格式:靠內容判斷、改走 LibreOffice 升級那條路。
    # 這一步在路由**之前**做——不然使用者只會看到「檔案可能已損壞」
    detected = _actually_legacy(src)
    suffix = detected or src.suffix.lower()
    route = _ROUTES.get(suffix)
    if route is None:
        raise UserFacingError(f"不支援的格式「{src.suffix}」:{src.name}")
    reader = route.read

    # assets 跟著**產出**走而不是跟著來源走:預設兩者同目錄、行為完全一樣,
    # 但 --out-dir 集中輸出時,圖片必須跟 md 在一起(md 裡是相對連結)
    assets = AssetsDir(dest.parent, dest.stem)
    kwargs: dict = {"ocr_enabled": ocr_enabled}
    if reader is docmail.convert_mail:
        kwargs["mail_attachments"] = mail_attachments
    # 副檔名說謊時,把 magic bytes 認出來的真格式帶給升級器(見 soffice.upgrade)
    if detected and reader is _convert_legacy:
        kwargs["source_ext"] = detected
    # 檔內進度:有沒有、怎麼接,由路由自己講(見 Route.progress)
    if route.progress is not None and on_inner is not None:
        kwargs.update(route.progress(on_inner))
    # 執行選項(音訊的模型/講者人數):只給要的路由,其他 reader 收到
    # 不認得的具名參數會直接 TypeError
    if route.wants_options:
        kwargs.update(options or {})
    if reader is docaudio.convert_audio:
        # 分群檔只在「relabel 真的找得到它」時才留(使用者 2026-08-19 指定):
        # 那支是從 md 出發找**同層同名**的檔,--out-dir 把 md 集中出去之後
        # 它既找不到,留在原始音檔旁邊又正好違反 --out-dir 的用意(不要在
        # 別人的資料夾裡留東西)。
        # ⚠️ **同層還不夠,檔名也要對得上**:--out-dir 模式的 md 帶一段內容
        # 雜湊(`週會-3f9a2b71.md`,見 docmd.md_path_for),而分群檔的名字是
        # 照**來源檔**取的——`--out-dir` 指回原資料夾時兩者同層、名字卻對不
        # 上,只比對目錄就會留下一個永遠不會被讀到的檔
        kwargs["keep_features"] = (
            dest.parent == src.parent and dest.stem == src.stem
        )
    blocks = list(reader(src, assets, **kwargs))
    # 寫不進去的圖片要留下痕跡(政策在 AssetsDir,不交辦給各 reader)
    blocks.extend(assets.failure_note())

    converted = traditionalize(blocks)
    meta = DocMeta(
        source=src,
        # 檔名也要繁化:它是**唯一**進 H1 的字串(docmd.render),而 H1 是
        # 切塊器最常貼進每個 chunk 的一行——留簡中等於整份文件在繁中查詢
        # 下都比對不到,正是 traditionalize 要解決的那個問題(2026-08-03
        # 實測簡體 epub:內文「書名:大模型RAG實戰」已繁化,H1 卻還是
        # 「大模型RAG实战」)。**只轉 title 不轉 source**:frontmatter 的
        # source_file/source_path 走 meta.source,是要拿去對回原始檔案的
        title=_tw(src.stem),
        converted_at=converted_at,
        source_type=src.suffix.lstrip(".").lower(),
        # 依實際結果標記,不是照開關標:RAG 建庫端要靠這個欄位篩掉
        # 「含機器辨識內容」的檔案(辨識會有錯字,有些用途不能收)
        ocr_used=any(
            isinstance(b, docmd.Note) and b.kind == docmd.KIND_OCR for b in blocks
        ),
        # 簡轉繁是無條件做的(RAG 查詢是繁中,內容留簡中就檢索不到),但
        # **內容確實被改寫過**——有人拿 md 回頭核對原件時會看到字不一樣,
        # 沒有這個欄位就無從解釋(使用者 2026-08-03 指定;只記事實、不加
        # 開關:兩個知識庫都硬性要求繁體輸出,答案永遠是「轉」)。
        # 記布林不記字數:s2tw 是 1:1,但 data/replace.txt 是整詞替換、
        # 長度會變,算出來的「改了幾個字」只會是個沒人該相信的數字
        # Raw 刻意不經 traditionalize(轉了會把相對連結指到不存在的檔案),
        # 所以「轉換前後有沒有變」對它問不出答案——已經在上游繁化過的
        # (音訊逐字稿)自己把事實帶進來,否則每一份逐字稿都會謊報 false
        extra={"traditionalised": (
            _text_of(converted) != _text_of(blocks)
            or any(
                isinstance(b, docmd.Raw) and b.traditionalised for b in converted
            )
        )},
    )
    docmd.write_md(docmd.render(converted, meta), dest)
    return FileResult(src=src, out=dest)


def convert_batch(
    files: list[Path],
    skipped: list[tuple[Path, str]] | None = None,
    on_stage: StageFn | None = None,
    converted_at: str | None = None,
    ocr_enabled: bool = True,
    mail_attachments: bool = True,
    out_dir: Path | None = None,
    options: dict | None = None,
) -> BatchReport:
    """整批轉換。單檔失敗只記錄、不中斷;按停止則立刻收手。

    **序列執行,不平行**,四個理由:
    1. 階段二的 OCR 子行程本身就會吃滿 CPU,再平行只是互搶;
    2. 階段三的 LibreOffice headless 共用 profile,不能併發;
    3. 8GB 基準機的記憶體峰值(openpyxl 讀大表可以吃到 GB 級);
    4. 完全繞開 `gr.Progress` 的 contextvars 陷阱(pipeline.py:201-213
       ——worker 執行緒沒有 copy_context 的話進度會**靜默**丟失)。
    日後若真要平行,只准平行階段一的純文字格式,而且每個 future 必須
    `contextvars.copy_context().run(...)`。

    取消:`Cancelled` 繼承 BaseException,所以下面單檔容錯的
    `except Exception` **天然放行**它——這正是 cancel.py 那樣設計的目的,
    不要改成 `except BaseException`。"""
    stamp = converted_at or datetime.now().isoformat(timespec="seconds")
    results: list[FileResult] = []
    out_dirs: list[Path] = []
    # 「已經有同名 .md」跟「格式不支援」一樣是略過,合進同一份清單給報告
    passed_over: list[tuple[Path, str]] = []
    cancelled = False

    def report(stage: str, frac: float) -> None:
        if on_stage:
            on_stage(stage, min(max(frac, 0.0), 1.0))

    with power.keep_awake():
        # 取消的檢查點從「準備」就開始,而且整段都在 try 內:使用者可能在
        # 引擎載入或路徑規劃期間就按停止,
        # 那時該回傳一份 cancelled 的報告,而不是讓 Cancelled 冒到 UI 變成
        # 一個沒有畫面的錯誤。ensure_engines_ready 的 UserFacingError 則
        # 刻意不接——缺元件是整批都做不了,該讓它往上拋
        try:
            cancel.check()
            report("準備中", 0.0)
            ensure_engines_ready(files, ocr_enabled)
            plan = plan_outputs(files, out_dir)
            total = len(plan)
            for i, (src, dest, note) in enumerate(plan):
                cancel.check()
                base = i / total
                report(f"({i + 1}/{total}) {src.name}", base)
                if dest is None:  # 已經有同名 .md,不重做
                    passed_over.append((src, note))
                    continue
                try:
                    results.append(convert_file(
                        src, dest, stamp, ocr_enabled=ocr_enabled,
                        mail_attachments=mail_attachments, options=options,
                        # 預設參數綁值:lambda 在迴圈裡建立,不綁的話每次
                        # 呼叫都讀到迴圈結束後的 i/src(經典的閉包晚綁定)
                        on_inner=lambda f, b=base, n=i + 1, name=src.name: report(
                            f"({n}/{total}) {name}", b + f / total,
                        ),
                    ))
                    if dest.parent not in out_dirs:
                        out_dirs.append(dest.parent)
                except UserFacingError as e:
                    logger.warning("轉換失敗:%s — %s", src.name, e)
                    results.append(FileResult(src=src, error=str(e)))
                except Exception:
                    logger.exception("轉換失敗(未預期):%s", src.name)
                    results.append(FileResult(
                        src=src, error="發生未預期的錯誤,詳情見終端機視窗",
                    ))
        except cancel.Cancelled:
            cancelled = True
        finally:
            # 子行程要收掉:留著會一直佔住幾百 MB(下一批會再起一支)
            ocr.shutdown()
        report("完成", 1.0)

    return BatchReport(
        results=results,
        skipped=list(skipped or []) + passed_over,
        cancelled=cancelled,
        out_dirs=out_dirs,
    )


def report_markdown(report: BatchReport) -> str:
    """批次報告 → 顯示在結果框的繁中文字。"""
    ok, failed = report.ok, report.failed
    lines: list[str] = []
    if report.cancelled:
        lines.append(f"**已停止**。已完成 {len(ok)} 個檔案,其餘未處理。")
    elif not ok and not failed:
        # 全部略過時不能說「成功 0 個」——那看起來像出了什麼事,
        # 但實情是「本來就沒有需要做的」(通常是同名 .md 都已存在)
        lines.append(f"**沒有需要轉換的檔案**({len(report.skipped)} 個都略過了)。")
    else:
        lines.append(f"**轉換完成**:成功 {len(ok)} 個" + (f",失敗 {len(failed)} 個" if failed else "") + "。")
    lines.append("")

    if ok:
        lines.append("已產生的檔案:")
        lines.extend(f"- {r.out.name}" for r in ok)
        lines.append("")
    if failed:
        lines.append("**沒有轉成功的檔案**:")
        lines.extend(f"- 「{r.src.name}」:{r.error}" for r in failed)
        lines.append("")
    if report.skipped:
        lines.append("略過的項目:")
        lines.extend(f"- 「{p.name}」:{reason}" for p, reason in report.skipped)
        lines.append("")
    if ok:
        # 把完整路徑寫在報告裡(不只放在會消失的 toast):Windows 的前景
        # 鎖定讓「開啟輸出資料夾」不保證把視窗帶到最前面,使用者至少要能
        # 從這裡複製路徑自己去(2026-08-01 使用者兩度回報「按了沒反應」)
        dirs = report.out_dirs
        if dirs:
            lines.append("輸出位置:")
            shown = dirs[:3]
            # 路徑**獨佔一行、前面不加任何字元**(使用者 2026-08-01 兩次
            # 指定):結果框是純文字 Textbox 不是 Markdown,反引號會原樣
            # 顯示;而項目符號「- 」會在整行複製時一起被帶走,貼進檔案
            # 總管就不能用了——這幾行的用途就是給人複製
            lines.extend(str(d) for d in shown)
            if len(dirs) > len(shown):
                lines.append(f"(另有 {len(dirs) - len(shown)} 個資料夾)")
            lines.append("")
        # 收尾那句要講**這一批**的失真長什麼樣子:對一批錄音講「圖表、
        # 儲存格底色」是假的,而使用者正是照這句去理解 〔 〕 的
        audio_only = all(
            r.src.suffix.lower() in docaudio.supported_types() for r in ok
        )
        examples = "轉錄跳針" if audio_only else "圖表、儲存格底色"
        lines.append(
            f"檔案就放在原始檔案的旁邊。內容裡的 〔 〕 是轉檔時無法完整呈現的地方"
            f"(例如{examples}),檔頭也有一份清單。"
        )
    return "\n".join(lines).strip()
