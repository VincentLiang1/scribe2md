"""三個資料分頁的事件處理(app.build_ui 只負責接線)。

「名單與聲紋」與「領域詞表」是「聲音→MD」的子分頁(那些資料只服務轉錄),
「用詞替換表」在頂層(兩條路徑的輸出都會套用它)。

詞表兩檔(hotwords.txt / replace.txt)的註解(收詞原則、預算說明)與
行序都有意義,故用純文字編輯器原樣存取,不拆成表格(表格 round-trip
會弄丟註解與順序)。存檔即生效於下一個轉錄檔:hotwords 每次重讀、
replace 依 mtime 快取失效。
"""
import logging

import gradio as gr

from meeting_scribe import attendees, convert, hotwords, pending
from meeting_scribe import voiceprints as voiceprints_store

logger = logging.getLogger(__name__)


# ---- 與會人員名單管理 ----

def attendee_rows() -> list[list[str]]:
    """名單 → 表格資料(每列一個名字);至少留一空列方便新增。"""
    rows = [[n] for n in attendees.load()]
    return rows or [[""]]


def _table_names(table) -> list[str]:
    """表格(list of rows,或 pandas)→ 乾淨的名字清單。"""
    names = []
    for row in (table.values.tolist() if hasattr(table, "values") else (table or [])):
        cell = row[0] if isinstance(row, (list, tuple)) and row else row
        if isinstance(cell, str) and cell.strip():
            names.append(cell.strip())
    return names


def detect_rename(before: list[str], after: list[str]) -> tuple[str, str] | None:
    """比對名單前後,認出「一個名字被改成另一個」;認不出就回 None。

    **判準刻意保守**:剛好一個消失、剛好一個出現才算。同時改好幾個名字時
    對應不起來(A→B 還是 A→C?),寧可不猜——猜錯會把聲紋掛到別人名下,
    而那是最難發現的一種錯。那種情況只存名單,並在訊息裡說清楚。

    ⚠️ 「刪一個人、同時加另一個人」也長這樣。所以這個函式只負責**提出
    可能性**,真正動聲紋庫之前一定要讓使用者按過確認鈕(呼叫端負責)。"""
    gone = [n for n in before if n not in after]
    added = [n for n in after if n not in before]
    if len(gone) == 1 and len(added) == 1:
        return gone[0], added[0]
    return None


def stranded(before: list[str], after: list[str]) -> list[tuple[str, int]]:
    """這次編輯會讓哪些「還有聲紋的人」從名單上消失;[(名字, 樣本數)]。

    ⚠️ **這是 `detect_rename` 回 None 那條路唯一的出聲機會**(2026-08-15
    code review 抓到:那條路先前**什麼都不說**,而 `detect_rename` 的
    docstring 白紙黑字寫著「那種情況只存名單,**並在訊息裡說清楚**」)。
    一次整批補部門正是使用者實際做過的事(1de1af1 十三人、0f08b5f 二十八
    人),而隔離環境實測那樣做的結果是:預告區不出現、儲存訊息只寫
    「已儲存與會名單(69 人)」,聲紋庫卻一口氣多出 25 個對不上的名字。
    漏改的症狀是那些人下次開會認不出來,而那個症狀長得像分群壞掉——
    診斷會被帶去完全錯的方向。

    也涵蓋「刻意刪掉一個還有聲紋的人」:那時提醒同樣是對的(他的聲紋還在
    庫裡,下次會被自動填成一個名單上沒有的名字)。"""
    if not before:
        return []
    left = set(after)
    library = voiceprints_store.load()[0]
    out = [(n, library.count(n)) for n in before if n not in left]
    return [(n, c) for n, c in out if c]


def _stranded_prompt(left: list[tuple[str, int]], saved: bool = False) -> str:
    """「一次改了好幾個名字」的說明(預告用未來式、儲存後用過去式)。

    **數字一定要講出來**(同 `_rename_prompt` 的理由):只說「有些人對不上」
    的話,使用者無從判斷嚴不嚴重、也不知道要去補哪幾個。"""
    shown = "、".join(f"「**{n}**」" for n, _c in left[:_ORPHANS_SHOWN])
    rest = len(left) - _ORPHANS_SHOWN
    if rest > 0:
        shown += f"、等 {len(left)} 人"
    samples = sum(c for _n, c in left)
    head = ("⚠️ 這次**一次改動了好幾個名字**,工具無法確定誰對應誰,"
            "所以聲紋庫**不會**跟著改")
    tail = ("按下「儲存名單」之後," if not saved else "")
    return (
        f"{head}。{tail}{shown}的 **{samples} 個聲紋樣本**"
        f"{'就' if not saved else '已'}只剩在聲紋庫裡、名單上沒有了"
        "——**下次開會認不出他們**。\n\n"
        "要補做的話:用最右邊的「修改名稱作業」一個一個改成新名字;"
        "下次要一起改聲紋的話,**一次只改一個名字**再儲存。"
    )


def _preview_hidden():
    """預告區收起來(勾選一併回到預設的「要改」)。

    ⚠️ 勾選的值要跟著回 True:使用者取消勾選、改回原名、再改另一個人時,
    留著上一次的 False 會**靜靜地**不同步聲紋——那正是這整段要修掉的錯。"""
    return gr.update(value="", visible=False), gr.update(value=True, visible=False)


def _merge_ask_hidden():
    """合併追問收起來:提示、「一併改聲紋」、「暫不修改」、待確認的改名。

    兩顆鈕**永遠一起出現、一起收掉**:只有一顆的話,不想合併的人沒有
    「關掉它」的方式,只能不理它——那樣就分不出「他決定不改」與
    「他根本沒看到」,而安全網那句提醒正是靠這個差別才有意義。"""
    return (gr.update(value="", visible=False), gr.update(visible=False),
            gr.update(visible=False), None)


def preview_rename(table):
    """表格一改完就預告「按下儲存會發生什麼」;回傳(預告區, 一起改的勾選)。

    **這一步不寫任何檔、也不動聲紋庫**,只是把追問提前到使用者**本來就要
    按的那顆鈕**上(2026-08-09 使用者選定的「方案 D」)。

    ⚠️ 原本的做法是儲存**之後**才在按鈕下方長出追問,而實測(Playwright,
    1240×736、64 人名單)那顆「儲存名單」本來就貼在視窗底(鈕底 y=700),
    追問區 207px **整個落在視窗外**、頁面又不會自動捲——使用者看不到就
    等於沒問,而沒按的後果(名單與聲紋庫從此對不上)要等下次開會才發現。

    ⚠️ **撞名(合併兩個人)不給勾選框**:那是破壞性的、事後極難發現,
    不能靠一個預設打勾的框帶過,必須維持「儲存後再問一次」(見
    `save_attendees`)。這裡只預告「等一下會被問」,免得那個追問又是憑空
    冒出來的。"""
    before = attendees.load()
    edited = _table_names(table)
    pair = detect_rename(before, edited)
    if pair is None:
        # 一次改了好幾個名字:對應不起來,聲紋庫不會動——但**一定要講**,
        # 而且要在他按下儲存**之前**講(見 `stranded`)。勾選框跟著藏起來:
        # 這條路沒有「一起改」可選,給一個按不動的框只會讓人以為按了就好
        left = stranded(before, edited)
        if not left:
            return _preview_hidden()
        return (gr.update(value=_stranded_prompt(left), visible=True),
                gr.update(value=True, visible=False))
    old, new = pair
    plan = voiceprints_store.rename_plan(old, new)
    if plan.moving == 0:
        return _preview_hidden()  # 聲紋庫沒有這個人,沒什麼好問的
    if plan.merges:
        return (
            gr.update(
                value=(f"⚠️ 「**{new}**」**已經有聲紋了**"
                       "——這會是把兩個人**合併**。\n\n"
                       "按「儲存名單」之後會**再問你一次**,"
                       "確認過才會動到聲紋庫。"),
                visible=True,
            ),
            gr.update(value=True, visible=False),
        )
    return (
        gr.update(
            value=(f"偵測到改名:「**{old}**」→「**{new}**」;"
                   f"聲紋庫裡有 **{plan.moving} 個樣本**。\n\n"
                   "**不一起改的話,下次開會就認不出這個人了。**"),
            visible=True,
        ),
        # 後果那句刻意獨立成行加粗:它是整段唯一講到後果的一句,而勾選框的
        # 標籤塞不下(欄寬只有 341px),塞進去會折成三行
        #
        # ⚠️ 每次偵測都把勾選**推回 True**(所以取消勾選之後、又去改別列的
        # 字,它會自己勾回來)。這是刻意選的方向:兩種錯的代價不對稱——
        # 多改了聲紋,結果訊息會當場說出來、也還能用「改名」改回去;漏改
        # 則完全無聲,要等下次開會那個人認不出來才發現。
        gr.update(value=True, visible=True),
    )


def save_attendees(table, sync_voiceprints=True):
    """表格存回名單,順便把改名同步到聲紋庫;回傳 10 值(見 build_ui 的接線)。

    **純改名一步做完、合併仍然停下來問**(2026-08-09 使用者選定的方案 D)。
    改名的決策在按下這顆鈕之前就由 `preview_rename` 問過了(勾選預設打勾),
    所以這裡看到 `sync_voiceprints=True` 就是使用者確認過的意思——原本那顆
    「一併改聲紋」追問鈕長在按鈕下方、實測整顆在視窗外,見 `preview_rename`。

    ⚠️ **合併不吃那個勾選**:新名字已經有聲紋時,這件事實質上是把兩個人
    併成一個,而且錯了在逐字稿裡看起來只是「少了一個人」,極難發現。
    那條路一定要走「儲存 → 看數字 → 按確認」。"""
    before = attendees.load()
    names = _table_names(table)
    # ⚠️ **整份清空一律擋下來**:那多半是「全選 → 刪」或表格值送壞了,而
    # 名單一旦寫成 0 bytes,每個人的下拉都空了、聲紋庫整批變成孤兒——
    # 隔壁「清除全部聲紋」為同一等級的破壞性動作準備了確認框與正面表列,
    # 這裡卻是一顆平凡的「儲存名單」。真的想清空,一個一個刪還是做得到
    if before and not names:
        return (
            f"**沒有儲存**:這樣會把名單上的 {len(before)} 個人整個清空。"
            "真的要清空請一列一列刪,或按「重新載入」把畫面復原。",
            *_preview_hidden(), *_merge_ask_hidden(),
            *_name_dropdowns(), gr.update(value=vp_summary()),
        )
    attendees.save_all(names)
    msg = f"已儲存與會名單({len(attendees.load())} 人)。"
    # 追問區(合併專用)與待確認的改名;預告區一律收起來(它的任務結束了)
    quiet = (*_preview_hidden(), *_merge_ask_hidden())

    def done(message, tail=quiet):
        # ⚠️ 中間欄那三個永遠一起更新,不是只有改名那條路要更新:兩個下拉是
        # **照名單順序**排的(見 vp_names_in_roster_order),而摘要裡的
        # 「只在聲紋庫、不在名單上」也是拿名單比對的——刪掉一個還有聲紋的
        # 人、或只是把名單重新排序,中間欄就已經過期了
        return (message, *tail, *_name_dropdowns(), gr.update(value=vp_summary()))

    after = attendees.load()
    pair = detect_rename(before, after)
    if pair is None:
        # 對應不起來就只存名單——但**要在訊息裡說清楚**(見 `stranded`):
        # 先前這條路一句話都沒有,而整批補部門正是使用者實際會做的事
        left = stranded(before, after)
        return done(msg if not left else f"{msg}\n\n{_stranded_prompt(left, saved=True)}")
    old, new = pair
    # 名單那一半已經改掉了,落地的命名草稿要跟著改(見 pending.rename_draft)
    # ——這與聲紋要不要一起改是兩件事,所以放在那個判斷之前
    pending.rename_draft(old, new)
    plan = voiceprints_store.rename_plan(old, new)
    if plan.moving == 0:
        # 聲紋庫裡沒有這個名字:名單改完就結束,不必問
        return done(f"{msg}(「{old}」在聲紋庫裡沒有樣本,不需要同步)")
    if plan.merges:
        # 兩顆鈕一起亮:「一併改聲紋」與「暫不修改」(見 _merge_ask_hidden)
        return done(msg, (
            *_preview_hidden(),
            gr.update(value=_rename_prompt(old, new, plan), visible=True),
            gr.update(visible=True), gr.update(visible=True), (old, new),
        ))
    if not sync_voiceprints:
        # 他自己取消勾選的,照辦——但要講清楚現在的狀態,而且中間欄那行
        # 摘要會立刻長出「有名字只在聲紋庫」的提醒(見 vp_summary)
        return done(
            f"{msg}**聲紋沒有一起改**——「{old}」的 {plan.moving} 個樣本"
            f"還掛在舊名字下,下次開會會認不出他。要補做的話用最右邊的"
            "「修改名稱作業」。")
    moved = voiceprints_store.rename(old, new)
    all_names, _ = voiceprints_store.load()
    return done(
        f"{msg}並把「{old}」的 {moved} 個聲紋樣本改掛到「{new}」"
        f"(目前共 {all_names.count(new)} 個)。")


def reload_attendees():
    """「重新載入」:表格回到檔案的內容,順手把預告/追問全部收掉。

    不收的話,畫面上會留著一句在講**已經不存在**的那次編輯的預告,
    而使用者接著按儲存,得到的結果與預告完全對不上。"""
    return (gr.update(value=attendee_rows()),
            *_preview_hidden(), *_merge_ask_hidden())


def _rename_prompt(old: str, new: str, plan) -> str:
    """改名的確認文字。

    **一定要把數字講出來**(設計稿定案):「3 個併進 2 個、共 5 個」才判斷
    得了,只說「名字重複,確定嗎?」等於沒問。"""
    if not plan.merges:
        return (f"偵測到改名:「**{old}**」→「**{new}**」。\n\n"
                f"聲紋庫裡「{old}」有 **{plan.moving} 個樣本**,"
                "要一起改過去嗎?(不改的話,下次開會就認不出這個人了)")
    lines = [
        f"⚠️ 「**{new}**」**已經有聲紋了**——這個動作會把兩個名字**合併**。",
        "",
        f"- 「{old}」的 {plan.moving} 個樣本會併進「{new}」(現有 {plan.existing} 個)",
        f"- 合併後共 **{plan.total} 個**",
    ]
    if plan.dropped:
        lines.append(
            f"- ⚠️ 超過每人 {voiceprints_store._MAX_SAMPLES_PER_NAME} 個的上限,"
            f"會淘汰 **{plan.dropped} 個最舊的樣本**"
            "(最舊的往往是別場次、別麥克風錄的,那種最難再取得)")
    lines += [
        "",
        "**只有在它們本來就是同一個人**(兩種寫法)時才該按下去。"
        "如果是兩個不同的人,合併之後會變成「一個名字底下裝了兩個人」,"
        "那種錯在逐字稿裡看起來只是「少了一個人」,很難發現。",
    ]
    return "\n".join(lines)


def apply_rename(pending):
    """把待確認的改名同步到聲紋庫;回傳(訊息, 提示區, 兩顆鈕, 待確認,
    兩個聲紋下拉, 聲紋摘要)。

    這條路現在只剩**合併**在走(純改名已在儲存那一步隨勾選一起做完,見
    `save_attendees`)。名單在儲存那一步就已經改好了,這裡只補聲紋庫那一半。"""
    if not pending:
        return ("", *_merge_ask_hidden(), gr.update(), gr.update(), gr.update())
    old, new = pending
    moved = voiceprints_store.rename(old, new)
    # ⚠️ 數樣本要用 load()[0](完整清單);known_names() 是**去重**的名單,
    # 拿它 count 永遠得到 1——畫面會寫成「5 個聲紋樣本(目前共 1 個)」
    all_names, _ = voiceprints_store.load()
    return (
        f"已把「{old}」的 {moved} 個聲紋樣本改掛到「{new}」"
        f"(目前共 {all_names.count(new)} 個)。",
        *_merge_ask_hidden(),
        *_name_dropdowns(),
        # 合併完成 = 名單與聲紋庫重新對上了,摘要那句提醒要跟著消失
        gr.update(value=vp_summary()),
    )


def dismiss_rename(pending):
    """「暫不修改」:把合併追問收起來,聲紋庫**一個位元組都不動**。

    ⚠️ 這不是「什麼都沒發生」——名單那一半在儲存那一步就已經改掉了,所以
    按下去等於**留下一個對不上的狀態**。訊息必須講清楚,而中間欄的摘要會
    立刻長出「只在聲紋庫、不在名單上」(見 `orphan_names`):追問消失了,
    這件事沒有消失。

    為什麼要有這顆鈕:少了它,不想合併的人只能不理那塊追問(它會一直留到
    下次儲存),於是「他決定不改」與「他根本沒看到」在畫面上長得一模一樣
    ——而安全網那句提醒正是靠這個差別才有意義。"""
    if not pending:
        return ("", *_merge_ask_hidden(), gr.update())
    old, _new = pending
    moving = list(voiceprints_store.load()[0]).count(old)
    return (
        f"沒有動聲紋庫:「{old}」的 {moving} 個樣本還掛在舊名字下,"
        "下次開會會認不出他。要補做的話用最右邊的「修改名稱作業」。",
        *_merge_ask_hidden(),
        gr.update(value=vp_summary()),
    )


def vp_rename_prefill(picked):
    """選了人就把名字帶進「改成」那格(使用者指定 2026-08-08)。

    多數改名只是**補幾個字**(加部門、修錯字),從零打起反而麻煩;帶進去
    之後游標點一下就能改。掛 `.input` 只在使用者真的挑人時觸發,程式化
    更新(改完清空選單)不會誤把空值蓋掉他剛打的東西。"""
    return gr.update(value=picked or "")


def vp_rename(old, new):
    """聲紋區的改名:單純改名就直接做,**合併則先問**。

    回傳(訊息, 確認鈕, 待確認的改名, 刪除用下拉, 改名用下拉, 新名字欄,
    名單表格, 聲紋摘要)。兩個下拉都要更新——只更新一個的話,另一個選單裡會
    留著已經不存在的舊名字,而使用者下次就是從那裡挑人。改完也要**清空新
    名字欄**:留著上一次的字,下次改別人時很容易直接按下去(使用者
    2026-08-08 回報)。摘要也要更新:這裡正是修好「只在聲紋庫、不在名單上」
    的地方(見 vp_summary),修好了那句提醒就該消失。"""
    old = (old or "").strip()
    new = (new or "").strip()
    nochange = (gr.update(),) * 5
    if not old or not new:
        return ("請先選一個人、並填入新名字。", gr.update(visible=False), None,
                *nochange)
    if old == new:
        return ("新名字與原本相同,沒有變更。", gr.update(visible=False), None,
                *nochange)
    plan = voiceprints_store.rename_plan(old, new)
    if plan.moving == 0:
        return (f"聲紋庫裡找不到「{old}」。", gr.update(visible=False), None,
                *nochange)
    if plan.merges:
        # 合併:停下來問,把數字講清楚
        return (_rename_prompt(old, new, plan), gr.update(visible=True),
                (old, new), *nochange)
    return _do_vp_rename(old, new)


def vp_rename_confirm(pending):
    """合併的第二段:使用者按了「確認合併」才真的動手。"""
    if not pending:
        return ("", gr.update(visible=False), None, *((gr.update(),) * 5))
    return _do_vp_rename(*pending)


def _do_vp_rename(old: str, new: str):
    """真正改名:聲紋庫與名單**一起**改。

    只改一邊會讓下拉選單與自動填名對不起來——選得到新名字卻認不出人,
    或是名單上還留著已經不存在的舊名字。"""
    moved = voiceprints_store.rename(old, new)
    attendees.rename(old, new)
    # 落地的命名草稿也要跟著改,否則舊名字會在下次開頁時復活成第二個身分
    # (見 pending.rename_draft)
    pending.rename_draft(old, new)
    # ⚠️ 同 apply_rename:總數要數完整清單,known_names() 去重過
    all_names, _ = voiceprints_store.load()
    return (
        f"已把「{old}」改名為「{new}」:{moved} 個聲紋樣本"
        f"(目前共 {all_names.count(new)} 個),與會名單也一併更新。",
        gr.update(visible=False), None,
        *_name_dropdowns(),                      # 兩個下拉一起更新
        gr.update(value=""),                     # 新名字欄:清空,免得被沿用
        gr.update(value=attendee_rows()),
        gr.update(value=vp_summary()),           # 「只在聲紋庫」那句要跟著消失
    )


# ---- 聲紋資料管理 ----

# 摘要那行最多點名幾個「只在聲紋庫」的人:欄寬只有 454px,列到第四個
# 就把摘要撐成五、六行,而使用者要的是「有幾個、大概是誰」——完整清單
# 本來就在下面那個下拉裡
_ORPHANS_SHOWN = 3


def orphan_names() -> list[str]:
    """有聲紋、卻**不在**與會名單上的名字(照聲紋庫的名字排序)。

    這是「名單與聲紋庫失聯」的安全網(2026-08-09 使用者選定,與方案 D
    一起做):改名只改了一邊、用記事本直接編過 `attendees.txt`、名單裡
    刪掉一個還有聲紋的人——所有來源都會在這裡浮出來。它也是唯一救得回
    **已經發生**那幾筆的東西:失聯本身沒有任何症狀,要等下次開會那個人
    認不出來才會發現,而那時沒有人會聯想到幾週前改過名字。

    ⚠️ **方向只能是單向的**。反過來(名單上有、聲紋庫沒有)是**常態**——
    名單是「可能出席者」,新同事還沒開過會本來就沒有聲紋(實測使用者的
    64 人名單對 59 人聲紋,這個方向天天成立)。兩邊都報等於永遠亮著,
    然後就被當成背景雜訊。"""
    roster = set(attendees.load())
    return [n for n in voiceprints_store.known_names() if n not in roster]


def vp_summary() -> str:
    """一行總數(使用者選定 2026-07-19):樣本數有 5 個上限、顯示無行動
    意義;名字牆會隨使用無限變長——完整名單由下方刪除下拉承擔
    (點開即列全部、支援打字搜尋),此處永遠一行。

    **健檢的結果順帶掛在這一行**(使用者選定 2026-08-07「主動提醒」):
    整件事的教訓就是「錯誤沒有被看見」——B 總在逐字稿裡消失了好幾個月,
    不是因為工具查不出來,是因為沒有人被告知。健檢若做成「要自己想到
    去按」的功能,等於把同一個問題再犯一次。掃描是純矩陣運算(實測 138
    個樣本 <0.1 秒),付得起每次開分頁掃一遍;算不出來就當作沒事,
    絕不能讓健檢擋住這一行本來要講的話。

    **「只在聲紋庫、不在名單上」也掛在這一行**(2026-08-09,同一個理由):
    它是名單與聲紋庫失聯的安全網,見 `orphan_names`。排在健檢那句**前面**
    ——它指得出是誰、也指得出怎麼修,而健檢那句要使用者再按一顆鈕。"""
    n = len(voiceprints_store.known_names())
    if n == 0:
        return "目前尚無登記任何聲紋。"
    line = f"目前已登記 **{n} 人**的聲紋;完整名單見下方選單(可打字搜尋)。"
    orphans = orphan_names()
    if orphans:
        shown = "、".join(f"「**{o}**」" for o in orphans[:_ORPHANS_SHOWN])
        rest = len(orphans) - _ORPHANS_SHOWN
        if rest > 0:
            shown += f"、等 {rest} 人"
        line += (
            f"\n\n⚠️ 有 **{len(orphans)} 個名字**只在聲紋庫、不在名單上:{shown}"
            "——多半是改名時只改了一邊,用右邊的「修改名稱作業」"
            "改成名單上的名字即可。"
        )
    try:
        found = len({s.name for s in voiceprints_store.suspects()})
    except Exception:  # noqa: BLE001 - 健檢是附加資訊,不能連坐主要內容
        logger.debug("聲紋健檢摘要失敗", exc_info=True)
        return line
    if found:
        # 「下面的」三個字是實測補的:鈕在下拉選單之後,只寫「按健檢」
        # 使用者得越過一個大欄位去找(Playwright 截圖上看得很清楚)
        line += (
            f"\n\n⚠️ 健檢發現 **{found} 個人**的聲紋可能混到別人,"
            "按下面的「健檢」看看。"
        )
    return line


# ---- 聲紋健檢 ----
#
# 版面是使用者 2026-08-07 從三個設計稿選的「方案 A:就地展開」(不新增
# 分頁,結果展在現有按鈕列下面、直接勾選要刪的)。
#
# ⚠️ **文案比版面重要**:聲紋庫只存 192 維向量、**不存聲音**,所以健檢
# 結果沒辦法試聽——使用者唯一的判斷依據就是這裡寫的字。故每一筆都要
# 講完三件事:哪個樣本、為什麼可疑(聽起來更像誰)、勾下去會發生什麼。
# 相似度數字放在句尾括號裡當佐證,不當主詞。

# 「清掉重複樣本」那一項的鍵。用 \t 開頭:人名不可能有 tab,撞不到
_DUP_KEY = "\t\t重複"


def _suspect_key(name: str, index: int) -> str:
    """勾選項的值。**帶得回 (名字, 第幾個)** 才刪得掉,而顯示文字會隨
    文案改寫,不能拿來當鍵。"""
    return f"{name}\t{index}"


def _parse_key(key: str) -> tuple[str, int]:
    name, _, idx = key.rpartition("\t")
    return name, int(idx)


def vp_health_report() -> tuple[str, "gr.update", "gr.update"]:
    """跑健檢,回傳 (報告 Markdown, 勾選清單更新, 刪除鈕更新)。"""
    try:
        found = voiceprints_store.suspects()
        dups = voiceprints_store.duplicates()
    except Exception:  # noqa: BLE001
        logger.warning("聲紋健檢失敗", exc_info=True)
        return (
            "健檢沒有跑完(聲紋庫可能損壞)。可以按「重新載入」再試一次。",
            gr.update(choices=[], value=[], visible=False),
            gr.update(visible=False),
        )

    total = len(voiceprints_store.load()[0])
    people = len(voiceprints_store.known_names())
    lines: list[str] = []
    choices: list[tuple[str, str]] = []

    by_name: dict[str, list] = {}
    for s in found:
        by_name.setdefault(s.name, []).append(s)

    all_names = voiceprints_store.load()[0]
    for name, items in by_name.items():
        # ⚠️ 星號一律在全形引號**內側**:`字**「詞」**` 在 CommonMark 的
        # flanking 規則下不構成粗體,gradio 與 GitHub 都會把星號原樣印出
        # (CLAUDE.md 的硬性慣例,測試掃全部 UI Markdown 守著)
        likes = "、".join(f"「**{s.like_name}**」" for s in items)
        plain = "、".join(f"「{s.like_name}」" for s in items)
        if voiceprints_store.whole_name_suspect(name, found):
            title = f"「{name}」的 {len(items)} 個樣本全部可疑"
            why = (
                f"這些樣本聽起來分別更像{likes},而且彼此也不像——"
                "這通常代表這個名字底下根本不是同一個人。"
            )
            act = f"勾選它 = 刪掉「{name}」的全部聲紋,下次開會重新命名一次。"
            label = f"刪掉「{name}」的全部 {len(items)} 個樣本(都更像別人)"
        else:
            nums = "、".join(f"第 {s.index + 1} 個" for s in items)
            keep = all_names.count(name) - len(items)
            title = f"「{name}」的{nums}樣本"
            why = f"聽起來更像{likes},反而和他自己的其他樣本不像。"
            act = (
                f"勾選它 = 只刪這 {len(items)} 個,其餘 {keep} 個保留,"
                "以後照樣認得出他。"
            )
            label = f"刪掉「{name}」的{nums}樣本(更像{plain})"
        # 佐證數字用純 Markdown 不用 <sub>:gradio 的 Markdown 對 HTML 的
        # 處理沒實測過,萬一被當純文字印出來,畫面上就會出現一串標籤。
        # 多筆之間用**全形分號**不用頓號:一筆本身就含頓號(「與 X、與自己人」),
        # 全用頓號串起來會讀不出一組到哪裡結束。半形 ; 夾在中文裡會貼字
        # (Playwright 截圖上看到「0.33;與」),使用者可見文字一律全形標點
        detail = "；".join(
            f"與「{s.like_name}」{s.like_sim:.2f}、與自己人 {s.own_sim:.2f}"
            for s in items
        )
        lines.append(f"- **{title}**  \n  {why}  \n  {act}  \n  *({detail})*")
        choices.append((label, _suspect_key(name, items[0].index)))

    head = []
    if by_name:
        head.append(f"### 健檢結果 — {len(by_name)} 項要你判斷\n")
        head.append(
            "勾選你確認要刪掉的,再按下面的按鈕。**判斷不出來就先別勾**——"
            "留著只是偶爾認錯人(而且認錯你看得到、可以當場改),"
            "刪錯了那個人就得重新命名一次。\n"
        )
    else:
        head.append("### 健檢結果\n")
        head.append("**聲紋庫看起來正常,沒發現需要處理的問題。**\n")

    tail = [f"\n共檢查 {total} 個聲紋樣本、{people} 個人。"]
    if dups:
        names = "、".join(sorted({d.name for d in dups}))
        tail.append(
            f"另外有 {len(dups)} 個**完全重複**的樣本({names}),"
            "已一併列在上面的勾選清單裡；刪掉它們不影響任何辨識結果。"
        )
        choices.append((
            f"清掉 {len(dups)} 個完全重複的樣本(不影響辨識,可安心勾)",
            _DUP_KEY,
        ))
    else:
        tail.append("沒有重複的樣本。")

    report = "\n".join(head) + "\n".join(lines) + "\n" + "  \n".join(tail)
    return (
        report,
        gr.update(choices=choices, value=[], visible=bool(choices)),
        gr.update(visible=bool(choices)),
    )


def vp_names_in_roster_order() -> list[str]:
    """有聲紋的人,照**與會名單的順序**排(使用者指定 2026-08-08)。

    左邊名單是使用者自己排的順序(`attendees.load()` 保留加入順序),右邊
    下拉若照字典序排,同一批人在兩邊的位置對不起來——要在幾十個人裡找同
    一個人,得重新掃一遍。

    ⚠️ **兩份名單不是同一個東西**:聲紋庫是「有聲紋的人」,與會名單是
    「可能出席的人」,多數重疊但不保證。所以有聲紋卻不在名單的接在後面
    (照原本的字典序),**一個都不能少**——那些人照樣要改得到名字。"""
    known = voiceprints_store.known_names()      # 已去重、字典序
    in_roster = set(known)
    ordered = [n for n in attendees.load() if n in in_roster]
    seen = set(ordered)
    return ordered + [n for n in known if n not in seen]


def _name_dropdowns():
    """兩個「聲紋名字」下拉的更新:刪除用(多選)與改名用(單選)。

    ⚠️ **這是唯一的產生點**,任何動到聲紋庫名字清單的 handler 都要用它。
    2026-08-08 踩過:新增「改名」那個下拉時只在它自己那條路更新,
    刪除／清除／重新載入／健檢刪除／名單改名同步**全都漏了**——使用者
    改完名字,發現右邊選單裡還是舊名字。「靠人記得」正是這種事的成因
    (同 `_servable` 那條規則的教訓);接線那邊由測試反向守著,凡是把
    刪除下拉掛進 outputs 的,也必須掛改名下拉。"""
    names = vp_names_in_roster_order()
    return gr.update(choices=names, value=[]), gr.update(choices=names, value=None)


def vp_health_apply(selected):
    """刪掉勾選的項目,回傳 (報告, 勾選清單, 刪除鈕, 人名下拉, 摘要)。"""
    picked = list(selected or [])
    items: list[tuple[str, int]] = []
    # 只算一次:每個勾選項各算一遍是 O(n²) 掃全庫,而且刪除是在最後才做,
    # 算出來的結果本來就該是同一份快照
    found = voiceprints_store.suspects() if picked else []
    all_names = voiceprints_store.load()[0] if picked else []
    for key in picked:
        if key == _DUP_KEY:
            items += [(d.name, d.drop) for d in voiceprints_store.duplicates()]
            continue
        name, _idx = _parse_key(key)
        if voiceprints_store.whole_name_suspect(name, found):
            # 整個名字都不可信:刪光,別留下一兩個沒過門檻的殘骸
            items += [(name, k) for k in range(all_names.count(name))]
        else:
            items += [(s.name, s.index) for s in found if s.name == name]
    removed = voiceprints_store.delete_samples(items)
    report, choices, btn = vp_health_report()
    note = f"已刪除 {removed} 個聲紋樣本。\n\n" if removed else ""
    return (note + report, choices, btn, *_name_dropdowns(), vp_summary())


def delete_voiceprints(selected):
    for name in selected or []:
        voiceprints_store.delete(name)
    return (*_name_dropdowns(), vp_summary())


# 「清除全部聲紋」的確認旗標:前端 confirm 的結果經一個隱藏 Textbox 送進來
# (接線與理由見 app.py)。js 與 Python 兩邊必須對同一個字串,故只有這一個出處
CLEAR_CONFIRMED = "confirmed"


def clear_voiceprints(confirmed: str = ""):
    """清除全部聲紋(破壞性)。⚠️ **只有拿到 `CLEAR_CONFIRMED` 才動手**。

    確認框仍在前端(見 app.py 接線的 js),但 2026-08-09 起**按取消時本函式
    照樣會被呼叫**——原本的 js 是 `throw` 中止派發(伺服器端連呼叫都不會
    發生),而 gradio 6.20 把 js 的例外當成事件失敗,在**每一個 output** 上
    畫一個紅色「錯誤」(使用者截圖回報:三個 output 就是三個)。

    所以判準寫成**正面表列**:等於那個字串才清除;其餘(空字串、None、
    旗標沒送到、元件沒渲染……)一律當成取消,回 `gr.skip()` 讓畫面原樣不動。
    ⚠️ **不可改寫成「不等於某個取消值才清除」**——那會讓任何意外都倒向
    「清掉整個聲紋庫」,而這個操作不可回復(使用者得重新累積每個人的聲紋)。
    隱藏旗標的建構值正是空字串,所以「js 沒跑到」的預設行為也是不清除。
    """
    if confirmed != CLEAR_CONFIRMED:
        return gr.skip(), gr.skip(), gr.skip()
    voiceprints_store.clear()
    return (*_name_dropdowns(), vp_summary())


def reload_voiceprints():
    return (*_name_dropdowns(), vp_summary())


# ---- 詞表檔案維護(hotwords.txt / replace.txt)----

def read_data_file(path) -> str:
    try:
        # utf-8-sig:編輯區讀到的第一個字如果是 BOM,存回去就多一個
        # (同 attendees.load;那份 docstring 有完整理由)
        return path.read_text(encoding="utf-8-sig")
    except OSError:  # 檔案不存在:給空編輯區,存檔時建立
        return ""


def hotwords_status() -> str:
    """詞表統計與預算預警:超出 token 預算的尾端詞會被引擎靜默截斷,
    這裡要在存檔當下就講明白,不能等使用者轉完檔才發現詞沒生效。"""
    words = hotwords.load()
    if not words:
        return "目前詞表為空(功能等同關閉)。"
    joined = "、".join(words)
    msg = f"目前 {len(words)} 個詞,合計約 {len(joined)} 字(建議 ≤{hotwords.WARN_CHARS} 字)。"
    over = len(joined) - hotwords.WARN_CHARS
    if over > 0:
        msg += (
            f"**已超出約 {over} 字:尾端的詞會被引擎靜默忽略、等於沒加**"
            "——請把重要的詞往前放、刪掉不常用的。"
        )
    return msg


def replace_status() -> str:
    """替換表統計;缺「新詞」的行要點名(convert 只會在黑視窗記 warning,
    使用者在介面上看不到)。解析共用 convert.parse_rules,格式變更不會
    兩邊走鐘。"""
    f = convert.replace_file()
    if not f.exists():
        return "目前無替換表(功能等同關閉)。"
    rules, bad = convert.parse_rules(f.read_text(encoding="utf-8-sig"))
    msg = f"目前 {len(rules)} 條替換規則。"
    if bad:
        msg += (
            "**以下行缺「新詞」(格式:原詞 新詞),會被略過:**"
            + "、".join(f"「{b}」" for b in bad)
        )
    return msg


def save_hotwords(text) -> str:
    f = hotwords.store_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text if isinstance(text, str) else "", encoding="utf-8")
    return "已儲存,下一個轉錄的檔案生效。" + hotwords_status()


def reload_hotwords():
    return read_data_file(hotwords.store_file()), hotwords_status()


def save_replace(text) -> str:
    # 措辭是「轉換」不是「轉錄」:這張表文件轉檔也吃(見模組 docstring)
    f = convert.replace_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text if isinstance(text, str) else "", encoding="utf-8")
    return "已儲存,下一個轉換的檔案生效。" + replace_status()


def reload_replace():
    return read_data_file(convert.replace_file()), replace_status()
