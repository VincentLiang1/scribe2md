"""現場收音:三情境分軌錄音,邊錄邊寫 WAV(當機可復原)。

情境對應(UI 文案照場合命名,2026-07-21 使用者選定):
- 現場會議(onsite)= 麥克風一軌
- 線上會議(online)= 麥克風軌 + 系統聲音軌,分軌不預混
- 只錄電腦聲音(playback)= 系統聲音一軌

擷取函式庫選 soundcard 的原因(2026-07-21 本機實測):
- sounddevice 內建的 PortAudio(19.7)「沒有」WASAPI loopback,列不出
  任何系統聲音裝置——網路上「Windows wheel 支援 loopback」指的是
  pyaudiowpatch 用的 PortAudio 分支,別再踩一次;
- pyaudiowpatch 可用,但是綁 CPython 版本的編譯套件,且只給裝置原生
  取樣率(48k 立體聲)要自行重採樣;
- soundcard 純 Python(CFFI 直呼 WASAPI),麥克風與 loopback 同一套
  API,shared mode 自動轉換讓兩者都能直接以 16k 單聲道開流(免重採樣),
  且 loopback 在「沒有程式播放聲音」時仍持續送靜音幀(實測 1.03 秒收
  16000 幀),時間軸不斷流。

設計要點:
- 分軌不預混:線上會議寫「麥克風軌+系統軌」兩個 16k 單聲道 WAV。兩軌
  分開轉錄互不干擾;回音(喇叭外放被麥克風收回去)由收尾階段的文字層
  去重處理,錄音層不做混音也不做 AEC。成品雙聲道 WAV 由收尾以 ffmpeg
  合成。
- 邊錄邊寫+定期回寫標頭:wave 模組要 close() 才補標頭,硬當機=標頭
  記 0 幀、檔案不可播;自寫 44-byte 標頭、每 _HEADER_FLUSH_SEC 回填
  長度欄位,當機最多損失最後兩秒。
- 拉取模型:soundcard 的 recorder 必須「在使用它的執行緒內」建立
  (WASAPI 用戶端有執行緒親和),故開裝置也在 worker 執行緒裡做,
  start() 以 Event 同步等開裝置結果——裝置被獨占等錯誤要在按下錄音
  的當下浮出,不能等錄完才發現一場空。
- 斷流補償:record() 若因裝置停擺長時間沒資料,恢復後先補零到牆鐘
  位置再寫新資料——「檔案時間軸 = 牆鐘時間軸」是跨軌時間戳可對照的
  前提(正常情況 loopback 自動補靜音,這是保險絲)。
- 掉幀防護:WASAPI 緩衝開 _WASAPI_BUFFER_SEC 秒扛住錄音期間的 GIL 阻塞
  (成因與實測數據見該常數註解——收音執行緒會被 sherpa 鎖住 5~15 秒),
  停止後 _drain 把緩衝裡的尾巴拉完;殘餘的 data discontinuity 警告=真
  掉資料,由 _SoundcardWarnings 轉成繁中節流 log+停止總結——只降噪、
  絕不悶掉。
- 掉幀診斷(_CaptureDiag,2026-08-03 加):黑視窗那條節流訊息只講得出
  「累計幾次」,而那個數字**分不出成因**——使用者回報一場會議累計 619 次、
  工作管理員卻只有 10~20% CPU,原本「被講者分析的執行緒排擠」的假設當場
  站不住。故每 30 秒往紀錄檔(filelog,不進黑視窗)記一筆足以判斷成因的
  統計,細節見該類 docstring。
- soundcard 惰性 import:單元測試注入假模組;app 啟動不必先初始化 COM。
- COM 每條執行緒各自初始化(_ensure_com,自己叫 ole32 不借 soundcard 的
  私有屬性):soundcard 只初始化「第一次 import 它的那條執行緒」,而
  gradio 事件跑在會被回收的 anyio worker 上
  ——那條退場後整個 MTA 被拆,第二次按「開始錄音」就 CO_E_NOTINITIALIZED
  (使用者 2026-08-05 回報的「找不到麥克風、只能重開程式」),詳見該函式。
- 已知限制:兩軌各用自家裝置時脈,長錄音累積毫秒級漂移(4 小時約
  零點幾秒);收尾文字去重的時間重疊判斷已留容差,不做時脈校正。
"""
import ctypes
import logging
import sys
import threading
import time
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from meeting_scribe import power
from meeting_scribe.errors import UserFacingError

logger = logging.getLogger(__name__)

TARGET_RATE = 16000

# 情境 → 要錄的軌(kind:mic=麥克風、system=系統聲音 loopback)
SCENARIO_TRACKS = {
    "onsite": ("mic",),
    "online": ("mic", "system"),
    "playback": ("system",),
}

_HEADER_FLUSH_SEC = 2.0
# 每次向裝置拉取的幀數(0.1 秒):停止延遲與輪詢開銷的折衷
_PULL_FRAMES = 1600
# WASAPI 緩衝秒數(recorder 的 blocksize)。soundcard 預設只要一個裝置週期
# (本機實測 356 幀≈22ms),一停頓就溢位掉幀。
#
# **緩衝要多大,取決於「主行程裡誰還會長時間持有 GIL」**(2026-08-03 定案)。
# 關鍵事實:GIL 被鎖住時**音訊沒有不見,只是我們太晚去拿**——它躺在 WASAPI
# 的環形緩衝裡,緩衝夠大就一個位元組都不會掉。
#
# 曾經需要 30 秒,是因為增量講者切分跑在主行程:sherpa-onnx 的 pybind11 綁定
# 在 C++ 運算期間沒有釋放 GIL(pybind11 預設就不釋放,要明寫 call_guard),
# 而 soundcard 的等待迴圈是 Python,收音執行緒因此**整整 5~15 秒排不上**;
# 2 秒緩衝下的災情是一場 90 分鐘的真實會議掉了 4.6 分鐘。**那條已改由
# 子行程根治**(見 diarworker),主行程剩下的最長 GIL 持有者是轉錄——
# 金絲雀實測(scripts/probe_gil.py)空機 3.5ms、轉錄 **1,450ms**、
# 講者分析 6,834ms(已移出);子行程版實跑 12 分鐘,拉取單圈最大 **188ms**。
#
# 5 秒 = 對轉錄那 1.45 秒留 3.4 倍餘裕。**實跑驗過但餘裕沒有想像中大**:
# 同樣 12 分鐘、5 秒緩衝那輪的拉取單圈最大是 **1,974ms**(不連續 0 次、
# 捏造靜音 2.0 秒、檔案 +4.7 秒、全機 CPU 中位 93%)——順帶說明了
# **原本的 2 秒緩衝本來就已經在邊緣**(1,974 vs 2,000ms),就算沒有講者
# 切分那條 GIL 也遲早會踩到。要再調小之前先跑 scripts/repro_live.py。
# **不留更多是因為緩衝要付代價**:
# 停止時得把緩衝排空(見 _drain),緩衝越大這段最壞等待越久,而使用者按下
# 停止是希望它停(使用者 2026-08-03 指定不要 30 秒)。緩衝是容量不是等待
# 單位,錄音期間加大不增加延遲。
# 試過並確認無效的:MMCSS 提高收音執行緒優先權(OS 優先權對「等 GIL」
# 沒有作用)、把 sherpa 從 7 條降到 5 條(GIL 不看執行緒數,反而更差)。
_WASAPI_BUFFER_SEC = 5.0
# 檔案落後牆鐘超過此秒數才補零:必須大於 _WASAPI_BUFFER_SEC——執行緒
# 停頓後恢復,拉回的是「最多落後緩衝容量的舊料」,落後量會短暫衝到
# 停頓長度;門檻若不大於緩衝容量,恢復瞬間會把遲到舊料誤判成斷流,
# 補零之後又寫入舊料,檔案反而比牆鐘長、之後整段後移。只有真斷流
# (裝置停擺,緩衝裡沒料)才該觸發,測試守著這個大小關係。
_DRIFT_PAD_SEC = 8.0
# 檔案**跑在牆鐘前面**超過此秒數就開始修(補零保險絲的反向,2026-08-04)。
# 成因是 soundcard 在裝置交不出資料時自己捏造靜音回傳(見 _install_chunk_probe),
# 那些零幀是「插進去」不是「換掉」——真實音訊一個都沒少,但整條時間軸被
# 往後推。2026-08-04 一場 64 分鐘的真實會議:停擺 781 次共 45.4 秒,檔案
# 3888.6 秒 vs 牆鐘 3844.5 秒(+1.1%),片尾的逐字稿時間戳因此晚了 44 秒。
#
# **門檻可以比補零那邊小得多**:補零是往檔案裡塞東西、塞錯地方會把後面
# 整段推走,所以要等到確定不是「緩衝裡的舊料」;修剪只從**全零段**裡拿,
# 拿錯了也只是少幾個零樣本。0.5 秒明顯高於實測的 ±0.2 秒正常抖動。
#
# **真實裝置上只修得掉一部分,而且原因是先後順序**(2026-08-04
# `scripts/repro_live.py` 12 分鐘各跑一輪,只差這個常數):
#     不修剪  牆鐘 723.0 / 檔案 726.6(+3.6)、停擺 50 次 4.1 秒
#     修剪    牆鐘 723.0 / 檔案 724.7(+1.8)、停擺 31 次 3.2 秒、修掉 1.1 秒
# 檔案裡 ≥20ms 的全零段從 50 段 4.06 秒降到 13 段 1.92 秒——**零星的小停擺
# 幾乎都修掉了**,那也正是實際會議的主要形態(781 次裡絕大多數是 20~100ms)。
# 修不掉的是**一次大爆發**:soundcard 補的零填的是「已經過去的那段時間」,
# 所以寫下去的當下檔案並沒有超前;超前是**之後**才浮現的——WASAPI 緩衝
# 裡那段時間的真實聲音接著被排空,而那些是有訊號的樣本、沒有零可以拿。
# 兩輪的最長零段都落在開錄後 2.4 秒(裝置暖機),1.82 → 1.60 秒。
# 要連這種也修掉,只能回頭改寫已經寫出去的資料,不值得為 0.25% 冒那個險。
_DRIFT_TRIM_SEC = 0.5
# 每個全零段至少留這麼多樣本(10ms)。零接零不會產生任何不連續,所以從
# 段落**內部**拿是無損的;但整段拿光會把說話之間的停頓抹掉,那是真的改
# 變了錄音內容
_TRIM_KEEP_FRAMES = 160
# start() 等 worker 開裝置的時限:soundcard 開流實測毫秒級,5 秒已極寬裕
_OPEN_TIMEOUT_SEC = 5.0
# 停止後排空緩衝的時限(見 _TrackRecorder._drain)。緩衝裡最多就是
# _WASAPI_BUFFER_SEC 秒的資料、而且已經在記憶體裡,排空本身是搬運;
# 這個保險絲是為了「排到一半又撞上一次 GIL 阻塞」
_DRAIN_TIMEOUT_SEC = 5.0
# stop() 等 worker 收工的時限:必須容得下一次完整的排空(測試守著),
# 否則好不容易留住的尾巴會在 join 逾時那條路上被丟掉。這個數字就是
# 「按下停止最壞要等多久」,不要為了保險把它拉大
_STOP_JOIN_SEC = 10.0
# 收檔那行要不要用 WARNING 的判準(理由見 _log_accounting 的 ⚠️)。
# 補零是實打實的損失,用絕對值;牆鐘與檔案的落差含裝置時鐘漂移,用比例
# ——0.2% 容得下實測的 0.06%(9120 秒差 5.6 秒),又攔得住 2026-08-03 那次
# 幻影靜音的災情(726.8 秒的錄音長出 907.6 秒,+24.9%)
_PAD_TOL_SEC = 1.0
_DRIFT_ABS_TOL_SEC = 1.0
_DRIFT_REL_TOL = 0.002


def _sc():
    """惰性載入 soundcard(原生庫與 COM 的成本延後到真正錄音才付)。

    順手補上 warn 替身與**本執行緒的 COM 初始化**:兩者都是便宜的
    idempotent 檢查,而「凡是要碰 soundcard 就得先經過這裡」正是
    _ensure_com 得以保證每條執行緒都初始化過的原因。"""
    import soundcard

    _install_warn_shim(soundcard)
    _ensure_com()
    return soundcard


# COM 初始化是 per-thread 的,每條執行緒各記一次(執行緒退場即隨之消失)
_com_local = threading.local()
_COINIT_MULTITHREADED = 0x0
# CoInitializeEx 可接受的回傳(一律轉無號再比):S_OK、S_FALSE(本執行緒
# 先前已初始化)、RPC_E_CHANGED_MODE(本執行緒已在別的 apartment/STA——
# COM 照樣能用,只是不歸我們初始化)
_COINIT_OK = (0x0, 0x1, 0x80010106)


def _co_initialize_ex() -> int | None:
    """在**目前這條執行緒**上呼叫 CoInitializeEx;回無號 HRESULT。

    非 Windows 回 None(沒有 COM 這回事)。抽成獨立一支是為了讓測試換掉
    它,同 `power._set_priority_class` 那一層。"""
    if sys.platform != "win32":
        return None
    try:
        # ctypes 預設把回傳當有號 c_int,HRESULT 一律 & 0xFFFFFFFF 轉無號再比
        return ctypes.windll.ole32.CoInitializeEx(
            None, _COINIT_MULTITHREADED
        ) & 0xFFFFFFFF
    except Exception:
        # ole32 是 KnownDLL,理論上不會發生;真發生時不得靜靜略過
        logger.warning("叫不到 ole32.CoInitializeEx", exc_info=True)
        return None


def _ensure_com() -> None:
    """在**目前這條執行緒**上初始化 COM(每條各自負責,不靠別人的 apartment)。

    2026-08-05 使用者回報「第一場錄完、講者也命名完,再按『開始錄音』
    就說找不到麥克風,只能關掉重開」的根因。實測(scratchpad 重現腳本)
    的完整鏈條:

    - soundcard 只在模組層做一次 `_com = _COMLibrary()`,也就是**只有
      第一次 import 它的那條執行緒**呼叫過 CoInitializeEx;
    - 未初始化的執行緒之所以「看起來也能用」,是因為 MTA 只要還有任何
      一條執行緒持有就存在(隱式 MTA)——第一場錄音正是靠這個活著的
      (實測:MTA 持有者還在時,全新執行緒呼叫成功);
    - gradio 的事件跑在 anyio 的 worker 執行緒上,閒置約 10 秒就回收。
      第一次按「開始錄音」的那條 worker 退場時,ole32 的 thread-detach
      收掉最後一份 MTA 參照,**整個 apartment 被拆掉**;
    - 第二次按「開始錄音」落在另一條 worker 上,CoCreateInstance 直接回
      CO_E_NOTINITIALIZED(0x800401f0),soundcard 翻成
      `RuntimeError: Error 0x800401f0`,再被 _default_mic 的
      `except Exception` 說成「找不到麥克風」——訊息是**假的**,而且會
      把人導去檢查 Windows 音效設定(那裡什麼問題都沒有)。重開程式會好,
      只是因為 import 又發生在一條剛好還活著的執行緒上。

    **刻意不 CoUninitialize**:那正是上面那條災難鏈的成因,而執行緒退場
    時 ole32 本來就會清掉自己那一份,不會累積。

    **刻意自己叫 ole32、不借 soundcard 的 `mediafoundation._ole32`**:
    apartment 是每條執行緒的 OS 狀態、ole32 又是整個行程共用的 KnownDLL,
    借私有屬性換不到任何東西,卻讓「上游改名 = 這條修正無聲失效」。
    走 ctypes 也與 `power.py` 那五支 Win32 呼叫同一個高度。"""
    if getattr(_com_local, "ready", False):
        return
    hr = _co_initialize_ex()
    if hr is not None and hr not in _COINIT_OK:
        # 失敗不得記成「已好」(下次重試)。錯誤本身留給接下來的 soundcard
        # 呼叫去炸——那裡的繁中訊息更貼近使用者當下在做的事
        logger.warning("CoInitializeEx 失敗(hr=%#010x)", hr)
        return
    _com_local.ready = True


# 各錄音 worker 執行緒把自己那軌的掉幀帳本掛在這裡,warn 替身依執行緒查表
_disc_local = threading.local()

_DISC_LOG_INTERVAL_SEC = 60.0
# 診斷統計往紀錄檔寫一筆的間隔。30 秒:兩小時會議 240 行(貼得進對話),
# 又細到看得出掉幀是「整場均勻」還是「跟著某件事叢發」
_DIAG_INTERVAL_SEC = 30.0
# 單次 _record_chunk 超過這麼久 = soundcard 走了「裝置交不出資料」那條路
# (見 _install_chunk_probe)。soundcard 的門檻是 4 個 default device period,
# 典型 10ms → 40ms;取固定值當判準,真值由開流時記進紀錄檔備查
_STALL_SEC = 0.040

_KIND_LABEL = {"mic": "麥克風", "system": "系統聲音"}


class _DiscontinuityLog:
    """單軌掉幀帳本:首次立即示警、之後節流,停止時總結。

    掉幀是真資料損失,不能因為訊息吵就悶掉——降噪的做法是節流+總結,
    黑視窗上一定看得到。hit() 只在該軌 worker 執行緒呼叫、count 由主
    執行緒在 join 之後讀,無並行寫,不需鎖。

    另外收下每次的發生時刻給 _CaptureDiag 做間隔分佈——**這是黑視窗那個
    累計數字給不出來的資訊**:均勻的間隔代表裝置節律(驅動/DSP),叢發
    代表負載尖峰,兩者的修法完全相反。視窗清空由 take_window 負責,
    最多累積 _DIAG_INTERVAL_SEC 這麼一段,不會無限長。"""

    def __init__(self, kind: str):
        self.kind = kind
        self.count = 0
        self._last_log: float | None = None
        self._window: list[float] = []

    def take_window(self) -> list[float]:
        """取走並清空本視窗的發生時刻(僅診斷執行緒=該軌 worker 自己呼叫)。"""
        window, self._window = self._window, []
        return window

    def hit(self) -> None:
        self.count += 1
        now = time.monotonic()
        self._window.append(now)
        if self._last_log is None or now - self._last_log >= _DISC_LOG_INTERVAL_SEC:
            self._last_log = now
            logger.warning(
                "錄音資料不連續(%s軌,累計 %d 次):電腦忙不過來,該瞬間的聲音已掉失",
                _KIND_LABEL.get(self.kind, self.kind),
                self.count,
            )

    def summary(self) -> None:
        if self.count:
            logger.warning(
                "錄音期間共 %d 次資料不連續(%s軌):次數偏多時,對應時間點的逐字稿可能缺漏",
                self.count,
                _KIND_LABEL.get(self.kind, self.kind),
            )


class _SoundcardWarnings:
    """soundcard.mediafoundation 命名空間裡 warnings 模組的替身。

    data discontinuity 警告=WASAPI 回報掉幀:原樣是英文且會洗版黑視窗
    (使用者可見訊息繁中,spec §8),轉成該軌帳本的計數+節流繁中 log。
    其餘警告、或在未註冊執行緒冒出的同訊息,一律原樣轉發給真 warnings
    ——寧可吵也不悶掉。只換 mediafoundation 自己的模組參照,不動全域
    warnings(filter/showwarning 是行程級,會波及其他函式庫與執行緒)。"""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def warn(self, message, *args, **kwargs):
        if "data discontinuity" in str(message):
            disc = getattr(_disc_local, "log", None)
            if disc is not None:
                disc.hit()
                return
        self._real.warn(message, *args, **kwargs)


def _install_warn_shim(sc_module) -> None:
    """把 warn 替身裝進 soundcard.mediafoundation(冪等;非 Windows 或
    測試的假模組沒有該子模組,直接略過)。"""
    mf = getattr(sc_module, "mediafoundation", None)
    if mf is None or isinstance(getattr(mf, "warnings", None), _SoundcardWarnings):
        return
    mf.warnings = _SoundcardWarnings(mf.warnings)


# ---------------------------------------------------------------------------
# 掉幀診斷


def _install_chunk_probe(rec, on_chunk) -> bool:
    """量「soundcard 向 WASAPI 要一個 packet」花多久;回傳是否裝上。

    **為什麼值得為它包一層第三方私有方法**:soundcard 在裝置連續交不出
    資料約 40ms 之後,會在 `_record_chunk` 裡**自己捏造靜音**回傳
    (mediafoundation.py 的 idle 分支)。那條路是「裝置端停擺」的直接
    指紋,而且捏造出來的零幀會讓檔案長度看起來完全正常——連我們自己的
    補零保險絲(_DRIFT_PAD_SEC)都抓不到,因為落後量根本不會累積。
    這是唯一測得到它的地方。

    soundcard 改名或換實作就安靜放棄(回 False,診斷少一欄),絕不讓
    診斷有辦法弄壞錄音。"""
    inner = getattr(rec, "_record_chunk", None)
    if not callable(inner):
        return False

    def timed():
        t0 = time.perf_counter()
        try:
            return inner()
        finally:
            on_chunk(time.perf_counter() - t0)

    rec._record_chunk = timed
    return True


def _trim_zero_runs(pcm: np.ndarray, want: int) -> tuple[np.ndarray, int]:
    """從連續全零段裡丟掉最多 want 個樣本,回傳 (結果, 實際丟掉幾個)。

    **只動全零段是整個做法成立的前提**:零接零銜接處沒有任何不連續,所以
    從段落內部拿掉樣本在訊號上是無損的;反過來,在有訊號的地方抽掉樣本會
    在波形上留一個階躍,聽起來就是一聲喀噠。

    **長段優先**:同樣要丟 N 個樣本,從長段丟比較不會把短停頓抹平。每段
    至少留 `_TRIM_KEEP_FRAMES`——說話之間的停頓本身是內容(逐字稿的斷句
    與講者分離都看得到它),整段抽光就不只是修時間軸了。

    拿不到足夠的零樣本就丟多少算多少:修剪是盡力而為,寧可少修一點也不
    要為了湊數去動有聲音的地方。下一次拉取還會再試(見 _run 的呼叫點)。"""
    if want <= 0 or pcm.size == 0:
        return pcm, 0
    zero = (pcm == 0).view(np.int8)
    if not zero.any():
        return pcm, 0
    edges = np.diff(np.concatenate(([np.int8(0)], zero, [np.int8(0)])))
    starts, ends = np.where(edges == 1)[0], np.where(edges == -1)[0]
    drop = np.zeros(pcm.size, dtype=bool)
    left = want
    for i in np.argsort(ends - starts)[::-1]:
        if left <= 0:
            break
        s, e = int(starts[i]), int(ends[i])
        take = min(left, (e - s) - _TRIM_KEEP_FRAMES)
        if take <= 0:
            break  # 已排序,這段不夠長,後面的更短
        drop[s: s + take] = True
        left -= take
    n = int(drop.sum())
    return (pcm[~drop] if n else pcm), n


def _pct(values: list[float], q: float) -> float:
    """排序後的第 q 分位(0~1)。統計庫的 quantiles 對小樣本要求多、這裡
    只要一個看趨勢的數字。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))]


class _CaptureDiag:
    """每 _DIAG_INTERVAL_SEC 往紀錄檔記一筆收音診斷(DEBUG,不進黑視窗)。

    黑視窗那條「累計 N 次」分不出成因,而三種成因的修法完全不同,所以
    這幾組數字缺一不可:

    - **掉幀的間隔分佈**:均勻(例如穩定每 0.6 秒一次)= 裝置/驅動節律,
      跟本工具的負載無關;叢發 = 負載尖峰,才輪得到執行緒優先權那些手段。
    - **裝置交不出資料的次數與秒數**(見 _install_chunk_probe):非 0 就
      指向裝置端,而且代表有一段音訊被換成了捏造的靜音。
    - **拉取單圈耗時**:只有超過 WASAPI 緩衝容量(_WASAPI_BUFFER_SEC)的
      停頓才代表「我們的執行緒被餓到」,那是唯一該去調優先權/執行緒數的
      情形。p50 落在拉取長度附近是正常的(等資料本來就要等)。
    - **落後牆鐘幾秒**:唯一能回答「到底掉了幾秒音訊」的數字。補零保險絲
      的門檻是 5 秒,少於 5 秒的損失它整場都不會提,先前完全沒被量過。
    - **行程 CPU 秒數**:把「工作管理員看起來只有 10~20%」變成客觀數字。
      process_time 是跨執行緒總和,除以牆鐘就是「等於幾顆核心在忙」。

    只在該軌 worker 執行緒內使用,無並行,不需鎖。"""

    def __init__(self, kind: str, disc: _DiscontinuityLog, start_mono: float):
        self.kind = kind
        self._disc = disc
        self._t0 = start_mono
        self._win_start = start_mono
        self._cpu = time.process_time()
        self._sys = power.system_cpu_ticks()
        self._pulls: list[float] = []
        self.stall_count = 0
        self.stall_sec = 0.0
        # 本視窗的停擺量 = 現值 − 視窗起點的快照。不另存一份清單:那些
        # 元素值從來沒被用到(對比 _pulls 真的要算分位數),一個事件記
        # 三個地方只是多兩個要同步的欄位
        self._stall_mark = (0, 0.0)
        # 探針裝不上時停擺數會一直是 0,而「沒量到」與「沒發生」在分析時
        # 是兩件完全不同的事——不講清楚會把人導向錯的結論
        self.probed = True

    def chunk(self, seconds: float) -> None:
        """一次 _record_chunk 的耗時(由探針回呼,每次拉取會有好幾筆)。"""
        if seconds > _STALL_SEC:
            self.stall_count += 1
            self.stall_sec += seconds

    def pull(self, seconds: float) -> None:
        self._pulls.append(seconds)

    def maybe_report(self, now: float, written_sec: float, force: bool = False) -> None:
        """到點就記一筆,然後開始新視窗。

        force=True 用在收音結束時把最後那個不滿 30 秒的視窗倒出來——
        **出事最常發生在收尾那一段**(引擎正忙、使用者剛按停止),丟掉它
        等於每次都漏掉最有價值的一筆(2026-08-03 實測踩到:42 秒的重現
        跑出 2 次不連續、8 次裝置停擺,全落在最後 12 秒,而診斷一行都沒印)。

        診斷有任何閃失都只留 DEBUG:**錄音不能重來**,絕不讓一行統計把它
        弄斷(同 _install_chunk_probe 的取捨)。視窗一律往前推,失敗也不
        累積成長。"""
        if not force and now - self._win_start < _DIAG_INTERVAL_SEC:
            return
        hits = self._disc.take_window()
        now_sys = power.system_cpu_ticks()
        try:
            logger.debug(
                "收音診斷(%s軌):%s", _KIND_LABEL.get(self.kind, self.kind),
                " · ".join(self._fields(now, written_sec, hits, now_sys)),
            )
        except Exception:
            logger.debug("收音診斷組不出來", exc_info=True)
        self._win_start = now
        self._cpu = time.process_time()
        # _sys 先前漏了重設,全機% 因此一直是「從錄音開始的累計平均」而不是
        # 本視窗——叢發型負載在後段會被稀釋掉,正是這欄要抓的東西
        self._sys = now_sys
        self._stall_mark = (self.stall_count, self.stall_sec)
        self._pulls = []

    def _fields(self, now: float, written_sec: float, hits: list[float],
                now_sys: tuple[float, float] | None) -> list[str]:
        span = now - self._win_start
        gaps = [b - a for a, b in zip(hits, hits[1:])]
        parts = [
            f"{self._win_start - self._t0:.0f}~{now - self._t0:.0f} 秒",
            f"不連續 {len(hits)} 次",
        ]
        if gaps:
            parts.append(
                f"間隔中位數 {_pct(gaps, 0.5):.2f}/最長 {max(gaps):.2f} 秒"
            )
        n = self.stall_count - self._stall_mark[0]
        parts.append(
            f"裝置停擺 {n} 次共 {self.stall_sec - self._stall_mark[1]:.2f} 秒"
            if self.probed else "裝置停擺(探針未裝上,無資料)"
        )
        if self._pulls:
            parts.append(
                f"拉取 p50 {_pct(self._pulls, 0.5) * 1000:.0f}ms"
                f"/p99 {_pct(self._pulls, 0.99) * 1000:.0f}ms"
                f"/最大 {max(self._pulls) * 1000:.0f}ms"
            )
        parts.append(f"落後牆鐘 {now - self._t0 - written_sec:+.2f} 秒")
        # process_time 是跨執行緒總和,除以牆鐘 = 這段期間等於幾顆核心在忙。
        # 全機那個才看得到子行程(講者分析),見 power.system_cpu_ticks
        cpu = f"CPU 本行程 {(time.process_time() - self._cpu) / max(span, 1e-6):.2f} 核"
        if self._sys is not None and now_sys is not None:
            d_idle, d_all = now_sys[0] - self._sys[0], now_sys[1] - self._sys[1]
            if d_all > 0:
                cpu += f"/全機 {(1 - d_idle / d_all) * 100:.0f}%"
        parts.append(cpu)
        return parts


# ---------------------------------------------------------------------------
# 裝置解析


def find_loopback_mic():
    """回傳系統聲音(WASAPI loopback)擷取物件。

    優先挑「名稱等於預設喇叭」的 loopback——使用者聽到聲音的那個裝置
    才是要錄的;對不上就退回第一個 loopback(至少錄得到某個播放裝置,
    總比直接失敗好)。"""
    sc = _sc()
    try:
        mics = sc.all_microphones(include_loopback=True)
    except Exception as e:
        # 列舉本身失敗 = 不是「沒有播放裝置」而是問不到(COM/驅動層);
        # 原樣往上丟會變成 app 的「未預期的錯誤」。細節(HRESULT)走 DEBUG:
        # 黑視窗不得有裸英文 traceback(spec §8),要診斷的人拿的是紀錄檔
        logger.debug("列舉播放裝置失敗", exc_info=True)
        raise UserFacingError(
            "無法查詢 Windows 音訊裝置:請重試一次;若持續發生請重新啟動程式"
            "(詳細錯誤已寫進 logs 資料夾的紀錄檔)"
        ) from e
    loopbacks = [m for m in mics if m.isloopback]
    if not loopbacks:
        raise UserFacingError(
            "找不到可錄製電腦聲音的裝置:請確認 Windows 有可用的播放裝置"
            "(喇叭/耳機),或改用「現場會議」情境只錄麥克風"
        )
    try:
        default_name = sc.default_speaker().name
    except Exception:
        # 退回第一個 loopback 可能不是使用者正在聽的那個裝置(= 整場錄到
        # 錯的系統聲音),而這條路徑不會浮出任何訊息:至少在紀錄檔留一筆
        logger.debug("問不到預設喇叭,退回第一個 loopback", exc_info=True)
        return loopbacks[0]
    for m in loopbacks:
        if m.name == default_name:
            return m
    return loopbacks[0]


def _default_mic():
    """回傳預設麥克風擷取物件;沒有麥克風時以繁中訊息浮出。"""
    sc = _sc()
    try:
        mic = sc.default_microphone()
    except Exception as e:
        mic = None
        cause = e
    else:
        cause = None
    if mic is None:
        # 真的沒有裝置與「問不到」都走這條(soundcard 對兩者都是丟例外,
        # 只差 HRESULT),所以措辭要兩種都對得起來——2026-08-05 的
        # CO_E_NOTINITIALIZED 就是被寫死的「找不到麥克風」導去查音效設定的。
        # 同上:HRESULT 只進紀錄檔(exc_info=None 就只是不附 traceback)。
        # 在此之前這條路徑**什麼都沒留下**——2026-08-05 那次的紀錄檔從轉檔
        # 完成到使用者重開程式之間一行都沒有,完全無從判斷是不是裝置問題
        logger.debug("查詢預設麥克風失敗", exc_info=cause)
        raise UserFacingError(
            "找不到麥克風:請確認 Windows 已連接並啟用錄音裝置,"
            "或改用「只錄電腦聲音」情境;裝置正常卻仍失敗,"
            "請重新啟動程式並附上 logs 資料夾的紀錄檔"
        ) from cause
    return mic


# ---------------------------------------------------------------------------
# 寫檔


class _WavWriter:
    """16k 單聲道 16-bit PCM WAV,邊寫邊定期回填標頭長度欄位。

    wave 模組要 close() 才寫正確標頭,硬當機檔案即報 0 幀不可播;
    自寫標頭讓「上次回填之前的內容」在任何時刻都是合法 WAV。"""

    def __init__(self, path: Path, rate: int = TARGET_RATE):
        self.path = path
        self.rate = rate
        self.frames = 0
        self._f = open(path, "wb")
        self._f.write(self._header(0))
        self._f.flush()

    def _header(self, data_bytes: int) -> bytes:
        return b"RIFF" + struct.pack("<I", 36 + data_bytes) + b"WAVE" + \
            b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, self.rate, self.rate * 2, 2, 16) + \
            b"data" + struct.pack("<I", data_bytes)

    def write(self, pcm: np.ndarray) -> None:
        self._f.write(pcm.astype("<i2").tobytes())
        self.frames += len(pcm)

    def flush_header(self) -> None:
        pos = self._f.tell()
        self._f.seek(0)
        self._f.write(self._header(self.frames * 2))
        self._f.seek(pos)
        self._f.flush()

    def close(self) -> None:
        if not self._f.closed:
            self.flush_header()
            self._f.close()


# ---------------------------------------------------------------------------
# 單軌錄音


@dataclass(frozen=True)
class RecordedTrack:
    path: Path
    kind: str  # "mic" | "system"
    duration: float  # 秒(以實際寫入幀數計)


class _TrackRecorder:
    """一軌:worker 執行緒內開裝置、拉取、轉 int16、寫檔、斷流補零。"""

    def __init__(self, kind: str, mic, path: Path):
        self.kind = kind
        self.mic = mic
        self.path = path
        self._stop = threading.Event()
        self._opened = threading.Event()
        self._disc = _DiscontinuityLog(kind)
        self._error: Exception | None = None
        self._writer: _WavWriter | None = None
        self._thread: threading.Thread | None = None
        self._start_mono: float | None = None
        # 錄音期間補進去的零(斷流保險絲)+ 收尾補到牆鐘的那一段:兩者
        # 合計才是「這一軌總共少了幾秒真實聲音」,停止時一次報出來
        self._pad_frames = 0
        # 反向:從全零段裡拿掉的捏造靜音(見 _DRIFT_TRIM_SEC)。與補零分開
        # 記——一個是「少了真實聲音」、一個是「多了不存在的靜音」,兩件事
        # 的成因與嚴重性完全不同,加總只會把兩邊都變得看不懂
        self._trim_frames = 0
        self._diag: _CaptureDiag | None = None

    def start(self) -> None:
        # writer 在主執行緒先建:就算 worker 開裝置失敗,collect/close
        # 也有東西可收;失敗的空檔由 abort 清掉
        self._writer = _WavWriter(self.path)
        self._start_mono = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._opened.wait(timeout=_OPEN_TIMEOUT_SEC):
            self._stop.set()
            raise RuntimeError(f"開啟錄音裝置逾時({self.kind})")
        if self._error is not None:
            raise self._error

    def _run(self) -> None:
        writer = self._writer
        _disc_local.log = self._disc  # warn 替身依執行緒把掉幀記到本軌帳本
        try:
            # 開流也是 COM 呼叫,本執行緒自己初始化,不靠呼叫端那條會被
            # 回收的 anyio worker 撐著(見 _ensure_com)
            _ensure_com()
            with self.mic.recorder(
                samplerate=TARGET_RATE,
                channels=1,
                blocksize=int(_WASAPI_BUFFER_SEC * TARGET_RATE),
            ) as rec:
                self._opened.set()
                diag = _CaptureDiag(self.kind, self._disc, self._start_mono)
                self._diag = diag
                try:
                    diag.probed = _install_chunk_probe(rec, diag.chunk)
                    self._log_device(rec, diag.probed)
                except Exception:
                    # 診斷絕不能擋住錄音:裝不上就當沒有,照常錄
                    logger.debug("收音診斷初始化失敗", exc_info=True)
                    diag.probed = False
                last_flush = time.monotonic()
                while not self._stop.is_set():
                    t_pull = time.perf_counter()
                    data = rec.record(numframes=_PULL_FRAMES)
                    diag.pull(time.perf_counter() - t_pull)
                    pcm = np.clip(
                        data.reshape(-1) * 32767.0, -32768, 32767
                    ).astype(np.int16)
                    # 斷流保險絲:裝置停擺後恢復,先補零到牆鐘位置再寫
                    # 新資料(新資料屬於「現在」,時間軸才對得上)
                    now = time.monotonic()
                    expected = int((now - self._start_mono) * TARGET_RATE)
                    gap = expected - (writer.frames + len(pcm))
                    if gap > _DRIFT_PAD_SEC * TARGET_RATE:
                        writer.write(np.zeros(gap, dtype=np.int16))
                        self._pad_frames += gap
                    elif -gap > _DRIFT_TRIM_SEC * TARGET_RATE:
                        # 反向:檔案跑在牆鐘前面 = soundcard 捏造的靜音被
                        # 寫了進來。從全零段裡把多出來的拿掉(見
                        # _DRIFT_TRIM_SEC)。一次最多只拿得到這一批的量,
                        # 追不完就下一批繼續——是收斂而不是一次修完
                        pcm, dropped = _trim_zero_runs(pcm, -gap)
                        self._trim_frames += dropped
                    writer.write(pcm)
                    diag.maybe_report(now, writer.frames / TARGET_RATE)
                    if now - last_flush >= _HEADER_FLUSH_SEC:
                        writer.flush_header()
                        last_flush = now
                self._drain(writer, rec)
                # 收尾那一段最容易出事,不能讓它隨著不滿一個視窗而消失
                diag.maybe_report(
                    time.monotonic(), writer.frames / TARGET_RATE, force=True,
                )
        except Exception as e:
            # 開裝置失敗(start 同步浮出)與錄到一半裝置消失都落在這;
            # 後者檔案保留到最後寫入點,絕不因裝置問題丟掉已錄內容
            self._error = e
            logger.warning("錄音執行緒中止(%s)", self.kind, exc_info=True)
        finally:
            _disc_local.log = None
            self._opened.set()  # start() 不得因開裝置即噴例外而卡死

    def _drain(self, writer: "_WavWriter", rec) -> None:
        """停止後把還躺在 WASAPI 緩衝裡的音訊拉完。

        **緩衝開到 30 秒是為了扛住 GIL 阻塞**(見 _WASAPI_BUFFER_SEC),
        代價是按下停止的當下,最後最多那麼多秒的聲音還沒被讀出來。不排空
        就等於**每一場會議的結尾都被剪掉一段**,再由 stop() 補上等長的
        靜音——而散會前的結論正是最不該丟的那一段。

        終點是「寫到按下停止那一刻的牆鐘長度」,不是「把緩衝讀到空」:
        後者在裝置持續送資料時永遠到不了。_DRAIN_TIMEOUT_SEC 是保險絲,
        排空本身只是記憶體搬運,會超時只可能是又撞上一次 GIL 阻塞。
        必須在 worker 執行緒內做(WASAPI 用戶端有執行緒親和)。"""
        if self._start_mono is None:
            return
        target = int((time.monotonic() - self._start_mono) * TARGET_RATE)
        deadline = time.monotonic() + _DRAIN_TIMEOUT_SEC
        before = writer.frames
        while writer.frames < target and time.monotonic() < deadline:
            data = rec.record(
                numframes=min(_PULL_FRAMES, target - writer.frames))
            writer.write(
                np.clip(data.reshape(-1) * 32767.0, -32768, 32767).astype(np.int16))
        pulled = (writer.frames - before) / TARGET_RATE
        if pulled > 0.5:
            logger.info(
                "停止後自緩衝補回 %.1f 秒(%s軌)", pulled,
                _KIND_LABEL.get(self.kind, self.kind),
            )

    def _log_device(self, rec, probed: bool) -> None:
        """開流當下記下「錄的是哪支裝置、用什麼參數」。

        掉幀成因高度綁裝置——筆電內建陣列麥要經過 DSP、降噪/波束成形 APO
        (Copilot+ 機器還有跑在 NPU 上的語音焦點),行為與 USB 麥克風完全
        不同,事後分析沒有這幾行就無從比對。device period 同時是 _STALL_SEC
        這個判準的真值來源;取不到只是少一欄,不影響錄音。"""
        try:
            period = rec.deviceperiod
        except Exception:
            period = None
        logger.info(
            "收音裝置(%s軌):%s", _KIND_LABEL.get(self.kind, self.kind),
            getattr(self.mic, "name", "(不明)"),
        )
        logger.debug(
            "  參數:%d Hz 單聲道、WASAPI 緩衝 %.1f 秒、每次拉 %d 幀(%.2f 秒)、"
            "device period %s、停擺探針%s",
            TARGET_RATE, _WASAPI_BUFFER_SEC, _PULL_FRAMES,
            _PULL_FRAMES / TARGET_RATE, period,
            "已裝上" if probed else "裝不上(soundcard 換過實作)",
        )

    def _log_accounting(self, wall_sec: float, tail_pad: int) -> None:
        """收檔時把這一軌的音訊帳算給人看:牆鐘多長、真的收到多少、補了多少零。

        **補零總量就是掉幀的真實損失量**,而它先前從來沒有被記下來過:錄音中
        的保險絲門檻是 _DRIFT_PAD_SEC(5 秒),少於 5 秒的落差整場都不會有人
        提;收尾這一段更是靜靜補完就收檔。黑視窗那個「累計 N 次」回答不了
        「所以到底掉了幾秒」——619 次 × 每次 10ms 和 × 每次 300ms 是完全
        不同量級的災情,而修法取決於此。

        **報檔案的真實長度,不要報推導值**:先前這行印的是「牆鐘 - 補零」,
        而那在檔案**比牆鐘長**的時候完全蓋掉了問題——2026-08-03 實測一輪
        牆鐘 726.8 秒、檔案 907.6 秒(soundcard 誤判裝置靜音,插了 181 秒
        幻影靜音把時間軸整個往後推),那行卻報「實際收到 726.8 秒」。
        兩個方向都要看得見:短了是掉音訊,長了是時間軸壞掉。

        **修剪量要單獨報**(2026-08-04):它是「本來會多出來、已經被拿掉」
        的量,跟補零(本來就少了)是相反的兩件事。合在一起看的話,一場
        修得很成功的錄音會長得跟一場沒事的錄音一模一樣——而那正是需要
        知道「裝置在停擺」的時候。

        真的出事就用 WARNING(壞消息不得只躺在 DEBUG 裡),否則 INFO。

        ⚠️ **牆鐘與檔案的落差要用比例判,不能用固定秒數**(2026-08-15 從
        執行紀錄查出來):裝置的取樣時鐘跟系統牆鐘本來就差個 0.05% 上下,
        那是晶振不是故障——9120 秒的錄音實測差 +5.6 秒,固定 1 秒的門檻
        等於**每一場長錄音都報警**,而那幾場補零 0 秒、掉幀 5 次,乾淨得
        很。示警一旦變成常態就會被當背景音,真的出事那次也就沒人看了
        (同 test_clean_recording_does_not_cry_wolf 守的那件事)。補零那半
        維持絕對值:補零是實打實的損失,一秒就是一秒。"""
        pad_sec = (self._pad_frames + max(0, tail_pad)) / TARGET_RATE
        trim_sec = self._trim_frames / TARGET_RATE
        file_sec = self._writer.frames / TARGET_RATE
        stalls = ""
        if self._diag is not None and self._diag.probed and self._diag.stall_count:
            stalls = (f",其中裝置交不出資料 {self._diag.stall_count} 次"
                      f"共 {self._diag.stall_sec:.1f} 秒")
        line = ("錄音收檔(%s軌):牆鐘 %.1f 秒、檔案 %.1f 秒(%+.1f)、"
                "補零 %.1f 秒、修掉捏造的靜音 %.1f 秒%s")
        args = (_KIND_LABEL.get(self.kind, self.kind), wall_sec, file_sec,
                file_sec - wall_sec, pad_sec, trim_sec, stalls)
        drift_tol = max(_DRIFT_ABS_TOL_SEC, wall_sec * _DRIFT_REL_TOL)
        bad = pad_sec >= _PAD_TOL_SEC or abs(file_sec - wall_sec) >= drift_tol
        (logger.warning if bad else logger.info)(line, *args)

    def stop(self) -> RecordedTrack:
        end_mono = time.monotonic()
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=_STOP_JOIN_SEC)
            self._disc.summary()  # 壞消息在收檔時一次講清楚,不只靠錄音中的節流示警
            if self._thread.is_alive():
                # 拉取卡死(裝置層停擺):不能碰 writer(執行緒可能還握著),
                # 檔案有效長度以最後一次標頭回寫為準
                logger.warning("錄音執行緒未在時限內結束(%s),檔案保留至最後回寫點", self.kind)
                return RecordedTrack(self.path, self.kind, self._writer.frames / TARGET_RATE)
        # 正常結束:補零至牆鐘等長——兩軌等長,收尾的雙聲道合成與
        # 跨軌時間戳對照才成立
        if self._start_mono is not None:
            expected = int((end_mono - self._start_mono) * TARGET_RATE)
            tail_pad = expected - self._writer.frames
            if tail_pad > 0:
                self._writer.write(np.zeros(tail_pad, dtype=np.int16))
            self._log_accounting(end_mono - self._start_mono, tail_pad)
        self._writer.close()
        return RecordedTrack(self.path, self.kind, self._writer.frames / TARGET_RATE)

    def abort(self) -> None:
        """start 到一半失敗時的靜默收拾:停執行緒、收檔,不回傳結果。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=_STOP_JOIN_SEC)
        if self._writer is not None and (self._thread is None or not self._thread.is_alive()):
            self._writer.close()


# ---------------------------------------------------------------------------
# 對外介面


class Recorder:
    """單場錄音的生命週期:start() → 錄音中(elapsed())→ stop()。

    使用方(app 層)負責:單一實例狀態機、keep_awake、錄音目錄管理。"""

    def __init__(self, out_dir: Path, scenario: str, stem: str = "錄音"):
        if scenario not in SCENARIO_TRACKS:
            raise ValueError(f"未知情境:{scenario}")
        self.out_dir = out_dir
        self.scenario = scenario
        self.stem = stem
        self._tracks: list[_TrackRecorder] = []
        self._start_mono: float | None = None

    def start(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        kinds = SCENARIO_TRACKS[self.scenario]
        # 裝置解析先於開流:缺裝置要在按下錄音的當下就以繁中浮出,
        # 且不留半開的軌
        mics = {
            kind: (_default_mic() if kind == "mic" else find_loopback_mic())
            for kind in kinds
        }
        suffix = {"mic": "mic", "system": "sys"}
        kind = kinds[0]
        try:
            for kind in kinds:
                tr = _TrackRecorder(
                    kind, mics[kind], self.out_dir / f"{self.stem}.{suffix[kind]}.wav"
                )
                self._tracks.append(tr)
                tr.start()
        except Exception as e:
            for tr in self._tracks:
                tr.abort()
            self._tracks = []
            src = "麥克風" if kind == "mic" else "電腦聲音"
            raise UserFacingError(
                f"無法開始錄音({src}):裝置可能被其他程式獨占或已停用,"
                "請檢查 Windows 音效設定後重試"
            ) from e
        self._start_mono = time.monotonic()

    def elapsed(self) -> float:
        """已錄秒數(牆鐘):UI 計時之用。"""
        return 0.0 if self._start_mono is None else time.monotonic() - self._start_mono

    def recorded_paths(self) -> list[Path]:
        """錄音中各軌檔案路徑(增量轉錄排程器讀取用)。"""
        return [tr.path for tr in self._tracks]

    def track_files(self) -> dict[str, Path]:
        """{軌別: 檔案路徑}(kind 與 RecordedTrack.kind 同值域):
        app 據此把各軌註冊給增量轉錄排程器(live.add_track 同鍵)。"""
        return {tr.kind: tr.path for tr in self._tracks}

    def stop(self) -> list[RecordedTrack]:
        tracks = [tr.stop() for tr in self._tracks]
        self._tracks = []
        return tracks
