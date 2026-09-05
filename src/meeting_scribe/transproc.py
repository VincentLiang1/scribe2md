r"""轉錄子行程的父端(協定與「為什麼要獨立行程」見 transworker 檔頭)。

與 diarproc 的差別,三處刻意不同:

- **模組層單例,活得越久越划算**(同 ocr.py):OV 模型冷編譯實測 200 秒
  上下,批次一整個資料夾若每檔重開一支,光編譯就把轉檔時間吃掉。
  diarproc 綁定「一場錄音」的增量狀態所以錄完就死,這支沒有狀態。
- **子行程降到 below-normal,父行程不降**:整個改動的目的就是這一條
  ——讓吃 CPU 的東西讓路,而 gradio 的網頁伺服器(在父行程)隨時回應。
- **失敗不重啟、直接往上拋**:轉錄是轉檔的主線,重跑一次要幾十分鐘,
  默默重試只會讓使用者多等一輪還不知道發生什麼事。OCR 那支重啟是因為
  「一張圖失敗只影響一張圖」,這裡不成立。

沿用被實測逼出來的三條規矩,一條都不能少(同 ocr/diarproc):
- 子行程一定要 `-u`(不緩衝),否則回應卡在它的緩衝區裡;
- **父行程必須另起執行緒 drain stderr**,不 drain 會在緩衝滿時雙方互等
  ——OpenVINO 的 onednn_verbose 話很多,這裡特別容易踩;
- **永不裸 readline**:背景執行緒 + queue 輪詢,才能同時看取消旗標與看門狗。
"""
import json
import logging
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from meeting_scribe import cancel, power
from meeting_scribe.types import TranscriptSegment

logger = logging.getLogger(__name__)

# 等子行程回報就緒:只是起行程 + import,不建模型(見 transworker 檔頭)
_READY_TIMEOUT = 120.0
# 單一轉錄指令的看門狗。**必須夠長**:一支 2 小時 19 分的錄音實測轉錄
# 約 30 分鐘,而 OV 首次還要付冷編譯(實測 200 秒上下)。90 分鐘只是
# 「行程還活著但不動了」的最後保險——正常結束由 stdout 的回應決定,
# 而且每則進度訊息都會把看門狗往後推(見 _await)
_WORK_TIMEOUT = 5400.0
_POLL_SEC = 0.25
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

_instance: "TransProcess | None" = None
_instance_lock = threading.Lock()


class TransWorkerGone(RuntimeError):
    """子行程死了、沒回應,或回報錯誤。"""


def _worker_cmd(threads: int) -> list[str]:
    """啟動子行程的命令。**`-u` 不可省**(同 diarproc._worker_cmd)。
    獨立成函式是為了讓協定測試換上假 worker——那樣測到的是真 Popen、
    真 pipe、真看門狗。"""
    return [
        sys.executable, "-u", "-m", "meeting_scribe.transworker",
        "--threads", str(threads),
    ]


class TransProcess:
    """轉錄子行程。用法:start() → transcribe()* → close()。"""

    def __init__(self, threads: int | None = None) -> None:
        self._threads = threads or power.cpu_worker_count()
        self._proc: subprocess.Popen | None = None
        self._replies: queue.Queue = queue.Queue()
        self._lock = threading.Lock()  # 引擎不可併發:一次只准一個指令在飛
        self.on_progress: Callable[[float], None] | None = None

    @property
    def threads(self) -> int:
        return self._threads

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        self._proc = subprocess.Popen(  # noqa: S603 - 固定命令,無 shell
            _worker_cmd(self._threads),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            # ⚠️ 這一行就是整個改動的目的:**只降子行程**。父行程(含 gradio
            # 網頁伺服器)維持一般優先權,滿載時介面照樣回應。
            # 優先權會被孫行程繼承(uv 環境下 sys.executable 是跳板,
            # 真正載模型的是它的子行程——ocr.py 實測兩層都從 8 降到 6)
            creationflags=_CREATE_NO_WINDOW | power.BELOW_NORMAL_PRIORITY_CLASS,
        )
        threading.Thread(
            target=self._pump_stdout, args=(self._proc.stdout,),
            daemon=True, name="trans-stdout",
        ).start()
        threading.Thread(
            target=self._drain_stderr, args=(self._proc.stderr,),
            daemon=True, name="trans-stderr",
        ).start()
        reply = self._await(_READY_TIMEOUT)
        if not reply.get("ready"):
            self.close()
            raise TransWorkerGone(f"轉錄子行程啟動失敗:{reply.get('error', '(無訊息)')}")
        logger.info("轉錄子行程已就緒(%d 條執行緒,below-normal)", self._threads)

    def close(self) -> None:
        """收掉子行程(冪等)。先關 stdin 讓它自己退,逾時再 kill。"""
        proc, self._proc = self._proc, None
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
            except Exception:  # pragma: no cover - 行程已消失
                logger.debug("轉錄子行程收不掉", exc_info=True)

    def transcribe(
        self, wav: Path | str, model_key: str = "fast",
        progress: Callable[[float], None] | None = None,
    ) -> tuple[list[TranscriptSegment], str]:
        """回傳 (轉錄結果, 實際裝置);簽章與 transcribe.transcribe 一致。"""
        self.on_progress = progress
        try:
            reply = self._request({
                "cmd": "transcribe", "wav": str(Path(wav).resolve()),
                "model": model_key, "progress": progress is not None,
            })
        finally:
            self.on_progress = None
        segments = [
            TranscriptSegment(float(a), float(b), t)
            for a, b, t in reply.get("segments", [])
        ]
        return segments, str(reply.get("device", "cpu"))

    # -- 管線 ----------------------------------------------------------

    def _request(self, msg: dict) -> dict:
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                raise TransWorkerGone("子行程已結束")
            try:
                proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
                proc.stdin.flush()
            except Exception as e:
                raise TransWorkerGone(f"無法送出指令:{e}") from e
            reply = self._await(_WORK_TIMEOUT)
        if not reply.get("ok"):
            raise TransWorkerGone(reply.get("error", "(無訊息)"))
        return reply

    def _await(self, timeout: float) -> dict:
        """等一行回應。輪詢而不是裸 readline,才能同時看取消旗標與看門狗。"""
        deadline = time.monotonic() + timeout
        while True:
            cancel.check()  # 停止鈕:Cancelled 往上拋,由呼叫端 close()
            try:
                raw = self._replies.get(timeout=_POLL_SEC)
            except queue.Empty:
                if time.monotonic() > deadline:
                    raise TransWorkerGone(f"等待回應超過 {timeout:.0f} 秒")
                continue
            if raw is None:
                raise TransWorkerGone("子行程已結束")
            try:
                reply = json.loads(raw)
            except ValueError:
                logger.debug("子行程回了非 JSON 的一行,略過:%.120s", raw)
                continue
            if "log" in reply:
                # 子行程自家的 log 按原級別重播:黑視窗的心跳訊息是使用者
                # 判斷「程式還活著」的唯一依據(見 transworker 檔頭的 ⚠️),
                # 藏進 debug 等於沒有。log 也要把看門狗往後推
                level = getattr(logging, str(reply.get("level", "INFO")), logging.INFO)
                logger.log(level, "%s", reply["log"])
                deadline = time.monotonic() + timeout
                continue
            if "progress" in reply:
                # 進度是「指令執行中」的中間訊息,不是這次的回應:轉給掛鉤
                # 之後**繼續等**,而且看門狗要跟著往後推——一支長錄音要跑
                # 幾十分鐘但一直有進度,不能被判成沒回應
                hook = self.on_progress
                if hook is not None:
                    hook(float(reply["progress"]))
                deadline = time.monotonic() + timeout
                continue
            return reply

    def _pump_stdout(self, stream) -> None:
        """每一行回應丟進佇列;串流結束時放一個 None 當墓碑。"""
        try:
            for line in stream:
                line = line.strip()
                if line:
                    self._replies.put(line)
        except Exception:  # pragma: no cover - 被 kill 時的正常結束方式
            logger.debug("轉錄子行程輸出中斷", exc_info=True)
        finally:
            self._replies.put(None)

    def _drain_stderr(self, stream) -> None:
        """**必須存在**:不讀 stderr 的話,子行程寫滿 OS 緩衝就會卡住,
        而父行程還在等 stdout——雙方互等,整支轉錄就此凍住。OpenVINO 的
        onednn_verbose 話很多,這裡特別容易踩到。

        一律 debug:這條通道上混著第三方的洪流,自家要給人看的訊息走的是
        結構化的 log 訊息(見 _await)。"""
        try:
            for line in stream:
                if line.strip():
                    logger.debug("轉錄子行程:%s", line.rstrip())
        except Exception:  # pragma: no cover - 行程被 kill 時的正常結束方式
            logger.debug("轉錄子行程 stderr 中斷", exc_info=True)


def get(threads: int | None = None) -> TransProcess:
    """取得(或建立)共用的轉錄子行程。

    模組層單例:OV 冷編譯很貴,批次多檔重用同一支才划算。執行緒數變更時
    重開一支——同 clear_engine_cache 的理由,執行緒數在引擎建構時定死。"""
    global _instance
    want = threads or power.cpu_worker_count()
    with _instance_lock:
        if _instance is not None and (
            not _instance.alive() or _instance.threads != want
        ):
            _instance.close()
            _instance = None
        if _instance is None:
            proc = TransProcess(want)
            proc.start()
            _instance = proc
        return _instance


def shutdown() -> None:
    """收掉共用子行程(冪等)。CPU 核心數變更或程式結束時呼叫。"""
    global _instance
    with _instance_lock:
        if _instance is not None:
            _instance.close()
            _instance = None
