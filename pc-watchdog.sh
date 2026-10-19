#!/usr/bin/env bash
# pc-watchdog: keeps the desktop from freezing/crashing.
#  - Kills the biggest non-critical process before RAM+swap run out (prevents hard freeze)
#  - Warns on high CPU temperature and low disk space
#  - Pings systemd's watchdog so systemd restarts us if we hang
# Runs as root via pc-watchdog.service. Config: /etc/pc-watchdog.conf

set -u

# ---- defaults (override in /etc/pc-watchdog.conf) ----
INTERVAL=2                 # seconds between checks
MEM_MIN_PCT=5              # act when MemAvailable below this % of RAM ...
SWAP_MIN_PCT=10            # ... AND free swap below this % of swap
PSI_FULL_MAX=40            # or when memory PSI "full avg10" exceeds this (system thrashing)
PSI_STRIKES=3              # ... for this many consecutive checks
KILL_COOLDOWN=10           # seconds to wait after a kill before killing again
TEMP_WARN=90               # °C: desktop notification
TEMP_CRIT=97               # °C: critical notification
DISK_MIN_PCT=5             # % free on / before warning
NOTIFY_COOLDOWN=300        # seconds between repeated warnings of same kind
# Apps closed first when memory runs out (process names, space separated)
PREFER_APPS="brave chrome chromium firefox electron code java node slack discord"
# Extra apps that must never be closed (process names, space separated)
EXTRA_PROTECT=""
NOTIFY_USER=""             # auto-detected if empty
# -------------------------------------------------------

[ -r /etc/pc-watchdog.conf ] && . /etc/pc-watchdog.conf

# Core system processes are always protected (regex on process name)
BASE_PROTECT='systemd.*|init|Hyprland|Xwayland|sddm|gdm|greetd|login|sshd|dbus.*|pipewire.*|wireplumber|NetworkManager|wpa_supplicant|iwd|kwin.*|gnome-shell|plasmashell|Xorg|agetty|journald|udevd|pc-watchdog.*|pcwatchdog.*|bash|zsh|fish|sudo|polkit.*|qs|quickshell'
names_to_re() { local n out=""; for n in $1; do n=${n//[^A-Za-z0-9_+-]/.}; out+="|$n"; done; echo "${out#|}"; }
PROTECT="^(${BASE_PROTECT}${EXTRA_PROTECT:+|$(names_to_re "$EXTRA_PROTECT")})$"
PREFER="^($(names_to_re "$PREFER_APPS"))$"
[ "$PREFER" = "^()$" ] && PREFER='^$'

log() { echo "$*"; }   # stdout goes to the journal: journalctl -u pc-watchdog

declare -A last_notify
notify() {  # notify <key> <urgency> <title> <body>
    local key=$1 urg=$2 title=$3 body=$4 now
    now=$(date +%s)
    (( now - ${last_notify[$key]:-0} < NOTIFY_COOLDOWN )) && return
    last_notify[$key]=$now
    log "NOTIFY [$urg] $title: $body"
    local user=$NOTIFY_USER uid
    [ -z "$user" ] && user=$(loginctl list-sessions --no-legend 2>/dev/null | awk '$3!="root"{print $3; exit}')
    [ -z "$user" ] && return
    uid=$(id -u "$user" 2>/dev/null) || return
    [ -S "/run/user/$uid/bus" ] || return
    timeout 5 runuser -u "$user" -- env DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
        notify-send -u "$urg" -a pc-watchdog "$title" "$body" >/dev/null 2>&1 &
}

sd_ping() { [ -n "${NOTIFY_SOCKET:-}" ] && systemd-notify --pid=$$ "$@" 2>/dev/null; }

meminfo() {  # sets MEM_TOTAL MEM_AVAIL SWAP_TOTAL SWAP_FREE (kB)
    while read -r k v _; do
        case $k in
            MemTotal:) MEM_TOTAL=$v ;;
            MemAvailable:) MEM_AVAIL=$v ;;
            SwapTotal:) SWAP_TOTAL=$v ;;
            SwapFree:) SWAP_FREE=$v ;;
        esac
    done < /proc/meminfo
}

psi_full() {  # integer part of memory "full avg10"
    awk '/^full/{split($2,a,"="); printf "%d", a[2]}' /proc/pressure/memory 2>/dev/null || echo 0
}

pick_victim() {  # prints "pid name rss_kB" of process to kill
    local best="" best_score=-1 pid name score rss
    for d in /proc/[0-9]*; do
        pid=${d#/proc/}
        [ "$pid" = $$ ] && continue
        name=$(cat "$d/comm" 2>/dev/null) || continue
        [[ $name =~ $PROTECT ]] && continue
        [ "$(cat "$d/oom_score_adj" 2>/dev/null)" = "-1000" ] && continue
        score=$(cat "$d/oom_score" 2>/dev/null) || continue
        [ "${score:-0}" -le 0 ] && continue
        [[ $name =~ $PREFER ]] && score=$(( score * 3 ))   # favour apps that are safe to restart
        if (( score > best_score )); then
            rss=$(awk '/^VmRSS:/{print $2}' "$d/status" 2>/dev/null)
            best_score=$score; best="$pid $name ${rss:-0}"
        fi
    done
    echo "$best"
}

KILL_DIR=/run/pc-watchdog/kills   # one file per killed pid, readable by everyone

tell_victim() {  # tell_victim <pid> <name> <rss_kB> <reason>: explain the kill to the process itself
    local pid=$1 name=$2 rss=$3 reason=$4 msg fd target
    msg="[pc-watchdog] $(date '+%F %T') killing $name (pid $pid, $((rss/1024)) MiB): system out of memory ($reason). This was not a crash; the process was stopped to keep the PC from freezing. Use less memory or retry later. Details: $KILL_DIR/$pid"
    mkdir -p "$KILL_DIR" && chmod 755 /run/pc-watchdog "$KILL_DIR"
    printf '%s\n' "$msg" > "$KILL_DIR/$pid"
    printf '%s\n' "$msg" >> /run/pc-watchdog/kills.log
    # Write to the process's stderr (else stdout) so whoever reads its output, e.g. the agent
    # that launched it, sees why it stopped. Skip /dev/null; timeout guards against a full pipe.
    for fd in 2 1; do
        target=$(readlink "/proc/$pid/fd/$fd" 2>/dev/null) || continue
        [ "$target" = /dev/null ] && continue
        timeout 1 bash -c 'printf "\n%s\n" "$1" >> "$2"' _ "$msg" "/proc/$pid/fd/$fd" 2>/dev/null && break
    done
}

kill_victim() {
    local reason=$1 v pid name rss
    v=$(pick_victim)
    [ -z "$v" ] && { log "low memory ($reason) but no killable process found"; return; }
    read -r pid name rss <<<"$v"
    log "LOW MEMORY ($reason): killing $name (pid $pid, $((rss/1024)) MiB)"
    tell_victim "$pid" "$name" "$rss" "$reason"
    kill -TERM "$pid" 2>/dev/null
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        sleep 0.5; kill -0 "$pid" 2>/dev/null || break
    done
    kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null
    last_notify[mem]=0
    notify mem critical "Low memory - killed $name" "Freed ~$((rss/1024)) MiB to prevent a system freeze ($reason)."
}

cpu_temp() {  # max °C across coretemp/k10temp, else thermal zones
    local max=0 t f
    for f in /sys/class/hwmon/hwmon*/temp*_input; do
        case $(cat "${f%/*}/name" 2>/dev/null) in coretemp|k10temp|zenpower) ;; *) continue ;; esac
        t=$(( $(cat "$f" 2>/dev/null || echo 0) / 1000 )); (( t > max )) && max=$t
    done
    if (( max == 0 )); then
        for f in /sys/class/thermal/thermal_zone*/temp; do
            t=$(( $(cat "$f" 2>/dev/null || echo 0) / 1000 )); (( t > max )) && max=$t
        done
    fi
    echo $max
}

log "started (interval ${INTERVAL}s, mem<${MEM_MIN_PCT}% & swap<${SWAP_MIN_PCT}%, psi>${PSI_FULL_MAX})"
sd_ping --ready
strikes=0 last_kill=0 tick=0

while :; do
    sd_ping WATCHDOG=1
    now=$(date +%s)

    # ---- memory ----
    meminfo
    mem_pct=$(( MEM_AVAIL * 100 / MEM_TOTAL ))
    if (( SWAP_TOTAL > 0 )); then swap_pct=$(( SWAP_FREE * 100 / SWAP_TOTAL )); else swap_pct=0; fi
    psi=$(psi_full)
    if (( psi > PSI_FULL_MAX )); then strikes=$((strikes+1)); else strikes=0; fi

    if (( now - last_kill >= KILL_COOLDOWN )); then
        if (( mem_pct < MEM_MIN_PCT && swap_pct < SWAP_MIN_PCT )); then
            kill_victim "mem ${mem_pct}% swap ${swap_pct}% free"; last_kill=$(date +%s); strikes=0
        elif (( strikes >= PSI_STRIKES )); then
            kill_victim "thrashing, psi full=${psi}%"; last_kill=$(date +%s); strikes=0
        elif (( mem_pct < MEM_MIN_PCT * 2 && swap_pct < 50 )); then
            notify memwarn normal "Memory getting low" "RAM ${mem_pct}% free, swap ${swap_pct}% free. Close some apps."
        fi
    fi

    # ---- slower checks every ~10 s ----
    if (( tick % 5 == 0 )); then
        t=$(cpu_temp)
        if (( t >= TEMP_CRIT )); then
            notify temp critical "CPU temperature critical: ${t}°C" "Close heavy apps and check cooling/fans."
        elif (( t >= TEMP_WARN )); then
            notify temp normal "CPU running hot: ${t}°C" "Sustained high temperature."
        fi

        disk_free=$(df --output=pcent / | tail -1 | tr -dc '0-9'); disk_free=$((100 - disk_free))
        (( disk_free < DISK_MIN_PCT )) && \
            notify disk critical "Disk almost full" "Only ${disk_free}% free on /. A full disk can break the system."
    fi

    tick=$((tick+1))
    sleep "$INTERVAL"
done
