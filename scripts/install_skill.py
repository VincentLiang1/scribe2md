r"""把 `skills/` 底下的 Claude Code Skill 安裝到使用者層級。

由 `安裝Skill.bat` 呼叫(也可以直接 `uv run python scripts/install_skill.py`)。

**存在的理由是那個寫死的路徑**:Skill 要叫用 `uv run --directory <這個 repo>
doc2md`,而 repo 在每台機器上的位置都不一樣(README 教使用者解壓到「桌面或
C:\ 底下的英文資料夾」,本來就不會一致)。手動複製 SKILL.md 的話,得記得改
裡面**兩處**路徑,漏一處就是「Skill 有裝、但叫用失敗」——而那種壞法沒有
任何提示。這支腳本從自己的位置推出 repo 根目錄,把佔位符填掉再寫出去。

**使用者層級不是專案層級**:裝到 `%USERPROFILE%\.claude\skills\`,任何專案
裡的 Claude Code 都吃得到;放進某個專案的 `.claude/skills/` 就只有在那個
目錄下才會觸發,而「處理散落各處的 PDF/Word」本來就不屬於任何一個專案。
"""
import shutil
import sys
from pathlib import Path

PLACEHOLDER = "{{MEETING_SCRIBE_DIR}}"
ROOT = Path(__file__).resolve().parents[1]


def skills_root() -> Path:
    """使用者層級的 skills 目錄。"""
    return Path.home() / ".claude" / "skills"


def render(template: str, install_dir: Path) -> str:
    """把範本裡的佔位符換成這台機器上的實際路徑。"""
    return template.replace(PLACEHOLDER, str(install_dir))


def install_one(src_dir: Path, dest_root: Path, install_dir: Path) -> Path:
    """安裝單一 Skill,回傳落地的目錄。

    同名目錄直接覆蓋:Skill 是產生出來的東西、不是使用者的資料,而「更新
    了 repo 卻還在跑舊 Skill」是這裡最可能出的錯。"""
    dest = dest_root / src_dir.name
    dest.mkdir(parents=True, exist_ok=True)
    for path in sorted(src_dir.rglob("*")):
        target = dest / path.relative_to(src_dir)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.suffix.lower() == ".md":
            target.write_text(
                render(path.read_text(encoding="utf-8"), install_dir),
                encoding="utf-8",
            )
        else:
            shutil.copy2(path, target)
    return dest


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    install_dir = Path(args[0]).resolve() if args else ROOT
    sources = sorted(p for p in (ROOT / "skills").iterdir() if p.is_dir())
    if not sources:
        print("找不到任何要安裝的 Skill(skills/ 是空的)。", file=sys.stderr)
        return 1
    dest_root = skills_root()
    for src in sources:
        dest = install_one(src, dest_root, install_dir)
        print(f"已安裝 Skill:{dest}")
    print(f"工具位置已填入:{install_dir}")
    print("重開 Claude Code 就會生效。")
    return 0


if __name__ == "__main__":  # pragma: no cover - bootstrap
    sys.exit(main())
