@echo off
setlocal
chcp 950 >nul
rem chcp 必須擺在任何中文(含本註解)之前:本檔存成 cp950(Big5),主控台代碼頁
rem 不是 950 時 cmd 會用錯的編碼解析整個檔案、把文字當指令跑(實測 cp437 起跑即壞)。
rem 為什麼不存 UTF-8 配 chcp 65001:cmd 在 65001 下 echo 中文到主控台時會算錯
rem 批次檔的讀取位置,跳到句子中間執行(實測 'I' is not recognized as ...)。
cd /d "%~dp0"

set "PATH=%USERPROFILE%\.local\bin;%PATH%"
set "GRADIO_ANALYTICS_ENABLED=False"
set "HF_HUB_DISABLE_TELEMETRY=1"

echo 正在啟動 AI 文件.MD 轉換器,瀏覽器會自動開啟。
echo 使用工具期間請保持這個視窗開著,關掉工具就停了。
echo 注意:第一次轉檔會先下載 AI 模型(約 2-3 GB)——
echo 網頁進度會停在「轉錄與講者分析」看似卡住,
echo 這個視窗會顯示下載進度,請耐心等候。
uv run meeting-scribe
if errorlevel 1 goto failed
exit /b 0

rem 失敗分流用 goto 不用巢狀 if:區塊內再開區塊時,訊息裡的標點更容易
rem 提前收掉區塊(見「區塊內訊息不可出現半形右括號」那條),而這幾行
rem 正是使用者唯一看得到的線索,不能被吃掉。
rem 沿革:2026-08-13 有人回報 uv trampoline failed to spawn Python child
rem process / entity not found(=啟動器找得到自己,但它要呼叫的 Python
rem 不在了)。當時只印一句「請先執行安裝.bat」——那句對「環境根本沒建」
rem 是對的,對「建好之後 Python 被搬走或被隔離」卻幫不上忙,而兩者在
rem 畫面上長得一模一樣。
:failed
echo.
if not exist ".venv\Scripts\python.exe" goto noenv
echo [錯誤] 啟動失敗:執行環境在,但 Python 起不來。
echo 最常見的三個原因:
echo   1. 安裝完之後搬過這個資料夾、或改過資料夾名字。重跑「安裝.bat」即可。
echo   2. 防毒或資安軟體把 Python 隔離了。請把這個工具資料夾與
echo      %%APPDATA%%\uv 加入白名單,再重跑「安裝.bat」。
echo   3. 安裝與啟動用了不同的 Windows 帳號。請用同一個帳號重跑「安裝.bat」。
pause
exit /b 1

:noenv
echo [錯誤] 啟動失敗:找不到執行環境。請先雙擊「安裝.bat」。
pause
exit /b 1
