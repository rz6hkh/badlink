"""Встроенный iperf3: сервер поднимается при запуске, клиент отдаёт скорость по секундам."""
import os
import re
import subprocess
import sys
import threading
import time

from .sysinfo import CREATE_NO_WINDOW

# [  5]   3.00-4.00   sec   183 KBytes  1.50 Mbits/sec   0   35.4 KBytes   (+ sender/receiver в итогах)
_LINE = re.compile(r"\[\s*(\d+|SUM)\]\s*(?:\[[\w-]+\]\s*)?([\d.]+)-([\d.]+)\s+sec\s+([\d.]+)\s+(\w?)Bytes\s+"
                   r"([\d.]+)\s+(\w?)bits/sec(.*)$")
_MULT = {"": 1e-3, "K": 1.0, "M": 1e3, "G": 1e6}


def _make_kill_job():
    """Job Object с KILL_ON_JOB_CLOSE: дочерние iperf3 умирают вместе с утилитой, даже при её падении."""
    import ctypes
    from ctypes import wintypes

    class _Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _Extended(ctypes.Structure):
        _fields_ = [("Basic", _Basic), ("Io", ctypes.c_uint64 * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    job = k32.CreateJobObjectW(None, None)
    info = _Extended()
    info.Basic.LimitFlags = 0x2000   # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))

    def assign(proc):
        k32.AssignProcessToJobObject(job, int(proc._handle))
    return assign


try:
    _assign_job = _make_kill_job()
except Exception:
    _assign_job = lambda proc: None


def _popen(args, cwd):
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         creationflags=CREATE_NO_WINDOW, cwd=cwd)
    _assign_job(p)
    return p


def iperf_path():
    roots = []
    if getattr(sys, "frozen", False):
        roots.append(os.path.dirname(sys.executable))
    roots.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for r in roots:
        p = os.path.join(r, "iperf3", "iperf3.exe")
        if os.path.isfile(p):
            return p
    return None


def parse_line(line):
    """-> dict(stream, start, end, kbps, final, role, extra) или None."""
    m = _LINE.search(line)
    if not m:
        return None
    stream, a, b, _, _, rate, unit, rest = m.groups()
    rest = rest.strip()
    final = "sender" in rest or "receiver" in rest
    return dict(stream=stream, start=float(a), end=float(b), kbps=float(rate) * _MULT.get(unit, 1.0),
                final=final, role="receiver" if "receiver" in rest else ("sender" if final else ""),
                extra=rest)


class IperfServer:
    """iperf3 -s с автоперезапуском (Windows-сборка иногда завершается после сбойного теста)."""

    def __init__(self, on_line):
        self.on_line = on_line
        self.port = 5201
        self.proc = None
        self._want = False
        self._lock = threading.Lock()

    @property
    def running(self):
        return self._want

    def start(self, port=5201):
        exe = iperf_path()
        if not exe:
            self.on_line("iperf3.exe не найден (ожидается в папке iperf3\\)")
            return False
        self.stop()
        self.port = port
        self._want = True
        threading.Thread(target=self._loop, args=(exe,), daemon=True, name="iperf-server").start()
        return True

    def _loop(self, exe):
        fails = 0
        while self._want:
            t0 = time.time()
            try:
                with self._lock:
                    self.proc = _popen([exe, "-s", "-p", str(self.port), "--forceflush", "-f", "k"],
                                       os.path.dirname(exe))
                self.on_line(f"iperf3-сервер слушает порт {self.port}")
                for raw in self.proc.stdout:
                    line = raw.decode("utf-8", errors="replace").rstrip()
                    if line:
                        self.on_line(line)
                self.proc.wait()
            except Exception as e:
                self.on_line(f"iperf3-сервер: {e}")
            if not self._want:
                break
            fails = fails + 1 if time.time() - t0 < 3 else 0
            if fails >= 3:
                self.on_line("iperf3-сервер падает при запуске (порт занят?) — остановлен")
                self._want = False
                break
            time.sleep(1)

    def stop(self):
        self._want = False
        with self._lock:
            p, self.proc = self.proc, None
        if p and p.poll() is None:
            p.kill()


class IperfClient:
    def __init__(self, on_interval, on_line, on_done):
        self.on_interval = on_interval   # (секунда_конца, kbps)
        self.on_line = on_line
        self.on_done = on_done           # (итоги: dict{sender, receiver} | None, код возврата)
        self.proc = None

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, host, port=5201, seconds=30, reverse=True, udp=False, bitrate="", parallel=1, bind=None,
              block=""):
        exe = iperf_path()
        if not exe:
            self.on_line("iperf3.exe не найден (ожидается в папке iperf3\\)")
            return False
        args = [exe, "-c", host, "-p", str(port), "-t", str(int(seconds)), "-i", "1",
                "-f", "k", "--forceflush"]
        if reverse:
            args.append("-R")
        if udp:
            args.append("-u")
        if bitrate:
            args += ["-b", bitrate]
        if block:
            args += ["-l", block]
        if parallel > 1:
            args += ["-P", str(int(parallel))]
        if bind:
            args += ["-B", bind]
        self.on_line("> iperf3 " + " ".join(args[1:]))
        self.proc = _popen(args, os.path.dirname(exe))
        threading.Thread(target=self._read, args=(self.proc, parallel > 1), daemon=True,
                         name="iperf-client").start()
        return True

    def _read(self, proc, multi):
        totals = {}
        for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if not line:
                continue
            self.on_line(line)
            r = parse_line(line)
            if not r or (multi and r["stream"] != "SUM"):
                continue
            if r["final"]:
                totals[r["role"]] = r["kbps"]
            elif r["end"] - r["start"] > 0.2:
                self.on_interval(r["end"], r["kbps"])
        code = proc.wait()
        self.on_done(totals or None, code)

    def stop(self):
        p = self.proc
        if p and p.poll() is None:
            p.kill()
