"""「显示桌面」韧性验证：SC_MINIMIZE 吞掉、自愈看门狗、忙碌守卫（无窗口）。"""
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


widget.tray_win.add_app = lambda *a, **k: True      # 不占托盘

wc = win32gui.WNDCLASS()
wc.lpfnWndProc = win32gui.DefWindowProc
wc.hInstance = win32api.GetModuleHandle(None)
wc.lpszClassName = "BsShowDesktopProbe"
win32gui.RegisterClass(wc)
hwnd = win32gui.CreateWindowEx(0, "BsShowDesktopProbe", "p",
                               win32con.WS_POPUP, 100, 100, widget.PILL_W,
                               widget.PILL_H, 0, 0, wc.hInstance, None)
widget.HWND = hwnd
widget.STATE["mode"] = "widget"
widget.STATE["busy"] = False
widget.STATE["forced_show"] = False
win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)

# 1) 自愈：被 SW_HIDE 藏掉
win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
widget.ensure_card_visible(hwnd)
check("隐藏后自愈", win32gui.IsWindowVisible(hwnd)
      and not win32gui.IsIconic(hwnd))

# 2) 自愈：被最小化（Win+D 的常规路径）
win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
check("最小化状态复现", win32gui.IsIconic(hwnd))
widget.ensure_card_visible(hwnd)
check("最小化后自愈", win32gui.IsWindowVisible(hwnd)
      and not win32gui.IsIconic(hwnd))

# 3) 看门狗走 on_watch：非忙碌 + 卡片被藏 → 自动恢复
win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
widget.on_watch(hwnd)
check("on_watch 看门狗自愈", win32gui.IsWindowVisible(hwnd))

# 4) 忙碌守卫：忙碌 + 非强制 → 卡片保持隐藏（不被自愈错误救回）
widget.user_busy = lambda: True
widget.STATE["busy"] = False          # 预设上一轮不忙，触发过渡
win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
widget.on_watch(hwnd)                 # 过渡到忙碌 → 自身隐藏
check("忙碌自动隐藏仍生效", not win32gui.IsWindowVisible(hwnd))
widget.on_watch(hwnd)                 # 忙碌中持续 tick → 不得自愈
check("忙碌中不自愈", not win32gui.IsWindowVisible(hwnd))

# 5) 忙碌 + 用户强制显示 → 自愈生效
widget.STATE["forced_show"] = True
win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
widget.on_watch(hwnd)
check("忙碌 + 强制显示 → 保持可见", win32gui.IsWindowVisible(hwnd))
widget.user_busy = lambda: False
widget.STATE["busy"] = True           # 预设忙碌，触发解除过渡
widget.on_watch(hwnd)
check("忙碌解除 → 恢复显示", win32gui.IsWindowVisible(hwnd)
      and not widget.STATE["forced_show"])

win32gui.DestroyWindow(hwnd)
print("\nRESULT:", "ALL PASS" if ok else "HAS FAILURES")
sys.exit(0 if ok else 1)
