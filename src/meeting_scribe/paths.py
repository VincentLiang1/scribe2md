r"""使用者資料落地路徑的單一出處。

%LOCALAPPDATA%\meeting-scribe 底下的所有落地——AI 模型快取(models)、
命名進度(pending)、錄音工作目錄(recordings)——都從
appdata_root() 出發:基底路徑邏輯只此一份,要盤點「工具在使用者機器上
寫了什麼」看這裡即可。專案 data/(隨 repo 版控)不在此列,見
models.data_dir。
"""
import os
from pathlib import Path


def repo_root() -> Path:
    r"""專案根目錄(output\、data\、docs\、logs\ 都掛在它底下)。

    src/meeting_scribe/paths.py → 往上兩層。editable 安裝(本專案的交付
    方式)直接跑 src,這條成立;真打成 wheel 裝進 site-packages 就不成立,
    而那正是要一處改、不是四處各自壞掉的理由。"""
    return Path(__file__).resolve().parents[2]


def assets_dir() -> Path:
    r"""套件自帶的靜態資產(程式圖示;隨 wheel 與交付副本走)。

    ⚠️ **不可以走 repo_root()**:那條在真打成 wheel 時不成立(見上),而圖示
    要跟著套件本身移動。內容由 `scripts/make_icon.py` 產生,不是手寫的。"""
    return Path(__file__).resolve().parent / "assets"


def appdata_root() -> Path:
    r"""%LOCALAPPDATA%\meeting-scribe;無 LOCALAPPDATA 的環境退回 ~/.cache。

    每次呼叫重讀環境變數:測試以 monkeypatch.setenv 隔離,不能在 import
    時定死。"""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".cache")
    return Path(base) / "meeting-scribe"
