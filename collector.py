"""设备电量采集层。

原则：所有百分比都来自设备真实上报，绝不生成模拟数据。
读不到就明确标注原因（协议未公开 / 设备离线 / 系统不暴露）。
"""
import asyncio
import ctypes
import json
import subprocess
import time
from ctypes import wintypes
from datetime import datetime

BATTERY_SERVICE = "0000180f"
BATTERY_LEVEL = "00002a19"

# ---- Windows PnP 电量属性 ------------------------------------------------
# DEVPKEY_Device_BatteryLevel = {104EA319-6EE2-4701-BD47-8DDBF425BBE5} 2。
# Windows 11「蓝牙和设备」页面显示的百分比就来自这个属性。经典蓝牙耳机（HFP）
# 的电量挂在 Class=System 的 Hands-Free AG 节点，不在 BLE GATT 0x180F 里。
# 用原生 SetupAPI 直读：枚举约 250 台设备只需约 6ms，无子进程、无编码问题。
class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]


class _DEVPROPKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", ctypes.c_uint32)]


class _SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("ClassGuid", _GUID),
                ("DevInst", ctypes.c_uint32), ("Reserved", ctypes.c_void_p)]


_PKEY_BATTERY = _DEVPROPKEY(
    _GUID(0x104EA319, 0x6EE2, 0x4701,
          (ctypes.c_ubyte * 8)(0xBD, 0x47, 0x8D, 0xDB, 0xF4, 0x25, 0xBB, 0xE5)),
    2)

_SETUPAPI = ctypes.WinDLL("setupapi", use_last_error=True)
_SETUPAPI.SetupDiGetClassDevsW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                           wintypes.HWND, wintypes.DWORD]
_SETUPAPI.SetupDiGetClassDevsW.restype = ctypes.c_void_p
_SETUPAPI.SetupDiEnumDeviceInfo.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                            ctypes.POINTER(_SP_DEVINFO_DATA)]
_SETUPAPI.SetupDiEnumDeviceInfo.restype = wintypes.BOOL
_SETUPAPI.SetupDiGetDevicePropertyW.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(_SP_DEVINFO_DATA),
    ctypes.POINTER(_DEVPROPKEY), ctypes.POINTER(ctypes.c_uint32),
    ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
    wintypes.DWORD]
_SETUPAPI.SetupDiGetDevicePropertyW.restype = wintypes.BOOL
_SETUPAPI.SetupDiGetDeviceRegistryPropertyW.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(_SP_DEVINFO_DATA), ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p, ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_uint32)]
_SETUPAPI.SetupDiGetDeviceRegistryPropertyW.restype = wintypes.BOOL
_SETUPAPI.SetupDiGetDeviceInstanceIdW.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(_SP_DEVINFO_DATA), wintypes.LPWSTR,
    ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
_SETUPAPI.SetupDiGetDeviceInstanceIdW.restype = wintypes.BOOL
_SETUPAPI.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
_SETUPAPI.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL
# ------------------------------------------------------------------------


def _kind(name):
    n = (name or "").lower()
    if "mouse" in n:
        return "mouse", "鼠标"
    if any(k in n for k in ("keyboard", "68 air", "hero", "kbd", "keychron")):
        return "keyboard", "键盘"
    if any(k in n for k in ("edifier", "head", "fit900", "buds", "ear", "airpod")):
        return "headset", "耳机"
    if "controller" in n or "xbox" in n or "gamepad" in n:
        return "gamepad", "手柄"
    return "device", "设备"


def _buf_bytes(buf):
    from winrt.windows.storage.streams import DataReader
    try:
        r = DataReader.from_buffer(buf)
        return bytes(r.read_bytes(r.unconsumed_buffer_length))
    except Exception:
        try:
            return bytes(buf)
        except Exception:
            return b""


def _mac_from_id(dev_id):
    tail = (dev_id or "").rsplit("-", 1)[-1]
    return tail if ":" in tail else ""


async def _ble_battery(log):
    """已配对 BLE 设备：WinRT 直连 GATT，读标准 Battery Level 0x2A19。"""
    out = []
    try:
        from winrt.windows.devices.bluetooth import BluetoothLEDevice
        from winrt.windows.devices.enumeration import DeviceInformation

        sel = BluetoothLEDevice.get_device_selector_from_pairing_state(True)
        devs = await DeviceInformation.find_all_async(sel, [])
        log.append("WinRT 枚举已配对 BLE 设备：%d 台" % len(devs))

        for d in devs:
            name = d.name or "未命名 BLE 设备"
            kind, kind_cn = _kind(name)
            item = {
                "id": "ble:" + _mac_from_id(d.id),
                "name": name, "kind": kind, "kind_cn": kind_cn,
                "link": "蓝牙 LE", "address": _mac_from_id(d.id),
                "battery": None, "status": "unknown",
                "source": "BluetoothLEDevice.FromIdAsync", "note": "",
            }
            try:
                ble = await BluetoothLEDevice.from_id_async(d.id)
                if ble is None:
                    item["note"] = "系统返回空设备对象，可能已休眠"
                    log.append("  %s：设备休眠" % name)
                else:
                    try:
                        res = await ble.get_gatt_services_async()
                        log.append("  %s：GATT 服务 %d 项" % (name, len(res.services)))
                        for s in res.services:
                            if BATTERY_SERVICE not in str(s.uuid).lower():
                                continue
                            cres = await s.get_characteristics_async()
                            for ch in cres.characteristics:
                                if BATTERY_LEVEL not in str(ch.uuid).lower():
                                    continue
                                r = await ch.read_value_async()
                                raw = _buf_bytes(r.value)
                                if raw:
                                    item["battery"] = int(raw[0])
                                    item["status"] = "real"
                                    item["source"] = "GATT 0x180F / 0x2A19"
                                    item["note"] = "标准电池服务，数值由设备直接报告"
                                    log.append("  %s：读到 %d%%" % (name, item["battery"]))
                        if item["status"] != "real":
                            item["status"] = "no-service"
                            item["note"] = "设备未提供标准 0x180F 电池服务"
                    finally:
                        try:
                            ble.close()
                        except Exception:
                            pass
            except Exception as e:
                item["status"] = "error"
                item["note"] = type(e).__name__
                log.append("  %s：连接失败 %s" % (name, type(e).__name__))
            out.append(item)
    except Exception as e:
        log.append("BLE 采集异常：%s %s" % (type(e).__name__, str(e)[:100]))
    return out


async def _classic_bluetooth(log):
    """已配对经典蓝牙设备台账：系统不暴露电量，只做识别与诚实标注。"""
    out = []
    names = []
    try:
        from winrt.windows.devices.bluetooth import BluetoothDevice
        from winrt.windows.devices.enumeration import DeviceInformation
        sel = BluetoothDevice.get_device_selector_from_pairing_state(True)
        devs = await DeviceInformation.find_all_async(sel, [])
        names = [d.name for d in devs if d.name]
        log.append("WinRT 枚举已配对经典蓝牙：%d 台" % len(names))
    except Exception:
        try:
            cmd = ("Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | "
                   "Select-Object -ExpandProperty FriendlyName | ConvertTo-Json -Compress")
            p = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                               capture_output=True, text=True, timeout=20)
            raw = json.loads(p.stdout or "[]")
            names = raw if isinstance(raw, list) else [raw]
            log.append("PowerShell PnP 枚举蓝牙设备：%d 项" % len(names))
        except Exception as e:
            log.append("经典蓝牙枚举失败：%s" % type(e).__name__)

    skip = ("{0000", "枚举器", "通用属性", "设备标识服务", "设备信息服务",
            "RFCOMM", "Avrcp", "传输", "配置文件", "HID 设备")
    seen = set()
    for name in names:
        if not name or any(k in name for k in skip):
            continue
        base = name.replace("MY的", "").strip()
        if base in seen:
            continue
        seen.add(base)
        kind, kind_cn = _kind(base)
        out.append({
            "id": "bt:" + base, "name": base, "kind": kind, "kind_cn": kind_cn,
            "link": "经典蓝牙", "address": "", "battery": None,
            "status": "protocol",
            "source": "系统配对台账",
            "note": "电量走 HFP / 厂商私有协议，Windows 未向应用暴露",
        })
    return out


def _usb_hid(log):
    """USB HID 枚举：识别无线接收器 / 有线外设与厂商自定义接口。"""
    out = []
    try:
        import hid
        groups = {}
        for e in hid.enumerate():
            vid, pid = e.get("vendor_id"), e.get("product_id")
            if not vid or not pid:
                continue
            key = (vid, pid)
            g = groups.setdefault(key, {
                "vid": "%04X" % vid, "pid": "%04X" % pid,
                "vendor": e.get("manufacturer_string") or "",
                "product": e.get("product_string") or "",
                "ifaces": [], "vendor_ifaces": [],
            })
            up = e.get("usage_page") or 0
            iface = e.get("interface_number")
            if iface not in g["ifaces"]:
                g["ifaces"].append(iface)
            if up >= 0xFF00 and up <= 0xFFFF and iface not in [x["iface"] for x in g["vendor_ifaces"]]:
                g["vendor_ifaces"].append({"iface": iface, "page": "0x%04X" % up})
        for g in groups.values():
            name = (g["product"] or g["vendor"] or ("USB HID %s:%s" % (g["vid"], g["pid"]))).strip()
            vendor = g["vendor"]
            has_vendor = bool(g["vendor_ifaces"])
            if "RivieraWaves" in vendor or "BLE" in name.upper():
                link = "蓝牙 LE · HID 映射"
                status = "no-battery"
                base = "同一蓝牙设备在系统里的 HID 映射视图，电量已在上方 GATT 通道读取"
            elif "I2C" in name.upper() or "HIDI2C" in name.upper():
                link = "板载 I2C HID"
                status = "no-battery"
                base = "主板内置输入设备，无独立电池"
            elif not vendor:
                link = "系统 HID 映射"
                status = "no-battery"
                base = "芯片组 / 系统虚拟 HID 节点，非独立外设"
            else:
                link = "USB / 2.4G HID"
                status = "protocol"
                base = "可能是 2.4G 接收器挂载的无线外设，枚举信息无法确认连接方式"
            if has_vendor:
                note = base + "；检测到 %d 个厂商自定义接口 (%s)，电量需厂商私有协议，本次未解码" % (
                    len(g["vendor_ifaces"]), ", ".join(x["page"] for x in g["vendor_ifaces"]))
            else:
                note = base + "；标准 HID 通道，无电量特征"
            out.append({
                "id": "usb:%s:%s" % (g["vid"], g["pid"]),
                "name": name, "kind": "mouse" if "mouse" in name.lower() else "device",
                "kind_cn": "鼠标" if "mouse" in name.lower() else "USB 外设",
                "link": link, "address": "%s:%s" % (g["vid"], g["pid"]),
                "battery": None, "status": status,
                "source": "HID 枚举 · %d 个接口" % len(g["ifaces"]),
                "note": note, "vendor": vendor,
            })
        log.append("USB HID 枚举：%d 组设备" % len(out))
    except Exception as e:
        log.append("HID 枚举失败：%s %s" % (type(e).__name__, str(e)[:100]))
    return out


_PNP_SUFFIX = (" Hands-Free AG", " Hands-Free", " Handsfree",
               " Avrcp 传输", " Avrcp", " Headset", " 耳机")


def _pnp_clean(name):
    """把 PnP 节点名还原成设备友好名。"""
    s = (name or "").replace("MY的", "").strip()
    for suf in _PNP_SUFFIX:
        if s.lower().endswith(suf.lower()):
            s = s[: -len(suf)].strip()
            break
    return s


def _pnp_battery(log):
    """读取 Windows 已缓存的蓝牙设备电量（DEVPKEY_Device_BatteryLevel）。

    原生 SetupAPI，零子进程；只返回 0-100 的有效读数，绝不猜测或填充。
    """
    out = []
    total = 0
    try:
        h = _SETUPAPI.SetupDiGetClassDevsW(None, None, None, 0x2 | 0x4)
        if not h:
            log.append("PnP 设备枚举句柄获取失败")
            return out
        try:
            i = 0
            while True:
                di = _SP_DEVINFO_DATA()
                di.cbSize = ctypes.sizeof(_SP_DEVINFO_DATA)
                if not _SETUPAPI.SetupDiEnumDeviceInfo(h, i, ctypes.byref(di)):
                    break
                i += 1
                ptype = ctypes.c_uint32()
                buf = (ctypes.c_ubyte * 16)()
                need = ctypes.c_uint32()
                ok = _SETUPAPI.SetupDiGetDevicePropertyW(
                    h, ctypes.byref(di), ctypes.byref(_PKEY_BATTERY),
                    ctypes.byref(ptype), buf, ctypes.sizeof(buf),
                    ctypes.byref(need), 0)
                if not ok:
                    continue
                t = ptype.value & 0xFFFF
                if t == 3:      # DEVPROP_TYPE_BYTE
                    val = buf[0]
                elif t == 7:    # DEVPROP_TYPE_UINT32
                    val = ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint32)).contents.value
                elif t == 6:    # DEVPROP_TYPE_INT32
                    val = ctypes.cast(buf, ctypes.POINTER(ctypes.c_int32)).contents.value
                else:
                    continue
                if not (0 <= val <= 100):
                    continue
                nb = ctypes.create_unicode_buffer(256)
                _SETUPAPI.SetupDiGetDeviceRegistryPropertyW(
                    h, ctypes.byref(di), 0x0C, None, nb, ctypes.sizeof(nb), None)
                if not nb.value:
                    _SETUPAPI.SetupDiGetDeviceRegistryPropertyW(
                        h, ctypes.byref(di), 0x00, None, nb, ctypes.sizeof(nb), None)
                ib = ctypes.create_unicode_buffer(512)
                _SETUPAPI.SetupDiGetDeviceInstanceIdW(
                    h, ctypes.byref(di), ib, ctypes.sizeof(ib) // 2, None)
                out.append({
                    "name": _pnp_clean(nb.value), "raw": nb.value or "",
                    "battery": int(val), "instance_id": ib.value or "",
                    "prop_type": t,
                })
            total = i
        finally:
            _SETUPAPI.SetupDiDestroyDeviceInfoList(h)
        log.append("PnP 电量属性：枚举 %d 台，%d 台有有效读数" % (total, len(out)))
    except Exception as e:
        log.append("PnP 电量读取失败：%s %s" % (type(e).__name__, str(e)[:100]))
    return out


def _merge_pnp(devices, pnp, log):
    """把 PnP 电量回填进台账：已有 GATT 真实读数不覆盖，空白处补上。

    返回全新列表（逐项浅拷贝），避免回填污染 _STATIC 台账缓存。
    """
    devices = [dict(d) for d in devices]
    if not pnp:
        return devices
    filled, added = 0, 0
    for p in pnp:
        base = p.get("name") or ""
        if not base:
            continue
        key = base.lower()
        hit = None
        for d in devices:
            dn = (d.get("name") or "").lower()
            if (dn == key
                    or (len(key) >= 6 and key in dn)
                    or (len(dn) >= 6 and dn in key)):
                hit = d
                break
        if hit is not None:
            if hit.get("battery") is not None:
                continue
            hit["battery"] = p["battery"]
            hit["status"] = "real"
            hit["source"] = "Windows PnP · DEVPKEY_Device_BatteryLevel"
            hit["note"] = "Windows 蓝牙栈已上报电量（%s）" % (p["instance_id"][:52])
            filled += 1
        else:
            kind, kind_cn = _kind(base)
            devices.append({
                "id": "pnp:" + (p["instance_id"] or base)[:48],
                "name": base, "kind": kind, "kind_cn": kind_cn,
                "link": "蓝牙", "address": "",
                "battery": p["battery"], "status": "real",
                "source": "Windows PnP · DEVPKEY_Device_BatteryLevel",
                "note": "由 Windows 蓝牙栈上报的电量",
            })
            added += 1
    if filled or added:
        log.append("PnP 电量回填：补全 %d 台，新增 %d 台" % (filled, added))
    return devices


_STATIC = {"devices": [], "at": 0.0}
STATIC_TTL = 1800


def scan_light():
    """轻量采集：BLE 电量 + Windows PnP 电量，设备台账沿用缓存。

    PnP 电量走原生 SetupAPI（约 6ms），每次扫描都读，保证耳机等
    经典蓝牙设备的电量随 300 秒周期更新；不唤醒蓝牙枚举器与 USB HID 枚举。
    """
    t0 = time.time()
    log = []
    ble = asyncio.run(_ble_battery(log))
    pnp = _pnp_battery(log)
    now = time.time()
    if not _STATIC["devices"] or now - _STATIC["at"] > STATIC_TTL:
        static = asyncio.run(_classic_bluetooth(log)) + _usb_hid(log)
        _STATIC["devices"] = static
        _STATIC["at"] = now
        log.append("设备台账已刷新")
    else:
        static = list(_STATIC["devices"])
        log.append("设备台账沿用缓存")
    devices = _merge_pnp(ble + static, pnp, log)
    log.append("轻量扫描完成，%d 台设备，%d 台读到真实电量"
               % (len(devices), sum(1 for d in devices if d["status"] == "real")))
    return {
        "scan_time": datetime.now().strftime("%H:%M:%S"),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "duration_ms": int((time.time() - t0) * 1000),
        "devices": devices,
        "log": log,
    }


def scan():
    t0 = time.time()
    log = []
    ble = asyncio.run(_ble_battery(log))
    classic = asyncio.run(_classic_bluetooth(log))
    usb = _usb_hid(log)
    pnp = _pnp_battery(log)
    devices = _merge_pnp(ble + classic + usb, pnp, log)
    _STATIC["devices"] = classic + usb
    _STATIC["at"] = time.time()
    log.append("扫描完成，共 %d 台设备，其中 %d 台读到真实电量"
               % (len(devices), sum(1 for d in devices if d["status"] == "real")))
    return {
        "scan_time": datetime.now().strftime("%H:%M:%S"),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "duration_ms": int((time.time() - t0) * 1000),
        "devices": devices,
        "log": log,
    }


if __name__ == "__main__":
    print(json.dumps(scan(), ensure_ascii=False, indent=2))
