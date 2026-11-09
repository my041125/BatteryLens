"""BatteryLens · 原生 Win32 托盘主图标（小米风）

只管理一枚常驻程序主图标（APP_UID=999）：显示最低电量数字，承担
右键菜单入口与低电量气泡通知。逐设备电量由桌面卡片 / 任务栏贴片展示。
被 widget.py import，不自带消息循环。
"""
import ctypes
import os
import sys
from ctypes import wintypes

import win32con
import win32gui
from gdiutil import NULL_PEN, cref, fill
from gdiutil import gdi32 as _G
from gdiutil import user32 as _U

# 打包成 exe 后资源在 PyInstaller 解包目录里
_BASE = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
ASSET_GEN = os.path.join(_BASE, "assets", "gen")

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
NIIF_INFO = 0x00000001
WM_TRAY = win32con.WM_APP + 2
APP_UID = 999            # 程序主图标，固定不回收
ICON_SIZE = 32
C_WHITE = 0xFFFFFF
C_BLACK = 0x000000


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HANDLE),
        ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
        ("uTimeout", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD), ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HANDLE),
    ]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HANDLE),
                ("hbmColor", wintypes.HANDLE)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


# DLL 绑定：gdi32 / user32 在 gdiutil.py 统一声明，这里只补托盘特有的
# 位图与 Shell 通知接口。
_S = ctypes.WinDLL("shell32", use_last_error=True)
_S.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
_S.Shell_NotifyIconW.restype = wintypes.BOOL
_U.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
_U.CreateIconIndirect.restype = wintypes.HANDLE
_U.DestroyIcon.argtypes = [wintypes.HANDLE]
_U.DestroyIcon.restype = wintypes.BOOL
_G.CreateBitmap.argtypes = [ctypes.c_int] * 4 + [ctypes.c_void_p]
_G.CreateBitmap.restype = wintypes.HANDLE
_G.CreateDIBSection.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                                ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                ctypes.c_uint]
_G.CreateDIBSection.restype = ctypes.c_void_p

_THEME = "light"
_APP_ICON = None     # 主图标 HICON


def set_theme(name):
    """切换托盘图标主题（light/dark），后续生成的图标使用新配色。"""
    global _THEME
    _THEME = name if name in ("light", "dark") else "light"


_NUM_SRC = 4            # 数字离屏 4x 超采样，缩回后边缘才干净
_FONTS = {}
_FONT_FILES = ("C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/msyhbd.ttc",
               "C:/Windows/Fonts/arialbd.ttf")


def _num_font(px):
    """按像素高度缓存的粗体数字字体；GDI 文本在 DDB 上会留黑边，故用 PIL 渲染。"""
    px = max(6, int(px))
    if px not in _FONTS:
        from PIL import ImageFont
        font = None
        for path in _FONT_FILES:
            try:
                font = ImageFont.truetype(path, px)
                break
            except Exception:
                continue
        _FONTS[px] = font or ImageFont.load_default()
    return _FONTS[px]


_BASE_IMG = {}


def _base_img(path):
    """缓存素材 PNG（RGBA），避免每次刷新都解码。"""
    if path not in _BASE_IMG:
        from PIL import Image
        _BASE_IMG[path] = Image.open(path).convert("RGBA")
    return _BASE_IMG[path]


_COMPOSED = {}


def _tray_img(path, size, level):
    """白底主图标 + 深色百分比数字，合成为最终 RGBA 托盘图。"""
    key = (path, size, level)
    img = _COMPOSED.get(key)
    if img is not None:
        return img
    from PIL import Image, ImageDraw
    img = _base_img(path)
    if img.size != (size, size):
        img = img.resize((size, size), Image.LANCZOS)
    if level is not None:
        big = size * _NUM_SRC
        canvas = img.resize((big, big), Image.LANCZOS)
        d = ImageDraw.Draw(canvas, "RGBA")
        txt = "%d" % int(level)
        px = big * 0.62
        while px > big * 0.30:
            f = _num_font(px)
            l, t, r, b = d.textbbox((0, 0), txt, font=f)
            if (r - l) <= big * 0.80:
                break
            px *= 0.92
        x = (big - (r - l)) / 2.0 - l
        y = (big - (b - t)) / 2.0 - t
        d.text((x, y), txt, font=f, fill=(26, 26, 26, 255))
        img = canvas.resize((size, size), Image.LANCZOS)
    if len(_COMPOSED) > 64:
        _COMPOSED.clear()
    _COMPOSED[key] = img
    return img


def _rgba_bitmap(dc, img):
    """把 RGBA 图写成 32bpp DIB section（带 alpha），系统据此做圆角抗锯齿。"""
    size = img.width
    bi = BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = size
    bi.bmiHeader.biHeight = -size
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    bi.bmiHeader.biCompression = 0            # BI_RGB
    bits = ctypes.c_void_p()
    hbmp = _G.CreateDIBSection(ctypes.c_void_p(int(dc)), ctypes.byref(bi), 0,
                               ctypes.byref(bits), None, 0)
    if not hbmp or not bits:
        return None
    data = img.convert("RGBA").tobytes("raw", "BGRA")
    ctypes.memmove(bits, data, len(data))
    return hbmp


def make_app_icon(level, size=ICON_SIZE):
    """主图标：白圆衬底（反白区分于彩色设备图标）+ 深色最低电量数字。"""
    screen = win32gui.GetDC(0)
    dc = win32gui.CreateCompatibleDC(screen)
    mbmp = None
    hbmp = None
    try:
        path = os.path.join(ASSET_GEN, "%s_app_tray.png" % _THEME)
        hbmp = _rgba_bitmap(dc, _tray_img(path, size, level))
        if not hbmp:
            return None

        # 单色掩码：1px 内缩圆角，兜底用（alpha 不被支持时仍能透明）
        mdc = win32gui.CreateCompatibleDC(screen)
        mbmp = _G.CreateBitmap(size, size, 1, 1, None)
        oldm = win32gui.SelectObject(mdc, mbmp)
        fill(mdc, 0, 0, size, size, C_WHITE)
        bk = win32gui.CreateSolidBrush(C_BLACK)
        ob = win32gui.SelectObject(mdc, bk)
        op = win32gui.SelectObject(mdc, _G.GetStockObject(NULL_PEN))
        u = size / 32.0
        _G.RoundRect(ctypes.c_void_p(int(mdc)), int(u), int(u),
                     int(31 * u), int(31 * u), int(9 * u), int(9 * u))
        win32gui.SelectObject(mdc, op)
        win32gui.SelectObject(mdc, ob)
        win32gui.DeleteObject(bk)
        win32gui.SelectObject(mdc, oldm)
        win32gui.DeleteDC(mdc)

        ii = ICONINFO()
        ii.fIcon = 1
        # pywin32 返回 PyHANDLE，ctypes 的 c_void_p 字段只接受整数句柄。
        ii.hbmMask = int(mbmp)
        ii.hbmColor = int(hbmp)
        return _U.CreateIconIndirect(ctypes.byref(ii))
    finally:
        if hbmp:
            win32gui.DeleteObject(hbmp)
        if mbmp:
            win32gui.DeleteObject(mbmp)
        win32gui.DeleteDC(dc)
        win32gui.ReleaseDC(0, screen)


def _notify(hwnd, msg, hicon=None, tip=""):
    """Shell_NotifyIconW 薄封装：增/改/删托盘图标。"""
    n = NOTIFYICONDATAW()
    n.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    n.hWnd = int(hwnd)
    n.uID = APP_UID
    n.uFlags = NIF_MESSAGE | NIF_TIP | (NIF_ICON if hicon else 0)
    n.uCallbackMessage = WM_TRAY
    if hicon:
        n.hIcon = hicon
    n.szTip = (tip or "")[:127]
    return bool(_S.Shell_NotifyIconW(msg, ctypes.byref(n)))


def add_app(hwnd, level=None, count=0, tip=""):
    """新增或更新常驻主图标；任何显示模式都会调用。"""
    global _APP_ICON
    hicon = make_app_icon(level)
    label = tip or ("电量透视 · %d 台设备" % count)
    if _APP_ICON is None:
        ok = _notify(hwnd, NIM_ADD, hicon, label)
    else:
        ok = _notify(hwnd, NIM_MODIFY, hicon, label)
    if ok:
        if _APP_ICON is not None:
            _U.DestroyIcon(ctypes.c_void_p(_APP_ICON))
        _APP_ICON = hicon
    else:
        _U.DestroyIcon(ctypes.c_void_p(hicon))
    return ok


def remove_app(hwnd):
    """移除主图标（仅退出时调用）。"""
    global _APP_ICON
    _notify(hwnd, NIM_DELETE)
    if _APP_ICON is not None:
        _U.DestroyIcon(ctypes.c_void_p(_APP_ICON))
        _APP_ICON = None


def notify_app(hwnd, title, text):
    """主图标气泡通知（低电量提醒等），不改变图标本身。"""
    if not hwnd:
        return False
    n = NOTIFYICONDATAW()
    n.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    n.hWnd = int(hwnd)
    n.uID = APP_UID
    n.uFlags = NIF_INFO
    n.szInfo = (text or "")[:255]
    n.szInfoTitle = (title or "")[:63]
    n.dwInfoFlags = NIIF_INFO
    return bool(_S.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(n)))
