"""轉錄領域詞表(hotwords):修正中文金融詞誤譯的引導詞。

一行一詞的純文字檔(# 開頭為註解),存在專案 data/ 子目錄(隨程式碼
版控/複製,與 attendees 同模式)。兩個轉錄引擎的每個解碼視窗都會把
詞表以「前文」(<|startofprev|>)注入,引導同音異字選字(金控≠監控、
壽險≠受險、靜止戶≠禁止戶);faster-whisper 與 openvino-genai 的
hotwords 語意一致(皆為逐窗注入,兩邊 API 文件/原始碼實查)。

選 hotwords 而非 initial_prompt:本專案 condition_on_previous_text=False
(幻覺防治),initial_prompt 只被當第一個 30 秒視窗的前文、之後即被
重置丟棄;hotwords 才是每個視窗都注入。

詞表有 token 預算:faster-whisper 超過 max_length//2(224)即截斷到
223 tokens,超出的「尾端」詞彙被靜默丟棄——檔案順序即優先序,重要的
詞放前面。繁中金融詞以 large-v3 tokenizer 實測約 1.3 tokens/字,
據此以字數近似預警(見 WARN_CHARS)。
"""

import logging
from pathlib import Path

from meeting_scribe import models

logger = logging.getLogger(__name__)

# 223 tokens ÷ 實測 1.3 tokens/字 ≈ 171 字;取 160 留安全餘裕
# (人名等罕用字可到 ~2 tokens/字,字數只是近似)。
# 公開常數:詞表分頁的統計/預警(data_tabs)與此共用同一個門檻
WARN_CHARS = 160


def store_file() -> Path:
    return models.data_dir() / "hotwords.txt"


def load() -> list[str]:
    """回傳詞表(去重、保留檔案順序;跳過空白行與 # 註解行)。

    檔案不存在回空清單(功能等同關閉)。順序必須保留:引擎截斷
    砍的是尾端,檔案順序就是優先序。"""
    f = store_file()
    if not f.exists():
        return []
    out: list[str] = []
    seen: set[str] = set()
    for line in f.read_text(encoding="utf-8").splitlines():
        w = line.strip()
        if w and not w.startswith("#") and w not in seen:
            seen.add(w)
            out.append(w)
    return out


def as_string() -> str | None:
    """引擎用的 hotwords 字串(頓號相連);詞表為空回 None(不注入)。

    每次呼叫重讀檔案:使用者編輯 data/hotwords.txt 後,下一個檔案
    即生效、不必重啟(檔案很小,成本可忽略;與 attendees 同準則)。"""
    words = load()
    if not words:
        return None
    joined = "、".join(words)
    if len(joined) > WARN_CHARS:
        logger.warning(
            "領域詞表過長(%d 字,建議 ≤%d):超過引擎 token 上限的部分"
            "(尾端詞彙)會被靜默截斷、等於沒加。請精簡 %s(重要的詞放前面)",
            len(joined), WARN_CHARS, store_file(),
        )
    return joined
