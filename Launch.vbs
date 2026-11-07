' Launch.vbs —— 開起會議助手的視窗，全程沒有黑框。
'
' 與「Troubleshooting\Launch-DEBUG.bat」的差別只在後者留一個主控台視窗顯示啟動訊息；
' 這一邊用 pythonw 啟動、並把主控台整個藏起來，所以桌面上只看得到程式自己的視窗。
'
' 代價是原本會印在那個視窗的東西（uv 的錯誤、Python 在 import 期就炸的
' traceback）沒有落點。作法：先收進系統暫存資料夾的一個暫存檔，程式沒能正常
' 結束時「當場把內容跳訊息框顯示出來」，然後把暫存檔刪掉——專案資料夾裡不留
' 任何東西。
'
' 【注意】這裡攔的是「視窗還沒能力做任何事」的那一段。程式自己的執行紀錄是另
' 一回事：由外層寫進專案底下的 logs 資料夾，一次執行一個檔、保留 30 天。
'
' 開頭那段「少了什麼檔案」的檢查擋的是這個部署方式唯一的失敗模式：整包複製搬家
' 時漏掉東西。uv 與 Python 吐得出來的是英文的建置／匯入錯誤，那份訊息照樣會被下
' 面攔下來跳出來，但它說不出「你少複製了什麼」——而那是使用者唯一能動手修的事。
'
' 【這個檔是產生出來的，不要手改】骨架住在 winkit（launcher.vbs.tmpl），這一份是
' 它套上本專案的欄位之後的產物。改了骨架或欄位就重跑 scripts/make_launcher.py，
' 產物跟著程式碼一起進版（同 make_skin.py / make_icon.py 的規矩）。
'
' 【啟動的快路與退路】環境還是新的時候，直接跑 .venv 裡的 pythonw，跳過 uv 每次
' 啟動都要做的專案解析與 lock 比對。換掉 uv run 就等於換掉它順手做的「環境沒同步
' 就自動補起來」，所以退路有兩道、缺一不可：事前看 site-packages 的時間戳（任何一
' 項輸入比它新就整個走 uv），事後看「非正常結束、而且不到 5 秒就結束」（那是 .venv
' 被整包複製到另一台機器、而那台沒有同一版 Python 的樣子）。兩道各自的理由寫在
' 下面它們自己那段。
Option Explicit

' MsgBox 大約 1024 個字元就會被截掉，而有用的部分（例外的最後幾行）在尾巴
Const MAX_MSG = 900
Const APP_TITLE = "會議助手電腦版"
Const APP_TITLE_EN = "Notetaker Desktop"

Dim sh, fso, here, q, capPath, cmd, rc, out, msg, missing
Dim usedFast, started, spent
Dim uiEn, ind

Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

here    = fso.GetParentFolderName(WScript.ScriptFullName)
q       = Chr(34)
' 攔訊息用的暫存檔放系統暫存資料夾（GetSpecialFolder(2)）：專案資料夾裡不留東西
capPath = fso.BuildPath(fso.GetSpecialFolder(2).Path, fso.GetTempName())

sh.CurrentDirectory = here

' 【複製不完整的守門】要擋的是什麼、為什麼要擋，寫在檔頭那一段。
' 【注意】這裡列的路徑必須真的存在於專案裡，否則守門會在「正常的安裝」上誤報，
' 而畫面上那句話還信誓旦旦地說「請把整個專案資料夾完整複製過來」——使用者照做也
' 不會好。清單由 scripts/make_launcher.py 給，那一支旁邊有測試釘著這一條。
' 【英文介面】跟程式自己同一套順序判斷介面語言（見檔尾的 UiEnglish）。英文介
' 面的訊息框一個中文字都不准有，連全形空格也算，所以守門清單的縮排也跟著換。
uiEn = UiEnglish(sh, fso)
If uiEn Then
    ind = "    "
Else
    ind = "　　"
End If

missing = ""
If Not fso.FileExists(fso.BuildPath(here, "pyproject.toml")) Then
    missing = missing & vbCrLf & ind & "pyproject.toml"
End If
If Not fso.FileExists(fso.BuildPath(here, "src\meeting_scribe\desktop.py")) Then
    missing = missing & vbCrLf & ind & "src\meeting_scribe\desktop.py"
End If
If Not fso.FileExists(fso.BuildPath(here, "data-default\replace.txt")) Then
    missing = missing & vbCrLf & ind & "data-default\replace.txt"
End If
If Not fso.FileExists(fso.BuildPath(here, "winkit\pyproject.toml")) Then
    missing = missing & vbCrLf & ind & "winkit\pyproject.toml"
End If
If uiEn And Len(missing) > 0 Then
    MsgBox "Some required files are missing from this folder:" & vbCrLf & missing & vbCrLf & vbCrLf & _
           here & vbCrLf & vbCrLf & _
           "Copy the whole project folder again, then run ""Install.bat"" once more." & vbCrLf & _
               "(Paths starting with .. are the shared folder next to the project; copy it together with the project.)", _
           vbCritical, APP_TITLE_EN
    WScript.Quit 1
End If
If Len(missing) > 0 Then
    MsgBox "這個資料夾裡少了必要的檔案：" & vbCrLf & missing & vbCrLf & vbCrLf & _
           here & vbCrLf & vbCrLf & _
           "請把整個專案資料夾完整複製過來，再執行一次「Install.bat」。" & vbCrLf & _
           "（上面若列出 .. 開頭的路徑，那是隔壁的共用資料夾，要跟專案一起複製。）", _
           vbCritical, APP_TITLE
    WScript.Quit 1
End If

' 子行程一律用 UTF-8 輸出，攔到的訊息才解得回來（主控台預設是 cp950，中文
' traceback 直接讀會是亂碼）。改的是本行程的環境區塊，Run 出去的子行程繼承它。
sh.Environment("PROCESS")("PYTHONIOENCODING") = "utf-8"

' 透過 cmd /c 才有重導向：WScript.Shell.Run 自己不支援 > 與 2>&1。
' 【注意】cmd /c 的引號規則：整串外層包一對引號，內層路徑照常用各自的引號。
' 寫成兩個雙引號想跳脫是錯的——cmd 不吃那套，而專案路徑含中文與可能的空格，
' 這一點錯了就是「雙擊沒反應」。
cmd = WrapCmd("uv run pythonw -m meeting_scribe.desktop", q, capPath)
usedFast = False
' 【快速路徑】環境還是新的就直接跑 .venv 裡的 pythonw，跳過 uv 的專案解析與
' lock 比對（實測那一層 32ms）。環境有任何一點對不上就不走這條，讓 uv 去補。
If EnvFresh(fso, here) Then
    cmd = WrapCmd(q & here & "\.venv\Scripts\pythonw.exe" & q & " -m meeting_scribe.desktop", q, capPath)
    usedFast = True
End If

' 【注意】兩個參數都不可改：0 = SW_HIDE（cmd 與 uv 都看不到），True = 等它
' 結束——不等就拿不到結束碼，也就沒辦法在失敗時跳訊息框。代價是 wscript 行程
' 會活到視窗關閉為止，這是刻意的。
On Error Resume Next
started = Timer
rc = sh.Run(cmd, 0, True)
If Err.Number <> 0 Then
    If uiEn Then
        MsgBox "Could not start the program (" & Err.Description & ")." & vbCrLf & vbCrLf & _
               "Make sure uv is installed (https://docs.astral.sh/uv/)," & vbCrLf & _
               "then run ""Install.bat"" to set up the environment.", vbCritical, APP_TITLE_EN
        Cleanup fso, capPath
        WScript.Quit 1
    End If
    MsgBox "無法啟動程式（" & Err.Description & "）。" & vbCrLf & vbCrLf & _
           "請先確認已安裝 uv（https://docs.astral.sh/uv/），" & vbCrLf & _
           "再執行「Install.bat」建立環境。", vbCritical, APP_TITLE
    Cleanup fso, capPath
    WScript.Quit 1
End If
On Error GoTo 0

' 【事後退路】快速路徑非正常結束、而且不到 5 秒就結束，就用 uv 再跑一次。
' 這一道接的是事前那道看不見的情況：.venv 被整包複製到另一台機器、但那台沒有同一
' 版 Python——檔案都在、時間也對，而 pythonw.exe 根本起不來。
' 【注意】5 秒是往安全那邊靠：使用者真的用過視窗的話，光是選檔就不只 5 秒，所以
' 「用到一半才出錯」不會被誤判成環境壞掉而重跑一次（那會讓視窗開兩次）。
' 【注意】Timer 是「今天過了幾秒」，跨午夜會歸零、讓差變成負的，所以補 +86400。
If usedFast And rc <> 0 Then
    spent = Timer - started
    If spent < 0 Then spent = spent + 86400
    If spent < 5 Then
        cmd = WrapCmd("uv run pythonw -m meeting_scribe.desktop", q, capPath)
        rc = sh.Run(cmd, 0, True)
    End If
End If

If uiEn And rc <> 0 Then
    out = Captured(fso, capPath)
    msg = "Notetaker Desktop did not exit normally (exit code " & rc & ")." & vbCrLf & vbCrLf
    If Len(out) > 0 Then
        msg = msg & out
    Else
        msg = msg & "No message was captured. If this is the first run, run ""Install.bat"" first; " & _
              "If it still happens, run ""Launch-DEBUG.bat"" in the ""Troubleshooting"" folder; the messages will show in that window."
    End If
    MsgBox msg, vbCritical, APP_TITLE_EN
    Cleanup fso, capPath
    WScript.Quit
End If
If rc <> 0 Then
    out = Captured(fso, capPath)
    msg = "會議助手沒有正常結束（結束碼 " & rc & "）。" & vbCrLf & vbCrLf
    If Len(out) > 0 Then
        msg = msg & out
    Else
        msg = msg & "沒有攔到任何訊息。若是第一次使用，請先執行「Install.bat」；" & _
              "仍然這樣的話，改用「Troubleshooting」資料夾裡的「Launch-DEBUG.bat」，訊息會直接顯示在那個視窗裡。"
    End If
    MsgBox msg, vbCritical, APP_TITLE
End If

Cleanup fso, capPath


' 把要跑的那一串包成 cmd /c 的形狀。透過 cmd /c 才有重導向：WScript.Shell.Run
' 自己不支援 > 與 2>&1。
Function WrapCmd(inner, q, capPath)
    WrapCmd = "cmd /c " & q & inner & " > " & q & capPath & q & " 2>&1" & q
End Function

' 環境還新不新：.venv\Lib\site-packages 的修改時間，不比下面那幾個輸入舊。
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
    If Not fso.FolderExists(here & "\.venv\Lib\site-packages") Then Exit Function
    If Not fso.FileExists(here & "\.venv\Scripts\pythonw.exe") Then Exit Function
    stamp = fso.GetFolder(here & "\.venv\Lib\site-packages").DateLastModified
    If Not NotNewer(fso, here & "\pyproject.toml", stamp) Then Exit Function
    If Not NotNewer(fso, here & "\uv.lock", stamp) Then Exit Function
    If Not NotNewer(fso, here & "\winkit\pyproject.toml", stamp) Then Exit Function
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

' 介面是不是英文：跟程式自己同一套順序——環境變數、使用者在程式裡選過的語言、
' 系統的顯示語言。前兩項的值不認得就當沒設，往下一項問。
' 【注意】任何一步讀不到都不可以讓這支啟動器停下來：它只決定訊息框用哪一種語言。
Function UiEnglish(sh, fso)
    Dim v, text, re, found
    On Error Resume Next
    v = sh.Environment("PROCESS")("MEETING_SCRIBE_UI_LANG")
    If LangChoice(v) >= 0 Then
        UiEnglish = (LangChoice(v) = 1)
        Exit Function
    End If
    text = ReadUtf8(fso, sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\meeting-scribe\settings.json")
    Set re = New RegExp
    re.Pattern = """ui_language""\s*:\s*""([^""]*)"""
    Set found = re.Execute(text)
    If Err.Number = 0 And found.Count > 0 Then
        v = found(0).SubMatches(0)
        If LangChoice(v) >= 0 Then
            UiEnglish = (LangChoice(v) = 1)
            Exit Function
        End If
    End If
    Err.Clear
    UiEnglish = SystemEnglish(sh)
End Function


' 1 = 英文、0 = 中文、-1 = 不認得（當沒設）。
Function LangChoice(v)
    LangChoice = -1
    Select Case v
        Case "en"
            LangChoice = 1
        Case "zh_TW", "zh_CN"
            LangChoice = 0
    End Select
End Function


' 系統的顯示語言是不是英文。清單裡第一個 zh 或 en 勝出，一個都沒有就英文（日文、
' 韓文的人看英文）。
' 【注意】讀不到登錄檔就回 False（繁中）：那是加英文之前的行為，失效往舊的倒。
' 【注意】問的是登錄檔，不是程式用的 GetUserPreferredUILanguages（VBScript 叫不
' 到）：使用者改過顯示語言才有 PreferredUILanguages，沒有就看安裝時的語言。
Function SystemEnglish(sh)
    Dim langs, i, tag, seen
    SystemEnglish = False
    On Error Resume Next
    langs = sh.RegRead("HKCU\Control Panel\Desktop\PreferredUILanguages")
    If Err.Number <> 0 Or Not IsArray(langs) Then
        Err.Clear
        langs = sh.RegRead("HKCU\Control Panel\Desktop\MuiCached\MachinePreferredUILanguages")
    End If
    If Err.Number <> 0 Or Not IsArray(langs) Then Exit Function
    seen = False
    For i = LBound(langs) To UBound(langs)
        tag = LCase(Replace(Trim(langs(i)), "_", "-"))
        If Len(tag) > 0 Then
            seen = True
            If tag = "zh" Or Left(tag, 3) = "zh-" Then Exit Function
            If tag = "en" Or Left(tag, 3) = "en-" Then
                SystemEnglish = True
                Exit Function
            End If
        End If
    Next
    SystemEnglish = seen
End Function


' 讀一個 UTF-8 文字檔（設定檔就是 UTF-8 寫的）；讀不到回空字串，等於「沒選過」。
' 【注意】不共用上面那支 Captured：它只留最後 MAX_MSG 個字，設定檔裡別的值一長，
' 語言那一項就被切掉了。
Function ReadUtf8(fso, path)
    Dim st
    ReadUtf8 = ""
    If Not fso.FileExists(path) Then Exit Function
    On Error Resume Next
    Set st = CreateObject("ADODB.Stream")
    st.Type = 2 : st.Charset = "utf-8" : st.Open
    st.LoadFromFile path
    ReadUtf8 = st.ReadText
    st.Close
    If Err.Number <> 0 Then ReadUtf8 = ""
    Err.Clear
End Function


' 讀回攔到的訊息。優先用 ADODB.Stream 解 UTF-8（子行程就是用 UTF-8 寫的）；
' 那個元件被停用時退回 FileSystemObject —— 中文會變亂碼，但 traceback 的骨架
' 仍讀得出來，比什麼都不顯示好。
Function Captured(fso, path)
    Dim st, f, text
    Captured = ""
    If Not fso.FileExists(path) Then Exit Function

    text = ""
    On Error Resume Next
    Set st = CreateObject("ADODB.Stream")
    If Err.Number = 0 Then
        st.Type = 2 : st.Charset = "utf-8" : st.Open
        st.LoadFromFile path
        text = st.ReadText
        st.Close
    End If
    If Err.Number <> 0 Or Len(text) = 0 Then
        Err.Clear
        Set f = fso.OpenTextFile(path, 1)
        If Err.Number = 0 Then
            If Not f.AtEndOfStream Then text = f.ReadAll
            f.Close
        End If
    End If
    On Error GoTo 0

    text = Trim(text)
    If uiEn And Len(text) > MAX_MSG Then
        Captured = "(The message is long; only the last " & MAX_MSG & " characters are shown.)" & _
                   vbCrLf & vbCrLf & Right(text, MAX_MSG)
        Exit Function
    End If
    If Len(text) > MAX_MSG Then
        text = "（訊息很長，以下只顯示最後 " & MAX_MSG & " 個字）" & _
               vbCrLf & vbCrLf & Right(text, MAX_MSG)
    End If
    Captured = text
End Function


' 暫存檔一律刪掉：它只是「把訊息端到訊息框」的通道，不是要留下來的東西。
Sub Cleanup(fso, path)
    On Error Resume Next
    If fso.FileExists(path) Then fso.DeleteFile path, True
    On Error GoTo 0
End Sub