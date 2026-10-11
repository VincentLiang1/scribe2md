"""與會人員名單:設定講者名稱時的下拉選單來源。

介面上那一欄叫「**與會人員名稱維護**」(2026-08-09 定名;程式與文件內部
一律沿用「名單 / attendees」這個詞,那是資料的名字,不是畫面上的字)。

純名字清單(一行一個),存在專案 data/ 子目錄(隨程式碼版控/複製)。
介面可新增/修改/刪除;命名講者時輸入新名字會自動加入。與聲紋庫分開——
名單是「可能出席者」,聲紋庫是「名字↔聲紋」;命名時兩邊都會補上該名字。

⚠️ **啟用「聲紋同步」之後(2026-10-09 起,`vpsync.py`)**,名單的每一個改動都寫成這台
自己的紀錄事件(`attendee_add` / `attendee_remove`),名單是所有裝置的紀錄重組出來的,
`data/attendees.txt` 只是快取。紀錄格式**沒有「排順序」這種事件**,而這份名單的順序是
使用者照部門排的——所以改名、調順序時,從第一個不一樣的位置往後那幾位一律寫成「先移除
再加回」(`_order_events`),三台重組出來的順序就跟他排的一樣(使用者 2026-10-10 同意
這個做法:不動跨專案格式,代價是事件多幾筆)。⚠️ 啟用同步時**用記事本直接改 txt 不會
同步**,下一次重組就被蓋回去,要在工具裡的名單框改。
"""

import logging
from pathlib import Path

from meeting_scribe import errors, models, vpsync


def store_file() -> Path:
    return models.data_dir() / "attendees.txt"


def load() -> list[str]:
    """回傳名單(去重、依加入順序保留、去除空白行)。啟用同步時回重組結果(順手寫回 txt 快取)。"""
    lib = vpsync.library()
    if lib is not None:
        _write_cache(lib)
        return list(lib.attendees)
    return _load_txt()


_cached: dict = {"lib": None}


def _write_cache(lib) -> None:
    """重組結果寫回 `data/attendees.txt`(同一份就不重寫)。失敗只記 log。"""
    if _cached["lib"] is lib:
        return
    try:
        _write_txt(lib.attendees)
        _cached["lib"] = lib
    except Exception:
        logging.getLogger(__name__).warning(
            "聲紋同步:名單寫不回 %s(只影響快取)", store_file(), exc_info=True)


def _sync_lib():
    """啟用同步時回重組結果;沒啟用回 None;啟用了卻讀不到資料夾就擋下(同 `voiceprints._sync_lib`)。"""
    if not vpsync.enabled():
        return None
    lib = vpsync.library()
    if lib is None:
        raise errors.UserFacingError(
            "找不到聲紋同步資料夾,名單先不改(同步資料夾回來之後再試一次)。"
            "要改用這台自己的名單,先在「聲紋資料管理」停用同步。")
    return lib


def _order_events(current: list[str], target: list[str]) -> list[dict]:
    """把重組結果從 `current` 變成 `target` 要寫的名單事件(順序也要對)。

    先移除不在 `target` 的;剩下的與 `target` 比,**共同的開頭**不動,從第一個不一樣的位置
    往後一律「先移除(還在的話)再加回」——`attendee_add` 只會加在尾端,這樣加完的順序就是
    `target` 的順序。"""
    keep = set(target)
    out = [{"op": "attendee_remove", "name": n} for n in current if n not in keep]
    rest = [n for n in current if n in keep]
    k = 0
    while k < len(rest) and k < len(target) and rest[k] == target[k]:
        k += 1
    left = set(rest[k:])
    for n in target[k:]:
        if n in left:
            out.append({"op": "attendee_remove", "name": n})
        out.append({"op": "attendee_add", "name": n})
    return out


def _load_txt() -> list[str]:
    """讀 `data/attendees.txt`(沒啟用同步時的真值、啟用時的快取)。

    ⚠️ **用 utf-8-sig 讀**:這個檔的預期用法就包含「用記事本直接編」
    (見 `roster.orphan_names`),而任何一個以「UTF-8 with BOM」存檔的
    編輯器都會在**第一行**前面留下 `\\ufeff`。純 utf-8 讀出來的第一個人
    因此變成另一個 key:畫面上兩個名字一模一樣,聲紋卻對不上(摘要開始亮
    「只在聲紋庫、不在名單上」),而使用者從下拉挑那個帶 BOM 的名字套用,
    就在聲紋庫裡建立了第三個身分。utf-8-sig 對沒有 BOM 的檔完全無害。"""
    f = store_file()
    if not f.exists():
        return []
    out: list[str] = []
    seen: set[str] = set()
    for line in f.read_text(encoding="utf-8-sig").splitlines():
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
    lib = _sync_lib()
    if lib is not None:
        events = _order_events(list(lib.attendees), out)
        if events:
            vpsync.append(events)
            load()
        return
    _write_txt(out)


def _write_txt(out) -> None:
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
    if vpsync.enabled():
        # ⚠️ 不走 save_all:套用命名時會叫到這裡,同步資料夾暫時不在也要記得住(同
        # `voiceprints.enroll`),加在尾端本來就不需要知道別台的狀態
        vpsync.append([{"op": "attendee_add", "name": name}])
        load()
        return True
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
