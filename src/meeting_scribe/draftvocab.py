r"""會議紀錄草稿的提示詞與草稿檔的固定字串,跟著**會議語言**(docs/dev/i18n.md;同 iOS `DraftVocab`)。

用哪一份由逐字稿 md 的語言決定(`vocab.detect`),**不看介面語言**:英文介面的人錄台灣中文會議,
草稿照樣寫繁中。

⚠️ **繁中那份照抄 2026-10-21 以前 `minutes.py` 的字,一字不改**(那是 Windows 與 Mac 實測調過的;
跟 iOS 的繁中提示詞**不一樣**,不要拿 iOS 那份來「統一」)。簡中由 OpenCC tw2sp 轉、再把「繁體中文
(台灣用語)」換成「简体中文」(同 iOS);英文照著翻,能沿用 iOS 英文措辭的地方照抄。英文與簡中的
品質要等真實會議實測。

⚠️ **純資料,不准 import 任何 UI 模組、也不准呼叫 `t()`**:這些字是寫進檔案、送進模型的,跟介面語言
無關(`minutes_worker` 子行程也 import 它)。
"""
from __future__ import annotations

from dataclasses import dataclass

from meeting_scribe import meetlang


@dataclass(frozen=True)
class DraftVocab:
    language: str
    # ---- 提示詞 ---- #
    map_instructions: str          # 一段逐字稿 → 重點與一條待辦(`llm-notes`)
    highlight_instructions: str    # 現場標的一個重點 → 一句話
    merge_instructions: str        # 重點太長時分批合併
    summary_instructions: str      # 整場摘要
    topics_instructions: str       # 主要議題
    notes_format: str              # Qwen 那條:`@Guide` 的字寫成提示詞的一段(`minutes_worker`)
    marked_label: str              # 摘要輸入:現場標記的重點
    notes_label: str               # 摘要輸入:各時段重點
    attendee_note: str             # 重點的提示裡接備註的那句開頭
    note_sep: str                  # 備註之間
    # ---- 草稿檔 ---- #
    summary_heading: str
    topics_heading: str
    todos_heading: str
    highlights_heading: str
    no_todos: str
    notice_apple: str              # 草稿檔第一行(Apple Intelligence 整理的)
    notice_local: str              # 草稿檔第一行(Qwen 那兩條)
    source_label: str              # 「> 來源:檔名」
    title_suffix: str              # 「# 檔名 會議紀錄草稿」
    # ---- 長度 ---- #
    #: 分段與合併的字數倍數:英文一個 token 約 4 個字母,中文一個字約 1.5 token(同 iOS `charScale`)
    char_scale: int
    #: 待辦的合理長度(`minutes.looks_like_task`;同 iOS `taskChars`)
    task_chars: tuple[int, int]


ZH_TW = DraftVocab(
    language=meetlang.ZH_TW,
    map_instructions=(
        "你是會議記錄助理。使用者會給你一段會議逐字稿(語音辨識的結果,有錯字、標點少,"
        "每行開頭是講者,可能是「未知」或「講者 N」)。\n"
        "用繁體中文(台灣用語)整理這一段的重點與待辦。只寫逐字稿裡真的有的,不要推測。"
    ),
    # ⚠️ **備註要一起給**(2026-10-17 使用者:「標示的重點應該也要隨著逐字稿送入」):備註是
    # 現場打的「為什麼重要、數字是多少」,手機版跳過了所有 `>` 開頭的行、備註因此沒送進模型
    highlight_instructions=(
        "你是會議記錄助理。使用者會給你一段會議發言(語音辨識的結果,有錯字、標點少,開頭是講者),"
        "與會者在現場把它標成重點,可能還附了與會者自己打的備註。\n"
        "用繁體中文(台灣用語)寫一句話講這段的重點(數字、結論、誰要做什麼),備註說的優先;"
        "只寫發言與備註裡有的,不要推測;不要加標題、條列或「這段在說」。"
    ),
    merge_instructions=(
        "你是會議記錄助理。使用者會給你幾段會議重點。用繁體中文(台灣用語)合併成最多 6 點、"
        "每點一句,合併重複的,只寫重點裡有的。"
    ),
    # 摘要與議題分開問、標題由程式加:一次要兩段時它不照格式(實測標題整個不見),
    # 給它寫開頭又會捏造日期、主持人
    summary_instructions=(
        "你是會議記錄助理。使用者會給你一場會議各時段的重點。用繁體中文(台灣用語)寫 3 句以內的摘要,"
        "講整場最重要的目標、數字與方向。\n"
        "只寫重點裡有的,不要推測;不要寫日期、地點、主持人、出席者;不要每句都寫「某某人認為」;"
        "不要加標題或條列。"
    ),
    topics_instructions=(
        "你是會議記錄助理。使用者會給你一場會議各時段的重點。用繁體中文(台灣用語)條列這場會議的"
        "主要議題,最多 8 點,每點一句,每點用「- 」開頭。\n"
        "只寫重點裡有的,不要推測;不要寫決議或待辦;不要加標題。"
    ),
    # 同 apple_asr.swift 那幾個 @Guide 的字
    notes_format=(
        "\n\n只回一個 JSON 物件,欄位:\n"
        "- points:這一段討論的重點,每點一句,最多 4 點\n"
        "- todo:這一段最明確的一件交辦或有人承諾要做的事;沒有就填 null,不要湊數。"
        "有的話是 {\"task\": 要做的事,一句話, \"quote\": 逐字稿裡作為依據的那一句,一字不改照抄,10 到 40 字}"
    ),
    marked_label="【與會者現場標記的重點】",
    notes_label="【各時段重點】",
    attendee_note="與會者的備註:",
    note_sep=";",
    summary_heading="## 摘要",
    topics_heading="## 主要議題",
    todos_heading="## 待辦(約略時間,請對照逐字稿)",
    # 錄音中按了「重點」的那幾輪(2026-10-17,同手機版 `AISummary.highlightsHeading`)
    highlights_heading="## 現場標記的重點",
    no_todos="- 沒有找到明確的待辦",
    # ⚠️ 「品質比不上雲端 AI」是使用者 2026-10-11 要求明講的(地端小模型能力有限)
    notice_apple=("> ⚠ AI 草稿:由這台 Mac 上的 Apple Intelligence 整理,能力有限、品質比不上"
                  "雲端 AI,可能漏掉或寫錯;正式紀錄請對照逐字稿另行整理。"),
    notice_local=("> ⚠ AI 草稿:由這台電腦上的地端小模型整理,能力有限、品質比不上雲端 AI,"
                  "可能漏掉或寫錯;正式紀錄請對照逐字稿另行整理。"),
    source_label="> 來源:",
    title_suffix="會議紀錄草稿",
    char_scale=1,
    task_chars=(4, 60),
)

ZH_CN = DraftVocab(
    language=meetlang.ZH_CN,
    map_instructions=(
        "你是会议记录助理。用户会给你一段会议逐字稿(语音识别的结果,有错字、标点少,"
        "每行开头是讲者,可能是「未知」或「讲者 N」)。\n"
        "用简体中文整理这一段的重点与待办。只写逐字稿里真的有的,不要推测。"
    ),
    highlight_instructions=(
        "你是会议记录助理。用户会给你一段会议发言(语音识别的结果,有错字、标点少,开头是讲者),"
        "与会者在现场把它标成重点,可能还附了与会者自己打的备注。\n"
        "用简体中文写一句话讲这段的重点(数字、结论、谁要做什么),备注说的优先;"
        "只写发言与备注里有的,不要推测;不要加标题、条列或「这段在说」。"
    ),
    merge_instructions=(
        "你是会议记录助理。用户会给你几段会议重点。用简体中文合并成最多 6 点、"
        "每点一句,合并重复的,只写重点里有的。"
    ),
    summary_instructions=(
        "你是会议记录助理。用户会给你一场会议各时段的重点。用简体中文写 3 句以内的摘要,"
        "讲整场最重要的目标、数字与方向。\n"
        "只写重点里有的,不要推测;不要写日期、地点、主持人、出席者;不要每句都写「某某人认为」;"
        "不要加标题或条列。"
    ),
    topics_instructions=(
        "你是会议记录助理。用户会给你一场会议各时段的重点。用简体中文条列这场会议的"
        "主要议题,最多 8 点,每点一句,每点用「- 」开头。\n"
        "只写重点里有的,不要推测;不要写决议或待办;不要加标题。"
    ),
    notes_format=(
        "\n\n只回一个 JSON 对象,字段:\n"
        "- points:这一段讨论的重点,每点一句,最多 4 点\n"
        "- todo:这一段最明确的一件交办或有人承诺要做的事;没有就填 null,不要凑数。"
        "有的话是 {\"task\": 要做的事,一句话, \"quote\": 逐字稿里作为依据的那一句,一字不改照抄,10 到 40 字}"
    ),
    marked_label="【与会者现场标记的重点】",
    notes_label="【各时段重点】",
    attendee_note="与会者的备注:",
    note_sep=";",
    summary_heading="## 摘要",
    topics_heading="## 主要议题",
    todos_heading="## 待办(约略时间,请对照逐字稿)",
    highlights_heading="## 现场标记的重点",
    no_todos="- 没有找到明确的待办",
    notice_apple=("> ⚠ AI 草稿:由这台 Mac 上的 Apple Intelligence 整理,能力有限、质量比不上"
                  "云端 AI,可能漏掉或写错;正式记录请对照逐字稿另行整理。"),
    notice_local=("> ⚠ AI 草稿:由这台电脑上的本地小模型整理,能力有限、质量比不上云端 AI,"
                  "可能漏掉或写错;正式记录请对照逐字稿另行整理。"),
    source_label="> 来源:",
    title_suffix="会议记录草稿",
    char_scale=1,
    task_chars=(4, 60),
)

EN = DraftVocab(
    language=meetlang.EN,
    map_instructions=(
        "You are a meeting minutes assistant. The user gives you part of a meeting transcript "
        "(speech recognition output, with errors; each line starts with the speaker, which may be "
        "\"Unknown\" or \"Speaker N\").\n"
        "Write in English the key points and any action item of this part. Only write what is "
        "actually in the transcript; don't guess."
    ),
    highlight_instructions=(
        "You are a meeting minutes assistant. The user gives you one turn from a meeting (speech "
        "recognition output, with errors; it starts with the speaker) that an attendee marked as a "
        "highlight during the meeting, possibly with the attendee's own note.\n"
        "In English, write one sentence on the key point (numbers, conclusions, who does what), "
        "giving priority to the note. Only what was said or noted; don't guess; no heading, bullet, "
        "or \"this part is about\"."
    ),
    merge_instructions=(
        "You are a meeting minutes assistant. The user gives you several sets of meeting notes. "
        "Merge them in English into at most 6 points, one sentence each, combining duplicates; "
        "only write what is in the notes."
    ),
    summary_instructions=(
        "You are a meeting minutes assistant. The user gives you the key points from each part of "
        "a meeting. Write a summary in English of at most 3 sentences covering the most important "
        "goals, numbers and direction.\n"
        "Only what is in the points; don't guess; don't mention the date, place, chair or "
        "attendees; don't start every sentence with \"X thinks\"; no heading or bullets."
    ),
    topics_instructions=(
        "You are a meeting minutes assistant. The user gives you the key points from each part of "
        "a meeting. List the main topics of the meeting in English, at most 8, one sentence each, "
        "each starting with \"- \".\n"
        "Only what is in the points; don't guess; no decisions or action items; no heading."
    ),
    notes_format=(
        "\n\nReply with one JSON object only, fields:\n"
        "- points: the key points discussed in this part, one sentence each, at most 4\n"
        "- todo: the clearest task assigned or promised in this part; null if there is none "
        "(don't make one up). If present: {\"task\": the thing to do, one sentence, \"quote\": the "
        "transcript sentence it is based on, copied word for word, 5 to 30 words}"
    ),
    marked_label="[Highlights marked by attendees]",
    notes_label="[Key points by section]",
    attendee_note="Attendee's note: ",
    note_sep="; ",
    summary_heading="## Summary",
    topics_heading="## Main topics",
    todos_heading="## Action items (approximate time; check the transcript)",
    highlights_heading="## Highlights marked during the meeting",
    no_todos="- No clear action items found",
    notice_apple=("> ⚠ AI draft: written by Apple Intelligence on this Mac. It is limited, not as "
                  "good as cloud AI, and may miss or get things wrong; check the transcript for "
                  "the official minutes."),
    notice_local=("> ⚠ AI draft: written by a small local model on this computer. It is limited, "
                  "not as good as cloud AI, and may miss or get things wrong; check the transcript "
                  "for the official minutes."),
    source_label="> Source: ",
    title_suffix="meeting minutes draft",
    char_scale=3,
    task_chars=(8, 160),
)

ALL = (ZH_TW, ZH_CN, EN)
_BY_LANG = {v.language: v for v in ALL}


def of(language: str) -> DraftVocab:
    """會議語言 → 那一份(認不得就是繁中,同 `meetlang.parse`)。"""
    return _BY_LANG[meetlang.parse(language)]
