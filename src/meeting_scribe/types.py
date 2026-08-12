"""管線共用的資料型別與講者領域常數(不依賴任何其他模組,誰都能 import)。

三種段落型別對應管線三步的產物:
  TranscriptSegment(轉錄輸出)+ SpeakerTurn(講者分離輸出)
  → merge.assign_speakers → SpokenSegment(「誰說了這句」,輸出用)。
"""
from dataclasses import dataclass

# 講者編號為 0-based;此哨兵值代表「未知」——某段語音與已辨識出的任何
# 講者都不夠像(通常是很短、模糊或重疊的碎片),不硬塞給某講者。
UNKNOWN_SPEAKER = -1

# 講者人數上限:UI 的命名框數/人數欄 clamp(app)與自動偵測的聚類封頂
# (diarize)共用同一個值——數千 cluster 的聚類無意義且可能炸掉,UI 也
# 擺不下;單一出處放這裡(與 UNKNOWN_SPEAKER 同為講者領域常數),
# 兩端才不會各改各的悄悄走鐘。
MAX_SPEAKERS = 30


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


@dataclass(frozen=True)
class SpokenSegment:
    """合併結果:掛上講者的一句話。"""

    start: float
    end: float
    speaker: int
    text: str


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
