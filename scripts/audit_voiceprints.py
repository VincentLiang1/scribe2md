r"""聲紋庫的體檢:找出「同一個名字底下裝了兩個人」的痕跡。

用法:
    uv run python scripts/audit_voiceprints.py                 # 只報告
    uv run python scripts/audit_voiceprints.py --drop-duplicates
    uv run python scripts/audit_voiceprints.py --drop "C 董事長#2" ...
    uv run python scripts/audit_voiceprints.py --against 某場會議.npz

**存在的理由**(使用者 2026-08-07 回報「錄音裡應該還有 B 總的聲音,但沒有
分出來」):查出來的不是講者分離的問題——分群把兩個人分得好好的,是**聲紋
辨識**把兩群都認成「C 董事長」,而那個名字底下早就混進了 B 總的樣本
(過去某次認錯人、確認命名,enroll 就把 B 總的聲紋存到董事長名下了)。
這種錯誤是**無聲**的:成品看起來只是「少了一個人」,沒有任何跡象。

⚠️ **這支工具不會自動判斷「哪個樣本是錯的」,那需要外部對照。**
2026-08-07 的實例正是反例:「C 董事長」5 個樣本裡,錯的(B 總)有
3 個、對的只有 2 個——任何「少數服從多數」的自動判準都會反過來把真的
那兩個刪掉。當時能判對,靠的是使用者指出「開場那段是 B 總、另一位與會者
00:07 才說董事長加入」,再拿那場會議兩群的質心回頭比每個樣本(見 --against)。

所以分工是:
- **自動處理**只做零風險的那一種:完全重複的樣本(相似度 ≥ 0.999,
  同一段聲紋被存了兩次,白佔名額)。
- **可疑的**列出「分簇結構」與「跟哪些別的名字像」,由人判斷。
- 判定之後用 `--drop "名字#序號"` 精準刪除(序號 = 本報告列出的序號)。

判準與門檻的來源(2026-08-07 實測,158 個樣本 / 60 人):
- 同一個人跨場次(不同麥克風/房間)仍有 0.46 以上(B 5 個樣本彼此
  最低 0.46);混進別人才掉到 0.26~0.36。故 `_SUSPECT_SIM` 取 0.40。
- 反過來**不能**用「跟別人像」當判準:原始 embedding 沒扣通道成分,
  同一場、同一支麥克風錄的不同人相似度天然就有 0.65~0.85,照那樣掃
  會把三分之一的庫標成可疑(實測留一法 155 個樣本中 46 個「認成別人」)。
"""
import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from meeting_scribe import stdio, voiceprints  # noqa: E402

# 報告與名字都是中文,輸出常被導向檔案(那時 stdout 退回 cp950)
stdio.force_utf8()

# ⚠️ **判準本身住在 `voiceprints.py`,不在這裡**:網頁介面的「健檢」
# (data_tabs.vp_health_report)與這支命令列吃的是同一份,兩邊各寫一套的話,
# 使用者在畫面上看到的「可疑」與這裡算出來的會不一樣。門檻的實測來源
# (為什麼是 0.20)寫在那邊的常數註解。這支只負責**報告怎麼排版**與 CLI。
_MISFILED_MARGIN = voiceprints._MISFILED_MARGIN
_LONE_MARGIN = voiceprints._LONE_MARGIN
_DUPLICATE_SIM = voiceprints._DUPLICATE_SIM
# 只有這一條是命令列獨有的:「同名樣本彼此不像」抓的是**另一種**東西
# (這個名字底下可能有兩個人),而它**說不出該刪哪一個**——所以不進網頁
# 介面:畫面上列一排「可疑但無從處理」的名字,只會讓人焦慮又動不了手。
# 分簇結構是坐下來查案時才看的,那時就該用這支。
_SUSPECT_SIM = 0.40


def _by_name(names: list[str]) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for i, n in enumerate(names):
        out.setdefault(n, []).append(i)
    return out


def _clusters(mat: np.ndarray, threshold: float) -> list[list[int]]:
    """把一個名字底下的樣本依相似度分簇(單一連結,只為了看結構)。

    用單一連結(連得上就同簇)而不是完整連結:這裡要回答的是「這批樣本
    能不能連成一片」,連不成才是「兩個人」的跡象;完整連結會因為一個
    品質差的樣本就把一片切碎,讀報告的人反而看不出重點。"""
    n = len(mat)
    parent = list(range(n))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i in range(n):
        for j in range(i + 1, n):
            if mat[i, j] >= threshold:
                parent[find(i)] = find(j)
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return sorted(groups.values(), key=len, reverse=True)


def report(names: list[str], vecs: np.ndarray, against: Path | None) -> None:
    by = _by_name(names)
    print(f"聲紋庫:{len(names)} 個樣本 / {len(by)} 人 "
          f"({voiceprints.store_file()})")

    dups = []
    for name, idx in by.items():
        M = vecs[idx]
        S = M @ M.T
        for a in range(len(idx)):
            for b in range(a + 1, len(idx)):
                if S[a, b] >= _DUPLICATE_SIM:
                    dups.append((name, a, b, float(S[a, b])))
    print(f"\n=== 完全重複的樣本({len(dups)} 對)===")
    print("   同一段聲紋被存了兩次,白佔名額。--drop-duplicates 可自動清掉")
    for name, a, b, s in dups:
        print(f"  {name}#{a} 與 #{b} 相似度 {s:.4f}")

    print(f"\n=== 同名樣本彼此不像的名字(最低 < {_SUSPECT_SIM})===")
    print("   ⚠️ 這只是「可疑」不是「有錯」:要刪哪一個必須有外部對照,")
    print("      別照多數決刪(檔頭有反例)。")
    suspect = []
    for name, idx in by.items():
        if len(idx) < 2:
            continue
        M = vecs[idx]
        S = M @ M.T
        off = S[~np.eye(len(idx), dtype=bool)]
        if float(off.min()) < _SUSPECT_SIM:
            suspect.append((float(off.min()), name, idx, S))
    for lo, name, idx, S in sorted(suspect):
        groups = _clusters(S, _SUSPECT_SIM)
        shape = " | ".join(
            "簇{" + ",".join(f"#{g}" for g in grp) + "}" for grp in groups
        )
        print(f"\n  {name}({len(idx)} 個樣本,彼此最低 {lo:.2f}):{shape}")
        for a in range(len(idx)):
            row = " ".join(f"{S[a, b]:5.2f}" for b in range(len(idx)))
            # 這個樣本最像的「別人」:判斷它是不是別人的聲音時的線索
            sims = vecs @ vecs[idx[a]]
            other = [(float(sims[j]), names[j]) for j in range(len(names))
                     if names[j] != name]
            s, who = max(other) if other else (0.0, "—")
            print(f"    #{a}: {row}   最像的別人 = {who}({s:.2f})")

    _report_misfiled(names, vecs, by)

    if against is not None:
        _report_against(names, vecs, against)


def _report_misfiled(names: list[str], vecs: np.ndarray,
                     by: dict[str, list[int]]) -> None:
    # 拿全部(門檻設極低)才看得到分佈與斷層——只印過線的那幾筆,讀報告的
    # 人無從判斷門檻訂得合不合理。判準本身仍是 voiceprints.suspects
    # 兩個門檻都放到最低才看得到完整分佈;判定仍用 voiceprints 的常數
    rows = [
        (s.gap, s.lone, s.name, s.index, s.own_sim, s.like_sim, s.like_name)
        for s in voiceprints.suspects(margin=-2.0, lone=-2.0)
    ]
    hard = [r for r in rows if r[0] >= _MISFILED_MARGIN and r[1] >= _LONE_MARGIN]
    print(f"\n=== 疑似存錯名字的樣本(差距 >= {_MISFILED_MARGIN} 且"
          f"孤例度 >= {_LONE_MARGIN}:{len(hard)} 個)===")
    print("   「差距」= 跟某個別人的相似度 － 跟自己人的相似度。為正代表")
    print("   這個樣本比較像那個別人。")
    print("   「孤例度」= 那個別人領先**第二個名字**多少。小 = 它跟一整群人")
    print("   一起變像(共用麥克風/遠端連線的通道成分),不是存錯名字的證據")
    print("   ——2026-08-08 誤刪過三個遠端樣本,就是少了這一欄。")
    print(f"   {'差距':>6} {'孤例':>6} {'名字':<20} {'#':>2} {'跟自己人':>7}  跟別人")
    for gap, lone, name, k, own, other, who in rows[:20]:
        if gap < 0.05:
            break
        mark = ("  ← 證據硬" if gap >= _MISFILED_MARGIN and lone >= _LONE_MARGIN
                else "  (孤例度不足,多半是通道效應)" if gap >= _MISFILED_MARGIN
                else "")
        print(f"   {gap:+6.2f} {lone:+6.2f} {name:<20} #{k} {own:7.2f}  "
              f"{who}({other:.2f}){mark}")
    if not hard:
        return
    gone = [n for n, idx in by.items()
            if all((n, k) in {(r[1], r[2]) for r in hard} for k in range(len(idx)))]
    if gone:
        print(f"\n   ⚠️ 這樣刪會讓 {len(gone)} 個名字整個消失"
              f"(下次開會要重新命名一次):{'、'.join(gone)}")
        print("      那代表這個名字底下**沒有一個樣本可信**——不是同一個人的"
              "幾份樣本,而是幾個不同的人被存到同一個名字底下。")
    print("\n   要刪的話(先自己看過再貼):")
    order = sorted(hard, key=lambda r: (r[1], r[2]))
    print("   uv run python scripts/audit_voiceprints.py --drop " +
          " ".join(f'"{n}#{k}"' for _g, n, k, _o, _x, _w in order))


def _report_against(names: list[str], vecs: np.ndarray, path: Path) -> None:
    """拿一場**已知誰是誰**的會議當外部對照,判每個樣本靠哪一群。

    npz 需要兩個陣列:`labels`(每群一個名稱)與 `centroids`(對應的聲紋
    質心,L2 正規化)。這正是 2026-08-07 判出「C 名下有 B 總樣本」的
    方法——沒有這種外部真值,聲紋庫自己說不出誰對誰錯。"""
    data = np.load(path, allow_pickle=True)
    labels = [str(x) for x in data["labels"]]
    cents = data["centroids"]
    print(f"\n\n=== 對照組 {path.name}({len(labels)} 群)===")
    print("   每個樣本靠哪一群 = 它其實是誰的聲音")
    by = _by_name(names)
    for name, idx in sorted(by.items()):
        sims = cents @ vecs[idx].T  # (群, 樣本)
        if sims.max() < 0.5:
            continue  # 這個人跟這場會議沒關係,不列
        print(f"\n  {name}:")
        for a in range(len(idx)):
            top = sorted(
                ((float(sims[g, a]), labels[g]) for g in range(len(labels))),
                reverse=True,
            )[:3]
            best_s, best_n = top[0]
            mark = f"   ← 靠向【{best_n}】" if best_s >= 0.5 else ""
            print(f"    #{a}: " + "  ".join(f"{n}={s:.2f}" for s, n in top) + mark)


def _drop(names: list[str], vecs: np.ndarray, specs: list[str]) -> None:
    """刪掉指定的樣本;spec 格式「名字#序號」,序號同報告。"""
    by = _by_name(names)
    items: list[tuple[str, int]] = []
    for spec in specs:
        name, _, num = spec.rpartition("#")
        if not name or not num.isdigit() or name not in by:
            raise SystemExit(f"看不懂的指定:{spec}(格式應為「名字#序號」)")
        k = int(num)
        if k >= len(by[name]):
            raise SystemExit(f"{name} 只有 {len(by[name])} 個樣本,沒有 #{k}")
        items.append((name, k))
    _write(names, items)


def _drop_duplicates(names: list[str], vecs: np.ndarray) -> None:
    """每組完全重複的樣本只留第一個(零風險:留下的那個一模一樣)。"""
    items = [(d.name, d.drop) for d in voiceprints.duplicates()]
    if not items:
        print("\n沒有完全重複的樣本,不動。")
        return
    _write(names, items)


def _write(names: list[str], items: list[tuple[str, int]]) -> None:
    """實際刪除走 `voiceprints.delete_samples`(序號位移的換算在那裡做,
    只有一份)。這裡只負責印出動了誰、以及提醒 commit。"""
    print(f"\n刪除 {len(items)} 個樣本:")
    for name, k in sorted(items):
        print(f"  {name}#{k}")
    if len(items) >= len(names):
        raise SystemExit("不能把聲紋庫清空,已中止")
    removed = voiceprints.delete_samples(items)
    print(f"樣本總數 {len(names)} → {len(names) - removed},"
          f"已寫回 {voiceprints.store_file()}")
    print("⚠️ data/voiceprints.npz 隨 repo 版控,記得 commit(訊息寫清楚動了誰)")


def _report_margins(names: list[str], vecs: np.ndarray) -> None:
    """校準 `voiceprints._RUNNER_UP_MARGIN`:留一法掃各種 margin 的取捨。

    **單樣本的名字要排除**:它在留一法裡必定「認成別人」(自己被拿走了,
    庫裡根本沒有正確答案),算進去會讓每一格的誤認數都虛高,而虛高的方向
    正好是「看起來門檻該調更嚴」——那會把一個本來就偏保守的判準推過頭。"""
    by = _by_name(names)
    evaluable = [i for i in range(len(names)) if len(by[names[i]]) >= 2]
    rows = []
    for i in evaluable:
        keep = [j for j in range(len(names)) if j != i]
        sims = vecs[keep] @ vecs[i]
        top = keep[int(np.argmax(sims))]
        best_name, best = names[top], float(sims.max())
        other = [float(s) for s, j in zip(sims, keep) if names[j] != best_name]
        rows.append((best, best - (max(other) if other else -1.0),
                     best_name == names[i]))
    ok = [r for r in rows if r[2]]
    bad = [r for r in rows if not r[2]]
    print(f"\n=== 次佳差距(margin)校準:留一法 {len(rows)} 個可評樣本"
          f"(單樣本的 {len(names) - len(rows)} 個已排除)===")
    print(f"門檻 {voiceprints._MATCH_THRESHOLD:.2f} 之上:認對 {len(ok)}、"
          f"認錯 {len(bad)}")
    print(f"{'margin':>7} {'正確保留':>16} {'誤認保留':>16} {'精確率':>7}")
    for mg in (0.0, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.10, 0.12, 0.15):
        a = sum(1 for r in ok if r[0] >= voiceprints._MATCH_THRESHOLD and r[1] >= mg)
        b = sum(1 for r in bad if r[0] >= voiceprints._MATCH_THRESHOLD and r[1] >= mg)
        mark = " ←目前" if abs(mg - voiceprints._RUNNER_UP_MARGIN) < 1e-9 else ""
        print(f"{mg:>7.2f} {a:>7}/{len(ok):<8} {b:>7}/{len(bad):<8} "
              f"{(100 * a / (a + b) if a + b else 0):>6.1f}%{mark}")


def main() -> None:
    ap = argparse.ArgumentParser(description="聲紋庫體檢")
    ap.add_argument("--drop", nargs="+", metavar="名字#序號",
                    help="精準刪除指定樣本(序號同報告)")
    ap.add_argument("--drop-duplicates", action="store_true",
                    help="清掉完全重複的樣本(零風險)")
    ap.add_argument("--against", type=Path, metavar="對照.npz",
                    help="拿一場已知誰是誰的會議判每個樣本其實是誰的")
    ap.add_argument("--margins", action="store_true",
                    help="校準自動命名的「次佳差距」門檻(留一法對照表)")
    args = ap.parse_args()

    names, vecs = voiceprints.load()
    if not names:
        raise SystemExit("聲紋庫是空的")
    if args.margins:
        _report_margins(names, vecs)
        return
    report(names, vecs, args.against)
    if args.drop:
        _drop(names, vecs, args.drop)
    elif args.drop_duplicates:
        _drop_duplicates(names, vecs)
    else:
        print("\n(只報告,沒有動任何東西;要刪請加 --drop 或 --drop-duplicates)")


if __name__ == "__main__":
    main()
