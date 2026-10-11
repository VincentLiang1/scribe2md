"""CLAUDE.md 對 `docs/` 的指路不得腐爛。

【這是範本】複製到專案的 `tests/` 之後,確認下面三個常數對得上你的目錄結構
就能用(`DEV_DOCS` 指到那些「靠 CLAUDE.md 指路才會被載入」的深度文件)。

**為什麼需要這個**:CLAUDE.md 每開一次新對話就整份載入,所以它必須短
——細節要搬進 `docs/dev/`,主檔只留摘要 +「動到 X 之前先讀 Y」的指路。

**這個作法的唯一風險是指路斷掉**:檔案被改名或刪掉時,CLAUDE.md 那一行
會變成指向不存在的檔案,而症狀是**知識安靜地消失**——以後的人(或以後的
AI)不會知道那裡本來有東西,只會重蹈一次已經記載過的覆轍。所以用測試釘住。

⚠️ 反向那條同樣重要:**沒有入口的文件等於不存在**。它不會被載入,
也不會有人想到要去讀——寫了等於沒寫,而且沒有任何跡象。
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLAUDE = ROOT / "CLAUDE.md"
DEV_DOCS = ROOT / "docs" / "dev"


def _pointers() -> set[str]:
    """CLAUDE.md 裡所有以反引號括住的 `docs/....md` 路徑。"""
    return set(re.findall(r"`(docs/[\w/.-]+\.md)`", CLAUDE.read_text(encoding="utf-8")))


def test_every_pointer_in_claude_md_resolves():
    """CLAUDE.md 提到的每一份 docs 都要真的存在。"""
    missing = [p for p in _pointers() if not (ROOT / p).is_file()]
    assert not missing, f"CLAUDE.md 指向不存在的檔案:{missing}"


def test_every_dev_doc_is_reachable_from_claude_md():
    """反向:`docs/dev/` 裡的每一份都要有人指得到它。

    沒有入口的文件等於不存在——它不會被載入、也不會有人想到要去讀。"""
    pointed = _pointers()
    orphans = [
        f"docs/dev/{p.name}" for p in DEV_DOCS.glob("*.md")
        if f"docs/dev/{p.name}" not in pointed
    ]
    assert not orphans, f"沒有從 CLAUDE.md 指到的文件:{orphans}"


# ---------------------------------------------------------------------------
# 以下是**可選**的一致性檢查,依專案挑用(不適用的整段刪掉)。
# 判準:能精確比對的才進測試;需要人判斷的做成盤點腳本,不要自動判定
# ——說明常常用別的說法講同一件事,**給假判定比不給更糟**。
# ---------------------------------------------------------------------------

@pytest.mark.skip(reason="範本:改成你自己的說明檔與 CLI 之後再啟用")
def test_docs_mention_every_cli_option():
    """命令列的每一個參數都要在說明文件裡出現。

    這條能精確比對(參數名是機器可讀的字串),所以適合進測試。
    使用者照著說明找不到某個參數 = 那個功能等於不存在。"""
    from your_package import cli  # noqa

    documented = (ROOT / "docs" / "使用說明.md").read_text(encoding="utf-8")
    options = [a.option_strings for a in cli.build_parser()._actions]
    missing = [o[0] for o in options if o and o[0] not in documented]
    assert not missing, f"說明文件沒有提到這些參數:{missing}"
