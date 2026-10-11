#!/bin/zsh
# macOS 上把 Claude Code Skill 裝到使用者層級。雙擊即可。
#
# ⚠️ **這一支是交付物**(2026-10-02 起;「啟動.command」「安裝.command」仍然不是,
# 那兩件事 `.app` 自己做了):Mac 版的 `.app` 首次啟動會把公開版整份搬到
# `~/Library/Application Support/meeting-scribe/app`,這支就在那個資料夾裡,
# 使用者照「❓ 使用說明」的「📂 工具資料夾在哪」開過去雙擊(`package.py` 的
# EXTRA_FILES 帶它)。⚠️ **所以訊息要同時對兩種擺法成立**:交付版裡 winkit 在
# 資料夾**裡面**、環境是 App 第一次啟動時建的;開發期 winkit 在**隔壁**、環境由
# 「安裝.command」建。
#
# ⚠️ **真正的邏輯在 `scripts/install_skill.py`,而那一支本來就是跨平台的**
# (預設問的是 `~/.claude/skills`,Windows 與 macOS 都成立;設了
# `CLAUDE_CONFIG_DIR` 則以它為準——見那支的 `skills_root`)。
# 所以這支與 Windows 的「安裝Skill.bat」是**同一支腳本的兩個入口**,不是兩份
# 實作——⚠️ 要改行為請改那支 Python,不要在這裡另外長一份。

cd "$(dirname "$0")" || { echo "找不到工具資料夾,請確認這個檔沒有被單獨搬走。"; exit 1; }

# ⚠️ **上面那道 `cd` 擋不到「這個檔被單獨複製走」**:`dirname "$0"` 一定是個存在
# 的目錄,cd 永遠成功,於是真正的症狀是 python 噴一行英文的 `can't open file`,
# 而底下那句「請參考上方訊息」指的就是那一行英文(spec §8:第三方的 cryptic
# 英文必須包裝)。⚠️ 這一關只驗檔案在不在,不重做安裝邏輯。
[ -f scripts/install_skill.py ] || {
    echo "這個資料夾裡沒有工具的程式檔(scripts/install_skill.py)。"
    echo "請確認這個檔沒有被單獨複製到別處,它必須留在工具資料夾裡。"
    echo
    echo "(按 Enter 關閉這個視窗)"
    read -r _
    exit 1
}

# Finder 起的終端機讀得到登入 shell 的 PATH,但 uv 裝在 ~/.local/bin,
# 那條不一定在預設 PATH 裡(同「啟動.command」)
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
    echo "[錯誤] 找不到 uv(Python 環境管理工具)。"
    echo "請先雙擊一次「AI 文件.MD 轉換器」App(第一次開會自動安裝執行環境),"
    echo "再回來執行這個檔案。"
    echo
    echo "(按 Enter 關閉這個視窗)"
    read -r _
    exit 1
fi

echo "正在把 Claude Code Skill 安裝到這台電腦..."
if ! uv run python scripts/install_skill.py; then
    echo
    echo "[錯誤] Skill 安裝失敗。"
    echo "最常見的原因是執行環境還沒建好:先雙擊一次「AI 文件.MD 轉換器」App,"
    echo "等它開起來之後再執行這個檔案。其他情況請參考上方訊息。"
    echo
    echo "(按 Enter 關閉這個視窗)"
    read -r _
    exit 1
fi

echo
# ⚠️ **不要在這裡列 Skill 名字**:`install_skill.py` 會把 `skills/` 底下**每一個**
# 目錄都裝掉(現在是 doc2md 與 project-guardrails),而它自己已經逐個印出來了;
# 寫死「文件轉 Markdown」就是 Windows 那支的老問題(加一個 Skill 就過時一次)
echo "安裝完成(上面列出的每一個 Skill 都裝好了)。重新開啟 Claude Code 之後,"
echo "直接跟它說要看哪份 PDF 或 Word,它就會自己先轉成 Markdown 再讀,省下大量 token。"
echo
echo "(按 Enter 關閉這個視窗)"
read -r _
# ⚠️ **這一行不可省**:`read` 碰到 EOF 回 1,而它是最後一道指令——不明寫 exit 0
# 的話,裝成功卻回報離開碼 1(`安裝.command` 也有同樣的問題)
exit 0
