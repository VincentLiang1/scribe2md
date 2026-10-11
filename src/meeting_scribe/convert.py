"""簡轉繁(s2tw)+ 台灣用詞定點替換(data/replace.txt)。

文件轉檔(docpipe)多一道:原文本來就是繁體時不整份過 s2tw,只轉確定是
簡體的字(`is_simplified_document` / `keep_traditional`,見那一段的註解)。

用 s2tw(純字形)而非 s2twp:s2twp 多掛的 TWPhrases 詞彙表(775 條,
IT 術語為主)是為「翻譯大陸技術文件」設計,拿來處理台灣人口語的
逐字稿會把合法台灣用詞整批偷換——58 分鐘真實會議實測誤傷 13+ 處
(動用戶→動使用者、股票代碼→股票程式碼、開放項目→開放專案、
找的對象→找的物件、有權限→有許可權),而它能修的大陸詞在同場
會議的口說中出現 0 次:ASR 逐音轉寫,台灣人說台灣詞(軟體 ruǎntǐ
≠ 軟件 ruǎnjiàn),寫出來就是台灣詞、只差字形。

殘餘風險是 Whisper 幻覺/LM 偏好偶發吐出大陸詞,由 data/replace.txt
定點替換兜底:只收「台灣絕不使用且無跨詞歧義」的詞,一詞一證據,
不做整批詞彙改寫(收詞原則與地雷案例見該檔案註解)。
"""

import logging
from pathlib import Path

from meeting_scribe import models

logger = logging.getLogger(__name__)

# 惰性建構(_get_cc 首次轉換才付 import+辭典載入):轉換只發生在轉檔
# 輸出與即時預覽,從不在啟動路徑上,啟動不必付這筆
_cc = None


def _get_cc():
    global _cc
    if _cc is None:
        from opencc import OpenCC

        _cc = OpenCC("s2tw")
    return _cc

# 替換規則快取:to_taiwan_traditional 逐 segment 呼叫(單檔數百次),
# 不能每段重讀檔;以 (路徑, mtime) 為 key,使用者改完 replace.txt
# 下一段轉換就生效、不必重啟(與 hotwords/attendees 同準則,
# 只是它們每檔才讀一次、無須快取)
_rules_cache: tuple[tuple[str, float], list[tuple[str, str]]] | None = None


def replace_file() -> Path:
    return models.data_dir() / "replace.txt"


def parse_rules(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """解析替換表內容,回傳 (規則, 缺「新詞」的壞行)。

    唯一的解析實作:轉換用的 _load_rules 與詞表分頁的統計/壞行點名
    (wordlists.replace_status)都用這一份,格式變更不會兩邊走鐘。"""
    rules: list[tuple[str, str]] = []
    bad: list[str] = []
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0].startswith("#"):
            continue
        if len(parts) < 2:
            bad.append(line.strip())
            continue
        rules.append((parts[0], parts[1]))
    return rules, bad


def _load_rules() -> list[tuple[str, str]]:
    global _rules_cache
    f = replace_file()
    try:
        mtime = f.stat().st_mtime
    except OSError:  # 檔案不存在:替換功能等同關閉
        return []
    key = (str(f), mtime)
    if _rules_cache and _rules_cache[0] == key:
        return _rules_cache[1]
    # utf-8-sig:替換表是使用者會用記事本直接編的檔(同 attendees.load),
    # BOM 會黏在第一條規則的原詞前面,那條規則從此不再命中任何東西
    rules, bad = parse_rules(f.read_text(encoding="utf-8-sig"))
    for line in bad:
        logger.warning("replace.txt 規則缺少新詞(需「原詞 新詞」),已略過:%s", line)
    # 長詞優先:「打印機→印表機」必須先於「打印→列印」執行,
    # 否則短前綴搶先命中,長詞永遠輪不到(打印機→列印機)
    rules.sort(key=lambda r: len(r[0]), reverse=True)
    _rules_cache = (key, rules)
    return rules


def _apply_rules(text: str) -> str:
    for old, new in _load_rules():
        text = text.replace(old, new)
    return text


def to_taiwan_traditional(text: str) -> str:
    return _apply_rules(_get_cc().convert(text))


# ---- 文件:原文本來就是繁體時,只轉「確定是簡體」的字 ----
#
# s2tw 對一對多的字只能猜:「游」同時對應繁體的游/遊,單獨出現時一律給
# 「遊」——原文是繁體時這種改動**全是改錯**(實跡:Excel 名單裡姓「游」的人
# 被轉成姓「遊」)。判斷一個字「確定是簡體」用兩條:
# (1) s2tw 會改它、而且它**不在 Big5(cp950)裡**——繁體文件寫不出這種字
#     (简、测、试、这……);
# (2) 雖然在 Big5 裡,但台灣文件幾乎只會在夾帶簡體時用到它(下面這張表)。
# Big5 裡會被 s2tw 改動的字共 138 個(2026-10-04 列舉),其餘的刻意不收:
# 它們在繁體裡本身就合法,而且不少是姓氏或名字(游、余、范、岳、涂、郁、
# 于、杰、台、后、里、干、云……),轉了就是把人名改錯。
# ⚠️ **只能加「台灣文件裡不會當本字用」的字**;拿不準就不收——留一個簡體字
# 頂多檢索少中一次,改錯人名則沒有人看得出來。
_SIMPLIFIED_IN_BIG5 = frozenset(
    "万与优体价党儿厂吨听圣坏惊怀怜扰机极构柜气洁种网蚕触赶适离确异"
    "忏岭帘茧篱痒蝎"
)


def _in_big5(ch: str) -> bool:
    try:
        ch.encode("cp950")
    except UnicodeEncodeError:
        return False
    return True


def _surely_simplified(ch: str, converted: str) -> bool:
    return converted != ch and (ch in _SIMPLIFIED_IN_BIG5 or not _in_big5(ch))


_tw2s = None


def _get_tw2s():
    global _tw2s
    if _tw2s is None:
        from opencc import OpenCC

        _tw2s = OpenCC("tw2s")
    return _tw2s


def is_simplified_document(text: str) -> bool:
    """整份文件是不是簡體:「確定是簡體」的字比「繁體才有」的字多。

    ⚠️ 簡體那一側**只數確定是簡體的字**,不能直接比 s2tw 改了幾個字:
    「游○○ 組長」s2tw 改 1 個(游)、tw2s 也改 1 個(長),「游○○」
    單獨一格更是 1 比 0——一對多的字在繁體文件裡會被當成簡體證據。
    真正的簡體文件滿是 Big5 寫不出的字,不靠那些字也一面倒。"""
    converted = _get_cc().convert(text)
    simp = sum(_surely_simplified(a, b) for a, b in zip(text, converted))
    trad = sum(a != b for a, b in zip(text, _get_tw2s().convert(text)))
    return simp > trad


def keep_traditional(text: str) -> str:
    """繁體文件用:只把「確定是簡體」的字轉掉,其餘原樣保留,再套替換表。

    轉換仍然整段交給 s2tw 做(夾帶的簡體詞可以吃到它的詞組表),只是逐字
    挑回原文;s2tw 是逐字 1:1 的,萬一長度對不上就退回逐字單獨轉。"""
    converted = _get_cc().convert(text)
    if len(converted) != len(text):
        converted = "".join(_get_cc().convert(ch) for ch in text)
    return _apply_rules("".join(
        b if _surely_simplified(a, b) else a for a, b in zip(text, converted)
    ))
