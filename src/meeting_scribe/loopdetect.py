"""轉錄跳針(重複迴圈)與訓練殘留(幻覺)偵測。

**兩種故障、兩套判準,不要互相套用**:

1. **跳針**(重複迴圈):Whisper 類模型在多人重疊、雜訊或長視窗解碼時的
   知名故障——開始無限重複同一詞組,時間戳同時失效。實際案例(42 分鐘
   真實會議):一句橫跨 418 秒的「包括資料,」×百次、另一句「公司的」
   ×數百次(前段還帶正常文字)。偵測用 zlib 壓縮比(重複文字壓縮比極高):
   以該會議實測校準——跳針段 2.5~3.4,正常長段最高 1.84(中位數 0.93),
   門檻取 2.2 有明確分離帶。短文字(< `_MIN_CHARS`)一律不判:「對對對」
   等日常合法重複壓縮比僅 ~1,且短句就算誤判也沒有標記價值,徒增誤傷風險。

2. **訓練殘留**(2026-09-18 新增):模型把訓練語料裡的 YouTube 結尾語、
   字幕組署名吐進逐字稿。⚠️ **壓縮比抓不到它**——它不重複,就只是一句
   不屬於這場會議的話,夾在正常內容裡。掃使用者 22 份真實逐字稿實測:
   4 處命中,壓縮比只有 1.24~1.81(全部低於 2.2 門檻),而其中兩處所在的
   區塊短於 80 字、連門檻都碰不到。所以另外用**字面比對**,見 `_RESIDUE`。
"""
import re
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


# ---- 訓練殘留(幻覺):模型把訓練語料的句子吐進逐字稿 ----
#
# ⚠️ **這份清單是拿使用者 22 份真實逐字稿掃出來的,不是憑印象列的**
# (2026-09-18)。那次掃描同時擋下了一個會出人命的錯:我原本要把「訂閱」
# 當幻覺詞——而它在這些錄音裡出現 6 次,**全部是真的**(「投顧要跟訂閱
# 要把它區分開」「他如果訂閱,你這個服務」:使用者是金融業,訂閱制是他們
# 的業務詞)。照憑印象的清單做,4 段真實內容會被當垃圾刪掉。
#
# ⚠️ **只收「在任何真實會議裡都不可能出現」的長片語**,判準有三:
#   ① 夠長夠獨特(「明鏡與點點欄目」而不是「點贊」);
#   ② 是完整的句式,不是單詞;
#   ③ 拿真實逐字稿驗過沒有誤中。
# 單詞一律不收——「訂閱」「分享」「轉發」「打賞」在商務會議裡都講得通。
#
# ⚠️ **移除而不是標記整段**:實測那 4 處的幻覺只佔所在區塊的 2~15%,
# 前後都是真實發言(「…是不一定是現在庫存。MING PAO CANADA…」)。沿用
# 跳針那套「整段換成標記」會把真內容一起丟掉,而那正是這個專案最不能
# 接受的事。所以這裡只挖掉那一句,其餘原樣留著。
_RESIDUE = [
    # YouTube 打賞結尾語(Whisper 中文最常見的訓練殘留;實測 2 處)。
    # 中間的標點與「支持」有無都會變,所以用寬鬆連接而不是寫死整句
    re.compile(r"請不吝[點点]贊[,，、]?\s*訂閱[^。!?！?]{0,12}"
               r"明鏡與點點欄目[。!?！?]?"),
    # 明報(加拿大/多倫多)字幕台標,實測 2 處;大小寫與空白都出現過變體
    re.compile(r"MING\s*PAO\s*(CANADA|TORONTO)[、,，]?\s*", re.I),
    # 以下三種是 2026-09-18 掃使用者的知識庫時補上的——他那邊逐頁記著
    # 「屬既知幻覺尾標、非內容」,而我原本的清單沒有。⚠️ **仍然逐條拿
    # 真實逐字稿驗過才收**(FWIKI 的清單是別人的語料、不能直接照抄)。
    #
    # 節目收尾語,實測 6 次全是幻覺,一律突兀地插在句子中間
    # (「是否有證據本集完?所以每天在初接」)。⚠️ **`(?!全)` 不可省**:
    # 「本集完全不談…」是合法中文,而個人 WIKI 裡就有一個活生生的例子
    # ——少了它就會把那句話挖成「全不談…」。
    re.compile(r"本集完(?!全)[。!?！?]?"),
    # 「多謝您的收看」+ 常跟著的「下次見/我們下期見」,實測 2 次全是幻覺。
    # 「您的收看」在商務會議裡不可能出現,所以這個組合夠獨特
    re.compile(r"[多感謝][謝谢]您的收看"
               r"(?:[,，。]?\s*(?:我們)?下[次期]見[。!?！?]?)?"),
    # 節目警語,實測 4 次全是幻覺(「我進 AI 啊,請勿模仿。這種件事…」)
    re.compile(r"請勿模仿[。!?！?]?"),
    # ⚠️ **以下兩條的證據來源與上面幾條不同,標清楚**:它們在手上這 22 份
    # 逐字稿裡**一次都沒出現**,收進來的依據是使用者知識庫的攝入註記
    # (`大中華會議-2026-09-07` 的「本期節目完結、本集完為既知幻覺尾標」、
    # `大中華會議-2026-08-03` 的「本集完／本文字幕由 Amara.org 社群提供」)
    # ——那是更早、已不在 output 的錄音。兩者都是節目/字幕組的固定句式,
    # 商務會議不可能出現,所以即使沒有本地實跡也收得安全。
    re.compile(r"本期節目完結[。!?！?]?"),
    re.compile(r"(本文)?字幕由\s*Amara\.org\s*社群提供[。!?！?]?", re.I),
]


def strip_residue(text: str) -> tuple[str, list[str]]:
    """挖掉訓練殘留的句子;回傳 (清理後的文字, 被挖掉的字串清單)。

    ⚠️ **回傳被挖掉的東西是刻意的**:呼叫端要把它寫進紀錄檔。無聲地改動
    使用者的逐字稿內容,與無聲地留著垃圾一樣不可接受——差別只在哪一種
    比較難發現(而這一種更難)。

    ⚠️ **不判斷「剩下的夠不夠多」**:挖完之後若整段幾乎空了,那是跳針
    那一層的事(`is_degenerate` 對清理後的文字重判一次即可),這支只負責
    挖乾淨,不兼任判定。"""
    removed: list[str] = []
    for pat in _RESIDUE:
        def _take(m: "re.Match[str]") -> str:
            removed.append(m.group(0))
            return ""
        text = pat.sub(_take, text)
    return text, removed


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
