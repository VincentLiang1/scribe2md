r"""命令列入口(`doc2md`):把文件批次轉成 md,不開網頁介面。

**存在的理由是給 AI 讀**。Claude Code 這類工具讀 PDF 是把每一頁當成圖片
送進模型(一頁上千 token,而簡報式的頁面往往只有幾十個字);.docx / .pptx
/ .xlsx 更是二進位,根本讀不了。先轉成 md 再讓它讀,省下來的是量級差距。
搭配的使用說明在使用者層級的 `doc2md` Skill 裡。

輸出契約(這支程式最重要的設計,因為主要呼叫端是程式不是人):

- **stdout 只有 md 的絕對路徑**,一行一個,不夾雜任何其他文字。
- **已經存在、因此被跳過的 md 也會列出來**。呼叫端要的是「該讀哪些檔」,
  不是「這次做了哪些工」——把跳過的省略掉,第二次執行就會回一片空白,
  呼叫端只好重轉或自己去猜路徑。
- 人看的進度與報告一律走 **stderr**;而且**進度只在真的終端機才印**
  (`stderr.isatty()`),被程式接走時保持安靜——這支程式的存在意義是
  省 token,自己卻對著 AI 洗 50 行進度就本末倒置了。

離開碼:0 全部順利、1 有檔案轉失敗(其餘仍已完成)、2 參數或選檔有問題
(什麼都沒做)、130 中途被 Ctrl+C 中止。
"""
import argparse
import logging
import sys
from pathlib import Path

from meeting_scribe import (
    cancel,
    docmd,
    docpipe,
    docprune,
    docsrc,
    models,
    pipeline,
    srcfile,
    stdio,
    transcribe,
)
from meeting_scribe.errors import UserFacingError

logger = logging.getLogger(__name__)


def _inside(path: Path, folder: Path) -> bool:
    """path 在 folder 底下嗎?(解析過的絕對路徑比較,跨磁碟不會炸)"""
    try:
        return path.resolve().is_relative_to(folder)
    except (OSError, ValueError):  # pragma: no cover - 斷掉的網路磁碟
        return False


def _md_paths(report: docpipe.BatchReport, out_dir: Path | None = None) -> list[Path]:
    """這一批「呼叫端該去讀」的 md,保序去重。

    = 這次產生的 + 早就存在所以跳過的。跳過的判準限定在**支援的格式**
    上:使用者可能同時丟了 `報告.zip`(不支援)進來,而旁邊剛好有一份
    別人產生的 `報告.md`,那份不是我們的產出,不該混進來。

    目標路徑一律問 `docmd.md_path_for`,**不要自己用 `with_suffix` 算**
    ——那份複製品漏掉 out_dir,`--out-dir` 模式下重跑就回一片空白(呼叫端
    以為沒轉過而重轉,「重跑近乎免費」的承諾直接跳票)。"""
    out: list[Path] = []
    seen: set[str] = set()

    def _add(p: Path) -> None:
        resolved = p.resolve()
        key = str(resolved).lower()
        if key not in seen:
            seen.add(key)
            out.append(resolved)

    for r in report.ok:
        _add(r.out)
    for src, _reason in report.skipped:
        if src.suffix.lower() not in docsrc.SUPPORTED_TYPES:
            continue
        existing = docmd.md_path_for(src, out_dir)
        if existing.exists():
            _add(existing)
    return out


def _summary(report: docpipe.BatchReport) -> str:
    """給人看的收尾(stderr)。刻意比 GUI 的報告短——這裡不必列出每個
    成功的檔名(stdout 已經有完整路徑了),但**失敗一定要點名**。"""
    parts = []
    if report.cancelled:
        parts.append(f"已中止。完成 {len(report.ok)} 個,其餘未處理。")
    elif not report.ok and not report.failed:
        # 全部跳過時不能說「成功 0 個」——那看起來像出了什麼事,
        # 實情是「本來就沒有需要做的」(通常是 md 都已經存在)
        parts.append(f"沒有需要轉換的檔案({len(report.skipped)} 個都略過了)。")
    else:
        line = f"轉換完成:成功 {len(report.ok)} 個"
        if report.failed:
            line += f",失敗 {len(report.failed)} 個"
        if report.skipped:
            line += f",略過 {len(report.skipped)} 個"
        parts.append(line + "。")
    for r in report.failed:
        parts.append(f"  失敗:「{r.src.name}」:{r.error}")
    for src, reason in report.skipped:
        parts.append(f"  略過:「{src.name}」:{reason}")
    return "\n".join(parts)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="doc2md",
        description=(
            "把 Office / PDF / 網頁 / 影像 / 郵件 / 錄音錄影轉成 Markdown。"
            "音訊與影片會轉成逐字稿(標「講者 1/2/3」,但不做講者命名"
            "——命名要一檔一檔當場做,命令列沒有「當場」可言)。"
            f"支援的格式:{docsrc.cli_supported_hint()}"
        ),
    )
    p.add_argument("paths", nargs="+", metavar="路徑", help="檔案或資料夾,可以給多個")
    p.add_argument(
        "--out-dir", metavar="資料夾",
        help="把整批產出集中到這個資料夾(預設放在原始文件旁邊)",
    )
    p.add_argument(
        "--prune-days", type=int, default=docprune.DEFAULT_PRUNE_DAYS, metavar="天數",
        help=(
            "--out-dir 的快取裡超過幾天的舊產出自動清掉"
            f"(預設 {docprune.DEFAULT_PRUNE_DAYS} 天;0 = 都不清)。"
            "只清本工具自己產生的檔案,而且只在有 --out-dir 時才做"
        ),
    )
    p.add_argument(
        "--no-ocr", action="store_true",
        help="不做文字辨識。掃描頁與圖片會只留標記,但快得多",
    )
    p.add_argument(
        "--no-recursive", action="store_true",
        help="給資料夾時不要往子資料夾找",
    )
    p.add_argument(
        "--no-attachments", action="store_true",
        help="郵件(.msg/.eml)不遞迴轉換附件",
    )
    p.add_argument(
        "--model", choices=("fast", "accurate"),
        help="錄音錄影用哪個轉錄模型(預設依本機有沒有 GPU 自動挑)",
    )
    p.add_argument(
        "--speakers", type=int, default=0, metavar="人數",
        help="錄音錄影的講者人數;0(預設)= 自動偵測。填了會套用到整批",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="只列出「會轉哪些、會跳過哪些」,不實際寫檔",
    )
    p.add_argument(
        "--verbose", action="store_true",
        help="即使輸出被接走也印出進度(預設只在終端機印)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    stdio.force_utf8(sys.stdout, sys.stderr)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    args = _build_parser().parse_args(argv)

    # 出廠預設補檔:網頁介面那條在 app.main,命令列這條同樣要有——
    # 只裝了工具、還沒開過網頁就直接跑 doc2md 的人,少了 replace.txt
    # 就是整批文件都沒做大陸詞替換,而且沒有任何跡象看得出來
    models.seed_missing()

    # 上一批(或當機的上一次執行)留下的旗標會讓這次一開工就自我了斷
    cancel.reset()
    pipeline.cleanup_stale_temp()

    out_dir = Path(args.out_dir).expanduser() if args.out_dir else None
    if out_dir is not None:
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"無法建立輸出資料夾「{out_dir}」:{e}", file=sys.stderr)
            return 2

    try:
        files, skipped = docsrc.validate_batch(
            "\n".join(args.paths), recursive=not args.no_recursive,
            # 命令列吃得比介面多:`.md`(**只在明確指名時**——給的是資料夾
            # 時 expand_folder 一律不收,那條規則在 docsrc 裡面、不是這裡的
            # 傳入值決定的,見 CLI_ONLY_TYPES)。音訊/影片兩邊都吃
            types=docsrc.SUPPORTED_TYPES,
        )
    except UserFacingError as e:
        print(str(e), file=sys.stderr)
        return 2

    # **輸出資料夾自己不能當來源**:`.md` 自 2026-08-02 起是支援的格式,
    # 而 `--out-dir` 的快取常常就放在被掃描的樹底下(知識庫攝入正是這樣)
    # ——不排除的話,每次重跑都會把上一輪的產出再吃一次,批次無限長大
    if out_dir is not None:
        resolved = out_dir.resolve()
        files = [f for f in files if not _inside(f, resolved)]

    # 快取清理。**一定要排在「這批要轉什麼」算出來之後**:`keep` 靠它才拿得到
    # 「這次還會用到的 md」,而清掉正要回傳的那一份等於先刪再重轉——既沒省到
    # 空間又白花時間。乾跑時也要跑(dry_run 只列不刪),因為「先看看會刪哪些
    # 再決定」正是使用者要的把關方式
    prune_report = docprune.prune(
        out_dir, args.prune_days,
        keep=[docmd.md_path_for(f, out_dir) for f in files] if out_dir else (),
        dry_run=args.dry_run,
    )
    for line in docprune.summary_lines(
        prune_report, args.prune_days, dry_run=args.dry_run,
    ):
        print(line, file=sys.stderr)

    if args.dry_run:
        for line in docpipe.dry_run_lines(docpipe.plan_outputs(files, out_dir)):
            print(line, file=sys.stderr)
        for line in docsrc.skipped_lines(skipped):
            print(line, file=sys.stderr)
        return 0

    show_progress = args.verbose or sys.stderr.isatty()

    def _on_stage(stage: str, frac: float) -> None:
        if show_progress:
            print(f"[{frac * 100:3.0f}%] {stage}", file=sys.stderr)

    # 錄音錄影與文件不是同一個量級:一份 PDF 幾秒鐘,一場兩小時的會議要跑
    # 一小時上下。把整個資料夾指過來的人多半是為了裡面的文件,**不該在毫無
    # 預告的情況下被拖進幾小時的轉錄**——先講一聲,要停還來得及(這是提醒,
    # 不是攔阻:使用者 2026-08-06 明確要求命令列也吃音訊)
    audio = [f for f in files if f.suffix.lower() in srcfile.SUPPORTED_TYPES]
    if audio:
        print(
            f"注意:這批有 {len(audio)} 個錄音/影片要轉逐字稿,"
            "每個檔可能要數十分鐘(視長度與本機有無 GPU)。",
            file=sys.stderr,
        )

    try:
        report = docpipe.convert_batch(
            files, skipped,
            on_stage=_on_stage,
            ocr_enabled=not args.no_ocr,
            mail_attachments=not args.no_attachments,
            out_dir=out_dir,
            # 只有音訊路由收得下(見 Route.wants_options);模型預設與介面
            # 同一份判準,不然同一台機器會因為入口不同而拿到不同品質
            options={
                "model_key": args.model or transcribe.default_model_key(),
                "num_speakers": max(args.speakers, 0),
            },
        )
    except UserFacingError as e:
        # 缺元件之類的「整批都做不了」:convert_batch 刻意不接,由這裡收
        print(str(e), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已中止。", file=sys.stderr)
        return 130
    except Exception:
        # 這支程式的使用者是開發者與 AI,不是非技術同仁——traceback 對他們
        # 有用(GUI 那邊才必須藏起來,見 spec §8)。繁中一行仍要有
        logger.exception("轉檔時發生未預期的錯誤")
        print("轉檔時發生未預期的錯誤,詳情見上方 traceback。", file=sys.stderr)
        return 2

    # stdout 只有路徑,先印完再印摘要:被 shell 接走時兩者本來就分流,
    # 但人在終端機看時,先看到清單再看到結論比較順
    for path in _md_paths(report, out_dir):
        print(str(path))
    summary = _summary(report)
    if summary:
        print(summary, file=sys.stderr)

    if report.cancelled:
        return 130
    return 1 if report.failed else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
