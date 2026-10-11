r"""`--out-dir` 快取的過期清理(`doc2md --prune-days`,使用者 2026-08-14 指定)。

**為什麼需要**:`--out-dir` 模式的檔名帶一段來源內容雜湊(見 `docmd.md_path_for`)
——來源改過就換一個名字重轉,舊的那份不會被覆寫、會留在快取裡變孤兒。而知識庫
攝入是「一批做完就不再回頭」的用法,所以快取只增不減:實測某個知識庫的
`.md_cache\` 十天累積 60 MB,其中 45 MB 是一本電子書抽出來的圖。工具本身
以前不清(`md_path_for` 的註解只寫著「整個快取目錄刪掉重來即可」),等於
把清理丟給人記得做,而那件事沒有人會記得。

四條規則,每一條都擋著一種「刪到不該刪的東西」:

- **只清 `--out-dir` 指定的資料夾**。預設模式的產出擺在使用者自己的文件旁邊,
  那是他的檔案、不是快取,任何自動刪除都不能碰(同 `docmd.AssetsDir` 那條
  「寫進使用者資料夾要有憑據」的精神)。
- **憑據制**:md 要在 frontmatter 帶 `converter: meeting-scribe/doc2md`,
  `.assets\` 目錄要有 `.meeting-scribe-assets` 標記檔。使用者自己丟進同一個
  資料夾的筆記、或別的工具的產出,一律不動——快取路徑是使用者給的,他指到
  桌面或指到一個已經有東西的資料夾都是合法用法。
- **這一批還要用的不刪**(`keep`):即使過期也留著。清掉正要回傳的那一份
  等於「先刪再重轉」,既沒省到空間又白花時間——文件只是幾秒鐘,但**錄音
  那條直接賠掉一小時**(使用者 2026-08-14 選定逐字稿與文件一視同仁,那條
  取捨才更要靠這裡兜住)。
- **只掃第一層**:`--out-dir` 是攤平的(見 `docmd.md_path_for`),往下遞迴
  只會走進 `.assets\` 裡面,而那裡的圖片是靠外面那層目錄整個刪掉的。

保留期預設 14 天(使用者 2026-08-14 選定):快取的用途是攝入完之後回頭核對
「Wiki 這段怪怪的,是原文如此還是轉壞的」,兩週內查得到就夠;而文件重轉近乎
免費,誤清的代價很低。
"""
import logging
import shutil
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from meeting_scribe import docmd

logger = logging.getLogger(__name__)

# 預設保留天數。0 = 完全不清(`--prune-days 0`)
DEFAULT_PRUNE_DAYS = 14

# 讀多少字元來找 frontmatter 的憑據。frontmatter 只有十來行,但 source_path
# 可能是一整條長路徑;取 4 KB 遠遠夠用,而且不必為了認一個欄位讀進一份
# 一百萬字元的電子書 md
_HEAD_CHARS = 4096


@dataclass
class PruneReport:
    """清了什麼。`freed` 是位元組,`failed` 是刪不掉的個數(檔案被鎖住等)。"""

    md: list[Path] = field(default_factory=list)
    assets: list[Path] = field(default_factory=list)
    freed: int = 0
    failed: int = 0

    @property
    def count(self) -> int:
        return len(self.md) + len(self.assets)


def _is_ours(md: Path) -> bool:
    """這份 md 是本工具產生的嗎?(看 frontmatter 的 `converter` 憑據)

    **只認 frontmatter、不認整份內容**:轉出來的 md 本身就可能在內文提到
    這個字串(這個 repo 自己的說明文件轉一次就是),拿整份去搜會把使用者
    自己的筆記也算成我們的。"""
    try:
        head = md.read_text(encoding="utf-8", errors="replace")[:_HEAD_CHARS]
    except OSError:
        logger.debug("讀不到 md,不清:%s", md, exc_info=True)
        return False
    if not head.startswith("---"):
        return False
    end = head.find("\n---", 3)
    front = head if end < 0 else head[:end]
    return docmd.GENERATED_MARKER in front


def _too_old(path: Path, cutoff: float) -> bool:
    """讀不到時間就當成「不夠舊」——判斷不了的東西一律留著。"""
    try:
        return path.stat().st_mtime < cutoff
    except OSError:
        logger.debug("取不到時間,不清:%s", path, exc_info=True)
        return False


def _size_of(path: Path) -> int:
    """要刪的東西有多大(給「釋出 X MB」用)。算不到就當 0,不影響刪除。"""
    total = 0
    try:
        if path.is_file():
            return path.stat().st_size
        for p in path.rglob("*"):
            if p.is_file():
                total += p.stat().st_size
    except OSError:
        logger.debug("算不出大小:%s", path, exc_info=True)
    return total


def _remove(path: Path, root: Path, *, tree: bool) -> bool:
    r"""刪掉一個項目,成功回 True。

    **刪之前再斷言一次它在 root 底下**:路徑來自 `iterdir` 所以天然成立,
    但這是全專案第二個會 `rmtree` 的地方,斷言只值一行——少了它,日後有人
    把來源換成別的清單(例如從 md 的連結反推)就完全沒有攔阻了。
    目錄另外**再確認一次標記檔**,理由同 `docmd.AssetsDir`。"""
    try:
        if not path.resolve().is_relative_to(root):
            logger.warning("要刪的路徑不在快取資料夾底下,略過:%s", path)
            return False
        if tree:
            if not (path / docmd.ASSETS_MARKER).exists():
                return False
            shutil.rmtree(path)
        else:
            path.unlink()
        return True
    except OSError:
        logger.debug("刪不掉:%s", path, exc_info=True)
        return False


def prune(
    out_dir: Path | None,
    days: int = DEFAULT_PRUNE_DAYS,
    keep: Iterable[Path] = (),
    *,
    dry_run: bool = False,
) -> PruneReport:
    r"""清掉 `out_dir` 裡過期的舊產出。`keep` 是這一批還要用的 md 路徑。

    `out_dir` 是 None(預設模式,產出在使用者文件旁邊)或 `days <= 0` 時
    **什麼都不做**,這是本模組最重要的一條。`dry_run` 只列不刪。"""
    report = PruneReport()
    if out_dir is None or days <= 0:
        return report
    root = Path(out_dir)
    if not root.is_dir():
        return report
    root = root.resolve()

    cutoff = time.time() - days * 86400
    keep_names = {Path(p).name.casefold() for p in keep}
    try:
        entries = list(root.iterdir())
    except OSError:
        logger.debug("列不出快取資料夾:%s", root, exc_info=True)
        return report

    doomed_md: list[Path] = []
    surviving_md: list[Path] = []
    for p in entries:
        if p.suffix.lower() != ".md" or not p.is_file():
            continue
        if (
            p.name.casefold() in keep_names
            or not _too_old(p, cutoff)
            or not _is_ours(p)
        ):
            surviving_md.append(p)
        else:
            doomed_md.append(p)

    # ⚠️ 要保護的 assets 目錄一定要從**留下來的 md** 反推,不能拿被刪的那份
    # 去配對:目錄名是 md 主檔名消毒後裁到 60 字元(`docmd.assets_name_for`),
    # 而裁切正好會把 `--out-dir` 的內容雜湊截掉——長檔名的兩份 md 因此共用
    # 同一個目錄。照被刪的那份去刪,還在用的那份圖片會一起消失,而 md 裡的
    # 連結還在(斷圖比沒有圖更難查)
    protected = {docmd.assets_name_for(p.stem).casefold() for p in surviving_md}

    doomed_dirs = [
        p for p in entries
        if p.is_dir()
        and p.name.lower().endswith(docmd.ASSETS_SUFFIX)
        and p.name.casefold() not in protected
        and (p / docmd.ASSETS_MARKER).exists()
        and _too_old(p, cutoff)
    ]

    for p in doomed_md:
        size = _size_of(p)
        if dry_run or _remove(p, root, tree=False):
            report.md.append(p)
            report.freed += size
        else:
            report.failed += 1
    for p in doomed_dirs:
        size = _size_of(p)
        if dry_run or _remove(p, root, tree=True):
            report.assets.append(p)
            report.freed += size
        else:
            report.failed += 1
    return report


def _mb(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MB"


def summary_lines(
    report: PruneReport, days: int, *, dry_run: bool = False,
) -> list[str]:
    """給人看的收尾(走 stderr)。沒清到東西時**保持安靜**。

    ⚠️ 靜默刪除是不可接受的:使用者過幾天回頭發現快取少了東西,而畫面上
    什麼都沒說過,那跟「檔案自己不見了」沒有差別。乾跑時反過來一定要出聲,
    包括「沒有東西要清」——那正是他跑乾跑想確認的事。"""
    if not report.count and not dry_run:
        return [] if not report.failed else [
            f"有 {report.failed} 個過期的快取刪不掉(檔案可能正被其他程式開著)。"
        ]
    if dry_run:
        if not report.count:
            return [f"快取裡沒有超過 {days} 天的舊產出,這次不會清任何東西。"]
        lines = [
            f"會清掉 {report.count} 個超過 {days} 天的舊快取"
            f"(約 {_mb(report.freed)}):"
        ]
        lines += [f"  {p.name}" for p in report.md + report.assets]
        return lines
    line = (
        f"已清掉 {report.count} 個超過 {days} 天的舊快取,"
        f"釋出約 {_mb(report.freed)}。"
    )
    if report.failed:
        line += f"另有 {report.failed} 個刪不掉(檔案可能正被其他程式開著)。"
    return [line]
