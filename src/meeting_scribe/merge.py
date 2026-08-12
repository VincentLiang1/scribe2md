"""管線第三步:把「轉錄句子」與「講者區段」依時間配對成「誰說了這句」。

轉錄(TranscriptSegment)與講者分析(SpeakerTurn)是兩條獨立時間軸,
assign_speakers() 為每句挑重疊時間最長的講者;完全無重疊(兩邊邊界
不一致的縫隙)才退而取距離最近者。輸出 SpokenSegment 交給 pipeline.
finalize 繁化與輸出。
"""
import bisect
from itertools import accumulate

from meeting_scribe.types import SpeakerTurn, SpokenSegment, TranscriptSegment


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    """兩區間的重疊秒數(無重疊為 0)。"""
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def assign_speakers(
    segments: list[TranscriptSegment], turns: list[SpeakerTurn]
) -> list[SpokenSegment]:
    """依時間重疊把講者掛到每句轉錄文字上。

    每句 TranscriptSegment 找出重疊時間最長的 SpeakerTurn;
    若完全無重疊(VAD 與 diarization 邊界不一致的間隙),
    改以句子中點到 turn 區間邊界的距離取最近者——
    比較「中點到中點」會偏袒短 turn(長 turn 的中點離自身邊緣遠,
    緊鄰的長 turn 反而輸給較遠的短 turn)。

    turns 假定已按 start 排序;重疊或距離平手時取序列中較早者。
    實作以二分把每句的候選縮到「時間上可能重疊的一小窗」——逐句掃
    全部 turn 是 O(句數×turn 數),長會議兩邊各上千段時要好幾秒;
    end 非單調(長 turn 可蓋過其後的短 turn),故二分對象是單調的
    「前綴最大 end」,窗內逐一比重疊,選擇語意與逐句全掃相同。
    """
    if not turns:
        return [SpokenSegment(s.start, s.end, 0, s.text) for s in segments]
    starts = [t.start for t in turns]
    cummax_end = list(accumulate((t.end for t in turns), max))
    # 前綴最大 end 的「首個達成者」:無重疊 fallback 的左側最近 turn;
    # 平手(同 end)取序列較早者,與原本 min() 首見即留的語意一致
    argmax_end = [0] * len(turns)
    for i in range(1, len(turns)):
        argmax_end[i] = i if turns[i].end > cummax_end[i - 1] else argmax_end[i - 1]
    result = []
    for seg in segments:
        # 候選窗 [lo, hi):hi 之後 start ≥ seg.end、lo 之前所有 end ≤
        # seg.start(前綴最大 end 保證),兩側都不可能有正重疊
        hi = bisect.bisect_left(starts, seg.end)
        lo = bisect.bisect_right(cummax_end, seg.start)
        best = None
        best_ov = 0.0
        for t in turns[lo:hi]:
            ov = _overlap(seg.start, seg.end, t.start, t.end)
            if ov > best_ov:
                best, best_ov = t, ov
        if best is None:
            # 完全無重疊:中點必在所有 turn 之外,邊界距離恆為正——
            # 最近者只可能是「mid 右側第一個 turn」(start 最小)或
            # 「mid 左側 end 最大的 turn」;距離平手取序列較早者
            # (左側索引必小於右側,tuple 比較第二元即索引)
            mid = (seg.start + seg.end) / 2
            r = bisect.bisect_left(starts, mid)
            cand: list[tuple[float, int]] = []
            if r > 0:
                li = argmax_end[r - 1]
                cand.append((mid - turns[li].end, li))
            if r < len(turns):
                cand.append((turns[r].start - mid, r))
            best = turns[min(cand)[1]]
        result.append(SpokenSegment(seg.start, seg.end, best.speaker, seg.text))
    return result
