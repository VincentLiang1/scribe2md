"""逐字稿輸出:md(對話式含標點)——**輸出格式固定 md** 的唯一實作處。

`to_markdown` 先把同講者的連續句子合併成區塊(_group_by_speaker)再交給
標點模型——長文脈絡的標點品質最好;跳針標記段自成區塊且跳過標點(理由
見 _group_by_speaker docstring)。`write_md` 只負責落檔,渲染由呼叫端
(pipeline.finalize)做一次、檔案與預覽共用。

(曾支援 txt/srt 與「輸出格式」勾選,使用者 2026-07-26 指定固定 md 輸出
而移除,srt 拋光規則等實作見 git 歷史的本檔;此後 md 是唯一格式,其他
模組不必再為格式分流。)
"""
import re
from collections.abc import Callable
from pathlib import Path

from meeting_scribe import loopdetect
from meeting_scribe.types import (NAME_SOURCE_AUTO, NAME_SOURCE_CONFIRMED,
                                  NAME_SOURCE_NONE, UNKNOWN_SPEAKER,
                                  SpeechBlock, SpokenSegment)


# ---- 講者行的行內標記(2026-09-18 使用者選定 A 案:標記緊跟名字)----
#
# ⚠️ **為什麼要在行內、而不是只放檔尾那張表**:下游是使用者的 FWIKI
# ——Claude Code 讀整份 md、寫成主題知識頁,而寫「某人說……」那一刻
# 讀的是正文那一行。檔尾的表它雖然讀得到(全文都在 context 裡),但要
# 自己把表對回正文、而且很容易漏。**標在名字旁邊,寫下那句話的同一瞬間
# 就看得到**。使用者 2026-09-18 選「行內＋檔尾都標」。
#
# ⚠️ **只標「不可靠」的那些,不標正常的**:使用者的習慣是「認得出來的
# 都會設」(2026-09-18 確認),所以人工確認才是多數——標多數只會讓整份
# 逐字稿每一行都掛著記號,重要的那幾行反而看不出來。
#
# ⚠️ **全形〔〕不是隨手挑的**:① 與 doc2md 那條線的失真標記同一種括號
# (`docmd` 的〔〕),兩種產物在 WIKI 端長得一致;② `**名字**` 後面直接
# 接〔不會破壞 CommonMark 的粗體——這是拿 markdown-it 真的渲染過的
# (10 種寫法全數通過),不是推理出來的。改這個字元前請重跑那個驗證,
# flanking 規則踩過四次。
MARK_AUTO_NAME = "機器辨識"      # 名字是聲紋自動填的,使用者沒動過
MARK_CROSSTALK = "多人交錯"      # 這一輪落在多人快速交錯討論的區間裡
MARK_UNCERTAIN = "身分存疑"      # 這個標籤的群內一致性明顯低於本場中位數
# 多個標記之間的分隔。用全形頓號的變體「・」而不是「、」:後者在中文
# 句子裡到處都是,標記串起來會看起來像內文的一部分
MARK_SEP = "・"


def mark_suffix(marks: list[str]) -> str:
    """標記清單 → 接在 `**名字**` 後面的那一段(沒有標記回空字串)。

    **只有這一份**:渲染(to_markdown)與解析(audit/relabel 的正則)必須
    對得起來,而三處各寫一次字串的話,改一個字就會有一邊失聯——症狀是
    「改掛按了沒反應」而畫面上完全看不出為什麼(同 speaker_label 那條)。"""
    return f"〔{MARK_SEP.join(marks)}〕" if marks else ""


# 逐字稿的講者行:`**名字**〔標記〕(00:12:34)`,標記那一段是選擇性的。
#
# ⚠️ **全 repo 只有這一份**(2026-09-18 起):先前 `relabel._SPEAKER_RE` 與
# `audit._SPEAKER_LINE` 各抄了一份一樣的,而行內標記一加上去,兩份都會
# **安靜地match不到**——症狀是「重設講者只剩手動命名」與「改掛按了說沒改到
# 任何一行」,而畫面上完全看不出成因(同 speaker_label 那條抄三份的教訓)。
# 產生那一行的是這個模組,規則跟著產生端走才追得上。
#
# ⚠️ **時間戳是關鍵**:少了它,內文裡任何一組粗體都會被當成講者標籤
# (多冒出一個不存在的講者 + 多一個命名欄位)。
# ⚠️ **分隔是「標記」或「一個空白」,二選一不是兩者**(2026-09-18 使用者
# 選定的寫法:標記直接貼著時間戳):沒有標記時是 `**名字** (00:12:34)`、
# 有標記時是 `**名字**〔機器辨識〕(00:12:34)`。寫成 `\s*` 放寬的話,
# `**名字**(00:12:34)` 這種本工具不會產生的形狀也會被收下,而那正是
# 「內文裡的粗體被當成講者標籤」的入口
SPEAKER_LINE_RE = re.compile(
    r"^\*\*(?P<name>.+?)\*\*(?:〔(?P<marks>[^〕]*)〕|\ )"
    r"\((?P<h>\d+):(?P<m>[0-5]\d):(?P<s>[0-5]\d)\)$"
)


def speaker_line(name: str, seconds: float, marks: list[str] | None = None) -> str:
    """組一行講者行。渲染與「套用名字時重建那一行」共用同一份組法。"""
    suffix = mark_suffix(marks or [])
    return f"**{name}**{suffix or ' '}({_hms(seconds)})"


def speaker_label(speaker: int) -> str:
    """講者顯示名稱:未知(哨兵 <0)顯示「未知」,其餘為「講者 N」(1-based)。

    ⚠️ **全 repo 只有這一份**(2026-08-15 起):先前 `audit` 與 `app` 各抄了
    一行,而 audit 那份的註解寫著「測試盯著它們一致」——`grep tests/` 是
    0 命中,也就是三份實作沒有任何東西守著。抄的理由(怕 import 繞回來)
    也不成立:`audit` 本來就已經 import export,而 export 只相依 types。"""
    return "未知" if speaker == UNKNOWN_SPEAKER else f"講者 {speaker + 1}"


def _hms(seconds: float) -> str:
    # 負值防線:上游時間戳異常時 floor 除法會產出垃圾時間(如 -1:59:59)
    s = int(max(seconds, 0.0))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def speech_blocks(spoken: list[SpokenSegment]) -> list[SpeechBlock]:
    """同講者連續句子合併為區塊(types.SpeechBlock)。

    跳針標記段「自成區塊」:混進一般區塊會被標點模型重新斷句斷壞
    (「重複輸出」被斷成「重複輸,出」,使用者回報),分開才能跳過標點。

    **迄秒是區塊最後一句的結束**,不是下一塊的開始:兩塊之間常有沉默,
    拿下一塊的起點當終點會把沉默也算進發言時長,核對音檔就會多出一段
    沒有人在講話的空白(而使用者會以為是漏聽了)。"""
    blocks: list[list] = []
    for seg in spoken:
        is_marker = seg.text.startswith(loopdetect.MARKER_PREFIX)
        if (
            blocks
            and blocks[-1][0] == seg.speaker
            and not is_marker
            and not blocks[-1][4]
        ):
            blocks[-1][2].append(seg.text)
            blocks[-1][3] = seg.end
            # 一輪發言裡**任一句**落在交錯區就算(見 SpeechBlock.crosstalk):
            # 一輪只有一個講者標籤,標記要回答的是「這一輪的歸屬可不可靠」
            blocks[-1][5] = blocks[-1][5] or seg.crosstalk
        else:
            blocks.append([seg.speaker, seg.start, [seg.text], seg.end, is_marker,
                           seg.crosstalk])
    return [
        SpeechBlock(speaker=sp, start=st, end=en, text="".join(texts), is_marker=m,
                    crosstalk=ct)
        for sp, st, texts, en, m, ct in blocks
    ]


def _group_by_speaker(
    spoken: list[SpokenSegment],
) -> list[tuple[int, float, str, bool, bool]]:
    """同 speech_blocks,但回傳既有的 tuple 形狀(渲染與診斷區塊用)。"""
    return [(b.speaker, b.start, b.text, b.is_marker, b.crosstalk)
            for b in speech_blocks(spoken)]


# 講者診斷區塊的標題。**公開常數**:relabel 解析逐字稿時要在這裡收手
# (診斷表不是某位講者的發言,被當成內文會污染命名摘錄),套用新名字時
# 也要靠它找到表格去同步改名——兩邊各寫一次字串,改標題就會有一邊失聯
DIAGNOSTIC_HEADING = "## 講者辨識診斷"


def starts_diagnostics(line: str) -> bool:
    """這一行是不是檔尾診斷區塊的開頭。

    **要共用的不只是常數,還有比對方式**:relabel.parse 在這裡收手、
    relabel.rename 從這裡開始改敘述句、audit.reassign 在這裡停止改掛——
    三處各寫一次 `.strip().startswith(...)` 的話,標題格式哪天放寬就會有
    人沒跟上,而症狀是「有些路徑吃得到診斷區塊、有些吃不到」,測試不會全紅。
    """
    return line.strip().startswith(DIAGNOSTIC_HEADING)


# 診斷表裡「一位講者」那一列的第一欄。改名時它要跟著逐字稿一起換
# (relabel.rename),否則命名之後診斷表講的是另一套編號,比沒有更糟。
# 後面接數字(發言輪次)才算:標題列與分隔列的第二欄不是數字,天然被排除
DIAG_ROW_RE = re.compile(r"^\| (?P<name>[^|]+?) \| \d")


def diag_prose_renamer(name_map: dict[str, str]) -> Callable[[str], str]:
    """回一個函式,把診斷區塊**敘述句**裡的舊標籤換成新名字(整行進、整行出)。

    ⚠️ **這一塊的敘述句也會點名標籤**,而 DIAG_ROW_RE 那個錨吃不到它們
    (2026-08-15 的實跡:命名之後表格改對了,上面那句仍寫「建議優先核對:
    **講者 3**」、檔尾仍寫「「**未知**」這一批共 38 段」,指向的標籤在文件
    裡已經不存在——正是 DIAG_ROW_RE 那條註解要防的自相矛盾,只是換個位置)。

    **敘述句點名標籤只有兩種寫法**:`**名字**`(「建議優先核對」那句、
    「未知」那段的開頭)與「名字」(表格圖例、「不要整批命名」那句)。
    **在這一塊裡加新句子時就照這兩種寫法寫**,別自創第三種括號——這支
    函式與那些句子放在同一個檔就是為了讓它跟得上,但它沒辦法自己發現
    第三種,而漏掉的症狀跟上面那條實跡一模一樣。

    **對照表由舊名字組出來**,而不是抓「任何粗體」:這一塊本來就有別的
    粗體(「**機器分出來的**」「**已知限制**」「**不要整批給同一個名字**」),
    照抓會把說明文字當標籤換掉。長的排前面、整行一次 sub 完,所以一個位置
    只會被換一次——互換名字(甲→乙、乙→丙)不會連鎖套用,這點與講者行
    那條的單次查表行為一致。"""
    table = {}
    for old, new in name_map.items():
        table[f"**{old}**"] = f"**{new}**"
        table[f"「{old}」"] = f"「{new}」"
    if not table:
        return lambda line: line   # 空 pattern 會「什麼都中」,不能交給 re
    pattern = re.compile(
        "|".join(re.escape(k) for k in sorted(table, key=len, reverse=True))
    )
    return lambda line: pattern.sub(lambda m: table[m[0]], line)


# 診斷區塊裡「建議優先核對」最多列幾個標籤。列太多等於沒列——這一節的
# 用處是把人工核對的力氣導到最該花的地方,不是把整份稿子都標成可疑
_CHECK_FIRST_MAX = 3
# 「一致性明顯低於本場中位數」的差距。**同一份錄音之內比**才有意義
# (跨錄音的絕對值不可比,理由見 types.SpeakerQuality 的警告)
_CHECK_FIRST_GAP = 0.10


def check_first(quality: list) -> list:
    """本場最該人工核對的幾個講者標籤(一致性最低且明顯低於本場中位數)。

    **刻意只排序、不下判決**:能自動判定「這個標籤裝了不只一個人」的
    統計量,2026-08-08 用三份真實錄音試過三種,沒有一種分得開(數據見
    types.SpeakerQuality)。給一個會冤枉主席、又會漏掉真正出事那群的
    「判定」,比誠實地說「這幾個最不一致,先看這些」有害得多。

    以**本場中位數**為基準而不是固定門檻:一致性的絕對值隨錄音環境浮動,
    但「同一場裡誰最不一致」是穩定的。"""
    if len(quality) < 3:
        return []  # 兩三位講者時「排序」說不出話,不如不講
    mid = sorted(q.cohesion for q in quality)[len(quality) // 2]
    low = [q for q in quality if q.cohesion <= mid - _CHECK_FIRST_GAP]
    return sorted(low, key=lambda q: q.cohesion)[:_CHECK_FIRST_MAX]


def speaker_diagnostics(quality: list, spoken: list[SpokenSegment]) -> list[str]:
    """公開入口(重設講者的重新分群要重建這一塊)。

    **開一個公開名字而不是讓呼叫端去拿私有的**:上游一改名就會安靜壞掉
    (同 diarize.voiceprints_for_spans 的理由),而診斷區塊的格式必須與
    轉檔當下產生的那一份完全一致——兩邊各寫一套的話,重新分群過的逐字稿
    檔尾會長得跟別人不一樣。"""
    return _speaker_diagnostics(quality, spoken)


def _speaker_diagnostics(quality: list, spoken: list[SpokenSegment]) -> list[str]:
    """講者標籤的診斷區塊(md 行清單;無資料回空清單)。

    **為什麼要寫進成品**:講者分離把好幾個人塌成同一群時,逐字稿裡看起來
    只是「少了一個人」——沒有任何跡象,而下游是拿這份稿子編知識庫的
    (2026-08-07 那場 3 小時 59 分的月會,四位遠端與會者全被併成一個名字,
    還被自動命名成其中一位)。把每個標籤的數字攤開、並點出本場最該核對的
    幾個,下游就不必整份都不敢信。

    放在**檔尾**而非檔頭:doc2md 那條路會在 md 前面另放一個 H1、並砍掉
    逐字稿開頭那一行,檔頭多一塊會擠在標題與正文之間;而 RAG 的 chunk
    切在標題上,獨立一節反而更容易被整段取用。"""
    if not quality:
        return []
    blocks = _group_by_speaker(spoken)
    lines = [
        DIAGNOSTIC_HEADING, "",
        f"這份逐字稿的講者是**機器分出來的**,共 {len(quality)} 位。"
        "以下數字供下游(知識庫/RAG)判斷哪些標籤該先人工核對。",
        "",
        "⚠️ **已知限制**:同一個標籤底下有可能其實是好幾個人"
        "(發言少、或透過視訊/電話加入的與會者最容易被併在一起)。"
        "程式**無法自動判定**某個標籤是不是混了多人——真的混了四個人的"
        "標籤,各項一致性指標都可能比單人的還漂亮。所以下面只給數字與"
        "排序,不下判定。",
        "",
    ]
    first = check_first(quality)
    if first:
        names = "、".join(f"**{speaker_label(q.speaker)}**" for q in first)
        lines += [
            f"**建議優先核對**:{names}——這幾個標籤的群內一致性明顯低於"
            "本場其他人,是最可能混進別人的。",
            "",
        ]
    lines += [
        "| 講者 | 發言輪次 | 總時長 | 群內一致性 | 名字來源 |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for q in sorted(quality, key=lambda x: x.speaker):
        n_blocks = sum(1 for sp, _s, _t, _m, _c in blocks if sp == q.speaker)
        # 名字來源在**這個時點一律是「未命名」**:標籤還是「講者 N」,使用者
        # 還沒命名。`relabel.rename` 套用名字時把這一欄改掉(見該處);批次
        # (doc2md)那條路沒有命名步驟,所以那份會一直停在「未命名」——那是
        # 對的,不是漏改
        lines.append(
            f"| {speaker_label(q.speaker)} | {n_blocks} | {_hms(q.seconds)} "
            f"| {q.cohesion:.2f} | {NAME_SOURCE_NONE} |"
        )
    lines += [
        "",
        "- **發言輪次**:這個標籤在逐字稿裡出現幾次(連續發言算一次)。",
        "- **群內一致性**:各段聲紋與該標籤平均聲紋的相似度。"
        "**只能在同一份逐字稿之內互相比**,不同錄音的數字沒有可比性。",
        f"- **名字來源**:「{NAME_SOURCE_CONFIRMED}」= 由人親自填上或改過;"
        f"「{NAME_SOURCE_AUTO}」= 聲紋比對自動填的、**沒有人確認過**"
        f"(這種會認錯人,正文那幾行也標了〔{MARK_AUTO_NAME}〕);"
        f"「{NAME_SOURCE_NONE}」= 仍是「講者 N」,表示這個人沒有聲紋建檔。",
        "- 「未知」不列入本表:那是與任何講者都不夠像的零碎語音,"
        "本來就不代表某一個人。",
        "",
        # ⚠️ **這一段是寫給下游的 AI 看的**(2026-09-18 使用者指定):他的
        # 流程是請 Claude Code 讀整份 md、寫進 FWIKI 的主題知識頁。而標記
        # 只有在「讀的人知道它代表什麼」時才有用——不解釋的話,下游最可能
        # 的反應是把〔〕當成逐字稿的雜訊忽略掉
        f"⚠️ **正文的行內標記**:〔{MARK_AUTO_NAME}〕= 這個名字是機器猜的;"
        f"〔{MARK_UNCERTAIN}〕= 這個標籤的一致性明顯低於本場中位數,"
        "底下可能不只一個人;"
        f"〔{MARK_CROSSTALK}〕= **那一輪落在多人快速交錯討論的區間**,"
        "當時好幾個人搶著講、機器分不開,所以那一行的講者歸屬是猜的。"
        "**引用帶標記的發言時不要斷言是誰說的。**",
        "",
    ]
    lines += _unknown_note(spoken)
    return lines


def _unknown_note(spoken: list[SpokenSegment]) -> list[str]:
    """「未知」那一批到底是什麼(md 行清單;沒有未知段落回空清單)。

    **為什麼要主動講**(2026-08-13,0812 資訊月會):以前這批零碎語音會自成
    一群、在逐字稿裡長得像一位真的講者,使用者去聽了、填上名字——而那一群
    其實混著七個人的插話。現在它們一律歸「未知」(見 `diarize` 的
    `_fragmentary_labels`),但**如果不解釋,使用者只會看到「未知」變多了**,
    那跟原本的意外一樣糟,只是換個方向。

    ⚠️ **要明講「不要整批命名」**:命名框改的是整批未知的文字,而未知本來
    就常是多人混合——工具因此不拿它登記聲紋,使用者也不該給它一個名字。"""
    unknown = [s for s in spoken if s.speaker == UNKNOWN_SPEAKER]
    if not unknown:
        return []
    durations = sorted(s.end - s.start for s in unknown)
    median = durations[len(durations) // 2]
    return [
        f"「**未知**」這一批共 {len(unknown)} 段、{_hms(sum(durations))}"
        f",單段中位長度 {median:.1f} 秒。這些語音太短或太模糊,"
        "聲紋不足以判斷是誰——多半是「對」「好」「瞭解」這類應答與插話,"
        "**它們很可能分屬好幾個不同的人**。",
        "",
        "⚠️ 所以工具**不把這一批當成一位講者**,也不會拿它登記聲紋。"
        "在介面上替「未知」命名只會改逐字稿上的文字;"
        "**不要整批給同一個名字**,除非你已經逐段聽過確認都是同一個人。",
        "",
    ]


def to_markdown(
    spoken: list[SpokenSegment],
    title: str,
    punctuate: Callable[[list[str]], list[str]] | None = None,
    quality: list | None = None,
) -> str:
    """punctuate:對「合併後的講者區塊」補標點的函式(長文脈絡標點
    品質最好);None 表示原樣輸出。跳針標記區塊不補標點:標記自帶
    完整標點,重斷只會斷壞。

    ⚠️ **punctuate 吃一整批、回一整批**(2026-09-17 起,原本一次一個區塊):
    標點模型要在區塊之間平行才快得起來(見 punctuate.add_punctuation_many),
    所以這裡先把要補的區塊收齊、一次交出去。**回來的數量對不上就炸**,不准
    `zip` 默默截短——那樣後半場的區塊會悄悄變成沒有標點的原文。

    quality:每位講者的分群品質(types.SpeakerQuality),有給就在檔尾附
    診斷區塊(見 _speaker_diagnostics)。"""
    groups = list(_group_by_speaker(spoken))
    todo = [i for i, g in enumerate(groups) if not g[3]] if punctuate else []
    done = (dict(zip(todo, punctuate([groups[i][2] for i in todo]), strict=True))
            if todo else {})
    # 一致性明顯低於本場中位數的那幾位:行內也標一次(見 mark_suffix 那段)
    uncertain = {q.speaker for q in check_first(quality or [])}
    lines = [f"## 會議逐字稿 — {title}", ""]
    for i, (speaker, start, text, _is_marker, crosstalk) in enumerate(groups):
        # ⚠️ **這裡只標「轉檔當下就知道的」兩種**:名字要等使用者命名之後
        # 才存在,`MARK_AUTO_NAME` 因此由 `relabel.rename` 在套用名字那一刻
        # 補上(見該處)。⚠️ 順序固定「標籤可疑 → 這一輪交錯」,由範圍大小
        # 排(前者講整位講者、後者只講這一輪);順序飄動的話,下游想 grep
        # 某一種標記就得寫出所有排列
        marks = []
        if speaker in uncertain:
            marks.append(MARK_UNCERTAIN)
        if crosstalk:
            marks.append(MARK_CROSSTALK)
        lines.append(speaker_line(speaker_label(speaker), start, marks))
        lines.append(done.get(i, text))
        lines.append("")
    lines += _speaker_diagnostics(quality or [], spoken)
    return "\n".join(lines)


def transcript_frontmatter(
    spoken: list[SpokenSegment], quality: list | None = None,
) -> list[str]:
    """逐字稿的 frontmatter(md 行清單)。

    ⚠️ **只放「不隨命名改變」的欄位**:使用者命名之後 `relabel.rename` 會
    重寫講者行與診斷表,但它**不碰 frontmatter**——放講者名字進來就會在
    改名後變成一份對不上的舊資料,而那沒有任何症狀。

    ⚠️ **這裡加、`to_markdown` 不加**:批次那條路(docaudio → docmd.render)
    會自己放一份 frontmatter,兩份疊在一起**整份 YAML 解析失敗**,而下游
    多半是先 parse frontmatter 再處理——壞一份就整份進不了知識庫
    (`docmd._frontmatter` 註解記著這條)。`write_md` 只有單檔/現場收音那條
    路會走,批次走的是 `docmd.write_md`,天然不會撞。"""
    if not spoken:
        return []
    total = sum(s.end - s.start for s in spoken)
    if total <= 0:
        return []
    cross = sum(s.end - s.start for s in spoken if s.crosstalk)
    unknown = sum(s.end - s.start for s in spoken if s.speaker == UNKNOWN_SPEAKER)
    return [
        "---",
        # 同 doc2md 那條線的憑據欄位:「這份 md 是本工具產生的」
        "converter: meeting-scribe",
        f"speakers: {len(quality or [])}",
        # ⚠️ 固定英文 token(同 lossy_kinds 的理由):值是給機器判斷的,
        # 而中文的「機器分出來的」沒辦法拿去做相等比較
        "speaker_labels: machine",
        f"crosstalk_share: {cross / total:.3f}",
        f"unknown_share: {unknown / total:.3f}",
        "---",
        # ⚠️ **兩個空字串 = frontmatter 與正文之間真的空一行**:`write_md`
        # 是 `"\n".join(...) + md_text`,少一個的話 `---` 會和 `## 會議逐字稿`
        # 黏成相鄰兩行。多數 YAML parser 不在意,但人讀起來像壞掉的
        "",
        "",
    ]


def write_md(md_text: str, out_dir: Path, stem: str,
             frontmatter: list[str] | None = None) -> Path:
    """把已渲染好的 md 內容寫成檔案,回傳路徑。

    渲染留在呼叫端(pipeline.finalize):同一份 to_markdown 輸出要同時
    當檔案內容與預覽,標點模型對整份逐字稿要跑上數十秒,不能各跑一遍。
    (曾接 spoken+punctuate 自行渲染當備援,生產路徑從未走過,已移除。)

    ⚠️ **frontmatter 只加在檔案上、不進預覽**(2026-09-18):預覽框要給人看
    的是逐字稿本身,開頭頂著一塊 YAML 只是噪音;而它也是「改掛」與命名套用
    比對的那份文字,多一塊在前面只會讓兩邊的內容不一致。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{stem}.md"
    p.write_text("\n".join(frontmatter or []) + md_text, encoding="utf-8")
    return p
