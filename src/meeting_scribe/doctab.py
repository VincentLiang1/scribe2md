r"""「文字、圖像→MD」分頁的事件處理(app.build_ui 只負責接線)。

分工同 `data_tabs.py`:**本模組不建任何 UI 元件**;需要狀態機(鎖介面、
互斥旗標、進度)的部分留在 app.py。與 data_tabs 的差別是這裡連 gradio
都不 import——按鈕亮暗改由 app 決定之後,本模組只剩純函式,錯誤一律
`UserFacingError`(同 docsrc 的作風)。

把關錯誤在這裡一律**回傳說明文字**而不是拋例外:選檔階段還沒開始做事,
用彈窗打斷太重。真正開始轉檔之後的錯誤才走 gr.Error,那是 app._doc_convert
的事。
"""
import logging
import os
import sys
from pathlib import Path

from meeting_scribe import docsrc, srcfile
from meeting_scribe.errors import UserFacingError

logger = logging.getLogger(__name__)

# 略過清單最多列幾筆:選了整個磁碟機時可能有上百個略過項,全列會把摘要
# 撐成一面牆。完整清單在轉檔後的批次報告裡
_MAX_SKIPPED_SHOWN = 8
# 一次最多開幾個檔案總管視窗
_MAX_OPEN_DIRS = 5


def preview_summary(text, recursive: bool = True) -> str:
    """路徑文字 → 顯示用摘要(選了幾個檔、會略過哪些)。

    **「開始轉檔」不依這個結果亮暗**(使用者 2026-08-01 指定):按鈕一律
    可按,按下去才把關。理由是**貼上路徑時前端不一定會觸發 input 事件**
    ——按鈕沒亮會讓人以為工具壞了,而「按了才知道錯在哪」對使用者反而
    直觀(錯誤訊息會講清楚是空的、找不到、還是格式不支援)。
    這裡只做即時回饋,不是把關;真正的把關在 app._doc_convert 那一步。
    """
    if not str(text or "").strip():
        return ""
    try:
        files, skipped = docsrc.validate_batch(text, recursive)
    except UserFacingError as e:
        return str(e)
    parts = [docsrc.summarize(files, skipped)]
    # 錄音錄影與文件差了好幾個量級(一份 PDF 幾秒鐘、一場兩小時的會議要跑
    # 一小時上下),而這個分頁的「包含子資料夾」預設是**開**的——把整棵
    # 專案樹指過來的人多半是為了裡面的文件,不該毫無預告被拖進幾小時的
    # 轉錄。摘要裡本來就數得出 mp4 幾個,但那行數字不會讓人意識到代價
    audio = sum(1 for f in files if f.suffix.lower() in docsrc.AUDIO_TYPES)
    if audio:
        parts.append(
            f"⚠️ 其中 {audio} 個是錄音/影片,要轉成逐字稿——"
            "每個檔可能要數十分鐘到數小時(視長度與本機有無顯示晶片),"
            "比文件慢很多。"
        )
    shown = docsrc.skipped_lines(skipped)[:_MAX_SKIPPED_SHOWN]
    if shown:
        parts.append("\n".join(shown))
        if len(skipped) > _MAX_SKIPPED_SHOWN:
            parts.append(f"(另有 {len(skipped) - _MAX_SKIPPED_SHOWN} 個項目未列出)")
    return "\n\n".join(p for p in parts if p)


def _append_paths(current, added: str) -> str:
    """把新選的路徑接在現有內容後面(實作在 srcfile,兩個分頁共用同一份)。"""
    return srcfile.append_paths(current, added)


def pick_files(current, recursive: bool = True):
    """「選擇檔案…」(多選,**累加**)→(路徑欄, 摘要)。"""
    merged = _append_paths(current, docsrc.pick_files())
    return merged, preview_summary(merged, recursive)


def pick_folder(current, recursive: bool = True):
    """「選擇資料夾…」(**累加**,可以選好幾個資料夾一起轉)→(路徑欄, 摘要)。"""
    merged = _append_paths(current, docsrc.pick_folder())
    return merged, preview_summary(merged, recursive)


def clear_paths():
    """「清空」→ 路徑欄與摘要都清掉(「開始轉檔」維持可按)。"""
    return "", ""


def top_level_dirs(dirs) -> list[Path]:
    """輸出資料夾清單 → 只留「最上層」的那幾個。

    批次轉一個含子資料夾的樹會產出好幾層的成品,每一層都開一個視窗是
    災難(使用者 2026-08-01 指定:下層不必開)。判準是包含關係而不是
    「只留一個」——`D:\\甲` 與 `D:\\甲\\乙` 只開前者,但 `D:\\甲` 與
    `D:\\乙` 是兩個獨立來源,兩個都要開。"""
    seen: list[Path] = []
    for raw in dirs or []:
        try:
            seen.append(Path(raw).resolve())
        except OSError:
            continue
    # 淺的排前面,才能用「已收的是不是我的祖先」一次判定;重複項會被
    # 同一條判斷吃掉(路徑對自己 is_relative_to 恆為真),不必先去重
    seen.sort(key=lambda p: len(p.parts))
    tops: list[Path] = []
    for p in seen:
        if not any(p.is_relative_to(t) for t in tops):
            tops.append(p)
    return tops


def _allow_foreground() -> bool:
    """盡力讓接下來開起來的檔案總管跳到最前面。回傳是否**可能**成功。

    **這件事在 Windows 上沒有保證成功的做法**,而且原因是設計如此:
    前景鎖定就是為了擋掉「背景程式亂搶焦點」。本程式在使用者操作瀏覽器
    時正是背景程序,所以 `AllowSetForegroundWindow(ASFW_ANY)` 的前提
    (MSDN:「**呼叫程序本身已經能設定前景視窗**」)並不成立,多半直接回
    FALSE——2026-08-01 只加這一步時,使用者回報仍然只有工作列閃爍。

    留著它是因為成本近乎零、某些情境下仍會生效;**但「使用者看得到
    成品」的保證不能押在這裡**——`open_output_dirs` 一定會回一句話說明
    位置,批次報告也會列出完整路徑。

    (曾另外用 `keybd_event` 送一次 VK_CONTROL 來重設前景鎖定計時器,
    使用者 2026-08-01 以「太複雜,而且工作列本來就會提醒」為由要求移除
    ——那是會在別人打字時插入一次按鍵的 hack,收益又不確定。**不要
    在無新指示下加回**。)"""
    try:
        import ctypes

        allowed = bool(ctypes.windll.user32.AllowSetForegroundWindow(-1))  # ASFW_ANY
        if not allowed:
            logger.debug("本程序無前景權,檔案總管可能只會在工作列閃爍")
        return allowed
    except Exception:
        logger.debug("放行前景權失敗", exc_info=True)
        return False


def open_output_dirs(dirs) -> None:
    """用檔案總管開啟輸出資料夾(只開最上層的那幾個)。

    成立前提同 srcfile 的原生對話框:App 只綁 127.0.0.1(spec §7),瀏覽器
    與伺服器必在同一台機器——所以「伺服器端開一個視窗」使用者才看得到。
    真做成 Server 版時這個功能整組不成立。

    **成功時不發任何提示**(使用者 2026-08-01 指定拿掉右上角的 toast):
    工作列本來就會提醒,而完整路徑已經印在批次報告裡了——「使用者找得到
    成品」的保證押在那份報告上,不需要再彈一次訊息。只有失敗才出聲。"""
    if sys.platform != "win32" or not hasattr(os, "startfile"):
        raise UserFacingError(
            "自動開啟資料夾只支援 Windows,請自行到原始文件所在的資料夾查看"
        )
    paths = top_level_dirs(dirs)
    if not paths:
        raise UserFacingError("這一批還沒有產生任何檔案")
    opened = 0
    for d in paths[:_MAX_OPEN_DIRS]:
        try:
            _allow_foreground()
            os.startfile(str(d))  # noqa: S606 - Windows 專用,路徑來自本程式的輸出
            opened += 1
        except OSError:
            logger.exception("開啟資料夾失敗:%s", d)
    if not opened:
        raise UserFacingError(f"開不了資料夾,請自行前往:{paths[0]}")
