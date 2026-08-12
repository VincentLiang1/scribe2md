r"""OCR 子行程本體(`python -m meeting_scribe.ocr_worker`)。

**這個模組只在子行程裡執行**,父行程(`ocr.py`)絕不 import 它——一 import
就把 rapidocr / opencv / onnxruntime 整套拉進主行程,隔離就白做了。隔離的
完整理由見 `ocr.py` 的模組 docstring。

協定(父子之間刻意做到最笨):
- **stdin**:一行一個 JSON 請求 `{"image": "<檔案路徑>"}`
- **stdout**:一行一個 JSON 回應,**只准有 NDJSON**;啟動完成先送一行
  `{"ready": true}`
- **stderr**:所有 log。父行程另起執行緒把它 drain 掉(不 drain 會在
  緩衝滿時死結,是這種互動式子行程的頭號死法)

圖片走**檔案路徑**而不是把位元組塞進 pipe:Windows 上以 pipe 傳大量
二進位是死結溫床(雙向阻塞),而 PNG 編碼 30ms 對比 OCR 的 1~3 秒完全
不是瓶頸。
"""
import ctypes
import ctypes.wintypes as wintypes
import gc
import json
import logging
import sys
from pathlib import Path

from meeting_scribe import power

logger = logging.getLogger(__name__)

# 一次最多接受的請求長度(位元組):防呆用,正常請求只有幾百 bytes
_MAX_LINE = 1 << 20

# 引擎回收門檻的上下限與對應的機器規格(見 `_memory_limit_mb`)
_MEM_LIMIT_FLOOR_MB, _MEM_LIMIT_CEILING_MB = 2000, 4000
_RAM_FLOOR_GB, _RAM_CEILING_GB = 8, 32


def _memory_limit_mb() -> int:
    """工作集超過多少 MB 就把引擎丟掉重建(見 `_over_memory_limit`)。

    **跟著機器的實體記憶體走**(使用者 2026-08-03 指定 8GB→2000、32GB→4000):
    這個數字是「願意分多少記憶體給 OCR 子行程」,而那顯然該看機器多大——
    基準機是 8GB(主行程還有 gradio 與可能沒釋放的轉錄引擎快取),開發機
    是 32GB。兩個錨點之間線性內插,兩端夾住。

    **上限訂在 4000 是因為再高也沒有用**(120 張真實影像實測):門檻 2000 →
    787ms/張、3000 → 732、4000 → 714、6000 → 702(而且峰值只到 3,984,
    根本沒碰到上限)。2000→4000 買到 9%,4000 以上買不到東西。**下限訂在
    2000 是因為再低就一直重建**:重建之後形狀快取是冷的,先前編過的核心
    要重編一次,省下的不只是那一秒建構。

    量不到就回下限:保守值在小機器上是對的,在大機器上只是慢一點。"""
    total = power.total_ram_mb()
    if total is None:
        return _MEM_LIMIT_FLOOR_MB
    span_mb = _MEM_LIMIT_CEILING_MB - _MEM_LIMIT_FLOOR_MB
    span_gb = _RAM_CEILING_GB - _RAM_FLOOR_GB
    limit = _MEM_LIMIT_FLOOR_MB + (total / 1024 - _RAM_FLOOR_GB) * span_mb / span_gb
    return int(min(max(limit, _MEM_LIMIT_FLOOR_MB), _MEM_LIMIT_CEILING_MB))


# 在模組層算一次:每張圖都重算等於每張圖多一次 Win32 呼叫,而機器的
# 記憶體不會在一次批次中途變動。測試與量測腳本改寫這個值即可
_MEM_LIMIT_MB = _memory_limit_mb()


def _preload_pip_onnxruntime() -> None:
    """Windows System32 內建舊版 onnxruntime.dll 會被依名稱優先解析,
    綁到舊版可能直接 segfault;先以完整路徑載入 pip 版 DLL。

    **刻意複製一份而不是 import diarize._preload_pip_onnxruntime**:那會
    把 sherpa-onnx 那條線的模組拉進這個子行程,而子行程存在的意義正是
    「只載入 OCR 需要的東西」。十行的重複換乾淨的相依圖,划算。"""
    if sys.platform != "win32":
        return
    try:
        import onnxruntime

        dll = Path(onnxruntime.__file__).parent / "capi" / "onnxruntime.dll"
        if dll.exists():
            ctypes.WinDLL(str(dll))
    except Exception:  # pragma: no cover - 預載失敗就讓後續 import 自然報錯
        logger.debug("onnxruntime DLL 預載失敗", exc_info=True)


def build_engine(threads: int = 0):
    """建立 RapidOCR 引擎(PP-OCRv6)。**模型打包在 wheel 內,這一步不連網**
    (2026-08-03 實測:攔死 socket.connect/getaddrinfo 後照樣建構成功)。

    **模型版本是準確度需求,不是「順手升級」**(2026-08-03,使用者拿一張
    Outlook 出席者截圖回報「OCR 出人名比 Claude 直接讀差很多」):舊的
    PP-OCRv4 **mobile** rec 對小字繁中會**整個字吃掉**——同一張圖(538×572、
    字高 14px)v4 讀成「陳豪」「林孝」「羅雪」(啟/謙/嬌 三個字消失)、
    「夏中道」(賈→夏)、「羅嘉僮」(偉→僮),v6 十個人名**全對**,而且
    更快(2.5 秒 vs 3.2 秒)。

    排除過的假設,別再重試:①**不是解析度不足**——整張圖放大 2/3/4 倍
    (LANCZOS 與 BICUBIC 都試過)命中數不升反降,因為 rec 的輸入寬度本來
    就是依長寬比動態算的、沒有壓縮;②**不是 det 框裁掉字**——逐行加 padding
    重裁重辨識會救回「啟」卻同時弄丟「羅」、把「嬌」讀成「嫣」,換個前處理
    就換一組錯,是模型能力不足的典型徵狀。真正的差別在訓練資料:v6 涵蓋
    繁體中文,v4 mobile 不足。

    `params` 的鍵是 OmegaConf 的點路徑(3.x 的設定介面,與 1.x 的關鍵字
    參數完全不同)。log 壓到 error:預設 info 會把三顆模型的載入路徑往
    stderr 洗,而父行程把 stderr 全部收進 DEBUG 紀錄檔。

    **推論後端是 OpenVINO CPU,不是 onnxruntime**(2026-08-03 實測):同一批
    1,179 張真實影像端到端,中位 1.78 秒 → 0.67 秒(**2.66 倍**),而**文字
    只有 2 張有差異**(0.17%,且兩張都是本來就已經糊掉的心智圖截圖),平均
    信心 0.9563 → 0.9563、總字數差 +1;25 張名片的人名判準兩邊都是 20/20。
    純粹是後端差異,不是拿準確度換速度。代價是記憶體,由 `_over_memory_limit`
    兜住——**那層守衛不是保險而是必要條件**,理由見該函式。

    **GPU 與 NPU 都不划算,別再重開**——理由與數據見 `_build_onnxruntime`。"""
    engine = _build_openvino(threads)
    if engine is not None:
        return engine
    return _build_onnxruntime(threads)


def _bundled_models() -> dict[str, Path] | None:
    """rapidocr wheel 內那三顆 ONNX 的路徑,少一顆就回 None。

    **一定要明確指定 model_path**:OpenVINO 後端在 model_path 為 None 時會
    自己去抓「OpenVINO 格式」的模型(`OpenVINOInferSession.__init__` 裡的
    DownloadFile),那就違反了「OCR 不連網」。給了路徑就完全不走那條。

    **靠關鍵字挑檔不寫死檔名**:檔名帶版本(`PP-OCRv6_det_small.onnx`),
    寫死的話 rapidocr 一升版就靜靜找不到檔案、退回較慢的後端,而那只會
    表現成「怎麼變慢了」。"""
    try:
        import rapidocr

        folder = Path(rapidocr.__file__).parent / "models"
        found = {}
        for task in ("det", "cls", "rec"):
            hits = [p for p in folder.glob("*.onnx") if task in p.name.lower()]
            if len(hits) != 1:
                logger.debug("找不到唯一的 %s 模型(%d 個)", task, len(hits))
                return None
            found[task] = hits[0]
        return found
    except Exception:  # noqa: BLE001 - 找不到就退回,不該讓 OCR 掛掉
        logger.debug("找不到 rapidocr 內建模型", exc_info=True)
        return None


def _build_openvino(threads: int):
    """OpenVINO CPU 後端;不可用就回 None(由呼叫端退回 onnxruntime)。"""
    models = _bundled_models()
    if models is None:
        return None
    try:
        from rapidocr import RapidOCR
        from rapidocr.utils.typings import EngineType

        params = {"Global.log_level": "error"}
        for section, task in (("Det", "det"), ("Cls", "cls"), ("Rec", "rec")):
            # engine_type 只吃 Enum,給字串會拋 TypeError
            params[f"{section}.engine_type"] = EngineType.OPENVINO
            params[f"{section}.model_path"] = str(models[task])
        if threads and threads > 0:
            # 對應「CPU 核心數」設定。OpenVINO 的鍵與 ORT 的
            # intra_op_num_threads **不同名**,換後端最容易漏的就是它
            params["EngineConfig.openvino.inference_num_threads"] = threads
        return RapidOCR(params=params)
    except Exception:  # noqa: BLE001 - 換後端失敗只是慢一點,不是壞掉
        logger.warning("OpenVINO 後端無法建立,改用 onnxruntime", exc_info=True)
        return None


def _working_set_mb() -> float | None:
    """本行程目前佔用的實體記憶體(MB);量不到回 None。

    **用 K32GetProcessMemoryInfo(kernel32)不用 psapi.dll 那個**:後者在
    新版 Windows 上是轉發樁,直接呼叫常常回 0 而且不報錯——那會讓守衛
    看起來有在跑、實際永遠不觸發。"""
    if sys.platform != "win32":
        return None
    try:
        class _Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in (
                    "PeakWorkingSetSize", "WorkingSetSize",
                    "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                    "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                    "PagefileUsage", "PeakPagefileUsage")]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.K32GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(_Counters), wintypes.DWORD]
        k32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        counters = _Counters()
        counters.cb = ctypes.sizeof(counters)
        if not k32.K32GetProcessMemoryInfo(
                k32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return None
        return counters.WorkingSetSize / 1e6
    except Exception:  # noqa: BLE001 - 量不到就當守衛不存在,不影響辨識
        logger.debug("讀取記憶體用量失敗", exc_info=True)
        return None


def _over_memory_limit() -> bool:
    """該把引擎丟掉重建了嗎?

    **記憶體守衛是 OpenVINO 後端的必要條件,不是保險**(2026-08-03 實測):
    它為每一種輸入形狀快取編譯好的核心,而**文件批次每張圖的形狀都不一樣**,
    所以工作集只升不降——同一批 120 張圖,onnxruntime 始終回到 200~300MB,
    OpenVINO 20 張後 10.7GB、120 張後 12.6GB。8GB 基準機會直接 OOM,而 OOM
    的表現是子行程無聲消失(父行程只看到一個 returncode)。

    **判準用記憶體不用張數**:每張圖的用量差很大(同樣 20 張,有時 10.7GB
    有時 2.4GB,取決於圖的尺寸與行數),張數門檻不是太早就是太晚。

    量不到就回 False(當守衛不存在):那時記憶體高,總比每張圖都白重建一次
    好——而且 `_working_set_mb` 量不到只會發生在非 Windows 上。"""
    used = _working_set_mb()
    if used is None or used < _MEM_LIMIT_MB:
        return False
    logger.info("記憶體用量 %.0f MB 超過 %d MB,重建辨識引擎", used, _MEM_LIMIT_MB)
    return True


def _recycle(threads: int):
    """建一顆新引擎。**呼叫之前必須先把舊引擎的參照清掉**(見 `_serve`)。

    順序是這條的全部重點:先建新的再丟舊的,峰值會是兩顆相加,而且**舊的
    根本不會被回收**——rapidocr 的物件之間有循環參照,單靠 refcount 放不掉,
    要等分代 GC 自己想跑才會動。實測「先建後丟」的版本跑完 40 張仍佔著
    11GB(等於守衛完全無效),改成「先丟 + gc.collect() 再建」才真的降下來。"""
    gc.collect()
    freed = _working_set_mb()
    engine = build_engine(threads)
    logger.info("重建完成:釋放後 %.0f MB,新引擎 %.0f MB",
                freed or -1, _working_set_mb() or -1)
    return engine


def _build_onnxruntime(threads: int):
    """退路:OpenVINO 起不來時用。也是 **GPU / NPU 為什麼不在選項內**的存放處
    (2026-08-03 實測結案,別再重開):

    - **NPU 連編譯都過不了**:`Upper bounds are not specified for node
      'Conv.0'`——模型的 batch/高/寬都是無上界的動態維度,而 NPU 是靜態
      管線(同 `scripts/bench_npu.py` 記的那一類)。而且 rec 的寬度**本質
      上**就是動態的(=48×該行長寬比),固定死會把長行壓扁。
    - **GPU 帳面上快、實際是淨損失**:Intel Arc 140V 上單顆模型比 CPU 快
      3.8~5.2 倍,但那是「同一個輸入形狀重複跑」量出來的。文件批次每張圖
      的形狀都不同,GPU 每遇到新形狀就要重編核心——同一張圖重跑 226ms/次
      vs 十張不同的圖 3,825ms/張(**17 倍**),CPU 同樣的對照只有 1.2 倍;
      整批 25 張名片 GPU 8,802ms/張 vs CPU 631ms/張。⚠️ 這個教訓比結論重要:
      **拿同一張圖暖機再計時,會把形狀切換的成本整個藏起來**(原型量到
      「GPU 170ms」正是這麼來的)。另外 GPU 預設 fp16 會讓 25 張名片有 8 張
      文字改變(f32 才與 CPU 一致)。"""
    _preload_pip_onnxruntime()
    from rapidocr import RapidOCR

    params = {"Global.log_level": "error"}
    if threads and threads > 0:
        params["EngineConfig.onnxruntime.intra_op_num_threads"] = threads
    return RapidOCR(params=params)


def _normalise_result(out) -> list[tuple]:
    """把 RapidOCR 的回傳整成 [(box, text, score), …]。

    形狀**依版本而異**,三種都要接住,否則版本一升就整條 OCR 掛掉:
    3.x 回一個 `RapidOCROutput`(boxes/txts/scores 三個平行序列)、1.x 回
    `(result, elapse)` tuple 或直接一個 list,而「沒有文字」在 3.x 是各欄位
    為 None、在 1.x 是 result 為 None。"""
    if hasattr(out, "txts"):
        # **看有沒有這個屬性,不看它是不是 None**:3.x 的「整張圖沒有文字」
        # 正是三個欄位都 None,拿 None 當「不是 3.x」會掉回 1.x 分支然後
        # 對著一個不可迭代的物件炸掉
        #
        # **三個欄位一律 `is None` 判斷,不可寫成 `x or []`**:3.x 的 boxes
        # 是 numpy 陣列,而 `or` 會去取它的真假值 → ValueError(元素超過一個
        # 就無法判定真假)。這條在單元測試裡看不出來(假引擎給的是 list),
        # 是拿真實影像跑稽核才炸出來的
        txts = [] if out.txts is None else list(out.txts)
        boxes = getattr(out, "boxes", None)
        boxes = [] if boxes is None else list(boxes)
        scores = getattr(out, "scores", None)
        scores = [] if scores is None else list(scores)
        return [
            (boxes[i] if i < len(boxes) else [],
             txt,
             scores[i] if i < len(scores) else 0.0)
            for i, txt in enumerate(txts)
        ]
    result = out[0] if isinstance(out, tuple) else out
    return list(result or [])


# 偵測模型會把**最短邊**放大到 736(rapidocr 的 `limit_type: min`)。細長條
# 圖因此會被放大到荒謬的尺寸,超過這個倍數就直接跳過——見 `_too_thin`。
# 64 倍 = 最短邊不到 12px,那種高度**放不下任何一個字**,跳過不會丟東西。
# **刻意訂得很寬**:實測一張 600×19 的窄圖(放大 39 倍)是有文字的,設成
# 「夠省記憶體」的門檻會把它一起丟掉,而無聲丟字比多用記憶體嚴重得多
_MAX_DET_UPSCALE = 64.0
_DET_MIN_SIDE = 736


def _too_thin(image_path: str) -> bool:
    """這張圖細長到會把偵測模型的輸入撐爆嗎?

    **實測踩到的地雷**(2026-08-03):一張 **4×281** 的分隔線圖(網頁與文件
    裡到處都是),偵測前處理把最短邊 4 放大到 736 = **184 倍**,整張變成
    736×51,704 的張量——單這一張就吃掉 **10.2GB**,而辨識結果是 0 段文字。
    8GB 機器會直接 OOM,**兩個後端都中**(onnxruntime 只是比較輕)。

    `docimage._MIN_OCR_PIXELS`(100×100)擋得掉這一張,但擋不掉「4×5000」
    那種面積夠大的細長條;而且**不是每個呼叫端都走 docimage**(命令列的
    稽核腳本就直接呼叫 `ocr.recognize`)。所以判準放在最底層這裡。

    門檻訂得很寬(最短邊不到 12px 才擋),因為**這條的目的是擋掉不可能有
    字的極端值,不是省記憶體**:實測 600×19 的窄圖有文字、也才用 703MB,
    收緊到「夠省記憶體」的程度就會把它一起丟掉。記憶體由 `_over_memory_limit`
    負責,兩件事不要混在一起。

    讀不到尺寸就放行:讓後面的正常流程去處理壞檔,不要在這裡多一種失敗。"""
    short = _preflight(image_path)[0]
    return short is not None and short > 0 and _DET_MIN_SIDE / short > _MAX_DET_UPSCALE


def _preflight(image_path: str):
    """開一次圖,回傳 (最短邊, 要餵給引擎的東西)。

    第二個回傳值平常就是原本的路徑;**只有調色盤/CMYK 這類模式**才會解成
    RGB 陣列再餵——rapidocr 對它們讀出來的是垃圾。實測一張 700×515、mode=P
    的 Windows 對話框截圖:直接餵路徑得到 6 段 105 字的亂碼(信心 0.584),
    先轉 RGB 得到 24 段 492 字、信心 0.975,整張正確。

    **這是無聲的品質流失**:輸出看起來仍是一份文字,不會有任何錯誤。全碟
    掃描的低信心檔案裡 78.6% 是 P 模式,而 P 在整體語料只佔 3.8%——差距
    大到不可能是巧合。

    `docimage._normalise` 本來就會 convert("RGB"),所以走 doc2md 的路徑不受
    影響;但**不是每個呼叫端都走 docimage**(`scripts/audit_ocr.py` 直接呼叫
    `ocr.recognize`),所以判準要放在最底層這裡。

    傳陣列而不是另存暫存檔:rapidocr 兩種都吃,而暫存檔要管生命週期。"""
    try:
        from PIL import Image

        with Image.open(image_path) as im:
            short = min(im.size)
            if im.mode in ("RGB", "L"):
                return short, image_path
            import numpy as np

            return short, np.asarray(im.convert("RGB"))
    except Exception:  # noqa: BLE001 - 讀不到就交給後面的正常流程處理壞檔
        logger.debug("影像前置檢查失敗,原樣交給引擎:%s", image_path, exc_info=True)
        return None, image_path


def recognise(engine, image_path: str) -> list[dict]:
    """辨識一張圖 → [{"text":…, "box":[x0,y0,x1,y1], "score":…}]。"""
    short, source = _preflight(image_path)
    if short is not None and short > 0 and _DET_MIN_SIDE / short > _MAX_DET_UPSCALE:
        logger.info("影像過於細長,跳過辨識(會把偵測模型撐爆):%s", image_path)
        return []
    lines: list[dict] = []
    for item in _normalise_result(engine(source)):
        try:
            box, text, score = item[0], item[1], item[2]
        except (TypeError, IndexError, KeyError):
            continue
        text = str(text or "").strip()
        if not text:
            continue
        try:
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
        except (TypeError, IndexError, ValueError):
            xs = []
        if not xs:
            # 沒有座標就排不了序(lines_to_text 靠框的位置重建行)。真實引擎
            # 的 boxes 與 txts 永遠等長,走到這裡代表形狀出乎意料——留個腳印
            logger.debug("辨識結果缺少座標,略過這一段:%.40s", text)
            continue
        lines.append({
            "text": text,
            "box": [min(xs), min(ys), max(xs), max(ys)],
            "score": float(score or 0.0),
        })
    return lines


def _serve(stdin, stdout, engine, threads: int = 0) -> None:
    """請求迴圈。抽成獨立函式是為了能用 StringIO + 假引擎直接測——
    真正的 OCR 引擎不該是「協定有沒有寫對」這件事的前提。

    **任何單一請求的失敗都回一行錯誤、不中斷迴圈**:一張圖壞掉不該讓
    後面幾百張跟著陪葬(父行程會把它記成該頁的 OCR 失敗)。

    記憶體守衛(`_over_memory_limit`)擺在**回覆送出之後**:重建要一秒上下,
    夾在辨識與回覆之間會平白墊高父行程量到的單張耗時,還可能逼近看門狗。
    這樣擺的話那一秒落在父行程準備下一張圖的空檔裡。"""
    for raw in stdin:
        raw = raw.strip()
        if not raw:
            continue
        if len(raw) > _MAX_LINE:
            _write(stdout, {"ok": False, "error": "請求過長"})
            continue
        try:
            req = json.loads(raw)
        except ValueError:
            _write(stdout, {"ok": False, "error": "請求不是合法的 JSON"})
            continue
        # 正常收尾是父行程關 stdin(見 ocr.py 的模組 docstring),這條
        # 只是保險——留著的成本是兩行
        if req.get("cmd") == "quit":
            return
        image = req.get("image")
        if not image:
            _write(stdout, {"ok": False, "error": "請求缺少 image"})
            continue
        try:
            _write(stdout, {"ok": True, "lines": recognise(engine, image)})
        except Exception as e:  # noqa: BLE001 - 什麼都不能讓迴圈停下來
            logger.exception("辨識失敗:%s", image)
            _write(stdout, {"ok": False, "error": f"{type(e).__name__}: {e}"})
        # 失敗的那張也要看:炸掉之前照樣可能已經把記憶體撐上去
        if _over_memory_limit():
            # **先把本地這份參照清掉再重建**——不清的話新舊兩顆並存,峰值
            # 加倍而且舊的不會被回收(理由見 `_recycle`)
            engine = None
            try:
                engine = _recycle(threads)
            except Exception:  # noqa: BLE001
                # 舊引擎已經丟了,救不回來:收工讓父行程重啟一支乾淨的
                # (那條路本來就存在,見 ocr.py 的 `_with_worker`)
                logger.exception("重建辨識引擎失敗,結束子行程等待父行程重啟")
                return


def _write(stdout, payload: dict) -> None:
    stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    stdout.flush()  # 父行程是逐行讀的,不 flush 就等於沒回


def main() -> int:  # pragma: no cover - bootstrap,由 ocr.py 以子行程啟動
    # 三條都要釘(見 stdio):先前只釘 stdin/stdout,漏掉的 stderr 讓本行程的
    # 繁中 log 在父端(以 UTF-8 解碼)變成替代字元——那些行現在會落地進
    # 紀錄檔(filelog),而紀錄檔存在的唯一理由就是事後分析
    from meeting_scribe import stdio

    stdio.force_utf8()
    # root 留在 WARNING(擋掉第三方的 INFO 洗版),但**自家模組放到 INFO**:
    # 引擎重建這種事必須在紀錄檔看得到,不然「怎麼變慢了」永遠查不出來。
    # stderr 會被父行程 drain 進 DEBUG 紀錄檔(filelog),不會進黑視窗
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    logger.setLevel(logging.INFO)
    threads = 0
    if "--threads" in sys.argv:
        try:
            threads = int(sys.argv[sys.argv.index("--threads") + 1])
        except (IndexError, ValueError):
            threads = 0
    try:
        engine = build_engine(threads)
    except Exception as e:
        _write(sys.stdout, {"ready": False, "error": f"{type(e).__name__}: {e}"})
        logger.exception("OCR 引擎啟動失敗")
        return 1
    _write(sys.stdout, {"ready": True})
    # **main 這一格不可以留著第一顆引擎的參照**:留了的話記憶體守衛永遠
    # 釋放不掉它——`_serve` 只清得掉自己那份,而這裡這份會活到行程結束。
    # 實測沒有這兩行時:重建 35 次、每次 gc.collect() 前後都是 10,222 MB,
    # 守衛看起來有在跑、實際一個位元組都沒放掉。用 list.pop() 是為了在
    # **呼叫發生的當下**就沒有任何區域變數綁著它。
    #
    # threads 也要傳下去:重建時得用同一組執行緒設定(對應介面上的
    # 「CPU 核心數」),否則重建之後就悄悄回到預設值
    holder = [engine]
    del engine
    _serve(sys.stdin, sys.stdout, holder.pop(), threads)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
