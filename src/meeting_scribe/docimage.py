r"""影像 → 文字:獨立影像檔的轉換,以及**所有 reader 共用的內嵌圖 OCR**。

`ocr_image_bytes` 是這裡最重要的東西:docoffice / docpdf / docweb / docmail
抽到的每一張內嵌圖都經過它。**給 AI 用時,圖裡的文字才是重點**——只留一個
圖片連結等於什麼都沒給(使用者 2026-08-01 指定內嵌圖也要 OCR)。

辨識本身在子行程裡做(見 `ocr.py`),本模組只負責前置:格式解碼、EXIF
轉正、尺寸正規化,以及把結果包成 Block。
"""
import contextlib
import io
import logging
import tempfile
import types
from collections.abc import Iterator
from pathlib import Path

from meeting_scribe import docmd, ocr, pipeline
from meeting_scribe.docmd import AssetsDir, Block, Heading, Image, Note, Para
from meeting_scribe.errors import UserFacingError

logger = logging.getLogger(__name__)

# 惰性載入的佔位(測試 monkeypatch 這兩個屬性換假貨)
PIL = None
pillow_heif = None

# 小於這麼多像素的圖直接跳過 OCR:項目符號圖示、裝飾線、圓角遮罩都在
# 這個量級,對它們跑 OCR 是純浪費(一張圖 1~3 秒)
_MIN_OCR_PIXELS = 100 * 100
# 送進 OCR 前的像素上限。超大圖(手機拍的 4800 萬像素)會讓子行程的
# 記憶體與時間都爆掉,而 OCR 的解析度甜蜜點遠低於此
_MAX_OCR_PIXELS = 12_000_000
# 多頁 TIFF(掃描器的預設輸出)最多處理幾頁:再多就該當成 PDF 處理了
_MAX_FRAMES = 200


def _ensure_pillow():
    global PIL
    if PIL is None:
        docmd.lazy_import("PIL", "影像")
        from PIL import Image as _im
        from PIL import ImageOps as _ops
        PIL = types.SimpleNamespace(Image=_im, ImageOps=_ops)
    return PIL


def ensure_ready() -> None:
    """預熱影像解碼器(給 docpipe 的路由表用)。

    heif 是可選的:缺它只影響 .heic,不該擋掉其他影像格式,所以
    只在這裡順手註冊、不把失敗往上拋。"""
    _ensure_pillow()
    _ensure_heif()


def _ensure_heif() -> bool:
    """註冊 HEIC 解碼器。回傳是否可用——缺它只影響 .heic,不該擋其他格式。"""
    global pillow_heif
    if pillow_heif is None:
        try:
            import pillow_heif as _real

            _real.register_heif_opener()
            pillow_heif = _real
        except Exception:
            logger.debug("pillow-heif 不可用,.heic 將無法開啟", exc_info=True)
            pillow_heif = False
    return bool(pillow_heif)


@contextlib.contextmanager
def _as_png(im) -> Iterator[Path]:
    """把 Pillow 影像存成暫存 PNG,交給 OCR 子行程。

    走檔案而不是把位元組塞進 pipe:Windows 上以 pipe 傳大量二進位是死結
    溫床(見 ocr.py),而 PNG 編碼的 30ms 對比 OCR 的 1~3 秒可以忽略。
    暫存目錄沿用管線的「前綴 + 自清」協定。"""
    with tempfile.TemporaryDirectory(prefix=pipeline.TMP_PREFIX + "ocr-") as d:
        path = Path(d) / "page.png"
        im.save(path, format="PNG")
        yield path


def _normalise(im):
    """EXIF 轉正 + 轉 RGB + 壓到像素上限。

    EXIF 轉正是必要的:手機拍的照片常是「橫著存、靠 EXIF 標示要轉 90 度」,
    不轉的話 OCR 會對著側躺的字辨識,幾乎全錯。"""
    mod = _ensure_pillow()
    try:
        im = mod.ImageOps.exif_transpose(im) or im
    except Exception:
        logger.debug("EXIF 轉正失敗,沿用原方向", exc_info=True)
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    w, h = im.size
    if w * h > _MAX_OCR_PIXELS:
        scale = (_MAX_OCR_PIXELS / (w * h)) ** 0.5
        im = im.resize((max(int(w * scale), 1), max(int(h * scale), 1)))
    return im


# markdown 顯示不出來的格式:Windows 中繼檔。舊版 Word 的圖表、SmartArt、
# 貼進來的 Visio 圖幾乎都是 emf——原樣存檔的話 md 連結指向一個 VS Code 與
# GitHub 都畫不出來的檔案,等於沒有圖(使用者 2026-08-02 的 2018 年 docx:
# 5 張圖有 4 張是 emf)。Pillow 在 Windows 上讀得動它們
_VECTOR_SUFFIXES = {".emf", ".wmf"}


def to_displayable(data: bytes, suffix: str) -> tuple[bytes, str]:
    """把 markdown 顯示不出來的圖轉成 PNG;能顯示的原樣回傳。

    轉不動就原樣回去——留一個打不開的檔,仍然好過把圖整個丟掉
    (呼叫端會在 md 裡留下連結,人至少知道那裡有東西、找得到檔案)。"""
    if suffix.lower() not in _VECTOR_SUFFIXES:
        return data, suffix
    mod = _ensure_pillow()
    try:
        with mod.Image.open(io.BytesIO(data)) as im:
            out = io.BytesIO()
            im.convert("RGB").save(out, format="PNG")
            return out.getvalue(), ".png"
    except Exception:
        logger.debug("向量圖轉 PNG 失敗(%s),原樣保留", suffix, exc_info=True)
        return data, suffix


def ocr_image_bytes(data: bytes, label: str, ocr_enabled: bool = True) -> list[Block]:
    """一份圖片位元組 → OCR 文字的 Blocks(沒有文字就回空清單)。

    **所有 reader 的內嵌圖都走這裡**。失敗一律只記 log 回空清單:圖裡
    讀不到字,不該讓整份文件轉不出來。

    文字前面一定帶來源標註——RAG 切塊後的 chunk 會脫離上下文,讀的人
    (與 AI)必須知道「這段是機器從圖片認出來的、可能有誤」。

    `label` 是**來源的名稱**(「這張影像」「第 3 頁的圖片」),會被填進
    「以下文字由{label}辨識而來」——所以不要自帶「辨識」「的文字」
    這類字眼,否則會出現「由影像辨識辨識而來」。"""
    if not ocr_enabled or not data:
        return []
    mod = _ensure_pillow()
    try:
        with mod.Image.open(io.BytesIO(data)) as raw:
            if raw.size[0] * raw.size[1] < _MIN_OCR_PIXELS:
                return []  # 圖示/裝飾,不值得花 1~3 秒
            im = _normalise(raw)
            with _as_png(im) as path:
                lines = ocr.recognize(path)
    except UserFacingError:
        raise  # OCR 整條停用要讓呼叫端知道
    except Exception:
        logger.debug("內嵌圖 OCR 失敗(%s),當作沒有文字", label, exc_info=True)
        return []
    text = ocr.lines_to_text(lines)
    if not text.strip():
        return []
    return [
        Note(f"以下文字由{label}辨識而來,可能有誤", docmd.KIND_OCR),
        Para(text),
    ]


# EXIF 的 IFD 指標與欄位編號。**不用 Pillow 的 TAGS 名稱表反查**:那張表
# 對 GPS 子 IFD 不適用,而且逐一比對字串比直接用編號慢又不穩
_IFD_EXIF, _IFD_GPS = 0x8769, 0x8825
_TAG_MAKE, _TAG_MODEL, _TAG_DATETIME = 0x010F, 0x0110, 0x0132
_TAG_DATETIME_ORIGINAL = 0x9003
_GPS_LAT_REF, _GPS_LAT, _GPS_LON_REF, _GPS_LON = 1, 2, 3, 4


def _exif_datetime(raw) -> str:
    """EXIF 的 `YYYY:MM:DD HH:MM:SS` → 一般寫法。

    只換前兩個冒號:時間那三段的冒號要留著。壞掉的值原樣回傳,不猜。"""
    text = str(raw or "").strip()
    date, _, time = text.partition(" ")
    if len(date) == 10 and date.count(":") == 2:
        return f"{date.replace(':', '-')} {time}".strip()
    return text


def _gps_decimal(value, ref) -> float | None:
    """(度, 分, 秒) + N/S/E/W → 十進位度數。

    **不做反向地理編碼**:那要連外,而本工具除了首次下載模型之外不連網
    (spec §7)。座標本身對 RAG 已經夠用(「這張照片拍於 25.03, 121.56」)。"""
    try:
        deg, minute, sec = (float(v) for v in value)
    except (TypeError, ValueError):
        return None
    dec = deg + minute / 60 + sec / 3600
    return -dec if str(ref or "").upper() in ("S", "W") else dec


def _exif_blocks(im) -> list[Block]:
    """影像的拍攝資訊(日期/座標/相機)。沒有就回空清單。

    **只給獨立的影像檔用**,不給文件內嵌圖:內嵌圖是插圖,它的 EXIF
    (如果還在的話)講的是插圖原始來源的事,對文件內容沒有幫助,
    每張圖多印三行只是雜訊。

    EXIF 惡名昭彰地不可靠(欄位缺、型別怪、值壞掉都常見),所以整段
    只 log 不拋——讀不到拍攝資訊絕不該讓一張圖轉不出來。

    沒有 EXIF **不下失真標記**:絕大多數影像本來就沒有,那不是「丟失」。"""
    try:
        exif = im.getexif()
    except Exception:  # noqa: BLE001
        logger.debug("EXIF 讀取失敗", exc_info=True)
        return []
    if not exif:
        return []
    lines: list[str] = []
    try:
        sub = exif.get_ifd(_IFD_EXIF) or {}
        when = _exif_datetime(sub.get(_TAG_DATETIME_ORIGINAL)
                              or exif.get(_TAG_DATETIME))
        if when:
            lines.append(f"- 拍攝日期:{when}")
        gps = exif.get_ifd(_IFD_GPS) or {}
        lat = _gps_decimal(gps.get(_GPS_LAT), gps.get(_GPS_LAT_REF))
        lon = _gps_decimal(gps.get(_GPS_LON), gps.get(_GPS_LON_REF))
        if lat is not None and lon is not None:
            lines.append(f"- 拍攝位置:緯度 {lat:.6f}、經度 {lon:.6f}")
        camera = " ".join(str(exif.get(tag) or "").strip()
                          for tag in (_TAG_MAKE, _TAG_MODEL)).strip()
        if camera:
            lines.append(f"- 拍攝裝置:{camera}")
    except Exception:  # noqa: BLE001
        logger.debug("EXIF 欄位解讀失敗", exc_info=True)
    # 標題讓這一塊被切出去之後仍然知道自己在講什麼(docmd.render 規則 3)
    return [Para("**拍攝資訊**\n" + "\n".join(lines))] if lines else []


def _page_count(im, suffix: str) -> tuple[int, bool]:
    """(要處理幾頁, 是不是被當成動畫只取一格)。

    多頁 TIFF 是掃描器的預設輸出,一份檔案就是一整疊紙——只讀第一頁
    等於丟掉其餘內容。但 gif/webp 的「多頁」是動畫,多取幾格只會得到
    同一句話的殘影。"""
    total = min(int(getattr(im, "n_frames", 1) or 1), _MAX_FRAMES)
    if total > 1 and suffix in (".gif", ".webp"):
        return 1, True
    return max(total, 1), False


def convert_image(src: Path, assets: AssetsDir, ocr_enabled: bool = True) -> list[Block]:
    """影像檔(jpg/png/tiff/bmp/gif/webp/heic)→ Blocks。

    影像本身也存進 assets:OCR 只認得出文字,圖表的形狀、手寫的塗鴉都
    留在原圖裡,人要核對時得看得到。"""
    mod = _ensure_pillow()
    if src.suffix.lower() in (".heic", ".heif") and not _ensure_heif():
        raise UserFacingError(
            f"無法開啟 HEIC 影像「{src.name}」:缺少解碼元件,"
            "請重新執行「安裝.bat」,或用「小畫家」另存為 JPG 再轉"
        )
    try:
        opened = mod.Image.open(src)
    except Exception as e:
        raise UserFacingError(
            f"無法開啟影像「{src.name}」:檔案可能已損壞或格式不支援"
        ) from e

    blocks: list[Block] = []
    with opened as im:
        # **在 seek 之前讀**:多頁影像 seek 過去之後,getexif 拿到的是那一頁
        # 的(通常是空的),整份檔案的拍攝資訊就沒了
        blocks.extend(_exif_blocks(im))
        total, animated = _page_count(im, src.suffix.lower())
        if animated:
            blocks.append(Note("這是動畫檔,只取第一格", docmd.KIND_IMAGE_ONLY))
        multi = total > 1
        # **邊 seek 邊處理,不先收集成清單**:多頁影像的每一頁共用同一個
        # Pillow 物件,`list(...)` 會在展開時把 seek 跑到最後,結果每一頁
        # 存出來的都是最後一頁的內容(測試逼出來的真 bug)
        for page_no in range(1, total + 1):
            try:
                im.seek(page_no - 1)
            except EOFError:  # n_frames 說謊的壞檔
                break
            if multi:
                blocks.append(Heading(1, f"第 {page_no} 頁"))
            # copy() 讓這一頁脫離共用的解碼緩衝,下一次 seek 才不會改到它
            normalised = _normalise(im.copy() if multi else im)
            buf = io.BytesIO()
            normalised.save(buf, format="PNG")
            data = buf.getvalue()
            link = assets.add_bytes(data, ".png")
            if link:
                blocks.append(Image(link, f"{src.stem} 第 {page_no} 頁" if multi else src.stem))
            # 這裡的「浪費」是刻意的,別順手優化掉:剛編好的 PNG 會被
            # ocr_image_bytes 解回來、原封不動再編一次到暫存檔(12MP 照片
            # 每張約 1~2 秒,未實測、量級推估)。省法是把 assets 落地的路徑
            # 直接餵給 ocr.recognize(它本來就吃路徑),但代價是 _MIN_OCR_PIXELS
            # 門檻與 OCR 前置邏輯從一處分成兩處,而且只有「整批都是照片」時
            # 才有感——內嵌圖只有位元組、走不到這條捷徑。使用者 2026-08-01
            # 權衡後決定不換,勿在無新指示下改回
            found = ocr_image_bytes(data, "這張影像", ocr_enabled)
            if found:
                blocks.extend(found)
            elif ocr_enabled:
                blocks.append(Note(
                    f"這張影像沒有辨識出文字{'(第 %d 頁)' % page_no if multi else ''}",
                    docmd.KIND_IMAGE_ONLY,
                ))
            else:
                blocks.append(Note(
                    "文字辨識(OCR)沒有啟用,這張影像的內容尚未取出",
                    docmd.KIND_IMAGE_ONLY,
                ))
    return blocks
