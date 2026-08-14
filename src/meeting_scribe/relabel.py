r"""重設現成逐字稿的講者(讀 md → 重新命名 → 寫回)。

使用者 2026-08-06 指定。解決的是三件事後才會發現的事:批次模式轉出來的
md 只有「講者 1／2／3」、當初命名時打錯字、以及當初跳過命名。

**不重跑轉檔**:逐字稿裡已經有講者標籤與時間戳,分群早在當初就做完了。
所以這支模組只做三件很便宜的事:

1. 解析 md 拿回「有哪幾位講者、各講了幾段、最長的一句是什麼」;
2. 同一層有同名媒體檔時,依那些時間戳**剪試聽片段**(幾秒鐘)並
   **抽聲紋**(幾分鐘,走 diarize.voiceprints_for_spans);
3. 套用時把標籤換掉、寫回原檔。

兩種介面(使用者指定):沒有媒體檔就只有命名欄位,有的話多出 ▶ 試聽,
而且聲紋會進聲紋庫、下次開會自動認人——與轉檔後的命名流程完全一致。

**標籤不假設是「講者 N」**:這個功能的意義正是「重新」設定,所以 md 裡
可能已經是真名(當初命名過、只是打錯或想改)。錨定靠的是輸出格式本身
(`**任何名字** (HH:MM:SS)`,見 export.to_markdown),時間戳讓它不可能
誤中內文裡的粗體字。
"""
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from meeting_scribe import export, srcfile
from meeting_scribe.errors import UserFacingError

logger = logging.getLogger(__name__)

# 逐字稿的講者行:`**名字** (00:12:34)`(export.to_markdown 的格式)。
# **時間戳是關鍵**:少了它,內文裡任何一組粗體都會被當成講者標籤
_SPEAKER_RE = re.compile(r"^\*\*(?P<name>.+?)\*\* \((\d+):([0-5]\d):([0-5]\d)\)$")
# 試聽片段的上限(秒)。見 Transcript.hints:md 只有每段的起點,終點是拿
# 下一段的起點頂上去的,中間的靜默全被算進來——不設上限就會播到下一個人
_CLIP_MAX_SEC = 20.0
# 沒有下一段時,最後一段給的預設長度(秒)。只用來剪試聽與抽聲紋,
# 寧可短一點也不要超出檔尾
_TAIL_SEC = 30.0


@dataclass(frozen=True)
class Block:
    """逐字稿裡的一段:誰、從第幾秒開始、說了什麼。"""
    name: str
    start: float
    text: str


@dataclass(frozen=True)
class Transcript:
    """一份解析過的逐字稿。

    order 是「講者在檔案裡第一次出現的順序」——命名欄位照它排,使用者
    由上往下填的順序才會跟他讀預覽的順序一致。
    """
    blocks: list[Block]
    order: list[str]

    def spans(self) -> dict[str, list[tuple[float, float]]]:
        """每位講者的發言區間(下一段的起點就是這一段的終點)。

        給剪試聽與抽聲紋用。最後一段沒有「下一段」,給 _TAIL_SEC 的保守
        長度——ffmpeg 與聲紋抽取超出檔尾都只會拿到比較短的音訊,不會壞。
        """
        out: dict[str, list[tuple[float, float]]] = {}
        for i, b in enumerate(self.blocks):
            end = (
                self.blocks[i + 1].start if i + 1 < len(self.blocks)
                else b.start + _TAIL_SEC
            )
            out.setdefault(b.name, []).append((b.start, end))
        return out

    def hints(self) -> dict[int, tuple[int, str, float, float]]:
        """命名欄位的認人線索,格式與 pipeline.PipelineResult.speaker_hints
        相同(段數, 最長一句摘錄, 起, 訖)——鍵是 order 的索引,好讓
        app._present_result 那一整套命名/試聽 UI 原封不動地共用。

        摘錄挑「字最多的那一段」(轉檔後的流程挑最長的一句,同樣的意思:
        字多的段落最容易認人)。

        ⚠️ **試聽的長度一定要另外設上限**,不能直接用區間長度:md 只有
        每段的**起點**,終點是拿下一段的起點頂上去的,中間的靜默全被算進來
        ——實測一段只講了幾秒、下一位隔了 48 秒才開口,照區間剪就會播到
        下一個人的聲音,而使用者正是靠這段音去認人的。
        _CLIP_MAX_SEC 之後那幾秒屬於誰無從得知,寧可短。"""
        by_name: dict[str, list[tuple[Block, tuple[float, float]]]] = {}
        for b, span in zip(self.blocks, _flatten(self.blocks, self.spans())):
            by_name.setdefault(b.name, []).append((b, span))
        out: dict[int, tuple[int, str, float, float]] = {}
        for idx, name in enumerate(self.order):
            items = by_name[name]
            best_block, (start, end) = max(items, key=lambda x: len(x[0].text))
            out[idx] = (
                len(items), best_block.text, start, min(end, start + _CLIP_MAX_SEC),
            )
        return out


def _flatten(blocks: list[Block], spans: dict[str, list[tuple[float, float]]]):
    """把 spans()(依講者分組)攤回與 blocks 同序,好與每一段對起來。"""
    cursor = {name: 0 for name in spans}
    for b in blocks:
        i = cursor[b.name]
        cursor[b.name] = i + 1
        yield spans[b.name][i]


def parse(md_text: str) -> Transcript:
    """md 全文 → Transcript。找不到任何講者行就拋繁中錯誤。

    非講者行一律當成前一段的內文(逐字稿本體就是這樣:標籤一行、
    內容跟在下面),frontmatter 與標題自然被前面沒有標籤的狀態忽略。"""
    blocks: list[Block] = []
    order: list[str] = []
    pending: list[str] = []
    for line in md_text.splitlines():
        # 檔尾的講者診斷區塊到此為止:它不是誰的發言,被當成前一段的內文
        # 會混進命名摘錄(`hints` 挑「字最多的那一段」,而那張表字很多)
        if export.starts_diagnostics(line):
            break
        m = _SPEAKER_RE.match(line.strip())
        if m is None:
            if blocks:
                pending.append(line.strip())
            continue
        if blocks:
            blocks[-1] = Block(
                blocks[-1].name, blocks[-1].start,
                " ".join(p for p in pending if p),
            )
        pending = []
        name = m.group("name")
        h, mi, s = (int(m.group(i)) for i in (2, 3, 4))
        blocks.append(Block(name, h * 3600 + mi * 60 + s, ""))
        if name not in order:
            order.append(name)
    if not blocks:
        raise UserFacingError(
            "這份 md 裡找不到講者標籤,可能不是本工具產生的逐字稿"
            "(講者行的格式是「**講者 1** (00:00:00)」)"
        )
    blocks[-1] = Block(
        blocks[-1].name, blocks[-1].start, " ".join(p for p in pending if p),
    )
    return Transcript(blocks=blocks, order=order)


def find_media(md_path: Path) -> Path | None:
    """同一層、同檔名的媒體檔(`會議.md` → `會議.m4a`);沒有回 None。

    **只認同層同名**是刻意的:兩條產生 md 的路徑都滿足它——批次模式把
    md 放在原檔旁邊,單檔模式的逐字稿與錄音檔都落在 `output/`。再放寬
    (例如往上層找、或模糊比對)只會讓「配到別場會議的錄音」變成可能,
    而那個錯誤的下場是聲紋庫被灌進錯的人。"""
    for ext in srcfile.SUPPORTED_TYPES:
        candidate = md_path.with_suffix(ext)
        if candidate.is_file():
            return candidate
    return None


def rename(md_text: str, name_map: dict[str, str]) -> str:
    """把逐字稿裡的講者標籤換成新名字(只動講者行與檔尾診斷區塊,不碰內文)。

    逐行重建而不是全文 replace:全文替換會誤中內文裡剛好一樣的粗體字,
    而且「講者 1」→「講者 10」這類前綴問題也要另外處理。逐行比對是
    這個格式天然給的錨,不必自己發明一個。

    **檔尾的診斷區塊要整塊一起改**,而且**表格與敘述句是兩件事**:
    表格靠 export.DIAG_ROW_RE 逐列換(它逐列點名「哪個標籤該先核對」),
    敘述句靠 export.diag_prose_renamer 換(「建議優先核對:**講者 3**」
    「「**未知**」這一批共 38 段」)。命名之後任何一邊若還停在「講者 6」,
    使用者與下游都對不回去是誰——一份自相矛盾的警告比沒有警告更糟。
    兩個錨都放在 export.py:產生那些句子的是它,規則跟著產生端走才追得上。
    ⚠️ **敘述句那條只在診斷區塊之內套用**:那一塊是程式產生的文字、
    不含任何人講的話,所以粗體必定是標籤;內文裡的粗體則不得被動
    (使用者講的話裡剛好出現某個名字是很正常的事)。"""
    rename_prose = export.diag_prose_renamer(name_map)
    out = []
    in_diagnostics = False
    for line in md_text.splitlines():
        if export.starts_diagnostics(line):
            in_diagnostics = True
        m = _SPEAKER_RE.match(line.strip())
        if m and m.group("name") in name_map:
            out.append(line.replace(
                f"**{m.group('name')}**", f"**{name_map[m.group('name')]}**", 1))
            continue
        d = export.DIAG_ROW_RE.match(line)
        if d and d.group("name") in name_map:
            out.append(line.replace(
                f"| {d.group('name')} |", f"| {name_map[d.group('name')]} |", 1))
            continue
        out.append(rename_prose(line) if in_diagnostics else line)
    return "\n".join(out) + ("\n" if md_text.endswith("\n") else "")


def read(md_path: Path) -> str:
    """讀逐字稿。編碼固定 UTF-8(本工具自己寫出來的一律是),
    讀不到就翻成繁中——第三方的 cryptic 英文不得面對使用者(spec §8)。"""
    try:
        return md_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise UserFacingError(
            f"「{md_path.name}」不是 UTF-8 文字檔,無法當成逐字稿讀取"
        ) from e
    except OSError as e:
        logger.debug("讀取逐字稿失敗", exc_info=True)
        raise UserFacingError(f"讀不到「{md_path.name}」:{e.strerror}") from e


def validate(text) -> Path:
    """「重設講者」的路徑把關:必須是存在的單一 `.md` 檔。"""
    cleaned = srcfile.clean_path(text)
    if not cleaned:
        raise UserFacingError("請先按「選擇逐字稿…」挑一份 md,或貼上它的路徑")
    p = Path(cleaned)
    if p.is_dir():
        raise UserFacingError(f"這是資料夾,請選擇單一逐字稿檔:{cleaned}")
    if not p.is_file():
        raise UserFacingError(f"找不到檔案,請確認路徑是否正確:{cleaned}")
    if p.suffix.lower() != ".md":
        raise UserFacingError(
            f"「重設講者」只吃逐字稿 md 檔,這是「{p.suffix or '(無副檔名)'}」"
        )
    return p


def pick_md() -> str:
    """「選擇逐字稿…」:原生對話框,只挑一份 md。取消回空字串。"""
    picked = srcfile.native_dialog(lambda fd, root: fd.askopenfilename(
        parent=root, title="選擇要重設講者的逐字稿(.md)",
        filetypes=[("逐字稿", "*.md"), ("所有檔案", "*.*")],
    ))
    return str(Path(picked)) if picked else ""
