r"""講者分析子行程:錄音期間的增量切分在這裡跑,不在主行程。

**為什麼一定要獨立行程**(2026-08-03 實測定案,數據見 scripts/probe_gil.py):
sherpa-onnx 的 pybind11 綁定在 C++ 運算期間**沒有釋放 GIL**(pybind11 預設
就不釋放,要明寫 call_guard)。金絲雀實測:空機最大停頓 3.5ms、轉錄
1,450ms、**講者分析 6,834ms 一整塊**。錄音時的災情是收音執行緒整整 5~15 秒
排不上,soundcard 的等待迴圈(也是 Python)醒來後誤判「裝置靜音」而捏造
零幀——一場 90 分鐘的真實會議掉了 4.6 分鐘。

試過並確認無效的:MMCSS 提高收音執行緒優先權(OS 優先權對「等 GIL」沒有
作用,824→905 次)、把 sherpa 從 7 條降到 5 條(GIL 不看執行緒數,824→1640
次更差)。加大 WASAPI 緩衝到 30 秒解決了「掉資料」(824→0 次),但檔案仍
被幻影靜音撐長 25%(726.8 秒的錄音寫出 907.6 秒)——**只有把 GIL 移出
主行程才治得了根**。順帶得到 ocr_worker 那幾條同樣的好處:sherpa 的原生
DLL 不進主行程、崩潰只殺子行程、ORT arena 隨行程歸還。

協定刻意做到最笨(同 ocr_worker):stdin 收 NDJSON 指令、stdout 只准 NDJSON
回應、log 走 stderr。**音訊不走 pipe**——子行程照位移直接讀那個正在被寫入
的軌檔(wavspan),Windows 上用 pipe 傳大 binary 是死結溫床,而且那份資料
本來就在磁碟上。回傳的只有 turns 與聲紋向量(base64 float32),一塊約幾十 KB。

指令:
  {"cmd":"poll","kind":"mic","wav":"<軌檔>","avail":300.0,"progress":false}
      → {"ok":true,"done":285.0}          錄音中逐塊推進
  {"cmd":"finish","kind":"mic","wav":"<軌檔>","total":600.0,"speakers":0}
      → {"ok":true,"turns":[[起,訖,講者],...],"vp":[[講者,base64],...],
         "quality":[[講者,段數,秒數,群內一致],...]}
  EOF → 退出(不必掃孤兒,同 ocr_worker)
"""
import base64
import json
import logging
import sys

import numpy as np

from meeting_scribe import stdio

logger = logging.getLogger("meeting_scribe.diarworker")


def _reply(obj: dict) -> None:
    """一行 NDJSON。stdout **只准**出現這個,其餘一律走 stderr。"""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _encode(vec: np.ndarray) -> str:
    return base64.b64encode(np.asarray(vec, dtype=np.float32).tobytes()).decode()


def _result(turns, vps, quality) -> dict:
    """(turns, 聲紋, 品質)→ 回應。**只有這一份**:diarize 與 finish 兩條
    指令的回應形狀必須一模一樣,各寫一次遲早會有一邊漏欄位,而漏的那邊
    的症狀是「診斷區塊時有時無」——最難查的那種。"""
    return {
        "ok": True,
        "turns": [[t.start, t.end, t.speaker] for t in turns],
        "vp": [[int(k), _encode(v)] for k, v in (vps or {}).items()],
        "quality": [
            [q.speaker, q.segments, q.seconds, q.cohesion]
            for q in (quality or [])
        ],
    }


def main(argv: list[str] | None = None) -> int:
    # 必須在讀第一行指令之前:中文軌檔名過 pipe 會變 surrogate(見 stdio)
    stdio.force_utf8()
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    argv = sys.argv[1:] if argv is None else argv
    threads = 0
    if "--threads" in argv:
        threads = int(argv[argv.index("--threads") + 1])

    try:
        from meeting_scribe import diarize, power, wavspan

        if threads > 0:
            power.set_worker_count(threads)
        # 就緒 = 引擎真的建得起來(模型齊全、DLL 載得動)。缺元件要在按下
        # 「開始錄音」的當下就浮出,不能等第一塊跑完才發現(同 ensure_ready)
        diarize._get_diarizer()
        diarize._get_embedder()
    except Exception as e:
        _reply({"ready": False, "error": f"{type(e).__name__}: {e}"})
        return 1
    _reply({"ready": True})

    states: dict[str, "diarize.IncrementalDiarizer"] = {}

    def state_for(kind: str):
        if kind not in states:
            states[kind] = diarize.IncrementalDiarizer()
        return states[kind]

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
            kind, wav = msg.get("kind", "mic"), msg["wav"]
            read = (lambda a, b, _w=wav: wavspan.read_float(_w, a, b))
            # 進度是「指令執行中」的中間訊息,父端轉給進度條之後繼續等
            # ——一塊要跑數分鐘,不回報的話那幾分鐘進度條會定格。
            # **沒人聽就不要送**(父端以 progress 旗標表態):錄音期間沒有
            # 進度條,而每則訊息都要父行程付兩次 GIL 取得 + 一次 JSON 解析,
            # 那正是這整個子行程設計要騰出來的東西
            on_progress = None
            if msg.get("progress"):
                def on_progress(f: float) -> None:  # noqa: F811
                    _reply({"progress": float(f)})

            if msg["cmd"] == "diarize":
                # 離線整檔一次做完(檔案轉檔用)。⚠️ **不可改用
                # IncrementalDiarizer.finish() 代替**:那支的塊長是錄音用的
                # _LIVE_CHUNK_SEC(5 分鐘),而離線是 _CHUNK_SEC(15 分鐘)
                # ——塊界不同,turn 的擁有權邊界就不同,分群結果會跟著變。
                # 使用者 2026-08-07 才剛確認過離線這條分得出 B 總,不能因為
                # 「搬進子行程」這種與演算法無關的改動而動到結果
                _reply(_result(*diarize.diarize(
                    msg["wav"], num_speakers=int(msg.get("speakers", 0)),
                    progress=on_progress,
                )))
            elif msg["cmd"] == "poll":
                st = state_for(kind)
                st.poll(read, float(msg["avail"]), progress=on_progress)
                _reply({"ok": True, "done": st.done_sec})
            elif msg["cmd"] == "finish":
                _reply(_result(*state_for(kind).finish(
                    read, float(msg["total"]),
                    num_speakers=int(msg.get("speakers", 0)),
                    progress=on_progress,
                )))
            else:
                _reply({"ok": False, "error": f"未知指令:{msg['cmd']}"})
        except Exception as e:
            logger.warning("處理指令失敗", exc_info=True)
            _reply({"ok": False, "error": f"{type(e).__name__}: {e}"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
