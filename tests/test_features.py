"""续航估算 + 充电管家状态机验证（合成数据 / 打桩，无窗口）。"""
import sys
import time

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import widget

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL "), name)


NOW = time.time()

# ---------- 续航估算 ----------
widget.STATE["rate_hist"] = {
    # 稳定放电：100 -> 91 跨 30 分钟 => 约 -18%/h
    "a": [(NOW - 1800, 100), (NOW - 1200, 97), (NOW - 600, 94), (NOW, 91)],
    # 粒度噪声：只掉 1%，不估算
    "b": [(NOW - 1800, 50), (NOW - 600, 50), (NOW, 49)],
    # 上升（充电中）：不估算
    "c": [(NOW - 1800, 60), (NOW - 600, 65), (NOW, 70)],
    # 样本太少
    "d": [(NOW - 300, 80), (NOW, 79)],
}
dev = {"id": "a", "name": "A", "battery": 91}
t = widget.time_left_text(dev)
print("  估算输出:", repr(t))
check("稳定放电给出估算", t.startswith("约剩") and "5h" in t)
check("粒度噪声不估算", widget.time_left_text({"id": "b", "battery": 49}) == "")
check("充电中不估算", widget.time_left_text({"id": "c", "battery": 70}) == "")
check("样本不足不估算", widget.time_left_text({"id": "d", "battery": 79}) == "")
low_dev = {"id": "a", "battery": 2}
check("临近没电提示", widget.time_left_text(low_dev) == "约剩不到半小时")
# 窗口裁剪：超 40 分钟的点被丢弃；间隔 >60s 的新读数正常入库
hist = [(NOW - 4000, 99), (NOW - 1800, 100), (NOW - 600, 97), (NOW - 100, 94)]
widget.STATE["rate_hist"]["w"] = hist
widget.update_rate_history({"devices": [
    {"id": "w", "battery": 93, "status": "real"}]})
pts = widget.STATE["rate_hist"]["w"]
check("窗口裁剪+去重", pts[-1][1] == 93 and pts[0][1] == 100 and len(pts) == 4)

# ---------- 充电管家 ----------
toasts = []
widget.tray_win.notify_app = lambda hwnd, title, text: toasts.append(title) or True
widget.STATE["charge_seen"] = {}
widget.STATE["aliases"] = {}
widget.STATE["data"] = {"devices": [
    {"id": "pc", "name": "本机电池", "battery": 60, "status": "real",
     "charging": True}]}
widget.check_charging(None)
check("插电提醒一次", toasts == ["充电中 · 本机电池"])
widget.STATE["data"]["devices"][0]["battery"] = 100
widget.check_charging(None)
check("充满提醒", toasts[-1] == "已充满 · 本机电池")
widget.check_charging(None)
check("充满不重复", len(toasts) == 2)
widget.STATE["data"]["devices"][0]["charging"] = False
widget.check_charging(None)
widget.STATE["data"]["devices"][0]["charging"] = True
widget.STATE["data"]["devices"][0]["battery"] = 55
widget.check_charging(None)
check("拔掉后重新布防", toasts[-1] == "充电中 · 本机电池")

# 外设上升沿推断（文案如实说「电量正在回升」）
toasts.clear()
widget.STATE["charge_seen"] = {}
widget.STATE["data"] = {"devices": [
    {"id": "m", "name": "鼠标", "battery": 45, "status": "real"}]}
widget.check_charging(None)
check("外设电量持平不提醒", toasts == [])
widget.STATE["data"]["devices"][0]["battery"] = 46
widget.check_charging(None)
check("外设回升提醒", toasts == ["充电中 · 鼠标"])
widget.STATE["data"]["devices"][0]["battery"] = 55
widget.check_charging(None)
check("回升中不重复", len(toasts) == 1)
widget.STATE["data"]["devices"][0]["battery"] = 54
widget.check_charging(None)
widget.STATE["data"]["devices"][0]["battery"] = 53
widget.check_charging(None)
check("回落重置", toasts == ["充电中 · 鼠标"])
widget.STATE["data"]["devices"][0]["battery"] = 100
widget.check_charging(None)
check("回落再回升到100提醒充满", toasts[-1] == "已充满 · 鼠标")

print("\nRESULT:", "ALL PASS" if ok else "HAS FAILURES")
sys.exit(0 if ok else 1)
