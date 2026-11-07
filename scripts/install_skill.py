r"""把 `skills/` 底下的 Claude Code Skill 安裝到使用者層級。

**兩個雙擊入口共用這一支**:Windows 的 `Install-Skill.bat` 與 macOS 的 `Install-Skill.command`
(2026-09-26 加,`docs/spec/mac/06` §6.2)——它們不是兩份實作,行為要改就改這裡。
也可以直接 `uv run python scripts/install_skill.py`。

**存在的理由是那個寫死的路徑**:Skill 要叫用 `uv run --directory <這個 repo>
doc2md`,而 repo 在每台機器上的位置都不一樣(README 教使用者解壓到「桌面或
C:\ 底下」,本來就不會一致,而且**路徑可能含中文與空格**——填佔位符時不能
假設它是純 ASCII)。手動複製 SKILL.md 的話,得記得改
裡面**三處**路徑,漏一處就是「Skill 有裝、但叫用失敗」——而那種壞法沒有
任何提示。這支腳本從自己的位置推出 repo 根目錄,把佔位符填掉再寫出去。

**使用者層級不是專案層級**:預設裝到 `~/.claude/skills`
(Windows 的 `%USERPROFILE%\.claude\skills\`、macOS 的 `~/.claude/skills/`——問的是
家目錄,所以這一支不必為平台分岔;⚠️ **但 `CLAUDE_CONFIG_DIR` 設了就以它為準**,
見 `skills_root`),任何專案
裡的 Claude Code 都吃得到;放進某個專案的 `.claude/skills/` 就只有在那個
目錄下才會觸發,而「處理散落各處的 PDF/Word」本來就不屬於任何一個專案。
"""
import os
import shutil
import sys
from pathlib import Path

# 範本裡的佔位符 → 固定入口 `meeting-scribe/bin/doc2md` 的絕對路徑(2026-11-07 起;之前是
# `uv run --directory "<工具資料夾>"`)。只裝核心或裝了會議助手電腦版都是同一個入口(見 `entry`),
# 所以技能不必知道裝在哪
PLACEHOLDER = "{{DOC2MD}}"
ROOT = Path(__file__).resolve().parents[1]

# ⚠️ **標記檔的用途是「這個資料夾是本工具建的,清得掉」**(2026-09-26 加):
# 判準借自 `docmd.AssetsDir`,但**只是寬限一輪、不是那條「沒有標記就絕不刪」**
# (差別寫在 `install_one`)。名字刻意跟
# 它同一個家族,而且**不可以是 `.md`**:`install_one` 會把每個 `.md` 當範本
# 跑一次 `render`,標記檔被當範本處理只是白費工,還讓它看起來像 Skill 內容。
MARKER = ".meeting-scribe-skill"
MARKER_TEXT = (
    "本資料夾由「AI 文件.MD 轉換器」的 Skill 安裝程式(Install-Skill.bat /\n"
    "Install-Skill.command)建立,重新安裝時會把範本已經沒有的檔一併清掉。\n"
    "請不要把自己的檔案放在這裡。\n"
)


def skills_root() -> Path:
    r"""使用者層級的 skills 目錄。

    ⚠️ **`CLAUDE_CONFIG_DIR` 設了就一切以它為準**(2026-09-26 補):官方文件
    寫著「If you set `CLAUDE_CONFIG_DIR`, every `~/.claude` path on this page
    lives under that directory instead.」,而 `skills/` 正是那一頁列的路徑之一
    (code.claude.com/docs/en/claude-directory)。
    ⚠️ **漏看這個變數的壞法是最糟的那一種**:裝到 `~/.claude/skills`、印「已安裝
    Skill:…」、離開碼 0、畫面寫「安裝完成」,而 Claude Code 讀的是另一個目錄
    ——**安靜地沒觸發,不是報錯**(`docs/dev/doc2md.md` 點名的正是這種壞法:
    使用者貼了路徑,Skill 一聲不響地不動作,而沒有任何跡象可查)。

    ⚠️ **空字串要當成沒設**:`CLAUDE_CONFIG_DIR=` 之下 `Path("")` 是**當前工作
    目錄**,那會把 Skill 裝到使用者剛好站在的地方去(而雙擊時那裡是家目錄,
    看起來還「差不多對」——最難查的那種)。
    """
    configured = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if configured:
        return Path(configured).expanduser() / "skills"
    return Path.home() / ".claude" / "skills"


def default_entry() -> Path:
    """固定入口(`entry.bin_dir()` 底下的 doc2md)。"""
    sys.path.insert(0, str(ROOT / "src"))
    from meeting_scribe import entry

    return entry.bin_dir() / entry.entry_name()


def render(template: str, install_dir: Path) -> str:
    r"""把範本裡的佔位符換成這台機器上的實際路徑。

    ⚠️ **一律填正斜線**(`as_posix()`),而範本裡的佔位符**一律包在雙引號
    裡**。填好的指令是給 Claude Code 照抄去跑的,而它的 Bash 工具是 POSIX
    shell:`C:\SOURCE5\Python\meeting-scribe` 裸寫時 `\S`、`\P`、`\m` 會被當
    成跳脫吃掉、整條路徑塌成 `C:SOURCE5Pythonmeeting-scribe`,uv 只回一句
    「error: 系統找不到指定的檔案。 (os error 2)」、離開碼 2。⚠️ **那句話
    看起來像轉檔失敗,其實指令根本沒跑到**——2026-09-18 在 FWIKI 的批次攝入
    連踩四次(同一天四次呼叫全部先炸一次、改寫成正斜線才過),其中一次還是
    背景執行,只在 `.output` 裡留下孤零零一行。引號另外擋掉路徑含空格的情況
    (README 教使用者解壓到「桌面或 C:\ 底下」,而桌面路徑本來就可能有空格)。
    """
    return template.replace(PLACEHOLDER, install_dir.as_posix())


def _sha(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _recorded(dest: Path) -> dict[str, str]:
    """標記檔裡記著的「我們寫進去的那份」雜湊(`sha256 <相對路徑> <雜湊>`;舊版標記沒有這幾行)。"""
    try:
        lines = (dest / MARKER).read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    return {parts[1]: parts[2] for parts in (ln.split(" ", 2) for ln in lines)
            if len(parts) == 3 and parts[0] == "sha256"}


def _edited(dest: Path, marked: bool) -> list[str]:
    r"""使用者改過(或不是我們裝)的 `.md`:有標記而雜湊對不上,或根本沒有標記卻已經有檔。
    舊版標記(沒記雜湊)當成我們裝的、沒人改過——那時候的技能指令已經不能用,蓋掉才對。"""
    if not dest.is_dir():
        return []
    recorded = _recorded(dest)
    out = []
    for path in sorted(dest.rglob("*.md")):
        rel = path.relative_to(dest).as_posix()
        if not marked or (rel in recorded and recorded[rel] != _sha(path)):
            out.append(rel)
    return out


def backup_dir() -> Path:
    """改過的技能備份到哪(⚠️ **在 `.claude` 外面**:放在 skills 底下會被 Claude Code 當成第二個技能載入)。"""
    sys.path.insert(0, str(ROOT / "src"))
    from meeting_scribe import paths

    return paths.appdata_root() / "skill-backup"


def _prune(dest: Path, wanted: set[Path]) -> None:
    r"""刪掉 `dest` 裡不在 `wanted` 的東西(`wanted` 是相對 `dest` 的路徑)。

    ⚠️ **逐檔刪,不准 `rmtree`**:全專案只有 `docmd.AssetsDir` 一個地方能對使用者
    資料夾下 `rmtree`(CLAUDE.md 明文),而這裡動的是 `~/.claude/skills/<名字>`
    ——同樣是使用者的家目錄。

    ⚠️ **深的先刪**(`len(parts)` 反向排序):目錄要等裡面清空才 `rmdir` 得掉。
    ⚠️ **只 `rmdir` 空目錄**,失敗就跳過——裡面還有東西表示那不是我們該刪的。
    ⚠️ **符號連結一律當檔案 `unlink`**:`is_dir()` 會跟著連結走,對一條指到別處的
    連結下 `rmdir` 等於試著刪那一頭的目錄。

    ⚠️ **`scripts/make_public.py` 的 `mirror()` 有一份同形狀的刪除半邊**(同樣的
    `wanted` 差集、同樣的深度反向排序、同樣的保護名單)。刻意沒有合併:那支不
    出貨而這支出貨(`package.SCRIPT_FILES`),方向只能是它 import 這裡,而共用
    一支要多帶 `keep` 與「`__pycache__` 不複製也不刪」兩個旗標——比兩份各自
    十行更難讀。⚠️ **但坑是共用的**:改這裡的排序或保護名單時去看那一邊一眼
    (它目前還會跟著符號連結走,這裡不會)。"""
    for path in sorted(dest.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.relative_to(dest) in wanted:
            continue
        if path.is_symlink() or not path.is_dir():
            path.unlink()
        else:
            try:
                path.rmdir()
            except OSError:
                pass


def install_one(src_dir: Path, dest_root: Path, install_dir: Path) -> Path:
    """安裝單一 Skill,回傳落地的目錄。

    同名檔案直接覆蓋:Skill 是產生出來的東西、不是使用者的資料,而「更新
    了 repo 卻還在跑舊 Skill」是這裡最可能出的錯。

    ⚠️ **光覆蓋是不夠的,範本沒有的檔要刪掉**(2026-09-26 補,原本這裡寫
    「同名目錄直接覆蓋」是錯的):`mkdir(exist_ok=True)` ＋ 逐檔複製**不會刪掉
    來源已經沒有的檔**——範本把 `references/old.md` 改名成 `new.md`,使用者機器
    上兩份都在,而 `project-guardrails` 會把那份舊的繼續灑進每個新專案。
    ⚠️ `test_reinstall_overwrites_old_copy` 蓋不到這一條:它只重寫**同一個檔名**。

    ⚠️ **而清理是「寬限一輪」不是 `docmd.AssetsDir` 那種絕對保護**——這一點要照
    實講,別把它讀成「沒有標記就絕不刪」:沒有標記的目錄**這一趟只覆蓋、不清理**,
    但標記會留下去,所以**下一趟就開始清了**。`AssetsDir` 在沒有標記時是拋
    `UserFacingError` 拒絕動作,這裡沒有,因為 2026-09-26 之前裝過的每一台都沒有
    標記(含同仁的),擋下來等於要他們先去改名一個**工具自己建的**資料夾。
    ⚠️ 代價不對稱這件事仍然成立、只是延後一輪:多留一份舊範本頂多讓新專案帶到
    過時的內容,刪錯了那是使用者的檔案、而且沒有任何跡象。⚠️ 刻意不為這一輪印
    訊息:每一台既有的機器都會走到這條路,而那句話對同仁而言是一個他無從處置
    的內部細節。`MARKER_TEXT` 本身就寫著「請不要把自己的檔案放在這裡」。"""
    dest = dest_root / src_dir.name
    # ⚠️ 這個判斷必須在寫標記(下面那行 `write_text`)**之前**取,否則永遠是真。
    # ⚠️ **不必另外記「目錄是不是新建的」**:全新安裝時複製完的 `dest` 內容恰好
    # 就是 `wanted`,`_prune` 一個檔都刪不到(實測回傳空清單),所以 `if marked:`
    # 與 `if 新建 or marked:` 逐位元組等價
    marked = (dest / MARKER).exists()
    # 使用者改過的先備份再蓋(2026-11-07:技能指令換成固定入口,不蓋就壞;備份讓改動不會消失)
    edited = _edited(dest, marked)
    if edited:
        import datetime

        saved = backup_dir() / f"{src_dir.name}-{datetime.datetime.now():%Y%m%d-%H%M%S}"
        shutil.copytree(dest, saved, ignore=shutil.ignore_patterns(MARKER))
        print(_say(f"你改過的技能已備份到:{saved}", f"你改过的技能已备份到:{saved}",
                   f"Your edited Skill was backed up to: {saved}"))
    dest.mkdir(parents=True, exist_ok=True)
    # ⚠️ 標記檔本來就是「該留下的檔」之一,放進 `wanted` 比在 `_prune` 裡開一個
    # 特例乾淨(而且那個特例其實是空轉的:下面無條件重寫標記,刪了也馬上回來)
    wanted = {Path(MARKER)}
    for path in sorted(src_dir.rglob("*")):
        rel = path.relative_to(src_dir)
        wanted.add(rel)
        target = dest / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.suffix.lower() == ".md":
            target.write_text(
                render(path.read_text(encoding="utf-8"), install_dir),
                encoding="utf-8",
            )
        else:
            shutil.copy2(path, target)
    if marked:
        _prune(dest, wanted)
    hashes = "".join(f"sha256 {p.relative_to(dest).as_posix()} {_sha(p)}\n"
                     for p in sorted(dest.rglob("*.md")))
    (dest / MARKER).write_text(MARKER_TEXT + hashes, encoding="utf-8")
    return dest


def _say(zh_tw: str, zh_cn: str, en: str) -> str:
    """挑介面語言那一句(與工具介面同一套:🌐 選過的優先,否則跟系統)。

    ⚠️ **不包 `t()`**:譯文抽取只掃 `src/`,`scripts/` 裡包了 `t()` 的字串查不到譯文,
    會無聲退回繁中。"""
    from meeting_scribe import i18n

    return {i18n.ZH_TW: zh_tw, i18n.ZH_CN: zh_cn}.get(i18n.current(), en)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    # `--entry <路徑>`:核心的安裝程式明給固定入口;沒給就照這台的固定位置算(`default_entry`)。
    # ⚠️ 舊的位置參數(工具資料夾)不再用:技能叫的是固定入口,不是某一份安裝
    install_dir = Path(args[args.index("--entry") + 1]) if "--entry" in args else default_entry()
    if "--entry" not in args:
        # 從電腦版的 Install-Skill 來的:先把這一份記下來、指好固定入口——使用者可能還沒開過 App,
        # 那時 `bin` 還不存在,技能一叫就找不到(開發用的資料夾 `register_desktop` 自己會略過)
        from meeting_scribe import entry

        entry.register_desktop(ROOT)
    sources = sorted(p for p in (ROOT / "skills").iterdir() if p.is_dir())
    if not sources:
        print(_say("找不到任何要安裝的 Skill(skills/ 是空的)。",
                   "找不到任何要安装的 Skill(skills/ 是空的)。",
                   "No Skills to install (skills/ is empty)."), file=sys.stderr)
        return 1
    dest_root = skills_root()
    for src in sources:
        dest = install_one(src, dest_root, install_dir)
        print(_say(f"已安裝 Skill:{dest}", f"已安装 Skill:{dest}", f"Installed Skill: {dest}"))
    print(_say(f"技能叫用的入口:{install_dir}", f"技能调用的入口:{install_dir}",
               f"The Skill runs: {install_dir}"))
    # 不在這裡講「重開 Claude Code」:兩個入口(`Install-Skill.bat`/`.command`)收尾那句已經講了,
    # 這裡再講一次就是同一件事連說兩遍(2026-10-23 使用者)
    return 0


if __name__ == "__main__":  # pragma: no cover - bootstrap
    sys.exit(main())
