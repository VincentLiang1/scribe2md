"""簡轉繁(s2tw)+ 台灣用詞定點替換(data/replace.txt)。

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
    (data_tabs.replace_status)都用這一份,格式變更不會兩邊走鐘。"""
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
    rules, bad = parse_rules(f.read_text(encoding="utf-8"))
    for line in bad:
        logger.warning("replace.txt 規則缺少新詞(需「原詞 新詞」),已略過:%s", line)
    # 長詞優先:「打印機→印表機」必須先於「打印→列印」執行,
    # 否則短前綴搶先命中,長詞永遠輪不到(打印機→列印機)
    rules.sort(key=lambda r: len(r[0]), reverse=True)
    _rules_cache = (key, rules)
    return rules


def to_taiwan_traditional(text: str) -> str:
    text = _get_cc().convert(text)
    for old, new in _load_rules():
        text = text.replace(old, new)
    return text
