"""Окно BadLink: настройки канала слева, графики и статистика справа."""
import collections
import csv
import json
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from . import __version__, sysinfo
from .chart import Chart, fmt_rate
from .engine import DOWN, UP, Engine, Params
from .iperf import IperfClient, IperfServer, parse_line

APP_NAME = "BadLink — эмулятор плохого канала"
SPAN = 120   # секунд на графиках

C_DOWN = "#4fc1ff"
C_DOWN_FILL = "#1d3a4d"
C_UP = "#b48cff"
C_UP_FILL = "#33294d"
C_LIMIT = "#f0c05a"
C_OFFER = "#6b6f76"
C_PING = "#5ad17a"
C_IPERF = "#ff9f43"

# Стандартные настройки канала для каждого режима (кнопка «Сбросить к стандартным»).
# «real» подогнан по реальным замерам iperf3 -R на объекте: итог ~2 Мбит/с, секунды от 0 до ~7 Мбит/с,
# одиночные нули и изредка серии по 4–5, 2–3 прерывания в минуту, всплеск после прерывания.
MODE_DEFAULTS = {
    "real": dict(down_rate="2,7", down_swing="3000", up_same=True, up_rate="2,7", up_swing="3000",
                 step_ms="800", smooth=False, queue_ms="100",
                 out_en=True, out_per_min="3", out_min="0,5", out_max="6", out_mode="hold"),
    "uniform": dict(down_rate="1,5", down_swing="500", up_same=True, up_rate="1,5", up_swing="500",
                    step_ms="1000", smooth=False, queue_ms="100",
                    out_en=False, out_per_min="2", out_min="1", out_max="5", out_mode="drop"),
}
MODE_HINTS = {
    "real": "Разброс — типичное отклонение: чаще ниже средней, изредка всплески до 3,5×. "
            "iperf покажет примерно на четверть меньше средней: стандартные 2,7 Мбит/с дают в iperf ≈ 2 Мбит/с.",
    "uniform": "Скорость равномерно гуляет в пределах средняя ± разброс.",
}

DEFAULTS = dict(
    mode="real", **MODE_DEFAULTS["real"], manual_s="3",
    seed="", peer="", ping_on=True,
    ip_time="90", ip_rev=True, ip_udp=False, ip_bitrate="", ip_len="8K", ip_parallel="1", ip_port="5201",
    srv_auto=True, csv_on=False, iface="",
)


def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def num(s):
    return float(str(s).strip().replace(",", ".").replace(" ", ""))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME}  v{__version__}")
        self.geometry("1320x860")
        self.minsize(1100, 720)
        try:
            ttk.Style(self).theme_use("vista")
        except tk.TclError:
            pass

        self.engine = Engine()
        self.io = None
        self.ifaces = []
        self.settings_path = os.path.join(app_dir(), "settings.json")
        if "--settings" in sys.argv[:-1]:            # --settings <файл>: отдельный профиль настроек
            self.settings_path = os.path.abspath(sys.argv[sys.argv.index("--settings") + 1])
        self.v = {}
        self._entries = {}
        self._apply_job = None
        self._ui_queue = collections.deque()   # (тип, данные) из фоновых потоков

        # история по секундам
        self.h = {k: collections.deque(maxlen=SPAN) for k in
                  ("down", "down_lim", "down_in", "up", "up_lim", "up_in", "outage", "ping", "ping_lost", "iperf")}
        self._prev = None            # (t, rx_down, tx_down, rx_up, tx_up) — байтовые счётчики
        self._lim_acc = [[], []]
        self._outage_acc = False
        self._ping_acc = []
        self._iperf_acc = None
        self._ping_series = collections.deque(maxlen=300)   # rtt | None для статистики
        self._ping_lost_run = 0
        self._last_rate = [0.0, 0.0, 0.0, 0.0]
        self._csv = None
        self._csv_writer = None
        self._next_sec = time.monotonic() + 1

        self._load_settings()
        self._build()
        self._bind_traces()

        self.pinger = sysinfo.Pinger(lambda t, rtt, err: self._ui_queue.append(("ping", (t, rtt, err))))
        self.iperf_srv = IperfServer(lambda line: self._ui_queue.append(("srv", line)))
        self.iperf_cli = IperfClient(
            on_interval=lambda sec, kbps: self._ui_queue.append(("cli_iv", kbps)),
            on_line=lambda line: self._ui_queue.append(("cli_line", line)),
            on_done=lambda totals, code: self._ui_queue.append(("cli_done", (totals, code))))

        hk_failed = sysinfo.register_hotkey(lambda: self._ui_queue.append(("hotkey", None)))
        self.after(500, lambda: hk_failed.is_set() and self.log("Ctrl+Shift+F12 занята другой программой"))

        if self.v["srv_auto"].get():
            self._srv_start()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._load_ifaces()
        self.after(250, self._fast_tick)

        if not sysinfo.is_admin():
            self.admin_bar.pack(fill="x", side="top", before=self.top)

    # ================= построение окна =================
    def _var(self, name):
        val = self._saved.get(name, DEFAULTS[name])
        v = tk.BooleanVar(value=bool(val)) if isinstance(DEFAULTS[name], bool) else tk.StringVar(value=str(val))
        self.v[name] = v
        return v

    def _num_entry(self, parent, name, width=8):
        e = tk.Entry(parent, textvariable=self._var(name), width=width, justify="right",
                     relief="solid", bd=1)
        self._entries[name] = e
        return e

    def _build(self):
        # полоса «нет прав»
        self.admin_bar = tk.Frame(self, bg="#7a2e2e")
        tk.Label(self.admin_bar, text="Нет прав администратора — перехват трафика невозможен.",
                 bg="#7a2e2e", fg="white", font=("Segoe UI", 10, "bold")).pack(side="left", padx=10, pady=6)
        ttk.Button(self.admin_bar, text="Перезапустить от администратора",
                   command=self._relaunch_admin).pack(side="left", padx=6)

        # верхняя панель
        self.top = ttk.Frame(self, padding=(10, 8, 10, 4))
        self.top.pack(fill="x")
        ttk.Label(self.top, text="Интерфейс:").pack(side="left")
        self._var("iface")
        self.iface_cb = ttk.Combobox(self.top, state="readonly", width=70, values=["загрузка списка…"])
        self.iface_cb.pack(side="left", padx=(6, 4))
        self.iface_cb.bind("<<ComboboxSelected>>", lambda e: self._iface_selected())
        self.refresh_btn = ttk.Button(self.top, text="⟳", width=3, command=self._load_ifaces)
        self.refresh_btn.pack(side="left")

        self.start_btn = tk.Button(self.top, text="▶  СТАРТ", width=14, font=("Segoe UI", 11, "bold"),
                                   bg="#2e7d32", fg="white", activebackground="#1b5e20", activeforeground="white",
                                   relief="flat", command=self.toggle)
        self.start_btn.pack(side="right")
        f = ttk.Frame(self.top)
        f.pack(side="right", padx=12)
        self.outage_btn = ttk.Button(f, text="Оборвать сейчас на", command=self._manual_outage)
        self.outage_btn.pack(side="left")
        self._num_entry(f, "manual_s", 4).pack(side="left", padx=3)
        ttk.Label(f, text="с").pack(side="left")

        body = ttk.Frame(self, padding=(10, 4, 10, 4))
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body, width=380)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))

        nb = ttk.Notebook(left)
        nb.pack(fill="both", expand=True)
        self._build_channel_tab(nb)
        self._build_test_tab(nb)
        self._build_log_tab(nb)
        self._build_right(right)

        # строка состояния
        sb = ttk.Frame(self, padding=(10, 2, 10, 6))
        sb.pack(fill="x", side="bottom")
        self.status = tk.Label(sb, text="Эмуляция выключена — канал без ограничений", anchor="w",
                               font=("Segoe UI", 10, "bold"), fg="#555")
        self.status.pack(side="left")
        ttk.Label(sb, text="Ctrl+Shift+F12 — стоп из любого окна", foreground="#888").pack(side="right")

    def _row(self, parent, label, *widgets, pady=2):
        r = ttk.Frame(parent)
        r.pack(fill="x", pady=pady)
        ttk.Label(r, text=label, width=24).pack(side="left")
        for w in widgets:
            if isinstance(w, str):
                ttk.Label(r, text=w).pack(side="left", padx=(3, 6))
            else:
                w.pack(in_=r, side="left")   # виджет создан в родителе строки — поднимаем над фреймом строки
                w.lift(r)
        return r

    def _build_channel_tab(self, nb):
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text="Канал")

        g = ttk.LabelFrame(tab, text=" Как меняется скорость ", padding=6)
        g.pack(fill="x")
        self._var("mode")
        ttk.Radiobutton(g, text="Как реальный канал (провалы и всплески)", value="real",
                        variable=self.v["mode"], command=self._sync_mode_hint).pack(anchor="w")
        ttk.Radiobutton(g, text="Равномерно: средняя ± разброс", value="uniform",
                        variable=self.v["mode"], command=self._sync_mode_hint).pack(anchor="w")
        self.mode_hint = ttk.Label(g, text="", foreground="#666", wraplength=340, justify="left")
        self.mode_hint.pack(anchor="w", pady=(2, 4))
        self._row(g, "Смена скорости каждые", self._num_entry(g, "step_ms"), "мс (±40%)")
        ttk.Checkbutton(g, text="Плавный переход (иначе ступенькой)", variable=self._var("smooth")).pack(anchor="w")
        ttk.Button(g, text="Сбросить к стандартным для режима",
                   command=self._reset_mode_defaults).pack(anchor="w", pady=(4, 0))

        g = ttk.LabelFrame(tab, text=" Приём (к этому ПК) ", padding=6)
        g.pack(fill="x", pady=(8, 0))
        self._row(g, "Средняя скорость", self._num_entry(g, "down_rate"), "Мбит/с")
        self._row(g, "Разброс", self._num_entry(g, "down_swing"), "кбит/с")

        g = ttk.LabelFrame(tab, text=" Отдача (от этого ПК) ", padding=6)
        g.pack(fill="x", pady=(8, 0))
        ttk.Checkbutton(g, text="Как приём", variable=self._var("up_same"),
                        command=self._sync_up_state).pack(anchor="w")
        self._row(g, "Средняя скорость", self._num_entry(g, "up_rate"), "Мбит/с")
        self._row(g, "Разброс", self._num_entry(g, "up_swing"), "кбит/с")
        ttk.Label(tab, text="0 = без ограничения в этом направлении", foreground="#888").pack(anchor="w")

        g = ttk.LabelFrame(tab, text=" Прерывания (0 в обе стороны) ", padding=6)
        g.pack(fill="x", pady=(8, 0))
        ttk.Checkbutton(g, text="Включить случайные прерывания", variable=self._var("out_en")).pack(anchor="w")
        self._row(g, "В среднем в минуту", self._num_entry(g, "out_per_min"), "раз")
        self._row(g, "Длительность, с", self._num_entry(g, "out_min", 5), "–", self._num_entry(g, "out_max", 5))
        r = ttk.Frame(g)
        r.pack(fill="x", pady=2)
        self._var("out_mode")
        ttk.Radiobutton(r, text="Обрыв (пакеты теряются)", value="drop",
                        variable=self.v["out_mode"]).pack(anchor="w")
        ttk.Radiobutton(r, text="Замирание (копятся и уходят после)", value="hold",
                        variable=self.v["out_mode"]).pack(anchor="w")

        g = ttk.LabelFrame(tab, text=" Дополнительно ", padding=6)
        g.pack(fill="x", pady=(8, 0))
        self._row(g, "Очередь узкого места", self._num_entry(g, "queue_ms"), "мс")
        self._row(g, "Seed (пусто = случайный)", self._num_entry(g, "seed", 10))
        self.seed_lbl = ttk.Label(g, text="", foreground="#888")
        self.seed_lbl.pack(anchor="w")
        ttk.Checkbutton(g, text="Писать статистику в CSV (папка logs)", variable=self._var("csv_on"),
                        command=self._csv_toggle).pack(anchor="w", pady=(4, 0))
        self._sync_up_state()
        self._sync_mode_hint()

    def _build_test_tab(self, nb):
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text="Тест / iperf3")

        g = ttk.LabelFrame(tab, text=" Сосед по каналу ", padding=6)
        g.pack(fill="x")
        self._row(g, "IP соседа", self._num_entry(g, "peer", 16))
        self._entries["peer"].configure(justify="left")
        ttk.Checkbutton(g, text="Пинговать раз в секунду (с IP интерфейса)", variable=self._var("ping_on"),
                        command=self._ping_restart).pack(anchor="w")
        self._entries["peer"].bind("<FocusOut>", lambda e: self._ping_restart())
        self._entries["peer"].bind("<Return>", lambda e: self._ping_restart())

        g = ttk.LabelFrame(tab, text=" iperf3-клиент ", padding=6)
        g.pack(fill="x", pady=(8, 0))
        self._row(g, "Длительность", self._num_entry(g, "ip_time"), "с")
        ttk.Checkbutton(g, text="-R  (сосед шлёт нам — меряем приём)", variable=self._var("ip_rev")).pack(anchor="w")
        ttk.Checkbutton(g, text="-u  UDP", variable=self._var("ip_udp")).pack(anchor="w")
        self._row(g, "-b  битрейт (напр. 5M)", self._num_entry(g, "ip_bitrate"))
        self._entries["ip_bitrate"].configure(justify="left")
        self._row(g, "-l  блок", self._num_entry(g, "ip_len"))
        self._entries["ip_len"].configure(justify="left")
        ttk.Label(g, text="Блок 8K обязателен на низких скоростях: с блоком по умолчанию (128K)\n"
                          "Windows-iperf3 показывает ступеньки 1,05 / 2,1 / 0 Мбит/с вместо реальной скорости.",
                  foreground="#a35d00", wraplength=340, justify="left").pack(anchor="w", pady=(0, 2))
        self._row(g, "-P  потоков", self._num_entry(g, "ip_parallel"))
        self._row(g, "Порт", self._num_entry(g, "ip_port"))
        r = ttk.Frame(g)
        r.pack(fill="x", pady=(6, 0))
        self.iperf_btn = ttk.Button(r, text="Запустить iperf3", command=self._iperf_toggle)
        self.iperf_btn.pack(side="left")
        self.iperf_res = ttk.Label(g, text="", foreground="#333", wraplength=340, justify="left")
        self.iperf_res.pack(anchor="w", pady=(6, 0))

        g = ttk.LabelFrame(tab, text=" iperf3-сервер ", padding=6)
        g.pack(fill="x", pady=(8, 0))
        ttk.Checkbutton(g, text="Запускать при старте программы", variable=self._var("srv_auto")).pack(anchor="w")
        r = ttk.Frame(g)
        r.pack(fill="x", pady=(4, 0))
        self.srv_btn = ttk.Button(r, text="Остановить сервер", command=self._srv_toggle)
        self.srv_btn.pack(side="left")
        self.srv_lbl = ttk.Label(g, text="", foreground="#888", wraplength=340)
        self.srv_lbl.pack(anchor="w", pady=(4, 0))
        ttk.Label(tab, text="На второй машине: iperf3 -c <IP этого ПК> -R -t 90 -l 8K",
                  foreground="#888").pack(anchor="w", pady=(8, 0))

        g = ttk.LabelFrame(tab, text=" Вывод iperf3 ", padding=4)
        g.pack(fill="both", expand=True, pady=(8, 0))
        self.iperf_out = tk.Text(g, height=8, font=("Consolas", 8), wrap="none", bg="#fafafa", relief="flat")
        self.iperf_out.pack(fill="both", expand=True)

    def _build_log_tab(self, nb):
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text="Журнал")
        self.log_txt = tk.Text(tab, font=("Consolas", 9), wrap="word", relief="flat", bg="#fafafa")
        sb = ttk.Scrollbar(tab, command=self.log_txt.yview)
        self.log_txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log_txt.pack(fill="both", expand=True)
        self._engine_log_seen = 0

    def _build_right(self, right):
        self.ch_down = Chart(right, "Приём", "rate", SPAN, height=150)
        self.ch_up = Chart(right, "Отдача", "rate", SPAN, height=150)
        self.ch_ping = Chart(right, "Пинг до соседа", "ms", SPAN, height=110)
        self.ch_iperf = Chart(right, "iperf3 (по секундам)", "rate", SPAN, height=120)
        for c in (self.ch_down, self.ch_up, self.ch_ping, self.ch_iperf):
            c.pack(fill="both", expand=True, pady=(0, 4))

        bottom = ttk.Frame(right)
        bottom.pack(fill="x", pady=(4, 0))
        cols = ("down", "up")
        self.tbl = ttk.Treeview(bottom, columns=cols, height=7, selectmode="none")
        self.tbl.heading("#0", text="")
        self.tbl.heading("down", text="Приём")
        self.tbl.heading("up", text="Отдача")
        self.tbl.column("#0", width=200, stretch=False)
        self.tbl.column("down", width=150, anchor="e")
        self.tbl.column("up", width=150, anchor="e")
        self._rows = {}
        for key, label in (("lim", "Лимит сейчас"), ("act", "Прошло за 1 с"), ("inp", "Пришло на вход за 1 с"),
                           ("tot", "Всего прошло"), ("dq", "Отброшено: очередь полна"),
                           ("do", "Отброшено: прерывания"), ("q", "В очереди сейчас")):
            self._rows[key] = self.tbl.insert("", "end", text=label, values=("—", "—"))
        self.tbl.pack(side="left")

        side = ttk.Frame(bottom, padding=(14, 0, 0, 0))
        side.pack(side="left", fill="both", expand=True)
        self.out_lbl = tk.Label(side, text="", font=("Segoe UI", 10, "bold"), anchor="w", justify="left")
        self.out_lbl.pack(anchor="w")
        self.ping_lbl = ttk.Label(side, text="", justify="left")
        self.ping_lbl.pack(anchor="w", pady=(6, 0))
        self.info_lbl = ttk.Label(side, text="", justify="left", foreground="#666")
        self.info_lbl.pack(anchor="w", pady=(6, 0))

    # ================= настройки =================
    def _load_settings(self):
        try:
            with open(self.settings_path, encoding="utf-8-sig") as f:   # -sig: файл мог быть сохранён с BOM
                self._saved = json.load(f)
        except Exception:
            self._saved = {}

    def _save_settings(self):
        data = {k: v.get() for k, v in self.v.items()}
        try:
            with open(self.settings_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
        except OSError:
            pass

    def _bind_traces(self):
        for name, v in self.v.items():
            if name in ("peer", "iface"):
                continue
            v.trace_add("write", lambda *a: self._schedule_apply())
        self._validate()

    def _schedule_apply(self):
        if self._apply_job:
            self.after_cancel(self._apply_job)
        self._apply_job = self.after(300, self._apply)

    def _sync_up_state(self):
        same = self.v["up_same"].get()
        for n in ("up_rate", "up_swing"):
            self._entries[n].configure(state="disabled" if same else "normal")

    def _sync_mode_hint(self):
        self.mode_hint.configure(text=MODE_HINTS.get(self.v["mode"].get(), ""))

    def _reset_mode_defaults(self):
        """Стандартные настройки канала для выбранного режима. Seed, интерфейс и тестовые поля не трогаем."""
        mode = self.v["mode"].get()
        for name, val in MODE_DEFAULTS[mode].items():
            self.v[name].set(val)
        self._sync_up_state()
        title = "реальный канал" if mode == "real" else "равномерно"
        self.log(f"Настройки канала сброшены к стандартным для режима «{title}»")

    def _validate(self):
        """Проверка полей; неверные подсвечиваются. Возвращает Params или None."""
        checks = dict(down_rate=(0, 1000), down_swing=(0, 1e6), up_rate=(0, 1000), up_swing=(0, 1e6),
                      step_ms=(50, 60000), queue_ms=(0, 60000), out_per_min=(0.01, 60),
                      out_min=(0.1, 600), out_max=(0.1, 600), manual_s=(0.1, 600))
        vals, ok = {}, True
        for name, (lo, hi) in checks.items():
            try:
                x = num(self.v[name].get())
                good = lo <= x <= hi
            except ValueError:
                x, good = None, False
            vals[name] = x
            self._entries[name].configure(bg="white" if good else "#ffc9c9")
            ok &= good
        seed = self.v["seed"].get().strip()
        seed_ok = seed == "" or seed.isdigit()
        self._entries["seed"].configure(bg="white" if seed_ok else "#ffc9c9")
        if not ok or not seed_ok:
            return None
        same = self.v["up_same"].get()
        up_rate = vals["down_rate"] if same else vals["up_rate"]
        up_swing = vals["down_swing"] if same else vals["up_swing"]
        return Params(
            down_kbps=vals["down_rate"] * 1000, down_swing_kbps=vals["down_swing"],
            up_kbps=up_rate * 1000, up_swing_kbps=up_swing,
            step_ms=vals["step_ms"], smooth=self.v["smooth"].get(), queue_ms=vals["queue_ms"],
            realistic=self.v["mode"].get() != "uniform",
            outage_enabled=self.v["out_en"].get(), outage_per_min=vals["out_per_min"],
            outage_min_s=vals["out_min"], outage_max_s=vals["out_max"],
            outage_hold=self.v["out_mode"].get() == "hold")

    def _apply(self):
        self._apply_job = None
        p = self._validate()
        if p:
            self.engine.set_params(p)
        return p

    # ================= интерфейсы =================
    def _load_ifaces(self):
        self.refresh_btn.configure(state="disabled")

        def work():
            data = sysinfo.list_interfaces()
            self._ui_queue.append(("ifaces", data))

        threading.Thread(target=work, daemon=True).start()

    def _iface_label(self, a):
        ips = ", ".join(a["ips"][:2]) or "нет IP"
        return f"[{a['idx']}] {a['name']} — {ips} — {a['status']}, {a['speed']} — {a['desc']}"

    def _set_ifaces(self, data):
        self.refresh_btn.configure(state="normal")
        self.ifaces = data
        self.iface_cb.configure(values=[self._iface_label(a) for a in data])
        want = self.v["iface"].get()
        idx = next((i for i, a in enumerate(data) if a["name"] == want), None)
        saved_found = idx is not None
        if idx is None:
            idx = next((i for i, a in enumerate(data) if a["status"] == "Up" and sysinfo.ipv4_of(a)), 0)
        if data:
            self.iface_cb.current(idx)
            self._iface_selected()
        else:
            self.iface_cb.set("интерфейсы не найдены")
        # ключи командной строки: --start — сразу включить эмуляцию, --iperf — сразу запустить iperf3-клиент
        if "--start" in sys.argv and not self.engine.running and not getattr(self, "_autostarted", False):
            self._autostarted = True
            if not saved_found:
                # никогда не включаем эмуляцию «на первом попавшемся» интерфейсе
                self.log(f"--start: сохранённый интерфейс «{want}» не найден — эмуляция НЕ запущена")
                messagebox.showwarning(APP_NAME, f"--start: интерфейс «{want}» не найден.\nЭмуляция не запущена.")
                return
            self.start()
            if "--iperf" in sys.argv:
                self.after(1500, self._iperf_toggle)

    def current_iface(self):
        i = self.iface_cb.current()
        return self.ifaces[i] if 0 <= i < len(self.ifaces) else None

    def _iface_selected(self):
        a = self.current_iface()
        if not a:
            return
        self.v["iface"].set(a["name"])
        self._prev = None
        self._ping_restart()

    # ================= старт / стоп =================
    def toggle(self):
        if self.engine.running:
            self.stop()
        else:
            self.start()

    def start(self):
        a = self.current_iface()
        if not a:
            messagebox.showerror(APP_NAME, "Не выбран интерфейс")
            return
        p = self._apply()
        if not p:
            messagebox.showerror(APP_NAME, "Исправьте поля, подсвеченные красным")
            return
        from .windivert import DivertIO, WinDivertError
        filt = sysinfo.build_filter(a["idx"])
        try:
            self.io = DivertIO(filt)
        except (WinDivertError, OSError) as e:
            messagebox.showerror(APP_NAME, f"Не удалось запустить перехват:\n\n{e}")
            self.log(f"Ошибка запуска: {e}")
            return
        seed = self.v["seed"].get().strip()
        self.engine.start(self.io, int(seed) if seed else None)
        self._prev = None
        self.seed_lbl.configure(text=f"Текущий seed: {self.engine.seed} (впишите его, чтобы повторить прогон)")
        self.log(f"Фильтр: {filt}")
        self.start_btn.configure(text="■  СТОП", bg="#c62828", activebackground="#8e0000")
        self.iface_cb.configure(state="disabled")
        self.refresh_btn.configure(state="disabled")
        self._entries["seed"].configure(state="disabled")
        self._ping_restart()

    def stop(self):
        if not self.engine.running:
            return
        self.engine.stop()
        self.io = None
        self._prev = None
        self.start_btn.configure(text="▶  СТАРТ", bg="#2e7d32", activebackground="#1b5e20")
        self.iface_cb.configure(state="readonly")
        self.refresh_btn.configure(state="normal")
        self._entries["seed"].configure(state="normal")

    def _manual_outage(self):
        if not self.engine.running:
            messagebox.showinfo(APP_NAME, "Сначала запустите эмуляцию")
            return
        try:
            s = num(self.v["manual_s"].get())
        except ValueError:
            return
        self.engine.trigger_outage(s)

    def _relaunch_admin(self):
        if sysinfo.relaunch_as_admin():
            self._on_close()

    # ================= пинг / iperf =================
    def _ping_restart(self):
        peer = self.v["peer"].get().strip()
        self._save_settings()
        if not self.v["ping_on"].get() or not peer:
            self.pinger.stop()
            return
        a = self.current_iface()
        self.pinger.start(peer, sysinfo.ipv4_of(a) if a else None)

    def _iperf_toggle(self):
        if self.iperf_cli.running:
            self.iperf_cli.stop()
            return
        peer = self.v["peer"].get().strip()
        if not peer:
            messagebox.showinfo(APP_NAME, "Укажите IP соседа")
            return
        try:
            secs = int(num(self.v["ip_time"].get()))
            par = max(1, int(num(self.v["ip_parallel"].get() or "1")))
            port = int(num(self.v["ip_port"].get()))
        except ValueError:
            messagebox.showerror(APP_NAME, "Проверьте длительность / потоки / порт")
            return
        a = self.current_iface()
        self.iperf_out.delete("1.0", "end")
        self.iperf_res.configure(text="идёт тест…")
        ok = self.iperf_cli.start(peer, port, secs, reverse=self.v["ip_rev"].get(), udp=self.v["ip_udp"].get(),
                                  bitrate=self.v["ip_bitrate"].get().strip(), parallel=par,
                                  block=self.v["ip_len"].get().strip(), bind=sysinfo.ipv4_of(a) if a else None)
        if ok:
            self.iperf_btn.configure(text="Остановить iperf3")

    def _srv_start(self):
        try:
            port = int(num(self.v["ip_port"].get()))
        except ValueError:
            port = 5201
        if self.iperf_srv.start(port):
            self.srv_btn.configure(text="Остановить сервер")

    def _srv_toggle(self):
        if self.iperf_srv.running:
            self.iperf_srv.stop()
            self.srv_btn.configure(text="Запустить сервер")
            self.srv_lbl.configure(text="сервер остановлен")
        else:
            self._srv_start()

    # ================= журнал / CSV =================
    def log(self, text):
        self.log_txt.insert("end", time.strftime("%H:%M:%S  ") + text + "\n")
        self.log_txt.see("end")

    def _csv_toggle(self):
        if self.v["csv_on"].get():
            d = os.path.join(app_dir(), "logs")
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, time.strftime("badlink_%Y%m%d_%H%M%S.csv"))
            self._csv = open(path, "w", newline="", encoding="utf-8-sig")
            self._csv_writer = csv.writer(self._csv, delimiter=";")
            self._csv_writer.writerow(["время", "эмуляция", "приём_лимит_кбит", "приём_кбит", "приём_вход_кбит",
                                       "отдача_лимит_кбит", "отдача_кбит", "отдача_вход_кбит", "прерывание",
                                       "отброс_очередь", "отброс_прерывания", "пинг_мс", "iperf_кбит"])
            self.log(f"CSV: {path}")
        elif self._csv:
            self._csv.close()
            self._csv = self._csv_writer = None

    # ================= тики =================
    def _fast_tick(self):
        try:
            self._drain_queue()
            if self.engine.running:
                s = self.engine.snapshot()
                for i in (DOWN, UP):
                    self._lim_acc[i].append(s["dirs"][i]["limit_kbps"])
                self._outage_acc |= s["outage"]
            self._sync_engine_log()
            self._update_status()
            if time.monotonic() >= self._next_sec:
                self._next_sec += 1
                if self._next_sec < time.monotonic():   # отстали (окно двигали) — не догоняем
                    self._next_sec = time.monotonic() + 1
                self._second_tick()
        finally:
            self.after(250, self._fast_tick)

    def _drain_queue(self):
        q = self._ui_queue
        while q:
            kind, data = q.popleft()
            if kind == "ping":
                t, rtt, err = data
                self._ping_acc.append(rtt)
            elif kind == "srv":
                self._on_srv_line(data)
            elif kind == "cli_iv":
                self._iperf_acc = data
            elif kind == "cli_line":
                self.iperf_out.insert("end", data + "\n")
                self.iperf_out.see("end")
            elif kind == "cli_done":
                totals, code = data
                self.iperf_btn.configure(text="Запустить iperf3")
                if totals:
                    parts = [f"{k}: {fmt_rate(v)}" for k, v in totals.items()]
                    self.iperf_res.configure(text="Итог — " + ", ".join(parts))
                    self.log("iperf3 итог: " + ", ".join(parts))
                else:
                    self.iperf_res.configure(text=f"iperf3 завершился без итогов (код {code}) — см. вывод")
            elif kind == "ifaces":
                self._set_ifaces(data)
            elif kind == "hotkey":
                if self.engine.running:
                    self.log("Стоп по Ctrl+Shift+F12")
                    self.stop()

    def _on_srv_line(self, line):
        r = parse_line(line)
        if r and not r["final"]:
            # интервалы входящего теста от соседа — на график iperf (SUM приходит последним и перекрывает)
            if r["end"] - r["start"] > 0.2:
                self._iperf_acc = r["kbps"]
            return
        if line.startswith("-") or not line.strip():
            return
        if "Server listening" in line:
            self.srv_lbl.configure(text=f"слушает порт {self.iperf_srv.port}")
            return
        self.srv_lbl.configure(text=line[:120])
        self.log("[iperf3-сервер] " + line)

    def _sync_engine_log(self):
        ev = self.engine.events
        # events — deque(maxlen); считаем по времени последнего показанного
        new = [e for e in ev if e[0] > getattr(self, "_last_ev_t", 0)]
        for t, text in new:
            self.log_txt.insert("end", time.strftime("%H:%M:%S  ", time.localtime(t)) + text + "\n")
            self._last_ev_t = t
        if new:
            self.log_txt.see("end")

    def _second_tick(self):
        now = time.monotonic()
        running = self.engine.running
        a = self.current_iface()
        rates = [None] * 4    # down, down_in, up, up_in (кбит/с)
        drops = (0, 0)
        if running:
            s = self.engine.snapshot()
            d, u = s["dirs"][DOWN], s["dirs"][UP]
            cur = (now, d["rx_bytes"], d["tx_bytes"], u["rx_bytes"], u["tx_bytes"])
            drops = (d["drop_queue"] + u["drop_queue"], d["drop_outage"] + u["drop_outage"])
            if self._prev and self._prev[0] == "eng":
                p = self._prev[1]
                dt = max(1e-3, cur[0] - p[0])
                rates = [(cur[2] - p[2]) * 8 / dt / 1000, (cur[1] - p[1]) * 8 / dt / 1000,
                         (cur[4] - p[4]) * 8 / dt / 1000, (cur[3] - p[3]) * 8 / dt / 1000]
            self._prev = ("eng", cur)
        elif a:
            c = sysinfo.if_counters(a["idx"])
            if c:
                cur = (now, c[0], c[1])
                if self._prev and self._prev[0] == "os":
                    p = self._prev[1]
                    dt = max(1e-3, cur[0] - p[0])
                    dn = (cur[1] - p[1]) * 8 / dt / 1000
                    up = (cur[2] - p[2]) * 8 / dt / 1000
                    rates = [dn, dn, up, up]
                self._prev = ("os", cur)

        lim = []
        for i in (DOWN, UP):
            vals = [x for x in self._lim_acc[i] if x is not None]
            lim.append(sum(vals) / len(vals) if vals and len(vals) == len(self._lim_acc[i]) else None)
            self._lim_acc[i] = []
        outage = self._outage_acc
        self._outage_acc = False

        # пинг за секунду
        acc, self._ping_acc = self._ping_acc, []
        got = [x for x in acc if x is not None]
        ping = got[-1] if got else None
        lost = bool(acc) and not got
        for x in acc:
            self._ping_series.append(x)
            self._ping_lost_run = 0 if x is not None else self._ping_lost_run + 1
        iperf, self._iperf_acc = self._iperf_acc, None

        h = self.h
        h["down"].append(rates[0]); h["down_in"].append(rates[1]); h["down_lim"].append(lim[0] if running else None)
        h["up"].append(rates[2]); h["up_in"].append(rates[3]); h["up_lim"].append(lim[1] if running else None)
        h["outage"].append(outage); h["ping"].append(ping); h["ping_lost"].append(lost); h["iperf"].append(iperf)
        self._last_rate = rates

        self._draw_charts(running)
        self._update_table(running, rates, lim)
        if self._csv_writer:
            r = lambda x: "" if x is None else f"{x:.0f}"
            self._csv_writer.writerow([time.strftime("%H:%M:%S"), int(running), r(lim[0]), r(rates[0]), r(rates[1]),
                                       r(lim[1]), r(rates[2]), r(rates[3]), int(outage), drops[0], drops[1],
                                       "" if ping is None else f"{ping:.1f}", r(iperf)])
            self._csv.flush()

    def _draw_charts(self, running):
        h = self.h
        shade = list(h["outage"])
        for ch, key, color, fill in ((self.ch_down, "down", C_DOWN, C_DOWN_FILL), (self.ch_up, "up", C_UP, C_UP_FILL)):
            series = [dict(values=list(h[key + "_in"]), color=C_OFFER, style="line"),
                      dict(values=list(h[key]), color=color, fill=fill, style="area"),
                      dict(values=list(h[key + "_lim"]), color=C_LIMIT, style="dash")]
            last = h[key][-1] if h[key] else None
            lim = h[key + "_lim"][-1] if h[key + "_lim"] else None
            legend = f"прошло {fmt_rate(last)}"
            if running:
                legend += f"   лимит {fmt_rate(lim)}   ▬ серая — пришло на вход"
            ch.set(series, shade, legend=legend)
        pv = [x for x in h["ping"] if x is not None]
        self.ch_ping.set([dict(values=list(h["ping"]), color=C_PING, style="line")], shade,
                         marks=list(h["ping_lost"]),
                         legend=(f"последний {pv[-1]:.1f} мс   ▼ — потерян" if pv else "▼ — потерян"))
        iv = list(h["iperf"])
        lastv = next((x for x in reversed(iv) if x is not None), None)
        self.ch_iperf.set([dict(values=iv, color=C_IPERF, style="bar")], shade,
                          legend=f"последняя секунда {fmt_rate(lastv)}" if lastv is not None else "нет теста")

    def _update_table(self, running, rates, lim):
        def both(fn):
            return tuple(fn(i) for i in (DOWN, UP))

        tv = self.tbl
        if running:
            s = self.engine.snapshot()
            dirs = s["dirs"]
            tv.item(self._rows["lim"], values=both(
                lambda i: "без лимита" if dirs[i]["limit_kbps"] is None else fmt_rate(dirs[i]["limit_kbps"])))
            tv.item(self._rows["tot"], values=both(
                lambda i: f"{dirs[i]['tx_bytes'] / 1e6:.2f} МБ / {dirs[i]['tx_pkts']} пак."))
            tv.item(self._rows["dq"], values=both(lambda i: f"{dirs[i]['drop_queue']} пак."))
            tv.item(self._rows["do"], values=both(lambda i: f"{dirs[i]['drop_outage']} пак."))
            tv.item(self._rows["q"], values=both(lambda i: f"{dirs[i]['qbytes'] / 1024:.1f} КБ"))
        else:
            for k in ("lim", "tot", "dq", "do", "q"):
                tv.item(self._rows[k], values=("—", "—"))
        tv.item(self._rows["act"], values=(fmt_rate(rates[0]), fmt_rate(rates[2])))
        tv.item(self._rows["inp"], values=(fmt_rate(rates[1]), fmt_rate(rates[3])) if running else ("—", "—"))

        ps = [x for x in self._ping_series if x is not None]
        n = len(self._ping_series)
        if n:
            lost = n - len(ps)
            txt = f"Пинг (последние {n}): потери {lost} ({100 * lost / n:.0f}%), подряд сейчас {self._ping_lost_run}"
            if ps:
                txt += f"\nмин {min(ps):.1f} / ср {sum(ps) / len(ps):.1f} / макс {max(ps):.1f} мс"
            self.ping_lbl.configure(text=txt)
        else:
            self.ping_lbl.configure(text="Пинг: укажите IP соседа на вкладке «Тест / iperf3»")

    def _update_status(self):
        e = self.engine
        if e.running:
            s = e.snapshot()
            if s["outage"]:
                self.status.configure(text=f"ПРЕРЫВАНИЕ — канал в нуле ещё {s['outage_left']:.1f} с", fg="#c62828")
                self.out_lbl.configure(text=f"● ПРЕРЫВАНИЕ ({s['outage_left']:.1f} с)", fg="#c62828")
            else:
                a = self.current_iface()
                self.status.configure(text=f"Эмуляция включена на «{a['name'] if a else '?'}»", fg="#2e7d32")
                self.out_lbl.configure(text="● канал есть", fg="#2e7d32")
            self.info_lbl.configure(text=f"Прерываний: {s['outage_count']}, суммарно {s['outage_total_s']:.1f} с\n"
                                         f"seed {e.seed}")
        else:
            self.status.configure(text="Эмуляция выключена — канал без ограничений", fg="#555")
            self.out_lbl.configure(text="")
            self.info_lbl.configure(text="")

    # ================= выход =================
    def _on_close(self):
        try:
            self.stop()
        finally:
            self.iperf_cli.stop()
            self.iperf_srv.stop()
            self.pinger.stop()
            self._save_settings()
            if self._csv:
                self._csv.close()
            self.destroy()


def main():
    if not sysinfo.is_admin() and "--no-elevate" not in sys.argv:
        if sysinfo.relaunch_as_admin():
            return
    app = App()
    app.mainloop()
