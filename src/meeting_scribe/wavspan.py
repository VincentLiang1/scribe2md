r"""讀取「還在被寫入」的 WAV 檔:按位移直接讀原始資料,不經 wave 模組。

錄音中的軌檔標頭每 _HEADER_FLUSH_SEC 才回寫一次(見 record._WavWriter),
落後於實際資料——wave 模組信標頭,會拒讀尾端那一段。可讀長度因此也要
從**檔案大小**推算,不能問標頭。

獨立成模組的理由:live.py(增量轉錄排程)與 diarworker.py(講者分析子
行程)各自需要它,而 diarworker 是刻意保持輕量的子行程,不該為了兩個
十行函式去 import live→pipeline 那一整串。契約只有一份,兩邊才不會在
「標頭幾個位元組」這種事情上分岔。
"""
from pathlib import Path

import numpy as np

RATE = 16000
# record._WavWriter 寫的固定標頭長度(RIFF 44 bytes)
HEADER_BYTES = 44


def available_seconds(path: Path | str) -> float:
    """目前可安全讀取的秒數(以檔案大小計,不靠標頭)。"""
    try:
        return max(0, (Path(path).stat().st_size - HEADER_BYTES) // 2) / RATE
    except OSError:
        return 0.0


def read_span(path: Path | str, start: float, end: float) -> np.ndarray:
    """讀 [start, end) 秒的 PCM(int16)。"""
    i0 = int(start * RATE)
    n = max(0, int(end * RATE) - i0)
    with open(path, "rb") as f:
        f.seek(HEADER_BYTES + i0 * 2)
        raw = f.read(n * 2)
    return np.frombuffer(raw, dtype=np.int16)


def read_float(path: Path | str, start: float, end: float) -> np.ndarray:
    """同 read_span,但回傳講者分析要的 float32 [-1, 1)。"""
    return read_span(path, start, end).astype(np.float32) / 32768.0
