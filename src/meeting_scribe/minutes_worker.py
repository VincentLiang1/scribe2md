r"""會議紀錄草稿的語言模型子行程(Windows;OpenVINO + Qwen3.5,父端是 `minutes_ov`)。

跟 `transworker` 同一套規矩:stdin 收 NDJSON、stdout **只准** NDJSON、優先權由父端降
(below-normal,「轉換時電腦還能用」)。模型與裝置是啟動參數:一次 `minutes.generate`
起一支、載一次、問完收掉——每問一次重載要 3~15 秒,整場要問二十次上下。

指令:
  啟動:-m meeting_scribe.minutes_worker <模型資料夾> <CPU|GPU>
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
    try:
        got = json.loads(out)
    except ValueError:
        return {"ok": False, "error": "decodingFailure", "detail": "回覆不是 JSON"}
    if not isinstance(got, dict):
        return {"ok": False, "error": "decodingFailure", "detail": "回覆不是物件"}
    return {"ok": True, "points": got.get("points") or [], "todo": got.get("todo")}


def main(argv: list[str] | None = None) -> int:
    stdio.force_utf8()   # 必須在讀第一行之前:逐字稿是中文
    argv = sys.argv[1:] if argv is None else argv
    plat.adopt_low_priority_from_argv(argv)
    model_dir, device = argv[0], argv[1]
    t0 = time.monotonic()
    try:
        # ⚠️ import 與載模型分開失敗:前者是環境(缺 Visual C++),後者多半是模型檔——
        # 父端看錯誤的型別名決定叫人裝元件還是重新下載(`minutes.generate`)
        import openvino_genai as g

        pipe = g.VLMPipeline(model_dir, device)
        tok = pipe.get_tokenizer()
    except Exception as e:
        _reply({"ready": False, "error": f"{type(e).__name__}: {e}"})
        return 1
    _reply({"ready": True, "load_sec": round(time.monotonic() - t0, 1)})
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            _reply(answer(pipe, tok, g, json.loads(raw)))
        except Exception as e:
            # ⚠️ 只回錯誤種類與例外型別:訊息裡可能夾著逐字稿原文(同 minutes 記錄檔那條)
            _reply({"ok": False, "error": "other", "detail": type(e).__name__})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
