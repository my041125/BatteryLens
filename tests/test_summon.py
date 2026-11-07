"""兜底召唤验证：贴片模式/忙碌/最小化三种状态下，托盘左键必出卡（无窗口）。"""
import sys

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import win32api
import win32con
import win32gui

import widget

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL "), name)


widget.tray_win.add_app = lambda *a, **k: True          # 不占托盘
widget.save_settings = lambda: None                      # 测试不写真实设置

wc = win32gui.WNDCLASS()
wc.lpfnWndProc = win32gui.DefWindowProc
wc.hInstance = win32api.GetModuleHandle(None)
wc.lpszClassName = "BsSummonProbe"
win32gui.RegisterClass(wc)
hwnd = win32gui.CreateWindowEx(0, "BsSummonProbe", "p",
                               win32con.WS_POPUP, 0, 0, widget.PILL_W,
                               widget.PILL_H, 0, 0, wc.hInstance, None)
widget.HWND = hwnd
sw = __import__("win32api").GetSystemMetrics(win32con.SM_CXSCREEN)

# 场景 1：贴片模式 + 全屏忙碌 + 卡片隐藏 → 召唤必现且切回卡片模式
widget.STATE["mode"] = "taskbar"
widget.STATE["busy"] = True
widget.STATE["forced_show"] = False
widget.user_busy = lambda: True
win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
widget.summon_card(hwnd)
check("贴片+忙碌+隐藏 → 召唤后可见且切回卡片",
      win32gui.IsWindowVisible(hwnd) and widget.STATE["mode"] == "widget"
      and widget.STATE["forced_show"])

# 场景 2：最小化（Win+D 常规路径）→ 召唤后还原
win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
check("最小化复现", win32gui.IsIconic(hwnd))
widget.summon_card(hwnd)
check("最小化 → 召唤后还原", win32gui.IsWindowVisible(hwnd)
      and not win32gui.IsIconic(hwnd))

# 场景 3：召唤后位置强制归位（右下角标准位置，S=1.0 时右缘 = 屏宽-18）
rect = win32gui.GetWindowRect(hwnd)
check("位置归位右下角", rect[2] == sw - 18)

# 场景 4：忙碌中再次 on_watch → 保持可见（意图持续）
widget.user_busy = lambda: True
widget.STATE["busy"] = False
widget.on_watch(hwnd)          # 过渡到忙碌
check("召唤后忙碌过渡 → 保持可见（forced_show）",
      win32gui.IsWindowVisible(hwnd) and widget.STATE["forced_show"])

win32gui.DestroyWindow(hwnd)
print("\nRESULT:", "ALL PASS" if ok else "HAS FAILURES")
sys.exit(0 if ok else 1)
