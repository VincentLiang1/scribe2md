"""名詞對照表:送進模型前把逐字稿裡聽錯的人名、公司名、系統名換成正確寫法(會議紀錄草稿第 6 階段)。

照手機版 `NameCorrections.swift`(scribe2md-ios 08 §8.12 改版 A)逐條搬:語音辨識把專有名詞聽成同音字,
4B 的模型不會自己改、給它詞表也常常不照用 → **送進模型前用程式在文字上取代**,逐字稿 md 本身不動。

對照表不是這個工具產生的:wiki-feeder 從 FWIKI 轉成 `name-corrections.json`,寫進 iCloud(給手機)與
OneDrive(給電腦)的 `_聲紋同步`(使用者 2026-10-29 定)。這邊只讀聲紋同步資料夾裡那一份——
**讀得到就用、讀不到就不做,沒有設定介面**。⚠️ 含真名,比照名單:不進 repo、不進知識庫,記錄檔只記條數。

一份通用(`common`)每場都套;分組(`groups`)照手機的規則,逐字稿檔名裡含分組名的才加套。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from meeting_scribe import convert

logger = logging.getLogger(__name__)

FILE_NAME = "name-corrections.json"   # 同手機版 `NameCorrections.fileName`、wiki-feeder 的 OUT_NAME
#: 對照表是攝入的 claude 寫的(不可信):太大就不讀(同 wiki-feeder 的 MAX_BYTES)
MAX_BYTES = 2_000_000


@dataclass(frozen=True)
class Entry:
    wrong: tuple[str, ...]
    right: str


@dataclass(frozen=True)
class Table:
    common: tuple[Entry, ...] = ()
    groups: tuple[tuple[str, tuple[Entry, ...]], ...] = ()

    def entries(self, title: str) -> list[Entry]:
        """通用 ＋ 名稱出現在 `title`(逐字稿檔名)裡的分組(同手機 `suggestedGroups`:沒選過的場次預設套這些)。"""
        out = list(self.common)
        for name, es in self.groups:
            if name and name in title:
                out.extend(es)
        return out


def _entries(raw) -> tuple[Entry, ...]:
    out = []
    for e in raw if isinstance(raw, list) else []:
        if not isinstance(e, dict) or not isinstance(e.get("right"), str):
            continue
        wrong = e.get("wrong")
        if isinstance(wrong, list):
            out.append(Entry(tuple(w for w in wrong if isinstance(w, str)), e["right"]))
    return tuple(out)


def parse(text: str) -> Table | None:
    """JSON → 對照表;格式不對回 None(手機版解不開也是當作沒有)。"""
    try:
        doc = json.loads(text)
    except (ValueError, RecursionError):   # 巢狀太深是 RecursionError,不是 ValueError
        return None
    if not isinstance(doc, dict):
        return None
    groups = doc.get("groups")
    groups = groups if isinstance(groups, dict) else {}
    return Table(_entries(doc.get("common")),
                 tuple(sorted((k, _entries(v)) for k, v in groups.items() if isinstance(k, str))))


def load() -> Table | None:
    """聲紋同步資料夾裡那一份;沒設定同步、檔案不在、太大、壞掉都回 None(記錄檔留一行,不打擾畫面)。"""
    from meeting_scribe import vpsync   # 惰性:它帶著 numpy,開窗路徑用不到

    folder = vpsync.folder()
    if folder is None:
        return None
    path = Path(folder) / FILE_NAME
    try:
        if not path.is_file():
            return None
        if path.stat().st_size > MAX_BYTES:
            logger.warning("名詞對照表太大(%d bytes),這次不套", path.stat().st_size)
            return None
        table = parse(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        # 雲端資料夾的佔位檔可能讀不到:這一場不套,下一場再試。⚠️ **解碼失敗是 ValueError**
        # (UnicodeDecodeError):同步到一半截在中文字中間、被存成 UTF-16/cp950,只接 OSError 的話
        # 每一份草稿都以「未預期的錯誤」失敗,而這張表沒有設定介面可以關(2026-10-30 審查)
        logger.warning("名詞對照表讀不到:%s", e)
        return None
    if table is None:
        logger.warning("名詞對照表格式不對,這次不套")
    return table


def simplified(entries: list[Entry]) -> list[Entry]:
    """簡體會議用:誤譯與正確寫法都轉成簡體(手機用 ICU Hant-Hans,這邊用 OpenCC t2s;使用者 2026-10-25 開放)。"""
    s = convert.to_simplified
    return [Entry(tuple(s(w) for w in e.wrong), s(e.right)) for e in entries]


def usable(wrong: str) -> bool:
    """一個誤譯能不能用:至少 2 個字(英數字另外只換整個字,見 `apply`)。"""
    return len(wrong.strip()) >= 2


def _ascii_word(s: str) -> bool:
    return s.isascii()


@dataclass(frozen=True)
class Prepared:
    """一場會議的對照表先算好(誤譯排序、編好的樣式),逐行 `apply` 時不必每一行重來。

    ⚠️ **不可以每一行重編**:`minutes._apply_corrections` 一行逐字稿換一次,而相異的樣式一超過 `re`
    自己的快取(512 個)每一行就全部重編——600 個樣式、3000 行要 30 秒,通用加上所有分組(1,100 多個)
    要兩分鐘,期間進度條不動、停止鈕也沒反應(2026-10-30 審查實測;真表最大的一組已經 492 個)。"""
    rights: tuple[str, ...] = ()
    patterns: tuple[tuple[re.Pattern, str], ...] = ()

    def apply(self, text: str) -> tuple[str, int]:
        return _apply(self, text)


def prepare(entries: list[Entry]) -> Prepared:
    """排好誤譯、編好樣式(規則見 `apply`)。"""
    pairs = sorted(((w, e.right) for e in entries if e.right for w in e.wrong if usable(w) and w != e.right),
                   key=lambda p: -len(p[0]))
    if not pairs:
        return Prepared()
    rights = sorted({e.right for e in entries if e.right}, key=lambda r: -len(r))
    patterns = []
    for w, r in pairs:
        if _ascii_word(w):
            pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(w) + r"(?![A-Za-z0-9])", re.I)
        else:
            pattern = re.compile(re.escape(w))
        patterns.append((pattern, r))
    return Prepared(tuple(rights), tuple(patterns))


def _apply(p: Prepared, text: str) -> tuple[str, int]:
    if not p.patterns or not text:
        return text, 0
    # 原文裡每個正確寫法出現的地方(可以互相重疊)
    correct: list[tuple[int, int]] = []
    for r in p.rights:
        k = text.find(r)
        while k >= 0:
            correct.append((k, k + len(r)))
            k = text.find(r, k + 1)

    def overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
        return max(a[0], b[0]) < min(a[1], b[1])

    def strictly_contains(a: tuple[int, int], b: tuple[int, int]) -> bool:
        return a[0] <= b[0] and b[1] <= a[1] and a != b

    taken: list[tuple[tuple[int, int], str]] = []
    for pattern, right in p.patterns:
        for m in pattern.finditer(text):
            span = m.span()
            if any(overlaps(s, span) for s, _ in taken):
                continue
            if any(overlaps(c, span) and not strictly_contains(span, c) for c in correct):
                continue
            taken.append((span, right))
    if not taken:
        return text, 0
    out = text
    for (a, b), right in sorted(taken, key=lambda x: -x[0][0]):
        out = out[:a] + right + out[b:]
    return out, len(taken)


def apply(text: str, entries: list[Entry]) -> tuple[str, int]:
    """在文字上換掉誤譯,回 (換過的文字, 換了幾處)。規則都照手機版(`4c6b95c`):
    - 長的誤譯先換(「甲乙丙」要先於「甲乙」);換過的地方不再被別條換
    - 已經寫對的不再換:一處符合如果碰到某個正確寫法、又沒有整個包住它,就跳過(「大明」→「王大明」時,
      「王大明」裡的「大明」不換)
    - 英數字的誤譯不分大小寫、只換整個字(字界看原文:「LE」不能吃掉「LEVEL」)
    ⚠️ 原本先把所有正確寫法遮起來再換,有兩個副作用(2026-10-30 審查):誤譯裡含別條的正確寫法就永遠換不掉;
    英數字的正確寫法被遮在字中間,字界跟著壞掉(「LEVEL」被換成「LMEVEL」、「SPEC」該換卻換不到)。
    同一份對照表要換很多段文字時先 `prepare` 一次(見 `Prepared`)。"""
    return prepare(entries).apply(text)


def glossary(text: str, entries: list[Entry], limit: int = 30) -> list[str]:
    """取代過的文字裡出現的正確名詞(給提示詞:「這些是專有名詞,照這樣寫」),出現多的在前,最多 `limit` 個。"""
    counts = [(r, text.count(r)) for r in {e.right for e in entries} if len(r) >= 2]
    counts = [(r, n) for r, n in counts if n > 0]
    counts.sort(key=lambda x: (-x[1], x[0]))
    return [r for r, _ in counts[:limit]]
