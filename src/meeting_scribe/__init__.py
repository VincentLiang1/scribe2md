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


# 本機一律不經代理的主機名(httpx / requests 都認 NO_PROXY 這三個寫法)
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def _bypass_proxy_for_localhost() -> None:
    """把 localhost 排除在系統代理之外——公司電腦不這樣做會連不上自己。

    gradio 的 `launch()` 收尾有一道自我健檢:用 httpx 打自己一槍
    `http://127.0.0.1:<port>/gradio_api/startup-events`(blocks.py)。而 httpx
    預設 `trust_env=True` → `urllib.request.getproxies()`,**那在 Windows 上
    會讀登錄檔的系統/IE 代理設定**(`HKCU\\...\\Internet Settings`);偏偏 httpx
    只認 `NO_PROXY`,**完全不理會**代理設定裡「近端網址不使用 Proxy 伺服器」
    那個勾(`ProxyOverride` 的 `<local>`)。於是打給 127.0.0.1 的請求被送去
    公司代理,代理不幫你連別人的 localhost、回 **403**,gradio 當場拋例外、
    整個工具啟動失敗(2026-08-12 同仁的公司電腦實際回報;開發機沒有代理設定,
    所以這條路永遠測不出來)。所以我們自己補上那個勾的效果。

    ⚠️ **不能只設 `NO_PROXY` 了事**:`getproxies()` 是
    `getproxies_environment() or getproxies_registry()`——只要環境變數裡出現
    **任何一個** `*_proxy`,登錄檔就整個不讀了。公司電腦要靠代理才連得出去,
    只設 `NO_PROXY` 會把首次下載 AI 模型(2-3 GB)一起弄死,而症狀會變成
    「模型下載失敗」這個看不出真因的樣子。所以先把登錄檔讀到的代理**明確
    搬進環境變數**,再排除本機:本機自檢直連、對外下載照走公司代理,兩邊都保住。

    純本機的環境變數操作,不連任何網路。
    """
    from urllib.request import getproxies

    proxies = getproxies()  # 環境變數優先,其次(Windows)登錄檔
    for scheme in ("http", "https"):
        url = proxies.get(scheme)
        # 已由使用者/IT 明設的不覆蓋,只補登錄檔那一份
        if url and not os.environ.get(f"{scheme.upper()}_PROXY"):
            os.environ[f"{scheme.upper()}_PROXY"] = url
    # 保留原本的例外清單(公司可能已列了內網主機),只補上缺的本機寫法
    listed = [h.strip() for h in os.environ.get("NO_PROXY", "").split(",") if h.strip()]
    lowered = {h.lower() for h in listed}
    os.environ["NO_PROXY"] = ",".join(listed + [h for h in _LOCAL_HOSTS if h not in lowered])


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


_bypass_proxy_for_localhost()
_trust_bundled_certificates()
_disable_openvino_telemetry()
