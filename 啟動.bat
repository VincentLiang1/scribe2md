@echo off
setlocal
chcp 950 >nul
rem chcp 必須擺在任何中文(含本註解)之前:本檔存成 cp950(Big5),主控台代碼頁
rem 不是 950 時 cmd 會用錯的編碼解析整個檔案、把文字當指令跑(實測 cp437 起跑即壞)。
rem 為什麼不存 UTF-8 配 chcp 65001:cmd 在 65001 下 echo 中文到主控台時會算錯
rem 批次檔的讀取位置,跳到句子中間執行(實測 'I' is not recognized as ...)。
rem 注意:本檔是 cp950,寫不出 emoji,所以註解裡的警告一律用【注意】。
cd /d "%~dp0"

set "PATH=%USERPROFILE%\.local\bin;%PATH%"
set "GRADIO_ANALYTICS_ENABLED=False"
set "HF_HUB_DISABLE_TELEMETRY=1"
rem 這個變數是「這個行程是啟動器丟出來的,請自己脫離主控台」的唯一憑據:
rem 開發時直接跑 uv run 或 python -m 不會設它,黑視窗照樣留著給人看。
set "MEETING_SCRIBE_DETACH_CONSOLE=1"

rem 沒有執行環境就當場說清楚。【注意】這一段是唯一還靠黑視窗講話的地方
rem ——紀錄檔要程式起來才接手,而程式正是起不來的那一個。
if not exist ".venv\Scripts\pythonw.exe" goto noenv

rem 這一支丟出來的是「門面」:它持有這個黑視窗,再生一個沒有視窗的本體去
rem 跑整個工具,並把本體說的話逐行轉印到這裡。等網頁真的開起來,本體會送一個
rem 收工信號,門面才退場、視窗跟著關上(機制與兩條死路見 console.py 檔頭)。
rem 【注意】丟出去就走,不等它:cmd 留著只是多一層,視窗是門面在持有的。
rem 【注意】本體先死掉的話,門面會把原因印出來並等按鍵,視窗不會關。
rem 【注意】/b 不可省:少了它,start 會另外開一個新的主控台,使用者看到的就是
rem 「開一個視窗、又跳出第二個、第一個才關掉」(使用者 2026-08-31 回報)。
rem 加了 /b 就沿用這個 cmd 的主控台,從頭到尾只有一個視窗;cmd 隨即退出,
rem 視窗改由門面持有,等網頁開好它才收工。
rem 【注意】這裡不要 echo:開場白由門面自己印(console._greet),它才知道
rem 什麼時候該說什麼——而且它印完之後緊接著就是本體的訊息。
start "" /b ".venv\Scripts\pythonw.exe" -m meeting_scribe
exit /b 0

rem 沿革:2026-08-13 有人回報 uv trampoline failed to spawn Python child
rem process / entity not found(=啟動器找得到自己,但它要呼叫的 Python
rem 不在了)。當時只印一句「請先執行安裝.bat」——那句對「環境根本沒建」
rem 是對的,對「建好之後 Python 被搬走或被隔離」卻幫不上忙,而兩者在
rem 畫面上長得一模一樣,所以三個原因一起列出來。
:noenv
echo.
echo [錯誤] 啟動失敗:找不到執行環境。請先雙擊「安裝.bat」。
echo 如果已經安裝過了,最常見的原因有三個:
echo   1. 安裝完之後搬過這個資料夾、或改過資料夾名字。重跑「安裝.bat」即可。
echo   2. 防毒或資安軟體把 Python 隔離了。請把這個工具資料夾與
echo      %%APPDATA%%\uv 加入白名單,再重跑「安裝.bat」。
echo   3. 安裝與啟動用了不同的 Windows 帳號。請用同一個帳號重跑「安裝.bat」。
pause
exit /b 1
