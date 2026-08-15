"""聲紋庫:記住「名字 ↔ 聲紋」,讓下次開會自動辨識重複出席的人。

使用者為某位講者命名時,把該講者的聲紋(原始 embedding 的質心)連同名字
存起來;下次轉檔後,拿新偵測到的每位講者聲紋比對聲紋庫,相似度夠高就
自動填入認出的名字,使用者只需修正認錯的(修正即更新聲紋,越用越準)。

誠實限制:跨會議(不同麥克風/房間/日期)聲紋會漂移,自動辨識會偶爾認錯
——故門檻取偏保守(寧可留白讓使用者填,也少亂填錯名),並靠「認錯再改」
的迴圈逐步累積樣本、提升準度。比對用「原始 embedding」(講者驗證的原生
空間),不用會議內分群的扣均值向量(那是各會議相對的,不跨會議通用)。

⚠️ **一整場會議要一起辨識(recognize_batch),不可逐位各自 recognize**
——認錯本身不可怕,可怕的是**認錯到看不出來**。2026-08-07 的實跡:
一場會議分出 5 群,其中兩群(開場的 B 總、00:07 才加入的董事長)都被
認成「C 董事長」,合起來看就像 B 總不存在;使用者確認命名後,
B 總的聲紋又被 enroll 進 C 名下,下一場錯得更牢。同一場會議裡
一個名字不可能是兩群人,這條約束由 recognize_batch 執行。
"""

import logging
from pathlib import Path
from typing import NamedTuple

import numpy as np

from meeting_scribe import models

logger = logging.getLogger(__name__)

# 跨會議聲紋辨識的 cosine 相似度門檻:高於此才自動填名。偏保守——
# 亂填錯名比留白更擾民(留白使用者補打即可,錯名要先發現再改)。
# 實測:原始 embedding 因共用麥克風/房間,同場不同講者相似度也可達 ~0.66,
# 故門檻取偏高;跨會議(不同場)基線較低,此值可依實際使用再調校。
_MATCH_THRESHOLD = 0.62
# 自動填名還要贏「第二個名字」這麼多分才算數(recognize_batch)。
# **過門檻不等於認出來**:原始 embedding 沒扣通道成分,同一場、同一支
# 麥克風錄的不同人天然有 0.65~0.85——一段聲紋同時對三個名字都 0.7 的時候,
# 第一名只是「剛好排在前面」,填上去就是一個會被相信的錯名字。
#
# ⚠️ **取 0.10 的理由是使用者的實際流程**(2026-08-08 他明確說明):
# 「自動識別的我會相信,不會特別去聽;**我只會去聽你沒有設定的講者**。」
# 這句話讓兩種錯誤的代價完全不對稱:
#   - **留白** → 他會去聽、會填,成本是多聽幾段
#   - **填錯** → 他不會查,錯名直接進逐字稿,而且套用時 `enroll` 會把
#     那段聲紋存進錯的人名下,下一場錯得更牢(無聲地自我複製)
# 所以這個門檻的目標不是「填得多」,是**誤認為零**。
#
# 校準數據(現有 144 個樣本 / 59 人的留一法,122 個可評樣本;重跑用
# `uv run python scripts/audit_voiceprints.py --margins`——單樣本的名字
# 會被排除,它們在留一法裡必錯)。過 0.62 門檻的有 90 對 / 32 錯:
#   margin  正確保留   誤認保留   精確率
#    0.00   90/90      32/32      73.8%
#    0.03   83/90      10/32      89.2%
#    0.05   76/90       5/32      93.8%
#    0.07   70/90       2/32      97.2%   ← 舊值
#    0.10   65/90       0/32     100.0%   ←取這個
#    0.12   60/90       0/32     100.0%
#    0.15   42/90       0/32     100.0%
# 0.07→0.10 用 5 個正確填名換掉最後 2 個誤認;再往上完全換不到東西
# (誤認已經是 0),只是白白多讓使用者填幾格——0.15 更要再賠 23 個。
#
# ⚠️ **留一法的 100% 不等於實際 100%**:它拿庫裡的樣本互相比,而實際是拿
# 「新會議的聲紋」比庫裡的,還要多跨一次場次漂移與通道差異(見
# _MISFILED_MARGIN 註解裡遠端與會者那段)。這個值買到的是「明顯更嚴」,
# 不是保證不出錯。
_RUNNER_UP_MARGIN = 0.10
# 「分不開」時最多列幾個名字給使用者參考(close_calls)。⚠️ **列一個是不
# 行的**:2026-08-15 拿 259 份執行紀錄回頭查,98 次留白**全部**卡在上面
# 那條 margin(不是「不夠像」),而其中 54% 的第一名與第二名只差 0.03 以
# 內——照上面那張校準表換算,那一段第一名只有 24% 是對的。單列第一名等於
# 給一個四次錯三次的答案,而使用者說過「自動識別的我會相信」。並列才誠實
# 傳達「這是待確認的名單,不是答案」。3 個是版面與資訊量的折衷
_MAX_RIVALS = 3
# 每個名字最多保留的聲紋樣本數:多樣本(不同場次)比對取最相似者更穩,
# 但限量避免無限膨脹、也讓過舊樣本自然淘汰。
#
# **2026-08-08 由 5 調到 8**(使用者決定)。留一法實測「上限越高認得越多、
# 而且沒有多認錯」:上限 2→34 對/1 錯、3→47/0、4→63/0、5→66/0(門檻 0.62、
# 次佳差距 0.10,139 樣本 / 59 人)。4→5 只多 3 個,增幅已在放緩。
#
# ⚠️ **真正的理由不是「認得更準」,是不要弄丟稀有的通道樣本**。淘汰挑的是
# 「最舊」,而最舊往往正是別場次、別麥克風、遠端連線錄的——那恰恰最有
# 價值。2026-08-08 的實跡:一次套用擠掉 7 個別場次的舊樣本,而擠進來的
# 有 16 個是同一場會議的複本(同一場多次辨識,每次 enroll 一份一模一樣的
# 質心);同一天差點因此弄丟 A 唯一一份遠端聲紋,而那個樣本一旦消失,
# 他遠端發言就再也認不出來。⚠️ 留一法**測不出**這個好處:它的查詢就是庫
# 裡的樣本本身,不含「新場地、新通道的發言」——上面那組數字只證明「多留
# 一點不會變差」,證明不了多樣性有多值錢。
#
# **也不是越大越好**:一場會議裡同一個人常被分成好幾群(2026-08-08 那場
# 月會,B 一個人分成 4 群,一次套用就 enroll 4 個)。上限開到 20,跑
# 五次月會就會被同一場的樣本佔滿,反而把跨場次的多樣性擠掉——跟現在要
# 解決的是同一個問題。8 容得下兩三場會議的份量,又不至於讓單場佔滿。
_MAX_SAMPLES_PER_NAME = 8
_DIM = 192  # 3D-Speaker eres2netv2 embedding 維度
# 登記時的一致性檢查:新樣本與「同一個名字既有樣本」的最高相似度低於此值,
# 就表示這個名字底下裝了兩個不同的人(過去認錯人又確認命名的痕跡)。
# **只記錄、不自動處置**:程式分不出「這次錯」還是「以前錯」,而 2026-08-07
# 的實例正好是以前錯的佔多數(C 名下 5 個裡 3 個是 B 總),任何多數決
# 都會反過來刪掉真的那兩個。清理要靠 scripts/audit_voiceprints.py 加上
# 外部對照(如某場會議已確認的講者)來判。
# 0.45 的來源:同一個人跨場次(不同麥克風/房間)實測仍有 0.46 以上
# (B 的 5 個樣本彼此最低 0.46);混進別人才會掉到 0.26~0.36。
_CONSISTENCY_SIM = 0.45
# 健檢:「這個樣本可能不是這個人的」的判準——它跟某個**別的**名字,比跟
# 自己的名字還像多少。要求「有去處」是關鍵:單看「跟自己人不像」會挑中
# **連接兩簇的橋**(實測:D #1 跟同名有 0.79,是好樣本卻被挑走)。
# 0.20 的來源:原始 embedding 沒扣通道成分,同一場、同一支麥克風錄的不同人
# 天然有 0.65~0.85,差距 0.1 上下多半是那個效應;2026-08-07 實測 138 個樣本
# 中 12 個過線,差距 +0.22~+0.49、下一名 +0.19,分佈有明顯斷層。
#
# ⚠️ **這條判準對「遠端與會者」會系統性誤判**(2026-08-08 實跡,刪錯過樣本)。
# 原始 embedding 沒扣通道成分,而遠端連線(電話/視訊)的通道特性很強:
#   - **同一個人**的現場樣本 vs 遠端樣本,可以低到 0.42
#   - **不同人**的遠端樣本彼此,因為共用通道可以高到 0.83
# 於是「跟別人比跟自己人還像」在遠端樣本上幾乎必然成立,判準會把它標成
# 存錯名字——而它其實是那個人唯一一份遠端聲紋,刪掉之後他遠端發言就再也
# 認不出來(當次:A 的遠端樣本被刪,下一場他那一段就填不出名字了,
# 而使用者是靠逐字稿內容才發現「上一版是對的」)。
#
# **判準是「跟誰像」的分佈,不是最高分那一個**:通道效應會讓它跟**所有**
# 遠端與會者一起偏高(實測 A #3:F 0.72、G 0.65、H 0.63,
# 而非遠端者平均只有 0.48);真的存錯人才會只對**某一個**名字特別突出
# (實測 C #4:E 0.85,而三位遠端者只有 0.57~0.65)。
# 健檢報告不做這個區分——所以**列出來的只是「待查」,不是「該刪」**,
# 判定前先看它跟同場其他遠端者的分佈,或者乾脆留著:留著頂多偶爾認錯
# (看得到、可以當場改),刪錯了那個人就再也認不出來。
_MISFILED_MARGIN = 0.20
# 健檢的第二道關卡:「最像的那個名字」要領先**第二個名字**這麼多,才算
# 「只像某一個人」。低於此代表它跟**一整群**人一起變像——那是通道的成分
# (共用麥克風、遠端連線),不是「這個樣本屬於某人」的證據。
#
# ⚠️ 這一關是 2026-08-08 用真實代價換來的:當天照著健檢刪掉三個樣本,
# 其中 A 那個其實是他**透過遠端連線**的唯一一份聲紋,刪掉之後他在
# 下一次辨識就整格消失,而症狀(「某某人沒有被分出來」)會把人誤導去查
# 分群,離真正的原因很遠。
#
# 0.08 的來源(當時聲紋庫 117 個可評樣本):
#   - 三個誤判的假陽性,孤例度只有 +0.02 ~ +0.04
#       A #3    像 F 0.72、次像 J 0.68  → 差 0.04
#       C #4    像 D 0.84、次像 E 0.83 → 差 0.02
#       H #3    像 G 0.83、次像 F 0.81  → 差 0.02
#   - 已知的真陽性(2026-08-07 K,三個樣本分屬三個不同的人):
#       像 L 0.87、次像 M 0.72 → 差 **0.15**
# 取兩者中間偏保守的 0.08。⚠️ **真陽性只有這一個資料點**,門檻是拿它跟
# 假陽性的分佈夾出來的,不是掃出來的;代價不對稱(刪錯 = 那個人再也認不
# 出來,留著 = 偶爾認錯而且看得見)所以寧可偏向「不標記」。
_LONE_MARGIN = 0.08
# 視為「同一段聲紋被存了兩次」的相似度。取 0.999 而非 1.0:float32 存檔與
# L2 正規化會有最後一位的誤差,實測重複樣本落在 0.99999 以上。
_DUPLICATE_SIM = 0.999


def store_file() -> Path:
    # 專案 data/ 子目錄(隨程式碼版控/複製):data/voiceprints.npz
    return models.data_dir() / "voiceprints.npz"


def _empty() -> tuple[list[str], np.ndarray]:
    return [], np.zeros((0, _DIM), dtype=np.float32)


def _set_aside(f: Path) -> Path:
    """把讀不動的聲紋庫搬到旁邊,回傳搬去哪裡。

    **不刪、也不原地留著**:留著的話下一次 `_save` 就把它蓋掉(那是使用者
    累積了幾個月、無法重錄的樣本);刪掉則連搶救的機會都沒有。搬開之後
    `models.seed_missing` 會在下次啟動補一個乾淨的種子檔,工具照常開得起來。
    已經有一份搬過的就再編號,絕不覆蓋(第一份通常才是最完整的那個)。"""
    for n in range(1, 100):
        dest = f.with_name(f"{f.name}.壞損{n}")
        if not dest.exists():
            f.rename(dest)
            return dest
    return f


def _read(f: Path, allow_pickle: bool) -> tuple[list[str], np.ndarray]:
    """真的把兩個陣列取出來。

    ⚠️ **一定要在這裡就取值、而且要 `with`**:`np.load` 對 npz 是**惰性**的
    ——它只開檔,「這是 pickle 而你沒開 allow_pickle」那個 ValueError 要等到
    `data["names"]` 才丟(2026-08-15 實測)。把取值留在外面的話,舊格式的檔
    會直接掉進「壞檔」那條路,而那條路會把使用者累積數月的聲紋庫搬走。
    `with` 則是為了關檔:handle 還開著時 `_set_aside` 的改名會被 Windows 擋。"""
    with np.load(f, allow_pickle=allow_pickle) as data:
        return [str(n) for n in data["names"]], data["vecs"]


def load() -> tuple[list[str], np.ndarray]:
    """回傳 (names, vecs):平行的名字清單與 L2 正規化聲紋矩陣(N×維度)。

    ⚠️ **讀不動也不能讓例外跑出去**:這支被 `data_tabs.vp_summary` 在
    `build_ui` **建構介面的當下**呼叫(而且在 try 之外),所以一個半截的
    npz 不只是聲紋失效,是整個工具打不開、黑視窗一串英文 traceback。
    壞檔搬到旁邊(見 `_set_aside`)、回空的,程式照常開,使用者看到的是
    「目前尚無登記任何聲紋」而不是打不開的網頁。

    ⚠️ **舊檔的名字是 pickle**(0.7.1 以前 `dtype=object` 存的),所以讀
    法是「先用安全的方式試,失敗才退回 allow_pickle 並就地轉存成新格式」
    ——同仁手上那些累積過的庫不能因為換格式就報廢,而轉存一次之後就不必
    再開 pickle 了(見 `_save` 的 ⚠️)。"""
    f = store_file()
    if not f.exists():
        return _empty()
    legacy = False
    try:
        try:
            names, vecs = _read(f, allow_pickle=False)
        except ValueError:
            # numpy 對「內容是 pickle、卻沒開 allow_pickle」丟的就是 ValueError
            names, vecs = _read(f, allow_pickle=True)
            legacy = True
    except Exception:
        dest = _set_aside(f)
        logger.exception(
            "聲紋庫讀不動(檔案可能在存檔中途被中斷),已搬到 %s;"
            "本次以空的聲紋庫繼續,那個檔請留著別刪", dest,
        )
        return _empty()
    if vecs.ndim != 2 or len(names) != len(vecs):
        return _empty()
    vecs = vecs.astype(np.float32)
    if legacy:
        logger.info("聲紋庫是舊格式(名字以 pickle 儲存),已就地轉存成新格式")
        _save(names, vecs)
    return names, vecs


def _save(names: list[str], vecs: np.ndarray) -> None:
    """整份覆寫聲紋庫。**先寫暫存檔再原子改名**,絕不就地截斷。

    ⚠️ 這是**天天在跑的熱路徑**:套用一次命名,每位講者各 `enroll` 一次,
    七位講者就是七個獨立的毀損窗口(斷電、防毒鎖檔、闔蓋進 Modern Standby
    之後被砍)。就地 `np.savez` 一旦寫到一半,留下的是半個 zip——而聲紋
    樣本錄不回來。`models.seed_missing` 早就為**同一個檔**寫了 .part + 改名
    (那條註解白紙黑字寫著「不會讓半個 voiceprints.npz 落地」),冷路徑有
    護欄、熱路徑沒有,是 2026-08-15 code review 才發現的。

    ⚠️ **名字不用 `dtype=object`**:那會讓 names.npy 變成 pickle,於是讀檔
    非開 `allow_pickle=True` 不可——而這個檔正是要私下互傳給同仁的那一個
    (公開版一律是空庫),路上被掉包就等於開啟工具的當下執行任意程式碼。
    一般的字串陣列對中文、全形「・」與多空白的名字 round-trip 完全相同。"""
    f = store_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f.name + ".part")
    try:
        with open(tmp, "wb") as fh:
            np.savez(fh, names=np.array(names, dtype=np.str_),
                     vecs=vecs.astype(np.float32))
        tmp.replace(f)          # 同一個磁碟區,POSIX/Windows 都是原子的
    finally:
        tmp.unlink(missing_ok=True)


def known_names() -> list[str]:
    """聲紋庫裡出現過的名字(去重、排序),供介面下拉快速選用。"""
    names, _ = load()
    return sorted(set(names))


def delete(name: str) -> None:
    """刪除某個名字的所有聲紋樣本(下次就不再自動辨識為此人)。"""
    name = (name or "").strip()
    names, vecs = load()
    keep = [i for i, n in enumerate(names) if n != name]
    if len(keep) == len(names):
        return  # 沒有此名字,不動
    new_names = [names[i] for i in keep]
    new_vecs = vecs[keep] if keep else np.zeros((0, _DIM), dtype=np.float32)
    _save(new_names, new_vecs)


def clear() -> None:
    """清除所有聲紋(檔案一併刪除)。"""
    f = store_file()
    try:
        f.unlink(missing_ok=True)
    except Exception:
        _save([], np.zeros((0, _DIM), dtype=np.float32))


def _normalize(vec) -> np.ndarray:
    v = np.asarray(vec, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def recognize(vec, threshold: float = _MATCH_THRESHOLD) -> str | None:
    """拿一段聲紋比對聲紋庫,回傳最相似且相似度 ≥ threshold 的名字;否則 None。"""
    names, vecs = load()
    if not names or vecs.shape[0] == 0:
        return None
    q = _normalize(vec)
    if q.shape[0] != vecs.shape[1]:
        return None
    sims = vecs @ q
    i = int(np.argmax(sims))
    return names[i] if float(sims[i]) >= threshold else None


def recognize_batch(
    vecs: dict[int, "np.ndarray"],
    threshold: float = _MATCH_THRESHOLD,
    margin: float = _RUNNER_UP_MARGIN,
) -> dict[int, str]:
    """一整場會議的講者一起辨識,回傳 {講者編號: 認出的名字}(認不出的不列)。

    **同一個名字只會給分數最高的那一位**,其餘留白。一場會議裡同一個
    名字不可能是兩群人——逐位各自 recognize 會讓兩群拿到同一個名字,
    而那在成品裡看起來就是「少了一個人」,不是「認錯人」:使用者看不到
    任何異狀,自然也不會去改(2026-08-07 實跡,見模組 docstring)。

    **輸的那一位留白,不退而求其次拿第二名**:那一位的第二名通常已經是
    不相干的人(該場實測:被讓出的那群第二名 0.69,是另一位完全沒出席的
    同事),填錯名比留白更擾民——留白使用者補打即可,錯名要先發現才會改。
    這與 _MATCH_THRESHOLD 取偏保守是同一個原則。

    **還要贏第二名夠多才算數**(margin,見 _RUNNER_UP_MARGIN):過了門檻
    但第一名與第二個**名字**只差一點點,代表這段聲紋對誰都差不多像——
    那不是「認出來了」,是「剛好這個人排在前面」。

    平手時取講者編號較小者(= 較早開口),結果穩定可預期。
    """
    best: dict[str, tuple[float, int]] = {}
    for speaker, ranked in _ranked_matches(vecs, threshold):
        name, score = ranked[0]
        if len(ranked) > 1 and score - ranked[1][1] < margin:
            logger.info(
                "聲紋辨識:講者 %d 最像「%s」(%.2f),但與第二名只差 %.2f"
                "(需 %.2f),留白不猜",
                speaker + 1, name, score, score - ranked[1][1], margin,
            )
            continue
        if name not in best or score > best[name][0]:
            best[name] = (score, speaker)
    return {speaker: name for name, (_score, speaker) in best.items()}


class CloseCall(NamedTuple):
    """一位「像得夠、卻分不出是哪一個」的講者(close_calls 的值)。"""

    rivals: list[str]   # 分不開的名字,分數由高到低;已排除被別人拿走的
    gap: float          # 第一名與第二個名字的差距(越小越難分)


def _ranked_matches(
    vecs: dict[int, "np.ndarray"], threshold: float,
) -> list[tuple[int, list[tuple[str, float]]]]:
    """每位講者對聲紋庫的比對結果:[(講者編號, [(名字, 分數)…由高到低])]。

    **同一個名字只留最高分的那一份**:同名多樣本是這個庫的常態,不合併的
    話「第二名」永遠是同一個人的另一份樣本,margin 那條規則等於沒寫。
    低於相似度門檻的講者整個不列——那是「不夠像」,與「分不開」是兩件事。

    ⚠️ **平手時保留先出現的名字**(dict 插入序 + 穩定排序),與先前直接
    `argmax` 的行為一致:同一份資料重跑要給同一個答案,不能每次換一個人。

    ⚠️ **recognize_batch 與 close_calls 共用這一份**:兩邊各算一次的話,
    畫面上的候選會與「為什麼沒自動填」的判準悄悄脫節,而那種不一致沒有
    任何症狀——使用者只會看到一份看起來很合理、卻不是同一套規則算出來的
    名單。"""
    names, lib = load()
    out: list[tuple[int, list[tuple[str, float]]]] = []
    if not names or lib.shape[0] == 0:
        return out
    for speaker in sorted(vecs):
        vec = vecs[speaker]
        if vec is None:
            continue
        q = _normalize(vec)
        if q.shape[0] != lib.shape[1]:
            continue
        best_of_name: dict[str, float] = {}
        for name, sim in zip(names, lib @ q):
            sim = float(sim)
            if sim > best_of_name.get(name, -2.0):
                best_of_name[name] = sim
        ranked = sorted(best_of_name.items(), key=lambda kv: -kv[1])
        if not ranked or ranked[0][1] < threshold:
            continue
        out.append((speaker, ranked))
    return out


def close_calls(
    vecs: dict[int, "np.ndarray"],
    taken=(),
    threshold: float = _MATCH_THRESHOLD,
    margin: float = _RUNNER_UP_MARGIN,
    limit: int = _MAX_RIVALS,
) -> dict[int, CloseCall]:
    """留白的那幾位「聲音同時像誰」;{講者編號: CloseCall}(認得出的不列)。

    只給介面當**線索**用——「這幾個人分不出來,去聽一下」——絕不拿來
    自動填名:填名的判準仍然只有 recognize_batch 那一套,這裡不碰。

    ⚠️ **回的是一份名單不是一個答案**,理由見 `_MAX_RIVALS`:98 次留白裡
    54% 的第一名與第二名只差 0.03 以內,而那一段第一名只有 24% 是對的。
    呼叫端要把它們**並列**呈現;只顯示第一個等於給了一個四次錯三次的
    答案,而使用者會相信自動填出來的名字(2026-08-08 他明確說過)。

    `taken` = 已經被自動填給別人的名字,一律排除:同一場會議一個名字只能
    給一位講者(recognize_batch 的約束),把已經確定屬於別人的名字列進
    來,等於邀請使用者製造一個「兩群同名」的成品——那在逐字稿裡看起來
    是「少了一個人」,不是「認錯人」,而使用者不會發現。"""
    taken = set(taken)
    out: dict[int, CloseCall] = {}
    for speaker, ranked in _ranked_matches(vecs, threshold):
        if len(ranked) < 2:
            continue  # 庫裡只有一個名字:沒有「分不開」這回事
        top = ranked[0][1]
        gap = top - ranked[1][1]
        if gap >= margin:
            continue  # 這位自動填得出名字,不需要線索
        rivals = [n for n, s in ranked if top - s < margin and n not in taken]
        if rivals:
            out[speaker] = CloseCall(rivals[:limit], gap)
    return out


def _log_inconsistent(name: str, q: "np.ndarray", names, vecs) -> None:
    """新樣本與同名既有樣本明顯不像時記一筆到執行紀錄檔。

    **只記錄不擋**:使用者當下指名道姓說「這是某某」,程式沒有立場否決;
    而且真正該懷疑的往往是**既有的**樣本(以前認錯人存進去的),擋掉新的
    只會讓錯誤留得更久。留下這一行的用處是事後對得起來——
    scripts/audit_voiceprints.py 掃出可疑名字時,log 說得出是哪一次進來的。"""
    same = [v for n, v in zip(names, vecs) if n == name]
    if not same:
        return
    top = float(max(float(v @ q) for v in same))
    if top < _CONSISTENCY_SIM:
        logger.warning(
            "聲紋登記:「%s」的新樣本與既有 %d 個樣本都不像(最高 %.2f < %.2f)"
            "——這個名字底下可能裝了兩個人,可用 scripts/audit_voiceprints.py 檢查",
            name, len(same), top, _CONSISTENCY_SIM,
        )


# ------------------------------------------------------------------ 健檢
#
# 判準與門檻的實測來源見上方常數。**邏輯只有這一份**:網頁介面
# (data_tabs)與命令列(scripts/audit_voiceprints.py)都從這裡取,
# 兩邊各寫一套的話,使用者在畫面上看到的「可疑」與工具算出來的會不一樣。


class Suspect(NamedTuple):
    """一筆「這個樣本可能不是這個人的」。

    index 是**該名字底下的第幾個樣本**(0-based),不是全庫索引——刪除與
    顯示都以「某某的第幾個」為單位,全庫索引一刪就位移。"""

    name: str
    index: int
    like_name: str    # 聽起來更像誰
    like_sim: float   # 與那個人的最高相似度
    own_sim: float    # 與同名其他樣本的最高相似度
    lone: float = 0.0  # 「最像的名字」領先第二個名字多少(見 _LONE_MARGIN)

    @property
    def gap(self) -> float:
        return self.like_sim - self.own_sim


class Duplicate(NamedTuple):
    """同一個名字底下,內容幾乎一模一樣的兩個樣本(後者白佔名額)。"""

    name: str
    keep: int
    drop: int


def _grouped(names: list[str]) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for i, n in enumerate(names):
        out.setdefault(n, []).append(i)
    return out


def suspects(
    margin: float = _MISFILED_MARGIN,
    lone: float = _LONE_MARGIN,
) -> list[Suspect]:
    """找出「跟別的名字比,跟自己的名字還不像」的樣本,差距大到小。

    **兩道關卡都要過**:
    1. `margin`——它跟某個別的名字,比跟自己人還像這麼多;
    2. `lone`——而且那個名字要領先**第二個名字**夠多,也就是它「只像
       某一個人」。少了第二道,通道效應(共用麥克風、遠端連線會讓一整群
       人一起變像)會被誤判成存錯名字,而照著刪掉的代價是那個人再也認
       不出來(2026-08-08 真的發生過,見 `_LONE_MARGIN` 的註解)。

    只有一個樣本的名字跳過:沒有「自己人」可比,無從判斷(不是沒問題,
    是這個判準說不出話——謊報無辜比漏報更糟)。同理,庫裡不足兩個「別的
    名字」時也跳過:算不出孤例度,無從分辨是不是通道效應。

    命令列報告要看完整分佈時把兩個門檻都調低(見 audit_voiceprints)。"""
    names, vecs = load()
    if not names:
        return []
    out: list[Suspect] = []
    for name, idx in _grouped(names).items():
        if len(idx) < 2:
            continue
        for k, i in enumerate(idx):
            own = max(float(vecs[j] @ vecs[i]) for j in idx if j != i)
            sims = vecs @ vecs[i]
            # 每個「別的名字」取它最高分的那個樣本,再排名次。用名字而不是
            # 樣本排名:同一個人的兩份樣本本來就會連續佔住前兩名,那樣算出
            # 來的「領先第二名」永遠接近 0,整條規則等於沒寫
            best: dict[str, float] = {}
            for j, other in enumerate(names):
                if other == name:
                    continue
                s = float(sims[j])
                if s > best.get(other, -1.0):
                    best[other] = s
            if not best:
                continue
            ranked = sorted(best.items(), key=lambda kv: -kv[1])
            like_name, like_sim = ranked[0]
            # 只有一個「別的名字」時算不出孤例度——但那時也沒有「一整群」
            # 可言(通道效應的前提是好幾個人一起變像),所以退回只看差距。
            # 這是小型聲紋庫才會走到的分支
            lone_gap = like_sim - ranked[1][1] if len(ranked) > 1 else float("inf")
            if like_sim - own >= margin and lone_gap >= lone:
                out.append(Suspect(
                    name, k, like_name, like_sim, own,
                    0.0 if lone_gap == float("inf") else lone_gap,
                ))
    return sorted(out, key=lambda s: -s.gap)


def duplicates() -> list[Duplicate]:
    """同名樣本裡內容重複的(留第一個、其餘可刪)。清掉它們**不影響任何
    辨識結果**——留下的那個一模一樣,而 recognize 取的是最大值。"""
    names, vecs = load()
    out: list[Duplicate] = []
    for name, idx in _grouped(names).items():
        dropped: set[int] = set()
        for a in range(len(idx)):
            if a in dropped:
                continue
            for b in range(a + 1, len(idx)):
                if b in dropped:
                    continue
                if float(vecs[idx[a]] @ vecs[idx[b]]) >= _DUPLICATE_SIM:
                    dropped.add(b)
                    out.append(Duplicate(name, a, b))
    return out


def whole_name_suspect(name: str, found: list[Suspect]) -> bool:
    """這個名字**每一個**樣本都可疑嗎?

    是的話意義完全不同:不是「同一個人的幾份樣本裡有一份錯了」,而是
    幾個不同的人被存進同一個名字(2026-08-07 實例:K 三個樣本分別
    像 L 0.87、M 0.72、N 0.70,彼此才 0.32~0.45)。
    這種沒有核心可留,只能整個刪掉、下次重新命名。"""
    names, _ = load()
    total = names.count(name)
    return total > 0 and sum(1 for s in found if s.name == name) == total


def delete_samples(items) -> int:
    """刪掉指定的 (名字, 該名字底下第幾個樣本);回傳實際刪除數。

    一次收一批而不是逐筆呼叫:**序號會位移**——刪掉「K#0」之後,
    原本的 #1 就變成 #0,逐筆刪第二筆就刪錯人了。"""
    names, vecs = load()
    grouped = _grouped(names)
    kill: set[int] = set()
    for name, k in items:
        idx = grouped.get(name) or []
        if 0 <= k < len(idx):
            kill.add(idx[k])
    if not kill:
        return 0
    keep = [i for i in range(len(names)) if i not in kill]
    _save(
        [names[i] for i in keep],
        vecs[keep] if keep else np.zeros((0, _DIM), dtype=np.float32),
    )
    return len(kill)


class RenamePlan(NamedTuple):
    """改名會發生什麼(供呼叫端在動手前把話講清楚)。

    **合併與單純改名是同一個動作、不同的後果**,所以介面必須問得出差別:
    新名字已經有樣本時,這件事實質上是「把兩個人併成一個」——如果它們本來
    就是同一個人的兩種寫法,那正是使用者要的;如果不是,就等於親手製造
    「一個名字底下裝了兩個人」,而那是最難發現的一種錯(成品看起來只是
    少了一個人,見模組 docstring)。"""

    moving: int    # 舊名字底下有幾個樣本要換標籤
    existing: int  # 新名字現在有幾個樣本(> 0 就是合併)
    dropped: int   # 合併後超過上限、會被淘汰掉幾個

    @property
    def merges(self) -> bool:
        return self.existing > 0

    @property
    def total(self) -> int:
        """改完之後新名字底下會有幾個。"""
        return self.moving + self.existing - self.dropped


def rename_plan(old: str, new: str) -> RenamePlan:
    """算出 `rename(old, new)` 的後果,不動任何資料。"""
    old, new = (old or "").strip(), (new or "").strip()
    names, _ = load()
    if not old or not new or old == new:
        return RenamePlan(0, 0, 0)
    moving, existing = names.count(old), names.count(new)
    return RenamePlan(
        moving, existing, max(0, moving + existing - _MAX_SAMPLES_PER_NAME),
    )


def rename(old: str, new: str) -> int:
    """把某個名字底下的所有聲紋改掛到新名字;回傳實際改動的樣本數。

    新名字已存在時就是**合併**——呼叫端負責先問過使用者(見 RenamePlan)。
    合併後超過上限一樣淘汰最舊的,策略與 `enroll` 一致:兩邊各寫一套的話,
    使用者會在兩條路上看到不同的結果。"""
    old, new = (old or "").strip(), (new or "").strip()
    if not old or not new or old == new:
        return 0
    names, vecs = load()
    if old not in names:
        return 0
    moved = names.count(old)
    renamed = [new if n == old else n for n in names]
    rows = [row for row in vecs]
    while sum(1 for n in renamed if n == new) > _MAX_SAMPLES_PER_NAME:
        drop = next(i for i, n in enumerate(renamed) if n == new)
        del renamed[drop]
        del rows[drop]
    _save(
        renamed,
        np.array(rows, dtype=np.float32) if rows
        else np.zeros((0, _DIM), dtype=np.float32),
    )
    return moved


def enroll(name: str, vec) -> None:
    """把一段聲紋登記到某名字下(留白名字忽略);同名超過上限時淘汰最舊樣本。"""
    name = (name or "").strip()
    if not name:
        return
    q = _normalize(vec)
    names, vecs = load()
    _log_inconsistent(name, q, names, vecs)
    rows = [row for row in vecs] + [q]
    names = list(names) + [name]
    # 同名樣本限量:超過上限丟最舊(清單前端者較舊)
    while sum(1 for n in names if n == name) > _MAX_SAMPLES_PER_NAME:
        drop = next(i for i, n in enumerate(names) if n == name)
        del names[drop]
        del rows[drop]
    _save(names, np.array(rows, dtype=np.float32))
