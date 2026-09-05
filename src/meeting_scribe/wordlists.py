r"""兩份**可以直接編輯的詞表檔**:領域詞表(`hotwords.txt`)與用詞替換表
(`replace.txt`)的讀、存、體檢。

2026-08-29 從 `data_tabs.py` 抽出來(原生介面遷移的階段 4 第一刀)。⚠️ **這個模組
不准 import 任何 UI 模組**——判準與 `naming.py` 那條一樣:不是「講的是不是同一件
事」,是**「換一套 UI 要不要改」**。`data_tabs.py` 在模組層 `import gradio`,所以
原生視窗一碰它就把 gradio 拉進啟動路徑(`tests/test_desktop.py` 釘著)。

⚠️ **體檢回的是結構、不是排版好的字串**,而這是這一刀真正的重點。原本 `data_tabs`
那兩支把警告寫成 `**…**` 直接嵌在句子裡——那是 **Markdown 的詞彙**,Tk 畫不出來
(它會原樣印出四個星號)。所以這裡回 `Status(summary, warning)` 兩截,由各自的 UI
決定怎麼強調:網頁版包成粗體(`data_tabs` 那兩支現在是薄薄的轉接層,輸出**一個字
都沒變**),原生視窗改用警告色。⚠️ **不要「順手」把 Markdown 留在這一層**:留著的
話,winkit 版就得反過來剝星號,而剝到一半的星號沒有人看得出來。

⚠️ **兩份檔案的規矩不同,措辭也不同**,別互相照抄:
  領域詞表**有長度預算**(超出的尾端會被引擎靜默忽略,所以順序即優先序),
    影響的是**轉錄**——文案要寫「下一個轉錄的檔案生效」。
  用詞替換表**沒有預算**,要點名的是「缺新詞」的壞行,而它**兩條路徑都吃**
    (逐字稿與文件轉 md),所以文案是「轉換」不是「轉錄」。

⚠️ **一律純文字原樣存取,不可以拆成表格**:兩份檔的**註解**(收詞原則、預算說明)
與**行序**都有意義,而表格 round-trip 會把兩者一起弄丟。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from meeting_scribe import convert, hotwords


@dataclass(frozen=True)
class Status:
    """一份詞表現在的狀況。

    `summary` 一律要顯示;`warning` 只有出問題時才有東西,而它值得被**強調**
    ——兩種警告(詞表超出預算、替換規則缺新詞)的共同點是**使用者不會自己發現**:
    引擎靜默忽略超出的詞,而缺新詞的行只在黑視窗留一句 warning。"""

    summary: str
    warning: str = ""

    def as_markdown(self) -> str:
        """網頁版要的那一份(警告包成粗體)。⚠️ 兩截之間**不加空白**:原本就是
        直接相接的,加了空白等於改動一句已經在用的文案。"""
        return self.summary + (f"**{self.warning}**" if self.warning else "")


def read(path: Path) -> str:
    """把整份檔案讀成編輯區的文字。檔案不存在就給空的(存檔時才建立)。

    ⚠️ `utf-8-sig`:編輯區讀到的第一個字如果是 BOM,存回去就會多一個
    (同 `attendees.load`,那份 docstring 有完整理由)。"""
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError:
        return ""


def write(path: Path, text: str) -> None:
    """原樣寫回去(含註解與行序)。⚠️ 不是字串就當空的——編輯區在某些狀態下會
    給 `None`,而那時該寫的是空檔、不是讓存檔炸掉。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if isinstance(text, str) else "", encoding="utf-8")


# ---- 領域詞表(hotwords.txt)----------------------------------------------- #
def hotwords_file() -> Path:
    return hotwords.store_file()


def hotwords_status() -> Status:
    """詞表統計與**預算預警**。

    ⚠️ 超出預算的尾端詞會被引擎**靜默截斷**,所以這件事要在存檔當下就講明白
    ——不能等使用者轉完一個小時的檔才發現那幾個詞根本沒生效。"""
    words = hotwords.load()
    if not words:
        return Status("目前詞表為空(功能等同關閉)。")
    joined = "、".join(words)
    summary = (f"目前 {len(words)} 個詞,合計約 {len(joined)} 字"
               f"(建議 ≤{hotwords.WARN_CHARS} 字)。")
    over = len(joined) - hotwords.WARN_CHARS
    if over <= 0:
        return Status(summary)
    return Status(summary,
                  f"已超出約 {over} 字:尾端的詞會被引擎靜默忽略、等於沒加"
                  "——請把重要的詞往前放、刪掉不常用的。")


def save_hotwords(text: str) -> Status:
    write(hotwords_file(), text)
    st = hotwords_status()
    return Status("已儲存,下一個轉錄的檔案生效。" + st.summary, st.warning)


# ---- 用詞替換表(replace.txt)---------------------------------------------- #
def replace_file() -> Path:
    return convert.replace_file()


def replace_status() -> Status:
    """替換表統計;**缺「新詞」的行要點名**。

    ⚠️ `convert` 只會在黑視窗記一句 warning,使用者在介面上完全看不到——而那幾行
    是被整行略過的。解析共用 `convert.parse_rules`,格式變更不會兩邊走鐘。"""
    f = replace_file()
    if not f.exists():
        return Status("目前無替換表(功能等同關閉)。")
    rules, bad = convert.parse_rules(f.read_text(encoding="utf-8-sig"))
    summary = f"目前 {len(rules)} 條替換規則。"
    if not bad:
        return Status(summary)
    return Status(summary,
                  "以下行缺「新詞」(格式:原詞 新詞),會被略過:"
                  + "、".join(f"「{b}」" for b in bad))


def save_replace(text: str) -> Status:
    # ⚠️ 措辭是「轉換」不是「轉錄」:這張表**文件轉檔也吃**(見模組 docstring)
    write(replace_file(), text)
    st = replace_status()
    return Status("已儲存,下一個轉換的檔案生效。" + st.summary, st.warning)
