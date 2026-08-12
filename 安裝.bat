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

echo.
echo 環境建置完成。請雙擊「啟動.bat」開始使用工具。
pause
