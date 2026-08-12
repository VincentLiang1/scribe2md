r"""OCR 的父行程介面:把辨識工作交給一支**獨立子行程**(`ocr_worker.py`)。

**本模組絕不 import ocr_worker**——一 import 就把 rapidocr / opencv /
onnxruntime 整套拉進主行程,隔離就白做了。子行程以
`sys.executable -u -m meeting_scribe.ocr_worker` 啟動。

為什麼要隔離(五條,依重要性):

1. **DLL 圖譜乾淨**。主行程可能已經載入 sherpa-onnx 那顆「靜態連 CPU 版
   ORT 的單一 .pyd」,再讓 pip 版 `onnxruntime.dll` 進來,加上 Windows
   System32 那份舊版的名稱優先解析(見 `diarize._preload_pip_onnxruntime`
   的血淚),就是三方混戰。子行程只載入 OCR 需要的那一份。
2. **原生相依不進主行程**。RapidOCR 會拖 opencv-python + shapely +
   pyclipper(數百 MB 原生碼),啟動路徑永遠不必付這筆。
3. **崩潰隔離**。segfault 只殺子行程,父行程看到 returncode 就把它記成
   「這一張圖的失敗」,其餘幾百張照跑。
4. **記憶體回收**。300 頁掃描 PDF 跑完,ORT 的 arena 隨行程結束全部歸還
   ——8GB 基準機的硬需求。
5. **真正可中斷**。這是全專案唯一能「硬中斷」的地方:停止鈕可以直接
   `kill()`,不必等協作式檢查點。

**pipe 死結是這種互動式子行程的頭號死法**,三道防護缺一不可:子行程用
`-u`(不緩衝)、父行程**另起執行緒 drain stderr**(不 drain 會在 stderr
緩衝滿時雙方互等)、父行程**永不裸 readline**(讀取在背景執行緒 + queue,
主迴圈輪詢才能同時看取消旗標與看門狗)。

子行程的收尾靠 **stdin EOF**:父行程無論正常結束或被強制終止,OS 都會關掉
pipe,子行程的請求迴圈隨之結束、行程自然退出——所以不需要另外掃孤兒。
"""
import atexit
import json
import logging
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from meeting_scribe import cancel, power
from meeting_scribe.errors import UserFacingError

logger = logging.getLogger(__name__)

# 首次啟動要載入三顆模型,給寬一點
_READY_TIMEOUT = 90.0
# 單張圖的看門狗:超過這個時間沒回,判定子行程掛了
_PAGE_TIMEOUT = 120.0
# 輪詢間隔:停止鈕的反應時間上限,也是子行程死亡的偵測延遲
_POLL_SEC = 0.25
# 整個批次最多重啟幾次子行程。連續掛掉多半是模型/裝置問題,一直重試
# 只是空轉 CPU,不如整條 OCR 收手並告訴使用者「其他格式仍可正常轉」
_MAX_RESTARTS = 2

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
# **OCR 子行程刻意跑在較低的優先權**(使用者 2026-08-03:「OCR 轉換時 CPU
# 被用掉很多資源,我希望還能用電腦」)。轉一批文件動輒幾十分鐘到數小時,
# 那段時間人不會乾等——所以它該是「撿空檔做」的背景工作,不是跟前景平起
# 平坐搶 CPU 的東西。
#
# 實測(8 核機器,前景 = 6 行程的 CPU 密集工作,3 輪取中位):
#
#   跑法              前景被拖慢   OCR 自己的速度
#   一般優先權          +16%        1,316 ms/張
#   below-normal       +2%        1,667 ms/張   ← 選這個
#   一般+限 2 執行緒     +14%        1,786 ms/張
#
# **「限制執行緒數」是無效的做法,別改用它**:OCR 行程仍與前景平起平坐,
# 前景只從 +16% 降到 +14%,而 OCR 自己反而比 below-normal 更慢——兩頭都輸。
# GPU 也試過(見 ocr_worker._build_onnxruntime):只吃 0.98 顆核心但 4,252
# ms/張,被這條完全壓制。
#
# 代價要誠實記著:**機器閒著時 OCR 也會慢約 21%**(25 秒內 19 張 → 15 張)。
# 理論上閒置時不該有差,實測有——推測是 Windows Defender 這類一般優先權的
# 背景程序會插隊。這不是免費午餐,是拿兩成批次速度換「電腦隨時能用」。
#
# 優先權**會被孫行程繼承**(uv 環境下 `sys.executable` 是跳板,真正載入模型
# 的是它的子行程),實測兩層的 Priority 都從 8 降到 6。
_BELOW_NORMAL_PRIORITY = 0x00004000 if sys.platform == "win32" else 0


@dataclass(frozen=True)
class OcrLine:
    """一段辨識結果。box 是 (x0, y0, x1, y1),原點在左上。"""
    text: str
    box: tuple[float, float, float, float]
    score: float


class _WorkerGone(RuntimeError):
    """子行程死了或沒回應(內部訊號,不會逸出到呼叫端)。"""


# 引擎不可併發,而且整批共用一支子行程(省掉每檔 2 秒的初始化):
# 所有對外函式都在這把鎖內操作模組層狀態
_lock = threading.RLock()
_proc: subprocess.Popen | None = None
_replies: queue.Queue | None = None
_restarts = 0
_disabled = False


def _spawn() -> tuple[subprocess.Popen, queue.Queue]:
    """啟動子行程,回傳 (行程, 回應佇列)。"""
    cmd = [
        sys.executable, "-u", "-m", "meeting_scribe.ocr_worker",
        "--threads", str(power.cpu_worker_count()),
    ]
    proc = subprocess.Popen(  # noqa: S603 - 固定命令,無 shell
        cmd,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        creationflags=_CREATE_NO_WINDOW | _BELOW_NORMAL_PRIORITY,
    )
    replies: queue.Queue = queue.Queue()
    threading.Thread(
        target=_pump_stdout, args=(proc.stdout, replies), daemon=True,
    ).start()
    threading.Thread(target=_drain_stderr, args=(proc.stderr,), daemon=True).start()
    return proc, replies


def _pump_stdout(stream, replies: queue.Queue) -> None:
    """把子行程的每一行回應丟進佇列。串流結束時放一個 None 當墓碑。"""
    try:
        for line in stream:
            line = line.strip()
            if line:
                replies.put(line)
    except Exception:  # pragma: no cover - 行程被 kill 時的正常結束方式
        logger.debug("OCR 子行程輸出中斷", exc_info=True)
    finally:
        replies.put(None)


def _drain_stderr(stream) -> None:
    """**必須存在**:不讀 stderr 的話,子行程寫滿 OS 緩衝就會卡住不動,
    而父行程還在等 stdout——雙方互等,整個轉檔凍住。"""
    try:
        for line in stream:
            if line.strip():
                logger.debug("OCR 子行程:%s", line.rstrip())
    except Exception:  # pragma: no cover
        pass


def _await_reply(timeout: float) -> dict:
    """等一行回應。輪詢而不是裸 readline,才能同時看取消旗標與看門狗。"""
    assert _replies is not None and _proc is not None
    deadline = time.monotonic() + timeout
    while True:
        cancel.check()  # 停止鈕:讓 Cancelled 往上拋,由呼叫端 kill
        try:
            raw = _replies.get(timeout=_POLL_SEC)
        except queue.Empty:
            if time.monotonic() > deadline:
                raise _WorkerGone(f"等待回應超過 {timeout:.0f} 秒")
            continue
        if raw is None:
            raise _WorkerGone("子行程已結束")
        try:
            return json.loads(raw)
        except ValueError:
            logger.debug("OCR 子行程回了非 JSON 的一行,略過:%.120s", raw)


def _kill_locked() -> None:
    """收掉目前的子行程。先關 stdin 讓它自己退(乾淨),逾時再 kill。"""
    global _proc, _replies
    proc, _proc, _replies = _proc, None, None
    if proc is None:
        return
    try:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
        proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:  # pragma: no cover
            logger.debug("OCR 子行程收不掉", exc_info=True)


def _start_locked() -> None:
    """確保有一支就緒的子行程。"""
    global _proc, _replies
    if _proc is not None and _proc.poll() is None:
        return
    _kill_locked()
    _proc, _replies = _spawn()
    reply = _await_reply(_READY_TIMEOUT)
    if not reply.get("ready"):
        detail = reply.get("error", "")
        _kill_locked()
        raise _WorkerGone(f"引擎啟動失敗:{detail}")


def _fail(detail: str) -> UserFacingError:
    global _disabled
    _disabled = True
    return UserFacingError(
        f"文字辨識(OCR)無法運作,已停用:{detail}。"
        "掃描檔與圖片這次不會有文字內容,其他格式仍可正常轉換。"
    )


def _with_worker(work, why: str):
    """在「確保子行程活著」的前提下做一件事,掛了就重啟重試一次。

    重試與停用是**同一份政策**,ensure_ready 與 recognize 共用——分開
    寫兩份的話,改 `_MAX_RESTARTS` 的語意或 `_disabled` 的時機都得記得
    改兩個地方。呼叫端已持有 `_lock`。"""
    global _restarts
    if _disabled:
        raise _fail(why)
    for attempt in (1, 2):
        try:
            _start_locked()
            return work()
        except cancel.Cancelled:
            _kill_locked()  # 硬中斷:這是全專案唯一能立刻停下的地方
            raise
        except _WorkerGone as e:
            _kill_locked()
            _restarts += 1
            if attempt == 2 or _restarts > _MAX_RESTARTS:
                raise _fail(str(e)) from None
            logger.warning("OCR 子行程異常(%s),重啟後重試一次", e)
    raise _fail("重試後仍無法完成")  # pragma: no cover - 迴圈必 return 或 raise


def ensure_ready() -> None:
    """先把子行程叫起來、等它載完模型。

    擺位抄 `pipeline.py` 的 `punctuate.ensure_ready()`:引擎問題要在批次的
    **第一秒**炸出來,不能跑到第 47 個檔才發現——那時前面的時間已經花掉,
    而使用者以為整批都會成功。"""
    with _lock:
        _with_worker(lambda: None, "先前已啟動失敗")


def recognize(image_path: Path | str, timeout: float = _PAGE_TIMEOUT) -> list[OcrLine]:
    """辨識一張圖。子行程掛掉會自動重啟並重試一次。

    回傳空清單代表「這張圖沒有文字」——那是正常結果,不是失敗。"""
    path = str(image_path)
    with _lock:
        return _with_worker(
            lambda: _request_locked(path, timeout), "先前已停用",
        )


def _request_locked(path: str, timeout: float) -> list[OcrLine]:
    assert _proc is not None and _proc.stdin is not None
    try:
        _proc.stdin.write(json.dumps({"image": path}, ensure_ascii=False) + "\n")
        _proc.stdin.flush()
    except OSError as e:
        raise _WorkerGone(f"送不出請求:{e}") from e
    reply = _await_reply(timeout)
    if not reply.get("ok"):
        # 單張圖辨識失敗(壞圖、格式不支援)不代表引擎壞了:回空結果,
        # 由呼叫端標註這一張沒有文字,不要把整批 OCR 拖下水
        logger.warning("OCR 辨識失敗(%s):%s", path, reply.get("error"))
        return []
    return [
        OcrLine(
            text=str(item.get("text", "")),
            box=tuple(float(v) for v in item.get("box", (0, 0, 0, 0))),
            score=float(item.get("score", 0.0)),
        )
        for item in reply.get("lines", [])
        if str(item.get("text", "")).strip()
    ]


def shutdown() -> None:
    """收掉子行程(批次結束時呼叫;atexit 也掛了一份保險)。"""
    global _restarts, _disabled
    with _lock:
        _kill_locked()
        _restarts = 0
        _disabled = False


atexit.register(shutdown)


# ---- 結果整理 ----

# 兩段文字的垂直中心差在「平均字高 × 這個比例」以內就算同一行
_SAME_LINE_RATIO = 0.6


def lines_to_text(lines: list[OcrLine]) -> str:
    """辨識結果 → 讀得順的文字。

    OCR 回的是一堆彼此獨立的框,**沒有「行」的概念**:同一行的字常被切成
    好幾段(欄位之間、標點附近),而框的順序也不保證是閱讀順序。不重新
    分行的話,輸出讀起來像被打散的字串,對 AI 與人都沒用。

    作法:依垂直中心分群成「行」(容差取平均字高的六成,對付基線抖動),
    行內再依水平位置排序。"""
    items = [ln for ln in lines if ln.text.strip()]
    if not items:
        return ""
    heights = [ln.box[3] - ln.box[1] for ln in items] or [1.0]
    tol = max(sum(heights) / len(heights) * _SAME_LINE_RATIO, 1.0)

    rows: list[list[OcrLine]] = []
    centres: list[float] = []
    for ln in sorted(items, key=lambda x: (x.box[1] + x.box[3]) / 2):
        cy = (ln.box[1] + ln.box[3]) / 2
        for i, centre in enumerate(centres):
            if abs(centre - cy) <= tol:
                rows[i].append(ln)
                centres[i] = sum((x.box[1] + x.box[3]) / 2 for x in rows[i]) / len(rows[i])
                break
        else:
            rows.append([ln])
            centres.append(cy)

    out = []
    for row in rows:
        row.sort(key=lambda x: x.box[0])
        out.append(" ".join(x.text.strip() for x in row))
    return "\n".join(line for line in out if line.strip())
