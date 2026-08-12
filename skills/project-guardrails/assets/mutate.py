r"""測試的反向稽核:把程式故意改壞,確認對應的測試真的會變紅。

【這是範本】複製到專案的 `scripts/` 之後要改兩個地方:
  1. `SRC` 常數 —— 指到你的原始碼目錄
  2. `MUTANTS` 清單 —— 清掉範例,寫你自己的(下面留了三條當格式參考)

用法:
    python scripts/mutate.py              # 全部跑一輪
    python scripts/mutate.py M1 M3        # 只跑指定幾個
    python scripts/mutate.py --list       # 列出清單,不動任何檔案
    python scripts/mutate.py --who M3     # 逃掉時:跑全套並印出誰真的紅了

**存在的理由是「事後補的測試從來沒有被看過失敗」**:多數專案的測試是
test-after ——先把功能寫完、跑通了才補測試。那種測試很容易變成「描述程式碼
剛好做了什麼」而不是「描述它應該做什麼」,而**它永遠是綠的,包括在程式壞掉
的時候**。AI 寫的測試尤其如此:它看得到實作。

真實案例(來源專案):一個「有沒有做過轉換」的欄位被寫死成永遠回報「沒有」,
等於每份產物都謊報——**974 條測試全綠**。因為那條測試用假引擎整支換掉了
被測函式,**真正做計算的那一行從頭到尾沒被執行**。
一句話:**假引擎測得到接線,測不到計算。**

**逃掉的有三種,報告分不出來**:

1. **真的沒人守** → 補測試。
2. **常數被「符號引用」**(測試拿同一個常數去算輸入再斷言,門檻怎麼改都
   跟著動)→ 補**關係式斷言**(上下界 + 實測理由),不是把數字抄第二遍。
3. **突變本身是 no-op**(例如把死程式插在真程式前面,真的那支還在)
   → 改突變,不是改測試。

`--who` 分得出第 1 種與「只是綁錯測試」(它施加突變後跑**整套**並印出實際
變紅的測試名:整套會紅 = 綁錯;整套也全綠才是真的洞)。第 3 種它分不出來,
只能回頭讀突變後的程式碼確認它真的壞了。

**報告有三種狀態,BROKEN 一定要跟 GREEN 分開**:RED = 測試抓到了;
GREEN = 逃掉了(上面那三種);**BROKEN = 這條突變自己跑不起來**,報告對它
沒有意義。混在一起的代價是**兩種假訊號**,而且方向相反:pytest 的 exit 2~5
是它自己出問題(4 = node id 打錯或測試函式改名,5 = 沒收集到測試),算成
RED 就是**假紅**——一個沒人守的風險看起來有人守;反過來,突變的 `old` 隨著
程式改動漂掉,算成 GREEN 就是**假洞**——叫人去補一條其實已經存在的測試。

`main()` 開頭會先跑 `_validate()`,把清單自己的四種失效當場擋下來(撞號、
目標字串漂掉、綁到改名過的測試、no-op 突變)。**每一種的症狀都是「報告照印,
只是失去意義」**,所以要擋在工具裡——新增突變的人跑的是這支腳本。

⚠️ **突變值本身別踩邊界**:把保留天數改成 0 會讓 cutoff 正好等於「現在」,
測試紅不紅取決於幾微秒的時間差——兩種結果都會出現,一度把真的洞誤判成
有人守著。

**沒被戳過的模組等於還沒驗收過**:某一輪 25/25 全紅,一算才發現 45 個測試
模組只戳過 9 個——**70% 的測試住在沒被戳過的模組裡**。加新突變時優先挑那些。

**什麼時候跑**:補完一批測試之後、或改動了帶有回歸防線的邏輯之後。不必每次
提交都跑——它不是 CI 的一部分,是「這批測試到底有沒有在守東西」的一次性驗收。

**新增功能時請一起加突變**:一條測試沒有被突變戳過,就只是「有跑過」而已。
挑法是「如果這一行被寫錯/拿掉,使用者會遇到什麼事」,那句話就是 `why`。

安全性:每個突變都是「改 → 只跑該測試 → **在 finally 裡還原**」,所以中斷
(Ctrl-C、當機)都不會留下壞掉的原始碼;還原是寫回一開始讀進記憶體的那份
原文,**逐位元組**寫回(不依賴 git,所以未提交的工作也不會被動到)。
"""
import argparse
import ast
import collections
import functools
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "你的套件名"          # ← 改這裡
TESTS_DIR = "tests"                        # ← 測試目錄(給 --who / --full 用)


class Mutant:
    """一個突變:把 `old` 換成 `new`,期待 `tests` 裡至少有一條變紅。

    `why` 寫的是「這樣改壞之後,使用者會遇到什麼事」——不是「改了哪一行」。
    報告只印 why,因為看報告的人要判斷的是「這個風險有沒有人守著」。
    """

    def __init__(self, tag, filename, old, new, why, tests):
        self.tag = tag
        # 帶 "/" 的當成相對 repo 根目錄(`scripts/` 底下的開發工具也要能戳);
        # 純檔名是 SRC 底下
        self.path = (ROOT / filename) if "/" in filename else (SRC / filename)
        self.old = old
        self.new = new
        self.why = why
        self.tests = tests

    def resolve(self, text):
        """把 old/new 的換行對齊目標檔的行尾;找不到 old 就回 None。

        ⚠️ **`_apply` 與 `_validate` 一定要共用這一份**:只檢查「檔案在不在」
        是不夠的,真正會漂掉的是**字串**——來源專案一查就有五條的 old 早已
        不存在,而它們每次都被算成「逃掉」(叫人去補一條其實已經存在的測試)。
        兩邊各寫一份的話,還會出現「驗證說目標字串在、實際跑卻找不到」。
        行尾為什麼要跟著原檔見 `_apply`。"""
        eol = "\r\n" if "\r\n" in text else "\n"
        old_s = self.old.replace("\n", eol)
        if old_s not in text:
            return None
        return old_s, self.new.replace("\n", eol)


# ⚠️ 以下三條是**格式範例**,複製到新專案後請整批換掉。
#    挑法:「如果這一行被寫錯,使用者會遇到什麼事?」答得出來的才值得寫。
MUTANTS = [
    # 型一:把計算結果寫死 —— 抓「假引擎測不到計算」那種假綠燈
    Mutant("M1", "example.py",
           "    changed = original != converted",
           "    changed = False",
           "產物謊報「沒有被轉換過」(下游據此決定要不要再處理一次)",
           [f"{TESTS_DIR}/test_example.py::test_reports_whether_conversion_changed_text"]),

    # 型二:把條件關掉 —— 抓「失敗時無聲」那種缺口
    Mutant("M2", "example.py",
           "    if result.degraded:",
           "    if False:",
           "降級時不留標記 → 內容無聲丟失,產出看起來完全正常",
           [f"{TESTS_DIR}/test_example.py::test_marks_degraded_output"]),

    # 型三:把保護整段拿掉 —— 抓「破壞性操作的護欄」
    Mutant("M3", "example.py",
           '    if not (target / MARKER).exists():\n        raise',
           '    if False:\n        raise',
           "沒有標記檔也照樣刪目錄(刪掉使用者自己的資料夾)",
           [f"{TESTS_DIR}/test_example.py::test_refuses_to_delete_without_marker"]),
]


def _run(tests: list[str], names: bool = False) -> tuple[str, str]:
    """跑指定測試,回 (red/green/broken, 最後一行輸出)。

    `names=True` 改回傳**實際變紅的測試名**(給 --who 用)。

    `--no-cov`:覆蓋率門檻是整個套件的,只跑三條測試必定不足門檻,
    不關掉的話每個突變都會「因為覆蓋率」變紅,整份報告就沒有意義了。
    (專案沒裝 pytest-cov 的話拿掉這個參數。)

    ⚠️ `PYTHONDONTWRITEBYTECODE`:**沒有它會毒到後面所有的執行**。Python
    判斷 `.pyc` 有沒有過期只看 **(mtime, size)**,而突變常常是等長替換
    (`if audio:` → `if False:` 剛好都是 5 個字元),還原又發生在同一秒內
    ——於是還原後的原始碼配上「從突變版編出來的 bytecode」被判定為有效,
    **程式碼看起來完全正常、測試卻紅著**,而且怎麼讀原始碼都找不出原因。
    不寫 .pyc 就不會留下那個快取。
    """
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header", "--tb=no", "-rf",
         "-p", "no:cacheprovider", "--no-cov", *tests],
        cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    out = r.stdout.strip().splitlines() or [""]
    # ⚠️ **只有 exit 1 才是「測試紅了」**:pytest 的 2~5 是它自己跑不起來
    # ——4 = usage error(綁定的 node id 打錯、測試函式改名),5 = no tests
    # collected。把那些算成 RED 等於**假紅**:一個沒人守的風險看起來有人守,
    # 比沒有突變更糟。實測:綁一個不存在的測試名 → exit 4,舊寫法照樣報 RED
    status = {0: "green", 1: "red"}.get(r.returncode, "broken")
    if names:
        failed = [ln.split()[1] for ln in out if ln.startswith("FAILED ")]
        return status, "\n      ".join(failed) or out[-1]
    return status, out[-1]


def _drop_bytecode(path: Path) -> None:
    """丟掉這支檔案的 `.pyc`(見 _run 的 PYTHONDONTWRITEBYTECODE 說明)。

    第二道保險:子行程已經不寫 .pyc 了,但這台機器上可能還留著先前被毒過
    的快取,而那個狀態沒有任何跡象、只會表現成「測試莫名其妙紅了」。"""
    try:
        Path(importlib.util.cache_from_source(str(path))).unlink(missing_ok=True)
    except OSError:  # pragma: no cover — 唯讀目錄之類
        pass


def _apply(m: Mutant, tests=None, names: bool = False) -> tuple[str, str]:
    """施加突變 → 跑測試 → **必定還原**。回 (red/green/broken, 說明)。"""
    original = m.path.read_bytes()          # **逐位元組**讀寫,見下
    # ⚠️ 行尾一定要跟著原檔:`read_text`/`write_text` 一來一回會把 LF 檔
    # 換成 CRLF(Windows 的 os.linesep 轉換)。`git diff` 看不到(git 自己
    # 正規化),但工作區的檔案**真的被改寫了**,`git status` 會莫名其妙標一排
    # M 而 diff 是空的。突變字串裡的換行因此也要跟著轉,否則 CRLF 檔比對不到、
    # 整條突變會靜靜地變成「找不到要突變的字串」(換行對齊見 Mutant.resolve)
    text = original.decode("utf-8")
    located = m.resolve(text)
    if located is None:
        # ⚠️ 這是 **broken 不是 green**:突變自己壞了,不代表沒人守著那個風險
        return "broken", "找不到要突變的字串(程式碼改過了,請更新這條突變)"
    old_s, new_s = located
    try:
        m.path.write_bytes(text.replace(old_s, new_s, 1).encode("utf-8"))
        status, tail = _run(tests or m.tests, names)
    finally:
        # 還原寫在 finally:中斷也不留下壞掉的原始碼。
        # 連 .pyc 一起丟掉——只還原原始碼是不夠的(見 _drop_bytecode)
        m.path.write_bytes(original)   # 逐位元組還原:保證與原檔完全相同
        _drop_bytecode(m.path)
    return status, "" if status == "red" else tail[:70]


@functools.lru_cache(maxsize=None)
def _test_names(path: Path) -> set[str]:
    """一個測試檔裡所有 `def test_*` 的名字(用 AST,不必付 --collect-only)。

    ⚠️ **一定要快取**:突變一多就會反覆綁到同幾個測試檔,不快取的話同一份
    檔案會被 `ast.parse` 幾十次(來源專案實測 129 次、13.2MB,而相異內容
    只有 524KB;`_validate()` 因此從 2.5 秒掉到 42 毫秒)。腳本是一次性
    行程,沒有快取失效的問題。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _validate() -> list[str]:
    """MUTANTS 這份資料自己的不變式。回問題清單(空 = 健康)。

    ⚠️ **每一種失效都是安靜的**:報告照印,只是失去意義。所以工具自己在
    `main()` 開頭擋下來,而不是只靠測試——新增突變的人跑的是這支腳本。
    四種在來源專案都真的發生過或差點發生:

    1. **代號撞號**:新增六條整批撞上既有代號,指定其中一條會默默跑兩條、
       報告出現兩行同號。
    2. **目標字串漂掉**:一查有五條的 old 早已不存在,每次都被算成逃掉
       ——叫人去補一條其實存在的測試。
    3. **綁到改過名的測試**:pytest 找不到 node id 會 exit 4,而那曾被
       判定成 RED(假紅)。⚠️ 測試**函式**改名遠比檔案改名頻繁,所以這裡
       比對到函式層級,不是只看檔案在不在。
    4. **no-op 突變**:`old == new` 永遠 GREEN,而報告分不出它與「沒人守」。
    """
    counts = collections.Counter(m.tag for m in MUTANTS)
    problems = [f"{tag}:代號重複({n} 條)"
                for tag, n in sorted(counts.items()) if n > 1]

    for m in MUTANTS:
        if not re.fullmatch(r"M\d+", m.tag):
            problems.append(f"{m.tag}:代號格式不符 M<數字>")
        if m.old == m.new:
            problems.append(f"{m.tag}:old 與 new 相同(no-op,永遠 GREEN)")
        if not m.why or not m.tests:
            problems.append(f"{m.tag}:why 或 tests 是空的")
        if not m.path.is_file():
            problems.append(f"{m.tag}:目標檔案不存在({m.path.name})")
        elif m.resolve(m.path.read_text(encoding="utf-8")) is None:
            problems.append(f"{m.tag}:目標字串已不存在於 {m.path.name}(程式碼改過了)")
        for node in m.tests:
            filename, _, func = node.partition("::")
            path = ROOT / filename
            if not path.is_file():
                problems.append(f"{m.tag}:綁到不存在的測試檔 {filename}")
            elif func and func.split("[")[0] not in _test_names(path):
                problems.append(f"{m.tag}:{filename} 裡沒有 {func}(改過名?)")
    return problems


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="mutate",
        description="把程式故意改壞,確認對應的測試真的會變紅。",
    )
    p.add_argument("tags", nargs="*", metavar="代號", help="只跑這幾個(預設全部)")
    p.add_argument("--list", action="store_true", help="列出突變清單,不動任何檔案")
    p.add_argument("--full", action="store_true",
                   help="改跑全套測試(確認逃掉的是不是被別處守著,慢很多)")
    p.add_argument("--who", action="store_true",
                   help="跑全套並印出**實際變紅的測試名**(逃掉時用來找真正的守門人)")
    args = p.parse_args(argv)

    # ⚠️ 清單自己的健康檢查放在**工具裡**、而且在 --list 之前:新增突變的人
    # 跑的是這支腳本,不是測試。撞號、綁到改過名的測試、目標字串漂掉——每一種
    # 的症狀都是「報告照印,只是失去意義」,所以要當場擋下來
    if problems := _validate():
        print("突變清單本身有問題,先修好再跑:", file=sys.stderr)
        for line in problems:
            print(f"  {line}", file=sys.stderr)
        return 2

    chosen = MUTANTS
    if args.tags:
        wanted = {t.upper() for t in args.tags}
        chosen = [m for m in MUTANTS if m.tag.upper() in wanted]
        missing = wanted - {m.tag.upper() for m in chosen}
        if missing:
            print(f"沒有這幾個代號:{', '.join(sorted(missing))}", file=sys.stderr)
            return 2

    if args.list:
        for m in chosen:
            print(f"{m.tag:<4} {m.path.name:<14} {m.why}")
        return 0

    if args.who:
        for m in chosen:
            status, who = _apply(m, [TESTS_DIR], names=True)
            print(f"{m.tag:<4} {status.upper():<6}  {m.why}\n      {who}", flush=True)
        return 0

    results = []
    for m in chosen:
        status, detail = _apply(m, [TESTS_DIR] if args.full else None)
        results.append((m, status, detail))
        print(f"{m.tag:<4} {status.upper():<6}  {m.why}", flush=True)

    escaped = [(m, d) for m, s, d in results if s == "green"]
    broken = [(m, d) for m, s, d in results if s == "broken"]
    print("\n" + "=" * 78)
    if escaped:
        print("以下突變**沒有被任何測試抓到**——那些測試是假綠燈:\n")
        for m, detail in escaped:
            print(f"  {m.tag}  {m.why}")
            print(f"      跑過的測試:{', '.join(t.split('::')[-1] for t in m.tests)}")
            if detail:
                print(f"      {detail}")
        print()
    if broken:
        # ⚠️ 這一區跟上面那區意思完全不同:上面是「風險沒人守」,這裡是
        # 「這條突變自己壞了」——把它們混在一起會叫人去補一條其實已經存在
        # 的測試,而真正該做的是修突變
        print("以下突變**自己跑不起來**(報告對它們沒有意義,請先修):\n")
        for m, detail in broken:
            print(f"  {m.tag}  {detail}")
        print()
    print(f"{len(results) - len(escaped) - len(broken)}/{len(results)} 個突變被抓到"
          + (f",{len(broken)} 條壞掉" if broken else ""))
    return 1 if escaped or broken else 0


if __name__ == "__main__":
    sys.exit(main())
