"""Тесты без драйвера и прав администратора: python -m unittest discover -s tests"""
import os
import sys
import threading
import time
import unittest
import queue

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from netem.chart import fmt_rate, nice_ceil          # noqa: E402
from netem.engine import DOWN, Engine, Params        # noqa: E402
from netem.iperf import parse_line                   # noqa: E402
from netem.sysinfo import build_filter               # noqa: E402
from netem.windivert import segment_tcp              # noqa: E402

PKT = 1400


class FakeIO:
    """Входящий поток offered_kbps пакетами по 1400 байт; считает то, что движок отправил."""

    def __init__(self, offered_kbps, outbound=False):
        self.q = queue.Queue()
        self.sent = []
        self.alive = True
        self.offered = offered_kbps
        self.outbound = outbound
        threading.Thread(target=self._gen, daemon=True).start()

    def _gen(self):
        interval = PKT * 8 / (self.offered * 1000)
        t = time.perf_counter()
        while self.alive:
            t += interval
            d = t - time.perf_counter()
            if d > 0:
                time.sleep(d)
            self.q.put((b"\x45" + b"\0" * (PKT - 1), b"\0" * 80, self.outbound))

    def recv(self):
        item = self.q.get()
        if item is None:
            raise OSError("shutdown")
        return item

    def send(self, pkt, addr):
        self.sent.append((time.perf_counter(), len(pkt)))
        return True

    def shutdown(self):
        self.alive = False
        self.q.put(None)

    def close(self):
        pass


def measured_kbps(io, t_from):
    s = [x for x in io.sent if x[0] > t_from]
    if len(s) < 2:
        return 0.0
    return sum(x[1] for x in s) * 8 / (s[-1][0] - s[0][0]) / 1000


class EngineTest(unittest.TestCase):
    def run_engine(self, params, secs, offered, outbound=False, seed=1):
        e = Engine()
        e.set_params(params)
        io = FakeIO(offered, outbound)
        e.start(io, seed=seed)
        t0 = time.perf_counter()
        time.sleep(secs)
        snap = e.snapshot()
        e.stop()
        return e, io, snap, t0

    def test_flat_limit(self):
        _, io, snap, t0 = self.run_engine(Params(down_kbps=1500, down_swing_kbps=0), 4, 4000)
        kbps = measured_kbps(io, t0 + 1)
        self.assertAlmostEqual(kbps, 1500, delta=120)
        self.assertGreater(snap["dirs"][DOWN]["drop_queue"], 0, "лишнее сверх лимита должно отбрасываться")

    def test_directions_are_independent(self):
        _, io, _, t0 = self.run_engine(Params(down_kbps=3000, up_kbps=500, up_swing_kbps=0), 4, 3000, outbound=True)
        self.assertAlmostEqual(measured_kbps(io, t0 + 1), 500, delta=60)

    def test_swing_stays_in_corridor(self):
        e, io, _, _ = self.run_engine(Params(down_kbps=1500, down_swing_kbps=500, step_ms=200), 3, 4000)
        d = e.dirs[DOWN]
        self.assertGreaterEqual(d.target_kbps, 1000)
        self.assertLessEqual(d.target_kbps, 2000)

    def test_unlimited_passes_everything(self):
        _, io, snap, _ = self.run_engine(Params(down_kbps=0, up_kbps=0), 1.5, 2000)
        d = snap["dirs"][DOWN]
        self.assertEqual(d["drop_queue"] + d["drop_outage"], 0)
        self.assertGreaterEqual(len(io.sent), d["rx_pkts"] - 1)

    def test_manual_outage_drops_traffic(self):
        e = Engine()
        e.set_params(Params(down_kbps=0))
        io = FakeIO(2000)
        e.start(io, seed=1)
        time.sleep(0.5)
        e.trigger_outage(1.0)
        time.sleep(0.2)
        n_in_outage = len(io.sent)
        time.sleep(0.7)
        self.assertEqual(len(io.sent), n_in_outage, "во время обрыва ничего не отправляется")
        time.sleep(0.8)
        snap = e.snapshot()
        e.stop()
        self.assertEqual(snap["outage_count"], 1)
        self.assertGreater(snap["dirs"][DOWN]["drop_outage"], 0)
        self.assertGreater(len(io.sent), n_in_outage, "после обрыва трафик идёт снова")

    def test_hold_outage_keeps_packets(self):
        e = Engine()
        e.set_params(Params(down_kbps=2000, down_swing_kbps=0, outage_hold=True, outage_max_s=2))
        io = FakeIO(500)
        e.start(io, seed=1)
        time.sleep(0.3)
        e.trigger_outage(1.0)
        time.sleep(2.0)
        snap = e.snapshot()
        e.stop()
        self.assertEqual(snap["dirs"][DOWN]["drop_outage"], 0, "в режиме замирания пакеты не теряются")

    def test_seed_repeats_rates_and_outages(self):
        """Одинаковый seed — одинаковые ступеньки скорости и расписание прерываний (время искусственное)."""
        import random
        from netem.engine import _Dir

        def rates(seed):
            d = _Dir(DOWN, random.Random(f"{seed}-down"))
            out = []
            for i in range(50):
                d.update_rate(i * 0.25, 1500, 500, 1.0, False)
                out.append(round(d.cur_kbps, 3))
            return out

        def outages(seed):
            e = Engine()
            e._reset_outage(random.Random(f"{seed}-outage"))
            p = Params(outage_enabled=True, outage_per_min=30, outage_min_s=1, outage_max_s=5)
            starts = []
            for i in range(3000):          # 300 с с шагом 0,1
                was = e.outage_active
                e._update_outage(i * 0.1, p)
                if e.outage_active and not was:
                    starts.append(round(i * 0.1, 1))
            return starts

        self.assertEqual(rates(7), rates(7))
        self.assertNotEqual(rates(7), rates(8))
        self.assertTrue(all(1000 <= r <= 2000 for r in rates(7)))
        self.assertEqual(outages(7), outages(7))
        self.assertNotEqual(outages(7), outages(8))
        # 30/мин = средний промежуток 2 с + длительность ~3 с → цикл ~5 с → ~60 прерываний за 300 с
        self.assertTrue(30 <= len(outages(7)) <= 90, len(outages(7)))


class SegmentTest(unittest.TestCase):
    def _packet(self, payload, v6=False):
        tcp = bytearray(20)
        tcp[12] = 0x50
        tcp[13] = 0x19                      # FIN + PSH + ACK
        tcp[4:8] = (1000).to_bytes(4, "big")
        if v6:
            ip = bytearray(40)
            ip[0] = 0x60
            ip[6] = 6
            ip[4:6] = (20 + len(payload)).to_bytes(2, "big")
        else:
            ip = bytearray(20)
            ip[0] = 0x45
            ip[9] = 6
            ip[6] = 0x40                    # DF
            ip[2:4] = (40 + len(payload)).to_bytes(2, "big")
            ip[4:6] = (7).to_bytes(2, "big")
        return bytes(ip + tcp) + payload

    def test_small_packet_untouched(self):
        self.assertIsNone(segment_tcp(self._packet(b"x" * 1000)))

    def test_ipv4_split(self):
        payload = bytes(range(256)) * 16
        segs = segment_tcp(self._packet(payload))
        self.assertEqual([len(s) for s in segs], [1500, 1500, 1216])
        self.assertEqual(b"".join(s[40:] for s in segs), payload)
        self.assertEqual([int.from_bytes(s[24:28], "big") for s in segs], [1000, 2460, 3920])
        self.assertEqual([s[33] & 0x09 for s in segs], [0, 0, 0x09], "FIN/PSH только у последнего")
        self.assertEqual([int.from_bytes(s[4:6], "big") for s in segs], [7, 8, 9])

    def test_ipv6_split(self):
        payload = b"y" * 3000
        segs = segment_tcp(self._packet(payload, v6=True))
        self.assertTrue(all(len(s) <= 1500 for s in segs))
        self.assertEqual(b"".join(s[60:] for s in segs), payload)

    def test_udp_untouched(self):
        p = bytearray(self._packet(b"z" * 3000))
        p[9] = 17
        self.assertIsNone(segment_tcp(bytes(p)))


class ParseTest(unittest.TestCase):
    def test_interval(self):
        r = parse_line("[  5]   3.00-4.00   sec   183 KBytes  1500 Kbits/sec    0   35.4 KBytes")
        self.assertEqual((r["start"], r["end"], r["kbps"], r["final"]), (3.0, 4.0, 1500.0, False))

    def test_summary(self):
        r = parse_line("[SUM]   0.00-10.00  sec  1.78 MBytes  1.49 Mbits/sec  12             sender")
        self.assertEqual((r["stream"], r["kbps"], r["role"]), ("SUM", 1490.0, "sender"))

    def test_bidir_and_zero(self):
        r = parse_line("[  5][RX-S]   1.00-2.00   sec  0.00 Bytes  0.00 bits/sec")
        self.assertEqual(r["kbps"], 0.0)

    def test_garbage(self):
        self.assertIsNone(parse_line("Connecting to host 10.0.0.1, port 5201"))


class MiscTest(unittest.TestCase):
    def test_filter(self):
        os.environ.pop("BADLINK_EXTRA_FILTER", None)
        self.assertEqual(build_filter(12), "ifIdx == 12 and !loopback")

    def test_format(self):
        self.assertEqual(fmt_rate(1500), "1.50 Мбит/с")
        self.assertEqual(fmt_rate(640), "640 кбит/с")
        self.assertEqual(fmt_rate(None), "—")
        self.assertEqual(nice_ceil(1900), 2000)
        self.assertEqual(nice_ceil(230), 240)


if __name__ == "__main__":
    unittest.main()
