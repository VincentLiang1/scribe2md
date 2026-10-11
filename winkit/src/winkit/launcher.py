r"""無黑框啟動器(`.vbs`)的骨架與產生器。

**為什麼需要它**:`.vbs` 沒有 import,所以「機制」與「這支 app 的內容」只能靠樣板
拆開——不拆的話兩支程式各自演化,而那正是三個 repo 漂開的老路(`make_skin.py` 一支
差 763 行)。骨架在 `launcher.vbs.tmpl`,欄位由下游那支 `scripts/make_launcher.py` 給。

骨架裡的五條規則(全部是**踩過**才知道的):

1. **`cmd /c` 的引號**:整串外層包一對引號,內層路徑照常用各自的引號。寫成兩個雙
   引號想跳脫是錯的——cmd 不吃那套,而專案路徑含中文與可能的空格,這一點錯了就是
   「雙擊沒反應」。
2. **`Run(cmd, 0, True)` 兩個參數都不可改**:`0` = SW_HIDE(cmd 與 uv 都看不到),
   `True` = 等它結束——不等就拿不到結束碼,也就沒辦法在失敗時跳訊息框。代價是
   wscript 行程會活到視窗關閉為止,那是刻意的。
3. **子行程要指定 UTF-8 輸出**:主控台預設是 cp950,中文 traceback 直接讀回來是亂碼。
4. **MsgBox 大約 1024 個字元就被截掉**,而有用的部分(例外的最後幾行)在尾巴,所以
   只顯示最後 `MAX_MSG` 個字。
6. **啟動的快路與兩道退路**(2026-08-28,為了 MP4-2-SRT 的「按下圖示到視窗出現」):
   環境還新的時候直接跑 `.venv\Scripts\pythonw.exe`,跳過 `uv run` 每次啟動的專案
   解析與 lock 比對(在 MP4-2-SRT 實測那一層是 32ms:`uv run python -c pass` 66ms
   對上直接用 venv 的 python 34ms)。⚠️ **換掉 `uv run` 就等於換掉它順手做的「環境
   沒同步就自動補起來」**,所以退路兩道缺一不可——事前比 `site-packages` 的時間戳、
   事後看「非正常結束而且不到 5 秒」。由 `run_target` 決定開不開(不給就完全是舊
   行為)。

5. **「複製不完整」的守門**:整包複製搬家時漏掉東西,uv 與 Python 吐的是英文的建置
   /匯入錯誤——那份訊息照樣會被攔下來跳出來,但它說不出「你少複製了東西」,而那正是
   這種部署方式唯一的失敗模式。清單由下游給(⚠️ 列的路徑必須真的存在,否則守門會在
   **正常的安裝**上誤報,而那時使用者照著訊息重新複製也不會好)。
"""
from pathlib import Path

TEMPLATE = Path(__file__).with_name("launcher.vbs.tmpl")

# 快速路徑跑的那顆解譯器(相對專案根)。
# ⚠️ **不可以改用 `[project.scripts]` 產生的那顆 `.exe`**:console script 是 console
# 子系統,黑視窗會回來——而藏掉黑框正是這支啟動器存在的唯一理由。
FAST_EXE = r".venv\Scripts\pythonw.exe"

# 事前那道退路要比對的輸入(相對專案根)。每個 uv 專案都有這兩個;走相對路徑相依的
# 專案要再加上那份 `pyproject.toml`(相依變了才要重裝,所以看的是它、不是原始碼)。
FRESH_INPUTS = ("pyproject.toml", "uv.lock")

# 走相對路徑相依的專案,守門訊息要多的那一句(傳給 `render(guard_note=...)`)。
# ⚠️ **住這裡、不由下游各寫一份**:A/B 判準問的是「另一支需要跟我長得不一樣嗎」,而
# 這句話解釋的是「清單裡為什麼會有 `..` 開頭的路徑」——那是這套部署方式的性質,不是
# 哪一支 app 的性質,所以它**不可能**不一樣(同一份骨架、同一種相依宣告)。旁邊那幾個
# 文字欄位剛好相反:`app_noun`、`no_output_hint`、`debug_launcher` 各家真的不同。
# ⚠️ 仍然是 opt-in:沒有相對路徑相依的下游不該拿到它,所以由呼叫端傳、不是預設值。
PATH_DEP_GUARD_NOTE = "（上面若列出 .. 開頭的路徑，那是隔壁的共用資料夾，要跟專案一起複製。）"


def _quoted(field: str, value: str) -> str:
    r"""把一段文字變成放得進 `.vbs` 的形狀:擋掉換行,把雙引號加倍。

    ⚠️ **VBScript 的字串字面值與註解都不跨行**:值裡混進一個換行,字面值就斷在半路、
    註解的第二行變成一句要被執行的指令——兩種都是**載入期**的語法錯誤,而那時
    `render()` 早就回傳位元組、產物早就跟著提交了,症狀是使用者雙擊時 Windows Script
    Host 跳一個英文的語法錯誤。⚠️ **不改成「靜靜把換行拿掉」**:那會讓訊息少講一句話
    而沒有人知道(同下面 cp950 那條「不准靜默換成 `?`」的理由)。⚠️ CR 一起擋:
    `render()` 最後那道 `\n` → `\r\n` 是無條件的,值裡自帶的 CR 會變成 `\r\r\n`,而
    wscript 讀到裸 CR 會拒收。

    ⚠️ **VBScript 沒有反斜線跳脫**,字面值裡的一個 `"` 只能寫成 `""`(或 `Chr(34)`)
    ——原樣放進去的話那個引號會把字面值提早關掉,一樣是載入期的語法錯誤,一樣要等到
    使用者雙擊才看得到。回傳的是**引號之間那一段**,外面那對引號留給骨架自己寫。

    ⚠️ **落在註解裡的欄位也走這一支**,不另外開一條「只擋換行」的路:理由(以及那條
    界線在這份骨架上切不開的證據)寫在 `render()` 組欄位的地方。
    """
    if "\n" in value or "\r" in value:
        raise ValueError(f"{field} 裡有換行,而 .vbs 的字串與註解都不跨行:{value!r}")
    return value.replace('"', '""')


def _check_rc(value: int | None) -> None:
    r"""`self_reported_rc` 的守門。⚠️ **只擋 `None` 是不夠的**——這個值會原樣內插進
    `Const RC_SELF_REPORTED = ...`,而下面每一種都產得出來、都不會當場抱怨:

    * `1` / `2`:直譯器自己會回的值(見 `_self_blocks`)。撞上去的下場是**每一個**未
      攔到的例外都走進「安靜收工」,traceback 連同暫存檔一起被刪掉——雙擊沒反應,而
      那正是最需要跳框的那一次。
    * `0`:正常結束也會命中,「自報過」這件事等於沒有意義。
    * `True`:`bool` 是 `int` 的子類,會寫成 `Const RC_SELF_REPORTED = True`,而
      VBScript 的 `True` 是 -1。
    * 負數:量過(2026-08-28)——Windows 上 `sys.exit(-1)` 回來是 `4294967295`,所以
      下游根本回不出負的結束碼,比對永遠不成立。(`300` 是原樣回來的,所以上限不設。)
    * 字串:`Option Explicit` 底下那是一個沒有定義的識別字,**整支啟動器載不起來**。
    """
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"self_reported_rc 要是 int:{value!r}")
    # 上一行之後只剩 int,所以「0、1、2 與所有負數」接起來就是這一段區間。
    if value <= 2:
        raise ValueError(
            f"self_reported_rc 不可以是 {value}——0 是正常結束、1 是未攔到的例外、"
            "2 是連 .py 都打不開,負數在 Windows 上回不來")


def _fast_blocks(*, run_cmd: str, run_target: str, fast_exe: str,
                 fresh_inputs, self_ok: str) -> dict[str, str]:
    r"""快速路徑那三段 VBScript。`run_target` 沒給就三段都空——**產物與加這個功能
    之前逐位元組相同**,那是「只准加、不准改語意」的實際形狀。

    ⚠️ **`.venv` 底下的路徑一律字串串接,不可以用 `fso.BuildPath(here, ...)`**:
    下游的測試拿 `BuildPath` 的第一個參數當「複製不完整的守門清單」在抓,而那份清單
    列的路徑必須真的存在(否則守門會在正常的安裝上誤報)。而 `.venv` 不在是**合法**
    狀態——它的答案是安靜退回 uv,不是跳一個「你少複製了東西」的錯誤框。

    ⚠️ **`self_ok`(事後退路要不要排除自報碼)是拿現成的、不是在這裡判的**:那個
    條件式參照 `RC_SELF_REPORTED`,而定義它的是 `_self_blocks`。兩支各判一次
    「有沒有給 `self_reported_rc`」的話,改動其中一邊就產得出「參照了沒有定義的識別
    字」的 `.vbs`——`Option Explicit` 底下那是**每一次啟動**都爆,而且爆在程式已經跑
    完之後。
    """
    if not run_target:
        return {"FAST_DOC": "", "FAST_SETUP": "", "FAST_RETRY": "",
                "FAST_FUNCS": ""}

    run_cmd = _quoted("run_cmd", run_cmd)
    run_target = _quoted("run_target", run_target)
    fast_exe = _quoted("fast_exe", fast_exe)
    fresh_inputs = [_quoted("fresh_inputs", rel) for rel in fresh_inputs]
    checks = "\n".join(
        rf'    If Not NotNewer(fso, here & "\{rel}", stamp) Then Exit Function'
        for rel in fresh_inputs)
    venv_lib = r"\.venv\Lib\site-packages"
    return {
        # ⚠️ 檔頭那段說明也跟著 opt-in:沒接快速路徑的下游,產物裡一個字都不該
        # 提到它——文件說謊比沒有文件更難查(`tests/test_launcher.py` 釘著)。
        "FAST_DOC": """'
' 【啟動的快路與退路】環境還是新的時候，直接跑 .venv 裡的 pythonw，跳過 uv 每次
' 啟動都要做的專案解析與 lock 比對。換掉 uv run 就等於換掉它順手做的「環境沒同步
' 就自動補起來」，所以退路有兩道、缺一不可：事前看 site-packages 的時間戳（任何一
' 項輸入比它新就整個走 uv），事後看「非正常結束、而且不到 5 秒就結束」（那是 .venv
' 被整包複製到另一台機器、而那台沒有同一版 Python 的樣子）。兩道各自的理由寫在
' 下面它們自己那段。""",
        "FAST_SETUP": rf"""' 【快速路徑】環境還是新的就直接跑 .venv 裡的 pythonw，跳過 uv 的專案解析與
' lock 比對（實測那一層 32ms）。環境有任何一點對不上就不走這條，讓 uv 去補。
If EnvFresh(fso, here) Then
    cmd = WrapCmd(q & here & "\{fast_exe}" & q & " {run_target}", q, capPath)
    usedFast = True
End If""",
        "FAST_RETRY": f"""' 【事後退路】快速路徑非正常結束、而且不到 5 秒就結束，就用 uv 再跑一次。
' 這一道接的是事前那道看不見的情況：.venv 被整包複製到另一台機器、但那台沒有同一
' 版 Python——檔案都在、時間也對，而 pythonw.exe 根本起不來。
' 【注意】5 秒是往安全那邊靠：使用者真的用過視窗的話，光是選檔就不只 5 秒，所以
' 「用到一半才出錯」不會被誤判成環境壞掉而重跑一次（那會讓視窗開兩次）。
' 【注意】Timer 是「今天過了幾秒」，跨午夜會歸零、讓差變成負的，所以補 +86400。
If usedFast And rc <> 0{self_ok} Then
    spent = Timer - started
    If spent < 0 Then spent = spent + 86400
    If spent < 5 Then
        cmd = WrapCmd("{run_cmd}", q, capPath)
        rc = sh.Run(cmd, 0, True)
    End If
End If
""",
        "FAST_FUNCS": rf"""' 環境還新不新：.venv\Lib\site-packages 的修改時間，不比下面那幾個輸入舊。
' 任何一項比它新就整個走 uv，讓 uv run 順手做掉「環境沒同步就自動補起來」——那件事在快速路徑上
' 沒有別人做了。
' 【注意】比的是 site-packages 這個資料夾，不是 pyvenv.cfg：後者只在建立環境的那
' 一次寫，之後 uv sync 裝了什麼、拿掉什麼都不會動到它，拿它當時間戳等於永遠回答
' 「環境是新的」。
' 【注意】這一支的失效方向是「誤判成舊的」：uv sync 沒事可做時不會動 site-packages，
' 於是連改個註解都會讓它回 False——那只是退回改動前的行為，慢一點而已，不會壞。
' 反過來（該補環境卻回 True）才危險，而事後那道退路正是為它準備的。
Function EnvFresh(fso, here)
    Dim stamp
    EnvFresh = False
    If Not fso.FolderExists(here & "{venv_lib}") Then Exit Function
    If Not fso.FileExists(here & "\{fast_exe}") Then Exit Function
    stamp = fso.GetFolder(here & "{venv_lib}").DateLastModified
{checks}
    EnvFresh = True
End Function


' 這個檔不比 stamp 新。
' 【注意】不存在的檔案不構成「環境過期」的理由：uv.lock 在還沒同步過的專案裡就
' 不存在，而那種情況該由 EnvFresh 開頭那兩道（site-packages、pythonw）擋下來。
Function NotNewer(fso, path, stamp)
    NotNewer = True
    If Not fso.FileExists(path) Then Exit Function
    NotNewer = (fso.GetFile(path).DateLastModified <= stamp)
End Function

""",
    }


def guard_block(paths) -> str:
    r"""把「這幾個檔案必須在」變成守門用的 VBScript 片段。

    ⚠️ 縮排用的是**全形空格**(訊息框裡列出來才對得齊,半形在比例字型下會歪)。
    ⚠️ 路徑一律寫 Windows 的反斜線形式——它會原樣進 `fso.BuildPath`,也會原樣顯示
    給使用者看,而使用者要拿它去檔案總管裡找。"""
    out = []
    for rel in paths:
        rel = _quoted("guards", rel)
        out.append(f'If Not fso.FileExists(fso.BuildPath(here, "{rel}")) Then')
        out.append(f'    missing = missing & vbCrLf & "　　{rel}"')
        out.append("End If")
    return "\n".join(out)


def _self_blocks(self_reported_rc) -> tuple[dict[str, str], str]:
    r"""「視窗自己已經跳過訊息框了」那個暗號(opt-in)。

    下游的 app 有時候比啟動器更清楚失敗原因(NotebookLM_OCR 的「同層找不到套件」會
    把資料夾路徑一起印出來),那時它自己跳框、回這個結束碼,啟動器就安靜收工——不然
    使用者會為同一件事看到兩個框(使用者 2026-08-25 指示)。

    ⚠️ **值本身有守門**,擋掉哪些、為什麼,見 `_checked_rc`。
    ⚠️ **也不可以無條件回它**:Tk 起不來的機器上那等於什麼都沒說,所以下游那半是
    「框真的跳出來才回這個值,跳不出來回 1」。⚠️ **那半個約定在下游、這裡看不到**
    ——本函式產的 `SELF_QUIT` 會把攔到的訊息連同暫存檔一起刪掉,所以下游一旦破約
    (回了這個碼、框卻沒跳出來),使用者手上什麼都不剩。破約的形狀不是假想的:
    NotebookLM_OCR 的 `fail_no_project()` 2026-08-28 之前是靠「`root.destroy()` 之後
    才 `return True`」來回報,`destroy()` 一拋例外就會回報反了。
    ⚠️ 量過而**否決**的補救(2026-08-28):在這個分支裡「攔到東西就照樣跳框」。它在
    NotebookLM_OCR 上是反效果——那支在跳框**之前**一定先寫一份 stderr(那是框跳不
    出來時唯一的落點,也是終端機使用者要看的),所以照約定走的每一次都會攔到東西、
    每一次都跳第二個框,正好是這個暗號要消滅的東西。守約的責任留在下游那半。

    ⚠️ 沒給的下游,產物裡一個字都不會提到這個功能(`tests/test_launcher.py` 釘著)。

    回傳兩樣:**佔位符字典**,以及事後退路那段要不要排除自報碼的**條件式**——後者參照
    上面那個 `Const`,所以由同一支發放(見下面 `self_ok` 那段)。⚠️ 不把條件式塞進字典
    當第三把鑰匙:那會讓「這一鍵不是佔位符、呼叫端要記得拿走」變成一條沒有人在守的約定
    ,而忘了拿走是**靜默的**——骨架裡沒有 `{{SELF_OK}}`,那次替換只是一趟空轉。
    """
    _check_rc(self_reported_rc)
    if self_reported_rc is None:
        return {"SELF_CONST": "", "SELF_QUIT": ""}, ""
    # ⚠️ **自報過的失敗不可以重跑**(2026-08-28,為了 NotebookLM_OCR):那個結束碼的
    # 意思是「視窗已經自己跳過訊息框了」,而事後那道退路的前提是「視窗根本沒開起來」
    # ——兩者都長得像「不到 5 秒就非正常結束」,分不開的話使用者會看到同一個框跳兩次。
    # ⚠️ **這個條件式由這裡發放、`_fast_blocks` 只是原樣貼上**:定義(下面那個 `Const`)
    # 與參照必須同進同出,理由見 `_fast_blocks` 的說明。
    self_ok = " And rc <> RC_SELF_REPORTED"
    # ⚠️ 產物裡的標點一律**全形**(骨架其餘部分就是這樣)：這幾段是使用者／維護者
    # 會讀到的 `.vbs`，不是本 repo 的 Python 註解，兩種風格混在同一個檔案裡很醒目。
    return {
        "SELF_CONST": f"""
' 這個結束碼是與程式講好的暗號：「我自己已經把訊息跳出來了，你不必再說一次」。
' 【注意】不可以改成 1 或 2：那兩個是直譯器自己會回的值（1 = 未攔到的例外、
' 2 = 連 .py 都打不開，也就是只複製了這個 .vbs 的情況），撞上去會讓那些真的需要
' 顯示的失敗被靜靜吞掉。
Const RC_SELF_REPORTED = {self_reported_rc}""",
        "SELF_QUIT": """' 程式自己已經說明過了，這裡再跳一個「結束碼 N」的框只是噪音。
' 安靜收工，但結束碼照傳出去（誰呼叫這支就看得出它失敗了）。
' 【注意】這一段必須留在下面那個「沒有正常結束」的訊息框前面：擺到後面去的話兩
' 個框都會跳，而那正是這個結束碼要消滅的東西。
If rc = RC_SELF_REPORTED Then
    Cleanup fso, capPath
    WScript.Quit rc
End If

""",
    }, self_ok


def render(*, app_title: str, app_noun: str, launcher_name: str,
           install_bat: str, run_cmd: str,
           no_output_hint: str, guards, debug_launcher: str = "",
           guard_note: str = "", self_reported_rc: int | None = None,
           generator: str = "scripts/make_launcher.py", run_target: str = "",
           fast_exe: str = FAST_EXE, fresh_inputs=FRESH_INPUTS) -> bytes:
    r"""套上欄位,回傳可以直接寫成 `.vbs` 的位元組(**cp950、CRLF、無 BOM**)。

    ⚠️ **那三條編碼規則沒有一條會在存檔時抱怨,而症狀都不像編碼問題**:UTF-8 存的
    `.vbs` 中文會變亂碼、BOM 會被 wscript 當成第一行的一部分。

    ⚠️ **cp950 編不出來的字元一律當場丟例外**,不准靜默換成 `?`:骨架與訊息裡混進
    一個 Big5 沒有的字(全形破折號以外的排版符號、emoji)時,產物會**成功**寫出來,
    而使用者看到的是訊息框裡一個問號——那時已經離現場很遠了。

    三個 opt-in 欄位(`debug_launcher`／`guard_note`／`self_reported_rc`)不給就一個
    字都不會進產物。⚠️ **那不是省事,是「只准加、不准改語意」的實際形狀**:另一個
    下游會**靜默地**拿到新版骨架(editable 相依),它的產物必須逐位元組沒變。
    ⚠️ **本 repo 的測試釘不住那句「逐位元組沒變」**,只釘得住「opt-in 的東西一個字
    都沒進去」——真正逐位元組在比的是下游那條「出貨的產物 == `build()`」,而它要等到
    你去跑那個 repo 才會紅。2026-08-28 就是這樣:同一筆改動順手重寫了骨架檔頭,
    MP4-2-SRT 的產物 8,884 → 8,923 bytes,這裡全綠。

    ⚠️ **每個文字欄位都會被檢查、被跳脫**(`_quoted`):`.vbs` 的字串與註解都不跨行,
    而字面值裡的 `"` 只能寫成 `""`。違反的話產物照樣寫得出來,而症狀是使用者雙擊時
    Windows Script Host 跳一個英文的語法錯誤。

    ⚠️ **`self_reported_rc` 的另一半義務在下游、這裡強制不了**:那個結束碼的意思是
    「視窗自己已經把訊息框跳出來了」,所以**只有框真的跳出來才准回它**,跳不出來要回
    1。回錯的話啟動器會安靜收工、並把攔到的訊息連同暫存檔一起刪掉——使用者手上什麼都
    不剩。完整的理由(含一個量過而否決的補救)見 `_self_blocks`。
    """
    text = TEMPLATE.read_text(encoding="utf-8")
    # `RC_SELF_REPORTED` 的定義與參照都由這一支發放:第二個回傳值是事後退路那段要不要
    # 排除自報碼的條件式,轉手交給 `_fast_blocks` 原樣貼上(理由見那支的說明)。
    self_blocks, self_ok = _self_blocks(self_reported_rc)

    # ⚠️ **下面兩個字典的分界是「要跳脫的值」與「已經是 VBScript 的片段」**,不是排版:
    # `values` 逐項套 `_quoted`,`blocks` 原樣貼。新加一個文字欄位**必須**落進其中一個,
    # 而選哪一個是當場看得見的——原本是每個欄位各自手寫一次 `_quoted(...)`,漏包一個是
    # **靜默的**:產物照樣寫得出來、三個 repo 照樣全綠,壞在使用者雙擊那一刻。而這個包
    # 的成長方式正是「每隔幾天多一個 opt-in 文字欄位」(`debug_launcher`、`guard_note`、
    # `generator`、`no_output_hint` 全是這樣來的)。
    # ⚠️ **一律當字面值處理,不按「落在註解還是字面值」分兩種**:`APP_NOUN` 同時出現在
    # 骨架第 1 行(註解)與那句「沒有正常結束」(字面值),那條界線在這份骨架上根本切不
    # 開。代價只是註解裡的引號會顯示成兩個,而會走到這裡的值是檔名、路徑與中文句子。
    values = {
        "APP_TITLE": app_title,
        "APP_NOUN": app_noun,
        "LAUNCHER_NAME": launcher_name,
        # 產物開頭那句「改了就重跑這一支」要指得到真的那一支。⚠️ 預設值是兩個下游
        # 裡先接的那個的位置(MP4-2-SRT: `scripts/`);NotebookLM_OCR 的 `tools/` 自己
        # 傳。⚠️ **留預設是為了「只准加、不准改語意」**——加一個必填參數會讓還沒接的
        # 下游當場 TypeError,而那個下游在這裡看不到。
        "GENERATOR": generator,
        "INSTALL_BAT": install_bat,
        "RUN_CMD": run_cmd,
        "NO_OUTPUT_HINT": no_output_hint,
    }
    blocks = {
        # ⚠️ 沒有 DEBUG 啟動器的下游,產物裡不可以提到它(NotebookLM_OCR 2026-08-25
        # 把那支刪了,理由是「交付給使用者的檔案要盡量少」)——那句話會變成一條指向
        # 不存在的檔案的指路,而讀到它的人正在找東西。
        "DEBUG_DOC": (f"\n' 與「{_quoted('debug_launcher', debug_launcher)}」的"
                      "差別只在後者留一個主控台視窗顯示啟動訊息；"
                      if debug_launcher else ""),
        "GUARDS": guard_block(guards),
        # 守門訊息的補充句(opt-in)。⚠️ 走相對路徑相依的專案需要它:清單裡那條
        # `..\某某` 不在專案資料夾底下,前一句「請把整個專案資料夾完整複製過來」
        # 照做也不會好。整句由 `PATH_DEP_GUARD_NOTE` 供應,見那裡。
        "GUARD_NOTE": (f' & vbCrLf & _\n           "{_quoted("guard_note", guard_note)}"'
                       if guard_note else ""),
        **_fast_blocks(run_cmd=run_cmd, run_target=run_target,
                       fast_exe=fast_exe, fresh_inputs=fresh_inputs,
                       self_ok=self_ok),
        **self_blocks,
    }
    # 佔位符名就是參數名的大寫,所以錯誤訊息裡那個欄位名不必再手寫第二份。
    fields = {**{k: _quoted(k.lower(), v) for k, v in values.items()}, **blocks}
    for key, value in fields.items():
        text = text.replace("{{" + key + "}}", value)

    if "{{" in text:
        left = {w.split("}}")[0] for w in text.split("{{")[1:]}
        raise KeyError(f"骨架裡還有沒填的欄位:{sorted(left)}")

    # ⚠️ 下面那道 `\n` → `\r\n` 是**無條件**的,所以這裡一個 CR 都不可以有:混進一個
    # 就變成 `\r\r\n`,而 wscript 讀到裸 CR 會拒收。⚠️ **既有的編碼測試抓不到它**
    # ——它比的是 `count(b"\n") == count(b"\r\n")`,而 `\r\r\n` 讓兩邊各加一、照樣相等
    # (2026-08-28 量過:塞一個 CR 進去,那條測試 149 == 149 全綠)。欄位那一側由
    # `_quoted` 擋著,這一道守的是「骨架或未來的新欄位繞過了它」。
    if "\r" in text:
        raise ValueError("套完的骨架裡有 CR:換行一律交給最後那道 `\\n` → `\\r\\n`")

    try:
        return text.replace("\n", "\r\n").encode("cp950")
    except UnicodeEncodeError as e:
        # ⚠️ `e.start`／`e.end` 指的是 **encode 吃進去的那一份**(已經換過 CRLF、比
        # `text` 長,每多一行就多一個字元),拿它去切 `text` 會指到別的字元、算出別的
        # 行號——而這個處理器存在的唯一理由就是把人帶到現場(2026-08-28 修正:原本
        # 切的是 `text`,實測會把第 92 行的 🎬 報成「第 100 行的『跑』」)。
        bad = e.object[e.start:e.end]
        line = e.object[:e.start].count("\n") + 1
        raise UnicodeEncodeError(
            e.encoding, e.object, e.start, e.end,
            f"cp950 編不出 {bad!r}(第 {line} 行)——.vbs 只能用 Big5 有的字") from None
