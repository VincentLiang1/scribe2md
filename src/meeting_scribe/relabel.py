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
from dataclasses import dataclass, replace
from pathlib import Path

from meeting_scribe import diarize, export, srcfile
from meeting_scribe.errors import UserFacingError
from meeting_scribe.types import UNKNOWN_SPEAKER, SpokenSegment

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


def _block_bounds(t: Transcript) -> list[tuple[float, float]]:
    """每個區塊的時間範圍(下一段的起點就是這一段的終點,同 Transcript.spans)。"""
    out = []
    for i, b in enumerate(t.blocks):
        end = t.blocks[i + 1].start if i + 1 < len(t.blocks) else b.start + _TAIL_SEC
        out.append((b.start, end))
    return out


def _labels_for_blocks(t: Transcript, turns: list) -> list[int]:
    """每個 md 區塊在新分群裡屬於誰(區間內講最久的那一位)。

    ⚠️ **區塊是原子的、拆不開**:一個區塊如果當初就把兩個人混在一起,
    這裡只能挑講得比較久的那一位。所以「往少的方向改」一定準(合併是
    粗化,區塊邊界是真值的超集),往多的方向改則受限於原本的粒度——
    這一點要寫在介面上,不能讓人填了 5 卻安靜地只拿到 3。"""
    out: list[int] = []
    last = 0
    for lo, hi in _block_bounds(t):
        by: dict[int, float] = {}
        for tu in turns:
            mid = (tu.start + tu.end) / 2
            if lo <= mid < hi:
                by[tu.speaker] = by.get(tu.speaker, 0.0) + (tu.end - tu.start)
        if by:
            last = max(by.items(), key=lambda kv: kv[1])[0]
        # 區間內一段都沒有(靜默、或被切掉的碎段):沿用前一段的講者,
        # 總比丟一個不存在的編號給下游好
        out.append(last)
    return out


def count_after_recluster(md_text: str, turns: list) -> int:
    """這批新分群貼回這份逐字稿之後,**實際**看得到幾位講者。

    給「重新分群」的把關試算用(`app._check_recluster`):使用者填的數字
    不等於他會拿到的位數——md 的段落是原子的,拿不到任何段落的群不會出現
    在逐字稿上(2026-08-19 實機:一份 881 個段落的稿子,填 13~17 全都得到
    10 位,而分群本身每次都確實分出了 13~17 群,掉的是最後這一步)。

    ⚠️ **走的就是 `recluster_md` 本人,不另外算一份**:兩邊各算一次的話,
    「把關說不會變、按下去卻變了」(或反過來)只是時間問題,而那種不一致
    比不擋更糟——使用者會學到這個提醒不可信。診斷區塊在試算時用不到,
    quality 傳空的。
    """
    return len([
        n for n in parse(recluster_md(md_text, turns, [])).order if n != "未知"
    ])


def _renumber_quality(quality: list, order: dict[int, int]) -> list:
    """分群品質改用**md 上的講者編號**,並丟掉沒有分到任何區塊的群。

    ⚠️ **兩件事都是正確性,不是順手整理**(2026-08-19 實機:一份填 11 位
    重新分群的逐字稿,檔尾寫「共 11 位」而內文只有 9 位,一致性還整排掛在
    別人身上)。`recluster_md` 依「首次出現」重編號標籤,`quality` 帶的卻是
    分群自己的編號——**只要有任何一群沒拿到區塊,後面全部錯開**。那次量到的
    映射是 {0:0, 2:1, 1:2, 3:3, 4:4, 7:5, 8:6, 10:7, 5:8}:檔尾「講者 2」
    印的是內文「講者 3」的數字,而「建議優先核對」(export.check_first 也吃
    這份 quality)跟著指錯人。⚠️ 表上的「發言輪次」是從 spoken 數的、早就
    是新編號,所以錯開時**同一列裡的輪次與一致性分屬兩個人**,看不出異狀。

    沒分到區塊的群一定要**整筆丟掉**而不是留著:md 的區塊是原子的,往多的
    方向改時分出來的群不保證每一群都拿得到區塊(表上長成「發言輪次 0」),
    留著會讓「共 N 位」與內文對不起來——而使用者正是看那個數字判斷
    「我填的人數有沒有生效」(那次的回報就是「改 11 但只分出 10」)。

    轉檔當下那條路沒有這個問題:`quality` 與 `spoken` 同源、編號本來就一致,
    所以修在這裡而不是 export(診斷區塊只能有一份格式,見 speaker_diagnostics)。
    """
    return [
        replace(q, speaker=order[q.speaker]) for q in quality if q.speaker in order
    ]


def recluster_md(md_text: str, turns: list, quality: list) -> str:
    """拿新的分群結果改寫逐字稿的講者標籤,**內文一個字都不動**。

    做法是**逐行改寫**而不是重建:`parse` 會把區塊內的多行併成一行
    (它只為了取線索),照它重建等於順手改掉使用者的排版。這裡只動
    「`**名字** (時:分:秒)`」那幾行與檔尾的診斷區塊。

    ⚠️ **絕對不要把相鄰的同人區塊併起來**(2026-08-18 實機災情,原本真的
    這樣做):併掉的是**下一次重新分群唯一能用的粒度**。使用者按了 4 → 2 →
    1,那個「1」把 417 個區塊併成 1 個,之後再按 2、按 3 都只能在一個區塊上
    重新分配——**永遠回不去了**,而畫面上看起來一切正常。

    保留每一行標籤,重新分群就是**可以反覆做的**:每次都拿原本那 417 個
    區塊重新分配,改錯了再改回來即可。代價只是同一位連續發言時會出現
    幾行同名標籤——那是原本就存在的發言輪次界線(時間戳是真的),
    比「改壞了救不回來」便宜太多。

    檔尾的診斷區塊整塊換成新的:群數、輪次、一致性全變了,留著舊的比
    沒有更糟。⚠️ 那些數字要跟著這裡的重編號一起換算,見 `_renumber_quality`
    ——不換算的話表格會安靜地掛在別人身上。"""
    t = parse(md_text)
    labels = _labels_for_blocks(t, turns)
    # 依「在檔案裡第一次出現」重編號,讀者看到的順序才與編號一致
    order: dict[int, int] = {}
    for lab in labels:
        if lab != UNKNOWN_SPEAKER and lab not in order:
            order[lab] = len(order)

    def label_name(lab: int) -> str:
        # 顯示規則(含「未知」)全 repo 只有 export.speaker_label 一份,
        # 這裡只負責把新分群的標籤換算成顯示用的編號
        return export.speaker_label(
            UNKNOWN_SPEAKER if lab == UNKNOWN_SPEAKER else order[lab])

    bounds = _block_bounds(t)
    out: list[str] = []
    spoken: list[SpokenSegment] = []
    i = 0
    for line in md_text.splitlines():
        if export.starts_diagnostics(line):
            break
        m = _SPEAKER_RE.match(line.strip())
        if m is None:
            out.append(line)
            continue
        lab = labels[i]
        lo, hi = bounds[i]
        i += 1
        # **每一個區塊都留一行標籤**(哪怕跟上一段同一位),理由見 docstring:
        # 那些界線是下一次重新分群唯一的粒度來源
        spoken.append(SpokenSegment(
            lo, hi, UNKNOWN_SPEAKER if lab == UNKNOWN_SPEAKER else order[lab], "",
        ))
        out.append(
            f"**{label_name(lab)}** ({m.group(2)}:{m.group(3)}:{m.group(4)})")
    text = "\n".join(out).rstrip("\n")
    diag = export.speaker_diagnostics(
        _renumber_quality(quality, order), spoken)
    return text + ("\n\n" + "\n".join(diag) if diag else "\n")


def find_features(md_path: Path) -> Path | None:
    """同一層、同檔名的分群特徵檔(`會議.md` → `會議.分群.npz`);沒有回 None。

    判準與 find_media 同一套(同層同名,理由見那支)。⚠️ **有媒體檔不代表
    有這個檔**:它是 v0.7.3 之後轉檔才會留下的,舊的逐字稿一律沒有——
    面板的三種狀態(只有 md / md+媒體 / md+媒體+分群檔)就是這樣分出來的。"""
    candidate = diarize.features_path(md_path)
    return candidate if candidate.is_file() else None


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
