"""音訊/影片 → 逐字稿 md 的 **doc2md 路由**(批次模式,不做講者命名)。

這支模組把既有的轉檔管線(pipeline)包成 docpipe 認得的 reader,好讓
「多檔/資料夾」與命令列共用文件那條路已經備妥的整套批次語意:
原地輸出 `<原檔名>.md`、同名 md 已存在就整份跳過、單檔失敗不中斷整批、
批次報告、「開啟輸出資料夾」。**不是另寫一條批次迴圈**——那些規則
(尤其「同名就跳過」與批次前先整批規劃)每重寫一次就是一次走樣的機會。

與「聲音→MD」單檔模式的分工(使用者 2026-08-06 指定):

- **單檔**:走 pipeline.run_pipeline → output/ + 講者命名 + 試聽 + 下載。
  講者編號每檔獨立分群,命名是一檔一檔當場做的事。
- **多檔/資料夾/命令列**:走這裡。**講者分析照做、標「講者 N」**,但
  不開命名介面、不試聽、不登記聲紋——批次沒有「當場」可言,硬做命名
  只會把這一檔的名字寫進別檔。

失真標記有兩種,都是「無聲失真不可接受」逼出來的:

- **轉錄跳針**(引擎連重轉都救不回的段落):整條管線唯一會真的弄丟
  內容的地方。
- **講者標籤待核對**:講者分離把好幾個人塌成同一群時,md 裡看起來只是
  「少了一個人」——沒有任何跡象,而下游是拿它編知識庫的。程式判不出
  「這個標籤是不是混了多人」(見 types.SpeakerQuality),但**排得出本場
  哪幾個最不一致**;逐字稿檔尾已有人看的診斷表(export),這個 token 是
  給**機器**篩的。

輸出內容本身沿用 export.to_markdown(與單檔模式**逐字相同**):同一份
錄音不該因為「你是一個一個轉還是一批轉」而得到不一樣的逐字稿。
"""
import logging
from pathlib import Path

from meeting_scribe import docmd, pipeline, punctuate, transcribe
from meeting_scribe.docmd import Block, Note, Raw

logger = logging.getLogger(__name__)

# 轉錄跳針:見模組 docstring。固定英文 token(同其他 KIND_*),繁中說明
# 在 Note.text——RAG 建庫端靠 token 篩,人靠 text 讀
KIND_TRANSCRIPT_GAP = "transcript_gap"
# 有講者標籤的群內一致性明顯偏低,建議人工核對(見模組 docstring)。
# **這不是「內容掉了」而是「內容可能掛錯人」**,但對知識庫是同一件事:
# 引用時會寫錯是誰說的
KIND_SPEAKER_CHECK = "speaker_check"

# 這條路由吃的格式。**與 srcfile.SUPPORTED_TYPES 是同一份清單**
# (由 srcfile 定義、這裡只是引用):兩邊各寫一份的話,「聲音→MD」分頁
# 收得下的檔案會與批次路由收得下的不一致,而症狀是使用者選了檔、按下去
# 才被說「不支援的格式」
def supported_types() -> list[str]:
    """本路由支援的副檔名(唯一出處在 srcfile,避免兩份清單漂移)。

    函式而非模組層常數:srcfile 會 import gradio 以外的東西,但更重要的
    是這樣不會在 import 時就把兩個模組綁死成循環。"""
    from meeting_scribe import srcfile

    return list(srcfile.SUPPORTED_TYPES)


def ensure_ready() -> None:
    """預熱:標點模型與轉錄引擎。

    擺位抄 pipeline 的 ensure_ready 慣例——缺元件/模型下載失敗要在
    **第一秒**炸出來,不是跑到第 47 個檔(而音訊的「第 47 個檔」可能是
    好幾個小時之後)。轉錄引擎只問裝置、不真的載入模型:載入要數十秒,
    而 run 的第一件事就是載它,提早付這個成本只是讓「開始」看起來當掉。"""
    punctuate.ensure_ready()
    transcribe.predicted_device()


def convert_audio(
    src: Path,
    assets,
    ocr_enabled: bool = True,
    *,
    model_key: str = "fast",
    num_speakers: int = 0,
    on_inner=None,
) -> list[Block]:
    """音訊/影片 → Block 清單(逐字稿本文 + 跳針標記)。

    簽章前三個位置參數是 docpipe._ROUTES 的共同契約(ocr_enabled 用不到,
    但一樣收下——為少數格式寫轉接反而更容易漏,見 Route docstring)。
    後三個具名參數由 docpipe 依路由的 inner_progress/audio 旗標帶進來。

    逐字稿本文包成 **Raw**:它已經是渲染好的 markdown,再拆成 Para/Heading
    只會在重組時走樣。Raw 不經 docpipe.traditionalize 也正是這裡要的——
    pipeline.render_transcript 內部已經做過繁化(而且是在標點之前做,順序
    有意義),再轉一次是白費工。frontmatter 的 traditionalised 由 docpipe
    依「內容有沒有被改寫」判斷,所以這裡要另外把事實補回去(見 docpipe)。
    """
    rendered = pipeline.transcribe_to_markdown(
        src,
        model_key=model_key,
        num_speakers=num_speakers,
        on_stage=(
            (lambda _stage, frac: on_inner(frac)) if on_inner else None
        ),
    )
    # 逐字稿本文的標題層級:export.to_markdown 開頭是 `## 會議逐字稿 — 檔名`,
    # 而 docmd.render 會另外放一個 H1(= 檔名)。兩者疊起來是「檔名 / 逐字稿
    # — 檔名」,同一個字串在切塊器眼裡出現兩次;去掉那一行,H1 就是唯一標題
    # (RAG 三規則之一:單一 H1),而「這是逐字稿」由 frontmatter 的
    # source_type 講得更清楚
    blocks: list[Block] = [Raw(
        _drop_leading_heading(rendered.md_text),
        traditionalised=rendered.traditionalised,
    )]
    if rendered.degenerate:
        lost = sum(s.end - s.start for s in rendered.degenerate)
        blocks.append(Note(
            f"有 {len(rendered.degenerate)} 段語音(共 {lost:.0f} 秒)"
            "轉錄時卡在重複迴圈、連重轉都救不回,已在逐字稿中以標記取代",
            kind=KIND_TRANSCRIPT_GAP,
        ))
    if rendered.check_speakers:
        names = "、".join(f"講者 {s + 1}" for s in rendered.check_speakers)
        blocks.append(Note(
            f"有 {len(rendered.check_speakers)} 個講者標籤({names})的"
            "群內一致性明顯低於本場其他人,那些段落的「誰說的」建議對照原音"
            "核對;詳見逐字稿檔尾的「講者辨識診斷」",
            kind=KIND_SPEAKER_CHECK,
        ))
    return blocks


def _drop_leading_heading(md_text: str) -> str:
    """拿掉 export.to_markdown 的開頭標題行(見 convert_audio 的說明)。

    只拿掉**開頭那一行**且必須真的是標題:逐字稿內文是使用者的講話內容,
    萬一有人真的說了一句以 `##` 開頭的話(標點模型不會產生,但引擎的
    原始輸出誰也不敢保證),砍錯就是砍掉內容。"""
    head, sep, rest = md_text.partition("\n")
    if not sep or not head.startswith("## "):
        return md_text
    return rest.lstrip("\n")
