"""电量透视 BatteryLens · 桌面悬浮卡片 / 任务栏贴片（小米风，原生 Win32）

两种显示形态可在右键菜单里随时切换，选择会记住：
  · 桌面卡片（默认）：右下角胶囊卡（图标 + 名称/续航 + 电量字 + 进度条），
    每 4 秒轮换一台设备，鼠标移上去展开全量列表。
  · 任务栏贴片：彩色圆点电量直接嵌进任务栏，逐台设备一眼可读。
另有一枚常驻托盘主图标（最低电量 + 菜单入口 + 低电量气泡）。
浅色 / 深色 / 跟随系统三态主题，切换并持久化。
低功耗：无高频轮询、全屏/演示模式暂停采集与轮换、进程低于正常优先级。
"""
import ctypes
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import threading
import time
import traceback

import win32api
import win32con
import win32gui

import collector
import taskbar
import tray_win
from gdiutil import cref, fill, frame, round_fill
from gdiutil import gdi32 as _GDI
from gdiutil import user32 as _USER32

# 路径：打包成 exe 后，可写文件（设置/日志/历史）放 exe 目录，
# 只读资源（assets 图标）在 PyInstaller 解包目录 _MEIPASS 里。
if getattr(sys, "frozen", False):
    ROOT = os.path.dirname(sys.executable)
    ASSET_ROOT = getattr(sys, "_MEIPASS", ROOT)
else:
    ROOT = os.path.dirname(os.path.abspath(__file__))
    ASSET_ROOT = ROOT
STARTUP = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                       "Start Menu", "Programs", "Startup")
LNK = os.path.join(STARTUP, "电量透视.lnk")
SETTINGS = os.path.join(ROOT, "settings.json")
LOG = os.path.join(ROOT, "error.log")
LOG_MAX = 256 * 1024              # error.log 上限；超过则只保留末尾 64KB
NO_WINDOW = 0x08000000
# 消息编号：WM_APP+1 扫描完成；WM_APP+2 被 tray_win.Shell_NotifyIconW 占用；
# WM_APP+3 把托盘右键异步转发成菜单，避免阻塞 explorer 的同步回调。
WM_REFRESH = win32con.WM_APP + 1
WM_TRAYMENU = win32con.WM_APP + 3
WM_CONTEXTMENU = 0x007B
# Z 序切换：SetWindowPos 的 hWndInsertAfter，以及扩展样式里的置顶标志。
HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
WS_EX_TOPMOST = 0x00000008
SWP_NOSIZE_NOMOVE_NOACTIVATE = 0x0001 | 0x0002 | 0x0010

WATCH_MS = 5000
# 忙碌时降频，但不宜太长：user_busy() 单次仅约 0.2ms，30s 档省不了多少 CPU，
# 却会让用户退出游戏后最多等 30s 才看到卡片。10s 是功耗与响应速度的折中。
WATCH_BUSY_MS = 10000
SCAN_INTERVAL = 300
HOVER_LEAVE_MS = 350
# 续航估算：读数窗口与最小跨度（斜率跨度太短会被读数粒度噪声支配）
RATE_WINDOW = 2400
RATE_MIN_SPAN = 900
# 折叠态多设备轮换周期；只在悬浮窗模式 + 非忙碌 + 折叠态挂定时器。
ROTATE_MS = 4000
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
MODES = ("widget", "taskbar")
# auto = 跟随系统深浅色（注册表 AppsUseLightTheme，监听 WM_SETTINGCHANGE 刷新）
THEME_NAMES = ("light", "dark", "auto")
MODE_CN = {"widget": "桌面卡片", "taskbar": "任务栏贴片"}
# 折叠态圆形电量环：逻辑直径与环宽（配合 MM_ISOTROPIC 自动按 DPI 缩放）
# 折叠态胶囊卡：逻辑尺寸（配合 MM_ISOTROPIC 自动按 DPI 缩放）
PILL_W = 200
PILL_H = 56
THEME_CN = {"light": "浅色", "dark": "深色", "auto": "跟随系统"}

# HyperOS 风调色板：近白卡片 + 极淡分隔、深色态用纯黑系而非蓝灰；
# 语义色（绿/橙/红）与米橙 accent 保持品牌一致
THEMES = {
    "light": {
        "card": "#FFFFFF", "border": "#EDEDEF", "sep": "#F4F4F6",
        "text": "#111114", "muted": "#8C8C8C", "dim": "#B9B9BE",
        "track": "#F2F2F4",
        "good": "#34C759", "mid": "#FF9500", "low": "#FF3B30",
    },
    "dark": {
        "card": "#1D1D1F", "border": "#2E2E30", "sep": "#29292B",
        "text": "#F5F5F7", "muted": "#98989F", "dim": "#6E6E73",
        "track": "#2C2C2E",
        "good": "#34C759", "mid": "#FF9500", "low": "#FF3B30",
    },
}
KIND_BG = {
    "mouse": "#FF6900", "keyboard": "#0A84FF", "headset": "#34C759",
    "gamepad": "#7B61FF", "device": "#8A8A8F", "app": "#FF6900",
}
MI_ORANGE = "#FF6900"

# DPI 感知必须尽早设置：一旦进程开始查询屏幕度量就会被锁定为「不感知」，
# 之后再调 SetProcessDPIAware 会静默失败（窗口按 96dpi 排版，字与圆环都偏小）。
try:
    ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass


# ---- 崩溃防护 ----------------------------------------------------------
# pythonw.exe 没有控制台，sys.stdout / sys.stderr 为 None。任何未捕获异常在
# pywin32 打印 traceback 时会二次崩溃，导致进程静默退出、窗口与图标一起消失。
def safe_streams():
    """pythonw 下 stdout/stderr 为 None，先重定向到 devnull 防打印崩溃。"""
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w"))
            except Exception:
                pass


def log_error(tag, exc=None):
    """异常落盘到同目录 error.log，绝不依赖 stdout/stderr；超限自动裁剪。"""
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > LOG_MAX:
            with open(LOG, "r", encoding="utf-8", errors="replace") as f:
                tail = f.read()[-64 * 1024:]        # 只保留末尾 64KB
            # 超限重写走 pathlib（追加仍走 open(..., "a")）
            Path(LOG).write_text(
                "[%s] == error.log 超过 256KB，以下为保留的末尾 64KB ==\n%s"
                % (time.strftime("%Y-%m-%d %H:%M:%S"), tail), encoding="utf-8")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), tag))
            if exc is not None:
                traceback.print_exception(type(exc), exc, exc.__traceback__, file=f)
            f.write("\n")
    except Exception:
        pass


def install_hooks():
    """全局与线程级异常钩子：未捕获异常只落 error.log，进程绝不静默退出。"""
    def hook(etype, value, tb):
        log_error("unhandled exception", value)
        try:
            traceback.print_exception(etype, value, tb)
        except Exception:
            pass

    sys.excepthook = hook

    def thread_hook(args):
        log_error("thread exception: %s" % getattr(args.thread, "name", "?"),
                  args.exc_value)

    try:
        threading.excepthook = thread_hook
    except Exception:
        pass


safe_streams()
install_hooks()
# ------------------------------------------------------------------------

STATE = {
    "data": None, "expanded": False, "tracking": False, "busy": False,
    "size": (PILL_W, PILL_H), "last_scan": 0.0, "scanning": False,
    "priority": None, "topmost": False, "mode": "widget", "carousel_i": 0,
    "theme": "light", "primary": "auto",
    "aliases": {}, "low_at": 20, "notified": {},
    "rate_hist": {}, "charge_seen": {}, "forced_show": False,
}
Q = queue.Queue()
HWND = None
_FONTS = {}
S = 1.0

C_CARD = C_BORDER = C_TEXT = C_MUTED = C_DIM = C_TRACK = C_SEP = 0
C_GOOD = C_MID = C_LOW = 0
C_ACCENT = cref(MI_ORANGE)


def system_theme():
    """系统应用主题：注册表 AppsUseLightTheme（1=浅色 0=深色），读不到默认浅色。"""
    try:
        v, _ = win32api.RegGetValue(
            win32con.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            "AppsUseLightTheme")
        return "light" if v else "dark"
    except Exception:
        return "light"


def effective_theme():
    """auto 主题解析为实际主题（读注册表），其余原样返回。"""
    return system_theme() if STATE["theme"] == "auto" else STATE["theme"]


def apply_palette():
    """按当前主题刷新 GDI 颜色常量；WM_PAINT 与自检入口都会调用。"""
    global C_CARD, C_BORDER, C_TEXT, C_MUTED, C_DIM, C_TRACK, C_SEP
    global C_GOOD, C_MID, C_LOW
    p = THEMES[effective_theme()]
    C_CARD = cref(p["card"])
    C_BORDER = cref(p["border"])
    C_SEP = cref(p["sep"])
    C_TEXT = cref(p["text"])
    C_MUTED = cref(p["muted"])
    C_DIM = cref(p["dim"])
    C_TRACK = cref(p["track"])
    C_GOOD = cref(p["good"])
    C_MID = cref(p["mid"])
    C_LOW = cref(p["low"])


apply_palette()

# gdi32 / user32 绑定统一在 gdiutil.py 声明（widget / tray_win 共享同一 DLL
# 实例），这里只保留本模块特有的 Shell 忙碌检测、进程优先级与控制台隐藏。
_USER32.GetDpiForSystem.restype = ctypes.c_uint
_USER32.SetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
_USER32.SetWindowRgn.restype = ctypes.c_int
_USER32.SetTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint, ctypes.c_void_p]
_USER32.SetTimer.restype = ctypes.c_size_t
_USER32.KillTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t]

_SHELL32 = ctypes.WinDLL("shell32", use_last_error=True)
_SHELL32.SHQueryUserNotificationState.argtypes = [ctypes.POINTER(ctypes.c_int)]
_SHELL32.SHQueryUserNotificationState.restype = ctypes.c_int

_K32 = ctypes.WinDLL("kernel32", use_last_error=True)
_K32.GetCurrentProcess.restype = ctypes.c_void_p
_K32.GetPriorityClass.argtypes = [ctypes.c_void_p]
_K32.GetPriorityClass.restype = ctypes.c_uint32
_K32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
_K32.SetPriorityClass.restype = ctypes.c_int
_K32.GetConsoleWindow.restype = ctypes.c_void_p

_USER32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
_USER32.ShowWindow.restype = ctypes.c_int


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("dwFlags", ctypes.c_uint32),
                ("hwndTrack", ctypes.c_void_p), ("dwHoverTime", ctypes.c_uint32)]


_USER32.TrackMouseEvent.argtypes = [ctypes.POINTER(TRACKMOUSEEVENT)]
_USER32.TrackMouseEvent.restype = ctypes.c_int


def color_of(v):
    """电量 -> 状态色：>=60 绿、20-59 橙、<20 红。"""
    return C_GOOD if v is None or v >= 60 else (C_MID if v >= 20 else C_LOW)


def reals(data):
    """取有真实读数的设备列表（data 为 None 时返回空表）。"""
    return [d for d in data["devices"] if d["battery"] is not None] if data else []


def display_name(d):
    """设备显示名：设置了别名用别名，否则用原始名。"""
    return STATE.get("aliases", {}).get(d.get("id") or d.get("name"),
                                        d.get("name") or "")


def cut(s, n):
    """超长字符串截断加省略号。"""
    return s if len(s) <= n else s[:n - 1] + "…"


def expanded_size(data):
    """展开态窗口尺寸 = 头部 + 行数 × 行高 + 底部说明。"""
    return 320, 52 + max(1, len(reals(data))) * 66 + 32


def user_busy():
    """全屏 D3D 游戏 / 演示模式 / 用户忙碌时返回 True，程序主动让路。"""
    try:
        st = ctypes.c_int()
        _SHELL32.SHQueryUserNotificationState(ctypes.byref(st))
        return st.value in (2, 3, 4)
    except Exception:
        return False


def hide_console():
    """python.exe 启动时隐藏控制台；pythonw.exe 下无控制台，自动跳过。"""
    try:
        h = _K32.GetConsoleWindow()
        if h:
            _USER32.ShowWindow(ctypes.c_void_p(h), 0)
    except Exception:
        pass


def set_low_priority():
    """降到 BELOW_NORMAL，不与游戏 / 前台程序抢 CPU 时间片。"""
    try:
        p = ctypes.c_void_p(_K32.GetCurrentProcess())
        _K32.SetPriorityClass(p, BELOW_NORMAL_PRIORITY_CLASS)
        return _K32.GetPriorityClass(p)
    except Exception:
        return None


def track_leave(hwnd):
    """登记 WM_MOUSELEAVE 跟踪（悬停展开的收起依赖它）。"""
    t = TRACKMOUSEEVENT()
    t.cbSize = ctypes.sizeof(t)
    t.dwFlags = 0x00000002
    t.hwndTrack = ctypes.c_void_p(int(hwnd))
    t.dwHoverTime = 0
    _USER32.TrackMouseEvent(ctypes.byref(t))


def inside(hwnd):
    """光标是否在卡片窗口矩形内。"""
    px, py = win32gui.GetCursorPos()
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    return l <= px <= r and t <= py <= b


def dpi_scale():
    """系统 DPI 缩放系数（96dpi = 1.0）。"""
    try:
        return max(1.0, _USER32.GetDpiForSystem() / 96.0)
    except Exception:
        return 1.0


def setup_scale(hdc):
    """给 DC 设 MM_ISOTROPIC 逻辑坐标：绘制代码全部按 96dpi 逻辑像素书写。"""
    if S <= 1.01:
        return
    h = ctypes.c_void_p(int(hdc))
    _GDI.SetMapMode(h, win32con.MM_ISOTROPIC)
    _GDI.SetWindowExtEx(h, 1000, 1000, None)
    _GDI.SetViewportExtEx(h, int(1000 * S), int(1000 * S), None)


def font(size, bold=False, name="Microsoft YaHei"):
    """按 (字号, 加粗, 字体名) 缓存的 GDI 字体句柄。"""
    key = (size, bold, name)
    if key not in _FONTS:
        _FONTS[key] = _GDI.CreateFontW(
            -size, 0, 0, 0, 700 if bold else 400, 0, 0, 0,
            win32con.DEFAULT_CHARSET, win32con.OUT_DEFAULT_PRECIS,
            win32con.CLIP_DEFAULT_PRECIS, win32con.CLEARTYPE_QUALITY,
            win32con.DEFAULT_PITCH | win32con.FF_SWISS, name)
    return _FONTS[key]


def text(hdc, s, rect, size, color, bold=False, right=False, center=False,
         name="Microsoft YaHei"):
    """单行文字绘制：透明底、可选对齐，返回前恢复原字体。"""
    old = win32gui.SelectObject(hdc, font(size, bold, name))
    win32gui.SetBkMode(hdc, win32con.TRANSPARENT)
    win32gui.SetTextColor(hdc, color)
    align = win32con.DT_CENTER if center else (
        win32con.DT_RIGHT if right else win32con.DT_LEFT)
    flags = win32con.DT_SINGLELINE | win32con.DT_VCENTER | win32con.DT_NOPREFIX | align
    win32gui.DrawText(hdc, s, len(s), rect, flags)
    win32gui.SelectObject(hdc, old)


_ICON_DIB = {}


def device_glyph(hdc, kind, x, y, size, fg=None, bg=None):
    """贴 Phosphor Icons 素材（MIT）：彩色圆角衬底 + 专业设备图标。

    PNG 由 _gen_icons.py 离线 4x 超采样渲染，四角已预乘当前主题卡片底色，
    因此直接 StretchDIBits 即可，无需 alpha 混合，也不再依赖 GDI 简笔画。
    """
    key = kind if kind in KIND_BG else "device"
    # 素材只有 light/dark 两套：auto 主题必须解析后再取，否则找不到文件
    path = os.path.join(ASSET_ROOT, "assets", "gen",
                        "%s_%s_ui.png" % (effective_theme(), key))
    dib = _ICON_DIB.get(path)
    if dib is None:
        try:
            from PIL import Image, ImageWin
            dib = ImageWin.Dib(Image.open(path).convert("RGB"))
            _ICON_DIB[path] = dib
        except Exception as e:
            log_error("icon asset " + path, e)
            return
    dib.draw(hdc, (x, y, x + size, y + size))


def primary_device(items):
    """圆环要显示哪台设备：指定了就固定，auto 则按轮换索引（单台时即唯一那台）。"""
    if not items:
        return None
    pid = STATE.get("primary", "auto")
    if pid and pid != "auto":
        for d in items:
            if d.get("id") == pid or d.get("name") == pid:
                return d
    return items[STATE["carousel_i"] % len(items)]


def paint_collapsed(hdc, w, h, data):
    """折叠态胶囊卡：设备图标 + 名称/续航 + HyperOS 电量字 + 底部细进度条。

    悬停展开全量列表；轮换机制不变（主设备为 auto 时逐台轮播）。
    """
    items = reals(data)
    cur = primary_device(items)
    v = cur["battery"] if cur else None
    col = color_of(v) if v is not None else C_DIM

    if cur:
        device_glyph(hdc, cur["kind"], 10, 10, 36, 0xFFFFFF, bg=cref(
            KIND_BG.get(cur["kind"], KIND_BG["device"])))
        name = cut(display_name(cur), 12)
        sub = cur["link"]
        if cur.get("charging"):
            sub = "充电中 · " + sub
        est = time_left_text(cur)
        if est:
            sub = est
        text(hdc, name, (54, 9, w - 82, 28), 12, C_TEXT, True)
        text(hdc, cut(sub, 16), (54, 28, w - 82, 46), 9, C_MUTED)
    else:
        text(hdc, "暂无设备", (54, 9, w - 82, 28), 12, C_TEXT, True)
        text(hdc, "等待扫描读数", (54, 28, w - 82, 46), 9, C_MUTED)

    # HyperOS 式电量字：大数字 + 小百分号
    pct = ("%d" % v) if v is not None else "--"
    text(hdc, pct, (w - 84, 6, w - 34, 48), 20, col, True, right=True,
         name="Segoe UI Semibold")
    text(hdc, "%", (w - 34, 16, w - 14, 48), 10, col, True, name="Segoe UI")

    # 底部细进度条
    round_fill(hdc, 10, h - 9, w - 10, h - 6, 1, C_TRACK)
    if v is not None:
        round_fill(hdc, 10, h - 9, 10 + max(2, int((w - 20) * v / 100)),
                   h - 6, 1, col)


def paint_expanded(hdc, w, h, data):
    """展开态全量列表：标题行 + 每台设备一行（图标/名称/电量字/进度条）。"""
    text(hdc, "电量透视", (22, 0, 140, 46), 15, C_TEXT, True)
    if data:
        text(hdc, "更新 " + data["scan_time"], (140, 0, w - 22, 46),
             11, C_DIM, right=True, name="Segoe UI")
    fill(hdc, 22, 46, w - 22, 47, C_SEP)
    y = 52
    items = reals(data)
    if not items:
        text(hdc, "暂未读到设备电量", (22, y, w - 100, y + 40), 13, C_DIM)
        y += 66
    for d in items:
        v, col = d["battery"], color_of(d["battery"])
        device_glyph(hdc, d["kind"], 22, y + 8, 40, 0xFFFFFF, bg=cref(
            KIND_BG.get(d["kind"], KIND_BG["device"])))
        text(hdc, cut(display_name(d), 22), (74, y + 6, w - 100, y + 30), 13, C_TEXT, True)
        meta = "%s · %s" % (d["kind_cn"], d["link"])
        if d.get("charging"):
            meta += " · 充电中"
        est = time_left_text(d)
        if est:
            meta += " · " + est
        text(hdc, cut(meta, 24), (74, y + 30, w - 100, y + 48), 10, C_MUTED)
        # HyperOS 式电量字：大号数字 + 小号百分号
        text(hdc, "%d" % v, (w - 108, y + 2, w - 40, y + 40), 22, col,
             True, right=True, name="Segoe UI Semibold")
        text(hdc, "%", (w - 40, y + 12, w - 22, y + 40), 11, col,
             True, name="Segoe UI")
        bw = w - 96
        round_fill(hdc, 74, y + 55, 74 + bw, y + 59, 2, C_TRACK)
        round_fill(hdc, 74, y + 55, 74 + max(3, int(bw * v / 100)), y + 59, 2, col)
        fill(hdc, 74, y + 64, w - 22, y + 65, C_SEP)
        y += 66
    n = sum(1 for d in (data["devices"] if data else []) if d["status"] == "protocol")
    text(hdc, ("另有 %d 台协议未公开" % n) if n else "所有设备均已读数",
         (22, h - 30, w - 22, h - 6), 10, C_DIM)


def apply_size(hwnd):
    """按当前形态计算尺寸并定位右下角，同时更新圆角矩形窗口区域。"""
    data = STATE["data"]
    if STATE["expanded"]:
        w, h = expanded_size(data)
    else:
        w, h = PILL_W, PILL_H
    STATE["size"] = (w, h)
    pw, ph = int(w * S), int(h * S)
    sw = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
    sh = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
    x, y = sw - pw - int(18 * S), sh - ph - int(62 * S)
    win32gui.SetWindowPos(hwnd, None, x, y, pw, ph,
                          win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)
    if STATE["expanded"]:
        r = int(22 * S)
    else:
        # 胶囊卡：圆角矩形窗口区域，四角透明、不挡桌面
        r = int(14 * S)
    hrgn = _GDI.CreateRoundRectRgn(0, 0, pw + 1, ph + 1, r, r)
    if hrgn:
        _USER32.SetWindowRgn(ctypes.c_void_p(int(hwnd)), ctypes.c_void_p(hrgn), 1)
    win32gui.InvalidateRect(hwnd, None, True)


def summon_card(hwnd):
    """兜底召唤：托盘左键一点，悬浮卡必现。

    无视当前形态（贴片模式自动切回卡片）、无视忙碌状态（用户意图优先）；
    最小化先还原，最后强制重定位到底部标准位置。
    """
    STATE["mode"] = "widget"
    STATE["forced_show"] = True
    apply_mode(hwnd, explicit=True)          # 切形态 + 显示（意图优先）
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    apply_size(hwnd)                         # 强制归位（无论此前状态）
    save_settings()


def ensure_card_visible(hwnd):
    """卡片被外部原因藏掉时自愈：「显示桌面」/ 最小化全部 / 意外隐藏。

    只在「按当前状态应当可见」时调用；忙碌自动隐藏是自身行为，不走这里。
    """
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        apply_size(hwnd)
    elif not win32gui.IsWindowVisible(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
        apply_size(hwnd)


def set_expanded(hwnd, value):
    """切换悬停展开态；不忙碌时立即重设窗口尺寸。"""
    if STATE["expanded"] == value:
        return
    STATE["expanded"] = value
    if not STATE["busy"] and win32gui.IsWindowVisible(hwnd):
        apply_size(hwnd)
    rotation_timer(hwnd, rotation_wanted())


def hover_check(hwnd):
    """5 秒一次的兜底：处理鼠标已停在卡片上但没产生移动消息的情况。"""
    if STATE["busy"] or not win32gui.IsWindowVisible(hwnd):
        return
    hit = inside(hwnd)
    if hit and not STATE["expanded"]:
        set_expanded(hwnd, True)
    elif not hit and STATE["expanded"]:
        set_expanded(hwnd, False)


def request_scan(force=False):
    """后台线程发起轻量扫描（300s 节流，force 可穿透），完成后投递 WM_REFRESH。"""
    if STATE["scanning"]:
        return
    if not force and time.time() - STATE["last_scan"] < SCAN_INTERVAL:
        return
    STATE["scanning"] = True
    STATE["last_scan"] = time.time()

    def job():
        try:
            Q.put(collector.scan_light())
            if HWND:
                win32gui.PostMessage(HWND, WM_REFRESH, 0, 0)
        except Exception as e:
            log_error("scan thread", e)
        finally:
            STATE["scanning"] = False

    threading.Thread(target=job, daemon=True).start()


def on_watch(hwnd):
    """5s/10s 心跳：忙碌切换、贴片/卡片保活自愈、悬停检测、到点扫描。"""
    busy = user_busy()
    if busy != STATE["busy"]:
        STATE["busy"] = busy
        h = ctypes.c_void_p(int(hwnd))
        _USER32.KillTimer(h, 1)
        _USER32.SetTimer(h, 1, WATCH_BUSY_MS if busy else WATCH_MS, None)
        if busy:
            # 忙碌时收起桌面卡片，除非是用户刚在菜单里主动要求的（意图优先）。
            if STATE["mode"] == "widget" and not STATE.get("forced_show"):
                win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
            elif STATE["mode"] != "widget":
                STATE["forced_show"] = False
            STATE["expanded"] = False
            STATE["tracking"] = False
        else:
            STATE["forced_show"] = False
            if STATE["mode"] == "widget":
                win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
            else:
                taskbar_sync()
        rotation_timer(hwnd, rotation_wanted())
    if busy:
        # 用户在菜单里主动要求显示期间，卡片保持可见（自愈被外部藏掉的情况）。
        if STATE["mode"] == "widget" and STATE.get("forced_show"):
            ensure_card_visible(hwnd)
        return
    if STATE["mode"] == "widget":
        ensure_card_visible(hwnd)
        hover_check(hwnd)
    elif STATE["mode"] == "taskbar":
        # 贴片保活：Explorer 重启 / DPI / 任务栏主题变化时自动重挂
        taskbar_sync()
    if time.time() - STATE["last_scan"] >= SCAN_INTERVAL:
        request_scan()


def load_settings():
    """读取显示模式 / 置顶 / 主题 / 主设备 / 任务栏勾选；兼容早期只有 topmost 的文件。"""
    try:
        with open(SETTINGS, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raw = {}
    except Exception:
        raw = {}
    mode = raw.get("mode", "widget")
    if mode not in MODES:
        mode = "widget"
    theme = raw.get("theme", "light")
    if theme not in THEME_NAMES:
        theme = "light"
    primary = raw.get("primary", "auto")
    if not isinstance(primary, str) or not primary:
        primary = "auto"
    aliases = raw.get("aliases")
    if not isinstance(aliases, dict):
        aliases = {}
    aliases = {str(k): str(v).strip() for k, v in aliases.items()
               if isinstance(k, str) and isinstance(v, str) and v.strip()}
    try:
        low_at = min(100, max(0, int(raw.get("low_at", 20))))
    except (TypeError, ValueError):
        low_at = 20
    return {"mode": mode, "topmost": bool(raw.get("topmost", False)),
            "theme": theme, "primary": primary,
            "aliases": aliases, "low_at": low_at}


def save_settings():
    """持久化当前设置（mode/置顶/主题/主设备/别名/低电量阈值）。"""
    try:
        with open(SETTINGS, "w", encoding="utf-8") as f:
            json.dump({"mode": STATE["mode"], "topmost": STATE["topmost"],
                       "theme": STATE["theme"], "primary": STATE["primary"],
                       "aliases": STATE.get("aliases", {}),
                       "low_at": STATE.get("low_at", 20)},
                      f, ensure_ascii=False)
    except Exception:
        pass


def apply_topmost(hwnd, on):
    """切换全局最上层：只改 Z 序，不动位置、尺寸与焦点。

    两步法：从未显示过的窗口直接 TOPMOST 会被系统静默忽略，
    先摘到普通层再挂顶，任何窗口状态下都保证生效。
    """
    try:
        if on:
            win32gui.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0,
                                  SWP_NOSIZE_NOMOVE_NOACTIVATE)
        win32gui.SetWindowPos(hwnd, HWND_TOPMOST if on else HWND_NOTOPMOST,
                              0, 0, 0, 0, SWP_NOSIZE_NOMOVE_NOACTIVATE)
    except Exception:
        pass


def refresh_app_icon(hwnd):
    """常驻程序主图标：最低电量 + 设备数 + 当前显示模式。"""
    items = reals(STATE["data"])
    low = min((d["battery"] for d in items), default=None)
    tip = "电量透视 · %s · %d 台设备" % (MODE_CN[STATE["mode"]], len(items))
    tray_win.add_app(hwnd, low, len(items), tip)


def check_low_battery(hwnd):
    """低电量气泡提醒：每台设备越过阈值只提醒一次，回到阈值上方重新布防。"""
    data = STATE["data"]
    low_at = STATE.get("low_at", 20)
    if not data or low_at <= 0:
        return
    for d in data["devices"]:
        v, did = d.get("battery"), d.get("id") or d.get("name")
        if v is None or d.get("status") != "real":
            continue
        last = STATE["notified"].get(did)
        if v <= low_at and (last is None or last > low_at):
            name = display_name(d)
            tray_win.notify_app(hwnd, "电量低 · " + name,
                                "%s 剩余 %d%%，建议充电" % (name, v))
            STATE["notified"][did] = v
        elif v > low_at and last is not None:
            STATE["notified"].pop(did, None)


def update_rate_history(data):
    """内存里留每台设备最近 40 分钟的真实读数（不落盘），供续航估算。"""
    now = time.time()
    hist = STATE["rate_hist"]
    seen = set()
    for d in data["devices"]:
        did, v = d.get("id") or d.get("name"), d.get("battery")
        if not did or v is None or d.get("status") != "real":
            continue
        seen.add(did)
        pts = hist.setdefault(did, [])
        if not pts or now - pts[-1][0] >= 60:       # 同一分钟内去重
            pts.append((now, v))
        while pts and now - pts[0][0] > RATE_WINDOW:
            pts.pop(0)
    for did in [k for k in hist if k not in seen]:
        hist.pop(did, None)          # 设备消失即清：休眠重连不背旧账


def drain_rate(did):
    """放电斜率（%/小时，负值）；样本不足或不在放电返回 None。"""
    pts = STATE["rate_hist"].get(did) or []
    if len(pts) < 3 or pts[-1][0] - pts[0][0] < RATE_MIN_SPAN:
        return None
    rate = (pts[-1][1] - pts[0][1]) / ((pts[-1][0] - pts[0][0]) / 3600.0)
    return rate if rate < -0.5 else None     # 下降不足 0.5%/h 视为粒度噪声


def time_left_text(d):
    """「约剩2h13m」式估算；斜率不足 / 估算超过一天时不显示（诚实原则）。"""
    rate = drain_rate(d.get("id") or d.get("name"))
    if rate is None:
        return ""
    hours = d["battery"] / -rate
    if hours >= 24:
        return ""
    if hours < 0.4:
        return "约剩不到半小时"
    h, m = int(hours), int(round((hours - int(hours)) * 60))
    if m == 60:
        h, m = h + 1, 0
    return "约剩%dh%02dm" % (h, m) if h else "约剩%dm" % m


def check_charging(hwnd):
    """充电管家：插电 / 充满各提醒一次，拔掉或电量回落后重新布防。

    本机电池用系统充电位；外设没有充电标志，用电量上升沿推断——
    气泡文案说「电量正在回升」而不是断言充电，保持诚实。
    """
    data = STATE["data"]
    if not data:
        return
    seen = set()
    for d in data["devices"]:
        did, v = d.get("id") or d.get("name"), d.get("battery")
        if not did or v is None or d.get("status") != "real":
            continue
        seen.add(did)
        st = STATE["charge_seen"].setdefault(
            did, {"v": None, "rise": 0, "plugged": False, "full": False})
        prev_v, st["v"] = st["v"], v
        if d.get("charging") is True:            # 本机电池：系统充电位
            charging = True
        elif d.get("charging") is False:
            charging = False
        else:                                    # 外设：电量上升沿推断
            st["rise"] = st["rise"] + 1 if (prev_v is not None and v > prev_v) else 0
            charging = st["rise"] >= 1
        if not charging:
            st["plugged"] = False
            st["full"] = False
            continue
        name = display_name(d)
        if not st["plugged"]:
            st["plugged"] = True
            tray_win.notify_app(hwnd, "充电中 · " + name,
                                "%s 电量正在回升，当前 %d%%" % (name, v))
        if v >= 100 and not st["full"]:
            st["full"] = True
            tray_win.notify_app(hwnd, "已充满 · " + name,
                                "%s 电量已满，可以拔掉了" % name)
    for did in [k for k in STATE["charge_seen"] if k not in seen]:
        STATE["charge_seen"].pop(did, None)


def apply_theme(hwnd):
    """切换浅/深主题：刷新调色板、重建主图标、重绘桌面卡片与贴片。"""
    apply_palette()
    tray_win.set_theme(effective_theme())
    refresh_app_icon(hwnd)
    if STATE["mode"] == "taskbar":
        taskbar_sync()
    win32gui.InvalidateRect(hwnd, None, True)


def rotation_wanted():
    """是否需要轮换：悬浮窗模式 + 未指定主设备 + 非忙碌 + 折叠态 + 至少两台设备。"""
    return (STATE["mode"] == "widget" and not STATE["busy"]
            and not STATE["expanded"] and STATE.get("primary", "auto") == "auto"
            and len(reals(STATE["data"])) > 1)


def rotation_timer(hwnd, on):
    """按需启停轮换定时器，避免全屏游戏时仍每 4 秒唤醒进程。"""
    h = ctypes.c_void_p(int(hwnd))
    _USER32.KillTimer(h, 3)
    if on:
        _USER32.SetTimer(h, 3, ROTATE_MS, None)


def rotate_step(hwnd):
    """轮换到下一台有读数的设备并重绘。"""
    items = reals(STATE["data"])
    if len(items) < 2:
        return
    STATE["carousel_i"] = (STATE["carousel_i"] + 1) % len(items)
    win32gui.InvalidateRect(hwnd, None, True)


def taskbar_devices():
    """贴片要显示的设备：全部有读数台账（含别名），无读数时画「--」。"""
    return [dict(d, name=display_name(d))
            for d in (STATE["data"]["devices"] if STATE["data"] else [])]


def taskbar_sync():
    """把设备列表与主题明暗推给任务栏贴片（不存在则重挂）。

    读数与主题都没变且贴片还在时不重绘——空闲时贴片零 GDI 活动，
    保活交给 ensure()（Explorer 重启后窗口失效会自动重建）。
    """
    if not taskbar.ensure(HWND):
        log_error("taskbar strip: Shell_TrayWnd 不可用，贴片未创建")
        return
    state = (tuple(sorted((d["id"], d.get("battery"))
                          for d in taskbar_devices())),
             effective_theme())
    if STATE.get("_strip_state") == state and taskbar.alive():
        return
    STATE["_strip_state"] = state
    taskbar.refresh()
    taskbar.update(taskbar_devices(), effective_theme() == "dark")


def apply_mode(hwnd, explicit=False):
    """切换显示形态：桌面卡片 / 任务栏贴片。

    explicit=True 表示用户从菜单主动切换：即使前台是全屏应用也立即显示
    卡片——用户意图优先于忙碌隐藏（忙碌结束或下次进入全屏后恢复自动行为）。
    """
    STATE["expanded"] = False
    STATE["tracking"] = False
    if STATE["mode"] == "taskbar":
        STATE["forced_show"] = False
        win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
        taskbar_sync()
    else:
        taskbar.destroy()
        STATE["forced_show"] = explicit
        if STATE["busy"] and not explicit:
            win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
        else:
            win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
    refresh_app_icon(hwnd)
    rotation_timer(hwnd, rotation_wanted())


def autostart_enabled():
    """开机自启是否已开启（以启动文件夹里的快捷方式为准）。"""
    return os.path.exists(LNK)


def toggle_autostart():
    """开关开机自启：往启动文件夹写/删快捷方式（exe 模式指向自身）。"""
    if autostart_enabled():
        try:
            os.remove(LNK)
        except OSError:
            pass
        return
    if getattr(sys, "frozen", False):
        target, args = sys.executable, ""          # exe 自身即入口
    else:
        target, args = sys.executable, '"%s"' % os.path.join(ROOT, "widget.py")
    wd = os.path.dirname(target)
    ps = ("$s=(New-Object -COM WScript.Shell).CreateShortcut('" + LNK + "');"
          "$s.TargetPath='" + target + "';"
          "$s.Arguments='" + args + "';"
          "$s.WorkingDirectory='" + wd + "';"
          "$s.WindowStyle=7;"
          "$s.Save()")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=15, creationflags=NO_WINDOW)
    except Exception:
        pass


def primary_label():
    """主设备菜单项的文案：自动轮换或「类别 · 电量」。"""
    pid = STATE.get("primary", "auto")
    if pid == "auto":
        return "自动轮换"
    for d in reals(STATE["data"]):
        if d.get("id") == pid or d.get("name") == pid:
            return "%s · %d%%" % (d.get("kind_cn", "设备"), d["battery"])
    return "已断开"


def on_menu(hwnd):
    """构建并弹出右键菜单，返回选中的命令号并分发处理。"""
    m = win32gui.CreatePopupMenu()
    win32gui.AppendMenu(m, win32con.MF_STRING, 1, "立即刷新")

    # 主设备子菜单：决定圆环显示哪一台
    sub = win32gui.CreatePopupMenu()
    achk = (win32con.MF_CHECKED if STATE.get("primary", "auto") == "auto"
            else win32con.MF_UNCHECKED)
    win32gui.AppendMenu(sub, win32con.MF_STRING | achk, 20, "自动（轮换）")
    for i, d in enumerate(reals(STATE["data"])[:9]):
        pid = STATE.get("primary", "auto")
        chk = (win32con.MF_CHECKED if pid in (d.get("id"), d.get("name"))
               else win32con.MF_UNCHECKED)
        win32gui.AppendMenu(sub, win32con.MF_STRING | chk, 30 + i,
                            "%s · %d%%" % (d.get("kind_cn", "设备"), d["battery"]))
    win32gui.AppendMenu(m, win32con.MF_POPUP, sub, "主设备：%s" % primary_label())

    # 低电量提醒阈值
    sub2 = win32gui.CreatePopupMenu()
    for i, lv in enumerate((0, 10, 15, 20, 30)):
        chk = (win32con.MF_CHECKED if STATE.get("low_at", 20) == lv
               else win32con.MF_UNCHECKED)
        win32gui.AppendMenu(sub2, win32con.MF_STRING | chk, 60 + i,
                            "不提醒" if lv == 0 else "低于 %d%%" % lv)
    win32gui.AppendMenu(m, win32con.MF_POPUP, sub2, "低电量提醒")

    tchk = win32con.MF_CHECKED if STATE["mode"] == "taskbar" else win32con.MF_UNCHECKED
    win32gui.AppendMenu(m, win32con.MF_STRING | tchk, 5, "显示为任务栏贴片")
    chk = win32con.MF_CHECKED if autostart_enabled() else win32con.MF_UNCHECKED
    win32gui.AppendMenu(m, win32con.MF_STRING | chk, 3, "开机自动启动")
    tchk = win32con.MF_CHECKED if STATE["topmost"] else win32con.MF_UNCHECKED
    win32gui.AppendMenu(m, win32con.MF_STRING | tchk, 4, "窗口置顶 · 全局最上层")
    win32gui.AppendMenu(m, win32con.MF_STRING, 6,
                        "切换主题 · 当前%s" % THEME_CN[STATE["theme"]])
    win32gui.AppendMenu(m, win32con.MF_SEPARATOR, 0, "")
    win32gui.AppendMenu(m, win32con.MF_STRING, 9, "退出")
    x, y = win32gui.GetCursorPos()
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    try:
        cmd = win32gui.TrackPopupMenu(
            m, win32con.TPM_RETURNCMD | win32con.TPM_LEFTALIGN,
            x, y, 0, hwnd, None)
    finally:
        win32gui.DestroyMenu(m)
        try:
            win32gui.PostMessage(hwnd, win32con.WM_NULL, 0, 0)
        except Exception:
            pass
    if cmd == 1:
        STATE["last_scan"] = 0.0
        request_scan(force=True)
    elif cmd == 3:
        toggle_autostart()
    elif cmd == 4:
        STATE["topmost"] = not STATE["topmost"]
        apply_topmost(hwnd, STATE["topmost"])
        refresh_app_icon(hwnd)
        save_settings()
    elif cmd == 5:
        STATE["mode"] = "taskbar" if STATE["mode"] != "taskbar" else "widget"
        apply_mode(hwnd, explicit=True)
        save_settings()
    elif cmd == 6:
        i = THEME_NAMES.index(STATE["theme"]) if STATE["theme"] in THEME_NAMES else 0
        STATE["theme"] = THEME_NAMES[(i + 1) % len(THEME_NAMES)]
        apply_theme(hwnd)
        save_settings()
    elif cmd == 9:
        win32gui.DestroyWindow(hwnd)
    elif cmd == 20:
        STATE["primary"] = "auto"
        STATE["carousel_i"] = 0
        save_settings()
        apply_size(hwnd)
        rotation_timer(hwnd, rotation_wanted())
    elif 30 <= cmd < 40:
        items = reals(STATE["data"])
        idx = cmd - 30
        if 0 <= idx < len(items):
            STATE["primary"] = items[idx].get("id") or items[idx].get("name")
            save_settings()
            apply_size(hwnd)
            rotation_timer(hwnd, rotation_wanted())
    elif 60 <= cmd < 60 + 5:
        STATE["low_at"] = (0, 10, 15, 20, 30)[cmd - 60]
        STATE["notified"] = {}
        save_settings()


def _wnd_proc(hwnd, msg, wp, lp):
    """卡片窗口过程：绘制/悬停/定时器/托盘回调/系统广播，全部走异常包装。"""
    if msg == win32con.WM_SYSCOMMAND:
        # 「显示桌面」/ 最小化全部：无边框悬浮卡没有最小化语义，直接吞掉，
        # 否则窗口会被收进一个不存在的任务栏按钮里，看起来像凭空消失。
        if (wp & 0xFFF0) in (win32con.SC_MINIMIZE, win32con.SC_MAXIMIZE):
            return 0
    if msg == win32con.WM_PAINT:
        hdc, ps = win32gui.BeginPaint(hwnd)
        try:
            apply_palette()
            w, h = STATE["size"]
            setup_scale(hdc)
            fill(hdc, 0, 0, w, h, C_CARD)
            frame(hdc, 0, 0, w, h, C_BORDER)
            (paint_expanded if STATE["expanded"] else paint_collapsed)(hdc, w, h, STATE["data"])
        except Exception as e:
            log_error("WM_PAINT", e)
        win32gui.EndPaint(hwnd, ps)
        return 0
    if msg == win32con.WM_MOUSEMOVE:
        if not STATE["busy"]:
            if not STATE["expanded"]:
                set_expanded(hwnd, True)
            if not STATE["tracking"]:
                track_leave(hwnd)
                STATE["tracking"] = True
        return 0
    if msg == win32con.WM_MOUSELEAVE:
        STATE["tracking"] = False
        _USER32.SetTimer(ctypes.c_void_p(int(hwnd)), 2, HOVER_LEAVE_MS, None)
        return 0
    if msg == win32con.WM_TIMER:
        if wp == 1:
            on_watch(hwnd)
        elif wp == 2:
            _USER32.KillTimer(ctypes.c_void_p(int(hwnd)), 2)
            if not inside(hwnd) and STATE["expanded"]:
                set_expanded(hwnd, False)
        elif wp == 3:
            if rotation_wanted():
                rotate_step(hwnd)
            else:
                rotation_timer(hwnd, False)
        return 0
    if msg == win32con.WM_RBUTTONUP:
        on_menu(hwnd)
        return 0
    if msg == tray_win.WM_TRAY:
        # Shell_NotifyIconW 以同步方式投递回调，这里只转发，不阻塞任务栏。
        low = lp & 0xFFFF
        if low in (win32con.WM_RBUTTONUP, WM_CONTEXTMENU):
            win32gui.PostMessage(hwnd, WM_TRAYMENU, 0, 0)
        elif low == win32con.WM_LBUTTONUP:
            # 左键 = 兜底召唤悬浮卡（任何状态下必现）+ 立即刷新
            summon_card(hwnd)
            STATE["last_scan"] = 0.0
            request_scan(force=True)
        return 0
    if msg == WM_TRAYMENU:
        on_menu(hwnd)
        return 0
    if msg == WM_REFRESH:
        try:
            while True:
                STATE["data"] = Q.get_nowait()
        except queue.Empty:
            pass
        update_rate_history(STATE["data"])
        refresh_app_icon(hwnd)
        check_low_battery(hwnd)
        check_charging(hwnd)
        if STATE["mode"] == "taskbar":
            taskbar_sync()
        if not STATE["busy"] and STATE["mode"] == "widget":
            apply_size(hwnd)
        rotation_timer(hwnd, rotation_wanted())
        return 0
    if msg == win32con.WM_SETTINGCHANGE:
        # 系统深浅色切换广播：auto 主题下实时跟随
        if STATE["theme"] == "auto" and lp:
            try:
                if ctypes.wstring_at(lp) == "ImmersiveColorSet":
                    apply_theme(hwnd)
            except Exception:
                pass
    if msg == win32con.WM_DESTROY:
        rotation_timer(hwnd, False)
        try:
            taskbar.destroy()
            tray_win.remove_app(hwnd)
        except Exception as e:
            log_error("tray cleanup", e)
        win32gui.PostQuitMessage(0)
        return 0
    return win32gui.DefWindowProc(hwnd, msg, wp, lp)


def wnd_proc(hwnd, msg, wp, lp):
    """所有消息的统一兜底：异常只落日志，绝不让 pywin32 的 WndProc 冒泡。"""
    try:
        return _wnd_proc(hwnd, msg, wp, lp)
    except Exception as e:
        log_error("wnd_proc msg=0x%04X" % (msg if isinstance(msg, int) else 0), e)
        try:
            return win32gui.DefWindowProc(hwnd, msg, wp, lp)
        except Exception:
            return 0


def paint_smoke(data):
    """离屏渲染折叠态与展开态，返回 OK/失败原因（渲染回归用）。"""
    screen = win32gui.GetDC(0)
    mem = win32gui.CreateCompatibleDC(screen)
    bmp = win32gui.CreateCompatibleBitmap(screen, 600, 400)
    old = win32gui.SelectObject(mem, bmp)
    try:
        apply_palette()
        setup_scale(mem)
        fill(mem, 0, 0, 600, 400, C_CARD)
        paint_collapsed(mem, PILL_W, PILL_H, data)
        w, h = expanded_size(data)
        paint_expanded(mem, w, h, data)
        return "OK"
    except Exception as e:
        return "FAIL %s: %s" % (type(e).__name__, e)
    finally:
        win32gui.SelectObject(mem, old)
        win32gui.DeleteObject(bmp)
        win32gui.DeleteDC(mem)
        win32gui.ReleaseDC(0, screen)


def run_checks():
    """一条命令跑完全部自检：进程功耗 → 采集 → 渲染 → 置顶 → 托盘 → 崩溃兜底。"""
    global S
    print("=" * 52)

    print("[1] 进程与功耗")
    print("streams_safe", sys.stdout is not None and sys.stderr is not None)
    print("excepthook_installed", sys.excepthook is not sys.__excepthook__)
    after = set_low_priority()
    print("priority_applied", after == BELOW_NORMAL_PRIORITY_CLASS,
          "(0x%08X)" % (after or 0))
    t0 = time.perf_counter()
    for _ in range(200):
        user_busy()
    print("user_busy x200 %.2f ms | watch %ds (busy %ds) | scan %ds | cache %ds"
          % ((time.perf_counter() - t0) * 1000, WATCH_MS // 1000,
             WATCH_BUSY_MS // 1000, SCAN_INTERVAL, collector.STATIC_TTL))

    print("[2] 采集")
    data = collector.scan()
    STATE["data"] = data
    items = reals(data)
    light = collector.scan_light()
    print("devices %d | with_battery %d | full %dms | light %dms"
          % (len(data["devices"]), len(items),
             data["duration_ms"], light["duration_ms"]))
    for d in items:
        print("  %s %s %d%% (%s)" % (d["kind_cn"], d["name"], d["battery"],
                                     d["source"]))

    cur = primary_device(items)
    print("[3] 渲染 ring %dx%d | expanded %s | primary %s"
          % (PILL_W, PILL_H, expanded_size(data),
             ("%d%% %s" % (cur["battery"], cur["kind_cn"])) if cur else "--"))
    saved_theme = STATE["theme"]
    for th in THEME_NAMES:
        STATE["theme"] = th
        tray_win.set_theme(effective_theme())
        print("paint_smoke", th, paint_smoke(data))
    STATE["theme"] = saved_theme
    tray_win.set_theme(effective_theme())
    apply_palette()
    S = 1.5
    print("paint_smoke_150pct", paint_smoke(data))
    S = 1.0

    print("[4] 置顶")
    wc = win32gui.WNDCLASS()
    wc.lpfnWndProc = win32gui.DefWindowProc
    wc.hInstance = win32api.GetModuleHandle(None)
    wc.lpszClassName = "BatteryLensCheckProbe"
    win32gui.RegisterClass(wc)
    hwnd = win32gui.CreateWindowEx(
        0, "BatteryLensCheckProbe", "probe", win32con.WS_POPUP,
        0, 0, 1, 1, 0, 0, wc.hInstance, None)
    try:
        apply_topmost(hwnd, True)
        on = bool(win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
                  & WS_EX_TOPMOST)
        apply_topmost(hwnd, False)
        off = bool(win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
                   & WS_EX_TOPMOST)
        print("topmost_on", on, "| topmost_off", not off,
              "| both_directions_ok", on and not off)
    finally:
        try:
            win32gui.DestroyWindow(hwnd)
            win32gui.UnregisterClass("BatteryLensCheckProbe", wc.hInstance)
        except Exception:
            pass

    print("[5] 崩溃兜底")
    before = os.path.getsize(LOG) if os.path.exists(LOG) else 0
    original = globals().get("_wnd_proc")

    def boom(hwnd, msg, wp, lp):
        raise RuntimeError("run_checks: injected wnd_proc failure")

    globals()["_wnd_proc"] = boom
    survived = True
    try:
        wnd_proc(0, 0, 0, 0)
    except Exception as e:
        survived = False
        print("wnd_proc_raised", e)
    finally:
        if original is not None:
            globals()["_wnd_proc"] = original
    print("wnd_proc_survived_injected_exception", survived)

    def bad_thread():
        raise RuntimeError("run_checks: injected thread failure")

    t = threading.Thread(target=bad_thread, daemon=True, name="CheckProbe")
    t.start()
    t.join(2)
    after_size = os.path.getsize(LOG) if os.path.exists(LOG) else 0
    with open(LOG, "r", encoding="utf-8") as f:
        tail = f.read()[-4000:]
    print("thread_exception_survived", not t.is_alive(),
          "| log_written", after_size > before,
          "| log_has_injected", "injected" in tail)

    print("settings_saved:", load_settings())
    print("=" * 52)


def main():
    """入口：单实例锁 → 设置 → 首次扫描 → 建窗 → 消息循环。"""
    global HWND, S
    hide_console()
    guard = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        guard.bind(("127.0.0.1", 8766))
        guard.listen(1)
    except OSError:
        guard.close()
        return
    STATE["priority"] = set_low_priority()
    STATE.update(load_settings())
    tray_win.set_theme(effective_theme())
    apply_palette()
    STATE["data"] = collector.scan()
    update_rate_history(STATE["data"])
    STATE["last_scan"] = time.time()
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
    S = dpi_scale()

    wc = win32gui.WNDCLASS()
    wc.lpfnWndProc = wnd_proc
    wc.hInstance = win32api.GetModuleHandle(None)
    wc.lpszClassName = "BatteryLensWidget"
    wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
    atom = win32gui.RegisterClass(wc)
    # 不带 WS_VISIBLE：先定位到右下角再由 apply_mode 决定显示，
    # 避免启动瞬间在左上角 (0,0) 闪现一个 80px 的小方块。
    hwnd = win32gui.CreateWindowEx(
        win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_NOACTIVATE,
        atom, "BatteryLens", win32con.WS_POPUP,
        0, 0, PILL_W, PILL_H, 0, 0, wc.hInstance, None)
    HWND = hwnd
    apply_size(hwnd)
    if STATE["topmost"]:
        apply_topmost(hwnd, True)
    STATE["busy"] = user_busy()
    apply_mode(hwnd)          # 内部会建立常驻程序主图标并按需启动轮换
    _USER32.SetTimer(ctypes.c_void_p(int(hwnd)), 1,
                     WATCH_BUSY_MS if STATE["busy"] else WATCH_MS, None)
    win32gui.PumpMessages()


if __name__ == "__main__":
    try:
        if "--check" in sys.argv:
            run_checks()
        else:
            main()
    except Exception as e:
        log_error("entrypoint crashed", e)
