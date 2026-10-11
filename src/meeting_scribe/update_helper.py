r"""換版小程式:等本程式關掉 → 備份 → 覆蓋 → `uv sync` → 重新開啟;任何一步失敗就還原。

    <基底 pythonw> -I update_helper.py --staged DIR --target DIR --pid N --version X

由 `plat_win.apply_update` 在本程式關閉前叫起來,**之後本程式就結束了**——所以這支
從頭到尾只有自己:不准 import `meeting_scribe` 或 `winkit`(它們正是要被換掉的檔案),
**只准用標準函式庫**(`tests/test_update.py` 以 AST 守著)。

⚠️ **跑它的是 uv 管的基底直譯器,不是 `.venv` 裡那支**:`uv sync` 可能換掉 `.venv` 的
啟動器,拿它跑自己等於鋸自己坐的樹枝。它也**先被複製到工作目錄**才執行,不從要被
覆蓋的那棵樹裡跑。

⚠️ **失敗的方向一律是「回到舊版還能用」**:備份每一個要被覆蓋的檔,任何一步失敗
(檔案被鎖、磁碟滿、`uv sync` 失敗)就把備份放回去、刪掉新增的檔,再把舊版開起來。
換到一半的混合版本是最糟的結果——它看起來裝好了,壞在沒人猜得到的地方。

⚠️ **不刪新版已經移除的檔**(`docs/spec/12` §12.3 的已知限制,與手動解壓覆蓋一樣):
留下來的模組沒有人 import。

⚠️ **沒有 `print`**:用 pythonw 跑、沒有主控台。進度是一扇只有一行字的小視窗,
錯誤是 `MessageBoxW`,經過記在 `<工具資料夾>\logs\update_*.log`。
"""
from __future__ import annotations

import argparse
import ctypes
import datetime as _dt
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

#: ⚠️ 與 `update.NEVER_TOUCH` 同一份(這支 import 不到那邊,測試釘著兩份一致)
NEVER_TOUCH = ("data", ".venv", "logs", "output", ".git")
TITLE = "AI 文件.MD 轉換器"
CREATE_NO_WINDOW = 0x08000000
#: 本程式關掉要多久:收尾(保存錄音、停子行程)正常是一兩秒,給寬一點
WAIT_EXIT_SEC = 120
#: `uv sync` 換了相依時要下載,慢網路上幾分鐘是正常的
SYNC_TIMEOUT_SEC = 20 * 60
#: 防毒掃描剛寫好的檔時會短暫鎖住它:重試幾次再算失敗
COPY_RETRIES = 5

_log_file = None


def log(msg: str) -> None:
    line = f"{_dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n"
    if _log_file is not None:
        try:
            _log_file.write(line)
            _log_file.flush()
        except OSError:
            pass


def message(text: str, error: bool = True) -> None:
    """繁中訊息框(不用 Tk 的 messagebox:它的按鈕字會是英文,見 `desktop._ask_close`)。"""
    try:
        flags = 0x10 if error else 0x40          # MB_ICONERROR / MB_ICONINFORMATION
        # MB_SETFOREGROUND | MB_TOPMOST:本程式已經關了,沒有父視窗可以掛
        ctypes.windll.user32.MessageBoxW(None, text, TITLE, flags | 0x10000 | 0x40000)
    except Exception:  # noqa: BLE001
        log(f"訊息框開不起來:{text}")


def wait_for_exit(pid: int, timeout: float = WAIT_EXIT_SEC) -> bool:
    """等本程式那個行程結束。等不到回 False(那就不能動它還開著的檔)。"""
    if pid <= 0:
        return True
    try:
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x00100000, False, pid)   # SYNCHRONIZE
        if not handle:
            return True                                    # 已經不在了
        try:
            return k32.WaitForSingleObject(handle, int(timeout * 1000)) == 0
        finally:
            k32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - 非 Windows(測試)
        return True


def files_to_copy(staged: Path) -> list[Path]:
    """新版裡要覆蓋過去的檔(相對路徑)。⚠️ `NEVER_TOUCH` 底下的一個都不帶。"""
    out = []
    for p in sorted(staged.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(staged)
        if rel.parts[0] in NEVER_TOUCH:
            log(f"略過不可覆蓋的位置:{rel}")
            continue
        out.append(rel)
    return out


def _copy_retry(src: Path, dst: Path) -> None:
    for attempt in range(COPY_RETRIES):
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            return
        except PermissionError:
            if attempt == COPY_RETRIES - 1:
                raise
            time.sleep(1)


class Swap:
    """覆蓋與還原。`done` 記著每一個已經動過的檔,還原就照著它倒回去。"""

    def __init__(self, staged: Path, target: Path, backup: Path):
        self.staged, self.target, self.backup = staged, target, backup
        self.done: list[tuple[Path, bool]] = []   # (相對路徑, 原本就存在)

    def apply(self) -> None:
        for rel in files_to_copy(self.staged):
            dst = self.target / rel
            existed = dst.exists()
            if existed:
                _copy_retry(dst, self.backup / rel)
            # ⚠️ 先記再寫:寫到一半失敗的那一個也要被還原
            self.done.append((rel, existed))
            _copy_retry(self.staged / rel, dst)

    def rollback(self) -> list[Path]:
        """倒回去;回傳還原不了的檔(空 = 完全回到舊版)。"""
        stuck = []
        for rel, existed in reversed(self.done):
            dst = self.target / rel
            try:
                if existed:
                    _copy_retry(self.backup / rel, dst)
                else:
                    dst.unlink(missing_ok=True)
            except OSError as e:
                log(f"還原失敗:{rel}({e})")
                stuck.append(rel)
        return stuck


def find_uv() -> str | None:
    """`安裝.bat` 裝 uv 的地方:PATH 上,或 `%USERPROFILE%\\.local\\bin`。"""
    exe = shutil.which("uv")
    if exe:
        return exe
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    for cand in (home / ".local" / "bin" / "uv.exe", home / ".cargo" / "bin" / "uv.exe"):
        if cand.exists():
            return str(cand)
    return None


def uv_sync(target: Path) -> bool:
    uv = find_uv()
    if uv is None:
        log("找不到 uv")
        return False
    kw = {"creationflags": CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    for attempt in range(2):
        try:
            r = subprocess.run([uv, "sync"], cwd=target, capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=SYNC_TIMEOUT_SEC, **kw)
        except (OSError, subprocess.TimeoutExpired) as e:
            log(f"uv sync 執行失敗:{e}")
            return False
        log(f"uv sync 結束碼 {r.returncode}\n{r.stdout}\n{r.stderr}")
        if r.returncode == 0:
            return True
        # 子行程(講者分析、轉錄)收到 EOF 才走,偶爾比主行程晚一兩秒放開 .venv 的檔
        if attempt == 0:
            time.sleep(5)
    return False


def relaunch(target: Path) -> None:
    r"""重新開啟。⚠️ **用 `wscript.exe` 帶啟動器當參數**,不靠 `.vbs` 的關聯(同
    `make_shortcut.py`:不少公司把 `.vbs` 關聯到記事本)。"""
    vbs = target / "啟動.vbs"
    wscript = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "wscript.exe"
    try:
        if wscript.exists():
            subprocess.Popen([str(wscript), str(vbs)], cwd=target, close_fds=True)
        else:
            os.startfile(vbs)  # type: ignore[attr-defined]
        log("已重新開啟")
    except Exception as e:  # noqa: BLE001
        log(f"重新開啟失敗:{e}")
        message(f"更新完成,但沒能自動重新開啟。請自己開啟「{TITLE}」。", error=False)


MANUAL = ("請到 GitHub 下載最新版,解壓縮後覆蓋到工具資料夾,再雙擊「安裝.bat」:\n"
          "https://github.com/VincentLiang1/scribe2md/releases/latest")


def run(staged: Path, target: Path, pid: int, version: str) -> tuple[int, str, bool]:
    r"""整趟。回傳 `(離開碼, 要給使用者看的話, 要不要重新開啟)`。

    離開碼:0 = 換好了;1 = 沒換(已還原成舊版);2 = 還原不完全。
    ⚠️ **訊息與重新開啟交給呼叫端在主執行緒做**:這支跑在工作執行緒上,而那時進度
    視窗還蓋在最上層——在這裡跳訊息框會被它擋住。"""
    log(f"開始更新到 v{version}:{staged} → {target}")
    if not wait_for_exit(pid):
        log("本程式沒有結束,放棄更新")
        return 1, ("更新沒有進行:程式沒有關掉。請關掉它之後,再從「❓ 使用說明」"
                   "最底下按一次「檢查更新」。"), False
    backup = staged.parent / "backup"
    shutil.rmtree(backup, ignore_errors=True)
    swap = Swap(staged, target, backup)
    synced = False                           # 環境有沒有被動過(還原時才需要再 sync 一次)
    try:
        swap.apply()
        log(f"覆蓋了 {len(swap.done)} 個檔")
        synced = True
        ok = uv_sync(target)
        reason = "" if ok else "執行環境更新失敗(多半是網路或防毒軟體)"
    except Exception as e:  # noqa: BLE001
        ok, reason = False, f"換檔失敗({e})"
        log(reason)
    if ok:
        shutil.rmtree(staged, ignore_errors=True)
        shutil.rmtree(backup, ignore_errors=True)
        log("更新完成")
        return 0, "", True
    stuck = swap.rollback()
    if not stuck:
        if synced:
            uv_sync(target)                  # 環境也回到舊版那一份(失敗也只能這樣)
        log("已還原成舊版")
        return 1, (f"更新到 v{version} 沒有成功:{reason}。\n\n已經還原成原本的版本,"
                   "可以照常使用。詳情在工具資料夾的 logs 裡(update_ 開頭的那份)。"), True
    log(f"還原不完全:{stuck}")
    return 2, (f"更新到 v{version} 沒有成功,而且有 {len(stuck)} 個檔沒能還原。\n\n"
               + MANUAL), False


def _progress_window(version: str):
    """只有一行字的小視窗。⚠️ 純 tkinter:winkit 不能 import(它正在被換掉)。"""
    try:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:  # noqa: BLE001
            pass
        import tkinter as tk
        from tkinter import ttk

        root = tk.Tk()
        root.title(TITLE)
        root.resizable(False, False)
        root.attributes("-topmost", True)
        root.protocol("WM_DELETE_WINDOW", lambda: None)   # 換到一半不准關
        frame = ttk.Frame(root, padding=24)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"正在換上 v{version},完成後會自動重新開啟…").pack(anchor="w")
        bar = ttk.Progressbar(frame, mode="indeterminate", length=360)
        bar.pack(fill="x", pady=(12, 0))
        bar.start(15)
        root.update_idletasks()
        x = (root.winfo_screenwidth() - root.winfo_width()) // 2
        y = (root.winfo_screenheight() - root.winfo_height()) // 3
        root.geometry(f"+{x}+{y}")
        return root
    except Exception:  # noqa: BLE001
        log("進度視窗開不起來(不影響更新)")
        return None


def main(argv: list[str] | None = None) -> int:
    global _log_file
    ap = argparse.ArgumentParser()
    ap.add_argument("--staged", required=True)
    ap.add_argument("--target", required=True)
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--version", required=True)
    args = ap.parse_args(argv)
    staged, target = Path(args.staged), Path(args.target)
    try:
        (target / "logs").mkdir(exist_ok=True)
        _log_file = open(target / "logs" / f"update_{_dt.datetime.now():%Y-%m-%d_%H%M%S}.log",
                         "a", encoding="utf-8")
    except OSError:
        _log_file = None

    root = _progress_window(args.version)
    result: dict = {"out": (1, "更新時發生未預期的錯誤。\n\n" + MANUAL, False)}

    def work() -> None:
        try:
            result["out"] = run(staged, target, args.pid, args.version)
        except Exception as e:  # noqa: BLE001
            log(f"未預期的錯誤:{e!r}")

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    if root is not None:
        # ⚠️ 工作執行緒不碰 Tk:由主執行緒輪詢它結束了沒
        def poll() -> None:
            if worker.is_alive():
                root.after(200, poll)
            else:
                root.destroy()
        root.after(200, poll)
        root.mainloop()
    worker.join()
    code, text, again = result["out"]
    if text:
        message(text, error=code != 0)
    if again:
        relaunch(target)
    if _log_file is not None:
        _log_file.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
