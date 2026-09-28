"""Минимальная ctypes-обёртка над WinDivert 2.2 (без сторонних пакетов)."""
import ctypes
import os
import sys
from ctypes import wintypes

LAYER_NETWORK = 0

PARAM_QUEUE_LENGTH = 0
PARAM_QUEUE_TIME = 1
PARAM_QUEUE_SIZE = 2

SHUTDOWN_RECV = 1
SHUTDOWN_BOTH = 3

ADDR_SIZE = 80          # sizeof(WINDIVERT_ADDRESS)
MTU_MAX = 40 + 0xFFFF   # WINDIVERT_MTU_MAX

_FLAG_OUTBOUND = 1 << 17   # биты поля Layer:8/Event:8/Sniffed/Outbound/...
_FLAG_LOOPBACK = 1 << 18

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

ERROR_MESSAGES = {
    2: "Не найден драйвер WinDivert64.sys рядом с WinDivert.dll (папка windivert\\).",
    5: "Нет прав администратора. Запустите программу от имени администратора.",
    87: "Некорректный фильтр WinDivert.",
    577: "Windows отклонила подпись драйвера WinDivert (Secure Boot / политика подписи).",
    654: "Загружена несовместимая версия драйвера WinDivert "
         "(перезагрузите ПК или закройте другие программы на WinDivert).",
    1060: "Служба драйвера WinDivert не установлена / была удалена.",
    1275: "Загрузка драйвера WinDivert заблокирована (антивирус или политика).",
    1753: "Служба Base Filtering Engine (BFE) остановлена — WinDivert не может работать.",
}


class WinDivertError(OSError):
    pass


def _candidate_dirs():
    here = os.path.dirname(os.path.abspath(__file__))
    roots = []
    if getattr(sys, "frozen", False):
        roots.append(os.path.dirname(sys.executable))
        roots.append(getattr(sys, "_MEIPASS", ""))
    roots.append(os.path.dirname(here))
    for r in roots:
        if r:
            yield os.path.join(r, "windivert")
            yield r


_dll = None


def dll_path():
    for d in _candidate_dirs():
        p = os.path.join(d, "WinDivert.dll")
        if os.path.isfile(p):
            return p
    return None


def _load():
    global _dll
    if _dll is not None:
        return _dll
    path = dll_path()
    if not path:
        raise WinDivertError(2, "Не найден WinDivert.dll: должен лежать в папке windivert\\ рядом с программой.")
    dll = ctypes.WinDLL(path, use_last_error=True)

    dll.WinDivertOpen.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int16, ctypes.c_uint64]
    dll.WinDivertOpen.restype = ctypes.c_void_p
    dll.WinDivertRecv.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                                  ctypes.POINTER(ctypes.c_uint), ctypes.c_void_p]
    dll.WinDivertRecv.restype = wintypes.BOOL
    dll.WinDivertSend.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint,
                                  ctypes.POINTER(ctypes.c_uint), ctypes.c_char_p]
    dll.WinDivertSend.restype = wintypes.BOOL
    dll.WinDivertShutdown.argtypes = [ctypes.c_void_p, ctypes.c_int]
    dll.WinDivertShutdown.restype = wintypes.BOOL
    dll.WinDivertClose.argtypes = [ctypes.c_void_p]
    dll.WinDivertClose.restype = wintypes.BOOL
    dll.WinDivertSetParam.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint64]
    dll.WinDivertSetParam.restype = wintypes.BOOL
    dll.WinDivertHelperCompileFilter.argtypes = [
        ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint,
        ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(ctypes.c_uint)]
    dll.WinDivertHelperCompileFilter.restype = wintypes.BOOL
    dll.WinDivertHelperCalcChecksums.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint64]
    dll.WinDivertHelperCalcChecksums.restype = wintypes.BOOL
    _dll = dll
    return dll


def check_filter(filt):
    """Возвращает None, если фильтр корректен, иначе текст ошибки."""
    dll = _load()
    err = ctypes.c_char_p()
    pos = ctypes.c_uint()
    if dll.WinDivertHelperCompileFilter(filt.encode(), LAYER_NETWORK, None, 0,
                                        ctypes.byref(err), ctypes.byref(pos)):
        return None
    msg = err.value.decode(errors="replace") if err.value else "ошибка"
    return f"{msg} (позиция {pos.value}): {filt}"


class WinDivert:
    def __init__(self, filt, priority=0, flags=0):
        self._dll = _load()
        bad = check_filter(filt)
        if bad:
            raise WinDivertError(87, "Ошибка фильтра: " + bad)
        h = self._dll.WinDivertOpen(filt.encode(), LAYER_NETWORK, priority, flags)
        if h is None or h == INVALID_HANDLE_VALUE:
            code = ctypes.get_last_error()
            raise WinDivertError(code, ERROR_MESSAGES.get(code, f"WinDivertOpen: ошибка Windows {code}"))
        self.handle = h
        self._buf = ctypes.create_string_buffer(MTU_MAX)
        self._addr = ctypes.create_string_buffer(ADDR_SIZE)
        self._rlen = ctypes.c_uint()
        self._slen = ctypes.c_uint()

    def set_param(self, param, value):
        self._dll.WinDivertSetParam(self.handle, param, value)

    def recv(self):
        """Блокирующий приём. Возвращает (packet_bytes, addr_bytes) или бросает WinDivertError."""
        if not self._dll.WinDivertRecv(self.handle, self._buf, MTU_MAX,
                                       ctypes.byref(self._rlen), self._addr):
            code = ctypes.get_last_error()
            raise WinDivertError(code, f"WinDivertRecv: ошибка {code}")
        return self._buf.raw[:self._rlen.value], self._addr.raw

    def send(self, packet, addr):
        return bool(self._dll.WinDivertSend(self.handle, packet, len(packet),
                                            ctypes.byref(self._slen), addr))

    def shutdown(self):
        self._dll.WinDivertShutdown(self.handle, SHUTDOWN_RECV)  # отправка остаётся — чтобы выпустить буферы

    def close(self):
        if self.handle:
            self._dll.WinDivertClose(self.handle)
            self.handle = None


def calc_checksums(pkt, addr):
    """Пересчитать IP/TCP/UDP-суммы. -> (packet, addr) с выставленными флагами сумм."""
    dll = _load()
    pb = ctypes.create_string_buffer(pkt, len(pkt))
    ab = ctypes.create_string_buffer(addr, ADDR_SIZE)
    dll.WinDivertHelperCalcChecksums(pb, len(pkt), ab, 0)
    return pb.raw, ab.raw


def segment_tcp(pkt, mtu=1500):
    """Режет TCP-«суперпакет» (LSO/RSC, больше MTU) на сегменты MSS. None — резать не нужно/нельзя.

    Сетевые карты склеивают сегменты до 64 КБ; на 1,5 Мбит/с такой пакет «передаётся» ~350 мс,
    и ограничитель скорости даёт рывки. Резка возвращает поведение обычного канала.
    """
    if len(pkt) <= mtu:
        return None
    ver = pkt[0] >> 4
    if ver == 4:
        ihl = (pkt[0] & 0x0F) * 4
        if pkt[9] != 6 or (int.from_bytes(pkt[6:8], "big") & 0x3FFF):   # не TCP или фрагмент
            return None
    elif ver == 6:
        ihl = 40
        if pkt[6] != 6:          # есть extension headers — не трогаем
            return None
    else:
        return None
    thl = (pkt[ihl + 12] >> 4) * 4
    head_len = ihl + thl
    payload = pkt[head_len:]
    mss = mtu - head_len
    if mss < 500 or not payload:
        return None
    ip_hdr, tcp_hdr = pkt[:ihl], pkt[ihl:head_len]
    seq = int.from_bytes(tcp_hdr[4:8], "big")
    ident = int.from_bytes(ip_hdr[4:6], "big") if ver == 4 else 0
    out = []
    for i, off in enumerate(range(0, len(payload), mss)):
        chunk = payload[off:off + mss]
        last = off + mss >= len(payload)
        ip = bytearray(ip_hdr)
        if ver == 4:
            ip[2:4] = (head_len + len(chunk)).to_bytes(2, "big")
            ip[4:6] = ((ident + i) & 0xFFFF).to_bytes(2, "big")
            ip[10:12] = b"\0\0"
        else:
            ip[4:6] = (thl + len(chunk)).to_bytes(2, "big")
        tcp = bytearray(tcp_hdr)
        tcp[4:8] = ((seq + off) & 0xFFFFFFFF).to_bytes(4, "big")
        if not last:
            tcp[13] &= ~0x09     # FIN и PSH — только у последнего сегмента
        tcp[16:18] = b"\0\0"
        out.append(bytes(ip) + bytes(tcp) + chunk)
    return out


def addr_flags(addr):
    return int.from_bytes(addr[8:12], "little")


def is_outbound(addr):
    return bool(addr_flags(addr) & _FLAG_OUTBOUND)


class DivertIO:
    """Адаптер WinDivert для движка эмуляции."""

    def __init__(self, filt, mtu=1500):
        self.w = WinDivert(filt)
        self.w.set_param(PARAM_QUEUE_LENGTH, 16384)
        self.w.set_param(PARAM_QUEUE_TIME, 8000)
        self.w.set_param(PARAM_QUEUE_SIZE, 32 * 1024 * 1024)
        self.mtu = mtu
        self._pending = []
        self.segmented = 0     # сколько суперпакетов порезано (для статистики)

    def recv(self):
        if self._pending:
            return self._pending.pop()
        pkt, addr = self.w.recv()
        out = is_outbound(addr)
        segs = segment_tcp(pkt, self.mtu)
        if not segs:
            return pkt, addr, out
        self.segmented += 1
        res = [calc_checksums(s, addr) + (out,) for s in segs]
        res.reverse()          # pop() с конца — сохраняем порядок
        self._pending = res
        return self._pending.pop()

    def send(self, pkt, addr):
        return self.w.send(pkt, addr)

    def shutdown(self):
        self.w.shutdown()

    def close(self):
        self.w.close()
