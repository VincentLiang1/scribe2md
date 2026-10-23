#!/bin/zsh
# macOS 上把 Claude Code Skill 裝到使用者層級。雙擊即可。
#
# ⚠️ **這一支是交付物**(2026-10-02 起;「Launch.command」「Install.command」仍然不是,
# 那兩件事 `.app` 自己做了):Mac 版的 `.app` 首次啟動會把公開版整份搬到
# `~/Library/Application Support/meeting-scribe/app`,這支就在那個資料夾裡,
# 使用者照「❓ 使用說明」的「📂 工具資料夾在哪」開過去雙擊(`package.py` 的
# EXTRA_FILES 帶它)。⚠️ **所以訊息要同時對兩種擺法成立**:交付版裡 winkit 在
# 資料夾**裡面**、環境是 App 第一次啟動時建的;開發期 winkit 在**隔壁**、環境由
# 「Install.command」建。
#
# ⚠️ **真正的邏輯在 `scripts/install_skill.py`,而那一支本來就是跨平台的**
# (預設問的是 `~/.claude/skills`,Windows 與 macOS 都成立;設了
# `CLAUDE_CONFIG_DIR` 則以它為準——見那支的 `skills_root`)。
# 所以這支與 Windows 的「Install-Skill.bat」是**同一支腳本的兩個入口**,不是兩份
# 實作——⚠️ 要改行為請改那支 Python,不要在這裡另外長一份。

# ---- 介面語言(`make_mac_app.UI_LANG_ZSH`:三支 zsh 安裝腳本各貼一份,測試釘著一字不差)----
# 與 `i18n.match` 同一套判準:走完整份偏好清單、第一個認得的勝出,都不認得就英文;中文看
# 書寫系統(Hant 或 TW/HK/MO 是繁中,其他 zh 是簡中)。環境變數同 `i18n.ENV_OVERRIDE`。
ui_lang() {
    case "${MEETING_SCRIBE_UI_LANG:-}" in
        zh_TW|zh_CN|en) print -r -- "$MEETING_SCRIBE_UI_LANG"; return ;;
    esac
    local tag
    for tag in $(defaults read -g AppleLanguages 2>/dev/null | tr -d '(),"'); do
        case "${tag:l}" in
            zh|zh-*|zh_*)
                case "-${${tag:l}//_/-}-" in
                    *-hant-*|*-tw-*|*-hk-*|*-mo-*) print -r -- zh_TW ;;
                    *) print -r -- zh_CN ;;
                esac
                return ;;
            en|en-*|en_*) print -r -- en; return ;;
        esac
    done
    print -r -- en
}
UI_LANG=$(ui_lang)
# 每則訊息照「繁中 简中 English」的順序給三種寫法,只印介面語言那一句
say() {
    case "$UI_LANG" in
        zh_TW) echo "$1" ;;
        zh_CN) echo "$2" ;;
        *) echo "$3" ;;
    esac
}
# ---- 介面語言到此 ----

cd "$(dirname "$0")" || { say "找不到工具資料夾,請確認這個檔沒有被單獨搬走。" "找不到工具文件夹,请确认这个文件没有被单独移走。" "Tool folder not found. Make sure this file was not moved out of it."; exit 1; }

# ⚠️ **上面那道 `cd` 擋不到「這個檔被單獨複製走」**:`dirname "$0"` 一定是個存在
# 的目錄,cd 永遠成功,於是真正的症狀是 python 噴一行英文的 `can't open file`,
# 而底下那句「請參考上方訊息」指的就是那一行英文(spec §8:第三方的 cryptic
# 英文必須包裝)。⚠️ 這一關只驗檔案在不在,不重做安裝邏輯。
[ -f scripts/install_skill.py ] || {
    say "這個資料夾裡沒有工具的程式檔(scripts/install_skill.py)。請確認這個檔沒有被單獨複製到別處,它必須留在工具資料夾裡。" \
        "这个文件夹里没有工具的程序文件(scripts/install_skill.py)。请确认这个文件没有被单独复制到别处,它必须留在工具文件夹里。" \
        "The tool's program file (scripts/install_skill.py) is not in this folder. This file must stay in the tool folder."
    echo
    say "(按 Enter 關閉這個視窗)" "(按 Enter 关闭这个窗口)" "(Press Enter to close this window)"
    read -r _
    exit 1
}

# Finder 起的終端機讀得到登入 shell 的 PATH,但 uv 裝在 ~/.local/bin,
# 那條不一定在預設 PATH 裡(同「Launch.command」)
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
    say "[錯誤] 找不到 uv(Python 環境管理工具)。請先雙擊一次「AI 文件.MD 轉換器」App(第一次開啟時會自動安裝執行環境),再回來執行這個檔案。" \
        "[错误] 找不到 uv(Python 环境管理工具)。请先双击一次「AI 文件.MD 轉換器」App(第一次打开时会自动安装运行环境),再回来运行这个文件。" \
        "[Error] uv (the Python environment manager) not found. Open the App once first (the first launch sets up the environment), then run this file again."
    echo
    say "(按 Enter 關閉這個視窗)" "(按 Enter 关闭这个窗口)" "(Press Enter to close this window)"
    read -r _
    exit 1
fi

say "正在把 Claude Code Skill 安裝到這台電腦..." "正在把 Claude Code Skill 安装到这台电脑..." \
    "Installing the Claude Code Skills on this computer..."
if ! uv run python scripts/install_skill.py; then
    echo
    say "[錯誤] Skill 安裝失敗。最常見的原因是執行環境還沒建好:先雙擊一次「AI 文件.MD 轉換器」App,等它開起來之後再執行這個檔案。其他情況請參考上方訊息。" \
        "[错误] Skill 安装失败。最常见的原因是运行环境还没建好:先双击一次「AI 文件.MD 轉換器」App,等它打开之后再运行这个文件。其他情况请参考上方信息。" \
        "[Error] Skill installation failed. Usually the environment is not ready yet: open the App once, wait for its window, then run this file again. Otherwise see the messages above."
    echo
    say "(按 Enter 關閉這個視窗)" "(按 Enter 关闭这个窗口)" "(Press Enter to close this window)"
    read -r _
    exit 1
fi

echo
# ⚠️ **不要在這裡列 Skill 名字**:`install_skill.py` 會把 `skills/` 底下**每一個**
# 目錄都裝掉(現在是 doc2md 與 project-guardrails),而它自己已經逐個印出來了;
# 寫死「文件轉 Markdown」就是 Windows 那支的老問題(加一個 Skill 就過時一次)
say "安裝完成(上面列出的每一個 Skill 都裝好了)。重新開啟 Claude Code 之後,直接跟它說要看哪份 PDF 或 Word,它就會自己先轉成 Markdown 再讀,省下大量 token。" \
    "安装完成(上面列出的每一个 Skill 都装好了)。重新打开 Claude Code 之后,直接跟它说要看哪份 PDF 或 Word,它就会自己先转成 Markdown 再读,省下大量 token。" \
    "Done (every Skill listed above is installed). Restart Claude Code, then just tell it which PDF or Word file to read, and it converts the file to Markdown first, saving a lot of tokens."
echo
say "(按 Enter 關閉這個視窗)" "(按 Enter 关闭这个窗口)" "(Press Enter to close this window)"
read -r _
# ⚠️ **這一行不可省**:`read` 碰到 EOF 回 1,而它是最後一道指令——不明寫 exit 0
# 的話,裝成功卻回報離開碼 1(`Install.command` 也有同樣的問題)
exit 0
