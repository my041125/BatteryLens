"""离线素材生成：Phosphor Icons (MIT) -> 小米风成套 PNG 图标。

只在本机运行一次，产出 assets/gen/*.png；运行时程序不依赖 svglib/reportlab。
用法: python tools\\_gen_icons.py
"""
import os
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from reportlab.graphics import renderPM
from svglib.svglib import svg2rlg

TOOLS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TOOLS)             # 项目根
ASSETS = os.path.join(ROOT, "assets")
GEN = os.path.join(ASSETS, "gen")
os.makedirs(GEN, exist_ok=True)
TMP_SVG = Path(TOOLS) / ".tmp_icon.svg"   # 固定路径临时文件，用完即删

# kind -> (Phosphor 图标文件名, 衬底色)
KIND_MAP = {
    "app":      ("battery-charging", "#FF6900"),
    "mouse":    ("mouse",            "#FF6900"),
    "keyboard": ("keyboard",         "#0A84FF"),
    "headset":  ("headphones",       "#34C759"),
    "gamepad":  ("game-controller",  "#7B61FF"),
    "device":   ("battery-full",     "#8A8A8F"),
}
THEMES = {
    "light": {"card": "#FFFFFF", "sheet": "#EBEBEE"},
    "dark":  {"card": "#2C2C2E", "sheet": "#1C1C1E"},
}
# 托盘圆形衬底的颜色覆盖：程序主图标和鼠标都是橙色，去掉线稿后会混淆，
# 所以主图标反白（白圆 + 深色数字），在彩色圆里一眼能认出「这是程序本身」。
TRAY_BG = {"app": "#FFFFFF"}


def rgba(hex_color, alpha=255):
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha)


def svg_mask(svg_path, px=768):
    """SVG -> 灰度蒙版（白=图标，黑=背景），用于抗锯齿合成。"""
    txt = Path(svg_path).read_text(encoding="utf-8")
    txt = txt.replace("currentColor", "#000000")
    TMP_SVG.write_text(txt, encoding="utf-8")
    dr = svg2rlg(str(TMP_SVG))
    TMP_SVG.unlink(missing_ok=True)
    scale = px / max(dr.width, dr.height)
    dr.scale(scale, scale)
    dr.width *= scale
    dr.height *= scale
    img = renderPM.drawToPIL(dr, bg=0xFFFFFF).convert("L")
    arr = 255 - np.asarray(img, dtype=np.uint8)
    return Image.fromarray(arr, "L")


def compose(mask, size, bg_hex, fg_hex, corner_hex, layout="ui", alpha=False):
    """4x 超采样合成完整图标。

    layout="ui"  ：圆角方形衬底 + 居中设备图标（桌面卡片 128px）。
    layout="tray"：纯色**圆形**衬底，不画设备线稿 —— 24px 渲染下图形和数字叠在
                   一起两头都看不清，托盘只保留一个大号百分比数字，靠圆色区分设备。
    alpha=True   ：输出 RGBA —— RGB 铺衬底色、形状交给 alpha，
                   这样任意缩放都不会把透明像素的黑色混进边缘（黑边根因）。
    """
    ss = 4
    big = size * ss
    canvas = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)
    if layout == "tray":
        d.ellipse((0, 0, big - 1, big - 1), fill=rgba(bg_hex))
        art = canvas.resize((size, size), Image.LANCZOS)
    else:
        d.rounded_rectangle((1 * ss, 1 * ss, big - 1 * ss, big - 1 * ss),
                            radius=int(big * 0.32), fill=rgba(bg_hex))
        pad = int(big * 0.20)
        icon_size = big - 2 * pad
        m = mask.resize((icon_size, icon_size), Image.LANCZOS)
        layer = Image.new("RGBA", (icon_size, icon_size), rgba(fg_hex))
        layer.putalpha(m)
        canvas.alpha_composite(layer, (pad, pad))
        art = canvas.resize((size, size), Image.LANCZOS)

    if alpha:
        # RGB 取「衬底 + 图标」合成结果（透明区落成衬底色，缩放不留黑边），
        # alpha 取形状本身；两者分离才不会把白色图标也一起抹掉。
        solid = Image.new("RGBA", (size, size), rgba(corner_hex))
        solid.alpha_composite(art)
        solid.putalpha(art.split()[3])
        return solid

    flat = Image.new("RGBA", (size, size), rgba(corner_hex))
    flat.alpha_composite(art)
    return flat.convert("RGB")


def main():
    masks = {}
    for kind, (svg_name, _) in KIND_MAP.items():
        path = os.path.join(ASSETS, "regular", svg_name + ".svg")
        masks[kind] = svg_mask(path)
        print("svg", kind, "<-", svg_name, "mask", masks[kind].size)

    for theme_name, cfg in THEMES.items():
        for kind, (_, color) in KIND_MAP.items():
            # 两套主题都用彩色衬底 + 白色实心图标：小尺寸清晰、深色卡片上也醒目
            bg, fg = color, "#FFFFFF"
            # ui：扁平 RGB（桌面卡片直接贴图，四角填卡片色）
            img = compose(masks[kind], 128, bg, fg, cfg["card"], "ui")
            img.save(os.path.join(GEN, "%s_%s_ui.png" % (theme_name, kind)))
            # tray：RGBA（RGB 铺衬底色、alpha 负责形状），供 32bpp 带 alpha 的图标
            img = compose(masks[kind], 32, TRAY_BG.get(kind, bg), fg, bg,
                          "tray", alpha=True)
            img.save(os.path.join(GEN, "%s_%s_tray.png" % (theme_name, kind)))
        print("theme", theme_name, "done")

    # 预览：2 主题 x 6 图标，8px 间距
    kinds = list(KIND_MAP)
    cell = 128 + 8
    W = 20 + cell * len(kinds)
    H = 20 + (128 + 8) * 2
    sheet = Image.new("RGB", (W, H), (235, 235, 238))
    sd = ImageDraw.Draw(sheet)
    for ti, (theme_name, cfg) in enumerate(THEMES.items()):
        y0 = 10 + ti * (128 + 8) - 4
        sd.rectangle((0, y0, W, y0 + 128 + 8), fill=cfg["sheet"])
        for ki, kind in enumerate(kinds):
            p = os.path.join(GEN, "%s_%s_ui.png" % (theme_name, kind))
            sheet.paste(Image.open(p), (10 + ki * cell, 10 + ti * (128 + 8)))
    preview = os.path.join(TOOLS, "preview-icons-assets.png")
    sheet.save(preview)
    print("preview", preview, sheet.size)
    print("generated", len(os.listdir(GEN)), "png files")


if __name__ == "__main__":
    main()
