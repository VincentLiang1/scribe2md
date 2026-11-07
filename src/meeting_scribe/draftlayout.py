r"""會議紀錄草稿的「顯示格式」:照雲端 Claude 會議紀錄的樣子排(同 iOS `DraftLayout.swift`,使用者 2026-11-07)。

「# 會議紀錄 — 名稱」、**時間**、**與會者**(機器辨識的加 ※ 與說明)、分隔線,再來各段。
層次一律三層(使用者 2026-11-07):**段落標題不編號**(沒東西的段整段不放,編號會跳)、段落裡的每一條 1. 2.(議題也不粗體,使用者同日改)、議題底下的重點才用項目符號;待辦是「負責人｜事項｜期限」表格。表頭從**逐字稿**取(Claude 拿到的也只有逐字稿),
顯示時才讀,所以舊草稿也有、改了講者名字會跟著變。

⚠️ **整頁跟會議語言、不跟介面語言**(使用者 2026-11-07,四台一起改):中文會議在英文介面時表頭也是中文、
英文會議整頁英文(機器辨識的記號是 * 不是 ※)。語言從逐字稿讀(`Header.language`),一律 `t(…, in_=lg)`。

不是會議不硬套:1 位講者 →「內容紀錄」,只有內容摘要與重點、不寫與會者;2 位 →「對談紀錄」「對談者」;
不論幾人,沒找到東西的段(只有「沒有找到明確的…」)整段不顯示。

⚠️ **只改顯示**:`minutes.save` 存的草稿照舊「- 事項｜負責人:…｜期限:…」——`scripts/minutes_score.py` 與各平台
都照它比對。這裡排出來的是**另外存的一份**(`minutes.save_layout`,使用者 2026-11-07 選)與右邊的預覽。

⚠️ 跟 `naming.py` 同一條規矩:**不准 import 任何 UI 模組**。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from meeting_scribe import draftvocab, export, i18n, vocab
from meeting_scribe.i18n import t

CONTENT, CONVERSATION, MEETING = "content", "conversation", "meeting"


@dataclass(frozen=True)
class Attendee:
    name: str
    machine: bool      # 名字是聲紋自動填的(講者行有〔機器辨識〕)


@dataclass
class Header:
    """表頭:逐字稿的會議名稱、開始時間、長度、講者(依第一次發言的順序)。"""
    title: str | None = None
    start: datetime | None = None
    seconds: float | None = None
    speakers: int | None = None       # 檔頭 speakers(講者分析的人數);沒有就數講者行
    attendees: list[Attendee] = field(default_factory=list)
    language: str = ""                # 逐字稿的會議語言(`vocab.detect`);空字串 = 介面語言

    @property
    def format(self) -> str:
        n = self.speakers if self.speakers is not None else len(self.attendees)
        return CONTENT if n <= 1 else CONVERSATION if n == 2 else MEETING


@dataclass(frozen=True)
class Todo:
    task: str
    owner: str | None = None
    deadline: str | None = None
    time: str | None = None


# 排好的每一行:(種類, 內容)。種類 title / field(k, v) / note / divider / heading / topic(n, s) /
# item(n, s) / sub / bullet / todos([Todo]) / text
Line = tuple


def _frontmatter(md: str) -> dict[str, str]:
    lines = md.split("\n")
    if not lines or lines[0] != "---":
        return {}
    out = {}
    for line in lines[1:60]:
        if line == "---":
            break
        k, sep, v = line.partition(":")
        if sep:
            out[k.strip()] = v.strip().strip('"')
    return out


def speakers(md: str) -> int | None:
    r"""逐字稿檔頭的講者人數(產生草稿時判斷是不是一個人講的,`minutes.generate`)。
    ⚠️ 桌面版 `speakers: 0` 也可能出現(診斷表是空的),那不是「0 個人」,當作沒寫。"""
    try:
        n = int(_frontmatter(md).get("speakers", ""))
    except ValueError:
        return None
    return n if n > 0 else None


# 桌面版錄音的檔名:逐字稿檔頭沒有 recorded_at(手機版才有),開始時間只能從檔名看。兩種都認:
# 2026-11-07 以前的「錄音_20261002_1011」,與之後跟手機版一致的「未命名-261002-1011」「Untitled-261002-1011」
# (`desktop.default_stem`;舊檔名的錄音還在使用者的 output 裡)
_STEM_TIME = re.compile(r"(?<!\d)(20\d{6})_(\d{4})(?!\d)")
_STEM_TIME_SHORT = re.compile(r"(?<!\d)(\d{6})-(\d{4})(?!\d)")


def header(md: str, name: str = "", seconds: float | None = None,
           modified: datetime | None = None) -> Header:
    """讀逐字稿 md 的表頭。桌面版逐字稿檔頭沒有 recorded_at/duration_seconds(手機版才有),那時
    (使用者 2026-11-07 裁定):長度用 `seconds`(呼叫端用 `minutes.duration_sec` 算最後一句的時間;這裡不
    import minutes,它 import 這裡),開始時間用逐字稿檔案的修改時間 `modified`——但檔名有「_20261002_1011」
    這種錄音時間就先用檔名:命名、重設講者都會重寫逐字稿,修改時間會變成改名那一刻。"""
    fm = _frontmatter(md)
    h = Header(speakers=speakers(md), language=vocab.detect(md).language)
    if fm.get("recorded_at"):
        try:
            h.start = datetime.fromisoformat(fm["recorded_at"]).astimezone()
        except ValueError:
            pass
    for pattern, fmt in ((_STEM_TIME, "%Y%m%d%H%M"), (_STEM_TIME_SHORT, "%y%m%d%H%M")):
        if h.start is None and (m := pattern.search(name)):
            try:
                h.start = datetime.strptime(m[1] + m[2], fmt)
            except ValueError:
                pass
    if h.start is None:
        h.start = modified
    try:
        h.seconds = float(fm["duration_seconds"])
    except (KeyError, ValueError):
        h.seconds = seconds if seconds else None
    seen: set[str] = set()
    autos = [v.mark_auto_name for v in vocab.ALL]
    for line in md.split("\n"):
        if h.title is None:
            v = next((v for v in vocab.ALL if line.startswith(v.transcript_heading)), None)
            if v is not None:
                h.title = line[len(v.transcript_heading):].strip()
                continue
        # ⚠️ 講者行全 repo 只有一份正則(`export.SPEAKER_LINE_RE`),取值一律用具名群組
        m = export.SPEAKER_LINE_RE.match(line)
        if not m or vocab.is_unknown(m["name"]) or m["name"] in seen:
            continue
        seen.add(m["name"])
        h.attendees.append(Attendee(m["name"], any(a in autos for a in export.split_marks(m))))
    return h


def _cjk(s: str) -> bool:
    return any("一" <= c <= "鿿" for c in s)


def lang_of(header: Header | None) -> str:
    """草稿整頁用的語言:逐字稿的會議語言;找不到逐字稿(或讀不出)就是介面語言。"""
    return (header.language if header else "") or i18n.current()


def display_name(name: str, lg: str = "") -> str:
    """「小林 總務組」→「小林（總務組）」(Claude 的寫法);英文名字中間的空白不動,沒命名的「講者 2」不拆。"""
    if not _cjk(name) or " " not in name:
        return name
    who, role = name.split(" ", 1)
    role = role.strip()
    # 括號跟草稿的語言(英文不能出現全形標點,使用者 2026-11-07)
    return name if not role or role.isdigit() else t("{who}（{role}）", in_=lg, who=who, role=role)


def _header_lines(h: Header) -> list[Line]:
    title = h.title or ""
    fmt, lg = h.format, lang_of(h)
    if fmt == MEETING:
        head = t("會議紀錄 — {v}", in_=lg, v=title) if title else t("會議紀錄", in_=lg)
    elif fmt == CONVERSATION:
        head = t("對談紀錄 — {v}", in_=lg, v=title) if title else t("對談紀錄", in_=lg)
    else:
        head = t("內容紀錄 — {v}", in_=lg, v=title) if title else t("內容紀錄", in_=lg)
    out: list[Line] = [("title", head)]
    minutes_ = max(1, round(h.seconds / 60)) if h.seconds else None
    if h.start is not None:
        when = h.start.strftime("%Y-%m-%d %H:%M")
        out.append(("field", t("時間", in_=lg),
                    t("{when} 起，約 {n} 分鐘", in_=lg, when=when, n=minutes_) if minutes_ else when))
    if h.attendees and fmt != CONTENT:   # 一個人講的(影片、演講)不寫與會者
        key = (t("對談者（依逐字稿標籤）", in_=lg) if fmt == CONVERSATION
               else t("與會者（依逐字稿標籤）", in_=lg))
        # 機器辨識的記號:英文是 *(外國人不熟 ※,使用者 2026-11-07)
        star = t("※", in_=lg)
        out.append(("field", key, t("、", in_=lg).join(display_name(a.name, lg) + (star if a.machine else "")
                                                      for a in h.attendees)))
        if any(a.machine for a in h.attendees):
            out.append(("note", t("※ 標示者為機器聲紋辨識、未經人工確認，身分待確認。", in_=lg)))
    out.append(("divider",))
    return out


def _key(heading: str) -> str:
    """標題比對只看「(」以前:待辦的標題後面有「(約略時間,請對照逐字稿)」;顯示也只留這段。"""
    h = heading.strip("# ").strip()
    return re.split(r"[(（]", h, maxsplit=1)[0].strip()


_V = draftvocab.ALL
_TODO_KEYS = {_key(h) for v in _V for h in (v.todos_heading, v.whole_todos_heading, v.about_me_heading)}
_TOPIC_KEYS = {_key(v.topics_heading) for v in _V}
_DECISION_KEYS = {_key(h) for v in _V for h in (v.decisions_heading, v.questions_heading)}
_SUMMARY_KEYS = {_key(h) for v in _V for h in (v.summary_heading, v.full_summary_heading)}
# 「沒有找到明確的…」:那段沒東西(三段的待辦那句帶著「- 」)
_EMPTY = {s.removeprefix("- ") for v in _V for s in (v.no_todos, v.no_decisions, v.no_questions)}
_OWNERS = {v.owner for v in _V}
_DEADLINES = {v.deadline for v in _V}


_TOPIC_POINT = re.compile(r"^(.+?[(（]\d{1,2}:\d{2}(?::\d{2})?[)）])[:：]\s*(.+)$")


def lines(md: str, header: Header | None = None) -> list[Line]:
    """草稿 md → 排好的行。`header` 是 None(找不到逐字稿)就不放表頭、照會議格式。"""
    # 先分段,再決定哪幾段要顯示
    sections: list[dict] = []
    lead: list[Line] = []
    for raw in md.split("\n"):
        line = raw.strip().replace("**", "")
        if line.startswith("#"):
            k = _key(line)
            kind = ("todo" if k in _TODO_KEYS else "topic" if k in _TOPIC_KEYS
                    else "decision" if k in _DECISION_KEYS else "plain")
            sections.append({"key": k, "kind": kind, "body": [], "found": False})
            continue
        if not line:
            continue
        if not sections:
            lead.append(("text", line))
            continue
        sec = sections[-1]
        is_item = line[:2] in ("- ", "* ", "• ")
        item = line[2:].strip() if is_item else line
        if item in _EMPTY:
            continue
        sec["found"] = True
        body = sec["body"]
        if not is_item:
            body.append(("text", line))
            continue
        n = sum(1 for x in body if x[0] in ("topic", "item")) + 1
        if sec["kind"] == "todo":
            if body and body[-1][0] == "todos":
                body[-1][1].append(todo_parts(item))
            else:
                body.append(("todos", [todo_parts(item)]))
        elif sec["kind"] == "topic":
            # 「議題(00:12:34):第一個重點」(`minutes._compose`):重點是第三層
            m = _TOPIC_POINT.match(item)
            body.append(("topic", n, m[1] if m else item))
            if m:
                body.append(("sub", m[2]))
        elif sec["kind"] == "decision":
            body.append(("item", n, item))
        else:
            body.append(("bullet", item))
    fmt = header.format if header else MEETING
    lg = lang_of(header)
    out = (_header_lines(header) if header else []) + lead
    for sec in sections:
        if not sec["found"]:
            continue
        # 一個人講的:決議、待辦、未決、與我相關都不放(舊草稿也一樣)
        if fmt == CONTENT and sec["kind"] in ("todo", "decision"):
            continue
        name = sec["key"]
        if fmt == CONTENT and name in _SUMMARY_KEYS:
            name = t("內容摘要", in_=lg)
        elif fmt == CONTENT and name in _TOPIC_KEYS:
            name = t("內容重點", in_=lg)
        out.append(("heading", name))
        out += sec["body"]
    return out


def _cell(s: str, lg: str) -> str:
    return s.replace("|", t("／", in_=lg)).replace("\n", " ")   # 英文不能出現全形斜線(使用者 2026-11-07)


def markdown(md: str, header: Header | None = None) -> str:
    """排好的 Markdown(另存的那一份,同 iOS `shareText`):標題、粗體欄位、分隔線、各段、待辦表格。"""
    out: list[str] = []
    lg = lang_of(header)
    for line in lines(md, header):
        kind = line[0]
        if kind == "title":
            out += ["# " + line[1], ""]
        elif kind == "field":
            out.append(f"**{line[1]}**{t('：', in_=lg)}{line[2]}  ")   # 行尾兩個空白:Markdown 換行,不併成一段
        elif kind == "note":
            out.append(line[1])
        elif kind == "divider":
            out += ["", "---"]
        elif kind == "heading":
            out += ["", "## " + line[1], ""]
        elif kind in ("topic", "item"):
            out.append(f"{line[1]}. {line[2]}")
        elif kind == "sub":
            out.append("   - " + line[1])   # 三個空白:接在「1. 」底下才是巢狀清單
        elif kind == "bullet":
            out.append("- " + line[1])
        elif kind == "todos":
            tbc = t("待確認", in_=lg)
            out += [f"| {t('負責人', in_=lg)} | {t('事項', in_=lg)} | {t('期限', in_=lg)} |", "|---|---|---|"]
            out += [f"| {_cell(x.owner or tbc, lg)} | {_cell(x.task + (f'({x.time})' if x.time else ''), lg)} "
                    f"| {_cell(x.deadline or tbc, lg)} |" for x in line[1]]
        else:
            out.append(line[1])
    return "\n".join(out).strip()


_TIME_TAIL = re.compile(r"[(（](\d{1,2}:\d{2}(?::\d{2})?)[)）]\s*$")
_PAREN_TAIL = re.compile(r"[(（]([^()（）]+)[)）]\s*$")


def todo_parts(item: str) -> Todo:
    """拆一條待辦:「事項｜負責人:小林｜期限:待確認(00:12:34)」、「事項(負責人:小林)(00:12:34)」(三段的舊格式)、
    「事項(00:12:34)」。認不出的段照舊接回事項,不丟字。"""
    s, time_ = item, None
    if m := _TIME_TAIL.search(s):
        time_, s = m[1], s[:m.start()].strip()
    found: dict[str, str] = {}

    def labeled(part: str) -> bool:
        m = re.search(r"[:：]", part)
        if not m:
            return False
        label, value = part[:m.start()].strip(), part[m.end():].strip()
        if not value:
            return False
        if label in _OWNERS:
            found["owner"] = value
            return True
        if label in _DEADLINES:
            found["deadline"] = value
            return True
        return False

    parts = s.split("｜")
    if len(parts) > 1:
        s = parts[0].strip()
        for p in parts[1:]:
            if not labeled(p):
                # 認不得的段接回事項:英文內容用半形「 / 」(英文不出現全形字,使用者 2026-11-07;同 iOS 85b3eca)
                s += "｜" + p if _cjk(s + p) else " / " + p.strip()
    elif (m := _PAREN_TAIL.search(s)) and labeled(m[1]):
        s = s[:m.start()].strip()
    return Todo(s, found.get("owner"), found.get("deadline"), time_)
