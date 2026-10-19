#!/usr/bin/env bash
# Install pc-watchdog. Run: sudo ./install.sh
set -euo pipefail
[ "$EUID" -eq 0 ] || { echo "Run with sudo"; exit 1; }
cd "$(dirname "$0")"

echo "==> Installing monitor script and service"
install -m 755 pc-watchdog.sh /usr/local/bin/pc-watchdog.sh
install -m 644 pc-watchdog.service /etc/systemd/system/pc-watchdog.service
[ -e /etc/pc-watchdog.conf ] || install -m 644 pc-watchdog.conf /etc/pc-watchdog.conf

echo "==> Installing app helper and permissions"
install -D -m 755 pcw-helper /usr/local/lib/pc-watchdog/pcw-helper
install -D -m 644 io.github.pcwatchdog.policy /usr/share/polkit-1/actions/io.github.pcwatchdog.policy
install -D -m 644 50-pc-watchdog.rules /etc/polkit-1/rules.d/50-pc-watchdog.rules

echo "==> Enabling hardware watchdog (auto-reboot if the kernel hangs)"
mkdir -p /etc/systemd/system.conf.d
cat > /etc/systemd/system.conf.d/10-hw-watchdog.conf <<'EOF'
[Manager]
# systemd pings the hardware watchdog (iTCO_wdt); if the system freezes
# completely for 30 s the board resets itself.
RuntimeWatchdogSec=30s
# If a reboot/shutdown hangs, force it after 2 min.
RebootWatchdogSec=2min
EOF

echo "==> Kernel crash/hang recovery settings"
cat > /etc/sysctl.d/90-pc-watchdog.conf <<'EOF'
# Reboot 10 s after a kernel panic instead of hanging forever
kernel.panic = 10
# Treat kernel oops as panic -> clean reboot instead of a half-broken system
kernel.panic_on_oops = 1
# Enable all Magic SysRq keys for manual recovery
# (Alt+SysRq+F = kill memory hog, Alt+SysRq+R,E,I,S,U,B = safe reboot)
kernel.sysrq = 1
# Keep some RAM free so the system stays responsive under memory pressure
vm.min_free_kbytes = 131072
EOF
sysctl --system >/dev/null

systemctl daemon-reload
systemctl daemon-reexec          # applies the RuntimeWatchdogSec change now
systemctl enable pc-watchdog.service
systemctl restart pc-watchdog.service

echo
echo "Done."
systemctl --no-pager --lines=3 status pc-watchdog.service || true
echo
echo "Hardware watchdog: $(systemctl show -p RuntimeWatchdogUSec --value)"
echo "Logs: journalctl -u pc-watchdog -f"
