r"""講者命名與核對的純邏輯 —— **不依賴任何 UI 框架**。

從 `app.py` 搬出來(2026-08-29,原生介面遷移的階段 2)。⚠️ **判準是「換一套 UI
這些函式一個字都不用改」**:名字怎麼套回逐字稿、核對片段怎麼挑、重設講者的
一致性怎麼算——那些是領域邏輯,跟畫面上長什麼樣無關。

⚠️ **搬出來的當下有一條紀律:這裡不准 import 任何 UI 模組。** 一旦有人為了方便
在這裡回一個 `gr.update()`,這個模組就白抽了,而那件事不會有任何錯誤訊息。
錯誤一律 `UserFacingError`(app 層原樣顯示,見 `docs/dev/conventions.md`)。

⚠️ **`_cut_speaker_clips` 與 `_analyse_for_relabel` 刻意還留在 `app.py`**:前者被
測試 monkeypatch 七次,連同呼叫它的後者一起搬,那些假貨會**靜默失效**——搬它們
要同一批把那七處 patch 改指到這裡,那是下一輪的事。
"""
import logging
import shutil
import tempfile
from pathlib import Path

import numpy as np

from meeting_scribe import (
    attendees, audio, cancel, diarize, export, pending, pipeline, relabel,
)
from meeting_scribe import audit as audit_mod
from meeting_scribe import voiceprints as voiceprints_store
from meeting_scribe.errors import UserFacingError
from meeting_scribe.types import MAX_SPEAKERS, UNKNOWN_SPEAKER, SpeechBlock

logger = logging.getLogger(__name__)

# 核對表裡代表「這一列可以播」的那一欄。⚠️ 跟著 `_audit_table` 從 app.py
# 搬過來:它只有這一支在用,留在那邊等於把資料格式的常數放在 UI 層。
_ROW_PLAY = "▶"


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


# 聲紋分不開時,最多讓幾列亮起「🔍 核對」(使用者 2026-08-15 選定)。
# ⚠️ **一定要有上限**:大型會議裡「認不出來」是常態,不設限的話 8/14 那場
# 11 位裡有 9 位、8/12 那場 19 位裡有 13 位都會亮鈕,而全部都亮就等於全部
# 都沒標。取「差距最小的前三位」= 最難分辨的那幾位,與 export.check_first
# 的「一致性最低前三名」同一套哲學:工具只負責排序,不下判定
_AUDIT_CLOSE_CALLS = 3


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


def _audit_blocks(audit, spk) -> list:
    """audit_state(純 dict,State 只放得下可序列化的東西)→ 該講者的區塊。"""
    blocks = [
        SpeechBlock(speaker=int(b["speaker"]), start=float(b["start"]),
                    end=float(b["end"]), text=str(b.get("text", "")),
                    cohesion=float(b.get("cohesion") or 0.0))
        for b in (audit or {}).get("blocks", [])
    ]
    return audit_mod.blocks_of(blocks, spk)


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


def _audit_flags(quality) -> set:
    """哪幾列要亮「🔍 核對」= 檔尾診斷點名「建議優先核對」的那幾位。"""
    return {q.speaker for q in export.check_first(quality or [])}


def _rival_pool(rivals) -> list[str]:
    """整場的聲紋候選聯集(保序去重)= 「聲紋庫覺得今天可能在場的人」。

    給核對面板的改掛選單排序用(見 _reassign_choices):外面都還沒填名字
    的時候,這是唯一能把 59 人的名單收斂一點的依據。"""
    pool: list[str] = []
    for spk in sorted(rivals or {}):
        pool.extend(rivals[spk])
    return list(dict.fromkeys(pool))
