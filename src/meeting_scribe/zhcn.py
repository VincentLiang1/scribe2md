r"""台灣繁體 → 大陸簡體(含用語)的轉換,介面文案與長文共用一份(docs/dev/i18n.md)。

做法照 iOS 版(`scribe2md-ios/scripts/l10n_sync.py`):OpenCC `tw2sp`,再套用語修正表。
兩個呼叫端:`scripts/make_zh_cn.py`(產生 `locales/zh_CN.json`)與 `help_text`(使用說明
簡中版在執行期轉——那份長文夾著 f-string 參數,產生一份 100 KB 的原始檔反而難維護)。

⚠️ **`IOS_FIX` 是 iOS 那張 `ZH_HANS_FIX` 的抄本**,兩邊用詞要一致:`tests/test_i18n.py`
在 iOS repo 放在隔壁時逐項比對,改了要兩邊一起改。
⚠️ **OpenCC 惰性載入**(同 `convert`):只有簡中介面打開使用說明、或跑產生器時才付辭典成本。
"""
from __future__ import annotations

# iOS `scripts/l10n_sync.py` 的 `ZH_HANS_FIX`,照抄(轉完才套,所以左邊是簡體)。
IOS_FIX = {
    "预设": "默认",
    "脱机": "离线",
    "云碟": "云盘",
    "送出": "发送",
    "资料夹": "文件夹",
}

# 這個工具才有的詞(iOS 用不到的)。⚠️ 順序有意義:長的寫前面,免得短的先換掉一半。
FIX = {
    "记录档": "日志",
    "录音档": "录音文件",   # ⚠️ 要排在「音档」前面:不然「錄音檔」變成「录音频文件」
    "音档": "音频文件",
    "档案": "文件",
    "分群档": "分群文件",
    "转档": "转换",
    "侦测": "检测",
}

_cc = None


def to_hans(text: str) -> str:
    """一段台灣繁體 → 大陸簡體(用語一併換,再套 `IOS_FIX`、`FIX`)。"""
    global _cc
    if _cc is None:
        import opencc

        _cc = opencc.OpenCC("tw2sp")
    s = _cc.convert(text)
    for table in (IOS_FIX, FIX):
        for a, b in table.items():
            s = s.replace(a, b)
    return s


# 長文(使用說明、README)裡**不能轉**的字:檔名與記錄檔裡的原話。它們在磁碟上、在記錄檔裡
# 就是繁體(記錄檔刻意不翻,見 docs/dev/i18n.md),簡中說明寫成「錄音资料不连续」就是叫人去找一句
# 不存在的話。⚠️ 行內程式碼(`…`)整段一律不轉,這張表只收**沒包在反引號裡**的那幾個。
# (安裝/啟動入口 2026-10-22 起是英文檔名,OpenCC 碰不到,不必再列)
KEEP = (
    "錄音資料不連續", "沒有偵測到內容損失", "掉了 N 秒的聲音", "程式版本",
    "以下是網頁程式裡的資料", "錄音_", ".重點.json", "_現場", "_電腦",
    # 用詞替換表的例子本身就是「大陸用詞 → 台灣用詞」,轉成簡體就變成「软件→软件」
    "軟件→軟體", "軟件 → 軟體",
    # App 與桌面捷徑在磁碟上的名字(`brand.APP_TITLE`):說明叫人去找那顆圖示,寫成簡體就找不到
    "AI 文件.MD 轉換器",
)
_HOLD = "{}"


def to_hans_doc(text: str) -> str:
    """一份 Markdown 長文 → 簡中,**行內程式碼與 `KEEP` 裡的字原樣保留**。

    做法:先把要保留的換成私用區字元包住的編號(OpenCC 不碰私用區),轉完再換回來。"""
    import re

    held: list[str] = []

    def hold(m) -> str:
        held.append(m.group(0))
        return _HOLD.format(len(held) - 1)

    pattern = "|".join([r"`[^`\n]+`"] + [re.escape(k) for k in KEEP])
    s = to_hans(re.sub(pattern, hold, text))
    return re.sub("(\\d+)", lambda m: held[int(m.group(1))], s)
