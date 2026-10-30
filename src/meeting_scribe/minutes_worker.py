r"""會議紀錄草稿的語言模型子行程(Qwen3.5;父端是 `minutes_ov`)。Windows 是 OpenVINO
(裝置參數 `CPU`/`GPU`),Mac 是 MLX(裝置參數 `MLX`,跑在 GPU;2026-10-14 起)。

跟 `transworker` 同一套規矩:stdin 收 NDJSON、stdout **只准** NDJSON、優先權由父端降
(below-normal,「轉換時電腦還能用」)。模型與裝置是啟動參數:一次 `minutes.generate`
起一支、載一次、問完收掉——每問一次重載要 3~15 秒,整場要問二十次上下。

指令:
  啟動:-m meeting_scribe.minutes_worker <模型資料夾> <CPU|GPU|MLX>
      → {"ready":true,"load_sec":5.5}   或 {"ready":false,"error":"…"}
  {"kind":"llm-notes"|"llm-ask","instructions":"…","prompt":"…","language":"zh_TW"|"zh_CN"|"en",
   "lite":false,"retry":false}
      → {"ok":true,"topic":"…","points":[{"text","quote"}…],"todo":{"task","quote"}|null}
        /   {"ok":true,"text":"…"}
      → {"ok":false,"error":"decodingFailure"|"other","detail":"…"}
  段落協定 v2(2026-10-28,同手機版 `ChunkNotes`):`lite` 不要待辦(格式錯重問兩次之後)、
  `retry` 改用固定種子取樣(同樣的不隨機再問一次多半同樣的錯)。
  EOF → 退出

⚠️ **回覆格式跟 Apple 那支小程式(`apple_asr.swift` 的 `llm-*`)一樣**,`minutes.py`
只管切段、核對、合併,不管是誰回答的。`@Guide` 的描述在這裡寫成提示詞的一段
(Foundation Models 會自動把它放進提示,這邊得自己放)。
"""
import json
import re
import sys
import time
from pathlib import Path

from meeting_scribe import draftvocab, plat, stdio

# 跟 apple_asr.swift 的 `ChunkNotes` / `Todo` 同一個形狀;JSON schema 由 OpenVINO 的
# 結構化輸出硬性約束(2026-10-11 實測:明講「不要用 JSON」照樣只吐 JSON)
_QUOTED = {"type": "object",
           "properties": {"text": {"type": "string"}, "quote": {"type": "string"}},
           "required": ["text", "quote"]}
#: 不要待辦的那一版(`lite`,同手機版 `ChunkNotesLite`)
LITE_SCHEMA = {
    "type": "object",
    "properties": {
        "topic": {"type": "string"},
        "points": {"type": "array", "items": _QUOTED, "maxItems": 3},
    },
    "required": ["topic", "points"],
}
NOTES_SCHEMA = {
    "type": "object",
    "properties": {
        **LITE_SCHEMA["properties"],
        "todo": {"anyOf": [
            {"type": "null"},
            {"type": "object",
             "properties": {"task": {"type": "string"}, "quote": {"type": "string"}},
             "required": ["task", "quote"]},
        ]},
    },
    "required": ["topic", "points", "todo"],
}
# 同 apple_asr.swift 那幾個 @Guide 的字;跟著會議語言換(`draftvocab.notes_format`),這個名字是繁中那份
NOTES_FORMAT = draftvocab.ZH_TW.notes_format


def _notes_format(msg: dict) -> str:
    """`llm-notes` 接在說明後面的那段欄位說明,照指令帶來的會議語言(沒帶就是繁中:舊的父端);
    `lite` 時是不含待辦的那一版。"""
    v = draftvocab.of(msg.get("language") or "")
    return v.lite_format if msg.get("lite") else v.notes_format
MAX_NEW_TOKENS = 700
REPETITION_PENALTY = 1.05
#: 格式錯重問那一次的取樣(同手機版 `retryOptions`):固定種子,同一版程式照樣每次一樣
RETRY_TOP_K, RETRY_SEED, RETRY_TEMPERATURE = 40, 7, 0.3
# Qwen3 系列預設會先「想」;關掉的模板會留一對空的 think 標籤,保險起見再剝一次
_THINK = re.compile(r"<think>.*?</think>", re.S)
#: MLX 的 repetition penalty 往回看幾個 token。⚠️ **必須涵蓋整段提示**:`mlx-lm` 的預設 20
#: 只罰最近 20 個 token,等於沒開——2026-10-14 同一場 43 分鐘的會(一段 900 字),20 時待辦只剩
#: 1 條、重點長到合併三層被截短;4096 時 12 條、合併 1 層,跟 OpenVINO 同級(OpenVINO 的
#: penalty 本來就罰整段,選型數字是在那個語意下量的)。2026-10-28 段長改 1800 字(≈ 2,700 token)
#: 再加前文約 400 字與說明、回覆,4096 不夠涵蓋 → 8192
MLX_REPETITION_CONTEXT = 8192
# MLX 沒有結構化輸出的硬性約束:回覆裡第一個 `{` 到最後一個 `}` 當成那個物件
# (實測 16 段都只回一個 JSON 物件,但偶爾會包在 ```json 圍欄裡)
_JSON_OBJECT = re.compile(r"\{.*\}", re.S)


def _parse_notes(out: str, strict: bool) -> dict:
    """`llm-notes` 的回覆 → 協定的形狀。`strict`:OpenVINO 有 schema 約束,整串就是 JSON。"""
    if not strict:
        m = _JSON_OBJECT.search(out)
        out = m.group(0) if m else ""
    try:
        got = json.loads(out)
    except ValueError:
        return {"ok": False, "error": "decodingFailure", "detail": "回覆不是 JSON"}
    if not isinstance(got, dict):
        return {"ok": False, "error": "decodingFailure", "detail": "回覆不是物件"}
    # 欄位齊不齊由父端看(`minutes._valid_notes`):少了就重問,兩條路(這裡與 Apple 的小程式)同一套
    return {"ok": True, "topic": got.get("topic"), "points": got.get("points"), "todo": got.get("todo")}


def _reply(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _prompt(tok, instructions: str, prompt: str) -> str:
    msgs = [{"role": "system", "content": instructions}, {"role": "user", "content": prompt}]
    return tok.apply_chat_template(msgs, add_generation_prompt=True,
                                   extra_context={"enable_thinking": False})


#: 換行隔開的相鄰字串常值(`"甲 "` 換行 `"乙"`):jinja2 把它們接成一串,openvino_genai 內建的 minja
#: 解析不了,而且它**一律先解析整份模板**,走不到的分支也一樣
_ADJACENT_LITERALS = (re.compile(r'"((?:[^"\\\n]|\\.)*)"\s*\n\s*"'),
                      re.compile(r"'((?:[^'\\\n]|\\.)*)'\s*\n\s*'"))


def _join_adjacent_literals(tpl: str) -> str:
    """把換行隔開的相鄰字串常值接成一個(jinja2 本來就當成同一串,畫出來的字不變)。"""
    for pat, quote in zip(_ADJACENT_LITERALS, ('"', "'")):
        while True:
            joined = pat.sub(lambda m, q=quote: q + m.group(1), tpl)
            if joined == tpl:
                break
            tpl = joined
    return tpl


def _ensure_template(tok) -> None:
    """模板要解得開才往下走:解不開就把相鄰字串接起來再試一次;還是不行就讓載入失敗(`ready:false`)。

    ⚠️ **是預防,不是修過的病**:Gemma 4 E4B 釘住的那一版(`models.GEMMA_OV_REVISION`)工具呼叫那段的
    `raise_exception(...)` 用了相鄰字串,openvino_genai 2026.4.1 拿 `Tokenizer(資料夾)` 載的話每一次
    `apply_chat_template` 都丟 `Expected closing parenthesis in call args`;但這裡用的
    `VLMPipeline(...).get_tokenizer()` 載入時**自己就把那三行接成一行**,所以現在這一版走不到改寫
    (2026-10-30 Windows 實測,見 `docs/dev/minutes.md`)。留著是怕換版本或改走 `LLMPipeline` 後它不再代接:
    不擋的話是每段重問 4 次全失敗、連 3 段就整場卡住。先試一次再改:解得開的模板一個字都不動。"""
    try:
        _prompt(tok, "x", "y")
        return
    except RuntimeError:
        pass
    tok.set_chat_template(_join_adjacent_literals(tok.chat_template))
    _prompt(tok, "x", "y")
    # stderr 進記錄檔(父端 `minutes_ov._drain`);stdout 是 NDJSON 專用,不可以寫在那裡
    sys.stderr.write("聊天模板的相鄰字串已接成一串(minja 解析不了)\n")


def answer(pipe, tok, g, msg: dict, free: bool = False) -> dict:
    """問一次。拆出來是為了測試能換上假的 pipeline。

    `free`:不帶結構化輸出(Gemma,見 `_free_format`),欄位說明前面接 `json_only`、從回覆裡撈
    `{…}`——同 MLX 那條。"""
    notes = msg["kind"] == "llm-notes"
    cfg = g.GenerationConfig()
    cfg.max_new_tokens = MAX_NEW_TOKENS
    cfg.do_sample = False
    if notes and msg.get("retry"):
        cfg.do_sample = True
        cfg.top_k, cfg.rng_seed, cfg.temperature = RETRY_TOP_K, RETRY_SEED, RETRY_TEMPERATURE
    # 2026-10-11 選型實測就是帶著這個值量的。同一份 43 分鐘的逐字稿、4B 內顯:帶著它
    # 待辦 12 條、合併 1 層;拿掉它只剩 4 條,重點也長到合併要三層、還被截短
    cfg.repetition_penalty = REPETITION_PENALTY
    cfg.apply_chat_template = False   # 模板上面自己套了(要關掉 thinking)
    hint = ""
    if notes and free:
        hint = draftvocab.of(msg.get("language") or "").json_only + _notes_format(msg)
    elif notes:
        hint = _notes_format(msg)
        schema = LITE_SCHEMA if msg.get("lite") else NOTES_SCHEMA
        cfg.structured_output_config = g.StructuredOutputConfig(json_schema=json.dumps(schema))
    text = _prompt(tok, msg["instructions"] + hint, msg["prompt"])
    out = pipe.generate(text, images=[], generation_config=cfg).texts[0]
    out = _without_channel(_THINK.sub("", out)).strip()
    if not notes:
        return {"ok": True, "text": out}
    return _parse_notes(out, strict=not free)


def _without_channel(s: str) -> str:
    """Gemma 4 的思考區塊「<|channel>thought …<channel|>」:只留後面的正文,沒收尾的整段不要
    (同 `minutes.without_thinking`;子行程不 import minutes,這裡留一份最小的)。"""
    a = s.find("<|channel>")
    if a < 0:
        return s
    b = s.find("<channel|>", a)
    return s[:a] if b < 0 else s[:a] + s[b + len("<channel|>"):]


def _free_format(model_dir: str) -> bool:
    """這顆要不要拿掉結構化輸出:Gemma 要(2026-10-28 W1:schema 的 `todo` 可為 null 時,17 段
    交 0 條;拿掉後 16 條,§5.4b),Qwen 不要(帶不帶都一樣,而帶著格式一定解得開)。看
    `config.json` 的 `model_type`(Gemma 4 E4B 是 `gemma4`)。"""
    try:
        cfg = json.loads((Path(model_dir) / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return str(cfg.get("model_type", "")).startswith("gemma")


def answer_mlx(model, tok, mlx_lm, msg: dict, seed=None) -> dict:
    """Mac(MLX)問一次。提示、上限、penalty 值都同 OpenVINO 那條,差在 penalty 的範圍要
    自己放大(`MLX_REPETITION_CONTEXT`)、格式沒有硬性約束。拆出來是為了測試換上假模型
    (`mlx_lm` 是傳進來的模組:Windows 上沒有 MLX,測試照樣跑得到這一支)。`seed` 是
    `mlx.core.random.seed`(`retry` 時設種子;取樣器本身不收種子)。"""
    su = mlx_lm.sample_utils
    notes = msg["kind"] == "llm-notes"
    retry = notes and msg.get("retry")
    if retry and seed is not None:
        seed(RETRY_SEED)
    # ⚠️ 不能共用 `_prompt`:那是 OpenVINO tokenizer 的參數形狀(`extra_context`),
    # HF 的模板參數直接當關鍵字傳——傳錯不會報錯,只會讓 Qwen 先「想」一大段再回答
    # ⚠️ 沒有格式約束:要明講「只回一個 JSON」,不然 Qwen 用文字回答(W2 模擬 7 段全解不開)。
    # 這一句**不可以**放進 OpenVINO 那條:帶著 schema 時多這一句,待辦就一條都不交(`draftvocab` 檔頭)
    hint = draftvocab.of(msg.get("language") or "").json_only + _notes_format(msg) if notes else ""
    msgs = [{"role": "system", "content": msg["instructions"] + hint},
            {"role": "user", "content": msg["prompt"]}]
    text = tok.apply_chat_template(msgs, add_generation_prompt=True, tokenize=False,
                                   enable_thinking=False)
    out = mlx_lm.generate(
        model, tok, text, max_tokens=MAX_NEW_TOKENS, verbose=False,
        # 平常不隨機(同 OpenVINO 的 do_sample=False);重問那次用固定種子取樣
        sampler=(su.make_sampler(temp=RETRY_TEMPERATURE, top_k=RETRY_TOP_K) if retry
                 else su.make_sampler(temp=0.0)),
        logits_processors=su.make_logits_processors(
            repetition_penalty=REPETITION_PENALTY,
            repetition_context_size=MLX_REPETITION_CONTEXT))
    # Qwen 的 think 標籤、Gemma 4 的 channel 思考區塊(手機版 10-17 Mac 實測 Gemma 偶爾先想一段)
    out = _without_channel(_THINK.sub("", out)).strip()
    if not notes:
        return {"ok": True, "text": out}
    return _parse_notes(out, strict=False)


def _load(model_dir: str, device: str):
    """載模型,回「問一次」的函式。⚠️ import 與載入都在這裡:失敗時父端看例外型別名決定
    叫人重裝元件還是重新下載(`minutes._generate_session`)。"""
    if device == "MLX":
        import contextlib

        import mlx_lm
        import mlx_lm.sample_utils  # noqa: F401 - `answer_mlx` 從模組屬性取用

        # ⚠️ stdout 是 NDJSON 專用通道:套件載入時印的任何一行都會被父端當成壞掉的 JSON
        with contextlib.redirect_stdout(sys.stderr):
            model, tok = mlx_lm.load(model_dir)

        import mlx.core as mx

        def ask(msg: dict) -> dict:
            with contextlib.redirect_stdout(sys.stderr):
                return answer_mlx(model, tok, mlx_lm, msg, seed=mx.random.seed)
        return ask
    import openvino_genai as g

    pipe = g.VLMPipeline(model_dir, device)
    tok = pipe.get_tokenizer()
    _ensure_template(tok)
    free = _free_format(model_dir)
    return lambda msg: answer(pipe, tok, g, msg, free=free)


def main(argv: list[str] | None = None) -> int:
    stdio.force_utf8()   # 必須在讀第一行之前:逐字稿是中文
    argv = sys.argv[1:] if argv is None else argv
    plat.adopt_low_priority_from_argv(argv)
    model_dir, device = argv[0], argv[1]
    t0 = time.monotonic()
    try:
        # ⚠️ import 與載模型分開失敗:前者是環境(缺 Visual C++),後者多半是模型檔——
        # 父端看錯誤的型別名決定叫人裝元件還是重新下載(`minutes.generate`)
        ask = _load(model_dir, device)
    except Exception as e:
        _reply({"ready": False, "error": f"{type(e).__name__}: {e}"})
        return 1
    _reply({"ready": True, "load_sec": round(time.monotonic() - t0, 1)})
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            _reply(ask(json.loads(raw)))
        except Exception as e:
            # ⚠️ 只回錯誤種類與例外型別:訊息裡可能夾著逐字稿原文(同 minutes 記錄檔那條)
            _reply({"ok": False, "error": "other", "detail": type(e).__name__})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
