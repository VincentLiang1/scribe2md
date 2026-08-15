r"""命名進度落地(使用者選定 2026-07-18)。

命名所需狀態(輸出路徑/講者線索/聲紋向量/試聽片段/已填名字)原本只活在
gradio session 裡:電腦睡眠、斷線、關瀏覽器都會讓 session 被判死,轉了
30 分鐘的檔案只能整個重轉才有辦法命名(使用者實際踩到)。改為轉檔完成
當下就把這一切寫進本機磁碟,開頁(demo.load)自動還原——與 session
徹底脫鉤;套用成功(=工作完成)才清掉。存放於 %LOCALAPPDATA%
(paths.appdata_root,同 AI 模型的存放區),不進 repo。

本模組是純儲存層:鍵一律是講者標籤(int,UNKNOWN_SPEAKER=-1 也是一鍵),
UI 端的欄位順序/更新組裝在 app。pending 目錄必須列進 launch(allowed_paths=)
——試聽片段的落地副本要能被 gradio 供應(app.main 負責)。
"""
import json
import logging
import shutil
from pathlib import Path

import numpy as np

from meeting_scribe import paths
from meeting_scribe.types import UNKNOWN_SPEAKER

logger = logging.getLogger(__name__)

_VERSION = 1


def anyone_to_name(count, hints) -> bool:
    """這份成品**有沒有人可以命名**:有講者,或有「未知」那一段。

    ⚠️ 兩者皆無時,整份落地是**沒有意義而且有害**的(使用者 2026-08-15
    實機踩到):UI 端命名區一個框都不會渲染,而「有成品在等命名」的判準
    (`app._naming_focus` 看 `paths_state`)卻成立——左欄那整組「開始下一份
    工作」被收走,畫面上只剩「進階參數設定」。落地之後每次開頁還原都會
    把那個狀態原封不動搬回來,重開程式也救不了。

    「一位講者都沒有」是真的會發生的:「只錄電腦聲音」錄到一段沒有人聲
    的音,逐字稿就只有一行標題。"""
    return bool(count) or hints.get(UNKNOWN_SPEAKER) is not None


def pending_dir() -> Path:
    return paths.appdata_root() / "pending"


def persist(outputs, preview, count, voiceprints, hints, clips, names,
            audit=None) -> dict:
    """把命名所需的一切寫進 pending 目錄;回傳(改指落地副本的)clips。

    落地是輔助功能:任何失敗只記 log,絕不讓剛跑完 30 分鐘的轉檔在最後
    一步炸掉;失敗時回傳原 clips(本 session 內仍可正常試聽)。"""
    if not outputs:
        # 沒有成品就沒有可接續的命名(整批停止/失敗)——保留舊落地,
        # 不讓一次失敗的嘗試毀掉上一份還能接續的命名
        return dict(clips or {})
    if not anyone_to_name(count, hints or {}):
        # 有成品、但一位講者都沒有:命名這件事根本不會發生,落地只會在
        # 下次開頁把畫面卡在一個空的命名狀態(見 anyone_to_name)
        return dict(clips or {})
    try:
        d = pending_dir()
        shutil.rmtree(d, ignore_errors=True)
        clips_dir = d / "clips"
        clips_dir.mkdir(parents=True, exist_ok=True)
        stored_clips: dict[int, str] = {}
        for spk, path in (clips or {}).items():
            try:
                dest = clips_dir / Path(path).name
                shutil.copyfile(path, dest)
                stored_clips[spk] = str(dest)
            except OSError:
                # 副本失敗:留原(暫存)路徑,本 session 仍可試聽;
                # 重開後該講者的試聽鈕不亮(load 會過濾不存在的檔)
                stored_clips[spk] = str(path)
        if voiceprints:
            np.savez(
                d / "voiceprints.npz",
                **{str(k): v for k, v in voiceprints.items()},
            )
        # meta 最後寫:它是「這份落地完整可用」的提交點(load 以其
        # 存在與否判斷),中途炸掉不會留下半套資料被誤還原
        meta = {
            "version": _VERSION,
            "outputs": [str(p) for p in outputs],
            "preview": preview,
            "count": count,
            "hints": {str(k): list(v) for k, v in (hints or {}).items()},
            "names": {str(k): v for k, v in (names or {}).items()},
            "clips": {str(k): v for k, v in stored_clips.items()},
            # 核對資料(每一輪發言 + 音檔來源 + 哪幾列該亮鈕)。⚠️ **一定要
            # 一起落地**:第一版沒存,重新整理之後核對鈕照樣亮著(它只看
            # 「有沒有未知」),按下去卻是「沒有可核對的段落」——使用者
            # 2026-08-13 實機踩到。純 JSON,不必另外存檔案
            "audit": audit or {},
        }
        (d / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8",
        )
        return stored_clips
    except Exception:
        logger.exception("命名進度落地失敗(不影響本次結果,但重新整理後無法接續命名)")
        return dict(clips or {})


def _usable_audit(audit) -> dict:
    """落地的核對資料還能不能用:要有區塊,而且音檔來源還在。"""
    if not isinstance(audit, dict) or not audit.get("blocks"):
        return {}
    src = audit.get("src") or ""
    if not src or not Path(src).exists():
        return {}
    return audit


def load() -> dict | None:
    """讀回未完成的命名;沒有、壞損、版本不符或輸出檔已被移走 → 清掉並回 None。"""
    meta_file = pending_dir() / "meta.json"
    if not meta_file.exists():
        return None
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        if meta.get("version") != _VERSION:
            raise ValueError("版本不符")
        outputs = [p for p in meta.get("outputs", []) if Path(p).exists()]
        if not outputs:
            raise ValueError("輸出檔已不存在")
        hints = {int(k): tuple(v) for k, v in meta.get("hints", {}).items()}
        # ⚠️ **這一道守的是舊版留在使用者機器上的那些**:寫入端(app 的
        # `_present_result`)已經不再落地「沒有人可命名」的成品,但先前寫下的
        # 那一份還在磁碟上,不清掉的話程式更新後開頁照樣被還原、左欄照樣鎖著
        if not anyone_to_name(int(meta.get("count", 0)), hints):
            raise ValueError("這份落地沒有任何可命名的講者")
        voiceprints: dict = {}
        npz = pending_dir() / "voiceprints.npz"
        if npz.exists():
            with np.load(npz) as data:
                voiceprints = {int(k): data[k] for k in data.files}
        return {
            "outputs": outputs,
            "preview": meta.get("preview", ""),
            "count": int(meta.get("count", 0)),
            "hints": hints,
            "names": {int(k): v for k, v in meta.get("names", {}).items()},
            "voiceprints": voiceprints,
            # 試聽片段逐檔過濾:少檔只是該講者試聽鈕不亮,不整包報廢
            "clips": {
                int(k): p for k, p in meta.get("clips", {}).items() if Path(p).exists()
            },
            # 音檔來源不在了(使用者搬走原檔)就整包不給:核對是「聽」的
            # 功能,沒有音檔時鈕不該亮
            "audit": _usable_audit(meta.get("audit")),
        }
    except Exception:
        logger.warning(
            "未完成命名的落地資料無法使用(過期、輸出檔被移走,"
            "或裡面根本沒有可命名的講者),已清除",
        )
        clear()
        return None


def clear() -> None:
    """套用完成(=這份檔案的工作結束)或資料過期時清掉落地。"""
    shutil.rmtree(pending_dir(), ignore_errors=True)


def update_names(names: dict[int, str]) -> None:
    """整份覆寫落地的草稿名字({講者標籤: 名字});沒有落地資料
    (套用後/從未轉檔)就略過。失敗只記 log:草稿是便利功能,
    不得干擾使用者輸入。"""
    meta_file = pending_dir() / "meta.json"
    if not meta_file.exists():
        return
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        meta["names"] = {str(k): v for k, v in names.items()}
        meta_file.write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8",
        )
    except Exception:
        logger.exception("命名草稿儲存失敗(不影響操作,僅重新整理後草稿不齊)")
