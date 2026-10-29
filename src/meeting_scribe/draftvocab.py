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
    notes_format: str              # Qwen 那條:`@Guide` 的字寫成提示詞的一段(`minutes_worker`)
    lite_format: str               # 同上,格式錯重問兩次之後只要議題與重點、不要待辦
    json_only: str                 # 沒有格式約束的那條(MLX)接在欄位說明前面:「只回一個 JSON 物件」
    context_label: str             # 分段:上一段最後幾輪(只供參考)
    body_label: str                # 分段:這一段要整理的內容
    marked_label: str              # 摘要輸入:現場標記的重點
    notes_label: str               # 摘要輸入:各時段重點
    attendee_note: str             # 重點的提示裡接備註的那句開頭
    note_sep: str                  # 備註之間
    # ---- 草稿檔 ---- #
    summary_heading: str
    topics_heading: str
    todos_heading: str
    highlights_heading: str
    about_me_heading: str          # 草稿最上面「與我相關」(手機版改版 F)
    glossary_label: str            # 指示末尾:這場的專有名詞(名詞對照表,手機版改版 A)
    list_sep: str                  # 專有名詞之間
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


# 一段的欄位說明(段落協定 v2):前半是議題與重點,後半是待辦——格式錯重問兩次之後只問前半
# (`lite_format`,同 iOS `ChunkNotesLite`)。
# ⚠️ **字照抄 iOS `notesHints`,一個字都不要「整理」**(2026-10-28 W2 實測,Qwen3.5-4B 內顯、
# 38 分鐘 7 段,交了幾條待辦):自己改寫成「只回一個 JSON 物件,欄位:- topic … - todo … 沒有就填
# null」的條列 → **0**;iOS 原文 → 7(核對後留 4);iOS 原文前面多一句「只回一個 JSON 物件。」→ **0**;
# 舊的 v1 寫法(沒有 topic、重點不附原句)→ 7。Qwen 對這段的措辭極度敏感,原因沒查,照抄是唯一驗過的
# 寫法。數據見 `docs/spec/mac/05` §5.4b
_ZH_TW_NOTES = "\n欄位:topic 這一段主要在談哪件事,8 到 16 字的名詞片語,用逐字稿裡的專有名詞;points 這件事的具體內容(數字、時程、誰要做什麼、結論),最多 3 點,每點 text 是一句話、quote 是逐字稿裡作為依據的那一句(一字不改照抄,10 到 40 字)"
_ZH_TW_TODO = ";todo 這一段最明確的一件交辦或有人承諾要做的事,沒有就不填、不要湊數,有的話 task 是要做的事(一句話)、quote 是逐字稿裡作為依據的那一句(一字不改照抄,10 到 40 字)"
_ZH_CN_NOTES = "\n字段:topic 这一段主要在谈哪件事,8 到 16 字的名词词组,用逐字稿里的专有名词;points 这件事的具体内容(数字、时程、谁要做什么、结论),最多 3 点,每点 text 是一句话、quote 是逐字稿里作为依据的那一句(一字不改照抄,10 到 40 字)"
_ZH_CN_TODO = ";todo 这一段最明确的一件交办或有人承诺要做的事,没有就不填、不要凑数,有的话 task 是要做的事(一句话)、quote 是逐字稿里作为依据的那一句(一字不改照抄,10 到 40 字)"
_EN_NOTES = "\nFields: topic = what this part is mainly about, a 3-to-8-word noun phrase using the transcript's own names and terms; points = the concrete details (numbers, timelines, who does what, conclusions), at most 3, each with text (one sentence) and quote (the transcript sentence it is based on, copied word for word, 5 to 30 words)"
# 沒有格式約束的那條(MLX)接在欄位說明前面的那一句。⚠️ **附骨架、內容只寫「…」**(2026-10-28 M2,
# Qwen3.5-4B MLX、43 分鐘 7 段、貪婪解碼,只換這一句):只講「只回一個 JSON 物件。」→ 待辦 **0** 交;
# 附一個有內容的範例 → 5 交 1 留,而且三段照抄範例的待辦「下週五前送報價單」;
# 骨架 → **7 交 6 留**、格式 0 錯。數據在 docs/spec/mac/05 §5.4b
_SKELETON = '{"topic": "…", "points": [{"text": "…", "quote": "…"}], "todo": {"task": "…", "quote": "…"}}'
_EN_TODO = "; todo = the clearest task assigned or promised in this part, leave it out if there is none (don't make one up); if present, task is the thing to do (one sentence) and quote is the transcript sentence it is based on, copied word for word (5 to 30 words)"


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
    # 同 apple_asr.swift 那幾個 @Guide 的字(段落協定 v2,同 iOS `ChunkNotes` / `notesHints`,2026-10-28):
    # 議題由每段的 topic 挑(`minutes.pick_topics`),重點每點附原句、由程式核對
    notes_format=_ZH_TW_NOTES + _ZH_TW_TODO + "。",
    lite_format=_ZH_TW_NOTES + "。",
    json_only="\n\n只回一個 JSON 物件,形狀像這樣(… 換成這一段的內容):\n" + _SKELETON,
    context_label="(前文,只供參考,不要整理)",
    body_label="(以下是要整理的這一段)",
    marked_label="【與會者現場標記的重點】",
    notes_label="【各時段重點】",
    attendee_note="與會者的備註:",
    note_sep=";",
    summary_heading="## 摘要",
    topics_heading="## 主要議題",
    todos_heading="## 待辦(約略時間,請對照逐字稿)",
    # 錄音中按了「重點」的那幾輪(2026-10-17,同手機版 `AISummary.highlightsHeading`)
    highlights_heading="## 現場標記的重點",
    about_me_heading="## 與我相關",
    glossary_label="這場的專有名詞(照這樣寫):",
    list_sep="、",
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
    notes_format=_ZH_CN_NOTES + _ZH_CN_TODO + "。",
    lite_format=_ZH_CN_NOTES + "。",
    json_only="\n\n只回一个 JSON 对象,形状像这样(… 换成这一段的内容):\n" + _SKELETON,
    context_label="(前文,只供参考,不要整理)",
    body_label="(以下是要整理的这一段)",
    marked_label="【与会者现场标记的重点】",
    notes_label="【各时段重点】",
    attendee_note="与会者的备注:",
    note_sep=";",
    summary_heading="## 摘要",
    topics_heading="## 主要议题",
    todos_heading="## 待办(约略时间,请对照逐字稿)",
    highlights_heading="## 现场标记的重点",
    about_me_heading="## 与我相关",
    glossary_label="这场的专有名词(照这样写):",
    list_sep="、",
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
    notes_format=_EN_NOTES + _EN_TODO + ".",
    lite_format=_EN_NOTES + ".",
    json_only="\n\nReply with one JSON object only, shaped like this (replace … with this part's content):\n" + _SKELETON,
    context_label="(Earlier context, for reference only; do not summarize)",
    body_label="(The part to summarize starts here)",
    marked_label="[Highlights marked by attendees]",
    notes_label="[Key points by section]",
    attendee_note="Attendee's note: ",
    note_sep="; ",
    summary_heading="## Summary",
    topics_heading="## Main topics",
    todos_heading="## Action items (approximate time; check the transcript)",
    highlights_heading="## Highlights marked during the meeting",
    about_me_heading="## Related to me",
    glossary_label="Proper names in this meeting (spell them this way): ",
    list_sep=", ",
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
