r"""聲紋庫與與會名單的「三台同步」(手機、Windows、Mac;跨專案契約,`docs/spec/02` §2.9)。

使用者 2026-10-09 定案:**每台裝置各寫自己的變更紀錄,放在共用的雲端資料夾(OneDrive),
先不加密**。三台用同一套規則把所有紀錄重組成同一個聲紋庫與名單。規則的**參考實作**在
手機版 repo(`scribe2md-ios`,scripts 底下的 `voiceprint_journal.py`),這裡的 `parse` /
`materialize` 照它逐條實作,`tests/test_vpsync.py` 拿它產的標準答案
(`tests/fixtures/journal_oracle.json`)逐位元比對。

⚠️ **改格式或重組規則 = 兩個 repo 一起改**(手機版的參考實作、Swift 實作、這裡)。單邊改
的症狀是「同一個人在手機認得、在電腦認不得」或「刪掉的錯樣本在某一台復活」,**沒有任何
錯誤訊息**。

⚠️ **這個模組不准 import 任何 UI 模組**(同 `naming.py`),也不 import `voiceprints` /
`attendees`(它們 import 這裡):要什麼由呼叫端傳進來。

落地(本機這一台):
- 自己的紀錄**本尊在本機**(`local_dir()`,`%LOCALAPPDATA%\meeting-scribe\voiceprint-sync`),
  同步資料夾裡那份是**發佈出去的副本**。理由:OneDrive 沒掛上、資料夾被改名、別台誤刪了
  我的檔——那一刻的新樣本都還在本機,資料夾回來時再發佈一次就好(`_publish`)。
- 裝置編號與同步資料夾記在 `settings.json`(描述的是**這台電腦**,同「只用 CPU」)。

⚠️ **紀錄含真名與聲紋:絕不進任何 repo、也絕不能在送出資料夾(`_WikiBox`)底下**——
wiki-feeder 會把那裡的檔攝入 FWIKI。選資料夾時由 `check_folder` 擋。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from meeting_scribe import paths, plat, settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 契約(兩個 repo 共用)
FORMAT = "scribe2md-voiceprint-journal"
VERSION = 1
# 聲紋模型識別字串(`models.EMBEDDING_URL` 的檔名;`test_vpsync` 釘住兩者一致)。⚠️ 換模型
# 而這裡沒跟著換,寫出去的檔頭就會謊報模型,別台拿著別顆模型的向量比對——「看起來正常但全錯」。
MODEL_ID = "3dspeaker_speech_eres2netv2_sv_zh-cn_16k-common"
DIM = 192
# 這兩個數字現在也是**跨專案契約**的一部分(重組規則 6):三台各自淘汰、各自去重複,數字
# 不一樣就重組出不一樣的庫。與 `voiceprints._MAX_SAMPLES_PER_NAME` / `_DUPLICATE_SIM` 同值,
# `test_vpsync` 守著。
MAX_SAMPLES_PER_NAME = 8
DUPLICATE_SIM = 0.999

# 本機設定的鍵(`settings.json`)
KEY_DEVICE_ID = "vp_sync_device"
KEY_FOLDER = "vp_sync_folder"

# 送出資料夾的名字(手機版 08 §8.9)。電腦版沒有「送出資料夾」這項設定,所以只能認名字。
SEND_FOLDER_NAME = "_wikibox"

_LOCK = threading.RLock()


# ================================================================ 純邏輯:讀與重組


def parse(text: str):
    """一份紀錄檔 → (檔頭, [事件])。讀不懂的行略過(最後一行可能還在同步中)。

    第一行不是檔頭回 `(None, [])`;檔頭不對(格式、版本、模型、維度)回 `(檔頭, None)`,
    由呼叫端整份拒收並講原因。⚠️ 與參考實作逐條相同,包含「第一行不是檔頭的檔**不算拒收**、
    只是什麼都不貢獻」這一點(改它要兩邊一起改)。"""
    rows = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    if not rows or not isinstance(rows[0], dict) or rows[0].get("op") != "header":
        return None, []
    head = rows[0]
    if (head.get("format") != FORMAT or head.get("version") != VERSION
            or head.get("model") != MODEL_ID or head.get("dim") != DIM):
        return head, None
    dev = str(head.get("device", ""))
    events = []
    for r in rows[1:]:
        if not isinstance(r, dict) or "op" not in r or "at" not in r or "seq" not in r:
            continue
        # ⚠️ 參考實作在這裡直接 `int(seq)`,壞的 seq 會讓整次重組丟例外;這裡改成略過那一行
        # (讀檔不准把例外放出去)。只影響壞檔,正常的檔兩邊結果相同。
        if isinstance(r["seq"], bool) or not isinstance(r["seq"], int):
            continue
        events.append({**r, "device": dev})
    return head, events


@dataclass
class Library:
    """重組結果。`sids` / `names` / `vecs` 平行(就是聲紋庫,參考實作的 names/vecs 再多帶 sid)。

    `live` 是規則 1~5 套完、**去重複與淘汰之前**的樣本 {sid: 名字}:刪人時要連被藏起來的
    那幾筆一起刪,否則刪掉看得見的那筆,被去重複藏起來的另一台副本就浮上來(見
    `voiceprints.delete`)。`seen` 是**出現過的每一個**樣本向量(含已刪除的),`touched` 是
    名單事件碰過的名字——兩個都給「起家」判斷「這筆別台早就知道了」用(`bootstrap_events`)。"""

    sids: list = field(default_factory=list)
    names: list = field(default_factory=list)
    vecs: np.ndarray = field(default_factory=lambda: np.zeros((0, DIM), dtype=np.float32))
    attendees: list = field(default_factory=list)
    rejected: list = field(default_factory=list)          # [(裝置名稱, 原因)]
    live: dict = field(default_factory=dict)
    live_vecs: dict = field(default_factory=dict)
    seen: np.ndarray = field(default_factory=lambda: np.zeros((0, DIM), dtype=np.float32))
    touched: set = field(default_factory=set)
    devices: list = field(default_factory=list)           # [(裝置名稱, 裝置編號, 最後一筆的 at)]


def _label(head) -> str:
    head = head or {}
    return str(head.get("label") or head.get("device") or "?")


def _reject_reason(head) -> str:
    if head.get("model") != MODEL_ID or head.get("dim") != DIM:
        return "用的是別的聲紋模型"
    return "紀錄格式的版本不認得"


def materialize(texts) -> Library:
    """多份紀錄檔的文字 → `Library`。

    規則(`docs/spec/02` §2.9「重組規則」,**三台必須逐條相同**):
    1. 全部事件依 (at, device, seq) 排序後依序套用
    2. sample:加入;同一個 sid 第二次出現略過
    3. delete:該 sid 標成刪除,之後再出現也不加回來
    4. rename:當下名叫 from 的全部改成 to
    5. 名單:attendee_add 加在尾端(已在就不動)、attendee_remove 移除
    6. 全部套完之後:同名相似度 ≥ 0.999 只留最早那筆;每名字上限 8 筆,淘汰「最像的那一對裡較舊的」
    """
    events, lib = [], Library()
    for t in texts:
        head, ev = parse(t)
        if ev is None:
            lib.rejected.append((_label(head), _reject_reason(head)))
            continue
        if head is not None:
            last = max((str(e["at"]) for e in ev), default="")
            lib.devices.append((_label(head), str(head.get("device", "")), last))
        events += ev
    events.sort(key=lambda e: (str(e["at"]), e["device"], int(e["seq"])))
    order: list[str] = []          # sid,依加入順序(舊的在前)
    samples: dict[str, list] = {}  # sid -> [name, vec]
    deleted: set[str] = set()
    attendees: list[str] = []
    seen: list = []
    for e in events:
        op = e["op"]
        if op == "sample":
            sid = str(e.get("sid", ""))
            vec = e.get("vec")
            name = str(e.get("name", "")).strip()
            if not isinstance(vec, list) or len(vec) != DIM:
                continue
            try:
                arr = np.asarray(vec, dtype=np.float32)
            except (TypeError, ValueError):
                continue                    # 向量裡混了不是數字的東西:這一筆不收
            seen.append(arr)
            if not sid or sid in samples or sid in deleted or not name:
                continue
            samples[sid] = [name, arr]
            order.append(sid)
        elif op == "delete":
            sid = str(e.get("sid", ""))
            deleted.add(sid)
            if sid in samples:
                del samples[sid]
                order.remove(sid)
        elif op == "rename":
            src, dst = str(e.get("from", "")).strip(), str(e.get("to", "")).strip()
            if src and dst:
                for s in samples.values():
                    if s[0] == src:
                        s[0] = dst
        elif op == "attendee_add":
            n = str(e.get("name", "")).strip()
            if n:
                lib.touched.add(n)
            if n and n not in attendees:
                attendees.append(n)
        elif op == "attendee_remove":
            n = str(e.get("name", "")).strip()
            if n:
                lib.touched.add(n)
            if n in attendees:
                attendees.remove(n)
    lib.live = {s: samples[s][0] for s in order}
    lib.live_vecs = {s: samples[s][1] for s in order}
    sids = list(order)
    names = [samples[s][0] for s in order]
    rows = [samples[s][1] for s in order]
    _dedupe(names, rows, sids)
    for n in list(dict.fromkeys(names)):
        trim_to_cap(names, rows, n, sids)
    lib.sids, lib.names = sids, names
    lib.vecs = np.array(rows, dtype=np.float32).reshape(-1, DIM)
    lib.attendees = attendees
    lib.seen = np.array(seen, dtype=np.float32).reshape(-1, DIM)
    return lib


def _dedupe(names, rows, sids) -> None:
    """同名、相似度 ≥ DUPLICATE_SIM 的樣本只留最早那筆(兩台從同一份舊庫起家時,同一筆會出現兩次)。"""
    i = 0
    while i < len(names):
        j = i + 1
        while j < len(names):
            if names[j] == names[i] and float(rows[i] @ rows[j]) >= DUPLICATE_SIM:
                del names[j]
                del rows[j]
                del sids[j]
            else:
                j += 1
        i += 1


def trim_to_cap(names, rows, name, sids=None) -> None:
    """同名超過上限時淘汰「最像的那一對裡較舊的」(= `voiceprints._trim_to_cap`,多帶 sid)。"""
    while True:
        idx = [i for i, n in enumerate(names) if n == name]
        if len(idx) <= MAX_SAMPLES_PER_NAME:
            return
        mat = np.array([rows[i] for i in idx], dtype=np.float32)
        sims = mat @ mat.T
        np.fill_diagonal(sims, -np.inf)
        a, b = np.unravel_index(int(np.argmax(sims)), sims.shape)
        drop = idx[min(a, b)]
        del names[drop]
        del rows[drop]
        if sids is not None:
            del sids[drop]


# ================================================================ 本機設定與落地


def device_id() -> str:
    """這一台的裝置編號(第一次問時產生,存在本機設定)。"""
    with _LOCK:
        dev = settings.get(KEY_DEVICE_ID)
        if isinstance(dev, str) and dev:
            return dev
        dev = str(uuid.uuid4())
        settings.set(KEY_DEVICE_ID, dev)
        return dev


def device_label() -> str:
    """給人看的裝置名稱(檔頭的 `label`;別台的畫面上顯示的就是這個)。"""
    return {"windows": "Windows", "macos": "Mac"}.get(plat.name(), "Linux")


def folder() -> Path | None:
    """使用者選的同步資料夾;沒啟用回 None。"""
    raw = settings.get(KEY_FOLDER)
    return Path(raw) if isinstance(raw, str) and raw else None


def enabled() -> bool:
    return folder() is not None


def local_dir() -> Path:
    r"""自己那份紀錄的本尊所在(`%LOCALAPPDATA%\meeting-scribe\voiceprint-sync`)。

    **是函式不是常數**:測試 monkeypatch 它把落地隔離到 tmp(同 `settings.store_file`)。"""
    return paths.appdata_root() / "voiceprint-sync"


def journal_name(dev: str) -> str:
    return f"journal-{dev}.jsonl"


def own_file() -> Path:
    return local_dir() / journal_name(device_id())


def _now_at() -> str:
    t = datetime.now(timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def check_folder(path) -> str | None:
    """這個資料夾能不能當同步資料夾;不行回一句給使用者看的話,可以回 None。

    ⚠️ 擋三種:不存在/寫不進去、**在送出資料夾(`_WikiBox`)裡或包著它**(wiki-feeder 會把
    聲紋與真名攝入 FWIKI)、**在 git repo 裡**(紀錄會被提交出去;這台的 data/ 就在 repo 裡)。"""
    try:
        p = Path(path).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return "這個路徑讀不懂,請重新選一次。"
    if not p.is_dir():
        return f"找不到這個資料夾:{p}"
    if any(part.casefold() == SEND_FOLDER_NAME for part in p.parts):
        return ("不能選送出資料夾(_WikiBox)或它底下的資料夾:那裡的檔會被送進知識庫,"
                "聲紋與真名就跟著進去了。請在 OneDrive 另開一個資料夾。")
    try:
        if (p / "_WikiBox").exists() or any(
                c.is_dir() and c.name.casefold() == SEND_FOLDER_NAME for c in p.iterdir()):
            return ("這個資料夾裡面就是送出資料夾(_WikiBox),兩者不能互相包含。"
                    "請在 OneDrive 另開一個專用的資料夾。")
    except OSError:
        pass
    for up in (p, *p.parents):
        if (up / ".git").exists():
            return ("這個資料夾在程式碼的版本控管(git)底下,紀錄會被一起提交出去。"
                    "請改選 OneDrive 裡的資料夾。")
    probe = p / f".write-test-{uuid.uuid4().hex}"
    try:
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError:
        return f"這個資料夾寫不進去:{p}"
    return None


def _write_atomic(path: Path, text: str) -> None:
    """整份先寫成「.」開頭的暫存檔再改名(雲端不會同步到寫一半的檔;別台讀 `journal-*` 也
    不會讀到它)。⚠️ Windows 上 OneDrive 正在上傳時改名可能被擋一下,重試幾次。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name)
    tmp.write_text(text, encoding="utf-8", newline="\n")
    last = None
    for _ in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:   # pragma: no cover - 只有 Windows 的 OneDrive 會撞到
            last = exc
            threading.Event().wait(0.2)
    try:
        tmp.unlink()
    except OSError:
        pass
    raise last  # type: ignore[misc]


def _read_text(path: Path) -> str:
    # errors="replace":最後一行可能停在一個多位元組字的中間(還在同步中),那一行本來就會被略過
    return path.read_text(encoding="utf-8", errors="replace")


def _header() -> dict:
    return {"op": "header", "format": FORMAT, "version": VERSION, "model": MODEL_ID,
            "dim": DIM, "device": device_id(), "label": device_label()}


def append(events) -> None:
    """把這一台的新事件寫進自己的紀錄(本機本尊 → 發佈到同步資料夾)。

    每次都**重讀本機那份再整份重寫**,不在記憶體留一份:這台的 repo 版與 `/Applications`
    那顆 App 共用同一個落地(同一個裝置編號),記憶體裡各留一份就會互相蓋掉對方的事件。
    `seq` 接著檔裡最大的往下編;`at` 不早於檔裡最後一筆(時鐘往回撥時,自己的事件順序不能亂)。

    ⚠️ 寫本機失敗就讓例外出去(呼叫端是使用者按下的動作,要讓他知道沒存到);發佈到同步
    資料夾失敗只記 log,下次 `library()` 會再試(`_publish`)。
    沒有事件、自己的紀錄也還不存在時,寫一份只有檔頭的(起家時別台已經什麼都有了,這台
    照樣要在同步資料夾裡露面,別台的「上次同步」才看得到它)。"""
    events = [e for e in events if e]
    if not events and own_file().exists():
        return
    with _LOCK:
        f = own_file()
        lines: list[str] = []
        seq, last_at = -1, ""
        if f.exists():
            head, old = parse(_read_text(f))
            if old:
                seq = max(int(e["seq"]) for e in old)
                last_at = max(str(e["at"]) for e in old)
            lines = [ln for ln in _read_text(f).split("\n") if ln.strip()]
            # 檔頭壞了(不該發生)就重寫一個,事件照留:那是這台唯一的本尊
            if not lines or head is None or old is None:
                lines = [json.dumps(_header(), ensure_ascii=False)] + lines[1:]
        else:
            lines = [json.dumps(_header(), ensure_ascii=False)]
        at = max(_now_at(), last_at)
        for e in events:
            seq += 1
            lines.append(json.dumps({**e, "at": at, "seq": seq}, ensure_ascii=False))
        _write_atomic(f, "\n".join(lines) + "\n")
        _publish()
        _forget()


def _publish() -> None:
    """把本機那份發佈到同步資料夾(大小不一樣才寫)。失敗只記 log。"""
    d, f = folder(), own_file()
    if d is None or not f.exists() or not d.is_dir():
        return
    dest = d / f.name
    try:
        if dest.exists() and dest.stat().st_size == f.stat().st_size:
            return
        _write_atomic(dest, _read_text(f))
    except OSError:
        logger.warning("聲紋同步:發佈到同步資料夾失敗(%s),下次再試", dest, exc_info=True)


# ================================================================ 讀:有快取的重組

_cache: dict = {"key": None, "lib": None}


def _forget() -> None:
    _cache["key"] = None
    _cache["lib"] = None


def _sources() -> list[Path] | None:
    """要重組的紀錄檔:同步資料夾裡別台的 + 本機自己的。資料夾找不到回 None。"""
    d = folder()
    if d is None or not d.is_dir():
        return None
    mine = journal_name(device_id())
    out = [p for p in sorted(d.glob("journal-*.jsonl")) if p.name != mine]
    own = own_file()
    if own.exists():
        out.append(own)
    return out


def library() -> Library | None:
    """目前的重組結果;沒啟用同步、或同步資料夾找不到時回 None(呼叫端退回本機快取)。

    ⚠️ **不讓例外出去**(同 `voiceprints.load`:它在建介面的當下被呼叫)。
    ⚠️ **檔案沒變就不重讀**:`load()` 一場會議要被叫好幾次,每次都解析整個資料夾太貴
    (Windows 的「檔案隨選」下讀檔還可能觸發下載)。判斷變了沒用 (路徑, 修改時間, 大小)。"""
    with _LOCK:
        try:
            if not enabled():
                return None
            _publish()
            srcs = _sources()
            if srcs is None:
                return None
            key = []
            for p in srcs:
                try:
                    st = p.stat()
                    key.append((str(p), st.st_mtime_ns, st.st_size))
                except OSError:
                    continue
            key = tuple(key)
            if _cache["key"] == key and _cache["lib"] is not None:
                return _cache["lib"]
            texts = []
            for p in srcs:
                try:
                    texts.append(_read_text(p))
                except OSError:
                    logger.warning("聲紋同步:讀不到 %s,這一次先略過", p, exc_info=True)
            lib = materialize(texts)
            for label, why in lib.rejected:
                logger.warning("聲紋同步:「%s」的紀錄%s,整份沒收", label, why)
            _cache["key"], _cache["lib"] = key, lib
            return lib
        except Exception:
            logger.exception("聲紋同步:重組失敗,這一次退回本機的聲紋庫")
            return None


# ================================================================ 起家與啟用


def bootstrap_events(lib: Library, names, vecs, attendees) -> list[dict]:
    """把這台現有的聲紋庫與名單寫成事件——**只寫別台還不知道的**。

    規格(§2.9「起家」)寫的是「每一筆都寫,規則 6 的去重複會收掉」;這裡多擋兩種:
    - **任何紀錄裡出現過的向量(相似度 ≥ 0.999,不分名字、含已刪除的)不再寫**。這台的舊庫
      可能是舊的:別台已經刪掉的錯樣本、改過名字的人,照寫的話會以新的 sid、舊的名字復活
      ——去重複只收「同名」的,收不到這兩種。
    - **名單事件碰過的名字不再加**:別台已經移除的人,不能被這台的舊名單加回去。
    純粹是寫的一端少寫,重組規則一條沒動,三台的結果仍然一致。"""
    out: list[dict] = []
    seen = lib.seen
    for name, vec in zip(names, vecs):
        name = str(name).strip()
        v = np.asarray(vec, dtype=np.float32).reshape(-1)
        if not name or v.shape[0] != DIM:
            continue
        if len(seen) and float(np.max(seen @ v)) >= DUPLICATE_SIM:
            continue
        out.append({"op": "sample", "sid": str(uuid.uuid4()), "name": name,
                    "vec": v.tolist()})
        seen = np.vstack([seen, v[None, :]])
    for n in attendees:
        n = str(n).strip()
        if n and n not in lib.touched:
            out.append({"op": "attendee_add", "name": n})
    return out


def enable(path, names, vecs, attendees) -> tuple[str | None, int]:
    """啟用(或換)同步資料夾,並把這台現有的庫起家進去。回 (錯誤訊息或 None, 寫了幾筆)。

    ⚠️ **每次啟用都跑起家**,不只第一次:停用期間在這台改過的(新記的聲紋、名單加的人)
    要在重新啟用時補進來;已經在紀錄裡的由 `bootstrap_events` 自己略過,跑幾次都一樣。"""
    why = check_folder(path)
    if why:
        return why, 0
    with _LOCK:
        before = settings.get(KEY_FOLDER)
        settings.set(KEY_FOLDER, str(Path(path).expanduser().resolve()))
        if folder() is None:
            return "設定寫不進去,同步沒有啟用(詳見記錄檔)。", 0
        _forget()
        # ⚠️ 失敗的每一條路都要把設定還原:回一句「沒有啟用」而設定還留著的話,畫面說沒啟用、
        # 程式卻已經改讀那個資料夾
        try:
            lib = library()
            if lib is None:
                settings.set(KEY_FOLDER, before)
                _forget()
                return "同步資料夾讀不到,同步沒有啟用(詳見記錄檔)。", 0
            events = bootstrap_events(lib, names, vecs, attendees)
            append(events)
        except OSError:
            settings.set(KEY_FOLDER, before)
            _forget()
            logger.exception("聲紋同步:起家時寫不進這台的紀錄,同步沒有啟用")
            return "這台的同步紀錄寫不進去,同步沒有啟用(詳見記錄檔)。", 0
        return None, len(events)


def disable() -> None:
    """停用同步。自己的紀錄留在本機(重新啟用時接著寫),同步資料夾裡那份也不動。"""
    with _LOCK:
        settings.set(KEY_FOLDER, None)
        _forget()


def _local_time(at: str) -> str:
    try:
        t = datetime.strptime(at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return at
    return t.astimezone().strftime("%m/%d %H:%M")


def status_text() -> str:
    """畫面上那幾行:上次同步(每一台最後一筆的時間)、整份沒收的那幾台。"""
    d = folder()
    if d is None:
        return ("還沒啟用。選一個 OneDrive 裡的資料夾,手機、Windows、Mac 就會共用同一份"
                "聲紋庫與與會名單。")
    if not d.is_dir():
        return f"⚠ 找不到同步資料夾({d});先用這台上次的聲紋庫,資料夾回來之後會自動補上。"
    lib = library()
    if lib is None:
        return "⚠ 同步資料夾讀不出來,先用這台上次的聲紋庫(詳見記錄檔)。"
    mine = device_id()
    others = [(label, _local_time(at)) for label, dev, at in lib.devices
              if dev != mine and at]
    lines = []
    if others:
        lines.append("上次同步:" + "、".join(f"{label} {t}" for label, t in others))
    else:
        lines.append("同步資料夾裡還沒有其他裝置的紀錄。")
    for label, why in lib.rejected:
        lines.append(f"⚠ 「{label}」的紀錄{why},那一份整份沒收(避免認錯人)。")
    return "\n".join(lines)
