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
from meeting_scribe.types import UNKNOWN_SPEAKER, SpokenSegment


def _speaker_label(speaker: int) -> str:
    """講者顯示名稱:未知(哨兵 <0)顯示「未知」,其餘為「講者 N」(1-based)。"""
    return "未知" if speaker == UNKNOWN_SPEAKER else f"講者 {speaker + 1}"


def _hms(seconds: float) -> str:
    # 負值防線:上游時間戳異常時 floor 除法會產出垃圾時間(如 -1:59:59)
    s = int(max(seconds, 0.0))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _group_by_speaker(
    spoken: list[SpokenSegment],
) -> list[tuple[int, float, str, bool]]:
    """同講者連續句子合併為區塊,回傳 (講者, 起始秒, 文字, 是否為跳針標記)。

    跳針標記段「自成區塊」:混進一般區塊會被標點模型重新斷句斷壞
    (「重複輸出」被斷成「重複輸,出」,使用者回報),分開才能跳過標點。"""
    blocks: list[tuple[int, float, list[str], bool]] = []
    for seg in spoken:
        is_marker = seg.text.startswith(loopdetect.MARKER_PREFIX)
        if (
            blocks
            and blocks[-1][0] == seg.speaker
            and not is_marker
            and not blocks[-1][3]
        ):
            blocks[-1][2].append(seg.text)
        else:
            blocks.append((seg.speaker, seg.start, [seg.text], is_marker))
    return [(sp, st, "".join(texts), m) for sp, st, texts, m in blocks]


# 講者診斷區塊的標題。**公開常數**:relabel 解析逐字稿時要在這裡收手
# (診斷表不是某位講者的發言,被當成內文會污染命名摘錄),套用新名字時
# 也要靠它找到表格去同步改名——兩邊各寫一次字串,改標題就會有一邊失聯
DIAGNOSTIC_HEADING = "## 講者辨識診斷"
# 診斷表裡「一位講者」那一列的第一欄。改名時它要跟著逐字稿一起換
# (relabel.rename),否則命名之後診斷表講的是另一套編號,比沒有更糟。
# 後面接數字(發言輪次)才算:標題列與分隔列的第二欄不是數字,天然被排除
DIAG_ROW_RE = re.compile(r"^\| (?P<name>[^|]+?) \| \d")


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
        names = "、".join(f"**{_speaker_label(q.speaker)}**" for q in first)
        lines += [
            f"**建議優先核對**:{names}——這幾個標籤的群內一致性明顯低於"
            "本場其他人,是最可能混進別人的。",
            "",
        ]
    lines += [
        "| 講者 | 發言輪次 | 總時長 | 群內一致性 |",
        "| --- | ---: | ---: | ---: |",
    ]
    for q in sorted(quality, key=lambda x: x.speaker):
        n_blocks = sum(1 for sp, _s, _t, _m in blocks if sp == q.speaker)
        lines.append(
            f"| {_speaker_label(q.speaker)} | {n_blocks} | {_hms(q.seconds)} "
            f"| {q.cohesion:.2f} |"
        )
    lines += [
        "",
        "- **發言輪次**:這個標籤在逐字稿裡出現幾次(連續發言算一次)。",
        "- **群內一致性**:各段聲紋與該標籤平均聲紋的相似度。"
        "**只能在同一份逐字稿之內互相比**,不同錄音的數字沒有可比性。",
        "- 「未知」不列入本表:那是與任何講者都不夠像的零碎語音,"
        "本來就不代表某一個人。",
        "",
    ]
    return lines


def to_markdown(
    spoken: list[SpokenSegment],
    title: str,
    punctuate: Callable[[str], str] | None = None,
    quality: list | None = None,
) -> str:
    """punctuate:對「合併後的講者區塊」補標點的函式(長文脈絡標點
    品質最好);None 表示原樣輸出。跳針標記區塊不補標點:標記自帶
    完整標點,重斷只會斷壞。

    quality:每位講者的分群品質(types.SpeakerQuality),有給就在檔尾附
    診斷區塊(見 _speaker_diagnostics)。"""
    lines = [f"## 會議逐字稿 — {title}", ""]
    for speaker, start, text, is_marker in _group_by_speaker(spoken):
        lines.append(f"**{_speaker_label(speaker)}** ({_hms(start)})")
        lines.append(punctuate(text) if punctuate and not is_marker else text)
        lines.append("")
    lines += _speaker_diagnostics(quality or [], spoken)
    return "\n".join(lines)


def write_md(md_text: str, out_dir: Path, stem: str) -> Path:
    """把已渲染好的 md 內容寫成檔案,回傳路徑。

    渲染留在呼叫端(pipeline.finalize):同一份 to_markdown 輸出要同時
    當檔案內容與預覽,標點模型對整份逐字稿要跑上數十秒,不能各跑一遍。
    (曾接 spoken+punctuate 自行渲染當備援,生產路徑從未走過,已移除。)"""
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{stem}.md"
    p.write_text(md_text, encoding="utf-8")
    return p
