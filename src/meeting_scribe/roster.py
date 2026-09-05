r"""與會人員名單的編輯:比對、把關、儲存(**不含任何 UI**)。

2026-08-29 從 `data_tabs.py` 抽出來(原生介面遷移的階段 4)。⚠️ **這個模組不准
import 任何 UI 模組**——判準同 `naming.py` / `wordlists.py`:不是「講的是不是同一
件事」,是**「換一套 UI 要不要改」**。`data_tabs.py` 在模組層 `import gradio`。

⚠️ **這裡真正要守的是「改名字時聲紋要跟著走」**,而它的失效方式極難發現:名單上
改了名字、聲紋庫沒跟著改的話,那個人下次開會**認不出來**——而那個症狀長得像分群
壞掉,診斷會被帶去完全錯的方向(CLAUDE.md 記著這條:聽到「某某人沒有被分出來」
的三種病因)。所以這一層回的是**一份計畫**(`Plan`),由 UI 先講給使用者聽、等他
按過確認才真的動聲紋庫。

⚠️ **判準刻意保守**:剛好一個名字消失、剛好一個出現才算「改名」。同時改好幾個時
對應不起來(A→B 還是 A→C?),寧可不猜——猜錯會把聲紋掛到別人名下,而那是最難
發現的一種錯。那種情況只存名單,但**一定要講出哪些人被丟下了**(見 `Plan.stranded`;
2026-08-15 的 code review 抓到那條路先前什麼都不說,而使用者真的一次補過 28 人)。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from meeting_scribe import attendees, pending
from meeting_scribe import voiceprints as voiceprints_store


@dataclass(frozen=True)
class Plan:
    """這次編輯要發生什麼事。

    `rename`:認得出來的那一組 `(舊名, 新名)`,聲紋可以跟著改。
    `stranded`:會從名單上消失、而**聲紋庫裡還有樣本**的人 `[(名字, 樣本數)]`。
    `names`:清乾淨之後真的要存的名單。
    """

    names: tuple[str, ...] = ()
    rename: tuple[str, str] | None = None
    stranded: tuple[tuple[str, int], ...] = ()

    @property
    def needs_confirming(self) -> bool:
        """有沒有「使用者不按確認就不該做」的事。

        ⚠️ 兩種都算:改名(要動聲紋庫)與丟下有聲紋的人(不動,但要讓他知道)。"""
        return bool(self.rename or self.stranded)


def current() -> list[str]:
    """現在存著的名單。"""
    return attendees.load()


def voiceprint_names() -> list[str]:
    """有聲紋的人,照**與會名單的順序**排(使用者指定 2026-08-08)。

    名單是使用者自己排的順序(`attendees.load()` 保留加入順序),聲紋那邊若照字典序
    排,同一批人在兩邊的位置對不起來——要在幾十個人裡找同一個人,得重新掃一遍。

    ⚠️ **兩份名單不是同一個東西**:聲紋庫是「有聲紋的人」,與會名單是「可能出席的
    人」,多數重疊但不保證。所以有聲紋卻不在名單的接在後面(照原本的字典序),
    **一個都不能少**——那些人照樣要改得到名字。"""
    known = voiceprints_store.known_names()      # 已去重、字典序
    in_roster = set(known)
    ordered = [n for n in attendees.load() if n in in_roster]
    seen = set(ordered)
    return ordered + [n for n in known if n not in seen]


# 摘要那行最多點名幾個「只在聲紋庫」的人。⚠️ **不是全部列出來**:那一欄只有
# 400 px 出頭,列到第四個就把摘要撐成五、六行,而使用者要的是「有幾個、大概是誰」
# ——完整清單本來就在底下那份「已登記的人」裡。
ORPHANS_SHOWN = 3


def orphan_names() -> list[str]:
    """有聲紋、卻**不在**與會名單上的名字(照聲紋庫的名字排序)。

    這是「名單與聲紋庫失聯」的安全網:改名只改了一邊、用記事本直接編過
    `attendees.txt`、名單裡刪掉一個還有聲紋的人——所有來源都會在這裡浮出來。
    它也是唯一救得回**已經發生**那幾筆的東西:失聯本身沒有任何症狀,要等下次
    開會那個人認不出來才會發現,而那時沒有人會聯想到幾週前改過名字。

    ⚠️ **方向只能是單向的**。反過來(名單上有、聲紋庫沒有)是**常態**——名單是
    「可能出席者」,新同事還沒開過會本來就沒有聲紋(實測使用者的 64 人名單對
    59 人聲紋,這個方向天天成立)。兩邊都報等於永遠亮著,然後就被當成背景雜訊。"""
    listed = set(attendees.load())
    return [n for n in voiceprints_store.known_names() if n not in listed]


def rename_everywhere(old: str, new: str) -> tuple[int, int]:
    """把一個名字在**聲紋庫、與會名單、落地的命名草稿**三處一起改掉。

    回 `(改掛的樣本數, 改完之後新名字底下共有幾個)`。

    ⚠️ **三處缺一不可**。只改聲紋庫的話,名單上還留著已經不存在的舊名字;
    只改名單的話,那個人下次開會認不出來(而症狀長得像分群壞掉)。**草稿那一份
    最容易被忘記**:它存的是名字字串本身,漏掉的話「轉完一份、自動填好名字還沒
    套用 → 中途去改那個人的名字 → 回頭開頁」之後,下拉裡放回來的是**舊**名字,
    按下套用就在庫裡多出第二個身分(見 `pending.rename_draft`)。
    ⚠️ **合併要由呼叫端先問過**:新名字已經有樣本時,這個動作實質上是「把兩個人
    併成一個」(`voiceprints.rename_plan().merges`)。"""
    old, new = (old or "").strip(), (new or "").strip()
    if not old or not new or old == new:
        return 0, 0
    moved = voiceprints_store.rename(old, new)
    attendees.rename(old, new)
    pending.rename_draft(old, new)
    # ⚠️ 數樣本要用 `load()[0]`(完整清單):`known_names()` 是**去重**的,拿它
    # count 永遠得到 1——畫面會寫成「5 個聲紋樣本(目前共 1 個)」。
    all_names, _ = voiceprints_store.load()
    return moved, all_names.count(new)


def clean(names) -> tuple[str, ...]:
    """把編輯區的內容整理成名單:去頭尾空白、丟掉空行、**保留順序去重**。

    ⚠️ **順序要保留**:那份名單是命名時下拉選單的順序,而使用者是照部門排的。"""
    out: list[str] = []
    for raw in names or ():
        name = (raw or "").strip()
        if name and name not in out:
            out.append(name)
    return tuple(out)


def plan(before: list[str], after) -> Plan:
    """比對前後兩份名單,算出這次編輯要發生什麼事(不寫任何檔案)。"""
    names = clean(after)
    return Plan(names=names, rename=_detect_rename(before, list(names)),
                stranded=_stranded(before, list(names)))


def _detect_rename(before: list[str], after: list[str]) -> tuple[str, str] | None:
    """認出「一個名字被改成另一個」;認不出就回 `None`(判準見模組 docstring)。

    ⚠️ 「刪一個人、同時加另一個人」也長這樣——所以這裡只負責**提出可能性**,
    真正動聲紋庫之前一定要讓使用者按過確認。

    ⚠️ **「一對一才算」是刻意的,這裡不做也不准做遞移配對**(2026-08-29 補這一段,
    因為它看起來像少寫了什麼)。誘惑是這樣的:消失兩個、出現兩個時,拿「名字相
    似度」把它們配起來,再把配對併成一組——但那是**用局部的判斷推出全域的結論**,
    而那個結論**從來沒有被檢查過**。姊妹專案 NotebookLM_OCR 2026-08-29 正好踩到同
    一族:同一塊色底的多行用 union-find 併組,配對條件是局部的(緊鄰＋重疊＋色差
    小),union 卻是全域的——A-B、B-C 就把坐在完全不同底色上的 A 和 C 併在一起,
    一張表格被串成九行、塗成一個頁面上根本不存在的顏色。
    這裡的**代價比那個更高**:配錯的下場是把某人的聲紋掛到別人名下,而那個錯誤
    看不見、還會在下一場會議自我複製。所以判準只有一條「剛好一個換一個」,多的
    一律回 `None`——那條路另外有 `_stranded` 負責出聲,不是靜靜放過。"""
    gone = [n for n in before if n not in after]
    added = [n for n in after if n not in before]
    return (gone[0], added[0]) if len(gone) == 1 and len(added) == 1 else None


def _stranded(before: list[str], after: list[str]) -> tuple[tuple[str, int], ...]:
    """這次編輯會讓哪些「還有聲紋的人」從名單上消失。

    ⚠️ **這是「認不出改名」那條路唯一的出聲機會**。也涵蓋「刻意刪掉一個還有聲紋的
    人」:那時提醒同樣是對的——他的聲紋還在庫裡,下次會被自動填成一個名單上沒有的
    名字。⚠️ `before` 是空的就不提醒:那是第一次使用,不是「刪掉了誰」。"""
    if not before:
        return ()
    left = set(after)
    library = voiceprints_store.load()[0]
    return tuple((n, library.count(n)) for n in before
                 if n not in left and library.count(n))


def save(plan_: Plan, rename_voiceprints: bool = False) -> str:
    """把名單存下去,回一句給使用者看的話(純文字,不帶 Markdown 記號)。

    ⚠️ **聲紋只在 `rename_voiceprints=True` 時才動**:呼叫端要先讓使用者看過
    `Plan` 並按下確認。⚠️ **順序是先改聲紋再存名單**:反過來的話,存完名單而改
    聲紋失敗時,名單上已經是新名字、聲紋卻還掛在舊名下——那正是「認不出來」的成因,
    而且此後再也對不起來(舊名字已經不在名單上,沒有人會想到去查它)。"""
    moved = 0
    if plan_.rename:
        # ⚠️ **草稿那一份不看 `rename_voiceprints`**:名單這一半無論如何都改掉了,
        # 而草稿裡留著舊名字,下次開頁就會把它復活成第二個身分(見
        # `pending.rename_draft`)——那與「聲紋要不要跟著走」是兩件事。
        pending.rename_draft(*plan_.rename)
    if rename_voiceprints and plan_.rename:
        old, new = plan_.rename
        moved = voiceprints_store.rename(old, new)
    attendees.save_all(list(plan_.names))
    msg = f"已儲存與會名單({len(plan_.names)} 人)。"
    if moved:
        msg += f"「{plan_.rename[0]}」的 {moved} 個聲紋樣本已改掛到「{plan_.rename[1]}」。"
    elif rename_voiceprints and plan_.rename:
        msg += f"「{plan_.rename[0]}」在聲紋庫裡沒有樣本,只改了名單。"
    if plan_.stranded:
        msg += ("⚠ 這幾個人不在新名單上,但聲紋庫裡還有他們的樣本:"
                + "、".join(f"{n}({c} 個)" for n, c in plan_.stranded)
                + "。下次開會他們會被填成名單上沒有的名字。")
    return msg
