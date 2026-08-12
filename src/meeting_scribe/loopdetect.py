"""轉錄跳針(重複迴圈)偵測。

Whisper 類模型在多人重疊、雜訊或長視窗解碼時的知名故障模式:開始無限
重複同一詞組,時間戳同時失效——實際案例(42 分鐘真實會議):一句橫跨
418 秒的「包括資料,」×百次、另一句「公司的」×數百次(前段還帶正常文字)。

偵測用 zlib 壓縮比(重複文字壓縮比極高):以該會議實測校準——跳針段
2.5~3.4,正常長段最高 1.84(中位數 0.93),門檻取 2.2 有明確分離帶。
短文字(< _MIN_CHARS)一律不判:「對對對」等日常合法重複壓縮比僅 ~1,
且短句就算誤判也沒有標記價值,徒增誤傷風險。
"""
import zlib
from collections import Counter

_MIN_CHARS = 80
_RATIO_THRESHOLD = 2.2
# repeated_phrase:重複短語至少出現這麼多次才可信(避免引用到巧合片語)
_MIN_PHRASE_COUNT = 8

# 跳針標記文字的開頭(pipeline 產生標記、hints 排除摘錄、export 讓標記
# 自成區塊並跳過標點模型——標點模型會把標記重新斷句斷壞,如「重複輸,出」,
# 使用者回報——三處共用;常數放本模組:export/pipeline 皆可引用而無循環)
MARKER_PREFIX = "(此段轉錄異常"


def compression_ratio(text: str) -> float:
    """utf-8 位元組長度 / zlib 壓縮後長度;重複文字比值極高。"""
    b = text.encode("utf-8")
    return len(b) / max(len(zlib.compress(b, 9)), 1)


def is_degenerate(text: str) -> bool:
    """這段文字是否為轉錄跳針(重複迴圈)輸出。"""
    if len(text) < _MIN_CHARS:
        return False
    return compression_ratio(text) >= _RATIO_THRESHOLD


_PUNCT = ",。、?!;:.!?;: "


def repeated_phrase(text: str) -> str:
    """找出被重複的短語(2~6 字,取「出現次數×長度」覆蓋最大者),
    供異常標記引用;找不到夠高頻的短語回空字串。

    n-gram 掃描抓到的是循環的任意切點(如「料,包括資」),要正規化:
    先縮到最小週期,再從所有旋轉中挑「不以標點開頭、且在原文連續出現
    兩次」者(偏好以標點結尾的自然斷點),最後去掉頭尾標點。"""
    best, best_score = "", 0
    for n in range(2, 7):
        if len(text) < n:
            break
        counts = Counter(text[i:i + n] for i in range(len(text) - n + 1))
        phrase, cnt = counts.most_common(1)[0]
        score = cnt * n
        if cnt >= _MIN_PHRASE_COUNT and score > best_score:
            best, best_score = phrase, score
    if not best:
        return ""
    for p in range(1, len(best)):  # 縮到最小週期(「公司的公司」→「公司的」)
        if best == (best[:p] * (len(best) // p + 1))[:len(best)]:
            best = best[:p]
            break
    rotations = [best[i:] + best[:i] for i in range(len(best))]
    cands = [r for r in rotations if r[0] not in _PUNCT and (r + r) in text]
    if cands:
        ending = [r for r in cands if r[-1] in _PUNCT]  # 以標點收尾=自然斷點
        pool = ending or cands
        best = min(pool, key=lambda r: text.find(r + r))
    return best.strip(_PUNCT)
