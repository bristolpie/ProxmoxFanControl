#!/usr/bin/env bash
# install.sh — install / upgrade / uninstall proxmox-fancontrol on a Proxmox (Debian) host.
#
#   sudo ./install.sh                 install or upgrade (keeps existing config)
#   sudo ./install.sh --port 8090     set web UI port on first install
#   sudo ./install.sh --yes           don't ask questions
#   sudo ./install.sh --uninstall     remove service + program files (keeps config)
#   sudo ./install.sh --uninstall --purge   also remove config + history
set -euo pipefail

APP=proxmox-fancontrol
PREFIX=/opt/$APP
CONF_DIR=/etc/$APP
CONF=$CONF_DIR/config.json
STATE_DIR=/var/lib/$APP
UNIT=/etc/systemd/system/$APP.service

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
YES=0; UNINSTALL=0; PURGE=0; WEB_PORT=8090

while (( $# )); do
    case "$1" in
        -y|--yes)     YES=1 ;;
        --uninstall)  UNINSTALL=1 ;;
        --purge)      PURGE=1 ;;
        --port)       WEB_PORT="$2"; shift ;;
        -h|--help)    sed -n '2,9p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done

c_ok()   { printf '\e[32m✔\e[0m %s\n' "$*"; }
c_info() { printf '\e[34m•\e[0m %s\n' "$*"; }
c_warn() { printf '\e[33m!\e[0m %s\n' "$*"; }
die()    { printf '\e[31m✘\e[0m %s\n' "$*" >&2; exit 1; }
ask() {  # ask "question" → 0 for yes
    (( YES )) && return 0
    local a; read -r -p "$1 [Y/n] " a || true
    [[ -z "$a" || "$a" =~ ^[Yy] ]]
}

[[ $EUID -eq 0 ]] || die "run as root (sudo ./install.sh)"
command -v systemctl >/dev/null || die "systemd is required"

# ── Uninstall ─────────────────────────────────────────────────────
if (( UNINSTALL )); then
    systemctl disable --now "$APP.service" 2>/dev/null || true
    rm -f "$UNIT"
    systemctl daemon-reload
    rm -rf "$PREFIX"
    c_ok "removed service and $PREFIX"
    if (( PURGE )); then
        rm -rf "$CONF_DIR" "$STATE_DIR"
        c_ok "removed $CONF_DIR and $STATE_DIR"
    else
        c_info "config kept in $CONF_DIR (use --purge to remove)"
    fi
    c_warn "fan control is no longer running — fans stay at their last PWM"
    exit 0
fi

# ── Pre-flight ────────────────────────────────────────────────────
[[ -f "$SRC/fancontrold/fancontrold.py" ]] || die "run this from the repository folder"

command -v python3 >/dev/null || die "python3 not found (apt install python3)"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 7) else 1)' \
    || die "python 3.7+ required, found $(python3 -V)"
c_ok "$(python3 -V)"

if ! command -v smartctl >/dev/null; then
    if ask "smartmontools (smartctl) is missing — needed for disk temps. Install it?"; then
        apt-get update -qq && apt-get install -y -qq smartmontools
    else
        c_warn "continuing without smartctl: disk temps will be unavailable"
    fi
fi
command -v zpool >/dev/null || c_warn "zpool not found: scrub profile detection disabled"

ls /sys/class/hwmon/*/name >/dev/null 2>&1 && \
    grep -qE 'coretemp|k10temp|zenpower' /sys/class/hwmon/*/name 2>/dev/null \
    && c_ok "CPU sensor: $(grep -hE 'coretemp|k10temp|zenpower' /sys/class/hwmon/*/name | head -1)" \
    || c_warn "no coretemp/k10temp hwmon found — run 'sensors-detect' or set sensors.cpu in $CONF"

# ── Serial port ───────────────────────────────────────────────────
PORT=""
if [[ -d /dev/serial/by-id ]]; then
    PORT=$(ls -1 /dev/serial/by-id/* 2>/dev/null | head -1 || true)
fi
[[ -z "$PORT" && -e /dev/ttyUSB0 ]] && PORT=/dev/ttyUSB0
[[ -z "$PORT" && -e /dev/ttyACM0 ]] && PORT=/dev/ttyACM0
if [[ -n "$PORT" ]]; then
    c_ok "Arduino serial port: $PORT"
else
    c_warn "no USB serial device found — the service will keep retrying (serial.port = auto)"
fi

# ── Anything else holding the serial port? (only one program can own it) ──
if [[ -n "$PORT" ]]; then
    if command -v fuser >/dev/null; then
        # Re-scan after each stop: stopping a service also ends its child
        # processes, which may have appeared in an earlier scan.
        for _attempt in 1 2 3 4 5; do
            holder=""
            for pid in $(fuser "$(readlink -f "$PORT")" 2>/dev/null); do
                [[ -d /proc/$pid ]] || continue                  # already exited
                unit=$(ps -o unit= -p "$pid" 2>/dev/null | tr -d ' ' || true)
                [[ "$unit" == "$APP.service" ]] && continue      # upgrading: our own service
                holder=$pid; break
            done
            [[ -z "$holder" ]] && break
            name=$(ps -o comm= -p "$holder" 2>/dev/null || true)
            c_warn "$PORT is in use by PID $holder (${name:-exited})${unit:+, systemd unit $unit}"
            if [[ "$unit" == *.service ]] && ask "Stop and disable $unit so $APP can use the Arduino?"; then
                systemctl disable --now "$unit" || true
                c_ok "$unit stopped and disabled (its files are left in place)"
                sleep 1
            elif [[ ! -d /proc/$holder ]]; then
                continue                                         # it went away on its own
            else
                die "free the serial port first (stop PID $holder), then re-run"
            fi
        done
    else
        c_warn "fuser not found (apt install psmisc) — can't check whether another program holds $PORT"
    fi
fi

# ── Files ─────────────────────────────────────────────────────────
install -d -m 755 "$PREFIX" "$PREFIX/web" "$CONF_DIR" "$STATE_DIR"
install -m 755 "$SRC/fancontrold/fancontrold.py" "$PREFIX/fancontrold.py"
install -m 644 "$SRC/fancontrold/web/index.html" "$PREFIX/web/index.html"
c_ok "program installed to $PREFIX"

if [[ -f "$CONF" ]]; then
    c_ok "keeping existing config $CONF"
else
    python3 "$PREFIX/fancontrold.py" --print-default-config | \
    PORT="$PORT" WEB_PORT="$WEB_PORT" python3 -c '
import json, os, sys
c = json.load(sys.stdin)
if os.environ["PORT"]:
    c["serial"]["port"] = os.environ["PORT"]
c["web"]["port"] = int(os.environ["WEB_PORT"])
json.dump(c, sys.stdout, indent=2); print()
' > "$CONF"
    chmod 600 "$CONF"
    c_ok "created $CONF with default curves"
fi

install -m 644 "$SRC/systemd/$APP.service" "$UNIT"
systemctl daemon-reload
systemctl enable "$APP.service" >/dev/null 2>&1
systemctl restart "$APP.service"
c_ok "service $APP enabled and started"

# ── Verify ────────────────────────────────────────────────────────
port=$(python3 -c "import json; print(json.load(open('$CONF'))['web']['port'])")
for _ in $(seq 1 15); do
    if python3 - "$port" <<'PY' 2>/dev/null
import json, sys, urllib.request
s = json.load(urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/api/status", timeout=2))
if not s.get("zones"):
    sys.exit(1)
a = s["arduino"]
print(f"  arduino: {'connected v%s' % a['firmware'] if a['connected'] else 'NOT connected (' + str(a['error']) + ')'}")
print(f"  cpu {s['temps']['cpu']['raw']}°C  disk {s['temps']['disk']['raw']}°C  profile {s['profile']['active']}")
print("  " + "  ".join(f"Z{z['id']} {z['name']}={z['percent']}%" for z in s["zones"]))
PY
    then ok=1; break; fi
    sleep 1
done
if [[ "${ok:-0}" == 1 ]]; then
    ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    c_ok "web UI: http://${ip:-<host-ip>}:$port/"
else
    c_warn "service didn't answer yet — check: journalctl -u $APP -e"
fi

# ── Firmware check ────────────────────────────────────────────────
# The firmware answers "V" with "VER n"; compare with the version in this checkout.
want=$(grep -o 'println("VER [0-9]*")' "$SRC/arduino/fan_zone_control/fan_zone_control.ino" | grep -o '[0-9]*' | head -1)
if [[ "${ok:-0}" == 1 && -n "$want" ]]; then
    # "<firmware> <port>"; firmware is 0 for old firmware without the V command, "offline" if not connected
    read -r have fw_port < <(python3 - "$port" <<'PY' 2>/dev/null || echo "offline -"
import json, sys, time, urllib.request
for _ in range(15):          # the daemon may still be opening the port
    a = json.load(urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/api/status", timeout=2))["arduino"]
    if a["connected"]:
        print(a["firmware"] or 0, a["port"] or "-")
        break
    time.sleep(1)
else:
    print("offline -")
PY
)
    if [[ "$have" == offline ]]; then
        c_warn "Arduino not connected, so the firmware wasn't checked. Flash it later with: ./flash.sh --release"
    elif (( have == want )); then
        c_ok "Arduino firmware v$have is up to date"
    elif (( have > want )); then
        c_warn "Arduino firmware v$have is newer than this checkout (v$want); leaving it alone"
    else
        c_warn "Arduino firmware is v$have; this release has v$want"
        if (( YES )); then
            c_info "not flashing unattended; run: ./flash.sh --release --port $fw_port"
        elif ask "Flash the Arduino now? (fans run at a fixed speed for a few seconds)"; then
            if ! "$SRC/flash.sh" --release --port "$fw_port"; then
                ask "Download failed. Build the firmware from source instead? (installs arduino-cli)" \
                    && "$SRC/flash.sh" --build --port "$fw_port" \
                    || c_warn "firmware not updated; try later with ./flash.sh"
            fi
        else
            c_info "skipped; flash later with: ./flash.sh --release"
        fi
    fi
fi

cat <<EOF

  Logs:      journalctl -u $APP -f
  Config:    $CONF   (or edit in the web UI → Settings)
  Restart:   systemctl restart $APP
  Uninstall: ./install.sh --uninstall
EOF
