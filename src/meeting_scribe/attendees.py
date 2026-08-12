"""與會人員名單:設定講者名稱時的下拉選單來源。

介面上那一欄叫「**與會人員名稱維護**」(2026-08-09 定名;程式與文件內部
一律沿用「名單 / attendees」這個詞,那是資料的名字,不是畫面上的字)。

純名字清單(一行一個),存在專案 data/ 子目錄(隨程式碼版控/複製)。
介面可新增/修改/刪除;命名講者時輸入新名字會自動加入。與聲紋庫分開——
名單是「可能出席者」,聲紋庫是「名字↔聲紋」;命名時兩邊都會補上該名字。
"""

from pathlib import Path

from meeting_scribe import models


def store_file() -> Path:
    return models.data_dir() / "attendees.txt"


def load() -> list[str]:
    """回傳名單(去重、依加入順序保留、去除空白行)。"""
    f = store_file()
    if not f.exists():
        return []
    out: list[str] = []
    seen: set[str] = set()
    for line in f.read_text(encoding="utf-8").splitlines():
        n = line.strip()
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def save_all(names) -> None:
    """以給定清單整批取代名單(供表格編輯後儲存);去重、去空白。"""
    out: list[str] = []
    seen: set[str] = set()
    for n in names or []:
        n = (n or "").strip() if isinstance(n, str) else str(n).strip()
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    f = store_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")


def add(name: str) -> bool:
    """加入一個名字(已存在則不動);回傳是否有新增。"""
    name = (name or "").strip()
    if not name:
        return False
    names = load()
    if name in names:
        return False
    save_all(names + [name])
    return True


def remove(name: str) -> None:
    name = (name or "").strip()
    names = load()
    if name in names:
        save_all([n for n in names if n != name])


def rename(old: str, new: str) -> bool:
    """名單裡的舊名字換成新名字;回傳是否有動到。

    **就地換掉、不搬到最後**:名單順序是使用者自己排的(load 保留加入
    順序),改個字就把人跳到清單尾巴,下次他得重新找一遍。新名字已經在
    名單裡時,舊的直接移除(save_all 本來就會去重,這裡明寫是為了讓
    「合併」這件事在程式碼裡看得出來)。"""
    old, new = (old or "").strip(), (new or "").strip()
    if not old or not new or old == new:
        return False
    names = load()
    if old not in names:
        return False
    renamed = [new if n == old else n for n in names]
    save_all(renamed)   # 去重交給它:新名字原本就在的話,兩列會合成一列
    return True
