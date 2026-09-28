"""Движок эмуляции плохого канала.

Для каждого направления (приём / отдача) пакет проходит:
    прерывание? -> очередь узкого места (drop-tail) -> ограничитель скорости (token bucket) -> отправка
Скорость ограничителя раз в ~интервал выбирается случайно:
  как реальный канал — логнормально со средним = база и СКО = разброс (часто ниже среднего,
                        изредка всплески в разы выше — так выглядят замеры iperf на реальных каналах);
  равномерно          — из [база - разброс, база + разброс].
Прерывания: оба направления сразу падают в 0 на случайное время.
  обрыв     — всё, что пришло во время прерывания, и содержимое очереди теряется;
  замирание — пакеты копятся (очередь растягивается на длительность прерывания) и уходят после.

Все решения принимает один поток (_tx); поток приёма только складывает пакеты в inbox.
Отдельные генераторы случайных чисел на скорость и прерывания: при одинаковом seed
последовательность скоростей и расписание прерываний повторяются независимо от трафика.
"""
import collections
import math
import random
import threading
import time
from dataclasses import dataclass

DOWN, UP = 0, 1
MTU = 1514


@dataclass
class Params:
    down_kbps: float = 1500.0       # 0 = без ограничения
    down_swing_kbps: float = 500.0
    up_kbps: float = 1500.0
    up_swing_kbps: float = 500.0
    step_ms: float = 1000.0         # как часто меняется скорость (±40% случайно)
    smooth: bool = False            # плавный переход вместо ступеньки
    realistic: bool = True          # True — логнормально (как реальный канал), False — равномерно ± разброс
    queue_ms: float = 100.0         # очередь узкого места, мс трафика на базовой скорости
    outage_enabled: bool = False
    outage_per_min: float = 2.0
    outage_min_s: float = 1.0
    outage_max_s: float = 5.0
    outage_hold: bool = False       # False = обрыв, True = замирание


class _Dir:
    def __init__(self, idx, rng):
        self.idx = idx
        self.rng = rng
        self.queue = collections.deque()
        self.qbytes = 0
        self.tokens = 0.0
        self.last_refill = time.perf_counter()
        self.base_seen = None
        self.prev_kbps = self.target_kbps = 0.0
        self.seg_start = self.seg_end = 0.0
        self.cur_kbps = None        # None = без ограничения
        # статистика (пишет только поток _tx)
        self.rx_pkts = self.rx_bytes = 0
        self.tx_pkts = self.tx_bytes = 0
        self.drop_queue = self.drop_outage = 0
        self.send_err = 0

    def _draw(self, base, swing, realistic):
        floor = 8.0
        if swing <= 0:
            return base
        if realistic:
            # логнормальное с заданными средним и СКО; всплески не выше 3,5× средней (в реальных замерах — до ~3×)
            s2 = math.log(1 + (swing / base) ** 2)
            mu = math.log(base) - s2 / 2
            return min(base * 3.5, max(floor, self.rng.lognormvariate(mu, math.sqrt(s2))))
        return self.rng.uniform(max(floor, base - swing), base + swing)

    def update_rate(self, now, base, swing, step_s, smooth, realistic=False):
        if base <= 0:
            self.cur_kbps = None
            self.base_seen = None
            return
        if self.base_seen != base:          # изменили базу — новая ступенька сразу
            self.base_seen = base
            self.prev_kbps = self.target_kbps = base
            self.seg_end = now
        if now >= self.seg_end:
            self.prev_kbps = self.cur_kbps if self.cur_kbps is not None else base
            self.target_kbps = self._draw(base, swing, realistic)
            self.seg_start = now
            self.seg_end = now + max(0.05, step_s * self.rng.uniform(0.6, 1.4))
        if smooth:
            frac = min(1.0, (now - self.seg_start) / max(1e-6, self.seg_end - self.seg_start))
            self.cur_kbps = self.prev_kbps + (self.target_kbps - self.prev_kbps) * frac
        else:
            self.cur_kbps = self.target_kbps


class Engine:
    def __init__(self):
        self.params = Params()
        self.io = None
        self.seed = None
        self.dirs = [_Dir(DOWN, random.Random()), _Dir(UP, random.Random())]
        self.events = collections.deque(maxlen=1000)   # (time.time(), текст)
        self._inbox = collections.deque()
        self._wake = threading.Event()
        self._running = False
        self._rx_thread = self._tx_thread = None
        self._reset_outage(random.Random())

    def _reset_outage(self, rng):
        self._orng = rng
        self._next_outage = None
        self._outage_end = 0.0
        self._manual_end = 0.0
        self.outage_active = False
        self.outage_count = 0
        self.outage_total_s = 0.0
        self._outage_started = 0.0

    # ---------- управление ----------
    @property
    def running(self):
        return self._running

    def set_params(self, p: Params):
        self.params = p   # атомарная подмена ссылки; _tx читает её каждый цикл

    def log(self, text):
        self.events.append((time.time(), text))

    def start(self, io, seed=None):
        if self._running:
            return
        self.io = io
        self.seed = seed if seed is not None else random.randrange(1, 10**6)
        self.dirs = [_Dir(DOWN, random.Random(f"{self.seed}-down")),
                     _Dir(UP, random.Random(f"{self.seed}-up"))]
        self._reset_outage(random.Random(f"{self.seed}-outage"))
        self._inbox.clear()
        self._running = True
        self._rx_thread = threading.Thread(target=self._rx, name="netem-rx", daemon=True)
        self._tx_thread = threading.Thread(target=self._tx, name="netem-tx", daemon=True)
        self._rx_thread.start()
        self._tx_thread.start()
        self.log(f"Эмуляция запущена (seed {self.seed})")

    def stop(self):
        if not self._running:
            return
        self._running = False
        try:
            self.io.shutdown()
        except Exception:
            pass
        self._wake.set()
        self._rx_thread.join(2)
        self._tx_thread.join(2)
        # остаток буферов отпускаем сразу, чтобы остановка не рвала соединения
        flushed = 0
        while self._inbox:
            _, pkt, addr, out = self._inbox.popleft()
            self._send(self.dirs[UP if out else DOWN], pkt, addr)
            flushed += 1
        for d in self.dirs:
            for pkt, addr in d.queue:
                self._send(d, pkt, addr)
                flushed += 1
            d.queue.clear()
            d.qbytes = 0
        try:
            self.io.close()
        except Exception:
            pass
        if self.outage_active:
            self.outage_total_s += time.perf_counter() - self._outage_started
            self.outage_active = False
        self.log(f"Эмуляция остановлена, канал без ограничений (из буферов отпущено {flushed} пак.)")

    def trigger_outage(self, seconds):
        self._manual_end = time.perf_counter() + seconds
        self._wake.set()

    # ---------- потоки ----------
    def _rx(self):
        io = self.io
        while True:   # после shutdown() драйвер отдаёт остаток очереди и возвращает ошибку
            try:
                pkt, addr, out = io.recv()
            except Exception as e:
                if self._running:
                    self.log(f"Ошибка приёма: {e}")
                break
            self._inbox.append((time.perf_counter(), pkt, addr, out))
            self._wake.set()

    def _tx(self):
        try:
            import ctypes
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass
        try:
            while self._running:
                self._wake.clear()
                now = time.perf_counter()
                p = self.params
                self._update_outage(now, p)
                step = max(0.05, p.step_ms / 1000.0)
                self.dirs[DOWN].update_rate(now, p.down_kbps, p.down_swing_kbps, step, p.smooth, p.realistic)
                self.dirs[UP].update_rate(now, p.up_kbps, p.up_swing_kbps, step, p.smooth, p.realistic)
                inbox = self._inbox
                while inbox:
                    _, pkt, addr, out = inbox.popleft()
                    self._ingress(self.dirs[UP if out else DOWN], pkt, addr, p)
                wait = 0.05
                for d in self.dirs:
                    wait = min(wait, self._service(d, now, p))
                if wait > 0:
                    self._wake.wait(wait)
        except Exception as e:
            self.log(f"Сбой потока эмуляции: {e!r}")
        finally:
            try:
                import ctypes
                ctypes.windll.winmm.timeEndPeriod(1)
            except Exception:
                pass

    # ---------- логика ----------
    def _update_outage(self, now, p):
        if p.outage_enabled and p.outage_per_min > 0:
            mean_gap = 60.0 / p.outage_per_min
            if self._next_outage is None:
                self._next_outage = now + self._orng.expovariate(1.0 / mean_gap)
            if now >= self._next_outage and now >= self._outage_end:
                lo, hi = sorted((max(0.1, p.outage_min_s), max(0.1, p.outage_max_s)))
                # равномерно по логарифму: короткие прерывания частые, длинные редкие (как в реальных замерах)
                self._outage_end = now + math.exp(self._orng.uniform(math.log(lo), math.log(hi)))
                self._next_outage = self._outage_end + self._orng.expovariate(1.0 / mean_gap)
        else:
            self._next_outage = None
            self._outage_end = min(self._outage_end, now)   # сняли галочку — текущее прерывание заканчивается

        active = now < self._outage_end or now < self._manual_end
        if active and not self.outage_active:
            self.outage_active = True
            self.outage_count += 1
            self._outage_started = now
            left = max(self._outage_end, self._manual_end) - now
            mode = "замирание" if p.outage_hold else "обрыв"
            self.log(f"Прерывание #{self.outage_count} ({mode}) на {left:.1f} с")
            if not p.outage_hold:
                for d in self.dirs:
                    d.drop_outage += len(d.queue)
                    d.queue.clear()
                    d.qbytes = 0
        elif not active and self.outage_active:
            self.outage_active = False
            dur = now - self._outage_started
            self.outage_total_s += dur
            self.log(f"Связь восстановлена через {dur:.1f} с")

    def outage_left(self):
        if not self.outage_active:
            return 0.0
        return max(0.0, max(self._outage_end, self._manual_end) - time.perf_counter())

    def _queue_limit(self, d, p):
        base = p.down_kbps if d.idx == DOWN else p.up_kbps
        if base <= 0:
            return 1 << 30
        ms = max(0.0, p.queue_ms)
        if self.outage_active and p.outage_hold:
            ms += max(p.outage_max_s, self.outage_left()) * 1000   # «замирание» копит всё прерывание
        return max(3 * MTU, base * 1000 / 8 * ms / 1000.0)

    def _ingress(self, d, pkt, addr, p):
        n = len(pkt)
        d.rx_pkts += 1
        d.rx_bytes += n
        if self.outage_active and not p.outage_hold:
            d.drop_outage += 1
            return
        if d.qbytes + n > self._queue_limit(d, p):
            if self.outage_active:
                d.drop_outage += 1
            else:
                d.drop_queue += 1
            return
        d.queue.append((pkt, addr))
        d.qbytes += n

    def _service(self, d, now, p):
        q = d.queue
        rate_kbps = 0.0 if self.outage_active else d.cur_kbps
        if rate_kbps is None:
            d.tokens = 0.0
            d.last_refill = now
            while q:
                pkt, addr = q.popleft()
                d.qbytes -= len(pkt)
                self._send(d, pkt, addr)
            return 0.05
        bps = rate_kbps * 1000 / 8
        cap = max(MTU, bps * 0.01)   # корзина ~10 мс: без длинных всплесков выше лимита
        d.tokens = min(cap, d.tokens + bps * (now - d.last_refill))
        d.last_refill = now
        while q:
            n = len(q[0][0])
            if d.tokens < min(n, cap):
                return (min(n, cap) - d.tokens) / bps if bps > 0 else 0.05
            pkt, addr = q.popleft()
            d.qbytes -= n
            d.tokens -= n   # для пакетов больше корзины (LSO) уходит в минус — это «долг»
            self._send(d, pkt, addr)
        return 0.05

    def _send(self, d, pkt, addr):
        try:
            ok = self.io.send(pkt, addr)
        except Exception:
            ok = False
        if ok:
            d.tx_pkts += 1
            d.tx_bytes += len(pkt)
        else:
            d.send_err += 1

    # ---------- статистика ----------
    def snapshot(self):
        res = []
        for d in self.dirs:
            rate = 0.0 if self.outage_active else d.cur_kbps
            res.append(dict(
                rx_pkts=d.rx_pkts, rx_bytes=d.rx_bytes, tx_pkts=d.tx_pkts, tx_bytes=d.tx_bytes,
                drop_queue=d.drop_queue, drop_outage=d.drop_outage, send_err=d.send_err,
                qbytes=d.qbytes, limit_kbps=rate,
            ))
        return dict(dirs=res, outage=self.outage_active, outage_left=self.outage_left(),
                    outage_count=self.outage_count, outage_total_s=self.outage_total_s)
