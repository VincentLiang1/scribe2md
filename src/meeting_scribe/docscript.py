r"""網頁 `<script>` 裡的內容 → 可讀的資料(給 docweb 用)。

**為什麼要讀程式碼**(使用者 2026-09-17 回報):AI 工具做出來的「單檔簡報」
把整份內容藏在 JavaScript 裡,靜態的標籤只剩播放器外殼——

1. **整頁 HTML 以字串塞在物件裡**:`var PAGEDOC = {"./01-cover.html":
   "<!doctype html>…", …}`,放映時才 `iframe.srcdoc = PAGEDOC[file]`。
   實測一份 13 頁的簡報轉出來只剩「‹ › 目錄 G 全屏 F」,而且 `lossy: 0`
   ——正是「無聲失真」。
2. **頁面內文是資料,由程式畫出來**:`window.TEAM_MEMBERS = [{name:…,
   desc:`…`}]`,標籤裡只有一個空的 `<div class="board">`。

**刻意靜態解析、不開瀏覽器執行**:①這類頁面的內容大半要「點選 / 停在
上面」才出現(成員卡點了才顯示職責、時序圖一次只畫一個分頁),就算真的
渲染也只拿得到預設那一格,資料字面值反而是**完整**的;②執行網頁程式
就管不住它連外(隱私規格 spec §7);③不必多一個瀏覽器相依。

**只讀「資料」,不讀「程式」**:判準是**指派的右邊是不是純字面值**
(物件 / 陣列,或宣告成常數的字串)。`el.innerHTML = '<p>載入失敗</p>'`
這種 UI 字串不收——那是程式的一部分,收進來只會讓 RAG 讀到一句從來沒
出現在畫面上的錯誤訊息。資料裡夾了算式(函式、變數)時只跳過那一格、
其餘照收,跳過的格子裡有文字就回報,不安靜地少。

這支模組**不 import bs4**:它只把 JS 讀成 Python 值、再排成 Blocks,遇到
內嵌網頁時交回呼叫端(`on_page`,即 docweb)處理。
"""
import html as html_mod
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from meeting_scribe import docmd
from meeting_scribe.docmd import Block, Note, Para

# ---- 詞法 ----

# 用佔有量詞(`*+` `++`):字串沒收尾時一般寫法會指數回溯,而內嵌整份
# 簡報的字串實測有 63 萬字元
_DQ_RE = re.compile(r'"((?:[^"\\\n]++|\\.)*+)"', re.S)
_SQ_RE = re.compile(r"'((?:[^'\\\n]++|\\.)*+)'", re.S)
_TPL_CHUNK_RE = re.compile(r"[^`\\$]++")
_NUM_RE = re.compile(
    r"0[xXoObB][0-9a-fA-F_]+n?|(?:\d[\d_]*\.?[\d_]*|\.\d[\d_]*)(?:[eE][+-]?\d+)?n?"
)
_IDENT_RE = re.compile(r"[A-Za-z_$\u0080-\uffff][\w$\u0080-\uffff]*")
_PUNCT_RE = re.compile(
    r">>>=|\.\.\.|===|!==|\*\*=|<<=|>>=|>>>|&&=|\|\|=|\?\?=|=>|==|!=|<=|>=|"
    r"\+\+|--|\+=|-=|\*=|/=|%=|&=|\|=|\^=|&&|\|\||\?\?|\?\.|\*\*|<<|>>|[^\s]"
)
_REGEX_RE = re.compile(r"/(?![*/])(?:[^/\\\[\n]++|\\.|\[(?:[^\]\\\n]++|\\.)*+\])++/[A-Za-z]*")
_WS_RE = re.compile(r"(?:\s++|//[^\n]*+|/\*.*?\*/)++", re.S)

# 前一個記號是這些時,`/` 開頭的是正規表示式而不是除號(JS 詞法的慣用判準)
_REGEX_AFTER_PUNCT = frozenset("( , = : [ ! & | ? { } ; + - * % < > ~ ^".split()) | {
    "=>", "==", "===", "!=", "!==", "&&", "||", "??", "+=", "-=", "*=", "/=",
    "<=", ">=", "...",
}
_REGEX_AFTER_WORD = frozenset({
    "return", "typeof", "instanceof", "in", "of", "new", "delete", "void",
    "throw", "case", "do", "else", "yield", "await",
})

_ESCAPE_RE = re.compile(
    r"\\(u\{[0-9a-fA-F]+\}|u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|\r\n|[\s\S])"
)
_SIMPLE_ESCAPES = {
    "n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0",
    "\n": "", "\r\n": "", "\r": "", "\u2028": "", "\u2029": "",
}


def unescape(body: str) -> str:
    r"""JS 字串的跳脫序列 → 字元(`<\/script>` 的 `\/` 也在這裡還原)。"""
    if "\\" not in body:
        return body

    def sub(m: re.Match) -> str:
        e = m.group(1)
        if e in _SIMPLE_ESCAPES:
            return _SIMPLE_ESCAPES[e]
        if e[0] in "ux" and len(e) > 1:
            try:
                return chr(int(e[2:-1] if e.startswith("u{") else e[1:], 16))
            except (ValueError, OverflowError):
                return e
        return e
    return _ESCAPE_RE.sub(sub, body)


@dataclass(frozen=True)
class _Tok:
    kind: str  # str / tpl / num / ident / regex / punct
    value: object  # str 與 tpl:解碼後的文字(含 `${}` 的樣板是 None)
    start: int
    end: int


def _lex(src: str, pos: int, prev: _Tok | None) -> _Tok | None:
    """從 pos 讀一個記號(跳過空白與註解);讀到底回 None。"""
    m = _WS_RE.match(src, pos)
    if m:
        pos = m.end()
    if pos >= len(src):
        return None
    c = src[pos]
    if c == '"' or c == "'":
        m = (_DQ_RE if c == '"' else _SQ_RE).match(src, pos)
        if m:
            return _Tok("str", unescape(m.group(1)), pos, m.end())
        # 沒收尾(壞掉的程式碼):當成一個標點吃掉,不讓整份卡住
        return _Tok("punct", c, pos, pos + 1)
    if c == "`":
        return _template(src, pos)
    if c.isdigit() or (c == "." and src[pos + 1:pos + 2].isdigit()):
        m = _NUM_RE.match(src, pos)
        if m and m.end() > pos:
            return _Tok("num", m.group(0), pos, m.end())
    m = _IDENT_RE.match(src, pos)
    if m:
        return _Tok("ident", m.group(0), pos, m.end())
    if c == "/" and _regex_allowed(prev):
        m = _REGEX_RE.match(src, pos)
        if m:
            return _Tok("regex", m.group(0), pos, m.end())
    m = _PUNCT_RE.match(src, pos)
    return _Tok("punct", m.group(0), pos, m.end())


def _regex_allowed(prev: _Tok | None) -> bool:
    if prev is None:
        return True
    if prev.kind == "punct":
        return prev.value in _REGEX_AFTER_PUNCT
    return prev.kind == "ident" and prev.value in _REGEX_AFTER_WORD


def _template(src: str, pos: int) -> _Tok:
    """樣板字串。含 `${…}` 的不是純資料(值要執行才知道),value 給 None,
    但**一定要正確跳過**——裡面可以再有字串、樣板與大括號。"""
    i, n = pos + 1, len(src)
    parts: list[str] = []
    has_expr = False
    while i < n:
        c = src[i]
        if c == "`":
            text = None if has_expr else unescape("".join(parts))
            return _Tok("tpl", text, pos, i + 1)
        if c == "\\":
            parts.append(src[i:i + 2])
            i += 2
        elif c == "$" and src.startswith("${", i):
            has_expr = True
            i = _skip_braces(src, i + 2)
        elif c == "$":
            parts.append(c)
            i += 1
        else:
            m = _TPL_CHUNK_RE.match(src, i)
            parts.append(m.group(0))
            i = m.end()
    return _Tok("tpl", None, pos, n)  # 沒收尾:吃到底


def _skip_braces(src: str, pos: int) -> int:
    """從 `${` 之後跳到對應的 `}` 之後。"""
    depth, prev = 0, None
    while True:
        t = _lex(src, pos, prev)
        if t is None:
            return len(src)
        pos, prev = t.end, t
        if t.kind == "punct":
            if t.value == "{":
                depth += 1
            elif t.value == "}":
                if depth == 0:
                    return pos
                depth -= 1


# ---- 字面值解析 ----

class _NotData(Exception):
    """這個位置不是純資料(函式、變數、算式)。"""


# 解析失敗而跳過的格子。與 None(JS 的 null)分開:null 是資料,這個是「讀不出來」
SKIPPED = object()
_LITERAL_WORDS = {"true": True, "false": False, "null": None, "undefined": None}
_CLOSERS = {"}", "]", ")"}
# 字串右邊接了這些就是程式在拼字串,不是資料
_NOT_DATA_AFTER = frozenset({
    "+", ".", "[", "(", "?", "?.", "||", "&&", "??", "==", "===", "!=", "!==",
    "<", ">", "<=", ">=", "*", "/", "%", "-",
})


# 這些開頭的格子是**程式**(函式、箭頭函式、類別):跳過時不收裡面的字串
# ——那是 UI 文案(「暫無資料」之類),回報成「讀不出來的內容」只是誤報
_CODE_START = frozenset({"function", "async", "class", "("})


class _Parser:
    def __init__(self, src: str, pos: int, prev: _Tok | None):
        self.src = src
        self.skipped: list[str] = []  # 跳過的**資料**格子裡讀得到的字串
        self.skipped_code = False  # 跳過了函式之類的程式(裡面可能還有資料的指派)
        self.last = prev
        self.tok = _lex(src, pos, prev)

    def advance(self) -> _Tok:
        t = self.tok
        self.last = t
        self.tok = _lex(self.src, t.end, t)
        return t

    def is_punct(self, value: str) -> bool:
        return self.tok is not None and self.tok.kind == "punct" and self.tok.value == value

    def value(self):
        t = self.tok
        if t is None:
            raise _NotData
        if t.kind == "punct":
            if t.value == "{":
                return self.obj()
            if t.value == "[":
                return self.arr()
            if t.value == "-":
                self.advance()
                if self.tok is not None and self.tok.kind == "num":
                    return _number("-" + self.advance().value)
            raise _NotData
        if t.kind == "str" or (t.kind == "tpl" and t.value is not None):
            self.advance()
            return t.value
        if t.kind == "num":
            self.advance()
            return _number(t.value)
        if t.kind == "ident" and t.value in _LITERAL_WORDS:
            self.advance()
            return _LITERAL_WORDS[t.value]
        raise _NotData

    def element(self):
        """容器裡的一格:讀不出來就跳到這一格的結尾,容器照常往下讀。"""
        try:
            return self.value()
        except _NotData:
            self.skip(collect=self.tok is None or self.tok.value not in _CODE_START)
            return SKIPPED

    def skip(self, collect: bool = True) -> None:
        """跳到同一層的 `,` 或收尾括號之前;collect 時順手記下沿途的字串。"""
        depth = 0
        if not collect:
            self.skipped_code = True
        while self.tok is not None:
            t = self.tok
            if t.kind == "punct":
                if depth == 0 and (t.value == "," or t.value in _CLOSERS):
                    return
                if t.value in ("{", "[", "("):
                    depth += 1
                elif t.value in _CLOSERS:
                    depth -= 1
            elif collect and t.kind in ("str", "tpl") and t.value:
                self.skipped.append(t.value)
            self.advance()

    def obj(self) -> dict:
        self.advance()  # {
        out: dict = {}
        while self.tok is not None and not self.is_punct("}"):
            key = self.tok
            if key.kind in ("str", "ident", "num") or (key.kind == "tpl" and key.value is not None):
                self.advance()
            else:
                key = None
            if key is None or not self.is_punct(":"):
                # 簡寫 `{a, b}`、方法 `f(){}`、展開 `...x`、算出來的鍵 `[k]`
                # ——都是程式的寫法,裡面的字串不收
                self.skip(collect=False)
                out[f"\0skipped{len(out)}"] = SKIPPED
            else:
                self.advance()  # :
                out[str(key.value)] = self.element()
            if self.is_punct(","):
                self.advance()
            elif not self.is_punct("}"):
                self.skip()
                if not self.is_punct(","):
                    break
                self.advance()
        if self.is_punct("}"):
            self.advance()
        return out

    def arr(self) -> list:
        self.advance()  # [
        out: list = []
        while self.tok is not None and not self.is_punct("]"):
            if self.is_punct(","):  # 空洞 [1,,2]
                self.advance()
                continue
            out.append(self.element())
            if self.is_punct(","):
                self.advance()
            elif not self.is_punct("]"):
                self.skip()
                if not self.is_punct(","):
                    break
                self.advance()
        if self.is_punct("]"):
            self.advance()
        return out


def _number(text: str):
    t = text.replace("_", "").rstrip("n")
    try:
        return int(t, 0) if re.fullmatch(r"-?(0[xXoObB][0-9a-fA-F]+|\d+)", t) else float(t)
    except ValueError:
        return t


# ---- 找出資料 ----

@dataclass
class Literal:
    """一個被指派成純資料的字面值。name 是給人看的指派對象
    (`TEAM_MEMBERS`、`DOCS["a.md"]`),skipped 是跳過的格子裡讀得到的字串。"""
    name: str
    value: object
    skipped: list[str] = field(default_factory=list)


def scan(src: str) -> list[Literal]:
    """一段 JS → 所有「指派成純資料」的字面值,依出現順序。

    收的三種指派(其餘一律當程式):
    - `var/let/const X = <任何字面值>`
    - `X = {…}` / `a.b.X = […]`(物件、陣列)
    - `X["鍵"] = <任何字面值>`(以字串為鍵填表,自動產生的資料檔常這樣寫)
    """
    out: list[Literal] = []
    window: list[_Tok] = []  # 最近幾個記號,判斷 `=` 左邊是什麼
    pos, prev = 0, None
    while True:
        t = _lex(src, pos, prev)
        if t is None:
            return out
        pos, prev = t.end, t
        if not (t.kind == "punct" and t.value == "="):
            window.append(t)
            if len(window) > 8:
                del window[0]
            continue
        target = _target(window)
        window.clear()
        if target is None:
            continue
        name, strings_ok = target
        parser = _Parser(src, pos, t)
        first = parser.tok
        if first is None:
            return out
        is_container = first.kind == "punct" and first.value in ("{", "[")
        if not is_container and not strings_ok:
            continue
        try:
            value = parser.value()
        except _NotData:
            continue
        after = parser.tok
        if not is_container and after is not None and after.kind == "punct" and after.value in _NOT_DATA_AFTER:
            continue  # `'<div>' + x`:在拼字串
        out.append(Literal(name, value, parser.skipped))
        if parser.skipped_code:
            # 跳過的函式裡可能還有資料的指派(`var app = {init: function(){
            # var DATA = […]}}`):從 `=` 之後接著掃,不要整段略過。字面值
            # 本身沒有 `=`,不會被重複收
            continue
        if after is None:
            return out
        pos, prev = after.start, parser.last


def _target(window: list[_Tok]) -> tuple[str, bool] | None:
    """`=` 左邊 → (顯示名, 是否也收字串)。認不出來回 None。"""
    if not window:
        return None
    last = window[-1]
    if last.kind == "ident":
        before = window[-2] if len(window) >= 2 else None
        if before is not None and before.kind == "ident" and before.value in ("var", "let", "const"):
            return last.value, True
        # a.b.C:往回收點號串,顯示時拿掉 window. 前綴
        parts = [last.value]
        i = len(window) - 2
        while i >= 1 and window[i].kind == "punct" and window[i].value == "." and window[i - 1].kind == "ident":
            parts.insert(0, window[i - 1].value)
            i -= 2
        if parts[0] == "window" and len(parts) > 1:
            parts = parts[1:]
        return ".".join(parts), False
    if (
        last.kind == "punct" and last.value == "]" and len(window) >= 3
        and window[-2].kind == "str" and window[-3].kind == "punct" and window[-3].value == "["
    ):
        base = window[-4].value if len(window) >= 4 and window[-4].kind == "ident" else ""
        return f'{base}["{window[-2].value}"]', True
    return None


# ---- 判斷與排版 ----

_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
_HTML_DOC_RE = re.compile(r"\s*(?:<!--.*?-->\s*)*(?:<!doctype\s+html|<html[\s>])", re.I | re.S)
_TAG_RE = re.compile(r"<[A-Za-z!/][^>]*>")
_BLOCK_END_RE = re.compile(r"<br\s*/?>|</(?:p|div|li|h[1-6]|tr|section|article|header|footer)\s*>", re.I)
_NON_TEXT_RE = re.compile(r"<(script|style|svg)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_MD_DOC_RE = re.compile(r"^[ \t]{0,3}(?:#{1,6}[ \t]|```|~~~|\|?[ \t]*:?-{3,}:?[ \t]*\|)", re.M)
# 一個字面值至少要有一格「像句子」才算內容:**四個字以上的中日韓文字**,或
# **四個以空白隔開的英文單字**。門檻是拿 310 份真實網頁校準的:
# - 單字詞清單(簡報隨機占位用的姓氏「林、沈、顧」、2 字的名字)擋在外面;
# - 英文原本只要兩個單字,結果 slides.com 匯出檔的帳號設定(姓名、字型名)
#   整包被當成內容。要求**空白**隔開則擋掉網址與字型清單
#   (`"PingFang SC","Hiragino Sans GB"` 的逗號接縫不算空白)
_MIN_CJK = 4
_WORDS_RE = re.compile(r"[A-Za-z]{2,}(?:[ \t]+[^\sA-Za-z]*[A-Za-z]{2,}[^\sA-Za-z]*){3,}")
# 壓縮過的程式碼(函式庫)**不收文字資料**:扣掉字串之後平均每行的程式
# 字元數。310 份真實網頁實測:手寫的頁面程式 23~29,函式庫 4,965~50,896
# (highlight.js 的關鍵字表、zlib 的錯誤訊息、reveal.js 的按鈕說明,原本全
# 會被當成內容)。另設總量下限:一行寫完的短小 inline script 不算壓縮
_MINIFIED_CODE_PER_LINE = 120
_MINIFIED_MIN_CODE = 2000


def is_html_document(text: str) -> bool:
    """整份網頁(不是片段):開頭就是 doctype 或 `<html>`。"""
    return bool(_HTML_DOC_RE.match(text))


def looks_like_prose(text: str) -> bool:
    return len(_CJK_RE.findall(text)) >= _MIN_CJK or bool(_WORDS_RE.search(text))


def is_minified(src: str) -> bool:
    """壓縮過的程式碼(見 `_MINIFIED_CODE_PER_LINE`)。字串不算程式字元:
    整份簡報以一行 JSON 塞進來時那一行有 63 萬字,但幾乎全是字串。"""
    strings, pos, prev = 0, 0, None
    while (t := _lex(src, pos, prev)) is not None:
        if t.kind in ("str", "tpl"):
            strings += t.end - t.start
        pos, prev = t.end, t
    code = len(src) - strings
    return code >= _MINIFIED_MIN_CODE and code / (src.count("\n") + 1) > _MINIFIED_CODE_PER_LINE


def html_title(doc: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title\s*>", doc, re.I | re.S)
    return re.sub(r"\s+", " ", html_mod.unescape(m.group(1))).strip() if m else ""


def plain_text(text: str) -> str:
    """一格字串 → 純文字。帶標籤的是 HTML 片段:區塊結尾換行、其餘剝掉。"""
    if _TAG_RE.search(text):
        text = _NON_TEXT_RE.sub(" ", text)
        text = _TAG_RE.sub("", _BLOCK_END_RE.sub("\n", text))
        text = html_mod.unescape(text)
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return "\n".join(ln for ln in lines if ln.strip()).strip("\n")


def _leaves(value):
    if isinstance(value, dict):
        for v in value.values():
            yield from _leaves(v)
    elif isinstance(value, list):
        for v in value:
            yield from _leaves(v)
    elif isinstance(value, str):
        yield value


def has_pages(lit: Literal) -> bool:
    return any(is_html_document(s) for s in _leaves(lit.value))


def has_prose(lit: Literal) -> bool:
    """內嵌網頁以外,至少一格像句子。"""
    return any(
        not is_html_document(s) and looks_like_prose(plain_text(s))
        for s in _leaves(lit.value)
    )


def skipped_prose(lit: Literal) -> list[str]:
    """跳過的格子裡像句子的字串——這些是**讀不出來而沒轉**的內容。"""
    return [p for p in (plain_text(s) for s in lit.skipped) if looks_like_prose(p)]


def demote_headings(text: str, levels: int) -> str:
    """markdown 的 ATX 標題整體下推 levels 層(上限 6),圍籬裡的不算。

    資料裡的 markdown(例如嵌進簡報的規則文件)自己就有 H1,原樣放進來會
    跟檔案的標題搶階層(`docmd.render` 規則 1)。"""
    out: list[str] = []
    fence: str | None = None
    for line in text.split("\n"):
        m = re.match(r"^\s{0,3}(```|~~~)", line)
        if m:
            fence = None if fence == m.group(1) else (fence or m.group(1))
        elif not fence:
            h = re.match(r"^(#{1,6})([ \t])", line)
            if h:
                line = "#" * min(len(h.group(1)) + levels, 6) + line[len(h.group(1)):]
        out.append(line)
    return "\n".join(out)


def _is_scalar(v) -> bool:
    return v is None or isinstance(v, (str, int, float, bool))


def _scalar_text(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return plain_text(v)
    return "" if v is None else str(v)


def _is_page(v) -> bool:
    return isinstance(v, str) and is_html_document(v)


def _is_md_doc(v) -> bool:
    """整份 markdown(有標題、圍籬或表格):放不進清單,另起一段。"""
    return isinstance(v, str) and not is_html_document(v) and bool(_MD_DOC_RE.search(v))


@dataclass
class _Deferred:
    """清單裡放不下、要接在這一筆後面的東西:整頁網頁或整份 markdown。"""
    label: str
    text: str
    is_page: bool


def _list_lines(value, depth: int, label: str, deferred: list[_Deferred]) -> list[str]:
    """資料 → markdown 巢狀清單(`- 鍵:值`,同 `docmd.render_records` 的寫法)。

    多行的文字接在項目底下縮排;整頁網頁與整份 markdown 放不進清單(裡面有
    標題),先記進 deferred,由呼叫端接在這一筆後面。"""
    pad = "  " * depth
    out: list[str] = []
    keyed = isinstance(value, dict)
    items = list(value.items()) if keyed else list(enumerate(value, 1))
    for k, v in items:
        if v is SKIPPED or v is None:
            continue
        head = f"{pad}- {k}:" if keyed else f"{pad}- "
        where = f"{label} · {k}" if keyed else f"{label} · 第 {k} 項"
        if _is_page(v) or _is_md_doc(v):
            deferred.append(_Deferred(where, v, _is_page(v)))
            what = "一整頁網頁,內容" if _is_page(v) else "一份文件,全文"
            out.append(f"{head}({what}接在下方)")
        elif _is_scalar(v):
            lines = _scalar_text(v).split("\n")
            if not lines[0]:
                continue
            if len(lines) == 1:
                out.append(head + lines[0])
            else:
                out.append(head.rstrip())
                out.extend(f"{pad}  {ln}" for ln in lines)
        elif isinstance(v, list) and v and all(
            _is_scalar(x) and not _is_page(x) and not _is_md_doc(x) and "\n" not in _scalar_text(x)
            for x in v
        ):
            texts = [t for t in (_scalar_text(x) for x in v) if t]
            if texts:
                out.append(head + "、".join(texts))
        else:
            # 只有一個元素的清單不另起「第 1 項」那一層:那一行只是雜訊
            inner = v[0] if isinstance(v, list) and len(v) == 1 and isinstance(v[0], (dict, list)) else v
            sub = _list_lines(inner, depth + 1, where, deferred)
            if sub:
                out.append(head.rstrip() if keyed else f"{pad}- 第 {k} 項")
                out.extend(sub)
    return out


def _records(name: str, value) -> list[tuple[str, object]]:
    """一個字面值切成「筆」:每筆一段,段首有標籤。

    RAG 切塊後 chunk 會脫離上下文(`docmd.render` 規則 2),所以陣列的每個
    元素、以檔名/鍵為索引的表(`DOCS["a.md"]`)的每一格各自成段、各自帶
    出處;單純的一組設定值則整個一段。"""
    def big(v) -> bool:
        return isinstance(v, (dict, list)) or _is_page(v) or _is_md_doc(v)

    if isinstance(value, list):
        items = [v for v in value if v is not SKIPPED and v is not None]
        if any(big(v) for v in items):
            return [(f"{name} · 第 {i} 筆(共 {len(items)} 筆)", v) for i, v in enumerate(items, 1)]
    if isinstance(value, dict):
        items = [(k, v) for k, v in value.items() if v is not SKIPPED and v is not None]
        if items and all(big(v) for _, v in items):
            return [(f"{name} · {k}", v) for k, v in items]
    return [(name, value)]


def _document_blocks(label: str, text: str, heading_depth: int) -> list[Block]:
    """資料裡的整份 markdown:標籤一行 + 原文(標題下推到頁面內容之下)。"""
    body = demote_headings(text.replace("\r\n", "\n").replace("\r", "\n").strip("\n"), heading_depth + 3)
    return [Para(f"**{docmd.one_line(label)}**"), Para(body)]


def literal_blocks(
    lit: Literal,
    on_page: Callable[[str, str], list[Block]],
    heading_depth: int = 0,
    text_ok: bool = True,
) -> list[Block]:
    """一個字面值 → Blocks。內嵌的整頁網頁交給 on_page(label, html)。

    text_ok=False(壓縮過的程式碼)時只取內嵌網頁,文字資料不收。

    heading_depth 是這段程式所在頁面的標題深度:資料裡的 markdown 標題要
    排在那一頁自己的標題底下,否則會跟頁面的章節搶階層。"""
    lost = skipped_prose(lit) if text_ok else []
    if not (text_ok and has_prose(lit)):
        out: list[Block] = []
        for label, page in _page_leaves(lit.name, lit.value):
            out.extend(on_page(label, page))
        return out + _lost_note(lit.name, lost)

    out = [Note(
        f"以下是網頁程式裡的資料「{lit.name}」:原本由程式放到畫面上"
        "(有些要點選或停在上面才看得到),這裡照資料原樣列出",
        docmd.KIND_SCRIPT_DATA, lossy=False,
    )]
    for label, value in _records(lit.name, lit.value):
        deferred: list[_Deferred] = []
        if _is_page(value) or _is_md_doc(value):
            deferred.append(_Deferred(label, value, _is_page(value)))
        elif label == lit.name:
            # 整個字面值就一段:`- 名稱:值`,不另加標籤行
            lines = _list_lines({lit.name: value}, 0, label, deferred)
            if lines:
                out.append(Para("\n".join(lines)))
        else:
            is_container = isinstance(value, (dict, list))
            text = "\n".join(_list_lines(value, 0, label, deferred)) if is_container else _scalar_text(value)
            if text:
                out.append(Para(f"**{docmd.one_line(label)}**"))
                out.append(Para(text))
        for d in deferred:
            if d.is_page:
                out.extend(on_page(d.label, d.text))
            else:
                out.extend(_document_blocks(d.label, d.text, heading_depth))
    return out + _lost_note(lit.name, lost)


def _lost_note(name: str, lost: list[str]) -> list[Block]:
    """資料裡夾在算式中、讀不出來的文字 → 失真標記。

    **每一格都讀不出來的字面值也要標**(例如整批用 `t('…')` 包起來做翻譯):
    那種情形沒有任何一格看得懂,只在「有內容可列」時才標的話,它正好整個
    安靜地消失。"""
    if not lost:
        return []
    examples = "、".join(f"「{docmd.one_line(s)[:20]}」" for s in lost[:3])
    return [Note(
        f"網頁程式裡的資料「{name}」有 {len(lost)} 段文字夾在程式碼裡、"
        f"沒有轉出來(例如 {examples})——請開啟原始檔查看",
        docmd.KIND_SCRIPT_DATA,
    )]


def _page_leaves(label: str, value):
    """(標籤, 整頁網頁) 依出現順序。"""
    if _is_page(value):
        yield label, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _page_leaves(f"{label} · {k}", v)
    elif isinstance(value, list):
        for i, v in enumerate(value, 1):
            yield from _page_leaves(f"{label} · 第 {i} 項", v)
