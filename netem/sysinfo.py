"""Интерфейсы, права администратора, ICMP-пинг, глобальная горячая клавиша, фильтр WinDivert."""
import ctypes
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from ctypes import wintypes

CREATE_NO_WINDOW = 0x08000000


# ---------------- права ----------------
def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin():
    """Перезапуск через UAC. True — новый процесс запущен."""
    if getattr(sys, "frozen", False):
        exe, args = sys.executable, subprocess.list2cmdline(sys.argv[1:])
    else:
        exe = sys.executable
        pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.isfile(pyw):
            exe = pyw
        args = subprocess.list2cmdline([os.path.abspath(sys.argv[0])] + sys.argv[1:])
    return ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, args, None, 1) > 32


# ---------------- интерфейсы ----------------
_PS_ADAPTERS = r"""
$ErrorActionPreference='SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$ips = Get-NetIPAddress | Where-Object { $_.AddressState -eq 'Preferred' }
$res = foreach ($a in Get-NetAdapter) {
  [pscustomobject]@{
    idx = $a.ifIndex; name = $a.Name; desc = $a.InterfaceDescription; status = [string]$a.Status
    speed = [string]$a.LinkSpeed
    ips = @($ips | Where-Object { $_.InterfaceIndex -eq $a.ifIndex } | ForEach-Object { $_.IPAddress })
  }
}
@($res) | ConvertTo-Json -Compress -Depth 3
"""


def list_interfaces():
    """[{idx, name, desc, status, speed, ips}], подключённые — сверху."""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_ADAPTERS],
                             capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW)
        text = out.stdout.decode("utf-8", errors="replace").strip()
        data = json.loads(text) if text else []
    except Exception:
        data = []
    if isinstance(data, dict):
        data = [data]
    for a in data:
        a["ips"] = [ip for ip in (a.get("ips") or []) if not ip.lower().startswith("fe80")]
    data.sort(key=lambda a: (a.get("status") != "Up", a.get("name", "")))
    return data


def ipv4_of(iface):
    for ip in iface.get("ips", []):
        if "." in ip:
            return ip
    return None


# ---------------- счётчики интерфейса (когда эмуляция выключена) ----------------
class _MibIfRow2(ctypes.Structure):
    _fields_ = [
        ("InterfaceLuid", ctypes.c_uint64), ("InterfaceIndex", ctypes.c_ulong), ("InterfaceGuid", ctypes.c_byte * 16),
        ("Alias", ctypes.c_wchar * 257), ("Description", ctypes.c_wchar * 257),
        ("PhysicalAddressLength", ctypes.c_ulong), ("PhysicalAddress", ctypes.c_ubyte * 32),
        ("PermanentPhysicalAddress", ctypes.c_ubyte * 32), ("Mtu", ctypes.c_ulong), ("Type", ctypes.c_ulong),
        ("TunnelType", ctypes.c_int), ("MediaType", ctypes.c_int), ("PhysicalMediumType", ctypes.c_int),
        ("AccessType", ctypes.c_int), ("DirectionType", ctypes.c_int), ("Flags", ctypes.c_ubyte),
        ("OperStatus", ctypes.c_int), ("AdminStatus", ctypes.c_int), ("MediaConnectState", ctypes.c_int),
        ("NetworkGuid", ctypes.c_byte * 16), ("ConnectionType", ctypes.c_int),
        ("TransmitLinkSpeed", ctypes.c_uint64), ("ReceiveLinkSpeed", ctypes.c_uint64),
        ("InOctets", ctypes.c_uint64), ("InUcastPkts", ctypes.c_uint64), ("InNUcastPkts", ctypes.c_uint64),
        ("InDiscards", ctypes.c_uint64), ("InErrors", ctypes.c_uint64), ("InUnknownProtos", ctypes.c_uint64),
        ("InUcastOctets", ctypes.c_uint64), ("InMulticastOctets", ctypes.c_uint64),
        ("InBroadcastOctets", ctypes.c_uint64), ("OutOctets", ctypes.c_uint64), ("OutUcastPkts", ctypes.c_uint64),
        ("OutNUcastPkts", ctypes.c_uint64), ("OutDiscards", ctypes.c_uint64), ("OutErrors", ctypes.c_uint64),
        ("OutUcastOctets", ctypes.c_uint64), ("OutMulticastOctets", ctypes.c_uint64),
        ("OutBroadcastOctets", ctypes.c_uint64), ("OutQLen", ctypes.c_uint64),
    ]


_iphlp_ = ctypes.WinDLL("iphlpapi")
_iphlp_.GetIfEntry2.argtypes = [ctypes.POINTER(_MibIfRow2)]


def if_counters(if_idx):
    """(принято байт, отправлено байт) по данным ОС или None."""
    row = _MibIfRow2()
    row.InterfaceIndex = int(if_idx)
    if _iphlp_.GetIfEntry2(ctypes.byref(row)) != 0:
        return None
    return row.InOctets, row.OutOctets


# ---------------- ICMP-пинг (без ping.exe, не зависит от языка системы) ----------------
class _IPOptions(ctypes.Structure):
    _fields_ = [("Ttl", ctypes.c_ubyte), ("Tos", ctypes.c_ubyte), ("Flags", ctypes.c_ubyte),
                ("OptionsSize", ctypes.c_ubyte), ("OptionsData", ctypes.c_void_p)]


class _EchoReply(ctypes.Structure):
    _fields_ = [("Address", ctypes.c_ulong), ("Status", ctypes.c_ulong), ("RoundTripTime", ctypes.c_ulong),
                ("DataSize", ctypes.c_ushort), ("Reserved", ctypes.c_ushort), ("Data", ctypes.c_void_p),
                ("Options", _IPOptions)]


_iphlp = ctypes.WinDLL("iphlpapi")
_iphlp.IcmpCreateFile.restype = wintypes.HANDLE
_iphlp.IcmpSendEcho2Ex.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p, wintypes.WORD,
                                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD]
_iphlp.IcmpSendEcho2Ex.restype = wintypes.DWORD
_iphlp.IcmpCloseHandle.argtypes = [wintypes.HANDLE]


def _ip4(ip):
    return struct.unpack("<I", socket.inet_aton(ip))[0]


def icmp_ping(ip, timeout_ms=2000, src=None, size=32):
    """RTT в мс или None при потере. src — IPv4 выбранного интерфейса (пинг идёт именно через него)."""
    h = _iphlp.IcmpCreateFile()
    try:
        data = ctypes.create_string_buffer(b"badlink-ping".ljust(size, b"."), size)
        reply = ctypes.create_string_buffer(ctypes.sizeof(_EchoReply) + size + 64)
        t0 = time.perf_counter()
        n = _iphlp.IcmpSendEcho2Ex(h, None, None, None, _ip4(src) if src else 0, _ip4(ip), data, size,
                                   None, reply, len(reply), timeout_ms)
        dt = (time.perf_counter() - t0) * 1000
        if n == 0:
            return None
        r = _EchoReply.from_buffer(reply)
        if r.Status != 0:
            return None
        # RoundTripTime целочисленный — своё измерение точнее на малых RTT
        return dt if abs(dt - r.RoundTripTime) < 20 else float(r.RoundTripTime)
    finally:
        _iphlp.IcmpCloseHandle(h)


class Pinger:
    """Пинг раз в секунду; каждый пинг в своём потоке, чтобы потери не сбивали ритм."""

    def __init__(self, on_result):
        self.on_result = on_result   # (time.time(), rtt_ms | None, ошибка | None)
        self.host = ""
        self.src = None
        self.interval = 1.0
        self.timeout_ms = 2000
        self._gen = 0

    def start(self, host, src=None):
        self.stop()
        self.host, self.src = host.strip(), src
        if not self.host:
            return
        self._gen += 1
        threading.Thread(target=self._loop, args=(self._gen,), daemon=True, name="pinger").start()

    def stop(self):
        self._gen += 1

    def _loop(self, gen):
        nxt = time.perf_counter()
        while gen == self._gen:
            threading.Thread(target=self._one, args=(gen,), daemon=True).start()
            nxt += self.interval
            time.sleep(max(0.0, nxt - time.perf_counter()))

    def _one(self, gen):
        t = time.time()
        rtt, err = None, None
        try:
            rtt = icmp_ping(socket.gethostbyname(self.host), self.timeout_ms, src=self.src)
        except OSError as e:
            err = str(e)
        if gen == self._gen:
            self.on_result(t, rtt, err)


# ---------------- глобальная горячая клавиша ----------------
def register_hotkey(callback, vk=0x7B, mods=0x0002 | 0x0004):
    """Ctrl+Shift+F12. callback вызывается из фонового потока. Возвращает Event: set, если клавиша занята."""
    MOD_NOREPEAT = 0x4000
    WM_HOTKEY = 0x0312
    failed = threading.Event()

    def loop():
        user32 = ctypes.windll.user32
        if not user32.RegisterHotKey(None, 1, mods | MOD_NOREPEAT, vk):
            failed.set()
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                callback()

    threading.Thread(target=loop, daemon=True, name="hotkey").start()
    return failed


# ---------------- фильтр WinDivert ----------------
def build_filter(if_idx):
    """Весь IP-трафик интерфейса. BADLINK_EXTRA_FILTER — отладочное сужение (только для разработки)."""
    f = f"ifIdx == {int(if_idx)} and !loopback"
    extra = os.environ.get("BADLINK_EXTRA_FILTER", "").strip()
    if extra:
        f += f" and ({extra})"
    return f
