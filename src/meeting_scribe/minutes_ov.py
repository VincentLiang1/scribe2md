r"""會議紀錄草稿的語言模型子行程,父端(Qwen 那兩條:Windows 的 OpenVINO、Mac 的 MLX;協定見
`minutes_worker` 檔頭)。

一次 `minutes.generate` 一支:`start()` → `ask()`×N → `close()`。跟 `transproc` 同樣的
三條規矩(都是那邊被實測逼出來的):子行程 `-u`、另起執行緒 drain stderr(OpenVINO
話很多,不讀會雙方互等)、永不裸 readline(才能同時看停止鈕與看門狗)。

跟 `transproc` 刻意不同的一處:**不是模組層單例**。這顆模型 2~5 GB 常駐記憶體,而
草稿一場才做一次;留著它等於轉下一份檔時多壓一顆模型在記憶體上。
"""
from __future__ import annotations

import json
import logging
import queue
import subprocess
import sys
import threading
import time

from meeting_scribe import cancel, plat

logger = logging.getLogger(__name__)

#: 等子行程載好模型。內顯第一次要編譯(4B 實測 14 秒;慢的內顯與硬碟再乘幾倍)
READY_TIMEOUT_SEC = 600.0
_POLL_SEC = 0.25


class WorkerGone(RuntimeError):
    """子行程起不來、死了、或不回話——這一場做不下去(不是跳過一段的那種失敗)。"""


class CallTimeout(RuntimeError):
    """這一次問太久——這一段跳過,子行程已經收掉、下一段會重開一支。"""


def _worker_cmd(model_dir: str, device: str) -> list[str]:
    """啟動命令。獨立一支是為了讓測試換上假 worker(測到的是真 Popen、真 pipe)。"""
    return [sys.executable, "-u", "-m", "meeting_scribe.minutes_worker", model_dir, device,
            *plat.worker_argv(low_priority=True)]


class Session:
    def __init__(self, model_dir: str, device: str) -> None:
        self.model_dir, self.device = model_dir, device
        self._proc: subprocess.Popen | None = None
        self._replies: queue.Queue = queue.Queue()

    def start(self) -> float:
        """起子行程、等模型載好;回載入秒數。"""
        self._replies = queue.Queue()
        self._proc = subprocess.Popen(  # noqa: S603 - 固定命令,無 shell
            _worker_cmd(self.model_dir, self.device),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            **plat.spawn_kwargs(low_priority=True),
        )
        threading.Thread(target=self._pump, args=(self._proc.stdout, self._replies),
                         daemon=True, name="minutes-stdout").start()
        threading.Thread(target=self._drain, args=(self._proc.stderr,),
                         daemon=True, name="minutes-stderr").start()
        try:
            reply = self._await(READY_TIMEOUT_SEC)
        except CallTimeout:
            self.close(kill=True)
            raise WorkerGone(f"AI 模型 {READY_TIMEOUT_SEC:.0f} 秒還沒載好") from None
        if not reply.get("ready"):
            self.close()
            raise WorkerGone(str(reply.get("error", "(無訊息)")))
        return float(reply.get("load_sec", 0.0))

    def ask(self, kind: str, instructions: str, prompt: str, timeout: float) -> dict:
        """問一次,回子行程的回覆(`ok` 為假的也照樣回,由呼叫端判斷)。"""
        if self._proc is None or self._proc.poll() is not None:
            self.start()   # 上一段超時被收掉了:重開一支
        try:
            self._proc.stdin.write(json.dumps(
                {"kind": kind, "instructions": instructions, "prompt": prompt},
                ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except Exception as e:
            self.close()
            raise WorkerGone(f"無法送出指令:{type(e).__name__}") from e
        try:
            return self._await(timeout)
        except CallTimeout:
            # 推論卡在原生碼裡,收不回來:收掉整支(下一段重開),不讓它佔著記憶體繼續算
            self.close(kill=True)
            raise

    def close(self, kill: bool = False) -> None:
        """收掉子行程(冪等)。平常先關 stdin 讓它自己退,逾時再 kill。

        ⚠️ **超時與停止鈕要 `kill=True`**:那時它正卡在一次推論裡,關 stdin 它也聽不到,
        客氣地等只是讓使用者多等那幾秒(假 worker 實測:超時後又多卡 4 秒)。"""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if kill:
                raise TimeoutError
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:  # pragma: no cover - 行程已消失
                logger.debug("會議紀錄子行程收不掉", exc_info=True)

    def _await(self, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            try:
                cancel.check()   # 停止鈕:Cancelled 往上拋
            except BaseException:
                self.close(kill=True)
                raise
            try:
                raw = self._replies.get(timeout=_POLL_SEC)
            except queue.Empty:
                if time.monotonic() > deadline:
                    raise CallTimeout(f"{timeout:.0f} 秒沒有回覆") from None
                continue
            if raw is None:
                code = self._proc.poll() if self._proc else None
                self.close()
                raise WorkerGone(f"子行程結束了(結束碼 {code})")
            try:
                return json.loads(raw)
            except ValueError:
                logger.debug("會議紀錄子行程回了非 JSON 的一行,略過:%.80s", raw)

    @staticmethod
    def _pump(stream, replies: queue.Queue) -> None:
        """每一行丟進**這一支**的佇列(重開時換新佇列,舊那支的墓碑不會串過來)。"""
        try:
            for line in stream:
                if line.strip():
                    replies.put(line.strip())
        except Exception:  # pragma: no cover - 被 kill 時的正常結束方式
            pass
        finally:
            replies.put(None)

    @staticmethod
    def _drain(stream) -> None:
        """**必須存在**(理由同 `transproc._drain_stderr`);一律 debug。"""
        try:
            for line in stream:
                if line.strip():
                    logger.debug("會議紀錄子行程:%s", line.rstrip())
        except Exception:  # pragma: no cover
            pass
