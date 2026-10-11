"""Apple SpeechTranscriber(zh_TW)轉錄引擎:只在 macOS 26 以上、模型鍵 `apple`。

引擎本體是一支 Swift 小程式(`packaging/mac/apple_asr.swift`,位置由
`plat.apple_speech_helper` 決定),這裡只負責呼叫它、把結果整理成
`TranscriptSegment`。輸出契約與 faster-whisper 那條一致:空句不出、文字是
引擎原文(繁化、跳針標記、標點都在 `pipeline.render_transcript` 做)。

**為什麼要它**(2026-10-10 在 10 核 M5 上用一場 2 小時 14 分的真實會議實測):
44 秒轉完(faster-whisper `fast` 要 1,790 秒)、跑兩次逐位元相同、零漏段、
零跳針;代價是英文術語、單位、數字明顯較差,而且**不吃領域詞表**。數據見
`docs/spec/mac/05` §5.4a。

三件與 faster-whisper 不同、每一件都會咬人的事:

1. ⚠️ **文字先 t2s**:Apple 的 zh_TW 一對多的字選錯(隻有、控製、一緻),
   轉回簡體之後交給 `render_transcript` 既有的 s2tw + replace.txt,結果就與
   whisper 那條同一套(手機版 scribe2md-ios 04 §4.6 / 06 §6.9 實測修正)。
2. ⚠️ **一段結果要依停頓切短**:Apple 一段中位數 14 秒、最長 22 秒,而
   `merge.assign_speakers` 一段只掛**一個**講者——不切的話跨兩個人的那段整段
   給了其中一個。切法見 `_split`;跳針那段整段不切(切碎之後每句都不到
   `loopdetect` 的門檻,判不出來)。
3. **領域詞表不適用**:長篇的 SpeechTranscriber 不吃 contextual strings
   (只有 DictationTranscriber 吃)。詞表有內容時記一行 INFO——實測「資安」
   五次全被寫成「治安」,不交代的話會被當成詞表壞了。
"""

from __future__ import annotations

import json
import logging
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from meeting_scribe import cancel, hotwords, loopdetect, models, plat
from meeting_scribe.errors import UserFacingError
from meeting_scribe.plat import Outcome
from meeting_scribe.types import TranscriptSegment

logger = logging.getLogger(__name__)

ProgressFn = Callable[[float], None]  # 同 transcribe.ProgressFn(那邊會 import 這裡,不反向引用)

# 斷句門檻(2026-10-10 拿 2 小時 14 分的真實會議 31,445 個字掃出來的,見 `_split`):
# ⚠️ **Apple 的逐字時間戳首尾相接、字與字之間沒有空隙**(實測空隙 > 0.05 秒的比例是 0),
# 停頓被併進「停頓之後那個字」的長度裡——所以停頓的訊號是**一個字特別長**,不是空隙。
# 字長中位數 0.18 秒、98% 在 1.02 秒以內。這一組切出來每句中位數 3.6 秒、九成在 5.3 秒
# 以內、最長 8.1 秒(faster-whisper 一段平均約 4.5 秒)
#: 一個字長到這樣,就當它前面有停頓、從它開始新的一句(約每 29 個字一個)
_PAUSE_PIECE_SEC = 0.8
#: 一句長到這裡之後,稍長的字(0.35 秒以上,約每 10 字一個)或標點也斷
_SOFT_MAX_SEC = 4.0
_SOFT_PIECE_SEC = 0.35
#: 再長就不管停頓直接斷(一句只掛一個講者,句子越長賭得越大)
_HARD_MAX_SEC = 8.0
_PUNCT = set("，。？！、；：,.?!;:")

#: 多久沒有任何一行輸出就當它卡死(下載語音資料時有進度,轉錄時每段都有)
_STALL_SEC = 600.0

_capability: Outcome | None = None
#: 「不吃領域詞表」那行一個行程只記一次:現場收音每 150 秒叫一次這裡,兩小時就是 55 行一樣的話
_hotwords_noted = False


def _helper_argv(exe) -> list[str]:
    """叫小程式的指令開頭。抽成一支是給測試換成「python 假小程式」用的
    (假小程式是 .py,在 Windows 上沒有 shebang 可以靠)。"""
    return [str(exe)]


def capability() -> Outcome:
    """這台能不能用 Apple 轉錄;結果留著(問一次要起一個行程)。

    `unsupported`:macOS 太舊、沒有這個功能、不支援繁中——只能換模型。
    `failed`:附的元件找不到或叫不起來——重裝或重開通常能解。
    ⚠️ 繁中語音資料**沒裝不算失敗**:轉錄前會請 macOS 自己下載(使用者
    2026-10-10 選定,算進「首次下載 AI 模型」那一類連網)。"""
    global _capability
    if _capability is None:
        _capability = _probe()
    return _capability


def clear_capability_cache() -> None:
    global _capability
    _capability = None


def _probe() -> Outcome:
    found, exe = plat.apple_speech_helper()
    if not found.ok:
        return found
    try:
        done = subprocess.run([*_helper_argv(exe), "status"], capture_output=True, text=True,
                              encoding="utf-8", timeout=60,
                              **plat.spawn_kwargs(low_priority=False))
        reply = json.loads(done.stdout.strip().splitlines()[-1])
    except Exception as e:  # 起不來、逾時、吐的不是 JSON
        logger.warning("Apple 語音辨識元件叫不起來:%s", e)
        return Outcome("failed", "Apple 語音辨識元件叫不起來,請重新啟動程式再試")
    state = reply.get("state")
    if state == "ok":
        logger.info("Apple 語音辨識可用(%s,語音資料%s)", reply.get("locale"),
                    "已安裝" if reply.get("installed") else "未安裝,第一次轉錄時下載")
        return Outcome("ok")
    return Outcome("unsupported", {
        "unsupported_os": "Apple 的語音辨識需要 macOS 26 以上",
        "unsupported_locale": "這台 Mac 的語音辨識不支援繁體中文",
    }.get(state, "這台 Mac 沒有 Apple 的語音辨識功能"))


def transcribe(wav_path: str | Path, progress: ProgressFn | None = None) -> list[TranscriptSegment]:
    """整檔交給小程式轉;進度 0~1;停止鈕在等輸出的空檔檢查。"""
    can = capability()
    if not can.ok:
        raise UserFacingError(f"無法使用 Apple 轉錄:{can.reason}")
    global _hotwords_noted
    if hotwords.load() and not _hotwords_noted:
        _hotwords_noted = True
        logger.info("Apple 轉錄不吃領域詞表(詞表照常留著,改選「快速」或「精準」時才生效)")
    _, exe = plat.apple_speech_helper()
    results = _run(exe, str(wav_path), progress)
    out: list[TranscriptSegment] = []
    cc = _t2s()
    for r in results:
        for start, end, text in _split(r):
            text = cc.convert(text).strip()
            if text:  # 空字串不輸出(與 transcribe / transcribe_ov 的契約一致)
                out.append(TranscriptSegment(start, end, text))
    return out


def _run(exe, wav: str, progress: ProgressFn | None) -> list[dict]:
    proc = subprocess.Popen([*_helper_argv(exe), "transcribe", wav], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            **plat.spawn_kwargs(low_priority=False))
    lines: queue.Queue = queue.Queue()

    def pump():
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=pump, daemon=True, name="apple-asr-out").start()
    results: list[dict] = []
    error = None
    last = time.monotonic()
    first_download = True
    try:
        while True:
            cancel.check()  # 停止響應點:等小程式輸出的空檔
            try:
                line = lines.get(timeout=0.25)
            except queue.Empty:
                if time.monotonic() - last > _STALL_SEC:
                    raise RuntimeError(f"Apple 語音辨識 {_STALL_SEC:.0f} 秒沒有任何進度")
                continue
            if line is None:
                break
            last = time.monotonic()
            try:
                msg = json.loads(line)
            except ValueError:
                logger.debug("apple-asr:%s", line.rstrip())
                continue
            if "result" in msg:
                results.append(msg["result"])
            elif "progress" in msg:
                if progress:
                    progress(float(msg["progress"]))
            elif "download" in msg:
                frac = float(msg["download"])
                if first_download and frac < 1:
                    logger.info("首次使用 Apple 轉錄,由 macOS 下載繁體中文語音資料")
                first_download = False
                if frac < 1:
                    models.report_progress("首次使用,下載 Apple 繁體中文語音資料", frac)
            elif "error" in msg:
                error = msg["error"]
            elif msg.get("done"):
                logger.info("Apple 轉錄:%d 段結果,耗時 %.1f 秒", len(results), msg.get("seconds", 0))
    except BaseException:
        proc.kill()  # 停止鈕(Cancelled 是 BaseException)與卡死都要收掉小程式
        proc.wait()
        raise
    code = proc.wait()
    if code != 0 or error:
        stderr = proc.stderr.read().strip() if proc.stderr else ""
        raise RuntimeError(f"Apple 語音辨識失敗(結束碼 {code}):{error or stderr[:300]}")
    return results


def _split(result: dict) -> list[tuple[float, float, str]]:
    """一段 Apple 結果 → 幾句短的 (起, 訖, 文字)。見模組 docstring 第 2 點。"""
    pieces = [(float(s), float(e), t) for s, e, t in result.get("pieces", []) if t]
    text = result.get("text", "")
    if not pieces or loopdetect.is_degenerate(text):
        return [(float(result.get("start", 0)), float(result.get("end", 0)), text)]
    groups: list[list[tuple[float, float, str]]] = [[pieces[0]]]
    for p in pieces[1:]:
        cur = groups[-1]
        dur = cur[-1][1] - cur[0][0]
        plen = p[1] - p[0]
        tail = cur[-1][2].strip()[-1:]
        if (plen >= _PAUSE_PIECE_SEC
                or (dur >= _SOFT_MAX_SEC and (plen >= _SOFT_PIECE_SEC or tail in _PUNCT))
                or dur >= _HARD_MAX_SEC):
            groups.append([p])
        else:
            cur.append(p)
    return [(g[0][0], g[-1][1], "".join(t for _, _, t in g)) for g in groups]


_cc = None


def _t2s():
    global _cc
    if _cc is None:
        from opencc import OpenCC  # 惰性:啟動路徑不付辭典成本(同 convert)

        _cc = OpenCC("t2s")
    return _cc
