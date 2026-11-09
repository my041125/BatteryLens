"""一键打包：python tools\\build_exe.py

产出 dist/BatteryLens.exe（单文件、无控制台）。双击即用，无需 Python 环境。
运行时源码在项目根（widget.py 等 5 个模块），素材在 assets/，产物在 dist/。
"""
import os
import subprocess
import sys

TOOLS = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(TOOLS)          # 项目根：tools/ 的上一级


def make_icon():
    """主图标 PNG -> 多尺寸 .ico（生成到 build/，不污染源码区）。"""
    from PIL import Image
    src = os.path.join(PROJECT, "assets", "gen", "light_app_tray.png")
    ico = os.path.join(PROJECT, "build", "app.ico")
    os.makedirs(os.path.dirname(ico), exist_ok=True)
    Image.open(src).convert("RGBA").save(
        ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                    (64, 64), (128, 128), (256, 256)])
    return ico


def main():
    ico = make_icon()
    print("building BatteryLens.exe ...")
    # 参数全部为脚本内常量，无外部输入参与
    subprocess.run(
        [sys.executable, "-m", "PyInstaller",
         "--onefile", "--noconsole",
         "--name", "BatteryLens",
         "--icon", ico,
         "--version-file", os.path.join(TOOLS, "version.txt"),
         "--add-data", os.path.join(PROJECT, "assets") + ";assets",
         "--distpath", os.path.join(PROJECT, "dist"),
         "--workpath", os.path.join(PROJECT, "build"),
         "--specpath", os.path.join(PROJECT, "build"),
         os.path.join(PROJECT, "widget.py")],
        cwd=PROJECT, check=True, shell=False)
    out = os.path.join(PROJECT, "dist", "BatteryLens.exe")
    print("OK ->", out, os.path.getsize(out) // 1024, "KB")


if __name__ == "__main__":
    main()
