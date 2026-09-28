"""Самопроверка без прав администратора и без перехвата трафика (для CI и проверки собранного exe).

Код возврата 0 — всё в порядке. Отчёт пишется в stdout (если есть консоль) и в selftest.log рядом с программой.
"""
import os
import subprocess
import sys
import time
import traceback


def _checks():
    from . import __version__
    from . import windivert, iperf, sysinfo
    from .engine import Engine, Params

    yield f"версия {__version__}"

    windivert._load()
    yield f"WinDivert.dll загружается: {windivert.dll_path()}"
    bad = windivert.check_filter(sysinfo.build_filter(1))
    assert bad is None, bad
    yield "фильтр WinDivert компилируется"

    exe = iperf.iperf_path()
    assert exe, "iperf3.exe не найден"
    out = subprocess.run([exe, "--version"], capture_output=True, timeout=20,
                         creationflags=sysinfo.CREATE_NO_WINDOW).stdout.decode(errors="replace")
    assert out.startswith("iperf 3"), out
    yield f"iperf3 запускается: {out.splitlines()[0]}"

    class _IO:
        def __init__(self):
            self.sent = 0
            self.n = 0

        def recv(self):
            if self.n >= 200:
                time.sleep(0.05)
                raise OSError("конец")
            self.n += 1
            return b"\x45" + b"\0" * 1399, b"\0" * 80, bool(self.n % 2)

        def send(self, pkt, addr):
            self.sent += 1
            return True

        def shutdown(self):
            pass

        def close(self):
            pass

    e = Engine()
    e.set_params(Params(down_kbps=0, up_kbps=0))
    io = _IO()
    e.start(io, seed=1)
    time.sleep(0.5)
    e.stop()
    assert io.sent == 200, f"движок отправил {io.sent} из 200"
    yield "движок эмуляции пропускает пакеты"


def run():
    lines, ok = [], True
    try:
        for msg in _checks():
            lines.append("OK   " + msg)
    except Exception:
        ok = False
        lines.append("FAIL " + traceback.format_exc())
    lines.append("SELFTEST " + ("PASSED" if ok else "FAILED"))
    text = "\n".join(lines)
    if sys.stdout:
        print(text)
    base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.getcwd()
    try:
        with open(os.path.join(base, "selftest.log"), "w", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError:
        pass
    return 0 if ok else 1
