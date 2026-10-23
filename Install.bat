@echo off
setlocal
chcp 950 >nul
rem 本檔存成 cp950(Big5),chcp 須在任何中文之前——原因見「啟動.bat」開頭註解。
cd /d "%~dp0"

rem 訊息語言:中文系統印繁中、其他一律英文(判準見檔尾的 :ui_lang)。注意:簡中系統也印
rem 繁中:這個檔是 cp950,寫不出簡體字(使用者 2026-10-23 同意)。注意:訊息放在 M_ 變數裡,
rem 而變數在區塊解析「之前」就展開,所以值裡不可以有半形右括號與 cmd 的四個特殊符號(會被
rem 當成語法);兩種語言的鍵要一樣多。test_bat.py 守著。
call :ui_lang
if "%UI%"=="en" goto msg_en
set "M_UV=正在安裝 uv 套件管理器..."
set "M_UV_FAIL=[錯誤] uv 安裝失敗。請檢查網路或代理伺服器設定。"
set "M_SYNC=正在建置 Python 執行環境,第一次需要幾分鐘..."
set "M_SYNC_FAIL=[錯誤] 環境建置失敗。請參考上方訊息,然後重新執行這個檔案。"
set "M_DONE=環境建置完成。"
set "M_WT1=這裡是開發用的 worktree,略過桌面捷徑。"
set "M_WT2=這條線請雙擊這個資料夾裡的「Launch.vbs」啟動。"
set "M_NOLNK=不影響使用,雙擊這個資料夾裡的「Launch.vbs」一樣能啟動工具。"
set "M_BROKEN1=[錯誤] 環境建好了,但實際執行時失敗。最常見的原因是防毒或資安軟體"
set "M_BROKEN2=把 Python 隔離了。請把這個工具資料夾與 %%APPDATA%%\uv 加入白名單,"
set "M_BROKEN3=再執行一次這個檔案;若是公司電腦,請把這兩行轉給 IT。"
goto msgs_set
:msg_en
set "M_UV=Installing the uv package manager..."
set "M_UV_FAIL=[Error] Failed to install uv. Check the network or proxy settings."
set "M_SYNC=Building the Python environment, the first time takes a few minutes..."
set "M_SYNC_FAIL=[Error] Setup failed. See the messages above, then run this file again."
set "M_DONE=Setup complete."
set "M_WT1=This is a development worktree, skipping the desktop shortcut."
set "M_WT2=Start this line with Launch.vbs in this folder."
set "M_NOLNK=The tool still works: double-click Launch.vbs in this folder to start it."
set "M_BROKEN1=[Error] Setup finished but the tool fails to run. Most likely antivirus or security"
set "M_BROKEN2=software quarantined Python. Add this tool folder and %%APPDATA%%\uv to its allow list"
set "M_BROKEN3=and run this file again. On a company computer, forward these lines to IT."
:msgs_set

where uv >nul 2>nul
rem 區塊內的訊息不可出現半形 ")",會被 cmd 當成區塊結尾提前收掉
if errorlevel 1 (
    echo %M_UV%
    powershell -ExecutionPolicy Bypass -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex"
    if errorlevel 1 (
        echo %M_UV_FAIL%
        pause
        exit /b 1
    )
    set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)

echo %M_SYNC%
uv sync
if errorlevel 1 (
    echo %M_SYNC_FAIL%
    pause
    exit /b 1
)

rem 建好之後**真的叫一次**:uv sync 成功不代表跑得起來——2026-08-13 有人
rem 回報安裝看似成功、啟動卻出現 uv trampoline failed to spawn Python child
rem process(Python 被搬走或被資安軟體隔離)。那種情況要在這裡就講,
rem 不要等使用者雙擊「啟動.bat」才發現,那時他已經不知道該回頭做什麼了。
uv run python -c "import meeting_scribe" >nul 2>&1
if errorlevel 1 goto broken

echo.
echo %M_DONE%

rem 捷徑要指的三段路徑只有這一刻算得出來:同仁把工具解壓到哪裡是他的自由,
rem 所以「Launch.vbs」、工作目錄與圖示都由腳本從自己的位置往上推,一段都不寫死。
rem 中文全留在那支 UTF-8 的 Python 裡,這行維持純 ASCII:字串過 cmd 這一層
rem 會被重新編碼。捷徑建不出來不算安裝失敗,只是換一句話收尾。
rem 捷徑的目標寫死是「Launch.vbs」(見 scripts/make_shortcut.py),所以在 git
rem worktree(.git 是檔案而不是資料夾)裡建下去,會把桌面與開始功能表那顆
rem 圖示無聲改指到這個開發資料夾、蓋掉原本那顆。環境仍然要建,只跳過捷徑。
if not exist ".git" goto lnk
if exist ".git\" goto lnk
echo %M_WT1%
echo %M_WT2%
pause
exit /b 0

:lnk
uv run python scripts/make_shortcut.py
if errorlevel 1 goto nolnk
pause
exit /b 0

:nolnk
echo %M_NOLNK%
pause
exit /b 0

:broken
echo.
echo %M_BROKEN1%
echo %M_BROKEN2%
echo %M_BROKEN3%
pause
exit /b 1

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
