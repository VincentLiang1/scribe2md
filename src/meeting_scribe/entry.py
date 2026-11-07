r"""固定入口 `doc2md` 與版本資訊(2026-11-07 拆包:核心 Scribe2MD 可以單獨安裝)。

**跟 wiki-agent(wiki-feeder)的契約**,改之前先跟那邊對齊(`docs/dev/publish.md` §4.7 的「核心包」那段):

- **固定入口**:`<使用者資料夾>/meeting-scribe/bin/doc2md`(Windows 是 `bin\doc2md.exe`)。`bin` 本身是**連結**
  (Mac 是 symlink、Windows 是目錄 junction),指向勝出那一份安裝的 venv 執行檔資料夾——所以入口就是 uv
  產生的那顆真的執行檔,不經過 cmd 或 shell(收件匣進來的檔名不完全可信,經過 cmd 就有注入面),也
  沒有多一層轉接行程。
- **兩份都裝時挑版本較新的**(使用者 2026-11-07 定):核心(`meeting-scribe/core/`)與完整 App(會議助手
  電腦版;Mac 在 `meeting-scribe/app/`、Windows 在使用者自選的工具資料夾,啟動時把自己記進 `installs.json`)。
  重指的時機是三個:核心安裝/更新完(安裝程式叫 `relink`)、完整 App 每次啟動(`register_desktop`)、
  wiki-feeder 看到 bin 懸空時(`install.command --relink` / `install.ps1 -Relink`,也是叫 `relink`)。
- `current.json`(在 `meeting-scribe/` 那一層,⚠️ **不在 `bin/` 裡**:`bin` 是連結,寫進去等於寫進那份
  venv)記著勝出的是哪一份,junction 出狀況時讓 wiki-feeder 直接叫那顆執行檔。
- `doc2md --version --json`:一行 JSON(`version_info`)。⚠️ **不載入任何模型、不連網、不建記錄檔**
  ——wiki-feeder 每一輪(60 秒)都會叫一次。

⚠️ **開發用的資料夾不登記**(`update.is_dev_tree`):開發機跑一次視窗就把使用者的固定入口指向原始碼
資料夾,是最難查的那種壞法。
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from meeting_scribe import paths, plat

logger = logging.getLogger(__name__)

#: 這份契約的版號(格式改了就加一,wiki-feeder 照它判斷認不認得)
ENTRY_CONTRACT = 1
NAME = "Scribe2MD"


def appdata() -> Path:
    return paths.appdata_root()


def core_root() -> Path:
    """單獨安裝的核心在哪(安裝程式固定裝在這裡;⚠️ venv 寫死絕對路徑,搬了就要重裝)。"""
    return appdata() / "core"


def bin_dir() -> Path:
    return appdata() / "bin"


def current_file() -> Path:
    return appdata() / "current.json"


def installs_file() -> Path:
    return appdata() / "installs.json"


def venv_bin(root: Path) -> Path:
    return root / ".venv" / plat.venv_bin_name()


def entry_name() -> str:
    return plat.executable_name("doc2md")


def project_version(root: Path) -> str | None:
    import tomllib

    try:
        return tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    except Exception:  # noqa: BLE001 - 讀不到就是不算數的那一份
        return None


def stamp(root: Path) -> str | None:
    """`VERSION` 第一行(`<commit> <日期>`)→ `20261107-7567739`(wiki-feeder 用 commit 比對 manifest)。"""
    try:
        commit, date = (root / "VERSION").read_text(encoding="utf-8").split("\n", 1)[0].split()[:2]
    except (OSError, ValueError):
        return None
    return f"{date.replace('-', '')}-{commit}"


def _vtuple(v: str | None) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in (v or "").split("."))
    except ValueError:
        return ()


def build_of(root: Path) -> str:
    """`core`(單獨安裝的核心)或 `full`(完整 App 內含的那一份)。"""
    try:
        return "core" if root.resolve() == core_root().resolve() else "full"
    except OSError:
        return "full"


def ffmpeg_path() -> str | None:
    r"""已經下載好的 ffmpeg(static-ffmpeg 第一次轉錄音時才下載);**還沒下載就回 None,這裡不下載**
    (`--version --json` 不連網)。"""
    import importlib.util

    spec = importlib.util.find_spec("static_ffmpeg")
    if spec is None or not spec.origin:
        return None
    name = plat.executable_name("ffmpeg")
    found = sorted(p for p in (Path(spec.origin).parent / "bin").rglob(name) if p.is_file())
    return str(found[0]) if found else None


def libreoffice_path() -> str | None:
    """這台找得到的 LibreOffice(不下載、不試跑)。"""
    try:
        from meeting_scribe import soffice

        found = soffice.find_existing()
        return str(found) if found else None
    except Exception:  # noqa: BLE001
        return None


def version_info(root: Path | None = None) -> dict:
    """`doc2md --version --json` 那一行。⚠️ 不載入模型、不連網(見模組 docstring)。"""
    import importlib.util

    from meeting_scribe import vocab

    root = root or paths.repo_root()
    return {
        "name": NAME,
        "version": project_version(root),
        "stamp": stamp(root),
        "build": build_of(root),
        "audio": importlib.util.find_spec("faster_whisper") is not None,
        "format_version": vocab.FORMAT_VERSION,
        "ffmpeg": ffmpeg_path(),
        "libreoffice": libreoffice_path(),
        "root": str(root),
        "entry_contract": ENTRY_CONTRACT,
    }


def candidates() -> list[Path]:
    """裝好、而且入口真的在的那幾份(核心、記下來的完整 App)。"""
    out = []
    roots = [core_root()]
    try:
        roots.append(Path(json.loads(installs_file().read_text(encoding="utf-8"))["desktop"]))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    for root in roots:
        if (venv_bin(root) / entry_name()).is_file() and project_version(root) and root not in out:
            out.append(root)
    return out


def winner() -> Path | None:
    """版本較新的那一份;同版時完整 App 優先(它多了介面才用得到的東西,核心能做的它都能做)。"""
    roots = candidates()
    if not roots:
        return None
    return max(roots, key=lambda r: (_vtuple(project_version(r)), build_of(r) == "full"))


def relink() -> int:
    """重比版本、把 `bin` 指向勝出的那一份、更新 `current.json`。回 0 成功、1 沒有可指的安裝。"""
    root = winner()
    if root is None:
        logger.warning("沒有可以當固定入口的安裝(核心與完整 App 都沒裝好)")
        return 1
    target = venv_bin(root)
    plat.point_dir_link(bin_dir(), target)
    info = {"doc2md": str(target / entry_name()), "root": str(root), "build": build_of(root),
            "version": project_version(root)}
    tmp = current_file().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, current_file())
    logger.info("固定入口指向 %s(%s %s)", root, info["build"], info["version"])
    return 0


def register_desktop(root: Path | None = None) -> None:
    r"""完整 App 啟動時:記下自己在哪、重指固定入口。⚠️ 開發用的資料夾不登記(見模組 docstring);
    ⚠️ **絕不拋例外**——開窗路徑上的順手工作。"""
    from meeting_scribe import update

    root = root or paths.repo_root()
    try:
        if update.is_dev_tree(root):
            return
        installs_file().parent.mkdir(parents=True, exist_ok=True)
        installs_file().write_text(json.dumps({"desktop": str(root)}, ensure_ascii=False), encoding="utf-8")
        relink()
    except Exception as e:  # noqa: BLE001
        logger.warning("固定入口沒能更新:%s", e)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["relink"]:
        logging.basicConfig(level=logging.INFO, stream=sys.stderr)
        return relink()
    if argv == ["fetch-ffmpeg"]:
        # 安裝時先把 ffmpeg 抓好(那時本來就預期要連網),第一次轉錄音才不會卡在下載(2026-11-07 Windows 實測)
        logging.basicConfig(level=logging.INFO, stream=sys.stderr)
        from meeting_scribe import audio

        print(f"ffmpeg: {audio.ffmpeg_path()}", file=sys.stderr)
        return 0
    print("用法:python -m meeting_scribe.entry relink", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
