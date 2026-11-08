#!/usr/bin/env bash
# flash.sh — flash the Arduino firmware directly from the Proxmox host over USB.
#
#   sudo ./flash.sh --release           download the prebuilt .hex from the latest GitHub release and flash it
#   sudo ./flash.sh --hex file.hex      flash a .hex you already have
#   sudo ./flash.sh --build             compile from source with arduino-cli, then flash
#
# Options:
#   --port /dev/serial/by-id/...        serial port (default: auto-detect)
#   --repo owner/name                   GitHub repo for --release (default: this clone's origin)
#   --yes                               don't ask before installing avrdude / arduino-cli
#
# The fan control service is stopped while flashing and started again afterwards.
# Fans briefly drop to the firmware default (PWM 50) while the Arduino reboots.
set -euo pipefail

APP=proxmox-fancontrol
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKETCH="$SRC/arduino/fan_zone_control"
MODE=""; HEX=""; PORT=""; REPO=""; YES=0

while (( $# )); do
    case "$1" in
        --release) MODE=release ;;
        --hex)     MODE=hex; HEX="$2"; shift ;;
        --build)   MODE=build ;;
        --port)    PORT="$2"; shift ;;
        --repo)    REPO="$2"; shift ;;
        -y|--yes)  YES=1 ;;
        -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done

c_ok()   { printf '\e[32m✔\e[0m %s\n' "$*"; }
c_info() { printf '\e[34m•\e[0m %s\n' "$*"; }
c_warn() { printf '\e[33m!\e[0m %s\n' "$*"; }
die()    { printf '\e[31m✘\e[0m %s\n' "$*" >&2; exit 1; }
ask()    { (( YES )) && return 0; local a; read -r -p "$1 [Y/n] " a || true; [[ -z "$a" || "$a" =~ ^[Yy] ]]; }

[[ $EUID -eq 0 ]] || die "run as root (sudo ./flash.sh ...)"
[[ -n "$MODE" ]] || { sed -n '2,15p' "$0"; exit 1; }

# ── Port ──────────────────────────────────────────────────────────
if [[ -z "$PORT" ]]; then
    PORT=$(ls -1 /dev/serial/by-id/* 2>/dev/null | head -1 || true)
    [[ -z "$PORT" && -e /dev/ttyUSB0 ]] && PORT=/dev/ttyUSB0
    [[ -z "$PORT" && -e /dev/ttyACM0 ]] && PORT=/dev/ttyACM0
fi
[[ -n "$PORT" && -e "$PORT" ]] || die "no Arduino serial port found (use --port)"
c_ok "Arduino on $PORT"

# ── Get a .hex ────────────────────────────────────────────────────
WORK=$(mktemp -d)
cleanup_work() { rm -rf -- "$WORK"; }
trap cleanup_work EXIT

need_avrdude() {
    command -v avrdude >/dev/null && return
    ask "avrdude is not installed. Install it with apt?" || die "avrdude is required"
    apt-get update -qq && apt-get install -y -qq avrdude
}

case "$MODE" in
    release)
        if [[ -z "$REPO" ]]; then
            url=$(git -C "$SRC" remote get-url origin 2>/dev/null || true)
            REPO=$(sed -E 's#(git@github\.com:|https://github\.com/)##; s#\.git$##' <<<"$url")
        fi
        [[ "$REPO" =~ ^[^/]+/[^/]+$ ]] || die "can't work out the GitHub repo — pass --repo owner/name"
        dl="https://github.com/$REPO/releases/latest/download/fan_zone_control.hex"
        c_info "downloading $dl"
        curl -fsSL -o "$WORK/fan_zone_control.hex" "$dl" || die "download failed (is there a release with fan_zone_control.hex?)"
        HEX="$WORK/fan_zone_control.hex"
        need_avrdude ;;
    hex)
        [[ -f "$HEX" ]] || die "hex file not found: $HEX"
        need_avrdude ;;
    build)
        if ! command -v arduino-cli >/dev/null; then
            ask "arduino-cli is not installed. Download it to /usr/local/bin (official installer, ~30 MB)?" \
                || die "arduino-cli is required for --build (or use --release / --hex)"
            curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | BINDIR=/usr/local/bin sh
        fi
        if ! arduino-cli core list 2>/dev/null | grep -q '^arduino:avr'; then
            c_info "installing the Arduino AVR core (one-off, ~150 MB in ~/.arduino15)"
            arduino-cli core update-index
            arduino-cli core install arduino:avr
        fi
        c_info "compiling $SKETCH"
        arduino-cli compile --fqbn arduino:avr:nano --output-dir "$WORK" "$SKETCH"
        HEX="$WORK/fan_zone_control.ino.hex"
        command -v avrdude >/dev/null || need_avrdude ;;
esac
c_ok "firmware: $HEX ($(stat -c %s "$HEX") bytes)"

# ── Stop the service (it holds the port), flash, restart ─────────
WAS_ACTIVE=0
if systemctl is-active --quiet "$APP"; then
    WAS_ACTIVE=1
    systemctl stop "$APP"
    c_info "stopped $APP (fans hold their current speed)"
fi
restart() {
    cleanup_work
    if (( WAS_ACTIVE )); then
        systemctl start "$APP" && c_ok "started $APP again"
    fi
}
trap restart EXIT

# Nanos ship with either the new (115200 baud) or old (57600 baud) bootloader — try both.
flashed=0
for baud in 115200 57600; do
    c_info "flashing at $baud baud…"
    if avrdude -q -p atmega328p -c arduino -P "$PORT" -b "$baud" -D -U "flash:w:$HEX:i"; then
        flashed=1
        c_ok "flashed successfully ($( (( baud == 57600 )) && echo "old" || echo "new") bootloader)"
        break
    fi
done
(( flashed )) || die "avrdude could not talk to the bootloader at 115200 or 57600 baud"

# ── Verify the firmware answers ──────────────────────────────────
sleep 2
stty -F "$PORT" 9600 cs8 -cstopb -parenb raw -echo -hupcl
exec 3<>"$PORT"
sleep 2                                          # opening the port resets the Nano
timeout 3 cat <&3 >/dev/null || true             # discard "READY"
printf 'V\n' >&3
reply=""; read -r -t 2 reply <&3 || true
exec 3>&-
reply=${reply//$'\r'/}
if [[ "$reply" == VER* ]]; then
    c_ok "Arduino replied: $reply"
else
    c_warn "unexpected reply to V: '${reply}' — check the service log after it starts"
fi
