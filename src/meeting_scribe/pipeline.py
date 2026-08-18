"""單檔處理管線總管:音檔/影片 → 繁體中文逐字稿(md)。

檔案轉檔的入口是 run_pipeline(),流程:
  audio.to_wav16k(ffmpeg 轉 16k 單聲道)
  → _transcribe_and_diarize(轉錄+講者分析;有 GPU 兩者平行、純 CPU 依序)
  → merge.assign_speakers(依時間重疊把講者掛到每句文字)
  → finalize(繁化 → 跳針標記 → 認人線索 → 輸出檔/預覽)

finalize() 是與現場收音共用的「後段」:錄音路徑的轉錄/講者分析/回音
去重在 live.py 自理,只把「已掛講者的段落」交進來走相同的輸出流程。
進度回報依 _STAGE_SPANS 把各階段折算成 0~1 的整體進度;暫存目錄用
「前綴+存活鎖」機制(TMP_PREFIX/TMP_LOCK,app 試聽片段與 live 亦沿用),
硬退出的孤兒由下次啟動的 cleanup_stale_temp 清掃。
"""
import contextvars
import logging
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path

from meeting_scribe import (
    audio,
    cancel,
    convert,
    diarize,
    export,
    loopdetect,
    merge,
    power,
    punctuate,
    runstate,
    transcribe,
)

logger = logging.getLogger(__name__)

StageFn = Callable[[str, float], None]

# 暫存目錄命名與存活鎖:正常結束(含例外)由 TemporaryDirectory 自清;
# 硬退出(關黑視窗/當機/斷電)來不及清的孤兒由 cleanup_stale_temp 在下次
# 啟動時掃掉。鎖檔在轉檔期間被本行程持開,Windows 不允許刪除持開中的
# 檔案——保護「另一個執行中實例」的暫存不被本實例啟動清掃誤殺
# (find_free_port 讓多實例可同時執行)。
# 公開常數:app(試聽片段目錄)與 live(增量轉錄暫存)沿用同一套
# 「前綴+存活鎖」機制,才會被同一個 cleanup_stale_temp 接手清孤兒。
TMP_PREFIX = "meeting-scribe-"
TMP_LOCK = ".lock"


def mmss(t: float) -> str:
    """秒數 → 「H:MM:SS」/「MM:SS」顯示(app 錄音狀態列與跳針標記共用)。"""
    h, rem = divmod(int(t), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# 跳針標記的開頭(hints 挑摘錄時據此排除:標記不是「發言」,且其文字比
# 一般口語句長,曾在 60 秒上限內反而霸佔摘錄——使用者回報);
# 常數本體在 loopdetect(export 也要用,放這裡會循環 import)
_DEGENERATE_PREFIX = loopdetect.MARKER_PREFIX


def _degenerate_note(seg) -> str:
    """跳針段的繁中標記,取代垃圾文字(誠實交代+指路原音)。"""
    phrase = loopdetect.repeated_phrase(seg.text)
    quoted = f"重複輸出「{phrase}」" if phrase else "輸出重複雜訊"
    return (
        f"{_DEGENERATE_PREFIX}:模型在 {mmss(seg.start)}~{mmss(seg.end)} {quoted},"
        "內容不可信、已略去;需要這段內容請對照原始錄音)"
    )


def cleanup_stale_temp() -> None:
    """清掃先前硬退出殘留的暫存目錄(app 啟動時呼叫一次)。

    鎖檔刪得掉 = 沒有實例持開(孤兒)→ 整目錄刪除;刪不掉(PermissionError)
    = 另一實例轉檔中 → 跳過。任何刪除失敗都安靜略過,清掃絕不能擋啟動。"""
    for d in Path(tempfile.gettempdir()).glob(TMP_PREFIX + "*"):
        try:
            (d / TMP_LOCK).unlink(missing_ok=True)
        except OSError:
            continue
        shutil.rmtree(d, ignore_errors=True)

# 各階段在整體進度中的區間(起點, 寬度)。
# 轉錄與講者分析合為一個階段:轉錄走 GPU 時與講者分析(CPU)平行,
# 總時間趨近 max(轉錄, 講者分析);純 CPU 時依序(見 _transcribe_and_diarize)。
_STAGE_SPANS = {
    "轉檔": (0.0, 0.05),
    "轉錄與講者分析": (0.05, 0.90),
    "輸出": (0.95, 0.05),
}

# 平行階段內兩引擎的進度權重:轉錄是主要耗時者(標準機粗估 3:1)
_TRANSCRIBE_WEIGHT = 0.75
_DIARIZE_WEIGHT = 0.25

# 命名摘錄/試聽的「最長一句」只在此時長內挑:超長的「句」若非轉錄跳針
# (實際案例:42 分鐘會議冒出一句 418 秒的重複迴圈),就是多人快速交談
# 被轉錄引擎併成一句——無論哪種,拿來認人都又長又混。完全沒有合格句的
# 講者才退回整體最長句(試聽剪輯另有 30 秒上限,見 audio._CLIP_MAX_SECONDS)
_HINT_MAX_SEC = 60.0


@dataclass(frozen=True)
class PipelineResult:
    outputs: list[Path]
    device: str
    preview: str
    # 講者命名/聲紋辨識用:講者數(不含未知)與每位講者的原始聲紋質心
    # ({講者標籤 0-based: 質心向量 np.ndarray});無講者分析時為 0 / 空
    speakers: int = 0
    voiceprints: dict | None = None
    # 命名欄位的認人線索({講者標籤 0-based: (發言段數, 最長一句, 該句起秒, 該句迄秒)});
    # 句子取繁化後、未補標點版本(標點只補在 md 的合併區塊上,線索用不到)。
    # 起訖秒數供 app 從「原始音檔」剪出同一句的試聽片段(暫存 wav 在管線
    # 結束即清除,只能回頭剪原始檔;兩者時間軸一致,ffmpeg 轉檔不平移時間)。
    # 未知段落(UNKNOWN_SPEAKER=-1)也列一鍵:供「未知」命名框顯示與提示
    # (該框只改逐字稿文字、絕不登記聲紋,見 app._apply_names)
    speaker_hints: dict | None = None
    # 現場收音(分軌)限定:{講者標籤: 試聽該剪哪個音檔}——線上會議的
    # 現場講者要剪麥克風軌、遠端講者剪系統軌,剪錯軌會聽到回音版或無聲。
    # 檔案轉檔為 None(app 沿用原始檔)
    speaker_sources: dict | None = None
    # 每位講者的分群品質(types.SpeakerQuality):段數、時長、群內一致性。
    # 寫進逐字稿檔尾的診斷區塊(export._speaker_diagnostics)
    quality: list | None = None
    # 逐字稿上的每一輪發言(types.SpeechBlock),供「🔍 核對」把某一位的
    # 發言接成一個音檔一次聽完(audit.py)。⚠️ **單位是區塊不是講者分離的
    # 區段**:md 以區塊為單位跑標點,區塊內的句界在成品裡已經不存在
    blocks: list | None = None


def _speaker_hints(spoken) -> dict[int, tuple[int, str, float, float]]:
    """命名欄位的認人線索:{講者標籤: (發言段數, 最長一句, 該句起秒, 迄秒)}。

    讓使用者在命名時不必翻預覽找「講者 N 說了什麼」。未知(-1)也列,
    UI 據此決定要不要多顯示「未知」命名框(只改文字、不登記聲紋)。
    「最長」以文字長度比,但僅在時長 ≤ _HINT_MAX_SEC 的句子中挑
    (理由見常數註解);跳針標記不是「發言」,不得成為認人摘錄(其文字
    比口語句長、標記段常在 60 秒上限內,會反過來霸佔摘錄——使用者
    回報),只留作「該講者全部句子都被標記」時的墊底。"""
    counts: dict[int, int] = {}
    best_ok: dict[int, tuple[str, float, float]] = {}
    best_any: dict[int, tuple[str, float, float]] = {}
    best_marked: dict[int, tuple[str, float, float]] = {}
    for s in spoken:
        counts[s.speaker] = counts.get(s.speaker, 0) + 1
        if s.text.startswith(_DEGENERATE_PREFIX):
            best_marked.setdefault(s.speaker, (s.text, s.start, s.end))
            continue
        if len(s.text) > len(best_any.get(s.speaker, ("", 0.0, 0.0))[0]):
            best_any[s.speaker] = (s.text, s.start, s.end)
        if (
            s.end - s.start <= _HINT_MAX_SEC
            and len(s.text) > len(best_ok.get(s.speaker, ("", 0.0, 0.0))[0])
        ):
            best_ok[s.speaker] = (s.text, s.start, s.end)
    hints: dict[int, tuple[int, str, float, float]] = {}
    for spk, cnt in counts.items():
        text, bs, be = (
            best_ok.get(spk) or best_any.get(spk) or best_marked[spk]
        )
        hints[spk] = (cnt, text, bs, be)
    return hints


def _run_transcribe(wav: Path, model_key: str, progress):
    """轉錄:優先走子行程,起不來就退回主行程。

    **為什麼要子行程**(使用者 2026-08-07 選定):轉檔要降到 below-normal
    好讓「電腦還能用」,但 gradio 的網頁伺服器跟轉錄在同一個行程裡,於是
    介面也一起被降——滿載時停止鈕 15~35 秒才有反應,使用者按 F5 等不到
    就以為程式死了。搬進子行程後只降子行程,父行程的介面隨時回應。

    **退回主行程是必要的安全網**:子行程起不來(環境怪、DLL 缺、被防毒
    擋掉)時,使用者要的是「轉檔跑得完」,不是「因為一個效能優化而失敗」。
    退回時介面會比較不順,所以留一行 log 交代——不能安靜地換行為。"""
    from meeting_scribe import transproc

    try:
        return transproc.get().transcribe(wav, model_key=model_key, progress=progress)
    except cancel.Cancelled:
        raise  # 停止鈕:原樣往上拋,不可被下面的兜底吞掉當成「子行程壞了」
    except Exception:
        logger.warning(
            "轉錄子行程不可用,改在主行程執行(轉檔期間網頁介面會比較不順)",
            exc_info=True,
        )
        transproc.shutdown()
        return transcribe.transcribe(wav, model_key=model_key, progress=progress)


def _run_diarize(wav: Path, num_speakers: int, progress, features_out=None):
    """講者分析:同上,優先走子行程。

    ⚠️ 這一支**也**要搬出去,只搬轉錄是不夠的:sherpa 的 pybind11 綁定在
    運算期間不釋放 GIL(probe_gil 實測一整塊阻塞 6,834ms,轉錄才 1,450ms)
    ——留在主行程的話,gradio 每塊還是會被卡住近 7 秒。

    子行程**每次轉檔開一支、用完關掉**(不像轉錄那支是常駐單例):它在
    diarproc 那邊本來就綁定「一場」的狀態,而且模型建構才 ~0.45 秒,
    重開的代價遠小於狀態殘留的風險。"""
    from meeting_scribe import diarproc

    proc = None
    try:
        proc = diarproc.DiarProcess(below_normal=True)
        proc.start()
        proc.on_progress = progress
        return proc.diarize(
            wav, num_speakers=num_speakers, features_out=features_out)
    except cancel.Cancelled:
        raise
    except Exception:
        logger.warning(
            "講者分析子行程不可用,改在主行程執行(轉檔期間網頁介面會比較不順)",
            exc_info=True,
        )
        return diarize.diarize(
            wav, num_speakers=num_speakers, progress=progress,
            features_out=features_out)
    finally:
        if proc is not None:
            proc.close()


def _transcribe_and_diarize(
    wav: Path,
    model_key: str,
    num_speakers: int,
    report: Callable[[str, float], None],
    features_out: Path | None = None,
):
    """執行轉錄與講者分析,合成單一進度回報。

    排程策略:轉錄走 GPU(CUDA / Intel GPU)時與講者分析(CPU)平行——
    異質資源,兩引擎皆為釋放 GIL 的原生程式碼,執行緒即可真平行;
    純 CPU 機上改為依序——兩引擎各開多執行緒同搶一顆 CPU 只會過度訂閱,
    依序還能避免兩組模型同時常駐,壓低基準機(8GB)的峰值記憶體。
    (GPU 偵測後轉錄仍可能中途降級 CPU;此為罕見故障路徑,不特別排程。)

    平行時任一引擎失敗,等另一個跑完才浮出例外(執行緒仍在讀暫存 wav,
    不能先清理)。"""
    fracs = {"transcribe": 0.0, "diarize": 0.0}
    lock = threading.Lock()

    def sub_progress(key: str) -> Callable[[float], None]:
        def cb(f: float) -> None:
            with lock:
                fracs[key] = max(fracs[key], min(f, 1.0))
                combined = (
                    _TRANSCRIBE_WEIGHT * fracs["transcribe"]
                    + _DIARIZE_WEIGHT * fracs["diarize"]
                )
                report("轉錄與講者分析", combined)
            # 落地成伺服器端狀態(鎖外:runstate 自己有鎖,而且心跳 log 會
            # 寫檔——不該把兩條引擎的回報卡在這裡等 I/O)。**兩段分開記**:
            # 畫面要能講出「轉錄 100%、講者分析 40%」,合成一個數字之後就
            # 再也還原不回來,而那正是使用者在轉錄跑完後盯著沉默黑視窗的
            # 那一到兩小時裡唯一想知道的事
            runstate.update(key, fracs[key])
        return cb

    if not transcribe.gpu_available():
        segments, device = _run_transcribe(
            wav, model_key, sub_progress("transcribe"))
        turns, voiceprints, quality = _run_diarize(
            wav, num_speakers, sub_progress("diarize"), features_out)
        return segments, device, turns, voiceprints, quality

    # gr.Progress 每次呼叫都經 contextvars 解析回報管道,而 worker 執行緒
    # 預設不繼承 context——不帶 context 副本的話,佔 90% 權重的主階段進度
    # 會全程靜默丟失(進度條卡 5% 直到跳 95%)。各 future 各自 copy:
    # 同一個 Context 不可被兩條執行緒同時進入。
    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_t = ex.submit(
            contextvars.copy_context().run, _run_transcribe, wav,
            model_key, sub_progress("transcribe"),
        )
        fut_d = ex.submit(
            contextvars.copy_context().run, _run_diarize, wav,
            num_speakers, sub_progress("diarize"), features_out,
        )
        segments, device = fut_t.result()
        turns, voiceprints, quality = fut_d.result()
    return segments, device, turns, voiceprints, quality


@dataclass(frozen=True)
class RenderedTranscript:
    """渲染好的逐字稿:md 內容 + 掛好講者且已繁化/已標記跳針的段落。

    degenerate 是「連重轉都救不回、已被標記取代」的段落——批次路徑
    (docaudio)要據此在 md 的 frontmatter 留失真標記,而 finalize 要據此
    寫紀錄檔。兩邊都需要,所以它是回傳值的一部分而不是內部細節。"""
    md_text: str
    spoken: list
    degenerate: list
    # 簡轉繁真的改到了字。批次路徑要把它填進 md 的 frontmatter——那份 md
    # 沒有「原始文字」可以回頭比對(來源是語音),沒有這個欄位就無從解釋
    # 「為什麼逐字稿寫的字跟我印象中不一樣」
    traditionalised: bool = False
    # 本場最該人工核對的講者標籤(0-based,見 export.check_first)。md 檔尾
    # 的診斷區塊已經寫得明白,這個欄位是給**機器**看的:批次路徑要據此在
    # frontmatter 留標記(固定英文 token),RAG 端才篩得出「這份稿子有幾個
    # 講者標籤要人工確認」
    check_speakers: list = field(default_factory=list)


def render_transcript(spoken, stem: str, quality=None) -> RenderedTranscript:
    """已掛講者的段落 → 繁化 → 跳針標記 → 標點 → md 文字(**不落檔**)。

    finalize(單檔/收音)與 docaudio(批次、走 doc2md 那條路)共用的核心。
    抽出來是因為批次路徑要的正是「md 內容」而不是「輸出檔+命名線索」:
    落檔的位置與時機在那邊由 docpipe 的批次規劃決定(原地輸出、同名跳過),
    讓它先寫一次再搬只是把 30 分鐘的成果多冒一次險。

    quality(types.SpeakerQuality 清單)有給就在 md 檔尾附講者診斷區塊。
    **兩條路都要給**:批次那份更需要——沒有人會逐份翻,而「四個人被併成
    一個名字」在成品裡看起來只是「少了一個人」。"""
    converted = [
        replace(s, text=convert.to_taiwan_traditional(s.text))
        for s in spoken
    ]
    traditionalised = any(a.text != b.text for a, b in zip(spoken, converted))
    spoken = converted
    # 轉錄跳針段的最後防線:引擎內的重轉(transcribe/_ov)救不回來的,
    # 在輸出前以繁中標記取代垃圾文字——壞內容不得安靜地混在逐字稿裡
    # (實際案例:418 秒的「包括資料,」×百次被當成一句,使用者試聽
    # 才發現;標記含時間範圍與重複短語,方便對照原音)
    degenerate = [s for s in spoken if loopdetect.is_degenerate(s.text)]
    spoken = [
        replace(s, text=_degenerate_note(s))
        if loopdetect.is_degenerate(s.text) else s
        for s in spoken
    ]
    # 標點整份只跑一次:md 檔與預覽是同一份 to_markdown 輸出,渲染一次
    # 兩處共用——分開各叫一次的話,標點模型對整份逐字稿等於跑兩遍
    md_text = export.to_markdown(
        spoken, stem, punctuate=punctuate.add_punctuation, quality=quality,
    )
    return RenderedTranscript(
        md_text=md_text, spoken=spoken, degenerate=degenerate,
        traditionalised=traditionalised,
        check_speakers=[q.speaker for q in export.check_first(quality or [])],
    )


def run_pipeline(
    src: Path,
    out_dir: Path,
    model_key: str = "fast",
    num_speakers: int = 0,
    on_stage: StageFn | None = None,
) -> PipelineResult:
    """檔案轉檔入口:把單一來源檔從頭跑到輸出檔(流程見模組 docstring)。

    輸出檔名取來源檔名(曾有 out_stem 參數供批次中同名來源加序號,
    UI 改一次一檔後移除,2026-07-26)。
    輸出固定 md、語言固定中文、講者分析一律執行——三項都已無參數可調
    (使用者 2026-07-26 逐項指定固定化;各自的權威記載在 export 模組
    docstring / transcribe.LANGUAGE / CLAUDE.md)。"""
    def report(stage: str, inner_frac: float = 0.0) -> None:
        if on_stage:
            start, width = _STAGE_SPANS.get(stage, (1.0, 0.0))
            on_stage(stage, min(start + width * inner_frac, 1.0))

    # 全程維持系統/螢幕喚醒:鎖定螢幕會觸發省電、壓抑 CPU 時脈拖慢轉檔。
    #
    # ⚠️ **這裡刻意不再降主行程的優先權**(2026-08-07 改,使用者選定):
    # 「轉換時電腦還能用」(2026-08-04 指定)現在由**子行程**達成——轉錄與
    # 講者分析都跑在 below-normal 的子行程裡(見 _run_transcribe/_run_diarize)。
    # 主行程同時是 gradio 的網頁伺服器,降它等於連介面一起降:滿載時停止鈕
    # 15~35 秒才有反應(暖機期 95 秒),使用者按 F5 等不到就以為程式死了,
    # 而它一直好好地在跑(2026-08-07 實跡,連關兩次重來)。
    with power.keep_awake():
        with tempfile.TemporaryDirectory(prefix=TMP_PREFIX) as tmp:
            # 持開存活鎖直到本檔處理結束(見 TMP_LOCK 註解)
            with (Path(tmp) / TMP_LOCK).open("wb"):
                report("轉檔")
                # 標點模型在此先下載載入:模型問題要在最初幾秒浮出,不能等
                # 30 分鐘轉錄完才在輸出階段炸掉、整場白跑
                punctuate.ensure_ready()
                wav = audio.to_wav16k(src, Path(tmp))
                # ffmpeg/模型下載不可中斷,停止要求在此(進引擎前)兌現;
                # 引擎內另有逐塊/逐窗檢查點。「輸出」階段不再攔——都跑完了,
                # 把結果寫出比丟棄划算
                cancel.check()

                report("轉錄與講者分析")
                # 分群特徵檔**跟著 md 走**(這條路是 output/)。relabel 是從
                # md 出發找同層同名的東西,放到別處等於白存
                segments, device, turns, voiceprints, quality = (
                    _transcribe_and_diarize(
                        wav, model_key, num_speakers, report,
                        features_out=diarize.features_path(out_dir / src.name),
                    )
                )

        spoken = merge.assign_speakers(segments, turns)
        return finalize(
            spoken, turns, voiceprints, out_dir, src.stem,
            device=device, on_stage=on_stage, quality=quality,
        )


def transcribe_to_markdown(
    src: Path,
    model_key: str = "fast",
    num_speakers: int = 0,
    on_stage: StageFn | None = None,
) -> RenderedTranscript:
    """批次入口:單一來源檔 → 逐字稿 md 內容(**不落檔、不做命名線索**)。

    與 run_pipeline 是同一條管線的兩個收尾:那邊落檔到 output/ 並回傳命名
    所需的一切(聲紋質心、摘錄、試聽起訖秒),這邊只回 md 內容,落檔交給
    docpipe 的批次規劃(原地輸出、同名跳過)。

    **講者分析照做、命名不做**(使用者 2026-08-06 指定):多檔時逐字稿仍
    標「講者 1/2/3」——誰在講是逐字稿的主要價值,少了它就只剩一團連續
    文字;但命名是一檔一檔當場做的事(講者編號每檔獨立分群),批次沒有
    「當場」可言,硬做只會把這一檔的名字寫進別檔。單檔模式因此維持原樣。

    num_speakers 由呼叫端逐檔決定(GUI 把欄位值套用到整批、CLI 預設 0 =
    自動偵測);其餘固定項(md 輸出、中文、一律做講者分析)同 run_pipeline。"""
    def report(stage: str, inner_frac: float = 0.0) -> None:
        if on_stage:
            start, width = _STAGE_SPANS.get(stage, (1.0, 0.0))
            on_stage(stage, min(start + width * inner_frac, 1.0))

    # 防睡眠與「不降主行程」的理由同 run_pipeline(那裡有完整說明);
    # 批次跑的時間只會更長,「轉檔時電腦還能用」與「介面要回應得動」
    # 在這裡都更重要,而兩者現在都由 below-normal 的子行程達成
    with power.keep_awake():
        with tempfile.TemporaryDirectory(prefix=TMP_PREFIX) as tmp:
            with (Path(tmp) / TMP_LOCK).open("wb"):
                report("轉檔")
                punctuate.ensure_ready()
                wav = audio.to_wav16k(src, Path(tmp))
                cancel.check()

                report("轉錄與講者分析")
                # 批次的 md 落在原檔旁邊,特徵檔跟著它。⚠️ **這條路更需要**:
                # 這個分頁沒有「講者人數」可填、一律自動偵測,而使用者不會
                # 逐份回頭看——分壞了,事後改人數是唯一的救
                segments, _device, turns, _vp, quality = _transcribe_and_diarize(
                    wav, model_key, num_speakers, report,
                    features_out=diarize.features_path(src),
                )

        report("輸出")
        spoken = merge.assign_speakers(segments, turns)
        rendered = render_transcript(spoken, src.stem, quality)
    # 同 finalize:跳針是整條管線唯一會真的弄丟內容的地方,批次更沒有人
    # 會逐份翻 md,紀錄檔那一行是唯一會被看見的痕跡(md 內另有標記)
    if rendered.degenerate:
        logger.warning(
            "轉錄跳針:%s 有 %d 段(共 %.0f 秒)連重轉都救不回,已在逐字稿標記",
            src.name, len(rendered.degenerate),
            sum(s.end - s.start for s in rendered.degenerate),
        )
    return rendered


def _blocks_with_cohesion(spoken, turns) -> list:
    """每一輪發言 + 它的聲紋一致性(給「🔍 核對」列出來)。

    一輪發言由好幾個講者分離的區段組成,所以取**時間加權平均**:一段
    10 秒的 0.7 與一段 0.5 秒的 0.2,平均起來不該各算一半。

    ⚠️ **算不出來就留 0**(沒有重疊的區段、或那條路根本沒跑分群):
    介面上顯示成空白,而不是假裝有一個數字——那正是本專案對「不下假判定」
    的一貫要求。"""
    out = []
    for b in export.speech_blocks(spoken):
        num = den = 0.0
        for t in turns:
            overlap = min(b.end, t.end) - max(b.start, t.start)
            if overlap > 0 and t.conf:
                num += t.conf * overlap
                den += overlap
        out.append(replace(b, cohesion=(num / den) if den else 0.0))
    return out


def finalize(
    spoken,
    turns,
    voiceprints,
    out_dir: Path,
    stem: str,
    device: str = "",
    on_stage: StageFn | None = None,
    speaker_sources: dict | None = None,
    quality: list | None = None,
) -> PipelineResult:
    """已掛講者的段落 → 繁化 → 跳針標記 → 認人線索 → 輸出檔/預覽。

    run_pipeline 與現場收音收尾(live.run_live_finish)共用的後段;
    spoken 的文字為引擎原始輸出(未繁化)。md 一律經標點模型——標點模型
    的存在理由就是「Whisper 的中文輸出幾乎無標點」,而語言已固定中文。
    渲染本身在 render_transcript(與批次路徑共用),這裡多做的是「落檔+
    命名線索」——那正是單檔模式才有的東西。"""
    def report(stage: str, inner_frac: float = 0.0) -> None:
        if on_stage:
            start, width = _STAGE_SPANS.get(stage, (1.0, 0.0))
            on_stage(stage, min(start + width * inner_frac, 1.0))

    report("輸出")
    t0 = time.monotonic()
    t_md = time.monotonic()
    rendered = render_transcript(spoken, stem, quality)
    spoken, degenerate, md_text = (
        rendered.spoken, rendered.degenerate, rendered.md_text,
    )
    # **這件事一定要進紀錄檔**:標記在 md 裡看得到,但沒人會為了確認
    # 「這次有沒有掉東西」去逐份翻逐字稿;而它就是這條管線唯一會真的
    # 弄丟內容的地方(2026-08-04 一場 64 分鐘的會議掉了 3 段共 92 秒)
    if degenerate:
        logger.warning(
            "轉錄跳針:%d 段(共 %.0f 秒)連重轉都救不回,已在逐字稿標記",
            len(degenerate), sum(s.end - s.start for s in degenerate),
        )
    # 命名欄位的認人線索(含該句起訖秒,供 app 剪同一句的試聽片段)
    hints = _speaker_hints(spoken)
    outputs = [export.write_md(md_text, out_dir, stem)]
    # 標點模型跑整份要數十秒,而在此之前使用者已經等了整段收尾。沒有這行
    # 的話「到底是還在跑還是卡住了」在紀錄檔裡完全看不出來
    logger.info(
        "輸出完成:%s(%d 段;標點與渲染 %.1f 秒、本階段共 %.1f 秒)",
        outputs[0].name, len(spoken),
        time.monotonic() - t_md, time.monotonic() - t0,
    )

    # 這一行同樣是「不主動講就沒有人會發現」那一類:分群把幾個人塌成一群
    # 時,逐字稿看起來只是少了一個人,而黑視窗是使用者唯一會瞄一眼的地方
    check = export.check_first(quality or [])
    if check:
        logger.info(
            "講者辨識:%s 的群內一致性明顯低於本場其他人,建議對照原音核對"
            "(逐字稿檔尾另有完整診斷)",
            "、".join(f"講者 {q.speaker + 1}" for q in check),
        )

    if on_stage:
        on_stage("完成", 1.0)
    # 講者數 = 出現過的非未知講者標籤數(重排後為 0..K-1 連號)
    n_speakers = len({t.speaker for t in turns if t.speaker >= 0})
    return PipelineResult(
        outputs=outputs, device=device, preview=md_text,
        speakers=n_speakers, voiceprints=voiceprints, speaker_hints=hints,
        speaker_sources=speaker_sources, quality=quality,
        blocks=_blocks_with_cohesion(spoken, turns),
    )
