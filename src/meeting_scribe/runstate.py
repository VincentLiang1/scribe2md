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
import collections
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
# 進度低於這個比例時不估剩餘時間。⚠️ **這個門檻 2026-08-15 由 2% 提到 10%,
# 是拿 259 份執行紀錄回頭算出來的**:把每一次「預估還要 N 分」跟它之後真正
# 完成的時間配對,96 組**全部高估、沒有一次低估**,而最離譜的都出在剛起步
# ——一支 3 小時 59 分的月會在已花 8.2 分時報「還要 404 分」(6.7 小時),
# 實際只剩 65 分。成因不是算錯,是**那個階段的速率不代表全程**:固定開銷
# (子行程啟動、模型載入、VAD;該場實測 2.3 分)全被算進速率裡,而且轉錄
# 與講者分析在搶 CPU,轉錄一跑完講者分析就快四倍。前一成的資料**推不出**
# 後面九成要多久,給了只會讓人關掉程式——那正是這條門檻本來要防的事
_ETA_MIN_FRAC = 0.10
# 速率取樣視窗:ETA 用「最近這段時間跑多快」而不是「從頭到現在的平均」。
# 平均的毛病是它永遠帶著起步那段的固定開銷,而且**追不上速率的階躍**
# (轉錄跑完那一刻,講者分析實測由 1%/分 跳到 4~6%/分)。視窗取已花時間的
# 四分之一,並夾在 2~5 分鐘之間:下限讓短檔也有足夠跨度,上限讓長檔的
# 視窗不會長到把階躍稀釋掉(實測長檔用 17 分鐘的視窗會回頭高估三倍)
_ETA_WINDOW_SHARE = 0.25
_ETA_WINDOW_MIN_SEC = 120.0
_ETA_WINDOW_MAX_SEC = 300.0
# 取樣間隔:引擎每秒回呼十幾次,每次都存只是讓佇列變長,速率不會更準
_ETA_SAMPLE_GAP_SEC = 5.0
# 視窗跨度不足就不估:剛 begin 完的一兩秒內,分母小到什麼數字都算得出來
_ETA_MIN_SPAN_SEC = 30.0


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
        """整體完成度(算法見 `_overall`)。"""
        return _overall(self.kind, self.stages, self.frac)


def _overall(kind: str, stages: dict[str, float], frac: float) -> float:
    """整體完成度。

    分階段時取**各階段的最小值而不是加權平均**:兩段平行跑,轉錄先
    跑完不代表快好了——總時間由較慢的講者分析決定(CLAUDE.md:總時間
    ≈ max(兩者))。取平均會讓進度條衝到 70% 然後卡住一小時,那比沒有
    進度條更擾民。批次/收尾則直接用回報進來的比例。

    **寫成模組函式而不是只有 Snapshot 的 property**:速率取樣要在鎖內、
    還沒建出 Snapshot 的時候就算出同一個數字,而兩邊各寫一份的話,改了
    這裡的規則之後 ETA 會安靜地拿另一套完成度去外插。"""
    if kind != KIND_FILE:
        return frac
    return min(stages.values()) if stages else 0.0


_lock = threading.Lock()
_state: dict = {}


def begin(label: str, kind: str = KIND_FILE) -> None:
    """開始一件工作(label 供畫面顯示)。重複呼叫以最後一次為準。

    `kind` 決定進度怎麼呈現、以及開頁時誰負責把畫面接回來(見上方常數)。"""
    with _lock:
        now = time.monotonic()
        _state.clear()
        _state.update(
            label=label or "",
            stages={},
            kind=kind,
            note="",
            frac=0.0,
            started=now,
            last_beat=now,
            # 速率取樣序列 [(時刻, 完成度)]。起點記一筆 0%,短檔才不必等
            # 滿一個視窗才有得比
            samples=collections.deque([(now, 0.0)]),
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
        _sample_locked(now)
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
        _sample_locked(now)
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


def _sample_locked(now: float) -> None:
    """把「此刻到幾成」記進取樣序列,並把滑出視窗的舊樣本丟掉。

    ⚠️ **視窗外的最後一筆要留著**:序列頭就是拿來當比較基準的,照 cutoff
    一路刪到底的話,視窗會塌成「最近 5 秒」,速率會隨引擎的回呼節奏抖到
    沒有意義。所以只在**第二筆**也出了視窗時才丟第一筆。"""
    samples = _state["samples"]
    done = _overall(_state["kind"], _state["stages"], _state["frac"])
    if done < samples[-1][1]:
        # ⚠️ **完成度倒退就整個重設基準**:`overall` 取各階段的最小值,而
        # 兩段引擎不是同時開始回報的——轉錄先報 36%、講者分析零點幾秒後
        # 才報 3%,那一刻 overall 從 36% 掉到 3%。留著倒退前的樣本會讓
        # 速率算成負的,ETA 就此消失一大段(拿真實紀錄重放時抓到:某場
        # 因此在最需要的那幾分鐘完全不報數字)。批次與收尾的 frac 本來
        # 就可能重來,同理。
        samples.clear()
        samples.append((now, done))
        return
    # ⚠️ 節流是 append 的節流,**不是更新末筆**:序列頭是唯一的比較基準,
    # 而 begin 記的那筆起點正好也是末筆——覆蓋它等於把基準搬到現在,
    # 短檔的 ETA 會整段消失(span 永遠是 0)
    if now - samples[-1][0] >= _ETA_SAMPLE_GAP_SEC:
        samples.append((now, done))
    window = min(
        max((now - _state["started"]) * _ETA_WINDOW_SHARE, _ETA_WINDOW_MIN_SEC),
        _ETA_WINDOW_MAX_SEC,
    )
    cutoff = now - window
    while len(samples) > 1 and samples[1][0] <= cutoff:
        samples.popleft()


def _eta_locked(now: float, done: float) -> float | None:
    """預估剩餘秒數;資料不足以講一個像樣的數字就回 None(不猜)。

    速率取自視窗頭尾兩筆,不取整體平均——理由見 `_ETA_MIN_FRAC` 與
    `_ETA_WINDOW_SHARE` 的註解(兩者缺一不可:視窗修的是「速率會變」,
    門檻修的是「前一成根本推不出後面九成」)。"""
    if done < _ETA_MIN_FRAC or done >= 1.0:
        return None
    t0, d0 = _state["samples"][0]
    span, gained = now - t0, done - d0
    if span < _ETA_MIN_SPAN_SEC or gained <= 0:
        return None
    return (1.0 - done) * span / gained


def _snapshot_locked() -> Snapshot:
    now = time.monotonic()
    elapsed = now - _state["started"]
    snap = Snapshot(
        _state["label"], dict(_state["stages"]), elapsed, None,
        _state["kind"], _state["note"], _state["frac"],
    )
    return snap._replace(eta=_eta_locked(now, snap.overall))


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
