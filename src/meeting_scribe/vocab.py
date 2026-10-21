r"""逐字稿 md 裡的固定字串,跟著**會議語言**(`meetlang`)——段落標題、講者標籤、行內〔〕標記、
暫停那一行、跳針標記、檔尾的診斷區塊。

**動到這裡之前先讀 `docs/dev/i18n.md`。** 這是跟 iOS 版(`scribe2md-ios` 的
`App/Transcript/TranscriptVocab.swift`)共用的契約,三份一起改:
- ⚠️ **繁體中文那份一個字都不能動**:知識庫裡已經有的幾百份逐字稿都是這樣寫的,下游的攝入規則
  靠它;`tests/test_vocab.py` 逐字比對 iOS 那一份
- 英文、簡中**只換字、結構不變**:講者行照舊 `**名字**〔標記〕(00:12:34)`、標記照舊用〔〕與・、
  診斷表欄數不變——下游的正規式只要多認幾個字
- **讀回 md 的地方三種都要認得**(`ALL`、`is_unknown`、`detect`):手機版錄的英文逐字稿也會進來

⚠️ 這裡的字**不經過 `t()`**:那是畫面的介面語言,而這些字寫進檔案、跟會議語言走——英文介面的人
錄的繁中會議,逐字稿照樣是繁中。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from meeting_scribe import meetlang


@dataclass(frozen=True)
class Vocab:
    language: str
    transcript_heading: str      # 後面接標題(檔名)
    diagnostic_heading: str
    speaker_prefix: str          # 後面接 1 起的編號
    unknown: str
    mark_auto_name: str
    mark_crosstalk: str
    mark_uncertain: str
    mark_highlight: str
    highlight_legend: str
    note_prefix: str
    note_legend: str
    pause_head: str              # 後面接暫停長度
    pause_tail: str              # 暫停長度後面那段(含收尾的〕)
    name_source_auto: str
    name_source_confirmed: str
    name_source_none: str
    list_sep: str                # 「建議優先核對」的名字之間
    diag_intro: str              # {n}
    diag_limits: str
    diag_check_first: str        # {names}
    diag_table_header: str
    diag_legend: tuple[str, ...]
    unknown_note: tuple[str, ...]   # {n} {total} {median}
    marker_prefix: str           # 跳針標記的開頭
    degenerate: str              # {start} {end} {quoted}
    degenerate_phrase: str       # {phrase}
    degenerate_noise: str
    # ---- 以下是桌面版才有的(iOS 沒有「🔍 核對」改掛),不在 iOS 的契約裡 ----
    reassign_note: str           # 診斷區塊裡「這份人工改掛過」的抬頭
    reassign_bullet: str         # {who} {moved} {new}
    reassign_group: str          # 沒有標籤時的「這一群」

    def speaker_label(self, speaker: int) -> str:
        """0 起的編號 → 「講者 1」/「Speaker 1」;未知(<0)→「未知」/「Unknown」。"""
        return self.unknown if speaker < 0 else f"{self.speaker_prefix}{speaker + 1}"


# ---- 繁體中文(原文;⚠️ 一個字都不能動,見模組 docstring)-------------------------- #

ZH_TW = Vocab(
    language=meetlang.ZH_TW,
    transcript_heading="## 會議逐字稿 — ",
    diagnostic_heading="## 講者辨識診斷",
    speaker_prefix="講者 ",
    unknown="未知",
    mark_auto_name="機器辨識",
    mark_crosstalk="多人交錯",
    mark_uncertain="身分存疑",
    mark_highlight="重點",
    highlight_legend="> 〔重點〕= 開會時有人在現場按了「重點」的發言，整理時請優先處理。",
    note_prefix="> 備註：",
    note_legend="> 「備註：」= 現場標重點時與會者打的補充，不是發言內容。",
    pause_head="〔錄音在此暫停 ",
    pause_tail=",這段時間的發言沒有錄進來〕",
    name_source_auto="機器辨識",
    name_source_confirmed="人工確認",
    name_source_none="未命名",
    list_sep="、",
    diag_intro=("這份逐字稿的講者是**機器分出來的**,共 {n} 位。"
                "以下數字供下游(知識庫/RAG)判斷哪些標籤該先人工核對。"),
    diag_limits=("⚠️ **已知限制**:同一個標籤底下有可能其實是好幾個人"
                 "(發言少、或透過視訊/電話加入的與會者最容易被併在一起)。"
                 "程式**無法自動判定**某個標籤是不是混了多人——真的混了四個人的"
                 "標籤,各項一致性指標都可能比單人的還漂亮。所以下面只給數字與"
                 "排序,不下判定。"),
    diag_check_first=("**建議優先核對**:{names}——這幾個標籤的群內一致性明顯低於"
                      "本場其他人,是最可能混進別人的。"),
    diag_table_header="| 講者 | 發言輪次 | 總時長 | 群內一致性 | 名字來源 |",
    diag_legend=(
        "- **發言輪次**:這個標籤在逐字稿裡出現幾次(連續發言算一次)。",
        "- **群內一致性**:各段聲紋與該標籤平均聲紋的相似度。"
        "**只能在同一份逐字稿之內互相比**,不同錄音的數字沒有可比性。",
        "- **名字來源**:「人工確認」= 由人親自填上或改過;"
        "「機器辨識」= 聲紋比對自動填的、**沒有人確認過**"
        "(這種會認錯人,正文那幾行也標了〔機器辨識〕);"
        "「未命名」= 仍是「講者 N」,表示這個人沒有聲紋建檔。",
        "- 「未知」不列入本表:那是與任何講者都不夠像的零碎語音,"
        "本來就不代表某一個人。",
        "",
        "⚠️ **正文的行內標記**:〔機器辨識〕= 這個名字是機器猜的;"
        "〔身分存疑〕= 這個標籤的一致性明顯低於本場中位數,"
        "底下可能不只一個人;"
        "〔多人交錯〕= **那一輪落在多人快速交錯討論的區間**,"
        "當時好幾個人搶著講、機器分不開,所以那一行的講者歸屬是猜的。"
        "**引用帶標記的發言時不要斷言是誰說的。**",
    ),
    unknown_note=(
        "「**未知**」這一批共 {n} 段、{total}"
        ",單段中位長度 {median} 秒。這些語音太短或太模糊,"
        "聲紋不足以判斷是誰——多半是「對」「好」「瞭解」這類應答與插話,"
        "**它們很可能分屬好幾個不同的人**。",
        "",
        "⚠️ 所以工具**不把這一批當成一位講者**,也不會拿它登記聲紋。"
        "在介面上替「未知」命名只會改逐字稿上的文字;"
        "**不要整批給同一個名字**,除非你已經逐段聽過確認都是同一個人。",
    ),
    marker_prefix="(此段轉錄異常",
    degenerate=("(此段轉錄異常:模型在 {start}~{end} {quoted},"
                "內容不可信、已略去;需要這段內容請對照原始錄音)"),
    degenerate_phrase="重複輸出「{phrase}」",
    degenerate_noise="輸出重複雜訊",
    reassign_note=("> ⚠️ **這份逐字稿人工改掛過**,所以下面的數字與排序是"
                   "**機器分群當下**的結果,與內文已經對不上:"),
    reassign_bullet="> - {who}的 {moved} 段已改掛給「{new}」",
    reassign_group="這一群",
)

# ---- 簡體中文(iOS `TranscriptVocab.zhCN` 照抄:tw2sp 轉完再改大陸用語)--------------- #

ZH_CN = Vocab(
    language=meetlang.ZH_CN,
    transcript_heading="## 会议逐字稿 — ",
    diagnostic_heading="## 讲者识别诊断",
    speaker_prefix="讲者 ",
    unknown="未知",
    mark_auto_name="机器识别",
    mark_crosstalk="多人交错",
    mark_uncertain="身份存疑",
    mark_highlight="重点",
    highlight_legend="> 〔重点〕= 开会时有人在现场按了「重点」的发言，整理时请优先处理。",
    note_prefix="> 备注：",
    note_legend="> 「备注：」= 现场标重点时与会者打的补充，不是发言内容。",
    pause_head="〔录音在此暂停 ",
    pause_tail=",这段时间的发言没有录进来〕",
    name_source_auto="机器识别",
    name_source_confirmed="人工确认",
    name_source_none="未命名",
    list_sep="、",
    diag_intro=("这份逐字稿的讲者是**机器分出来的**,共 {n} 位。"
                "以下数字供下游(知识库/RAG)判断哪些标签该先人工核对。"),
    diag_limits=("⚠️ **已知限制**:同一个标签底下有可能其实是好几个人"
                 "(发言少、或通过视频/电话加入的与会者最容易被并在一起)。"
                 "程序**无法自动判定**某个标签是不是混了多人——真的混了四个人的"
                 "标签,各项一致性指标都可能比单人的还漂亮。所以下面只给数字与"
                 "排序,不下判定。"),
    diag_check_first=("**建议优先核对**:{names}——这几个标签的群内一致性明显低于"
                      "本场其他人,是最可能混进别人的。"),
    diag_table_header="| 讲者 | 发言轮次 | 总时长 | 群内一致性 | 名字来源 |",
    diag_legend=(
        "- **发言轮次**:这个标签在逐字稿里出现几次(连续发言算一次)。",
        "- **群内一致性**:各段声纹与该标签平均声纹的相似度。"
        "**只能在同一份逐字稿之内互相比**,不同录音的数字没有可比性。",
        "- **名字来源**:「人工确认」= 由人亲自填上或改过;"
        "「机器识别」= 声纹比对自动填的、**没有人确认过**"
        "(这种会认错人,正文那几行也标了〔机器识别〕);"
        "「未命名」= 仍是「讲者 N」,表示这个人没有声纹建档。",
        "- 「未知」不列入本表:那是与任何讲者都不够像的零碎语音,"
        "本来就不代表某一个人。",
        "",
        "⚠️ **正文的行内标记**:〔机器识别〕= 这个名字是机器猜的;"
        "〔身份存疑〕= 这个标签的一致性明显低于本场中位数,"
        "底下可能不只一个人;"
        "〔多人交错〕= **那一轮落在多人快速交错讨论的区间**,"
        "当时好几个人抢着讲、机器分不开,所以那一行的讲者归属是猜的。"
        "**引用带标记的发言时不要断言是谁说的。**",
    ),
    unknown_note=(
        "「**未知**」这一批共 {n} 段、{total}"
        ",单段中位长度 {median} 秒。这些语音太短或太模糊,"
        "声纹不足以判断是谁——多半是「对」「好」「了解」这类应答与插话,"
        "**它们很可能分属好几个不同的人**。",
        "",
        "⚠️ 所以工具**不把这一批当成一位讲者**,也不会拿它登记声纹。"
        "在界面上替「未知」命名只会改逐字稿上的文字;"
        "**不要整批给同一个名字**,除非你已经逐段听过确认都是同一个人。",
    ),
    marker_prefix="(此段转录异常",
    degenerate=("(此段转录异常:模型在 {start}~{end} {quoted},"
                "内容不可信、已略去;需要这段内容请对照原始录音)"),
    degenerate_phrase="重复输出「{phrase}」",
    degenerate_noise="输出重复杂音",
    reassign_note=("> ⚠️ **这份逐字稿人工改挂过**,所以下面的数字与排序是"
                   "**机器分群当下**的结果,与内文已经对不上:"),
    reassign_bullet="> - {who}的 {moved} 段已改挂给「{new}」",
    reassign_group="这一群",
)

# ---- English(iOS `TranscriptVocab.en` 照抄;標記照舊用〔〕)------------------------- #

EN = Vocab(
    language=meetlang.EN,
    transcript_heading="## Meeting transcript — ",
    diagnostic_heading="## Speaker diagnostics",
    speaker_prefix="Speaker ",
    unknown="Unknown",
    mark_auto_name="auto-named",
    mark_crosstalk="crosstalk",
    mark_uncertain="identity unsure",
    mark_highlight="highlight",
    highlight_legend=('> 〔highlight〕= someone at the meeting tapped "Highlight" during this turn. '
                      'Give it priority when writing minutes.'),
    note_prefix="> Note: ",
    note_legend=('> "Note:" = a comment typed by an attendee when marking the highlight. '
                 'It is not part of what was said.'),
    pause_head="〔Recording paused here for ",
    pause_tail="; nothing said during this time was recorded〕",
    name_source_auto="auto-named",
    name_source_confirmed="confirmed",
    name_source_none="unnamed",
    list_sep=", ",
    diag_intro=("The speakers in this transcript were **separated by machine**: {n} in total. "
                "The numbers below help downstream tools (knowledge base / RAG) decide which "
                "labels to check by hand first."),
    diag_limits=("⚠️ **Known limitation**: one label may actually cover several people (those who "
                 "speak little, or join by video or phone, are the most likely to be merged). The "
                 "app **cannot tell automatically** whether a label mixes several people—a label "
                 "that really mixes four people can score better on every cohesion measure than a "
                 "single person. So below are numbers and rankings only, not verdicts."),
    diag_check_first=("**Check these first**: {names}—their cohesion is clearly lower than "
                      "everyone else's in this meeting, so they are the most likely to include "
                      "someone else."),
    diag_table_header="| Speaker | Turns | Total time | Cohesion | Name source |",
    diag_legend=(
        "- **Turns**: how many times this label appears in the transcript (consecutive speech "
        "counts once).",
        "- **Cohesion**: how similar each segment's voiceprint is to the label's average "
        "voiceprint. **Compare only within the same transcript**; numbers from different "
        "recordings are not comparable.",
        '- **Name source**: "confirmed" = typed or changed by a person; "auto-named" = filled in '
        "by voiceprint matching and **not confirmed by anyone** (it can be the wrong person; those "
        'turns are also marked 〔auto-named〕); "unnamed" = still "Speaker N", meaning this person '
        "has no voiceprint on file.",
        '- "Unknown" is not listed: those are short fragments that don\'t match any speaker well '
        "enough, and they don't represent one person.",
        "",
        "⚠️ **Inline marks in the transcript**: 〔auto-named〕= the name was guessed by machine; "
        "〔identity unsure〕= this label's cohesion is clearly below the meeting's median, so it "
        "may cover more than one person; 〔crosstalk〕= **this turn falls in a stretch where "
        "several people talked over each other** and the machine couldn't separate them, so who "
        "said it is a guess. **Don't state who said a marked turn when quoting it.**",
    ),
    unknown_note=(
        '"**Unknown**" covers {n} segments, {total} in total, median length {median} s. These '
        "are too short or unclear for the voiceprint to tell who spoke—mostly replies and "
        'interjections like "right", "OK", "got it", and **they very likely come from several '
        "different people**.",
        "",
        "⚠️ So the app **does not treat them as one speaker** and never enrolls a voiceprint from "
        'them. Naming "Unknown" in the app only changes the text in the transcript; **don\'t give '
        "the whole batch one name** unless you have listened to every segment and confirmed it is "
        "the same person.",
    ),
    marker_prefix="(Transcription problem",
    degenerate=("(Transcription problem: from {start} to {end} the model {quoted}. This part is "
                "unreliable and was left out; check the original recording if you need it)"),
    degenerate_phrase='kept repeating "{phrase}"',
    degenerate_noise="produced repeated noise",
    reassign_note=("> ⚠️ **Turns in this transcript were reassigned by hand**, so the numbers and "
                   "ranking below are **what the machine clustered**, and no longer match the text:"),
    reassign_bullet='> - {moved} turns of {who} were moved to "{new}"',
    reassign_group="this group",
)

ALL = (ZH_TW, ZH_CN, EN)
_BY_LANG = {v.language: v for v in ALL}


def of(language: str) -> Vocab:
    """會議語言 → 那一套字串(認不得就是繁中)。"""
    return _BY_LANG.get(meetlang.parse(language), ZH_TW)


# ---- 讀回 md 用:三套都認 ----------------------------------------------------------- #

def is_unknown(label: str) -> bool:
    """「未知」「Unknown」(繁簡同字)。"""
    return any(label == v.unknown for v in ALL)


def speaker_number(label: str) -> int | None:
    """沒命名的講者標籤 → 0 起的編號(「講者 3」「Speaker 3」「讲者 3」→ 2);其他回 None。"""
    for v in ALL:
        if label.startswith(v.speaker_prefix):
            rest = label[len(v.speaker_prefix):]
            if rest.isdigit() and int(rest) >= 1:
                return int(rest) - 1
    return None


def is_highlight_mark(mark: str) -> bool:
    """〔重點〕〔重点〕〔highlight〕其中之一(講者行標記串裡的一個)。"""
    return any(mark == v.mark_highlight for v in ALL)


def is_note_line(line: str) -> bool:
    """重點底下的備註行(「> 備註：」「> 备注：」「> Note: 」)。"""
    return any(line.startswith(v.note_prefix) for v in ALL)


def note_text(line: str) -> str:
    """備註行去掉開頭那一段。不是備註行就原樣回傳。"""
    for v in ALL:
        if line.startswith(v.note_prefix):
            return line[len(v.note_prefix):]
    return line


def is_legend(line: str) -> bool:
    """重點與備註那兩行說明(三種語言)。"""
    return any(line in (v.highlight_legend, v.note_legend) for v in ALL)


def is_marker(text: str) -> bool:
    """跳針標記(三種語言的開頭都認)。"""
    return any(text.startswith(v.marker_prefix) for v in ALL)


_LANG_RE = re.compile(r'^language:\s*"?([A-Za-z_-]+)"?\s*$')


def detect(md_text: str) -> Vocab:
    """這份 md 是哪一套字串:先看 frontmatter 的 `language:`,沒有就看正文標題;都沒有是繁中。

    ⚠️ **frontmatter 優先**(同 iOS):正文標題可能被下游刪掉(doc2md 那條路就會砍),
    而 frontmatter 是寫給機器看的。"""
    lines = md_text.splitlines()
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() == "---":
                break
            m = _LANG_RE.match(line.strip())
            if m:
                return of(m.group(1))
    for line in lines:
        for v in ALL:
            if line.startswith(v.transcript_heading.rstrip()):
                return v
    return ZH_TW
