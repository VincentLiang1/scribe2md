"""meeting-scribe(對使用者顯示的名稱是「AI 文件.MD 轉換器」):地端的逐字稿與文件轉檔工具。

匯入本套件時即關閉 OpenVINO 遙測——否則 Intel GPU 加速引擎(OpenVINO)在
consent 檔不存在時,會對 www.google-analytics.com 送出用量統計 ping
(不含音檔/逐字稿內容,但仍是對外連線)。官方 opt-out 是把 consent 檔寫成
"0";在任何子模組載入 OpenVINO 之前先寫好,兌現「全程不外連」。純本地寫入、
best-effort,失敗不影響轉檔。
"""

import os
import tempfile
from pathlib import Path

# gradio/HF 遙測在任何 import gradio 之前關閉(spec §7):UI 相關模組
# (app/ui_style/data_tabs)都會 import gradio,開關放在套件根保證任何
# import 順序都先經過這裡(app.py 開頭另留一份,保 `python app.py` 直跑
# 不經套件 __init__ 的情況;setdefault 冪等)。
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
# gradio 供應快取改指本行程專屬目錄(隱私:對外供應的檔案——下載區逐字稿、
# 試聽片段——會被 gradio「複製」進這裡,預設位置 %TEMP%\gradio 整機共用且
# gradio 從不清理,機敏副本會永遠堆著)。此處只講「在這裡設定」的三個理由:
# 必須在 import gradio 之前(gradio import 時就讀取)、前綴必須等同
# pipeline.TMP_PREFIX(啟動清掃靠它認孤兒;不直接 import pipeline——套件根
# 要保持輕量,一致性由測試守著)、帶 pid 讓多實例不互踩。
# 存活鎖與正常退場清理的完整說明見 app._hold_serve_cache。
os.environ.setdefault(
    "GRADIO_TEMP_DIR",
    str(Path(tempfile.gettempdir()) / f"meeting-scribe-serve-{os.getpid()}"),
)


def _disable_openvino_telemetry() -> None:
    # OpenVINO consent 檔(Windows):%LOCALAPPDATA%\Intel Corporation\openvino_telemetry
    # 內容 "0" = 拒絕(不送任何遙測)、"1" = 同意。不存在才會觸發 GA ping。
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return  # 非 Windows / 無此環境變數:本工具僅支援 Windows,略過
    consent = Path(base) / "Intel Corporation" / "openvino_telemetry"
    try:
        if consent.exists() and consent.read_text(encoding="utf-8", errors="ignore").strip() == "0":
            return  # 已是拒絕狀態,不重複寫
        consent.parent.mkdir(parents=True, exist_ok=True)
        consent.write_text("0", encoding="utf-8")
    except Exception:
        pass  # best-effort:寫入失敗不影響轉檔


_disable_openvino_telemetry()
