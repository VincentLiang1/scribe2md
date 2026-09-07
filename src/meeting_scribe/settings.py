r"""使用者偏好的落地(%LOCALAPPDATA%\meeting-scribe\settings.json)。

目前只裝一項:「只用 CPU」(見 `transcribe.cpu_only`)。⚠️ **它與「收音情境」
那種每次開都回到預設的選擇刻意不同**(使用者 2026-09-07 選定「記住」):
「這台電腦的 GPU 比 CPU 還慢」是**機器屬性**,不是這一場會議的選擇——每次
重開都要重設一次的話,忘了設的那一趟就是白等一輪,而舊機器上那個差距是
以倍計的。

⚠️ **不放「這一趟」的選擇**(模型、講者人數、CPU 核心數、收音情境):那幾個
是每一份檔案各自的決定,記住反而會讓下一次悄悄沿用上一次的設定——使用者
2026-08-09 就是為此指定拿掉「記住上次選擇」。要往這裡加東西之前,先問「它
描述的是這台電腦,還是這一份工作」。

⚠️ **讀寫失敗一律當成「沒有這份設定」**:偏好是輔助功能,唯讀的資料夾、被
資安軟體鎖住的檔案、手動編壞的 JSON,都不該讓工具起不來或中途炸掉。同
`pending.persist` 的態度。

⚠️ **這份檔案不進 repo、也不進交付包**:它是這台電腦的事(同
`%LOCALAPPDATA%` 底下的模型與錄音),`data/` 那幾個才是跟著程式走的。
"""
import json
import logging
import os
from pathlib import Path

from meeting_scribe import paths

logger = logging.getLogger(__name__)

# 這一版的鍵。⚠️ 讀到不認得的鍵一律原樣保留(見 `set`):舊版工具讀到新版寫的
# 檔案時,不該把還不認得的設定清掉——使用者換回新版就會發現設定不見了。
KEY_CPU_ONLY = "cpu_only"


def store_file() -> Path:
    r"""設定檔的位置。**是函式不是常數**:測試 monkeypatch 這個名字把落地
    隔離到 tmp(同 `attendees.store_file` 那條慣例),而 `appdata_root` 本身
    每次重讀環境變數。"""
    return paths.appdata_root() / "settings.json"


def load() -> dict:
    """讀出整份設定;檔案不存在、讀不動、或內容不是物件都回空的 dict。"""
    try:
        raw = json.loads(store_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        logger.warning("設定檔讀不出來,這一次當成沒有設定", exc_info=True)
        return {}
    return raw if isinstance(raw, dict) else {}


def get(key: str, default=None):
    """讀一項設定。"""
    return load().get(key, default)


def set(key: str, value) -> None:  # noqa: A001 - 與 get 成對,名字要對稱
    r"""寫一項設定(其餘的鍵原樣保留)。失敗只記 log。

    ⚠️ **先寫暫存檔再 `os.replace`**:直接覆寫的話,寫到一半斷電或被防毒
    中斷會留下一個「半份」JSON,而那比沒有設定更糟——下次啟動讀到壞檔,
    使用者只會看到設定莫名其妙自己變回預設。`os.replace` 在同一個磁碟區
    上是原子的。"""
    data = load()
    data[key] = value
    path = store_file()
    tmp = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        logger.warning("設定寫不進去(%s),這一次的變更不會被記住", path,
                       exc_info=True)
        try:
            tmp.unlink()
        except OSError:
            pass
