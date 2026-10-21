r"""會議語言:一場會議用哪種語言轉文字,以及每一種語言在管線上的規則。

**動到這裡之前先讀 `docs/dev/i18n.md`**(決策與跟 iOS 版對齊的做法)。

⚠️ **這是「會議語言」不是「介面語言」**(`i18n`):英文介面的人也可能錄繁中會議。逐字稿裡的字
(講者標籤、〔〕標記、段落標題)、轉錄用的語言、要不要繁化與加標點,全部看這一個;畫面上的字
看 `i18n`。兩者唯一的交集是**預設值**:沒有另外選的時候,會議語言跟著介面語言。

規則照抄 iOS 版的 `MeetingLanguage.swift`(使用者 2026-10-18 在 iOS 定、2026-10-21 桌面版照用):
- **繁化只對繁體中文**:簡體中文的會議輸出簡體,英文沒有字可轉
- **英文不跑標點模型**:那顆是中文的,英文的引擎自己就有標點與大小寫,跑了反而會洗成中文標點
- 夾雜英文的國語會議照舊選繁體中文(Apple 沒有中英混合模型,一場只能一種)

⚠️ **這個模組不准 import 任何 UI 模組,也不准在模組層呼叫 `t()`**:轉錄子行程也 import 它。
"""
from __future__ import annotations

ZH_TW, ZH_CN, EN = "zh_TW", "zh_CN", "en"
LANGS = (ZH_TW, ZH_CN, EN)
#: 沒有寫語言的逐字稿(2026-10-21 以前的全部)就是繁體中文
DEFAULT = ZH_TW


def parse(value: str | None) -> str:
    """frontmatter 的 `language:`、子行程指令裡的值 → 三種之一;認不得或沒有就是繁中。

    ⚠️ **寬鬆認**(同 iOS 的 `MeetingLanguage(identifier:)`):手機寫的是 Apple 的 locale 名稱
    (`zh_TW`、`zh_CN`、`en_US`、`en_GB`…),桌面版寫 `en`;`-` 與 `_` 兩種寫法都會出現。"""
    v = (value or "").strip().replace("-", "_")
    if v.startswith(("zh_CN", "zh_Hans")):
        return ZH_CN
    if v.startswith("en"):
        return EN
    return ZH_TW


def from_ui(ui_lang: str) -> str:
    """介面語言 → 預設的會議語言(兩者的代碼刻意相同)。"""
    return parse(ui_lang)


def whisper_code(lang: str) -> str:
    """Whisper(faster-whisper 與 OpenVINO 兩條)要的語言碼。簡繁同一個 `zh`:字形由後處理決定。"""
    return "en" if lang == EN else "zh"


def apple_locale(lang: str) -> str:
    """Apple 語音辨識要的 locale。⚠️ **繁中一定是 `zh_TW`**:舊的轉錄結果都記著它。

    英文先用 `en_US`(iOS 照手機地區挑口音;桌面版的使用者在台灣,地區挑不出英文口音)。"""
    return {ZH_TW: "zh_TW", ZH_CN: "zh_CN", EN: "en_US"}[lang]


def traditionalize(lang: str) -> bool:
    """要不要繁化(s2tw ＋ 用詞替換表 `data/replace.txt`)。只有繁體中文要。"""
    return lang == ZH_TW


def simplify(lang: str) -> bool:
    """要不要把輸出統一成簡體(OpenCC `t2s`)。只有簡體中文要。

    ⚠️ **Whisper 的 `zh` 不保證字形**:同一個 `zh` 會繁簡混著出;簡中會議的成品不該簡繁夾雜。
    (領域詞表 2026-10-21 起不注入簡中會議,見 `inject_hotwords`,所以它不再是繁體字的來源。)"""
    return lang == ZH_CN


def punctuate(lang: str) -> bool:
    """要不要跑中文標點模型。英文不跑(引擎自己有標點,跑了會換成中文標點)。"""
    return lang != EN


def inject_hotwords(lang: str) -> bool:
    """要不要把 `data/hotwords.txt` 的領域詞注入解碼。**只有繁體中文會議**(使用者 2026-10-21)。

    ⚠️ **英文不注**:那份詞表是中文的,塞進英文解碼的 prompt 等於叫模型往中文寫。
    ⚠️ **簡中也不注**:詞表是繁體的、內容是使用者自己公司的詞,使用者裁定「簡體及英文不會作用,
    只有繁體中文」。同一天介面上的「領域詞表」子分頁也拿掉了,詞表改成只用記事本維護檔案。"""
    return lang == ZH_TW
