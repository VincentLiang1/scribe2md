r"""把「使用說明」那十篇的 Markdown 解析成區塊,給原生視窗畫。

2026-08-29 新增(原生介面遷移的階段 4)。⚠️ **這個模組不准 import 任何 UI 模組**
——它只回**資料**(區塊與行內片段),怎麼畫是各自 UI 的事(同 `naming.py`／
`wordlists.py` 那條:判準是「換一套 UI 要不要改」)。

⚠️ **這不是一個通用的 Markdown 引擎,也不該變成一個。** 十篇 22,126 字丟進
markdown-it 數過,實際用到的只有**七種**語法:

| 語法 | 出現次數 | 備註 |
| --- | --- | --- |
| 標題 | h2×10、h3×59 | **只有兩層** |
| 段落 | 189 | |
| 無序清單 | 24 組 70 項 | **巢狀深度 1** |
| 表格 | **3 張** | 3 欄 / 2 欄 / 2 欄 |
| 程式碼區塊 | 2 | |
| 行內粗體 | 280 | |
| 行內 code | 68 | |

**完全沒用到**連結、斜體、有序清單、內嵌圖片、刪除線。所以這裡要蓋住的是七種語法
——多寫的每一種都是沒有人用的程式碼,而它跟刻意留的東西長得一模一樣。
⚠️ **哪天說明文字用了第八種語法,這裡會安靜地把它當成普通文字印出來**(例如
`[連結](網址)` 會原樣顯示中括號)。`tests/test_helpmd.py` 反向守著:掃真的那十篇,
出現沒支援的語法就紅。

⚠️ **不要改用 markdown-it 之類的套件**:它現在是 gradio 的傳遞相依,而 gradio 正要
從本 repo 移除——真要用就得在 `pyproject` 明寫一行,為了七種語法多一個相依不划算。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


# 變化選擇符。⚠️ **要在畫進 Tk 之前剝掉**:說明文字是網頁版寫的,那邊的 emoji 帶著
# 它才會顯示成彩色字形;而 Tk 對「本身沒有 emoji 樣式」的字符會把它畫成一個 30px 寬
# 的方塊(2026-08-29 實測,一般空白是 6px),於是目錄與內文到處是說不出理由的縫。
# ⚠️ **剝在這一層、不去改 `help_text`**:那份文字兩套介面共用,而網頁版需要它。
VS16 = "️"


def plain(text: str) -> str:
    """把一段文字整理成 Tk 畫得對的樣子(目前就是剝掉 VS16)。"""
    return text.replace(VS16, "")


@dataclass(frozen=True)
class Span:
    """一段行內文字與它的樣式。`style` 是 `""`(一般)/`"bold"`/`"code"`。"""

    text: str
    style: str = ""


@dataclass(frozen=True)
class Block:
    """一個區塊。

    `kind` ∈ `heading` / `para` / `bullet` / `code` / `table`。
    `level` 只有 `heading` 用得到(2 或 3);`rows` 只有 `table` 用得到。
    """

    kind: str
    spans: tuple[Span, ...] = ()
    level: int = 0
    rows: tuple[tuple[tuple[Span, ...], ...], ...] = ()
    text: str = ""


# 行內:`code` 先切,粗體其次。⚠️ **順序不能反**:反引號裡面的 `**` 是**字面值**
# (說明文字裡真的有一句在講星號本身),先解粗體會把它吃掉。
_INLINE = re.compile(r"`([^`]+)`|\*\*(.+?)\*\*")


def spans(text: str) -> tuple[Span, ...]:
    """把一行文字切成 (文字, 樣式) 的序列。"""
    text = plain(text)
    out: list[Span] = []
    pos = 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            out.append(Span(text[pos:m.start()]))
        out.append(Span(m.group(1), "code") if m.group(1) is not None
                   else Span(m.group(2), "bold"))
        pos = m.end()
    if pos < len(text):
        out.append(Span(text[pos:]))
    return tuple(out)


def flatten(md: str) -> str:
    r"""把一段文字攤平成 Tk 畫得對的純文字:剝掉 VS16、`**` 與反引號,行結構保留。

    ⚠️ **給那些「寫給網頁版看」的字串用的**(`doctab.preview_summary`、
    `docpipe.report_markdown`、`dry_run_lines`):它們回的是 Markdown,而 Tk 不渲染
    ——不剝的話畫面上就是「已選 `**2 個檔案**`」那樣,四個星號原樣印出來。
    ⚠️ **逐行做、不要整段丟進 `parse()`**:那一支會把連續行接成一個段落,而這些字串
    的換行是內容(一行一個檔)。"""
    return "\n".join("".join(s.text for s in spans(line)) for line in md.split("\n"))


def _cells(line: str) -> tuple[tuple[Span, ...], ...]:
    """把表格的一列切成幾格。⚠️ 兩端的 `|` 要先剝掉,不然頭尾各多一個空格。"""
    return tuple(spans(c.strip()) for c in line.strip().strip("|").split("|"))


def _is_divider(line: str) -> bool:
    """表格標題底下那一行(`| --- | --- |`)。"""
    body = line.strip().strip("|")
    return bool(body) and all(set(c.strip()) <= set("-: ") and "-" in c
                              for c in body.split("|"))


def parse(md: str) -> list[Block]:
    r"""把一篇說明解析成區塊序列。

    ⚠️ **段落是「連續的非空行接成一行」**:本 repo 的 `.md` 一律不硬折行(一個段落
    一行),但說明文字是 Python 字串、換行是寫在原始碼裡的,所以照樣要接。接的時候
    **不補空白**——中文之間補空白會多出縫。"""
    out: list[Block] = []
    lines = md.split("\n")
    i = 0
    para: list[str] = []

    def flush() -> None:
        if para:
            out.append(Block("para", spans("".join(para))))
            para.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            flush()
            i += 1
        elif stripped.startswith("```"):
            flush()
            i += 1
            body: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1                      # 收尾那一行圍欄
            out.append(Block("code", text="\n".join(body)))
        elif stripped.startswith("#"):
            flush()
            level = len(stripped) - len(stripped.lstrip("#"))
            out.append(Block("heading", spans(stripped[level:].strip()), level=level))
            i += 1
        elif stripped.startswith(("- ", "* ")):
            flush()
            out.append(Block("bullet", spans(stripped[2:])))
            i += 1
        elif (stripped.startswith("|") and i + 1 < len(lines)
              and _is_divider(lines[i + 1])):
            flush()
            rows = [_cells(stripped)]
            i += 2                      # 標題列 + 分隔列
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_cells(lines[i].strip()))
                i += 1
            out.append(Block("table", rows=tuple(rows)))
        else:
            para.append(stripped)
            i += 1
    flush()
    return out


# 這個子集**沒有**支援、而且說明文字裡也確實沒用到的語法。⚠️ 反向守著:哪天有人在
# 說明裡寫了連結,`tests/test_helpmd.py` 會紅——不然它會被當成普通文字原樣印出來。
UNSUPPORTED = {
    "連結": re.compile(r"(?<!!)\[[^\]]+\]\([^)]+\)"),
    "內嵌圖片": re.compile(r"!\[[^\]]*\]\([^)]+\)"),
    "有序清單": re.compile(r"^\s*\d+\.\s", re.M),
    "刪除線": re.compile(r"~~.+?~~"),
    # ⚠️ 斜體要避開粗體(`**x**` 的內外都是星號)與程式碼區塊裡的字面值,所以只認
    # 「單獨一顆星號貼著非空白」而且兩側都不是星號的那種
    "斜體": re.compile(r"(?<![*\w])\*(?!\*)[^*\n]+\*(?!\*)"),
}


def unsupported(md: str) -> list[str]:
    """這段文字用到了哪幾種本模組不支援的語法(空 = 都在子集內)。"""
    return [name for name, pat in UNSUPPORTED.items() if pat.search(md)]
