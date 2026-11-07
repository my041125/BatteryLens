"""显隐状态机验证：auto 主题素材路径 + 忙碌/强制显示/贴片切换（无窗口）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import win32con
import win32gui

import widget

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL "), name)


# 打桩：不真往托盘加图标
widget.tray_win.add_app = lambda *a, **k: True

# 1) auto 主题素材路径：effective_theme 解析后文件必须存在
widget.STATE["theme"] = "auto"
et = widget.effective_theme()
p = os.path.join(widget.ASSET_ROOT, "assets", "gen",
                 "%s_headset_ui.png" % et)
check("auto 主题解析到存在的素材 (%s)" % et, os.path.exists(p))
screen = win32gui.GetDC(0)
mem = win32gui.CreateCompatibleDC(screen)
bmp = win32gui.CreateCompatibleBitmap(screen, 320, 348)
old = win32gui.SelectObject(mem, bmp)
widget.S = 1.0
widget.apply_palette()
data = {"scan_time": "20:00:00", "devices": [
    {"id": "bt:ed", "name": "耳机", "kind": "headset", "kind_cn": "耳机",
     "link": "经典蓝牙", "battery": 80, "status": "real"}]}
widget.STATE["data"] = data
before = os.path.getsize(widget.LOG) if os.path.exists(widget.LOG) else 0
widget.paint_expanded(mem, 320, 348, data)
after = os.path.getsize(widget.LOG) if os.path.exists(widget.LOG) else 0
check("auto 主题绘制不报错", after == before)
win32gui.SelectObject(mem, old)
win32gui.DeleteObject(bmp)
win32gui.DeleteDC(mem)
win32gui.ReleaseDC(0, screen)

# 2) 显隐状态机：探针窗口模拟卡片窗口
wc = win32gui.WNDCLASS()
wc.lpfnWndProc = win32gui.DefWindowProc
wc.hInstance = __import__("win32api").GetModuleHandle(None)
wc.lpszClassName = "BsToggleProbe"
win32gui.RegisterClass(wc)
hwnd = win32gui.CreateWindowEx(0, "BsToggleProbe", "p", win32con.WS_POPUP,
                               0, 0, widget.PILL_W, widget.PILL_H,
                               0, 0, wc.hInstance, None)
widget.HWND = hwnd
widget.STATE["mode"] = "widget"
widget.STATE["busy"] = True
widget.STATE["forced_show"] = False
widget.apply_mode(hwnd)                      # 忙碌 + 非主动 → 隐藏
check("忙碌时非主动切换 → 卡片隐藏", not win32gui.IsWindowVisible(hwnd))
widget.apply_mode(hwnd, explicit=True)       # 忙碌 + 菜单主动 → 显示
check("忙碌时菜单主动切换 → 卡片立即显示", win32gui.IsWindowVisible(hwnd))
check("forced_show 已置位", widget.STATE["forced_show"])
# 忙碌解除：预设「上一轮是忙碌」，on_watch 实测不忙 → 触发过渡分支
widget.STATE["busy"] = True
widget.on_watch(hwnd)
check("忙碌解除 → 保持显示且清除标记", win32gui.IsWindowVisible(hwnd)
      and not widget.STATE["forced_show"])
# 再次进入全屏：打桩 user_busy 返回 True
orig_busy = widget.user_busy
widget.STATE["busy"] = False
widget.user_busy = lambda: True
widget.on_watch(hwnd)
widget.user_busy = orig_busy
check("再次忙碌 → 恢复自动隐藏", not win32gui.IsWindowVisible(hwnd))
win32gui.DestroyWindow(hwnd)

print("\nRESULT:", "ALL PASS" if ok else "HAS FAILURES")
sys.exit(0 if ok else 1)
