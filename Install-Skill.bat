@echo off
setlocal
chcp 950 >nul
rem 本檔存成 cp950(Big5),chcp 必須在任何中文之前——理由見「Install.bat」開頭。
cd /d "%~dp0"

rem 訊息語言:中文系統印繁中、其他一律英文(判準見檔尾的 :ui_lang)。注意:簡中系統也印
rem 繁中:這個檔是 cp950,寫不出簡體字(使用者 2026-10-23 同意)。注意:訊息放在 M_ 變數裡,
rem 而變數在區塊解析「之前」就展開,所以值裡不可以有半形右括號與 cmd 的四個特殊符號(會被
rem 當成語法);兩種語言的鍵要一樣多。test_bat.py 守著。
call :ui_lang
if "%UI%"=="en" goto msg_en
set "M_NO_UV=[錯誤] 找不到 uv,請先執行「Install.bat」把環境裝好,再回來執行這個檔案。"
set "M_INSTALLING=正在把 Claude Code Skill 安裝到這台電腦..."
set "M_FAIL=[錯誤] Skill 安裝失敗。請參考上方訊息,然後重新執行這個檔案。"
set "M_DONE1=安裝完成。重新開啟 Claude Code 之後,直接跟它說要看哪份 PDF 或 Word,"
set "M_DONE2=它就會自己先轉成 Markdown 再讀,省下大量 token。"
goto msgs_set
:msg_en
set "M_NO_UV=[Error] uv not found. Run Install.bat first to set up the environment, then run this file again."
set "M_INSTALLING=Installing the Claude Code Skills on this computer..."
set "M_FAIL=[Error] Skill installation failed. See the messages above, then run this file again."
set "M_DONE1=Done. Restart Claude Code, then just tell it which PDF or Word file to read:"
set "M_DONE2=it converts the file to Markdown first, saving a lot of tokens."
:msgs_set

where uv >nul 2>nul
rem 區塊內的訊息不可出現半形 ")",會被 cmd 當成區塊結尾提前收掉
if errorlevel 1 (
    echo %M_NO_UV%
    pause
    exit /b 1
)

echo %M_INSTALLING%
uv run python scripts/install_skill.py
if errorlevel 1 (
    echo %M_FAIL%
    pause
    exit /b 1
)

echo.
echo %M_DONE1%
echo %M_DONE2%
pause
exit /b 0

rem ---- 介面語言(Install.bat 與 Install-Skill.bat 各一份,test_bat.py 釘著一字不差)----
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
