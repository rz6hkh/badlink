"""Простой график по секундам на tk.Canvas (без matplotlib)."""
import math
import tkinter as tk

BG = "#1e1f22"
GRID = "#34373d"
TEXT = "#a9adb4"
TITLE = "#e3e5e8"
SHADE = "#4a2227"
MARK = "#f2555a"


def nice_ceil(v):
    if v <= 0:
        return 1.0
    e = 10 ** math.floor(math.log10(v))
    for m in (1, 1.2, 1.6, 2, 2.4, 3, 4, 6, 8, 10):   # делятся на 4 без «62.5»
        if v <= m * e:
            return m * e
    return 10 * e


def fmt_rate(kbps, digits=2):
    if kbps is None:
        return "—"
    if kbps >= 1000:
        return f"{kbps / 1000:.{digits}f} Мбит/с"
    return f"{kbps:.0f} кбит/с"


class Chart(tk.Canvas):
    """series: [dict(values=[...], color=str, style='area'|'line'|'dash'|'bar', name=str)]
    values выровнены по правому краю (последняя секунда справа), None — нет данных."""

    def __init__(self, master, title, kind="rate", span=120, height=140, min_scale=None):
        super().__init__(master, height=height, bg=BG, highlightthickness=0)
        self.title = title
        self.kind = kind            # rate (кбит/с) | ms
        self.span = span
        self.min_scale = min_scale if min_scale is not None else (100.0 if kind == "rate" else 10.0)
        self.series = []
        self.shade = []
        self.marks = []
        self.legend = ""
        self.bind("<Configure>", lambda e: self.redraw())

    def set(self, series, shade=None, marks=None, legend=""):
        self.series = series
        self.shade = shade or []
        self.marks = marks or []
        self.legend = legend
        self.redraw()

    def _ylabel(self, v, top):
        if self.kind == "ms":
            return f"{v:.0f} мс"
        if top >= 2000:
            return f"{v / 1000:g} М"
        return f"{v:g} к"

    def redraw(self):
        self.delete("all")
        W, H = self.winfo_width(), self.winfo_height()
        if W < 50 or H < 40:
            return
        L, R, T, B = 50, 8, 20, 16
        w, h = W - L - R, H - T - B
        N = self.span
        slot = w / N

        vmax = 0.0
        for s in self.series:
            for v in s["values"][-N:]:
                if v is not None and v > vmax:
                    vmax = v
        top = nice_ceil(max(self.min_scale, vmax * 1.08))

        def X(i, n):          # i-й элемент из n, выровнено вправо, центр слота
            return L + (N - n + i + 0.5) * slot

        def Y(v):
            return T + h - min(v, top) / top * h

        # подсветка прерываний
        sh = self.shade[-N:]
        n = len(sh)
        for i, on in enumerate(sh):
            if on:
                x0 = L + (N - n + i) * slot
                self.create_rectangle(x0, T, x0 + slot + 0.5, T + h, fill=SHADE, width=0)

        # сетка
        for k in range(5):
            v = top * k / 4
            y = Y(v)
            self.create_line(L, y, L + w, y, fill=GRID)
            self.create_text(L - 4, y, text=self._ylabel(v, top), anchor="e", fill=TEXT, font=("Segoe UI", 8))
        for sec in range(0, N + 1, 30):
            x = L + w - sec * slot
            self.create_line(x, T, x, T + h, fill=GRID, dash=(2, 3))
            anchor = "ne" if sec == 0 else ("nw" if sec == N else "n")
            self.create_text(x, T + h + 2, text=f"-{sec} с" if sec else "сейчас", anchor=anchor,
                             fill=TEXT, font=("Segoe UI", 7))

        # серии
        for s in self.series:
            vals = s["values"][-N:]
            n = len(vals)
            style, color = s.get("style", "line"), s["color"]
            if style == "bar":
                for i, v in enumerate(vals):
                    if v is None:
                        continue
                    x0 = L + (N - n + i) * slot
                    self.create_rectangle(x0 + 0.5, Y(v), x0 + max(1.0, slot - 0.5), T + h,
                                          fill=color, width=0)
                continue
            # непрерывные куски без None
            chunk = []
            chunks = []
            for i, v in enumerate(vals):
                if v is None:
                    if chunk:
                        chunks.append(chunk)
                    chunk = []
                else:
                    chunk.append((X(i, n), Y(v)))
            if chunk:
                chunks.append(chunk)
            for c in chunks:
                if style == "area" and len(c) >= 2:
                    poly = [(c[0][0], T + h)] + c + [(c[-1][0], T + h)]
                    self.create_polygon(*[k for p in poly for k in p], fill=s.get("fill", color), outline="")
                    self.create_line(*[k for p in c for k in p], fill=color, width=1.5)
                elif len(c) >= 2:
                    self.create_line(*[k for p in c for k in p], fill=color, width=1.5 if style == "line" else 1,
                                     dash=(4, 3) if style == "dash" else None)
                elif len(c) == 1:
                    x, y = c[0]
                    self.create_oval(x - 1.5, y - 1.5, x + 1.5, y + 1.5, fill=color, outline="")

        # отметки (потерянные пинги)
        mk = self.marks[-N:]
        n = len(mk)
        for i, on in enumerate(mk):
            if on:
                x = X(i, n)
                self.create_polygon(x - 3, T + 1, x + 3, T + 1, x, T + 7, fill=MARK, outline="")

        self.create_text(L, 3, text=self.title, anchor="nw", fill=TITLE, font=("Segoe UI", 9, "bold"))
        self.create_text(W - R, 3, text=self.legend, anchor="ne", fill=TEXT, font=("Segoe UI", 8))
