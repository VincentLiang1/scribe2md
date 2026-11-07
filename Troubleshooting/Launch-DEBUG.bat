@echo off
chcp 950 >nul
setlocal
rem 這支住在「Troubleshooting」子資料夾裡,那兩點是把工作目錄帶回專案根,與「Launch.vbs」
rem 的 sh.CurrentDirectory = here 對齊。
rem 【這個檔不是產生出來的】「Launch.vbs」由 scripts/make_launcher.py 產生,這一支是
rem 手寫的:它是 .bat、規矩完全不同(cp950、CRLF、chcp 950 在第一個中文之前),而且
rem 它存在的理由正是「那一支連訊息框都跳不出來的時候」——兩支同源反而少了一條退路。
rem 訊息語言:中文系統印繁中、其他一律英文(判準與 Install.bat 同一份,見檔尾的 :ui_lang)。
rem 注意:這支讀不到工具裡選的介面語言(那在 settings.json,.bat 不解析它),只看
rem Windows 的語言清單;簡中系統也印繁中(cp950 寫不出簡體字)。訊息放在 M_ 變數裡,值裡
rem 不可以有半形右括號與 cmd 的四個特殊符號。test_bat.py 守著。
cd /d %~dp0..
call :ui_lang
if "%UI%"=="en" goto msg_en
set "M_START=啟動中,轉換器的視窗隨即開啟。"
set "M_DBG1=【這一支是除錯用的】平常請用上一層資料夾的「Launch.vbs」,或桌面與「開始」"
set "M_DBG2=功能表那顆圖示。只有在那條路連視窗都開不起來、或它跳出的訊息框看不出原因"
set "M_DBG3=時,才改用這一支:錯誤訊息會完整留在這個黑框裡,訊息框只顯示得下最後幾"
set "M_DBG4=百個字。"
set "M_KEEP=本視窗請不要關閉,關掉它會一併結束程式。平常這裡不會有任何訊息,那是正常的。"
set "M_MODEL0=【第一次轉檔會先下載 AI 模型】"
set "M_MODEL1=  語音辨識與講者分離的模型合計約 2 到 3GB,只下載一次。"
set "M_MODEL2=  下載期間畫面上的訊息會停在「首次使用:下載模型…」看似卡住,請耐心等候。"
set "M_END=【程式已結束】上面的訊息看完再關閉這個視窗。"
goto msgs_set
:msg_en
set "M_START=Starting; the Scribe2MD window will open in a moment."
set "M_DBG1=[This one is for troubleshooting] Normally use Launch.vbs in the folder above, or the icon"
set "M_DBG2=on the desktop and in the Start menu. Use this one only when that way cannot even open the"
set "M_DBG3=window, or its message box does not say why: the full error stays in this black window,"
set "M_DBG4=while the message box only fits the last few hundred characters."
set "M_KEEP=Do not close this window; closing it also ends the tool. Normally nothing shows up here, which is fine."
set "M_MODEL0=[The first conversion downloads the AI models first]"
set "M_MODEL1=  The speech recognition and speaker models total about 2 to 3 GB and download only once."
set "M_MODEL2=  While downloading, the status may stay at 'First use: downloading ...' and look stuck; please wait."
set "M_END=[The tool has exited] Read the messages above, then close this window."
:msgs_set
echo %M_START%
echo.
echo %M_DBG1%
echo %M_DBG2%
echo %M_DBG3%
echo %M_DBG4%
echo.
echo %M_KEEP%
echo.
echo %M_MODEL0%
echo %M_MODEL1%
echo %M_MODEL2%
echo.
uv run python -m meeting_scribe.desktop
rem 【一定要停,不可以只在 errorlevel 1 才停】這一支存在的理由就是「把訊息完整
rem 留在這個框裡」,而 Python 正常結束時離開碼是 0——只在非零時停的話,收尾那
rem 幾行字會隨視窗一起消失。使用者回報的正是「有字但是很快就一起關閉了」。
echo.
echo %M_END%
pause
goto :eof

rem ---- 介面語言(Install.bat、Install-Skill.bat、Troubleshooting\Launch-DEBUG.bat 各一份,test_bat.py 釘著一字不差)----
rem 讀「設定 - 時間與語言」的語言清單(與工具介面的 plat_win.preferred_languages 同一份),
rem 第一個認得的勝出:zh 開頭 = 中文、en 開頭 = 英文,都不認得就英文(同 i18n.match)。
rem 讀不到清單就照舊印中文。MEETING_SCRIBE_UI_LANG 同 i18n.ENV_OVERRIDE(測試用)。
:ui_lang
set "UI=zh"
if /i "%MEETING_SCRIBE_UI_LANG%"=="en" (set "UI=en"& goto :eof)
if not "%MEETING_SCRIBE_UI_LANG%"=="" goto :eof
set "LANGS="
rem 注意:不接 find、reg 寫完整路徑:PATH 裡常有別的工具帶來的同名程式排在系統的前面
rem (2026-10-23 實測:某台的 find 把 REG_MULTI_SZ 當成檔名,印一行 os error 2)。
for /f "tokens=1,2,*" %%a in ('%SystemRoot%\System32\reg.exe query "HKCU\Control Panel\International\User Profile" /v Languages 2^>nul') do if /i "%%b"=="REG_MULTI_SZ" set "LANGS=%%c"
if not defined LANGS goto :eof
set "UI="
for %%t in (%LANGS:\0= %) do call :ui_pick %%t
if not defined UI set "UI=en"
goto :eof

:ui_pick
if defined UI goto :eof
set "T=%~1"
if /i "%T:~0,2%"=="zh" set "UI=zh"
if /i "%T:~0,2%"=="en" set "UI=en"
goto :eof
