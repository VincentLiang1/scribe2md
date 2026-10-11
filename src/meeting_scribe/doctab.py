r"""「文字、圖像→MD」分頁的事件處理(`desktop.py` 只負責接線)。

分工同 `roster.py`／`wordlists.py`:**本模組不建任何 UI 元件**,也不 import
任何 UI 模組;需要狀態機(鎖介面、互斥旗標、進度)的部分留在 `desktop.py`。
判準不是「講的是不是同一件事」,是**「換一套 UI 要不要改」**——本模組只剩
純函式,錯誤一律 `UserFacingError`(同 docsrc 的作風)。

把關錯誤在這裡一律**回傳說明文字**而不是拋例外:選檔階段還沒開始做事,
用彈窗打斷太重。真正開始轉檔之後的錯誤才由 UI 層顯示,那是
`desktop.App._doc_start` 的事。
"""
import importlib.util
import io
import logging
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from meeting_scribe import docsrc, plat, srcfile
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
    這裡只做即時回饋,不是把關;真正的把關在 `desktop.App._doc_start` 那一步。
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
            f"⚠ 其中 {audio} 個是錄音/影片,要轉成逐字稿——"
            "每個檔可能要數十分鐘到數小時(視長度與本機有無顯示晶片),"
            "比文件慢很多。"
        )
    shown = docsrc.skipped_lines(skipped)[:_MAX_SKIPPED_SHOWN]
    if shown:
        parts.append("\n".join(shown))
        if len(skipped) > _MAX_SKIPPED_SHOWN:
            parts.append(f"(另有 {len(skipped) - _MAX_SKIPPED_SHOWN} 個項目未列出)")
    return "\n\n".join(p for p in parts if p)


def audio_summary(text, recursive: bool = False) -> str:
    """「聲音→MD」選檔區的摘要:這樣選會走**單檔**(轉完命名)還是**批次**(不命名)。

    與 `preview_summary` 同一個位置、同一個用途,但規則是音訊自己那一套:模式看
    輸入的**形狀**(`srcfile.looks_like_batch`,使用者 2026-08-06 拍板),白名單是
    `srcfile.SUPPORTED_TYPES`(混用會讓「聲音→MD」那顆「開始轉檔」開始接受 PDF)。
    使用者是在這裡決定要不要一次丟一批的——等 30 分鐘後才發現沒有命名就白花了。

    住在這裡而不是 `srcfile`:它要用 `docsrc.validate_batch`,而 `docsrc` 已經
    import 了 `srcfile`,反向 import 就是循環。**兩套介面共用這一份**(網頁版
    `app._src_summary` 只多包一層 `gr.update`;原生視窗 2026-09-02 補上多檔/資料夾
    時直接用),空字串 = 沒話說。

    只做即時回饋、**不控制按鈕**:把關全落在按下「開始轉檔」那一步。這裡不能拋
    例外——路徑打到一半必然是「找不到」,那不是錯誤。單檔那句刻意不講「選了 1 個
    檔案」:路徑打到一半時那句等於宣稱檔案存在(而它還不存在);這裡只講模式,
    那句話什麼時候都是真的。"""
    if not srcfile.clean_paths(text):
        return ""
    if not srcfile.looks_like_batch(text):
        return "**單一檔案**:轉完會讓你替每位講者命名。"
    try:
        files, skipped = docsrc.validate_batch(
            text, recursive=bool(recursive), types=srcfile.SUPPORTED_TYPES,
            what="錄音或錄影檔", hint=srcfile.supported_hint(),
        )
    except UserFacingError as e:
        return str(e)
    return f"{docsrc.summarize(files, skipped)}(整批連續轉,不做講者命名)"


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
    在無新指示下加回**。)

    ⚠️ **2026-09-24 起真正動手的是 `plat`**:macOS 根本沒有前景權這個概念
    (`open` 會自己把 Finder 帶到前面),而這裡留一支薄殼是因為上面那段
    「為什麼不保證成功」是**呼叫端**要知道的事。"""
    outcome = plat.allow_foreground()
    if not outcome.ok:
        logger.debug("沒有放行前景權:%s", outcome.reason)
    return outcome.ok


def open_output_dirs(dirs) -> None:
    """用檔案總管開啟輸出資料夾(只開最上層的那幾個)。

    成立前提同 srcfile 的原生對話框:程式與使用者在**同一台機器**上——所以
    「程式開一個檔案總管視窗」使用者才看得到。真做成 Server 版時這個功能
    整組不成立。

    **成功時不發任何提示**(使用者 2026-08-01 指定拿掉右上角的 toast):
    工作列本來就會提醒,而完整路徑已經印在批次報告裡了——「使用者找得到
    成品」的保證押在那份報告上,不需要再彈一次訊息。只有失敗才出聲。"""
    paths = top_level_dirs(dirs)
    if not paths:
        raise UserFacingError("這一批還沒有產生任何檔案")
    opened = 0
    unsupported = ""
    for d in paths[:_MAX_OPEN_DIRS]:
        outcome = plat.open_folder(d)
        if outcome.ok:
            opened += 1
            continue
        # ⚠️ **「這個平台不做這件事」與「這次開失敗了」要分開講**
        # (spec/mac/02 §2.2):前者是只能自己去找資料夾,後者可能下次就好
        if outcome.state == "unsupported":
            unsupported = outcome.reason
        else:
            logger.warning("開啟資料夾失敗(%s):%s", d, outcome.reason)
    if opened:
        return
    if unsupported:
        raise UserFacingError(
            f"這個系統不支援自動開啟資料夾,請自行前往:{paths[0]}")
    raise UserFacingError(f"開不了資料夾,請自行前往:{paths[0]}")


def reveal(path) -> None:
    r"""在檔案總管裡指出一個**檔**(選起來)或**資料夾**(開進去)。

    **為什麼這支住在這裡**:這裡是全 repo 唯一「用檔案總管開東西」的地方(前景權那一
    套就在隔壁的 `_allow_foreground`),而它與介面無關——換一套 UI 不必改,所以照
    `naming.py` 那條判準留在非 UI 模組。呼叫端目前是頁首那顆「📂 記錄檔…」
    (`desktop.App._open_log`)。

    ⚠️ **走 `explorer.exe` 而不是 `os.startfile`**(做法取自 MP4-2-SRT,那邊踩過):
    記錄檔是 `.log`,而 `.log` 在很多機器上**沒有預設開啟程式**——`startfile` 那時會跳
    出「你要如何開啟?」的選擇器,而使用者要的只是「讓我看到這個檔」。隔壁那支開的是
    資料夾,不會踩到,所以兩邊用不同的作法不是不一致。
    ⚠️ **一樣要先放行前景權**:不放行的話按下去只有工作列閃爍,而使用說明裡已經有一條
    FAQ 在講那個現象(「📁 按了「輸出資料夾…」,但沒看到視窗跳出來」)——別再製造第二個。
    """
    p = Path(path)
    if not p.exists():
        # ⚠️ 記錄檔那條路走得到這裡:開檔失敗(資料夾唯讀)時 `logs\` 根本沒被建出來。
        # 靜靜什麼都不做的話,那顆鈕看起來就是壞的。
        raise UserFacingError(f"找不到這個位置:{p}")
    _allow_foreground()
    outcome = plat.reveal(p)
    if outcome.ok:
        return
    logger.warning("開啟檔案管理員失敗(%s):%s", p, outcome.reason)
    if outcome.state == "unsupported":
        raise UserFacingError(f"這個系統不支援自動開啟檔案管理員,請自行前往:{p}")
    raise UserFacingError(f"開不了檔案管理員,請自行前往:{p}")


def install_skill(root: Path | None = None) -> str:
    r"""「🤖 安裝 Claude Skill…」:把 `skills/` 底下的 Skill 裝到這台電腦,回一段結果給使用者看。

    (2026-10-02 使用者選案 C1:Mac 交付的是 `.app`,工具資料夾藏在 `~/Library` 底下,
    叫人去那裡雙擊 `安裝Skill.command` 太繞。)
    ⚠️ **邏輯一律在 `scripts/install_skill.py`**:這顆鈕、`安裝Skill.bat`、`安裝Skill.command`
    是同一支的三個入口,這裡只是把它載進來叫 `main([])`——要改行為請改那一支。
    ⚠️ **載進同一個行程,不開子行程**:那一支只用標準函式庫,而 Mac 版的 python 是 bundle
    裡那顆、靠 `PYTHONPATH` 起來的,另開一個行程還得把那套環境變數原樣傳過去。
    ⚠️ 它的輸出是給終端機看的(逐個路徑),這裡改成一句話;**失敗時那段輸出才進記錄檔**。"""
    from meeting_scribe.paths import repo_root
    root = root or repo_root()
    script = root / "scripts" / "install_skill.py"
    if not script.is_file():
        raise UserFacingError("找不到 Skill 的安裝程式(scripts/install_skill.py),"
                              "工具資料夾可能不完整,請重新解壓縮或重新安裝。")
    out = io.StringIO()
    try:
        spec = importlib.util.spec_from_file_location("_meeting_scribe_install_skill", script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with redirect_stdout(out), redirect_stderr(out):
            rc = mod.main([])
    except Exception as exc:  # noqa: BLE001 - 寫不進設定資料夾之類,英文原文進記錄檔
        logger.exception("安裝 Skill 失敗;安裝程式的輸出:\n%s", out.getvalue())
        raise UserFacingError("Skill 安裝失敗,寫不進 Claude Code 的設定資料夾。") from exc
    if rc != 0:
        logger.warning("安裝 Skill 失敗(離開碼 %s);安裝程式的輸出:\n%s", rc, out.getvalue())
        lines = [ln for ln in out.getvalue().splitlines() if ln.strip()]
        raise UserFacingError("Skill 安裝失敗:" + (lines[-1] if lines else "原因不明。"))
    logger.info("已安裝 Skill:\n%s", out.getvalue())
    names = sorted(p.name for p in (root / "skills").iterdir() if p.is_dir())
    return (f"已安裝 Claude Code Skill:{'、'.join(names)}\n"
            f"位置:{mod.skills_root()}\n\n"
            "重新開啟 Claude Code 就會生效。之後只要跟它說要看哪份 PDF 或 Word,"
            "它就會自己先轉成 Markdown 再讀。換一台電腦要在那台再按一次;"
            "工具更新之後也按一次,才會用到新版的 Skill 說明。")
