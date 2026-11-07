"""BatteryScope · 任务栏贴片：SetParent 嵌入 Shell_TrayWnd。

参考 TrafficMonitor 的嵌入思路：把自绘窗口挂成任务栏子窗口，贴在
TrayNotifyWnd（通知区）左侧，逐设备画迷你电量环 + 百分比。
背景色从任务栏实际像素采样，文字取对比色，自动适配任意任务栏主题。
只负责窗口与绘制；数据由 widget.py 调 update() 推送。
"""
import ctypes

import win32api
import win32con
import win32gui

from gdiutil import cref, fill, gdi32, ring_arc, ring_track, round_fill

STRIP_CLASS = "BatteryScopeStrip"
PER_DEV = 46          # 每台设备占宽（逻辑 px）
GAP = 8               # 与通知区的间距（逻辑 px）
# 设备彩色圆环衬底（与卡片 KIND_BG 一致）
KIND_COLORS = {
    "mouse": "#FF6900", "keyboard": "#0A84FF", "headset": "#34C759",
    "gamepad": "#7B61FF", "device": "#8A8A8F", "app": "#FF6900",
}
STRIP = {"hwnd": None, "devices": [], "bg": 0, "dark": False}
_FONTS = {}


def _scale():
    """系统 DPI 缩放系数（96dpi = 1.0）。"""
    try:
        return max(1.0, ctypes.windll.user32.GetDpiForSystem() / 96.0)
    except Exception:
        return 1.0


def _font(size, bold):
    """按 (像素高, 加粗) 缓存的 Segoe UI 字体句柄。"""
    key = (size, bold)
    if key not in _FONTS:
        _FONTS[key] = gdi32.CreateFontW(
            -size, 0, 0, 0, 700 if bold else 400, 0, 0, 0,
            win32con.DEFAULT_CHARSET, win32con.OUT_DEFAULT_PRECIS,
            win32con.CLIP_DEFAULT_PRECIS, win32con.CLEARTYPE_QUALITY,
            win32con.DEFAULT_PITCH | win32con.FF_SWISS, "Segoe UI")
    return _FONTS[key]


def _text(hdc, s, rect, size, color, bold=True):
    """单行居中文本绘制（透明底）。"""
    old = win32gui.SelectObject(hdc, _font(size, bold))
    win32gui.SetBkMode(hdc, win32con.TRANSPARENT)
    win32gui.SetTextColor(hdc, color)
    win32gui.DrawText(hdc, s, len(s), rect,
                      win32con.DT_SINGLELINE | win32con.DT_VCENTER
                      | win32con.DT_CENTER | win32con.DT_NOPREFIX)
    win32gui.SelectObject(hdc, old)


def _taskbar():
    """取主任务栏窗口句柄 Shell_TrayWnd。"""
    try:
        return win32gui.FindWindow("Shell_TrayWnd", None)
    except Exception:
        return 0


def _sample_bg(taskbar):
    """任务栏背景色采样：颜色和明度决定文字 / 轨道用深还是浅。"""
    try:
        l, t, _, b = win32gui.GetWindowRect(taskbar)
        dc = win32gui.GetDC(taskbar)
        c = win32gui.GetPixel(dc, 8, max(1, (b - t) // 2))
        win32gui.ReleaseDC(taskbar, dc)
        return c
    except Exception:
        return cref("#202020")


def _size():
    """按设备数计算贴片宽度（逻辑 px）。"""
    s = _scale()
    n = max(1, len(STRIP["devices"]))
    return int((n * PER_DEV + GAP) * s), 0, s


def _place():
    """贴到 TrayNotifyWnd 左侧，高度铺满任务栏客户区。"""
    tb = _taskbar()
    if not tb or not STRIP["hwnd"]:
        return False
    s = _scale()
    w, _, _ = _size()
    w = max(w, int(PER_DEV * s))
    cl, ct, cr, cb = win32gui.GetClientRect(tb)
    x = cr - w - int(2 * s)
    try:
        tn = win32gui.FindWindowEx(tb, 0, "TrayNotifyWnd", None)
        if tn:
            tl, tt, trr, tbm = win32gui.GetWindowRect(tn)
            pt = win32gui.ScreenToClient(tb, (tl, tt))
            x = pt[0] - w - int(GAP * s)
    except Exception:
        pass
    h = cb - ct
    win32gui.SetWindowPos(STRIP["hwnd"], 0, x, ct, w, h,
                          win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW)
    return True


def _paint(hdc, w, h):
    """贴片绘制：任务栏底色上逐台设备画彩色药丸 + 圆环 + 白色数字。"""
    s = _scale()
    fill(hdc, 0, 0, w, h, STRIP["bg"])
    n = len(STRIP["devices"])
    for i, d in enumerate(STRIP["devices"]):
        cell_w = w / max(1, n)
        cx = int(cell_w * i + cell_w / 2)
        cy = int(h / 2)
        r = int(15 * s)
        col = cref(KIND_COLORS.get(d.get("kind"), KIND_COLORS["device"]))
        # 彩色衬底 + 深一档轨道 + 白色进度弧和数字，深浅任务栏上都清晰
        round_fill(hdc, cx - r - int(2 * s), cy - r - int(2 * s),
                   cx + r + int(2 * s), cy + r + int(2 * s),
                   int((r + 2 * s) * 0.45), col)
        ring_track(hdc, cx, cy, r, int(3 * s), _shade(col, 0.62))
        v = d.get("battery")
        if v is not None:
            ring_arc(hdc, cx, cy, r, int(3 * s), 0xFFFFFF, 0, max(1.0, 3.6 * v))
            _text(hdc, "%d" % v,
                  (cx - r, cy - int(8 * s), cx + r, cy + int(8 * s)),
                  int(10 * s), 0xFFFFFF)
        else:
            _text(hdc, "--", (cx - r, cy - int(8 * s), cx + r, cy + int(8 * s)),
                  int(10 * s), _shade(col, 0.45))


def _shade(color, f):
    """颜色按系数变暗（轨道 / 占位符用）。"""
    r = int((color & 0xFF) * f)
    g = int(((color >> 8) & 0xFF) * f)
    b = int(((color >> 16) & 0xFF) * f)
    return r | (g << 8) | (b << 16)


def _strip_proc(hwnd, msg, wp, lp):
    """贴片窗口过程：只处理绘制，其余交系统默认。"""
    if msg == win32con.WM_PAINT:
        hdc, ps = win32gui.BeginPaint(hwnd)
        try:
            w, h = win32gui.GetClientRect(hwnd)[2:]
            _paint(hdc, w, h)
        finally:
            win32gui.EndPaint(hwnd, ps)
        return 0
    return win32gui.DefWindowProc(hwnd, msg, wp, lp)


def alive():
    """贴片窗口是否还存在（Explorer 重启后为 False，触发重建）。"""
    return bool(STRIP["hwnd"] and win32gui.IsWindow(STRIP["hwnd"]))


def ensure(owner):
    """创建或重新挂载贴片；Explorer 重启后旧窗口失效会自动重建。"""
    tb = _taskbar()
    if not tb:
        return False
    if STRIP["hwnd"] and win32gui.IsWindow(STRIP["hwnd"]):
        return _place()
    hinst = win32api.GetModuleHandle(None)
    try:
        wc = win32gui.WNDCLASS()
        wc.lpfnWndProc = _strip_proc
        wc.hInstance = hinst
        wc.lpszClassName = STRIP_CLASS
        win32gui.RegisterClass(wc)
    except Exception:
        pass          # 已注册
    s = _scale()
    hwnd = win32gui.CreateWindowEx(
        win32con.WS_EX_NOACTIVATE | win32con.WS_EX_TOOLWINDOW,
        STRIP_CLASS, "BatteryScopeStrip", win32con.WS_POPUP,
        0, 0, int(PER_DEV * s), int(40 * s), 0, 0, hinst, None)
    if not hwnd:
        return False
    STRIP["hwnd"] = hwnd
    win32gui.SetParent(hwnd, tb)          # 挂成任务栏子窗口
    return _place()


def update(devices, dark=False):
    """推送设备列表与主题明暗并重绘；贴片不存在时返回 False。"""
    STRIP["devices"] = list(devices or [])
    STRIP["dark"] = bool(dark)
    if not STRIP["hwnd"] or not win32gui.IsWindow(STRIP["hwnd"]):
        return False
    _place()
    win32gui.InvalidateRect(STRIP["hwnd"], None, True)
    return True


def refresh():
    """任务栏外观可能变化（DPI / 重启）：重采样背景色并重排。"""
    if not STRIP["hwnd"] or not win32gui.IsWindow(STRIP["hwnd"]):
        return
    tb = _taskbar()
    if tb:
        STRIP["bg"] = _sample_bg(tb)
    _place()
    win32gui.InvalidateRect(STRIP["hwnd"], None, True)


def destroy():
    """销毁贴片窗口（切换到卡片模式 / 退出时调用）。"""
    h = STRIP["hwnd"]
    STRIP["hwnd"] = None
    STRIP["devices"] = []
    if h and win32gui.IsWindow(h):
        try:
            win32gui.DestroyWindow(h)
        except Exception:
            pass
