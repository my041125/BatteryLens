"""BatteryLens 共用 GDI 薄封装：widget.py / tray_win.py 的公共底层。

只放真正共享的部分：COLORREF 颜色转换、常用 gdi32/user32 绑定、
纯色图元填充。调用方用 `from gdiutil import gdi32 as _GDI` 复用同一
DLL 实例，argtypes 绑定全项目只做一次。
"""
import ctypes

import win32gui

gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)

NULL_BRUSH = 5
NULL_PEN = 8
# GDI 弧线默认按逆时针画（视觉方向），电量环这类进度图形必须顺时针
AD_CLOCKWISE = 2
PS_GEOMETRIC = 0x00010000
PS_ENDCAP_ROUND = 0x00000000

gdi32.CreateFontW.argtypes = [ctypes.c_int] * 5 + [ctypes.c_uint] * 8 + [ctypes.c_wchar_p]
gdi32.CreateFontW.restype = ctypes.c_void_p
gdi32.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint]
gdi32.CreatePen.restype = ctypes.c_void_p
gdi32.SetMapMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
gdi32.SetWindowExtEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
gdi32.SetViewportExtEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
gdi32.RoundRect.argtypes = [ctypes.c_void_p] + [ctypes.c_int] * 6
gdi32.Ellipse.argtypes = [ctypes.c_void_p] + [ctypes.c_int] * 4
gdi32.Arc.argtypes = [ctypes.c_void_p] + [ctypes.c_int] * 8
gdi32.SetArcDirection.argtypes = [ctypes.c_void_p, ctypes.c_int]
gdi32.SetArcDirection.restype = ctypes.c_int
for _fn in (gdi32.RoundRect, gdi32.Ellipse, gdi32.Arc):
    _fn.restype = ctypes.c_int
gdi32.CreateEllipticRgn.argtypes = [ctypes.c_int] * 4
gdi32.CreateEllipticRgn.restype = ctypes.c_void_p
gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
gdi32.CreateRoundRectRgn.restype = ctypes.c_void_p
gdi32.GetStockObject.argtypes = [ctypes.c_int]
gdi32.GetStockObject.restype = ctypes.c_void_p


def cref(s):
    """#RRGGBB -> COLORREF（0x00BBGGRR）。"""
    h = s.lstrip("#")
    return int(h[0:2], 16) | (int(h[2:4], 16) << 8) | (int(h[4:6], 16) << 16)


def fill(dc, x1, y1, x2, y2, color):
    """纯色矩形填充。"""
    br = win32gui.CreateSolidBrush(color)
    win32gui.FillRect(dc, (x1, y1, x2, y2), br)
    win32gui.DeleteObject(br)


def frame(dc, x1, y1, x2, y2, color):
    """纯色矩形描边。"""
    br = win32gui.CreateSolidBrush(color)
    win32gui.FrameRect(dc, (x1, y1, x2, y2), br)
    win32gui.DeleteObject(br)


def shape(hdc, fn, args, color):
    """纯色 brush + NULL_PEN 填一个 GDI 图元，画完恢复 DC 原对象。"""
    br = win32gui.CreateSolidBrush(color)
    ob = win32gui.SelectObject(hdc, br)
    op = win32gui.SelectObject(hdc, gdi32.GetStockObject(NULL_PEN))
    fn(ctypes.c_void_p(int(hdc)), *args)
    win32gui.SelectObject(hdc, op)
    win32gui.SelectObject(hdc, ob)
    win32gui.DeleteObject(br)


def round_fill(hdc, x1, y1, x2, y2, r, color):
    """纯色圆角矩形填充（NULL_PEN 消除描边毛边）。"""
    shape(hdc, gdi32.RoundRect, (x1, y1, x2, y2, r, r), color)


# ---- 进度圆环（widget 折叠态 / 任务栏贴片共用） --------------------------

def ring_pen(width, color):
    """几何圆头笔：普通笔不支持宽度>1 的平滑圆头弧。"""
    return gdi32.CreatePen(PS_GEOMETRIC | PS_ENDCAP_ROUND, int(width), color)


def ring_arc(hdc, cx, cy, r, width, color, start_deg, sweep_deg):
    """画一段圆环：start_deg 从 12 点方向顺时针算，sweep_deg 为扫过角度。"""
    import math

    pen = ring_pen(width, color)
    if not pen:
        return
    old = win32gui.SelectObject(hdc, pen)
    try:
        h = ctypes.c_void_p(int(hdc))
        gdi32.SetArcDirection(h, AD_CLOCKWISE)
        a0 = math.radians(start_deg - 90.0)
        a1 = math.radians(start_deg + sweep_deg - 90.0)
        x0 = int(round(cx + r * math.cos(a0)))
        y0 = int(round(cy + r * math.sin(a0)))
        x1 = int(round(cx + r * math.cos(a1)))
        y1 = int(round(cy + r * math.sin(a1)))
        gdi32.Arc(h, int(cx - r), int(cy - r), int(cx + r), int(cy + r),
                  x0, y0, x1, y1)
    finally:
        win32gui.SelectObject(hdc, old)
        win32gui.DeleteObject(pen)


def ring_track(hdc, cx, cy, r, width, color):
    """整圈轨道：空心圆，只留描边。"""
    pen = ring_pen(width, color)
    if not pen:
        return
    old_pen = win32gui.SelectObject(hdc, pen)
    old_br = win32gui.SelectObject(hdc, gdi32.GetStockObject(NULL_BRUSH))
    try:
        gdi32.Ellipse(ctypes.c_void_p(int(hdc)),
                      int(cx - r), int(cy - r), int(cx + r), int(cy + r))
    finally:
        win32gui.SelectObject(hdc, old_br)
        win32gui.SelectObject(hdc, old_pen)
        win32gui.DeleteObject(pen)
