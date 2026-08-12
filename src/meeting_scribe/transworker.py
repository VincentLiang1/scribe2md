r"""轉錄子行程:檔案轉檔的轉錄在這裡跑,不在主行程。

**為什麼要獨立行程**(使用者 2026-08-07 選定):轉檔期間主行程被降到
below-normal 好讓「電腦還能用」(2026-08-04 指定),而 **gradio 的網頁
伺服器跟轉錄跑在同一個行程裡**——於是介面也一起被降,滿載時停止鈕要
15~35 秒才有反應(暖機期 95 秒,ui.md 實測),使用者按 F5 等不到就以為
程式死了(2026-08-07 實跡:連關兩次重來,而它一直好好地在跑)。

搬進子行程之後:**只降子行程的優先權**(父端的 creationflags),主行程
維持一般優先權,介面隨時回應。順帶拿到 ocr_worker/diarworker 那幾條
同樣的好處:OpenVINO/CTranslate2 的原生 DLL 不進主行程、崩潰只殺子行程、
執行緒池與 arena 隨行程歸還(轉錄的 GIL 停頓實測 1,450ms,也一併移出)。

協定同 diarworker(stdin 收 NDJSON、stdout 只准 NDJSON、log 走
**結構化訊息**而非 stderr,見下),音訊不走 pipe——子行程直接讀那個
暫存 wav,路徑本來就在磁碟上。

指令:
  {"cmd":"transcribe","wav":"<檔>","model":"fast","progress":true}
      → {"progress":0.13}                     執行中的中間訊息
      → {"log":"轉錄進行中:47/369 塊…","level":"INFO"}
      → {"ok":true,"segments":[[起,訖,文字],…],"device":"intel-gpu"}
  EOF → 退出

⚠️ **log 一定要走結構化訊息回傳,不能只寫 stderr**:黑視窗的心跳訊息是
使用者判斷「程式還活著」的唯一依據(那正是這次事件的核心),而 stderr
上混著 OpenVINO 的 onednn_verbose 洪流——父端若照單全收會把黑視窗洗掉,
只收 debug 又等於心跳消失。故自家 logger 的 INFO 以上另外送一則 log
訊息,父端按原級別重播;stderr 仍照舊 drain 進紀錄檔供除錯。

⚠️ **就緒 ≠ 模型載得起來**(與 diarworker 刻意不同):轉錄有 CUDA →
Intel GPU → CPU 三路降級,而 OV 冷編譯實測要 200 秒上下——在 ready 前
先建模型會讓「按下開始」到「看到第一個進度」多等好幾分鐘,而且根本不知道
該建哪一路。這裡的 ready 只代表「行程起得來、import 沒問題」。
"""
import json
import logging
import sys
import threading

from meeting_scribe import stdio

logger = logging.getLogger("meeting_scribe.transworker")

# stdout 是單一通道:進度、log、回應都走它,而 log 可能來自任何執行緒
# (引擎自己的執行緒也會 log)——不上鎖就會在行中交錯,把 NDJSON 弄壞
_out_lock = threading.Lock()


def _reply(obj: dict) -> None:
    """一行 NDJSON。stdout **只准**出現這個。"""
    with _out_lock:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()


class _ForwardHandler(logging.Handler):
    """把自家 logger 的紀錄轉成 NDJSON 送回父端(見檔頭的 ⚠️)。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            _reply({"log": record.getMessage(), "level": record.levelname})
        except Exception:  # pragma: no cover - log 絕不能反過來弄死轉錄
            pass


def main(argv: list[str] | None = None) -> int:
    # 必須在讀第一行指令之前:中文檔名過 pipe 會變 surrogate(見 stdio)
    stdio.force_utf8()
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    argv = sys.argv[1:] if argv is None else argv
    threads = 0
    if "--threads" in argv:
        threads = int(argv[argv.index("--threads") + 1])

    try:
        from meeting_scribe import power, transcribe

        if threads > 0:
            power.set_worker_count(threads)
        # 自家的 INFO 以上轉發給父端;propagate 留著,stderr 那份進紀錄檔
        own = logging.getLogger("meeting_scribe")
        own.setLevel(logging.INFO)
        own.addHandler(_ForwardHandler())
    except Exception as e:  # pragma: no cover - import 失敗
        _reply({"ready": False, "error": f"{type(e).__name__}: {e}"})
        return 1
    _reply({"ready": True})

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            logger.warning("收到非 JSON 的指令,略過:%.120s", raw)
            continue
        try:
            if msg["cmd"] != "transcribe":
                _reply({"ok": False, "error": f"未知指令:{msg['cmd']}"})
                continue
            on_progress = None
            if msg.get("progress"):
                def on_progress(f: float) -> None:  # noqa: F811
                    _reply({"progress": float(f)})

            segments, device = transcribe.transcribe(
                msg["wav"], model_key=msg.get("model", "fast"),
                progress=on_progress,
            )
            _reply({
                "ok": True, "device": device,
                "segments": [[s.start, s.end, s.text] for s in segments],
            })
        except Exception as e:
            logger.warning("轉錄失敗", exc_info=True)
            _reply({"ok": False, "error": f"{type(e).__name__}: {e}"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
