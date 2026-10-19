#!/usr/bin/env python3
"""PC Watchdog - native GTK4 / libadwaita control center."""
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gi  # noqa: E402
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

import system as S  # noqa: E402
from widgets import (CSS, Ring, StatCard, badge, boxed_list, keycaps,  # noqa: E402
                     page_scroller, section)

APP_ID = "io.github.pcwatchdog"
VERSION = "2.0"
STATE_FILE = os.path.join(S.HOME, ".config/pc-watchdog/app.json")
HERE = os.path.dirname(os.path.abspath(__file__))


def bg(fn, done=None, *args):
    """Run fn(*args) in a thread, then done(result, error) on the GTK main loop."""
    def work():
        try:
            res, err = fn(*args), None
        except Exception as e:  # noqa: BLE001
            res, err = None, e
        if done:
            GLib.idle_add(lambda: (done(res, err), False)[1])
    threading.Thread(target=work, daemon=True).start()


def subprocess_popen(cmd):
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def clear(box):
    while (c := box.get_first_child()) is not None:
        box.remove(c)


def load_state():
    try:
        return json.load(open(STATE_FILE))
    except (OSError, ValueError):
        return {}


def save_state(st):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    json.dump(st, open(STATE_FILE, "w"))


def pill(label, style="suggested-action"):
    b = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
    b.add_css_class("pill")
    if style:
        b.add_css_class(style)
    return b


def small_button(label, style=None):
    b = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
    if style:
        b.add_css_class(style)
    return b


STATUS_ICON = {"ok": "object-select-symbolic", "warn": "dialog-warning-symbolic",
               "bad": "dialog-error-symbolic", "info": "dialog-information-symbolic"}


# ====================================================================== pages
class Page:
    title = ""
    icon = ""

    def __init__(self, win):
        self.win = win
        self.widget = None

    def on_show(self):
        pass

    def refresh(self):
        pass

    def tick(self, st):
        pass


# ---------------------------------------------------------------- dashboard
class Dashboard(Page):
    title, icon = "Dashboard", "pcw-shield-symbolic"

    def __init__(self, win):
        super().__init__(win)
        self._switch_guard = False
        self.victim = None
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)

        # hero banner
        self.hero = Gtk.Box(spacing=26)
        self.hero.add_css_class("hero")
        self.hero_icon = Gtk.Image.new_from_icon_name("pcw-shield-symbolic")
        self.hero_icon.set_pixel_size(92)
        self.hero_icon.add_css_class("hero-icon")
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True, valign=Gtk.Align.CENTER)
        self.hero_title = Gtk.Label(xalign=0)
        self.hero_title.add_css_class("hero-title")
        self.hero_sub = Gtk.Label(xalign=0, wrap=True)
        self.hero_sub.add_css_class("hero-sub")
        self.hero_meta = Gtk.Label(xalign=0, wrap=True)
        self.hero_meta.add_css_class("dim-label")
        for w in (self.hero_title, self.hero_sub, self.hero_meta):
            texts.append(w)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, valign=Gtk.Align.CENTER)
        self.switch = Gtk.Switch(halign=Gtk.Align.CENTER)
        self.switch.set_tooltip_text("Turn protection on or off")
        self.switch.connect("notify::active", self._on_switch)
        self.switch_lbl = Gtk.Label(label="Protection")
        self.switch_lbl.add_css_class("caption-heading")
        self.install_btn = pill("Install protection")
        self.install_btn.connect("clicked", lambda *_: win.do_install())
        for w in (self.switch, self.switch_lbl, self.install_btn):
            right.append(w)
        for w in (self.hero_icon, texts, right):
            self.hero.append(w)
        box.append(self.hero)

        # live stat cards
        grid = Gtk.Grid(column_spacing=14, row_spacing=14, column_homogeneous=True)
        self.c_ram = StatCard("Memory (RAM)", "pcw-memory-symbolic", "blue",
                              tooltip="How much of your RAM is in use. When it is nearly full the PC can freeze.")
        self.c_swap = StatCard("Swap", "drive-harddisk-symbolic", "purple",
                               tooltip="Backup memory used when RAM is full.")
        self.c_cpu = StatCard("CPU", "pcw-cpu-symbolic", "teal", tooltip="Processor usage across all cores.")
        self.c_temp = StatCard("CPU temperature", "pcw-temp-symbolic", "green", maxv=105,
                               tooltip="Above ~90°C the CPU slows down; overheating can crash the PC.")
        self.c_risk = StatCard("Freeze risk", "pcw-pulse-symbolic", "green", maxv=50,
                               tooltip="Based on free memory and how long the system was stalled waiting for memory.")
        self.c_disk = StatCard("Disk", "drive-harddisk-symbolic", "blue",
                               tooltip="Space used on your main drive. A full disk breaks updates and apps.")
        for i, c in enumerate((self.c_ram, self.c_swap, self.c_cpu, self.c_temp, self.c_risk, self.c_disk)):
            grid.attach(c, i % 3, i // 3, 1, 1)
        box.append(grid)

        # per-core bars
        cores_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        cores_card.add_css_class("card")
        cores_card.add_css_class("stat")
        lbl = Gtk.Label(label="CPU cores", xalign=0)
        lbl.add_css_class("stat-title")
        cores_card.append(lbl)
        self.cores_box = Gtk.Box(spacing=10, homogeneous=True, margin_bottom=8)
        cores_card.append(self.cores_box)
        self.core_bars = []
        box.append(cores_card)

        # safety net
        box.append(section("Safety net", "What PC Watchdog will do for you, and shortcuts to the main tools."))
        lst = boxed_list()
        self.row_next = Adw.ActionRow(title="Next app to close if memory runs out", subtitle="…")
        self.row_next.add_prefix(Gtk.Image.new_from_icon_name("process-stop-symbolic"))
        b = small_button("Details")
        b.connect("clicked", lambda *_: self.victim and win.open_app(self.victim))
        self.row_next.add_suffix(b)
        b = small_button("Manage apps")
        b.connect("clicked", lambda *_: win.go("apps"))
        self.row_next.add_suffix(b)
        self.row_last = Adw.ActionRow(title="Last incident", subtitle="Loading…")
        self.row_last.add_prefix(Gtk.Image.new_from_icon_name("document-open-recent-symbolic"))
        b = small_button("View events")
        b.connect("clicked", lambda *_: win.go("events"))
        self.row_last.add_suffix(b)
        self.row_health = Adw.ActionRow(title="Health check", subtitle="Find problems that make your PC crash")
        self.row_health.add_prefix(Gtk.Image.new_from_icon_name("pcw-pulse-symbolic"))
        b = small_button("Run check")
        b.connect("clicked", lambda *_: win.go("health"))
        self.row_health.add_suffix(b)
        self.row_space = Adw.ActionRow(title="Free up disk space", subtitle="")
        self.row_space.add_prefix(Gtk.Image.new_from_icon_name("user-trash-symbolic"))
        b = small_button("Open cleanup")
        b.connect("clicked", lambda *_: win.go("cleanup"))
        self.row_space.add_suffix(b)
        for r in (self.row_next, self.row_last, self.row_health, self.row_space):
            lst.append(r)
        box.append(lst)
        self.widget = page_scroller(box)

    def _on_switch(self, sw, _p):
        if self._switch_guard:
            return
        action = "start" if sw.get_active() else "stop"
        if action == "stop":
            self._switch_guard = True
            sw.set_active(True)
            self._switch_guard = False
            self.win.confirm("Turn off protection?",
                             "Your PC can freeze again when memory runs out, until you turn it back on.",
                             "Turn off", lambda: self.win.service_action("stop"), destructive=True)
        else:
            self.win.service_action("start")

    def tick(self, st):
        svc = st["svc"]
        m = st["mem"]
        if not svc["installed"]:
            state, title, sub = "bad", "Not protected", "Protection is not installed yet. Click Install protection — it takes a few seconds."
        elif not svc["active"]:
            state, title, sub = "bad", "Protection is off", "Your PC can freeze when memory runs out. Turn the switch on."
        elif st["temp"] >= 90 or st["avail_pct"] < 10:
            state, title, sub = "warn", "Protected — under stress", "PC Watchdog is working. Memory or temperature is high right now."
        else:
            state, title, sub = "ok", "Your PC is protected", "PC Watchdog is watching memory, temperature and disk in the background."
        for c in ("ok", "warn", "bad"):
            self.hero.remove_css_class(c)
            self.hero_icon.remove_css_class(c)
        self.hero.add_css_class(state)
        self.hero_icon.add_css_class(state)
        self.hero_title.set_label(title)
        self.hero_sub.set_label(sub)
        meta = [f"Up {S.fmt_duration(st['uptime'])}",
                "Hardware watchdog " + ("on" if st["hw"] else "off"),
                f"{st['kills_today']} app(s) closed today"]
        self.hero_meta.set_label("  ·  ".join(meta))
        self.install_btn.set_visible(not svc["installed"])
        self.switch.set_visible(svc["installed"])
        self.switch_lbl.set_visible(svc["installed"])
        self._switch_guard = True
        self.switch.set_active(svc["active"])
        self._switch_guard = False

        used = m["MemTotal"] - m["MemAvailable"]
        ram_pct = used / m["MemTotal"] * 100
        self.c_ram.update(f"{ram_pct:.0f}%", f"{S.fmt_bytes(used)} of {S.fmt_bytes(m['MemTotal'])} used",
                          ram_pct, "red" if ram_pct > 90 else "orange" if ram_pct > 80 else "blue")
        if m.get("SwapTotal"):
            su = m["SwapTotal"] - m["SwapFree"]
            sp = su / m["SwapTotal"] * 100
            self.c_swap.update(f"{sp:.0f}%", f"{S.fmt_bytes(su)} of {S.fmt_bytes(m['SwapTotal'])} used", sp,
                               "red" if sp > 85 else "purple")
        else:
            self.c_swap.update("None", "No swap configured", 0)
        self.c_cpu.update(f"{st['cpu']:.0f}%", f"{len(st['cores'])} cores · load {os.getloadavg()[0]:.2f}", st["cpu"])
        t = st["temp"]
        self.c_temp.update(f"{t}°C", "Normal" if t < 75 else "Warm" if t < 88 else "Hot — check cooling",
                           t, "green" if t < 75 else "orange" if t < 88 else "red")
        some, full = st["psi"]
        ap = st["avail_pct"]
        if ap < 8 or full > 20:
            risk, col = "High", "red"
        elif ap < 15 or full > 5:
            risk, col = "Medium", "orange"
        else:
            risk, col = "Low", "green"
        self.c_risk.update(risk, f"Stalled {some:.1f}% of the time · {ap:.0f}% RAM free", max(some, full), col)
        du, dt = st["disk"]
        dp = du / dt * 100
        self.c_disk.update(f"{dp:.0f}%", f"{S.fmt_bytes(dt - du)} free of {S.fmt_bytes(dt)}", dp,
                           "red" if dp > 95 else "orange" if dp > 85 else "blue")
        self.row_space.set_subtitle(f"{S.fmt_bytes(dt - du)} free — clean caches, trash and old packages")

        if not self.core_bars:
            for i in range(len(st["cores"])):
                col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
                bar = Gtk.LevelBar(orientation=Gtk.Orientation.VERTICAL, inverted=True, min_value=0,
                                   max_value=100, height_request=70, halign=Gtk.Align.CENTER)
                bar.remove_offset_value("low")
                bar.remove_offset_value("high")
                bar.remove_offset_value("full")
                lbl = Gtk.Label(label=f"{i}")
                lbl.add_css_class("caption")
                lbl.add_css_class("dim-label")
                col.append(bar)
                col.append(lbl)
                self.cores_box.append(col)
                self.core_bars.append(bar)
        for bar, v in zip(self.core_bars, st["cores"]):
            bar.set_value(v)

        v = st["victim"]
        if v:
            self.victim = v["comm"]
            self.row_next.set_subtitle(f"{self.win.app_name(v['comm'])}  ·  using {S.fmt_bytes(v['rss'])} in its largest process")
        ev = self.win.events_cache
        if ev is not None:
            inc = next((e for e in ev if e["kind"] in ("kill", "crit", "warn")), None)
            self.row_last.set_subtitle(f"{inc['title']} · {S.ago(inc['ts'])}" if inc else "No incidents yet — everything is calm")


# ---------------------------------------------------------------- apps
class AppRow(Adw.ActionRow):
    def __init__(self, page, name):
        super().__init__(activatable=True)
        self.set_tooltip_text("Click for details and options")
        self.connect("activated", lambda *_: page.win.open_app(self.name))
        self.page, self.name, self.rss, self.flags = page, name, 0, None
        disp, icon = page.win.app_info(name)
        self.set_title(GLib.markup_escape_text(disp))
        img = Gtk.Image.new_from_gicon(icon) if icon else Gtk.Image.new_from_icon_name("application-x-executable")
        img.set_pixel_size(32)
        self.add_prefix(img)
        self.badges = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.add_suffix(self.badges)
        self.bar = Gtk.LevelBar(min_value=0, max_value=1, valign=Gtk.Align.CENTER)
        self.bar.add_css_class("appbar")
        for o in ("low", "high", "full"):
            self.bar.remove_offset_value(o)
        self.add_suffix(self.bar)
        self.size = Gtk.Label(width_chars=8, xalign=1)
        self.size.add_css_class("heading")
        self.add_suffix(self.size)
        # actions popover
        self.menu = Gtk.MenuButton(icon_name="view-more-symbolic", valign=Gtk.Align.CENTER)
        self.menu.add_css_class("flat")
        self.menu.set_tooltip_text("Actions")
        pop = Gtk.Popover()
        pbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6,
                       margin_start=6, margin_end=6)
        self.b_details = Gtk.Button(label="Details & options…")
        self.b_details.connect("clicked", lambda *_: (pop.popdown(), page.win.open_app(self.name)))
        self.b_close = Gtk.Button(label="Close this app now")
        self.b_prefer = Gtk.Button()
        self.b_protect = Gtk.Button()
        for b in (self.b_details, self.b_close, self.b_prefer, self.b_protect):
            b.add_css_class("flat")
            b.get_child().set_xalign(0) if isinstance(b.get_child(), Gtk.Label) else None
            pbox.append(b)
        self.b_close.connect("clicked", lambda *_: (pop.popdown(), page.close_app(self.name)))
        self.b_prefer.connect("clicked", lambda *_: (pop.popdown(), page.toggle_list("PREFER_APPS", self.name)))
        self.b_protect.connect("clicked", lambda *_: (pop.popdown(), page.toggle_list("EXTRA_PROTECT", self.name)))
        pop.set_child(pbox)
        self.menu.set_popover(pop)
        self.add_suffix(self.menu)

    def update(self, g, total):
        self.rss = g["rss"]
        disp, _ = self.page.win.app_info(self.name)
        parts = [f"{g['count']} process{'es' if g['count'] > 1 else ''}"]
        if disp != self.name:
            parts.insert(0, self.name)
        if not g["mine"]:
            parts.append("system")
        self.set_subtitle(" · ".join(parts))
        self.size.set_label(S.fmt_bytes(g["rss"]))
        self.bar.set_value(min(1.0, g["rss"] / total * 4))  # 25% of RAM = full bar
        flags = (g["victim"], g["prefer"], g["protected"], g["mine"])
        if flags != self.flags:
            self.flags = flags
            clear(self.badges)
            if g["victim"]:
                self.badges.append(badge("NEXT TO CLOSE", "next"))
            if g["protected"]:
                self.badges.append(badge("Protected", "protect"))
            elif g["prefer"]:
                self.badges.append(badge("Closes first", "first"))
            if not g["mine"]:
                self.badges.append(badge("System", "system"))
            in_base = bool(S.BASE_PROTECT.match(self.name))
            self.b_close.set_sensitive(g["mine"])
            self.b_prefer.set_label("Don't close this app first" if g["prefer"] else "Close this app first when memory is low")
            self.b_protect.set_label("Allow closing this app" if (g["protected"] and not in_base) else "Never close this app")
            self.b_protect.set_sensitive(not in_base)
            self.b_prefer.set_sensitive(not g["protected"])


def texture_from_b64(data):
    import base64
    try:
        return Gdk.Texture.new_from_bytes(GLib.Bytes.new(base64.b64decode(data)))
    except (GLib.Error, ValueError, TypeError):
        return None


class TabRow(Adw.ActionRow):
    def __init__(self, view, tab):
        super().__init__(activatable=True)
        self.view, self.tid, self.rss, self.icon_set = view, tab["id"], -1, False
        self.set_tooltip_text("Click to switch to this tab in Brave")
        self.img = Gtk.Image.new_from_icon_name("web-browser-symbolic")
        self.img.set_pixel_size(24)
        self.add_prefix(self.img)
        self.cpu = Gtk.Label(width_chars=6, xalign=1)
        self.cpu.add_css_class("dim-label")
        self.mem = Gtk.Label(width_chars=9, xalign=1)
        self.mem.add_css_class("heading")
        self.add_suffix(self.cpu)
        self.add_suffix(self.mem)
        self.b_sleep = Gtk.Button(icon_name="weather-clear-night-symbolic", valign=Gtk.Align.CENTER,
                                  tooltip_text="Sleep tab — frees its memory, the tab stays open and reloads when you click it")
        b_close = Gtk.Button(icon_name="window-close-symbolic", valign=Gtk.Align.CENTER,
                             tooltip_text="Close tab (reopen with Ctrl+Shift+T in Brave)")
        for b in (self.b_sleep, b_close):
            b.add_css_class("flat")
            b.add_css_class("circular")
            self.add_suffix(b)
        self.b_sleep.connect("clicked", lambda *_: view.tab_action("discard", self.tid, "Tab is sleeping — memory freed"))
        b_close.connect("clicked", lambda *_: view.tab_action("close", self.tid, "Tab closed — reopen with Ctrl+Shift+T in Brave"))
        self.connect("activated", lambda *_: view.tab_action("focus", self.tid, None))

    def update(self, tab, pid, shared, cpu, rss, measuring):
        if not self.icon_set and tab.get("icon"):
            tex = texture_from_b64(tab["icon"])
            if tex:
                self.img.set_from_paintable(tex)
                self.icon_set = True
        self.set_title(GLib.markup_escape_text(tab["title"] or tab["url"]))
        host = re.sub(r"^[a-z-]+://(www\.)?", "", tab["url"]).split("/")[0] or tab["url"]
        bits = [host]
        if tab["active"]:
            bits.append("open now")
        if tab["audible"]:
            bits.append("🔊 playing")
        if tab["pinned"]:
            bits.append("📌 pinned")
        if tab["discarded"]:
            bits.append("💤 sleeping")
        elif pid and shared > 1:
            bits.append(f"shares a process with {shared - 1} other tab{'s' if shared > 2 else ''}")
        elif not pid:
            bits.append("measuring…" if measuring else "can't measure this page")
        self.set_subtitle(GLib.markup_escape_text(" · ".join(bits)))
        self.rss = rss if pid else (-2 if tab["discarded"] else -1)
        self.mem.set_label(S.fmt_bytes(rss) if pid else ("0 B" if tab["discarded"] else "–"))
        self.cpu.set_label(f"{cpu:.0f}%" if pid else "")
        self.b_sleep.set_sensitive(not tab["discarded"] and not tab["active"])


class TabsView(Gtk.Stack):
    """Brave tabs with live RAM/CPU (via the PC Watchdog Tabs extension)."""

    def __init__(self, dlg):
        super().__init__(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.dlg, self.win = dlg, dlg.win
        self.tabs = []
        self.rows = {}
        self.busy = False
        self.probing = False
        self.connected = None
        if not hasattr(self.win, "tab_mapper"):
            self.win.tab_mapper = S.TabMapper()
        self.mapper = self.win.tab_mapper
        self.add_named(self._setup(), "setup")
        self.add_named(self._list(), "list")
        self.add_named(Adw.StatusPage(title="Connecting to Brave…", child=Adw.Spinner()), "wait")
        self.set_visible_child_name("wait")

    def _setup(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=24, margin_end=24, margin_bottom=18)
        sp = Adw.StatusPage(icon_name="web-browser-symbolic", title="See your Brave tabs here",
                            description="One-time setup, about 30 seconds. It adds a tiny companion extension to Brave, so "
                                        "PC Watchdog can list your tabs with their RAM and CPU, and sleep or close them. "
                                        "It only talks to this app on your PC — nothing is sent online.")
        sp.add_css_class("compact")
        box.append(sp)
        lst = boxed_list()
        path = S.BRAVE_EXT_DIR
        steps = [
            ("1 · Open Brave's extensions page", "Opens brave://extensions in a new Brave tab",
             [("Open", self._open_ext_page, "suggested-action")]),
            ("2 · Turn on “Developer mode”", "The switch in the top-right corner of that page", []),
            ("3 · Click “Load unpacked” and pick this folder",
             f"{path}\nTip: in the folder picker press Ctrl+L, paste the path and press Enter",
             [("Copy path", lambda: (self.get_clipboard().set(path), self.win.toast("Path copied")), None)]),
            ("4 · Done", "This page connects by itself within a few seconds", []),
        ]
        for title, sub, buttons in steps:
            r = Adw.ActionRow(title=title, subtitle=GLib.markup_escape_text(sub), subtitle_selectable=True)
            for label, cb, style in buttons:
                b = small_button(label, style)
                b.connect("clicked", lambda *_, cb=cb: cb())
                r.add_suffix(b)
            lst.append(r)
        box.append(lst)
        sw = Gtk.ScrolledWindow(child=box, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        return sw

    def _open_ext_page(self):
        S.brave_install_host()
        try:
            subprocess_popen(["brave", "brave://extensions/"])
        except OSError as e:
            self.win.toast(f"Could not open Brave: {e}")

    def _list(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_start=24, margin_end=24, margin_bottom=18)
        ctl = Gtk.Box(spacing=10)
        self.search = Gtk.SearchEntry(placeholder_text="Search tabs…", hexpand=True)
        self.search.connect("search-changed", lambda *_: self.render())
        b = small_button("Sleep background tabs", "suggested-action")
        b.set_tooltip_text("Puts every tab you're not using to sleep (not playing audio, not pinned). They reload when clicked.")
        b.connect("clicked", lambda *_: self.sleep_background())
        ctl.append(self.search)
        ctl.append(b)
        box.append(ctl)
        sb = Gtk.Box(spacing=8)
        self.summary = Gtk.Label(xalign=0, hexpand=True, wrap=True)
        self.summary.add_css_class("dim-label")
        self.spin = Adw.Spinner(visible=False)
        sb.append(self.summary)
        sb.append(self.spin)
        box.append(sb)
        self.list = boxed_list()
        self.list.set_sort_func(lambda a, b: (b.rss > a.rss) - (b.rss < a.rss))
        box.append(self.list)
        note = Gtk.Label(xalign=0, wrap=True, label="RAM and CPU are measured for the process each tab runs in. Tabs of the same "
                         "website often share one process, so they show the same numbers. Click a tab to switch to it.")
        note.add_css_class("caption")
        note.add_css_class("dim-label")
        box.append(note)
        return Gtk.ScrolledWindow(child=box, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)

    # ---- data
    def refresh(self):
        if self.busy:
            return
        self.busy = True

        def fetch():
            try:
                return S.brave_call("list", icons=True)["tabs"]
            except (OSError, ValueError, KeyError):
                return None
        bg(fetch, self._got)

    def _got(self, tabs, err):
        self.busy = False
        if tabs is None:
            if self.connected is not False:
                S.brave_install_host()
            self.connected = False
            self.set_visible_child_name("setup")
            return
        self.connected = True
        self.tabs = tabs
        self.set_visible_child_name("list")
        self.render()
        self._probe_some()

    def _probe_some(self):
        if self.probing:
            return
        todo = [t for t in self.tabs if self.mapper.needs_probe(t)]
        if not todo:
            return
        self.probing = True
        self.spin.set_visible(True)

        def work():
            for t in todo[:6]:
                self.mapper.probe(t, self.dlg.name)
            return True

        def done(*_):
            self.probing = False
            self.spin.set_visible(False)
            self.render()
            self.dlg.refresh_titles()
        bg(work, done)

    def pid_titles(self):
        out = {}
        for t in self.tabs:
            pid = self.mapper.pid_for(t)
            if pid:
                out.setdefault(pid, []).append(t["title"] or t["url"])
        return out

    def render(self):
        if not self.tabs:
            return
        q = self.search.get_text().lower().strip()
        procs = {p["pid"]: p for p in self.dlg.procs}
        cpu = self.dlg.cpu
        pids = {t["id"]: self.mapper.pid_for(t) for t in self.tabs}
        shared = {}
        for pid in pids.values():
            if pid:
                shared[pid] = shared.get(pid, 0) + 1
        shown = [t for t in self.tabs if not q or q in (t["title"] or "").lower() or q in t["url"].lower()]
        ids = {t["id"] for t in shown}
        for tid in list(self.rows):
            if tid not in ids:
                self.list.remove(self.rows.pop(tid))
        for t in shown:
            row = self.rows.get(t["id"])
            if row is None:
                row = self.rows[t["id"]] = TabRow(self, t)
                self.list.append(row)
            pid = pids[t["id"]]
            p = procs.get(pid)
            row.update(t, pid if p else None, shared.get(pid, 0), cpu.get(pid, 0.0), p["rss"] if p else 0,
                       self.mapper.needs_probe(t))
        self.list.invalidate_sort()
        sleeping = sum(1 for t in self.tabs if t["discarded"])
        measured = sum(1 for t in self.tabs if pids[t["id"]])
        waiting = sum(1 for t in self.tabs if self.mapper.needs_probe(t))
        self.summary.set_label(f"{len(self.tabs)} tabs · {sleeping} sleeping · {measured} measured"
                               + (f" · measuring {waiting} more…" if waiting else ""))

    # ---- actions
    def tab_action(self, cmd, tid, msg):
        def done(res, err):
            if res is None:
                self.win.toast(f"Brave didn't respond: {err}")
            elif msg:
                self.win.toast(msg)
            self.busy = False
            self.refresh()
        bg(lambda: S.brave_call(cmd, tabId=tid), done)

    def sleep_background(self):
        def done(res, err):
            self.win.toast(f"Put {res['count']} tabs to sleep — memory freed" if res else f"Failed: {err}")
            GLib.timeout_add(1500, lambda: (self.refresh(), False)[1])
        bg(lambda: S.brave_call("sleepBackground", timeout=20), done)


class ProcRow(Adw.ActionRow):
    def __init__(self, dlg, pid):
        super().__init__(activatable=True)
        self.dlg, self.pid, self.rss = dlg, pid, 0
        self.cpu = Gtk.Label(width_chars=6, xalign=1)
        self.cpu.add_css_class("dim-label")
        self.mem = Gtk.Label(width_chars=9, xalign=1)
        self.mem.add_css_class("heading")
        kill = Gtk.Button(icon_name="window-close-symbolic", valign=Gtk.Align.CENTER, tooltip_text="End this process")
        kill.add_css_class("flat")
        kill.add_css_class("circular")
        kill.connect("clicked", lambda *_: dlg.end_process(self.pid))
        self.add_suffix(self.cpu)
        self.add_suffix(self.mem)
        self.add_suffix(kill)
        self.connect("activated", lambda *_: dlg.show_process(self.pid))

    def update(self, p, cpu):
        self.rss = p["rss"]
        state = " · paused" if p["state"] == "T" else ""
        titles = self.dlg.pid_titles.get(p["pid"])
        if titles:
            label = "Tab: " + titles[0][:70] + (f"  (+{len(titles) - 1} more)" if len(titles) > 1 else "")
        else:
            label = p["kind"]
        self.set_title(GLib.markup_escape_text(label) + f"  <small>PID {p['pid']}</small>")
        started = S.fmt_duration(max(0, S.uptime() - p["start"]))
        self.set_subtitle(f"Started {started} ago · {p['threads']} threads{state} · click for details")
        self.cpu.set_label(f"{cpu:.0f}%")
        self.mem.set_label(S.fmt_bytes(p["rss"]))


class AppDialog(Adw.Dialog):
    """Pop-up with live usage, processes, options and details for one app."""

    def __init__(self, win, name):
        super().__init__(content_width=860, content_height=720)
        self.win, self.name = win, name
        self.disp, icon = win.app_info(name)
        self.set_title(self.disp)
        self.set_size_request(360, 400)
        self.prev = {}
        self.prev_t = time.monotonic()
        self.procs = []
        self.rows = {}
        self._guard = False
        self.mine = True

        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        self.stack = Adw.ViewStack()
        hb.set_title_widget(Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE))
        tv.add_top_bar(hb)
        switcher_bar = Adw.ViewSwitcherBar(stack=self.stack)
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 720sp"))
        bp.add_setter(switcher_bar, "reveal", True)
        bp.add_setter(hb, "title-widget", Adw.WindowTitle(title=self.disp))
        self.add_breakpoint(bp)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        head = Gtk.Box(spacing=16, margin_start=24, margin_end=24, margin_top=8, margin_bottom=12)
        img = Gtk.Image.new_from_gicon(icon) if icon else Gtk.Image.new_from_icon_name("application-x-executable")
        img.set_pixel_size(64)
        head.append(img)
        hv = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER, hexpand=True)
        t = Gtk.Label(label=self.disp, xalign=0)
        t.add_css_class("title-1")
        self.head_sub = Gtk.Label(xalign=0)
        self.head_sub.add_css_class("dim-label")
        hv.append(t)
        hv.append(self.head_sub)
        self.badges = Gtk.Box(spacing=6, margin_top=4)
        hv.append(self.badges)
        head.append(hv)
        body.append(head)
        body.append(self.stack)
        tv.set_content(body)

        # bottom action bar
        bar = Gtk.Box(spacing=10, margin_start=18, margin_end=18, margin_top=10, margin_bottom=10)
        self.b_pause = small_button("Pause")
        self.b_pause.set_tooltip_text("Freeze the app temporarily (it stops using CPU). Click again to resume.")
        self.b_pause.connect("clicked", lambda *_: self.toggle_pause())
        bar.append(self.b_pause)
        bar.append(Gtk.Box(hexpand=True))
        b_force = small_button("Force kill", None)
        b_force.set_tooltip_text("Stop immediately, without letting the app save anything")
        b_force.connect("clicked", lambda *_: self.end_app(force=True))
        b_close = small_button("Close app", "destructive-action")
        b_close.set_tooltip_text("Ask the app to quit normally")
        b_close.connect("clicked", lambda *_: self.end_app(force=False))
        bar.append(b_force)
        bar.append(b_close)
        tv.add_bottom_bar(bar)
        tv.add_bottom_bar(switcher_bar)
        self.set_child(tv)

        self.cpu = {}
        self.pid_titles = {}
        self.tabs_view = None
        self.stack.add_titled_with_icon(self._usage_page(), "usage", "Usage", "pcw-pulse-symbolic")
        if name in S.BROWSER_APPS:
            self.tabs_view = TabsView(self)
            self.stack.add_titled_with_icon(self.tabs_view, "tabs", "Tabs", "web-browser-symbolic")
        self.stack.add_titled_with_icon(self._procs_page(), "procs", "Processes", "view-list-symbolic")
        self.stack.add_titled_with_icon(self._options_page(), "options", "Options", "preferences-system-symbolic")
        self.stack.add_titled_with_icon(self._details_page(), "details", "Details", "dialog-information-symbolic")

        self._closed = False
        self.refresh()
        if self.tabs_view:
            self.stack.set_visible_child_name("tabs")
            self.tabs_view.refresh()
        self.timer = GLib.timeout_add_seconds(2, self.refresh)
        self.connect("closed", self._on_closed)

    def _on_closed(self, *_):
        self._closed = True
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = None

    # ---- pages
    def _scroll(self, child):
        child.set_margin_start(24)
        child.set_margin_end(24)
        child.set_margin_bottom(18)
        return Gtk.ScrolledWindow(child=child, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)

    def _usage_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        grid = Gtk.Grid(column_spacing=12, row_spacing=12, column_homogeneous=True)
        self.c_mem = StatCard("Memory", "pcw-memory-symbolic", "blue", maxv=None)
        self.c_cpu = StatCard("CPU", "pcw-cpu-symbolic", "teal", maxv=None)
        grid.attach(self.c_mem, 0, 0, 1, 1)
        grid.attach(self.c_cpu, 1, 0, 1, 1)
        box.append(grid)
        lst = boxed_list()
        self.r_share = Adw.ActionRow(title="Share of your RAM")
        self.share_bar = Gtk.LevelBar(min_value=0, max_value=1, valign=Gtk.Align.CENTER, width_request=180)
        for o in ("low", "high", "full"):
            self.share_bar.remove_offset_value(o)
        self.r_share.add_suffix(self.share_bar)
        self.r_counts = Adw.ActionRow(title="Processes and threads")
        self.r_io = Adw.ActionRow(title="Disk read / written", subtitle="Since the app started")
        self.r_started = Adw.ActionRow(title="Running for")
        self.r_state = Adw.ActionRow(title="Status")
        for r in (self.r_share, self.r_counts, self.r_io, self.r_started, self.r_state):
            lst.append(r)
        box.append(lst)
        box.append(section("Breakdown", "What the app's processes are doing."))
        self.kinds = boxed_list()
        box.append(self.kinds)
        return self._scroll(box)

    def _procs_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        hint = Gtk.Label(xalign=0, wrap=True, label="Every process of this app. Click one for details, or ✕ to end just that one "
                         "(for a browser, ending a “Tab / page” process closes those tabs only).")
        hint.add_css_class("dim-label")
        box.append(hint)
        self.plist = boxed_list()
        self.plist.set_sort_func(lambda a, b: (b.rss > a.rss) - (b.rss < a.rss))
        box.append(self.plist)
        return self._scroll(box)

    def _options_page(self):
        page = Adw.PreferencesPage()
        g = Adw.PreferencesGroup(title="Behaviour")
        self.sw_pause = Adw.SwitchRow(title="Pause app", subtitle="Freezes the app so it stops using CPU. Its windows stop responding until resumed.")
        self.sw_pause.connect("notify::active", lambda r, _p: self._guard or self.toggle_pause(r.get_active()))
        self.sw_nice = Adw.SwitchRow(title="Low priority", subtitle="Let other apps go first when the CPU is busy. Good for builds and background jobs.")
        self.sw_nice.connect("notify::active", lambda r, _p: self._guard or self.set_low_priority(r.get_active()))
        for r in (self.sw_pause, self.sw_nice):
            g.add(r)
        page.add(g)
        g = Adw.PreferencesGroup(title="When memory runs out")
        self.sw_first = Adw.SwitchRow(title="Close this app first", subtitle="PC Watchdog closes it before other apps")
        self.sw_first.connect("notify::active", lambda r, _p: self._guard or self._toggle_cfg("PREFER_APPS", r.get_active()))
        self.sw_never = Adw.SwitchRow(title="Never close this app", subtitle="PC Watchdog will close other apps instead")
        self.sw_never.connect("notify::active", lambda r, _p: self._guard or self._toggle_cfg("EXTRA_PROTECT", r.get_active()))
        g.add(self.sw_first)
        g.add(self.sw_never)
        page.add(g)
        g = Adw.PreferencesGroup(title="More")
        for title, sub, label, cb in (
                ("Open install folder", "Show where this app's program file is", "Open", self.open_folder),
                ("Copy command line", "Copy the main process's full command", "Copy", self.copy_cmd),
                ("End all but the main process", "Frees memory from tabs/helpers — a browser will reload them", "End helpers",
                 self.end_helpers)):
            r = Adw.ActionRow(title=title, subtitle=sub)
            b = small_button(label)
            b.connect("clicked", lambda *_, cb=cb: cb())
            r.add_suffix(b)
            g.add(r)
        page.add(g)
        return page

    def _details_page(self):
        page = Adw.PreferencesPage()
        g = Adw.PreferencesGroup(title="Main process")
        self.d_rows = {}
        for key, title in (("comm", "Process name"), ("pid", "Process ID (PID)"), ("ppid", "Parent PID"), ("user", "Runs as user"),
                           ("exe", "Program file"), ("cwd", "Working folder"), ("cmd", "Command line"),
                           ("started", "Started at"), ("nice", "Priority (nice)"), ("files", "Open files")):
            r = Adw.ActionRow(title=title, subtitle_selectable=True)
            r.add_css_class("property")
            self.d_rows[key] = r
            g.add(r)
        page.add(g)
        return page

    # ---- live refresh
    def refresh(self):
        if self._closed:
            return False
        procs = S.group_detail(self.name)
        if not procs:
            self.win.toast(f"{self.disp} has exited")
            self.timer = None
            self.close()
            return False
        now = time.monotonic()
        dt = max(now - self.prev_t, 0.1)
        cpu = {}
        for p in procs:
            prev = self.prev.get(p["pid"])
            cpu[p["pid"]] = 0.0 if prev is None else max(0.0, (p["ticks"] - prev) / S.CLK / dt * 100 / S.NCPU)
        self.cpu = cpu
        self.prev = {p["pid"]: p["ticks"] for p in procs}
        self.prev_t = now
        self.procs = procs
        self.mine = all(p["uid"] == S.MY_UID for p in procs)
        main = next((p for p in procs if p["is_main"]), procs[0])
        self.main = main
        total = S.meminfo()["MemTotal"]
        rss = sum(p["rss"] for p in procs)
        tcpu = sum(cpu.values())
        threads = sum(p["threads"] for p in procs)
        paused = all(p["state"] == "T" for p in procs)
        low = main["nice"] > 0
        age = S.uptime() - main["start"]

        self.head_sub.set_label(f"{self.name} · {len(procs)} process{'es' if len(procs) > 1 else ''} · "
                                f"running for {S.fmt_duration(age)}" + (" · system app" if not self.mine else ""))
        cfg = self.win.cfg
        clear(self.badges)
        groups, victim = S.app_groups(S.processes(), cfg)
        if victim and victim["comm"] == self.name:
            self.badges.append(badge("NEXT TO CLOSE", "next"))
        if S.is_protected(self.name, cfg):
            self.badges.append(badge("Protected", "protect"))
        elif self.name in cfg["PREFER_APPS"].split():
            self.badges.append(badge("Closes first", "first"))
        if paused:
            self.badges.append(badge("Paused", "system"))

        # usage
        self.c_mem.update(S.fmt_bytes(rss), f"{rss / total * 100:.1f}% of your RAM", rss / 2**20)
        self.c_cpu.update(f"{tcpu:.1f}%", f"of total CPU ({S.NCPU} cores)", tcpu)
        self.share_bar.set_value(rss / total)
        self.r_share.set_subtitle(f"{rss / total * 100:.1f}% — {S.fmt_bytes(rss)} of {S.fmt_bytes(total)}")
        self.r_counts.set_subtitle(f"{len(procs)} processes · {threads} threads")
        ex = S.proc_extra(main["pid"])
        self.extra = ex
        self.r_io.set_subtitle(f"{S.fmt_bytes(ex['read'])} read · {S.fmt_bytes(ex['write'])} written"
                               if "read" in ex else "Not available (system app)")
        self.r_started.set_subtitle(S.fmt_duration(age))
        self.r_state.set_subtitle("Paused" if paused else ("Running · low priority" if low else "Running"))
        kinds = {}
        for p in procs:
            k = kinds.setdefault(p["kind"], [0, 0, 0.0])
            k[0] += 1
            k[1] += p["rss"]
            k[2] += cpu[p["pid"]]
        clear(self.kinds)
        for kind, (n, mem, c) in sorted(kinds.items(), key=lambda kv: -kv[1][1]):
            r = Adw.ActionRow(title=GLib.markup_escape_text(kind), subtitle=f"{n} process{'es' if n > 1 else ''} · CPU {c:.1f}%")
            lb = Gtk.LevelBar(min_value=0, max_value=1, value=mem / rss if rss else 0, valign=Gtk.Align.CENTER, width_request=140)
            for o in ("low", "high", "full"):
                lb.remove_offset_value(o)
            r.add_suffix(lb)
            sz = Gtk.Label(label=S.fmt_bytes(mem), width_chars=9, xalign=1)
            sz.add_css_class("heading")
            r.add_suffix(sz)
            self.kinds.append(r)

        # processes
        pids = {p["pid"] for p in procs}
        for pid in list(self.rows):
            if pid not in pids:
                self.plist.remove(self.rows.pop(pid))
        for p in procs:
            row = self.rows.get(p["pid"])
            if row is None:
                row = self.rows[p["pid"]] = ProcRow(self, p["pid"])
                self.plist.append(row)
            row.update(p, cpu[p["pid"]])
        self.plist.invalidate_sort()

        if self.tabs_view:
            self._tick = getattr(self, "_tick", 0) + 1
            self.pid_titles = self.tabs_view.pid_titles()
            self.tabs_view.render()
            if self._tick % 2 == 0:
                self.tabs_view.refresh()

        # options (without triggering handlers)
        self._guard = True
        self.sw_pause.set_active(paused)
        self.sw_nice.set_active(low)
        self.sw_first.set_active(self.name in cfg["PREFER_APPS"].split())
        self.sw_never.set_active(self.name in cfg["EXTRA_PROTECT"].split() or bool(S.BASE_PROTECT.match(self.name)))
        self.sw_never.set_sensitive(not S.BASE_PROTECT.match(self.name))
        self.sw_first.set_sensitive(not S.is_protected(self.name, cfg))
        self._guard = False
        self.b_pause.set_label("Resume" if paused else "Pause")

        # details
        d = self.d_rows
        d["comm"].set_subtitle(self.name)
        d["pid"].set_subtitle(str(main["pid"]))
        d["ppid"].set_subtitle(str(main["ppid"]))
        d["user"].set_subtitle(S.user_name(main["uid"]))
        d["exe"].set_subtitle(GLib.markup_escape_text(ex.get("exe", "")))
        d["cwd"].set_subtitle(GLib.markup_escape_text(ex.get("cwd", "")))
        d["cmd"].set_subtitle(GLib.markup_escape_text(" ".join(main["cmd"])[:600] or "(none)"))
        d["started"].set_subtitle(time.strftime("%a %b %d, %H:%M:%S", time.localtime(time.time() - age)))
        d["nice"].set_subtitle(f"{main['nice']}  (0 = normal, higher = lower priority)")
        d["files"].set_subtitle(ex.get("files", ""))
        return True

    def refresh_titles(self):
        if self.tabs_view:
            self.pid_titles = self.tabs_view.pid_titles()
            for row in self.rows.values():
                p = next((x for x in self.procs if x["pid"] == row.pid), None)
                if p:
                    row.update(p, self.cpu.get(row.pid, 0.0))

    # ---- actions
    def _pids(self):
        return [p["pid"] for p in self.procs]

    def _after(self, res, err, msg):
        ok, emsg = res if res else (False, str(err))
        self.win.toast(msg if ok else f"Failed: {emsg}")
        GLib.timeout_add(600, lambda: (self._closed or self.refresh(), False)[1])

    def _signal(self, pids, sig, msg):
        mine = all(p["uid"] == S.MY_UID for p in self.procs if p["pid"] in pids)
        bg(lambda: S.signal_pids(pids, sig, mine), lambda r, e: self._after(r, e, msg))

    def end_app(self, force):
        n = len(self.procs)
        if force:
            self.win.confirm(f"Force kill {self.disp}?",
                             f"All {n} process(es) stop immediately. Anything unsaved in {self.disp} will be lost.",
                             "Force kill", lambda: self._signal(self._pids(), "KILL", f"{self.disp} was force killed"),
                             destructive=True, parent=self)
        else:
            pids = self._pids()

            def do():
                self._signal(pids, "TERM", f"Asked {self.disp} to close")
                if self.mine:
                    GLib.timeout_add(4000, lambda: (S.kill_pids(S.alive(pids), signal.SIGKILL), False)[1])
            self.win.confirm(f"Close {self.disp}?", f"{self.disp} will be asked to quit ({n} process(es)). "
                             "If it doesn't respond within 4 seconds it is force closed.", "Close app", do,
                             destructive=True, parent=self)

    def end_process(self, pid):
        p = next((x for x in self.procs if x["pid"] == pid), None)
        if not p:
            return
        warn = " This is the main process — the whole app will close." if p["is_main"] else ""
        self.win.confirm(f"End “{p['kind']}” (PID {pid})?", f"Frees about {S.fmt_bytes(p['rss'])}.{warn}", "End process",
                         lambda: self._signal([pid], "TERM", f"Ended process {pid}"), destructive=True, parent=self)

    def end_helpers(self):
        pids = [p["pid"] for p in self.procs if not p["is_main"] and p["kind"] not in
                ("Graphics (GPU)", "Helper (zygote)", "Network", "Storage", "Crash reporter", "Audio")]
        if not pids:
            self.win.toast("No helper processes to end")
            return
        mem = sum(p["rss"] for p in self.procs if p["pid"] in pids)
        self.win.confirm(f"End {len(pids)} helper processes?",
                         f"Frees about {S.fmt_bytes(mem)}. In a browser, open tabs will show “crashed” and can be reloaded.",
                         "End helpers", lambda: self._signal(pids, "TERM", f"Ended {len(pids)} processes"),
                         destructive=True, parent=self)

    def toggle_pause(self, want=None):
        paused = all(p["state"] == "T" for p in self.procs)
        want = (not paused) if want is None else want
        self._signal(self._pids(), "STOP" if want else "CONT", f"{self.disp} paused" if want else f"{self.disp} resumed")

    def set_low_priority(self, on):
        pids = self._pids()
        bg(lambda: S.renice(pids, 10 if on else 0),
           lambda r, e: self._after(r, e, "Priority lowered" if on else "Priority back to normal"))

    def _toggle_cfg(self, key, on):
        cfg = dict(self.win.cfg)
        names = [n for n in cfg[key].split() if n != self.name]
        if on:
            names.append(self.name)
            other = "EXTRA_PROTECT" if key == "PREFER_APPS" else "PREFER_APPS"
            cfg[other] = " ".join(n for n in cfg[other].split() if n != self.name)
        cfg[key] = " ".join(names)
        self.win.save_config(cfg, "Saved", after=self.refresh)

    def open_folder(self):
        exe = self.extra.get("exe", "")
        if not exe.startswith("/"):
            self.win.toast("Program location is not accessible")
            return
        Gio.AppInfo.launch_default_for_uri(GLib.filename_to_uri(os.path.dirname(exe)), None)

    def copy_cmd(self):
        self.get_clipboard().set(" ".join(self.main["cmd"]))
        self.win.toast("Command line copied")

    def show_process(self, pid):
        p = next((x for x in self.procs if x["pid"] == pid), None)
        if not p:
            return
        ex = S.proc_extra(pid)
        txt = (f"Type:        {p['kind']}\nPID:         {pid}   (parent {p['ppid']})\n"
               f"Memory:      {S.fmt_bytes(p['rss'])}\nThreads:     {p['threads']}\n"
               f"State:       {'paused' if p['state'] == 'T' else 'running'}\nPriority:    {p['nice']}\n"
               f"User:        {S.user_name(p['uid'])}\nOpen files:  {ex.get('files')}\n"
               f"Program:     {ex.get('exe')}\n\nCommand line:\n" + "\n  ".join(p["cmd"]))
        self.win.text_dialog(f"{p['kind']} — PID {pid}", txt, parent=self)


class AppsPage(Page):
    title, icon = "Apps", "view-grid-symbolic"

    def __init__(self, win):
        super().__init__(win)
        self.rows = {}
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.append(section("Running apps", "Everything using memory right now, biggest first."))
        info = Gtk.Box(spacing=12)
        info.add_css_class("card")
        info.set_margin_bottom(4)
        ic = Gtk.Image.new_from_icon_name("dialog-information-symbolic")
        ic.set_margin_start(14)
        ic.set_pixel_size(24)
        il = Gtk.Label(wrap=True, xalign=0, hexpand=True, margin_top=12, margin_bottom=12, margin_end=14,
                       label="Click any app to see its live usage, processes and options. "
                             "When memory runs out, PC Watchdog closes the app marked NEXT TO CLOSE.")
        info.append(ic)
        info.append(il)
        box.append(info)
        ctl = Gtk.Box(spacing=10)
        self.search = Gtk.SearchEntry(placeholder_text="Search apps…", hexpand=True)
        self.search.connect("search-changed", lambda *_: self.tick(None, force=True))
        self.show_sys = Gtk.CheckButton(label="Show system processes")
        self.show_sys.connect("toggled", lambda *_: self.tick(None, force=True))
        self.summary = Gtk.Label()
        self.summary.add_css_class("dim-label")
        for w in (self.search, self.show_sys):
            ctl.append(w)
        box.append(ctl)
        self.summary.set_xalign(0)
        box.append(self.summary)
        self.list = boxed_list()
        self.list.set_sort_func(lambda a, b: (b.rss > a.rss) - (b.rss < a.rss))
        box.append(self.list)
        self.widget = page_scroller(box)

    def on_show(self):
        self.tick(None, force=True)

    def tick(self, st, force=False):
        if not force and self.win.current is not self:
            return
        groups, victim = S.app_groups(S.processes(), self.win.cfg)
        total = S.meminfo()["MemTotal"]
        q = self.search.get_text().lower().strip()
        shown = []
        for g in groups:
            if g["rss"] < 20 * 2**20 and not g["victim"]:
                continue
            if not self.show_sys.get_active() and not g["mine"] and not g["victim"]:
                continue
            if q and q not in g["name"].lower() and q not in self.win.app_info(g["name"])[0].lower():
                continue
            shown.append(g)
            if len(shown) >= 40:
                break
        names = {g["name"] for g in shown}
        for n in list(self.rows):
            if n not in names:
                self.list.remove(self.rows.pop(n))
        for g in shown:
            row = self.rows.get(g["name"])
            if row is None:
                row = self.rows[g["name"]] = AppRow(self, g["name"])
                self.list.append(row)
            row.update(g, total)
        self.list.invalidate_sort()
        used = sum(g["rss"] for g in groups if g["mine"])
        self.summary.set_label(f"Your apps use {S.fmt_bytes(used)}")

    def close_app(self, name):
        groups, _ = S.app_groups(S.processes(), self.win.cfg)
        g = next((x for x in groups if x["name"] == name), None)
        if not g:
            return
        disp = self.win.app_name(name)

        def do():
            pids = g["pids"]
            S.kill_pids(pids, signal.SIGTERM)

            def force():
                left = S.alive(pids)
                if left:
                    S.kill_pids(left, signal.SIGKILL)
                self.win.toast(f"Closed {disp} — freed about {S.fmt_bytes(g['rss'])}")
                self.tick(None, force=True)
                return False
            GLib.timeout_add(3000, force)
        self.win.confirm(f"Close {disp}?",
                         f"All {g['count']} {disp} process(es) will be closed. Unsaved work in this app may be lost.",
                         "Close app", do, destructive=True)

    def toggle_list(self, key, name):
        cfg = dict(self.win.cfg)
        names = cfg[key].split()
        adding = name not in names
        names = names + [name] if adding else [n for n in names if n != name]
        cfg[key] = " ".join(names)
        other = "EXTRA_PROTECT" if key == "PREFER_APPS" else "PREFER_APPS"
        if adding:
            cfg[other] = " ".join(n for n in cfg[other].split() if n != name)
        disp = self.win.app_name(name)
        msg = {("PREFER_APPS", True): f"{disp} will be closed first when memory is low",
               ("PREFER_APPS", False): f"{disp} is no longer closed first",
               ("EXTRA_PROTECT", True): f"{disp} will never be closed",
               ("EXTRA_PROTECT", False): f"{disp} can be closed again if needed"}[(key, adding)]
        self.win.save_config(cfg, msg, after=lambda: self.tick(None, force=True))


# ---------------------------------------------------------------- events
class EventsPage(Page):
    title, icon = "Events", "document-open-recent-symbolic"
    FILTERS = [("All", None), ("Closed apps", {"kill"}), ("Warnings", {"warn", "crit"}), ("Service", {"start", "stop"})]
    ICONS = {"kill": "process-stop-symbolic", "crit": "dialog-error-symbolic", "warn": "dialog-warning-symbolic",
             "start": "media-playback-start-symbolic", "stop": "media-playback-pause-symbolic"}

    def __init__(self, win):
        super().__init__(win)
        self.filter = None
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.append(section("Events", "Everything PC Watchdog has done to keep your PC running."))
        stats = Gtk.Box(spacing=14, homogeneous=True)
        self.n_kill, self.n_warn, self.n_last = (self._stat(stats, t) for t in
                                                 ("Apps closed · 7 days", "Warnings · 7 days", "Last incident"))
        box.append(stats)
        self.list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        fb = Gtk.Box(spacing=0, halign=Gtk.Align.START)
        fb.add_css_class("linked")
        first = None
        for label, kinds in self.FILTERS:
            tb = Gtk.ToggleButton(label=label, group=first)
            first = first or tb
            tb.connect("toggled", self._on_filter, kinds)
            fb.append(tb)
        first.set_active(True)
        box.append(fb)
        box.append(self.list_box)
        self.widget = page_scroller(box)

    def _stat(self, parent, title):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        card.add_css_class("card")
        card.add_css_class("stat")
        t = Gtk.Label(label=title, xalign=0)
        t.add_css_class("stat-title")
        v = Gtk.Label(label="–", xalign=0, ellipsize=Pango.EllipsizeMode.END)
        v.add_css_class("stat-value")
        v.set_margin_bottom(6)
        card.append(t)
        card.append(v)
        parent.append(card)
        return v

    def _on_filter(self, tb, kinds):
        if tb.get_active():
            self.filter = kinds
            self.render()

    def on_show(self):
        self.refresh()

    def refresh(self):
        bg(S.events, self._loaded)

    def _loaded(self, ev, err):
        if ev is not None:
            self.win.events_cache = ev
        self.render()

    def render(self):
        ev = self.win.events_cache or []
        week = time.time() - 7 * 86400
        self.n_kill.set_label(str(sum(1 for e in ev if e["kind"] == "kill" and e["ts"] > week)))
        self.n_warn.set_label(str(sum(1 for e in ev if e["kind"] in ("warn", "crit") and e["ts"] > week)))
        inc = next((e for e in ev if e["kind"] in ("kill", "crit", "warn")), None)
        self.n_last.set_label(S.ago(inc["ts"]) if inc else "None")
        clear(self.list_box)
        shown = [e for e in ev if self.filter is None or e["kind"] in self.filter]
        if not shown:
            self.list_box.append(Adw.StatusPage(icon_name="pcw-shield-symbolic", title="All quiet",
                                                description="Nothing has happened here yet. "
                                                            "PC Watchdog will list every app it closes and every warning."))
            return
        day, lst = None, None
        today = time.strftime("%Y-%m-%d")
        yday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
        for e in shown[:300]:
            d = time.strftime("%Y-%m-%d", time.localtime(e["ts"]))
            if d != day:
                day = d
                name = "Today" if d == today else "Yesterday" if d == yday else time.strftime("%A, %b %d", time.localtime(e["ts"]))
                h = Gtk.Label(label=name, xalign=0, margin_top=6)
                h.add_css_class("heading")
                self.list_box.append(h)
                lst = boxed_list()
                self.list_box.append(lst)
            row = Adw.ActionRow(title=GLib.markup_escape_text(e["title"]),
                                subtitle=GLib.markup_escape_text(e["detail"] or ""))
            img = Gtk.Image.new_from_icon_name(self.ICONS.get(e["kind"], "dialog-information-symbolic"))
            img.add_css_class("ev-" + e["kind"])
            row.add_prefix(img)
            tl = Gtk.Label(label=time.strftime("%H:%M", time.localtime(e["ts"])))
            tl.add_css_class("dim-label")
            row.add_suffix(tl)
            lst.append(row)


# ---------------------------------------------------------------- health
class HealthPage(Page):
    title, icon = "Health", "pcw-pulse-symbolic"
    FIX_LABEL = {"install": "Fix", "start": "Start", "cleanup": "Free space", "apps": "See apps",
                 "show_failed": "Details", "show_kerr": "Details"}

    def __init__(self, win):
        super().__init__(win)
        self.loaded = False
        self.data = None
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        top = Gtk.Box(spacing=28)
        top.add_css_class("card")
        top.add_css_class("stat")
        self.ring = Ring(160)
        self.ring.set_margin_top(6)
        self.ring.set_margin_bottom(10)
        top.append(self.ring)
        tv = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, valign=Gtk.Align.CENTER, hexpand=True)
        self.verdict = Gtk.Label(label="Checking your PC…", xalign=0, wrap=True)
        self.verdict.add_css_class("title-1")
        self.verdict_sub = Gtk.Label(xalign=0, wrap=True)
        self.verdict_sub.add_css_class("dim-label")
        bb = Gtk.Box(spacing=10, margin_top=6)
        self.rerun = pill("Run check again", None)
        self.rerun.connect("clicked", lambda *_: self.refresh())
        self.spinner = Adw.Spinner(visible=False)
        bb.append(self.rerun)
        bb.append(self.spinner)
        for w in (self.verdict, self.verdict_sub, bb):
            tv.append(w)
        top.append(tv)
        box.append(top)
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.append(self.body)
        self.widget = page_scroller(box)

    def on_show(self):
        if not self.loaded:
            self.refresh()

    def refresh(self):
        self.loaded = True
        self.spinner.set_visible(True)
        self.rerun.set_sensitive(False)
        bg(S.health_checks, self._done)

    def _done(self, res, err):
        self.spinner.set_visible(False)
        self.rerun.set_sensitive(True)
        if err or not res:
            self.verdict.set_label("Check failed")
            self.verdict_sub.set_label(str(err))
            return
        checks, boots, self.failed, self.kerr = res
        score = S.health_score(checks)
        problems = [c for c in checks if c["status"] in ("warn", "bad")]
        col = "green" if score >= 85 else "orange" if score >= 60 else "red"
        self.ring.set(score / 100, str(score), "of 100", col)
        self.verdict.set_label("Excellent" if score >= 95 else "Good" if score >= 85 else
                               "Needs attention" if score >= 60 else "At risk")
        self.verdict_sub.set_label(
            "Everything looks great." if not problems else
            f"{len(problems)} thing(s) to look at below. Items with a button can be fixed in one click.")
        clear(self.body)
        for group in ("Protection", "Resources", "Stability"):
            items = [c for c in checks if c["group"] == group]
            self.body.append(section(group))
            lst = boxed_list()
            for c in items:
                row = Adw.ActionRow(title=c["title"], subtitle=GLib.markup_escape_text(c["detail"]))
                img = Gtk.Image.new_from_icon_name(STATUS_ICON[c["status"]])
                img.add_css_class("status-" + c["status"])
                row.add_prefix(img)
                if c["fix"]:
                    b = small_button(self.FIX_LABEL.get(c["fix"], "Fix"),
                                     "suggested-action" if c["status"] == "bad" else None)
                    b.connect("clicked", self._fix, c["fix"])
                    row.add_suffix(b)
                lst.append(row)
            self.body.append(lst)
        crashes = sum(1 for b in boots if b["status"] == "crash")
        self.body.append(section(
            "Recent sessions",
            "Sessions that ended without a clean shutdown are freezes, crashes or forced power-offs. "
            + ("With protection on, memory freezes are prevented and total freezes reboot automatically."
               if crashes else "")))
        lst = boxed_list()
        for b in boots:
            row = Adw.ActionRow(title=time.strftime("%A, %b %d · %H:%M", time.localtime(b["start"])),
                                subtitle=("running for " if b["status"] == "current" else "lasted ")
                                + S.fmt_duration(b["duration"]))
            row.add_prefix(Gtk.Image.new_from_icon_name("computer-symbolic"))
            row.add_suffix({"clean": lambda: badge("Clean shutdown", "protect"),
                            "crash": lambda: badge("Crashed / forced off", "next"),
                            "current": lambda: badge("Running now", "system")}[b["status"]]())
            lst.append(row)
        self.body.append(lst)

    def _fix(self, _b, fix):
        w = self.win
        if fix == "install":
            w.do_install(after=self.refresh)
        elif fix == "start":
            w.service_action("start", after=self.refresh)
        elif fix in ("cleanup", "apps"):
            w.go(fix)
        elif fix == "show_failed":
            w.text_dialog("Failed services", "\n".join(self.failed) +
                          "\n\nTip: these services tried to start and failed. Most are harmless, "
                          "but repeated failures can point to a problem.")
        elif fix == "show_kerr":
            w.text_dialog("Kernel errors since boot", "\n".join(self.kerr))


# ---------------------------------------------------------------- cleanup
class CleanupPage(Page):
    title, icon = "Cleanup", "user-trash-symbolic"

    def __init__(self, win):
        super().__init__(win)
        self.items = []
        self.loaded = False
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        top.add_css_class("card")
        top.add_css_class("stat")
        t = Gtk.Label(label="Disk space", xalign=0)
        t.add_css_class("stat-title")
        top.append(t)
        self.disk_bar = Gtk.LevelBar(min_value=0, max_value=1, height_request=14)
        top.append(self.disk_bar)
        self.disk_lbl = Gtk.Label(xalign=0)
        top.append(self.disk_lbl)
        row = Gtk.Box(spacing=14, margin_top=6, margin_bottom=8)
        fv = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.found = Gtk.Label(label="Scanning…", xalign=0)
        self.found.add_css_class("big-number")
        fl = Gtk.Label(label="can be freed safely", xalign=0)
        fl.add_css_class("dim-label")
        fv.append(self.found)
        fv.append(fl)
        row.append(fv)
        self.spinner = Adw.Spinner(visible=False)
        row.append(self.spinner)
        self.clean_rec = pill("Clean recommended")
        self.clean_rec.set_sensitive(False)
        self.clean_rec.connect("clicked", lambda *_: self.clean_recommended())
        row.append(self.clean_rec)
        top.append(row)
        box.append(top)
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.append(self.body)
        self.widget = page_scroller(box)

    def on_show(self):
        self._disk()
        if not self.loaded:
            self.refresh()

    def _disk(self):
        used, total = S.disk_usage()
        self.disk_bar.set_value(used / total)
        self.disk_lbl.set_label(f"{S.fmt_bytes(used)} used of {S.fmt_bytes(total)}  ·  {S.fmt_bytes(total - used)} free")

    def refresh(self):
        self.loaded = True
        self.spinner.set_visible(True)
        self.clean_rec.set_sensitive(False)
        self.found.set_label("Scanning…")
        bg(S.cleanup_targets, self._done)

    def _done(self, items, err):
        self.spinner.set_visible(False)
        self._disk()
        if err or items is None:
            self.found.set_label("Scan failed")
            return
        self.items = items
        rec = sum(i["size"] for i in items if i["safe"])
        self.found.set_label(S.fmt_bytes(rec))
        self.clean_rec.set_sensitive(rec > 0)
        self.clean_rec.set_label(f"Clean recommended ({S.fmt_bytes(rec)})")
        clear(self.body)
        group, lst = None, None
        for it in items:
            if it["group"] != group:
                group = it["group"]
                self.body.append(section(group))
                lst = boxed_list()
                self.body.append(lst)
            row = Adw.ActionRow(title=GLib.markup_escape_text(it["title"]),
                                subtitle=GLib.markup_escape_text(it["desc"]))
            row.add_prefix(Gtk.Image.new_from_icon_name(it["icon"]))
            if it["safe"]:
                row.add_suffix(badge("Recommended", "protect"))
            sz = Gtk.Label(label=S.fmt_bytes(it["size"]), width_chars=9, xalign=1)
            sz.add_css_class("heading")
            row.add_suffix(sz)
            b = small_button("Clean", None if it["safe"] else "destructive-action")
            b.set_sensitive(it["size"] > 0)
            b.connect("clicked", lambda _b, it=it: self.clean_one(it))
            row.add_suffix(b)
            lst.append(row)

    def clean_one(self, it):
        warn = "" if it["safe"] else "\n\nThis cannot be undone."
        self.win.confirm(f"Clean {it['title']}?", f"{it['desc']}\n\nFrees about {S.fmt_bytes(it['size'])}.{warn}",
                         "Clean", lambda: self._run([it]), destructive=not it["safe"])

    def clean_recommended(self):
        items = [i for i in self.items if i["safe"] and i["size"] > 0]
        names = "\n".join(f"• {i['title']} — {S.fmt_bytes(i['size'])}" for i in items)
        self.win.confirm("Clean recommended items?",
                         f"These are caches that are rebuilt automatically:\n\n{names}\n\n"
                         "System items will ask for your password once.",
                         "Clean all", lambda: self._run(items))

    def _run(self, items):
        before = S.disk_usage()[0]
        self.spinner.set_visible(True)
        self.clean_rec.set_sensitive(False)

        def work():
            errs = []
            for it in items:
                ok, err = S.clean(it["id"])
                if not ok:
                    errs.append(f"{it['title']}: {err}")
            return errs

        def done(errs, err):
            freed = before - S.disk_usage()[0]
            if errs:
                self.win.toast("Some items failed: " + "; ".join(errs)[:200])
            else:
                self.win.toast(f"Done — freed {S.fmt_bytes(max(freed, 0))}")
            self.refresh()
        bg(work, done)


# ---------------------------------------------------------------- settings
class SettingsPage(Page):
    title, icon = "Settings", "preferences-system-symbolic"
    SPINS = [
        ("Memory protection", "When should PC Watchdog close an app to stop a freeze?", [
            ("MEM_MIN_PCT", "Free RAM below (%)", "Act when available memory drops below this", 1, 30, 1),
            ("SWAP_MIN_PCT", "…and free swap below (%)", "Both conditions must be true", 0, 60, 1),
            ("PSI_FULL_MAX", "Freeze detection: stall level (%)", "Lower = acts sooner when the PC starts to lag", 10, 90, 5),
            ("PSI_STRIKES", "Freeze detection: duration (checks)", "How many 2-second checks the stall must last", 1, 30, 1),
            ("KILL_COOLDOWN", "Wait between closing apps (seconds)", "Gives memory time to free up before closing another", 3, 120, 1),
        ]),
        ("Alerts", "When should you get a notification?", [
            ("TEMP_WARN", "CPU hot warning (°C)", "Normal notification above this temperature", 50, 105, 1),
            ("TEMP_CRIT", "CPU critical warning (°C)", "Urgent notification above this temperature", 60, 110, 1),
            ("DISK_MIN_PCT", "Low disk warning (% free)", "Warn when free space drops below this", 1, 50, 1),
            ("NOTIFY_COOLDOWN", "Repeat warnings every (minutes)", "Avoids spamming the same alert", 1, 120, 1),
        ]),
    ]

    def __init__(self, win):
        super().__init__(win)
        self.pending = None
        self.spins = {}
        self._guard = False
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.banner = Adw.Banner(title="You have unsaved changes", button_label="Save changes")
        self.banner.connect("button-clicked", lambda *_: self.save())
        outer.append(self.banner)
        page = Adw.PreferencesPage(vexpand=True)
        outer.append(page)
        for gtitle, gdesc, rows in self.SPINS:
            grp = Adw.PreferencesGroup(title=gtitle, description=gdesc)
            for key, title, sub, lo, hi, step in rows:
                r = Adw.SpinRow.new_with_range(lo, hi, step)
                r.set_title(title)
                r.set_subtitle(sub)
                r.connect("notify::value", self._on_spin, key)
                self.spins[key] = r
                grp.add(r)
            page.add(grp)
        self.lists = {}
        for key, title, desc in (
                ("PREFER_APPS", "Close these apps first",
                 "When memory runs out, these are closed before anything else. Use process names (see the Apps page)."),
                ("EXTRA_PROTECT", "Never close these apps",
                 "These apps are never closed, even when memory runs out. Core system parts are always protected.")):
            grp = Adw.PreferencesGroup(title=title, description=desc)
            entry = Adw.EntryRow(title="Add app by process name, then press Enter", show_apply_button=True)
            entry.connect("apply", self._on_add, key)
            entry.connect("entry-activated", self._on_add, key)
            grp.add(entry)
            self.lists[key] = (grp, entry, [])
            page.add(grp)
        grp = Adw.PreferencesGroup(title="Tools")
        for title, sub, label, cb, style in (
                ("Test notification", "Check that alerts reach your desktop", "Send test", self._test, None),
                ("Restore defaults", "Reset every setting above to the recommended values", "Restore", self._defaults, None),
                ("Reinstall / update protection", "Repairs the background service and system settings", "Reinstall",
                 lambda: win.do_install(), None),
                ("Show guide again", "The short tour of what PC Watchdog does", "Open guide", lambda: win.go("guide"), None),
                ("Uninstall protection", "Removes the background service. Your PC will no longer be protected.",
                 "Uninstall", self._uninstall, "destructive-action")):
            r = Adw.ActionRow(title=title, subtitle=sub)
            b = small_button(label, style)
            b.connect("clicked", lambda *_, cb=cb: cb())
            r.add_suffix(b)
            grp.add(r)
        page.add(grp)
        self.widget = outer

    def on_show(self):
        if self.pending is None:
            self.load(self.win.cfg)

    def load(self, cfg):
        self._guard = True
        for key, r in self.spins.items():
            r.set_value(cfg[key] / 60 if key == "NOTIFY_COOLDOWN" else cfg[key])
        for key in self.lists:
            self._render_list(key, cfg[key].split())
        self._guard = False

    def _render_list(self, key, names):
        grp, entry, rows = self.lists[key]
        for r in rows:
            grp.remove(r)
        rows.clear()
        for n in names:
            r = Adw.ActionRow(title=GLib.markup_escape_text(self.win.app_name(n)),
                              subtitle=n if self.win.app_name(n) != n else "")
            _, icon = self.win.app_info(n)
            img = Gtk.Image.new_from_gicon(icon) if icon else Gtk.Image.new_from_icon_name("application-x-executable")
            img.set_pixel_size(24)
            r.add_prefix(img)
            b = Gtk.Button(icon_name="list-remove-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Remove")
            b.add_css_class("flat")
            b.connect("clicked", lambda *_, n=n: self._remove(key, n))
            r.add_suffix(b)
            grp.add(r)
            rows.append(r)

    def _cur(self):
        if self.pending is None:
            self.pending = dict(self.win.cfg)
        return self.pending

    def _dirty(self):
        self.banner.set_revealed(self.pending is not None and self.pending != self.win.cfg)

    def _on_spin(self, r, _p, key):
        if self._guard:
            return
        v = int(r.get_value())
        self._cur()[key] = v * 60 if key == "NOTIFY_COOLDOWN" else v
        self._dirty()

    def _on_add(self, entry, key):
        name = entry.get_text().strip()
        if not name:
            return
        if not S.VALID_NAME.match(name):
            self.win.toast("Use the process name, e.g. brave or code (letters, numbers, . _ - +)")
            return
        cfg = self._cur()
        names = cfg[key].split()
        if name not in names:
            names.append(name)
        cfg[key] = " ".join(names)
        entry.set_text("")
        self._render_list(key, names)
        self._dirty()

    def _remove(self, key, name):
        cfg = self._cur()
        names = [n for n in cfg[key].split() if n != name]
        cfg[key] = " ".join(names)
        self._render_list(key, names)
        self._dirty()

    def save(self):
        if self.pending is None:
            return
        cfg = self.pending
        if cfg["TEMP_CRIT"] <= cfg["TEMP_WARN"]:
            self.win.toast("Critical temperature must be higher than the warning temperature")
            return

        def after():
            self.pending = None
            self._dirty()
        self.win.save_config(cfg, "Settings saved and applied", after=after)

    def _defaults(self):
        self.pending = dict(S.DEFAULTS)
        self.load(self.pending)
        self._dirty()

    def _test(self):
        self.win.toast("Notification sent" if S.notify_test() else "Could not send notification")

    def _uninstall(self):
        self.win.confirm("Uninstall protection?",
                         "The background service, hardware watchdog and crash-reboot settings will be removed. "
                         "This app stays installed so you can reinstall any time.",
                         "Uninstall", lambda: self.win.run_priv(lambda: S.helper("uninstall"),
                                                                "Protection removed"), destructive=True)


# ---------------------------------------------------------------- guide
class GuidePage(Page):
    title, icon = "Guide", "help-browser-symbolic"

    def __init__(self, win):
        super().__init__(win)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_bottom=20)
        self.car = Adw.Carousel(vexpand=True, hexpand=True, allow_scroll_wheel=False)
        slides = [
            ("io.github.pcwatchdog", "Welcome to PC Watchdog",
             "PC Watchdog keeps your PC from freezing and crashing.\n"
             "It works in the background all the time — you don't need to keep this app open.", None),
            ("pcw-memory-symbolic", "Stops memory freezes",
             "When RAM is almost full, Linux can freeze for minutes. PC Watchdog closes the biggest app "
             "(Brave, VS Code, Java first) a moment before that happens, and tells you with a notification.", None),
            ("system-reboot-symbolic", "Recovers from total freezes",
             "If the whole PC hangs, the watchdog chip on your motherboard reboots it automatically after 30 seconds. "
             "After a kernel crash it reboots in 10 seconds — no more holding the power button.", None),
            ("pcw-temp-symbolic", "Warns you early",
             "You get a notification when the CPU is too hot or the disk is nearly full — "
             "both common causes of crashes.", None),
            ("input-keyboard-symbolic", "Emergency keys",
             "If the screen is frozen and nothing responds, hold Alt and the SysRq / PrtSc key, then press:",
             self._keys()),
            ("view-grid-symbolic", "How to use this app",
             "Everything is in the sidebar on the left:", self._howto()),
        ]
        for icon, title, desc, child in slides:
            sp = Adw.StatusPage(icon_name=icon, title=title, description=desc, hexpand=True, vexpand=True)
            if child:
                sp.set_child(child)
            self.car.append(sp)
        outer.append(self.car)
        outer.append(Adw.CarouselIndicatorDots(carousel=self.car))
        nav = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER, margin_top=6)
        self.back = pill("Back", None)
        self.next = pill("Next")
        self.back.connect("clicked", lambda *_: self._step(-1))
        self.next.connect("clicked", lambda *_: self._step(1))
        nav.append(self.back)
        nav.append(self.next)
        outer.append(nav)
        self.car.connect("page-changed", lambda *_: self._update())
        self._update()
        self.widget = outer

    def _keys(self):
        lst = boxed_list()
        lst.set_halign(Gtk.Align.CENTER)
        for keys, title, sub in (
                (("Alt", "SysRq", "F"), "Close the biggest memory hog", "Use when the PC is frozen because memory is full"),
                (("Alt", "SysRq", "R E I S U B"), "Safe reboot",
                 "Press the letters one by one, about 1 second apart. Saves your disk from damage.")):
            r = Adw.ActionRow(title=title, subtitle=sub)
            r.add_suffix(keycaps(*keys))
            lst.append(r)
        return lst

    def _howto(self):
        lst = boxed_list()
        lst.set_halign(Gtk.Align.CENTER)
        lst.set_size_request(520, -1)
        for icon, title, sub in (
                ("pcw-shield-symbolic", "Dashboard", "Live status, the protection switch and charts"),
                ("view-grid-symbolic", "Apps", "Choose which apps close first, or are never closed"),
                ("document-open-recent-symbolic", "Events", "Every app closed and every warning"),
                ("pcw-pulse-symbolic", "Health", "Find and fix problems in one click"),
                ("user-trash-symbolic", "Cleanup", "Free disk space from caches, trash and old packages"),
                ("preferences-system-symbolic", "Settings", "Adjust when PC Watchdog steps in")):
            r = Adw.ActionRow(title=title, subtitle=sub)
            r.add_prefix(Gtk.Image.new_from_icon_name(icon))
            lst.append(r)
        return lst

    def _step(self, d):
        i = round(self.car.get_position()) + d
        n = self.car.get_n_pages()
        if i >= n:
            st = load_state()
            st["guide_seen"] = True
            save_state(st)
            self.win.go("dashboard")
            return
        self.car.scroll_to(self.car.get_nth_page(max(0, i)), True)

    def _update(self):
        i = round(self.car.get_position())
        self.back.set_sensitive(i > 0)
        self.next.set_label("Start using PC Watchdog" if i >= self.car.get_n_pages() - 1 else "Next")


# ====================================================================== window
class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="PC Watchdog", default_width=1220, default_height=820)
        self.set_size_request(380, 500)
        self.cfg = S.load_config()
        self.events_cache = None
        self.current = None
        self._app_map = None
        self.cpu = S.CpuSampler()

        self.toasts = Adw.ToastOverlay()
        self.split = Adw.NavigationSplitView(min_sidebar_width=220, max_sidebar_width=280)

        # sidebar
        side_tv = Adw.ToolbarView()
        shb = Adw.HeaderBar()
        menu = Gio.Menu()
        menu.append("Guide", "win.guide")
        menu.append("Keyboard shortcuts", "win.shortcuts")
        menu.append("About PC Watchdog", "win.about")
        shb.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Menu"))
        side_tv.add_top_bar(shb)
        sbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.nav = Gtk.ListBox(vexpand=True)
        self.nav.add_css_class("navigation-sidebar")
        sbox.append(self.nav)
        self.side_status = Gtk.Box(spacing=10)
        self.side_status.add_css_class("sidebar-status")
        self.side_icon = Gtk.Image.new_from_icon_name("pcw-shield-symbolic")
        self.side_lbl = Gtk.Label(xalign=0, wrap=True)
        self.side_status.append(self.side_icon)
        self.side_status.append(self.side_lbl)
        sbox.append(self.side_status)
        side_tv.set_content(sbox)
        self.split.set_sidebar(Adw.NavigationPage(title="PC Watchdog", child=side_tv))

        # content
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=150)
        ctv = Adw.ToolbarView()
        chb = Adw.HeaderBar()
        self.refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh (Ctrl+R)")
        self.refresh_btn.connect("clicked", lambda *_: self.current and self.current.refresh())
        chb.pack_end(self.refresh_btn)
        ctv.add_top_bar(chb)
        ctv.set_content(self.stack)
        self.content_page = Adw.NavigationPage(title="Dashboard", child=ctv)
        self.split.set_content(self.content_page)

        self.pages = {}
        for key, cls in (("dashboard", Dashboard), ("apps", AppsPage), ("events", EventsPage),
                         ("health", HealthPage), ("cleanup", CleanupPage), ("settings", SettingsPage),
                         ("guide", GuidePage)):
            p = cls(self)
            self.pages[key] = p
            self.stack.add_named(p.widget, key)
            row = Gtk.ListBoxRow()
            row.key = key
            rb = Gtk.Box(spacing=12, margin_top=8, margin_bottom=8, margin_start=6)
            rb.append(Gtk.Image.new_from_icon_name(p.icon))
            rb.append(Gtk.Label(label=p.title, xalign=0))
            row.set_child(rb)
            self.nav.append(row)
        self.nav.connect("row-activated", lambda _l, row: self.go(row.key))

        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 760sp"))
        bp.add_setter(self.split, "collapsed", True)
        bp.connect("apply", lambda *_: self.add_css_class("narrow"))
        bp.connect("unapply", lambda *_: self.remove_css_class("narrow"))
        self.add_breakpoint(bp)
        self.toasts.set_child(self.split)
        self.set_content(self.toasts)

        for name, cb, accel in (("about", self._about, None), ("guide", lambda: self.go("guide"), "F1"),
                                ("shortcuts", self._shortcuts, "<Ctrl>question"),
                                ("refresh", lambda: self.current and self.current.refresh(), "<Ctrl>r")):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_a, cb=cb: cb())
            self.add_action(a)
            if accel:
                app.set_accels_for_action(f"win.{name}", [accel])
        for i, key in enumerate(self.pages, 1):
            a = Gio.SimpleAction.new(f"page{i}", None)
            a.connect("activate", lambda *_a, k=key: self.go(k))
            self.add_action(a)
            app.set_accels_for_action(f"win.page{i}", [f"<Alt>{i}"])

        self.go("dashboard" if load_state().get("guide_seen") else "guide")
        self._tick()
        GLib.timeout_add_seconds(2, self._tick)
        self._load_events()
        GLib.timeout_add_seconds(30, self._load_events)

    # ---- navigation / helpers
    def go(self, key):
        p = self.pages[key]
        self.current = p
        self.stack.set_visible_child_name(key)
        self.content_page.set_title(p.title)
        row = self.nav.get_first_child()
        while row is not None:
            if getattr(row, "key", None) == key:
                self.nav.select_row(row)
            row = row.get_next_sibling()
        self.split.set_show_content(True)
        p.on_show()

    def toast(self, msg, timeout=4):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(msg), timeout=timeout))

    def confirm(self, heading, body, label, on_yes, destructive=False, parent=None):
        d = Adw.AlertDialog(heading=heading, body=body)
        d.add_response("cancel", "Cancel")
        d.add_response("ok", label)
        d.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE if destructive
                                  else Adw.ResponseAppearance.SUGGESTED)
        d.set_default_response("cancel" if destructive else "ok")
        d.set_close_response("cancel")
        d.connect("response", lambda _d, r: on_yes() if r == "ok" else None)
        d.present(parent or self)

    def text_dialog(self, title, text, parent=None):
        d = Adw.AlertDialog(heading=title)
        tv = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                          top_margin=8, bottom_margin=8, left_margin=8, right_margin=8)
        tv.get_buffer().set_text(text)
        sw = Gtk.ScrolledWindow(child=tv, min_content_height=320, min_content_width=600)
        sw.add_css_class("card")
        d.set_extra_child(sw)
        d.add_response("close", "Close")
        d.present(parent or self)

    def app_info(self, comm):
        if self._app_map is None:
            self._app_map = {}
            for ai in Gio.AppInfo.get_all():
                exe = ai.get_executable() or ""
                base = os.path.basename(exe)[:15]
                if base and base not in self._app_map and base not in ("env", "sh", "bash", "flatpak", "python3"):
                    self._app_map[base] = (ai.get_display_name(), ai.get_icon())
        if comm in self._app_map:
            return self._app_map[comm]
        theme = Gtk.IconTheme.get_for_display(self.get_display())
        if theme.has_icon(comm.lower()):
            return comm, Gio.ThemedIcon.new(comm.lower())
        return comm, None

    def open_app(self, name):
        AppDialog(self, name).present(self)

    def app_name(self, comm):
        return self.app_info(comm)[0]

    # ---- actions
    def run_priv(self, fn, ok_msg, after=None):
        self.toast("Waiting for your password…", timeout=2)

        def done(res, err):
            ok, msg = res if res else (False, str(err))
            self.toast(ok_msg if ok else f"Failed: {msg}")
            self.cfg = S.load_config()
            self._tick()
            if after:
                after()
        bg(fn, done)

    def service_action(self, action, after=None):
        msg = {"start": "Protection turned on", "stop": "Protection turned off", "restart": "Protection restarted"}[action]
        bg(lambda: S.service_action(action),
           lambda res, err: (self.toast(msg if res and res[0] else f"Failed: {res[1] if res else err}"),
                             self._tick(), after and after()))

    def do_install(self, after=None):
        self.run_priv(S.install, "Protection installed and up to date", after)

    def save_config(self, cfg, ok_msg, after=None):
        def fn():
            return S.save_config(cfg)

        def then():
            if after:
                after()
        self.run_priv(fn, ok_msg, then)

    # ---- periodic
    def _tick(self):
        m = S.meminfo()
        cpu, cores = self.cpu.sample()
        groups, victim = S.app_groups(S.processes(), self.cfg) if self.current is self.pages["dashboard"] else ([], None)
        st = {
            "mem": m, "avail_pct": m["MemAvailable"] / m["MemTotal"] * 100,
            "cpu": cpu, "cores": cores, "temp": S.cpu_temp(), "psi": S.psi_memory(),
            "disk": S.disk_usage(), "svc": S.service_info(), "hw": S.hw_watchdog(),
            "uptime": S.uptime(), "victim": victim,
            "kills_today": S.kills_today(self.events_cache or []),
        }
        self.pages["dashboard"].tick(st)
        self.pages["apps"].tick(st)
        svc = st["svc"]
        state = "ok" if svc["active"] else "bad"
        for c in ("ok", "bad", "warn"):
            self.side_status.remove_css_class(c)
        self.side_status.add_css_class(state)
        self.side_lbl.set_markup("<b>Protected</b>\n<small>Running in background</small>" if svc["active"] else
                                 "<b>Not protected</b>\n<small>Open Dashboard to fix</small>")
        return True

    def _load_events(self):
        def done(ev, err):
            if ev is not None:
                self.events_cache = ev
        bg(S.events, done)
        return True

    def _about(self):
        Adw.AboutDialog(application_name="PC Watchdog", application_icon=APP_ID, version=VERSION,
                        developer_name="thang", comments="Keeps your PC from freezing and crashing.",
                        license_type=Gtk.License.MIT_X11).present(self)

    def _shortcuts(self):
        self.text_dialog("Keyboard shortcuts",
                         "Alt+1 … Alt+7   Switch page\nCtrl+R          Refresh\nF1              Guide\n"
                         "Ctrl+Q          Quit\n\nEmergency (when the PC is frozen):\n"
                         "Alt+SysRq+F     Close biggest memory hog\nAlt+SysRq+R,E,I,S,U,B   Safe reboot")


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        GLib.set_application_name("PC Watchdog")

    def do_startup(self):
        Adw.Application.do_startup(self)
        disp = Gdk.Display.get_default()
        Gtk.IconTheme.get_for_display(disp).add_search_path(os.path.join(HERE, "icons"))
        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(disp, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Gtk.Window.set_default_icon_name(APP_ID)
        q = Gio.SimpleAction.new("quit", None)
        q.connect("activate", lambda *_: self.quit())
        self.add_action(q)
        self.set_accels_for_action("app.quit", ["<Ctrl>q"])

    def do_activate(self):
        win = self.get_active_window() or Window(self)
        win.present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
