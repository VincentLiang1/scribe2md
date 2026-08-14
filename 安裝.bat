@echo off
setlocal
chcp 950 >nul
rem 本檔存成 cp950(Big5),chcp 須在任何中文之前——原因見「啟動.bat」開頭註解。
cd /d "%~dp0"

where uv >nul 2>nul
rem 區塊內的訊息不可出現半形 ")",會被 cmd 當成區塊結尾提前收掉
if errorlevel 1 (
    echo 正在安裝 uv 套件管理器...
    powershell -ExecutionPolicy Bypass -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex"
    if errorlevel 1 (
        echo [錯誤] uv 安裝失敗。請檢查網路或代理伺服器設定。
        pause
        exit /b 1
    )
    set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)

echo 正在建置 Python 執行環境(第一次需要幾分鐘)...
uv sync
if errorlevel 1 (
    echo [錯誤] 環境建置失敗。請參考上方訊息,然後重新執行這個檔案。
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
echo 環境建置完成。請雙擊「啟動.bat」開始使用工具。
pause
exit /b 0

:broken
echo.
echo [錯誤] 環境建好了,但實際執行時失敗。最常見的原因是防毒或資安軟體
echo 把 Python 隔離了。請把這個工具資料夾與 %%APPDATA%%\uv 加入白名單,
echo 再執行一次這個檔案;若是公司電腦,請把這兩行轉給 IT。
pause
exit /b 1
