"""渲染冒烟 + 设置兼容 + 贴片/主图标离屏渲染（无窗口）。"""
import ctypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import win32con
import win32gui

import collector
import taskbar
import tray_win
import widget

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL "), name)


# 1) 旧 settings.json 兼容读（含已废弃的 tray_devices 字段）
cfg = widget.load_settings()
check("settings 兼容读(忽略 tray_devices)",
      set(cfg) == {"mode", "topmost", "theme", "primary", "aliases", "low_at"})

# 2) 别名显示
widget.STATE["aliases"] = {"bt:X": "耳机"}
check("display_name 别名", widget.display_name(
    {"id": "bt:X", "name": "EDIFIER"}) == "耳机")

# 3) 低电量提醒逻辑
widget.STATE["low_at"] = 20
widget.STATE["notified"] = {}
widget.STATE["data"] = {"devices": [
    {"id": "a", "name": "低电设备", "battery": 15, "status": "real"},
    {"id": "b", "name": "正常设备", "battery": 80, "status": "real"},
    {"id": "c", "name": "缓存设备", "battery": 5, "status": "cached"}]}
widget.check_low_battery(None)
check("低电量提醒(仅real)", widget.STATE["notified"] == {"a": 15})
widget.check_low_battery(None)
check("不重复提醒", widget.STATE["notified"] == {"a": 15})
widget.STATE["data"]["devices"][0]["battery"] = 50
widget.check_low_battery(None)
widget.STATE["data"]["devices"][0]["battery"] = 12
widget.check_low_battery(None)
check("回升重布防", widget.STATE["notified"] == {"a": 12})

# 4) 离屏渲染：折叠胶囊 / 展开列表（内存 DC，不上屏）
data = collector.scan_light()
widget.STATE["data"] = data
screen = win32gui.GetDC(0)
mem = win32gui.CreateCompatibleDC(screen)
bmp = win32gui.CreateCompatibleBitmap(screen, 640, 420)
old = win32gui.SelectObject(mem, bmp)
try:
    widget.apply_palette()
    widget.setup_scale(mem)
    widget.fill(mem, 0, 0, 640, 420, widget.C_CARD)
    widget.paint_collapsed(mem, widget.PILL_W, widget.PILL_H, data)
    w, h = widget.expanded_size(data)
    widget.paint_expanded(mem, w, h, data)
    c = win32gui.GetPixel(mem, 28, 28)       # 胶囊卡设备图标的彩色衬底
    check("胶囊卡渲染", c != widget.C_CARD)
    check("展开列表渲染", win32gui.GetPixel(mem, 200, 20) != 0)
finally:
    win32gui.SelectObject(mem, old)
    win32gui.DeleteObject(bmp)
    win32gui.DeleteDC(mem)
    win32gui.ReleaseDC(0, screen)

# 5) 贴片离屏渲染（全部真实读数设备在列）
taskbar.STRIP["bg"] = taskbar.cref("#202020")
taskbar.STRIP["devices"] = [dict(d) for d in data["devices"]]
screen = win32gui.GetDC(0)
mem2 = win32gui.CreateCompatibleDC(screen)
bmp2 = win32gui.CreateCompatibleBitmap(screen, 400, 72)
old2 = win32gui.SelectObject(mem2, bmp2)
try:
    taskbar._paint(mem2, 400, 72)
    colors = set()
    for x in range(0, 400, 4):
        for y in range(4, 72, 4):
            colors.add(win32gui.GetPixel(mem2, x, y))
    check("贴片多设备渲染(出现多种颜色)", len(colors) > 4)
finally:
    win32gui.SelectObject(mem2, old2)
    win32gui.DeleteObject(bmp2)
    win32gui.DeleteDC(mem2)
    win32gui.ReleaseDC(0, screen)

# 6) 主图标离屏生成（只建 HICON 不入托盘）
for th in ("light", "dark"):
    tray_win.set_theme(th)
    hicon = tray_win.make_app_icon(80)
    check("主图标生成 %s" % th, bool(hicon))
    if hicon:
        tray_win._U.DestroyIcon(ctypes.c_void_p(hicon))

# 7) 静默采集（无 UI）：结构检查，设备数量因机器而异
ids = [d["id"] for d in data["devices"]]
check("采集返回设备列表", isinstance(ids, list) and len(ids) >= 1)
print("  本次读数:", [(d["kind_cn"], d["battery"])
                       for d in data["devices"] if d["battery"] is not None])

print("\nRESULT:", "ALL PASS" if ok else "HAS FAILURES")
sys.exit(0 if ok else 1)
