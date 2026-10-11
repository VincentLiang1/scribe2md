r"""應用程式圖示的**畫法**:超橢圓輪廓、漸層底、超取樣、多尺寸 `.ico` 打包。

**這裡只有「怎麼畫」,沒有「畫什麼」。** 底色、記號的形狀與座標、反光的強度全部
留在下游那支 `scripts/make_icon.py`——⚠️ **圖示的圖案就是那支 app 的臉**,它正是
兩支程式必須長得不一樣的地方。

⚠️ **16px 才是這顆圖示真正的工作尺寸**(工作列與 Alt+Tab),而 512 的設計畫布縮到
16 是 32 倍——所以下游的線寬與間隙都不敢低於 32 設計單位(=1px)。那條規則寫在
下游的常數旁邊,因為要遵守它的是那些座標。
"""
from __future__ import annotations

import math

from PIL import Image, ImageDraw


# 超取樣:PIL 的 ImageDraw 沒有反鋸齒,一律畫大再縮。
#
# ⚠️ **不要用「目標尺寸 × 固定倍率」當畫布**(2026-08-26 實測)。16px 配 8 倍只有
# 128px 的畫布,而播放鍵的圓角是靠「把邊描粗 26 單位再收圓」做的,那條線在 128px
# 上只剩 6 像素寬,Pillow 畫得不準。改成一律先畫在 ≥2048 的母版上再縮,同一顆
# 圖示的色差:16px 最大 43/255、32px 最大 128/255、48px 最大 85/255(平均都在
# 1~2/255,差的是輪廓那幾個像素)。
#
# ⚠️ 順帶一筆免得下次又追錯方向:**16px 的三角形斜邊本來就會有階梯**,那是
# 4 像素高的斜線,不是畫錯。同一天我一度把它當成形狀的 bug,把 512 那張放大
# 看才確認輪廓是乾淨的。
SS = 8
MASTER_MIN = 2048   # 再小畫不準(上面那筆)
MASTER_MAX = 4096   # 再大只是浪費:4096² RGBA 已經 67MB


def master_size(size: int) -> int:
    """畫這個尺寸時,母版該用多大。

    ⚠️ **不要用「目標尺寸 × 固定倍率」**(2026-08-26 實測):16px 配 8 倍只有 128px 的
    畫布,而細部(例如靠「描粗再收圓」做出來的圓角)在那個尺寸上 Pillow 畫不準。一律
    先畫在 ≥`MASTER_MIN` 的母版上再縮。"""
    return min(max(size * SS, MASTER_MIN), MASTER_MAX)


# .ico 的尺寸表。16/32 是瀏覽器分頁與工作列真正會用的,48 是檔案總管的「中圖示」,
# 256 是「超大圖示」與 Alt+Tab
ICO_SIZES = (16, 32, 48, 64, 128, 256)


def save_ico(path, render, sizes=ICO_SIZES) -> None:
    """把 `render(n)` 畫出來的每一檔尺寸打包成一個多尺寸 `.ico`。

    ⚠️ **由大到小**餵給 Pillow:第一張是主圖、其餘走 `append_images`,而 `sizes`
    裡沒有對應來源的那幾檔由 Pillow 自己縮——主圖給最大的那張,縮小的品質遠好過
    放大。"""
    frames = [render(n) for n in sorted(sizes, reverse=True)]
    frames[0].save(path, format="ICO", sizes=[(n, n) for n in sizes],
                   append_images=frames[1:])


def squircle(canvas: float, n: float,
             steps: int = 720) -> list[tuple[float, float]]:
    """超橢圓的封閉折線(`|x|^n + |y|^n = r^n`,座標落在 0..canvas)。

    ⚠️ `n` 由下游給:圖示的外框比皮膚的圓角**方**得多(這兩支程式的圖示都用 5.0,
    而皮膚是 2.0 的正圓弧),而那是「這顆圖示長什麼樣」的一部分。"""
    r = canvas / 2
    pts = []
    for i in range(steps):
        t = 2 * math.pi * i / steps
        ct, st = math.cos(t), math.sin(t)
        pts.append((r + r * math.copysign(abs(ct) ** (2 / n), ct),
                    r + r * math.copysign(abs(st) ** (2 / n), st)))
    return pts


def gradient(size: int, top: tuple[int, int, int],
              bottom: tuple[int, int, int]) -> Image.Image:
    """垂直線性漸層。逐列填,size 最大 4096、成本可以忽略。"""
    img = Image.new("RGB", (size, size))
    d = ImageDraw.Draw(img)
    for y in range(size):
        f = y / max(size - 1, 1)
        d.line([(0, y), (size, y)],
               fill=tuple(round(a + (b - a) * f) for a, b in zip(top, bottom)))
    return img
