r"""轉檔進行中的狀態:讓「畫面」與「執行」脫鉤。

**為什麼要這個**(使用者 2026-08-08 實際踩到):轉檔進度原本只活在
`gr.Progress` 裡,而 `gr.Progress` 綁在**那一次事件執行**上——瀏覽器
把背景分頁的計時器節流、`RECONNECT_HEAD` 判定成「連線可能已中斷」、
使用者照著提示按下「重新連線」(= `location.reload()`),新頁面就再也
接不回那條進度。畫面回到剛開啟的樣子:檔名沒了、進度沒了,**連停止鈕
都沒了**——而轉檔其實還在後端跑。想中止只能關黑視窗,等於整場重來
(當次是一支 3 小時 59 分的月會錄音,已經轉了 43 分鐘)。

錄音那條路 2026-07-18 就用 `_restore_recording` 解決了同一件事(錄音在
伺服器端持續跑,開頁重新接回畫面),轉檔漏了對應的一份。這個模組就是
轉檔的那一份:**唯一的真相在伺服器端**,畫面隨時可以重新長出來。

設計同 `cancel.py`——模組級單例,不依賴 gradio、不依賴任何 session。
`pipeline` 只管回報,要不要畫、畫成什麼樣是 `app` 的事。

**心跳 log 也由這裡發**(CLAUDE.md:任何可能跑超過一分鐘的迴圈都要有
心跳,否則黑視窗的沉默會被當成當機)。講者分析是全流程最慢的一段
(4 小時錄音要跑 1~3 小時),先前從頭到尾只印一行「子行程已就緒」——
使用者看著安靜的黑視窗,唯一的判斷依據只剩「電腦順不順」,而那跟
「轉檔有沒有在跑」根本是兩回事。進度資料本來就會流經這裡,順手印出來
比另外拉一條心跳便宜,也不可能與畫面上的數字對不起來。
"""
import logging
import threading
import time
from typing import NamedTuple

logger = logging.getLogger(__name__)

# 兩個階段的顯示名稱。鍵沿用 pipeline._transcribe_and_diarize 的 fracs
# ——那裡本來就分開記,只是合成一個數字才丟給 gr.Progress
STAGE_LABELS = {
    "transcribe": "轉錄",
    "diarize": "講者分析",
}

# 進行中的工作屬於哪一條路。決定兩件事:進度**怎麼呈現**,以及開頁時
# **誰負責把畫面接回來**(檔案/批次歸 `_restore_transcribing`,錄音收尾
# 歸 `_restore_recording`——收尾畫面長在錄音那一側,鈕也是那一組)。
KIND_FILE = "file"            # 單檔轉檔:轉錄與講者分析兩段各自的進度
KIND_BATCH = "batch"          # 多檔/資料夾:逐檔進度,「(3/12) 檔名」
KIND_RECORDING = "recording"  # 現場收音按下停止之後的收尾
# 心跳 log 間隔:60 秒。取這個值是因為它要對付的是「安靜一到兩小時」,
# 不是秒級的診斷;印太密反而會把黑視窗洗成瀑布,真正的錯誤訊息會被沖走
_HEARTBEAT_SEC = 60.0
# 進度低於這個比例時不估剩餘時間:剛起步的 elapsed/frac 會外插出荒謬的
# 數字(實測第一次回報常在 0.3% 上下,推出來是好幾個小時),而使用者看到
# 「預估還要 8 小時」的第一反應是關掉程式
_ETA_MIN_FRAC = 0.02


class Snapshot(NamedTuple):
    """畫面重建所需的一切(給 app 的 Timer 讀)。"""

    label: str                  # 正在處理的檔名(畫面上要看得到「我的檔案還在」)
    stages: dict[str, float]    # {階段鍵: 完成比例}(KIND_FILE)
    elapsed: float              # 已經跑了幾秒
    eta: float | None           # 預估剩餘秒數;資料不足時 None(不猜)
    kind: str = KIND_FILE       # 這是哪一條路的工作(見上方常數)
    note: str = ""              # 自由描述的進度,如「(3/12) 月會.m4a」
    frac: float = 0.0           # 那段描述對應的完成比例(batch/recording)

    @property
    def detailed(self) -> bool:
        """分階段進度有沒有意義。

        只有單檔轉檔有:批次每個檔案都重跑一遍 pipeline,而 `update` 只增
        不減,第二個檔案的「轉錄 5%」蓋不掉第一個留下的 100%,整批會卡在
        滿格;錄音收尾走的是 live 那條路,根本不經過 `_transcribe_and_diarize`。
        那兩種改用 `note` 呈現。"""
        return self.kind == KIND_FILE

    @property
    def overall(self) -> float:
        """整體完成度。

        分階段時取**各階段的最小值而不是加權平均**:兩段平行跑,轉錄先
        跑完不代表快好了——總時間由較慢的講者分析決定(CLAUDE.md:總時間
        ≈ max(兩者))。取平均會讓進度條衝到 70% 然後卡住一小時,那比沒有
        進度條更擾民。批次/收尾則直接用回報進來的比例。"""
        if not self.detailed:
            return self.frac
        if not self.stages:
            return 0.0
        return min(self.stages.values())


_lock = threading.Lock()
_state: dict = {}


def begin(label: str, kind: str = KIND_FILE) -> None:
    """開始一件工作(label 供畫面顯示)。重複呼叫以最後一次為準。

    `kind` 決定進度怎麼呈現、以及開頁時誰負責把畫面接回來(見上方常數)。"""
    with _lock:
        _state.clear()
        _state.update(
            label=label or "",
            stages={},
            kind=kind,
            note="",
            frac=0.0,
            started=time.monotonic(),
            last_beat=time.monotonic(),
        )


def update(stage: str, frac: float) -> None:
    """回報某階段的完成比例(0~1)。

    只增不減:兩條執行緒同時回報時,晚到的舊值不得把進度往回拉
    (同 pipeline.sub_progress 的 max 語意)。"""
    beat = None
    with _lock:
        if not _state:
            return  # 沒有進行中的轉檔(例如錄音收尾走的是另一條路)
        stages = _state["stages"]
        stages[stage] = max(stages.get(stage, 0.0), min(max(frac, 0.0), 1.0))
        now = time.monotonic()
        if now - _state["last_beat"] >= _HEARTBEAT_SEC:
            _state["last_beat"] = now
            beat = _describe(_snapshot_locked())
    # log 放在鎖外:寫檔案的 handler 可能卡住,不能連帶擋住兩條引擎的回報
    if beat is not None:
        logger.info("轉檔進行中:%s", beat)


def note(text: str, frac: float) -> None:
    """回報一段自由描述的進度(批次的「(3/12) 檔名」、收尾的階段名)。

    批次與錄音收尾沒有「轉錄/講者分析」兩段可分:批次是逐檔重跑,收尾走
    live 那條路。它們回報的本來就是一句話加一個比例,原樣留著就好——
    `docpipe` 已經把「第幾個/共幾個 + 檔名」組進那句話裡了。

    frac 這裡**不做只增不減**:批次的比例本來就是單調的,而收尾的階段
    比例可能在階段之間重來一次,壓成單調反而會卡住。"""
    beat = None
    with _lock:
        if not _state:
            return
        _state["note"] = text or ""
        _state["frac"] = min(max(frac, 0.0), 1.0)
        now = time.monotonic()
        if now - _state["last_beat"] >= _HEARTBEAT_SEC:
            _state["last_beat"] = now
            beat = _describe(_snapshot_locked())
    if beat is not None:
        logger.info("轉檔進行中:%s", beat)


def end() -> None:
    """本次工作結束(完成/失敗/停止都要呼叫)。"""
    with _lock:
        _state.clear()


def active() -> bool:
    """現在有沒有轉檔正在跑。

    這是**互斥檢查的判準**,不能改用 app 的 `_transcribing` 旗標:那面
    旗在鏈頭 `_start_run` 就舉起來了,而互斥檢查在第二步 `_run`——拿它
    來擋等於每次都擋到自己。這裡的真相是「pipeline 真的在跑」。"""
    with _lock:
        return bool(_state)


def snapshot() -> Snapshot | None:
    """目前狀態;沒有進行中的轉檔回 None。"""
    with _lock:
        return _snapshot_locked() if _state else None


def _snapshot_locked() -> Snapshot:
    elapsed = time.monotonic() - _state["started"]
    snap = Snapshot(
        _state["label"], dict(_state["stages"]), elapsed, None,
        _state["kind"], _state["note"], _state["frac"],
    )
    done = snap.overall
    if done >= _ETA_MIN_FRAC and elapsed > 0:
        snap = snap._replace(eta=elapsed / done * (1.0 - done))
    return snap


def _describe(snap: Snapshot) -> str:
    """心跳 log 那一行(也給畫面當文字用,兩邊講的話才會一致)。"""
    if snap.detailed:
        parts = [
            f"{STAGE_LABELS.get(k, k)} {snap.stages[k] * 100:.0f}%"
            for k in STAGE_LABELS if k in snap.stages
        ]
        text = "、".join(parts) if parts else "準備中"
    else:
        text = snap.note or "進行中"
        if snap.frac > 0:
            text += f"({snap.frac * 100:.0f}%)"
    text += f",已花 {snap.elapsed / 60:.1f} 分"
    if snap.eta is not None:
        text += f",預估還要 {snap.eta / 60:.0f} 分"
    return text


def describe() -> str | None:
    """目前進度的一句話(畫面與心跳 log 共用);沒在轉檔回 None。"""
    snap = snapshot()
    return _describe(snap) if snap is not None else None
