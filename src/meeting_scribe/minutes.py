"""會議紀錄草稿:地端語言模型。Mac 每場選 Apple 裝置端模型(Foundation Models,macOS 26 以上、
Apple Intelligence 打開時)或 Qwen3.5-4B(MLX 跑在 GPU,16 GB 以上的 Apple 晶片 Mac,第一次用時
下載;2026-10-14 使用者選定,同手機版);Windows 用 OpenVINO 跑 Qwen3.5(第一次用時下載,
有內顯用 4B、只有 CPU 用 2B——2026-10-11 使用者選定)。兩條 Qwen 都在子行程裡跑(`minutes_ov`
+ `minutes_worker`)。數字見 `docs/spec/mac/05` §5.4b。

做法照搬手機版 scribe2md-ios 的 `AISummary.swift`(那邊 `docs/spec/08` §8.12 有三輪實測與定案),
**邏輯在這裡、Swift 小程式只負責問一次**(`packaging/mac/apple_asr.swift` 的 `llm-*` 子命令;
2026-10-11 使用者選定:核對原句、推算時間這類「改錯也不會報錯、只會悄悄變差」的東西要有測試守著)。

只產生「摘要 + 主要議題 + 待辦(附約略時間)」。決議、未決問題、負責人都不做:實測它做不來
(負責人幾乎都填成說話的人);開頭給它自由就會捏造日期、主持人、議題,所以最後一步只准寫兩段、
標題由程式加。⚠️ **草稿不寫進逐字稿 md**(使用者 2026-10-11:跟手機版一樣,只給自己看、
不送進知識庫)——小模型編的東西不能流進下游。

幾件實測出來、拿掉就會壞的事(數據見 `docs/spec/mac/05` §5.4b):

1. 窗只有 4096 token、中文一字約 1.5 token → 逐字稿切段(`CHUNK_CHARS`)寫重點再合併;
2. 它不照字數要求縮短 → 用「最多幾點、每點一句」限制,合併仍不收斂就截短(會掉內容,記一筆);
3. 待辦每條要附逐字稿原句,**程式對回逐字稿、對不上就丟**(`locate`)——擋編造的唯一一道;
4. 回覆會混進簡體字(Mac 實測「什么问题」)→ 出來的文字一律再轉成這一場的字形(繁中會議繁化、
   簡中會議簡化;手機版沒做,這裡補上)。

提示詞與草稿檔的字跟著**逐字稿的會議語言**(`draftvocab`,同 iOS `DraftVocab`)。
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from meeting_scribe import (cancel, convert, errors, export, models, namefix, paths, plat,
                            power, settings, srcfile)
from meeting_scribe.errors import UserFacingError
from meeting_scribe.i18n import t
from meeting_scribe import draftvocab, vocab
from meeting_scribe.plat import Outcome

logger = logging.getLogger(__name__)

ProgressFn = Callable[[float], None]

#: 每段幾個字問一次:Apple 在 Mac 上的窗 4096(手機版實測:1200 字時 50 段有 3 段爆窗,要多交待辦與原句之後改 900)
CHUNK_CHARS = 900
#: 窗 8192 以上(下載的模型、新版系統的 Apple)一段幾字,見 `_chunk_chars`
CHUNK_CHARS_WIDE = 1800
#: 合併時一批最多幾個字
MERGE_CHARS = 1100
#: 一次呼叫最多等這麼久;超過就當這一段失敗、跳過(手機實機一段卡住時整場停在同一個百分比)
CALL_TIMEOUT_SEC = 90.0
#: 按之前說大概要等多久:2026-10-28 段落協定 v2 之後在 Mac(M5、macOS 26,一段 900 字)重量:
#: 43 分鐘的會 86 秒、3 時 34 分的長會 507 秒(46 段、重問 7 次)≈ 每分鐘錄音 2.4 秒
SECONDS_PER_RECORDED_MINUTE = 2.4
#: 這麼短的會不提醒(幾十秒就好)
WAIT_HINT_MIN_SEC = 20 * 60

# --- Windows(OpenVINO)那條的數字 ------------------------------------------------
#: 一次呼叫最多等多久。⚠️ **不可以沿用 Apple 的 90 秒**:超時是「這一段悄悄跳過」,而
#: 純 CPU 跑 2B 一次中位 16 秒(Core Ultra 7 258V),一般公司電腦慢兩三倍、長的那幾次
#: 就會撞上 90——少掉的段落在草稿上看不出來。Mac 的 Qwen(MLX)也走這一個(一次約 5~10 秒,用不到)
OV_CALL_TIMEOUT_SEC = 300.0
#: Gemma 4 E4B 內顯每分鐘錄音要等幾秒(2026-10-29 W3 量:38 分鐘的會 201 秒、3 時 34 分的長會
#: 900 秒,平均約 4.7;放寬 1.5 倍同上)。比 Qwen 4B 慢約一倍
GEMMA_OV_SECONDS_PER_RECORDED_MINUTE = 7.0
#: 有內顯也要這麼多記憶體才用 4B:**內顯沒有自己的記憶體**,4B 跑起來佔 4.3 GB,
#: 8 GB 的筆電上等於跟 Windows 搶同一份(16 GB 的機器報出來約 15.7 GB,門檻放在 15)
OV_4B_MIN_RAM_MB = 15_000
#: 每分鐘錄音要等幾秒(Core Ultra 7 258V 上量的;一般公司電腦慢,各放寬 1.5 倍)。4B 內顯是
#: 2026-10-28 段落協定 v2、一段 1800 字之後重量的(38 分鐘的會 99 秒、3 時 34 分的長會 474 秒,
#: 取 2.6);⚠️ 2B 那兩格還是改版前(一段 900 字)43 分鐘那場的 66 秒與 328 秒,W5 重量
OV_SECONDS_PER_RECORDED_MINUTE = {("4b", "GPU"): 3.9, ("2b", "GPU"): 2.3, ("2b", "CPU"): 11.4}

# --- Mac 的 Qwen(MLX)那條的數字 ------------------------------------------------------
#: 記憶體多少以上才給 Qwen 4B(使用者 2026-10-14 選「16 GB 以上」)。跑起來 GPU 佔 3.2 GB,
#: 8 GB 的 Mac 上跟系統搶同一份;16 GB 的機器報出來約 16,384 MB,門檻同 `OV_4B_MIN_RAM_MB`
MLX_MIN_RAM_MB = 15_000
#: 每分鐘錄音要等幾秒:2026-10-28 段落協定 v2、一段 1800 字之後在 M5 重量,43 分鐘的會 62 秒、
#: 3 時 34 分的長會 369 秒 ≈ 1.7,而 M1~M4 的 GPU 慢,放寬 1.5 倍(只在 M5 量過)
MLX_SECONDS_PER_RECORDED_MINUTE = 2.6
#: Gemma 4 E4B(MLX)每分鐘錄音要等幾秒:2026-10-29 M3 在 M5 上量,43 分鐘的會 70 秒、3 時 34 分的
#: 長會 445 秒 ≈ 2.1(比 Qwen 慢約兩成),放寬 1.5 倍同上
GEMMA_MLX_SECONDS_PER_RECORDED_MINUTE = 3.1

#: 誰來回答(`engines()` 的鍵;Mac 的那兩個也是畫面上那一排的鍵與 `settings` 記的值)
ENGINE_APPLE = "apple"
ENGINE_QWEN = "qwen"
ENGINE_OV = "openvino"
#: Gemma 4 E4B(2026-10-28 使用者要「也放進 Mac 與 Windows」):Windows 是 OpenVINO、只跑在內顯,
#: Mac 是 MLX。**兩個平台同一個鍵**(`settings` 記的值);走哪一條看 `_backend()`
ENGINE_GEMMA = "gemma"

# 提示詞與草稿檔的字跟著**逐字稿的會議語言**(`draftvocab`;`generate` 開頭挑好放進 `_draft`)。
# 下面這幾個名字是繁中那份的別名,舊的呼叫端與測試照用
_ZH = draftvocab.ZH_TW
TODOS_HEADING = _ZH.todos_heading
HIGHLIGHTS_HEADING = _ZH.highlights_heading
#: 最多整理幾個重點(同手機版):一個一次呼叫,按了幾十下時不讓草稿多跑好幾分鐘
MAX_HIGHLIGHTS = 12
NO_TODOS = _ZH.no_todos
_MAP_INSTRUCTIONS = _ZH.map_instructions
_HIGHLIGHT_INSTRUCTIONS = _ZH.highlight_instructions
_MERGE_INSTRUCTIONS = _ZH.merge_instructions
_SUMMARY_INSTRUCTIONS = _ZH.summary_instructions

#: 這一場用哪一份(`generate` 開頭依逐字稿挑;`render` / `draft_notice` 也看它)
_draft: draftvocab.DraftVocab = _ZH
#: 這一場的專有名詞(名詞對照表換過之後逐字稿裡有的正確寫法):每次問都接在指示最後(`_call`;
#: 同手機版 `AISummary.glossary` 那個 TaskLocal)。`generate` 開頭算好
_glossary: list[str] = []
#: 這一場「與我相關」用的稱呼(`generate` 開頭讀好;見 `aliases`)
_aliases: list[str] = []
#: 這一場名詞對照換了幾處(記錄檔用;只記數字,不記名詞——含真名)
_replaced = 0


# --- 能不能用 -------------------------------------------------------------------

_engines: dict[str, Outcome] | None = None
#: Apple 裝置端模型的窗(token):小程式的 `llm-status` 回報;舊版小程式沒有這一欄就當 4096
#: (macOS 26 是 4096、iOS 27 是 8192——手機版 10-11 實測)。決定一段幾字,見 `_chunk_chars`
_apple_context = 4096
#: 這一場實際用了誰(`generate` 開頭記下;草稿檔的警語照它挑,見 `draft_notice`)
_used_engine: str | None = None


def _helper_argv(exe) -> list[str]:
    """叫小程式的指令開頭(測試換成「python 假小程式」用;同 `transcribe_apple._helper_argv`)。"""
    return [str(exe)]


def _backend() -> str | None:
    """哪個平台那一套:Mac 是 `apple`(Apple、Qwen、Gemma 三個模型)、Windows 是 `openvino`、其他沒有。

    ⚠️ **Mac 不會「自動」退回 Qwen**:Apple Intelligence 沒打開要回 `failed` 叫使用者去打開,
    悄悄改用另一顆模型等於把那句提示吞掉。用不用 Qwen 是使用者在畫面上自己選的
    (2026-10-14,`default_engine`)。"""
    return {"macos": "apple", "windows": "openvino"}.get(plat.name())


def engines() -> dict[str, Outcome]:
    """這台有哪幾個模型、各自能不能用(順序就是畫面上那一排的順序);結果留著(問 Apple
    那一個要起一個行程)。

    Apple:`unsupported` 是 macOS 太舊、機器不支援 Apple Intelligence;`failed` 是沒打開、
    模型還在下載、元件叫不起來——使用者自己解得了。⚠️ **打開 Apple Intelligence 要到 Mac
    前面的「系統設定」做**,從別台電腦連進來的人看不到那個畫面,所以句子裡要講明是哪裡。
    Qwen 見 `_qwen_capability`。Windows 一律 `ok`:模型第一次按下去時才下載,問這一句不碰
    任何原生庫。"""
    global _engines
    if _engines is None:
        backend = _backend()
        if backend == "apple":
            _engines = {ENGINE_APPLE: _probe(), ENGINE_QWEN: _qwen_capability(),
                        ENGINE_GEMMA: _gemma_mlx_capability()}
        elif backend == "openvino":
            _engines = {ENGINE_OV: Outcome("ok"), ENGINE_GEMMA: _gemma_capability()}
        else:
            _engines = {}
    return _engines


def capability() -> Outcome:
    """這台能不能產生會議紀錄草稿:任一個模型用得了就是 `ok`;都不行時,有一個是 `failed`
    (使用者解得了)就回它,否則 `unsupported`——那一格不出現。"""
    # ⚠️ Windows 一律 ok(Qwen 永遠用得了),**不問 `engines()`**:那一支要問 Gemma 用不用得了、
    # 要偵測內顯,而這一句開窗就會問(`test_Windows一律用得了_不碰原生庫`)
    if _backend() == "openvino":
        return Outcome("ok")
    outs = list(engines().values())
    if not outs:
        return Outcome("unsupported", t("這台電腦沒有可以用的地端語言模型"))
    if any(o.ok for o in outs):
        return Outcome("ok")
    return next((o for o in outs if o.state == "failed"), outs[0])


def expected_available() -> bool:
    """開窗當下、**不探測**地猜「地端 AI」那一格要不要出現:Windows 一律有(不必偵測就知道);
    Mac 照上次偵測的結果(`remember_available`);其他沒有。偵測完由畫面照真的結果更正。"""
    backend = _backend()
    if backend == "openvino":
        return True
    if backend == "apple":
        return bool(settings.get(settings.KEY_MINUTES_AVAILABLE, False))
    return False


def remember_available(ok: bool) -> None:
    """記下這次偵測的結果(Mac;沒變就不寫,免得每次開窗都寫一次設定檔)。"""
    if _backend() == "apple" and settings.get(settings.KEY_MINUTES_AVAILABLE) != ok:
        settings.set(settings.KEY_MINUTES_AVAILABLE, ok)


def choices() -> list[str]:
    """畫面上那一排有哪幾格:`unsupported` 的不列(`failed` 要列——按下去會講怎麼解)。
    少於兩格時整排收起來(使用者 2026-10-14 選)。"""
    return [k for k, o in engines().items() if o.state != "unsupported"]


def _preferred() -> str | None:
    """不探測任何東西的「想用誰」:記住的那個;沒記過時 Windows 是 Qwen(比較快、比較小)、Mac 是 Apple
    (不必下載、比較快;同手機版)。"""
    backend = _backend()
    if backend == "openvino":
        saved = settings.get(settings.KEY_MINUTES_ENGINE)
        return saved if saved in (ENGINE_OV, ENGINE_GEMMA) else ENGINE_OV
    if backend != "apple":
        return None
    saved = settings.get(settings.KEY_MINUTES_ENGINE)
    return saved if saved in (ENGINE_APPLE, ENGINE_QWEN, ENGINE_GEMMA) else ENGINE_APPLE


def default_engine() -> str | None:
    """這一場用誰:記住的那個列得出來就用它,否則用列得出來的第一個。"""
    opts = choices()
    want = _preferred()
    return want if want in opts else (opts[0] if opts else want)


def remember_engine(engine: str) -> None:
    """記住使用者選的(畫面上點了就記;下一場、下次開程式都沿用)。"""
    settings.set(settings.KEY_MINUTES_ENGINE, engine)


def clear_capability_cache() -> None:
    """重問能不能用(每一場按下去時;使用者可能剛去打開 Apple Intelligence)。
    上一場用了誰也一起清掉:那是 `render` 緊接著 `generate` 用的,不能串到下一場。"""
    global _engines, _used_engine
    _engines = None
    _used_engine = None


def qwen_available() -> bool:
    """這台列不列得出 Qwen 4B(「地端 AI 進階設定」要不要多一列模型管理)。⚠️ 不經 `engines()`:
    對話框開在主執行緒上,而那一支沒快取時要起 Apple 的小程式。"""
    return _backend() == "apple" and _qwen_capability().state != "unsupported"


def _gemma_capability() -> Outcome:
    """Windows 能不能跑 Gemma 4 E4B(OpenVINO)。只看機器,不起行程、不碰模型。

    ⚠️ **只有 CPU 的電腦不給**(2026-10-28 使用者定:W1 量到 CPU 每分鐘錄音約 17 秒,跟當初判
    「太慢,不用」的 Qwen 4B CPU 同級);「進階參數設定」選了「只用 CPU」也一樣。⚠️ **記憶體門檻
    同 Qwen 4B**(`OV_4B_MIN_RAM_MB`;同日使用者定,知道 Gemma 峰值 8~8.6 GB、內顯跟 Windows
    共用同一份)。兩種都是 `unsupported`:那一格不列。"""
    from meeting_scribe import transcribe

    if not transcribe._intel_gpu_available() or transcribe.cpu_only():
        return Outcome("unsupported", t("Gemma 4 需要 Intel 內顯"))
    if (plat.total_ram_mb() or 0) < OV_4B_MIN_RAM_MB:
        return Outcome("unsupported", t("Gemma 4 需要 16 GB 以上的記憶體"))
    return Outcome("ok")


def _qwen_capability() -> Outcome:
    """Mac 能不能跑 Qwen 4B(MLX)。"""
    return _mlx_capability("Qwen 4B")


def _gemma_mlx_capability() -> Outcome:
    """Mac 能不能跑 Gemma 4 E4B(MLX)。(Windows 那顆是 `_gemma_capability`)"""
    return _mlx_capability("Gemma 4")


def _mlx_capability(name: str) -> Outcome:
    """Mac 能不能跑這顆 MLX 模型(Qwen 4B、Gemma 4)。不起行程、不 import MLX,只看機器與套件在不在。

    ⚠️ 記憶體不夠、不是 Apple 晶片是 `unsupported`(那一格不列);套件不在是 `failed`
    (重新安裝就好)。⚠️ **Gemma 的記憶體門檻同 Qwen**(`MLX_MIN_RAM_MB`,16 GB;同 Windows 的
    Gemma,2026-10-29 M3 先這樣定、等使用者確認):Gemma 檔案 5.2 GB,比 Qwen 大七成。"""
    import importlib.util
    import platform

    if platform.machine() != "arm64":
        return Outcome("unsupported", t("{name} 需要 Apple 晶片的 Mac", name=name))
    ram = plat.total_ram_mb() or 0
    if ram < MLX_MIN_RAM_MB:
        return Outcome("unsupported", t("{name} 需要 16 GB 以上的記憶體", name=name))
    if importlib.util.find_spec("mlx_lm") is None:
        return Outcome("failed", t("{name} 需要的元件沒有安裝完整:{v}", name=name, v=plat.reinstall_hint()))
    return Outcome("ok")


def _probe() -> Outcome:
    found, exe = plat.apple_speech_helper()
    if not found.ok:
        return found if found.state == "unsupported" else Outcome("failed", found.reason)
    try:
        done = subprocess.run([*_helper_argv(exe), "llm-status"], capture_output=True, text=True,
                              encoding="utf-8", timeout=60,
                              **plat.spawn_kwargs(low_priority=False))
        reply = json.loads(done.stdout.strip().splitlines()[-1])
    except Exception as e:  # 起不來、逾時、吐的不是 JSON(舊版 App 的小程式沒有這個子命令)
        logger.warning("Apple 語言模型元件叫不起來:%s", e)
        return Outcome("failed", t("Apple 語言模型元件叫不起來,請更新到最新版或重新啟動程式再試"))
    state = reply.get("state")
    if state == "ok":
        global _apple_context
        ctx = reply.get("context")
        _apple_context = ctx if isinstance(ctx, int) and ctx > 0 else 4096
        return Outcome("ok")
    if state == "not_enabled":
        return Outcome("failed", t("在這台 Mac 的「系統設定 → Apple Intelligence 與 Siri」打開 "
                                 "Apple Intelligence,就能產生會議紀錄草稿"))
    if state == "not_ready":
        return Outcome("failed", t("Apple Intelligence 的模型還在下載,好了就能產生會議紀錄草稿"))
    return Outcome("unsupported", {
        "unsupported_os": t("會議紀錄草稿需要 macOS 26 以上"),
        "not_eligible": t("這台 Mac 不支援 Apple Intelligence"),
        "unsupported_locale": t("這台 Mac 的 Apple Intelligence 不支援繁體中文"),
    }.get(state, t("這台 Mac 沒有 Apple 的裝置端語言模型")))


# --- 逐字稿 → 一行一句 ------------------------------------------------------------

@dataclass
class TimedLine:
    """一行發言。`time` 是那一輪的開始時間;`start`/`end` 是這一行大約涵蓋的秒數
    (一輪按字數比例分給各行,下一輪開始就是這一輪結束)——用來推估待辦在一輪裡的
    位置:有的逐字稿一輪就是一大段、講一兩分鐘(手機實機:三筆待辦都標成 00:00:00)。"""
    time: str
    text: str
    start: float = 0.0
    end: float | None = None


def _is_transcript_heading(line: str) -> bool:
    return any(line.startswith(v.transcript_heading.rstrip(" —")) for v in vocab.ALL)


def timed_lines(md: str) -> list[TimedLine]:
    """逐字稿 md 的正文(`## 會議逐字稿` 到 `## 講者辨識診斷`)→「名字:這一輪說的話」。

    ⚠️ 講者行一律用 `export.SPEAKER_LINE_RE`(全 repo 只有一份),取時間用具名群組。
    暫停那行與跳針標記不是發言,不送給模型(白花 token,還可能被當成待辦的原句)。"""
    out: list[TimedLine] = []
    in_body, who, stamp = False, "", ""
    for raw in md.split("\n"):
        line = raw.strip()  # 連 \r 一起去掉:Windows 產生的 md 是 CRLF
        # ⚠️ 三種會議語言的標題都認(手機版與這邊都可能產出英文、簡中逐字稿)
        if _is_transcript_heading(line):
            in_body = True
            continue
        if export.starts_diagnostics(line):
            break
        if not in_body or not line:
            continue
        m = export.SPEAKER_LINE_RE.match(line)
        if m:
            who = m["name"]
            stamp = f"{int(m['h']):02d}:{m['m']}:{m['s']}"
            continue
        if export.is_pause_line(line) or vocab.is_marker(line):
            continue
        # 「> 〔重點〕= …」說明行與「> 備註:…」不是發言:備註另外走 `highlighted_turns`
        if line.startswith(">"):
            continue
        out.append(TimedLine(stamp, f"{who}:{line}" if who else line))
    _spread_times(out)
    return out


@dataclass(frozen=True)
class MarkedTurn:
    """標了〔重點〕的那一輪:時間、「名字:內容」、與會者打的備註。"""
    time: str
    text: str
    notes: tuple[str, ...] = ()


def highlighted_turns(md: str) -> list[MarkedTurn]:
    r"""逐字稿 md 裡標了〔重點〕的那幾輪(同手機版 `AISummary.highlightedTurns`)。

    同一位講者連續標著的幾行(收尾時被切開的同一輪;中間只隔著「未知」的插話也算)
    併成一輪,備註跟著那一輪;暫停一定斷開。"""
    rows: list[list] = []      # [名字, 時間, 標了沒, 內文, 備註, 前面有暫停]
    in_body, paused = False, False
    for raw in md.split("\n"):
        line = raw.strip()
        # ⚠️ 三種會議語言的標題都認(手機版與這邊都可能產出英文、簡中逐字稿)
        if _is_transcript_heading(line):
            in_body = True
            continue
        if export.starts_diagnostics(line):
            break
        if not in_body or not line:
            continue
        m = export.SPEAKER_LINE_RE.match(line)
        if m:
            on = any(vocab.is_highlight_mark(x) for x in (m["marks"] or "").split(export.MARK_SEP))
            rows.append([m["name"], f"{int(m['h']):02d}:{m['m']}:{m['s']}", on, [], [], paused])
            paused = False
        elif export.is_pause_line(line):
            paused = True
        elif not rows:
            continue
        elif vocab.is_note_line(line):
            if c := vocab.note_text(line).strip():
                rows[-1][4].append(c)
        elif not line.startswith(">") and not vocab.is_marker(line):
            rows[-1][3].append(line)
    out: list[MarkedTurn] = []
    last_on: int | None = None     # 上一個併進 out 的重點行
    for i, (who, at, on, body, notes, paused) in enumerate(rows):
        if not on:
            continue
        joins = (last_on is not None and rows[last_on][0] == who
                 and not any(rows[j][5] for j in range(last_on + 1, i + 1))
                 and all(vocab.is_unknown(rows[j][0]) for j in range(last_on + 1, i)))
        if joins:
            prev = out.pop()
            out.append(MarkedTurn(prev.time, prev.text + "".join(body), prev.notes + tuple(notes)))
        else:
            out.append(MarkedTurn(at, f"{who}:{''.join(body)}", tuple(notes)))
        last_on = i
    return out


def _spread_times(out: list[TimedLine]) -> None:
    """每一輪:開始 = 標頭時間,結束 = 下一輪開始;最後一輪用前面的平均語速估。"""
    turns: dict[str, list[int]] = {}
    for i, l in enumerate(out):
        turns.setdefault(l.time, []).append(i)
    groups = sorted(turns.values(), key=lambda idx: idx[0])
    starts = [seconds(out[g[0]].time) for g in groups]
    before_last = sum(len(out[i].text) for g in groups[:-1] for i in g)
    rate = starts[-1] / before_last if groups and before_last > 0 and starts[-1] > 0 else None
    for k, idx in enumerate(groups):
        total = sum(len(out[i].text) for i in idx)
        s0 = starts[k]
        s1 = starts[k + 1] if k + 1 < len(starts) else (s0 + total * rate if rate else None)
        acc = 0
        for i in idx:
            n = len(out[i].text)
            if s1 is not None and total > 0:
                out[i].start = s0 + (s1 - s0) * acc / total
                out[i].end = s0 + (s1 - s0) * (acc + n) / total
            else:
                out[i].start = s0
            acc += n


def seconds(hms: str) -> float:
    t = 0.0
    for part in hms.split(":"):
        try:
            t = t * 60 + float(part)
        except ValueError:
            continue
    return t


def clock(s: float) -> str:
    t = int(s)
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


# --- 原句核對 -----------------------------------------------------------------------

def squash(s: str) -> str:
    """只留文字與數字(去標點、空白),比對前用。"""
    return "".join(c for c in s if unicodedata.category(c)[0] in "LM" or unicodedata.category(c) == "Nd")


def _bigrams(s: str) -> list[str]:
    return [s[i:i + 2] for i in range(len(s) - 1)]


#: 原句的兩字組有這個比例出現在某一行,才算對得上(手機版實測:一字不差擋掉九成,六成放進太多改寫)
LOCATE_RATIO = 0.75
#: 原句兩字組數的合理範圍:太短什麼都對得上,太長是模型把整段倒進來
LOCATE_MIN_BIGRAMS, LOCATE_MAX_BIGRAMS = 5, 80


def locate(quote: str, lines: list[TimedLine]) -> str | None:
    """模型交的原句對回逐字稿,回推估的時間(那一輪的開始 + 原句在這一輪裡的位置);對不上回 None。
    ⚠️ 上限 80 組是照中文定的;英文原句(一百多個字母)不走這條,改比詞(`_locate_words`,上限 120 組詞)。"""
    hit = locate_line(quote, lines)
    return hit[0] if hit else None


def locate_line(quote: str, lines: list[TimedLine]) -> tuple[str, int] | None:
    """同 `locate`,另外回對上的那一行(核對內容要看原句附近那幾行;同手機版 `locateLine`)。
    沒有中日韓文字的原句改比相鄰兩個詞(`_locate_words`)。"""
    if not has_cjk(quote):
        return _locate_words(quote, lines)
    qb = _bigrams(squash(quote))
    if not LOCATE_MIN_BIGRAMS <= len(qb) <= LOCATE_MAX_BIGRAMS:
        return None

    def score(text: str) -> float:
        have = set(_bigrams(text))
        return sum(b in have for b in qb) / len(qb)

    # 先只比單一行(連下一行一起比的話,引第二行的會算成第一行的時間),都對不上才看跨兩行的
    own = [score(squash(l.text)) for l in lines]
    if own:
        i = max(range(len(own)), key=lambda k: own[k])
        if own[i] >= LOCATE_RATIO:
            text = squash(lines[i].text)
            pos = sorted(p for p in (text.find(b) for b in qb) if p >= 0)
            if lines[i].end is None or not pos or not text:
                return clock(lines[i].start), i
            frac = pos[len(pos) // 2] / len(text)
            return clock(lines[i].start + (lines[i].end - lines[i].start) * frac), i
    for i in range(len(lines) - 1):
        if score(squash(lines[i].text) + squash(lines[i + 1].text)) >= LOCATE_RATIO:
            return clock(lines[i].start), i
    return None


#: 英文原句的詞兩字組數的合理範圍(一段約 350 個詞,倒整段進來還是擋得到)
LOCATE_MIN_WORD_BIGRAMS, LOCATE_MAX_WORD_BIGRAMS = 3, 120


def _locate_words(quote: str, lines: list[TimedLine]) -> tuple[str, int] | None:
    """英文(拼音文字)的原句改比相鄰兩個詞(手機版 `d426beb`,同 Android ce40101):字母兩字組在英文裡
    太常見,編的句子、打亂詞序的句子都有九成以上對得上;真的原句又常超過 80 組被當成「整段倒進來」。
    詞兩字組:真的原句 0.95~1.00、編的 0~0.15。門檻同樣 75%。"""
    qb = _word_bigrams(words(quote))
    if not LOCATE_MIN_WORD_BIGRAMS <= len(qb) <= LOCATE_MAX_WORD_BIGRAMS:
        return None

    def score(w: list[str]) -> float:
        have = set(_word_bigrams(w))
        return sum(b in have for b in qb) / len(qb)

    lw = [words(l.text) for l in lines]
    own = [score(w) for w in lw]
    if own:
        i = max(range(len(own)), key=lambda k: own[k])
        if own[i] >= LOCATE_RATIO:
            # 在這一行裡的位置:對上的詞組第一次出現處取中位數 ÷ 這一行的詞數
            lb = _word_bigrams(lw[i])
            pos = sorted(lb.index(b) for b in qb if b in lb)
            if lines[i].end is None or not pos or not lw[i]:
                return clock(lines[i].start), i
            frac = pos[len(pos) // 2] / len(lw[i])
            return clock(lines[i].start + (lines[i].end - lines[i].start) * frac), i
    for i in range(len(lines) - 1):
        if score(lw[i] + lw[i + 1]) >= LOCATE_RATIO:
            return clock(lines[i].start), i
    return None


def has_cjk(s: str) -> bool:
    """有中日韓文字就照字比(中文夾英文術語也算中文;同手機版 `hasCJK` 的 Han/平假名/片假名/諺文)。"""
    return any(_cjk_char(ord(c)) for c in s)


def _cjk_char(cp: int) -> bool:
    return (0x2E80 <= cp <= 0x2FDF or cp in (0x3005, 0x3007) or 0x3021 <= cp <= 0x3029
            or 0x3038 <= cp <= 0x303B or 0x3041 <= cp <= 0x30FF or 0x3131 <= cp <= 0x318E
            or 0x31F0 <= cp <= 0x31FF or 0x3400 <= cp <= 0x4DBF or 0x4E00 <= cp <= 0x9FFF
            or 0xA960 <= cp <= 0xA97F or 0xAC00 <= cp <= 0xD7FF or 0xF900 <= cp <= 0xFAFF
            or 0xFF66 <= cp <= 0xFFDC or 0x1100 <= cp <= 0x11FF or 0x20000 <= cp <= 0x3FFFF)


def words(s: str) -> list[str]:
    """小寫的詞:連續的字母、組合記號、數字(I'll 拆成 i、ll,兩邊一樣拆就對得上;同手機版 `words`)。"""
    out: list[str] = []
    cur = ""
    for c in s.lower():
        cat = unicodedata.category(c)
        if cat[0] in "LM" or cat == "Nd":
            cur += c
        elif cur:
            out.append(cur)
            cur = ""
    if cur:
        out.append(cur)
    return out


def _word_bigrams(w: list[str]) -> list[str]:
    return [w[i] + " " + w[i + 1] for i in range(len(w) - 1)]


# --- 核對內容(手機版 08 §8.12 改版 D、E,2026-10-19)---------------------------------
# 原句對得上還不夠:模型會引一句真的話、卻寫成相反的意思(原文說「就照這個方案」,條目寫成「需評估
# 後續影響」),或把「5 號上線」寫成「4 日或 5 日遷移」。所以句子本身也要大半是原文的字,數字要原文都有

def support(text: str, context: str) -> float:
    """這句話(去標點)的兩字組有幾成出現在 context;沒有兩字組算 1。"""
    b = _bigrams(squash(text))
    if not b:
        return 1.0
    have = set(_bigrams(squash(context)))
    return sum(x in have for x in b) / len(b)


def share(text: str, context: str) -> float:
    """這句話有幾成出自 context:英文會議比實詞(`word_support`),其他比兩字組(`support`)。
    英文的字母兩字組太常見,編的句子對整段也有 0.74~0.89(2026-10-30 審查;手機版 `005d10d`)。"""
    if _draft.language == draftvocab.EN.language:
        return word_support(text, context)
    return support(text, context)


#: 重點、待辦:跟原句所在那一行(連前後各一行)比
MIN_SUPPORT = 0.4
#: 原句對不上時改看整段,門檻較高(Apple 常改寫原句:手機版 10-19 一場 15 條重點丟了 11 條是這樣)
MIN_SUPPORT_NO_QUOTE = 0.6
#: 議題跟整段比(0.5 時 Apple 9 個議題濾掉 5 個:它寫議題會換詞)
MIN_TOPIC_SUPPORT = 0.35
#: 看事實的核對:原句前後幾輪
FACT_WINDOW = 6
#: 看事實的核對:至少幾個不是虛詞的兩字組出現在原文
MIN_FACT_HITS = 4
#: 到處都有、不算數的兩字組(照抄手機版)
STOP_BIGRAMS = frozenset(
    "我們 這個 可以 一個 就是 那個 然後 他們 你們 的話 沒有 現在 因為 所以 需要 進行 相關 部分 "
    "問題 時候 已經 還是 什麼 這邊 那邊 如果 這樣 比較 應該 大家 自己 目前 是否 以及 並且 關於 "
    "表示 討論 確認 對方 這些 未來 後續 提到 的是 了一 在這 也是 是在 不是 都是 會議".split())
#: 簡中會議用的(手機版 `458daf8`:52 個裡 33 個簡體寫法不同,只有繁體的話簡中會議的虛詞都算數)。
#: ⚠️ 寫死手機版 ICU Hant-Hans 轉出來的結果,不在執行期用 OpenCC 轉——兩邊對照組要一樣
STOP_BIGRAMS_HANS = frozenset(
    "我们 这个 可以 一个 就是 那个 然后 他们 你们 的话 没有 现在 因为 所以 需要 进行 相关 部分 "
    "问题 时候 已经 还是 什么 这边 那边 如果 这样 比较 应该 大家 自己 目前 是否 以及 并且 关于 "
    "表示 讨论 确认 对方 这些 未来 后续 提到 的是 了一 在这 也是 是在 不是 都是 会议".split())


def grounded(text: str, quote: str, i: int, lines: list[TimedLine]) -> bool:
    """重點或待辦站得住:字大半出自原句附近(前後各一行)、數字原文都有;不行就看前後
    `FACT_WINDOW` 輪的事實(`fact_grounded`)。`i` 是 `locate_line` 對上的那一行。英文比實詞(`share`)。"""
    near = "\n".join(l.text for l in lines[max(0, i - 1):i + 2]) + "\n" + quote
    if share(text, near) >= MIN_SUPPORT and numbers_supported(text, near):
        return True
    wide = "\n".join(l.text for l in lines[max(0, i - FACT_WINDOW):i + FACT_WINDOW + 1]) + "\n" + quote
    return fact_grounded(text, wide)


def fact_grounded(text: str, context: str) -> bool:
    """看事實:條目裡的英文縮寫、數字 context 都有,而且至少 `MIN_FACT_HITS` 個不是虛詞的
    兩字組出現。用字比例對「用自己的話歸納」太嚴——手機版 10-19 查到 E4B 被丟的條目大多是對的。
    只有虛詞的空話(「需評估後續影響」0 個)照樣擋;用了原文名詞、意思寫反的擋不住(已知風險)。
    英文會議改看實詞:數字對得上、實詞 `MIN_WORD_FACT` 以上出現在 context(手機版 `bcafb06`)。"""
    if _draft.language == draftvocab.EN.language:
        return numbers_supported(text, context) and word_support(text, context) >= MIN_WORD_FACT
    if not numbers_supported(text, context) or not latin_terms(text) <= latin_terms(context):
        return False
    stops = STOP_BIGRAMS_HANS if _draft.language == draftvocab.ZH_CN.language else STOP_BIGRAMS
    have = set(_bigrams(squash(context).lower()))
    hits = {b for b in _bigrams(squash(text).lower())
            if b in have and b not in stops and not all(c.isnumeric() for c in b)}
    return len(hits) >= MIN_FACT_HITS


#: 英文會議看事實的門檻:前後幾輪裡實詞有七成出現
MIN_WORD_FACT = 0.7

#: 英文虛詞(手機版 `enStop`、Android `EN_STOP` 同一份,改了三邊一起改)
EN_STOP = frozenset((
    "a an the and or but if of to in on at by for with from as is are was were be been being it its this "
    "that these those there here i you he she we they me him her us them my your his our their what which "
    "who whom whose when where why how all any both each few more most other some such no nor not only own "
    "same so than too very can will just should would could may might must shall do does did doing have has "
    "had having about into over under again further then once up down out off also yeah um uh okay ok right "
    "well like oh mm hmm speaker").split())


def word_support(text: str, context: str) -> float:
    """英文會議的「站得住」比實詞(手機版 `bcafb06`,同 Android 9d9337b):字母兩字組在英文裡太常見,
    把重點配到 40 行外別段的原句附近也有 117/123 過關。⚠️ 只看話題有沒有跑掉,不看意思:
    not 是虛詞,寫反了照樣過(中文的字比對也一樣)。沒有實詞算 1。"""
    t_ = content_words(text)
    if not t_:
        return 1.0
    have = set(content_words(context))
    return sum(w in have for w in t_) / len(t_)


def content_words(s: str) -> list[str]:
    """實詞:小寫的詞去掉虛詞、單一字母、純數字,粗略去詞尾(ing、ed、es、s,去掉後至少留 3 個字母),
    再去結尾的 e(超過 3 個字母才去)——不去 e 的話 noises → nois、noise 不變,單複數對不上(手機版 `6350883`)。"""
    out = []
    for w in words(s):
        if w in EN_STOP or len(w) <= 1 or all(c.isnumeric() for c in w):
            continue
        v = next((w[:-len(suf)] for suf in ("ing", "ed", "es", "s") if len(w) > len(suf) + 2 and w.endswith(suf)), w)
        out.append(v[:-1] if len(v) > 3 and v.endswith("e") else v)
    return out


def thin(s: str) -> bool:
    """沒有內容:去掉標點不到 4 個字(空字串、照抄骨架的「…」、一兩個字的議題),英文另外是一個實詞
    都沒有("It is.")。`support` / `word_support` 對這些一律回 1,原本會照收(2026-10-30 審查;手機版 `e6de84c`)。"""
    return len(squash(s)) < 4 or (_draft.language == draftvocab.EN.language and not content_words(s))


def topic_stands(topic: str, text: str) -> bool:
    """議題站得住:有內容、字大半出現在這一段、數字原文都有。站不住的走「用第一個重點當標題」的退路——
    兩個字的議題原本會過這關,再在 `pick_topics`(標題至少 4 字)連重點一起消失。"""
    return not thin(topic) and share(topic, text) >= MIN_TOPIC_SUPPORT and numbers_supported(topic, text)


def latin_terms(s: str) -> set[str]:
    """英文縮寫與代號(「ABC」「X2」「QA」這類),不分大小寫。"""
    return {m.lower() for m in re.findall(r"[A-Za-z][A-Za-z0-9]+", s)}


def numbers_supported(text: str, context: str) -> bool:
    """句子裡的每個數字 context 都有(中文數字先換成阿拉伯數字:「300 萬」對得上「三百萬」)。
    英文數字詞只換 context 那邊(手機版 `69f6b43`:逐字稿念 "eighty-two point seven"、草稿寫 82.7%):
    句子這邊也換的話,"one of"、"two approaches" 都變成要對的數字,會多丟一堆。"""
    need = numbers(text)
    return not need or need <= numbers(context) | english_numbers(context)


def numbers(s: str) -> set[str]:
    """文字裡的數字:阿拉伯數字(去千分位)與中文數字(繁簡都認)。⚠️ 不能寫 `\\d`:它連全形與其他文字的
    數字都算(手機版 Swift 的 `\\d` 甚至把「三百萬」算進去)。"""
    out = {m.replace(",", "") for m in re.findall(r"[0-9][0-9,]*(?:\.[0-9]+)?", s)}
    # 两、万、亿:簡中(手機版 `458daf8`;原本「两百」被讀成 100)
    for m in re.findall(r"[零〇一二兩两三四五六七八九十百千萬万億亿]+", s):
        out |= chinese_number(m)
    return out


_EN_UNITS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
_EN_TEENS = ("ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
             "eighteen", "nineteen")
_EN_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
            "eighty": 80, "ninety": 90}
_EN_SCALES = {"thousand": 1000, "million": 1_000_000, "billion": 1_000_000_000}
_EN_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
                "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13,
                "fourteenth": 14, "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18,
                "nineteenth": 19, "twentieth": 20, "thirtieth": 30}


def english_numbers(s: str) -> set[str]:
    """英文數字詞 → 阿拉伯數字(逐條照手機版 `englishNumbers`):"eighty-two point seven" → 82.7、
    "three hundred million" → 300 與 300000000(同中文,scale 前面那段也算)、"twenty-first" → 21。
    連著念的個位數("two three")是兩個數;"and" 只在 hundred／thousand 後面接數字時算在裡面。"""
    w = words(s)
    out: set[str] = set()
    total = cur = 0
    on = False
    last = ""   # 剛才那個詞是 unit／teen／ten／hundred／scale

    def flush() -> None:
        nonlocal total, cur, on, last
        if on:
            out.add(str(total + cur))
        total = cur = 0
        on, last = False, ""

    def is_number_word(x: str) -> bool:
        return x in _EN_UNITS or x in _EN_TEENS or x in _EN_TENS or x == "hundred" or x in _EN_SCALES

    i = 0
    while i < len(w):
        x = w[i]
        if x in _EN_UNITS:
            if on and last in ("unit", "teen"):
                flush()
            cur += _EN_UNITS.index(x)
            on, last = True, "unit"
        elif x in _EN_TEENS:
            if on and last in ("unit", "teen", "ten"):
                flush()
            cur += 10 + _EN_TEENS.index(x)
            on, last = True, "teen"
        elif x in _EN_TENS:
            if on and last in ("unit", "teen", "ten"):
                flush()
            cur += _EN_TENS[x]
            on, last = True, "ten"
        elif x in _EN_ORDINALS:
            if on and last not in ("ten", "hundred", "scale"):
                flush()
            cur += _EN_ORDINALS[x]
            on = True
            flush()
        elif x == "hundred" or x in _EN_SCALES:
            if not on:
                if i == 0 or w[i - 1] != "a":
                    i += 1
                    continue
                cur, on = 1, True   # "a hundred"
            if x == "hundred":
                cur = max(cur, 1) * 100
                last = "hundred"
            else:
                out.add(str(cur))
                total += max(cur, 1) * _EN_SCALES[x]
                cur, last = 0, "scale"
        elif (x == "and" and on and last in ("hundred", "scale") and i + 1 < len(w)
              and is_number_word(w[i + 1])):
            pass   # one hundred and five:接著算
        elif x == "point" and i + 1 < len(w) and w[i + 1] in _EN_UNITS:
            frac = ""
            while i + 1 < len(w) and w[i + 1] in _EN_UNITS:
                frac += str(_EN_UNITS.index(w[i + 1]))
                i += 1
            out.add(f"{total + cur}.{frac}")
            on = False
            flush()
        else:
            flush()
        i += 1
    flush()
    return out


_CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6,
             "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000}


def chinese_number(s: str) -> set[str]:
    """「三百萬」→ {"300", "3000000"}(萬、億前面那段也算一個數:逐字稿寫「三百萬」、草稿寫
    「300 萬」)。口語約數:相鄰兩個數字後一個大 1(一兩、三四、五六)兩種讀法都算——「一兩百」→
    100、200,「三四十」→ 30、40,「十五六」→ 15、16(手機版 `4946414`:原本「一兩百」只讀得出 200,
    模型寫「一百到兩百」就判數字對不上)。口語省略最後一位:「一千一」= 1100、「兩百五」= 250、「三萬五」=
    35000(手機版 `9d21f4f`:原本讀成 1001、205、30005,「底薪一千一」被判內容站不住);原本的讀法也留著
    (只會多認)。⚠️ 手機版的怪處照抄、不要順手修(兩邊對照組要一樣):單獨一個「〇」不算數字。"""
    forms = [s]
    for i in range(len(s) - 1):
        x, y = _CN_DIGIT.get(s[i]), _CN_DIGIT.get(s[i + 1])
        if x is None or y is None or x < 1 or y != x + 1:
            continue
        for keep in (s[i], s[i + 1]):
            forms.append(s[:i] + keep + s[i + 2:])
    out: set[str] = set()
    for f in forms:
        out |= _chinese_number_one(f)
        full = _implied_unit(f)
        if full is not None:
            out |= _chinese_number_one(full)
    return out


#: 口語省略的那一位:百 → 十、千 → 百、萬 → 千、億 → 千萬(同手機版 `impliedUnit`)
_CN_IMPLIED = {"百": "十", "千": "百", "萬": "千", "万": "千", "億": "千萬", "亿": "千万"}


def _implied_unit(s: str) -> str | None:
    """最後是一個數字(1~9)、前面緊接著百/千/萬/億、再前面是數字或十/百/千、整串沒有零、至少 3 個字:
    補上省略的下一位(「一千一」→「一千一百」;「兩億三」→「兩億三千萬」);不是這種說法回 None。"""
    if len(s) < 3 or "零" in s or "〇" in s or _CN_DIGIT.get(s[-1], 0) < 1:
        return None
    n = _CN_IMPLIED.get(s[-2])
    if n is None or (s[-3] not in _CN_DIGIT and s[-3] not in _CN_UNIT):
        return None
    return s + n


def _chinese_number_one(s: str) -> set[str]:
    """一種讀法(`chinese_number` 的原本那一段)。"""
    def small(t: str) -> int | None:
        if not t:
            return None
        total = cur = 0
        any_ = False
        for c in t:
            if c in _CN_DIGIT:
                cur, any_ = _CN_DIGIT[c], True
            elif c in _CN_UNIT:
                total += max(cur, 1) * _CN_UNIT[c]
                cur, any_ = 0, True
            else:
                return None
        return total + cur if any_ else None

    out: set[str] = set()
    total = 0
    rest = s
    for marks, mul in (("億亿", 100_000_000), ("萬万", 10_000)):
        r = next((k for k, c in enumerate(rest) if c in marks), -1)
        if r >= 0:
            v = small(rest[:r])
            if v is not None:
                out.add(str(v))
                total += v * mul
            rest = rest[r + 1:]
    v = small(rest)
    if v is not None:
        total += v
        if total != v:
            out.add(str(v))
    if total > 0 or "零" in s:
        out.add(str(total))
    return out


def grounded_sentences(text: str, context: str) -> tuple[str, int]:
    """摘要的每一句:數字要在 context(核對過的重點)裡、字要有三成以上出自 context;
    站不住的句子丟掉。回 (留下的, 丟了幾句)。英文比實詞(`share`)。"""
    parts = summary_sentences(text)
    kept = [p for p in parts if numbers_supported(p, context) and share(p, context) >= 0.3]
    return "".join(kept).strip(), len(parts) - len(kept)


def summary_sentences(text: str) -> list[str]:
    """摘要斷句(手機版 `AISummary.sentences`;這邊另有一支 `sentences` 是「與我相關」切一輪話用的):
    「。！？!?」;英文會議另外認句點——後面接空白或結尾才算,縮寫(e.g.、Dr.)不算,小數點後面接數字
    本來就不算。原本英文只認「!?」,整段摘要是一句,一個數字對不上整段摘要就沒了(手機版 `a529ba3`)。
    每句保留原本的空白,接回去跟原文一樣。"""
    en = _draft.language == draftvocab.EN.language
    out: list[str] = []
    cur = ""
    for i, c in enumerate(text):
        cur += c
        if c in "。！？!?":
            out.append(cur)
            cur = ""
            continue
        if not en or c != "." or not (i + 1 == len(text) or text[i + 1].isspace()):
            continue
        j = i
        while j > 0 and not text[j - 1].isspace():
            j -= 1
        if text[j:i].lower().strip("(\"'") in EN_ABBREVIATIONS:
            continue
        out.append(cur)
        cur = ""
    if cur.strip():
        out.append(cur)
    return out


#: 後面的句點不是句尾(比對時去掉最後的句點、轉小寫;同手機版 `enAbbreviations`)。no、co、st、est、ca
#: 不放(手機版 `f9d59d1`):它們也是一般的字,會出現在句尾("The answer was no.")
EN_ABBREVIATIONS = frozenset((
    "e.g", "i.e", "etc", "vs", "dr", "mr", "mrs", "ms", "prof", "jr", "sr", "inc", "ltd",
    "u.s", "u.k", "a.m", "p.m", "approx", "fig", "dept"))


def repetitive(s: str) -> bool:
    """模型陷入重複寫出來的(「900000……」「對對對對……」):同一個字連 12 次,或最後 30 字
    是 2~4 字的片段一直重複。"""
    a = squash(s)
    run = 1
    for i in range(1, len(a)):
        run = run + 1 if a[i] == a[i - 1] else 1
        if run >= 12:
            return True
    if len(a) < 30:
        return False
    tail = a[-30:]
    return any(all(tail[j] == tail[j % k] for j in range(30)) for k in (2, 3, 4))


def _cjk_ok(cp: int) -> bool:
    return (0x3400 <= cp <= 0x4DBF or 0x4E00 <= cp <= 0x9FFF or 0xF900 <= cp <= 0xFAFF
            or 0x20000 <= cp <= 0x3FFFF or 0xFF21 <= cp <= 0xFF5A or cp in (0x3005, 0x3007))


def garbled(s: str) -> bool:
    """重複,或中文會議的條目混進中英文以外的字(手機版 10-19 E4B 實機:中文的待辦中間夾了一個泰文詞)。"""
    if repetitive(s):
        return True
    if _draft.language == draftvocab.EN.language:
        return False
    return any(c.isalpha() and ord(c) >= 0x80 and not _cjk_ok(ord(c)) for c in s)


def without_thinking(s: str) -> str:
    """Gemma 4 沒被要求思考也可能先寫一段「<|channel>thought …<channel|>」才回答(手機版
    10-17 Mac 實測):只留思考區塊後面的正文;沒寫完的思考(沒有收尾標記)整段不要。"""
    a = s.find("<|channel>")
    if a < 0:
        return s
    b = s.find("<channel|>", a + len("<|channel>"))
    if b < 0:
        return s[:a].strip()
    return (s[:a] + s[b + len("<channel|>"):]).strip()


def looks_like_task(task: str, chars: tuple[int, int] = (4, 60)) -> bool:
    """待辦要像一件事:不是把逐字稿原話(「名字:……」)搬過來、不太短也不太長。
    `chars` 是合理長度(英文一件事的字母數是中文字數的幾倍,見 `draftvocab.task_chars`)。"""
    t = task.strip()
    return chars[0] <= len(t) <= chars[1] and ":" not in t and "：" not in t   # 半形與全形冒號


def timed_chunks(lines: list[TimedLine], limit: int) -> list[list[TimedLine]]:
    """依字數切段(一行不拆;單行超過上限就自己一段)。"""
    out: list[list[TimedLine]] = []
    cur: list[TimedLine] = []
    n = 0
    for l in lines:
        if cur and n + len(l.text) + 1 > limit:
            out.append(cur)
            cur, n = [], 0
        cur.append(l)
        n += len(l.text) + 1
    if cur:
        out.append(cur)
    return out


def natural_chunks(lines: list[TimedLine], limit: int) -> list[list[TimedLine]]:
    """在自然的斷點切段(手機版 2026-10-20:固定字數會把一來一往的對話切斷——前一段只有問、
    後一段只有答):字數到上限的 80% 以後,下一輪開頭是轉場話就在它前面切;到 120% 還沒碰到
    就照字數切。一行不拆。逐字稿的時間是一輪一筆、量不到停頓,所以只看轉場話。"""
    out: list[list[TimedLine]] = []
    cur: list[TimedLine] = []
    n = 0
    soft, hard = limit * 4 // 5, limit * 6 // 5
    for l in lines:
        size = len(l.text) + 1
        if cur and ((n >= soft and is_transition(l.text)) or n + size > hard):
            out.append(cur)
            cur, n = [], 0
        cur.append(l)
        n += size
    if cur:
        out.append(cur)
    return out


#: 中文(繁簡)的轉場話:一輪開口 8 字內出現就算(放寬到 20 字會把「我們下週再看下一個版本」也算進去)
TRANSITION_WORDS = (
    "下一個", "下一位", "下一項", "下一題", "接下來", "接著", "再來是", "再來我們", "進入", "我們來看",
    "換到", "那我們就", "第二個部分", "第三個部分", "第二部分", "第三部分", "另外一個", "最後一個",
    "下一个", "下一位", "接下来", "再来是", "进入", "我们来看", "另外一个", "最后一个")
#: 英文的轉場片語:開頭 5 個詞裡整詞比對。⚠️ 不收單獨的 "next"(手機版 `5be450b`,2026-10-28):
#: "next week / next update" 在會議裡太常見,會在不該切的地方切段
EN_TRANSITION_WORDS = (
    "moving on", "move on", "let's move", "next item", "next topic", "next point", "next question",
    "next on the agenda", "next up", "on to the next", "turning to", "that brings us to")


def _ideographic(c: str) -> bool:
    cp = ord(c)
    return (0x3006 <= cp <= 0x3007 or 0x3021 <= cp <= 0x3029 or 0x3038 <= cp <= 0x303A
            or 0x3400 <= cp <= 0x4DBF or 0x4E00 <= cp <= 0x9FFF or 0xF900 <= cp <= 0xFAFF
            or 0x20000 <= cp <= 0x3FFFF)


def is_transition(line: str) -> bool:
    """這一輪(「名字:話」)一開口就是轉場話。開頭 8 字有漢字就照中文看;沒有就當英文,改看
    開頭 5 個詞——"moving on" 這些片語比 8 字長,只看 8 字永遠比不到(⚠️ 判斷看的是**這一行有沒有
    漢字**,不是會議語言)。只認半形冒號(全形的不算講者分隔,同手機版)。"""
    parts = [p for p in line.split(":", 1) if p]   # 同 Swift `split`:空的那一段不算
    body = parts[-1] if parts else line
    head = body[:8].lower()
    if any(_ideographic(c) for c in head):
        return any(w in head for w in TRANSITION_WORDS)
    # 標點當空白(彎引號先換成 '),整詞比對:"next up" 不中 "next update"
    words = " ".join([w for w in re.split(r"(?:[^\w']|_)+", body.lower().replace("’", "'")) if w][:5])
    return any(f" {w} " in f" {words} " for w in EN_TRANSITION_WORDS)


def context(before: int, parts: list[list[TimedLine]]) -> str:
    """前文:上一段最後幾輪(最多 3 輪、約 400 字),讓模型看得懂這一段開頭在接什麼話。
    標明只供參考;模型引了前文裡的話照樣核對得上(`quote_pool`),引前文的待辦另外跟上一段去重
    (`_map_all`)。"""
    picked = context_lines(before, parts)
    if not picked:
        return ""
    scale = _draft.char_scale
    return (_draft.context_label + "\n" + "\n".join(l.text[:200 * scale] for l in picked) + "\n"
            + _draft.body_label + "\n")


def context_lines(before: int, parts: list[list[TimedLine]]) -> list[TimedLine]:
    """前文是上一段最後幾行(每行截 200 字、合計約 400 字;同手機版 `contextLines`)。"""
    if before <= 0:
        return []
    scale = _draft.char_scale
    picked: list[TimedLine] = []
    n = 0
    for l in reversed(parts[before - 1][-3:]):
        c = min(len(l.text), 200 * scale)
        if n + c > 400 * scale and picked:
            break
        picked.insert(0, l)
        n += c
    return picked


def quote_pool(chunk: int, parts: list[list[TimedLine]]) -> list[TimedLine]:
    """核對原句時拿來比的行:前文 + 這一段。模型會引前文裡的話(手機版 `b11ea16`,2026-10-30 Qwen 實測:
    原句一字不差,只找這一段就判「原句對不上」)。"""
    return context_lines(chunk, parts) + parts[chunk]


def compact(lines: list[TimedLine]) -> list[TimedLine]:
    """送進模型前縮短(手機版 2026-10-12):講者標籤只留名字(「王小明 某某部」→「王小明」;
    「講者 3」「未知」不動),去掉「呃」「嗯」、連說的「對對對」只留一個——同樣的窗塞更多內容。"""
    out = []
    for l in lines:
        t_ = l.text
        if ":" in t_:
            who, rest = t_.split(":", 1)
            if not _is_speaker_label(who) and " " in who:
                t_ = who.split(" ", 1)[0] + ":" + rest
        for f in ("呃", "嗯"):
            t_ = t_.replace(f, "")
        while "對對" in t_:
            t_ = t_.replace("對對", "對")
        out.append(TimedLine(l.time, t_, l.start, l.end))
    return out


def _is_speaker_label(label: str) -> bool:
    """沒命名的講者(「講者 3」「Speaker 3」)或「未知」(同手機版 `TranscriptVocab.isSpeakerLabel`)。"""
    return vocab.speaker_number(label) is not None or vocab.is_unknown(label)


# --- 主要議題:每段一個主題,依時間區間挑(手機版 2026-10-12)---------------------------
# 原本議題跟摘要一樣從「合併過的重點」問,合併超過字數就截短、截掉的都是後段:手機版實測
# 3 小時半的長會,8 條議題全出自前 40 分鐘;改成這樣涵蓋到 3 小時 11 分

@dataclass
class Topic:
    title: str
    time: str
    chunks: int
    points: list[str]


#: 主要議題最多幾個
MAX_TOPICS = 12


def similar(a: str, b: str) -> bool:
    """主題相近:兩字組重疊佔較短那個的一半以上。"""
    x, y = set(_bigrams(squash(a))), set(_bigrams(squash(b)))
    if not x or not y:
        return False
    return len(x & y) / min(len(x), len(y)) >= 0.5


def short_title(t_: str) -> str:
    """模型常把一段裡的幾件事串成一長串(「甲、乙、丙、丁、戊」):只留前三個。"""
    parts = t_.split("、")
    return "、".join(parts[:3]) + "等" if len(parts) > 3 else t_


def pick_topics(raw: list[Topic], max_: int) -> list[Topic]:
    """相近而且相隔不遠(4 筆內)的主題併成一個;太短或沒有內容的丟掉。超過 `max_` 個時把整場
    切成 `max_` 個區間、每區挑花最多段的那個(打平取前面)——只依段數挑的話大家都是 1 段、
    全被前段佔走(手機版 2026-10-12 實機:只挑到前 1 小時 13 分)。"""
    merged: list[Topic] = []
    for t_ in raw:
        if len(squash(t_.title)) < 4 or not t_.points:
            continue
        i = next((k for k in range(len(merged) - 1, -1, -1) if similar(merged[k].title, t_.title)), None)
        if i is not None and len(merged) - i <= 4:
            merged[i] = Topic(merged[i].title, merged[i].time, merged[i].chunks + t_.chunks,
                              merged[i].points + t_.points)
        else:
            merged.append(Topic(t_.title, t_.time, t_.chunks, list(t_.points)))
    if len(merged) <= max_:
        return merged
    out = []
    for k in range(max_):
        window = merged[k * len(merged) // max_:(k + 1) * len(merged) // max_]
        best = max(w.chunks for w in window)
        out.append(next(w for w in window if w.chunks == best))
    return out


def duration_sec(md: str) -> float:
    """逐字稿大約多長(最後一輪的開始時間;估等待時間用,不必精確)。"""
    lines = timed_lines(md)
    return max((l.end or l.start for l in lines), default=0.0)


# --- 選檔與存檔 -------------------------------------------------------------------

def validate(text: str) -> Path:
    """路徑把關:必須是存在的單一 `.md` 檔(同 `relabel.validate`,話換成這個功能的)。"""
    cleaned = srcfile.clean_path(text)
    if not cleaned:
        raise UserFacingError(t("請先按「選擇逐字稿…」挑一份 md,或貼上它的路徑"))
    p = Path(cleaned)
    if p.is_dir():
        raise UserFacingError(t("這是資料夾,請選擇單一逐字稿檔:{cleaned}", cleaned=cleaned))
    if not p.is_file():
        raise UserFacingError(t("找不到檔案,請確認路徑是否正確:{cleaned}", cleaned=cleaned))
    if p.suffix.lower() != ".md":
        raise UserFacingError(t("會議紀錄草稿只吃逐字稿 md 檔,這是「{v}」", v=p.suffix or t("(無副檔名)")))
    return p


def drafts_dir() -> Path:
    """草稿放哪:App 自己的資料夾(使用者 2026-10-11 選定)。

    ⚠️ **絕不放在逐字稿旁邊**:使用者的逐字稿就放在餵知識庫的資料夾裡,旁邊多一個 md
    就會被一起收進知識庫——小模型編的東西不能流進下游(跟手機版同一個理由)。"""
    return paths.appdata_root() / "minutes"


#: 草稿檔的第一行。⚠️ **檔案之後會在 Finder 裡被單獨打開**,那時畫面上的警語都不在了,
#: 能提醒「這是 AI 寫的」只剩這一行
#: ⚠️ 「品質比不上雲端 AI」是使用者 2026-10-11 要求明講的(地端小模型能力有限)
DRAFT_NOTICE = _ZH.notice_apple
#: Qwen 那兩條的警語(模型是這台電腦上跑的小模型,不是 Apple Intelligence)
DRAFT_NOTICE_OV = _ZH.notice_local


def draft_notice() -> str:
    """草稿檔第一行:照**這一場實際用的**模型挑(Mac 每場可以換);還沒跑過就照平台。"""
    engine = _used_engine or (ENGINE_OV if _backend() == "openvino" else ENGINE_APPLE)
    return _draft.notice_apple if engine == ENGINE_APPLE else _draft.notice_local


def render(md_path: Path, body: str) -> str:
    """草稿檔的全文:警語 + 來源 + `generate` 的三段。"""
    return (f"{draft_notice()}\n{_draft.source_label}{md_path.name}\n\n"
            f"# {md_path.stem} {_draft.title_suffix}\n\n{body}")


def save(md_path: Path, text: str) -> Path:
    """寫進 `drafts_dir()`,檔名跟著逐字稿;同一份重做就蓋掉(舊草稿沒有保留的價值)。"""
    out = drafts_dir() / f"{md_path.stem}_會議紀錄草稿.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return out


def wait_hint(recording_sec: float, engine: str | None = None) -> str | None:
    """按之前先說大概要等多久(使用者 2026-10-11 選);20 分鐘以下的會不提醒。
    `engine` 是這一場要用的那個;不給就照記住的(不探測)。"""
    rate = SECONDS_PER_RECORDED_MINUTE
    first_time = ""
    engine = engine or _preferred()   # ⚠️ 不探測:這一支在主執行緒上、按下去的當下問
    if engine == ENGINE_OV:
        size, device = _ov_plan()
        rate = OV_SECONDS_PER_RECORDED_MINUTE[(size, device)]
        if not models.llm_cached(size):
            first_time = t(";第一次要先下載 AI 模型,約 {v} GB", v=models.LLM_DOWNLOAD_GB[size])
    elif engine == ENGINE_GEMMA and _backend() == "apple":
        rate = GEMMA_MLX_SECONDS_PER_RECORDED_MINUTE
        if not models.gemma_mlx_cached():
            first_time = t(";第一次要先下載 AI 模型,約 {v} GB", v=models.GEMMA_MLX_DOWNLOAD_GB)
    elif engine == ENGINE_GEMMA:
        rate = GEMMA_OV_SECONDS_PER_RECORDED_MINUTE
        if not models.gemma_ov_cached():
            first_time = t(";第一次要先下載 AI 模型,約 {v} GB", v=models.GEMMA_OV_DOWNLOAD_GB)
    elif engine == ENGINE_QWEN:
        rate = MLX_SECONDS_PER_RECORDED_MINUTE
        if not models.mlx_llm_cached():
            first_time = t(";第一次要先下載 AI 模型,約 {MLX_LLM_DOWNLOAD_GB} GB", MLX_LLM_DOWNLOAD_GB=models.MLX_LLM_DOWNLOAD_GB)
    if recording_sec < WAIT_HINT_MIN_SEC:
        return first_time.lstrip(";") or None
    minutes = max(1, round(recording_sec / 60 * rate / 60))
    return t("這場 {v} 分鐘,估計約要 {minutes} 分鐘{first_time}", v=int(recording_sec // 60), minutes=minutes, first_time=first_time)


def _ov_plan() -> tuple[str, str]:
    """Windows 用哪顆、跑在哪:(`4b`|`2b`, `GPU`|`CPU`)。每次問(設定可能剛改過),不記住。

    有 Intel 內顯就用內顯;「只用 CPU」打開了就照辦(舊電腦的內顯可能比 CPU 慢,使用者
    就是為此才打開它)。用 4B 還要記憶體夠:見 `OV_4B_MIN_RAM_MB`。"""
    from meeting_scribe import transcribe

    gpu = transcribe._intel_gpu_available() and not transcribe.cpu_only()
    device = "GPU" if gpu else "CPU"
    ram = plat.total_ram_mb() or 0
    return ("4b" if gpu and ram >= OV_4B_MIN_RAM_MB else "2b"), device


# --- 問模型 ---------------------------------------------------------------------------

class _Guardrail(Exception):
    """被 Apple 的安全過濾擋下。"""


class _Overflow(Exception):
    """超出模型的窗(中文的 token 數估不準,事前擋不完)。"""


class _CallFailed(Exception):
    """格式解不開、其他錯誤:這一段跳過(`llm-notes` 先重問,見 `_notes`)。"""


class _CallTimeout(_CallFailed):
    """超時:**不重問**——已經等了一次上限,再問一次多半同樣卡住(同手機版 `retryOnBadFormat`)。"""


#: Windows 那條在 `generate` 期間開著的子行程(`minutes_ov.Session`);其他時候是 None
_session = None


def _call(kind: str, instructions: str, prompt: str, lite: bool = False, retry: bool = False) -> dict:
    """問一次(`llm-ask` / `llm-notes`),回覆格式兩條路一樣。`lite`:`llm-notes` 不要待辦;
    `retry`:改用固定種子取樣(格式錯重問時)。
    ⚠️ **這場的專有名詞接在這裡**(唯一的咽喉):分段、重問、重點、合併、摘要每一次問都要帶
    (同手機版);接在指示的文字後面,子行程再把格式說明接在它後面。"""
    terms = _glossary
    if _session is None and _apple_context < 8192:
        # ⚠️ Mac 的 Apple 窗只有 4096、一段又塞 900 字:整場前 30 個名詞全接上去,回答寫到一半就被
        # 截斷、格式解不開(2026-10-29 真機:3 時 34 分那場連續 3 段失敗、整場停掉;不接清單就跑得完)。
        # 只接**這一次問的字裡真的有**的(手機的窗是 8192,照接整場的)
        terms = [g for g in terms if g in prompt]
    if terms:
        instructions += "\n" + _draft.glossary_label + _draft.list_sep.join(terms)
    if _session is not None:
        return _call_ov(kind, instructions, prompt, lite, retry)
    return _call_apple(kind, instructions, prompt, lite, retry)


def _call_ov(kind: str, instructions: str, prompt: str, lite: bool = False, retry: bool = False) -> dict:
    """Qwen 那兩條:問開著的那支子行程。⚠️ **子行程死掉不是「跳過這一段」**:後面每一段都會
    一樣死,跳過到最後只剩一句「沒有整理出內容」,真正的原因就看不到了。"""
    from meeting_scribe import minutes_ov

    try:
        msg = _session.ask(kind, instructions, prompt, OV_CALL_TIMEOUT_SEC,
                           language=_draft.language, lite=lite, retry=retry)
    except minutes_ov.CallTimeout as e:
        raise _CallTimeout(str(e)) from None
    except minutes_ov.WorkerGone as e:
        logger.warning("會議紀錄草稿:AI 模型子行程中斷:%s", e)
        raise UserFacingError(t("地端 AI 模型中途停止了,請再試一次。")) from None
    if not msg.get("ok"):
        raise _CallFailed(f"{msg.get('error')}:{str(msg.get('detail', ''))[:80]}")
    return msg


def _call_apple(kind: str, instructions: str, prompt: str, lite: bool = False,
                retry: bool = False) -> dict:
    """Mac:叫一次小程式。每問一次起一個行程:實測整支跑完 0.26 秒,
    跟呼叫本身一樣短,換到的是超時直接收掉行程、不必管常駐的那支卡在哪裡。"""
    _, exe = plat.apple_speech_helper()
    # 會議語言跟著帶:英文要用英文說明的那組 @Guide(中文說明配英文指示,模型照樣回中文;
    # iOS 2026-10-18 實測)
    proc = subprocess.Popen([*_helper_argv(exe), kind, _draft.language], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", **plat.spawn_kwargs(low_priority=False))
    payload: str | None = json.dumps({"instructions": instructions, "prompt": prompt,
                                      "lite": lite, "retry": retry}, ensure_ascii=False)
    deadline = time.monotonic() + CALL_TIMEOUT_SEC
    try:
        while True:
            cancel.check()  # 停止響應點:等模型回覆的空檔
            try:
                out, err = proc.communicate(payload, timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                payload = None  # 第一次已經送進去了,重試時不能再送
                if time.monotonic() > deadline:
                    raise _CallTimeout(t("{CALL_TIMEOUT_SEC:.0f} 秒沒有回覆", CALL_TIMEOUT_SEC=CALL_TIMEOUT_SEC))
    except BaseException:
        proc.kill()  # 停止鈕(Cancelled 是 BaseException)與超時都要收掉小程式
        proc.wait()
        raise
    try:
        msg = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise _CallFailed(t("回覆解不開(結束碼 {returncode}):{v}", returncode=proc.returncode, v=err.strip()[:200])) from None
    error = msg.get("error")
    if error == "guardrail":
        raise _Guardrail()
    if error == "context":
        raise _Overflow()
    if error or proc.returncode != 0:
        # 只留錯誤的種類(如 decodingFailure):後面的 debugDescription 夾著逐字稿原文,不進記錄檔
        raise _CallFailed(f"{error}:{str(msg.get('detail', '')).split('(')[0][:80]}")
    return msg


def _tw(s: str) -> str:
    """模型的回覆 → 這一場的字形。⚠️ **照會議語言,不是一律繁化**:簡中會議的草稿被繁化就是
    一份繁簡夾雜的東西;英文不動(OpenCC 對英文無害,但白付載入辭典的成本)。"""
    if _draft.language == draftvocab.ZH_CN.language:
        return convert.to_simplified(s).strip()
    if _draft.language == draftvocab.EN.language:
        return s.strip()
    return convert.to_taiwan_traditional(s).strip()


def _ask(instructions: str, prompt: str) -> str:
    try:
        # Gemma 4 偶爾先寫一段思考才回答(手機版 10-17 Mac 實測):自由回答的摘要混進來,使用者會直接看到
        return _tw(without_thinking(_call("llm-ask", instructions, prompt).get("text", "")))
    except _Guardrail:
        raise UserFacingError(t("Apple 的安全過濾擋下了這場的內容,沒有產生會議紀錄草稿。")) from None
    except (_Overflow, _CallFailed) as e:
        # ⚠️ `str(e) or …` 不是 `e or …`:例外物件一律為真,而 `_Overflow()` 不帶訊息——那樣寫畫面上
        # 就只剩一對空括號「()」(2026-10-30 審查)
        raise UserFacingError(t("{v}沒有回覆,請再試一次({v2})。", v=_engine_name(),
                                v2=str(e) or t("超出長度"))) from None


# 行首的條列記號:「- 」「* 」「• 」「1. 」「1、」「(1)」「1)」
_BULLET = re.compile(r"^\s*(?:[-*•‧·]\s*|\(?\d{1,2}[.、)）](?!\d)\s*|（\d{1,2}）\s*)")


def _tidy_summary(text: str) -> str:
    """摘要是一段話:拿掉條列記號、接成一段。⚠️ 提示詞明講「不要條列」,Windows 的小模型
    照樣寫成「1. …」三行(2026-10-11 實測 Qwen3.5-2B)。"""
    return "".join(_BULLET.sub("", l).strip() for l in text.splitlines() if l.strip())


#: 這一場的模型怎麼稱呼(錯誤訊息用;`_generate_session` 換成那顆的名字)
_label: str = ""


def _engine_name() -> str:
    """錯誤訊息裡怎麼稱呼回答的那一方。"""
    if _session is not None:
        return _label   # 跟 `_session` 一起設、一起清(`_generate_session`),一定有名字
    return t("Apple 的語言模型")


@dataclass
class _ChunkResult:
    topics: list[Topic] = field(default_factory=list)
    todos: list[tuple[str, str]] = field(default_factory=list)   # (事項, 約略時間)
    dropped: int = 0          # 待辦:原句對不上、不像一件事、內容站不住、亂碼
    points_dropped: int = 0   # 重點:同上
    topics_dropped: int = 0   # 議題名稱站不住(改用第一個重點當名稱)
    skipped: int = 0          # 跳過的行數(被擋、超時、單行就爆窗)
    #: 這一段寫不出來(重問都解不開、超時、安全過濾)。對半切的兩半都失敗才算——整場卡住就停
    #: 看的是它(`_map_all`),整場加總後的值不用看
    failed: bool = False
    #: 點到「與我相關」稱呼的原句(「講者:原話(時間)」):不經模型,一律收(手機版改版 F)
    mine: list[str] = field(default_factory=list)
    #: `todos` 裡原句在前文(上一段最後幾行)的那幾條:上一段可能已經寫過同一件事(`_map_all` 去重;
    #: 手機版 `TodoItem.fromContext`)
    ctx_todos: list[tuple[str, str]] = field(default_factory=list)

    def __add__(self, o: "_ChunkResult") -> "_ChunkResult":
        return _ChunkResult(self.topics + o.topics, self.todos + o.todos, self.dropped + o.dropped,
                            self.points_dropped + o.points_dropped,
                            self.topics_dropped + o.topics_dropped, self.skipped + o.skipped,
                            self.failed and o.failed, self.mine + o.mine, self.ctx_todos + o.ctx_todos)


def _valid_notes(msg: dict, lite: bool) -> bool:
    """段落協定 v2 的形狀(`topic` 字串、`points` 是附原句的物件、`todo` 是物件或 null)。
    ⚠️ 少了欄位算格式錯、要重問(手機版 10-19:E4B 有 3 段交回的少了 topic,整段沒了)。"""
    pts = msg.get("points")
    if not isinstance(msg.get("topic"), str) or not isinstance(pts, list):
        return False
    if not all(isinstance(p, dict) and isinstance(p.get("text"), str) for p in pts):
        return False
    todo = msg.get("todo")
    return lite or todo is None or (isinstance(todo, dict) and isinstance(todo.get("task"), str))


def _notes(prompt: str) -> dict:
    """問一段的筆記,格式錯就重問(同手機版 `retryOnBadFormat` + `ChunkNotesLite`):不隨機 →
    開新的一次、改用固定種子取樣 → 還不行就只問議題與重點、放棄這一段的待辦(其他內容保住;
    手機版 10-20 Apple 實機:某一段的待辦兩次都少了 quote)。⚠️ 超時、安全過濾、爆窗不重問。"""
    last: _CallFailed | None = None
    for lite, retry in ((False, False), (False, True), (True, False), (True, True)):
        try:
            msg = _call("llm-notes", _draft.map_instructions, prompt, lite=lite, retry=retry)
        except _CallTimeout:
            raise
        except _CallFailed as e:
            last = e
        else:
            if _valid_notes(msg, lite):
                if lite:
                    msg = {**msg, "todo": None}
                return msg
            last = _CallFailed("decodingFailure:欄位不齊")
        logger.info("會議紀錄草稿:格式解不開,重問(%s):%s", "不含待辦" if lite else "取樣", last)
    raise last


def _map_chunk(lines: list[TimedLine], ctx: str = "",
               pool: list[TimedLine] | None = None) -> _ChunkResult:
    """一段 → 議題、核對過的重點與待辦(同手機版 `mapChunk`)。爆窗就對半切再各問一次;單行還爆
    就跳過這一行;被安全過濾擋下、超時、重問也解不開就跳過這一段(少一點內容,不讓整場失敗:
    手機版實測一場 44 分鐘的會有一段被擋)。`ctx` 是前文(`context`),只供模型參考;`pool` 是核對
    原句用的行(`quote_pool`:前文 + 這一段;沒給就是這一段),行號都是 `pool` 的。"""
    text = "\n".join(l.text for l in lines)
    pool = pool if pool is not None else lines
    own = len(pool) - len(lines)   # pool 裡前文佔幾行
    try:
        msg = _notes(ctx + text)
    except _Overflow:
        if len(lines) <= 1:
            return _ChunkResult(skipped=1)
        half = len(lines) // 2
        return _map_chunk(lines[:half]) + _map_chunk(lines[half:])
    except (_Guardrail, _CallFailed) as e:
        logger.info("會議紀錄草稿:跳過一段(%d 行):%s", len(lines),
                    "安全過濾" if isinstance(e, _Guardrail) else e)
        return _ChunkResult(skipped=len(lines), failed=True)
    r = _ChunkResult()
    todo = msg.get("todo")
    if isinstance(todo, dict):
        task, quote = _tw(str(todo.get("task", ""))), _tw(str(todo.get("quote", "")))
        hit = None if garbled(task) or repetitive(quote) else locate_line(quote, pool)
        if hit and looks_like_task(task, _draft.task_chars) and grounded(task, quote, hit[1], pool):
            r.todos.append((task, hit[0]))
            if hit[1] < own:
                r.ctx_todos.append((task, hit[0]))
        else:
            r.dropped += 1
    # 重點:原句對得上、句子本身也大半是原文的字、數字原文都有,才留(對不上的直接丟,手機版
    # 10-19 使用者選)。原句對不上(Apple 常改寫原句)時改看這句跟整段的吻合度,門檻較高。
    # 沒有內容的(照抄骨架的「…」、一兩個字)不收:support 對它們一律回 1(手機版 `e6de84c`)
    points: list[str] = []
    for p in msg.get("points") or []:
        p_text, p_quote = _tw(str(p.get("text", ""))), _tw(str(p.get("quote", "")))
        if garbled(p_text) or repetitive(p_quote) or thin(p_text):
            r.points_dropped += 1
            continue
        hit = locate_line(p_quote, pool)
        if hit is None:
            ok = ((share(p_text, text) >= MIN_SUPPORT_NO_QUOTE and numbers_supported(p_text, text))
                  or fact_grounded(p_text, text))
        else:
            ok = grounded(p_text, p_quote, hit[1], pool)
        if ok:
            points.append(p_text)
        else:
            r.points_dropped += 1
    when = clock(lines[0].start)
    topic = _tw(str(msg.get("topic", "")))
    # 議題的字要大半出現在這一段裡(用逐字稿的專有名詞);編出來的議題丟掉,改用這段第一個核對過的
    # 重點(前 20 字)當名稱——Apple 寫議題常換詞,整個議題不見更糟(手機版 10-19);兩個字的議題也走這條
    if topic_stands(topic, text):
        r.topics.append(Topic(short_title(topic), when, 1, points))
    else:
        r.topics_dropped += 1
        if points:
            r.topics.append(Topic(points[0][:20].strip(",。、,. "), when, 1, points))
    return r


#: 連續這麼多段寫不出來就整場停下、不往下合併與寫摘要(手機版 2026-10-26:Android 的 Qwen 在
#: LiteRT-LM 連問會退化,8 段壞 6 段、燒 26 分鐘交出幾乎空白的草稿;這邊是萬一的出口)
MAX_STUCK_CHUNKS = 3


class Stuck(UserFacingError):
    """整場卡住:連續 `MAX_STUCK_CHUNKS` 段寫不出來。畫面在後面接記錄檔的指路(`desktop.LOG_HINT`)。"""


def _stuck_message() -> str:
    """選得到別的模型才叫他換(同手機版 `stuckMessage`)。"""
    others = [k for k in choices() if k != _used_engine and engines()[k].ok]
    if others:
        return t("{model} 整理這場會議一直失敗(連續 {n} 段寫不出來)。請換另一個模型再試;"
                 "也請回報這一場,幫我們改進。", model=_engine_name(), n=MAX_STUCK_CHUNKS)
    return t("{model} 整理這場會議一直失敗(連續 {n} 段寫不出來)。請再試一次;仍然失敗的話,"
             "請回報這一場,幫我們改進。", model=_engine_name(), n=MAX_STUCK_CHUNKS)


def aliases() -> list[str]:
    """「與我相關」的稱呼(「⚙ 地端 AI 設定…」裡填的;逗號、頓號、換行都算分隔,同手機版)。"""
    raw = settings.get(settings.KEY_MINUTES_ALIASES)
    if not isinstance(raw, str):
        return []
    return [a.strip() for a in re.split(r"[,，、\n]", raw) if a.strip()]


#: 一句最長幾字(太長的照逗號再切;同手機版 `Directives.maxChars`)
SENTENCE_MAX_CHARS = 90


def sentences(body: str) -> list[str]:
    """一輪的話 → 句子(同手機版 `Directives.sentences`):句號、問號、驚嘆號切,太長的照逗號
    再切成不超過 `SENTENCE_MAX_CHARS`;不到 6 字的不要。(半形逗號也算:這邊的逐字稿兩種都有)"""
    out, cur = [], ""
    for c in body:
        cur += c
        if c in "。？！?!":
            out.append(cur)
            cur = ""
    if cur.strip():
        out.append(cur)
    parts: list[str] = []
    for sent in out:
        if len(sent) <= SENTENCE_MAX_CHARS:
            parts.append(sent)
            continue
        piece = ""
        for chunk in re.split(r"[，,]", sent):
            if piece and len(piece) + len(chunk) + 1 > SENTENCE_MAX_CHARS:
                parts.append(piece)
                piece = ""
            piece += ("，" if piece else "") + chunk
        if piece:
            parts.append(piece)
    return [p.strip() for p in parts if len(p.strip()) >= 6]


def mentions(lines: list[TimedLine], names: list[str]) -> list[str]:
    """點到稱呼的原句,一句一行「- 講者:原話(時間)」(手機版改版 F:不經模型判斷,一律收)。
    ⚠️ **兩個字以上的稱呼才算**(一個字到處都是;同手機版)。"""
    names = [a for a in names if len(a) >= 2]
    if not names:
        return []
    out = []
    for line in lines:
        who, sep, body = line.text.partition(":")
        if not sep:
            who, body = "", line.text
        for sent in sentences(body):
            if any(a in sent for a in names):
                hit = locate_line(sent, lines)
                at = hit[0] if hit else clock(line.start)
                out.append(f"- {who + ':' if who else ''}{sent}({at})")
    return out


def _map_all(parts: list[list[TimedLine]], report: ProgressFn) -> _ChunkResult:
    """逐段做 `_map_chunk`(帶前文);連續 `MAX_STUCK_CHUNKS` 段失敗就丟 `Stuck`(後面的段不做),
    有一段成功就重算。"""
    total = prev = _ChunkResult()
    stuck = 0
    for i, p in enumerate(parts):
        r = _map_chunk(p, context(i, parts), quote_pool(i, parts))
        # 引前文的待辦:上一段在 90 秒內已經有一條,多半是同一件事換句話說(措辭不同,最後的去重認不出來;
        # 手機版 `b11ea16`)。上一段沒有就留——上一段漏掉的,靠這一段補回來
        if r.ctx_todos and prev.todos:
            dup = [x for x in r.ctx_todos
                   if any(abs(seconds(y[1]) - seconds(x[1])) <= 90 for y in prev.todos)]
            r.todos = [x for x in r.todos if x not in dup]
        prev = r
        # 與我相關:**在這裡收、不在 `_map_chunk` 裡**——那一段寫不出來(被擋、超時)也照樣收得到;
        # 那裡爆窗時會對半切再呼叫自己,放進去還會重複收(手機版是問完才收,這是刻意的改良)
        r.mine = r.mine + mentions(p, _aliases)
        total = total + r
        stuck = stuck + 1 if r.failed else 0
        if stuck >= MAX_STUCK_CHUNKS:
            # 記錄檔只留段數(內容含真名),回報問題時看得出是這個出口停的
            logger.warning("會議紀錄草稿:連續 %d 段寫不出來,停在第 %d 段(共 %d 段)",
                           stuck, i + 1, len(parts))
            raise Stuck(_stuck_message())
        report(0.85 * (i + 1) / len(parts))
        if (i + 1) % 5 == 0:   # 心跳:Windows 只有 CPU 時一段要十幾秒,整場十幾分鐘
            logger.info("會議紀錄草稿:%d/%d 段", i + 1, len(parts))
    return total


def _merge(notes: list[str]) -> tuple[list[str], int, bool]:
    """重點太長就分批合併,直到塞得進一次呼叫;回 (合併後, 合併了幾層, 有沒有截短)。"""
    level, truncated = 0, False
    limit = MERGE_CHARS * _draft.char_scale
    while len("\n".join(notes)) > limit:
        level += 1
        nxt: list[str] = []
        cur: list[str] = []
        for n in notes:
            if cur and len("\n".join(cur + [n])) > limit:
                nxt.append(cur[0] if len(cur) == 1 else _ask(_draft.merge_instructions, "\n".join(cur)))
                cur = []
            cur.append(n)
        if cur:
            nxt.append(cur[0] if len(cur) == 1 else _ask(_draft.merge_instructions, "\n".join(cur)))
        if len(nxt) >= len(notes):  # 合併不再變少:截短保證收斂(會掉內容)
            nxt = [n[:limit // 3] for n in nxt]
            truncated = True
        notes = nxt
    return notes, level, truncated


def _chunk_chars(engine: str) -> int:
    """一段幾字。Apple 在 Mac 上是 4096 的窗 → 900(手機版 10-11:1200 字時 50 段有 3 段爆窗);
    小程式回報 8192 以上 → 1800。下載的模型窗夠大,一律 1800(手機版 10-20 實測 1800 / 3600 / 7200,
    長段一段出錯丟的更多、議題涵蓋全面下降,定案 1800)。"""
    wide = engine != ENGINE_APPLE or _apple_context >= 8192
    return (CHUNK_CHARS_WIDE if wide else CHUNK_CHARS) * _draft.char_scale


def generate(md: str, progress: ProgressFn | None = None, engine: str | None = None, *,
             title: str = "", corrections: list[namefix.Entry] | None = None) -> str:
    """逐字稿 md → 會議紀錄草稿(Markdown,三段)。進度 0~1:分段佔 85%、合併與收尾佔 15%。

    `engine` 不給就用 `default_engine()`(畫面上點了就記進設定,所以就是使用者選的那個)。
    `title` 是逐字稿檔名(名詞對照表的分組照它挑);`corrections` 給了就不讀聲紋同步資料夾那一份
    (評測與測試用)。"""
    global _used_engine
    can = capability()
    if not can.ok:
        raise UserFacingError(t("無法產生會議紀錄草稿:{reason}", reason=can.reason))
    engine = engine or default_engine()
    # ⚠️ 選的那個用不了(例如選了 Apple 而 Apple Intelligence 沒打開)就講它的理由,
    # **不換另一個**:悄悄換掉等於把「去打開」那句提示吞掉
    mine = engines().get(engine or "")
    if mine is None or not mine.ok:
        raise UserFacingError(t("無法產生會議紀錄草稿:{v}", v=mine.reason if mine else can.reason))
    _used_engine = engine
    report = progress or (lambda f: None)
    t0 = time.monotonic()
    global _highlights, _draft, _aliases
    # 提示詞與草稿的字照**逐字稿的會議語言**(不看介面語言,同 iOS);每一場重挑,不串到下一場
    _draft = draftvocab.of(vocab.detect(md).language)
    # 送進模型的是縮短過的(`compact`),核對原句也對它:送進去的就是這些字
    lines = compact(timed_lines(md))
    _highlights = highlighted_turns(md)
    _apply_corrections(lines, title, corrections)
    _aliases = aliases()
    parts = natural_chunks(lines, _chunk_chars(engine))
    if not parts:
        raise UserFacingError(t("逐字稿是空的,沒有東西可以整理。"))
    # ⚠️ 跟轉檔同一道:Windows 只有 CPU 時要跑 7~15 分鐘,筆電一闔蓋/鎖屏就進新式待命、
    # 整個行程被凍結(症狀長得跟當機一樣,見 `power.keep_awake`)
    with power.keep_awake():
        if engine == ENGINE_APPLE:
            return _compose(parts, report, t0)
        if engine == ENGINE_GEMMA and _backend() == "apple":
            # Mac:MLX(第一次會下載 5.2 GB)
            return _generate_session(models.gemma_mlx_dir(), "MLX", "Gemma 4 E4B", parts, report, t0)
        if engine == ENGINE_GEMMA:
            # 第一次會下載(6.5 GB);只有內顯能選到它(`_gemma_capability`)
            return _generate_session(models.gemma_ov_dir(), "GPU", "Gemma 4 E4B", parts, report, t0)
        if engine == ENGINE_QWEN:
            # 第一次會下載;進度由 models 的掛鉤送上畫面
            return _generate_session(models.mlx_llm_dir(), "MLX", "Qwen3.5-4B",
                                     parts, report, t0)
        size, device = _ov_plan()
        return _generate_session(models.minutes_llm_dir(size), device, f"Qwen3.5-{size.upper()}",
                                 parts, report, t0)


def _apply_corrections(lines: list[TimedLine], title: str,
                       corrections: list[namefix.Entry] | None) -> None:
    """名詞對照表(手機版改版 A):送進模型的那幾行與現場標的重點**就地**換成正確寫法,算好這場的
    專有名詞(`_glossary`)。逐字稿 md 不動;核對原句也對換過的字(送進去的就是這些字)。
    沒有對照表就什麼都不做(使用者 2026-10-29:讀不到就不做)。"""
    global _highlights, _glossary, _replaced
    _glossary, _replaced = [], 0
    if corrections is None:
        table = namefix.load()
        corrections = table.entries(title) if table else []
    if not corrections:
        return
    if _draft.language == draftvocab.ZH_CN.language:
        # 簡中會議:對照表整份轉簡體再換,不混進繁體字(同手機版 2026-10-25)
        corrections = namefix.simplified(corrections)
    # 整場只排一次、編一次樣式(`namefix.Prepared`:逐行重編的話,表一大就是開頭白等幾十秒)
    fix = namefix.prepare(corrections)
    for i, line in enumerate(lines):
        text, n = fix.apply(line.text)
        if n:
            lines[i] = replace(line, text=text)
            _replaced += n
    _highlights = [replace(h, text=fix.apply(h.text)[0]) for h in _highlights]
    _glossary = namefix.glossary("\n".join(line.text for line in lines), corrections)


def _generate_session(model_dir: Path, device: str, label: str,
                      parts: list[list[TimedLine]], report: ProgressFn, t0: float) -> str:
    """Qwen 那兩條:起子行程載模型 → 照常整理 → 收掉子行程(下載在呼叫端先做完)。"""
    global _session, _label
    from meeting_scribe import minutes_ov

    session = minutes_ov.Session(str(model_dir), device)
    try:
        load = session.start()
    except minutes_ov.WorkerGone as e:
        # 記錄檔留一行原因(環境問題是預期內的,不附堆疊),畫面只講能做的事
        logger.warning("會議紀錄草稿:AI 模型載入失敗(%s,%s):%s", label, device, e)
        if str(e).startswith(("ImportError", "ModuleNotFoundError")):
            # ⚠️ 元件載不起來(最常見是缺 Visual C++ 執行階段)跟模型檔壞掉處方相反:
            # 叫他刪掉 3 GB 重新下載,下載完照樣載不起來
            raise errors.native_engine_error(ImportError(str(e))) from None
        raise UserFacingError(t("地端 AI 模型載入失敗,請再試一次;仍然失敗的話,{v}。", v=models.redownload_hint())) from None
    logger.info("會議紀錄草稿:%s 跑在 %s,載入 %.1f 秒", label, device, load)
    _session, _label = session, label
    try:
        return _compose(parts, report, t0)
    finally:
        _session, _label = None, ""
        session.close()


def _compose(parts: list[list[TimedLine]], report: ProgressFn, t0: float) -> str:
    """分段寫議題與重點 → 合併 → 摘要;議題從各段挑、待辦逐段核對(兩條路共用;差別只在
    `_call` 問誰)。"""
    r = _map_all(parts, report)
    notes = ["\n".join("- " + x for x in tp.points) for tp in r.topics if tp.points]
    if not notes:
        # 每段都失敗(可能是安全過濾,也可能是模型出錯;各段的錯誤都被跳過了,分不出是哪個),**或者每一條
        # 重點都在核對時被丟掉**(改版 D 之後;段落本身都成功)。⚠️ 下面那行統計在 raise 之後寫不出來,
        # 這裡先記數字,回報時才分得出是哪一種(2026-10-30 審查;只記數字,不記內容)
        logger.warning("會議紀錄草稿:沒有留下任何重點(%d 段、重點丟掉 %d 條、待辦 %d 條、跳過 %d 行)",
                       len(parts), r.points_dropped, len(r.todos), r.skipped)
        raise UserFacingError(t("這場沒有整理出內容(可能被 Apple 的安全過濾擋下),請再試一次。")
                              if _session is None else
                              t("這場沒有整理出內容,請再試一次。"))
    unique: dict[str, tuple[str, str]] = {}
    # ⚠️ 迴圈變數不可以叫 t:這個函式裡有 `t("…")`,一綁定就整個函式都是區域變數
    for todo in r.todos:   # 同一件事出現在兩段:留第一次
        unique.setdefault(squash(todo[0]), todo)
    # 不設上限:截掉的會是後半場(手機版實測)。照時間排(同一段裡模型給的順序不一定照時間)
    todos = sorted(unique.values(), key=lambda x: seconds(x[1]))
    # 摘要照舊:各段重點一輪輪合併再問(手機版 10-12 試過只看挑出的議題寫摘要,會混進閒聊、
    # 數字套錯對象,使用者選維持)
    notes, level, truncated = _merge(notes)
    report(0.88)
    marked, marked_dropped = _marked_points(_highlights)
    report(0.9)
    v = _draft
    joined = "\n".join(notes)
    # 摘要先看現場標的重點(同手機版):那是與會者自己講「這個重要」的地方
    summary_input = f"{v.marked_label}\n{marked}\n\n{v.notes_label}\n{joined}" if marked else joined
    # 摘要只看核對過的重點寫;寫出來的每一句,數字要在重點裡、字要大半出自重點(不然丟掉那一句)
    summary, summary_dropped = grounded_sentences(
        _tidy_summary(_ask(v.summary_instructions, summary_input)), summary_input)
    report(0.95)
    picked = pick_topics(r.topics, MAX_TOPICS)
    topics = "\n".join(f"- {tp.title}({tp.time})" + (f":{tp.points[0]}" if tp.points else "")
                       for tp in picked)
    todo_text = "\n".join(f"- {x}({at})" for x, at in todos) if todos else v.no_todos
    mine = _about_me(todos, r)
    logger.info("會議紀錄草稿:%d 段、議題 %d 個(名稱站不住 %d)、重點丟掉 %d 條、合併 %d 層%s、"
                "待辦 %d 條(丟掉 %d 條)、摘要丟掉 %d 句、重點標記丟掉 %d 個、跳過 %d 行、"
                "名詞對照換 %d 處(專有名詞 %d 個)、與我相關 %d 條,耗時 %.1f 秒",
                len(parts), len(picked), r.topics_dropped, r.points_dropped, level,
                "(有截短)" if truncated else "", len(todos), r.dropped, summary_dropped,
                marked_dropped, r.skipped, _replaced, len(_glossary), len(mine),
                time.monotonic() - t0)
    report(1.0)
    marked_text = f"\n\n{v.highlights_heading}\n{marked}" if marked else ""
    # 與我相關放在草稿最上面(手機版改版 F);沒有就整節不出現
    about = f"{v.about_me_heading}\n" + "\n".join(mine) + "\n\n" if mine else ""
    return (f"{about}{v.summary_heading}\n{summary}{marked_text}\n\n{v.topics_heading}\n{topics}"
            f"\n\n{v.todos_heading}\n{todo_text}\n")


def _about_me(todos: list[tuple[str, str]], r: _ChunkResult) -> list[str]:
    """「與我相關」那一節(手機版改版 F 的順序):待辦裡點到稱呼的 → 重點裡出現稱呼的 → 原句。"""
    names = [a for a in _aliases if len(a) >= 2]
    if not names:
        return []
    out = [f"- {x}({at})" for x, at in todos if any(a in x for a in names)]
    out += [f"- {p}({tp.time})" for tp in r.topics for p in tp.points if any(a in p for a in names)]
    out += r.mine
    return list(dict.fromkeys(out))


#: 這一場標了〔重點〕的那幾輪(`generate` 開頭讀好;`_compose` 的兩條路共用)
_highlights: list[MarkedTurn] = []


def _marked_points(turns: list[MarkedTurn]) -> tuple[str, int]:
    """每個重點一句「- …(時間)」;回 (那幾行, 核對沒過丟掉幾個)。一個問不出來就跳過那一個
    (同 `_map_chunk`:少一點,不整場失敗)。寫出來的那句字要有四成出自發言與備註、數字都在
    (手機版 10-19 改版 D;這邊連備註一起算,備註本來就是要它優先看的)。"""
    out = []
    dropped = 0
    picked = turns[:MAX_HIGHLIGHTS]
    for i, h in enumerate(picked):
        # 心跳:一個重點問一次,Windows 只有 CPU 時一次十幾秒、12 個就是好幾分鐘,進度條這段不動
        logger.info("會議紀錄草稿:現場重點 %d/%d", i + 1, len(picked))
        prompt = h.text[:_chunk_chars(_used_engine or ENGINE_APPLE)]
        if h.notes:
            prompt += "\n" + _draft.attendee_note + _draft.note_sep.join(h.notes)
        try:
            msg = _call("llm-ask", _draft.highlight_instructions, prompt)
        except (_Guardrail, _Overflow, _CallFailed) as e:
            logger.info("會議紀錄草稿:跳過一個重點(%s):%s", h.time,
                        "安全過濾" if isinstance(e, _Guardrail) else (str(e) or "超出長度"))
            continue
        line = _BULLET.sub("", " ".join(without_thinking(_tw(msg.get("text", ""))).splitlines()).strip())
        if not line:
            continue
        if share(line, prompt) < MIN_SUPPORT or not numbers_supported(line, prompt):
            dropped += 1
            continue
        out.append(f"- {line}({h.time})")
    return "\n".join(out), dropped
