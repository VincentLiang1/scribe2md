@echo off
chcp 950 >nul
rem 這支住在「疑難排解」子資料夾裡,那兩點是把工作目錄帶回專案根,與「啟動.vbs」
rem 的 sh.CurrentDirectory = here 對齊。
rem 【這個檔不是產生出來的】「啟動.vbs」由 scripts/make_launcher.py 產生,這一支是
rem 手寫的:它是 .bat、規矩完全不同(cp950、CRLF、chcp 950 在第一個中文之前),而且
rem 它存在的理由正是「那一支連訊息框都跳不出來的時候」——兩支同源反而少了一條退路。
cd /d %~dp0..
echo 啟動中,轉換器的視窗隨即開啟。
echo.
echo 【這一支是除錯用的】平常請用上一層資料夾的「啟動.vbs」,或桌面與「開始」
echo 功能表那顆圖示。只有在那條路連視窗都開不起來、或它跳出的訊息框看不出原因
echo 時,才改用這一支:錯誤訊息會完整留在這個黑框裡,訊息框只顯示得下最後幾
echo 百個字。
echo.
echo 本視窗請不要關閉,關掉它會一併結束程式。平常這裡不會有任何訊息,那是正常的。
echo.
echo 【第一次轉檔會先下載 AI 模型】
echo   語音辨識與講者分離的模型合計約 2 到 3GB,只下載一次。
echo   下載期間畫面上的訊息會停在「首次使用:下載模型…」看似卡住,請耐心等候。
echo.
uv run python -m meeting_scribe.desktop
rem 【一定要停,不可以只在 errorlevel 1 才停】這一支存在的理由就是「把訊息完整
rem 留在這個框裡」,而 Python 正常結束時離開碼是 0——只在非零時停的話,收尾那
rem 幾行字會隨視窗一起消失。使用者回報的正是「有字但是很快就一起關閉了」。
echo.
echo 【程式已結束】上面的訊息看完再關閉這個視窗。
pause
