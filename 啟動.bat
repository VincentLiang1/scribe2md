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
rem 區塊內的訊息不可出現半形 ")",會被 cmd 當成區塊結尾提前收掉
if errorlevel 1 (
    echo [錯誤] 啟動失敗。請先執行「安裝.bat」,或參考上方訊息。
    pause
    exit /b 1
)
