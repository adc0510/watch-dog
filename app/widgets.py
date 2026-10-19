"""Custom widgets: live sparkline charts, score ring, stat cards."""
import math

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Graphene, Gsk, Gtk  # noqa: E402

COLORS = {
    "blue": (0.21, 0.52, 0.89), "green": (0.18, 0.76, 0.49), "orange": (1.0, 0.47, 0.0),
    "red": (0.88, 0.11, 0.14), "purple": (0.57, 0.25, 0.85), "teal": (0.13, 0.63, 0.67),
    "yellow": (0.96, 0.76, 0.07),
}

CSS = """
.hero { border-radius: 18px; padding: 26px 30px; }
.hero.ok   { background: linear-gradient(120deg, alpha(#2ec27e,0.28), alpha(#2ec27e,0.08)); }
.hero.warn { background: linear-gradient(120deg, alpha(#e5a50a,0.30), alpha(#e5a50a,0.08)); }
.hero.bad  { background: linear-gradient(120deg, alpha(#e01b24,0.30), alpha(#e01b24,0.08)); }
.hero-title { font-size: 26pt; font-weight: 800; }
.hero-sub { font-size: 12pt; opacity: 0.85; }
.hero-icon.ok { color: #2ec27e; } .hero-icon.warn { color: #e5a50a; } .hero-icon.bad { color: #e01b24; }
.stat { padding: 16px 18px 10px 18px; border-radius: 14px; }
.stat-value { font-size: 22pt; font-weight: 800; font-feature-settings: "tnum"; }
.stat-title { font-weight: 700; opacity: 0.75; }
.stat-sub { font-size: 9pt; opacity: 0.65; }
.stat-icon.blue { color: #3584e4; } .stat-icon.green { color: #2ec27e; } .stat-icon.orange { color: #ff7800; }
.stat-icon.red { color: #e01b24; } .stat-icon.purple { color: #9141ac; } .stat-icon.teal { color: #21a0aa; }
.badge { border-radius: 99px; padding: 2px 10px; font-size: 8.5pt; font-weight: 700; }
.badge.next { background: alpha(#e01b24,0.18); color: #e01b24; }
.badge.first { background: alpha(#e5a50a,0.20); color: #e5a50a; }
.badge.protect { background: alpha(#2ec27e,0.20); color: #2ec27e; }
.badge.system { background: alpha(@window_fg_color,0.10); }
.status-ok { color: #2ec27e; } .status-warn { color: #e5a50a; } .status-bad { color: #e01b24; }
.status-info { color: @accent_color; }
.ev-kill { color: #e01b24; } .ev-crit { color: #e01b24; } .ev-warn { color: #e5a50a; }
.ev-start { color: #2ec27e; } .ev-stop { color: alpha(@window_fg_color,0.6); }
.big-number { font-size: 34pt; font-weight: 800; }
.section-title { font-size: 15pt; font-weight: 800; }
.keycap { border: 1px solid alpha(@window_fg_color,0.3); border-bottom-width: 3px; border-radius: 6px;
          padding: 2px 9px; font-family: monospace; font-weight: 700; background: alpha(@window_fg_color,0.06); }
.guide-step { font-size: 12pt; }
.sidebar-status { padding: 10px 14px; border-radius: 10px; margin: 6px; }
.sidebar-status.ok { background: alpha(#2ec27e,0.15); }
.sidebar-status.bad { background: alpha(#e01b24,0.15); }
.sidebar-status.warn { background: alpha(#e5a50a,0.15); }
levelbar.appbar trough { min-width: 110px; }
.narrow levelbar.appbar trough { min-width: 24px; }
.narrow .badge { padding: 2px 6px; font-size: 7.5pt; }
"""


def _rgba(rgb, a=1.0):
    c = Gdk.RGBA()
    c.red, c.green, c.blue, c.alpha = rgb[0], rgb[1], rgb[2], a
    return c


class Sparkline(Gtk.Widget):
    """Smooth filled line chart of the last N values (0..max). Drawn with GSK (no cairo needed)."""

    def __init__(self, color="blue", maxv=100.0, points=150, height=56):
        super().__init__()
        self.values = []
        self.points = points
        self.maxv = maxv
        self.color = COLORS[color]
        self.height = height
        self.set_hexpand(True)

    def do_measure(self, orientation, for_size):
        if orientation == Gtk.Orientation.VERTICAL:
            return self.height, self.height, -1, -1
        return 60, 200, -1, -1

    def push(self, v):
        self.values.append(v)
        if len(self.values) > self.points:
            self.values.pop(0)
        self.queue_draw()

    def set_color(self, color):
        self.color = COLORS[color]

    def do_snapshot(self, snap):
        w, h = self.get_width(), self.get_height()
        if w <= 0 or h <= 0:
            return
        grid = Gsk.PathBuilder.new()
        for frac in (0.25, 0.5, 0.75):
            grid.move_to(0, h * frac)
            grid.line_to(w, h * frac)
        snap.append_stroke(grid.to_path(), Gsk.Stroke.new(1), _rgba((0.5, 0.5, 0.5), 0.15))
        if len(self.values) < 2:
            return
        top = self.maxv or max(max(self.values) * 1.25, 1e-9)  # maxv=None -> auto-scale
        step = w / (self.points - 1)
        x0 = w - step * (len(self.values) - 1)
        pts = [(x0 + i * step, h - 2 - (h - 4) * max(0.0, min(v, top)) / top)
               for i, v in enumerate(self.values)]

        def curve(pb):
            pb.move_to(*pts[0])
            for (xa, ya), (xb, yb) in zip(pts, pts[1:]):
                xm = (xa + xb) / 2
                pb.cubic_to(xm, ya, xm, yb, xb, yb)

        area = Gsk.PathBuilder.new()
        curve(area)
        area.line_to(pts[-1][0], h)
        area.line_to(pts[0][0], h)
        area.close()
        snap.push_fill(area.to_path(), Gsk.FillRule.WINDING)
        rect = Graphene.Rect().init(0, 0, w, h)
        s0, s1 = Gsk.ColorStop(), Gsk.ColorStop()
        s0.offset, s0.color = 0.0, _rgba(self.color, 0.45)
        s1.offset, s1.color = 1.0, _rgba(self.color, 0.02)
        snap.append_linear_gradient(rect, Graphene.Point().init(0, 0), Graphene.Point().init(0, h), [s0, s1])
        snap.pop()
        line = Gsk.PathBuilder.new()
        curve(line)
        st = Gsk.Stroke.new(2)
        st.set_line_join(Gsk.LineJoin.ROUND)
        snap.append_stroke(line.to_path(), st, _rgba(self.color))
        dot = Gsk.PathBuilder.new()
        dot.add_circle(Graphene.Point().init(pts[-1][0] - 2, pts[-1][1]), 3.2)
        snap.append_fill(dot.to_path(), Gsk.FillRule.WINDING, _rgba(self.color))


class _RingArc(Gtk.Widget):
    def __init__(self, size, width):
        super().__init__()
        self.size, self.lw, self.value, self.color = size, width, 0.0, COLORS["green"]

    def do_measure(self, orientation, for_size):
        return self.size, self.size, -1, -1

    def _arc(self, cx, cy, r, frac):
        pb = Gsk.PathBuilder.new()
        n = max(2, int(120 * frac))
        for i in range(n + 1):
            a = -math.pi / 2 + 2 * math.pi * frac * i / n
            (pb.move_to if i == 0 else pb.line_to)(cx + r * math.cos(a), cy + r * math.sin(a))
        return pb.to_path()

    def do_snapshot(self, snap):
        w, h = self.get_width(), self.get_height()
        cx, cy = w / 2, h / 2
        r = min(w, h) / 2 - self.lw / 2 - 1
        st = Gsk.Stroke.new(self.lw)
        st.set_line_cap(Gsk.LineCap.ROUND)
        snap.append_stroke(self._arc(cx, cy, r, 1.0), st, _rgba((0.5, 0.5, 0.5), 0.18))
        if self.value > 0.005:
            snap.append_stroke(self._arc(cx, cy, r, min(self.value, 1.0)), st, _rgba(self.color))


class Ring(Gtk.Overlay):
    """Circular progress ring with big text in the middle."""

    def __init__(self, size=150, width=14):
        super().__init__()
        self.arc = _RingArc(size, width)
        self.set_child(self.arc)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        self.text = Gtk.Label()
        self.text.add_css_class("big-number")
        self.sub = Gtk.Label()
        self.sub.add_css_class("dim-label")
        box.append(self.text)
        box.append(self.sub)
        self.add_overlay(box)

    def set(self, value, text, sub="", color="green"):
        self.arc.value, self.arc.color = value, COLORS[color]
        self.arc.queue_draw()
        self.text.set_label(text)
        self.sub.set_label(sub)


class StatCard(Gtk.Box):
    """Card with icon, title, big value, subtitle and live chart."""

    def __init__(self, title, icon, color, maxv=100.0, tooltip=""):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.add_css_class("card")
        self.add_css_class("stat")
        self.color = color
        if tooltip:
            self.set_tooltip_text(tooltip)
        top = Gtk.Box(spacing=8)
        self.icon = Gtk.Image.new_from_icon_name(icon)
        self.icon.add_css_class("stat-icon")
        self.icon.add_css_class(color)
        t = Gtk.Label(label=title, xalign=0, hexpand=True)
        t.add_css_class("stat-title")
        top.append(self.icon)
        top.append(t)
        self.value = Gtk.Label(label="–", xalign=0)
        self.value.add_css_class("stat-value")
        self.sub = Gtk.Label(label="", xalign=0, ellipsize=3)
        self.sub.add_css_class("stat-sub")
        self.chart = Sparkline(color, maxv)
        for wdg in (top, self.value, self.sub, self.chart):
            self.append(wdg)

    def update(self, value_text, sub, chart_value, color=None):
        self.value.set_label(value_text)
        self.sub.set_label(sub)
        if color and color != self.color:
            self.icon.remove_css_class(self.color)
            self.icon.add_css_class(color)
            self.chart.set_color(color)
            self.color = color
        self.chart.push(chart_value)


def badge(text, kind):
    lbl = Gtk.Label(label=text, valign=Gtk.Align.CENTER)
    lbl.add_css_class("badge")
    lbl.add_css_class(kind)
    return lbl


def keycaps(*keys):
    box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
    for i, k in enumerate(keys):
        if i:
            box.append(Gtk.Label(label="+"))
        lbl = Gtk.Label(label=k)
        lbl.add_css_class("keycap")
        box.append(lbl)
    return box


def boxed_list():
    lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
    lb.add_css_class("boxed-list")
    return lb


def section(title, subtitle=None):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=8)
    t = Gtk.Label(label=title, xalign=0)
    t.add_css_class("section-title")
    box.append(t)
    if subtitle:
        s = Gtk.Label(label=subtitle, xalign=0, wrap=True)
        s.add_css_class("dim-label")
        box.append(s)
    return box


def page_scroller(child, max_width=1100):
    clamp = Adw.Clamp(maximum_size=max_width, tightening_threshold=700, child=child)
    child.set_margin_top(24)
    child.set_margin_bottom(32)
    child.set_margin_start(18)
    child.set_margin_end(18)
    sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True, child=clamp)
    return sw
