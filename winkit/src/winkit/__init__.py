r"""winkit —— 兩支 Windows 桌面 AP 共用的那一層,以及它唯一的注入點 `bind()`。

**這個包裝的是「每個專案都不該長得不一樣」的東西**(B 類):路徑、色票、Windows
整合、皮膚、捷徑、紀錄檔。判準只有一句——**這個專案需要跟別的專案長得不一樣嗎?**
要 → 留在下游(A 類:身分值、版面、圖示畫什麼);不要 → 住這裡,唯一真值。

⚠️ **三個 repo 漂開的成因不是忘了同步,是兩類寫在同一個檔案裡**:`make_skin.py`
一支裡同時有「要產哪幾張皮」(A)與「超橢圓怎麼取樣」(B),所以只能整支複製、然後
各自演化——量到的差距是 763 行(`make_icon.py` 348 行、`make_shortcut.py` 291 行)。

下游(2026-08-28 現況):`C:\SOURCE5\Python\MP4-2-SRT` 與
`C:\SOURCE5\Python\NotebookLM_OCR`。⚠️ **`meeting-scribe` 不接**(使用者
2026-08-28 定:它正往 macOS/iOS 走,共用到 mac 太複雜)——所以這個包**整包就是
Windows 專用**,不必為跨平台留餘地。

怎麼接
------
下游各自用 `[tool.uv.sources]` 的相對路徑相依(`{ path = "../winkit",
editable = true }`),而不是 `git subtree`:使用者換電腦是複製整個 `C:\SOURCE5\`,
複製一次全部帶走、改一次兩邊拿到、debug 直接改隔壁資料夾、沒有 build step。

⚠️ **本包不准 import 任何下游**。反過來一旦成環,拆的就不只一行,而且**不是當場
壞掉——是搬家那天才發現搬不動**。下游要給的東西全部經過 `bind()` 這一個口。
"""
from pathlib import Path
from typing import NamedTuple


class Host(NamedTuple):
    r"""下游注入進來的一切:身分值、環境變數前綴、**以及它自己在磁碟上的位置**。

    ⚠️ 最後那三個(`package_dir` / `repo_root` / `skin_generator`)是搬家最大的坑。這些模組住在下游
    的時候,「我在哪」全是 `Path(__file__).resolve().parents[2]` 算出來的;搬進本
    包之後那條算式指到的是 **winkit 自己**,於是紀錄檔寫進 `winkit\logs`、版本號
    讀成 winkit 的 `.git`、皮膚資產在 winkit 底下找不到。⚠️ **三個症狀都沒有錯誤
    訊息**,而且看起來都像「東西不見了」。所以位置一律由呼叫端給,本包不准自己推。

    ⚠️ **`repo_root` 也不可以從 `package_dir` 往上推**:本專案是 src layout
    (`src/mp4_2_srt/` → 往上兩層),NotebookLM_OCR 是 flat layout(`pdf2ppt/` →
    往上一層)。推的那個版本會在其中一邊安靜地算錯。
    """

    app_id: str          # 工作列拿來認「這個視窗屬於哪支程式」的身分
    app_title: str       # 視窗標題列那一行
    app_desc: str        # 桌面圖示的提示文字(.lnk 的 description)
    app_dir_name: str    # %LOCALAPPDATA% 底下的資料夾名
    env_prefix: str      # 環境變數前綴,例如 MP4_2_SRT → MP4_2_SRT_LOG_DIR
    package_dir: Path    # 下游套件自己的目錄(資產跟著它走)
    repo_root: Path      # 下游專案根(logs、scripts、.git 掛在它底下)
    skin_generator: Path  # 下游那支 make_skin.py(見 `bind()` 的同名參數)


# 下游要在 `brand` 裡提供的五個值。⚠️ 用**名字**檢查而不是型別:下游傳進來的通常
# 就是它的 `brand` 模組本身(那支刻意零 import,誰都載得起),不是某個類別的實例。
_REQUIRED = ("APP_ID", "APP_TITLE", "APP_DESC", "APP_DIR_NAME", "ENV_PREFIX")

_host: Host | None = None


def bind(brand, *, package_dir, repo_root, skin_generator=None) -> None:
    r"""下游啟動時呼叫一次。`brand` 是 duck-typed——直接把它的 `brand` 模組傳進來。

    放在下游套件的 `__init__.py` 裡:**任何子模組被 import 都會先經過那裡**,所以
    「忘了 bind」不會發生在正常的執行路徑上。⚠️ 連那支跑在安裝當下的
    `make_shortcut.py` 也一樣——它走的是 `from <pkg>.brand import ...`,而 Python
    匯入子模組必先匯入父套件。

    `skin_generator` = 下游那支 `make_skin.py` 的完整路徑,不給就是
    `repo_root/scripts/make_skin.py`。⚠️ **它是參數而不是寫死的慣例**,因為兩個下游
    真的不一樣:MP4-2-SRT 放在 `scripts/`,NotebookLM_OCR 放在它自己的 `tools/`
    (那個 repo 的開發腳本全在那一層,搬過來只為了配合這裡不划算)。⚠️ 找錯的症狀是
    **安靜降級**:當場畫那條路走不通(`_drawn()` import 不到),於是顯示縮放對不上
    出貨資產的機器就整個掉皮膚,而畫面看起來只是「這台的長相跟別台不一樣」。
    ⚠️ 順帶一提它**也是快取指紋的一部分**(`skin._pixel_sources`),所以指錯還會讓
    指紋算不出來——那一條被接住當成「沒有快取」,同樣不作聲。

    兩種失敗都當場丟例外,**不准回預設值**:靜默用預設的下場是「模型還在磁碟上、
    程式卻說沒有」那一種——兩個位置都存在,連「檔案不見了」都不會發生。
    """
    global _host

    missing = [f for f in _REQUIRED if not getattr(brand, f, None)]
    if missing:
        raise AttributeError(
            f"brand 少了 {'、'.join(missing)}:winkit 要靠這幾個值才知道自己在替誰做事")

    fresh = Host(brand.APP_ID, brand.APP_TITLE, brand.APP_DESC, brand.APP_DIR_NAME,
                 brand.ENV_PREFIX, Path(package_dir), Path(repo_root),
                 Path(skin_generator) if skin_generator
                 else Path(repo_root) / "scripts" / "make_skin.py")

    # ⚠️ **同值冪等、異值當場炸**。一個行程裡只會有一支 app,綁第二個身分一定是
    # 出事了(測試互相污染最常見),而那種錯的症狀會出現在**別的測試**上。
    if _host is not None and _host != fresh:
        raise RuntimeError(
            f"winkit 已經綁給 {_host.app_id} 了,不可以再綁給 {fresh.app_id}:"
            "一個行程只服務一支 app")
    _host = fresh


def host() -> Host:
    """拿注入進來的那份。⚠️ **沒 bind 就用到是當場 `RuntimeError`**,不是預設值。"""
    if _host is None:
        raise RuntimeError(
            "winkit 還沒有被 bind:下游要在套件的 __init__.py 裡呼叫一次 "
            "winkit.bind(brand, package_dir=..., repo_root=...)")
    return _host


def env_var(suffix: str) -> str:
    """組出這支 app 的環境變數名(`MP4_2_SRT` + `LOG_DIR` → `MP4_2_SRT_LOG_DIR`)。

    ⚠️ 代價已知:名字變成組出來的字串,`grep MP4_2_SRT_LOG_DIR` 找不到。**常數名
    仍然是唯一入口**(`filelog.LOG_DIR_ENV` 那種),要查是誰在用就 grep 常數。
    """
    return f"{host().env_prefix}_{suffix}"


def _unbind() -> None:
    """只給測試用:模組級全域忘了復位的話,失敗會跨檔案、依順序才重現。"""
    global _host
    _host = None
