"""測試隔離的四個模式(範本,**不要整份複製**)。

挑你需要的貼進 `tests/conftest.py`。每一個模式都對應一種真實的災情:
洩漏的全域狀態會造成**假綠燈或假紅燈**,而症狀通常出現在**別的測試**上,
極難回推來源。

四個模式:
  1. 全域 logging 狀態(以及會落地的紀錄檔)
  2. 模組層快取(模型物件 / 路徑 memo / 連線池)
  3. 真實硬體探測
  4. 使用者的真實資料檔

⚠️ 共同原則:**測試絕不碰使用者的真實資料,也不探測真實硬體**。
前者的代價是「跑個測試把我的資料改了」;後者的代價是「在你的機器上綠,
在我的機器上紅」,而那會慢慢訓練出「紅了先重跑一次」的習慣。
"""
import logging

import pytest


# ---------------------------------------------------------------------------
# 模式 1:紀錄檔 + logging 全域狀態
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def _log_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("logs")


@pytest.fixture(autouse=True)
def _isolate_logging(_log_dir, monkeypatch):
    r"""紀錄檔一律導到 tmp,且測試後把 logging 的全域狀態還原。

    沒有這道隔離會有兩個後果:
    (1) 每跑一次測試就在原始碼樹裡長出 `logs\` 目錄;
    (2) handler 與被改過的等級洩漏給**後面所有測試**——logging 是行程級
        全域狀態,洩漏後的症狀是別的測試莫名其妙收到 DEBUG 或收不到訊息。
    """
    from your_package import filelog  # ← 換成你的模組

    monkeypatch.setenv(filelog.LOG_DIR_ENV, str(_log_dir))
    root = logging.getLogger()
    ours = logging.getLogger("your_package")
    handlers = [(h, h.level) for h in root.handlers]
    root_level, ours_level = root.level, ours.level
    yield
    filelog.detach()
    root.handlers[:] = [h for h, _ in handlers]
    for h, lvl in handlers:
        h.setLevel(lvl)
    root.setLevel(root_level)
    ours.setLevel(ours_level)


# ---------------------------------------------------------------------------
# 模式 2:模組層快取
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_module_caches(monkeypatch):
    """模組層快取(模型物件 / 路徑 memo / 連線池)不得跨測試洩漏。

    測試各自 monkeypatch 環境變數與假物件,洩漏會造成假綠燈或假紅燈。

    ⚠️ 這條跟「惰性載入」是一組的:如果模組層用
    `模組名 = None` 佔位 + `_ensure_x()` 首次使用才回填,
    測試換上假貨之後**一定**要在這裡清掉,否則下一條測試拿到的是上一條的假貨。
    """
    from your_package import engine, models  # ← 換成你的模組

    monkeypatch.setattr(models, "_cache", None)
    for name in ("heavy_lib", "another_lib"):
        monkeypatch.setattr(engine, name, None, raising=False)


# ---------------------------------------------------------------------------
# 模式 3:不探測真實硬體
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _no_hardware_probing(monkeypatch):
    """有沒有 GPU、幾顆核心——一律給固定答案。

    不隔離的話,同一份測試在不同機器上會走不同分支:
    症狀是「在你那邊是綠的」,而那種紅綠不定會慢慢訓練出「先重跑一次」的習慣。
    """
    from your_package import device  # ← 換成你的模組

    monkeypatch.setattr(device, "predicted_device", lambda: "cpu")
    monkeypatch.setattr(device, "cpu_count", lambda: 4)


# ---------------------------------------------------------------------------
# 模式 4:使用者的真實資料檔
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolate_user_data(tmp_path, monkeypatch):
    """把資料檔的位置整個導到 tmp,**絕不動真實資料**。

    來源專案的資料檔是使用者累積了幾個月的東西(而且是二進位、看不出被改過)
    ——那種東西弄壞了沒有回頭路,所以這條是 autouse,不給任何測試選擇的機會。

    ⚠️ 導向要做在**最上游**(路徑函式或環境變數),不要在每個測試各自 patch:
    漏掉一條就是真的寫到使用者的檔案上,而且那條測試看起來完全正常。
    """
    from your_package import paths  # ← 換成你的模組

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path / "data")
