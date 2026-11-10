"""重新生成 README banner：把 my-pi-agent 品牌替换为 my-agent-harness。

原图是 1376x768 的 JPEG（扩展名误标为 .png）。
这里用 PIL 重绘制一张风格一致的 PNG：
- 深色底 + 绿青径向光晕
- 窗口卡片 + 标题栏
- 左侧代码片段（my-agent-harness 导入示例）
- 中央大字标题
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1376, 768
OUT = Path("banner.png")

# ── 配色 ──
BG_DARK = (8, 15, 23)
GREEN = (2, 98, 61)
GREEN_BRIGHT = (52, 211, 153)
CYAN = (34, 211, 238)
TEXT_MAIN = (158, 252, 216)
TEXT_DIM = (110, 160, 150)
WINDOW_BG = (12, 26, 29)


def find_font(size: int) -> ImageFont.FreeTypeFont:
    """找一个可用的等宽/黑体字体。"""
    candidates = [
        r"C:\Windows\Fonts\consola.ttf",
        r"C:\Windows\Fonts\consolab.ttf",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\arial.ttf",
    ]
    for c in candidates:
        if Path(c).exists():
            try:
                return ImageFont.truetype(c, size)
            except OSError:
                continue
    return ImageFont.load_default()


def make_glow(size: tuple[int, int], center: tuple[int, int], radius: int, color, alpha: int) -> Image.Image:
    """生成一个径向渐变光晕图层。"""
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    steps = 40
    for i in range(steps, 0, -1):
        r = int(radius * i / steps)
        a = int(alpha * (1 - i / steps) ** 1.6)
        if a <= 0:
            continue
        cx, cy = center
        d.ellipse([cx - r, cy - r * 0.75, cx + r, cy + r * 0.75], fill=color + (a,))
    return layer.filter(ImageFilter.GaussianBlur(60))


def main() -> None:
    img = Image.new("RGB", (W, H), BG_DARK)

    # ── 光晕 ──
    img = Image.alpha_composite(img.convert("RGBA"), make_glow((W, H), (230, 80), 620, GREEN, 235)).convert("RGB")
    img = Image.alpha_composite(img.convert("RGBA"), make_glow((W, H), (1180, 110), 560, CYAN, 120)).convert("RGB")
    img = Image.alpha_composite(img.convert("RGBA"), make_glow((W, H), (1080, 700), 460, GREEN, 100)).convert("RGB")

    draw = ImageDraw.Draw(img)

    # ── 网格（独立透明图层，避免被 RGB 转换吞掉 alpha）──
    grid = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grid)
    for x in range(0, W, 92):
        gd.line([(x, 0), (x, H)], fill=(120, 255, 220, 14), width=1)
    for y in range(0, H, 92):
        gd.line([(0, y), (W, y)], fill=(120, 255, 220, 14), width=1)
    img = Image.alpha_composite(img.convert("RGBA"), grid).convert("RGB")
    draw = ImageDraw.Draw(img)

    # ── 窗口卡片 ──
    wx0, wy0, wx1, wy1 = 300, 150, 1076, 640
    # 窗口投影
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rounded_rectangle([wx0 + 8, wy0 + 14, wx1 + 8, wy1 + 14], radius=18, fill=(0, 0, 0, 160))
    shadow = shadow.filter(ImageFilter.GaussianBlur(28))
    img.paste(Image.alpha_composite(img.convert("RGBA"), shadow).convert("RGB"), (0, 0))
    draw = ImageDraw.Draw(img)
    # 窗口主体（带青色描边高光）
    draw.rounded_rectangle([wx0, wy0, wx1, wy1], radius=18, fill=(10, 24, 27), outline=(48, 120, 110), width=2)
    # 标题栏
    draw.rounded_rectangle([wx0, wy0, wx1, wy0 + 38], radius=14, fill=(31, 45, 48))
    draw.rectangle([wx0, wy0 + 26, wx1, wy0 + 38], fill=(31, 45, 48))
    # 三个圆点
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        cx = wx0 + 22 + i * 20
        draw.ellipse([cx - 6, wy0 + 12, cx + 6, wy0 + 24], fill=c)

    mono_l = find_font(15)
    mono_s = find_font(13)
    mono_big = find_font(44)

    # 标题栏文字
    title = "my-agent-harness"
    tw = draw.textlength(title, font=mono_s)
    draw.text(((wx0 + wx1) / 2 - tw / 2, wy0 + 11), title, font=mono_s, fill=(170, 200, 195))

    # ── 左侧代码片段 ──
    code_y = wy0 + 66
    code_lines = [
        ("import", " { agent }", "from 'my-agent-harness'"),
        ("", "", None),
        ("const", " runner = {", None),
        ("", "  loop:    'react-pure-fn',", None),
        ("", "  tools:   [read, write, edit, bash],", None),
        ("", "  session: 'entry-tree-jsonl',", None),
        ("", "  cache:   'append-only',", None),
        ("", "}", None),
    ]
    for i, (kw, rest, suffix) in enumerate(code_lines):
        y = code_y + i * 24
        if y > wy1 - 24:
            break
        x = wx0 + 22
        if kw:
            draw.text((x, y), kw, font=mono_l, fill=(120, 200, 255))
            x += draw.textlength(kw, font=mono_l)
        if rest:
            draw.text((x, y), rest, font=mono_l, fill=TEXT_DIM)
            x += draw.textlength(rest, font=mono_l)
        if suffix:
            draw.text((x, y), suffix, font=mono_l, fill=(150, 190, 180))

    # ── 中央标题 ──
    big = "my-agent-harness"
    bw = draw.textlength(big, font=mono_big)
    bx = (wx0 + wx1) / 2 - bw / 2
    by = wy0 + 330
    # 阴影
    draw.text((bx + 2, by + 2), big, font=mono_big, fill=(10, 40, 35))
    draw.text((bx, by), big, font=mono_big, fill=TEXT_MAIN)

    # 副标题
    sub = "Python agent harness  ·  ReAct kernel  ·  Pi-TUI frontend"
    small = find_font(16)
    sw = draw.textlength(sub, font=small)
    draw.text(((wx0 + wx1) / 2 - sw / 2, by + 74), sub, font=small, fill=(120, 180, 170))

    img.save(OUT, "PNG", optimize=True)
    print("已生成:", OUT, img.size)


if __name__ == "__main__":
    main()
