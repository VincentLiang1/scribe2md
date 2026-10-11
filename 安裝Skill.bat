@echo off
setlocal
chcp 950 >nul
rem 本檔存成 cp950(Big5),chcp 必須在任何中文之前——理由見「安裝.bat」開頭。
cd /d "%~dp0"

where uv >nul 2>nul
rem 區塊內的訊息不可出現半形 ")",會被 cmd 當成區塊結尾提前收掉
if errorlevel 1 (
    echo [錯誤] 找不到 uv,請先執行「安裝.bat」把環境裝好,再回來執行這個檔案。
    pause
    exit /b 1
)

echo 正在把「文件轉 Markdown」的 Claude Code Skill 安裝到這台電腦...
uv run python scripts/install_skill.py
if errorlevel 1 (
    echo [錯誤] Skill 安裝失敗。請參考上方訊息,然後重新執行這個檔案。
    pause
    exit /b 1
)

echo.
echo 安裝完成。重新開啟 Claude Code 之後,直接跟它說要看哪份 PDF 或 Word,
echo 它就會自己先轉成 Markdown 再讀,省下大量 token。
pause
