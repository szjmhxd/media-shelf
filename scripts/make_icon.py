"""生成应用图标 assets/icon.ico（多尺寸），供打包与仓库使用。"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

MASTER = 1024
OUT = Path(__file__).resolve().parents[1] / "assets"

BG_TOP = (32, 46, 70)
BG_BOTTOM = (13, 22, 36)
ACCENT = (247, 183, 49)
STRIP = (232, 238, 246)


def _gradient(size: int) -> Image.Image:
    img = Image.new("RGB", (1, size))
    draw = ImageDraw.Draw(img)
    for y in range(size):
        t = y / max(size - 1, 1)
        draw.point(
            (0, y),
            fill=tuple(int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3)),
        )
    return img.resize((size, size), Image.NEAREST)


def build() -> Image.Image:
    img = _gradient(MASTER).convert("RGBA")

    # 圆角遮罩，留出一点透明边距
    mask = Image.new("L", (MASTER, MASTER), 0)
    ImageDraw.Draw(mask).rounded_rectangle((24, 24, MASTER - 24, MASTER - 24), radius=200, fill=255)
    img.putalpha(mask)

    layer = Image.new("RGBA", (MASTER, MASTER), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    # 胶片外框（留边距，让圆角矩形不贴边）
    left, top, right, bottom = 196, 268, MASTER - 196, MASTER - 268
    d.rounded_rectangle((left, top, right, bottom), radius=96, outline=STRIP, width=30, fill=None)

    # 上下齿孔：贴在内侧，小而匀，避免看起来像锯齿
    hole_w, hole_h = 46, 34
    inset = 58
    count = 5
    usable = (right - left) - inset * 2
    step = (usable - hole_w) / (count - 1)
    for i in range(count):
        x = left + inset + step * i
        for y in (top + 30, bottom - 30 - hole_h):
            d.rounded_rectangle((x, y, x + hole_w, y + hole_h), radius=10, fill=STRIP)

    # 中心播放三角：按外接框居中，视觉重心才不偏
    cx, cy = MASTER / 2, MASTER / 2
    half_w, half_h = 118, 150
    d.polygon(
        [
            (cx - half_w, cy - half_h),
            (cx - half_w, cy + half_h),
            (cx + half_w, cy),
        ],
        fill=ACCENT,
    )

    img.alpha_composite(layer)
    return img


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    master = build()
    master.save(OUT / "icon.png")
    sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
    master.save(OUT / "icon.ico", sizes=sizes)
    print("已写入", OUT / "icon.ico", (OUT / "icon.ico").stat().st_size, "bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
