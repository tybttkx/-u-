# -*- coding: utf-8 -*-
"""把默认「录音设备」临时切到回环设备（立体声混音 / VB-Cable），用完恢复。

跟读题要求录音内容就是示范音：让系统把"正在播放的声音"当麦克风输入，
浏览器录到的就是示范音的数字副本 —— 没有环境噪音，也不依赖播放时机。

实现：直接调 CoreAudio 的 IMMDeviceEnumerator 与 IPolicyConfig::SetDefaultEndpoint。
设备识别：先认内置设备 id（开发机），认不出就按设备名找（CABLE Input / CABLE Output，
再退「立体声混音」）—— 换一台机器不用改代码。
兜底：切换前记录原始默认设备，atexit 恢复（强杀/断电后下次运行 use_real_devices 也会拉回）；
多实例并行用临时文件锁做非阻塞检测（检测到只警告，不阻止）。
"""

import atexit
import ctypes
import logging
import time
import os
import tempfile
from ctypes import POINTER, byref, c_void_p, c_wchar_p
from typing import List, Tuple

logger = logging.getLogger(__name__)

CLSCTX_ALL = 0x17


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", ctypes.c_ulong)]


class _PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                ("r3", ctypes.c_ushort), ("pwszVal", c_wchar_p)]


def _guid(data1, data2, data3, data4):
    return _GUID(data1, data2, data3, (ctypes.c_ubyte * 8)(*data4))


CLSID_MMDeviceEnumerator = _guid(0xBCDE0395, 0xE52F, 0x467C,
                                 (0x8E, 0x3D, 0xC4, 0x57, 0x92, 0x91, 0x69, 0x2E))
IID_IMMDeviceEnumerator = _guid(0xA95664D2, 0x9614, 0x4F35,
                                (0xA7, 0x46, 0xDE, 0x8D, 0xB6, 0x36, 0x17, 0xE6))
CLSID_CPolicyConfigClient = _guid(0x870AF99C, 0x171D, 0x4F9E,
                                  (0xAF, 0x0D, 0xE6, 0x3D, 0xF4, 0x0C, 0x2B, 0xC9))
IID_IPolicyConfig = _guid(0xF8679F50, 0x850A, 0x41CF,
                          (0x9C, 0x72, 0x43, 0x08, 0x29, 0x30, 0x3C, 0xBD))
#: Win10/11 上 CPolicyConfigClient 常创建失败（0x80040154），Vista 版通常可用
CLSID_CPolicyConfigVistaClient = _guid(0x294935CE, 0xF637, 0x4E7C,
                                       (0xA4, 0x1B, 0xAB, 0x25, 0x54, 0x60, 0xB8, 0x62))
IID_IPolicyConfigVista = _guid(0x568B9108, 0x44BF, 0x40B4,
                               (0x90, 0x06, 0x86, 0xAF, 0xE5, 0xB5, 0xA6, 0x20))
PKEY_Device_FriendlyName = _PROPERTYKEY(
    _guid(0xA45C254E, 0xDF1C, 0x4EFD,
          (0x80, 0x20, 0x67, 0xD1, 0x46, 0xA8, 0x50, 0xE0)), 14)

eCapture, eConsole = 1, 0


def _method(interface_ptr, index, restype, *argtypes):
    """取 COM 接口第 index 个虚函数。

    接口指针指向「指向虚函数表的指针」，要先解一层拿到虚表地址，
    再把函数地址转成 int 交给 WINFUNCTYPE —— 直接把 c_void_p 对象传进去
    会被当成"该对象的地址"，就会崩成 access violation。
    """
    p = ctypes.cast(interface_ptr, POINTER(c_void_p))
    table_addr = p[0]
    if isinstance(table_addr, c_void_p):
        table_addr = table_addr.value
    table = ctypes.cast(c_void_p(table_addr), POINTER(c_void_p))
    fn_addr = table[index]
    if isinstance(fn_addr, c_void_p):
        fn_addr = fn_addr.value
    return ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)(fn_addr)


def _co_create(clsid, iid):
    # 先 CoInitialize：同一线程没初始化过 COM 时 CoCreateInstance 会返回
    # 0x800401F0（CO_E_NOTINITIALIZED）—— 之前切设备失败就是这个原因。
    try:
        ctypes.windll.ole32.CoInitialize(None)
    except Exception:
        pass
    ptr = c_void_p()
    hr = ctypes.windll.ole32.CoCreateInstance(
        byref(clsid), None, CLSCTX_ALL, byref(iid), byref(ptr))
    if hr != 0:
        raise OSError(f"CoCreateInstance 失败 0x{hr & 0xFFFFFFFF:08X}")
    return ptr


def _enumerator():
    ctypes.windll.ole32.CoInitialize(None)
    return _co_create(CLSID_MMDeviceEnumerator, IID_IMMDeviceEnumerator)


def _device_name(device) -> str:
    try:
        store = c_void_p()
        _method(device, 4, ctypes.c_long, ctypes.c_int, ctypes.c_int,
                POINTER(c_void_p))(device, 0, CLSCTX_ALL, byref(store))
        if not store:
            return ""
        pv = _PROPVARIANT()
        hr = _method(store, 5, ctypes.c_long, POINTER(_PROPERTYKEY),
                     POINTER(_PROPVARIANT))(store, byref(PKEY_Device_FriendlyName), byref(pv))
        if hr == 0 and pv.pwszVal:
            return str(pv.pwszVal)
    except Exception:
        pass
    return ""


def _device_id(device) -> str:
    try:
        ptr = c_wchar_p()
        _method(device, 5, ctypes.c_long, POINTER(c_wchar_p))(device, byref(ptr))
        return str(ptr.value or "")
    except Exception:
        return ""


def default_capture_device() -> Tuple[str, str]:
    """当前默认录音设备 (id, 名字)。"""
    try:
        enum = _enumerator()
        device = c_void_p()
        hr = _method(enum, 4, ctypes.c_long, ctypes.c_int, ctypes.c_int,
                     POINTER(c_void_p))(enum, eCapture, eConsole, byref(device))
        if hr == 0 and device:
            return _device_id(device), _device_name(device)
    except Exception as exc:
        logger.warning("读取默认录音设备失败: %s", exc)
    return "", ""


def set_default_capture(device_id: str) -> bool:
    """把某个录音端点设为默认（eConsole/多媒体/通信三个角色都设）。"""
    if not device_id:
        return False
    policy = None
    for clsid, iid, tag in ((CLSID_CPolicyConfigClient, IID_IPolicyConfig, "Client"),
                            (CLSID_CPolicyConfigVistaClient, IID_IPolicyConfigVista, "Vista")):
        try:
            policy = _co_create(clsid, iid)
            if policy:
                break
        except Exception as exc:
            logger.warning("IPolicyConfig(%s) 创建失败: %s", tag, exc)
    if not policy:
        return False
    ok = False
    for role in (0, 1, 2):                      # eConsole / eMultimedia / eCommunications
        try:
            if _method(policy, 3, ctypes.c_long, c_wchar_p, ctypes.c_int)(
                    policy, device_id, role) == 0:
                ok = True
        except Exception:
            pass
    return ok


def _default_id(flow: int) -> str:
    try:
        enum = _enumerator()
        dev = ctypes.c_void_p()
        if _method(enum, 4, ctypes.c_long, ctypes.c_int, ctypes.c_int,
                   ctypes.POINTER(ctypes.c_void_p))(enum, flow, eConsole, ctypes.byref(dev)) == 0 and dev:
            return _device_id(dev)
    except Exception:
        pass
    return ""


def switch_default(dev_id: str, flow: int) -> bool:
    """切默认端点并按读回结果判定是否成功。

    这台机器上 IPolicyConfig::SetDefaultEndpoint 会返回非 0，但**实际已经生效**
    （实测：报告失败、读回的默认播放设备却真的变了），所以判定改成"切换后读回比对"。
    """
    if not dev_id:
        return False
    try:
        set_default_capture(dev_id) if flow == eCapture else _set_render_default_hack(dev_id)
    except Exception:
        pass
    for _ in range(6):
        if _default_id(flow) == dev_id:
            return True
        time.sleep(0.5)
    return _default_id(flow) == dev_id


def _set_render_default_hack(dev_id: str) -> bool:
    policy = None
    for clsid, iid in ((CLSID_CPolicyConfigClient, IID_IPolicyConfig),
                       (CLSID_CPolicyConfigVistaClient, IID_IPolicyConfigVista)):
        try:
            policy = _co_create(clsid, iid)
            if policy:
                break
        except Exception:
            policy = None
    if not policy:
        return False
    for role in (0, 1, 2):
        try:
            _method(policy, 3, ctypes.c_long, ctypes.c_wchar_p, ctypes.c_int)(policy, dev_id, role)
        except Exception:
            pass
    return True


#: 已知设备 id（开发机上设备名读不到，只能用 id 记）。
#: 换一台机器这些 id 自然不存在 —— 那时按下面的「名字」找设备（装了 VB-Cable
#: 就有 CABLE Input / CABLE Output 两个端点），所以这两组常量只是「先试哪个」。
CABLE_OUT_ID = "{0.0.1.00000000}.{c6b906d6-98d2-4f43-8192-a7bc348befdd}"
CABLE_IN_ID = "{0.0.0.00000000}.{4d2c6df3-0171-4b09-94e8-18e66d52ff3f}"  # 2026-10-03 实机枚举补全（原值少了尾巴 -3f}，"播放→CABLE"从未成功）
REAL_MIC_ID = "{0.0.1.00000000}.{3cfe3212-991c-477f-9cba-840c394bcd56}"
REAL_SPK_ID = "{0.0.0.00000000}.{c0a8f4f7-f9fc-42c6-82a4-be77ad9eea4b}"

#: 按设备名兜底（VB-Cable 装好后，播放端叫 CABLE Input，录音端叫 CABLE Output）
LOOPBACK_RENDER_KEYWORDS = ("CABLE Input",)
LOOPBACK_CAPTURE_KEYWORDS = ("CABLE Output",)
#: 没装 VB-Cable 时，Realtek/其它声卡自带的「立体声混音」也能当回环录音设备
STEREO_MIX_KEYWORDS = ("立体声混音", "stereo mix")
#: 找真实设备时优先的名字（识别顺序用，不命中也不影响）
SPEAKER_KEYWORDS = ("扬声器", "speaker", "耳机", "headphone", "headset")
MIC_KEYWORDS = ("麦克风", "microphone")
#: 下面这些名字一律算「回环设备」，恢复真实设备时要避开
_LOOPBACK_KEYWORDS = (LOOPBACK_RENDER_KEYWORDS + LOOPBACK_CAPTURE_KEYWORDS
                      + STEREO_MIX_KEYWORDS + ("cable",))


#: 切换前的原始默认设备（播放 id, 录音 id）—— 进程内第一次切换前记录，供兜底恢复
_saved_defaults: Tuple[str, str] = ("", "")
_defaults_saved = False
_atexit_registered = False
#: 多实例互斥用的临时锁文件（msvcrt.locking）
_INSTANCE_LOCK_PATH = os.path.join(tempfile.gettempdir(), "unipus_audio_route.lock")
_lock_handle = None


def _save_original_defaults() -> None:
    """记录切换前的原始默认设备（只记一次）。"""
    global _saved_defaults, _defaults_saved
    if _defaults_saved:
        return
    cap_id, _cap_name = default_capture_device()
    _saved_defaults = (_default_id(0), cap_id)
    _defaults_saved = True
    logger.info("已记录切换前的默认设备：播放=%s 录音=%s", _saved_defaults[0], _saved_defaults[1])


def _restore_original_defaults() -> None:
    """把默认设备恢复到切换前的原始设备（atexit 兜底）。

    强杀 / 断电收不到这个回调（所以 use_real_devices 不依赖它），
    但正常退出与未捕获异常退出都会走到 —— 避免用户机器停在 CABLE 上"没声音"。
    """
    if not _defaults_saved:
        return
    render_id, capture_id = _saved_defaults
    restored = False
    try:
        if render_id and _default_id(0) != render_id:
            restored = switch_default(render_id, 0) or restored
        if capture_id and _default_id(1) != capture_id:
            restored = switch_default(capture_id, 1) or restored
    except Exception as exc:
        logger.warning("退出兜底恢复默认设备失败: %s", exc)
    if restored:
        print("     录音设备：退出兜底 —— 已恢复切换前的默认设备")


def _ensure_exit_restore() -> None:
    """注册 atexit 兜底恢复（只注册一次）。"""
    global _atexit_registered
    if not _atexit_registered:
        _atexit_registered = True
        atexit.register(_restore_original_defaults)


def _instance_lock_check(verbose: bool = True) -> bool:
    """非阻塞检测是否已有另一个实例在切换默认设备（msvcrt.locking 锁临时文件）。

    检测到占用时只打印警告、不阻止本实例（保持简单，避免锁死自己）。
    """
    global _lock_handle
    if _lock_handle is not None:
        return True
    try:
        import msvcrt
        fh = open(_INSTANCE_LOCK_PATH, "a+")
        try:
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                fh.write(str(os.getpid()))
                fh.flush()
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            fh.close()
            if verbose:
                print("     录音设备：另一个实例也在切换默认设备（继续执行，别同时跑录音题）")
            return False
        _lock_handle = fh
        return True
    except Exception as exc:
        if verbose:
            print(f"     录音设备：实例锁不可用（{str(exc)[:40]}），继续")
        return False


_MMDEVICES_KEY = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\%s\%s\Properties")
#: 端点名字在注册表里有两个候选值：友好名、设备描述（开发机上友好名为空、描述有值）
_PKEY_DEVICE_NAME = ("{a45c254e-df1c-4efd-8020-67d146a850e0},14",
                     "{a45c254e-df1c-4efd-8020-67d146a850e0},2")


def _registry_device_name(device_id: str, flow: int) -> str:
    """从注册表读端点名字；读不到返回空串。

    COM 的 IPropertyStore 在部分机器上返回空名（开发机就是），但注册表这份一直有，
    名字兜底识别（CABLE Input / CABLE Output / 立体声混音）全靠它。
    device_id 形如 {0.0.0.00000000}.{4d2c6df3-…}，注册表键名只用后一段 GUID。
    """
    if not device_id:
        return ""
    guid = device_id.rsplit(".", 1)[-1]
    path = _MMDEVICES_KEY % ("Render" if flow == 0 else "Capture", guid)
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            for value_name in _PKEY_DEVICE_NAME:
                try:
                    value = winreg.QueryValueEx(key, value_name)[0]
                except OSError:
                    continue
                if value and str(value).strip():
                    return str(value).strip()
    except Exception:
        pass
    return ""


def _enum_active(flow: int) -> List[Tuple[str, str]]:
    """当前活动端点 [(id, 名字), ...]（COM 读不到名字时用注册表补上）。"""
    out: List[Tuple[str, str]] = []
    try:
        enum = _enumerator()
        coll = c_void_p()
        hr = _method(enum, 3, ctypes.c_long, ctypes.c_int, ctypes.c_int,
                     POINTER(c_void_p))(enum, flow, 0x1, byref(coll))
        if hr != 0 or not coll:
            return out
        count = ctypes.c_uint()
        _method(coll, 3, ctypes.c_long, POINTER(ctypes.c_uint))(coll, byref(count))
        for i in range(count.value):
            dev = c_void_p()
            hr = _method(coll, 4, ctypes.c_long, ctypes.c_uint,
                         POINTER(c_void_p))(coll, i, byref(dev))
            if hr == 0 and dev:
                dev_id = _device_id(dev)
                name = _device_name(dev) or _registry_device_name(dev_id, flow)
                out.append((dev_id, name))
    except Exception as exc:
        logger.warning("枚举音频端点失败: %s", exc)
    return out


def _resolve_device(device_id: str, flow: int) -> str:
    """设备 id 常量可能过期或截断（本文件的 CABLE_IN_ID 就漏过尾巴）：

    失配时按前缀在活动端点里找回完整 id；找不回就原样返回（切换自会失败并如实报告）。
    """
    if not device_id:
        return device_id
    devices = _enum_active(flow)
    if not devices:
        return device_id
    for full_id, _name in devices:
        if full_id == device_id:
            return device_id
    for full_id, _name in devices:
        if full_id.startswith(device_id):
            logger.info("设备 id %s 已过期/截断，按前缀找回 %s", device_id, full_id)
            print(f"     录音设备：id 常量已过期/截断，按前缀找回 {full_id[-14:]}")
            return full_id
    return device_id


def _find_by_name(keywords: Tuple[str, ...], flow: int) -> str:
    """在当前活动端点里按名字找第一个匹配的；名字读不到（返回空串）就没有结果。"""
    for dev_id, name in _enum_active(flow):
        low = (name or "").lower()
        if low and any(word.lower() in low for word in keywords):
            return dev_id
    return ""


def _active_ids(flow: int) -> List[str]:
    return [dev_id for dev_id, _name in _enum_active(flow)]


def _resolve_builtin(known_id: str, keywords: Tuple[str, ...], flow: int,
                     verbose: bool = False) -> str:
    """先认内置 id，认不出（换机器了）再按名字找；都找不到返回空串。

    只按名字找得到的时候打一行日志 —— 明确告诉用户"用的是名字兜底"，
    免得后面切换失败时不知道是哪一步不合预期。
    """
    resolved = _resolve_device(known_id, flow)
    if resolved in _active_ids(flow):
        return resolved
    found = _find_by_name(keywords, flow)
    if found and verbose:
        print(f"     录音设备：内置 id 在本机不存在，按名字找到「{keywords[0]}」")
    return found


def _pick_real_device(known_id: str, flow: int) -> str:
    """恢复真实设备时用的目标：常见名字（扬声器 / 麦克风）→ 内置 id → 第一个非回环端点。

    名字优先：注册表能读到名字，而内置 id 的标签并不完全可靠
    （开发机的 REAL_MIC_ID 实际指向「立体声混音」而不是麦克风）。
    """
    picked = _find_by_name(SPEAKER_KEYWORDS if flow == 0 else MIC_KEYWORDS, flow)
    if picked:
        return picked
    resolved = _resolve_device(known_id, flow)
    if resolved in _active_ids(flow):
        return resolved
    for dev_id, name in _enum_active(flow):
        if not any(word.lower() in (name or "").lower() for word in _LOOPBACK_KEYWORDS):
            return dev_id
    return known_id


def use_vb_cable(verbose: bool = True) -> bool:
    """录音题前：播放→回环播放端、录音→回环录音端。

    首选 VB-Cable（CABLE Input / CABLE Output）：设备 id 认不出时按名字找；
    没装 VB-Cable 就退回「立体声混音」，这时只切录音设备、播放保持原样
    （立体声混音录的是"系统正在播放的声音"，不需要额外接线）。

    切换前记录原始默认设备，只要动过默认设备就挂 atexit 兜底恢复（只挂一次）。
    返回：至少一项切换成功 → True（调用方据此仍会触发恢复）；全失败 → False。
    """
    _instance_lock_check(verbose)
    _save_original_defaults()
    render_id = _resolve_builtin(CABLE_IN_ID, LOOPBACK_RENDER_KEYWORDS, 0, verbose)
    capture_id = _resolve_builtin(CABLE_OUT_ID, LOOPBACK_CAPTURE_KEYWORDS, 1, verbose)
    stereo_mix = False
    if not capture_id:
        capture_id = _find_by_name(STEREO_MIX_KEYWORDS, 1)
        stereo_mix = bool(capture_id)
    if stereo_mix and verbose:
        print("     VB-Cable：未找到，改用「立体声混音」当录音输入（只切录音，播放不动）")

    ok_in = switch_default(render_id, 0) if (render_id and not stereo_mix) else False
    ok_out = switch_default(capture_id, 1) if capture_id else False
    if ok_in or ok_out:
        _ensure_exit_restore()
    if verbose:
        if ok_in and ok_out:
            print("     VB-Cable：播放 ✔ 录音 ✔")
        elif ok_in or ok_out:
            print(f"     VB-Cable：部分成功（播放{'✔' if ok_in else '✘'} 录音{'✔' if ok_out else '✘'}）"
                  "—— 未完全切换，恢复仍会执行")
        else:
            print("     VB-Cable：播放 ✘ 录音 ✘（未切换，保持原设备）")
            print("     （没装虚拟声卡？装 VB-Cable 后回环录音才可用，见使用手册第五节）")
    return ok_in or ok_out


def use_real_devices(verbose: bool = True) -> bool:
    """跑完恢复：切回「切换前的原始默认设备」。

    不再依赖"曾认为切换成功"—— 强杀后新进程没有记录，机器可能还停在 CABLE 上，
    这里照样往真实设备切：没记录过原始设备时，按内置 id → 常见名字 → 非回环端点
    依次找目标（换机器后内置 id 不存在，靠后两级兜底）。
    """
    render_id, capture_id = _saved_defaults
    target_render = render_id or _pick_real_device(REAL_SPK_ID, 0)
    target_capture = capture_id or _pick_real_device(REAL_MIC_ID, 1)
    # 已经是目标设备就别再切（省掉读回比对的等待，也不惊动用户正在用的设备）
    ok_render = (True if _default_id(0) == target_render
                 else switch_default(target_render, 0))
    ok_capture = (True if _default_id(1) == target_capture
                  else switch_default(target_capture, 1))
    if verbose:
        src = "切换前记录" if (render_id or capture_id) else "已知真实设备 id（本进程没记录过原设备）"
        print(f"     真实设备：播放{'✔' if ok_render else '✘'} 录音{'✔' if ok_capture else '✘'}（{src}）")
    return ok_render or ok_capture
