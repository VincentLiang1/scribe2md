r"""錄音中按「重點」→ 逐字稿標〔重點〕(2026-10-17 使用者:照手機版做到電腦版)。

**規則逐條照抄 `scribe2md-ios` 的 `App/Transcript/Highlights.swift`**,數字也一樣:
md 會跟手機版的進同一個知識庫,同一場會議在兩邊標出來的範圍不一樣,下游就分不出
是誰對(md 格式是跨專案契約,見 `docs/spec/09` §9.3d)。

按下時重點多半已經講完了,所以標的是「按下那一刻正在講、或剛講完的那位講者的這一輪」:
往回到這一輪的開頭(最多 `MAX_BACK` 秒),往後到換人或停頓(最多 `MAX_FORWARD` 秒)。
換人就是重點結束,不用另外判斷。⚠️ **錄音中只記按下的時間**,標在哪一段是出 md 時才
算:重新分群、重設講者都會跟著重算——所以時間要存下來(`sidecar_path`),不能只留
md 上的標記。

⚠️ **這個模組不准 import 任何 UI 模組**(同 `naming.py`):判準是「換一套 UI 要不要改」。

兩種粒度共用同一支 `_region`:
- 收尾時是**逐句**(`SpokenSegment`;`apply`),跟手機版一樣可以把一輪切成兩行;
- 重新分群、重設講者時只剩 md,是**逐行**(`rewrite_md`):每一行講者行就是一個單位。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from meeting_scribe import export, vocab
from meeting_scribe.types import UNKNOWN_SPEAKER, SpokenSegment

logger = logging.getLogger(__name__)

# 往回最多標幾秒:一個人連講十分鐘時,不把整段都標成重點
MAX_BACK = 120.0
# 按下時這一輪還沒講完:往後最多再標幾秒
MAX_FORWARD = 60.0
# 兩次按下相隔這麼近算同一下(連按、手抖)
DOUBLE_TAP = 5.0


@dataclass(frozen=True)
class Note:
    """一則備註:掛在哪一下重點(`normalized` 之後的那一下)、內容。"""
    at: float
    text: str


@dataclass
class Highlights:
    """一場會議的重點:按下的時間(錄音時間軸上的秒數,暫停的那段不算)與備註。

    欄位名照手機版 `RecordingStore.Meta` 的 `highlights` / `highlightNotes`,落地也用
    那兩個鍵——哪天要互相讀,不必再對一次。"""
    times: list[float] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.times)

    def marks(self) -> list[float]:
        """畫面上一下一張卡片:連按併成一下(`normalized`)。"""
        return normalized(self.times)

    def add(self, t: float) -> float:
        """按了一下;回這一下在卡片上算哪一張(連按時是前面那張)。"""
        self.times.append(t)
        return owner(t, self.marks())

    def remove(self, t: float) -> None:
        """刪掉按錯的那一下(連按併成的那一下整組刪),備註一起刪。"""
        self.times = [x for x in self.times if not owns(t, x)]
        self.set_note("", [t])

    def remove_group(self, group: list[float]) -> None:
        """刪掉一組(標到同一段的幾下,見 `groups`);備註一起刪。"""
        self.times = [x for x in self.times if not any(owns(g, x) for g in group)]
        self.set_note("", group)

    def note_of(self, group: list[float]) -> str | None:
        """某一組重點的備註(卡片顯示用);幾則用全形分號接起來。"""
        texts = [c for n in self.notes if any(owns(g, n.at) for g in group)
                 if (c := clean(n.text))]
        return "；".join(texts) if texts else None

    def set_note(self, text: str, group: list[float]) -> None:
        """改一組重點的備註:那組原有的備註都換掉,記在第一下;空白 = 刪掉備註。"""
        if not group:
            return
        notes = [n for n in self.notes if not any(owns(g, n.at) for g in group)]
        # ⚠️ **那一組已經刪掉了就不收**:命名卡按 ✕ 時,那一列的輸入格被拆掉時會補送一次
        # <FocusOut>,把舊的字當成新備註寫回來——留下一則掛在不存在的重點上的孤兒
        alive = any(owns(g, t) for g in group for t in self.times)
        if alive and (c := clean(text)) is not None:
            notes.append(Note(group[0], c))
        self.notes = notes


def owns(t: float, at: float) -> bool:
    """`at` 屬於按下時間 `t` 的那一下(連按併成一下時,記的是最早那下)。"""
    return t <= at < t + DOUBLE_TAP


def owner(at: float, marks: list[float]) -> float:
    """`at` 落在哪一下(`marks` 是 `normalized` 過的);都不是就回自己。"""
    for t in marks:
        if owns(t, at):
            return t
    return at


def clean(s: str) -> str | None:
    """備註整理成一行;空的不算。"""
    t = " ".join(s.splitlines()).strip()
    return t or None


def normalized(times: list[float]) -> list[float]:
    """按下時間整理:排序、連按的併成一下。"""
    out: list[float] = []
    for t in sorted(times):
        if not out or t - out[-1] >= DOUBLE_TAP:
            out.append(t)
    return out


@dataclass(frozen=True)
class _Unit:
    """`_region` 看得懂的一個單位(一句,或 md 的一行講者行)。"""
    start: float
    end: float
    speaker: object        # 收尾時是編號,md 那條路是顯示名稱
    talk: bool             # 「未知」的插話與跳針標記不算一輪
    pause_before: bool


def _region(t: float, units: list[_Unit]) -> list[int]:
    """一次按下標到哪幾個單位。未知(「對」「好」這類插話)不算一輪,往前找真正在講的人。"""
    b = next((i for i in range(len(units) - 1, -1, -1)
              if units[i].start <= t and units[i].talk), None)
    if b is None:
        return []
    who = units[b].speaker
    # 同一輪:同一位講者、中間沒有錄音暫停(未知的插話夾在中間不算換人)
    lo = b
    while lo > 0 and not units[lo].pause_before:
        p = lo - 1
        if units[p].talk and units[p].speaker != who:
            break
        if units[p].end <= t - MAX_BACK:
            break
        lo = p
    hi = b
    while (hi + 1 < len(units) and not units[hi + 1].pause_before
           and units[hi + 1].start <= t + MAX_FORWARD):
        n = hi + 1
        if units[n].talk and units[n].speaker != who:
            break
        hi = n
    return [i for i in range(lo, hi + 1) if units[i].speaker == who]


def _group_regions(times: list[float], units: list[_Unit]) -> list[tuple[list[float], set[int]]]:
    r"""按下時間整理後,標到的單位有重疊的併成一組。

    (手機版 2026-10-15 使用者:刪了一個重點、逐字稿沒變——同一輪按兩下,兩下標的是
    同一段,只刪一下另一下照樣標著。)畫面上一組一列,刪就整組刪。"""
    out: list[tuple[list[float], set[int]]] = []
    for t in normalized(times):
        r = set(_region(t, units))
        if out and r and not out[-1][1].isdisjoint(r):
            out[-1][0].append(t)
            out[-1][1].update(r)
        else:
            out.append(([t], r))
    return out


def _units_of(spoken: list[SpokenSegment]) -> list[_Unit]:
    return [_Unit(s.start, s.end, s.speaker,
                  s.speaker != UNKNOWN_SPEAKER
                  and not vocab.is_marker(s.text),
                  bool(s.pause_before))
            for s in spoken]


def groups(h: Highlights, spoken: list[SpokenSegment]) -> list[list[float]]:
    """哪幾下按的是同一段(畫面上一組一列)。"""
    return [g for g, _ in _group_regions(h.times, _units_of(spoken))]


def apply(h: Highlights, spoken: list[SpokenSegment]) -> list[SpokenSegment]:
    r"""每一組重點 → 把那一輪的句子標成 highlight;備註掛在那一組標到的最後一句。

    md 裡備註因此出現在那一輪(或跨過插話的最後一輪)下面。"""
    out = list(spoken)
    for times, idx in _group_regions(h.times, _units_of(spoken)):
        for i in idx:
            out[i] = replace(out[i], highlight=True)
        texts = [c for n in h.notes if any(owns(t, n.at) for t in times)
                 if (c := clean(n.text))]
        if idx and texts:
            last = max(idx)
            out[last] = replace(out[last], notes=out[last].notes + tuple(texts))
    return out


# ---- 只剩 md 的那條路(重新分群、重設講者裡改重點)----

def _md_units(lines: list[str]) -> tuple[list[int], list[_Unit]]:
    """md 的講者行 → (行號, 單位)。終點是下一行講者行的起點(md 只有起點)。"""
    rows: list[int] = []
    starts: list[float] = []
    names: list[str] = []
    talk: list[bool] = []
    pause: list[bool] = []
    pending_pause = False
    for i, line in enumerate(lines):
        if export.starts_diagnostics(line):
            break
        if export.is_pause_line(line):
            pending_pause = True
            continue
        m = export.SPEAKER_LINE_RE.match(line.strip())
        if m is None:
            continue
        rows.append(i)
        starts.append(int(m.group("h")) * 3600 + int(m.group("m")) * 60 + int(m.group("s")))
        names.append(m.group("name"))
        body = next((x for x in lines[i + 1:] if x.strip()), "")
        # ⚠️ 三種會議語言的「未知」與跳針標記都要認(英文逐字稿是「Unknown」)
        talk.append(not vocab.is_unknown(m.group("name"))
                    and not vocab.is_marker(body))
        pause.append(pending_pause)
        pending_pause = False
    units = [_Unit(st, starts[k + 1] if k + 1 < len(starts) else st, names[k],
                   talk[k], pause[k])
             for k, st in enumerate(starts)]
    return rows, units


def md_groups(h: Highlights, md_text: str) -> list[list[float]]:
    """同 `groups`,但只有 md 可看(重設講者那一頁)。"""
    _rows, units = _md_units(md_text.splitlines())
    return [g for g, _ in _group_regions(h.times, units)]


def from_md(md_text: str) -> Highlights:
    r"""沒有重點檔時,從 md 自己的〔重點〕推回按下的時間與備註(**近似**)。

    手機版產的 md 沒有 `.重點.json`、使用者也可能把那個檔刪了;重新分群會重寫每一行
    講者行,不推回來的話重點就整批消失。同一位講者連續標著〔重點〕的幾行(中間只隔著
    未知的插話)算一組、一組算一下,時間取那一組**最後一行的起點**(按下那一刻一定在講
    它),備註取那一組底下的。⚠️ 只是近似:原本的按下時間可能更晚,往回兩分鐘那條邊界
    會稍微不同。"""
    lines = md_text.splitlines()
    rows, units = _md_units(lines)
    found: list[tuple[int, list[str]]] = []     # (那一組最後一行, 那一組的備註)
    prev: int | None = None
    for k, r in enumerate(rows):
        m = export.SPEAKER_LINE_RE.match(lines[r].strip())
        if not any(vocab.is_highlight_mark(x)
                   for x in (m.group("marks") or "").split(export.MARK_SEP)):
            continue
        end = rows[k + 1] if k + 1 < len(rows) else len(lines)
        notes = []
        for x in lines[r + 1:end]:
            if export.starts_diagnostics(x):
                break
            if is_note_line(x) and (c := clean(vocab.note_text(x))):
                notes.append(c)
        same = (prev is not None and units[k].speaker == units[prev].speaker
                and not any(units[j].pause_before for j in range(prev + 1, k + 1))
                and not any(units[j].talk for j in range(prev + 1, k)))
        if same:
            found[-1] = (k, found[-1][1] + notes)
        else:
            found.append((k, notes))
        prev = k
    out = Highlights()
    for k, notes in found:
        t = units[k].start
        out.times.append(t)
        out.notes += [Note(t, n) for n in notes]
    return out


@dataclass(frozen=True)
class Item:
    """畫面上一列重點(重設講者那一頁;同手機版單場頁「現場標記的重點」)。"""
    group: list[float]      # 標到同一段的那幾下(刪就整組刪)
    name: str               # 那一輪是誰(標到的第一行)
    excerpt: str            # 那段話的開頭


def md_items(h: Highlights, md_text: str, width: int = 30) -> list[Item]:
    """每一組重點 → 一列:誰、那段話的開頭(`width` 個字)。沒標到任何一行的那一下也列出來(可以刪)。"""
    lines = md_text.splitlines()
    rows, units = _md_units(lines)
    out: list[Item] = []
    for times, idx in _group_regions(h.times, units):
        if not idx:
            out.append(Item(times, "", ""))
            continue
        k = min(idx)
        end = rows[k + 1] if k + 1 < len(rows) else len(lines)
        body = "".join(x.strip() for x in lines[rows[k] + 1:end]
                       if x.strip() and not x.startswith(">") and not export.is_pause_line(x)
                       and not export.starts_diagnostics(x))
        out.append(Item(times, str(units[k].speaker),
                        body[:width] + ("…" if len(body) > width else "")))
    return out


def is_note_line(line: str) -> bool:
    """重點底下的備註行(三種會議語言都認,見 `vocab.is_note_line`)。"""
    return vocab.is_note_line(line)


def rewrite_md(md_text: str, h: Highlights) -> str:
    r"""照 `h` 重標整份 md 的〔重點〕、備註與兩行說明,**內文一個字都不動**。

    重新分群(講者換了,一輪的邊界跟著換)與重設講者裡刪重點、改備註都走這裡。
    ⚠️ **先全部拆掉再照時間重算**,不是在舊的上面加減:舊標記講的是舊的講者歸屬。
    ⚠️ 單位是**一行講者行**:收尾時逐句切開的那幾行(手機版同樣會切)照樣留著,
    所以重算的粒度跟原本一樣細。"""
    # ⚠️ 重標時照**這份 md 的**會議語言寫(英文逐字稿是〔highlight〕與「> Note: 」)
    v = vocab.detect(md_text)
    lines = _strip(md_text.splitlines())
    rows, units = _md_units(lines)
    marked: set[int] = set()
    notes_at: dict[int, list[str]] = {}
    for times, idx in _group_regions(h.times, units):
        marked |= idx
        texts = [c for n in h.notes if any(owns(t, n.at) for t in times)
                 if (c := clean(n.text))]
        if idx and texts:
            notes_at.setdefault(max(idx), []).extend(texts)
    row_of = {r: k for k, r in enumerate(rows)}
    out: list[str] = []
    open_k: int | None = None       # 目前這一輪(備註要接在它的內文後面)
    for i, line in enumerate(lines):
        diag = export.starts_diagnostics(line)
        k = row_of.get(i)
        if open_k is not None and (diag or k is not None or export.is_pause_line(line)):
            out = _flush_notes(out, notes_at.get(open_k, []), v)
            open_k = None
        if diag:
            out.extend(lines[i:])
            break
        if k is None:
            out.append(line)
            continue
        open_k = k
        m = export.SPEAKER_LINE_RE.match(line.strip())
        marks = [x for x in (m.group("marks") or "").split(export.MARK_SEP)
                 if x and not vocab.is_highlight_mark(x)]
        if k in marked:
            marks.append(v.mark_highlight)
        secs = int(m.group("h")) * 3600 + int(m.group("m")) * 60 + int(m.group("s"))
        out.append(export.speaker_line(m.group("name"), secs, marks))
    else:
        if open_k is not None:
            out = _flush_notes(out, notes_at.get(open_k, []), v)
    out = _insert_legends(out, bool(marked), bool(notes_at), v)
    return "\n".join(out) + ("\n" if md_text.endswith("\n") else "")


def _strip(lines: list[str]) -> list[str]:
    r"""拆掉舊的備註行與兩行說明(連同說明後面跟著加的那一行空白)。

    ⚠️ **只拆自己加的東西**:別的空行一行都不動(內文的排版是使用者的)。"""
    out: list[str] = []
    skip_blank = False
    for x in lines:
        if skip_blank and not x.strip():
            skip_blank = False
            continue
        skip_blank = False
        if vocab.is_legend(x):
            skip_blank = True
            continue
        if is_note_line(x):
            continue
        out.append(x)
    return out


def _flush_notes(out: list[str], texts: list[str], v: "vocab.Vocab" = vocab.ZH_TW) -> list[str]:
    """把備註接在目前這一輪內文的最後一行之後(跳過尾端的空行)。"""
    if not texts:
        return out
    tail = len(out)
    while tail > 0 and not out[tail - 1].strip():
        tail -= 1
    return out[:tail] + [v.note_prefix + t for t in texts] + out[tail:]


def _insert_legends(lines: list[str], highlight: bool, notes: bool,
                    v: "vocab.Vocab" = vocab.ZH_TW) -> list[str]:
    r"""兩行說明放在逐字稿標題底下(同 `export.to_markdown`);沒有標題(批次那條路)就放在
    第一行講者行前面。"""
    add: list[str] = []
    if highlight:
        add += [v.highlight_legend, ""]
    if notes:
        add += [v.note_legend, ""]
    if not add:
        return lines
    for i, x in enumerate(lines):
        if any(x.startswith(w.transcript_heading) for w in vocab.ALL):
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            return lines[:i + 1] + [""] + add + lines[j:]
        if export.SPEAKER_LINE_RE.match(x.strip()):
            return lines[:i] + add + lines[i:]
    return lines


# ---- 落地:md 旁邊的同名檔 ----

def sidecar_path(md_path: Path) -> Path:
    """`會議.md` → `會議.重點.json`(同 `diarize.features_path` 的「同層同名」)。"""
    return md_path.with_name(f"{md_path.stem}.重點.json")


def to_json(h: Highlights) -> str:
    return json.dumps({"highlights": h.times,
                       "highlightNotes": [{"at": n.at, "text": n.text} for n in h.notes]},
                      ensure_ascii=False, indent=1)


def from_json(text: str) -> Highlights:
    data = json.loads(text)
    return Highlights(
        times=[float(t) for t in data.get("highlights") or []],
        notes=[Note(float(n["at"]), str(n["text"])) for n in data.get("highlightNotes") or []],
    )


def load(path: Path) -> Highlights:
    r"""讀不到或壞掉就當作沒有重點(記一行,不擋):逐字稿本身照樣能用,而這個檔只是
    讓重點跟著重算——為了它擋住整個重設講者,比少幾個標記更糟。"""
    try:
        return from_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Highlights()
    except (OSError, ValueError, KeyError, TypeError) as e:
        logger.warning("重點檔讀不了,當作沒有重點:%s(%s)", path, e)
        return Highlights()


def save(path: Path, h: Highlights) -> None:
    """寫進暫存檔再換名(錄音中每按一下都寫,寫到一半當機不能留下半個檔);沒有重點就刪檔。"""
    if not h.times:
        path.unlink(missing_ok=True)
        return
    part = path.with_name(path.name + ".part")
    part.write_text(to_json(h), encoding="utf-8")
    os.replace(part, path)
