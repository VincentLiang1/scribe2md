r"""會議紀錄草稿的語言模型子行程(Qwen3.5;父端是 `minutes_ov`)。Windows 是 OpenVINO
(裝置參數 `CPU`/`GPU`),Mac 是 MLX(裝置參數 `MLX`,跑在 GPU;2026-10-14 起)。

跟 `transworker` 同一套規矩:stdin 收 NDJSON、stdout **只准** NDJSON、優先權由父端降
(below-normal,「轉換時電腦還能用」)。模型與裝置是啟動參數:一次 `minutes.generate`
起一支、載一次、問完收掉——每問一次重載要 3~15 秒,整場要問二十次上下。

指令:
  啟動:-m meeting_scribe.minutes_worker <模型資料夾> <CPU|GPU|MLX>
      → {"ready":true,"load_sec":5.5}   或 {"ready":false,"error":"…"}
  {"kind":"llm-notes"|"llm-ask","instructions":"…","prompt":"…"}
      → {"ok":true,"points":[…],"todo":{…}|null}   /   {"ok":true,"text":"…"}
      → {"ok":false,"error":"decodingFailure"|"other","detail":"…"}
  EOF → 退出

⚠️ **回覆格式跟 Apple 那支小程式(`apple_asr.swift` 的 `llm-*`)一樣**,`minutes.py`
只管切段、核對、合併,不管是誰回答的。`@Guide` 的描述在這裡寫成提示詞的一段
(Foundation Models 會自動把它放進提示,這邊得自己放)。
"""
import json
import re
import sys
import time

from meeting_scribe import plat, stdio

# 跟 apple_asr.swift 的 `ChunkNotes` / `Todo` 同一個形狀;JSON schema 由 OpenVINO 的
# 結構化輸出硬性約束(2026-10-11 實測:明講「不要用 JSON」照樣只吐 JSON)
NOTES_SCHEMA = {
    "type": "object",
    "properties": {
        "points": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
        "todo": {"anyOf": [
            {"type": "null"},
            {"type": "object",
             "properties": {"task": {"type": "string"}, "quote": {"type": "string"}},
             "required": ["task", "quote"]},
        ]},
    },
    "required": ["points", "todo"],
}
# 同 apple_asr.swift 那幾個 @Guide 的字
NOTES_FORMAT = (
    "\n\n只回一個 JSON 物件,欄位:\n"
    "- points:這一段討論的重點,每點一句,最多 4 點\n"
    "- todo:這一段最明確的一件交辦或有人承諾要做的事;沒有就填 null,不要湊數。"
    "有的話是 {\"task\": 要做的事,一句話, \"quote\": 逐字稿裡作為依據的那一句,一字不改照抄,10 到 40 字}"
)
MAX_NEW_TOKENS = 700
REPETITION_PENALTY = 1.05
# Qwen3 系列預設會先「想」;關掉的模板會留一對空的 think 標籤,保險起見再剝一次
_THINK = re.compile(r"<think>.*?</think>", re.S)
#: MLX 的 repetition penalty 往回看幾個 token。⚠️ **必須涵蓋整段提示**(一段逐字稿 900 字
#: ≈ 1,400 token,加說明與回覆):`mlx-lm` 的預設 20 只罰最近 20 個 token,等於沒開——
#: 2026-10-14 同一場 43 分鐘的會,20 時待辦只剩 1 條、重點長到合併三層被截短;4096 時 12 條、
#: 合併 1 層,跟 OpenVINO 同級(OpenVINO 的 penalty 本來就罰整段,選型數字是在那個語意下量的)
MLX_REPETITION_CONTEXT = 4096
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
    return {"ok": True, "points": got.get("points") or [], "todo": got.get("todo")}


def _reply(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _prompt(tok, instructions: str, prompt: str) -> str:
    msgs = [{"role": "system", "content": instructions}, {"role": "user", "content": prompt}]
    return tok.apply_chat_template(msgs, add_generation_prompt=True,
                                   extra_context={"enable_thinking": False})


def answer(pipe, tok, g, msg: dict) -> dict:
    """問一次。拆出來是為了測試能換上假的 pipeline。"""
    notes = msg["kind"] == "llm-notes"
    cfg = g.GenerationConfig()
    cfg.max_new_tokens = MAX_NEW_TOKENS
    cfg.do_sample = False
    # 2026-10-11 選型實測就是帶著這個值量的。同一份 43 分鐘的逐字稿、4B 內顯:帶著它
    # 待辦 12 條、合併 1 層;拿掉它只剩 4 條,重點也長到合併要三層、還被截短
    cfg.repetition_penalty = REPETITION_PENALTY
    cfg.apply_chat_template = False   # 模板上面自己套了(要關掉 thinking)
    if notes:
        cfg.structured_output_config = g.StructuredOutputConfig(json_schema=json.dumps(NOTES_SCHEMA))
    text = _prompt(tok, msg["instructions"] + (NOTES_FORMAT if notes else ""), msg["prompt"])
    out = _THINK.sub("", pipe.generate(text, images=[], generation_config=cfg).texts[0]).strip()
    if not notes:
        return {"ok": True, "text": out}
    return _parse_notes(out, strict=True)


def answer_mlx(model, tok, mlx_lm, msg: dict) -> dict:
    """Mac(MLX)問一次。提示、上限、penalty 值都同 OpenVINO 那條,差在 penalty 的範圍要
    自己放大(`MLX_REPETITION_CONTEXT`)、格式沒有硬性約束。拆出來是為了測試換上假模型
    (`mlx_lm` 是傳進來的模組:Windows 上沒有 MLX,測試照樣跑得到這一支)。"""
    su = mlx_lm.sample_utils
    notes = msg["kind"] == "llm-notes"
    # ⚠️ 不能共用 `_prompt`:那是 OpenVINO tokenizer 的參數形狀(`extra_context`),
    # HF 的模板參數直接當關鍵字傳——傳錯不會報錯,只會讓 Qwen 先「想」一大段再回答
    msgs = [{"role": "system", "content": msg["instructions"] + (NOTES_FORMAT if notes else "")},
            {"role": "user", "content": msg["prompt"]}]
    text = tok.apply_chat_template(msgs, add_generation_prompt=True, tokenize=False,
                                   enable_thinking=False)
    out = mlx_lm.generate(
        model, tok, text, max_tokens=MAX_NEW_TOKENS, verbose=False,
        sampler=su.make_sampler(temp=0.0),   # 同 OpenVINO 的 do_sample=False
        logits_processors=su.make_logits_processors(
            repetition_penalty=REPETITION_PENALTY,
            repetition_context_size=MLX_REPETITION_CONTEXT))
    out = _THINK.sub("", out).strip()
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

        def ask(msg: dict) -> dict:
            with contextlib.redirect_stdout(sys.stderr):
                return answer_mlx(model, tok, mlx_lm, msg)
        return ask
    import openvino_genai as g

    pipe = g.VLMPipeline(model_dir, device)
    tok = pipe.get_tokenizer()
    return lambda msg: answer(pipe, tok, g, msg)


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
