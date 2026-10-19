"""System data + actions for the PC Watchdog app. No GTK in here."""
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import time

HOME = os.path.expanduser("~")
APPDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # ~/pc-watchdog
SERVICE = "pc-watchdog.service"
CONF = "/etc/pc-watchdog.conf"
HELPER = "/usr/local/lib/pc-watchdog/pcw-helper"
DAEMON = "/usr/local/bin/pc-watchdog.sh"
MY_UID = os.getuid()

DEFAULTS = {
    "INTERVAL": 2, "MEM_MIN_PCT": 5, "SWAP_MIN_PCT": 10, "PSI_FULL_MAX": 40,
    "PSI_STRIKES": 3, "KILL_COOLDOWN": 10, "TEMP_WARN": 90, "TEMP_CRIT": 97,
    "DISK_MIN_PCT": 5, "NOTIFY_COOLDOWN": 300,
    "PREFER_APPS": "brave chrome chromium firefox electron code java node slack discord",
    "EXTRA_PROTECT": "",
}
INT_KEYS = [k for k, v in DEFAULTS.items() if isinstance(v, int)]
# must match BASE_PROTECT in pc-watchdog.sh
BASE_PROTECT = re.compile(
    r"^(systemd.*|init|Hyprland|Xwayland|sddm|gdm|greetd|login|sshd|dbus.*|pipewire.*|wireplumber|"
    r"NetworkManager|wpa_supplicant|iwd|kwin.*|gnome-shell|plasmashell|Xorg|agetty|journald|udevd|"
    r"pc-watchdog.*|pcwatchdog.*|bash|zsh|fish|sudo|polkit.*|qs|quickshell)$")
VALID_NAME = re.compile(r"^[A-Za-z0-9._+-]{1,32}$")


def run(cmd, timeout=15, input=None):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def fmt_bytes(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


def fmt_duration(sec):
    sec = int(sec)
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m = sec // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def ago(ts):
    s = time.time() - ts
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)} min ago"
    if s < 86400:
        return f"{int(s // 3600)} h ago"
    return time.strftime("%b %d, %H:%M", time.localtime(ts))


# ---------------------------------------------------------------- live stats
def meminfo():
    out = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            out[k] = int(v.split()[0]) * 1024
    return out


class CpuSampler:
    def __init__(self):
        self.prev = self._read()

    @staticmethod
    def _read():
        rows = []
        with open("/proc/stat") as f:
            for line in f:
                if not line.startswith("cpu"):
                    break
                v = list(map(int, line.split()[1:]))
                idle = v[3] + (v[4] if len(v) > 4 else 0)
                rows.append((sum(v), idle))
        return rows

    def sample(self):
        cur = self._read()
        pct = []
        for (t1, i1), (t0, i0) in zip(cur, self.prev):
            dt = t1 - t0
            pct.append(0.0 if dt <= 0 else 100.0 * (1 - (i1 - i0) / dt))
        self.prev = cur
        return pct[0], pct[1:]  # total, per core


def cpu_temp():
    best = 0
    base = "/sys/class/hwmon"
    try:
        for h in os.listdir(base):
            try:
                name = open(f"{base}/{h}/name").read().strip()
            except OSError:
                continue
            if name not in ("coretemp", "k10temp", "zenpower"):
                continue
            for f in os.listdir(f"{base}/{h}"):
                if f.startswith("temp") and f.endswith("_input"):
                    try:
                        best = max(best, int(open(f"{base}/{h}/{f}").read()) // 1000)
                    except (OSError, ValueError):
                        pass
    except OSError:
        pass
    return best


def psi_memory():
    some = full = 0.0
    try:
        for line in open("/proc/pressure/memory"):
            parts = dict(p.split("=") for p in line.split()[1:])
            if line.startswith("some"):
                some = float(parts["avg10"])
            else:
                full = float(parts["avg10"])
    except OSError:
        pass
    return some, full


def disk_usage(path="/"):
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    return total - free, total


def uptime():
    return float(open("/proc/uptime").read().split()[0])


# ---------------------------------------------------------------- service
def service_info():
    r = run(["systemctl", "show", SERVICE, "-p", "LoadState", "-p", "ActiveState",
             "-p", "UnitFileState", "-p", "ActiveEnterTimestampMonotonic"], timeout=5)
    info = dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)
    installed = info.get("LoadState") == "loaded"
    return {
        "installed": installed,
        "active": info.get("ActiveState") == "active",
        "state": info.get("ActiveState", "unknown"),
        "enabled": info.get("UnitFileState") == "enabled",
    }


def hw_watchdog():
    v = run(["systemctl", "show", "-p", "RuntimeWatchdogUSec", "--value"], timeout=5).stdout.strip()
    return None if v in ("", "0", "infinity") else v


def daemon_outdated():
    try:
        a = hashlib.sha256(open(DAEMON, "rb").read()).digest()
        b = hashlib.sha256(open(os.path.join(APPDIR, "pc-watchdog.sh"), "rb").read()).digest()
        helper_ok = os.path.exists(HELPER)
        return a != b or not helper_ok
    except OSError:
        return True


def load_config():
    cfg = dict(DEFAULTS)
    try:
        for line in open(CONF):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.split("#")[0].strip().strip("'\"")
            if k in INT_KEYS:
                try:
                    cfg[k] = int(v)
                except ValueError:
                    pass
            elif k in ("PREFER_APPS", "EXTRA_PROTECT"):
                cfg[k] = v
    except OSError:
        pass
    return cfg


def config_text(cfg):
    lines = ["# /etc/pc-watchdog.conf - managed by the PC Watchdog app (Settings page)"]
    for k in INT_KEYS:
        lines.append(f"{k}={int(cfg[k])}")
    for k in ("PREFER_APPS", "EXTRA_PROTECT"):
        names = [n for n in cfg[k].split() if VALID_NAME.match(n)]
        lines.append(f'{k}="{" ".join(names)}"')
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- privileged actions
def _pk_result(r):
    if r.returncode == 0:
        return True, ""
    if r.returncode in (126, 127):
        return False, "Cancelled — password not entered"
    return False, (r.stderr or r.stdout).strip()[-400:] or f"failed (code {r.returncode})"


def helper(*args, input=None, timeout=300):
    if not os.path.exists(HELPER):
        return False, "Helper not installed — click Install / Update first"
    return _pk_result(run(["pkexec", HELPER, *args], input=input, timeout=timeout))


def service_action(action):
    # polkit rule lets wheel users do this without a password; fall back to helper
    r = run(["systemctl", "--no-ask-password", action, SERVICE], timeout=20)
    if r.returncode == 0:
        return True, ""
    return helper(action)


def install():
    return _pk_result(run(["pkexec", os.path.join(APPDIR, "install.sh")], timeout=300))


def save_config(cfg):
    return helper("save-config", input=config_text(cfg))


def notify_test():
    r = run(["notify-send", "-a", "PC Watchdog", "-i", "io.github.pcwatchdog",
             "PC Watchdog test", "Notifications are working. You'll see alerts like this one."])
    return r.returncode == 0


# ---------------------------------------------------------------- processes
def processes():
    procs = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        d = f"/proc/{pid}"
        try:
            comm = open(f"{d}/comm").read().strip()
            uid = rss = 0
            with open(f"{d}/status") as f:
                for line in f:
                    if line.startswith("Uid:"):
                        uid = int(line.split()[1])
                    elif line.startswith("VmRSS:"):
                        rss = int(line.split()[1]) * 1024
            score = int(open(f"{d}/oom_score").read())
            adj = int(open(f"{d}/oom_score_adj").read())
        except (OSError, ValueError):
            continue
        procs.append({"pid": int(pid), "comm": comm, "uid": uid, "rss": rss,
                      "score": score, "adj": adj})
    return procs


def _names(s):
    return set(s.split())


def is_protected(comm, cfg):
    return bool(BASE_PROTECT.match(comm)) or comm in _names(cfg["EXTRA_PROTECT"])


def app_groups(procs, cfg):
    """Group processes by name, and predict which one the daemon would close next."""
    prefer = _names(cfg["PREFER_APPS"])
    victim, best = None, -1
    groups = {}
    me = os.getpid()
    for p in procs:
        g = groups.setdefault(p["comm"], {"name": p["comm"], "rss": 0, "count": 0, "pids": [],
                                          "mine": True, "protected": is_protected(p["comm"], cfg),
                                          "prefer": p["comm"] in prefer})
        g["rss"] += p["rss"]
        g["count"] += 1
        g["pids"].append(p["pid"])
        if p["uid"] != MY_UID:
            g["mine"] = False
        if p["pid"] == me or g["protected"] or p["adj"] == -1000 or p["score"] <= 0:
            continue
        s = p["score"] * (3 if g["prefer"] else 1)
        if s > best:
            best, victim = s, p
    for g in groups.values():
        g["victim"] = victim is not None and g["name"] == victim["comm"]
    return sorted(groups.values(), key=lambda g: -g["rss"]), victim


def kill_pids(pids, sig=signal.SIGTERM):
    ok = 0
    for pid in pids:
        try:
            os.kill(pid, sig)
            ok += 1
        except (ProcessLookupError, PermissionError):
            pass
    return ok


def alive(pids):
    out = []
    for pid in pids:
        try:
            os.kill(pid, 0)
            out.append(pid)
        except (ProcessLookupError, PermissionError):
            pass
    return out


# ---------------------------------------------------------------- events
KILL_RE = re.compile(r"LOW MEMORY \((.*)\): killing (\S+) \(pid (\d+), (\d+) MiB\)")
NOTIFY_RE = re.compile(r"NOTIFY \[(\w+)\] (.*?): (.*)")


def events(limit=500):
    r = run(["journalctl", "-u", SERVICE, "-o", "json", "-n", str(limit * 3), "--no-pager"], timeout=20)
    out = []
    for line in r.stdout.splitlines():
        try:
            j = json.loads(line)
        except ValueError:
            continue
        msg = j.get("MESSAGE")
        if not isinstance(msg, str):
            continue
        ts = int(j.get("__REALTIME_TIMESTAMP", "0")) / 1e6
        ident = j.get("SYSLOG_IDENTIFIER", "")
        ev = None
        if ident.startswith("pc-watchdog"):
            m = KILL_RE.search(msg)
            if m:
                ev = ("kill", f"Closed {m.group(2)} to prevent a freeze",
                      f"Freed about {int(m.group(4))} MB · {m.group(1)}")
            elif (m := NOTIFY_RE.search(msg)):
                urg, title, body = m.groups()
                if title.startswith("Low memory - killed"):
                    continue
                ev = ("crit" if urg == "critical" else "warn", title, body)
            elif msg.startswith("started"):
                ev = ("start", "Protection started", "Watching memory, temperature and disk")
            elif "no killable" in msg:
                ev = ("warn", "Low memory", msg)
        elif ident == "systemd":
            if msg.startswith("Stopped "):
                ev = ("stop", "Protection stopped", "")
            elif "watchdog timeout" in msg.lower() or msg.startswith("Failed") or "Main process exited" in msg:
                ev = ("crit", "Watchdog service problem", msg)
        if ev:
            out.append({"kind": ev[0], "title": ev[1], "detail": ev[2], "ts": ts})
    out.reverse()
    return out[:limit]


def kills_today(evts):
    t0 = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    return sum(1 for e in evts if e["kind"] == "kill" and e["ts"] >= t0)


# ---------------------------------------------------------------- health
CLEAN_MARKERS = ("Journal stopped", "Shutting down.", "Reached target System Power Off",
                 "Reached target System Reboot", "System is rebooting", "System is powering down")


def boot_history(n=10):
    r = run(["journalctl", "--list-boots", "-o", "json", "--no-pager"], timeout=20)
    try:
        boots = json.loads(r.stdout)
    except ValueError:
        return []
    out = []
    for b in boots[-(n + 1):]:
        first, last = b["first_entry"] / 1e6, b["last_entry"] / 1e6
        if b["index"] == 0:
            out.append({"start": first, "duration": time.time() - first, "status": "current"})
            continue
        tail = run(["journalctl", "-b", b["boot_id"], "-n", "40", "-o", "cat", "--no-pager", "-q"],
                   timeout=15).stdout
        clean = any(m in tail for m in CLEAN_MARKERS)
        out.append({"start": first, "duration": last - first, "status": "clean" if clean else "crash"})
    out.reverse()
    return out


def failed_units():
    sysu = run(["systemctl", "--failed", "--no-legend", "--plain"]).stdout.split("\n")
    usr = run(["systemctl", "--user", "--failed", "--no-legend", "--plain"]).stdout.split("\n")
    return [l.split()[0] for l in sysu if l.strip()] + [l.split()[0] + " (user)" for l in usr if l.strip()]


def kernel_errors():
    r = run(["journalctl", "-k", "-p", "err", "-b", "-o", "short-precise", "--no-pager", "-q"])
    return [l for l in r.stdout.splitlines() if l.strip()]


def sysctl(name):
    try:
        return open("/proc/sys/" + name.replace(".", "/")).read().strip()
    except OSError:
        return ""


def health_checks():
    """Returns (checks, boots). Each check: id,title,detail,status(ok|warn|bad|info),fix,weight."""
    svc = service_info()
    m = meminfo()
    used, total = disk_usage()
    temp = cpu_temp()
    checks = []

    def add(cid, title, status, detail, fix=None, weight=1):
        checks.append({"id": cid, "title": title, "status": status, "detail": detail,
                       "fix": fix, "weight": weight, "group": group})

    group = "Protection"
    if not svc["installed"]:
        add("svc", "Protection service", "bad", "Not installed — your PC is not protected", "install", 4)
    elif not svc["active"]:
        add("svc", "Protection service", "bad", "Installed but stopped", "start", 4)
    else:
        add("svc", "Protection service", "ok", "Running and watching your PC", weight=4)
    if svc["installed"]:
        add("enabled", "Starts automatically at boot", "ok" if svc["enabled"] else "bad",
            "Yes" if svc["enabled"] else "Protection won't run after a reboot", None if svc["enabled"] else "install", 2)
        out = daemon_outdated()
        add("update", "Protection is up to date", "warn" if out else "ok",
            "A newer version is ready to install" if out else "Latest version installed",
            "install" if out else None, 1)
    hw = hw_watchdog()
    add("hw", "Hardware watchdog", "ok" if hw else "bad",
        f"Auto-reboot if the PC freezes completely ({hw})" if hw else "Off — a total freeze needs a manual reset",
        None if hw else "install", 3)
    panic = sysctl("kernel.panic")
    add("panic", "Auto-reboot after kernel crash", "ok" if panic not in ("", "0") else "warn",
        f"Reboots {panic} s after a kernel panic" if panic not in ("", "0") else "PC hangs forever on kernel panic",
        None if panic not in ("", "0") else "install", 2)
    sysrq = sysctl("kernel.sysrq")
    add("sysrq", "Emergency keys (SysRq)", "ok" if sysrq == "1" else "warn",
        "Enabled — see Guide for how to use them" if sysrq == "1" else "Only partially enabled",
        None if sysrq == "1" else "install", 1)

    group = "Resources"
    avail = m["MemAvailable"] / m["MemTotal"] * 100
    add("ram", "Free memory", "ok" if avail >= 20 else "warn" if avail >= 8 else "bad",
        f"{avail:.0f}% free ({fmt_bytes(m['MemAvailable'])} of {fmt_bytes(m['MemTotal'])})",
        "apps" if avail < 20 else None, 2)
    swap = m.get("SwapTotal", 0)
    add("swap", "Swap space", "ok" if swap >= 3.5 * 2**30 else "warn" if swap > 0 else "bad",
        f"{fmt_bytes(swap)} available as backup memory" if swap else "No swap — RAM spikes freeze the PC faster",
        None, 1)
    free_pct = (total - used) / total * 100
    add("disk", "Disk space", "ok" if free_pct >= 15 else "warn" if free_pct >= 5 else "bad",
        f"{fmt_bytes(total - used)} free ({free_pct:.0f}%)", "cleanup" if free_pct < 25 else None, 2)
    add("temp", "CPU temperature", "ok" if temp < 80 else "warn" if temp < 95 else "bad",
        f"{temp}°C" + ("" if temp < 80 else " — clean the fans / check cooling, heat causes crashes"), None, 2)

    group = "Stability"
    fu = failed_units()
    add("failed", "Failed services", "ok" if not fu else "warn",
        "None" if not fu else f"{len(fu)} failed: " + ", ".join(fu[:4]) + ("…" if len(fu) > 4 else ""),
        "show_failed" if fu else None, 1)
    ke = kernel_errors()
    add("kerr", "Kernel errors since boot", "ok" if len(ke) < 5 else "warn",
        f"{len(ke)} error message(s)" if ke else "None", "show_kerr" if ke else None, 1)
    boots = boot_history()
    crashes = sum(1 for b in boots if b["status"] == "crash")
    add("crash", "Crashes in recent boots", "ok" if crashes == 0 else "warn" if crashes < 3 else "bad",
        "No crashes or forced power-offs" if crashes == 0 else
        f"{crashes} of the last {len(boots) - 1} sessions ended without a clean shutdown", None, 2)
    return checks, boots, fu, ke


def health_score(checks):
    total = sum(c["weight"] for c in checks if c["status"] != "info")
    got = sum(c["weight"] * {"ok": 1, "warn": 0.5, "bad": 0}.get(c["status"], 1)
              for c in checks if c["status"] != "info")
    return round(100 * got / total) if total else 100


# ---------------------------------------------------------------- cleanup
CACHE_INFO = {
    "yay": ("AUR build files (yay)", "Leftover package builds. Safe to delete.", True),
    "paru": ("AUR build files (paru)", "Leftover package builds. Safe to delete.", True),
    "uv": ("Python packages (uv)", "Downloaded Python packages. Re-downloaded when needed.", True),
    "pip": ("Python packages (pip)", "Downloaded Python packages. Re-downloaded when needed.", True),
    "BraveSoftware": ("Brave browser cache", "Cached web pages. Close Brave first for best results.", False),
    "google-chrome": ("Chrome browser cache", "Cached web pages. Close Chrome first.", False),
    "mozilla": ("Firefox cache", "Cached web pages. Close Firefox first.", False),
    "puppeteer": ("Puppeteer browsers", "Test browsers, re-downloaded by tools when needed.", True),
    "ms-playwright": ("Playwright browsers", "Test browsers, re-downloaded by tools when needed.", True),
    "ms-playwright-go": ("Playwright browsers (Go)", "Test browsers, re-downloaded when needed.", True),
    "JetBrains": ("JetBrains IDE caches", "IDE indexes. Rebuilt on next start (slower first launch).", False),
    "go-build": ("Go build cache", "Compiled Go objects. Rebuilt automatically.", True),
    "pre-commit": ("pre-commit hooks", "Hook environments. Re-installed when needed.", True),
    "pnpm": ("pnpm cache", "Package metadata. Re-downloaded when needed.", True),
    "npm": ("npm cache", "Downloaded packages. Re-downloaded when needed.", True),
    "huggingface": ("Hugging Face models", "Downloaded AI models — large re-download if you need them again!", False),
    "thumbnails": ("Image thumbnails", "Preview images. Regenerated automatically.", True),
    "electron": ("Electron downloads", "Re-downloaded when needed.", True),
    "spotify": ("Spotify cache", "Cached music.", True),
}


def du_bytes(path):
    r = run(["du", "-sb", path], timeout=120)
    try:
        return int(r.stdout.split()[0])
    except (IndexError, ValueError):
        return 0


def cleanup_targets(min_size=100 * 2**20):
    items = []
    trash = os.path.join(HOME, ".local/share/Trash")
    if os.path.isdir(trash):
        items.append({"id": "trash", "group": "Your files", "title": "Trash",
                      "desc": "Files you deleted. Emptying is permanent!", "size": du_bytes(trash),
                      "safe": False, "root": False, "icon": "user-trash-full-symbolic"})
    cache = os.path.join(HOME, ".cache")
    try:
        names = os.listdir(cache)
    except OSError:
        names = []
    for name in names:
        p = os.path.join(cache, name)
        if not os.path.isdir(p) or os.path.islink(p):
            continue
        size = du_bytes(p)
        if size < min_size:
            continue
        title, desc, safe = CACHE_INFO.get(name, (name, f"App cache in ~/.cache/{name}. Usually safe; the app rebuilds it.", False))
        items.append({"id": "cache:" + name, "group": "App caches", "title": title, "desc": desc,
                      "size": size, "safe": safe, "root": False, "icon": "folder-symbolic"})
    items.append({"id": "pacman", "group": "System (needs password)", "title": "Old package versions (pacman)",
                  "desc": "Keeps the newest version of each package, removes the rest.",
                  "size": du_bytes("/var/cache/pacman/pkg"), "safe": True, "root": True,
                  "icon": "package-x-generic-symbolic"})
    r = run(["journalctl", "--disk-usage"])
    m = re.search(r"take up ([\d.]+)([KMGT])", r.stdout)
    jsize = int(float(m.group(1)) * 1024 ** "KMGT".index(m.group(2)) * 1024) if m else 0
    items.append({"id": "journal", "group": "System (needs password)", "title": "Old system logs",
                  "desc": "Shrinks the system journal to 200 MB.", "size": max(0, jsize - 200 * 2**20),
                  "safe": True, "root": True, "icon": "document-open-recent-symbolic"})
    order = {"Your files": 0, "App caches": 1, "System (needs password)": 2}
    items.sort(key=lambda i: (order[i["group"]], -i["size"]))
    return items


def empty_trash():
    # Done by hand (freedesktop trash spec) because `gio trash --empty` needs gvfs.
    trash = os.path.join(HOME, ".local/share/Trash")
    errors = []

    def retry_writable(func, path, exc):
        # Read-only dirs block deleting their contents: make the parent writable and retry once
        try:
            os.chmod(os.path.dirname(path), 0o700)
            if os.path.isdir(path) and not os.path.islink(path):
                os.chmod(path, 0o700)
                shutil.rmtree(path, onexc=lambda f, p, e: errors.append(p))
            else:
                func(path)
        except OSError:
            errors.append(path)

    for sub in ("files", "info", "expunged"):
        d = os.path.join(trash, sub)
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p, onexc=retry_writable)
            else:
                try:
                    os.unlink(p)
                except OSError:
                    errors.append(p)
    try:
        os.unlink(os.path.join(trash, "directorysizes"))
    except FileNotFoundError:
        pass
    return (not errors), (f"{len(errors)} files could not be removed" if errors else "")


def clean(item_id):
    if item_id == "trash":
        return empty_trash()
    if item_id == "pacman":
        return helper("clean-pacman")
    if item_id == "journal":
        return helper("clean-journal")
    if item_id.startswith("cache:"):
        name = item_id[6:]
        cache = os.path.realpath(os.path.join(HOME, ".cache"))
        p = os.path.realpath(os.path.join(cache, name))
        if os.path.dirname(p) != cache:
            return False, "refusing to delete outside ~/.cache"
        if name == "uv" and shutil.which("uv"):
            r = run(["uv", "cache", "clean"], timeout=600)
            if r.returncode == 0:
                return True, ""
        if name == "go-build" and shutil.which("go"):
            r = run(["go", "clean", "-cache"], timeout=600)
            if r.returncode == 0:
                return True, ""
        errors = []
        shutil.rmtree(p, onexc=lambda f, path, e: errors.append(path))
        return (not errors), (f"{len(errors)} files could not be removed" if errors else "")
    return False, "unknown item"


# ---------------------------------------------------------------- app details
CLK = os.sysconf("SC_CLK_TCK")
NCPU = os.cpu_count() or 1


def proc_stat(pid):
    """Fields from /proc/<pid>/stat (handles spaces in comm)."""
    raw = open(f"/proc/{pid}/stat").read()
    rest = raw[raw.rindex(")") + 2:].split()
    return {"state": rest[0], "ppid": int(rest[1]), "ticks": int(rest[11]) + int(rest[12]),
            "nice": int(rest[16]), "threads": int(rest[17]), "start": int(rest[19]) / CLK}


def proc_cmdline(pid):
    try:
        args = [a.decode(errors="replace") for a in open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0") if a]
    except OSError:
        return []
    # Chromium/Electron rewrite argv into one space-joined string
    if len(args) == 1 and " --" in args[0]:
        args = args[0].split(" ")
    return args


CHROMIUM_TYPES = {"renderer": "Tab / page", "gpu-process": "Graphics (GPU)", "zygote": "Helper (zygote)",
                  "broker": "Helper (broker)", "crashpad-handler": "Crash reporter", "ppapi": "Plugin"}
UTILITY_TYPES = {"network": "Network", "audio": "Audio", "storage": "Storage", "video_capture": "Camera",
                 "data_decoder": "Data decoder", "tracing": "Tracing"}


def proc_kind(cmd, is_main):
    t = next((a.split("=", 1)[1] for a in cmd if a.startswith("--type=")), None)
    if t is None:
        if cmd and "crashpad_handler" in os.path.basename(cmd[0]):
            return "Crash reporter"
        return "Main process" if is_main else "Child process"
    if t == "renderer" and any(a.startswith("--extension-process") for a in cmd):
        return "Extension"
    if t == "utility":
        sub = next((a.split("=", 1)[1] for a in cmd if a.startswith("--utility-sub-type=")), "")
        for k, v in UTILITY_TYPES.items():
            if k in sub.lower():
                return v
        return "Utility"
    return CHROMIUM_TYPES.get(t, t.replace("-", " ").capitalize())


def group_detail(name):
    """All processes named `name` with stats, ordered biggest first."""
    out = []
    for p in processes():
        if p["comm"] != name:
            continue
        try:
            st = proc_stat(p["pid"])
        except (OSError, ValueError, IndexError):
            continue
        out.append({**p, **st, "cmd": proc_cmdline(p["pid"])})
    pids = {p["pid"] for p in out}
    for p in out:
        p["is_main"] = p["ppid"] not in pids
        p["kind"] = proc_kind(p["cmd"], p["is_main"])
    out.sort(key=lambda p: -p["rss"])
    return out


def proc_extra(pid):
    d = f"/proc/{pid}"
    info = {}
    for key, fn in (("exe", lambda: os.readlink(f"{d}/exe")), ("cwd", lambda: os.readlink(f"{d}/cwd")),
                    ("files", lambda: str(len(os.listdir(f"{d}/fd"))))):
        try:
            info[key] = fn()
        except OSError:
            info[key] = "(needs admin rights)"
    try:
        io = dict(l.split(": ") for l in open(f"{d}/io").read().splitlines())
        info["read"] = int(io["read_bytes"])
        info["write"] = int(io["write_bytes"])
    except (OSError, KeyError, ValueError):
        pass
    return info


def user_name(uid):
    import pwd
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def signal_pids(pids, sig, mine=True):
    """Send TERM/KILL/STOP/CONT. Own processes directly, others via the admin helper."""
    if mine:
        return kill_pids(pids, getattr(signal, "SIG" + sig)) > 0, ""
    return helper("kill", sig, *map(str, pids))


def renice(pids, value):
    failed = []
    for pid in pids:
        try:
            os.setpriority(os.PRIO_PROCESS, pid, value)
        except OSError:
            failed.append(pid)
    if not failed:
        return True, ""
    return helper("renice", str(value), *map(str, failed))


# ---------------------------------------------------------------- Brave tabs bridge
import socket  # noqa: E402

BRAVE_DIR = os.path.join(APPDIR, "brave")
BRAVE_EXT_DIR = os.path.join(BRAVE_DIR, "extension")
BRAVE_HOST_NAME = "io.github.pcwatchdog.brave"
BRAVE_HOST_MANIFEST = os.path.join(HOME, ".config/BraveSoftware/Brave-Browser/NativeMessagingHosts",
                                   BRAVE_HOST_NAME + ".json")
BRAVE_SOCK = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{MY_UID}", "pcwatchdog-brave.sock")
BROWSER_APPS = ("brave",)


def brave_ext_id():
    try:
        return open(os.path.join(BRAVE_DIR, ".id")).read().strip()
    except OSError:
        return ""


def brave_install_host():
    """Register the native messaging host with Brave (user-level, no password needed)."""
    os.makedirs(os.path.dirname(BRAVE_HOST_MANIFEST), exist_ok=True)
    host = os.path.join(BRAVE_DIR, "host.py")
    os.chmod(host, 0o755)
    json.dump({"name": BRAVE_HOST_NAME, "description": "PC Watchdog tabs bridge", "path": host,
               "type": "stdio", "allowed_origins": [f"chrome-extension://{brave_ext_id()}/"]},
              open(BRAVE_HOST_MANIFEST, "w"), indent=2)
    return True


def brave_host_installed():
    return os.path.exists(BRAVE_HOST_MANIFEST)


def brave_call(cmd, timeout=6, **kw):
    """Send one command to the Brave extension. Returns the response dict, or raises OSError."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout + 1)
    try:
        s.connect(BRAVE_SOCK)
        s.sendall(json.dumps({"cmd": cmd, "timeout": timeout, **kw}).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    resp = json.loads(buf or b"{}")
    if "error" in resp:
        raise OSError(resp["error"])
    return resp


def brave_connected():
    try:
        return bool(brave_call("ping", timeout=2).get("ok"))
    except (OSError, ValueError):
        return False


def _origin(url):
    m = re.match(r"^([a-z]+://[^/]+)", url or "")
    return m.group(1) if m else url


def _ticks(pids):
    out = {}
    for pid in pids:
        try:
            out[pid] = proc_stat(pid)["ticks"]
        except (OSError, ValueError, IndexError):
            pass
    return out


class TabMapper:
    """Works out which Brave renderer process each tab lives in.

    The extension makes the tab busy-loop for a moment; the renderer whose CPU time jumps is the one."""
    BURN_MS = 250

    def __init__(self):
        self.map = {}       # tab id -> (origin, pid)
        self.failed = {}    # tab id -> (origin, tries)

    def renderers(self, name="brave"):
        return [p["pid"] for p in group_detail(name) if p["kind"] in ("Tab / page", "Extension")]

    def pid_for(self, tab):
        m = self.map.get(tab["id"])
        if m and m[0] == _origin(tab["url"]) and alive([m[1]]):
            return m[1]
        return None

    def needs_probe(self, tab):
        if tab["discarded"] or not tab["url"].startswith(("http://", "https://", "file://")):
            return False
        if self.pid_for(tab):
            return False
        f = self.failed.get(tab["id"])
        return not (f and f[0] == _origin(tab["url"]) and f[1] >= 2)

    def probe(self, tab, name="brave"):
        pids = self.renderers(name)
        before = _ticks(pids)
        try:
            brave_call("burn", timeout=4, tabId=tab["id"], ms=self.BURN_MS)
        except OSError:
            self._fail(tab)
            return None
        after = _ticks(pids)
        deltas = sorted(((after.get(p, 0) - before.get(p, 0), p) for p in before), reverse=True)
        need = self.BURN_MS / 1000 * CLK * 0.6
        if deltas and deltas[0][0] >= need and (len(deltas) == 1 or deltas[0][0] - deltas[1][0] >= need * 0.5):
            pid = deltas[0][1]
            self.map[tab["id"]] = (_origin(tab["url"]), pid)
            self.failed.pop(tab["id"], None)
            return pid
        self._fail(tab)
        return None

    def _fail(self, tab):
        o = _origin(tab["url"])
        f = self.failed.get(tab["id"])
        self.failed[tab["id"]] = (o, (f[1] + 1) if f and f[0] == o else 1)
