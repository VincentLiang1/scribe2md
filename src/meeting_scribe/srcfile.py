r"""來源檔案的取得與把關(貼路徑 / 原生選檔;**不上傳**)。

使用者指定 2026-07-26(參考 MP4-2-SRT):gradio 的上傳元件會把整份檔案
複製進暫存快取——2GB 錄影慢又佔碟,而本工具只在本機操作,拿「路徑」
就夠。未來若要做成 Server 版才需要真正的上傳。

**成立前提**:App 只綁 127.0.0.1(spec §7),瀏覽器與伺服器必在同一台
機器——使用者看到的檔案總管路徑,伺服器端直接開得起來,原生選檔對話框
也才有意義。這個前提一旦改變(真的做成 Server),本模組整個不成立。

分工:`clean_path`/`validate` 是純把關(不依賴 gradio,錯誤走
`UserFacingError` 交由 app 層原樣顯示,spec §8);`pick_file` 開 Windows
原生對話框,只取路徑、檔案不複製也不進 gradio 快取。
"""
import threading
from pathlib import Path

from meeting_scribe.errors import UserFacingError

# 支援的來源格式(單一出處):副檔名白名單、選檔對話框的篩選器、
# 錯誤訊息裡給使用者看的清單都由此推導——手寫第二份必定漏改
SUPPORTED_TYPES = [".m4a", ".mp3", ".wav", ".mp4", ".mov", ".avi"]


def format_hint(types: list[str]) -> str:
    """副檔名清單 → 給使用者看的字串(「m4a / mp3 / wav …」)。

    docsrc 的文件格式清單也用這一份:兩張白名單各自獨立,但排版只該
    有一種樣子。"""
    return " / ".join(ext.lstrip(".") for ext in types)


def supported_hint() -> str:
    """給使用者看的格式清單(「m4a / mp3 / wav …」)。"""
    return format_hint(SUPPORTED_TYPES)


def clean_path(text) -> str:
    """剝空白與引號:Explorer「複製檔案地址/複製路徑」貼上自帶引號。"""
    return (text or "").strip().strip('"').strip()


def clean_paths(text) -> list[str]:
    """多行路徑文字 → 路徑清單(逐行剝空白與引號、去掉空行)。

    住在這裡而不是 docsrc,是因為「一行一個路徑」現在是兩個分頁共用的
    輸入形式,而 srcfile 是下層(docsrc import 它,反過來會循環)。"""
    return [
        cleaned for line in (text or "").splitlines()
        if (cleaned := clean_path(line))
    ]


def looks_like_batch(text) -> bool:
    """這串輸入要走批次(多檔/資料夾)還是單檔模式?

    **判準看「輸入的形狀」,不看展開後有幾個檔**(使用者 2026-08-06 拍板):
    給了一個以上的路徑、或給了資料夾 → 批次;剛好一個檔案路徑 → 單檔。
    若改看展開結果,同一個資料夾今天裡面一個檔就跑講者命名、明天多放
    一個檔就不跑,使用者完全無從預期。

    找不到的單一路徑回 False(= 單檔),好讓 srcfile.validate 去給那句
    更具體的「找不到檔案,請確認路徑是否正確」;批次那條只會說
    「找不到這個檔案或資料夾」。"""
    paths = clean_paths(text)
    if len(paths) != 1:
        return len(paths) > 1
    try:
        return Path(paths[0]).is_dir()
    except OSError:  # pragma: no cover — 路徑長度/權限之類的極端情況
        return False


def validate(text) -> Path:
    """路徑正規化+檢查,回傳可直接使用的 Path。

    上傳元件時代由 gradio 的 `file_types` 把關;改貼路徑後「檔案存在」與
    「格式支援」都得自己驗,且訊息要講清楚是哪裡不對(spec §8 繁中)。
    錯誤一律 UserFacingError——本模組不綁 gradio,呼叫端(app)負責顯示。
    """
    cleaned = clean_path(text)
    if not cleaned:
        raise UserFacingError("請先按「選擇檔案…」挑選檔案,或貼上檔案路徑")
    p = Path(cleaned)
    if p.is_dir():
        raise UserFacingError(f"這是資料夾,請選擇單一檔案:{cleaned}")
    if not p.is_file():
        raise UserFacingError(f"找不到檔案,請確認路徑是否正確:{cleaned}")
    if p.suffix.lower() not in SUPPORTED_TYPES:
        raise UserFacingError(
            f"不支援的檔案格式「{p.suffix or '(無副檔名)'}」,"
            f"支援:{supported_hint()}"
        )
    return p


# 原生對話框一次只開一個:對話框開著時再按鈕直接忽略(同 MP4-2-SRT)。
# 鎖是模組層級單例,「聲音→MD」與「文字、圖像→MD」兩個分頁共用同一把
# ——同時開兩個 tkinter 對話框會互搶焦點,使用者只看得到後開的那個
_dialog_lock = threading.Lock()


def native_dialog(opener):
    """開 Windows 原生選擇對話框,回傳選取結果(取消/重複開啟回 None)。

    公開給 docsrc(文件轉檔分頁的多選檔/選資料夾)共用:withdraw+topmost、
    防重複開、Tk 物件用完即 destroy 這幾件事只該有一份實作。

    tkinter 在函式內才 import:GUI 工具箱不進啟動路徑(同 sherpa/whisper
    的惰性載入原則)。root 視窗藏起來、對話框置頂——否則會被瀏覽器視窗
    蓋住,使用者看起來像「按了沒反應」。Tk 物件有執行緒親和性,用完即
    destroy,不跨事件快取。"""
    if not _dialog_lock.acquire(blocking=False):
        return None
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)  # 浮到瀏覽器視窗前面
        try:
            return opener(filedialog, root) or None
        finally:
            root.destroy()
    finally:
        _dialog_lock.release()


def append_paths(current, added: str) -> str:
    """把新選的路徑**接在現有內容後面**,重複的不再加一次。

    取代式的選檔讓使用者「永遠只能處理一批」(2026-08-01 回報:選了第二個
    資料夾就把第一個蓋掉)。要重新開始有「清空」鈕,累加才是選檔按鈕該做
    的事。去重是為了路徑欄好讀——validate_batch 本來就會去重,但欄位裡
    出現兩行一樣的路徑會讓人以為自己選錯了。

    住在 srcfile(下層)是因為兩個分頁的選檔鈕現在都是這個行為。"""
    lines = clean_paths(current)
    seen = {line.lower() for line in lines}
    for line in clean_paths(added):
        if line.lower() not in seen:
            seen.add(line.lower())
            lines.append(line)
    return "\n".join(lines)


def pick_files() -> str:
    """「選擇檔案…」:**多選**(2026-08-06 起收多檔;選一個仍走單檔模式,
    見 looks_like_batch)。回傳一行一個路徑;**取消回空字串**——保不保留
    現值是呼叫端的決策,不該埋在對話框函式裡。"""
    picked = native_dialog(lambda fd, root: fd.askopenfilenames(
        parent=root, title="選擇會議錄音或錄影檔(可多選)",
        filetypes=[
            ("會議錄音/錄影", " ".join("*" + ext for ext in SUPPORTED_TYPES)),
            ("所有檔案", "*.*"),
        ],
    ))
    if not picked:
        return ""
    # tkinter 回傳 C:/x/y 正斜線,轉回與 Explorer 一致的反斜線樣式
    return "\n".join(str(Path(p)) for p in picked)


def pick_folder() -> str:
    """「選擇資料夾…」:整個資料夾的錄音一次轉完(= 批次模式)。
    取消回空字串(同 pick_files)。"""
    picked = native_dialog(lambda fd, root: fd.askdirectory(
        parent=root, title="選擇要轉逐字稿的資料夾",
    ))
    return str(Path(picked)) if picked else ""
