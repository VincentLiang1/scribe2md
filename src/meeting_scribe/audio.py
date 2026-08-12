"""ffmpeg 音訊處理與共用的 wav 讀取。

- to_wav16k:任何支援格式(m4a/mp3/wav/mp4/mov/avi)→ 16k 單聲道 wav,
  管線第一步,後續引擎都吃這個規格
- cut_clip:從「原始檔」剪講者試聽片段(-ss 放 -i 前,長錄音免全檔解碼)
- merge_stereo:雙軌錄音合成立體聲成品(左=麥克風、右=系統聲音)
- read_wav16k:16k wav → float32 樣本(transcribe_ov/diarize 共用)

ffmpeg 本體由 static_ffmpeg 提供,首次呼叫自動下載(約 50MB);三個
轉檔函式共用 _run_ffmpeg(stderr 進 log、對使用者只拋繁中訊息)。
"""
import logging
import subprocess
import threading
import wave
import weakref
from pathlib import Path

import numpy as np
from static_ffmpeg import run

from meeting_scribe.errors import UserFacingError

logger = logging.getLogger(__name__)


def ffmpeg_path() -> str:
    # 首次呼叫會下載平台版 ffmpeg 到 static_ffmpeg 套件目錄(約 50MB)
    exe, _probe = run.get_or_fetch_platform_executables_else_raise()
    return exe


# 同一檔案的解碼結果以「單槽弱參照」共享:OV(Intel GPU)路徑下轉錄與
# 講者分析平行、幾乎同時各讀同一個 16k wav——各讀各的等於 4 小時錄音兩份
# ~0.9GB float32 同時常駐(8GB 基準機吃緊)、磁碟白讀一趟。弱參照讓兩引擎
# 共用同一份陣列,且雙方用完即自動釋放(不佔到下一檔);鎖住整段
# 「查快取→讀檔→存快取」,晚到的引擎稍等先到的讀完直接共用,不會兩邊
# 同時開讀而各自落地一份。呼叫端把回傳陣列當唯讀(現有呼叫端皆只切片)。
_READ_LOCK = threading.Lock()
_read_cache: tuple[tuple[str, int, int], "weakref.ReferenceType"] | None = None


def read_wav16k(path: str | Path) -> np.ndarray:
    """讀 16kHz 單聲道 16-bit wav 為 float32(-1~1)。

    轉錄(transcribe_ov)與講者分析(diarize)引擎共用;上游必先經
    to_wav16k 或 record 落地,格式不符是內部流程錯誤。平行讀同一檔時
    共享同一份陣列(見 _read_cache 註解),回傳值視為唯讀。"""
    global _read_cache
    st = Path(path).stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    with _READ_LOCK:
        if _read_cache is not None and _read_cache[0] == key:
            cached = _read_cache[1]()
            if cached is not None:
                return cached
        with wave.open(str(path), "rb") as w:
            if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
                raise ValueError("需要 16kHz 單聲道 16-bit 音檔(內部流程錯誤)")
            raw = w.readframes(w.getnframes())
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        _read_cache = (key, weakref.ref(samples))
        return samples


def _run_ffmpeg(cmd: list[str], dest: Path, log_msg: str, user_msg: str) -> Path:
    """跑 ffmpeg 並確認成品落地;失敗把完整 stderr 記進 log(黑視窗診斷用),
    對使用者只拋繁中訊息(spec §8)。"""
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not dest.exists():
        logger.error("%s:%s", log_msg, proc.stderr)
        raise UserFacingError(user_msg)
    return dest


# 試聽片段:尾端補一點緩衝(轉錄的結束時間戳常提早收,句尾會被切掉半個字);
# 長度上限防極端長段落(試聽認人用不到 30 秒以上,檔案也不必大)
_CLIP_TAIL_PAD = 0.3
_CLIP_MAX_SECONDS = 30.0


def cut_clip(src: Path, dest: Path, start: float, end: float) -> Path:
    """從原始音/影檔剪出 [start, end] 秒的試聽片段(16k 單聲道 wav)。

    -ss 放在 -i 之前:先粗跳再解碼,長錄音剪十幾秒不用從頭讀整檔
    (放 -i 之後是精確但全檔解碼,兩小時錄音要等數十秒)。"""
    duration = min(max(end - start, 0.1) + _CLIP_TAIL_PAD, _CLIP_MAX_SECONDS)
    cmd = [
        ffmpeg_path(), "-y", "-ss", f"{max(start, 0.0):.3f}", "-i", str(src),
        "-t", f"{duration:.3f}",
        "-vn", "-ac", "1", "-ar", "16000", "-sample_fmt", "s16",
        str(dest),
    ]
    return _run_ffmpeg(
        cmd, dest, f"ffmpeg 剪試聽片段失敗({src.name})",
        f"試聽片段剪輯失敗({src.name})",
    )


def _wav_seconds(path: Path) -> float:
    """wav 的長度(秒;讀標頭,不讀資料)。讀不到就回 0,交給呼叫端決定。"""
    try:
        with wave.open(str(path)) as w:
            return w.getnframes() / (w.getframerate() or 16000)
    except Exception:  # noqa: BLE001 - 合成不該因為量長度失敗而整個放棄
        logger.debug("讀不到 wav 長度:%s", path, exc_info=True)
        return 0.0


def merge_stereo(left: Path, right: Path, dest: Path) -> Path:
    """兩個 16k 單聲道 wav 合成雙聲道(左=現場麥克風、右=系統聲音)。

    線上會議的成品音檔:分軌錄音(見 record.py)在收尾合成一檔,回聽時
    左右耳可分辨聲音來源。

    **兩軌不等長時要補短的,不能讓 join 自己收尾**(2026-08-04):ffmpeg 的
    `join` 是**取最短的那軌**(實測 10.0 秒 + 9.5 秒 → 9.5 秒),長的那軌
    尾巴會被安靜切掉。錄音路徑會盡量把兩軌都對齊牆鐘(補零/修剪,見
    `record._DRIFT_PAD_SEC` 與 `_DRIFT_TRIM_SEC`),但那是「盡量」——修剪
    拿不到足夠的零樣本時就會留下差額,而在此之前,一軌被捏造的靜音撐長
    的情形下差額曾經高達數十秒。**錄音不能重來,不接受這種安靜的截斷**,
    所以這裡以較長的那軌為準、短的補靜音(`apad` 會無限補,一定要用 `-t`
    收在正確長度)。"""
    longest = max(_wav_seconds(left), _wav_seconds(right))
    cmd = [
        ffmpeg_path(), "-y", "-i", str(left), "-i", str(right),
        "-filter_complex",
        "[0:a]apad[l];[1:a]apad[r];[l][r]join=inputs=2:channel_layout=stereo[a]",
        "-map", "[a]", "-sample_fmt", "s16",
    ]
    # 量不到長度就不下 -t:apad 會無限補,但沒有 -t 的話 ffmpeg 對
    # 「輸入都結束了」仍會收尾——退回舊行為(取最短)也好過產出一個
    # 永遠不結束的檔案
    if longest:
        cmd += ["-t", f"{longest:.6f}"]
    cmd.append(str(dest))
    return _run_ffmpeg(cmd, dest, "ffmpeg 合成雙聲道失敗", "錄音檔合成失敗(雙聲道)")


def to_wav16k(src: Path, out_dir: Path) -> Path:
    """來源音檔/影片 → 16k 單聲道 16-bit wav(管線第一步;-vn 丟棄影像軌)。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{src.stem}_16k.wav"
    cmd = [
        ffmpeg_path(), "-y", "-i", str(src),
        "-vn", "-ac", "1", "-ar", "16000", "-sample_fmt", "s16",
        str(dest),
    ]
    return _run_ffmpeg(
        cmd, dest, f"ffmpeg 轉檔失敗({src.name})",
        f"音訊轉檔失敗({src.name}):請確認檔案未損壞且為支援的音訊/影片格式",
    )
