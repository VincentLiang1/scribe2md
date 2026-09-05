"""meeting-scribe(對使用者顯示的名稱是「AI 文件.MD 轉換器」):地端的逐字稿與文件轉檔工具。

匯入本套件時即關閉 OpenVINO 遙測——否則 Intel GPU 加速引擎(OpenVINO)在
consent 檔不存在時,會對 www.google-analytics.com 送出用量統計 ping
(不含音檔/逐字稿內容,但仍是對外連線)。官方 opt-out 是把 consent 檔寫成
"0";在任何子模組載入 OpenVINO 之前先寫好,兌現「全程不外連」。純本地寫入、
best-effort,失敗不影響轉檔。
"""

import os
from pathlib import Path

# HF 遙測在任何 import 之前關閉(spec §7:對外遙測全部關掉)。開關放在套件根,
# 保證不管誰先被 import 都會先經過這裡;`setdefault` 冪等。
# ⚠️ **2026-09-05 少了 `GRADIO_ANALYTICS_ENABLED` 那一行**:gradio 連同它那套介面
# 一起移除了,那個變數已經沒有對象。隱私那條規格本身沒有放寬——它現在只剩
# huggingface_hub 這一個會對外說話的相依。
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
# oneDNN 的 OpenCL 探測失敗訊息一律不印(2026-08-30 使用者把黑視窗的內容貼過來
# 才發現)。⚠️ **這不是錯誤**:OpenVINO 照樣在 Intel Arc iGPU 上跑完轉錄(實測有無
# 這個變數,結果與速度都一樣),那十幾行是 oneDNN 自己另外探一次 OpenCL 引擎、
# 失敗後的抱怨(`no opencl gpu device is available` / `CL_INVALID_OPERATION`)。
# ⚠️ **為什麼非關不可**:黑視窗是我們叫非技術同仁「看得出原因」時打開的地方
# (`疑難排解\啟動DEBUG.bat`),而那整片英文 error 會把真正的訊息淹掉,還會讓人
# 以為 GPU 壞了。oneDNN 3.4 起預設就會印 error 級的訊息,要 `none` 才全關。
# ⚠️ **`setdefault`**:自己要查 oneDNN 時,外面設 `ONEDNN_VERBOSE=all` 仍然蓋得過。
# ⚠️ 必須在**任何** import openvino 之前——同上面兩個遙測開關的理由。
os.environ.setdefault("ONEDNN_VERBOSE", "none")
def _trust_bundled_certificates() -> None:
    r"""把 certifi 的公開根憑證**疊加**到 Python 的驗證清單上——不這樣做,
    有些公司電腦一下載 AI 模型就死在憑證驗證,而瀏覽器明明開得了同一個網址。

    Python 在 Windows 上驗 https 是把「Windows 憑證存放區」裡的根憑證 dump
    出來比對,而 Windows 的根憑證是**用到才補**的(Automatic Root Certificates
    Update):瀏覽器經 SChannel 連線時會即時下載那張根,Python 卻只看得到
    已經躺在存放區裡的那幾十張(開發機實測 58 張,certifi 有 150+)。於是同
    一台電腦「Chrome 開得了 github,Python 報 unable to get local issuer
    certificate」——2026-08-20 另一台電腦實際回報,症狀是重設講者按下去
    只剩手動命名。公司若用群組原則關掉根憑證自動更新,那台會**永遠**補不到。

    ⚠️ **是疊加不是取代**(開發機實測 61 → 154 張):`load_default_certs()`
    先載 Windows 存放區、再吃 `SSL_CERT_FILE`,所以公司自己派下來的憑證不會
    因為這一行失效——對本來就正常的電腦零風險,這也是它敢無條件執行的理由。
    ⚠️ **IT 或使用者自己設過就不覆蓋**:那個值通常正是公司的 CA 包。

    純本機的環境變數操作,不連任何網路。另一種壞法(公司代理做 TLS 攔截、
    憑證由公司自己的 CA 簽)certifi 救不了,由 models.with_tls_rescue 在
    真的撞到時改用作業系統原生驗證兜底。
    """
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        return
    try:
        import certifi
    except Exception:
        return  # best-effort:沒有 certifi 就維持 Python 預設,不影響啟動
    os.environ["SSL_CERT_FILE"] = certifi.where()


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


_trust_bundled_certificates()
_disable_openvino_telemetry()


# ---- Windows 桌面外殼的共用層(2026-08-29,原生介面遷移的階段 3)----
# ⚠️ **綁在套件根**:任何子模組被 import 都會先經過這裡,所以「忘了 bind」不會
# 發生在正常的執行路徑上——連那支跑在安裝當下的 `make_shortcut.py` 也一樣
# (它走 `from meeting_scribe.brand import …`,而匯入子模組必先匯入父套件)。
# ⚠️ **位置要我們自己算,不可以讓 winkit 用它的 `__file__` 推**:那幾支模組住在
# 下游的時候「我在哪」是 `Path(__file__).parents[2]`,搬進共用包之後那條會指到
# winkit 自己——紀錄檔寫進 `winkit\logs`、版本號讀成 winkit 的 `.git`、皮膚資產
# 找不到,而**三個症狀都沒有錯誤訊息**。
# ⚠️ `repo_root` 也不可以從 `package_dir` 往上推:本專案是 src layout(往上兩層),
# 姊妹專案 NotebookLM_OCR 是 flat layout(一層),推的那個版本會在一邊安靜地算錯。
import winkit  # noqa: E402

from meeting_scribe import brand  # noqa: E402

winkit.bind(brand,
            package_dir=Path(__file__).resolve().parent,
            repo_root=Path(__file__).resolve().parents[2])
