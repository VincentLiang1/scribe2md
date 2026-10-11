"""會議紀錄草稿:地端語言模型。Mac 用 Apple 裝置端模型(Foundation Models,macOS 26 以上、
Apple Intelligence 打開時);Windows 用 OpenVINO 跑 Qwen3.5(`minutes_ov`,第一次用時下載,
有內顯用 4B、只有 CPU 用 2B——2026-10-11 使用者選定,數字見 `docs/spec/mac/05` §5.4b)。

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
4. 回覆會混進簡體字(Mac 實測「什么问题」)→ 出來的文字一律再繁化一次(手機版沒做,這裡補上)。
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from meeting_scribe import (cancel, convert, errors, export, loopdetect, models, paths, plat,
                            power, srcfile)
from meeting_scribe.errors import UserFacingError
from meeting_scribe.plat import Outcome

logger = logging.getLogger(__name__)

ProgressFn = Callable[[float], None]

#: 每段幾個字問一次(手機版實測:1200 字時 50 段有 3 段爆窗,要多交待辦與原句之後改 900)
CHUNK_CHARS = 900
#: 合併時一批最多幾個字
MERGE_CHARS = 1100
#: 一次呼叫最多等這麼久;超過就當這一段失敗、跳過(手機實機一段卡住時整場停在同一個百分比)
CALL_TIMEOUT_SEC = 90.0
#: 按之前說大概要等多久:Mac 實測約 3 小時的錄音 386 秒 ≈ 每分鐘錄音 2.1 秒
SECONDS_PER_RECORDED_MINUTE = 2.1
#: 這麼短的會不提醒(幾十秒就好)
WAIT_HINT_MIN_SEC = 20 * 60

# --- Windows(OpenVINO)那條的數字 ------------------------------------------------
#: 一次呼叫最多等多久。⚠️ **不可以沿用 Apple 的 90 秒**:超時是「這一段悄悄跳過」,而
#: 純 CPU 跑 2B 一次中位 16 秒(Core Ultra 7 258V),一般公司電腦慢兩三倍、長的那幾次
#: 就會撞上 90——少掉的段落在草稿上看不出來
OV_CALL_TIMEOUT_SEC = 300.0
#: 有內顯也要這麼多記憶體才用 4B:**內顯沒有自己的記憶體**,4B 跑起來佔 4.3 GB,
#: 8 GB 的筆電上等於跟 Windows 搶同一份(16 GB 的機器報出來約 15.7 GB,門檻放在 15)
OV_4B_MIN_RAM_MB = 15_000
#: 每分鐘錄音要等幾秒(43 分鐘的會實測 4B 內顯 161 秒、2B 內顯 66 秒、2B CPU 328 秒,
#: 在 Core Ultra 7 258V 上;一般公司電腦慢,各放寬 1.5 倍)
OV_SECONDS_PER_RECORDED_MINUTE = {("4b", "GPU"): 5.6, ("2b", "GPU"): 2.3, ("2b", "CPU"): 11.4}

TODOS_HEADING = "## 待辦(約略時間,請對照逐字稿)"
NO_TODOS = "- 沒有找到明確的待辦"

_MAP_INSTRUCTIONS = (
    "你是會議記錄助理。使用者會給你一段會議逐字稿(語音辨識的結果,有錯字、標點少,"
    "每行開頭是講者,可能是「未知」或「講者 N」)。\n"
    "用繁體中文(台灣用語)整理這一段的重點與待辦。只寫逐字稿裡真的有的,不要推測。"
)
_MERGE_INSTRUCTIONS = (
    "你是會議記錄助理。使用者會給你幾段會議重點。用繁體中文(台灣用語)合併成最多 6 點、"
    "每點一句,合併重複的,只寫重點裡有的。"
)
# 摘要與議題分開問、標題由程式加:一次要兩段時它不照格式(實測標題整個不見),
# 給它寫開頭又會捏造日期、主持人
_SUMMARY_INSTRUCTIONS = (
    "你是會議記錄助理。使用者會給你一場會議各時段的重點。用繁體中文(台灣用語)寫 3 句以內的摘要,"
    "講整場最重要的目標、數字與方向。\n"
    "只寫重點裡有的,不要推測;不要寫日期、地點、主持人、出席者;不要每句都寫「某某人認為」;"
    "不要加標題或條列。"
)
_TOPICS_INSTRUCTIONS = (
    "你是會議記錄助理。使用者會給你一場會議各時段的重點。用繁體中文(台灣用語)條列這場會議的"
    "主要議題,最多 8 點,每點一句,每點用「- 」開頭。\n"
    "只寫重點裡有的,不要推測;不要寫決議或待辦;不要加標題。"
)


# --- 能不能用 -------------------------------------------------------------------

_capability: Outcome | None = None


def _helper_argv(exe) -> list[str]:
    """叫小程式的指令開頭(測試換成「python 假小程式」用;同 `transcribe_apple._helper_argv`)。"""
    return [str(exe)]


def _backend() -> str | None:
    """誰來回答:Mac 是 `apple`、Windows 是 `openvino`、其他沒有。

    ⚠️ **Mac 不退回 OpenVINO**:Apple Intelligence 沒打開要回 `failed` 叫使用者去打開,
    改用另一顆沒驗過的模型等於把那句提示吞掉。"""
    return {"macos": "apple", "windows": "openvino"}.get(plat.name())


def capability() -> Outcome:
    """這台能不能產生會議紀錄草稿;結果留著(問一次要起一個行程)。

    `unsupported`:macOS 太舊、這台機器不支援 Apple Intelligence、不是 Mac 也不是
    Windows——按鈕不出現。`failed`:Apple Intelligence 沒打開、模型還在下載、元件叫不起來
    ——使用者自己解得了,理由照原樣顯示。⚠️ **打開 Apple Intelligence 要到 Mac 前面的
    「系統設定」做**,從別台電腦連進來的人看不到那個畫面,所以句子裡要講明是哪裡。
    Windows 一律 `ok`:模型第一次按下去時才下載,問這一句不碰任何原生庫。"""
    global _capability
    if _capability is None:
        backend = _backend()
        if backend == "apple":
            _capability = _probe()
        elif backend == "openvino":
            _capability = Outcome("ok")
        else:
            _capability = Outcome("unsupported", "這台電腦沒有可以用的地端語言模型")
    return _capability


def clear_capability_cache() -> None:
    global _capability
    _capability = None


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
        return Outcome("failed", "Apple 語言模型元件叫不起來,請更新到最新版或重新啟動程式再試")
    state = reply.get("state")
    if state == "ok":
        return Outcome("ok")
    if state == "not_enabled":
        return Outcome("failed", "在這台 Mac 的「系統設定 → Apple Intelligence 與 Siri」打開 "
                                 "Apple Intelligence,就能產生會議紀錄草稿")
    if state == "not_ready":
        return Outcome("failed", "Apple Intelligence 的模型還在下載,好了就能產生會議紀錄草稿")
    return Outcome("unsupported", {
        "unsupported_os": "會議紀錄草稿需要 macOS 26 以上",
        "not_eligible": "這台 Mac 不支援 Apple Intelligence",
        "unsupported_locale": "這台 Mac 的 Apple Intelligence 不支援繁體中文",
    }.get(state, "這台 Mac 沒有 Apple 的裝置端語言模型"))


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


def timed_lines(md: str) -> list[TimedLine]:
    """逐字稿 md 的正文(`## 會議逐字稿` 到 `## 講者辨識診斷`)→「名字:這一輪說的話」。

    ⚠️ 講者行一律用 `export.SPEAKER_LINE_RE`(全 repo 只有一份),取時間用具名群組。
    暫停那行與跳針標記不是發言,不送給模型(白花 token,還可能被當成待辦的原句)。"""
    out: list[TimedLine] = []
    in_body, who, stamp = False, "", ""
    for raw in md.split("\n"):
        line = raw.strip()  # 連 \r 一起去掉:Windows 產生的 md 是 CRLF
        if line.startswith("## 會議逐字稿"):
            in_body = True
            continue
        if line.startswith(export.DIAGNOSTIC_HEADING):
            break
        if not in_body or not line:
            continue
        m = export.SPEAKER_LINE_RE.match(line)
        if m:
            who = m["name"]
            stamp = f"{int(m['h']):02d}:{m['m']}:{m['s']}"
            continue
        if export.is_pause_line(line) or line.startswith(loopdetect.MARKER_PREFIX):
            continue
        out.append(TimedLine(stamp, f"{who}:{line}" if who else line))
    _spread_times(out)
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
    """模型交的原句對回逐字稿,回推估的時間(那一輪的開始 + 原句在這一輪裡的位置);對不上回 None。"""
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
                return clock(lines[i].start)
            frac = pos[len(pos) // 2] / len(text)
            return clock(lines[i].start + (lines[i].end - lines[i].start) * frac)
    for i in range(len(lines) - 1):
        if score(squash(lines[i].text) + squash(lines[i + 1].text)) >= LOCATE_RATIO:
            return clock(lines[i].start)
    return None


def looks_like_task(task: str) -> bool:
    """待辦要像一件事:不是把逐字稿原話(「名字:……」)搬過來、不太短也不太長。"""
    t = task.strip()
    return 4 <= len(t) <= 60 and ":" not in t and "：" not in t   # 半形與全形冒號


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


def duration_sec(md: str) -> float:
    """逐字稿大約多長(最後一輪的開始時間;估等待時間用,不必精確)。"""
    lines = timed_lines(md)
    return max((l.end or l.start for l in lines), default=0.0)


# --- 選檔與存檔 -------------------------------------------------------------------

def validate(text: str) -> Path:
    """路徑把關:必須是存在的單一 `.md` 檔(同 `relabel.validate`,話換成這個功能的)。"""
    cleaned = srcfile.clean_path(text)
    if not cleaned:
        raise UserFacingError("請先按「選擇逐字稿…」挑一份 md,或貼上它的路徑")
    p = Path(cleaned)
    if p.is_dir():
        raise UserFacingError(f"這是資料夾,請選擇單一逐字稿檔:{cleaned}")
    if not p.is_file():
        raise UserFacingError(f"找不到檔案,請確認路徑是否正確:{cleaned}")
    if p.suffix.lower() != ".md":
        raise UserFacingError(f"會議紀錄草稿只吃逐字稿 md 檔,這是「{p.suffix or '(無副檔名)'}」")
    return p


def drafts_dir() -> Path:
    """草稿放哪:App 自己的資料夾(使用者 2026-10-11 選定)。

    ⚠️ **絕不放在逐字稿旁邊**:使用者的逐字稿就放在餵知識庫的資料夾裡,旁邊多一個 md
    就會被一起收進知識庫——小模型編的東西不能流進下游(跟手機版同一個理由)。"""
    return paths.appdata_root() / "minutes"


#: 草稿檔的第一行。⚠️ **檔案之後會在 Finder 裡被單獨打開**,那時畫面上的警語都不在了,
#: 能提醒「這是 AI 寫的」只剩這一行
DRAFT_NOTICE = ("> AI 草稿:Apple Intelligence 在這台 Mac 上整理,可能漏掉或寫錯,"
                "正式紀錄請對照逐字稿另行整理。")
#: Windows 那條的警語(模型是這台電腦上跑的小模型,不是 Apple Intelligence)
DRAFT_NOTICE_OV = ("> AI 草稿:地端 AI 模型在這台電腦上整理,可能漏掉或寫錯,"
                   "正式紀錄請對照逐字稿另行整理。")


def draft_notice() -> str:
    """這台的草稿檔第一行。"""
    return DRAFT_NOTICE_OV if _backend() == "openvino" else DRAFT_NOTICE


def render(md_path: Path, body: str) -> str:
    """草稿檔的全文:警語 + 來源 + `generate` 的三段。"""
    return f"{draft_notice()}\n> 來源:{md_path.name}\n\n# {md_path.stem} 會議紀錄草稿\n\n{body}"


def save(md_path: Path, text: str) -> Path:
    """寫進 `drafts_dir()`,檔名跟著逐字稿;同一份重做就蓋掉(舊草稿沒有保留的價值)。"""
    out = drafts_dir() / f"{md_path.stem}_會議紀錄草稿.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return out


def wait_hint(recording_sec: float) -> str | None:
    """按之前先說大概要等多久(使用者 2026-10-11 選);20 分鐘以下的會不提醒。"""
    rate = SECONDS_PER_RECORDED_MINUTE
    first_time = ""
    if _backend() == "openvino":
        size, device = _ov_plan()
        rate = OV_SECONDS_PER_RECORDED_MINUTE[(size, device)]
        if not models.llm_cached(size):
            first_time = f";第一次要先下載 AI 模型,約 {models.LLM_DOWNLOAD_GB[size]} GB"
    if recording_sec < WAIT_HINT_MIN_SEC:
        return first_time.lstrip(";") or None
    minutes = max(1, round(recording_sec / 60 * rate / 60))
    return f"這場 {int(recording_sec // 60)} 分鐘,估計約要 {minutes} 分鐘{first_time}"


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
    """超時、格式解不開、其他錯誤:這一段跳過。"""


#: Windows 那條在 `generate` 期間開著的子行程(`minutes_ov.Session`);其他時候是 None
_session = None


def _call(kind: str, instructions: str, prompt: str) -> dict:
    """問一次(`llm-ask` / `llm-notes`),回覆格式兩條路一樣。"""
    if _session is not None:
        return _call_ov(kind, instructions, prompt)
    return _call_apple(kind, instructions, prompt)


def _call_ov(kind: str, instructions: str, prompt: str) -> dict:
    """Windows:問開著的那支子行程。⚠️ **子行程死掉不是「跳過這一段」**:後面每一段都會
    一樣死,跳過到最後只剩一句「沒有整理出內容」,真正的原因就看不到了。"""
    from meeting_scribe import minutes_ov

    try:
        msg = _session.ask(kind, instructions, prompt, OV_CALL_TIMEOUT_SEC)
    except minutes_ov.CallTimeout as e:
        raise _CallFailed(str(e)) from None
    except minutes_ov.WorkerGone as e:
        logger.warning("會議紀錄草稿:AI 模型子行程中斷:%s", e)
        raise UserFacingError("地端 AI 模型中途停止了,請再試一次。") from None
    if not msg.get("ok"):
        raise _CallFailed(f"{msg.get('error')}:{str(msg.get('detail', ''))[:80]}")
    return msg


def _call_apple(kind: str, instructions: str, prompt: str) -> dict:
    """Mac:叫一次小程式。每問一次起一個行程:實測整支跑完 0.26 秒,
    跟呼叫本身一樣短,換到的是超時直接收掉行程、不必管常駐的那支卡在哪裡。"""
    _, exe = plat.apple_speech_helper()
    proc = subprocess.Popen([*_helper_argv(exe), kind], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", **plat.spawn_kwargs(low_priority=False))
    payload: str | None = json.dumps({"instructions": instructions, "prompt": prompt},
                                     ensure_ascii=False)
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
                    raise _CallFailed(f"{CALL_TIMEOUT_SEC:.0f} 秒沒有回覆")
    except BaseException:
        proc.kill()  # 停止鈕(Cancelled 是 BaseException)與超時都要收掉小程式
        proc.wait()
        raise
    try:
        msg = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise _CallFailed(f"回覆解不開(結束碼 {proc.returncode}):{err.strip()[:200]}") from None
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
    return convert.to_taiwan_traditional(s).strip()


def _ask(instructions: str, prompt: str) -> str:
    try:
        return _tw(_call("llm-ask", instructions, prompt).get("text", ""))
    except _Guardrail:
        raise UserFacingError("Apple 的安全過濾擋下了這場的內容,沒有產生會議紀錄草稿。") from None
    except (_Overflow, _CallFailed) as e:
        raise UserFacingError(f"{_engine_name()}沒有回覆,請再試一次({e or '超出長度'})。") from None


#: 主要議題最多幾點(提示詞也是這個數)
MAX_TOPICS = 8
# 行首的條列記號:「- 」「* 」「• 」「1. 」「1、」「(1)」「1)」
_BULLET = re.compile(r"^\s*(?:[-*•‧·]\s*|\(?\d{1,2}[.、)）](?!\d)\s*|（\d{1,2}）\s*)")


def _tidy_summary(text: str) -> str:
    """摘要是一段話:拿掉條列記號、接成一段。⚠️ 提示詞明講「不要條列」,Windows 的小模型
    照樣寫成「1. …」三行(2026-10-11 實測 Qwen3.5-2B)。"""
    return "".join(_BULLET.sub("", l).strip() for l in text.splitlines() if l.strip())


def _tidy_topics(text: str) -> str:
    """議題:每點統一成「- 」開頭、最多 `MAX_TOPICS` 點。⚠️ 提示詞寫了「最多 8 點」,
    Qwen3.5 照樣寫 12~18 點(2026-10-11 實測);截掉的是後面的,而後面多半是瑣事。"""
    items = [t for t in (_BULLET.sub("", l).strip() for l in text.splitlines()) if t]
    return "\n".join(f"- {t}" for t in items[:MAX_TOPICS])


def _engine_name() -> str:
    """錯誤訊息裡怎麼稱呼回答的那一方。"""
    return "地端 AI 模型" if _backend() == "openvino" else "Apple 的語言模型"


@dataclass
class _ChunkResult:
    points: list[str]
    todos: list[tuple[str, str]]   # (事項, 約略時間)
    dropped: int                    # 原句對不上或不像一件事、被丟掉的待辦
    skipped: int                    # 跳過的行數(被擋、超時、單行就爆窗)


def _map_chunk(lines: list[TimedLine]) -> _ChunkResult:
    """一段 → 重點與核對過的待辦。爆窗就對半切再各問一次;單行還爆就跳過這一行;
    被安全過濾擋下、超時、格式壞掉就跳過這一段(少一點內容,不讓整場失敗:
    手機版實測一場 44 分鐘的會有一段被擋)。"""
    try:
        msg = _call("llm-notes", _MAP_INSTRUCTIONS, "\n".join(l.text for l in lines))
    except _Overflow:
        if len(lines) <= 1:
            return _ChunkResult([], [], 0, 1)
        half = len(lines) // 2
        a, b = _map_chunk(lines[:half]), _map_chunk(lines[half:])
        return _ChunkResult(a.points + b.points, a.todos + b.todos,
                            a.dropped + b.dropped, a.skipped + b.skipped)
    except (_Guardrail, _CallFailed) as e:
        logger.info("會議紀錄草稿:跳過一段(%d 行):%s", len(lines),
                    "安全過濾" if isinstance(e, _Guardrail) else e)
        return _ChunkResult([], [], 0, len(lines))
    points = [p for p in (_tw(str(x)) for x in msg.get("points") or []) if p]
    todos: list[tuple[str, str]] = []
    dropped = 0
    todo = msg.get("todo")
    if isinstance(todo, dict):
        task, at = _tw(str(todo.get("task", ""))), locate(_tw(str(todo.get("quote", ""))), lines)
        if at and looks_like_task(task):
            todos.append((task, at))
        else:
            dropped += 1
    return _ChunkResult(points, todos, dropped, 0)


def _merge(notes: list[str]) -> tuple[list[str], int, bool]:
    """重點太長就分批合併,直到塞得進一次呼叫;回 (合併後, 合併了幾層, 有沒有截短)。"""
    level, truncated = 0, False
    while len("\n".join(notes)) > MERGE_CHARS:
        level += 1
        nxt: list[str] = []
        cur: list[str] = []
        for n in notes:
            if cur and len("\n".join(cur + [n])) > MERGE_CHARS:
                nxt.append(cur[0] if len(cur) == 1 else _ask(_MERGE_INSTRUCTIONS, "\n".join(cur)))
                cur = []
            cur.append(n)
        if cur:
            nxt.append(cur[0] if len(cur) == 1 else _ask(_MERGE_INSTRUCTIONS, "\n".join(cur)))
        if len(nxt) >= len(notes):  # 合併不再變少:截短保證收斂(會掉內容)
            nxt = [n[:MERGE_CHARS // 3] for n in nxt]
            truncated = True
        notes = nxt
    return notes, level, truncated


def generate(md: str, progress: ProgressFn | None = None) -> str:
    """逐字稿 md → 會議紀錄草稿(Markdown,三段)。進度 0~1:分段佔 85%、合併與收尾佔 15%。"""
    can = capability()
    if not can.ok:
        raise UserFacingError(f"無法產生會議紀錄草稿:{can.reason}")
    report = progress or (lambda f: None)
    t0 = time.monotonic()
    lines = timed_lines(md)
    parts = timed_chunks(lines, CHUNK_CHARS)
    if not parts:
        raise UserFacingError("逐字稿是空的,沒有東西可以整理。")
    # ⚠️ 跟轉檔同一道:Windows 只有 CPU 時要跑 7~15 分鐘,筆電一闔蓋/鎖屏就進新式待命、
    # 整個行程被凍結(症狀長得跟當機一樣,見 `power.keep_awake`)
    with power.keep_awake():
        if _backend() != "openvino":
            return _compose(parts, report, t0)
        return _generate_ov(parts, report, t0)


def _generate_ov(parts: list[list[TimedLine]], report: ProgressFn, t0: float) -> str:
    """Windows:下載(第一次)→ 起子行程載模型 → 照常整理 → 收掉子行程。"""
    global _session
    from meeting_scribe import minutes_ov

    size, device = _ov_plan()
    model_dir = models.minutes_llm_dir(size)   # 第一次會下載;進度由 models 的掛鉤送上畫面
    session = minutes_ov.Session(str(model_dir), device)
    try:
        load = session.start()
    except minutes_ov.WorkerGone as e:
        # 記錄檔留一行原因(環境問題是預期內的,不附堆疊),畫面只講能做的事
        logger.warning("會議紀錄草稿:AI 模型載入失敗(%s,%s):%s", size, device, e)
        if str(e).startswith(("ImportError", "ModuleNotFoundError")):
            # ⚠️ 元件載不起來(最常見是缺 Visual C++ 執行階段)跟模型檔壞掉處方相反:
            # 叫他刪掉 3 GB 重新下載,下載完照樣載不起來
            raise errors.native_engine_error(ImportError(str(e))) from None
        raise UserFacingError(f"地端 AI 模型載入失敗,請再試一次;仍然失敗的話,"
                              f"{models.redownload_hint()}。") from None
    logger.info("會議紀錄草稿:Qwen3.5-%s 跑在 %s,載入 %.1f 秒", size.upper(), device, load)
    _session = session
    try:
        return _compose(parts, report, t0)
    finally:
        _session = None
        session.close()


def _compose(parts: list[list[TimedLine]], report: ProgressFn, t0: float) -> str:
    """分段寫重點 → 合併 → 摘要、議題、待辦(兩條路共用;差別只在 `_call` 問誰)。"""
    notes: list[str] = []
    todos: list[tuple[str, str]] = []
    dropped = skipped = 0
    for i, p in enumerate(parts):
        r = _map_chunk(p)
        if r.points:
            notes.append("\n".join("- " + x for x in r.points))
        todos += r.todos
        dropped += r.dropped
        skipped += r.skipped
        report(0.85 * (i + 1) / len(parts))
        if (i + 1) % 5 == 0:   # 心跳:Windows 只有 CPU 時一段要十幾秒,整場十幾分鐘
            logger.info("會議紀錄草稿:%d/%d 段", i + 1, len(parts))
    if not notes:  # 每段都失敗:可能是安全過濾,也可能是模型出錯(各段的錯誤都被跳過了,分不出是哪個)
        raise UserFacingError("這場沒有整理出內容(可能被 Apple 的安全過濾擋下),請再試一次。"
                              if _backend() != "openvino" else
                              "這場沒有整理出內容,請再試一次。")
    unique: dict[str, tuple[str, str]] = {}
    for t in todos:   # 同一件事出現在兩段:留第一次
        unique.setdefault(squash(t[0]), t)
    todos = list(unique.values())
    # 不設上限:截掉的會是後半場(手機版實測)。照時間排是防線:現在一段最多一條、原句只在
    # 那一段裡核對,各段交上來的本來就照時間;一段放寬成多條時,同一段裡模型給的順序不一定照時間
    todos.sort(key=lambda t: seconds(t[1]))
    notes, level, truncated = _merge(notes)
    report(0.9)
    joined = "\n".join(notes)
    summary = _tidy_summary(_ask(_SUMMARY_INSTRUCTIONS, joined))
    report(0.95)
    topics = _tidy_topics(_ask(_TOPICS_INSTRUCTIONS, joined))
    todo_text = "\n".join(f"- {t}({at})" for t, at in todos) if todos else NO_TODOS
    logger.info("會議紀錄草稿:%d 段、合併 %d 層%s、待辦 %d 條(丟掉 %d 條)、跳過 %d 行,耗時 %.1f 秒",
                len(parts), level, "(有截短)" if truncated else "", len(todos), dropped, skipped,
                time.monotonic() - t0)
    report(1.0)
    return f"## 摘要\n{summary}\n\n## 主要議題\n{topics}\n\n{TODOS_HEADING}\n{todo_text}\n"
