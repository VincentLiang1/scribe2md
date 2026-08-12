r"""把標準輸出入釘成 UTF-8。

**這是 Windows 上的必要步驟,不是保險**:PEP 528 之後,連到真終端機的
stdio 本來就是 UTF-8,但**被重新導向、或接進 pipe 時會退回地區設定的
編碼**(台灣機器是 cp950)。踩過的兩次:

- `doc2md 資料夾 > out.txt` 得到 cp950 的 `\xa6W\xb3\xe6.md`,任何按
  UTF-8 讀的呼叫端都拿到亂碼;檔名有 cp950 表達不了的字時更會直接
  UnicodeEncodeError,整批轉完卻在最後印路徑時掛掉。
- 講者分析子行程收到的軌檔路徑 `錄音_20260803_1006.wav` 變成一串
  surrogate、FileNotFoundError,整場錄音的增量切分在第 30 秒靜靜停擺
  ——而收音那邊一切正常,量出來的數字乾淨得像是修好了。

**為什麼獨立成一個 leaf 模組**:同一件事原本在 7 個地方各寫一次(兩支
CLI、兩支子行程、三支腳本),而且**已經分岔**——「該釘哪幾條串流」有三種
答案,`ocr_worker` 漏了 stderr,於是它的繁中 log 經父端以 UTF-8 解碼後
變成替代字元,而那些行現在會落地進紀錄檔(filelog),紀錄檔存在的唯一
理由就是事後分析。這個模組**不 import 任何自家模組**,子行程與腳本才
能無代價地用它(子行程刻意保持輕量,不該為兩行程式碼拖進整條相依鏈)。
"""
import logging
import sys

logger = logging.getLogger(__name__)


def force_utf8(*streams) -> None:
    """把給定的串流釘成 UTF-8;沒給就釘 stdin/stdout/stderr 三條。

    釘不動只記 debug 不拋:呼叫端多半是「印東西之前」,不該因為調不動
    編碼就跑不起來(測試換上的假串流也沒有 reconfigure)。"""
    for stream in (streams or (sys.stdin, sys.stdout, sys.stderr)):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            logger.debug("無法把輸出串流改成 UTF-8", exc_info=True)
