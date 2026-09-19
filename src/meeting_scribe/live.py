"""邊錄邊轉:錄音期間背景增量轉錄;收尾合併兩軌+回音文字去重。

痛點(使用者 2026-07-21 提出):4 小時會議錄完才開始轉,會後還要再等
一大輪。解法:錄音中每累積 _CHUNK_SEC 就把「已完成的音訊區間」切給
轉錄引擎,切點挑區間尾端 _SEARCH_SEC 窗內 RMS 最低的 _RMS_WIN_SEC 視窗
中點(不切在句子中間;技巧同 transcribe_ov A 層的「最安靜處切半」);
最低視窗仍不夠安靜(連續發言沒有空隙)就延後再切,最多延
_CUT_DEFER_MAX_SEC 後強制切(使用者指定 2026-07-23);
停止錄音後只剩最後一段轉錄+講者分析+輸出。

執行緒模型:單一排程執行緒輪詢所有軌——轉錄引擎(模型快取)不可併用,
兩軌依序轉;段落在鎖內累積,UI 週期性 snapshot() 做即時預覽。引擎失敗
「不推進 done」(下一輪或收尾重試),絕不安靜跳過一段音訊。錄音中的
轉錄不掛 gr.Progress(背景執行緒無 gradio context),收尾才回報進度。

講者分離也增量(LiveDiarizer,2026-07-29):切分+抽聲紋是塊內獨立的,
錄音中就逐塊做掉,停止後只剩尾巴與全域重聚(重聚要看全場,但 18 分鐘
會議實測只要 0.3 秒)——講者分析是全流程最慢的一段(標準機 RTF
0.24~0.77),過去整段壓在散會後。只在轉錄走 GPU 時啟用:純 CPU 機上兩者同搶 CPU,
即時轉錄會落後,收尾反而更久(排程理由同 pipeline._transcribe_and_diarize)。
兩軌「不必」跨軌聚類——現場的人只出現在麥克風軌、遠端的人只出現在
系統軌,回音段落由文字去重丟掉,各軌獨立編號再合併重排即可。

回音去重(線上會議,喇叭外放):遠端聲音從喇叭出來又被麥克風收一次,
兩軌會轉出幾乎相同的文字。合併時丟棄麥克風軌中「時間重疊(±_ECHO_TOL_SEC
容差,涵蓋聲學延遲與兩軌時脈漂移)且文字近似(SequenceMatcher ≥ _ECHO_SIM
或正規化後互為子串)」於系統軌的段落——不倚賴訊號層 AEC 的濾波效果,
逐字稿保證無重複句(訊號層 AEC 屬後續優化)。
"""
import bisect
import contextvars
import difflib
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import wave
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from itertools import accumulate
from pathlib import Path

import numpy as np

from meeting_scribe import (
    audio, cancel, diarize, diarproc, merge, pipeline, power, punctuate,
    transcribe, wavspan,
)
from meeting_scribe.types import UNKNOWN_SPEAKER, SpeakerTurn, SpokenSegment, TranscriptSegment

logger = logging.getLogger(__name__)

_RATE = wavspan.RATE

# 每累積這麼多秒就轉一段:太短=引擎冷啟動/30 秒窗開銷佔比高(Whisper
# 以 30 秒窗運算,段長至少要攤得開 4~6 個窗),太長=收尾殘留的「最後
# 一段」變長、邊錄邊轉的攤平效果變差。原 600,使用者 2026-07-23 為了
# 預覽更即時逐步縮短(600→300→180),2026-07-24 拍板回到 300。
#
# ⚠️ **2026-09-18 起試用 150**(使用者裁定,起因是「散會後要等一下才能設定
# 講者」)。它與 `diarize._LIVE_CHUNK_SEC` 是**一組的**,收尾等的是兩者中較慢
# 的那個,只縮一顆幾乎省不到時間(生產記錄:轉錄尾巴 17~29 秒,每一場都比
# 整段收尾 33~37 秒先結束)。實測與代價見 `scripts/bench_live_tail.py` 檔頭,
# 摘要:尾巴期望值減半;轉錄本身與長度成正比、切短不多付也不省
# (150 秒音訊 25~32 秒、300 秒 57~62 秒);收音安全過關(20 分鐘、爆發次數
# 加倍,除了第一次載入模型那一次之外零停擺零不連續);⚠️ **代價是分群**
# ——五場真實會議一致地略差(見 _LIVE_CHUNK_SEC 那一條)。
# ⚠️ **要改回去就是把這顆與 `diarize._LIVE_CHUNK_SEC` 一起改回 300**,
# 其餘什麼都不必動(兩顆都只是門檻,沒有人依賴它們的具體數值)。
_CHUNK_SEC = 150.0
# 第一段提早轉(每軌 done=0 時適用):即時預覽的第一批文字是使用者確認
# 「收音正常」的訊號,等滿一整個標準段才出現太久(使用者實際問了
# 「多久會出來」);1 分鐘 + 該段轉錄時間,兩三分鐘內就看得到字。
# 原 120,使用者 2026-08-11 指定縮短為 60:1 分鐘只攤得開 2 個 30 秒窗、
# 固定成本(音檔解碼+全檔 VAD 每段各付一次)佔比明顯高於標準段,但
# **它整場只發生一次**,換到的是「收音正常」的確認提早一分鐘——這正是
# 只動這一顆、不動 _CHUNK_SEC 的理由:標準段調短會讓那份成本乘上整場的
# 段數,而錄音中的轉錄跑在主行程(GIL 停頓實測 1,450ms,見
# scripts/probe_gil.py),爆發變頻繁會反過來威脅收音本身
_FIRST_CHUNK_SEC = 60.0
# 在段尾這一窗內找最安靜切點;RMS 視窗 0.5 秒(語句間隙的典型尺度)
_SEARCH_SEC = 30.0
_RMS_WIN_SEC = 0.5
# 「夠安靜」門檻(使用者指定 2026-07-23:找不到安靜點寧可晚點切,
# 不硬切在句子中間)。兩條件其一成立即算安靜:絕對門檻抓數位靜音與
# 低室噪(int16 語音典型 RMS 1000~8000,300 已遠低於說話聲);相對門檻
# 抓「比周圍明顯安靜的空隙」——整窗都安靜時中位數≈最低值、相對門檻
# 必失效,靠絕對門檻兜住
_QUIET_RMS_ABS = 300.0
_QUIET_RATIO = 0.3
# 延後切點的上限:連續發言一直沒有空隙時,最多再多等這麼久,之後在
# 「目前最安靜處」強制切(安全網:整段音訊毫無空隙時,預覽不能永遠
# 不出現)——原本是 5+5=10 分鐘,與最初的 10 分鐘節奏相當;_CHUNK_SEC
# 2026-09-18 改成 150 之後最壞是 2.5+5=7.5 分鐘。⚠️ **這顆刻意不跟著減半**:
# 它管的是「一直沒有空隙可切」,而那是人講話的事、與節奏無關;調小只會
# 讓更多段落被硬切在句子中間(使用者 2026-07-23 指定寧可晚切)
_CUT_DEFER_MAX_SEC = 300.0
# 不追到寫入端屁股後面(writer 每 2 秒回寫標頭,檔案尾端可能還在長)
_TAIL_MARGIN_SEC = 2.0
_POLL_SEC = 1.0
# 增量講者切分的輪詢間隔:一塊要 diarize._LIVE_CHUNK_SEC(2.5 分鐘)音訊
# 才滿,秒級輪詢毫無意義(每次還要 stat 檔案)。⚠️ 塊長減半之後這顆就是
# 尾巴的下限:最壞情況「剛滿就停止」還要等這 30 秒才會被撿走
_DIAR_POLL_SEC = 30.0
# 引擎失敗後的重試間隔:立刻重試多半再失敗(如模型下載中斷),白燒 CPU
_RETRY_SEC = 60.0

# 回音去重參數:容差涵蓋「喇叭→麥克風的聲學延遲(毫秒級)+ 兩軌時脈
# 漂移(4 小時零點幾秒)+ 兩引擎斷句時間戳差(秒級,主要項)」
_ECHO_TOL_SEC = 2.0
_ECHO_SIM = 0.66
# 太短的正規化文字(1~3 字的「好」「對對對」)相似度沒有鑑別力,
# 只在時間重疊時以子串規則處理
_ECHO_MIN_CHARS = 4


# 「讀取還在被寫入的 wav」只有一份實作(wavspan):講者分析子行程也要用,
# 而它是刻意保持輕量的行程、不該為此 import 本模組整串相依
_available_seconds = wavspan.available_seconds
_read_span = wavspan.read_span


def _choose_cut(
    path: Path, lo: float, hi: float,
    *, search_lo: float | None = None, require_quiet: bool = False,
) -> float | None:
    """在 (lo, hi] 內挑切點:搜尋窗(預設 hi 往前 _SEARCH_SEC;呼叫端
    可用 search_lo 放寬——延後切點時窗要跟著已累積的音訊變寬)內 RMS
    最低的 _RMS_WIN_SEC 視窗中點。require_quiet=True 時,最低視窗仍不夠
    安靜(_QUIET_* 門檻,見常數註解)就回 None 讓呼叫端延後再切;
    False 時(收尾與強制切)一律回切點,讀檔失敗/窗太短回 hi(=改版前
    行為)。"""
    if search_lo is None:
        search_lo = hi - _SEARCH_SEC
    search_lo = max(lo, search_lo)
    try:
        pcm = _read_span(path, search_lo, hi).astype(np.float64)
    except OSError:
        return None if require_quiet else hi
    win = int(_RMS_WIN_SEC * _RATE)
    if len(pcm) < 2 * win:
        return None if require_quiet else hi
    n_win = len(pcm) // win
    rms = np.sqrt(
        (pcm[: n_win * win].reshape(n_win, win) ** 2).mean(axis=1)
    )
    best = int(np.argmin(rms))
    if require_quiet:
        threshold = max(_QUIET_RMS_ABS, _QUIET_RATIO * float(np.median(rms)))
        if rms[best] > threshold:
            return None
    return search_lo + (best + 0.5) * _RMS_WIN_SEC


def _next_cut(path: Path, done: float, avail: float) -> float | None:
    """這一輪要不要切、切在哪:None=先不切(門檻未到,或過了段界但找不到
    夠安靜的切點、仍在延後限度內)。所有段(含第一段,使用者指定
    2026-07-23:第一段也不硬切)一律自段界起找「夠安靜」的切點,找不到
    就先不切、下一輪帶著更多音訊再找(搜尋窗隨 avail 變寬);連續發言
    最多延 _CUT_DEFER_MAX_SEC,之後在目前最安靜處強制切(安全網:
    整段音訊毫無空隙時,預覽不能永遠不出現)。第一段與後續段的差別只在
    門檻長短(_FIRST_CHUNK_SEC 較短,第一批文字=收音正常的確認訊號)。"""
    threshold = _FIRST_CHUNK_SEC if done == 0 else _CHUNK_SEC
    if avail - done < threshold:
        return None
    boundary = done + threshold
    cut = _choose_cut(
        path, done, avail,
        search_lo=boundary - _SEARCH_SEC, require_quiet=True,
    )
    if cut is None and avail - done >= threshold + _CUT_DEFER_MAX_SEC:
        cut = _choose_cut(path, done, avail, search_lo=boundary - _SEARCH_SEC)
    return cut


@dataclass
class _Track:
    key: str  # "mic" | "system"
    path: Path
    done: float = 0.0  # 已轉錄至(秒)
    segments: list[TranscriptSegment] = field(default_factory=list)
    next_attempt: float = 0.0  # 引擎失敗後的重試時點(monotonic)


class LiveDiarizer:
    """錄音期間的增量講者切分(理由與啟用條件見模組 docstring)。

    自己一條執行緒:一塊音訊要跑數分鐘,擠在轉錄排程執行緒上會讓即時
    預覽停擺。停止錄音時 stop() 先等進行中的那塊跑完——sherpa 引擎不可
    兩條執行緒併用,且那塊的工作不該白費(塊長 _LIVE_CHUNK_SEC 就是為了
    把這段死等壓短,見該常數註解)。

    任何失敗都只是「沒提前做」:狀態不推進,收尾照樣算得出來(最壞退回
    等同離線行為),絕不因此影響錄音本身。

    追得上嗎:單軌 RTF 0.57~0.77(標準機實測),錄音中綽綽有餘;線上會議
    雙軌合計 >1,追不上、會積壓,收尾仍有殘量要算——但那也已經是把
    三分之二的工作挪到會議期間,遠優於全部壓在散會後。積壓時 stop() 等
    進行中那一次 poll 回來(子行程一次會啃完所有已備妥的塊);真要立刻
    收手是 close(),它直接把子行程殺掉。"""

    def __init__(self, tracks: dict[str, Path]):
        self._paths = dict(tracks)
        self._proc: diarproc.DiarProcess | None = None
        self._done: dict[str, float] = dict.fromkeys(tracks, 0.0)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def active(self) -> bool:
        """背景切分是否還在做。**推導自「子行程還在不在」**,不另存旗標:
        兩處各記一次的話,放棄那條路很容易只更新其中一個,而症狀是
        finish() 對著一支已經放棄的子行程要結果。"""
        return self._proc is not None

    @property
    def done_sec(self) -> dict[str, float]:
        """各軌「錄音中已經切分到第幾秒」的快照。

        給收尾那段報「還欠多少」用(見 run_live_finish)——增量追不追得上
        完全看機器,而那個差額就是散會後還要等多久。"""
        return dict(self._done)

    def start(self) -> None:
        """轉錄走 GPU 才啟用(純 CPU 機兩者同搶 CPU,見模組 docstring);
        不啟用時安靜跳過,收尾一律照舊。

        啟用與否記進記錄檔:兩種情形的 CPU 使用量差一個量級,而使用者
        看得到的只有「散會後等多久」——分析收音掉幀或收尾耗時的時候,
        第一件要確認的就是這條到底有沒有在跑(2026-08-03 加)。"""
        if not self._paths:
            return
        if not transcribe.gpu_available():
            logger.info(
                "錄音中的講者切分未啟用(轉錄未走 GPU),講者分析全部留到停止後"
            )
            return
        self._proc = diarproc.DiarProcess()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="live-diarize",
        )
        self._thread.start()

    def _give_up(self) -> None:
        """放棄增量切分:把子行程收掉。留著只是白佔兩顆 sherpa 模型的
        記憶體與執行緒到散會為止,而 finish() 已經會退回離線路徑。"""
        proc, self._proc = self._proc, None
        if proc is not None:
            proc.close()

    def _loop(self) -> None:
        # spawn + 等就緒要載 sherpa DLL 與兩顆模型(實測熱機 1.3~1.7 秒,
        # 冷開機更久),**刻意不放在 start() 裡**:那條路跑在 gradio 的
        # 「開始錄音」事件上,擋住的是按鈕回饋(雙鈕狀態、計時器、狀態列),
        # 使用者按下去只會看到一秒多什麼都不動。而這段等待換不到東西——
        # 啟動失敗本來就只記一行 warning、使用者看不到
        proc = self._proc
        try:
            proc.start()
        except cancel.Cancelled:
            # ⚠️ **取消不是啟動失敗**(2026-09-19 review 補,與下面 poll 那條
            # 是同一件事的另一半):`Cancelled` 繼承 BaseException,下面那個
            # `except Exception` **攔不到它**,於是它會帶著整段英文 traceback
            # 從執行緒冒出去——`threading.excepthook` 印到 stderr,而
            # `filelog.tee_console` 把 stderr 整條提級成 WARNING 寫進記錄檔
            # (`filelog._LineTee` 的 ⚠️),長得跟真的壞掉一模一樣。而且
            # `_give_up()` 沒跑的話 `self._proc` 留著非 None,`active` 整場
            # 回報 True、收尾會對著一支從來沒就緒的 worker 要結果。
            # ⚠️ **這個窗口是真的**:`DiarProcess.start()` 在等 worker 就緒
            # (熱機 1.3~1.7 秒,冷開機更久)期間 `_await` 每圈都
            # `cancel.check()`,而 `desktop._shutdown()` 無條件
            # `cancel.request()`——「開始錄音後很快關視窗」就會踩到
            logger.info("使用者取消,錄音中的講者切分未啟用")
            self._give_up()
            return
        except Exception:
            # 起不來就退回「收尾再算」:結果完全正確,只是散會後要多等
            logger.warning("講者分析子行程啟動失敗,改由停止後一次處理", exc_info=True)
            self._give_up()
            return
        logger.info("錄音中的講者切分已啟用(%d 軌,子行程)", len(self._paths))
        while not self._stop.wait(_DIAR_POLL_SEC):
            for kind, path in self._paths.items():
                if self._stop.is_set() or self._proc is None:
                    break
                try:
                    before = self._done[kind]
                    t0 = time.monotonic()
                    done = self._proc.poll(
                        kind, path, _available_seconds(path) - _TAIL_MARGIN_SEC)
                    self._done[kind] = done
                    # 只在真的推進時記(沒滿一塊的輪詢會秒回,每 30 秒印一次
                    # 是雜訊)。耗時/音訊長度就是這台機器的 RTF,收尾要等多久
                    # 由它決定
                    if done > before:
                        logger.info(
                            "錄音中的講者切分(%s):已完成至 %.0f 秒"
                            "(本輪 %.0f 秒音訊,耗時 %.1f 秒)",
                            kind, done, done - before, time.monotonic() - t0,
                        )
                except cancel.Cancelled:
                    # ⚠️ **使用者按取消不是失敗**(2026-09-18 讀執行記錄時
                    # 發現):下面那個 `self._stop` 只代表「錄音停了」,而
                    # 取消是 `cancel.py` 的**全域**旗標——在收尾途中按取消
                    # 時 _stop 根本還沒設,於是一次完全正常的取消在記錄檔裡
                    # 留下「切分失敗」加一整段 traceback,長得跟真的壞掉
                    # 一模一樣(實測 2026-09-16 那場:警告的下一行就是
                    # 「收尾沒做完,已保住 1/1 條音軌」)
                    # ⚠️ **不記成失敗不等於不記**(2026-09-19 review 補):
                    # 「這條到底有沒有在跑」是分析收尾耗時的第一個問題
                    # (見 start() 的 docstring),整場只留「已啟用」而沒有
                    # 結束的那一行就答不出來了——而 `desktop._rec_start` 的
                    # docstring 至今還寫著這個症狀「只在記錄檔裡留一行」
                    logger.info("使用者取消,錄音中的講者切分就此停止")
                    # ⚠️ **`_stop` 已設時不可以收子行程**(2026-09-19 review
                    # 補;舊版 `if self._stop.is_set(): return` 保護的正是
                    # 這件事):那代表 `stop()` 正在 join,而它之後的
                    # `finish()` 還要靠這支子行程。收掉的話 `finish()` 會退回
                    # 主行程的 `diarize.diarize()`,而那要先載兩顆 sherpa 模型
                    # 再把整份 wav 讀進記憶體,**之後**才碰得到第一個取消檢查
                    # 點(`diarize._segment_and_embed`)——使用者按了取消反而
                    # 要多等一整趟冷載入。收子行程的事交給 close()
                    if not self._stop.is_set():
                        self._give_up()
                    return
                except Exception:
                    if self._stop.is_set():
                        # 收工時是我們自己把子行程殺掉的(close),不是失敗。
                        # 照樣示警的話每一場正常錄音都會留下一行「切分失敗」,
                        # 真出事那次就淹沒在裡面了
                        return
                    # 提前做只是為了省收尾時間:失敗就退回「收尾再算」,
                    # 但不再重試(多半是模型/裝置問題,重試只是空轉 CPU)。
                    # 訊息要明講「本場不再重試」——這條執行緒就此結束,
                    # 之後整場錄音都沒有增量切分,而畫面上看不出任何差別
                    logger.warning(
                        "錄音中的講者切分失敗(%s),本場錄音不再重試,"
                        "改由停止後一次處理", kind,
                        exc_info=True,
                    )
                    self._give_up()  # finish 隨即退回離線路徑
                    return

    def stop(self, progress: Callable[[float], None] | None = None) -> None:
        """停止背景切分,並等進行中的那塊跑完(冪等)。

        progress:等待期間的完成度(由子行程回報,見 diarproc)——不掛的話
        進度條會在這幾分鐘定格。

        ⚠️ **`self._proc` 一律先綁成區域變數再用**(2026-09-19 review 補):
        切分執行緒會在使用者按取消時把它設成 None(`_give_up`),而這支跑在
        收尾那條執行緒上——`if self._proc is not None:` 與下一行的屬性寫入
        之間只差兩個 bytecode,落在那個窗口就是
        `AttributeError: 'NoneType' object has no attribute 'on_progress'`,
        而它不是 `UserFacingError`,使用者錄完一整場會議只會看到「未預期的
        錯誤」。`finish()` 的 `finally` 同一個形狀,而且那裡還會把正在傳播的
        例外整個蓋掉。"""
        proc = self._proc
        if proc is not None:
            proc.on_progress = progress
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        proc = self._proc
        if proc is not None:
            proc.on_progress = None

    def finish(
        self,
        kind: str,
        path: Path,
        total_sec: float,
        num_speakers: int = 0,
        progress: Callable[[float], None] | None = None,
        features_out: Path | None = None,
    ) -> tuple[list[SpeakerTurn], dict, list]:
        """該軌的講者分析結果(補完剩餘的塊 + 全域重聚 + 分群品質)。

        沒啟用增量時直接走離線 diarize:塊長不同(_LIVE_CHUNK_SEC 較短、
        重疊佔比較高),不該讓純 CPU 機為了共用路徑多付一成切分成本。
        子行程中途死掉也走這條——結果完全正確,只是白做了前面那些塊。

        ⚠️ **`self._proc` 綁成區域變數**(理由同 `stop()` 的 ⚠️):這裡的
        `finally` 更兇——`self._proc` 被切分執行緒清掉的話,`AttributeError`
        會在 `finally` 裡炸開,把正在往上傳的那個例外(含使用者按的取消)
        整個蓋掉。"""
        proc = self._proc
        if proc is None or kind not in self._paths:
            # ⚠️ **退回離線路徑之前先看取消旗標**(2026-09-19 review 補):
            # `diarize.diarize()` 會先 `_get_diarizer()`(把兩顆 sherpa 模型
            # 載進**主行程**)再 `read_wav16k()`(整份 wav 進記憶體),而第一個
            # `cancel.check()` 要到 `_segment_and_embed` 裡才碰得到——使用者
            # 按了取消卻要先等這一整趟冷載入,正好與「子行程是硬中斷點」相反
            cancel.check()
            return diarize.diarize(
                path, num_speakers=num_speakers, progress=progress,
                features_out=features_out)
        try:
            proc.on_progress = progress
            return proc.finish(
                kind, path, total_sec, num_speakers, features_out=features_out)
        except Exception:
            logger.warning("講者分析子行程收尾失敗,改在主行程重算", exc_info=True)
            return diarize.diarize(
                path, num_speakers=num_speakers, progress=progress,
                features_out=features_out)
        finally:
            proc.on_progress = None

    def close(self) -> None:
        """關閉會話:直接把子行程收掉,不等進行中的那塊跑完。

        **一定要真的收掉**:留一支在跑的子行程,下一場錄音會與它併用同一批
        模型檔、也白佔 CPU。子行程是硬中斷點——不必像從前那樣靠協作式的
        callback 才停得下來。"""
        self._stop.set()
        self._give_up()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None


class LiveTranscriber:
    """增量轉錄排程器(另含增量講者切分,見 diar/LiveDiarizer)。用法:
    add_track()* → start() → (錄音中自動轉)
    → stop_and_flush(final_lengths, progress) → 各軌完整段落。"""

    def __init__(self, model_key: str = "fast"):
        self.model_key = model_key
        # 增量講者切分:軌要等 add_track 進來,start() 才成形
        self.diar = LiveDiarizer({})
        self._tracks: dict[str, _Track] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # 暫存目錄沿用 pipeline 的「前綴+存活鎖」:持開鎖檔擋住另一實例
        # 啟動時的 cleanup_stale_temp(前綴相同、沒鎖就會被當孤兒掃掉);
        # 硬退出的孤兒同樣由下次啟動清
        self._tmp = tempfile.TemporaryDirectory(prefix=pipeline.TMP_PREFIX + "live-")
        self._tmp_lock = open(Path(self._tmp.name) / pipeline.TMP_LOCK, "wb")

    def add_track(self, key: str, path: Path) -> None:
        self._tracks[key] = _Track(key, path)

    def start(self) -> None:
        self.diar = LiveDiarizer({k: t.path for k, t in self._tracks.items()})
        self.diar.start()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    # -- 錄音中 --------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.wait(_POLL_SEC):
            for tr in self._tracks.values():
                if self._stop.is_set():
                    break
                avail = _available_seconds(tr.path) - _TAIL_MARGIN_SEC
                if time.monotonic() < tr.next_attempt:
                    continue
                cut = _next_cut(tr.path, tr.done, avail)
                if cut is None:
                    continue
                try:
                    self._transcribe_span(tr, tr.done, cut)
                except cancel.Cancelled:
                    # ⚠️ **這裡也一樣:取消不是失敗**(2026-09-19 review 補
                    # ——`LiveDiarizer._loop` 2026-09-18 修了,這條姊妹迴圈
                    # 當時漏掉,而它的症狀更糟):`Cancelled` 繼承
                    # BaseException,下面那個 `except Exception` 根本攔不到,
                    # 於是它直接從執行緒冒出去——`threading.excepthook` 把
                    # 整段英文 traceback 印到 stderr,而 `filelog.tee_console`
                    # 把 stderr 整條提級成 **WARNING** 寫進記錄檔
                    # (`filelog._LineTee` 自己記著「259 份記錄裡有 35 次
                    # traceback」),連軌別與時間區間都沒有。
                    # ⚠️ **窗口很現成**:`stop_and_flush` 要先 `_stop.set()`
                    # 才 join 這條執行緒,所以從「按下停止錄音」到那一刻、
                    # 以及 join 正在等一塊轉錄跑完的那幾十秒,全都算;而
                    # `desktop._shutdown()`(錄到一半關視窗)無條件
                    # `cancel.request()`,那條路更是必中
                    return
                except Exception:
                    # 失敗不推進 done:這段音訊下一輪(或收尾)重來,
                    # 絕不安靜跳過;退避避免緊迴圈重複失敗
                    tr.next_attempt = time.monotonic() + _RETRY_SEC
                    logger.warning(
                        "背景轉錄失敗(%s %.0f~%.0f 秒),稍後重試",
                        tr.key, tr.done, cut, exc_info=True,
                    )

    def _transcribe_span(self, tr: _Track, start: float, end: float) -> None:
        """轉錄 [start, end) 並累積段落(時間戳平移回全域時間軸)。

        起訖各記一行(2026-08-03 加):使用者回報「GPU 每隔一陣子飆升一下」
        且掉幀警告的出現節奏與這些爆發一致,而收音診斷(record._CaptureDiag)
        是每 30 秒一筆——兩邊落在同一個記錄檔、同一份毫秒時間戳,對得起來
        才答得出「掉幀到底是不是這一下造成的」。起是 DEBUG(只給分析)、
        訖是 INFO(耗時是使用者也看得到的即時進度感)。"""
        if end - start <= 0.05:
            with self._lock:
                tr.done = end
            return
        logger.debug("背景轉錄開始(%s):%.0f~%.0f 秒", tr.key, start, end)
        began = time.monotonic()
        pcm = _read_span(tr.path, start, end)
        span_wav = Path(self._tmp.name) / f"{tr.key}-{int(start)}.wav"
        with wave.open(str(span_wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(_RATE)
            w.writeframes(pcm.tobytes())
        try:
            segments, _device = transcribe.transcribe(
                span_wav, model_key=self.model_key, progress=None,
            )
        finally:
            span_wav.unlink(missing_ok=True)
        shifted = [
            TranscriptSegment(s.start + start, s.end + start, s.text)
            for s in segments
        ]
        logger.info(
            "背景轉錄(%s):%.0f~%.0f 秒(%.0f 秒音訊,耗時 %.1f 秒)",
            tr.key, start, end, end - start, time.monotonic() - began,
        )
        with self._lock:
            tr.segments.extend(shifted)
            tr.done = end

    def transcribed_until(self) -> float:
        """全軌皆已轉錄完成的時間點(UI「背景轉錄:已完成至」)。"""
        with self._lock:
            return min((tr.done for tr in self._tracks.values()), default=0.0)

    def snapshot(self) -> list[TranscriptSegment]:
        """目前累積的段落(全軌合併、按開始時間排序):即時預覽用。
        未做講者/回音去重(那要等收尾),預覽只求「看得到內容在長」。"""
        with self._lock:
            merged = [s for tr in self._tracks.values() for s in tr.segments]
        return sorted(merged, key=lambda s: (s.start, s.end))

    # -- 收尾 ----------------------------------------------------------

    def stop_and_flush(
        self,
        final_lengths: dict[str, float],
        progress: Callable[[float], None] | None = None,
    ) -> dict[str, list[TranscriptSegment]]:
        """停止背景排程並轉完各軌殘段,回傳各軌完整段落。

        排程執行緒可能正轉到一半:join 等它完成(該段工作不白費),
        殘段在呼叫端執行緒依序補轉(收尾有 gradio context,才有進度條)。
        使用者按「停止」由引擎內的 cancel 檢查點以 Cancelled 浮出。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        remain = {
            key: max(0.0, final_lengths.get(key, 0.0) - tr.done)
            for key, tr in self._tracks.items()
        }
        total = sum(remain.values())
        flushed = 0.0
        for key, tr in self._tracks.items():
            end_at = final_lengths.get(key, tr.done)
            while tr.done < end_at - 0.05:
                cancel.check()
                cut = min(tr.done + _CHUNK_SEC, end_at)
                if end_at - cut > 1.0:  # 還不是最後一刀才挑靜音切點
                    cut = _choose_cut(tr.path, tr.done, cut)
                before = tr.done
                self._transcribe_span(tr, tr.done, cut)
                flushed += tr.done - before
                if progress is not None and total > 0:
                    progress(min(flushed / total, 1.0))
        if progress is not None:
            progress(1.0)
        with self._lock:
            return {key: list(tr.segments) for key, tr in self._tracks.items()}

    def close(self) -> None:
        """冪等:呼叫端的 finally 兜底與正常收尾可能都會呼叫。"""
        if getattr(self, "_closed", False):
            return
        self._closed = True
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self.diar.close()
        self._tmp_lock.close()
        self._tmp.cleanup()


# ---------------------------------------------------------------------------
# 回音文字去重 + 兩軌合併


def _norm_text(t: str) -> str:
    """去空白/標點、統一比對面:回音兩份轉錄的差異主要在標點與斷句。"""
    return re.sub(r"[\W_]+", "", t, flags=re.UNICODE).lower()


def _prep_echo_index(
    sys_segments: list[TranscriptSegment],
) -> tuple[list[float], list[float], list[float], list[str]]:
    """回音比對的預備索引:按 start 排序後的 (starts, ends, 前綴最大 end,
    正規化文字)。文字正規化(regex)每段只做一次——先前每個麥克風段都
    對全部系統段重算,長會議兩邊各上千段時是 O(n×m) 的 regex 開銷;
    「前綴最大 end」單調,供 _is_echo 以二分縮小候選窗。"""
    ordered = sorted(sys_segments, key=lambda s: s.start)
    starts = [s.start for s in ordered]
    ends = [s.end for s in ordered]
    cummax_end = list(accumulate(ends, max))
    norms = [_norm_text(s.text) for s in ordered]
    return starts, ends, cummax_end, norms


def _is_echo(
    seg: SpokenSegment,
    sys_segments: list[TranscriptSegment],
    prep: tuple | None = None,
) -> bool:
    """麥克風軌段落是否為系統軌內容的回音(時間重疊+文字近似)。

    prep:_prep_echo_index 的預備索引(combine_tracks 整批比對時算一次
    重用);None 時自建(單獨呼叫,如測試)。"""
    mine = _norm_text(seg.text)
    if not mine:
        return False
    if prep is None:
        prep = _prep_echo_index(sys_segments)
    starts, ends, cummax_end, norms = prep
    # 候選窗 [lo, hi):hi 之後 start > seg.end+容差、lo 之前所有 end <
    # seg.start-容差(前綴最大 end 保證),都不在時間重疊範圍
    hi = bisect.bisect_right(starts, seg.end + _ECHO_TOL_SEC)
    lo = bisect.bisect_left(cummax_end, seg.start - _ECHO_TOL_SEC)
    window: list[str] = []
    for k in range(lo, hi):
        if ends[k] < seg.start - _ECHO_TOL_SEC:
            continue
        other = norms[k]
        if not other:
            continue
        window.append(other)
        if len(mine) >= _ECHO_MIN_CHARS or len(other) >= _ECHO_MIN_CHARS:
            if difflib.SequenceMatcher(None, mine, other).ratio() >= _ECHO_SIM:
                return True
    # 斷句不同(一句被拆兩半/兩句被併一句):與重疊窗內全文串接比子串
    joined = "".join(window)
    return len(mine) >= _ECHO_MIN_CHARS and (mine in joined)


@dataclass(frozen=True)
class TrackResult:
    """單軌的轉錄+講者分析結果(收尾中間產物)。"""
    kind: str  # "mic" | "system"
    path: Path
    segments: list[TranscriptSegment]
    turns: list[SpeakerTurn]
    voiceprints: dict
    # 該軌每位講者的分群品質(見 types.SpeakerQuality);合併時跟著重編號
    quality: list = field(default_factory=list)


def combine_tracks(
    results: list[TrackResult],
) -> tuple[list[SpokenSegment], list[SpeakerTurn], dict, dict, list]:
    """各軌獨立掛講者 → 麥克風軌回音去重 → 全域重編號合併。

    回傳 (spoken, turns, voiceprints, speaker_sources, quality):講者編號
    依首次發言重排(跨軌交錯也穩定),speaker_sources = {編號: 試聽該剪的
    軌檔}。被去重清空的講者(回音「幽靈講者」)連同 turns/聲紋/品質一併
    移除——幽靈的聲紋是遠端講者的房間回音版,登記進聲紋庫會污染比對。"""
    per_track: list[tuple[TrackResult, list[SpokenSegment]]] = []
    sys_segments = [
        s for r in results if r.kind == "system" for s in r.segments
    ]
    sys_segments.sort(key=lambda s: s.start)
    prep = _prep_echo_index(sys_segments) if sys_segments else None
    for r in results:
        spoken = merge.assign_speakers(r.segments, r.turns)
        if r.kind == "mic" and sys_segments:
            spoken = [s for s in spoken if not _is_echo(s, sys_segments, prep)]
        per_track.append((r, spoken))

    # 首次發言順序重編號((軌, 原標籤) → 新標籤);未知(-1)不參與
    tagged: list[tuple[float, str, SpokenSegment]] = []
    for r, spoken in per_track:
        tagged.extend((s.start, r.kind, s) for s in spoken)
    tagged.sort(key=lambda t: (t[0], t[2].end))
    mapping: dict[tuple[str, int], int] = {}
    for _start, kind, s in tagged:
        if s.speaker != UNKNOWN_SPEAKER and (kind, s.speaker) not in mapping:
            mapping[(kind, s.speaker)] = len(mapping)

    spoken_all = [
        SpokenSegment(
            s.start, s.end,
            mapping.get((kind, s.speaker), UNKNOWN_SPEAKER), s.text,
        )
        for _start, kind, s in tagged
    ]
    turns_all: list[SpeakerTurn] = []
    voiceprints: dict = {}
    sources: dict = {}
    quality: list = []
    for r, _spoken in per_track:
        for t in r.turns:
            new = mapping.get((r.kind, t.speaker))
            if new is not None:
                turns_all.append(SpeakerTurn(t.start, t.end, new, t.conf))
            elif t.speaker == UNKNOWN_SPEAKER:
                turns_all.append(t)
        for lab, vec in (r.voiceprints or {}).items():
            new = mapping.get((r.kind, lab))
            if new is not None:
                voiceprints[new] = vec
        for q in (r.quality or []):
            new = mapping.get((r.kind, q.speaker))
            if new is not None:
                quality.append(replace(q, speaker=new))
    for (kind, _old), new in mapping.items():
        sources[new] = next(r.path for r in results if r.kind == kind)
    turns_all.sort(key=lambda t: (t.start, t.end))
    quality.sort(key=lambda q: q.speaker)
    return spoken_all, turns_all, voiceprints, sources, quality


# ---------------------------------------------------------------------------
# 收尾總管


# 收尾各階段在進度中的區間(起點, 寬度):殘段轉錄多半不到一個
# _CHUNK_SEC(背景已攤掉大頭;連續發言延後切點時最多累到
# _CHUNK_SEC+_CUT_DEFER_MAX_SEC,收尾迴圈仍按 _CHUNK_SEC 分段轉),
# 講者分析要跑完整檔、是收尾主要耗時者。
# 「輸出」由共用的 pipeline.finalize 依其 _STAGE_SPANS(0.95 起)回報,
# 銜接在講者分析(至 0.95)之後,整體單調。
# 有 GPU 時兩相平行、合成單一階段回報(權重取兩區間的相對比例,
# 見 run_live_finish),依序路徑才逐階段走這張表
_FINISH_SPANS = {
    "轉錄收尾": (0.0, 0.30),
    "講者分析": (0.30, 0.65),
}


def track_dest(out_dir: Path, stem: str, kind: str) -> Path:
    """分軌檔放哪(雙軌合成失敗改交分軌、收尾沒做完保住音軌,兩處共用同一套檔名)。"""
    return out_dir / f"{stem}_{'現場' if kind == 'mic' else '電腦'}.wav"


def _move_in_place(src: Path, dest: Path) -> None:
    r"""同一顆磁碟上把檔換到另一個目錄(`os.replace`)。

    抽出來只為了讓測試換掉它、模擬「不同磁碟換不了名」(真的造出兩顆磁碟太麻煩)——
    同 `record._co_initialize_ex` 的理由。"""
    os.replace(src, dest)


def salvage_tracks(tracks: list, out_dir: Path, stem: str) -> list[Path]:
    r"""收尾沒做完(按了中止、報錯)或錄音中關視窗時,把錄好的音軌放進 `out_dir`,回傳**真的存好**的檔。

    ⚠️ **硬規則**(`docs/spec/01` §1.3 原則 4、`docs/spec/10`):現場收音的任何失敗路徑都要
    先把音軌保進成品資料夾再報錯——逐字稿可以重轉,會議原音不能。網頁版在 `app.py` 做了,
    原生版接上錄音收尾時漏掉(成功時音檔在收尾最後才進 `output`,中途失敗就只留在
    `%LOCALAPPDATA%` 底下的 `recordings`,同仁找不到),2026-09-15 對文案時才發現、使用者
    裁定補上。
    ⚠️ **單軌存成 `<stem>.wav`**(與成功時同名);**雙軌存兩個分軌檔、不合成**:合成要跑
    ffmpeg,而收尾失敗的原因可能正是它。
    ⚠️ **一軌失敗不擋另一軌,而且不拋**:呼叫端正要報另一個錯,這裡再拋會把原本的錯蓋掉;
    失敗寫進記錄檔,由回傳值少了哪幾個讓呼叫端講出來。
    ⚠️ **先直接搬,搬不動才複製**(2026-09-15 使用者:「用移動檔案的方式會比較快」):同一顆
    磁碟上搬只是換目錄項,幾百 MB 也是瞬間,錄音工作目錄裡也不會多留一份。搬不動的兩種情況
    才退回複製——**不同磁碟**(工具裝在 D:,`%LOCALAPPDATA%` 在 C:),以及**檔案還被佔用**
    (收尾中關視窗時,收尾那條執行緒或講者分析子行程可能還開著它,Windows 不准搬)。
    ⚠️ **複製成功才刪原檔,刪不掉就留著**:多一份比少一份好。同一顆磁碟的搬移是一步完成的,
    不會出現「搬到一半」,所以「原檔是複製失敗時唯一的退路」這件事照樣成立。
    ⚠️ **複製先寫暫存檔再換名,不直接寫 `dest`**:關視窗那條由主執行緒保音軌,而收尾那條
    工作執行緒是 daemon、可能同時也在保,或者寫到一半就隨行程結束被砍掉。直接寫的話,`dest`
    可能是另一邊剛放好的完整檔,被 `wb` 截斷之後又沒寫完;換名則讓 `dest` 永遠是完整的,最壞
    只在旁邊留一個 `.part`。暫存檔名帶執行緒代號,兩邊各寫各的。"""
    saved: list[Path] = []
    for t in tracks:
        dest = out_dir / f"{stem}.wav" if len(tracks) == 1 else track_dest(out_dir, stem, t.kind)
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            _move_in_place(t.path, dest)
        except FileNotFoundError:
            # 來源已經不在(另一邊搬走了,或根本沒有)——複製也只會再失敗一次
            logger.exception("錄音音軌沒能存進成品資料夾:%s → %s", t.path, dest)
            continue
        except OSError:
            if not _copy_track(t.path, dest):
                continue
        except Exception:
            logger.exception("錄音音軌沒能存進成品資料夾:%s → %s", t.path, dest)
            continue
        saved.append(dest)
    logger.info("收尾沒做完,已保住 %d/%d 條音軌:%s", len(saved), len(tracks),
                "、".join(p.name for p in saved) or "(一條都沒存成)")
    return saved


def _copy_track(src: Path, dest: Path) -> bool:
    """搬不動時的退路:複製到暫存檔、換成正式檔名、再刪原檔。成功回 True,失敗記記錄檔回 False。"""
    part = dest.with_name(f"{dest.name}.{threading.get_ident()}.part")
    try:
        shutil.copy2(src, part)
        part.replace(dest)
    except Exception:
        logger.exception("錄音音軌沒能存進成品資料夾:%s → %s", src, dest)
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass            # 清不掉就留著:它只是暫存檔,原本那個錯才是要講的
        return False
    try:
        Path(src).unlink()
    except OSError:
        logger.debug("音軌已複製進成品資料夾,原檔還被佔用、先留著:%s", src, exc_info=True)
    return True


def run_live_finish(
    tracks: list,  # list[record.RecordedTrack]
    live: LiveTranscriber,
    out_dir: Path,
    stem: str,
    num_speakers: int = 0,
    on_stage: Callable[[str, float], None] | None = None,
) -> pipeline.PipelineResult:
    """停止錄音後的收尾:殘段轉錄 + 各軌講者分析(有 GPU 時兩者平行,
    純 CPU 依序)→ 合併/回音去重 → 共用 finalize(繁化/跳針標記/線索/
    輸出檔)→ 成品音檔。

    num_speakers 只在單軌情境傳給講者分析;線上會議(雙軌)一律自動
    偵測——「總人數」無法拆成兩軌各幾人,硬塞會把單軌聚類逼出錯的刀。
    講者分析一律執行(曾有「講者辨識」開關,使用者 2026-07-26 指定移除)。
    成品音檔:單軌直接搬進 out_dir;雙軌合成立體聲(左=現場、右=系統),
    軌檔保留原位供試聽剪輯(speaker_sources)。"""
    def report(stage: str, frac: float = 0.0) -> None:
        if on_stage:
            start, width = _FINISH_SPANS.get(stage, (1.0, 0.0))
            on_stage(stage, min(start + width * frac, 1.0))

    with power.keep_awake():
        report("轉錄收尾")
        # **收尾階段的記錄**(2026-08-04 補):在此之前,從「按下停止」到
        # 成品落地之間記錄檔是一片空白——2026-08-04 那場 64 分鐘的錄音,
        # 停止之後 40 幾分鐘沒有任何一行,無從判斷是還在算、還是卡住了。
        # 而這段正是最慢的一段(講者分析的積壓要在這裡補完)
        t_start = time.monotonic()
        logger.info(
            "開始收尾:%d 軌、共 %.0f 秒音訊(%s)",
            len(tracks), sum(t.duration for t in tracks),
            "轉錄與講者分析平行" if transcribe.gpu_available() else "依序執行",
        )
        # 標點模型先就緒(同 run_pipeline)
        punctuate.ensure_ready()
        final_lengths = {t.kind: t.duration for t in tracks}
        by_kind = {t.kind: t for t in tracks}
        n_tracks = len(tracks)
        per_track = num_speakers if n_tracks == 1 else 0
        # 分群特徵檔跟著成品音檔走(成品與逐字稿都在 out_dir、同名)。
        # ⚠️ **只有單軌才存**:線上會議是兩軌各自分群再合併,一份 npz
        # 表達不了那個結果——事後拿它重分群會得到跟成品不一樣的東西,
        # 那比不給還糟(同 per_track 只在單軌才傳的理由)。
        # ⚠️ 2026-08-19 討論過要不要支援雙軌(結論:先不做),真要做得處理的
        # 四件事與那次的調查結論記在 docs/dev/recording.md,別再從頭查一遍
        features_out = (
            diarize.features_path(out_dir / f"{stem}.wav") if n_tracks == 1
            else None
        )

        def diarize_all(progress_fn: Callable[[float], None]) -> list[tuple]:
            # 錄音中的增量切分先收工:sherpa 引擎不可兩條執行緒併用。進行中
            # 那塊等它跑完(工作不白費),等待期間的完成度佔前 10% 進度
            # ——不掛進度的話,這幾分鐘進度條會定格
            live.diar.stop(progress=lambda f: progress_fn(0.1 * f))
            # 增量做到哪、還欠多少:這是「散會後還要等多久」的唯一預告,
            # 而增量追不追得上完全看機器(實測這台 RTF 0.83~1.47,一場
            # 64 分鐘的會停止時還積著約 600 秒沒算)
            if live.diar.active:
                snapshot = live.diar.done_sec
                for t in tracks:
                    done = snapshot.get(t.kind, 0.0)
                    logger.info(
                        "講者分析待補(%s):錄音中已完成 %.0f 秒 / 全長 %.0f 秒,"
                        "還要補 %.0f 秒", t.kind, done, t.duration,
                        max(0.0, t.duration - done),
                    )
            out = []
            for i, t in enumerate(tracks):
                turns, vps, quality = live.diar.finish(
                    t.kind, t.path, t.duration, num_speakers=per_track,
                    progress=lambda f, i=i: progress_fn(
                        0.1 + 0.9 * (i + f) / n_tracks
                    ),
                    features_out=features_out,
                )
                out.append((t, turns, vps, quality))
            return out

        if transcribe.gpu_available():
            # 殘段轉錄(GPU)與講者分析(CPU)異質資源、互不依賴(軌檔在
            # 錄音停止當下即完整),執行緒真平行——收尾時間趨近 max 而非
            # 相加,「會議一散等收尾」的體感直接縮短。排程理由與
            # contextvars 地雷同 pipeline._transcribe_and_diarize。進度
            # 合成單一階段:權重取 _FINISH_SPANS 兩區間的相對比例,兩相
            # 各自只增不減、on_stage 在鎖內呼叫,合成後保證單調
            fracs = {"flush": 0.0, "diar": 0.0}
            flock = threading.Lock()

            def sub(key: str) -> Callable[[float], None]:
                def cb(f: float) -> None:
                    with flock:
                        fracs[key] = max(fracs[key], min(f, 1.0))
                        if on_stage:
                            combined = 0.30 * fracs["flush"] + 0.65 * fracs["diar"]
                            on_stage("轉錄與講者分析收尾", combined)
                return cb

            with ThreadPoolExecutor(max_workers=2) as ex:
                fut_f = ex.submit(
                    contextvars.copy_context().run, live.stop_and_flush,
                    final_lengths, progress=sub("flush"),
                )
                fut_d = ex.submit(
                    contextvars.copy_context().run, diarize_all, sub("diar"),
                )
                segments = fut_f.result()
                diarized = fut_d.result()
        else:
            # 純 CPU:依序(同 pipeline 的排程理由——兩引擎同搶一顆 CPU
            # 只會過度訂閱,依序也壓低 8GB 基準機的峰值記憶體)
            segments = live.stop_and_flush(
                final_lengths, progress=lambda f: report("轉錄收尾", f),
            )
            report("講者分析")
            diarized = diarize_all(lambda f: report("講者分析", f))
        results = [
            TrackResult(t.kind, t.path, segments[t.kind], turns, vps, quality)
            for t, turns, vps, quality in diarized
        ]
        logger.info(
            "轉錄與講者分析收尾完成(耗時 %.0f 秒):%s",
            time.monotonic() - t_start,
            "、".join(
                f"{r.kind} {len(r.segments)} 段/{len(r.turns)} 個發言輪"
                for r in results
            ),
        )
        cancel.check()  # 同 run_pipeline:「輸出」階段不再攔

        spoken, turns_all, voiceprints, sources, quality = combine_tracks(results)

        # 成品音檔先落地再寫逐字稿:雙軌合成失敗要在輸出前浮出/降級,
        # 不能等使用者收工才發現沒有音檔
        out_dir.mkdir(parents=True, exist_ok=True)
        audio_out = out_dir / f"{stem}.wav"
        if n_tracks == 2:
            try:
                audio.merge_stereo(by_kind["mic"].path, by_kind["system"].path, audio_out)
            except Exception:
                # 合成失敗不擋逐字稿:退而求其次交付兩個單軌檔
                logger.warning("雙聲道合成失敗,改交付分軌檔", exc_info=True)
                audio_out = None
                extra = []
                for t in tracks:
                    dest = track_dest(out_dir, stem, t.kind)
                    shutil.copy2(t.path, dest)
                    extra.append(dest)
        else:
            shutil.copy2(tracks[0].path, audio_out)
        # 音檔先落地是刻意的(見上),所以它值得單獨一行:錄音不能重來,
        # 「音檔到底存了沒」是出事時第一個要回答的問題
        logger.info("成品音檔已存檔:%s", audio_out or "(分軌檔)")
        result = pipeline.finalize(
            spoken, turns_all, voiceprints, out_dir, stem,
            device="", on_stage=on_stage,
            speaker_sources=sources, quality=quality,
        )
        outputs = list(result.outputs)
        if audio_out is not None:
            outputs.append(audio_out)
        else:
            outputs.extend(extra)
        return replace(result, outputs=outputs)
