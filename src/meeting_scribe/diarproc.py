r"""講者分析子行程的父端(協定與「為什麼要獨立行程」見 diarworker 檔頭)。

與 ocr.py 的差別,兩處刻意不同:

- **不是模組層單例,是每場錄音一支**:OCR 那支服務整批文件、活得越久越
  划算;這支綁定一場錄音的狀態(各軌的增量切分進度),錄完就該死透,
  留著只會讓下一場接到上一場的狀態。
- **不重啟、不重試**:呼叫端(live.LiveDiarizer)本來就把增量切分當成
  「提前做而已」——任何失敗都退回「收尾再算一次」,結果完全正確,只是
  慢一點。在這裡加重啟只是把同一個政策寫兩遍。

沿用 ocr.py 那三條被實測逼出來的規矩,一條都不能少:
- 子行程一定要 `-u`(不緩衝),否則回應卡在它的緩衝區裡;
- **父行程必須另起執行緒 drain stderr**,不 drain 會在緩衝滿時雙方互等;
- **永不裸 readline**:背景執行緒 + queue 輪詢,才能同時看取消旗標與看門狗。
"""
import base64
import json
import logging
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np

from meeting_scribe import cancel, power
from meeting_scribe.types import SpeakerQuality, SpeakerTurn

logger = logging.getLogger(__name__)

# 等子行程回報就緒:要載 sherpa 的原生 DLL 並建兩顆模型,冷開機慢
_READY_TIMEOUT = 120.0
# 單一指令的看門狗。一塊 5 分鐘音訊的切分實測數分鐘,收尾(finish)還要
# 補算剩餘塊 + 全域重聚;放寬到 30 分鐘,它只是「行程還活著但不動了」
# 的最後保險,正常結束由 stdout 的回應決定
_WORK_TIMEOUT = 1800.0
_POLL_SEC = 0.25
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class DiarWorkerGone(RuntimeError):
    """子行程死了、沒回應,或回報錯誤。呼叫端據此退回「收尾再算」。"""


def _abs(path: Path | str) -> str:
    """一律送絕對路徑:子行程雖然繼承 cwd,但那是個沒必要的隱含相依——
    路徑是這條協定唯一會出錯的欄位(見 stdio 記的那次災情),
    能少一個變因就少一個。"""
    return str(Path(path).resolve())


def _worker_cmd(threads: int) -> list[str]:
    """啟動子行程的命令。**`-u` 不可省**:子行程的回應會卡在它的輸出
    緩衝區裡,父行程乾等到看門狗超時(同 ocr._spawn)。獨立成函式是為了
    讓協定測試換上假 worker——那樣測到的是真 Popen、真 pipe、真看門狗。"""
    return [
        sys.executable, "-u", "-m", "meeting_scribe.diarworker",
        "--threads", str(threads),
    ]


class DiarProcess:
    """一場錄音用的講者分析子行程。用法:start() → poll()* → finish()* → close()。"""

    def __init__(
        self, threads: int | None = None, below_normal: bool = False,
    ) -> None:
        # below_normal:**錄音一律 False**(收音執行緒被排擠正是 2026-08-03
        # 掉 4.6 分鐘音訊的災情);檔案轉檔給 True——那是使用者 2026-08-04
        # 要的「轉換時電腦還能用」,而降的是子行程,父行程的 gradio 不受影響
        self._below_normal = below_normal
        self._threads = threads or power.cpu_worker_count()
        self._proc: subprocess.Popen | None = None
        self._replies: queue.Queue = queue.Queue()
        self._lock = threading.Lock()  # 引擎不可併發:一次只准一個指令在飛
        # 進行中那塊的完成度。**平時是 None**(錄音期間沒人看),此時指令會
        # 帶 progress=false、子行程根本不建 callback——實測引擎每秒回呼約
        # 19 次,每則在父端要付兩次 GIL 取得 + 一次 JSON 解析,而收音執行緒
        # 每秒也才 110 次,那是整個改動花大力氣清空的那條路。收尾等待時由
        # live.LiveDiarizer 掛上,否則進度條會在那幾分鐘定格
        self.on_progress: Callable[[float], None] | None = None

    # -- 生命週期 ------------------------------------------------------

    def start(self) -> None:
        self._proc = subprocess.Popen(  # noqa: S603 - 固定命令,無 shell
            _worker_cmd(self._threads),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            creationflags=_CREATE_NO_WINDOW | (
                power.BELOW_NORMAL_PRIORITY_CLASS if self._below_normal else 0
            ),
        )
        threading.Thread(
            target=self._pump_stdout, args=(self._proc.stdout,),
            daemon=True, name="diar-stdout",
        ).start()
        threading.Thread(
            target=self._drain_stderr, args=(self._proc.stderr,),
            daemon=True, name="diar-stderr",
        ).start()
        reply = self._await(_READY_TIMEOUT)
        if not reply.get("ready"):
            self.close()
            raise DiarWorkerGone(f"引擎啟動失敗:{reply.get('error', '(無訊息)')}")
        logger.info("講者分析子行程已就緒(%d 條執行緒)", self._threads)

    def close(self) -> None:
        """收掉子行程(冪等)。先關 stdin 讓它自己退,逾時再 kill。

        **一定要真的收掉**:留一支在跑的,下一場錄音會與它併用同一批模型
        檔、也白佔 CPU(同 live.LiveDiarizer.close 的理由)。"""
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
                logger.debug("講者分析子行程收不掉", exc_info=True)

    # -- 指令 ----------------------------------------------------------

    def poll(self, kind: str, wav: Path, avail_sec: float) -> float:
        """推進該軌的增量切分,回傳已完成秒數。"""
        reply = self._request(
            {"cmd": "poll", "kind": kind, "wav": _abs(wav), "avail": avail_sec,
             "progress": self.on_progress is not None})
        return float(reply.get("done", 0.0))

    def finish(
        self, kind: str, wav: Path, total_sec: float, num_speakers: int = 0,
    ) -> tuple[list[SpeakerTurn], dict, list[SpeakerQuality]]:
        """補完剩餘的塊 + 全域重聚,回傳 (turns, 聲紋, 分群品質)。"""
        return self._turns_reply({
            "cmd": "finish", "kind": kind, "wav": _abs(wav),
            "total": total_sec, "speakers": num_speakers,
            "progress": self.on_progress is not None,
        })

    def diarize(
        self, wav: Path, num_speakers: int = 0,
    ) -> tuple[list[SpeakerTurn], dict, list[SpeakerQuality]]:
        """離線整檔一次做完(檔案轉檔用),等同 diarize.diarize()。

        **與 finish() 不是同一條路**:finish 走的是錄音用的
        IncrementalDiarizer(5 分鐘塊),離線是 15 分鐘塊——塊界不同分群
        結果就不同,不可互相代用(理由寫在 diarworker 那一段)。"""
        return self._turns_reply({
            "cmd": "diarize", "wav": _abs(wav), "speakers": num_speakers,
            "progress": self.on_progress is not None,
        })

    def _turns_reply(
        self, msg: dict,
    ) -> tuple[list[SpeakerTurn], dict, list[SpeakerQuality]]:
        reply = self._request(msg)
        # 舊回應只有三欄(起, 訖, 講者):conf 給 0 = 核對表顯示空白,
        # 而不是讓整場收尾因為一個欄位炸掉(同下面 quality 的取捨)
        turns = [
            SpeakerTurn(float(row[0]), float(row[1]), int(row[2]),
                        float(row[3]) if len(row) > 3 else 0.0)
            for row in reply.get("turns", [])
        ]
        vps = {
            int(lab): np.frombuffer(base64.b64decode(blob), dtype=np.float32)
            for lab, blob in reply.get("vp", [])
        }
        # 舊回應沒有 quality 欄位就給空清單:少了它只是少一段診斷,
        # 而讓整場錄音的收尾因為一個診斷欄位炸掉是不成比例的
        quality = [
            SpeakerQuality(int(sp), int(n), float(sec), float(coh))
            for sp, n, sec, coh in reply.get("quality", [])
        ]
        return turns, vps, quality

    # -- 管線 ----------------------------------------------------------

    def _request(self, msg: dict) -> dict:
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                raise DiarWorkerGone("子行程已結束")
            try:
                proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
                proc.stdin.flush()
            except Exception as e:
                raise DiarWorkerGone(f"無法送出指令:{e}") from e
            reply = self._await(_WORK_TIMEOUT)
        if not reply.get("ok"):
            raise DiarWorkerGone(reply.get("error", "(無訊息)"))
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
                    raise DiarWorkerGone(f"等待回應超過 {timeout:.0f} 秒")
                continue
            if raw is None:
                raise DiarWorkerGone("子行程已結束")
            try:
                reply = json.loads(raw)
            except ValueError:
                logger.debug("子行程回了非 JSON 的一行,略過:%.120s", raw)
                continue
            if "progress" in reply:
                # 進度是「指令執行中」的中間訊息,不是這次的回應:轉給
                # 掛鉤之後**繼續等**,而且看門狗要跟著往後推——一塊跑很久
                # 但一直有進度,不能被判成沒回應
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
            logger.debug("講者分析子行程輸出中斷", exc_info=True)
        finally:
            self._replies.put(None)

    def _drain_stderr(self, stream) -> None:
        """**必須存在**:不讀 stderr 的話,子行程寫滿 OS 緩衝就會卡住,
        而父行程還在等 stdout——雙方互等,整場錄音的切分就此凍住。"""
        try:
            for line in stream:
                if line.strip():
                    logger.debug("講者分析子行程:%s", line.rstrip())
        except Exception:  # pragma: no cover - 行程被 kill 時的正常結束方式
            # 留一行:stderr drain 停掉正是這種子行程最經典的死法,
            # 而它一停,下一次緩衝寫滿就是父子互等(同 _pump_stdout)
            logger.debug("講者分析子行程 stderr 中斷", exc_info=True)
