"""核對音檔:把某一位講者(或「未知」)的發言接成一個檔,一次聽完。

**存在的理由是 ▶️ 試聽 回答不了的那個問題**(使用者 2026-08-13 指定做):
試聽只放**一段**(該標籤最長的那一句、上限 20 秒),回答的是「這聽起來
像誰」。它回答不了「**這一群裡面是不是混了別人**」——2026-08-12 資訊
月會實跡:某個標籤的 38 段裡有 13 段共 20 秒分屬另外七個人,而試聽播的
那 14.1 秒**真的是本人**。聽完它任何人都會確認「就是他」然後命名。
把整群接起來一次聽完才看得出來(當時是靠命令列的
`scripts/make_audit_clip.py` 才查出真相)。

⚠️ **單位是「一輪發言」(區塊)不是講者分離的原始區段**,這是架構限制
不是偷懶:md 的每個區塊是「同一位講者的連續句子合併後**整段跑標點模型**」
的產物,區塊內部的句界已經不存在——要在區塊中間切一刀改掛,只能整份
重跑標點(數十秒)。而**與前後鄰居不同講者的插話本來就自成一個區塊**,
所以要查的那些都查得到,而且核對表上的一列 = 逐字稿上看得到的一行。

⚠️ **抽樣是分層的,不是「取最長的幾段」**(使用者 2026-08-13 選定):
只取最長的段會**正好漏掉短插話**——0812 混進來的別人全是短的,那樣抽
等於查不出問題。所以上限的前半給最長的段(夠長才聽得出是誰)、後半在
剩下的段裡依時間均勻取(才看得出整場有沒有混人)。
"""
import logging
import re
import wave
from dataclasses import dataclass
from pathlib import Path

from meeting_scribe import audio, export
from meeting_scribe.errors import UserFacingError
from meeting_scribe.types import SpeechBlock

logger = logging.getLogger(__name__)

# 核對音檔的長度上限(秒)。使用者 2026-08-13 先選 5 分鐘,**實際聽過
# 0812 那批之後改成 3 分鐘**——78 段一段段跳著聽,體感遠比想像中久。
# ⚠️ 這個數字是「使用者願意坐著聽多久」,不是技術限制:調大不會更準,
# 只會讓人不想點。
CAP_SEC = 180.0
# 上限的前半留給「最長的段」,後半給均勻抽樣(見模組 docstring 的 ⚠️)
LONG_SHARE = 0.5
# 每段前後各多留一點,免得切掉字頭字尾(同 make_audit_clip.py)
PAD_SEC = 0.25
# 段與段之間的靜音:聽得出換段了,不會把兩個人的話聽成一句
GAP_SEC = 0.45


@dataclass(frozen=True)
class AuditRow:
    """核對表的一列(= 逐字稿上的一輪發言)。"""

    index: int          # 在該講者所有區塊裡的序號(1-based,給使用者對照用)
    start: float        # 原錄音時間
    end: float
    text: str           # 內容摘錄

    @property
    def seconds(self) -> float:
        return max(self.end - self.start, 0.0)


def blocks_of(blocks: list[SpeechBlock], speaker: int) -> list[SpeechBlock]:
    """某一位講者(或 UNKNOWN_SPEAKER)的所有區塊,依時間排序。"""
    return sorted(
        (b for b in blocks if b.speaker == speaker), key=lambda b: b.start,
    )


def _cost(block: SpeechBlock) -> float:
    """這一段放進核對音檔會佔多少秒。

    ⚠️ **必須把留白與段間靜音算進去**:只算語音的話,128 段就會多出
    128 × 0.95 = 122 秒——實測 0812 那批「未知」用 5 分鐘的上限接出
    **7 分鐘**的檔案(2026-08-13 拿真實資料驗才發現)。上限是使用者
    要聽多久,不是語音有多長。"""
    return block.seconds + 2 * PAD_SEC + GAP_SEC


def plan(blocks: list[SpeechBlock], cap_sec: float = CAP_SEC,
         max_rows: int | None = None) -> list[int]:
    """挑出要放進核對音檔的區塊索引(**依時間排序**回傳)。

    總長沒超過上限就全放。超過才分層:前半額度給最長的段,剩下的額度在
    「還沒被選到的段」裡依時間均勻取。

    ⚠️ **回傳一定要依時間排序**:聽的順序必須和逐字稿一致,否則對照表
    上的「原錄音時間」會忽前忽後,人根本對不回去。"""
    if not blocks:
        return []
    order = sorted(range(len(blocks)), key=lambda i: blocks[i].start)
    # ⚠️ **列數也要有上限**(2026-08-13 使用者實機回報「勾選改掛很慢」):
    # 逐列播放之後不再需要「聽多久」的上限,但幾百列的表格光是前端重排就
    # 卡得有感——那個成本與「聽多久」無關,是**列數**本身。
    if max_rows is not None and len(order) > max_rows:
        cap_sec = min(cap_sec, sum(_cost(blocks[i]) for i in order))
        long_n = max(1, max_rows // 2)
        picked = set(sorted(order, key=lambda i: -blocks[i].seconds)[:long_n])
        rest = [i for i in order if i not in picked]
        want = max_rows - len(picked)
        for k in range(want):
            picked.add(rest[min(int(k * len(rest) / max(want, 1)), len(rest) - 1)])
        return [i for i in order if i in picked]
    if sum(_cost(blocks[i]) for i in order) <= cap_sec:
        return order

    picked: set[int] = set()
    used = 0.0
    for i in sorted(order, key=lambda i: -blocks[i].seconds):
        if used + _cost(blocks[i]) > cap_sec * LONG_SHARE:
            continue
        picked.add(i)
        used += _cost(blocks[i])

    rest = [i for i in order if i not in picked]
    if rest:
        # 均勻取:先估「剩下的額度大概放得下幾段」,再等距挑。用浮點位置
        # 而不是固定 step,段數少時才不會全擠在前面
        avg = sum(_cost(blocks[i]) for i in rest) / len(rest)
        want = max(1, int((cap_sec - used) / max(avg, 0.1)))
        for k in range(want):
            i = rest[min(int(k * len(rest) / want), len(rest) - 1)]
            if i in picked or used + _cost(blocks[i]) > cap_sec:
                continue
            picked.add(i)
            used += _cost(blocks[i])
    return [i for i in order if i in picked]


def rows_for(blocks: list[SpeechBlock], picks: list[int], excerpt: int = 40) -> list[AuditRow]:
    """核對表:每一列在核對音檔裡的位置,對回原錄音的時間與內容。"""
    return [
        AuditRow(index=n, start=blocks[i].start, end=blocks[i].end,
                 text=blocks[i].text[:excerpt])
        for n, i in enumerate(picks, 1)
    ]


# 逐字稿的講者行:`**名字** (00:12:34)`(export.to_markdown 的格式;
# 與 relabel._SPEAKER_RE 同一個錨,只是這裡要連時間一起拿出來對)
_SPEAKER_LINE = re.compile(r"^\*\*(?P<name>.+?)\*\* \((\d+):([0-5]\d):([0-5]\d)\)$")


def _hms(seconds: float) -> str:
    s = int(max(seconds, 0.0))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def reassign(md_text: str, blocks: list[SpeechBlock], new_name: str) -> tuple[str, int]:
    """把指定的那幾個區塊在逐字稿上改掛給 `new_name`;回傳 (新 md, 改了幾行)。

    **靠「講者行的時間戳」定位**,不是行號:命名套用、繁化、標點都可能
    動過內文,唯一穩定的錨是 `**名字** (00:12:34)` 那一行——那也正是
    `relabel.rename` 用的錨。

    ⚠️ **同一秒可能有兩行**(0812 實測有多段起點落在同一秒),所以用
    「第幾個」而不是「第一個符合的」:逐行掃描時記住每個 (名字, 時間)
    出現到第幾次,只改要改的那一次。認錯行的話,使用者會看到另一個人的
    發言被改名,而那比不能改更糟。

    ⚠️ **檔尾診斷區塊之後不動**:那張表講的是分群結果,不是誰的發言
    (`relabel` 同此)。人工改掛之後表上的數字會與內文不一致,所以呼叫端
    要接著呼叫 `note_reassigned` 在那一塊**加註**,不在這裡偷偷改數字
    ——那張表的意義是「機器分成這樣」。"""
    if not blocks or not new_name.strip():
        return md_text, 0
    want: dict[tuple[str, str], int] = {}
    for b in blocks:
        key = (export.speaker_label(b.speaker), _hms(b.start))
        want[key] = want.get(key, 0) + 1

    seen: dict[tuple[str, str], int] = {}
    out, changed = [], 0
    in_diag = False
    for line in md_text.splitlines():
        if export.starts_diagnostics(line):
            in_diag = True
        m = None if in_diag else _SPEAKER_LINE.match(line.strip())
        if m is None:
            out.append(line)
            continue
        key = (m.group("name"), f"{int(m.group(2)):02d}:{m.group(3)}:{m.group(4)}")
        n = seen.get(key, 0)
        seen[key] = n + 1
        if n < want.get(key, 0):
            out.append(line.replace(f"**{m.group('name')}**", f"**{new_name}**", 1))
            changed += 1
        else:
            out.append(line)
    return "\n".join(out) + ("\n" if md_text.endswith("\n") else ""), changed


# 加註的抬頭。⚠️ 裡面點名標籤的寫法**只能用「名字」或 `**名字**`**——那是
# `export.diag_prose_renamer` 認得的兩種,套用名字時它要能把「講者 3」一起
# 換掉;自創第三種寫法的話,這段加註會在命名之後指向一個文件裡已經不存在
# 的標籤(那正是 DIAG_ROW_RE 註解說的「比沒有警告更糟」)
_REASSIGN_NOTE = ("> ⚠️ **這份逐字稿人工改掛過**,所以下面的數字與排序是"
                  "**機器分群當下**的結果,與內文已經對不上:")


def note_reassigned(md_text: str, labels, new_name: str, moved: int) -> str:
    """在檔尾診斷區塊加一句「這份被人工改掛過」;回傳新的 md。

    ⚠️ **這是 `reassign` 的另一半**(2026-08-15 code review 抓到:先前那半
    根本不存在)。`reassign` 刻意不動診斷表,docstring 說「由呼叫端另外
    加註」——而在 `src\\` 全域搜尋找不到任何這樣的呼叫端。後果:把講者 3
    的 13 輪改掛給別人之後,出貨的 md 仍寫著 `| 講者 3 | 38 | … |`、仍寫著
    「建議優先核對:**講者 3**」,而內文只剩 25 輪。這份 md 的既定消費者
    是 RAG,等於餵進一段自我矛盾的診斷。

    **加註而不是改數字**是刻意的:那張表的意義就是「機器分成這樣」,把它
    改成人工修正後的樣子,下游就再也看不出哪些是機器判的、哪些是人改的。

    改掛好幾次會累積成好幾條(同一個抬頭底下,依動作順序排)。"""
    if not moved or not md_text:
        return md_text
    who = "、".join(f"「{n}」" for n in labels) or "這一群"
    bullet = f"> - {who}的 {moved} 段已改掛給「{new_name}」"
    lines = md_text.splitlines()
    at = next((i for i, ln in enumerate(lines) if export.starts_diagnostics(ln)), None)
    if at is None:
        return md_text          # 沒有診斷區塊(沒給 quality)就沒有東西會對不上
    head = next((i for i in range(at + 1, len(lines))
                 if lines[i] == _REASSIGN_NOTE), None)
    if head is None:
        lines[at + 1:at + 1] = ["", _REASSIGN_NOTE, bullet]
    else:
        end = head + 1
        while end < len(lines) and lines[end].startswith("> "):
            end += 1
        lines[end:end] = [bullet]
    return "\n".join(lines) + ("\n" if md_text.endswith("\n") else "")


def _is_wav16k(path: Path) -> bool:
    try:
        with wave.open(str(path)) as w:
            return w.getframerate() == 16000 and w.getsampwidth() == 2
    except (wave.Error, OSError):
        return False


def _cut_and_join(src16k: Path, rows: list[AuditRow], dest: Path) -> None:
    """從 16k wav 依序取出各段、段間補靜音,寫成一個 wav。

    ⚠️ **用 `wave` 的隨機存取,不整檔讀進來、也不叫 ffmpeg**:
    - 整檔讀:一份 165 分鐘的錄音是 607MB(8GB 基準機不划算);
    - ffmpeg 的 atrim+concat 濾鏡:**實測 64 秒**(128 個分支讓它把整份
      音訊反覆走過)——那是使用者按下去之後的等待,不能接受。
    隨機存取只讀真正要的那幾段,實測同一份錄音 0.2 秒(2026-08-13)。"""
    with wave.open(str(src16k)) as r:
        sr, ch, sw = r.getframerate(), r.getnchannels(), r.getsampwidth()
        n_frames = r.getnframes()
        gap = b"\x00" * int(GAP_SEC * sr) * ch * sw
        with wave.open(str(dest), "wb") as w:
            w.setnchannels(ch)
            w.setsampwidth(sw)
            w.setframerate(sr)
            for row in rows:
                a = max(int((row.start - PAD_SEC) * sr), 0)
                b = min(int((row.end + PAD_SEC) * sr), n_frames)
                if b <= a:
                    continue
                r.setpos(a)
                w.writeframes(r.readframes(b - a))
                w.writeframes(gap)


def ensure_wav16k(src: Path, work_dir: Path) -> Path:
    """要剪的來源:已經是 16k wav 就直接用,否則轉一份放進 work_dir **並留著**。

    ⚠️ **留著是必要的,不能用完就丟**(2026-08-13 使用者實測抓到):
    逐段重播每點一列剪一次,而原始檔常是 m4a/mp4——每次都整檔重轉,長錄音
    要數十秒,等於那個功能不能用。而第一版更糟:`cut_one` 根本沒有這道轉檔,
    直接把 m4a 餵給 `wave.open` → `file does not start with RIFF id`
    (整批可以、單段一點就炸,因為只有 `build` 那條路有轉)。"""
    if _is_wav16k(src):
        return src
    work_dir.mkdir(parents=True, exist_ok=True)
    cached = work_dir / f"{src.stem}_16k.wav"
    if _is_wav16k(cached):
        return cached
    return audio.to_wav16k(src, work_dir)


def cut_one(src: Path, row: AuditRow, dest: Path) -> Path:
    """單獨剪出一列(核對表點某一列時重播那一段)。

    **為什麼不是在整批音檔上定位播放**:gradio 的播放器沒有「跳到第 N 秒」
    的伺服器端 API,而核對表上要點的就是「這一段再聽一次」;各剪一小段
    最直接,而且成本近乎零(隨機存取,實測 0.01 秒)。"""
    _cut_and_join(ensure_wav16k(src, dest.parent), [row], dest)
    return dest
