"""管線共用的資料型別與講者領域常數(不依賴任何其他模組,誰都能 import)。

三種段落型別對應管線三步的產物:
  TranscriptSegment(轉錄輸出)+ SpeakerTurn(講者分離輸出)
  → merge.assign_speakers → SpokenSegment(「誰說了這句」,輸出用)。
"""
from dataclasses import dataclass

# 講者編號為 0-based;此哨兵值代表「未知」——某段語音與已辨識出的任何
# 講者都不夠像(通常是很短、模糊或重疊的碎片),不硬塞給某講者。
UNKNOWN_SPEAKER = -1

# 逐字稿上那個名字**是怎麼來的**(2026-09-18 使用者指定)。
#
# ⚠️ **這三種的可信度差一個量級,而在 md 上長得一模一樣**——下游(使用者
# 的 FWIKI:Claude Code 讀整份 md 寫成主題知識頁)把「有名字」一律當成
# 「聲紋命中建檔樣本、可信度高」,而自動辨識其實會認錯人(留一法實測:
# 光過相似度門檻的 90 對 32 錯,`_RUNNER_UP_MARGIN` 擋掉一批之後仍非零)。
#
# ⚠️ **而這類錯誤是下游整套健檢唯一抓不到的**:WIKI 那邊的〈抽樣回原件
# 核對〉是唯一拿頁面對原件的檢查,逐句判「符合/不符/原文找不到」——把甲
# 的話掛給乙時,頁面寫「乙說……」、回原件一對**逐字稿上確實寫著乙說**,
# 判定是「符合」。那正是他們自己定義的「寫錯但前後一致的錯誤查不出來」。
# 所以誠實標示來源是這條路上唯一的防線,不是錦上添花。
NAME_SOURCE_CONFIRMED = "人工確認"   # 使用者自己填/改過的名字
NAME_SOURCE_AUTO = "機器辨識"        # 聲紋自動辨識填的,使用者沒有動過
NAME_SOURCE_NONE = "未命名"          # 仍是「講者 N」/「未知」

# 講者人數上限:UI 的命名框數/人數欄 clamp(app)與自動偵測的聚類封頂
# (diarize)共用同一個值——數千 cluster 的聚類無意義且可能炸掉,UI 也
# 擺不下;單一出處放這裡(與 UNKNOWN_SPEAKER 同為講者領域常數),
# 兩端才不會各改各的悄悄走鐘。
MAX_SPEAKERS = 30

# 執行裝置的顯示名。⚠️ **住這裡不住 `app.py`**(2026-08-29 搬過來):兩套介面都要
# 用它——網頁版拿去填「本機偵測」那句與使用說明,原生視窗的使用說明同樣要,而
# `app.py` 是 gradio 專屬的、原生視窗不能碰。
# ⚠️ **只報「實際在算的」裝置**:標準機還有一顆 NPU(AI Boost),但本專案沒有任何
# 運算跑在上面,列出來會讓人以為它在幫忙(曾短暫顯示「Intel NPU+GPU」,2026-08-03
# 使用者以「完全沒有使用」為由指定退回;轉錄搬上 NPU 的實測結論見
# `scripts/bench_npu.py`)。
DEVICE_NAMES = {"cuda": "NVIDIA GPU", "intel-gpu": "Intel GPU", "cpu": "CPU"}


@dataclass(frozen=True)
class TranscriptSegment:
    """轉錄輸出:一句帶時間戳的文字。"""

    start: float
    end: float
    text: str


@dataclass(frozen=True)
class SpeakerTurn:
    """講者分離輸出:某講者(0-based 編號)連續說話的時間區段。"""

    start: float
    end: float
    speaker: int
    # 這一段的聲紋與所屬講者群質心的相似度(分群當下就算出來的,見
    # diarize._cluster 的 conf)。⚠️ **只在同一份錄音之內比才有意義**
    # ——同 SpeakerQuality 的警告。0.0 = 沒有這個資訊(例如「重設講者」
    # 那條路,分群早在當初就做完了)
    conf: float = 0.0
    # 這一段落在「多人快速交錯討論」的時間區間裡(見 diarize._crosstalk_spans)。
    # ⚠️ **它標的是「這段時間分不開」,不是「這一群混了人」**——後者 2026-08-08
    # 用三種統計量試過、分不開(見 SpeakerQuality 的警告),而前者判準是段長與
    # 相鄰段換人頻率,是時間區間層級的訊號,兩者不是同一個問題
    crosstalk: bool = False


@dataclass(frozen=True)
class SpokenSegment:
    """合併結果:掛上講者的一句話。"""

    start: float
    end: float
    speaker: int
    text: str
    # 同 SpeakerTurn.crosstalk,由 merge.assign_speakers 從掛到的那個 turn 帶過來
    crosstalk: bool = False


@dataclass(frozen=True)
class SpeechBlock:
    """逐字稿上的**一輪發言**:同一位講者的連續句子合併後的那一段。

    ⚠️ **這是「使用者看得到的單位」,與講者分離的原始區段不同**:一輪
    發言可能由十幾個區段組成,而 md 是以它為單位跑標點模型的——所以
    區塊內部的句界在成品裡已經不存在。任何「改掛給別人」的功能只能以
    它為單位(見 audit.py 的模組說明)。"""

    speaker: int
    start: float
    end: float
    text: str
    is_marker: bool = False     # 跳針標記段(自成區塊、不跑標點)
    # 這一輪發言的聲紋一致性(組成它的區段 conf 的加權平均;0 = 沒資訊)。
    # 「🔍 核對」把它列出來,讓使用者一眼看出**哪幾列比較可疑**——
    # ⚠️ 它是**同一群之內的相對值**,不是「這一段是不是他」的判定
    cohesion: float = 0.0
    # 這一輪發言裡有句子落在多人交錯區間(任一句中招即為 True:一輪發言只有
    # 一個講者標籤,而標記要回答的是「這一輪的歸屬可不可靠」)
    crosstalk: bool = False

    @property
    def seconds(self) -> float:
        return max(self.end - self.start, 0.0)


@dataclass(frozen=True)
class SpeakerQuality:
    """一位講者的分群品質:段數、總時長,以及這個標籤內部有多一致。

    存在的理由是**下游要分得出「哪些標籤最該人工核對」**。分群把好幾個人
    塌成一群時,成品裡看起來只是「少了一個人」——沒有任何跡象,使用者不會
    去改,聲紋庫還會把錯的名字學起來(2026-08-07 實跡,見 voiceprints 檔頭)。

    cohesion = 各段聲紋對本群質心的平均 cosine 相似度。

    ⚠️ **它是「同一份錄音之內的相對指標」,不是可以跨錄音比的分數,更不是
    「這一群有幾個人」的判準。** 2026-08-08 用三份真實錄音實測過三種想
    自動判定「這群裝了不只一個人」的統計量(群內一致性、最佳二分裂的子質心
    相似度、扣掉群質心後重新分群),**沒有一種分得開**:那場月會裡真正混了
    四個人的群,二分相似度 0.665,比確定是單人的總經理那群(0.627)還高;
    而總經理與董事長的 cohesion(0.510 / 0.497)低於對照組每一位真實講者。
    照那種統計量設門檻,不是把幾乎每個標籤都標成可疑,就是反過來冤枉主席。
    所以這裡**只提供數字與排序,不下判決**——輸出的診斷區塊據此列出
    「本場一致性最低的幾個標籤,建議優先核對」,那是誠實而且真的有用的。
    """

    speaker: int
    segments: int
    seconds: float
    cohesion: float
