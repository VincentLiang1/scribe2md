r"""Outlook 郵件(.msg / .eml)→ Block 清單,**含附件遞迴轉換**。

「把這串信歸檔」在公司很高頻,而信的價值常常一半在附件裡——所以附件
能轉的就遞迴轉成 md 並在本文連結,不能轉的(zip/exe)原樣存進 assets
並標註(使用者 2026-08-01 選定)。

.eml 用標準函式庫的 `email` 就夠;.msg 是 OLE 複合文件,走 extract-msg。

**附件是不可信輸入**,三道防護缺一不可(見 convert_mail 的 docstring):
深度上限、循環偵測、總量預算。檔名另外過 `docmd.sanitize_name` + 寫入前
的 is_relative_to 斷言——`..\..\evil.txt` 這種檔名是真的會出現的。
"""
import hashlib
import logging
import re
import tempfile
from dataclasses import dataclass, field
from email import message_from_bytes, policy
from pathlib import Path

from meeting_scribe import docmd, pipeline
from meeting_scribe.docmd import AssetsDir, Block, Heading, Note, Para
from meeting_scribe.errors import UserFacingError

logger = logging.getLogger(__name__)

# 惰性載入的佔位(測試 monkeypatch 這個屬性換假貨)
extract_msg = None

# .msg 可以內含 .msg,而那封又可以再內含——沒有上限的話,一封離譜或惡意
# 的信就能讓轉檔永遠跑不完
_MAX_DEPTH = 3
# 深度與循環都擋不住「橫向爆量」:一封信塞 1000 個附件
_MAX_ATTACHMENTS = 200
_MAX_TOTAL_BYTES = 2 * 1024**3
# 單一附件的大小上限:再大就不該用郵件寄了,而且讀進記憶體會出事
_MAX_ONE_ATTACHMENT = 512 * 1024**2

_HEADER_LABELS = [
    ("from", "寄件者"), ("to", "收件者"), ("cc", "副本"),
    ("date", "日期"), ("subject", "主旨"),
]


def _ensure_extract_msg():
    global extract_msg
    if extract_msg is None:
        extract_msg = docmd.lazy_import("extract_msg", "Outlook 郵件")
    return extract_msg


@dataclass
class _Budget:
    """整封信(含所有層附件)共用的預算。用可變 dataclass 而不是回傳值
    累加:遞迴到第三層時,誰該扣誰的帳一目了然。"""
    attachments: int = 0
    total_bytes: int = 0
    notes: list[str] = field(default_factory=list)

    def take(self, size: int) -> bool:
        if self.attachments >= _MAX_ATTACHMENTS:
            return False
        if self.total_bytes + size > _MAX_TOTAL_BYTES:
            return False
        self.attachments += 1
        self.total_bytes += size
        return True


@dataclass(frozen=True)
class _Attachment:
    name: str
    data: bytes


def _header_blocks(get) -> list[Block]:
    """信件標頭 → 一段可讀的摘要。

    RAG 切塊後的 chunk 會脫離上下文,所以寄件者/日期/主旨要在**內容裡**,
    不能只放在 frontmatter(那是整份檔案層級的)。"""
    lines = []
    for key, label in _HEADER_LABELS:
        value = str(get(key) or "").strip()
        if value:
            lines.append(f"- {label}:{value}")
    return [Para("\n".join(lines))] if lines else []


def _eml_parts(raw: bytes) -> tuple[str, list[_Attachment], dict]:
    """.eml → (本文, 附件, 標頭取值函式用的 dict)。"""
    msg = message_from_bytes(raw, policy=policy.default)
    body = ""
    attachments: list[_Attachment] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        ctype = (part.get_content_type() or "").lower()
        if filename:
            try:
                payload = part.get_payload(decode=True) or b""
            except Exception:
                logger.debug("附件解碼失敗:%s", filename, exc_info=True)
                continue
            attachments.append(_Attachment(filename, payload))
        elif ctype == "text/plain" and not body:
            try:
                body = part.get_content()
            except Exception:
                body = ""
        elif ctype == "text/html" and not body:
            # 只有 HTML 版時退而求其次:標籤先粗略去掉,細活交給 docweb
            # 對付不了的情形,這裡只求別把整段吞掉
            try:
                import re

                body = re.sub(r"<[^>]+>", " ", part.get_content())
            except Exception:
                body = ""
    return body, attachments, {k: msg.get(k) for k, _ in _HEADER_LABELS}


# 宣告的內碼解不開時,依序換這些再試。**cp950 排第一**是因為使用者在
# 台灣:實測一封繁中信件自報 gb2312、內容其實是 cp950,extract-msg 照著
# 宣告解就整封讀不出來。`"chardet"` 是 extract-msg 自己的自動偵測,放最後
# 當保底(理由同 `doctext.read_text_auto` 把統計猜測降為同分裁判)
_MSG_ENCODING_FALLBACKS = ("cp950", "chardet")


def _msg_attachments(message) -> tuple[list[_Attachment], int]:
    """附件清單,以及**讀不出來的個數**。

    附件是最容易踩到編碼地雷的地方(檔名的內碼跟著郵件宣告走),而
    「一個附件的檔名解不開」不該讓整封信報廢——實測一封 1,049 字的真實
    郵件就是這樣被整個丟掉的。讀不到的算個數,由呼叫端下標記。"""
    out: list[_Attachment] = []
    try:
        items = list(getattr(message, "attachments", []) or [])
    except Exception:  # noqa: BLE001 - 連清單都取不到,當成「有附件但讀不到」
        logger.info("郵件附件清單讀取失敗", exc_info=True)
        return [], 1
    lost = 0
    for att in items:
        try:
            name = str(
                getattr(att, "longFilename", None)
                or getattr(att, "shortFilename", None)
                or "附件"
            )
            data = getattr(att, "data", None)
        except Exception:  # noqa: BLE001 - 逐個容錯:壞一個不該連累其他
            logger.info("郵件附件讀取失敗", exc_info=True)
            lost += 1
            continue
        if isinstance(data, bytes):
            out.append(_Attachment(name, data))
        elif data is not None:
            # 巢狀 .msg:extract-msg 給的是 Message 物件而不是位元組
            blob = getattr(data, "export", None)
            if callable(blob):
                try:
                    out.append(_Attachment(
                        name if name.lower().endswith(".msg") else f"{name}.msg",
                        blob(),
                    ))
                except Exception:
                    logger.debug("巢狀郵件附件匯出失敗:%s", name, exc_info=True)
                    lost += 1
        else:
            lost += 1
    return out, lost


def _msg_read(message) -> tuple[str, list[_Attachment], dict, list[str]]:
    """從已開啟的 Message 取出本文、附件、標頭,以及**讀不出來的東西**。

    **標頭要逐欄位取**:extract-msg 惰性解碼,而收件者顯示名稱的內碼跟著
    郵件宣告走——實測一封真實郵件的 `to`/`cc` 是 cp950 卻宣告 gb2312,而
    寄件者、主旨、本文、附件全都好好的。整批取的話,兩個欄位解不開就把
    1,049 字的本文連同附件一起丟掉。

    **順序也是刻意的**:本文與標頭在前、附件最後且自己容錯,因為先取附件
    的話,一個檔名解不開同樣會把讀得好好的本文帶走。"""
    headers: dict[str, str] = {}
    unreadable: list[str] = []
    labels = dict(_HEADER_LABELS)
    for key, attr in (("from", "sender"), ("to", "to"), ("cc", "cc"),
                      ("date", "date"), ("subject", "subject")):
        try:
            headers[key] = str(getattr(message, attr, "") or "")
        except Exception:  # noqa: BLE001 - 少一個欄位遠好過整封信報廢
            logger.info("郵件標頭 %s 解不開,略過", attr, exc_info=True)
            headers[key] = ""
            unreadable.append(labels[key])
    try:
        body = str(getattr(message, "body", "") or "")
    except Exception:  # noqa: BLE001
        logger.info("郵件本文解不開", exc_info=True)
        body, _ = "", unreadable.append("本文")
    attachments, lost = _msg_attachments(message)
    if lost:
        unreadable.append(f"{lost} 個附件")
    return body, attachments, headers, unreadable


def _msg_parts(src: Path) -> tuple[str, list[_Attachment], dict, list[str]]:
    """.msg → (本文, 附件, 標頭, 讀不出來的項目)。巢狀的 .msg 附件會被還原
    成位元組,交給遞迴那一層當成一般附件處理。

    **郵件自報的內碼不可盡信**:同 `docweb._decode_html`,宣告權威但解不開
    就得換一個再試,否則一封讀得出來的信會整封失敗(2026-08-02 抽樣掃描
    踩到:繁中信件宣告 gb2312)。"""
    mod = _ensure_extract_msg()
    last: Exception | None = None
    for override in (None, *_MSG_ENCODING_FALLBACKS):
        kwargs = {} if override is None else {"overrideEncoding": override}
        try:
            message = mod.Message(str(src), **kwargs)
        except Exception as e:
            if override is not None:
                last = e
                continue
            raise UserFacingError(
                f"無法開啟 Outlook 郵件「{src.name}」:檔案可能已損壞"
            ) from e
        try:
            return _msg_read(message)
        except Exception as e:  # noqa: BLE001 - 換個內碼再試,都不行才報錯
            last = e
            logger.info("郵件 %s 以 %s 解不開,改試下一個內碼", src.name,
                        override or "宣告的內碼")
        finally:
            try:
                message.close()
            except Exception:
                pass
    raise UserFacingError(
        f"無法讀取 Outlook 郵件「{src.name}」的內容:檔案可能已損壞或編碼有誤"
    ) from last


def convert_mail(
    src: Path,
    assets: AssetsDir,
    ocr_enabled: bool = True,
    mail_attachments: bool = True,
    _depth: int = 0,
    _seen: frozenset[str] = frozenset(),
    _budget: _Budget | None = None,
) -> list[Block]:
    r"""郵件 → Blocks(本文 + 附件)。

    **三道防護,缺一不可**:

    1. **深度上限** —— .msg 可以內含 .msg,無上限的話一封離譜的信就能讓
       轉檔跑不完。
    2. **循環偵測** —— 以附件位元組的 sha256 帶進遞迴路徑;同一份內容在
       同一條路徑上再次出現就是自我包含,立刻停(把自己當附件寄出是真的
       會發生的事)。
    3. **總量預算** —— 深度與循環都擋不住「一封信 1000 個附件」的橫向爆量。

    附件檔名來自不可信來源:一律過 `docmd.sanitize_name`(剝 `..\`、
    Windows 保留裝置名等),寫入時 `AssetsDir` 再做一次 is_relative_to 斷言。
    """
    budget = _budget if _budget is not None else _Budget()
    if src.suffix.lower() == ".msg":
        body, attachments, headers, unreadable = _msg_parts(src)
    else:
        body, attachments, headers = _eml_parts(src.read_bytes())
        unreadable = []

    blocks: list[Block] = []
    blocks.extend(_header_blocks(headers.get))
    if unreadable:
        # 本文救回來了,但這幾樣是真的沒了——不能安靜地少
        blocks.append(Note(
            f"這封郵件的 {'、'.join(unreadable)} 讀不出來(郵件自報的編碼有誤)",
            docmd.KIND_ENCODING_GUESS,
        ))
    text = (body or "").strip()
    if text:
        blocks.append(Para(text))
    elif not attachments:
        blocks.append(Note("這封郵件沒有內容", docmd.KIND_BLANK_PAGE, lossy=False))

    if not attachments:
        return blocks + _body_gaps(body, blocks, _depth)
    if not mail_attachments:
        blocks.append(Note(
            f"這封郵件有 {len(attachments)} 個附件,設定為不轉換附件",
            docmd.KIND_ATTACHMENT,
        ))
        return blocks + _body_gaps(body, blocks, _depth)
    if _depth >= _MAX_DEPTH:
        blocks.append(Note(
            f"郵件附件已達 {_MAX_DEPTH} 層上限,更深的附件沒有展開",
            docmd.KIND_DEPTH_LIMIT,
        ))
        return blocks + _body_gaps(body, blocks, _depth)

    blocks.append(Heading(1, f"附件({len(attachments)} 個)"))
    for att in attachments:
        blocks.extend(_attachment_blocks(
            att, assets, ocr_enabled, _depth, _seen, budget,
        ))
    if budget.notes:
        blocks.append(Note("、".join(dict.fromkeys(budget.notes)), docmd.KIND_ATTACHMENT))
    return blocks + _body_gaps(body, blocks, _depth)


def _body_gaps(body: str, blocks: list[Block], depth: int) -> list[Block]:
    """自我稽核:**信件本文有沒有整段掉在半路**。

    郵件的對照來源就是 `_msg_parts`/`_eml_parts` 解出來的本文——那是
    「這封信說了什麼」的唯一版本,而它到輸出之間只隔一個 `Para`。所以這條
    抓的不是「某種節點沒被走訪」(郵件沒有那種結構),而是**本文在
    multipart 挑選、編碼解碼、或 html 轉純文字那幾步被吃掉**。

    **只在最外層做**(`depth == 0`):遞迴進去的附件郵件各自有自己的
    Blocks,拿內層本文去比對外層的輸出會整批誤報。"""
    if depth or not (body or "").strip():
        return []
    paragraphs = [p for p in re.split(r"\n\s*\n", body) if p.strip()]
    return docmd.extraction_gap_note(docmd.missing_from(paragraphs, blocks))
    return docmd.extraction_gap_note(docmd.missing_from(paragraphs, blocks))


def _attachment_blocks(
    att: _Attachment, assets: AssetsDir, ocr_enabled: bool,
    depth: int, seen: frozenset[str], budget: _Budget,
) -> list[Block]:
    """(不收 mail_attachments:走到這裡時它必為 True——convert_mail
    在更上面就 return 了。)

    附件的內容會整體下推 `_ATTACHMENT_HEADING_SHIFT` 層,見 _shift_headings。"""
    name = docmd.sanitize_name(att.name, "附件")
    if len(att.data) > _MAX_ONE_ATTACHMENT:
        return [Note(f"附件「{name}」太大({len(att.data) // 1024**2} MB),未處理",
                     docmd.KIND_ATTACHMENT)]
    if not budget.take(len(att.data)):
        budget.notes.append("附件數量或總大小超過上限,其餘附件未處理")
        return []

    digest = hashlib.sha256(att.data).hexdigest()
    if digest in seen:
        return [Note(
            f"附件「{name}」的內容與上層郵件相同(自我包含),已略過",
            docmd.KIND_CYCLE,
        )]

    link = assets.add_bytes(att.data, Path(name).suffix or ".bin", filename=name)
    blocks: list[Block] = [Heading(2, f"附件:{name}")]
    if link:
        blocks.append(Para(f"[{name}]({link})"))

    inner = _convert_attachment(
        att, name, assets, ocr_enabled, depth, seen | {digest}, budget,
    )
    if inner:
        blocks.extend(_shift_headings(inner, _ATTACHMENT_HEADING_SHIFT))
    else:
        blocks.append(Note(
            f"附件「{name}」不是可轉換的格式,已原樣存放", docmd.KIND_ATTACHMENT,
        ))
    return blocks


# 附件本身的標題是 `Heading(2)`(渲染成 `###`),內容要比它更深一層
_ATTACHMENT_HEADING_SHIFT = 2


def _shift_headings(blocks: list[Block], by: int) -> list[Block]:
    """把一批 Block 的標題整體下推。

    reader 是以「文件內視角」給層級的(docx 自己的 H1 就是 1),而附件的
    內容要掛在「### 附件:合約.docx」底下。不下推的話,那份合約的 H1 會
    渲染成 `##`——**比它的父層還淺**,等於跳出了附件區塊,RAG 切塊時它
    看起來像是信件的頂層章節而不是某個附件裡的東西(使用者 2026-08-02
    看到實際輸出後指出)。

    巢狀郵件會自然疊加:內層 convert_mail 產生的標題先被內層這一步推過,
    再被外層推一次。`docmd.render` 本來就把層級夾在 6 以內。"""
    return [
        Heading(b.level + by, b.text) if isinstance(b, Heading) else b
        for b in blocks
    ]


def _convert_attachment(
    att: _Attachment, name: str, assets: AssetsDir, ocr_enabled: bool,
    depth: int, seen: frozenset[str], budget: _Budget,
) -> list[Block]:
    """能轉的附件就地轉成 Blocks(遞迴)。不能轉的回空清單。

    **延後 import docpipe**:docpipe 已經 import 了本模組,模組層互 import
    會變成循環。函式內 import 是這種「上下層互相需要」的標準解法。"""
    from meeting_scribe import docpipe

    suffix = Path(name).suffix.lower()
    reader = docpipe.route_for(suffix)
    if reader is None:
        return []

    # `ignore_cleanup_errors` 與 docpipe._convert_legacy 同因:附件是舊格式
    # 時 soffice 會在這裡面產出升級檔,而它退出後 Windows 有時還按著那個檔
    # 不放,清暫存就吃 WinError 32——**發生在轉檔成功之後**,不吞掉的話一封
    # 讀得好好的信會在最後一刻炸掉(2026-08-02 兩封真實郵件實際踩到)。
    # 殘留目錄帶 TMP_PREFIX,下次啟動的 cleanup_stale_temp 會掃掉
    with tempfile.TemporaryDirectory(
        prefix=pipeline.TMP_PREFIX + "mail-", ignore_cleanup_errors=True,
    ) as tmp:
        inner_src = Path(tmp) / name
        try:
            inner_src.write_bytes(att.data)
        except OSError:
            logger.debug("附件落地失敗:%s", name, exc_info=True)
            return []
        try:
            if suffix in (".msg", ".eml"):
                return convert_mail(
                    inner_src, assets, ocr_enabled,
                    _depth=depth + 1, _seen=seen, _budget=budget,
                )
            return list(reader(inner_src, assets, ocr_enabled=ocr_enabled))
        except UserFacingError as e:
            return [Note(f"附件「{name}」轉換失敗:{e}", docmd.KIND_ATTACHMENT)]
        except Exception:
            logger.exception("附件轉換失敗:%s", name)
            return [Note(f"附件「{name}」轉換失敗", docmd.KIND_ATTACHMENT)]
