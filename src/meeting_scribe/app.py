r"""Gradio 網頁介面(工具的進入點):版面接線、事件流程與按鈕狀態機。

模組分工(app.py 只留「接線與狀態」,內容各歸其所):
- ui_style.py   Apple 風 theme/CSS/前端 JS 常數
- help_text.py  「使用說明」分頁長文
- pending.py    命名進度落地(睡眠/斷線後重新整理可接續)
- data_tabs.py  「名單與聲紋」「領域詞表」「用詞替換表」三個資料分頁的處理函式
- doctab.py     「文字、圖像→MD」分頁的處理函式(docpipe 是它的管線)
- paths.py      %LOCALAPPDATA%\meeting-scribe 落地根目錄
- pipeline.py   實際的轉檔管線(本檔只負責呼叫與呈現)

本檔閱讀地圖(由上而下):
1. 常數與參數正規化(_normalize_* 系列、_apply_cpu_cores)
2. 試聽片段(_cut_speaker_clips:剪每位講者「最長一句」的原音)
3. 命名草稿(_save_draft_names → pending.update_names)
4. 檔案轉檔流程(路徑輸入、一次一檔):_run(把關 → run_pipeline)→
   _present_result(轉檔成果 → 整組 UI 更新)、_restore_pending
5. 套用與復位:_apply_names、_end_of_job_updates、_page_reset_updates
6. 現場收音流程:_start_recording / _rec_tick / _finish_recording 系列
7. build_ui():版面與事件接線(元件關係、事件鏈與 gradio 地雷註解都在這)
8. main():launch()——theme/css/head/allowed_paths 都必須在此傳入;
   quiet=True 關掉 gradio 的英文輸出(含 share 廣告),網址自己印繁中

主要事件鏈(接線於 build_ui;.then/.failure 成對的理由見接線處註解):
- 開始轉檔:_start_run(鎖介面+整頁復位)→ _run → _after_run
- 停止:_request_stop(協作式取消,見 cancel.py)
- 開始錄音:_reset_for_new_recording → _start_recording;
  停止錄音:_lock_for_rec_finish → _finish_recording → _after_rec_finish
- 套用名字:_apply_names → js 自動下載 → 清下載區
- 文字、圖像→MD:_doc_start(鎖介面)→ _doc_convert → _doc_after
"""
import os
import tempfile
from pathlib import Path

# 隱私規格(spec §7):必須在 import gradio 之前設定,否則遙測已初始化
# (與套件 __init__ 同一組,保 `python app.py` 直跑;setdefault 冪等)
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault(
    "GRADIO_TEMP_DIR",
    str(Path(tempfile.gettempdir()) / f"meeting-scribe-serve-{os.getpid()}"),
)

import functools
import logging
import shutil
import socket
from datetime import datetime

import gradio as gr

from meeting_scribe import (
    export,
    audit as audit_mod,
    attendees,
    audio,
    cancel,
    convert,
    data_tabs,
    diarize,
    docpipe,
    docsrc,
    doctab,
    filelog,
    help_text,
    hotwords,
    models,
    paths,
    pending,
    power,
    punctuate,
    relabel,
    runstate,
    srcfile,
    transcribe,
    transproc,
    ui_style,
)
from meeting_scribe import live as live_scribe
from meeting_scribe import pipeline
from meeting_scribe import record
from meeting_scribe import voiceprints as voiceprints_store
from meeting_scribe.errors import UserFacingError
from meeting_scribe.pipeline import cleanup_stale_temp, run_pipeline
from meeting_scribe.types import MAX_SPEAKERS, UNKNOWN_SPEAKER, SpeechBlock

logger = logging.getLogger(__name__)

# 已「固定化」、不再有 UI 控件的設定(使用者 2026-07-26 逐項指定移除;
# 實作與取捨見 git 歷史,勿在無新指示下加回):
# - 語音語言:固定中文(引擎寫死 zh)。代價:英文音檔會被強制往中文
#   解碼成品質不穩的「翻譯」,同事若有英文影片需求再依歷史還原
# - 輸出格式:固定 md(曾有 md/txt/srt 勾選)
# - 講者辨識:一律執行(曾有開關,並依檔案類型帶預設)
# 唯一保留可調的是「模型」快速/精準——無 GPU 機器上精準慢約 4 倍,
# 工具要給同事用,不能沒有退路(使用者裁決 2026-07-26,結案勿再拆)
# 「🎧 轉檔」分頁的三種工作模式(使用者 2026-08-06 指定把「重設講者」放這裡,
# 而不是另開子分頁)。原本這顆 radio 只有檔案/收音兩項、標籤是「音訊來源」——
# 「重設講者」的來源是一份現成的 md、不是音訊,所以標籤跟著改成「要做什麼」。
# 「轉錄音檔」曾叫「上傳檔案」、再叫「現成檔案」(使用者 2026-08-06 定名:
# 三項要用同一種「做什麼」的說法,講「來源是什麼」跟另兩項對不起來)。
# **順序跟著預設值走**(收音排第一,同日指定):預設就是收音,擺中間會
# 變成「打開來選中的在中間」。字串改動只此一處,測試連順序一起鎖。
# **前面掛 emoji 圖示**(使用者 2026-08-08 指定,同一批把「收音情境」也加上):
# 圖示與「❓ 使用說明」那三處**刻意相同**——🎙️/🎧 就是那兩篇的圖示、
# 🔄 是「事後補命名」那個小標的,使用者在畫面上認的形狀與說明書裡一致。
# 用 emoji 不用 SVG,同 2026-08-04 定的分頁命名規則。
# ⚠️ 這幾個字串同時是**值**(存進設定檔、比對模式),不只是顯示文字
_MODE_RECORD = "🎙️ 現場收音"
_MODE_FILE = "🎧 轉錄音檔"
_MODE_RELABEL = "🔄 重設講者"

# 那顆 radio 的 info(說明小字)**跟著選中的模式換**(使用者 2026-08-06 回報:
# 原本固定寫著「重設講者=…」,停在別的模式時等於把錯的說明擺在最顯眼處)。
# 每段**壓在一行內**:三段長短差太多的話,切模式時下面的路徑欄與按鈕會上下
# 跳。實測(gradio 6.20 + Playwright,最小重現見 docs/dev/verification.md):
# `gr.update(info=…)` 對 Radio 有效、只帶 info 不會動到選中值,而只帶
# interactive 的更新(鎖定/解鎖那批)也不會把 info 洗回建構時的值
_MODE_INFO = {
    _MODE_RECORD: "開會時直接收音,邊開會邊轉,散會幾乎不用等",
    _MODE_FILE: "挑已經錄好的錄音/錄影檔,轉成逐字稿",
    _MODE_RELABEL: "拿一份已經轉好的逐字稿,重新命名裡面的講者(不會重轉)",
}

MODEL_LABELS = {"快速": "fast", "精準": "accurate"}
# 反查(模型鍵 → 介面標籤):transcribe.default_model_key 回的是鍵
MODEL_KEYS = {v: k for k, v in MODEL_LABELS.items()}

# 選檔區的模式說明(使用者 2026-08-06 指定要寫在介面上,同日再指定精簡並
# 移到選檔鈕之下)。**兩種模式的差別必須在選檔當下就講清楚**:使用者是在
# 這裡決定要不要一次丟一批的,而「這批不會讓你命名講者」如果等到 30 分鐘
# 後才發現,那 30 分鐘就白花了。判準見 srcfile.looks_like_batch。
# **只留兩種模式的關鍵差異**(命名與成品位置):試聽、自動下載、「要重轉
# 請先刪掉舊 md」這些細節在 README 與「使用說明」分頁裡,擺在主流程上只會
# 讓人跳過整段不讀
_SRC_MODE_HINT = (
    "**選 1 個檔案**:轉完可替講者命名(記住聲紋),成品在 `output`。  \n"
    "**選多檔或資料夾**:整批連轉、**不做命名**(只標「講者 1／2／3」),"
    "成品是原檔旁的 `<原檔名>.md`,已有同名 md 就跳過。"
)
# 整段沒聽到人說話時貼在預覽最前面(2026-08-15,與「左欄整組消失」同一批修:
# 使用者用「只錄電腦聲音」錄了一段沒有人聲的音)。**空的逐字稿一定要有一句
# 解釋**——不講的話畫面上就只有一行標題,而那看起來跟程式壞掉一模一樣。
# 錄音與轉檔案共用同一句(兩邊的成因不同,但「先檢查聲音來源」是同一件事)
# ⚠️ **預覽區是 `gr.Textbox` 不是 Markdown**:寫 `**粗體**` 只會讓星號原樣
# 印在畫面上(Playwright 實拍抓到),所以這裡一律純文字,語氣與同區的
# 「已依要求停止,逐字稿未完成。」那幾句一致
_NO_SPEECH_NOTE = (
    "這一段聲音裡沒有聽到任何人說話,所以逐字稿是空的,也沒有講者可以命名。\n"
    "常見原因:收音時電腦其實沒有在出聲、麥克風被靜音或選錯裝置。"
    f"音檔已經存起來了——確認聲音沒問題之後,可以用「{_MODE_FILE}」把它重轉一次。"
)
DEVICE_NAMES = {"cuda": "NVIDIA GPU", "intel-gpu": "Intel GPU", "cpu": "CPU"}
# 「本機偵測」只報**實際在算的**裝置:標準機還有一顆 NPU(AI Boost),
# 但本專案沒有任何運算跑在上面,列出來會讓人以為它在幫忙
# (曾短暫顯示「Intel NPU+GPU」,2026-08-03 使用者以「完全沒有使用」為由
#  指定退回;轉錄搬上 NPU 的實測結論見 scripts/bench_npu.py)
# 講者人數上限在 types.MAX_SPEAKERS(與 diarize 的自動偵測封頂同源);
# clamp 在伺服器端做(_normalize_speakers)——gr.Number 的 minimum/maximum
# 由 gradio preprocess 強制,超限直接拋「英文」錯誤,違反繁中原則(spec §8)
# 錨定專案根目錄(editable 安裝下 __file__ 在 src/ 內),不依賴工作目錄
OUTPUT_DIR = paths.repo_root() / "output"
# 使用手冊插圖:docs/ 隨 repo 版控,README(GitHub 上的手冊)與
# 「使用說明」分頁共用同一份檔案,改圖只改一處
_PRIVACY_IMG = paths.repo_root() / "docs" / "claude-privacy-setting.jpg"
# 瀏覽器分頁的圖示:**白底磚**那一份(`favicon.ico`,不是去背的 `icon.ico`)。
# ⚠️ 分頁列的底色多半是白或淺灰,透明的圖示貼上去只剩幾條細線、等於消失
# (使用者 2026-08-20 實際回報)。桌面捷徑則相反,走去背的 icon.ico——
# 兩個檔的底色需求相反,不可以合併,理由見 scripts/make_icon.py
_FAVICON = paths.assets_dir() / "favicon.ico"


def find_free_port(start: int = 7860, limit: int = 20) -> int:
    """從 start 往上找第一個可綁定的埠(讓多個實例並存);都被占用才報錯。"""
    for port in range(start, start + limit):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"找不到可用的連接埠({start}~{start + limit - 1} 都被占用)")


def _normalize_speakers(value) -> int:
    """gr.Number 欄位被清空時會回傳 None;一律安全歸零(自動偵測)。
    超過 MAX_SPEAKERS 者夾到上限(上限已註明於欄位標籤)。"""
    try:
        return max(0, min(MAX_SPEAKERS, int(value)))
    except (TypeError, ValueError):
        return 0


def _normalize_cores(value) -> int:
    """CPU 核心數欄位:夾到 1..最大核心數;空白/非法回到預設(最大核心數-1)。"""
    try:
        return max(1, min(power.max_cpu_cores(), int(value)))
    except (TypeError, ValueError):
        return power.default_worker_count()


def _apply_cpu_cores(value) -> None:
    """套用使用者指定的核心數;若與上次不同,清掉吃 CPU 執行緒的引擎快取
    (執行緒數在引擎建構時就固定,不清快取新值不會生效)。"""
    if power.set_worker_count(_normalize_cores(value)):
        diarize.clear_engine_cache()
        punctuate.clear_engine_cache()
        transcribe.clear_engine_cache()
        # 轉錄子行程的執行緒數是**啟動時用參數傳進去的**,清主行程的快取
        # 對它毫無作用——留著那支等於使用者調了核心數卻沒有生效,而且
        # 完全看不出來。收掉即可,下次轉檔會用新的數字重開一支
        # (transproc.get 也會自己比對,這裡是把時機提前到「按下設定」)
        transproc.shutdown()


# ---- 試聽片段(命名時認人用)----
# 片段檔要活過「轉檔事件結束 → 使用者按試聽/命名」的跨事件窗口,不能用
# with 自清的暫存目錄;沿用 pipeline 的「前綴+存活鎖」機制:目錄同前綴、
# 鎖檔由本行程持開——另一實例啟動時 cleanup_stale_temp 刪不掉鎖檔就整包
# 跳過(多實例可並存);本行程硬退出後鎖自動釋放,下次啟動掃掉。
# 換下一檔時舊目錄直接汰換,不必等啟動清掃。
_clips_dir: Path | None = None
_clips_lock = None  # 持開中的鎖檔 handle(見上)


def _new_clips_dir() -> Path:
    """換新一批試聽片段的目錄:上一批(連同鎖檔)一併汰換。"""
    global _clips_dir, _clips_lock
    if _clips_lock is not None:
        _clips_lock.close()
        _clips_lock = None
    if _clips_dir is not None:
        shutil.rmtree(_clips_dir, ignore_errors=True)
    _clips_dir = Path(tempfile.mkdtemp(prefix=pipeline.TMP_PREFIX + "clips-"))
    _clips_lock = (_clips_dir / pipeline.TMP_LOCK).open("wb")
    return _clips_dir


def _cut_speaker_clips(src: Path, hints: dict, sources: dict | None = None) -> dict:
    """從原始檔剪出每位講者(含未知)「最長一句」的試聽片段。

    回 {講者標籤: 片段路徑};該句起訖秒數由 hints 帶回(與命名欄顯示的
    摘錄同一句,聽到的就是看到的那句)。sources({講者標籤: 音檔路徑},
    現場收音的分軌結果)優先於 src:線上會議的現場講者要剪麥克風軌、
    遠端講者剪系統軌,剪錯軌只會聽到回音版或無聲。試聽是輔助功能:
    任何一段剪失敗只記 log、少一顆試聽鈕,絕不讓整批轉檔失敗。"""
    clips: dict[int, str] = {}
    if not hints:
        return clips
    out_dir = _new_clips_dir()
    for spk, (_cnt, _quote, start, end) in hints.items():
        name = "unknown.wav" if spk == UNKNOWN_SPEAKER else f"speaker_{spk}.wav"
        origin = Path(sources[spk]) if sources and spk in sources else src
        try:
            clips[spk] = str(audio.cut_clip(origin, out_dir / name, start, end))
        except Exception:
            logger.exception("試聽片段剪輯失敗(講者標籤 %s),該講者不提供試聽", spk)
    return clips


# ---- 命名草稿(儲存本體在 pending.py,這裡只做欄位順序 → 講者標籤)----

def _save_draft_names(*name_values) -> None:
    """命名欄輸入即存草稿(掛 .input:只在「使用者」輸入時觸發,_run/套用
    /換檔的程式化更新不會誤存)。前 MAX_SPEAKERS 個是講者框、最後一個是
    「未知」框;欄位順序 → 講者標籤的轉換在此,儲存本體在 pending。"""
    names = {
        i: v.strip() if isinstance(v, str) else ""
        for i, v in enumerate(name_values[:MAX_SPEAKERS])
    }
    if len(name_values) > MAX_SPEAKERS:
        v = name_values[MAX_SPEAKERS]
        names[UNKNOWN_SPEAKER] = v.strip() if isinstance(v, str) else ""
    pending.update_names(names)


# ---- 過期點擊(斷線後的死頁面)----

def _stale_click_guard(paths) -> None:
    """兩顆「收工鈕」的守門:手上沒有成品路徑 = 這次點擊沒有任何工作內容,
    當作過期點擊擋下,絕不讓它清掉落地的命名進度。

    為什麼需要:電腦睡眠會讓 SSE 斷線、gradio 把 session 判死,但**死掉的
    session 上的點擊照樣在伺服器端執行**——gradio 6.20 的 /queue/join 只擋
    session_hash 為 None,不檢查 session 死活(routes.queue_join →
    queueing.Queue.push 直接補一個新佇列就跑),回傳訊息丟進沒人讀的佇列,
    所以畫面毫無反應、使用者以為沒按到。此時 gr.State 多半已被回收(斷線
    後 1 小時 TTL,gradio state_holder.STATE_TTL_WHEN_CLOSED),
    「套用名稱」拿到空的 paths_state:名字一個字也寫不回去,卻會照常執行
    最後的 pending.clear() 把命名進度清光。使用者實際踩到(2026-07-31:
    2.5 小時的會議錄音睡醒後點一下,講者命名整份消失,只能重轉)。

    活著的 session 不可能走到這裡:兩顆鈕都在 #name-box 內,而該區塊只在
    有命名框渲染時才由 CSS 顯示,那時 paths_state 必為轉檔/還原寫入的
    成品路徑。訊息在死頁面上其實看不到(結果送不回前端),真正的目的是
    「什麼都不要動」,把 pending 留給下一次 F5 接回(見 _restore_pending)。"""
    if not paths:
        raise gr.Error(
            "與程式的連線已中斷(電腦睡眠或閒置過久),這次點擊沒有生效。"
            "請重新整理頁面(F5),未完成的講者命名會自動接回,不必重新轉檔。",
            title="連線已中斷", print_exception=False,
        )


# ---- 檔案來源(路徑輸入,不上傳;把關與原生選檔在 srcfile.py)----

def _pick_file(current, recursive=False, mode=None):
    """「選擇檔案…」:開原生對話框取路徑(**可複選**),接在現有內容後面。

    累加不取代(同「文字、圖像→MD」分頁,2026-08-01 使用者回報:選第二次
    會把第一次蓋掉,等於永遠只能處理一批)。程式化填值不會觸發路徑欄的
    .input,所以「開始」的狀態與摘要都得自己帶上。

    「重設講者」模式下同一顆鈕改成挑一份 md,而且**取代不累加**——那個
    模式一次只處理一份逐字稿,累加只會讓路徑欄長出第二行沒人會用的東西。"""
    if mode == _MODE_RELABEL:
        picked = relabel.pick_md()
        return _picked(picked or current, recursive)
    return _picked(srcfile.append_paths(current, srcfile.pick_files()), recursive)


def _pick_dir(current, recursive=False):
    """「選擇資料夾…」:整個資料夾丟進來(= 批次模式,見 _SRC_MODE_HINT)。"""
    return _picked(srcfile.append_paths(current, srcfile.pick_folder()), recursive)


def _clear_src(recursive=False):
    """「清空」:選檔是累加的,要重來得有這顆(同文件分頁)。"""
    return _picked("", recursive)


def _picked(text, recursive):
    """選檔類事件的共同回傳:路徑欄、「開始」亮不亮、選檔摘要。

    三顆鈕共用一份——各寫一次的話,少更新摘要的那顆會讓畫面停在上一次
    的檔案數,而使用者正是照那個數字決定要不要按下去的。"""
    return text, _run_btn_for(text), _src_summary(text, recursive)


def _src_summary(text, recursive=False):
    """選檔摘要:選了幾個檔、哪些格式(轉檔前就讓人確認範圍)。

    只做即時回饋、**不控制按鈕**:把關全落在 _run(同文件分頁的決定)。
    這裡不能拋例外——路徑打到一半必然是「找不到」,那不是錯誤。"""
    if not srcfile.clean_paths(text):
        return gr.update(visible=False, value="")
    if not srcfile.looks_like_batch(text):
        # 措辭刻意不講「選了 1 個檔案」:路徑打到一半時那句等於宣稱檔案
        # 存在(而它還不存在)。這裡只講模式,那句話什麼時候都是真的
        return gr.update(
            visible=True, value="**單一檔案**:轉完會讓你替每位講者命名。",
        )
    try:
        files, skipped = docsrc.validate_batch(
            text, recursive=bool(recursive), types=srcfile.SUPPORTED_TYPES,
            what="錄音或錄影檔", hint=srcfile.supported_hint(),
        )
    except UserFacingError as e:
        return gr.update(visible=True, value=str(e))
    return gr.update(
        visible=True,
        value=f"{docsrc.summarize(files, skipped)}(整批連續轉,不做講者命名)",
    )


def _run_btn_for(text):
    """「開始」的唯一判準:路徑欄有內容就亮。掛在路徑欄 .input(只回應
    使用者輸入)、選檔鈕與轉檔收尾三處,三處必須同一判準——各寫一份的話,
    「打字亮了、收尾卻鎖住」這種不一致只會在報錯路徑上浮現。"""
    return gr.update(interactive=bool(srcfile.clean_path(text)))


# ---- 按鈕狀態機:開始/停止/檔案來源的鎖定與還原(使用者規格)----

def _end_of_job_updates() -> tuple:
    """「這一檔收工」的共同尾端:清路徑欄、雙鈕回等待狀態、講者人數歸零。

    收工點有三個——套用名字、跳過命名、按停止(_apply_names /
    _discard_naming / _request_stop),三者共用本組更新,政策只講一次。
    人數歸零的理由(使用者回報 2026-07-24):收工沒歸零,上一場填的人數
    會殘留到下一場、被拿去強制分群。刻意「不」把按下開始那一刻算作收工
    ——人數正是使用者剛為這一檔填的;「開始錄音」也不歸零(錄音中隨時
    可填、停止當下才讀,開錄復位會清掉預先填好的值)。
    路徑欄清空後「開始」必須在同一批訊息裡鎖回:路徑欄的亮燈事件掛
    `.input`(只回應使用者輸入),程式化清空不會觸發它。"""
    return (
        gr.update(value=""),           # 路徑欄清空,等下一檔
        gr.update(interactive=False),  # 開始:重新選檔才亮
        gr.update(interactive=False),  # 停止
        gr.update(value=0),            # 講者人數歸零回自動偵測
    )


def _request_stop():
    """停止按鈕:設取消旗標,轉檔在下一個檢查點(通常數秒內)停下。
    與轉檔事件並行執行(gradio 佇列的併發上限是「每個事件各自一件」,
    不是全域一件,故轉檔進行中本函式仍能即時執行)。

    按下當下即收工(_end_of_job_updates:清路徑欄、雙鈕鎖住、人數歸零
    ——使用者規格「按一次就好,等下一個檔案」);路徑欄的 interactive
    維持鎖定,轉檔鏈真正收尾(_after_run)才解鎖。
    競態無害:若停止恰好按在轉檔完成後,這組更新與完成後狀態完全一致。"""
    cancel.request()
    gr.Info("已要求停止:等目前片段收尾(數秒)即停,轉到一半的檔案會放棄、不留半成品。")
    return _end_of_job_updates()


def _start_run():
    """轉檔鏈第一步:鎖介面 + 整頁復位(=上一檔收工的畫面部分)。

    鎖定:路徑欄/「選擇檔案…」/「開始」鎖住、「停止」亮起,「要做什麼」
    與「進階參數設定」三控件一併鎖住(與錄音同邏輯:參數在開始當下定死,
    轉檔中改動易被誤解成即時生效;來源切換會清路徑、藏掉進行中的轉檔
    介面,使用者指定 2026-07-22 轉檔中不可切)。路徑欄一併鎖住(使用者
    選定):轉檔中換路徑會與進行中的轉檔對不上。
    復位:清上一檔成品與落地命名,講者框全隱藏——這是 _present_result
    送 gr.skip() 的前置條件(上一檔 7 位講者、這檔 2 位時,3~7 號框必須
    先藏起來),所以**必須是 _run 之前的另一批訊息**,不能併進 _run。
    曾掛在 files.upload(上傳=收工點),改路徑輸入後沒有「上傳」時刻,
    改在按下開始當下(使用者指定 2026-07-26)。「講者人數」不在此歸零
    ——人數是使用者剛為這一檔填的(歸零點見 _end_of_job_updates)。

    必須是鏈的「第一步」而非並行事件:並行與 _run/_after_run 之間無順序
    保證,極短轉檔(如把關立即報錯)會後發先至、把已復位的介面又鎖回去。
    **本步不得拋例外**:收尾 `_after_run` 的 .failure 掛在 _run 那一步,
    這裡若拋例外就沒人解鎖、介面鎖死——故 pending 清除失敗只記 log。"""
    _transcribing["on"] = True  # 錄音與轉檔互斥(引擎快取非執行緒安全)
    try:
        pending.clear()
    except Exception:
        logger.exception("清除命名進度失敗(不影響本次轉檔)")
    return (
        gr.update(interactive=False),  # 路徑欄
        gr.update(interactive=False),  # 選擇檔案…
        gr.update(interactive=False),  # 選擇資料夾…
        gr.update(interactive=False),  # 清空
        gr.update(interactive=False),  # 開始
        gr.update(interactive=True),   # 停止
        gr.update(interactive=False),  # 要做什麼(鎖切換)
        *_param_updates(False),        # 進階參數鎖住
        *_page_reset_updates(None),    # 整頁復位(上一檔成品清掉)
    )


def _after_run(path_value):
    """轉檔鏈收尾(.then/.failure,完成/報錯/停止都執行):解鎖路徑欄/
    選檔鈕、要做什麼與進階參數、鎖「停止」;「開始」看路徑欄還有沒有
    內容(判準與路徑欄亮燈同源,見 _run_btn_for)——完成(_run 回傳
    清空)與停止(_request_stop 清空)後已清空,維持鎖住等下一檔;
    報錯(gr.Error 中斷、_run 的 outputs 沒落地)路徑還在 → 恢復可按,
    使用者修正問題後直接重試(使用者選定)。"""
    _transcribing["on"] = False  # 完成/報錯/停止都會經過這裡(then/failure 成對)
    return (
        gr.update(interactive=True),       # 路徑欄
        gr.update(interactive=True),       # 選擇檔案…
        gr.update(interactive=True),       # 選擇資料夾…
        gr.update(interactive=True),       # 清空
        _run_btn_for(path_value),
        gr.update(interactive=False),      # 停止
        gr.update(interactive=True),       # 要做什麼解鎖
        *_param_updates(True),
    )


# ---- 轉檔的斷線還原:畫面與執行脫鉤(使用者選定 2026-08-08,方案 C)----
#
# 為什麼要這一組:轉檔進度原本只活在 `gr.Progress` 裡,而它綁在那一次
# 事件執行上。瀏覽器把背景分頁節流 → 重連橫幅浮出 → 使用者按下「重新
# 連線」(=reload)→ 新頁面接不回那條進度,畫面回到剛開啟的樣子:檔名、
# 進度、**連停止鈕都沒了**,而轉檔還在後端跑,要中止只能關黑視窗。
# 錄音那條路 2026-07-18 就用 `_restore_recording` 解決了,轉檔漏了一份。
#
# 伺服器端的真相在 `runstate`;這裡只負責把它畫回畫面上。

# 秒針在「整頁更新」裡只動預覽區那一格。preview 在 `_naming_page_updates`
# 的回傳裡排第二個——給它一個名字而不是寫死 1:將來那個函式前面多一格,
# 寫死的版本會靜默地把進度畫到別的元件上(而隔壁那格是下載區)
_PREVIEW_IN_PAGE = 1
# 左欄控件的回傳長度:要做什麼/路徑欄/三顆選檔鈕/開始/停止 + 四個進階
# 參數 + 錄音那一側四個(狀態列/錄音雙鈕/收音情境)。**兩側的聯集**是因為
# 秒針要同時服務兩條路——檔案轉檔與錄音收尾結束時,該收拾的鈕不一樣
# (尤其「停止」:檔案模式留著等下一檔,收音模式要收回隱藏)
_RUN_UI_LEN = 7 + 4 + 4


def _page_only_preview(preview_md: str) -> tuple:
    """整頁「其他都別動」,只把預覽區換成這段文字。

    其餘一律 gr.skip():轉檔中每秒跑一次,動到命名框/下載區都是白費往返,
    而 State 元件更不能收 gr.update()(update dict 會被當成「值」存進去)。"""
    page = [gr.skip()] * PAGE_UPDATE_LEN
    page[_PREVIEW_IN_PAGE] = gr.update(value=preview_md)
    return tuple(page)


def _transcribe_ui_updates(busy: bool, path_value: str = "") -> tuple:
    """左欄控件的鎖定/解鎖(順序 = build_ui 的 `run_restore_ui`)。

    busy=True 必須與鏈頭 `_start_run` 鎖出來的狀態**一致**——reload 接回
    的畫面若比原本鬆(例如「開始」還能按),那正是會跑出第二份轉檔的路;
    互斥檢查是最後一道防線,不該讓它天天上工。"""
    rec_side = tuple(gr.skip() for _ in range(4))  # 錄音那四個不歸這裡管
    if busy:
        return (
            # 模式帶 info 一起送:只送 value/interactive 的話,說明小字會
            # 停在使用者上次切換的模式(同 `_restore_recording` 那條)
            gr.update(value=_MODE_FILE, interactive=False,
                      info=_MODE_INFO[_MODE_FILE]),
            gr.update(value=path_value, interactive=False),  # 檔名要看得到
            gr.update(interactive=False),   # 選擇檔案…
            gr.update(interactive=False),   # 選擇資料夾…
            gr.update(interactive=False),   # 清空
            gr.update(interactive=False),   # 開始
            gr.update(interactive=True),    # 停止:這顆回來,才不必關黑視窗
            *_param_updates(False),
            *rec_side,
        )
    return (
        gr.update(interactive=True),        # 要做什麼(只解鎖,不動選中值)
        gr.update(interactive=True),        # 路徑欄
        gr.update(interactive=True),        # 選擇檔案…
        gr.update(interactive=True),        # 選擇資料夾…
        gr.update(interactive=True),        # 清空
        _run_btn_for(path_value),
        gr.update(interactive=False),       # 停止
        *_param_updates(True),
        *rec_side,
    )


def _recording_end_updates() -> tuple:
    """錄音收尾結束後的復位(形狀同 `_transcribe_ui_updates`)。

    內容等同 `_after_rec_finish`——但那條掛在停止錄音的事件鏈上,**reload
    之後那條鏈並不存在**,所以斷線接回來的畫面得由秒針自己收拾。
    差別最大的是「停止」那顆:檔案模式留著等下一檔,收音模式要收回隱藏
    (它平時不顯示,只在收尾期間臨時亮出來)。"""
    return (
        gr.update(interactive=True),                  # 要做什麼:解鎖
        gr.skip(), gr.skip(), gr.skip(), gr.skip(),   # 路徑欄與選檔三鈕(收音模式隱藏)
        gr.skip(),                                    # 開始轉檔(同上)
        gr.update(visible=False, interactive=False),  # 停止:收回隱藏
        *_param_updates(True),
        gr.update(value=_REC_IDLE_MD),                # 錄音狀態列回待機
        gr.update(interactive=True),                  # 開始錄音
        gr.update(interactive=False),                 # 停止錄音
        gr.update(interactive=True),                  # 收音情境解鎖
    )


def _progress_md(snap) -> str:
    """轉檔中預覽區顯示的進度(使用者選定方案 C:兩段分開列)。

    **分開列而不是一條總進度**:轉錄與講者分析在有 GPU 時真平行,而總
    時間由較慢的講者分析決定。合成一個數字的話,轉錄跑完就會衝到七成
    然後卡住一兩個小時——那比沒有進度更擾民。分開列還順帶回答了使用者
    在黑視窗沉默那段時間裡唯一想知道的事:還在跑嗎、跑到哪了。"""
    what = ("**收尾中**" if snap.kind == runstate.KIND_RECORDING
            else "**轉檔進行中**")
    lines = [
        f"{what}——可以切去做別的事,"
        "甚至關掉這個頁面都不會中斷,回來重新整理就接得回來。",
        "",
    ]
    if snap.detailed:
        for key, label in runstate.STAGE_LABELS.items():
            if key not in snap.stages:
                lines.append(f"- {label}:準備中")
                continue
            frac = snap.stages[key]
            lines.append(
                f"- {label}:{'完成' if frac >= 0.999 else f'{frac * 100:.0f}%'}")
    elif snap.note:
        # 批次:docpipe 給的那句話已經是「(3/12) 檔名」;收尾:階段名。
        # 原樣呈現比在這裡重組一份會走樣的好
        lines.append(f"- {snap.note}")
        if snap.frac > 0:
            lines.append(f"- 整體約 {snap.frac * 100:.0f}%")
    else:
        lines.append("- 準備中")
    lines.append("")
    tail = f"已花 {snap.elapsed / 60:.0f} 分"
    if snap.eta is not None:
        tail += f",預估還要 {snap.eta / 60:.0f} 分"
    lines.append(tail)
    return "\n".join(lines)


def _restore_transcribing():
    """開頁(demo.load)接回進行中的轉檔。

    沒在轉檔時**整組 gr.skip()**:同一個 demo.load 上還掛著
    `_restore_recording` 與 `_restore_pending`,三者的 outputs 有重疊
    (要做什麼/路徑欄/雙鈕/進階參數)——這裡若在閒置時送「解鎖」,就會
    把錄音中的畫面覆蓋掉,變成另一個 bug。"""
    snap = runstate.snapshot()
    # 錄音收尾也在 runstate 裡,但它的畫面長在錄音那一側(rec_status、
    # 錄音雙鈕),由 `_restore_recording` 接回——這裡碰它只會把那組更新
    # 覆蓋掉。秒針則兩邊共用,由那條路負責打開
    if snap is None or snap.kind == runstate.KIND_RECORDING:
        return (
            *(gr.skip() for _ in range(PAGE_UPDATE_LEN)),
            gr.skip(),                                    # 秒針不動
            *(gr.skip() for _ in range(_RUN_UI_LEN)),
        )
    return (
        *_page_only_preview(_progress_md(snap)),
        gr.Timer(active=True),
        *_transcribe_ui_updates(True, snap.label),
    )


def _run_tick(mode=None):
    """轉檔中的秒針(reload 之後的畫面靠它活著)。

    `mode` 是「要做什麼」的當下選擇,只用在**收工那一刻**:檔案轉檔與錄音
    收尾要收拾的鈕不一樣。判準取畫面上的值而不是另外記一個模組變數——
    收尾中的畫面本來就由 `_restore_finishing` 把它設成收音模式了。

    ⚠️ **收尾也是它的事**:reload 之後的頁面沒有原本那條
    `_run` → `_after_run` 事件鏈(那條屬於已經被判死的舊 session),轉檔
    完成時沒有別人會來解鎖介面、把命名畫面接上。所以秒針發現轉檔結束就
    自己收尾——資料走 `_restore_pending`(轉檔完成當下已由
    `pending.persist` 落地),與正常路徑拿的是同一份。

    少了這一段,使用者會看著進度跑到完成然後畫面永遠停在那裡,而命名畫面
    其實只差一次 F5——那正是這次要修掉的困惑,不能換個地方留著。"""
    snap = runstate.snapshot()
    if snap is not None:
        return (
            *_page_only_preview(_progress_md(snap)),
            gr.Timer(active=True),
            # 控件在還原/鏈頭時已經鎖好,每秒重送一次是白費往返
            *(gr.skip() for _ in range(_RUN_UI_LEN)),
        )
    # 轉完了(或被停止/失敗):把命名畫面接上並解鎖。`_restore_pending`
    # 沒有落地資料時整組 skip,此時畫面就只是解鎖回到等待下一檔
    end_ui = (_recording_end_updates() if mode == _MODE_RECORD
              else _transcribe_ui_updates(False, ""))
    return (*_restore_pending(), gr.Timer(active=False), *end_ui)


# ---- 講者命名:逐字稿改寫與認人線索 ----

def _rename_speakers(
    text: str, name_map: dict[int, str], unknown_name: str | None = None,
    labels: dict[int, str] | None = None,
) -> str:
    """把 md 逐字稿裡的講者標籤換成指定名字。

    `labels` 是「這些編號目前在檔案裡長什麼樣子」,預設「講者 N」/「未知」。
    「重設講者」模式(_run_relabel)會傳入實際讀到的標籤——那份 md 可能
    **早就命名過**(當初打錯字、或想換個稱呼),標籤是真名而不是「講者 N」。

    改寫委託給 relabel.rename:它逐行比對 `**名字** (時:分:秒)` 這個輸出
    格式本身,時間戳讓它不可能誤中內文裡的粗體字,也天然沒有
    「講者 1 誤配到講者 10」的前綴問題(那正是舊實作要靠尾隨 `**` 迴避的)。
    (曾依副檔名分流 txt/srt 錨定,隨輸出格式固定 md 移除,2026-07-26。)"""
    labels = labels or {}
    by_label = {
        labels.get(n, f"講者 {n}"): name for n, name in name_map.items()
    }
    if unknown_name:
        by_label[labels.get(UNKNOWN_SPEAKER, "未知")] = unknown_name
    return relabel.rename(text, by_label)


# 命名欄位摘錄長度:一行內讀得完的識別線索即可,不是給全文
# 摘錄字數上限。⚠️ **40 → 24 是量出來的**(2026-08-18 精簡面板):40 字在
# 482px 的左欄折成 **3 行**、整段線索佔 84px;認人其實看前二十幾字就夠——
# 那是「這個人講話的樣子」,而真要確認,試聽鈕就在同一列
_HINT_QUOTE_CHARS = 24


def _hint_text(hint, rivals=None) -> str | None:
    """命名欄位下的認人線索:「共 N 段發言・『最長一句摘錄』」,聲紋分不開
    的那幾位再加一行候選。

    讓使用者不必翻預覽找「講者 N 說了什麼」就能認人。無線索回 None
    (該講者沒有合格的摘錄句;線索與聲紋同源,見 speaker_hints)。

    ⚠️ **候選一定要並列、而且不寫分數**(使用者 2026-08-15 選定 3 案):
    98 次留白裡 54% 的第一名與第二名只差 0.03 以內,那一段第一名只有
    24% 是對的——單獨顯示第一名等於給一個四次錯三次的答案。分數不寫則是
    因為「0.86 對 0.85」會讓那 0.01 看起來像一種依據,而它其實是雜訊。

    ⚠️ **換行用單一 `\\n`**:gradio 6.20 的 info 會把它轉成 `<br>`
    (2026-08-15 Playwright 實測,見 docs/dev/ui.md)——它不是 markdown,
    所以既不必寫兩個空格,也不怕摘錄裡的符號被當成語法。

    ⚠️ **線索裡不寫任何按鈕名稱**(2026-08-18 精簡面板時整句拿掉)。沿革:
    原本候選後面附「建議按『🔍 核對』聽過再選」,而核對只亮在差距最小的
    前三位——有幾列因此叫人去按一顆畫面上不存在的鈕(2026-08-15 使用者
    截圖抓到,當時是加 `can_audit` 改指試聽)。**現在改成不指鈕**:那半句
    是規則不是這一位的資訊,每一位重複一次、實測每列多 33px,而兩顆鈕
    本來就在同一列右邊。指路改在面板頂部講一次,並同時提試聽與核對。
    `test_hint_text_never_names_a_button` 守著。"""
    parts = []
    if hint:
        count, quote = hint[0], hint[1]  # 尾端另有該句起訖秒(剪試聽用),這裡用不到
        if len(quote) > _HINT_QUOTE_CHARS:
            quote = quote[:_HINT_QUOTE_CHARS] + "…"
        parts.append(f"共 {count} 段發言・「{quote}」")
    if rivals:
        # ⚠️ **只講「像誰」,不再附「建議按 X 聽過再選」**(2026-08-18 精簡):
        # 那半句是**規則**不是這一位的資訊,每一位重複一次——實測它讓每一列
        # 多 33px,十位講者就是 330px。而「🔍 核對」「▶️ 試聽」本來就在同一列
        # 的右邊,按鈕自己就是指路;那句話改成整個面板頂部講一次
        parts.append(f"聲音同時像:{'、'.join(rivals)}")
    return "\n".join(parts) or None


# ---- 試聽(無播放器介面:按「試聽」即從頭播、再按即停、換人直接切)----

# 試聽鈕字樣:播放中的那顆變「停止」,其餘維持「試聽」
# ⚠️ **兩個圖示都用 emoji 版**(使用者 2026-08-14 指定):試聽與核對上下疊,
# 而「▶」「■」是窄的文字符號、「🔍」是全寬 emoji——寬度不同,後面的中文
# 就對不齊。先前試過用 ::first-letter 補間距,實機上仍然不準(不同字型的
# 幾何符號寬度不一樣);改用 emoji 表示形式(加 U+FE0F)才是**同一種東西
# 比同一種東西**,不必調任何數字
_AUD_PLAY_LABEL = "▶️ 試聽"
_AUD_STOP_LABEL = "⏹️ 停止"


def _aud_btn_updates(playing) -> list:
    """31 顆試聽鈕(30 講者+未知)的字樣更新:正在播的那顆變「停止」,
    其餘一律回「試聽」。只動字樣不動 visible——顯示與否由 _run 與
    復位系列(_apply_names/_start_run)管理。"""
    keys = [*range(MAX_SPEAKERS), UNKNOWN_SPEAKER]
    return [
        gr.update(value=_AUD_STOP_LABEL if k == playing else _AUD_PLAY_LABEL)
        for k in keys
    ]


def _audition_reset_updates() -> tuple:
    """試聽整組復位(轉完新檔/套用/轉檔鏈復位共用):清片段與播放狀態、
    停掉出聲載體(value=None 即卸載媒體)、藏所有試聽鈕。片段檔留在磁碟
    由下一批汰換,不在此刪——載體可能正在串流該檔。"""
    hidden = [
        gr.update(visible=False, value=_AUD_PLAY_LABEL)
        for _ in range(MAX_SPEAKERS + 1)
    ]
    return ({}, None, gr.update(value=None), *hidden)


# 「🔍 核對」的鈕字樣。與試聽鈕一樣走「字樣即狀態」,不另開狀態元件
_AUDIT_LABEL = "🔍 核對"


def _audit_reset_updates() -> tuple:
    """核對整組復位:清狀態、關面板、藏所有核對鈕(轉完新檔/套用/復位共用)。

    形狀 = audit_state + 面板 4 件(播放器/表格/改掛下拉/整個面板)
    + 30+1 顆鈕,與 _audit_panel_updates 對齊。"""
    hidden = [gr.update(visible=False) for _ in range(MAX_SPEAKERS + 1)]
    return (
        {}, gr.update(value=None), gr.update(value=None),
        gr.update(value=[], choices=[]), gr.update(value="", choices=[]),
        gr.update(visible=False), *hidden,
    )


def _audit_dir() -> Path:
    r"""核對音檔放哪:與試聽片段同一個暫存目錄底下。

    那個目錄已經有鎖檔、換一批就整個汰換,而且被 `cleanup_stale_temp`
    納管(當機殘留下次啟動自動清)。⚠️ **絕不能放
    `%LOCALAPPDATA%\meeting-scribe
ecordings`**——那裡是錄音的地盤,
    規矩相反(錄音不能被當孤兒掃掉,核對音檔則是用完即丟)。"""
    if _clips_dir is None:
        _new_clips_dir()
    d = _clips_dir / "audit"
    d.mkdir(exist_ok=True)
    return d


def _audit_blocks(audit, spk) -> list:
    """audit_state(純 dict,State 只放得下可序列化的東西)→ 該講者的區塊。"""
    blocks = [
        SpeechBlock(speaker=int(b["speaker"]), start=float(b["start"]),
                    end=float(b["end"]), text=str(b.get("text", "")),
                    cohesion=float(b.get("cohesion") or 0.0))
        for b in (audit or {}).get("blocks", [])
    ]
    return audit_mod.blocks_of(blocks, spk)


# 逐列播放鈕的字樣(**字樣即狀態**,不另開狀態元件;同命名區的試聽鈕)
# 核對表最多列幾列。⚠️ **這是效能上限,不是「該聽多少」**:幾百列的
# Dataframe 光前端重排就卡得有感(使用者 2026-08-13 回報「勾選改掛很慢」)。
# 超過就抽樣(最長的那些 + 全場均勻),而抽樣本來就夠回答「這一群純不純」
_AUDIT_MAX_ROWS = 100
_ROW_PLAY = "▶"


def _audit_table(rows, blocks=None) -> list[list]:
    """核對表的表格值:[播放鈕, 序號, 相似度, 長度, 內容]。

    ⚠️ **播放鈕在最前面、每一列一顆**(使用者 2026-08-13 實際用過後指定):
    原本是上面一個整批播放器,他說「我直接在那列上按下播放比較好操作,
    不用點下面又去點上面」。連帶**拿掉「核對檔位置」那一欄**——那一欄
    是用來在整批音檔裡定位的,整批播放器沒了它就沒有意義,寬度讓給內容。"""
    coh = [b.cohesion for b in (blocks or [])]
    return [
        [_ROW_PLAY, r.index,
         f"{coh[n]:.2f}" if n < len(coh) and coh[n] else "—",
         f"{r.seconds:.1f}", r.text]
        for n, r in enumerate(rows)
    ]


def _audit_choices(rows, blocks=None) -> list[tuple[str, int]]:
    """「要改掛哪幾段」的下拉選項:(顯示字串, 列序號)。

    ⚠️ **勾選搬出表格是效能決定**(使用者 2026-08-14 第四次回報,而且
    19 列也卡):可編輯的 Dataframe 每勾一次就整張表重繪——延遲發生在
    「勾下去的瞬間」,那是純前端成本,伺服器再快也沒用。多選下拉是原生
    元件,勾幾百個都不卡,而且**可以打字搜尋**。"""
    # ⚠️ **選項裡不放時間**(使用者 2026-08-14 截圖:「0:00:01·0.75」擠在
    # 一起很難讀):時間在表格上看得到,這裡要的是「哪幾段可疑」——
    # 留序號對得回表格、留相似度當判準就夠了
    coh = [b.cohesion for b in (blocks or [])]
    out = []
    for n, r in enumerate(rows):
        mark = f"{coh[n]:.2f}" if n < len(coh) and coh[n] else "—"
        out.append((f"{r.index}. {mark}  {r.text[:22]}", n))
    return out


def _all_names() -> list[str]:
    """所有選得到的名字:與會名單(維持使用者排的順序)+ **只在聲紋庫裡的**。

    ⚠️ **有聲紋卻不在名單上的人一定要列進來**(2026-08-15 code review 抓到
    命名下拉與改掛下拉都少了這一半):名單與聲紋庫是兩份資料,而
    `data_tabs.orphan_names` 那整套安全網存在的理由,正是「這種狀態真的會
    發生」(改名只改一邊、用記事本編過名單、半途而廢的改名)。選單裡找不到
    就只能自己打字,而打錯一個字就是聲紋庫裡多一個人——那正是這些下拉
    要防的事。`_choice_layout` 的註解早就寫著這條規則,只是它靠 rivals
    才做得到,沒有進候選的那些人漏在外面。"""
    return list(dict.fromkeys(
        [*attendees.load(), *voiceprints_store.known_names()]))


def _reassign_choices(spk, name_values, audit) -> list[str]:
    """「把選取的段落改掛給」的名單順序(使用者 2026-08-15 選定「只排序」)。

    好選的排前面,但**任何人都還選得到**:
      ① 本場其他講者已經填好的名字——插話的人最可能就在這場會議裡
      ② 本場的聲紋候選——外面都還沒填時,這就是「聲紋庫覺得今天可能在場
         的人」;零成本,`_naming_clues` 已經算過了
      ③ 其餘完整名單

    ⚠️ **只排序不限縮**:插話的人常常只講一兩句、根本沒有自成一群,所以
    不會出現在①②裡(0812 那場實測:某個標籤裡 13 段插話分屬**七個人**,
    而那七位多半沒有自己的講者編號)。限縮的話他們就只能靠打字,而打錯
    一個字就是聲紋庫裡多一個人。

    ⚠️ **排除當前這一位**:改掛的意思是「這幾段其實不是他」,把他自己排在
    第一個只會擋路。

    ⚠️ 同樣**不加標記**,理由見 `_choice_layout`(那邊的琥珀底色畫在
    CSS,這裡沒做)。"""
    head: list[str] = []
    for i, v in enumerate(name_values[:MAX_SPEAKERS]):
        if i == spk or not isinstance(v, str) or not v.strip():
            continue
        head.append(v.strip())
    head += list((audit or {}).get("rivals") or [])
    head = list(dict.fromkeys(head))        # 保序去重
    seen = set(head)
    return head + [n for n in _all_names() if n not in seen]


def _audit_open(spk, audit, *name_values):
    """「🔍 核對」:把這一位的發言抽樣接成一個音檔 + 對照表,開面板。

    `name_values` 是命名區各欄目前的值(順序同 `_save_draft_names`):
    用來把「本場已經填好的名字」排到改掛選單前面,見 `_reassign_choices`。
    讀畫面上的值而不是落地草稿——那才是使用者此刻看到的真相。

    ⚠️ **核對是輔助功能,失敗只提示、不影響命名**(同 _audition_clips):
    音檔剪不出來時關掉面板即可,使用者照樣填名字、照樣套用。"""
    mine = _audit_blocks(audit, spk)
    if not mine:
        gr.Warning("這個標籤沒有可核對的段落。", title="無法核對")
        return gr.skip(), gr.skip(), gr.skip(), gr.skip(), gr.skip(), gr.skip()
    src = (audit.get("sources") or {}).get(str(spk)) or audit.get("src") or ""
    try:
        # ⚠️ **不再接成一個整批音檔**(使用者 2026-08-13 指定改成逐列播放):
        # 這裡只要「這一位有哪些輪發言」與一份 16k 快取,逐列播放時才從
        # 快取剪出那一段(隨機存取,實測 0.01 秒)。
        # 連帶**不再抽樣**:上限 3 分鐘的意義是「不要讓人坐著聽 20 分鐘」,
        # 而逐列播放一次只聽一段——限制列數反而是把東西藏起來
        audit_mod.ensure_wav16k(Path(src), _audit_dir())
    except UserFacingError as e:
        gr.Warning(str(e), title="無法核對")
        return gr.skip(), gr.skip(), gr.skip(), gr.skip(), gr.skip(), gr.skip()
    picks = audit_mod.plan(mine, cap_sec=float("inf"), max_rows=_AUDIT_MAX_ROWS)
    rows = audit_mod.rows_for(mine, picks)
    picked_blocks = [mine[i] for i in picks]
    state = dict(audit)
    state["open"] = spk
    state["playing"] = None
    state["rows"] = [
        {"start": r.start, "end": r.end, "speaker": spk} for r in rows
    ]
    label = export.speaker_label(spk)     # 顯示規則全 repo 只有這一份
    # ⚠️ **不跳提示**(使用者 2026-08-14 指定):面板一打開就在眼前,右上角
    # 再彈一個 toast 只是擋住畫面;真正該講的(抽了幾段、怎麼操作)寫在
    # 面板頂端那一行,看得到、也不會自己消失
    logger.info(
        "核對「%s」:共 %d 段,列出 %d 段", label, len(mine), len(rows),
    )
    return (
        state,
        gr.update(value=None),
        gr.update(value=_audit_table(rows, picked_blocks)),
        gr.update(choices=_audit_choices(rows, picked_blocks), value=[]),
        gr.update(choices=_reassign_choices(spk, name_values, audit), value=""),
        gr.update(visible=True),
        gr.update(visible=False),          # 右欄的預覽讓位給面板
    )


def _audit_play_row(audit, evt: gr.SelectData):
    """點某一列最左邊那一格 → 播那一段;再點一次 → 停。

    ⚠️ **這支刻意不吃、也不回那張表**(2026-08-13~14 使用者三次回報「勾選
    改掛很慢」,19 列也有感):Dataframe 的 select 在**每一格**都觸發,而只要
    把表格列進 inputs/outputs,gradio 每點一下就得把**整張表**序列化送去
    伺服器、再送回來重畫。前幾輪修的「別剪音檔」「少列一點 + 關 wrap」
    「queue=False」都真的有幫助,但**這一條才是每次都會發生的那個成本**。
    代價:沒有 ▶/■ 的字樣切換——播放中的回饋就是聲音本身。

    ⚠️ 出聲載體沒有介面(CSS 移出畫面,同 #audition-player);整格都可以按,
    不必點準那個三角形(使用者 2026-08-14 指定)。"""
    rows = (audit or {}).get("rows") or []
    idx = evt.index if isinstance(evt.index, (list, tuple)) else (evt.index, 0)
    i, col = (int(idx[0]), int(idx[1]) if len(idx) > 1 else 0)
    # 只有點第 0 欄(聽)才播:別的欄是勾選與資料,點它們不該有任何動作
    if col != 0 or not 0 <= i < len(rows):
        return gr.skip(), gr.skip()
    state = dict(audit or {})
    if state.get("playing") == i:                    # 再點一次 = 停
        state["playing"] = None
        return gr.update(value=None), state
    r = rows[i]
    src = (audit.get("sources") or {}).get(str(r["speaker"])) or audit.get("src") or ""
    try:
        row = audit_mod.AuditRow(
            index=i + 1, start=float(r["start"]), end=float(r["end"]), text="",
        )
        dest = audit_mod.cut_one(Path(src), row, _audit_dir() / "one.wav")
    except Exception:
        logger.exception("單段重播剪輯失敗")
        gr.Warning("這一段剪不出來,請看紀錄檔。", title="無法播放")
        return gr.skip(), gr.skip()
    state["playing"] = i
    # ⚠️ **playback_position 必須明確歸零**(2026-08-15 使用者第二次回報:
    # 「短於 3 秒的按第二次就不行,長的有時可以」)。前端會停在上次的結尾
    # 位置,重播時一開播就播畢——**短音檔的結尾 ≈ 全長,所以必然沒聲音**;
    # 長音檔若上次是中途按停的,位置在中間,還聽得到一小段,於是表現成
    # 「有時可以」。Playwright 量「送出 → stop」的間隔:3 秒的音檔不設這個
    # 參數時,第一次 3.36 秒、**第二次 0.05 秒**;設了是 3.17 / 3.14 秒。
    # ⚠️ 這與 _audit_row_ended 清空 value 是**兩件事,缺一不可**:清 value
    # 讓前端肯重新載入(值不變就不重載),歸零 position 讓它從頭播。實測
    # 那一組「有清 value、沒歸零 position」正是 0.05 秒那一欄。
    # 命名區的試聽兩件都做了(_audition / _audition_ended),核對這條路是
    # 後來加的,兩次都只補了一半——同一個坑到此咬了三次。
    return gr.update(value=str(dest), playback_position=0), state


def _audit_row_ended(audit):
    """播完自然結束:清掉載體 value、讓「再點一次 = 停」的判斷歸零
    (不動表格,見上)。

    ⚠️ **一定要連 value 一起清**(2026-08-15 使用者回報:同一列播完再按
    一次就沒反應,要先按別列再切回來才會播):每一列都剪到同一個
    `one.wav`,而重播同一列時內容也一模一樣——gradio 的快取路徑是
    `<內容 hash>/<原檔名>`,兩者都沒變,送回去的 URL 就跟上一次完全相同,
    前端判定「值沒變」而不重新載入,自然不出聲。清成 None 之後,同一列
    再按才有「None → 路徑」的變化可以觸發 autoplay。

    命名區的試聽早就這樣做了(`_audition_ended` 的同一段註解),核對是
    後來才加的,只補了 playing 這一半——**同一個 gradio 行為,兩條路要用
    同一個解法**。Playwright 最小重現實測:同檔名送第二次不播,值先清成
    None 或換個檔名(內容完全相同)就播。"""
    state = dict(audit or {})
    state["playing"] = None
    return gr.update(value=None), state


def _audit_apply(files, audit, picked, new_name, preview_text):
    """把選中的那幾段改掛給 `new_name`:改檔案、改預覽。

    picked 是多選下拉的值(列序號清單)——**不再從表格讀勾選**
    (2026-08-14:可編輯的表格每勾一次就整張重繪,那是延遲的來源)。

    ⚠️ **改的是「逐字稿上的一輪發言」**(見 audit.reassign 的錨):那正是
    表格上看到的一列。沒選任何段落或沒填名字就什麼都不做——寧可沒反應,
    也不要在使用者還沒決定時動到成品。"""
    rows = (audit or {}).get("rows") or []
    name = (new_name or "").strip()
    picked = [int(i) for i in (picked or []) if isinstance(i, (int, float))]
    chosen = [rows[i] for i in picked if 0 <= i < len(rows)]
    if not chosen or not name:
        gr.Warning("請先選要改掛的段落,並選一個名字。", title="還沒完成")
        return gr.skip(), gr.skip(), gr.skip()
    blocks = [
        SpeechBlock(speaker=int(r["speaker"]), start=float(r["start"]),
                    end=float(r["end"]), text="")
        for r in chosen
    ]
    # 檔尾診斷區塊要加註「這份人工改掛過」(見 audit.note_reassigned):
    # 那張表講的是**機器分群當下**的結果,改掛之後數字與內文就對不上了,
    # 而這份 md 的既定消費者是 RAG——自相矛盾的診斷比沒有診斷更糟
    labels = sorted({export.speaker_label(int(r["speaker"])) for r in chosen})
    changed = 0
    for path in files or []:
        pth = Path(path)
        try:
            before = pth.read_text(encoding="utf-8")
            after, n = audit_mod.reassign(before, blocks, name)
            if n:
                pth.write_text(
                    audit_mod.note_reassigned(after, labels, name, n),
                    encoding="utf-8",
                )
            changed += n
        except OSError:
            logger.exception("改掛寫回失敗:%s", pth)
    if not changed:
        # 改不到就要出聲:錨定的是 md 的講者行格式,改不到多半是格式對不上
        # ——那是工具的 bug,不是使用者的(同 _apply_names 的同一條規矩)
        logger.warning("改掛沒有改到任何一行(%d 段、名字「%s」)", len(blocks), name)
        gr.Warning("沒有改到任何一行,請把紀錄檔提供給維護者。", title="改掛失敗")
        return gr.skip(), gr.skip(), gr.skip()
    # ⚠️ **記下「這一群被改掛過」**:改掛這個動作本身就是使用者親口說
    # 「這一群不純」,而那是**他給的證據**,不是工具的猜測。套用名字時
    # 據此跳過聲紋登記(見 _apply_names)
    state = dict(audit or {})
    moved = {int(r["speaker"]) for r in chosen}
    state["reassigned"] = sorted(set(state.get("reassigned") or []) | moved)
    gr.Info(
        f"已把 {changed} 段改掛給「{name}」。"
        "⚠️ 這一位這次不會被登記進聲紋庫——你改掛過,表示這一群不只一個人。",
        title="改掛完成",
    )
    new_preview, moved_in_preview = audit_mod.reassign(preview_text or "", blocks, name)
    new_preview = audit_mod.note_reassigned(
        new_preview, labels, name, moved_in_preview,
    )
    return gr.update(value=new_preview), gr.update(value=[]), state


def _audit_close():
    """關面板、把預覽切回來。"""
    return gr.update(visible=False), gr.update(value=None), gr.update(visible=True)


def _servable(files) -> list:
    r"""過濾出「gradio 供應得了」的檔案,給下載區用。

    gradio 只肯供應 cwd / 系統暫存 / 自家快取 / `launch(allowed_paths=)`
    列出來的目錄裡的檔案;其餘會在 **postprocess** 階段丟
    `InvalidPathError`,而那時整個事件的 outputs 一個都不會落地——前端
    完全沒反應,黑視窗一串英文 traceback(使用者 2026-08-06 用「重設講者」
    開 `C:\SOURCE5\錄音\…md` 時實際踩到)。

    **不能靠加 allowed_paths 解決**:那份 md 在使用者自己的資料夾裡,是
    動態路徑(同「文字、圖像→MD」的產出不做下載區的理由)。所以改成
    「供應不了的就不要放進下載區」——`paths_state` 仍然拿完整清單,
    套用名字寫回的是那一份,功能不受影響,只是少一顆下載鈕。

    白名單**照抄 gradio 的規則**(工作目錄 / 系統暫存 / allowed_paths),
    不自創一套更嚴的:自創的話會把 gradio 其實供應得了的檔案也擋掉,而
    症狀同樣是「下載鈕不見了」、一樣難查。工作目錄就是 repo 根目錄,
    `output/` 在它底下;allowed_paths 目前只有 pending(見 main)。

    ⚠️ **只准由兩個 gate 呼叫**:`_naming_page_updates`(轉檔收尾/開頁
    還原)與 `_page_reset_updates`(各收工點)。原本是每個收尾點各自
    記得,而漏掉的那個就成了上面那個 bug;測試以 AST 反向守著,不得再
    長出第三個產生點(test_servable_gates_every_download_value)。
    更深的一層(元件自己的 postprocess)在 gradio 6.20 走不通,理由記在
    `docs/dev/ui.md`。"""
    roots = [
        paths.repo_root().resolve(),
        Path(tempfile.gettempdir()).resolve(),
        pending.pending_dir().resolve(),
    ]
    out = []
    for p in files or []:
        try:
            resolved = Path(p).resolve()
        except OSError:  # pragma: no cover — 路徑長度之類的極端情況
            continue
        if any(resolved.is_relative_to(r) for r in roots):
            out.append(p)
    return out


def _page_reset_updates(downloads_files, keep_context: bool = False) -> tuple:
    """整頁復位的共用前段(套用收工/轉檔鏈復位/開始錄音/跳過命名四處
    共用):下載區、預覽清空、30+1 個命名框藏回、vp/paths 狀態歸零、
    試聽整組復位。呼叫端各自追加尾端更新(路徑欄/雙鈕);唯一的參數是
    下載區——套用收工要「暫留成品」供自動下載 js 取址(傳檔案清單),
    其餘三處清空(傳 None)。

    清單一律在這裡過 `_servable`,呼叫端不必記得:連同
    `_naming_page_updates`,這是全檔僅有的兩個「下載區的值」產生點
    (漏一處的症狀見 _servable,測試以 AST 反向守著不得再冒出第三個)。"""
    cleared = [
        # 候選的琥珀底色掛在欄位的 class 上,復位要一起歸零
        # (理由見 ui_style.rival_classes)
        gr.update(visible=False, value="", info=None, elem_classes=[])
        for _ in range(MAX_SPEAKERS + 1)  # 30 個講者框+1 個「未知」框
    ]
    return (
        # None = 清空,原樣傳下去(gr.Files 的空值語意,不要順手改成 [])
        None if downloads_files is None else _servable(downloads_files),
        # ⚠️ **預覽要一起「顯示回來」**(2026-08-14 使用者實機踩到):
        # 核對面板打開時預覽是被藏起來讓位的,而套用名字/跳過命名這條路
        # 不會經過「完成核對」——只清值不改 visible 的話,收工之後右半邊
        # 就一直是空的,而且**再也回不來**(下一次轉檔也只清值)
        gr.update(value="", visible=True), *cleared,
        # ⚠️ **keep_context=True 時這幾個 state 一律 gr.skip()**(2026-08-18
        # 實機踩到):「重新分群」的復位跟下一步在**同一條鏈**上,而下一步
        # 的輸入正是 paths_state 與分群檔路徑——清掉的話按下去只會回一句
        # 「找不到這份逐字稿的分群檔」,而畫面上明明有那顆按鈕。
        # 轉檔鏈那條沒踩到是因為它的輸入是 src_path,而 _start_run 留著它
        gr.skip() if keep_context else {},
        gr.skip() if keep_context else [],
        *_audition_reset_updates(),
        *_audit_reset_updates(),
        # 重新分群那一列:收工就收起來;同一條鏈上的復位則原樣留著
        # (跟著閃一下再回來只是視覺雜訊)
        *((gr.skip(), gr.skip(), gr.skip()) if keep_context
          else (gr.update(visible=False), None,
                gr.update(value=0, elem_classes=["recluster-n", "now-0"]))),
    )


def _reset_for_recluster() -> tuple:
    """「重新分群」前的整頁復位。

    **具名而不是 lambda**:它與 `_run_recluster` 是同一條鏈上的兩步,而
    「復位保不保留下一步要讀的東西」正是 2026-08-18 踩過的坑——具名之後
    測試才盯得到接線(`demo.fns` 只認得出有名字的函式),換回
    `_page_reset_updates(None)` 會當場變紅。"""
    return _page_reset_updates(None, keep_context=True)


def _audition(spk, clips, playing):
    """試聽按鈕:該講者「最長一句」片段從頭自動播放;再按同一顆(=正在播
    這位)則停止,按另一位直接換片段重播。無播放器介面(使用者指定
    2026-07-18:按了就播、停止就停,不要跳出播放器)——gr.Audio 只當
    出聲載體,由 CSS 移出畫面;一律只動 value,絕不動 visible(gradio 6
    對 visible=False 整個不渲染,前端沒有元件就不會出聲)。
    回傳:載體 value、playing 狀態、31 顆鈕的字樣。"""
    clips = clips or {}
    if playing == spk or spk not in clips:
        return (gr.update(value=None), None, *_aud_btn_updates(None))
    # playback_position 必須明確歸零:同一片段重播時,前端會停留在上次的
    # 結尾位置,一開播就觸發播畢(Playwright 實測 0.2~0.4 秒即「播完」),
    # 聽起來像按了沒聲音
    return (
        gr.update(value=clips[spk], playback_position=0),
        spk,
        *_aud_btn_updates(spk),
    )


def _audition_ended():
    """片段播放到結尾(Audio.stop 事件):按鈕字樣復原,並清掉載體 value
    ——同一顆再按時才有「None→路徑」的變化可觸發 autoplay(值不變時
    gradio 前端不會重播)。"""
    return (gr.update(value=None), None, *_aud_btn_updates(None))


# ---- 轉檔成果 → 整組 UI 更新(形狀契約見 PAGE_UPDATE_LEN)----

# 「成品/命名區」一批更新的值數量:下載區+預覽+30 命名框+「未知」框
# +vp/paths 兩個 state+試聽整組(clips/playing/播放器+30+1 顆鈕)
# +核對整組(audit_state+面板 4 件+30+1 顆核對鈕,見 _audit_reset_updates)。
# 這是 _present_result / _restore_pending / _page_reset_updates 共同的
# 回傳長度,也等於 build_ui 裡 page_outputs 的元件數——公開給測試引用,
# 免得同一串算式在 src/tests 各抄一份(增減元件時漏改一處就靜默失準)
PAGE_UPDATE_LEN = (
    2 + MAX_SPEAKERS + 1 + 2 + 3 + MAX_SPEAKERS + 1
    + 1 + 5 + MAX_SPEAKERS + 1
    + 3  # 重新分群那一列(顯示與否)+ 分群檔路徑 + 人數欄的值
)

def _run(src_text, model_label, num_speakers, cpu_cores=None, recursive=False,
         mode=None, progress=gr.Progress()):
    """「開始轉檔」(聲音→MD):把關路徑 → 轉檔 → 整組 UI 更新。

    **兩種模式,由輸入的形狀決定**(使用者 2026-08-06;判準見
    srcfile.looks_like_batch,文案見 _SRC_MODE_HINT):

    - 剛好一個**檔案** → run_pipeline → output/ + 講者命名 + 試聽 + 下載。
      講者編號每檔獨立分群,命名是一檔一檔當場做的事;這正是 2026-07-26
      「一次一檔」那條裁決的內容,對單檔仍然成立。
    - 多個路徑、或任何**資料夾** → _run_batch(走 doc2md 那條批次路徑)。
      講者分析照做、標「講者 N」,但不開命名介面——一批幾十個檔沒有
      「當場」可言,硬做只會把這一檔的名字寫進別檔。

    錯誤一律以繁中 gr.Error 浮出(spec §8):把關問題是「提醒」,轉檔
    失敗是「轉檔失敗」;兩者都 print_exception=False——黑視窗印 traceback
    會嚇到非技術使用者(實際回報過),真正的 traceback 由 logger 印。
    報錯時本函式的 outputs 不落地,路徑留在欄位供直接重試(_after_run
    據此決定「開始」亮不亮)。"""
    # 互斥檢查放這一步(不是鏈頭 _start_run):鏈頭不得拋例外,否則
    # .failure 沒人接、介面會鎖死(見 _start_run 的 docstring)
    if _converting["on"]:
        raise _busy_error("文件轉檔")
    # 「已經有一份逐字稿在轉」也要擋(2026-08-08 使用者踩到的路徑上長出來
    # 的洞):平常靠介面狀態擋住——轉檔中「開始」是鎖的——但斷線後
    # reload,介面整個回到初始狀態,那層保護就沒了,重新選檔案按下去會真的
    # 跑第二份。引擎快取非執行緒安全,而 cancel 的旗標是全域單例:兩份同時
    # 跑的話,任一顆停止鈕會把兩份一起殺掉。
    # ⚠️ 判準是 runstate 不是 `_transcribing`——後者在鏈頭 `_start_run`
    # 就舉起來了,拿它來擋等於每次都擋到自己
    if runstate.active():
        raise _busy_error("逐字稿轉檔")
    if mode == _MODE_RELABEL:
        return _run_relabel(src_text, cpu_cores, progress)
    if srcfile.looks_like_batch(src_text):
        return _run_batch(
            src_text, model_label, num_speakers, cpu_cores, recursive, progress,
        )
    try:
        src = srcfile.validate(src_text)
    except UserFacingError as e:
        raise gr.Error(str(e), title="提醒", print_exception=False) from None
    _apply_cpu_cores(cpu_cores)
    cancel.reset()  # 上一次按過的停止不得波及本次
    # 進度同時落地成伺服器端狀態:gr.Progress 綁在這一次事件執行上,
    # 分頁被瀏覽器節流、使用者按下「重新連線」(=reload)之後就再也接不
    # 回來——畫面連停止鈕都不見了,而轉檔還在跑(見 runstate 模組 docstring)
    runstate.begin(src.name)
    # ⚠️ 巢狀 try 是刻意的:`runstate.end()` 必須晚於 `_present_result`
    # ——命名進度是在那裡面才由 `pending.persist` 落地的。若在拿到 result
    # 當下就 end,中間會有一個「轉檔已結束、但命名還沒落地」的窗口,而
    # 秒針(`_run_tick`)正好在那個窗口會判定「轉完了又沒有命名資料」,
    # 把畫面解鎖清空——剛跑完幾十分鐘的成果就這樣從畫面上消失。
    # 窗口只有寫一個檔案那麼短,但那是每次轉檔都會經過的一條路。
    try:
        try:
            result = run_pipeline(
                src, OUTPUT_DIR,
                model_key=MODEL_LABELS[model_label],
                num_speakers=_normalize_speakers(num_speakers),
                on_stage=lambda stage, frac: progress(frac, desc=f"{src.name}:{stage}"),
            )
        except cancel.Cancelled:
            # 按停止:半成品已由 pipeline 清掉,畫面回到等待下一檔
            return (*_present_result(
                [], "已依要求停止,本檔案尚未轉完;要重新轉檔請再選一次檔案。",
                0, {}, {}, {},
            ), None)
        except UserFacingError as e:
            # 自家模組精心寫的繁中訊息,可直接給使用者;第三方函式庫的
            # cryptic 英文則落到下方通用文案
            logger.exception("處理失敗:%s", src.name)
            raise gr.Error(str(e), title="轉檔失敗", print_exception=False) from None
        except Exception as e:
            logger.exception("處理失敗:%s", src.name)
            raise gr.Error(
                "發生未預期的錯誤,詳情見終端機視窗;"
                "若為記憶體不足,請改選「快速」模型再試",
                title="轉檔失敗", print_exception=False,
            ) from e
        device_name = DEVICE_NAMES.get(result.device, result.device)
        hints = result.speaker_hints or {}
        # 試聽片段從「原始檔」剪(管線的暫存 wav 已清除);與命名欄的摘錄
        # 同一句,聽到的就是看到的那句
        clips = _cut_speaker_clips(src, hints)
        # 尾端 None 清空路徑欄(完成即還原、等下一檔,使用者規格)
        return (*_present_result(
            [str(p) for p in result.outputs],
            f"(執行裝置:{device_name})\n\n{result.preview}",
            result.speakers, result.voiceprints or {}, hints, clips,
            audit=_audit_payload(result, src),
            audit_flags=_audit_flags(result.quality),
        ), None)
    finally:
        # 完成/停止/報錯三條路都要放掉:留著的話下一次轉檔會被自己的
        # 互斥檢查擋在門外,而且開頁還原會一直以為有一份轉檔在跑
        runstate.end()


def _choice_layout(known: list[str], rivals) -> tuple[list[str], list[str]]:
    """名字選單的**順序**與**候選標示**;回 (選項, elem_classes)。

    聲紋分不開的那幾位排到最前面(其餘維持名單原順序),並讓 CSS 把那
    幾筆畫成淺琥珀底、補上「聲音接近的 / 全部名單」兩個分組小標(設計稿
    D 案,使用者 2026-08-16 選定;收起來不留記號也是他指定的)。

    ⚠️ **兩件事出自同一次計算,不是兩條各自的算式**:CSS 認的是「選單
    最前面 N 筆」,N 一旦與實際前綴長度對不上,底色就會落在別人身上
    ——而標錯人比不標更糟。共用同一個 `head` 之後,這件事不必靠註解或
    測試維持,它是同一個變數的兩種用法。

    ⚠️ **選項字串只重排、不加任何標記**:這個欄位是 `allow_custom_value`
    的下拉,**選項字串就是最後寫進逐字稿與 `data/voiceprints.npz` 的
    名字**——把「★」寫進選項,那個星號就會原樣變成人名。標示一律做在
    CSS(見 `ui_style.rival_classes` 與那段「候選標示」),選項因此一路
    乾淨,收名字那一關不必剝任何東西。突變 M185 守著這條。

    ⚠️ **候選就算不在與會名單裡也要列進來**:名單與聲紋庫是兩份資料,
    聲紋庫記得的人不見得還在名單上。info 都已經把名字講出來了,選單裡
    卻找不到,只會逼使用者自己打字——而打錯一個字就是聲紋庫裡多一個人。"""
    head = list(dict.fromkeys(rivals or ()))      # 保序去重
    if not head:
        return known, []
    seen = set(head)
    return (head + [n for n in known if n not in seen],
            ui_style.rival_classes(len(head)))


def _name_section_updates(count, hints, clips, names, audit_flags=(),
                          has_audit=True, rivals=None):
    """命名框/「未知」框/試聽鈕的整組更新(_present_result 與
    _restore_pending 共用)。names={講者標籤: 欄位值}:轉檔完成時是
    自動辨識預填、開頁還原時是落地草稿;缺鍵留白。

    「維持隱藏」的元件一律送 gr.skip(),不得送 visible=False 更新:
    gradio 6.20 前端對「隱藏中(未渲染)」的元件收到帶 visible=False 的
    更新,會把它連同**舊 props** 重新掛載——上一檔講者較多時,舊講者框
    整排帶著舊摘錄冒回來(使用者回報 7 講者→2 講者;Playwright 攔 SSE
    證實伺服器送的旗標正確、是前端套用層的問題)。skip 安全的前提:
    呼叫端進來時所有講者框/試聽鈕已是隱藏且清空(轉檔鏈經
    _start_run、錄音經 _reset_for_new_recording、開頁還原
    則是建構時的初始狀態)。"""
    # 下拉選單來源:與會人員名單(「與會人員名稱維護」那一欄)+ 只在聲紋庫
    # 裡的那些(見 _all_names——選不到就只能打字,打錯就是庫裡多一個人)
    known = _all_names()
    # 哪幾列會亮「🔍 核對」——線索文字要據此指路,不能叫人去按一顆不存在
    # 的鈕(使用者 2026-08-15 截圖抓到:候選給每一位,而鈕只給前三位)
    flagged = set(audit_flags or ()) if has_audit else set()
    updates = []
    for i in range(MAX_SPEAKERS):
        if i < count:
            # 聲紋分不開的那幾位:候選排到選單最前面,並在線索那行並列講出
            # 來(3 案,使用者 2026-08-15 選定)。認得出來的人 rivals 是空的
            # ——那時這一列與先前完全一樣
            mine = (rivals or {}).get(i) or []
            # 選單順序與候選的琥珀底色出自同一次計算(見 _choice_layout);
            # 認得出來的人 mine 是空的,marks 就是空清單——**要照送**,
            # 不送的話上一檔的 class 會留在這一欄上
            choices, marks = _choice_layout(known, mine)
            # ⚠️ **填好名字的那一列收起線索**(使用者 2026-08-18 選定):
            # 線索是拿來「認出這是誰」的,認完就只剩佔位——實測每列 84px,
            # 十位講者填到第八位時畫面有 672px 是已經用不到的東西。
            # 原文存進 clues 交給 _toggle_clues:**清空名字就放回來**,
            # 所以這不是丟掉資訊,是收合
            updates.append(gr.update(
                visible=True, choices=choices, elem_classes=marks,
                value=names.get(i, ""),
                label=f"講者 {i + 1} 的名字",
                info=_hint_text(hints.get(i), mine),
            ))
        else:
            updates.append(gr.skip())
    # 「未知」命名框:逐字稿有未知段落(自動偵測時與每位講者都不夠像的
    # 零碎語音)才顯示。此框只改逐字稿文字、絕不登記聲紋(未知常是多人
    # 重疊的混合,質心混雜,登記會污染聲紋庫)——info 明示,免使用者疑慮
    # ⚠️ **有核對可用時,「未知」那一列只留核對鈕**(使用者 2026-08-13 指定,
    # 理由是他自己的使用經驗:「未知我常聽,裡面通常都混著好幾個人」):
    # - **命名框拿掉**:給整批未知一個名字,正是檔尾診斷明文勸阻的事
    #   (它常是多人混合)。工具一邊勸阻、一邊提供那個框,是自相矛盾的;
    #   真的想統一叫「其他」也做得到——打開核對、在清單裡選起來、指定
    #   名字,那條路至少逼你看過每一段。
    # - **試聽鈕拿掉**:它只播「最長一句」,而混合群裡那一句是誰的都不知道
    #   ——聽了只會**給錯誤的信心**,那正是 0812 那次的病因(試聽播到本人
    #   那一句,聽完更確信,而整群其實混了七個人)。
    # ⚠️ **沒有核對可用時(沒有音檔)仍保留舊的框**:那時它是唯一能處理
    # 未知的路,收掉等於什麼都不能做。
    unknown_hint = hints.get(UNKNOWN_SPEAKER)
    if unknown_hint and not has_audit:
        unknown_update = gr.update(
            visible=True, choices=known, value=names.get(UNKNOWN_SPEAKER, ""),
            info=f"只改逐字稿文字、不會登記聲紋・{_hint_text(unknown_hint)}",
        )
    else:
        unknown_update = gr.skip()  # 維持隱藏:不得送 visible=False(見上)
    # 試聽鈕跟著對應命名框亮:有該講者的片段才顯示(剪失敗的講者沒有鈕,
    # 只剩文字摘錄);沒亮的維持隱藏(gr.skip,見上)——字樣不必重設,
    # 進來前的整頁復位已把整排鈕復位成「試聽」
    aud_updates = [
        gr.update(visible=True, value=_AUD_PLAY_LABEL)
        if (i < count and i in clips) else gr.skip()
        for i in range(MAX_SPEAKERS)
    ]
    unknown_aud_update = (
        gr.update(visible=True, value=_AUD_PLAY_LABEL)
        if (unknown_hint is not None and UNKNOWN_SPEAKER in clips and not has_audit)
        else gr.skip()
    )
    # 「🔍 核對」只亮在**該核對的那幾列**(使用者 2026-08-13 選案 B):
    # 「未知」一定亮(那一批本來就常是多人混合),其餘只有被檔尾診斷點名
    # 「建議優先核對」的才亮。版面因此零新增——其他列一個像素都沒變,
    # 而力氣被導到最該花的地方(與 export.check_first 同一套哲學)
    # ⚠️ **沒有核對資料就一顆都不亮**:顯示與資料必須是同一個判準。第一版
    # 只看「有沒有未知」,於是重新整理之後鈕還在、按下去卻是空的
    # (使用者 2026-08-13 實機踩到)
    # `flagged` 在上面算過一次(線索文字要用它決定指哪顆鈕),這裡沿用
    # 同一份——各算一份的話,文字與鈕遲早各走各的,而那正是這次的 bug
    audit_updates = [
        gr.update(visible=True) if i in flagged else gr.skip()
        for i in range(MAX_SPEAKERS)
    ]
    unknown_audit_update = (
        gr.update(visible=True)
        if (has_audit and unknown_hint is not None) else gr.skip()
    )
    return (updates, unknown_update, aud_updates, unknown_aud_update,
            audit_updates, unknown_audit_update)


def _run_batch(src_text, model_label, num_speakers, cpu_cores, recursive, progress):
    """多檔/資料夾模式:整批連續轉,不做講者命名(使用者 2026-08-06)。

    **走的是「文字、圖像→MD」那條批次路徑**(docpipe.convert_batch),
    音訊只是路由表上多幾個副檔名(見 docaudio)。不另寫一條批次迴圈:
    「原地輸出」「同名 md 就跳過」「先整批規劃再動手」「單檔失敗不中斷」
    「報告列出完整路徑」這些規則每重寫一次就是一次走樣的機會,而它們在
    那邊都已經有測試守著。

    回傳形狀與 _run 相同(整組 UI 更新 + 尾端清空路徑欄),但命名相關的
    全部給空值:沒有講者框、沒有試聽、沒有 pending 落地。下載區也是空的
    ——成品在原檔案旁邊,50 個檔用瀏覽器下載是災難(同文件分頁的決定)。
    """
    try:
        files, skipped = docsrc.validate_batch(
            src_text, recursive=bool(recursive),
            # 音訊自己那份白名單:混用會讓這顆「開始轉檔」開始接受 PDF
            types=srcfile.SUPPORTED_TYPES,
            what="錄音或錄影檔", hint=srcfile.supported_hint(),
        )
    except UserFacingError as e:
        raise gr.Error(str(e), title="提醒", print_exception=False) from None
    _apply_cpu_cores(cpu_cores)
    cancel.reset()
    # 批次一樣要進 runstate:一批幾十個檔跑更久,斷線的機會只多不少。
    # **逐檔進度直接沿用 docpipe 給的那句話**——它已經把「第幾個/共幾個
    # + 檔名」組好了(`(3/12) 月會.m4a`),不必在這裡重組一份會走樣的
    runstate.begin(f"{len(files)} 個檔案", kind=runstate.KIND_BATCH)

    def batch_stage(stage: str, frac: float) -> None:
        progress(frac, desc=stage)
        runstate.note(stage, frac)

    try:
        report = docpipe.convert_batch(
            files, skipped,
            on_stage=batch_stage,
            options={
                "model_key": MODEL_LABELS[model_label],
                # 使用者填的人數套用到整批(欄位就在畫面上,填 0 = 自動偵測)
                "num_speakers": _normalize_speakers(num_speakers),
            },
        )
    except UserFacingError as e:
        logger.exception("批次轉檔失敗")
        raise gr.Error(str(e), title="轉檔失敗", print_exception=False) from None
    except Exception as e:
        logger.exception("批次轉檔失敗(未預期)")
        raise gr.Error(
            "發生未預期的錯誤,詳情見終端機視窗;"
            "若為記憶體不足,請改選「快速」模型再試",
            title="轉檔失敗", print_exception=False,
        ) from e
    finally:
        runstate.end()
    return (*_present_result(
        [], docpipe.report_markdown(report), 0, {}, {}, {},
    ), None)


def _run_relabel(src_text, cpu_cores, progress):
    """「重設講者」:讀現成逐字稿 → (有媒體檔就剪試聽+抽聲紋)→ 命名介面。

    使用者 2026-08-06 指定。解決的是三件事後才發現的事:批次轉出來的 md
    只有「講者 1／2／3」、當初命名打錯字、當初跳過命名。

    **不重跑轉檔**:分群早在當初就做完了,md 裡有講者標籤與時間戳。所以
    兩種介面(使用者指定):

    - 同一層沒有同名媒體檔 → 只有命名欄位(照樣看得到「共 N 段發言・
      最長一句摘錄」,那全部解析自 md 本身);
    - 有 → 多出 ▶️ 試聽,而且**抽聲紋登記**,下次開會自動認人
      ——與轉檔後的命名流程完全一致。

    共用的是收尾那一整套:_present_result 建命名欄位、_apply_names 改寫並
    登記聲紋、pending 落地(睡眠/斷線後 F5 接得回)。這裡只負責把
    「outputs / preview / 講者數 / 聲紋 / 線索 / 試聽片段」湊出來。"""
    try:
        md_path = relabel.validate(src_text)
        transcript = relabel.parse(relabel.read(md_path))
    except UserFacingError as e:
        raise gr.Error(str(e), title="提醒", print_exception=False) from None
    _apply_cpu_cores(cpu_cores)
    cancel.reset()

    hints = transcript.hints()
    # 「未知」不佔講者編號:命名框那邊它是獨立的一欄,而且**絕不登記聲紋**
    # (多人零碎語音的混合,登記會污染聲紋庫)。改鍵成哨兵,_present_result
    # 那一整套就原封不動地認得它
    named = [n for n in transcript.order if n != "未知"]
    hints = {
        (UNKNOWN_SPEAKER if transcript.order[i] == "未知" else named.index(
            transcript.order[i],
        )): h
        for i, h in hints.items()
    }
    media = relabel.find_media(md_path)
    # 分群檔在不在,決定第三種狀態——**而且要在分析之前讀**:有它的話聲紋
    # 與每段相似度都直接從裡面算,一個字節的音訊都不必碰(見 _from_features)。
    # ⚠️ **存在不等於用得動**:換過聲紋模型或格式舊了的檔要當成沒有,而且
    # 要把原因講出來——擺一顆按下去才報錯的鈕,比不擺更糟
    features = relabel.find_features(md_path)
    feat = None
    feat_note = ""
    origin = ""
    if features is not None:
        try:
            feat = diarize.load_features(features)
            origin = (
                "自動判斷" if feat.num_speakers == 0
                else f"當初指定 {feat.num_speakers} 位"
            )
        except UserFacingError as e:
            features = None
            feat_note = f"\n{e}"
    voiceprints: dict = {}
    clips: dict = {}
    audit = _audit_payload_from_transcript(transcript, named, media)
    if media is not None:
        try:
            voiceprints, clips, cohesion = _analyse_for_relabel(
                md_path, media, transcript, named, progress,
                blocks=audit.get("blocks"), feat=feat,
            )
            for b, c in zip(audit.get("blocks") or [], cohesion):
                b["cohesion"] = float(c)
        except cancel.Cancelled:
            raise
        except Exception:
            # 分析失敗只是「少了試聽與聲紋」,命名本身照樣做得到——
            # 為了它擋掉整個功能不划算(同 _cut_speaker_clips 的取捨)
            logger.exception("重設講者:媒體檔分析失敗,只提供命名")
            gr.Info("找到媒體檔但分析失敗,這次只能手動命名(詳見紀錄檔)。")
    have_media = media is not None and voiceprints
    if not have_media:
        tail = (
            "\n同一層沒有同名的錄音檔——這次只能改名字:沒有試聽、"
            "不會記住聲紋,也不能改人數。名字照樣寫得回逐字稿。"
        )
    elif features is None:
        tail = (
            f"\n同一層找到「{media.name}」:可以試聽原音,命名後也會記住聲紋、"
            "下次開會自動認人。"
            + (feat_note or
               f"\n沒有找到分群檔「{diarize.features_path(md_path).name}」,"
               "所以這份改不了講者人數——分群檔是新版轉檔才會留下的,"
               "舊的逐字稿沒有。要改人數只能重轉一次。")
        )
    else:
        tail = (
            f"\n同一層找到「{media.name}」與分群檔「{features.name}」:"
            "可以試聽、命名後記住聲紋,而且**可以直接改講者人數重新分群**,"
            "幾秒鐘就好、不必重轉。"
        )
    note = (
        f"已讀取「{md_path.name}」,共 {len(named)} 位講者"
        + (f"(當初是{origin}分出來的)" if origin else "")
        + "。" + tail
    )
    return (*_present_result(
        [str(md_path)], f"{note}\n\n{relabel.read(md_path)}",
        len(named), voiceprints, hints, clips,
        # 核對在這條路一樣可用(使用者 2026-08-13 問起):既有逐字稿 +
        # 同層的錄音檔就夠了,不必重轉一支兩小時的檔。⚠️ **哪幾列亮**
        # 在這裡沒有分群品質可依據(那是轉檔當下才算得出來的),所以
        # 只亮「未知」——真正常需要核對的也是它
        audit=audit, features=features,
    ), None)


def _check_recluster(count, features, paths) -> None:
    """按下「重新分群」的把關。**掛在鏈頭、在整頁復位之前**。

    ⚠️ **順序就是這條的重點**(2026-08-18 使用者回報「每次還要按 F5」):
    原本檢查寫在 `_run_recluster` 裡,而它前面那一步已經把整頁復位了——
    一拋錯,畫面停在被清空的狀態、又沒有東西填回去,只能重新整理。改成
    第一步就檢查:`.then` 只在前一步成功後才跑(gradio 6.20 實測,見
    docs/dev/ui.md),所以擋下來的時候**復位根本沒有發生**,畫面原封不動。

    ⚠️ **前端已經先擋了一層**(按鈕會變灰,見 `NAME_PANEL_JS`),這一支是
    安全網:前端的判斷可以被繞過(改 DOM、舊分頁),而「按了也沒用」的兩種
    情況都會讓使用者付出代價(畫面重來、名字清掉),不能只靠畫面把關。

    ⚠️ **比對的是「試算出來的位數」,不是使用者填的數字**(2026-08-19 實機
    回報:填 13、14、15、16、17 全都得到 10 位)。md 的段落是原子的,新分出
    來的一兩秒碎群拿不到任何段落,就不會出現在逐字稿上——拿填的數字去比,
    那五次全部放行,每一次都白跑一趟、又把已填的名字清光。試算走
    `relabel.count_after_recluster`(= `recluster_md` 本人,不另算一份),
    代價是這裡與 `_run_recluster` 各讀一次分群檔(各 0.3 秒),換掉的是
    「整頁重跑一遍才發現什麼都沒變」。"""
    if not features or not paths:
        raise gr.Error(
            "找不到這份逐字稿的分群檔,不能改人數。", title="提醒",
            print_exception=False,
        )
    n = _normalize_speakers(count)
    if n <= 1:
        # 0 是自動偵測——而自動判斷正是要改掉的那個結果;1 則是把整份逐字稿
        # 都掛到同一位名下,那不是「分講者」而是「取消分講者」
        raise gr.Error(
            "請填 2 以上的講者人數。0 是自動偵測(那正是你要改掉的結果);"
            "1 等於把整份逐字稿都算成同一個人,不必經過重新分群。",
            title="提醒", print_exception=False,
        )
    md_text = relabel.read(Path(paths[0]))
    now = len([x for x in relabel.parse(md_text).order if x != "未知"])
    try:
        feat = diarize.load_features(features)
        turns, _vps, _quality = diarize.recluster(feat, n)
    except UserFacingError as e:
        raise gr.Error(str(e), title="提醒", print_exception=False) from None
    if relabel.count_after_recluster(md_text, turns) != now:
        return
    if n == now:
        raise gr.Error(
            f"這份逐字稿現在就是 {now} 位講者,不必重新分群。"
            "要改成別的位數再按一次。",
            title="提醒", print_exception=False,
        )
    raise gr.Error(
        f"填 {n} 位,在這份逐字稿上分出來還是 {now} 位——跟現在一樣,所以"
        "沒有重跑,已經填好的名字都還在。逐字稿的段落是當初那次分群切出來"
        "的,一個段落只能歸一位;多分出來的都是一兩秒的碎段,會被併回鄰近"
        "的講者。要再分出更多人只能重轉一次。",
        title="提醒", print_exception=False,
    )


def _run_recluster(count, features, paths, cpu_cores, progress=gr.Progress()):
    """「重新分群」:拿分群特徵檔重算 → 改寫 md 的講者標籤 → 回到命名流程。

    **不重轉、也不重讀音訊做分群**:貴的那一段(切分+抽聲紋)轉檔當下就
    存下來了,這裡只做重聚——實測 0.3 秒。之後照樣走 `_run_relabel`,所以
    試聽、聲紋登記、核對那一整套完全共用。

    ⚠️ **重分群等於推翻上一次的命名,所以一律強制重新命名**(使用者
    2026-08-18 指定):講者編號整個換過了,舊名字對應的那一位已經不存在。
    走 `_run_relabel` 天然就是這個行為(它從改寫後的 md 重新建命名欄位)。

    ⚠️ **上一輪已經 enroll 進聲紋庫的樣本不會自動撤銷**——那要使用者自己
    去「🩺 聲紋健檢」看。這一點寫在使用說明裡,不在這裡默默處理:程式沒有
    立場判斷哪一次的命名才是對的。"""
    # 把關全部在 _check_recluster,而且是**鏈頭**那一步(理由見那支)
    n = _normalize_speakers(count)
    md_path = Path(paths[0])
    try:
        feat = diarize.load_features(features)
        turns, _vps, quality = diarize.recluster(feat, n)
    except UserFacingError as e:
        raise gr.Error(str(e), title="提醒", print_exception=False) from None
    md_path.write_text(
        relabel.recluster_md(relabel.read(md_path), turns, quality),
        encoding="utf-8",
    )
    logger.info("重新分群:%s → %d 位講者", md_path.name, n)
    return _run_relabel(str(md_path), cpu_cores, progress)


def _relabel_cohesion(wav, blocks, progress) -> list[float]:
    """每一輪發言對「**自己那一群**的質心」的相似度。

    ⚠️ **要分群算,不能全部混在一起比**:每一位講者各有自己的聲音,拿全場
    共同的平均當基準的話,聲音特別的人整排都會偏低——那個數字就變成
    「你像不像平均值」,不是「這一段像不像同一位講者」。

    算不出來就整排 0(顯示空白):相似度是輔助,聽與改掛不靠它。"""
    import numpy as np

    spans = [(b["start"], b["end"]) for b in blocks]
    try:
        samples = audio.read_wav16k(wav)
        vecs = diarize._extract_embeddings(
            samples, spans,
            progress=lambda f: progress(0.75 + 0.18 * f, desc="計算每段的相似度"),
        )
    except Exception:
        logger.exception("重設講者:相似度算不出來(不影響聽與改掛)")
        return []
    out = [0.0] * len(blocks)
    by_speaker: dict[int, list[int]] = {}
    for i, b in enumerate(blocks):
        by_speaker.setdefault(int(b["speaker"]), []).append(i)
    for idxs in by_speaker.values():
        weights = np.array([spans[i][1] - spans[i][0] for i in idxs], dtype=float)
        centroid = diarize._wcentroid(vecs[idxs], np.maximum(weights, 0.01))
        for i in idxs:
            out[i] = float(vecs[i] @ centroid)
    return out


def _from_features(feat, transcript, named, blocks):
    """有分群檔時的快路:(聲紋質心, 每輪發言的相似度),全部取自 npz。

    ⚠️ **這條路一個字節的音訊都不讀**——原本的慢主要在兩件事:把整份音訊
    轉成 16k、以及**把每一輪發言各抽一次聲紋**(使用者 2026-08-18 回報
    「計算每段的相似度跑得較久」,而他手上那份有 417 輪)。兩者要的向量
    轉檔當下就抽好在 .分群.npz 裡了。"""
    import numpy as np

    spans_by_name = transcript.spans()
    vp: dict[int, np.ndarray] = {}
    for name, spans in spans_by_name.items():
        if name == "未知":  # 多人零碎語音的混合,絕不登記聲紋
            continue
        vecs = diarize.block_vectors(feat, spans)
        good = vecs[np.linalg.norm(vecs, axis=1) > 0]
        if not len(good):
            continue
        c = good.sum(axis=0)
        n = float(np.linalg.norm(c))
        if n > 0:
            vp[named.index(name)] = (c / n).astype(np.float32)
    cohesion: list[float] = []
    if blocks:
        bv = diarize.block_vectors(feat, [(b["start"], b["end"]) for b in blocks])
        cohesion = [0.0] * len(blocks)
        by_speaker: dict[int, list[int]] = {}
        for i, b in enumerate(blocks):
            by_speaker.setdefault(int(b["speaker"]), []).append(i)
        for idxs in by_speaker.values():
            rows = [i for i in idxs if np.linalg.norm(bv[i]) > 0]
            if not rows:
                continue
            w = np.array([blocks[i]["end"] - blocks[i]["start"] for i in rows],
                         dtype=float)
            centroid = diarize._wcentroid(bv[rows], np.maximum(w, 0.01))
            for i in rows:
                cohesion[i] = float(bv[i] @ centroid)
    return vp, cohesion


def _analyse_for_relabel(md_path, media, transcript, named, progress,
                         blocks=None, feat=None):
    """媒體檔 → (每位講者的聲紋質心, 試聽片段, 每一輪發言的相似度)。

    先轉 16k 單聲道再抽聲紋(`diarize.voiceprints_for_spans` 吃的就是那個
    格式);試聽片段則從**原始檔**剪,與轉檔後的流程同一個作法
    (_cut_speaker_clips:長錄音免全檔解碼)。

    ⚠️ **相似度在這裡一併算掉**(使用者 2026-08-14 指定):這條路本來就已經
    讀了整份音訊、也已經在抽聲紋,順手把每一輪發言各抽一次(實測 78ms/段,
    一場 2.7 小時的會議約多一分鐘),核對表才有「哪幾段可疑」的數字。
    ⚠️ **算完只留這一份**——它跟著命名進度落地(pending),中途關掉程式、
    重開之後還在;不做「每轉一次留一份」的快取,那是使用者明確不要的。"""
    hints = transcript.hints()
    spans = transcript.spans()
    cohesion: list[float] = []
    if feat is not None:
        # 有分群檔:聲紋與相似度都算得出來,而且是毫秒級(見 _from_features)
        progress(0.5, desc="讀分群檔")
        vp, cohesion = _from_features(feat, transcript, named, blocks)
        cancel.check()
    else:
        with tempfile.TemporaryDirectory(prefix=pipeline.TMP_PREFIX) as tmp:
            progress(0.05, desc=f"{media.name}:準備音訊")
            wav = audio.to_wav16k(media, Path(tmp))
            cancel.check()
            progress(0.15, desc=f"{media.name}:抽取聲紋")
            vp = diarize.voiceprints_for_spans(
                wav,
                {named.index(n): s for n, s in spans.items() if n != "未知"},
                progress=lambda f: progress(0.15 + 0.60 * f, desc="抽取聲紋"),
            )
            cancel.check()
            if blocks:
                progress(0.75, desc="計算每段的相似度")
                cohesion = _relabel_cohesion(wav, blocks, progress)
    progress(0.95, desc="剪試聽片段")
    # 線索的鍵在呼叫端已改成哨兵/序號,這裡要的是同一組;直接照 named 重建
    clip_hints = {
        (UNKNOWN_SPEAKER if transcript.order[i] == "未知"
         else named.index(transcript.order[i])): h
        for i, h in hints.items()
    }
    return vp, _cut_speaker_clips(media, clip_hints), cohesion


# 「改成幾位」底下那行限制說明(設計稿定案文案)。⚠️ **一定要寫**:
# md 的區塊是原子的,往多的方向改只能在現有段落之間重新分配,而使用者
# 填了 5 卻安靜地只拿到 3 是最糟的一種——他不會知道
# ⚠️ **只留那個限制**(2026-08-18 精簡:原本 46 字)。「在現有段落間重新
# 分配」是實作細節,使用者要知道的只有「往少改一定準、同一段裡的兩個人
# 拆不開」——完整說明在「❓ 使用說明」
_RECLUSTER_HINT = "往少改一定準;當初就在同一段裡的兩個人拆不開,要重轉。"


# 「沒有指定 features」與「指定為沒有」是兩回事,所以不能用 None 當預設:
# `_run_relabel` 判斷分群檔用不動(換過聲紋模型/格式舊了)時會**明確**傳
# None,那時候絕不可以又自己推一個回來
_DERIVE_FEATURES = object()


def _features_for(outputs) -> Path | None:
    """成品清單 → 同層同名的分群檔(沒有回 None)。

    ⚠️ **統一在匯流點推,不要各呼叫端各自傳**(2026-08-18 使用者回報:
    「設定講者的功能出現時,也要提供改成幾位講者」):命名區會出現的路徑有
    四條——檔案轉檔、現場收音、重設講者、開頁還原——當初只接了後兩條,
    於是同一份逐字稿「剛轉完沒有那一列、重新整理之後就有了」。漏掉一條
    使用者根本分不出是功能沒做還是這份檔不支援。"""
    for p in outputs or []:
        if str(p).lower().endswith(".md"):
            return relabel.find_features(Path(p))
    return None


def _naming_page_updates(outputs, preview, voiceprints, clips, section,
                         audit=None, naming=True,
                         features=_DERIVE_FEATURES, count=0) -> tuple:
    """「成品/命名區」那一批更新(PAGE_UPDATE_LEN 個值)的**唯一**組法。

    轉檔收尾(_present_result)與開頁還原(_restore_pending)送的是同一
    批元件、同一個順序,差別只在資料來自剛跑完的結果還是落地檔——原本
    兩邊各抄一份,而 `_servable` 就是這樣在 _restore_pending 那份漏掉的
    (使用者 2026-08-06 真機踩到)。`section` = _name_section_updates
    的四元組原樣傳進來。

    `naming=False` = 這份成品沒有任何人可以命名(見 `pending.anyone_to_name`):
    下載區與預覽照常給,但 `paths_state` 要留空——它是「有沒有東西在等命名」
    的判準(`_naming_focus` 據它收起左欄那整組),而命名區這時一個框都不會
    渲染,兩邊對不起來的下場就是「左欄只剩進階參數設定」。

    下載區的值只在這裡與 `_page_reset_updates` 產生,見那裡的註解。"""
    if features is _DERIVE_FEATURES:
        features = _features_for(outputs)
    (updates, unknown_update, aud_updates, unknown_aud_update,
     audit_updates, unknown_audit_update) = section
    return (
        # 下載區只放 gradio 供應得了的(見 _servable);paths_state 拿完整
        # 清單——套用名字寫回的是它,「重設講者」的 md 在使用者自己的
        # 資料夾裡,下載不了但照樣改得到
        _servable(outputs), gr.update(value=preview, visible=True),
        *updates, unknown_update,
        voiceprints, outputs if naming else [],
        clips, None, gr.update(value=None),
        *aud_updates, unknown_aud_update,
        # 核對:狀態帶著「這一份的區塊與音檔來源」,面板關著等使用者點
        audit, gr.update(value=None), gr.update(value=None),
        gr.update(value=[], choices=[]),
        gr.update(value="", choices=_all_names()), gr.update(visible=False),
        *audit_updates, unknown_audit_update,
        # 重新分群那一列:只有「同層真的有分群檔」時才出現(第三種狀態)
        gr.update(visible=features is not None),
        str(features) if features is not None else None,
        # ⚠️ **人數欄預設填「現在幾位」,不是猜一個建議值**(使用者
        # 2026-08-18 選定):同一天實測過兩種標準做法(合併相似度的斷層、
        # 譜分群的 eigengap),**都推算不出人數**——四份已知人數的錄音全被
        # 回答「1~2 人」(見 docs/dev/pipeline.md)。既然算不出來,欄位就
        # 該顯示**事實**;建議留給說明文字,兩件事不要混在同一個格子裡
        # ⚠️ **值與「現在幾位」一起送**:前端要拿它當基準,才判斷得出
        # 「使用者根本沒改」。class 是唯一送得進 DOM 的路(value 會被使用者
        # 改掉,就不能再當基準了)
        gr.update(value=count, elem_classes=["recluster-n", f"now-{count}"]),
    )


def _audit_payload(result, src_path, sources=None) -> dict:
    """核對面板要的東西:每一輪發言 + 從哪個音檔剪。

    ⚠️ **音檔來源優先用管線留下的 16k wav**:從原始 m4a/mp4 剪要先整檔
    解碼(長錄音數十秒),而 16k wav 是隨機存取、實測 0.01 秒
    (見 audit._cut_and_join)。沒有就退回原始檔,慢但仍可用。

    ⚠️ **`sources` 的鍵一定要是字串**(2026-08-15 code review 抓到):讀的
    那兩處(`_audit_open`、`_audit_play_row`)查的是 `str(spk)`,而這裡先前
    寫的是 `int`——於是**剛轉完的那一次**永遠查不到、一律退回 `src`,
    重新整理之後(經過 JSON 落地,鍵變成字串)才會生效。整份落地一趟就
    改變行為,而症狀是「線上會議的核對播錯音軌」:現場講者要剪麥克風軌、
    遠端講者剪系統軌,退回 `src` 就是全部剪同一軌(見 PipelineResult
    的 speaker_sources)。

    `sources` 可另外指定:現場收音那條路的軌檔在錄音工作目錄裡、收尾後
    整個刪掉,不能拿來當核對來源(見 `_finish_recording`)。"""
    blocks = getattr(result, "blocks", None) or []
    if not blocks:
        return {}
    if sources is None:
        sources = result.speaker_sources or {}
    return {
        "blocks": [
            {"speaker": b.speaker, "start": b.start, "end": b.end,
             "text": b.text, "cohesion": getattr(b, "cohesion", 0.0)}
            for b in blocks
        ],
        "src": str(src_path or ""),
        "sources": {str(k): str(v) for k, v in sources.items()},
    }


def _audit_payload_from_transcript(transcript, named, media) -> dict:
    """「🔄 重設講者」那條路的核對資料:從既有逐字稿的區塊組。

    ⚠️ **迄秒是估的**:md 只有每一輪的**起點**,終點只能拿下一輪的起點頂
    上去,中間的靜默全被算進來(`relabel.Transcript.spans` 的同一個坑)。
    所以這裡跟試聽一樣壓上限——不壓的話,一段 3 秒的插話後面接了 5 分鐘
    的沉默,核對音檔就會播 5 分鐘的空白,而使用者以為是程式壞了。

    沒有媒體檔就回空的:核對是「聽」的功能,沒有音檔時連面板都不該開。"""
    if media is None:
        return {}
    blocks, order = [], transcript.blocks
    for i, b in enumerate(order):
        nxt = order[i + 1].start if i + 1 < len(order) else b.start + relabel._TAIL_SEC
        end = min(nxt, b.start + relabel._CLIP_MAX_SEC)
        spk = UNKNOWN_SPEAKER if b.name == "未知" else (
            named.index(b.name) if b.name in named else None
        )
        if spk is None:
            continue
        blocks.append({"speaker": spk, "start": float(b.start),
                       "end": float(max(end, b.start + 0.3)), "text": b.text[:40]})
    # 相似度由 _analyse_for_relabel 現場算完之後填進來(那條路本來就在讀
    # 整份音訊);沒有音檔就沒有數字,核對表顯示空白
    return {"blocks": blocks, "src": str(media), "sources": {}}


def _audit_flags(quality) -> set:
    """哪幾列要亮「🔍 核對」= 檔尾診斷點名「建議優先核對」的那幾位。"""
    return {q.speaker for q in export.check_first(quality or [])}


# 聲紋分不開時,最多讓幾列亮起「🔍 核對」(使用者 2026-08-15 選定)。
# ⚠️ **一定要有上限**:大型會議裡「認不出來」是常態,不設限的話 8/14 那場
# 11 位裡有 9 位、8/12 那場 19 位裡有 13 位都會亮鈕,而全部都亮就等於全部
# 都沒標。取「差距最小的前三位」= 最難分辨的那幾位,與 export.check_first
# 的「一致性最低前三名」同一套哲學:工具只負責排序,不下判定
_AUDIT_CLOSE_CALLS = 3


def _rival_pool(rivals) -> list[str]:
    """整場的聲紋候選聯集(保序去重)= 「聲紋庫覺得今天可能在場的人」。

    給核對面板的改掛選單排序用(見 _reassign_choices):外面都還沒填名字
    的時候,這是唯一能把 59 人的名單收斂一點的依據。"""
    pool: list[str] = []
    for spk in sorted(rivals or {}):
        pool.extend(rivals[spk])
    return list(dict.fromkeys(pool))


def _naming_clues(count, voiceprints, audit_flags, has_audit):
    """自動填名、聲紋分不開的候選、以及該亮「🔍 核對」的那幾列。

    回 (預填名, {講者: 候選名字}, 該亮核對鈕的講者集合)。

    ⚠️ **轉檔完成與開頁還原共用這一份**:候選不隨命名進度落地,而是兩邊
    各自從聲紋向量重算——落地的話,使用者中途改了名單或聲紋庫之後,重新
    整理會看到一份與現況對不上的舊候選,而那沒有任何症狀。重算的成本是
    一次矩陣乘法(144×192),可以忽略。

    ⚠️ **核對鈕的兩個來源要合併不是取代**:原本那幾位是「群內一致性最低」
    (這一群是不是混了人),新加的是「聲紋分不開」(這一群到底是誰)——
    兩個判準問的是不同問題,實測名單幾乎不重疊(8/14 那場 9 位認不出來,
    其中 8 位手上一顆鈕都沒有)。"""
    vecs = {
        i: voiceprints[i] for i in range(count) if voiceprints.get(i) is not None
    }
    # 自動辨識的預填名 = 落地草稿的初始值。**整場一起辨識**,不逐位各自
    # recognize:同一個名字只能給一位講者,否則兩群拿到同一個名字,成品
    # 看起來就是「少了一個人」而非「認錯人」(見 voiceprints.recognize_batch)
    guesses = voiceprints_store.recognize_batch(vecs)
    close = voiceprints_store.close_calls(vecs, taken=guesses.values())
    rivals = {spk: cc.rivals for spk, cc in close.items()}
    flags = set(audit_flags or ())
    if has_audit:
        hardest = sorted(close.items(), key=lambda kv: (kv[1].gap, kv[0]))
        flags |= {spk for spk, _cc in hardest[:_AUDIT_CLOSE_CALLS]}
    return guesses, rivals, flags


def _present_result(outputs, preview, count, voiceprints, hints, clips,
                    audit=None, audit_flags=(), features=_DERIVE_FEATURES):
    """轉檔成果 → 命名區/試聽/下載/落地的整組 UI 更新(PAGE_UPDATE_LEN 個值)。

    檔案轉檔(_run)與現場收音收尾(_finish_recording)共用;回傳形狀
    = _restore_pending 的回傳(見該處),呼叫端各自追加自己的尾端更新。
    前置條件:進來時所有講者框/試聽鈕已是隱藏且清空(轉檔鏈經
    _start_run、錄音經 _reset_for_new_recording 保證),
    「維持隱藏」才能安全地送 gr.skip()(地雷詳見 _name_section_updates)。"""
    naming = pending.anyone_to_name(count, hints)
    if outputs and not naming:
        # 有成品、卻連一位講者都沒有 = 整段沒聽到人說話。**一定要明講**:
        # 不講的話畫面上只有一份空的逐字稿,而使用者(2026-08-15 錄了一段
        # 沒有人聲的電腦聲音)第一個念頭是「程式壞了」。同一批修的還有
        # 「左欄整組消失」——那才是他回報的症狀,但看到空稿子一樣會卡住
        preview = f"{_NO_SPEECH_NOTE}\n\n{preview}"
    guesses, rivals, flags = _naming_clues(
        count, voiceprints, audit_flags, has_audit=bool(audit),
    )
    prefill: dict[int, str] = {i: guesses.get(i, "") for i in range(count)}
    if hints.get(UNKNOWN_SPEAKER):
        prefill[UNKNOWN_SPEAKER] = ""
    # 命名區塊的顯示不在此控制:容器永遠掛載、由 CSS 依「內有無渲染中的
    # 命名框」決定顯示(#name-box 規則;容器 visible 切換的地雷見 build_ui)
    # 命名進度落地:睡眠/斷線/關瀏覽器後重新整理即可接續,不必重轉
    # (demo.load → _restore_pending)。clips 改指落地副本——暫存副本會被
    # 下次啟動清掃,落地副本活到套用完成
    audit = dict(audit or {})
    if audit:
        # 落地的是**合併後**的那一份(含聲紋分不開的那幾位):重新整理之後
        # 亮的鈕要跟轉完當下一模一樣,否則使用者會以為自己記錯了
        audit["flags"] = sorted(flags)
        audit["rivals"] = _rival_pool(rivals)
    clips = pending.persist(outputs, preview, count, voiceprints, hints, clips,
                            prefill, audit=audit)
    # outputs 也回傳給 paths_state:套用名字時要寫回「真正的 output/ 檔案」,
    # 不能靠下載元件(gr.Files 當輸入時給的是 Gradio 快取副本,改了不會存回原檔)
    # ——⚠️ 但一位講者都沒有時要留空,否則左欄整組被收走而命名區又是空的
    # (使用者 2026-08-15 實機踩到,見 pending.anyone_to_name)
    return _naming_page_updates(
        outputs, preview, voiceprints, clips,
        _name_section_updates(count, hints, clips, prefill, sorted(flags),
                              has_audit=bool(audit), rivals=rivals),
        audit=audit, naming=naming, features=features, count=count,
    )


def _restore_pending():
    """開頁(demo.load)還原未完成的命名:睡眠、斷線、關瀏覽器、甚至重開
    程式之後,重新整理頁面就能接續命名(含草稿名字與試聽),不必重轉
    音檔(使用者選定 2026-07-18)。沒有落地資料時整組 gr.skip() 略過——
    State 元件不能回 gr.update()(update dict 會被當成「值」存進去)。
    回傳形狀 = _run 去掉尾端的路徑欄清空(= PAGE_UPDATE_LEN)。"""
    data = pending.load()
    if data is None:
        return tuple(gr.skip() for _ in range(PAGE_UPDATE_LEN))
    hints, names, clips = data["hints"], data["names"], data["clips"]
    # 還原提示走「預覽區頂端」而非 gr.Info:gradio 6.20 的 Info toast 會
    # 忽略 title 參數、標題永遠是英文 "Info"(Playwright 最小重現實測;
    # gr.Error 的 title 是另一條路徑才有效),違反繁中訊息原則(spec §8)
    note = "(已還原上次未完成的講者命名,可直接接續;不需要重新轉檔。)"
    preview = f"{note}\n\n" + data["preview"]
    # 核對也要還原(2026-08-13 使用者實機踩到):第一版刻意不帶,結果核對鈕
    # **照樣亮著**(它只看「有沒有未知」),按下去卻是「沒有可核對的段落」
    # ——比不還原更糟。現在區塊與音檔來源跟著命名進度一起落地
    audit = data.get("audit") or {}
    # 候選不落地、在這裡重算(見 _naming_clues 的 ⚠️):落地的話,中途改過
    # 名單或聲紋庫之後重新整理,會看到一份與現況對不上的舊候選。核對鈕的
    # 旗標則用落地那份——轉完當下亮哪幾顆,重新整理後就要是哪幾顆
    _guesses, rivals, _flags = _naming_clues(
        data["count"], data["voiceprints"], (), has_audit=bool(audit),
    )
    if audit:
        audit = {**audit, "rivals": _rival_pool(rivals)}
    # 分群檔在不在由 _naming_page_updates 統一從成品清單推(見 _features_for)
    return _naming_page_updates(
        data["outputs"], preview, data["voiceprints"], clips,
        _name_section_updates(data["count"], hints, clips, names,
                              audit_flags=audit.get("flags") or (),
                              has_audit=bool(audit), rivals=rivals),
        audit=audit, count=data["count"],
    )


# ---- 套用名字與整頁復位 ----

def _labels_in(text: str) -> dict[int, str]:
    """檔案裡**目前**的講者標籤 → {命名框編號: 標籤}(編號同 name_map,
    1-based;「未知」給 UNKNOWN_SPEAKER)。

    這一步讓「轉檔後命名」與「重設講者」變成同一件事,而且**不必多帶一個
    State**:標籤的唯一真相就在那份檔案裡,現場讀最準。一般逐字稿讀到的
    就是「講者 1／2／3」(pipeline 依首次出現重編號,順序天然對得起來);
    「重設講者」模式讀到的則可能是當初命名過的真名——那正是要改的東西。

    解析失敗(不是本工具產生的 md)回空字典,呼叫端會退回預設的「講者 N」。
    """
    try:
        order = relabel.parse(text).order
    except UserFacingError:
        return {}
    labels: dict[int, str] = {}
    n = 0
    for label in order:
        if label == "未知":
            labels[UNKNOWN_SPEAKER] = label
        else:
            n += 1
            labels[n] = label
    return labels


def _apply_names(files, voiceprints, audit=None, *name_values):
    """把使用者填的名字套用到逐字稿檔案,並把「名字↔聲紋」登記到聲紋庫
    供下次自動辨識。留白的講者維持「講者 N」、不登記。

    name_values 前 MAX_SPEAKERS 個是講者命名框,第 MAX_SPEAKERS+1 個
    (有傳才有)是「未知」命名框:只改逐字稿文字,**絕不登記聲紋**、也不
    加入與會名單——未知是與每位講者都不夠像的零碎語音(常是多人重疊)的
    集合,聲紋混雜,登記會污染聲紋庫、讓日後自動辨識亂認人;使用者給它的
    稱呼也常非人名(如「其他」),不該進名單下拉。

    套用即收尾:除下載區外整個畫面復位(藏命名區、清空路徑/預覽/狀態)——
    成品已寫回 output/,畫面留著舊內容會讓人誤以為還沒套用完(使用者回報);
    全留白也一樣復位(=全部維持「講者 N」)。下載區「保留改好名字的檔案」
    是刻意的:下一步 js(ui_style.APPLY_DOWNLOAD_JS)要從它的前端值取網址觸發
    自動下載,下載觸發後才由事件鏈清空。

    「重設講者」模式(_run_relabel)也走這一支:要換掉的舊標籤現場從檔案
    讀回來(_labels_in),所以兩條路是同一個實作、不必多帶一個 State。

    files 為空 = 斷線後的過期點擊,當場擋下(見 _stale_click_guard):
    名字無處可寫,卻會清掉還能接續的命名進度。"""
    _stale_click_guard(files)
    speaker_values = name_values[:MAX_SPEAKERS]
    unknown_value = name_values[MAX_SPEAKERS] if len(name_values) > MAX_SPEAKERS else ""
    name_map = {
        i + 1: v.strip()
        for i, v in enumerate(speaker_values)
        if isinstance(v, str) and v.strip()
    }
    unknown_name = unknown_value.strip() if isinstance(unknown_value, str) else ""
    if name_map or unknown_name:
        for path in files or []:
            p = Path(path)
            try:
                before = p.read_text(encoding="utf-8")
                # 標籤現場從檔案讀(見 _labels_in):一般逐字稿讀到的就是
                # 「講者 N」、與預設值相同;「重設講者」模式讀到的是當初
                # 命名過的真名,那正是這次要換掉的東西
                after = _rename_speakers(
                    before, name_map, unknown_name or None, _labels_in(before),
                )
                # **改不到就要出聲**:改寫錨定 md 的講者行格式,而套用成功
                # 與否使用者是看不出來的(畫面照樣復位、檔案照樣在)。
                # 真的發生時多半是格式對不上,那是工具的 bug,不是使用者的
                if after == before:
                    logger.warning(
                        "套用名稱沒有改到任何一行(格式對不上?):%s", path,
                    )
                p.write_text(after, encoding="utf-8")
            except Exception:
                logger.exception("套用名稱失敗:%s", path)
        # ⚠️ **在「🔍 核對」裡改掛過的講者不登記聲紋**(使用者 2026-08-13 選定):
        # 改掛等於他親口說「這一群不只一個人」,而那是**他給的證據**,不是
        # 工具的猜測(工具明確拒絕判定「這群有幾個人」,見 types.SpeakerQuality)。
        # 不純的群一旦登記,聲紋庫就把別人的聲音學進這個人名下——下次會認得
        # 更錯,而使用者看不出來。逐字稿照樣改名:那是他確認過的事實。
        # ⚠️ 代價要知道:那個人這次不會被學起來(下次不會因此變準),但也
        # 不會學錯;他在別場乾淨的錄音裡照樣登記得到。
        impure = {int(i) for i in (audit or {}).get("reassigned") or []}
        for spk_num, name in name_map.items():
            attendees.add(name)  # 輸入的新名字自動加入與會名單(下次下拉可選)
            vec = (voiceprints or {}).get(spk_num - 1)  # 0-based 講者標籤
            if vec is not None and (spk_num - 1) not in impure:
                voiceprints_store.enroll(name, vec)  # 記住名字↔聲紋,下次自動辨識
            elif vec is not None:
                logger.info("「%s」核對時改掛過段落,這次不登記聲紋", name)
    # 套用 = 這份檔案的工作完成:清掉落地的命名進度(含全留白套用——
    # 那是使用者明確表示「維持講者 N」收工)
    pending.clear()
    # 下載區「暫留成品」是唯一與其他收工點不同之處:自動下載 js 要從它的
    # 前端值取址,清空由並行的 .then 稍後執行(見接線)。供應不了的會被
    # _page_reset_updates 濾掉——「重設講者」改寫的是使用者自己資料夾裡的
    # md,放進去會在 postprocess 炸 InvalidPathError、整個套用的 outputs
    # 一個都不落地(畫面完全沒反應,而名字其實已經寫進去了)
    return (
        *_page_reset_updates(list(files or [])),
        *_end_of_job_updates(),
    )


# ---- 現場收音(2026-07-21 規格;錄音層 record.py、邊錄邊轉 live.py)----
# UI 文案照「使用場合」命名(使用者選定),對應 record.py 的情境鍵
# 圖示與「❓ 使用說明」的「🎙️ 開會時直接錄音」那一篇裡列的三個情境**完全相同**
# (使用者 2026-08-08 指定要一致):照著說明書挑情境的人,眼睛在畫面上找的是
# 同一個形狀。改這裡要連 help_text._record() 一起改。
# ⚠️ 「現場會議」用 🎙️ 不用 🏢(使用者 2026-08-09 指定):這個情境**只收
# 麥克風**,麥克風圖示比「建築物」傳神得多。與上面「🎙️ 現場收音」同一個
# 圖示是刻意的——兩者講的都是「只有麥克風這條路」,而它們一個在「要做
# 什麼」、一個在「收音情境」,不會同時出現在同一列造成混淆。
SCENARIO_LABELS = {
    "🎙️ 現場會議": "onsite",       # 只錄麥克風
    "💻 線上會議": "online",       # 麥克風+系統聲音分軌(回音由文字層去重)
    "🔊 只錄電腦聲音": "playback",  # 只錄 WASAPI loopback
}
# **每次啟動一律回到這一項**(使用者 2026-08-09 指定拿掉「記住上次選擇」,
# 推翻 2026-07-21 的規格)。連帶 `settings.json` 整個退役——它先前的唯一
# 內容就是這個情境;要再加「記住某個選擇」的功能時,先想清楚它值不值得
# 一個持久化檔案(這一個實際用了兩週就被要求拿掉)
DEFAULT_SCENARIO = "🎙️ 現場會議"

# 情境的說明小字**跟著選中的情境換**(使用者 2026-08-07 指定:三種情境的
# 說明原本三段一起攤在上面,只有一段是當下有用的)。同 _MODE_INFO 的做法
# 與同一批實測結論(gr.update(info=…) 對 Radio 有效、只帶 info 不動選中值、
# 只帶 interactive/visible 的更新不會把 info 洗回建構時的值)。
# 每段一樣**壓在一行內**:長短差太多的話,切情境時下面的「講者人數」與
# 「開始錄音」會上下跳(左欄約放得下 30 個全形字;測試以 len ≤ 30 守著,
# 半形字元佔的寬度只有一半,所以帶軟體名的那段實際更窄)。
# 「線上會議」那段**要舉具體的軟體名**(使用者 2026-08-08 指定):光講
# 「線上會議」跟選項標籤同義,非技術同仁看不出自己算不算——看到
# Teams / Meet 才對得上自己每天在開的那個東西。舉例用字與 README、
# 「使用說明」分頁一致(那兩處本來就寫 Teams / Meet),三處要一起改。
_SCENARIO_INFO = {
    "🎙️ 現場會議": "大家在同一個房間,只錄麥克風收到的聲音",
    "💻 線上會議": "如 Teams / Meet,麥克風+喇叭都錄,回音自動剔除",
    "🔊 只錄電腦聲音": "只錄電腦放出來的聲音,適合純旁聽、線上課程",
}

# 錄音會話的模組級狀態:錄音橫跨多個 UI 事件(開始/計時 tick/停止),
# 且必須與 gradio session 脫鉤(睡眠斷線後重開頁面要能接回進行中的錄音,
# 同 pending 的教訓);gradio 佇列同時只跑一批,無並發疑慮
_rec: dict = {"recorder": None, "live": None, "dir": None, "stem": None, "scenario": None}
# 即時預覽快取:段落數沒變就 gr.skip(),不必每秒重轉繁體整份文字;
# conv 是逐段繁化結果((起,迄,原文)→繁化文),長錄音每有新段落只轉
# 新增的那幾段——先前每次都整份重轉,錄越久每次刷新越慢(O(n²))
_live_preview: dict = {"n": -1, "text": "", "conv": {}}
# 轉檔(檔案模式)進行中旗標:_start_run 舉、_after_run 放(完成/報錯
# 都會跑)。錄音的排程器與轉檔共用引擎快取(非執行緒安全),兩者互斥:
# 轉檔中不得開始錄音(反向由「錄音中路徑欄隱藏+開始鈕鎖住」擋)
_transcribing = {"on": False}
# 文件轉 Markdown 批次進行中旗標:_doc_start 舉、_doc_after 放
# (.then/.failure 成對,完成/報錯/停止都會跑)。與逐字稿那兩條互斥,
# 而且這是**正確性**需求不只是體驗:cancel.py 的取消旗標是全域單例,
# 兩邊同時跑的話任一顆停止鈕會把另一邊也一起殺掉
_converting = {"on": False}


def _busy_error(what: str) -> gr.Error:
    """互斥擋下時的繁中提醒(三處共用,措辭只講一次)。"""
    return gr.Error(
        f"{what}還在進行,請等它完成或按「停止」後再開始",
        title="提醒", print_exception=False,
    )


def _scenario_info(label):
    """切換收音情境:把說明小字換成這個情境的那一段。

    掛 `.input`(只有使用者操作才觸發,送回自己的更新不會再繞回來,
    同 `_switch_source` 對「要做什麼」的做法)。回傳的是 radio 自己的更新:
    值不動(使用者剛按的就是它),只換 info。認不得的值寧可沒有說明,
    也不要讓整個切換炸在一行文案上。

    ⚠️ **這支不再記住選擇**(使用者 2026-08-09 指定拿掉,推翻 2026-07-21
    的「記住上次選擇」):每次啟動一律回到 `DEFAULT_SCENARIO`。連帶
    `settings.json` 與 `_load_scenario` 整組移除——那個檔先前的唯一內容
    就是這個情境,留著一個永遠只讀不寫的空檔案只會讓人以為還有別的設定。"""
    return gr.update(info=_SCENARIO_INFO.get(label, ""))


def _recordings_root() -> Path:
    """錄音工作目錄的根。**絕不能放系統暫存的 meeting-scribe-* 前綴下**:
    cleanup_stale_temp 會把硬退出殘留當孤兒掃掉——錄音是使用者的會議
    原音(可能 4 小時),當機後必須救得回來。成功收尾(音檔已複製進
    output/)才刪;殘留目錄留給使用者手動處理(README 註明位置)。"""
    return paths.appdata_root() / "recordings"


# 「開始下一份工作」那整組的長度(= source_switch_outputs / _switch_source
# 的回傳)。刻意做成同一個形狀:命名結束時的還原直接交給 _switch_source 算,
# 兩邊永遠對得起來
_NAMING_FOCUS_LEN = 16


def _naming_focus(mode, paths):
    """命名進行中就把「開始下一份工作」那整組收起來(使用者選定 2026-08-08
    設計稿 A 案)。

    **起因**:轉完檔進到命名時,左欄最上方是命名區,往下卻緊接著
    「要做什麼／收音情境／講者人數／開始錄音」——那整組都是開始**下一份**
    工作的入口。使用者在重開網頁、命名進度被接回來時發現的(那時模式會
    回到預設的現場收音,那組控件視覺上最顯眼),但正常轉完檔也一樣。
    與「轉檔中鎖住那整組」是同一個道理:工作沒收工就不開新的。

    **判準用 `paths_state`**:它非空 = 有成品在等命名,與命名區的顯示條件
    同源(命名區是「有渲染中的講者框才顯示」)。批次模式的 `_present_result`
    傳空清單,所以不會誤收——那條路本來就沒有命名區。

    ⚠️ **還原不能一律送 `visible=True`**:這些元件的可見性本來就依模式而異
    (收音模式沒有路徑欄、檔案模式沒有錄音鈕、重設講者連講者人數都藏),
    硬設會把 `_switch_source` 的規則整個洗掉。所以還原就交給它算。"""
    if paths:
        return tuple(gr.update(visible=False) for _ in range(_NAMING_FOCUS_LEN))
    first, *rest = _switch_source(mode)
    # `_switch_source` 不動「要做什麼」自己的 visible(它平時一直在),
    # 而命名期間我們把它藏了,所以還原這一步要明確把它送回來
    return ({**first, "visible": True}, *rest)


def _switch_source(mode):
    """「要做什麼」切換:現場收音 ↔ 轉錄音檔 ↔ 重設講者。只切「葉子元件」的
    visible,絕不切容器——容器 remount 會讓 children 帶舊 props 重生
    (gradio 6.20 地雷,見 name-box 註解)。切換即清路徑(模式互斥,
    留著會有「畫面沒檔案但開始可按」的矛盾)。

    「開始轉檔/停止」在收音模式整組隱藏:留著一顆灰色大按鈕只會
    誤導使用者以為要按它(實際回報);收音模式的主按鈕是「開始錄音」,
    收尾期間需要的「停止」由 _lock_for_rec_finish 臨時亮回來。

    檔案轉檔進行中不得切到現場收音(引擎互斥、且會藏起轉檔中的停止鈕):
    把 radio 撥回「轉錄音檔」並提示。錄音中本元件是鎖住的,不會切到一半。

    radio 自己也在 outputs 裡:值不動,但**說明小字要換成這個模式的**
    (`_MODE_INFO`)。撥回那條路徑同理——連 value 一起送,否則畫面會停在
    「選中轉錄音檔、說明卻在講現場收音」。掛的是 `.input`(只有使用者操作
    才觸發),送回自己的更新不會再繞回來。

    尾端三個是「這個模式用不到就別擺出來」的參數(講者人數、模型、
    包含子資料夾),見那裡的註解。
    ⚠️ 藏起來的元件仍留在 `_run`/`_start_recording` 的 inputs 裡,這是安全的:
    gradio 6.20 對 visible=False 是**整個不從 DOM 渲染**,但事件照樣送出
    **最後一次的值**(不是 None),切回顯示也不掉值——Playwright 最小重現
    實測過(做法見 docs/dev/verification.md 第 1 節)。"""
    rec_mode = mode == _MODE_RECORD
    relabel_mode = mode == _MODE_RELABEL
    # 轉檔中只擋「切到現場收音」;切到轉錄音檔/重設講者無害(都不動引擎狀態)
    if _transcribing["on"] and rec_mode:
        # 模式名帶圖示,夾在句子裡要加引號才不會跟前後文黏成一團
        gr.Info(f"轉檔進行中:請等它完成、或按「停止」後再切換到「{_MODE_RECORD}」。")
        return (
            gr.update(value=_MODE_FILE, info=_MODE_INFO[_MODE_FILE]),
            *(gr.skip(),) * 15,
        )
    files_mode = not rec_mode and not relabel_mode
    return (
        # 值不動(使用者剛按的就是它),只換說明小字。.get 兜底:認不得的
        # 值寧可沒有說明,也不要讓整個切換炸在一行文案上
        gr.update(info=_MODE_INFO.get(mode, "")),
        gr.update(                                          # 路徑欄
            visible=not rec_mode, value="",
            label=(
                "要重設講者的逐字稿(.md)" if relabel_mode
                else "會議錄音/錄影檔案路徑"
            ),
            lines=1 if relabel_mode else 3,
            placeholder=(
                "按下方「選擇逐字稿…」挑選,或直接貼上 md 的完整路徑"
                if relabel_mode else
                "按下方按鈕挑選,或直接貼上完整路徑(一行一個,帶引號沒關係)"
            ),
        ),
        # 「重設講者」一次一份 md,沒有單檔/多檔之分,那段對照說明用不上
        gr.update(visible=files_mode),                      # 模式說明
        gr.update(                                          # 選擇檔案…/選擇逐字稿…
            visible=not rec_mode,
            value="選擇逐字稿…" if relabel_mode else "選擇檔案…",
        ),
        gr.update(visible=files_mode),                      # 選擇資料夾…
        gr.update(visible=files_mode),                      # 清空
        gr.update(visible=False, value=""),                 # 選檔摘要(清空)
        gr.update(visible=rec_mode),                        # 收音情境
        gr.update(visible=rec_mode, value=_REC_IDLE_MD),    # 錄音狀態列
        gr.update(visible=rec_mode, interactive=True),      # 開始錄音
        gr.update(visible=rec_mode, interactive=False),     # 停止錄音
        gr.update(                                          # 開始轉檔/讀取逐字稿
            visible=not rec_mode, interactive=False,
            value="讀取並設定講者" if relabel_mode else "開始轉檔",
        ),
        gr.update(visible=not rec_mode, interactive=False),  # 停止(轉檔)
        # 「重設講者」用不到的兩個參數就藏起來(使用者 2026-08-06 回報
        # 「講者人數是不是沒用?」——確實沒用:分群早在當初轉檔就做完,
        # 講者有幾位由 md 的內容決定,`_run_relabel` 根本收不到這個值;
        # 「模型」同理,那個模式不跑 Whisper,抽聲紋用的是聲紋模型)。
        # 擺在畫面上的控件會被當成有作用,而它還是跨模式共用的——在這裡
        # 填的數字會原封不動留到切回「轉錄音檔」,下一次轉檔真的會吃到。
        # 「CPU 核心數」**不藏**:有同名媒體檔時要抽聲紋(走 CPU),它有效
        gr.update(visible=not relabel_mode),                 # 講者人數
        gr.update(visible=not relabel_mode),                 # 模型
        # 「包含子資料夾」只有「轉錄音檔」用得到:收音沒有來源資料夾,
        # 重設講者一次一份 md。⚠️ 建構時的初始 visible 必須是 False
        # (預設模式是收音),否則開啟程式就會看到一個當下無效的勾選框
        gr.update(visible=files_mode),                       # 包含子資料夾
    )


def _reset_for_new_recording():
    """開始錄音前的整頁復位(= _start_run 的復位部分,錄音版):清掉上一檔
    成品與落地命名,講者框全隱藏——_present_result 的 gr.skip() 前置條件。
    另清路徑欄、把「開始轉檔」鎖住(錄音模式用不到);「講者人數」
    刻意不歸零——錄音中隨時可填、停止當下才讀(見 _end_of_job_updates)。"""
    pending.clear()
    return (
        *_page_reset_updates(None),
        gr.update(value=""),
        gr.update(interactive=False),
        gr.update(interactive=False),
    )


def _discard_naming(paths):
    """「跳過命名」(使用者指定 2026-07-24:有時不想改名也不想下載):
    放棄講者命名,整頁回到閒置狀態並收工(_end_of_job_updates)。成品
    「不」刪——逐字稿(講者以「講者 N」標示)與現場收音的錄音檔都已在
    output/,這顆只清畫面與落地的命名進度(pending 必清,否則 F5 命名區
    又冒回來);預覽留一行指路,不讓使用者以為檔案不見了。

    paths(paths_state)只用來擋斷線後的過期點擊(見 _stale_click_guard)
    ——死頁面上的誤點會靜靜把命名進度清掉,使用者卻連畫面都沒變。"""
    _stale_click_guard(paths)
    pending.clear()
    updates = list(_page_reset_updates(None))
    # ⚠️ **要連 visible 一起送**(2026-08-14 使用者實機踩到第二次):這裡覆蓋
    # 的是整頁復位的「預覽」那一格,而那一格帶著 visible=True——只塞字串
    # 等於把那個意圖丟掉,核對面板藏起來的預覽就再也回不來。
    # ⚠️ 通則:**凡是覆蓋整頁復位任何一格的地方,都要保留原本那一格的意圖**
    updates[1] = gr.update(
        value=(
            "已跳過講者命名,畫面已清空。\n"
            "已完成的逐字稿(講者以「講者 1、講者 2…」標示,現場收音含錄音檔)"
            "仍在 output 資料夾。"
        ),
        visible=True,
    )
    return (*updates, *_end_of_job_updates())


# 錄音狀態列的字樣(markdown;#rec-status 有放大樣式)。
# 第一批預覽文字 = 累積 1 分鐘(live._FIRST_CHUNK_SEC)+該段轉錄時間,
# 要把「還要等幾分鐘」講明白,否則使用者會以為收音壞了(實際問過)
_REC_IDLE_MD = "尚未開始錄音。"
_REC_WARMUP = "背景轉錄準備中(第一批文字約 2~4 分鐘後出現在預覽區)"
# 按下「停止錄音」之後那一段。兩處共用(鏈頭 `_lock_for_rec_finish` 與
# 開頁還原 `_restore_finishing`)——各寫一份的話,斷線接回來的畫面會跟
# 原本那句對不起來,而使用者正是靠這句話判斷「它還在跑」
_REC_FINISHING_MD = "**收尾中**:完成剩餘轉錄與講者分析,進度見右側預覽區…"


def _param_updates(enabled: bool, lock_speakers: bool = True):
    """參數控件的鎖定/解鎖,回傳順序=[講者人數, 模型, CPU 核心數,
    包含子資料夾](與接線的 adv_params 對齊)。工作進行中一律鎖住:模型
    在開始當下就定死,其餘雖是收尾才讀,進行中改動易被誤解成即時生效
    (使用者指定 2026-07-22;沿革見 CLAUDE.md)。

    lock_speakers=False 是「錄音中」的唯一例外(使用者指定 2026-07-24):
    講者人數在按「停止錄音」當下才讀、講者分析也收尾才跑,開會中數清
    人數、停止前填入是正當用法,故錄音中維持可輸入(gr.skip 不動狀態);
    檔案轉檔沒有這個例外——人數在按「開始」當下就定案,中途改不生效,
    開放輸入只會誤導。"""
    speakers_upd = gr.update(interactive=enabled) if lock_speakers else gr.skip()
    return (
        speakers_upd,
        gr.update(interactive=enabled),  # 模型
        gr.update(interactive=enabled),  # CPU 核心數
        gr.update(interactive=enabled),  # 包含子資料夾
    )


def _start_recording(scenario_label, model_label, cpu_cores=None):
    """「開始錄音」:啟動分軌錄音+背景增量轉錄。裝置缺失(無麥克風/
    無播放裝置)以繁中 gr.Error 當場浮出,絕不錄一場空。
    模型開錄當下定案(增量轉錄立刻在用)。"""
    if _rec["recorder"] is not None:
        raise gr.Error("已在錄音中", title="提醒", print_exception=False)
    if _transcribing["on"]:
        raise gr.Error(
            "檔案轉檔還在進行,請等它完成或按「停止」後再開始錄音",
            title="提醒", print_exception=False,
        )
    if _converting["on"]:
        raise _busy_error("文件轉檔")
    _apply_cpu_cores(cpu_cores)
    # 上一批(檔案轉檔)按過的停止不得波及本場錄音:錄音中的增量講者切分
    # 有 cancel 檢查點,旗標沒清會讓它一開工就自我了斷(只剩 log)
    cancel.reset()
    scenario = SCENARIO_LABELS.get(scenario_label, "onsite")
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    stem = f"錄音_{stamp}"
    rec_dir = _recordings_root() / stamp
    recorder = record.Recorder(rec_dir, scenario, stem=stem)
    try:
        recorder.start()
    except UserFacingError as e:
        raise gr.Error(str(e), title="無法開始錄音", print_exception=False)
    except Exception as e:
        logger.exception("開始錄音失敗")
        raise gr.Error(
            "無法開始錄音:發生未預期的錯誤,詳情見終端機視窗",
            title="無法開始錄音", print_exception=False,
        ) from e
    lt = live_scribe.LiveTranscriber(model_key=MODEL_LABELS[model_label])
    for kind, path in recorder.track_files().items():
        lt.add_track(kind, path)
    lt.start()
    power.stay_awake_begin()  # 錄音中不得睡眠(停止/收尾時解除)
    _live_preview.update(n=-1, text="", conv={})
    _rec.update(recorder=recorder, live=lt, dir=rec_dir, stem=stem, scenario=scenario_label)
    return (
        gr.update(interactive=False),                 # 開始錄音
        gr.update(interactive=True),                  # 停止錄音
        gr.update(interactive=False),                 # 要做什麼(鎖切換)
        gr.update(interactive=False),                 # 收音情境(鎖切換)
        gr.update(value=f"**● 錄音中**(情境:{scenario_label})|{_REC_WARMUP}"),
        gr.Timer(active=True),                        # 啟動計時 tick
        # 錄音中講者人數保持可輸入(收尾才讀,見 _param_updates docstring)
        *_param_updates(False, lock_speakers=False),
    )


def _rec_start_failed():
    """開始錄音失敗(gr.Error 已顯示):錄音鈕與來源切換復原。"""
    return (
        gr.update(interactive=True),
        gr.update(interactive=False),
        gr.update(interactive=True),
        gr.update(interactive=True),
        gr.update(value=_REC_IDLE_MD),
        gr.Timer(active=False),
        *_param_updates(True),
    )


def _rec_tick():
    """計時 tick(每秒):更新錄音狀態列;背景轉錄有新段落時同步刷新
    即時預覽(講者名收尾後才標,預覽只求看得到內容在長=收音正常)。"""
    rec, lt = _rec["recorder"], _rec["live"]
    if rec is None or lt is None:
        return gr.skip(), gr.skip()
    elapsed = pipeline.mmss(rec.elapsed())
    done = lt.transcribed_until()
    done_txt = f"背景轉錄:已完成至 {pipeline.mmss(done)}" if done > 0 else _REC_WARMUP
    status = f"**● 錄音中 {elapsed}**(情境:{_rec['scenario']})|{done_txt}"
    snap = lt.snapshot()
    if len(snap) == _live_preview["n"]:
        return gr.update(value=status), gr.skip()
    # 逐段快取繁化結果,只轉新增段落(錄音中途改 replace.txt 對「已轉過
    # 的段落」不重套——預覽只求看得到內容在長,成品由收尾統一重繁化)。
    # s2tw 對英文是無作用直通,不需依語言分流
    conv = _live_preview.setdefault("conv", {})
    lines = []
    for s in snap:
        key = (s.start, s.end, s.text)
        if key not in conv:
            conv[key] = convert.to_taiwan_traditional(s.text)
        lines.append(f"[{pipeline.mmss(s.start)}] {conv[key]}")
    _live_preview.update(
        n=len(snap),
        text="(錄音中即時預覽;講者名字會在停止錄音、完成分析後標註。)\n\n"
             + "\n".join(lines),
    )
    return gr.update(value=status), gr.update(value=_live_preview["text"])


def _lock_for_rec_finish():
    """停止錄音鏈第一步:鎖錄音雙鈕與來源切換、停計時、把「停止」亮回來
    (收音模式平時隱藏它;收尾轉錄要能中止,協作式取消同檔案轉檔流程)。
    必須是鏈的第一步(順序保證,同 _start_run 的理由)。"""
    return (
        gr.update(interactive=False),                # 開始錄音
        gr.update(interactive=False),                # 停止錄音
        gr.update(visible=True, interactive=True),   # 停止(轉檔):收尾中可中止
        gr.Timer(active=False),
        gr.update(value=_REC_FINISHING_MD),
        gr.update(interactive=False),                # 要做什麼(鎖切換)
    )


def _salvage_tracks(tracks, stem) -> list[str]:
    """收尾失敗/被停止時的兜底:把錄好的音軌複製進 output/——錄音是
    會議原音,無論轉檔結果如何都不得丟失。回傳成功保存的檔名。"""
    saved: list[str] = []
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for t in tracks:
        suffix = "" if len(tracks) == 1 else ("_現場" if t.kind == "mic" else "_電腦")
        dest = OUTPUT_DIR / f"{stem}{suffix}.wav"
        try:
            shutil.copy2(t.path, dest)
            saved.append(dest.name)
        except Exception:
            logger.exception("錄音音軌保存失敗:%s", t.path)
    return saved


def _finish_recording(num_speakers, cpu_cores=None, progress=gr.Progress()):
    """「停止錄音」:停錄 → 殘段轉錄+講者分析+輸出(live.run_live_finish)
    → 與檔案轉檔相同的命名/試聽/落地呈現(_present_result)。
    講者人數停止當下才讀(分析在收尾才跑)。

    任何失敗路徑都先把音軌保進 output/ 再報錯——逐字稿可以重來
    (切回「轉錄音檔」模式重轉),錄音本身不能重來。"""
    rec, lt = _rec["recorder"], _rec["live"]
    if rec is None or lt is None:
        raise gr.Error("目前沒有進行中的錄音", title="提醒", print_exception=False)
    # 立刻取走所有權:收尾期間重按停止/重開頁面誤觸,第二次進來直接被
    # 上面的 None 檢查擋掉,不會對同一個 recorder/live 動兩次手
    _rec.update(recorder=None, live=None)
    _apply_cpu_cores(cpu_cores)
    cancel.reset()  # 上一批按過的停止不得波及本次收尾
    stem = _rec["stem"]
    # 收尾也要進 runstate:按下「停止錄音」之後這一段(殘段轉錄+講者分析)
    # 對長會議是好幾分鐘,而在此之前它是**唯一沒有斷線還原的路徑**——
    # `_restore_recording` 此時已經 skip(recorder 已交出),使用者重新整理
    # 就會看到假裝沒事的初始畫面,而收尾還在跑。
    # ⚠️ 巢狀 try 的理由同 `_run`:`runstate.end()` 必須晚於成功路徑的
    # `_present_result`(裡面才做 `pending.persist`),否則秒針會在那個窗口
    # 判定「跑完了又沒有命名資料」而把畫面清空——而這裡的窗口還隔著一次
    # 剪試聽片段,不是一瞬間
    runstate.begin(stem, kind=runstate.KIND_RECORDING)
    try:
        return _run_recording_finish(rec, lt, stem, num_speakers, progress)
    finally:
        runstate.end()


def _run_recording_finish(rec, lt, stem, num_speakers, progress):
    """收尾的主體(2026-08-08 從 `_finish_recording` 抽出來)。

    抽出來只為了一件事:讓 `runstate` 的起訖能**包住整個流程**,包括最後
    那個 `_present_result`——`pending.persist` 在它裡面,而收尾成功路徑的
    `_present_result` 位在既有 try/finally 之外,中間還隔著一次剪試聽片段。
    若 `runstate.end()` 早於它,秒針就會在那個窗口判定「跑完了又沒有命名
    資料」,把剛跑完的成果從畫面上清掉(同 `_run` 用巢狀 try 的理由)。"""
    try:
        # stop() 也放 try 內:它失敗時同樣要走 finally 清防睡眠旗標與 lt
        tracks = rec.stop()
        if not tracks or max((t.duration for t in tracks), default=0.0) < 2.0:
            raise gr.Error(
                "錄音太短(不到 2 秒),沒有可轉的內容",
                title="提醒", print_exception=False,
            )
        try:
            def finish_stage(stage: str, frac: float) -> None:
                progress(frac, desc=f"{stem}:{stage}")
                # 落地給開頁還原用:收尾對長會議是好幾分鐘,而斷線之後
                # 畫面上唯一的線索就只剩這句話
                runstate.note(stage, frac)

            result = live_scribe.run_live_finish(
                tracks, lt, OUTPUT_DIR, stem,
                num_speakers=_normalize_speakers(num_speakers),
                on_stage=finish_stage,
            )
        except cancel.Cancelled:
            saved = _salvage_tracks(tracks, stem)
            note = "、".join(saved) if saved else "(保存失敗,詳見終端機視窗)"
            preview = (
                "已依要求停止,逐字稿未完成。\n"
                f"錄音音檔已保留於 output 資料夾:{note}\n"
                f"之後可切回「{_MODE_FILE}」模式,把音檔重新轉成逐字稿。"
            )
            return _present_result([], preview, 0, {}, {}, {})
        except UserFacingError as e:
            logger.exception("現場收音收尾失敗")
            _salvage_tracks(tracks, stem)
            raise gr.Error(
                f"{e}(錄音音檔已保留於 output 資料夾,可改用「{_MODE_FILE}」模式重試)",
                title="轉檔失敗", print_exception=False,
            )
        except Exception:
            logger.exception("現場收音收尾失敗")
            _salvage_tracks(tracks, stem)
            raise gr.Error(
                "發生未預期的錯誤,詳情見終端機視窗;"
                f"錄音音檔已保留於 output 資料夾,可改用「{_MODE_FILE}」模式重試",
                title="轉檔失敗", print_exception=False,
            )
    finally:
        lt.close()
        # 放進 finally:防睡眠旗標現在由長駐執行緒真持有(power.py),
        # 收尾路上任何報錯若漏掉 end,行程活多久就多久不省電
        power.stay_awake_end()
        _rec.update(recorder=None, live=None)
    # 成功:成品音檔已由 run_live_finish 放進 output/。下載區只列逐字稿
    # ——套用名字後的自動下載 js 會逐檔觸發瀏覽器下載,幾百 MB 的 wav
    # 不該被強推;音檔位置在預覽區點明
    transcripts = [str(p) for p in result.outputs if p.suffix != ".wav"]
    wav_names = [p.name for p in result.outputs if p.suffix == ".wav"]
    preview = (
        f"(錄音檔已存於 output 資料夾:{'、'.join(wav_names)})\n\n{result.preview}"
    )
    # 試聽剪對應音軌(speaker_sources);「未知」等無對應者剪第一軌
    clips = _cut_speaker_clips(
        Path(tracks[0].path), result.speaker_hints, sources=result.speaker_sources,
    )
    # 核對也要給現場收音這條路(2026-08-15 code review 抓到:先前只有檔案
    # 轉檔給,於是線上會議永遠沒有「🔍 核對」鈕,而使用說明寫著「未知那一列
    # 一定有核對鈕」)。⚠️ **來源只能用 output\ 裡的成品音檔**:
    # `speaker_sources` 指的是錄音工作目錄裡的軌檔,而那個目錄下面幾行就
    # 整個刪掉了(留著才是錯的——錄音檔動輒幾百 MB)。分軌的差別因此在
    # 核對時消失(雙軌是合成後的立體聲),但那是「聽得到就好」的功能,
    # 而試聽仍然剪對軌(clips 在刪目錄之前就剪好了)
    audit_src = next((p for p in result.outputs if p.suffix == ".wav"), None)
    out = _present_result(
        transcripts, preview, result.speakers,
        result.voiceprints or {}, result.speaker_hints or {}, clips,
        audit=_audit_payload(result, audit_src, sources={}) if audit_src else {},
        audit_flags=_audit_flags(result.quality),
    )
    # 音檔已進 output/、試聽片段已剪出(pending 另存副本):錄音工作目錄
    # 功成身退。放最後:前面任何一步炸掉都還留著原始素材
    if _rec["dir"] is not None:
        shutil.rmtree(_rec["dir"], ignore_errors=True)
        _rec["dir"] = None
    return out


def _after_rec_finish():
    """停止錄音鏈收尾(.then/.failure 成對掛,同 _after_run 的地雷):
    錄音鈕回待機、解鎖來源切換、「停止」收回隱藏(收音模式平時不顯示,
    只在收尾期間由 _lock_for_rec_finish 臨時亮出)。"""
    return (
        gr.update(interactive=True),                  # 開始錄音
        gr.update(interactive=False),                 # 停止錄音
        gr.update(visible=False, interactive=False),  # 停止(轉檔)
        gr.update(interactive=True),                  # 要做什麼
        gr.update(interactive=True),                  # 收音情境
        gr.update(value=_REC_IDLE_MD),
        *_param_updates(True),                        # 進階參數解鎖
    )


def _restore_finishing():
    """開頁接回「按下停止錄音之後的收尾」(2026-08-08 補上)。

    收尾對長會議是好幾分鐘(殘段轉錄+講者分析),而在此之前它是**唯一
    沒有斷線還原的路徑**:`_restore_recording` 這時已經 skip(recorder 早在
    `_finish_recording` 開頭就被交出去了),`runstate` 又只認檔案那條路,
    所以重新整理會看到假裝沒事的初始畫面,而收尾還在跑。

    畫面等同 `_lock_for_rec_finish` 鎖出來的樣子——**尤其是「停止」那顆**:
    收尾期間本來就允許中止,少了它使用者只能關黑視窗。"""
    return (
        gr.update(value=_MODE_RECORD, interactive=False,
                  info=_MODE_INFO[_MODE_RECORD]),
        gr.update(visible=False, value=""),              # 路徑欄
        gr.update(visible=False),                        # 模式說明
        gr.update(visible=False),                        # 選擇檔案…
        gr.update(visible=False),                        # 選擇資料夾…
        gr.update(visible=False),                        # 清空
        gr.update(visible=False, value=""),              # 選檔摘要
        gr.update(visible=True, interactive=False,
                  value=(scen := _rec["scenario"] or DEFAULT_SCENARIO),
                  info=_SCENARIO_INFO.get(scen, "")),
        gr.update(visible=True, value=_REC_FINISHING_MD),  # 錄音狀態列
        gr.update(visible=True, interactive=False),      # 開始錄音
        gr.update(visible=True, interactive=False),      # 停止錄音
        gr.Timer(active=False),                          # 錄音計時停了
        gr.update(visible=False, interactive=False),     # 開始轉檔
        gr.update(visible=True, interactive=True),       # 停止:收尾可中止
        *_param_updates(False),
        gr.Timer(active=True),                           # 轉檔秒針接手畫進度
    )


def _restore_recording():
    """開頁(demo.load)接回進行中的錄音:錄音與 session 脫鉤(伺服器端
    持續錄),睡眠/斷線/誤關分頁後重開頁面,畫面要回到「錄音中」而不是
    假裝沒事的檔案模式。

    三種狀態:錄音中 → 收尾中(見 `_restore_finishing`)→ 都不是就整組
    gr.skip()(14 個介面元件+進階參數四控件+轉檔秒針)。"""
    if _rec["recorder"] is None:
        snap = runstate.snapshot()
        if snap is not None and snap.kind == runstate.KIND_RECORDING:
            return _restore_finishing()
        return tuple(gr.skip() for _ in range(19))
    return (
        # 「要做什麼」:接回錄音中的畫面,說明小字也要跟著回到收音那一段
        # (漏了的話 F5 之後會停在上一次切換的說明,就是原本那個 bug)
        gr.update(value=_MODE_RECORD, interactive=False,
                  info=_MODE_INFO[_MODE_RECORD]),
        gr.update(visible=False, value=""),              # 路徑欄
        gr.update(visible=False),                        # 模式說明
        gr.update(visible=False),                        # 選擇檔案…
        gr.update(visible=False),                        # 選擇資料夾…
        gr.update(visible=False),                        # 清空
        gr.update(visible=False, value=""),              # 選檔摘要
        # 收音情境:值接回錄音當時選的那個,說明小字要一起送——同「要做
        # 什麼」那條的理由,漏了就會停在 F5 之前的那一段(值與說明對不上)
        gr.update(visible=True, interactive=False,
                  value=(scen := _rec["scenario"] or DEFAULT_SCENARIO),
                  info=_SCENARIO_INFO.get(scen, "")),
        gr.update(visible=True),                         # 錄音狀態列(內容由 tick 補)
        gr.update(visible=True, interactive=False),      # 開始錄音
        gr.update(visible=True, interactive=True),       # 停止錄音
        gr.Timer(active=True),
        gr.update(visible=False, interactive=False),     # 開始轉檔(收音模式隱藏)
        gr.update(visible=False, interactive=False),     # 停止(轉檔)
        # 進階參數維持鎖住;講者人數錄音中可輸入(同 _start_recording)
        *_param_updates(False, lock_speakers=False),
        gr.skip(),  # 轉檔秒針:錄音中用不到(即時預覽走 rec_timer)
    )


# ---- 文字、圖像→MD(第二分頁;把關在 docsrc、管線在 docpipe)----

def _doc_summary(text):
    """選檔摘要文字 → 元件更新:**空的時候整塊藏起來**。

    ⚠️ 空的 `gr.Markdown` 高度是 0、畫面上什麼都看不到,**但它仍是 Column
    的一個孩子、照樣吃掉兩份 layout_gap**:2026-08-15 Playwright 實測(使用者
    截圖圈出「選檔鈕與『開始轉檔』中間空一大段」),選檔列底到「開始轉檔」列頂
    量到 **40px**,而同一欄其他每一對相鄰列都是 20px。藏起來之後回到 20px
    ——gradio 6 對 visible=False 是整個不渲染(開頁)或加 `.hidden`(執行中
    切換),兩種都不佔 gap。**改回常駐的空元件就會把那段空白帶回來。**

    做法與「🎙️ 聲音→MD」的 `_src_summary` 一致(兩個分頁本來就該長一樣);
    doctab 回的是純字串(它刻意不 import gradio),顯不顯示到這一層才決定。
    """
    return gr.update(value=text, visible=bool(text))


def _doc_picked(picker, current, recursive):
    """「選擇檔案…」/「選擇資料夾…」共用的收尾:(路徑欄, 摘要更新)。"""
    path, summary = picker(current, recursive)
    return path, _doc_summary(summary)


def _doc_clear():
    """「清空」:路徑欄與摘要一起回到空的狀態(摘要連帶收起來)。"""
    path, summary = doctab.clear_paths()
    return path, _doc_summary(summary)


def _doc_preview(text, recursive=True):
    """路徑欄打字/切「包含子資料夾」時重算摘要。"""
    return _doc_summary(doctab.preview_summary(text, recursive))


def _doc_start():
    """文件批次鏈第一步:鎖介面 + 亮「停止」。

    與 _start_run 同構,理由也相同——鎖定要與轉檔在**不同批訊息**裡落地,
    而且**本步不得拋例外**:收尾 `_doc_after` 的 .failure 掛在第二步上,
    鏈頭失敗就沒人解鎖、介面鎖死。"""
    _converting["on"] = True
    return (
        gr.update(interactive=False),  # 路徑欄
        gr.update(interactive=False),  # 選擇檔案…
        gr.update(interactive=False),  # 選擇資料夾…
        gr.update(interactive=False),  # 清空
        gr.update(interactive=False),  # 包含子資料夾
        gr.update(interactive=False),  # 辨識圖片裡的文字
        gr.update(interactive=False),  # 郵件附件一併轉檔
        gr.update(interactive=False),  # 開始轉檔
        gr.update(interactive=True),   # 停止
        gr.update(interactive=False),  # 開啟輸出資料夾
    )


def _doc_convert(
    text, recursive, ocr_enabled=True, mail_attachments=True,
    progress=gr.Progress(),
):
    """「開始轉檔」:把關 → 乾跑預告 → 批次轉換 → 報告。

    是 generator:第一個 yield 先把「將寫出/將覆寫/將改名」的清單送到
    畫面上,再開始真的做事。批次一次動使用者資料夾裡數十個檔,開跑前
    讓人看得到要發生什麼,比事後報告有用得多。"""
    if _transcribing["on"]:
        raise _busy_error("逐字稿轉檔")
    if _rec["recorder"] is not None:
        raise _busy_error("錄音")
    try:
        files, skipped = docsrc.validate_batch(text, recursive)
    except UserFacingError as e:
        raise gr.Error(str(e), title="提醒", print_exception=False) from None

    cancel.reset()  # 上一批按過的停止不得波及本批
    plan = docpipe.plan_outputs(files)
    yield (
        "即將轉換以下檔案(輸出會放在原始文件旁邊):\n\n"
        + "\n".join(docpipe.dry_run_lines(plan)),
        gr.skip(), gr.skip(),
    )
    try:
        report = docpipe.convert_batch(
            files, skipped,
            on_stage=lambda stage, frac: progress(frac, desc=stage),
            ocr_enabled=bool(ocr_enabled),
            mail_attachments=bool(mail_attachments),
            # 這個分頁沒有「模型」與「講者人數」控件,錄音就用預設值跑
            # (使用者 2026-08-06 指定這裡也收音訊)。**刻意不把那兩個控件
            # 搬過來**:這個分頁的用法是「一次丟一堆混合檔案」,為其中一種
            # 格式加兩個旋鈕會讓另外三十種格式的使用者每次都看到它們;
            # 要調參數就到「🎙️ 聲音→MD」——那裡本來就是為聲音而生的
            options={
                "model_key": transcribe.default_model_key(),
                "num_speakers": 0,  # 自動偵測:這裡沒地方讓人填人數
            },
        )
    except UserFacingError as e:
        logger.exception("文件轉檔失敗")
        raise gr.Error(str(e), title="轉檔失敗", print_exception=False) from None
    except Exception as e:
        logger.exception("文件轉檔失敗(未預期)")
        raise gr.Error(
            "發生未預期的錯誤,詳情見終端機視窗", title="轉檔失敗", print_exception=False,
        ) from e
    dirs = [str(d) for d in report.out_dirs]
    yield (
        docpipe.report_markdown(report),
        dirs,
        gr.update(interactive=bool(dirs)),
    )


def _doc_after():
    """文件批次收尾(.then/.failure 成對:完成/報錯/停止都要跑)。

    「開始轉檔」一律恢復可按——它不隨路徑欄內容亮暗(與逐字稿那條的
    `_after_run` 刻意不同,理由見 doctab.preview_summary)。"""
    _converting["on"] = False
    return (
        gr.update(interactive=True),   # 路徑欄
        gr.update(interactive=True),   # 選擇檔案…
        gr.update(interactive=True),   # 選擇資料夾…
        gr.update(interactive=True),   # 清空
        gr.update(interactive=True),   # 包含子資料夾
        gr.update(interactive=True),   # 辨識圖片裡的文字
        gr.update(interactive=True),   # 郵件附件一併轉檔
        gr.update(interactive=True),   # 開始轉檔
        gr.update(interactive=False),  # 停止
    )


def _doc_stop():
    """「停止」:設取消旗標。實際停下來要等當前這個檔案處理完(檔界)。"""
    cancel.request()
    gr.Info("已要求停止:目前這個檔案處理完就停,已轉好的檔案會保留。")
    return gr.update(interactive=False)


def _doc_open_dirs(dirs):
    """「開啟輸出資料夾」。**成功時不出任何提示**(使用者 2026-08-01 指定
    拿掉右上角 toast):工作列本來就會提醒,完整路徑也已經在批次報告裡。
    只有開不起來才以 gr.Error 說明。"""
    try:
        doctab.open_output_dirs(dirs)
    except UserFacingError as e:
        raise gr.Error(str(e), title="提醒", print_exception=False) from None


# 「使用說明」的篇章清單(目錄順序)。內容含本機偵測結果與講者人數上限,
# 所以要等 build_ui 算出裝置才生得出來——建構時算一次、存成模組級查表,
# 因為 handler 從前端拿得到的只有「使用者點了哪個標籤」這一個字串。
_HELP_ORDER: list[help_text.HelpPage] = []


def _load_help(device_name: str, max_speakers: int) -> list[help_text.HelpPage]:
    """算出說明篇章並存進模組級查表(就地換內容,參照不變)。"""
    _HELP_ORDER[:] = help_text.help_pages(device_name, max_speakers)
    return _HELP_ORDER


def _show_help(label: str):
    """換一篇說明:兩塊 Markdown 換值、隱私截圖換顯示與否。

    只動葉子元件的「值」,不碰任何容器(見分頁裡的註解)。找不到標籤時
    整組 gr.skip():選項本來就是這份清單生出來的,對不上代表是程式的錯,
    而在使用者面前把「使用說明」換成一段英文 traceback 是更糟的結果。
    """
    page = next((p for p in _HELP_ORDER if p.label == label), None)
    if page is None:
        return gr.skip(), gr.skip(), gr.skip()
    # 截圖檔缺失時那個元件的 value 是 None,顯示出來只會是一塊空框
    return page.top, gr.update(visible=page.image and _PRIVACY_IMG.exists()), page.bottom


def build_ui() -> gr.Blocks:
    # 啟動時偵測一次,讓使用者在開始前就對速度有預期(spec §6);
    # 執行中若降級,完成後預覽開頭會標示「實際」裝置
    device = transcribe.predicted_device()
    has_gpu = device != "cpu"
    # 有 GPU 時轉錄與講者分析並行、且講者分析才是瓶頸,精準幾乎不增加總時間
    # (16 分鐘實測快速/精準總時間皆約 5.6 分)——故預設精準、換取更準的文字。
    # 無 GPU 時轉錄改在 CPU 且與講者分析依序執行,精準約慢 4 倍,預設快速。
    # 判準在 transcribe(命令列也用同一份):兩邊各寫一次的話,同一台機器
    # 會因為你從哪個入口進來而拿到不同品質的逐字稿
    default_model = MODEL_KEYS[transcribe.default_model_key()]
    # 欄位旁直接講偵測結果與已選好的模型(文案為使用者 2026-07-26 指定);
    # 完整理由收在「使用說明」分頁
    model_info = (
        "電腦偵測到 GPU,已自動選好「精準」(文字更準,總時間和快速差不多)"
        if has_gpu else
        "電腦沒有偵測到 GPU,已自動選好「快速」(改選精準會慢約 4 倍)"
    )
    # 使用說明長文只有文案,收在 help_text;裝置名與講者上限在此帶入
    _load_help(DEVICE_NAMES[device], MAX_SPEAKERS)
    # gradio 6 起 theme/css 移到 launch() 傳入(見 main());Blocks 只留結構
    with gr.Blocks(title="AI 文件.MD 轉換器") as demo:
        # 標題列一行帶過用途與隱私;完整說明(首次下載模型等)在「使用說明」分頁。
        # ⚠️ **不放「本機偵測:<裝置>」**(使用者 2026-08-08 指定刪除,同批
        # 要求整句更簡潔):那是**選模型**時才用得上的資訊,擺在每個分頁都
        # 看得到的標題列上,等於讓一行永遠不會變的字佔著最顯眼的位置。
        # 它沒有消失——「❓ 使用說明」的「模型」那段仍報偵測結果
        # (`help_text` 的 device_name),而那裡正是要拿它做決定的地方。
        # ⚠️ **「檔案不外傳」不可省**:那是隱私規格對使用者的承諾
        # (spec §7),也是他敢把會議錄音丟進來的唯一理由,測試守著
        gr.Markdown(
            "# AI 文件.MD 轉換器\n"
            "本系統係將聲音、文字、圖像轉換成適合 AI 閱讀的 Markdown 格式"
            "(以節省 Token)。全部在本機轉換,檔案不外傳;"
            "第一次使用請先看「❓ 使用說明」(首次轉檔需下載模型)。",
            elem_id="app-header",
        )
        # 各分頁的 id= 會落在分頁鈕的 data-tab-id:轉檔/錄音中鎖分頁的
        # CSS 錨點+Playwright 測試的穩定選擇器(預設是隨元件增減漂移的
        # 數字流水號,不可依賴;分頁文字又與內文/按鈕字樣重複,不可用文字選)
        with gr.Tabs(elem_id="main-tabs"):
            # ---- 分頁 1:聲音→MD(錄音/音檔 → 逐字稿 md)----
            # 「名單與聲紋」收成子分頁(使用者指定 2026-08-04):名單只供
            # 講者命名的下拉、聲紋只供講者自動辨識,對文件轉檔毫無意義——
            # 擺在頂層會讓兩條產品線的功能混在同一排分頁裡。子分頁鈕仍是
            # #main-tabs 的後代,「工作進行中鎖名單分頁」那條 CSS 不必改;
            # 膠囊樣式則要另寫一份(見 ui_style 的 #audio-tabs 註解)
            with gr.Tab("🎙️ 聲音→MD", id="tab-audio"):
                with gr.Tabs(elem_id="audio-tabs"):
                    # ---- 子分頁 1-1:主流程(左欄操作、右欄結果)----
                    with gr.Tab("🎧 轉檔", id="tab-run"):
                        with gr.Row():
                            with gr.Column(scale=5):
                                # 命名區放「左欄最上方」:轉檔前隱藏、不佔位;轉完出現時
                                # 與右欄預覽頂端切齊——放在設定區下方的話,講者一多,
                                # 欄位起點就掉到預覽結尾之後,又回到上下捲動(使用者實測回報)。
                                # 選檔/設定被推下去無妨:命名當下用不到,要轉下一檔再捲回去。
                                # 各欄位帶該講者的發言摘錄(_hint_text),多數情況看摘錄
                                # 即可認人、連預覽都不必翻
                                # 命名區塊「永遠掛載」,顯示與否交給 CSS(#name-box
                                # 依內有無渲染中的命名框判斷)——gradio 6.20 地雷:
                                # 「visible 會切換的容器」remount 時,children 會帶
                                # 上一次顯示時的舊 props 重生,同訊息的 children 更新
                                # 只有部分生效(使用者回報 7 講者→2 講者換檔後舊框
                                # 整排冒回來;Playwright 最小重現:連明確送
                                # visible=False 都擋不住)。容器不動,children 的
                                # 顯示/隱藏更新就完全可靠(同一重現驗證)
                                with gr.Column(variant="panel", elem_id="name-box"):
                                    # pad-x:面板圓角會裁掉貼邊的首字(使用者截圖回報:
                                    # 「為/名/動」左半被切)
                                    # 精簡版說明(使用者指定:原版字太多);完整細節
                                    # 在「使用說明」分頁
                                    # ⚠️ **只留「這一屏要做的事」**(2026-08-18
                                    # 精簡:原本 83 字三行,講了四件事)。搬走的
                                    # 三件——聲紋會被記住、下次自動辨識、未知欄
                                    # 不記聲紋——「❓ 使用說明」都已經有。
                                    # 「分不出來就按核對」原本印在**每一位**的
                                    # 線索尾巴(每列多 33px),移到這裡講一次
                                    gr.Markdown(
                                        # ⚠️ **同時提兩顆鈕**:核對只亮在該核對
                                        # 的那幾列,只寫它等於叫某些列的人去按
                                        # 一顆不存在的鈕——那正是 2026-08-15
                                        # 在每一列的線索裡踩過的坑,搬到頂部
                                        # 講一次也一樣要避
                                        "**為講者命名(選填)**——看摘錄、按"
                                        "「▶️ 試聽」或「🔍 核對」認人;"
                                        "填好按「套用」,留白維持「講者 N」。",
                                        elem_classes=["pad-x"],
                                    )
                                    # 試聽的「出聲載體」(使用者指定 2026-07-18:不要
                                    # 播放器介面,按試聽即從頭播、按停止即停):gr.Audio
                                    # 僅供出聲,由 CSS(#audition-player)移出畫面。
                                    # 必須維持 visible=True——gradio 6 對 visible=False
                                    # 的元件整個不渲染,前端沒有元件就不會有聲音;
                                    # 事件也一律只動 value 不動 visible。autoplay 建構時
                                    # 設定,之後每次換 value 都自動播;interactive 必須
                                    # 明寫 False——此元件只當事件輸出,但空值時被推斷
                                    # 成可互動會整塊變成錄音區(同 downloads 地雷)
                                    audition_player = gr.Audio(
                                        interactive=False, autoplay=True,
                                        buttons=[],  # 藏下載/分享鈕(gradio 6 以此取代 show_download_button)
                                        elem_id="audition-player",
                                    )
                                    # 試聽鈕放在命名框「旁邊」同列(設計稿:欄位標題旁
                                    # 的膠囊小鈕;gradio 的 label 列塞不進按鈕,退而與
                                    # 整個下拉同列置中對齊)。字樣/顯示由 _run 與
                                    # _audition 系列統一管理
                                    # 「改成幾位 + 重新分群」(設計稿 B 案,
                                    # 使用者 2026-08-18 選定;A 案重用左欄的
                                    # 「講者人數」要改個數字就得重走一趟讀取,
                                    # C 案折疊成一顆鈕則把剛講清楚的能力又藏
                                    # 起來)。⚠️ **只在第三種狀態出現**:逐字稿
                                    # 同層要有錄音檔**與**分群檔,由
                                    # _naming_page_updates 依 features 切;整組
                                    # 包在一個 Column 裡、只切它一個 visible,
                                    # 分開切會在三個元件之間留下空隙
                                    with gr.Column(
                                        visible=False, elem_id="recluster-box",
                                    ) as recluster_box:
                                        # 排法:標題、說明、輸入列各一行,最後
                                        # 一條分隔線(使用者 2026-08-18 指定;
                                        # 前一版把標題塞進輸入列裡,三個東西擠
                                        # 在同一行反而讀不出「這是一組功能」)。
                                        # ⚠️ **標題不用 Number 自己的 label**:
                                        # gr.Number 的 label 排在輸入框上面,
                                        # 同列的按鈕就只能跟輸入框齊高、跟標題
                                        # 錯開(使用者回報「高低有差,怪怪的」)
                                        # 兩段文字的樣式照抄命名列的
                                        # 「講者 N 的名字」與「共 N 段發言…」
                                        # (使用者 2026-08-18 指定),由 CSS
                                        # 以這兩個 class 對齊
                                        gr.Markdown(
                                            "改成幾位講者",
                                            elem_classes=["pad-x", "recluster-title"],
                                        )
                                        gr.Markdown(
                                            _RECLUSTER_HINT,
                                            elem_classes=["pad-x", "recluster-hint"],
                                        )
                                        # ⚠️ **排法照抄命名列**(使用者
                                        # 2026-08-18 指定「跟下面的一樣」):
                                        # 欄位 scale=1 佔滿、按鈕接在右邊,
                                        # 連 Row 都不加 .pad-x(.name-row 也
                                        # 沒有)——幾何一致就不必再補 padding。
                                        # ⚠️ **不可以直接掛 .name-row**:
                                        # `#name-box:not(:has(.name-row .block))`
                                        # 拿它當「命名區有沒有東西」的判準,
                                        # 掛上去會讓沒有講者時命名區也跑出來
                                        with gr.Row(elem_classes=["recluster-row"]):
                                            # ⚠️ **要 container=False**:留著
                                            # 容器會多畫一層灰底圓框、還把整列
                                            # 撐到 88px 高(使用者 2026-08-18
                                            # 回報「上下行距太寬,有看到一個
                                            # 灰色的圈圈」)。拿掉之後 input 自己
                                            # 的圓角就是 10px——**與下拉可見框
                                            # 的 10px 相同**(量出來的,不是猜的)
                                            recluster_n = gr.Number(
                                                value=2, precision=0,
                                                # ⚠️ **下限 2 由前端設 min 屬性**
                                                # (使用者 2026-08-18:上下箭頭
                                                # 按得到負數),不設 gradio 的
                                                # `minimum`——它的 preprocess
                                                # 會對超限值拋**英文**錯誤
                                                # (spec §8:訊息一律繁中),
                                                # 範圍照本專案慣例在伺服器端
                                                # clamp。見 CLUE_COLLAPSE_JS
                                                show_label=False, container=False,
                                                scale=1,
                                                elem_classes=["recluster-n"],
                                            )
                                            # 大小與「試聽」一致(size="sm" →
                                            # 13px、高 28、圓角 999px);scale=0
                                            # 讓它維持自身寬度靠右,不設的話會
                                            # 跟輸入框一起長,實測撐到 225px
                                            recluster_btn = gr.Button(
                                                "🔀 重新分群", scale=0, size="sm",
                                                elem_id="recluster-btn",
                                            )
                                    # 分群檔路徑:重新分群時要讀它。放 State
                                    # 而不是每次重推,是因為那一列的顯示與這個
                                    # 值必須同進同出(見 _naming_page_updates)
                                    features_state = gr.State(None)
                                    name_inputs = []
                                    audition_btns = []
                                    # 「🔍 核對」只亮在該核對的那幾列(設計稿方案 B,
                                    # 使用者 2026-08-13 選定):未知 + 檔尾診斷點名的
                                    # 前三名。其餘列與原本完全一樣,版面零新增
                                    audit_btns = []
                                    for i in range(MAX_SPEAKERS):
                                        with gr.Row(elem_classes=["name-row"]):
                                            name_inputs.append(gr.Dropdown(
                                                choices=[], allow_custom_value=True,
                                                visible=False, scale=1,
                                                label=f"講者 {i + 1} 的名字",
                                            ))
                                            # ⚠️ **兩顆鈕上下疊**(使用者 2026-08-14
                                            # 第二次修正):放左邊雖然讓試聽齊頭,
                                            # 卻讓「有核對」那幾列的名字欄與摘錄
                                            # 變窄——同一份摘錄在不同列折行不同,
                                            # 讀起來更亂。改成疊在試聽下面:名字欄
                                            # 的寬度從此與有沒有核對鈕無關,而試聽
                                            # 永遠在同一個位置
                                            with gr.Column(
                                                scale=0, min_width=100,
                                                elem_classes=["name-btns"],
                                            ):
                                                audition_btns.append(gr.Button(
                                                    _AUD_PLAY_LABEL, visible=False,
                                                    size="sm", elem_classes=["aud-btn"],
                                                ))
                                                audit_btns.append(gr.Button(
                                                    _AUDIT_LABEL, visible=False,
                                                    size="sm", elem_classes=["audit-btn"],
                                                ))
                                    # 「未知」命名框:排在所有講者框之後,逐字稿有未知
                                    # 段落才顯示(_run 決定)。只改文字、絕不登記聲紋
                                    # (理由見 _apply_names docstring);info 由 _run
                                    # 動態帶「不登記聲紋」提示+未知段落的線索
                                    with gr.Row(elem_classes=["name-row"]):
                                        unknown_input = gr.Dropdown(
                                            choices=[], allow_custom_value=True,
                                            visible=False, scale=1,
                                            label="「未知」的名字",
                                        )
                                        # 「未知」一定有核對鈕:那一批本來就常是
                                        # 好幾個人的插話混在一起(檔尾診斷也這樣寫);
                                        # 同樣疊在試聽下面(見上面那條寬度的理由)
                                        with gr.Column(
                                            scale=0, min_width=100,
                                            elem_classes=["name-btns"],
                                        ):
                                            unknown_aud_btn = gr.Button(
                                                _AUD_PLAY_LABEL, visible=False,
                                                size="sm", elem_classes=["aud-btn"],
                                            )
                                            unknown_audit_btn = gr.Button(
                                                _AUDIT_LABEL, visible=False,
                                                size="sm", elem_classes=["audit-btn"],
                                            )
                                    # 套用/放棄同列:有時不想改名也不想下載(使用者
                                    # 指定 2026-07-24),「跳過命名」走 _discard_naming
                                    # 整頁復位;成品不刪、預覽留一行指路。字樣要短:
                                    # 長字樣在窄欄折行(使用者截圖回報 2026-07-24)。
                                    # 與上方留白由 .apply-row 的 CSS 給——掛在
                                    # 個別按鈕上會兩顆一高一低(同日截圖回報)
                                    with gr.Row(elem_classes=["apply-row"]):
                                        apply_btn = gr.Button(
                                            "套用名字到逐字稿並下載",
                                            variant="primary", scale=3,
                                        )
                                        discard_btn = gr.Button(
                                            "跳過命名", scale=1, min_width=110,
                                        )
                                # ---- 「要做什麼」切換(2026-07-21 設計稿案 A:同分頁)----
                                # 三種模式互斥,切換只動「葉子元件」的 visible
                                # (_switch_source;容器 remount 地雷見 name-box 註解)。
                                # 預設「現場收音」(使用者指定 2026-07-23:開啟程式就是
                                # 收音模式):下方各葉子元件的初始 visible 必須與
                                # _switch_source 切到收音模式的結果一致,兩處要同步改
                                source_mode = gr.Radio(
                                    # 選項字串與順序的沿革見 _MODE_* 常數處
                                    choices=[_MODE_RECORD, _MODE_FILE, _MODE_RELABEL],
                                    value=_MODE_RECORD,
                                    label="要做什麼", elem_classes=["seg-radio"],
                                    # 收音模式下與下方「收音情境」貼成同一張卡
                                    # 的 CSS 錨點(2026-08-07 選案 A,見 ui_style
                                    # 的「左欄兩張卡貼合」規則)
                                    elem_id="source-mode",
                                    # 說明小字跟著模式換(_MODE_INFO;切換在
                                    # _switch_source、開頁接回在 _restore_recording)
                                    info=_MODE_INFO[_MODE_RECORD],
                                )
                                # 檔案來源=路徑,不上傳(參考 MP4-2-SRT,使用者指定
                                # 2026-07-26;理由見「檔案來源」區註解)。
                                # 2026-08-06 起收多檔/資料夾,兩種模式的差別見
                                # _SRC_MODE_HINT 與 srcfile.looks_like_batch
                                src_path = gr.Textbox(
                                    label="會議錄音/錄影檔案路徑",
                                    lines=3, max_lines=8,
                                    placeholder="按下方按鈕挑選,或直接貼上完整路徑"
                                                "(一行一個,帶引號沒關係)",
                                    visible=False,  # 預設收音模式;切「轉錄音檔」才顯示
                                    elem_id="src-path",
                                )
                                # 收音/重設講者模式下三顆鈕都隱藏(gradio 6 對
                                # visible=False 整個不渲染),整列是零高的空盒、
                                # 仍照吃一份 layout_gap——.src-pick-row 讓 CSS
                                # 收掉它(同 .rec-row 的 :has 技巧)
                                with gr.Row(elem_classes=["src-pick-row"]):
                                    pick_btn = gr.Button(
                                        "選擇檔案…", visible=False,
                                        elem_id="pick-file-btn",
                                    )
                                    pick_dir_btn = gr.Button(
                                        "選擇資料夾…", visible=False,
                                        elem_id="pick-dir-btn",
                                    )
                                    # 選檔是「累加」不是「取代」(同文件分頁,
                                    # 2026-08-01 使用者回報),所以要有清空
                                    clear_btn = gr.Button(
                                        "清空", visible=False, scale=0, min_width=80,
                                        elem_id="clear-src-btn",
                                    )
                                # 選了什麼的即時摘要(轉檔前就讓人確認範圍,同文件分頁)。
                                # **緊接在選檔鈕之下**(使用者指定 2026-08-06):
                                # 它是「按下按鈕的當場回饋」,中間隔著一段靜態說明
                                # 的話,選完檔的變化不仔細看會整個被略過
                                src_summary = gr.Markdown(
                                    "", visible=False, elem_classes=["pad-x"],
                                )
                                # 兩種模式的差別必須寫在選檔介面上(使用者指定
                                # 2026-08-06):不寫的話「為什麼這次沒讓我命名講者」
                                # 只能靠自己撞出來,而那已經是 30 分鐘之後的事。
                                # 排在摘要**之後**:它是選之前掃一眼的靜態參考,
                                # 不是當下的狀態
                                src_hint = gr.Markdown(
                                    _SRC_MODE_HINT, visible=False,
                                    elem_classes=["pad-x"],
                                )
                                # 「開始轉檔/停止」**排在「講者人數」之前**(設計稿
                                # 選案 B,使用者 2026-08-15 選定):在「轉錄音檔」模式
                                # 下,原本的順序讓這顆鈕落在 857px、而使用者的可視
                                # 高度是 797px——**整顆鈕在畫面外**,每次轉檔都得先
                                # 捲一下(他截圖回報)。移上來之後底端 705px、離視窗
                                # 底還有 92px(Playwright 實測,視窗 1307×797)。
                                # ⚠️ **只影響「轉錄音檔」**:這一列在收音模式平時整組
                                # 隱藏,收音那一頁的版面一格都沒動(2026-08-07 才調好的
                                # 貼卡不受影響)。代價是「講者人數」退到鈕的下面,要填
                                # 它仍得捲——取捨是「每次都要按的」贏過「多數場次留 0
                                # 不動的」。
                                # ⚠️ **「平時」兩個字是後來補的**(2026-08-15 使用者當天
                                # 就回報):按下「停止錄音」之後的收尾期間,
                                # `_lock_for_rec_finish` 會把這一列的「停止」**臨時亮
                                # 回來**——於是它就夾在兩張卡中間、把貼合撐開 60px。
                                # 修法在 ui_style(`.run-row` 的 `order`,收音模式下把
                                # 這一列**在視覺上**排到錄音雙鈕之後),**不是搬回 DOM
                                # 原位**:搬回去等於把「開始轉檔」推回第一屏外。
                                # ⚠️ 教訓:判斷「這一列在某個模式看不看得見」時,要把
                                # **每一個狀態**都走過(待機/錄音中/收尾中/開頁接回),
                                # 不能只看三個模式的靜態畫面——設計稿與驗收都只走了
                                # 靜態模式,所以漏掉收尾那一段。
                                # ⚠️ **位置有 CSS 相依**:它插在「要做什麼」與「收音
                                # 情境」兩張卡中間,而那兩張卡靠**相鄰兄弟選擇器**貼合
                                # (ui_style 的「左欄兩張卡貼合」),中間每多一列就要在
                                # 那三條規則裡各補一段 `+ .run-row`,否則貼合**靜默**
                                # 失效、收音模式的「開始錄音」跟著被擠下去。
                                # 測試 test_recording_mode_merges_two_cards_css 把
                                # 「中間有幾列」與「CSS 有沒有寫進去」綁在一起守。
                                # ⚠️ 也**不可以再往下挪到「收音情境」與「講者人數」
                                # 之間**:那兩顆是連續的表單元件、被 gradio 包在同一個
                                # <form> 裡(= 畫面上的同一張白卡),中間插一列 Row 會
                                # 把那張卡切成兩張。
                                # 收音模式兩顆都隱藏 → 整列空,由 .run-row 的
                                # CSS 收掉(同 .src-pick-row,見該處註解)
                                with gr.Row(elem_classes=["run-row"]):
                                    # 按鈕狀態設計(使用者規格):初始皆不可按——路徑
                                    # 欄有內容「開始」才亮,按下「開始」後才輪到「停止」。
                                    # 預設收音模式時整組隱藏(切「轉錄音檔」才顯示)。
                                    # elem_id 是 js 即時鎖鈕(run-btn)/「停止中…」
                                    # 改字(stop-btn)的錨點
                                    run_btn = gr.Button(
                                        "開始轉檔", variant="primary", scale=4,
                                        visible=False, interactive=False, elem_id="run-btn",
                                    )
                                    stop_btn = gr.Button(
                                        "停止", variant="stop", scale=1,
                                        visible=False, interactive=False, elem_id="stop-btn",
                                    )
                                # ---- 現場收音(預設模式,初始顯示)----
                                # 情境照「使用場合」命名(訊號來源對照見 SCENARIO_LABELS)。
                                # **每次啟動都是 DEFAULT_SCENARIO**(使用者 2026-08-09
                                # 指定拿掉「記住上次選擇」);錄音中開頁接回的是「這場
                                # 錄音當時選的」,那是另一回事,見 _restore_recording
                                scenario = gr.Radio(
                                    choices=list(SCENARIO_LABELS), value=DEFAULT_SCENARIO,
                                    label="收音情境",
                                    elem_classes=["seg-radio"],
                                    # 與上方「要做什麼」貼成同一張卡的 CSS 錨點
                                    # (見 source_mode 的 elem_id 註解)
                                    elem_id="rec-scenario",
                                    # 說明只給選中的那一個情境(使用者指定
                                    # 2026-08-07;切換由 _scenario_info 換,
                                    # 開頁接回錄音由 _restore_recording 換)
                                    info=_SCENARIO_INFO[DEFAULT_SCENARIO],
                                )
                                # (曾有「講者辨識」勾選框在此、「講者人數」之前;
                                # 使用者 2026-07-26 以會議一律要分講者為由移除,
                                # 連同 diarize_speakers=False 整條跳過路徑與
                                # 「下載檔案」收工鈕,勿在無新指示下加回)
                                # 講者人數留在主流程、不收回「進階參數設定」(使用者
                                # 指定 2026-07-24 從摺疊區移出):開會中數清人數要當場
                                # 填得到。⚠️ 2026-08-15 為了把「開始轉檔」拉進第一屏,
                                # 選案時另有一案是把它收回摺疊區,使用者**沒有選**
                                # ——那會連現場收音一起收掉,正是 07-24 移出來的理由。
                                # 位置關係從此**兩個模式不同**:收音模式仍在「開始
                                # 錄音」之前(這一段沒動),轉錄音檔模式則排在「開始
                                # 轉檔」之後(見上方 .run-row 的註解)。
                                # 檔案/收音兩模式共用、不隨 _switch_source 切換;
                                # 鎖定走 _param_updates(檔案轉檔中鎖;錄音中保持
                                # 可輸入——收尾才讀,見 _param_updates docstring)
                                speakers = gr.Number(
                                    value=0, precision=0,
                                    label="講者人數(0 = 自動偵測)",
                                    # 使用者指定的口吻:人多根本數不清,別鼓勵硬填;
                                    # 只有人少又確定時填了才更準
                                    info=f"人少又確定(如 1~3 人)才填;其餘留 0 自動判斷,最多 {MAX_SPEAKERS}",
                                )
                                # pad-x:貼容器邊的字會被裁半邊(pad-x 地雷;使用者
                                # 截圖回報狀態列換行處「現/在」各剩半個字)
                                rec_status = gr.Markdown(
                                    _REC_IDLE_MD, elem_id="rec-status",
                                    elem_classes=["pad-x"],
                                )
                                # 錄音雙鈕同列;整列空(兩鈕都隱藏)時由 CSS 收掉
                                # (.rec-row 規則,同 .name-row 的 :has 技巧)
                                with gr.Row(elem_classes=["rec-row"]):
                                    rec_start_btn = gr.Button(
                                        "● 開始錄音", variant="primary",
                                        elem_id="rec-start-btn",
                                    )
                                    rec_stop_btn = gr.Button(
                                        "■ 停止錄音並完成逐字稿", variant="stop",
                                        interactive=False,
                                        elem_id="rec-stop-btn",
                                    )
                                # 錄音計時/即時預覽的秒針(開始錄音才啟動)
                                rec_timer = gr.Timer(1.0, active=False)
                                # 轉檔進度的秒針。**正常轉檔時不啟動**——
                                # 同一個 session 裡 gr.Progress 本來就好好的,
                                # 每秒多一趟往返是白付的成本。只有開頁發現
                                # 「有一份轉檔在跑」時(= 使用者按過重新連線
                                # 或 F5)才由 `_restore_transcribing` 打開,
                                # 接手畫進度並在轉完時收尾
                                run_timer = gr.Timer(1.0, active=False)
                                # 其餘參數都有堪用預設值,收進摺疊區、平常不用點開;
                                # 摺疊區放主按鈕列之後(使用者指定 2026-07-24:主流程
                                # 「選檔/開錄 → 開始」在上、少用的設定沉底。同日講者
                                # 人數移回主流程,見上;2026-07-19 曾連講者人數一起收)
                                with gr.Accordion(
                                    "進階參數設定", open=False, elem_id="adv-params",
                                ):
                                    # 摺疊區內順序:模型 → CPU 核心數(曾有
                                    # 「輸出格式」勾選與「語音語言」中/英選擇,
                                    # 2026-07-26 分別隨固定 md 輸出、固定中文移除)
                                    model = gr.Radio(
                                        choices=list(MODEL_LABELS), value=default_model,
                                        label="模型", info=model_info,
                                        elem_classes=["seg-radio"],
                                    )
                                    # 預設**不含**子資料夾(使用者指定 2026-08-06,
                                    # 與「文字、圖像→MD」那邊的預設相反):一份 PDF
                                    # 幾秒鐘,一場兩小時的會議要跑一小時上下——不小心
                                    # 指到上層目錄的代價在音訊上是好幾天,不是幾分鐘
                                    recursive = gr.Checkbox(
                                        value=False, label="包含子資料夾",
                                        # 預設是收音模式,那時這個勾選框無效
                                        # → 初始隱藏,與 _switch_source 切到
                                        # 收音的結果一致(兩處要同步改)
                                        visible=False,
                                        info="只在選了資料夾時有作用。預設不含"
                                             "——錄音檔轉一份要數十分鐘,"
                                             "不小心掃到整顆磁碟會跑上好幾天",
                                    )
                                    cores = gr.Number(
                                        value=power.default_worker_count(), precision=0,
                                        label="CPU 核心數",
                                        # 文案重點(使用者指定 2026-07-26):講清楚
                                        # 「預設已自動留 1 核」與「太卡就調小」的用法,
                                        # 舊電腦的使用者才知道這裡是逃生口
                                        info=f"預設 {power.default_worker_count()}"
                                             "(=自動保留 1 核給其他程式);轉檔時"
                                             "電腦太卡就把數字調小——轉檔會變慢,"
                                             f"但電腦比較順(可填 1～{power.max_cpu_cores()})",
                                    )
                            with gr.Column(scale=7):
                                # 高度固定 20 行:整份逐字稿很長,框內捲動即可,
                                # 不讓預覽把頁面撐滿;進度條也畫在這裡(show_progress_on)
                                # elem_id 是進度文字放大的 CSS 錨點(#preview-box)
                                # 核對面板:按「🔍 核對」時取代預覽(聽的時候本來
                                # 就不必看預覽,按「完成核對」再切回來)。**零新增
                                # 版面**——它佔的是預覽原本的位置
                                # ⚠️ **用 Column 不用 Group**(2026-08-13 使用者截圖):
                                # Group 會把相鄰的按鈕併成一條「分段控制」——
                                # 圓角被吃掉、兩顆黏在一起,而且播放器自己的圓角
                                # 會跟 Group 的圓角在上緣互咬出缺口
                                with gr.Column(visible=False, elem_id="audit-panel") as audit_panel:
                                    # ⚠️ **用字由使用者自己定**(2026-08-15 他逐字給
                                    # 的版本;先前那版他嫌太長,並指定刪掉「怎麼點
                                    # 播放」那句——那是基本操作常識,而且核對表自己
                                    # 的標籤上已經寫了)。**改措辭前先問他,不要順手
                                    # 潤稿**;測試守的是三個要點在不在,不是逐字比對。
                                    # 介面上只留「不知道就會做錯」的兩條——改掛會取消
                                    # 這次的聲紋登記、一列混多人時整列改掛會波及別人;
                                    # 完整說明在「❓ 使用說明」分頁,本來就只該有一份。
                                    # ⚠️ 第二條是使用者選擇**不做**「拆列改掛」(設計稿
                                    # C 案)之後唯一的出路說明,所以它不是裝飾。拆列的
                                    # 診斷與代價見 docs/dev/pipeline.md(切分其實有切開,
                                    # 是後面合併的;調參數已實測否決)
                                    gr.Markdown(
                                        # 開頭標出面板的來歷(使用者 2026-08-15:
                                        # 「這樣才知道這個面板是按下 🔍 核對 長出來
                                        # 的」)——它取代的是右欄的預覽,不標的話
                                        # 不容易看出畫面為什麼換了
                                        f"**{_AUDIT_LABEL}**\n\n"
                                        "聽出某幾段其實是別人,多選起來、指定名字按"
                                        "「套用改掛」。**改掛就不會登記聲紋庫**,"
                                        "否則會讓下次辨識失準。\n\n"
                                        "⚠️ **遇到一列混著好幾人聲時,那列請不要勾**,"
                                        "改掛會把其他人一起登記錯誤。"
                                        "如想分開講者,請直接用 md 手動改。",
                                        elem_classes=["audit-note"],
                                    )
                                    # 出聲載體:**沒有介面**,由 CSS 移出畫面
                                    # (同 #audition-player)。使用者看到的只有
                                    # 每一列前面那顆 ▶/■——2026-08-13 他用過
                                    # 整批播放器之後指定的:「我直接在那列上按
                                    # 播放比較好操作,不用點下面又去點上面」。
                                    # ⚠️ 絕不可改成 visible=False:gradio 對它
                                    # 整個不渲染,前端沒有元件就不會出聲
                                    audit_player = gr.Audio(
                                        label="核對播放", interactive=False,
                                        autoplay=True, elem_id="audit-player",
                                    )
                                    # 第一欄是勾選框:改掛只動勾起來的列。
                                    # ⚠️ 內容欄要夠寬才認得出是哪一句,所以
                                    # 「長度」用純數字(不加單位)省一點寬度
                                    # ⚠️ **整張表唯讀**(2026-08-14 使用者第四次
                                    # 回報延遲,而且 19 列也卡):可編輯的
                                    # Dataframe 每勾一次就整張重繪,延遲發生在
                                    # 「勾下去的瞬間」——那是純前端成本,伺服器
                                    # 再快也沒用。勾選因此搬到下面的多選下拉,
                                    # 表格只剩「看」與「點左欄聽」
                                    audit_table = gr.Dataframe(
                                        headers=["聽", "#", "相似度", "長度", "內容"],
                                        datatype=["str", "number", "str", "str", "str"],
                                        column_count=5, interactive=False,
                                        wrap=False, max_height=420,
                                        # ⚠️ **右上角兩顆內建鈕用參數關掉**(使用者
                                        # 2026-08-14 圈掉):`[]` 與不給不同義——
                                        # 沒給就是兩顆全開(見 ui.md 名單表格那條)。
                                        # 這張表是拿來聽與選的,「複製」給的 TSV
                                        # 沒有去處、「全螢幕」把它攤滿整個視窗也
                                        # 沒有意義
                                        buttons=[],
                                        # ⚠️ **不顯示標籤**(使用者 2026-08-15 指定
                                        # 刪掉「逐段對照(點左邊那一格聽那一段,再點
                                        # 一次停)」):怎麼點播放是基本操作常識,而
                                        # 表頭第一欄的「聽」已經講完同一件事。
                                        # 用 show_label=False 而不是 label="" ——後者
                                        # 在 gradio 6.20 仍會佔一個標籤列的高度
                                        show_label=False,
                                    )
                                    # 要改掛哪幾段:原生多選,勾幾百個都不卡,
                                    # 而且可以打字搜尋(「相似度」也印在選項上,
                                    # 低的那幾段一眼看得到)
                                    # 下半部(選段落 + 指定名字 + 兩顆鈕)收成
                                    # **一張卡**(使用者 2026-08-14:「現在是
                                    # 分好幾塊,合起來、行距跟旁邊卡片一樣」)。
                                    # ⚠️ 用 CSS 讓內部的 block 透明、外框由這一層
                                    # 給——**不用 gr.Group**:Group 會把相鄰的兩顆
                                    # 鈕併成分段控制(圓角被吃掉),那是 2026-08-13
                                    # 才踩過的坑
                                    with gr.Column(elem_classes=["audit-actions"]):
                                        audit_pick = gr.Dropdown(
                                            choices=[], value=[], multiselect=True,
                                            label="要改掛的段落(可多選;數字是相似度)",
                                        )
                                        with gr.Row(elem_classes=["apply-row"]):
                                            audit_name = gr.Dropdown(
                                                choices=[], allow_custom_value=True,
                                                scale=1, label="把選取的段落改掛給",
                                            )
                                            audit_apply_btn = gr.Button(
                                                "套用改掛", variant="primary",
                                                scale=0, min_width=110,
                                            )
                                            audit_close_btn = gr.Button(
                                                "完成核對", scale=0, min_width=110,
                                            )
                                preview = gr.Textbox(
                                    label="逐字稿預覽(轉檔進度顯示於此)",
                                    lines=20, max_lines=20, buttons=["copy"],
                                    elem_id="preview-box",
                                )
                                # interactive=False 必須明寫:此元件也是「套用後自動
                                # 下載」js 步驟的輸入,gradio 會因此自動推斷成可互動,
                                # 空值時整塊變成上傳拖放區(使用者截圖回報)。
                                # height 只管「有檔案時」的列表上限;空狀態整塊由
                                # CSS 藏(#download-box:has(.empty),見
                                # ui_style.APPLE_CSS)——元件必須永遠掛載供 js 取址,
                                # 不可改成 visible 切換
                                downloads = gr.Files(
                                    label="下載逐字稿", elem_classes=["titled-label"],
                                    elem_id="download-box", interactive=False, height=120,
                                )

                    # ---- 子分頁 1-2:資料維護(名單供命名下拉;聲紋供自動辨識)----
                    with gr.Tab("👥 名單與聲紋", id="tab-roster"):
                        # 三欄的 scale 是 **3 / 4 / 3**,不是等分(Playwright 實測
                        # 定的):等分是每欄 379px,而中間那欄的四顆鈕需要
                        # 96×3 + 80 + 20×3 = **428px**——等分會把它們打回兩列
                        # (那正是 2026-08-09 才剛整平的東西)。3/4/3 給中間 455px、
                        # 兩側各 341px,四顆鈕留 27px 餘裕。
                        # ⚠️ **動任何一欄的 scale 都要重算中間那欄**:判準是
                        # 「中間欄實寬 ≥ 428px」,算法見該按鈕列的註解。
                        # 中間最寬是有道理的,不是為了遷就按鈕:它裝的控件最多
                        # (摘要+下拉+四顆鈕+會就地展開的健檢結果),而兩側各只有
                        # 一張表格或兩個欄位。
                        # gr.Column 的 min_width 預設 320,三欄 960+40=1000px
                        # 仍小於容器的 1177px,所以全寬下不會換列(視窗再窄就會
                        # 自動疊成上下,那是 gradio 的預設響應式行為,可接受)
                        with gr.Row():
                            with gr.Column(scale=3):
                                # 三欄的說明一律只留「這是什麼 + 最關鍵的一個動作」
                                # (使用者 2026-08-09:「字太多」;同 2026-08-08
                                # 文件分頁那次的處理)。砍掉的三件事**都不是消失**,
                                # 而是本來就在「❓ 使用說明」裡各有一份:命名時打的
                                # 新名字會自動加入、`data/attendees.txt` 這個路徑、
                                # 以及可隨程式版控——欄寬只有 341px,三行說明會把
                                # 表格整個往下推,而那三件事沒有一件是**當下**要知道的。
                                # 2026-08-09 使用者再改成現在這句:原本的
                                # 「改完按『儲存名單』」是**指令**,而那顆鈕就在
                                # 表格正下方、自己看得到;留下的是「這一欄是什麼、
                                # 它餵給誰」——那才是使用者站在這裡會問的問題
                                gr.Markdown(
                                    "### 與會人員名稱維護\n"
                                    "設定講者時的名稱下拉式選單,請在此維護。",
                                    elem_classes=["pad-x"],
                                )
                                # 名單一長會把整頁撐開(使用者截圖回報):限高、表格內自捲
                                # elem_id 是給 CSS 補「白卡外觀」用的錨點:
                                # gr.Dataframe 沒有 .form 那一層、它的 .block 又帶
                                # hide-container(透明底、padding 0),所以預設長得
                                # 不像旁邊兩欄的卡片(見 ui_style 的 #att-table)
                                # buttons=[] 拿掉表格右上角 gradio 內建的兩顆
                                # (複製整張表 / 全螢幕),使用者 2026-08-10 圈掉:
                                # 這裡是**一份要編輯的名單**不是要帶走的資料——
                                # 複製出去的 TSV 沒有用途,全螢幕更是把一欄名字
                                # 攤成整個視窗。⚠️ 空清單與 None 不同義:前端是
                                # `buttons===null ? 顯示 : buttons.includes(...)`,
                                # 不給這個參數就是兩顆全開(見 docs/dev/ui.md)。
                                # 表格沒有 label、show_search 又是預設的 none,
                                # 所以整條工具列會一起消失(不是留一條空白)。
                                # ⚠️ **max_height 326 與 ui_style 的內距 24/28
                                # 是一組的**(2026-08-10 C 案):內距從 12 加到
                                # 24 會讓白卡高 +24px,而「儲存名單」本來就快
                                # 貼到視窗底(1240×736 實測鈕底 728、只剩 8px)
                                # ——把限高同步減 24 才能讓整欄高度與那顆鈕的
                                # 位置一個像素都不動(實測鈕底維持 728)。
                                # 只改一邊就是把鈕推到視窗外,而那顆鈕不見的
                                # 症狀是「改完名字沒有存到」。突變 M121 守著。
                                att_table = gr.Dataframe(
                                    elem_id="att-table",
                                    value=data_tabs.attendee_rows(), headers=["與會人員名稱"], datatype="str",
                                    column_count=(1, "fixed"), row_count=1, type="array",
                                    interactive=True, max_height=326, buttons=[],
                                )
                                # ⚠️ **決策區一律在「儲存名單」正上方,不在
                                # 下面**(設計稿方案 D,使用者選定
                                # 2026-08-09)。原本是儲存**之後**才在按鈕
                                # 下方長出追問,而 Playwright 實測
                                # (1240×736、64 人名單):那顆鈕本來就貼在
                                # 視窗底(鈕底 y=700),追問區 207px **整個**
                                # 落在視窗外(確認鈕 y=867)、頁面又不會自動
                                # 捲——使用者看不到就等於沒問,而沒按的後果
                                # (名單與聲紋庫從此對不上)要等下次開會那個
                                # 人認不出來才發現。
                                #
                                # 現在:改完名字**還沒按儲存**就先預告,並附
                                # 一個預設打勾的「一起改聲紋」,按一次儲存兩
                                # 邊都改完。預告會把儲存鈕往下推,而他為了按
                                # 到那顆鈕必然往下捲、必然經過預告。
                                #
                                # ⚠️ 容器**永遠掛載**、四個孩子各自切
                                # visible(同 #name-box:gradio 6.20 對
                                # 「visible 會切換的容器」有 children 帶舊
                                # props 重生的地雷)。整塊的顯示與否交給
                                # CSS(`#att-decision` 依內有無沒被藏起來的
                                # `.att-cue` 判斷)——包成一張卡是必要的:
                                # 這是使用者唯一會被問到聲紋的地方,長得跟
                                # 旁邊的說明文字一樣就等於沒問
                                with gr.Column(elem_id="att-decision"):
                                    att_preview_note = gr.Markdown(
                                        "", visible=False,
                                        elem_classes=["pad-x", "att-cue"],
                                    )
                                    att_sync_check = gr.Checkbox(
                                        label="儲存時一併改掉聲紋", value=True,
                                        visible=False, container=False,
                                        elem_classes=["pad-x", "att-cue"],
                                    )
                                    # 合併(新名字已經有聲紋)**不吃那個勾選**:
                                    # 把兩個人併成一個是破壞性的、錯了在逐字稿
                                    # 裡只看得出「少了一個人」,一定要走
                                    # 「儲存 → 看數字 → 按確認」
                                    att_rename_note = gr.Markdown(
                                        "", visible=False,
                                        elem_classes=["pad-x", "att-cue"],
                                    )
                                    # 兩顆鈕**永遠一起出現、一起收掉**:少了
                                    # 「暫不修改」,不想合併的人只能不理那塊
                                    # 追問(它會留到下次儲存),於是「他決定
                                    # 不改」與「他根本沒看到」長得一模一樣
                                    # ——而安全網那句常駐提醒正是靠這個差別
                                    # 才有意義。min_width 100:預設 160×2+20
                                    # = 340px,這一欄只有 341px(同下面那列
                                    # 的 1px 陷阱)。整列都藏起來時由
                                    # `.att-btn-row` 那條 CSS 收掉空列
                                    with gr.Row(elem_classes=["att-btn-row"]):
                                        att_rename_btn = gr.Button(
                                            "一併改聲紋", variant="primary",
                                            min_width=100,
                                            visible=False,
                                            elem_classes=["att-cue"],
                                        )
                                        att_rename_skip_btn = gr.Button(
                                            "暫不修改", min_width=100,
                                            visible=False,
                                            elem_classes=["att-cue"],
                                        )
                                att_rename_state = gr.State(None)
                                # min_width 給 100(四個字要 96px):預設 160×2 + 20
                                # = 340px,而這一欄實寬 341px——只差 1px 就換列,
                                # 而換列之後兩顆各佔一整行(算法見中間欄的註解)
                                with gr.Row():
                                    att_save_btn = gr.Button(
                                        "儲存名單", variant="primary", min_width=100,
                                    )
                                    att_reload_btn = gr.Button("重新載入", min_width=100)
                                att_status = gr.Markdown("")
                            with gr.Column(scale=4):
                                gr.Markdown("### 聲紋資料管理", elem_classes=["pad-x"])
                                vp_info = gr.Markdown(data_tabs.vp_summary(), elem_classes=["pad-x"])
                                vp_pick = gr.Dropdown(
                                    # 順序跟左邊名單一致(見 vp_names_in_roster_order)
                                    choices=data_tabs.vp_names_in_roster_order(),
                                    multiselect=True,
                                    # 這個下拉兼任「已登記名單」的完整檢視(見 data_tabs.vp_summary)
                                    label="已登記的人(要刪除聲紋時在此選取,可多選)",
                                )
                                # **四顆同一列**(使用者 2026-08-09 指定;原本
                                # 「健檢」自己掉到第三列)。⚠️ **能塞進一列是
                                # 靠縮短字樣換來的,不是排版技巧**:同 scale 的
                                # 鈕會**等分**寬度,而這一欄只有 578px(視窗
                                # ≥1240)/508px(1100)/458px(1000),四等分後
                                # 每顆只有 129/112/**99**px——字塞不下時 gradio
                                # 不裁字、是折成兩行(驗收要看鈕高,見 ui.md)。
                                # 以「字數×16 + 32」反推:最窄情況下**每顆最多
                                # 四個字**。原字樣「刪除選取的人的聲紋」要 176px、
                                # 「清除全部聲紋」128px,兩顆都超標。
                                # 字能縮是因為**周圍已經講過「聲紋」**(區塊標題
                                # 「聲紋資料管理」+ 下拉標籤「已登記的人(要刪除
                                # 聲紋時在此選取)」),按鈕只需要講「動作+範圍」;
                                # 「選取/全部」正好是一組對照,比原本兩句長話好讀。
                                # ⚠️ 改字樣要連 `help_text.py` 那兩句一起改。
                                # 「健檢」給 scale=0:它只要 64px,跟著等分會讓
                                # 另外三顆各少 12px(106→99),而 99px 對四個字
                                # 只剩 3px 餘裕——差一點就折行。不等分還順帶保住
                                # 2026-08-07 那條考量:健檢是這裡最無害的動作
                                # (只讀不寫),不該跟「清除全部」一樣顯眼。
                                # ⚠️ 它的 min_width 給 **80** 不是剛好的 64:
                                # 64 = 文字寬本身,餘裕 0px,字型換一顆(這裡是
                                # -apple-system 在 Windows 上的回退)或使用者調
                                # 瀏覽器縮放就折行。80 與文件分頁的「清空」同寬,
                                # 兩處的小鈕看起來也才是一套的。
                                # 不加 emoji:同列其他三顆都沒有(使用者 2026-08-07 指出)
                                with gr.Row():
                                    vp_del_btn = gr.Button("刪除選取", min_width=96)
                                    vp_clear_btn = gr.Button(
                                        "清除全部", variant="stop", min_width=96,
                                    )
                                    vp_reload_btn = gr.Button("重新載入", min_width=96)
                                    vp_health_btn = gr.Button(
                                        "健檢", scale=0, min_width=80,
                                    )
                                # 「清除全部」確認結果的載體(見下方接線)。
                                # 不顯示也不佔版面:gradio 6.20 對開頁就
                                # visible=False 的元件是**整個不渲染**。
                                # 建構值是空字串,而空字串 = 不清除
                                vp_clear_flag = gr.Textbox(visible=False, value="")
                                # 健檢結果(設計稿方案 A,使用者 2026-08-07 選定:
                                # 就地展開、不另開分頁)。三個都是**葉子元件**,
                                # 切 visible 是安全的——ui.md 那條「visible 會切換
                                # 的容器 remount 會帶舊 props」講的是容器,而這裡
                                # 刻意不包 Column/Group,就是為了避開它
                                vp_health_out = gr.Markdown("", elem_classes=["pad-x"])
                                vp_health_pick = gr.CheckboxGroup(
                                    choices=[], label="要刪除的項目(可複選)",
                                    visible=False,
                                )
                                vp_health_apply_btn = gr.Button(
                                    "刪除勾選的項目", variant="primary", visible=False,
                                )
                            # ---- 第三欄:改名(設計稿 **B 案**,使用者 2026-08-09 選定)----
                            # ⚠️ **改名不是第三個功能,是橫跨左邊兩份資料的一個動作**:
                            # 名字是聲紋庫認人的鍵,只改名單不改聲紋,下次開會就認不出
                            # 這個人。它原本疊在「聲紋資料管理」底下(同一欄兩個 ###
                            # 標題、另一半的入口又藏在最左欄底部),使用者 2026-08-09
                            # 回報「介面比較混亂」並問**要不要整個拿掉**。
                            # ⚠️ **不拿掉的理由記在這裡,免得下次再問一次**:它是聲紋庫
                            # 唯一的**非破壞性**修正手段——沒有它,名字打錯或要補部門時
                            # 只剩「刪掉那個人的聲紋、重新累積」,而刪除的代價是實測過的
                            # (CLAUDE.md 那條 2026-08-08 實跡:刪三個樣本後重跑,段數
                            # 字數逐一吻合、名字卻從 1:17:04 起整串錯開);它也是唯一的
                            # **合併**手段——同一個人被登記成兩個名字時,
                            # `recognize_batch`「同一場會議一個名字只給一位」的約束會
                            # 形同虛設,那個人可以佔兩格,而錯誤會自我複製。
                            # 提升成獨立一欄之後標題層級才對得起來:三欄、三個 ###。
                            # ⚠️ 「一併改聲紋」那半**刻意留在最左欄**:它不是第二個
                            # 入口,是在表格裡改完名字按下儲存之後的追問,離開那個時刻
                            # 就沒有意義。拿掉它的話,順手改名單會靜靜地讓聲紋庫對不上。
                            with gr.Column(scale=3):
                                # 欄位標題是「修改名稱作業」(使用者 2026-08-09
                                # 定名),而鈕上的字仍是「改名」——標題講的是
                                # 「這一欄在做什麼」,鈕講的是「按下去會發生
                                # 什麼」,兩者本來就不必是同一個詞(同左欄的
                                # 「與會人員名稱維護」配「儲存名單」)。
                                # ⚠️ 指路文字要跟著標題走:程式訊息與使用說明
                                # 裡「用最右邊的『…』」那幾句指的是**這一欄**,
                                # 不是那顆鈕(data_tabs 三處、help_text 三處)
                                gr.Markdown("### 修改名稱作業", elem_classes=["pad-x"])
                                # **這一行是這一欄存在的理由**:三張卡並排時,使用者
                                # 要能立刻看出這張與左邊兩張的關係(它同時動兩邊),
                                # 否則「改名」看起來只像聲紋庫的附屬功能。
                                # 精簡到一句(使用者 2026-08-09:「字太多」)——留下的
                                # 是**只有事前才有用**的那半:改名會動到兩邊。
                                # 同日再砍掉句尾的「只改一邊,下次就認不出他」:
                                # 這裡根本沒有「只改一邊」這個選項(這一欄一律兩邊
                                # 一起改),那半句在講的是**別條路**的後果,擺在
                                # 這裡只會讓人以為自己要做什麼選擇。真的會分兩邊的
                                # 是名單表格那條路,那句話在那裡(見預告文案)。
                                # 砍掉的「撞名=合併」那兩句不必留在這裡:真的撞到時
                                # 流程會停下來、把誰的幾個樣本併進誰列給你看
                                # (vp_rename_note + 確認合併鈕),那時說才有用;
                                # 完整版另在「❓ 使用說明」的改名那節
                                gr.Markdown(
                                    "改掉一個人的名稱,**名稱與聲紋庫同時生效**。",
                                    elem_classes=["pad-x"],
                                )
                                # 兩個欄位是一組,用 gr.Group 讓它們貼在一起
                                # ——各自獨立時中間吃一份 layout_gap(20px),
                                # 看起來像兩件不相干的事(使用者 2026-08-08
                                # 回報「行距太遠」)。Group 是 gradio 原生的
                                # 「這幾個欄位屬於同一件事」表達,而且**這個
                                # 容器不切 visible**,沒有 children 帶舊 props
                                # 重生的地雷(見 name-box 註解)
                                with gr.Group(elem_id="vp-rename-fields"):
                                    vp_rename_pick = gr.Dropdown(
                                        choices=data_tabs.vp_names_in_roster_order(),
                                        label="要改名的人", filterable=True,
                                    )
                                    # 不放 placeholder(使用者指定 2026-08-08):
                                    # 選了人就會把名字帶進來,這格幾乎不會是空的
                                    # ——擺一句範例反而讓人以為要照那個格式填
                                    vp_rename_to = gr.Textbox(label="改成")
                                vp_rename_btn = gr.Button("改名")
                                vp_rename_note = gr.Markdown("", elem_classes=["pad-x"])
                                vp_rename_confirm_btn = gr.Button(
                                    "確認合併", variant="stop", visible=False,
                                )
                                vp_rename_state = gr.State(None)

                    # ---- 子分頁 1-3:領域詞表(使用者指定 2026-08-04 搬進來)----
                    # 它**只**注入 Whisper 的解碼視窗(hotwords.as_string),
                    # 與「名單與聲紋」同理是這個功能專屬的資料。
                    # 另一份 replace.txt 為何留在頂層見 tab-lexicon 那一段
                    with gr.Tab("🔤 領域詞表", id="tab-hotwords"):
                        gr.Markdown(
                            "轉錄時提示 AI 優先選這些詞,減少同音錯字"
                            "(金控≠監控、壽險≠受險)。一行一詞、`#` 開頭是註解,"
                            "在這裡編輯等同用記事本改 `data/hotwords.txt`,"
                            "**存檔後下一個轉錄的檔案生效**(進行中的那個不受影響)。\n"
                            "**順序即優先序**:詞表有長度預算,超出的尾端會被靜默"
                            "忽略——重要的詞放前面。",
                            elem_classes=["pad-x"],
                        )
                        hw_box = gr.Textbox(
                            value=data_tabs.read_data_file(hotwords.store_file()),
                            lines=16, max_lines=16, label="詞表內容(一行一詞)",
                        )
                        with gr.Row():
                            hw_save_btn = gr.Button("儲存詞表", variant="primary")
                            hw_reload_btn = gr.Button("重新載入")
                        hw_status = gr.Markdown(
                            data_tabs.hotwords_status(), elem_classes=["pad-x"],
                        )

            # ---- 分頁 2:文字、圖像→MD(批次;與逐字稿那條完全獨立)----
            # 刻意不併進「聲音→MD」的選檔鈕:那條左欄是講者命名/聲紋/
            # 試聽的狀態機,對一份 PDF 毫無意義,合併只會讓 _run 從中間
            # 分岔成兩條無關的路,把回歸風險壓在已經在用的功能上
            with gr.Tab("📄 文字、圖像→MD", id="tab-doc"):
                # 說明只留三件事:轉什麼、檔案放哪裡、錄音怎麼辦(使用者
                # 2026-08-08 選案 B:「字太多」)。**格式不再逐一列副檔名**
                # ——40 個副檔名排下來比說明本身還長,而真正需要那份清單的
                # 時刻是「我這個檔到底能不能轉」,那時 `docsrc.validate` 的
                # 錯誤訊息就會把完整清單列出來(見 docsrc.supported_hint)。
                # ⚠️ 種類數走 `len(GUI_TYPES)` 不寫死:加了新格式卻忘了改
                # 這裡,說明就會**默默少報**一個數字,沒有任何跡象
                # ⚠️ **刻意寫成一整段,不分行也不空行**(使用者 2026-08-08
                # 兩次指定的合起來:先「字太多」、看過分段版又說「我要的是
                # 版面更矮」)。三件事照舊都在,只是讓它們流水排下去——
                # Playwright 實測(1500px 視窗、內容寬 1300px)分段版 102px
                # → 48px,而精簡前的舊版是 93px:少掉 40 個副檔名之後,
                # 整段從四行縮到兩行。
                # 順帶記一筆 Markdown 的行為:單一 `\n` **不會**換行(原本
                # 那四句在程式裡分了行,渲染出來一直是黏成一片的),要分段
                # 只能用空行,而空行帶來的段距正是這次要省掉的東西
                gr.Markdown(
                    "把 Word、PowerPoint、Excel、PDF、網頁、掃描件、照片與"
                    "**錄音錄影**轉成 Markdown(`.md`),方便交給 AI 閱讀。"
                    "轉好的檔案放在**原始檔案的旁邊**(同名、副檔名換成 `.md`),"
                    "文件裡的圖片會存進同名的 `.assets` 資料夾。"
                    f"共支援 {len(docsrc.GUI_TYPES)} 種格式(完整清單見"
                    "「❓ 使用說明」);錄音錄影會轉成逐字稿,要**替講者取真實"
                    "姓名**請改用「🎙️ 聲音→MD」分頁。",
                    # 測試靠 elem_id 找這段(同全檔慣例);拿文案內容當定位鍵
                    # 的話,下次潤稿就會讓測試以「找不到分頁說明」倒掉——
                    # 而那個訊息跟真正的原因完全無關(這段文案兩天內改了三次)
                    elem_id="doc-intro", elem_classes=["pad-x"],
                )
                with gr.Row():
                    with gr.Column(scale=5):
                        doc_src = gr.Textbox(
                            label="要轉換的檔案或資料夾(一行一個)",
                            placeholder="按下方按鈕挑選,或直接把路徑貼進來",
                            lines=6, max_lines=6, elem_id="doc-src-path",
                        )
                        # ⚠️ **三顆鈕的 min_width 是一組的,動一顆要重算**
                        # (2026-08-09 Playwright 實測):gradio 6.20 給每顆鈕
                        # `min-width: min(<min_width>px, 100%)`、Row 的 gap 是
                        # 20px,而這一欄**再寬也只有 482px**(scale=5/12 + 版面
                        # 上限)。預設的 160×3 + 20×2 = 520px > 482px,所以舊版
                        # 在**任何**視窗寬度都會換行,「清空」自己佔滿一整行
                        # ——使用者 2026-08-09 截圖回報的「按鈕有點亂」正是它。
                        # 現在 130+130+80+40 = 380px,連 1000px 的視窗(此欄
                        # 382px)都塞得下。130 是實測值:鈕縮到 131px 時
                        # 「選擇資料夾…」六個字仍完整不裁切
                        with gr.Row():
                            doc_files_btn = gr.Button(
                                "選擇檔案…", elem_id="doc-pick-files-btn",
                                min_width=130,
                            )
                            doc_folder_btn = gr.Button(
                                "選擇資料夾…", elem_id="doc-pick-folder-btn",
                                min_width=130,
                            )
                            # 「清空」是「選檔累加」的配套(見 doctab.clear_paths),
                            # 但它不是與兩顆選檔鈕同層級的動作——等寬會讓它看起來
                            # 一樣重要(使用者 2026-08-09 回報「按鈕有點亂」)。窄版
                            # 與「🎙️ 聲音→MD」的 clear_btn 一致,兩個分頁本來就該長
                            # 一樣;⚠️ **不要順手拿掉這顆鈕**:這個分頁轉完檔不會
                            # 自動清空路徑欄(`_doc_convert` 的 outputs 沒有 doc_src,
                            # 與逐字稿那條相反),沒有它就只剩「自己去框裡全選刪除」
                            doc_clear_btn = gr.Button(
                                "清空", scale=0, min_width=80,
                                elem_id="doc-clear-btn",
                            )
                        # ⚠️ **visible=False 不是可有可無的初始值**:常駐的
                        # 空 Markdown 高 0 卻照吃兩份 layout_gap,選檔列與
                        # 「開始轉檔」中間就會空出 40px(其他相鄰列都是 20px)。
                        # 見 _doc_summary 的實測數據
                        doc_summary = gr.Markdown(
                            "", visible=False, elem_classes=["pad-x"],
                        )
                        # **三顆同一列**(使用者 2026-08-09 指定;原本是
                        # 「開始轉檔/停止」一列、「開啟輸出資料夾」獨佔一列)
                        # ——省掉一列 60px(鈕高 40 + gap 20),而那三顆本來就
                        # 是同一件事的三個時刻:開始、中止、看成果。
                        # ⚠️ 三顆的 min_width 是一組的,動一顆要重算(算式與
                        # 482px 這個上限見上一列的註解):100+80+110+20×2=330px,
                        # 1000px 視窗(此欄 382px)也不換行。
                        # ⚠️ **真正的限制不是換行、是折行**:三顆同 scale 會**等分**
                        # 剩餘寬度(min_width 只決定換不換列,不影響分到多寬),
                        # 每顆只有 114px(1000px 視窗)~147px(≥1240px);字塞不下
                        # 時 gradio 的鈕**不裁字也不溢出,是折成兩行**(鈕高 40→64),
                        # 所以驗收要看 **`getBoundingClientRect().height`**,
                        # 拿 `scrollWidth - clientWidth` 量會全程是 0、假綠燈。
                        # Playwright 實測各字樣「不折行」所需鈕寬(字型 16px、
                        # 左右內距各 16px):**開啟輸出資料夾 144px**(原名,
                        # 1000/1100px 視窗都折兩行,故 2026-08-09 使用者指定縮短)、
                        # **輸出資料夾… 126px**(現名,≥1100px 視窗單行)、
                        # 開啟資料夾/輸出資料夾 112px(連 1000px 都單行)。
                        # 改字樣要連說明文件三處一起改(README 一處、使用說明兩處)
                        with gr.Row():
                            # 「開始轉檔」**一律可按**(使用者 2026-08-01
                            # 指定,與逐字稿那條刻意不同):貼上路徑時前端
                            # 不一定會觸發 input 事件,按鈕不亮會讓人以為
                            # 工具壞了;按下去才把關,錯誤訊息會講清楚是
                            # 空的、找不到、還是格式不支援
                            doc_run_btn = gr.Button(
                                "開始轉檔", variant="primary",
                                elem_id="doc-run-btn", min_width=100,
                            )
                            doc_stop_btn = gr.Button(
                                "停止", variant="stop",
                                elem_id="doc-stop-btn", interactive=False,
                                min_width=80,
                            )
                            doc_open_btn = gr.Button(
                                "輸出資料夾…", elem_id="doc-open-dir-btn",
                                interactive=False, min_width=110,
                            )
                        # 三個開關的預設值都是對的,收進摺疊區、平常不用點開
                        # (使用者指定 2026-08-01,與「轉逐字稿」分頁一致:
                        # 主流程「選檔 → 開始」在上、少用的設定沉到按鈕列之後)
                        with gr.Accordion(
                            "進階參數設定", open=False, elem_id="doc-adv-params",
                        ):
                            # **三個都要有 info**:少一個的話那一列比別人矮
                            # 一截,三列的行距看起來忽大忽小(使用者截圖回報
                            # 2026-08-01)。這是版面問題但解法在內容——補一行
                            # 說明比拿 CSS 去撬 gradio 的 .block 間距穩得多
                            doc_recursive = gr.Checkbox(
                                value=True, label="包含子資料夾",
                                info="選整個資料夾時,連裡面的子資料夾一起找;"
                                     "關掉只轉最上層那一層",
                            )
                            # 內嵌圖也會 OCR(使用者 2026-08-01 指定:給 AI 用
                            # 時圖裡的文字才是重點),所以這個開關影響的不只
                            # 掃描檔——關掉會快很多,但簡報裡的圖就只剩連結
                            doc_ocr = gr.Checkbox(
                                value=True,
                                label="辨識圖片裡的文字(OCR)",
                                info="掃描檔、照片,以及文件裡的內嵌圖都會辨識;"
                                     "關掉會快很多,但那些圖只會留下連結",
                            )
                            doc_mail_att = gr.Checkbox(
                                value=True,
                                label="郵件附件一併轉檔",
                                info="Outlook 郵件(msg/eml)的附件會一起轉成 "
                                     "Markdown 並在信件本文連結;關掉只轉信件本身",
                            )
                        gr.Markdown(
                            "轉出的內容裡,「**〔 〕**」標的是無法完整呈現的地方"
                            "(圖表、儲存格底色等),檔頭也有一份清單——"
                            "寧可讓你看到少了什麼,也不要安靜地少掉。",
                            elem_classes=["hint"],
                        )
                    with gr.Column(scale=7):
                        doc_result = gr.Textbox(
                            label="轉檔進度與結果", lines=20, max_lines=20,
                            elem_id="doc-result-box", interactive=False,
                            # gradio 6 拿掉了 show_copy_button,改成 buttons 清單
                            buttons=["copy"],
                        )
                # 成品所在的資料夾(給「開啟輸出資料夾」用):批次可能跨
                # 多個資料夾,所以存清單不是單一路徑
                doc_dirs_state = gr.State([])

            # ---- 分頁 3:用詞替換表(replace.txt,純文字原樣編輯)----
            # **留在頂層**(使用者裁決 2026-08-04,同日領域詞表搬進「聲音→MD」):
            # 這一份**兩條路徑的輸出都會套用**——逐字稿走 pipeline.finalize、
            # 文件走 docpipe._traditionalize,兩者都是 convert.to_taiwan_traditional
            # ——收進聲音功能底下等於把影響文件輸出的設定藏起來。
            # 分頁名稱跟著內容走(原為「詞表維護」,只剩一張表之後那個名字
            # 已經名不副實)。elem_id 維持 tab-lexicon 純粹是不值得為此改四處
            # (這裡、ui_style 的鎖定規則、兩條測試斷言)——**不是**因為有
            # 回歸風險:那兩條測試都是全等比對,改漏了會當場失敗。
            # 代價是 lexicon 這個字如今在隔壁的 tab-hotwords 身上更貼切,
            # 下次真要動這些 id 時順手改成 tab-replace 即可
            with gr.Tab("📚 用詞替換表", id="tab-lexicon"):
                gr.Markdown(
                    "輸出前把偶發的大陸用詞換成台灣用詞(軟件→軟體)。"
                    "一行一條「原詞 新詞」(空格或 Tab 分隔)、`#` 開頭是註解,"
                    "在這裡編輯等同用記事本改 `data/replace.txt`,"
                    "**存檔後下一個轉換的檔案生效**(進行中的那個不受影響)。\n"
                    "**加詞前先讀檔頭的收詞原則**:台灣也在用的詞"
                    "(如「優化、用戶」)收了反而會改壞原文。",
                    elem_classes=["pad-x"],
                )
                rp_box = gr.Textbox(
                    value=data_tabs.read_data_file(convert.replace_file()),
                    lines=16, max_lines=16, label="替換規則(一行一條:原詞 新詞)",
                )
                with gr.Row():
                    rp_save_btn = gr.Button("儲存替換表", variant="primary")
                    rp_reload_btn = gr.Button("重新載入")
                rp_status = gr.Markdown(
                    data_tabs.replace_status(), elem_classes=["pad-x"],
                )

            # ---- 分頁 4:完整使用說明(2026-08-08 設計稿方案 A:左目錄、右內容)----
            with gr.Tab("❓ 使用說明", id="tab-help"):
                # 原本是一整篇 9,816 字的長文、一條捲軸到底,改成點一篇看一篇。
                # 分類軸(使用需求 8 篇,不是功能)與每一篇的內容見 help_text
                # 的 docstring。**三個內容元件都是葉子**(Markdown / Image /
                # Markdown),換篇時只換它們的「值」與截圖的 visible,
                # 完全不動容器——會切 visible 的容器 remount 會帶舊 props
                # 重生(命名區踩過,見 docs/dev/ui.md)。目錄用 gr.Radio 而
                # 不是一排按鈕:選中狀態由它自己維持,不必自己寫狀態機。
                with gr.Row():
                    # sticky 在 CSS(#help-nav-col):右欄捲到中段還要換得了篇,
                    # 否則就是「捲回頂端才能換」——那正是這次要修掉的毛病
                    with gr.Column(scale=0, min_width=248, elem_id="help-nav-col"):
                        help_nav = gr.Radio(
                            choices=[p.label for p in _HELP_ORDER],
                            value=_HELP_ORDER[0].label,
                            label="你現在想做什麼",
                            elem_id="help-nav",
                        )
                    with gr.Column():
                        # pad-x:內文與分頁容器圓角之間留空隙,標題首字不被截
                        help_top = gr.Markdown(
                            _HELP_ORDER[0].top, elem_classes=["pad-x"],
                        )
                        # 隱私設定截圖(夾在「先關一個設定」那段中間,只有
                        # 該篇與「全部內容」看得到)。靜態 value 會在建構時
                        # 複製進 gradio 自家快取,不需列入 allowed_paths;
                        # 檔案缺失只少一張圖,絕不擋啟動(value=None + 永不顯示)
                        help_img = gr.Image(
                            value=str(_PRIVACY_IMG) if _PRIVACY_IMG.exists() else None,
                            show_label=False,
                            interactive=False,
                            buttons=["fullscreen"],
                            container=False,
                            # 原圖 1423px,滿版太大;窄視窗時退回 100%
                            width="min(100%, 720px)",
                            elem_classes=["pad-x"],
                            visible=_HELP_ORDER[0].image,
                        )
                        help_bottom = gr.Markdown(
                            _HELP_ORDER[0].bottom, elem_classes=["pad-x"],
                        )
                # radio 不是自己的 outputs,用 .change 即可(無繞回疑慮)
                help_nav.change(
                    _show_help, help_nav, [help_top, help_img, help_bottom],
                )

        # 右上角外觀設定(gradio 頁尾已由 CSS 整個藏掉);position:fixed,
        # 放哪裡都不佔版面,收在尾端不打斷主流程的結構
        gr.HTML(ui_style.THEME_MENU_HTML, elem_id="theme-menu")

        vp_state = gr.State({})
        # 記住「真正的 output/ 檔案路徑」;套用名字時寫回這些原檔(而非下載快取)
        paths_state = gr.State([])
        # 試聽:{講者標籤: 片段路徑}與「正在播誰」(None=沒在播);
        # 兩者都跟著每一次轉檔換新、復位時歸零
        clips_state = gr.State({})
        playing_state = gr.State(None)
        # 核對:{blocks: 每一輪發言, src: 從哪個音檔剪, sources: 分軌對應,
        # open: 正在核對誰, rows: 這一批抽出來的段落}。⚠️ State 只放得下
        # 可序列化的東西,所以區塊存成 dict 不是 SpeechBlock
        audit_state = gr.State({})

        # 轉檔中關瀏覽器要先確認(ui_style.UNLOAD_GUARD_HEAD 的 beforeunload 依
        # __msBusy 判斷):舉旗用獨立的純前端事件(js-only 不進佇列,點下
        # 當下即生效),不掛在主事件的 js 參數上——那會改寫送給 fn 的輸入。
        # 放下掛 .then+.failure「成對」:完成/報錯/按停止一律恢復可關
        # (gradio 6.20 的 .then 只在成功後觸發,詳見下方收尾段的地雷註解)。
        # 舉旗之外,順手在瀏覽器端立刻鎖住「開始」:伺服器端的 _start_run
        # 隨後補上正式的 interactive=False,這裡先關掉「連點兩下排進兩批轉檔」
        # 的空窗(js-only 不進佇列,點下當下生效)
        run_btn.click(None, js="""
        () => {
          window.__msBusy = true;
          for (const id of ['run-btn', 'pick-file-btn', 'pick-dir-btn',
                            'clear-src-btn']) {
            const b = document.getElementById(id);
            if (b) b.disabled = true;
          }
        }
        """)
        # 進階參數三控件:檔案轉檔中全鎖;錄音中鎖二留一——講者人數
        # 收尾才讀,錄音中可輸入(_param_updates;使用者指定 2026-07-24)。
        # 順序即 _param_updates 的回傳順序(speakers 在首位,特殊處理)
        adv_params = [speakers, model, cores, recursive]
        # 選檔區的鈕(三顆一起鎖):轉檔中改選檔會與進行中的批次對不上
        src_btns = [pick_btn, pick_dir_btn, clear_btn]
        # 「成品/命名區」的元件群:轉完檔要整組換新、復位要整組清掉,
        # 每個相關事件的 outputs 都以它為前段。具名一次是形狀契約的唯一
        # 出處——曾在各接線處逐字重複,增減元件時漏改一處就只在該路徑
        # 上安靜失準(命名區塊容器**不在**其中:永遠掛載、顯示交給 CSS)
        page_outputs = [
            downloads, preview, *name_inputs, unknown_input,
            vp_state, paths_state,
            clips_state, playing_state, audition_player,
            *audition_btns, unknown_aud_btn,
            # 核對整組:順序必須與 _audit_reset_updates / _naming_page_updates
            # 的尾端完全一致(那兩支各組一次值,錯位就是「音檔跑到表格裡」)
            audit_state, audit_player, audit_table, audit_pick, audit_name,
            audit_panel, *audit_btns, unknown_audit_btn,
            # 重新分群那一列(顯示與否)+ 分群檔路徑 + 人數欄的值,
            # 順序同上面兩支的尾端
            recluster_box, features_state, recluster_n,
        ]
        # 轉檔鏈三步依序:鎖介面+整頁復位 → 轉檔 → 收尾還原(順序保證與
        # 「復位必須是 _run 之前的另一批訊息」的理由見 _start_run)
        run_evt = run_btn.click(
            _start_run,
            outputs=[src_path, *src_btns, run_btn, stop_btn, source_mode,
                     *adv_params, *page_outputs],
            show_progress="hidden",
        ).then(
            _run,
            inputs=[src_path, model, speakers, cores, recursive, source_mode],
            outputs=[*page_outputs, src_path],
            # 進度條預設會在「每個輸出元件」上各畫一份;指定只畫在預覽區
            show_progress_on=preview,
        )
        # 「重新分群」:與轉檔鏈同一個形狀(先整頁復位再跑)——_present_result
        # 的前置條件是「進來時所有講者框/試聽鈕已隱藏且清空」,少了復位那一步
        # 就會踩到 _name_section_updates 那串 gr.skip() 的地雷
        # ⚠️ **檢查排在復位之前**:`.then` 只在前一步成功後才跑,所以
        # 擋下來的時候整頁復位根本沒有發生,畫面原封不動——反過來寫的話
        # 一報錯就停在被清空的畫面上,只能按 F5(2026-08-18 使用者回報)
        recluster_btn.click(
            _check_recluster,
            inputs=[recluster_n, features_state, paths_state],
            show_progress="hidden",
        ).then(
            _reset_for_recluster, outputs=page_outputs, show_progress="hidden",
        ).then(
            _run_recluster,
            inputs=[recluster_n, features_state, paths_state, cores],
            outputs=[*page_outputs, src_path],
            show_progress_on=preview,
        )
        # 收尾:按鈕/路徑欄還原,依路徑欄有無內容決定「開始」亮不亮
        # (報錯保留路徑可直接重試,使用者選定)。
        # 地雷(gradio 6.20,Playwright 最小重現實測):.then 實際只在前一
        # 事件「成功」後觸發(與文件宣稱的不分成敗不符),報錯後只有
        # .failure 會跑(server 與 js-only 皆然)——收尾必須 .then/.failure
        # 成對掛,缺 .failure 則一次報錯就把介面鎖死、守門旗標永遠放不下。
        # 兩者互斥(成功走 .then、報錯走 .failure),不會重複執行。
        # 注意 .failure 掛在「_run 那一步」上:鏈頭 _start_run 必須不可能
        # 拋例外(純 update + 已兜底的 pending.clear),否則失敗會落在沒有
        # .failure 的步上、鏈死介面鎖死。
        for chain in (run_evt.then, run_evt.failure):
            chain(
                _after_run, inputs=[src_path],
                outputs=[src_path, *src_btns, run_btn, stop_btn,
                         source_mode, *adv_params],
                show_progress="hidden",
            )
            chain(None, js=ui_style.RUN_DONE_JS)
        # 停止的即時回饋(js-only、點下當下生效)+伺服器端設取消旗標,
        # 同時收工(清路徑欄、鎖雙鈕、人數歸零;使用者規格:按停止即還原)
        stop_btn.click(None, js=ui_style.STOP_FEEDBACK_JS)
        stop_btn.click(
            _request_stop,
            outputs=[src_path, run_btn, stop_btn, speakers],
            show_progress="hidden",
        )
        # 路徑欄有內容才亮「開始」。掛 .input 而非 .change:程式化清空
        # (轉檔收尾/停止/套用/跳過/切換來源)本來就在自己那批訊息裡帶了
        # 正確的按鈕狀態,.change 會為每次清空多跑一趟無意義的往返
        src_path.input(
            _run_btn_for, inputs=[src_path], outputs=[run_btn],
            show_progress="hidden",
        )
        # 摘要另掛一條:它要多讀「包含子資料夾」,而按鈕那條刻意只吃路徑欄
        # (判準單一,見 _run_btn_for)。兩者都掛 .input——程式化清空自己
        # 那批訊息裡就帶了正確狀態,不必多跑一趟
        src_path.input(
            _src_summary, inputs=[src_path, recursive], outputs=[src_summary],
            show_progress="hidden",
        )
        # 改「包含子資料夾」會改變「這批有幾個檔」,摘要要跟著動
        recursive.input(
            _src_summary, inputs=[src_path, recursive], outputs=[src_summary],
            show_progress="hidden",
        )
        # 選檔三鈕:Windows 原生對話框,只取路徑、不上傳(檔案不複製、
        # 不進 gradio 快取;理由見 srcfile 模組 docstring)。程式化填值不
        # 觸發 .input,故三者都自己把「開始」與摘要一起帶回(共用 _picked)
        src_outputs = [src_path, run_btn, src_summary]
        pick_btn.click(
            _pick_file, inputs=[src_path, recursive, source_mode],
            outputs=src_outputs, show_progress="hidden",
        )
        pick_dir_btn.click(
            _pick_dir, inputs=[src_path, recursive], outputs=src_outputs,
            show_progress="hidden",
        )
        clear_btn.click(
            _clear_src, inputs=[recursive], outputs=src_outputs,
            show_progress="hidden",
        )

        # ---- 現場收音接線(2026-07-21;伺服器端錄音,session 死了照錄)----
        # 來源切換與情境記憶都掛 .input:只在「使用者」操作時觸發,
        # 程式化更新(_restore_recording 等)不誤觸
        # 順序即 _switch_source 的回傳順序
        source_switch_outputs = [
            source_mode, src_path, src_hint, *src_btns, src_summary,
            scenario, rec_status, rec_start_btn, rec_stop_btn, run_btn, stop_btn,
            # 尾端三個是「用不到就別擺出來」的參數(講者人數、模型只在
            # 重設講者藏;包含子資料夾只有轉錄音檔用得到)。它們同時屬於
            # _param_updates 的鎖定那批,但兩邊各送各的 key(只帶
            # interactive 的更新不會洗掉 visible,反之亦然,實測過)
            speakers, model, recursive,
        ]
        source_mode.input(
            _switch_source, inputs=[source_mode],
            outputs=source_switch_outputs,
            show_progress="hidden",
        )
        # 命名進行中收起「開始下一份工作」那整組(設計稿 A 案)。
        # **掛在 paths_state 的 .change 上**:所有會進入或離開命名的路徑都會
        # 動到它——轉檔完成、錄音收尾、開頁還原、套用名字、跳過命名、
        # 按開始轉下一檔。掛在那六處的 .then 上要記得六次,漏一處就是
        # 「命名畫面收起來了、收工後卻沒還原」那種只在某條路上出現的怪事
        paths_state.change(
            _naming_focus, inputs=[source_mode, paths_state],
            outputs=source_switch_outputs,
            show_progress="hidden",
        )
        # 情境切換:把說明小字換成這個情境的那一段(radio 自己就是 outputs;
        # 掛 .input 所以只有使用者操作才觸發,不會繞回來)。
        # 2026-08-09 起不再記住選擇,見 _scenario_info
        scenario.input(
            _scenario_info, inputs=[scenario], outputs=[scenario],
            show_progress="hidden",
        )
        # 開始錄音:js 先舉守門旗+鎖鈕(點下當下生效,堵連點空窗)→
        # 整頁復位(講者框全隱藏=_present_result 的 skip 前置條件)→ 啟動。
        # 守門旗在錄音期間常駐:錄音中誤關分頁雖不會中斷錄音(伺服器端),
        # 但重開頁面才接得回,先攔一下讓使用者知道有事在跑
        rec_start_btn.click(None, js="""
        () => {
          window.__msBusy = true;
          const b = document.getElementById('rec-start-btn');
          if (b) b.disabled = true;
        }
        """)
        rec_ui = [rec_start_btn, rec_stop_btn, source_mode, scenario, rec_status,
                  rec_timer, *adv_params]
        rec_start_evt = rec_start_btn.click(
            _reset_for_new_recording,
            outputs=[*page_outputs, src_path, run_btn, stop_btn],
            show_progress="hidden",
        ).then(
            _start_recording, inputs=[scenario, model, cores],
            outputs=rec_ui, show_progress="hidden",
        )
        # 失敗(無麥克風/無播放裝置,gr.Error 已顯示):錄音鈕復原+放旗
        rec_start_evt.failure(_rec_start_failed, outputs=rec_ui, show_progress="hidden")
        rec_start_evt.failure(None, js=ui_style.RUN_DONE_JS)
        # 計時 tick:狀態列+即時預覽。show_progress 必須藏——tick 每秒跑,
        # 預設的進度遮罩會讓預覽區一直閃
        rec_timer.tick(
            _rec_tick, outputs=[rec_status, preview], show_progress="hidden",
        )
        # 停止錄音三步:js 即時鎖鈕 → 鎖介面+停計時+亮「停止」→ 收尾
        # (殘段轉錄+講者分析+輸出,進度畫在預覽區)→ 復位。
        # 收尾必須 .then/.failure 成對掛(gradio 6.20 地雷:.then 只在成功
        # 後觸發,見上方轉檔鏈註解),否則一次報錯錄音鈕就永遠鎖死
        rec_stop_btn.click(None, js="""
        () => {
          const b = document.getElementById('rec-stop-btn');
          if (b) b.disabled = true;
        }
        """)
        rec_finish_evt = rec_stop_btn.click(
            _lock_for_rec_finish,
            outputs=[rec_start_btn, rec_stop_btn, stop_btn, rec_timer,
                     rec_status, source_mode],
            show_progress="hidden",
        ).then(
            _finish_recording, inputs=[speakers, cores],
            outputs=page_outputs,
            show_progress_on=preview,
        )
        for chain in (rec_finish_evt.then, rec_finish_evt.failure):
            chain(
                _after_rec_finish,
                outputs=[rec_start_btn, rec_stop_btn, stop_btn,
                         source_mode, scenario, rec_status, *adv_params],
                show_progress="hidden",
            )
            chain(None, js=ui_style.RUN_DONE_JS)
        # 開頁接回進行中的錄音:伺服器端錄音與 session 脫鉤,睡眠/斷線/
        # 誤關分頁後重開頁面要回到「錄音中」畫面,不是假裝沒事的檔案模式
        # 尾端多一個 run_timer:收尾中接回來時要由轉檔秒針畫進度
        # (錄音自己的 rec_timer 在按下停止那一刻就關了)
        demo.load(
            _restore_recording,
            outputs=[source_mode, src_path, src_hint, *src_btns, src_summary,
                     scenario, rec_status,
                     rec_start_btn, rec_stop_btn, rec_timer, run_btn, stop_btn,
                     *adv_params, run_timer],
            show_progress="hidden",
        )
        # 開頁接回進行中的**轉檔**(使用者選定 2026-08-08):與錄音同一個
        # 道理——轉檔在伺服器端跑,分頁被節流/按了重新連線/誤關分頁之後,
        # 畫面要回到「轉檔中」而不是假裝沒事的初始狀態。少了這一條,使用者
        # 在畫面上看不到檔名、進度,**也按不到停止**,想中止只能關黑視窗
        # (= 幾十分鐘到幾小時的轉檔全丟)。
        # 順序契約:與 `_transcribe_ui_updates` 的回傳一一對應(長度
        # `_RUN_UI_LEN`);秒針夾在 page_outputs 與控件之間
        # 尾端四個是錄音那一側:秒針同時服務「檔案轉檔」與「錄音收尾」,
        # 收工時要收拾的鈕不一樣(見 _recording_end_updates)
        run_restore_ui = [source_mode, src_path, *src_btns,
                          run_btn, stop_btn, *adv_params,
                          rec_status, rec_start_btn, rec_stop_btn, scenario]
        demo.load(
            _restore_transcribing,
            outputs=[*page_outputs, run_timer, *run_restore_ui],
            show_progress="hidden",
        )
        # 轉檔秒針:畫進度,並在轉完時自己收尾(接命名畫面+解鎖)——
        # reload 之後沒有 `_after_run` 可以依靠,見 `_run_tick` 的 docstring。
        # show_progress 必須藏(同 rec_timer):每秒跑一次,預設的進度遮罩
        # 會讓預覽區一直閃
        run_timer.tick(
            _run_tick,
            inputs=[source_mode],   # 收工時要依模式決定收拾哪一組鈕
            outputs=[*page_outputs, run_timer, *run_restore_ui],
            show_progress="hidden",
        )
        # 套用即收尾:(1) 寫檔+畫面復位,唯下載區換成改好名字的檔案——
        # js 要從它取網址;之後 (2) 自動下載 js 與 (3) 清空下載區是
        # apply_evt 的兩個「並行」.then(接線在下方)。下載已由瀏覽器
        # 接手,清掉元件不影響進行中的下載
        apply_evt = apply_btn.click(
            _apply_names,
            inputs=[paths_state, vp_state, audit_state, *name_inputs, unknown_input],
            outputs=[*page_outputs, src_path, run_btn, stop_btn, speakers],
        )
        # 試聽:每顆鈕綁定自己的講者標籤(partial);再按同一顆=停止,
        # 按另一位直接切換。播放自然結束由 Audio.stop 把字樣復原
        aud_outputs = [audition_player, playing_state, *audition_btns, unknown_aud_btn]
        for aud_key, aud_btn in [*enumerate(audition_btns), (UNKNOWN_SPEAKER, unknown_aud_btn)]:
            aud_btn.click(
                functools.partial(_audition, aud_key),
                inputs=[clips_state, playing_state],
                outputs=aud_outputs,
                show_progress="hidden",
            )
        audition_player.stop(
            _audition_ended,
            outputs=aud_outputs,
            show_progress="hidden",
        )
        # 核對:每顆鈕綁自己的講者標籤(partial,同試聽);開面板時預覽讓位,
        # 「完成核對」再切回來
        for audit_key, audit_btn in [
            *enumerate(audit_btns), (UNKNOWN_SPEAKER, unknown_audit_btn),
        ]:
            audit_btn.click(
                functools.partial(_audit_open, audit_key),
                # 命名欄一起送進來:改掛的名單要把「本場已經填好的名字」排到
                # 前面(見 _reassign_choices),而那是畫面上的即時值
                inputs=[audit_state, *name_inputs, unknown_input],
                outputs=[audit_state, audit_player, audit_table, audit_pick,
                         audit_name, audit_panel, preview],
            )
            # 捲回頁面最上面(講者多時這顆鈕在螢幕下方,而面板長在右欄上方)。
            # ⚠️ 另外掛一顆、不與上面共用:js= 的回傳值會被當成 fn 的 inputs,
            # 併進去會把 audit_state 洗掉(見 ui_style.AUDIT_SCROLL_TOP_JS)
            audit_btn.click(None, js=ui_style.AUDIT_SCROLL_TOP_JS)
        # 點表格任一列 → 單獨重聽那一段(使用者 2026-08-13 補的需求:整批
        # 用來掃、單段用來在標名字之前再確認一次)
        # ⚠️ **queue=False 是效能關鍵**(2026-08-13 使用者回報「勾選改掛很慢」):
        # Dataframe 的 select 事件在**每一格**都會觸發,包含勾選框;預設走
        # gradio 的事件佇列,於是每點一下都要排隊等一次伺服器往返——即使
        # handler 立刻回 skip 也一樣慢。不進佇列就沒有這個等待
        audit_table.select(
            _audit_play_row, inputs=[audit_state],
            outputs=[audit_player, audit_state],
            show_progress="hidden", queue=False,
        )
        # 播完自然結束:伺服器端讓「再點一次 = 停」歸零,前端把 ■ 換回 ▶。
        # ⚠️ **那顆 ■ 只能在這裡換回來**(2026-08-14 使用者回報「播完沒有還原」,
        # 第二次才修對):第一版是在 head 裡收 <audio> 的 ended/pause/emptied,
        # 而 Playwright 實測的事實是**真正在播的那顆 <audio> 不在文件樹上**
        # (wavesurfer 自己 createElement 一顆來播),document 上的監聽連捕獲
        # 階段都收不到任何一個事件。走元件自己的 stop 才收得到
        # ⚠️ outputs 要含 audit_player:播完把 value 清成 None,同一列再按
        # 才會出聲(見 _audit_row_ended 的 ⚠️;同 _audition_ended)
        audit_player.stop(
            _audit_row_ended, inputs=[audit_state],
            outputs=[audit_player, audit_state],
            show_progress="hidden", queue=False,
        )
        # ⚠️ **另外掛一顆、不與上面那顆共用**:gradio 的 `js=` 是「先跑前端、
        # 回傳值當成 fn 的 inputs」,掛在上面那顆的話這支回 undefined 會把
        # audit_state 整個洗掉(rows 沒了 = 之後點哪一列都不會出聲)
        audit_player.stop(None, js=ui_style.AUDIT_PLAY_ENDED_JS)
        audit_apply_btn.click(
            _audit_apply,
            inputs=[paths_state, audit_state, audit_pick, audit_name, preview],
            outputs=[preview, audit_pick, audit_state],
        )
        audit_close_btn.click(
            _audit_close, outputs=[audit_panel, audit_player, preview],
            show_progress="hidden",
        )
        # 命名草稿隨打隨存(.input 只在「使用者」輸入時觸發;_run 預填、
        # 套用/換檔的程式化清空都不會誤存):睡眠/斷線後填到一半的名字
        # 也能還原
        for name_box_input in [*name_inputs, unknown_input]:
            name_box_input.input(
                _save_draft_names,
                inputs=[*name_inputs, unknown_input],
                show_progress="hidden",
            )
        # 開頁自動還原未完成的命名(睡眠/斷線/關瀏覽器/重開程式後,
        # 重新整理頁面即可接續;沒有落地資料時 _restore_pending 整組 skip)
        demo.load(
            _restore_pending, outputs=page_outputs, show_progress="hidden",
        )
        # 「填好名字的那一列收起線索」:純前端,開頁掛一次就自己維持
        # (裡面裝 MutationObserver 與 input/change 監聽,見 CLUE_COLLAPSE_JS
        # ——為什麼不在伺服器端做,那段註解有寫)
        demo.load(None, js=ui_style.CLUE_COLLAPSE_JS)
        # 開頁時把名單表格與聲紋那三個元件**重新讀一次檔**(2026-08-15 code
        # review 抓到)。⚠️ 它們的值是 `build_ui` **建構當下**的快照,而
        # build_ui 一個行程只跑一次——工具開著的期間用記事本或 git 改過
        # `data\attendees.txt`(那本來就是預期用法,見 orphan_names),畫面
        # 連重新整理都還是舊的;接著改別人那一列按儲存,送回伺服器的是那份
        # 舊快照,於是外面的改動被整批倒回去,連聲紋都會被「偵測到改名」
        # 一起改回舊名字。走 reload_* 兩支與「重新載入」鈕完全同一條路
        demo.load(
            data_tabs.reload_attendees,
            outputs=[att_table, att_preview_note, att_sync_check,
                     att_rename_note, att_rename_btn, att_rename_skip_btn,
                     att_rename_state],
            show_progress="hidden",
        )
        demo.load(
            data_tabs.reload_voiceprints,
            outputs=[vp_pick, vp_rename_pick, vp_info],
            show_progress="hidden",
        )
        # (2)(3) 必須是「並行」的兩個 .then,不能寫成 js→清空的直鏈:
        # 接在 js-only 步(fn=None+js)「之後」的環節一律不觸發——js 當
        # 鏈頭或鏈中皆然,死鏈無聲無息(js 本身有跑,極易誤判有接上;
        # gradio 6.20 地雷,Playwright 最小重現實測 2026-07-25),js 步
        # 只能當「鏈尾」或「並行事件」——原本鏈在 js 後的清空其實從沒
        # 跑過。並行無競態:js 在 (1) 完成當下於瀏覽器端讀值執行,
        # 清空要再一趟伺服器往返才落地(Playwright 實測)
        apply_evt.then(None, inputs=[downloads], js=ui_style.APPLY_DOWNLOAD_JS)
        apply_evt.then(lambda: None, outputs=[downloads], show_progress="hidden")
        # 「不命名,清空畫面」:放棄命名的整頁復位+收工(尾端四值與套用
        # /停止同一組,見 _end_of_job_updates)
        discard_btn.click(
            _discard_naming,
            inputs=[paths_state],  # 只為擋過期點擊,見 _stale_click_guard
            outputs=[*page_outputs, src_path, run_btn, stop_btn, speakers],
            show_progress="hidden",
        )
        # 名單表格一改完就預告「按下儲存會發生什麼」(設計稿方案 D,使用者
        # 選定 2026-08-09)。⚠️ 掛 `.input` 是刻意的:它只在**使用者**編輯
        # 時觸發,程式化更新(重新載入、聲紋區改名寫回表格)不會誤觸發而
        # 冒出一則在講別次編輯的預告(同 vp_rename_pick 那條)
        att_table.input(
            data_tabs.preview_rename, inputs=[att_table],
            outputs=[att_preview_note, att_sync_check], show_progress="hidden",
        )
        # 儲存:純改名隨勾選一次做完,**撞名(合併兩個人)仍然停下來問**
        # ——偵測判準再保守也可能撞上「刪一個、加一個」,而合併錯了在逐字稿
        # 裡只看得出「少了一個人」。中間欄那三個一律跟著更新(見 save_attendees)
        att_save_btn.click(
            data_tabs.save_attendees, inputs=[att_table, att_sync_check],
            outputs=[att_status, att_preview_note, att_sync_check,
                     att_rename_note, att_rename_btn, att_rename_skip_btn,
                     att_rename_state, vp_pick, vp_rename_pick, vp_info],
        # 合併追問比預告長,實測確認鈕底會超出視窗 8px:只在真的看不到時
        # 捲最少的距離(見 ROSTER_CONFIRM_SCROLL_JS)。js-only 只能當鏈尾
        ).then(None, js=ui_style.ROSTER_CONFIRM_SCROLL_JS)
        # 合併的兩條出路,收的東西一模一樣(訊息 + 追問三件 + 待確認 + 摘要);
        # 「暫不修改」不動聲紋庫,但名單那一半已經改掉了——它留下的是一個
        # 對不上的狀態,所以摘要那句失聯提醒會**立刻**亮起來
        merge_ask_outputs = [att_status, att_rename_note, att_rename_btn,
                             att_rename_skip_btn, att_rename_state]
        att_rename_btn.click(
            data_tabs.apply_rename, inputs=[att_rename_state],
            outputs=[*merge_ask_outputs, vp_pick, vp_rename_pick, vp_info],
        )
        att_rename_skip_btn.click(
            data_tabs.dismiss_rename, inputs=[att_rename_state],
            outputs=[*merge_ask_outputs, vp_info],
        )
        # 重新載入要把預告/追問一起收掉:留著的是在講一次**已經不存在**的
        # 編輯,而使用者接著按儲存,結果會跟預告完全對不上
        att_reload_btn.click(
            data_tabs.reload_attendees,
            outputs=[att_table, att_preview_note, att_sync_check,
                     att_rename_note, att_rename_btn, att_rename_skip_btn,
                     att_rename_state],
        )
        # 聲紋區的改名:單純改名直接做,撞名(=合併兩個人)先停下來問
        # 尾端第二個是「改成」那格:改完要清空,留著上一次的字,下次改別人
        # 時很容易直接按下去(使用者 2026-08-08 回報)
        # 尾端的 vp_info:這裡正是修好「只在聲紋庫、不在名單上」的地方
        # (見 data_tabs.orphan_names),修好了摘要那句提醒就該當場消失
        rename_outputs = [vp_rename_note, vp_rename_confirm_btn, vp_rename_state,
                          vp_pick, vp_rename_pick, vp_rename_to, att_table,
                          vp_info]
        # 選了人就把名字帶進「改成」:多數改名只是補幾個字(加部門、修錯字),
        # 從零打起反而麻煩(使用者指定 2026-08-08)。掛 .input 只在使用者真的
        # 挑人時觸發——程式化更新(改完清空選單)不會誤把空值蓋掉他打的字
        vp_rename_pick.input(
            data_tabs.vp_rename_prefill, inputs=[vp_rename_pick],
            outputs=[vp_rename_to], show_progress="hidden",
        )
        vp_rename_btn.click(
            data_tabs.vp_rename, inputs=[vp_rename_pick, vp_rename_to],
            outputs=rename_outputs,
        )
        vp_rename_confirm_btn.click(
            data_tabs.vp_rename_confirm, inputs=[vp_rename_state],
            outputs=rename_outputs,
        )
        # ⚠️ 兩個「聲紋名字」下拉都要更新(見 data_tabs._name_dropdowns):
        # 少掛一個,使用者就會在另一個選單裡看到已經不存在的名字
        vp_del_btn.click(
            data_tabs.delete_voiceprints, inputs=[vp_pick],
            outputs=[vp_pick, vp_rename_pick, vp_info],
        )
        # 清除全部是破壞性操作:瀏覽器原生確認框攔在事件送出前(使用者指定)。
        # ⚠️ **取消不可讓事件「失敗」**:原本的 js 是 `throw new Error(...)`,
        # 伺服器端確實連呼叫都不會發生(安全),但 gradio 6.20 把 js 的例外
        # 當成事件失敗,在**每一個 output** 上留一個紅色「錯誤」——三個 output
        # 就是三個(使用者 2026-08-09 截圖回報)。而那三個字對非技術同仁的
        # 意思是「我剛剛把東西弄壞了」,實際上什麼事都沒發生。
        # 改法:**js 的回傳陣列會成為 fn 的 inputs**(Playwright 最小重現實測),
        # 所以拿一個隱藏 Textbox 當旗標,把 confirm 的結果送到伺服器端判斷。
        # ⚠️ 這與底下那條「試過失敗」的 gr.State **不是同一件事**:State 的輸入
        # 由伺服器端解析,js 改寫對它無效;Textbox 的值來自前端,可以。
        # ⚠️ 代價是**取消時 clear_voiceprints 會被呼叫**,所以判準必須是正面
        # 表列(等於 CLEAR_CONFIRMED 才清),而旗標的建構值是空字串——js 沒跑到、
        # 送錯、元件沒渲染,通通落在「不清除」那一邊。理由詳見該函式的 docstring。
        # js 與 Python 對的是同一個常數,不要在這裡寫死字串。
        vp_clear_btn.click(
            data_tabs.clear_voiceprints,
            inputs=[vp_clear_flag],
            outputs=[vp_pick, vp_rename_pick, vp_info],
            js='() => [confirm("確定要清除全部聲紋?清除後所有人都需重新累積'
               '才能自動辨識,此操作無法復原。") ? '
               f'"{data_tabs.CLEAR_CONFIRMED}" : ""]',
        )
        vp_reload_btn.click(
            data_tabs.reload_voiceprints,
            outputs=[vp_pick, vp_rename_pick, vp_info],
        )
        # 健檢:掃描是純矩陣運算(138 個樣本實測 <0.1 秒),不需要進度回報。
        # 刪除**不加確認框**(與「清除全部」不同):勾選本身已是逐項的明確
        # 動作,而且每一項的說明都寫了「勾選 = 會發生什麼」;真刪錯了代價
        # 也只是那個人下次要重新命名一次,不像「清除全部」那樣不可回復
        vp_health_btn.click(
            data_tabs.vp_health_report,
            outputs=[vp_health_out, vp_health_pick, vp_health_apply_btn],
        )
        vp_health_apply_btn.click(
            data_tabs.vp_health_apply,
            inputs=[vp_health_pick],
            outputs=[vp_health_out, vp_health_pick, vp_health_apply_btn,
                     vp_pick, vp_rename_pick, vp_info],
        )
        hw_save_btn.click(data_tabs.save_hotwords, inputs=[hw_box], outputs=[hw_status])
        hw_reload_btn.click(data_tabs.reload_hotwords, outputs=[hw_box, hw_status])
        rp_save_btn.click(data_tabs.save_replace, inputs=[rp_box], outputs=[rp_status])
        rp_reload_btn.click(data_tabs.reload_replace, outputs=[rp_box, rp_status])

        # ---- 文件轉 Markdown 分頁的接線 ----
        # 選檔/選資料夾/清空/手動輸入,四條路都更新「已選 N 個檔案」摘要。
        # 摘要純粹是即時回饋,**不控制「開始轉檔」的亮暗**(那顆一律可按,
        # 見 doctab.preview_summary)——所以貼上路徑時就算前端沒觸發 input
        # 事件、摘要沒更新,按下去照樣會轉。「包含子資料夾」也要重算摘要:
        # 它會改變資料夾展開的結果,勾掉之後可能一個檔都不剩
        # 四條路都經過 app 這一層的薄包裝(_doc_summary):摘要為空時要把
        # 元件藏起來,而 doctab 刻意不 import gradio、回的是純字串
        doc_files_btn.click(
            functools.partial(_doc_picked, doctab.pick_files),
            inputs=[doc_src, doc_recursive],
            outputs=[doc_src, doc_summary], show_progress="hidden",
        )
        doc_folder_btn.click(
            functools.partial(_doc_picked, doctab.pick_folder),
            inputs=[doc_src, doc_recursive],
            outputs=[doc_src, doc_summary], show_progress="hidden",
        )
        doc_clear_btn.click(
            _doc_clear, outputs=[doc_src, doc_summary],
            show_progress="hidden",
        )
        for doc_trigger in (doc_src.input, doc_recursive.change):
            doc_trigger(
                _doc_preview, inputs=[doc_src, doc_recursive],
                outputs=[doc_summary], show_progress="hidden",
            )
        # 連點空窗:js-only 先在前端 disable(同 run_btn 的作法)。
        # ⚠️ **這四顆的 elem_id 只有這裡在用**(沒有樣式、測試也不全靠它),
        # 看起來像死鉤子——2026-08-19 清死 id 時差點連 `doc-clear-btn` 一起刪,
        # 查下去才發現是**漏列**:四顆都在 `doc_lock_outputs` 裡、伺服器端本來
        # 就會鎖,這段補的是「鎖落地之前」那個空窗,漏掉哪一顆那一顆在空窗期
        # 就還按得下去。而「清空」清掉的正是 doc_src——`_doc_convert` 要讀的
        # 那個路徑欄(聲音分頁的清單一直都含 clear-src-btn,兩邊本來就該一樣)
        doc_run_btn.click(None, js="""
        () => {
          window.__msBusy = true;
          for (const id of ['doc-run-btn', 'doc-pick-files-btn', 'doc-pick-folder-btn',
                            'doc-clear-btn']) {
            const b = document.getElementById(id);
            if (b) b.disabled = true;
          }
        }
        """)
        doc_lock_outputs = [
            doc_src, doc_files_btn, doc_folder_btn, doc_clear_btn,
            doc_recursive, doc_ocr, doc_mail_att,
            doc_run_btn, doc_stop_btn, doc_open_btn,
        ]
        # 兩步鏈,理由同逐字稿那條:鎖介面必須與轉檔在不同批訊息落地,
        # 而且鏈頭不得拋例外(.failure 掛在第二步上)
        doc_evt = doc_run_btn.click(
            _doc_start, outputs=doc_lock_outputs, show_progress="hidden",
        ).then(
            _doc_convert,
            inputs=[doc_src, doc_recursive, doc_ocr, doc_mail_att],
            outputs=[doc_result, doc_dirs_state, doc_open_btn],
            show_progress_on=doc_result,
        )
        # .then/.failure 成對(gradio 6.20 的 .then 實測只在成功後觸發,
        # 缺 .failure 則一次報錯就把介面鎖死、__msBusy 永遠放不下)
        for doc_chain in (doc_evt.then, doc_evt.failure):
            doc_chain(
                _doc_after,
                outputs=doc_lock_outputs[:-1],  # 「開啟輸出資料夾」由轉檔那步決定
                show_progress="hidden",
            )
            doc_chain(None, js=ui_style.DOC_RUN_DONE_JS)
        doc_stop_btn.click(None, js=ui_style.DOC_STOP_FEEDBACK_JS)
        doc_stop_btn.click(
            _doc_stop, outputs=[doc_stop_btn], show_progress="hidden",
        )
        doc_open_btn.click(
            _doc_open_dirs, inputs=[doc_dirs_state], show_progress="hidden",
        )
    return demo


class _AsyncioNoiseFilter(logging.Filter):
    """濾掉 Windows Proactor 事件迴圈的 ConnectionResetError(10054)噪音。

    瀏覽器那端斷開連線(睡眠、分頁被回收、關分頁)後,asyncio 清理死
    socket 時會把例行收尾印成整串英文 traceback——無任何後果,但黑視窗
    使用者看得到、兩度被誤認為系統故障(專案原則:黑視窗不印嚇人訊息)。
    只濾這一種例外;asyncio 其他錯誤照常顯示。"""

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        return not isinstance(exc, ConnectionResetError)


# 單例:main() 可重入(測試會重複呼叫),addFilter 對同一物件冪等
_ASYNCIO_NOISE_FILTER = _AsyncioNoiseFilter()


# ---- gradio 供應快取的接管(隱私:機敏副本不得殘留)----
# gradio 把「供應給瀏覽器」的檔案(下載區的逐字稿、試聽片段)先複製進
# GRADIO_TEMP_DIR,預設位置 %TEMP%\gradio 整機共用且 gradio 從不清理
# ——會議內容是機敏資料,副本不能永遠堆在暫存區。
# 對策:套件 __init__ 把 GRADIO_TEMP_DIR 指到本行程專屬的
# meeting-scribe-serve-<pid>(前綴=pipeline.TMP_PREFIX),執行期間持開
# 存活鎖(另一實例啟動清掃時刪不掉鎖就整包跳過),正常關閉由 main 的
# finally 刪除、硬退出的殘留由下次啟動 cleanup_stale_temp 掃掉——協定
# 與 pipeline 暫存/試聽片段完全相同。執行中絕不動快取:下載/試聽可能
# 正在供應中。(此接管原為「上傳副本」而設;2026-07-26 改路徑輸入後
# 來源檔不再被複製,但供應副本仍在,接管照樣必要。)
_serve_cache_lock = None  # 持開中的鎖檔 handle(行程結束自動釋放)


def _hold_serve_cache() -> None:
    """建立供應快取目錄並持開存活鎖。使用者自行指定 GRADIO_TEMP_DIR
    (非 meeting-scribe 前綴)時不接管——那是他的目錄,不鎖也不刪;
    建立失敗只記 log,gradio 屆時自建目錄、行為退回預設(不影響轉檔)。"""
    global _serve_cache_lock
    cache = Path(os.environ.get("GRADIO_TEMP_DIR", ""))
    if not cache.name.startswith(pipeline.TMP_PREFIX):
        return
    try:
        cache.mkdir(parents=True, exist_ok=True)
        _serve_cache_lock = (cache / pipeline.TMP_LOCK).open("wb")
    except OSError:
        logger.exception("供應快取目錄接管失敗,退回 gradio 預設清理行為")


def _drop_serve_cache() -> None:
    """正常關閉時清掉供應快取(含鎖檔);只清自己持鎖的目錄。"""
    global _serve_cache_lock
    if _serve_cache_lock is None:
        return
    _serve_cache_lock.close()
    _serve_cache_lock = None
    shutil.rmtree(os.environ["GRADIO_TEMP_DIR"], ignore_errors=True)


def main() -> None:
    """程式進入點:清孤兒暫存 → 找空埠 → launch(僅綁 127.0.0.1)。"""
    # 自家模組的 INFO(VAD 塊統計等診斷)與 WARNING(引擎降級)須顯示於
    # 黑視窗;只調自家 logger 等級,不動 root(避免第三方函式庫 INFO 洗版)
    logging.basicConfig()
    logging.getLogger("meeting_scribe").setLevel(logging.INFO)
    logging.getLogger("asyncio").addFilter(_ASYNCIO_NOISE_FILTER)
    # 紀錄檔:黑視窗關掉就沒了,而收音掉幀這類問題只有事後才分析得動。
    # 必須在 basicConfig 之後(見 filelog.tee_console 的 docstring)
    filelog.start()
    # 出廠預設補檔:交付出去的那一份不含 data\(否則解壓覆蓋更新會蓋掉
    # 使用者累積的聲紋與名單),四個資料檔改由這裡從 data-default\ 補上。
    # 必須在 filelog.start() 之後——第一次啟動補了什麼要留在紀錄檔裡
    models.seed_missing()
    cleanup_stale_temp()  # 清掃先前硬退出(關窗/當機)殘留的暫存目錄
    _hold_serve_cache()   # 供應快取上鎖(清掃之後:自己的新目錄不能被掃)
    port = find_free_port()
    try:
        _launch(port)
    finally:
        # Ctrl+C/正常關閉:供應副本當場清掉(機敏資料不過夜);
        # 直接關黑視窗等硬退出走不到這裡,由下次啟動的清掃兜底
        _drop_serve_cache()


def _launch(port: int) -> None:
    # gradio 那句「* To create a public link, set `share=True` in `launch()`.」
    # 由 quiet 關掉(使用者 2026-08-06 指定):對外分享從沒測過,而且隱私
    # 規格只綁 127.0.0.1——在黑視窗教人怎麼把服務開到外網,是把一條沒驗證
    # 過的路擺在使用者面前。日後真要支援對外再說。
    #
    # quiet 是唯一的官方開關,連帶關掉的只有「* Running on local URL」那行
    # (其餘 quiet 分支都是 colab / Node SSR proxy 的事,本機兩者皆不成立
    # ——gradio 6.20 的 ssr_mode 預設 False,實測黑視窗只有這兩行)。那行
    # 改由我們自己印:網址是瀏覽器沒自動開起來時唯一的退路,而黑視窗不得
    # 有裸英文(spec §8)
    print(f"網頁介面網址:http://127.0.0.1:{port}(瀏覽器會自動開啟)")
    try:
        _launch_ui(port)
    except Exception as exc:  # noqa: BLE001
        # gradio 的 launch() 收尾有一道自我健檢(用 httpx 打自己的
        # `gradio_api/startup-events`)。它失敗時原訊息是一長串英文 traceback,
        # 收在「Check your network or proxy settings」——而「啟動.bat」接著印的是
        # 「請先執行安裝.bat」,對一個安裝明明成功的人**純屬誤導**(2026-08-12
        # 同仁實際遇到:公司代理攔截 127.0.0.1、回 403)。
        # 登錄檔設的代理已由 `_bypass_proxy_for_localhost` 擋掉,這裡兜底剩下的
        # ——PAC 自動組態、資安軟體攔截 localhost、防火牆擋本機埠——那幾種我們
        # 無從自動處理,只能讓他看懂發生什麼事、知道找誰(spec §8)。
        # 原例外照樣往上冒:traceback 有回報問題要用的資訊,而它會進紀錄檔
        if "startup-events" in str(exc):
            print(
                "\n[錯誤] 網頁介面啟動失敗:這台電腦有東西把「連自己」擋掉了"
                "(常見是公司的代理伺服器 Proxy,或資安軟體)。\n"
                "        工具只在你自己的電腦上跑、不會連到外面,"
                "但那些設定連 127.0.0.1 也一起攔了。\n"
                "        請洽貴單位 IT,把 localhost 與 127.0.0.1 "
                "加進代理的例外清單(或防火牆白名單)。"
            )
        raise


def _launch_ui(port: int) -> None:
    """gradio 的 launch 參數。與 `_launch` 分開只為讓錯誤翻譯那層讀得清楚。"""
    build_ui().launch(
        server_name="127.0.0.1",  # 隱私規格:僅本機可連,絕不可改 0.0.0.0
        server_port=port,
        inbrowser=True,
        quiet=True,  # 見上:關掉 gradio 的 share 廣告與英文網址行
        theme=ui_style.apple_theme(),  # Apple 風視覺層(gradio 6:theme/css 由 launch 收)
        css=ui_style.APPLE_CSS,
        # 分頁圖示。檔案缺失就交回 gradio 的預設圖案(給 None):圖示是外觀,
        # 不得讓程式起不來——同 ui_style._header_icon_css 的降級原則
        favicon_path=str(_FAVICON) if _FAVICON.exists() else None,
        # head 四段:開頁過場(蓋掉 gradio 還沒畫出畫面的那 2~3 秒白畫面,
        # 使用者 2026-08-08 回報;**必須排第一**——它要蓋的正是後面幾段
        # 執行前的那段空窗)+深淺色的還原/儲存與切換鈕的點擊行為
        # +轉檔中關頁確認+斷線提示橫幅(睡眠喚醒後 gradio session 已死、
        # 按鈕全無反應,前端毫無提示;使用者實際踩到,見 RECONNECT_HEAD)
        # 第五段是分頁圖示的 <link>:gradio 只註冊 /favicon.ico 路由、
        # **不插標籤**,而那條隱式路徑會被 Chrome 的 favicon 快取擋死
        # (使用者 2026-08-20 實際踩到)。理由與雜湊的用途見 favicon_head
        head=(
            ui_style.SPLASH_HEAD
            + ui_style.THEME_PERSIST_HEAD
            + ui_style.UNLOAD_GUARD_HEAD
            + ui_style.RECONNECT_HEAD
            + ui_style.AUDIT_PLAYING_HEAD
            + ui_style.favicon_head()
        ),
        # 試聽片段的落地副本在 %LOCALAPPDATA%\meeting-scribe\pending——不在
        # gradio 檔案白名單(僅 cwd/系統暫存/gradio 快取)內,不明列的話
        # 按試聽在 postprocess 就炸 InvalidPathError、前端毫無反應
        # (使用者回報:試聽鈕沒作用+黑視窗整串 traceback 洗版)。
        # 只開放 pending 目錄本身;服務僅綁 127.0.0.1,無對外風險
        allowed_paths=[str(pending.pending_dir())],
    )


if __name__ == "__main__":
    main()
