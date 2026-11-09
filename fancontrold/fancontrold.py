#!/usr/bin/env python3
"""
fancontrold — Proxmox / ZFS fan controller with a built-in web UI.

Drives a 3-zone Arduino PWM controller over USB serial using temperature
curves with hysteresis, cross-zone floors, rolling averages and an
automatic ZFS scrub profile.  Serves a web dashboard + JSON API for live
monitoring, curve editing and manual overrides.

Pure Python 3 standard library — no pip packages needed.

    fancontrold.py                       # run (reads /etc/proxmox-fancontrol/config.json)
    fancontrold.py --simulate            # fake sensors + fake Arduino, for UI development
    fancontrold.py --print-default-config
"""

import argparse
import copy
import hmac
import json
import logging
import math
import os
import random
import re
import select
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION = "1.3.0"
log = logging.getLogger("fancontrold")

PROFILES = ("normal", "scrub")

# Built-in sensor ids (see Controller._read_sensors for the full list).
#   cpu         CPU package temp (coretemp / k10temp, or sensors.cpu path)
#   disks_max   hottest SATA/SAS disk (smartctl), minus sensors.disk_exclude
#   nvme_max    hottest NVMe drive (hwmon)
#   pool:<name> hottest disk in that ZFS pool
#   hwmon:<driver>:<device>:<label>   any kernel hwmon temperature
#   disk:<serial>                     one disk

DEFAULT_CONFIG = {
    "serial": {
        "port": "auto",            # "auto" = first /dev/serial/by-id/*, else /dev/ttyUSB0
        "baud": 9600,
        "watchdog_seconds": 30,    # Arduino goes to failsafe if the host goes silent (0 = off)
    },
    "interval": 5,                 # seconds per control cycle
    "temp_avg_samples": 3,         # rolling average depth
    "hysteresis": 3,               # °C below peak before ramping down
    "scrub_cooldown_cycles": 3,    # cycles to stay in scrub profile after scrub ends
    "safe_exit_pwm": 100,          # sent on clean shutdown
    "sensor_fail_pwm": 200,        # used when a zone's sensor can't be read
    "emergency": {                 # raw temp at/above which ALL zones go to pwm (beats manual)
        "cpu": 90,                 # checked against the "cpu" sensor
        "disk": 60,                # checked against "disks_max"
        "pwm": 255,
    },
    "sensors": {
        "cpu": "auto",             # "auto" (hwmon coretemp/k10temp) or path to a temp*_input file
        "disks": "auto",           # "auto" or list like ["/dev/sda", "/dev/sdb"]
        "disk_exclude": [],        # e.g. ["sdd"] — still shown, left out of disks_max / pool sensors
        "skip_standby": False,     # True = don't wake spun-down disks to read temp
        "detect_resilver": True,   # treat resilver like a scrub
    },
    "profile": {
        "mode": "auto",            # auto | normal | scrub
    },
    # Named curves. Each reads one sensor. "scrub" points may be null = same as normal.
    "curves": [
        {"name": "CPU", "sensor": "cpu", "points": {
            "normal": [[30, 0], [40, 70], [50, 100], [60, 150], [70, 200], [75, 220]],
            "scrub":  [[30, 80], [40, 110], [50, 140], [60, 170], [70, 210], [75, 220]],
        }},
        {"name": "Disks", "sensor": "disks_max", "points": {
            # never fully off: a drive stack needs some airflow; scrub starts higher to get ahead of the heat
            "normal": [[25, 30], [30, 45], [35, 75], [40, 120], [44, 170], [48, 220], [52, 255]],
            "scrub":  [[25, 100], [30, 100], [35, 130], [40, 160], [44, 200], [48, 255]],
        }},
    ],
    # Each zone follows a curve. Optional floor: if <sensor> is above <above>, PWM never drops below <pwm>.
    "zones": [
        {"id": 1, "name": "Front", "curve": "Disks", "floor": {"sensor": "cpu",       "above": 60, "pwm": 100}},
        {"id": 2, "name": "Top",   "curve": "CPU",   "floor": {"sensor": "disks_max", "above": 45, "pwm": 90}},
        {"id": 3, "name": "Rear",  "curve": "CPU",   "floor": {"sensor": "disks_max", "above": 45, "pwm": 90}},
    ],
    "web": {
        "bind": "0.0.0.0",
        "port": 8090,
        "token": "",               # if set, required for any change (header X-Auth-Token)
    },
    "history": {
        "hours": 24,
        "file": "/var/lib/proxmox-fancontrol/history.json",
    },
}


# ════════════════════════════════════════════════════════════════════
# Config
# ════════════════════════════════════════════════════════════════════

def migrate_config(user):
    """Upgrade an older config (fixed cpu/disk curves) to named curves. Returns True if changed."""
    changed = False
    old = user.get("curves")
    if isinstance(old, dict):
        names = {"cpu": ("CPU", "cpu"), "disk": ("Disks", "disks_max")}
        user["curves"] = [
            {"name": name, "sensor": sensor,
             "points": {p: old[p][src] for p in PROFILES if p in old and src in old[p]}}
            for src, (name, sensor) in names.items()
        ]
        changed = True
    to_sensor = {"cpu": "cpu", "disk": "disks_max"}
    to_curve = {"cpu": "CPU", "disk": "Disks"}
    for z in user.get("zones", []):
        if "source" in z:
            z["curve"] = to_curve.get(z.pop("source"), "CPU")
            changed = True
        f = z.get("floor")
        if f and "source" in f:
            f["sensor"] = to_sensor.get(f.pop("source"), "cpu")
            changed = True
    if user.get("sensors", {}).pop("include_nvme", None) is not None:
        changed = True              # NVMe now has its own sensors (nvme_max)
    # the idle profile was removed: drop its points and setting
    for c in user.get("curves", []) if isinstance(user.get("curves"), list) else []:
        if isinstance(c.get("points"), dict) and "idle" in c["points"]:
            del c["points"]["idle"]
            changed = True
    prof = user.get("profile", {})
    if prof.pop("idle_load_below", "-") != "-":
        changed = True
    if prof.get("mode") == "idle":
        prof["mode"] = "auto"
        changed = True
    return changed


def curve_points(curve, profile):
    """Points for a profile; scrub falls back to normal when unset."""
    return curve["points"].get(profile) or curve["points"]["normal"]


def deep_merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _num(v, name, lo, hi):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"{name} must be a number")
    if not lo <= v <= hi:
        raise ValueError(f"{name} must be between {lo} and {hi}")
    return v


def validate_config(cfg):
    """Raise ValueError with a readable message if cfg is invalid."""
    _num(cfg["interval"], "interval", 1, 300)
    _num(cfg["temp_avg_samples"], "temp_avg_samples", 1, 60)
    _num(cfg["hysteresis"], "hysteresis", 0, 30)
    _num(cfg["scrub_cooldown_cycles"], "scrub_cooldown_cycles", 0, 10000)
    for k in ("safe_exit_pwm", "sensor_fail_pwm"):
        _num(cfg[k], k, 0, 255)
    em = cfg["emergency"]
    _num(em["cpu"], "emergency.cpu", 40, 120)
    _num(em["disk"], "emergency.disk", 30, 90)
    _num(em["pwm"], "emergency.pwm", 100, 255)
    _num(cfg["serial"]["baud"], "serial.baud", 300, 2000000)
    _num(cfg["serial"]["watchdog_seconds"], "serial.watchdog_seconds", 0, 3600)
    if cfg["profile"]["mode"] not in ("auto",) + PROFILES:
        raise ValueError("profile.mode must be auto, normal or scrub")
    curves = cfg["curves"]
    if not isinstance(curves, list) or not curves:
        raise ValueError("curves must be a non-empty list")
    names = set()
    for c in curves:
        n = c.get("name")
        if not isinstance(n, str) or not n.strip():
            raise ValueError("every curve needs a name")
        if n in names:
            raise ValueError(f"curve name '{n}' is used twice")
        names.add(n)
        if not isinstance(c.get("sensor"), str) or not c["sensor"]:
            raise ValueError(f"curve '{n}' needs a sensor")
        pts_by = c.get("points")
        if not isinstance(pts_by, dict) or not pts_by.get("normal"):
            raise ValueError(f"curve '{n}' needs normal points")
        for prof in PROFILES:
            pts = pts_by.get(prof)
            if pts is None and prof != "normal":
                continue
            label = f"curve '{n}' ({prof})"
            if not isinstance(pts, list) or len(pts) < 2:
                raise ValueError(f"{label} needs at least 2 points")
            last = -1e9
            for p in pts:
                if not isinstance(p, (list, tuple)) or len(p) != 2:
                    raise ValueError(f"{label}: each point must be [temp, pwm]")
                _num(p[0], f"{label} temp", 0, 120)
                _num(p[1], f"{label} pwm", 0, 255)
                if p[0] <= last:
                    raise ValueError(f"{label}: temperatures must be strictly increasing")
                last = p[0]
    ids = set()
    if not isinstance(cfg["zones"], list) or not cfg["zones"]:
        raise ValueError("zones must be a non-empty list")
    for z in cfg["zones"]:
        if z.get("id") not in (1, 2, 3) or z["id"] in ids:
            raise ValueError("zone ids must be unique values 1-3")
        ids.add(z["id"])
        if not isinstance(z.get("tach", False), bool):
            raise ValueError(f"zone {z['id']} tach must be true or false")
        if not isinstance(z.get("enabled", True), bool):
            raise ValueError(f"zone {z['id']} enabled must be true or false")
        if z.get("curve") not in names:
            raise ValueError(f"zone {z['id']} uses curve '{z.get('curve')}', which doesn't exist")
        f = z.get("floor")
        if f:
            if not isinstance(f.get("sensor"), str) or not f["sensor"]:
                raise ValueError(f"zone {z['id']} floor needs a sensor")
            _num(f["above"], f"zone {z['id']} floor.above", 0, 120)
            _num(f["pwm"], f"zone {z['id']} floor.pwm", 0, 255)
    _num(cfg["web"]["port"], "web.port", 1, 65535)
    _num(cfg["history"]["hours"], "history.hours", 0, 24 * 14)


class ConfigManager:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.cfg = self._load()

    def _load(self):
        user = {}
        if os.path.exists(self.path):
            with open(self.path) as f:
                user = json.load(f)
        migrated = migrate_config(user)
        cfg = deep_merge(DEFAULT_CONFIG, user)
        validate_config(cfg)
        if migrated:
            try:
                self._write(cfg)             # previous file kept as config.json.bak
                log.info("upgraded %s to named curves (old version saved as .bak)", self.path)
            except OSError as e:
                log.warning("could not save upgraded config: %s", e)
        if not os.path.exists(self.path):
            try:
                self._write(cfg)
                log.info("wrote default config to %s", self.path)
            except OSError as e:
                log.warning("could not write default config %s: %s", self.path, e)
        return cfg

    def _write(self, cfg):
        d = os.path.dirname(self.path) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".config.", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
        if os.path.exists(self.path):
            try:
                with open(self.path) as src, open(self.path + ".bak", "w") as dst:
                    dst.write(src.read())
            except OSError:
                pass
        os.replace(tmp, self.path)

    def get(self):
        with self.lock:
            return copy.deepcopy(self.cfg)

    def update(self, new_cfg):
        migrate_config(new_cfg)
        merged = deep_merge(DEFAULT_CONFIG, new_cfg)
        validate_config(merged)
        with self.lock:
            old = self.cfg
            self._write(merged)
            self.cfg = merged
        return old, copy.deepcopy(merged)


# ════════════════════════════════════════════════════════════════════
# Arduino serial
# ════════════════════════════════════════════════════════════════════

def resolve_port(port):
    if port and port != "auto":
        return port
    by_id = "/dev/serial/by-id"
    if os.path.isdir(by_id):
        ids = sorted(os.listdir(by_id))
        if ids:
            return os.path.join(by_id, ids[0])
    for p in ("/dev/ttyUSB0", "/dev/ttyACM0"):
        if os.path.exists(p):
            return p
    return "/dev/ttyUSB0"


class Arduino:
    """Line-based serial link using termios (no pyserial needed)."""

    def __init__(self, port, baud):
        self.port = port
        self.baud = baud
        self.fd = None
        self.buf = b""

    def open(self):
        import termios
        fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            attrs = termios.tcgetattr(fd)
            speed = getattr(termios, f"B{self.baud}")
            attrs[0] = 0                                                # iflag: raw
            attrs[1] = 0                                                # oflag: raw
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL    # 8N1, no flow ctl
            attrs[3] = 0                                                # lflag: no echo/canon
            attrs[4] = speed
            attrs[5] = speed
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except Exception:
            os.close(fd)
            raise
        self.fd = fd
        self.buf = b""
        # Opening the port resets a Nano; wait for its "READY" greeting.
        greeting = self.readline(4.0)
        return greeting

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = None

    def readline(self, timeout):
        end = time.monotonic() + timeout
        while True:
            if b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                return line.decode(errors="replace").strip()
            left = end - time.monotonic()
            if left <= 0:
                return None
            r, _, _ = select.select([self.fd], [], [], left)
            if r:
                chunk = os.read(self.fd, 256)
                if not chunk:
                    raise OSError("serial port closed")
                self.buf += chunk

    def command(self, cmd, timeout=1.0):
        """Send cmd, return first non-empty reply line (or None on timeout)."""
        # drain anything stale
        try:
            while True:
                r, _, _ = select.select([self.fd], [], [], 0)
                if not r:
                    break
                if not os.read(self.fd, 256):
                    break
        except BlockingIOError:
            pass
        self.buf = b""
        os.write(self.fd, (cmd + "\n").encode())
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return None
            line = self.readline(left)
            if line is None:
                return None
            if line:
                return line


class SimArduino:
    """Fake firmware for --simulate."""

    def __init__(self, *_):
        self.port = "simulated"
        self.z = {1: 50, 2: 50, 3: 50}
        self.rpm_seed = {1: 7.8, 2: 8.4, 3: 6.9}

    def open(self):
        return "READY"

    def close(self):
        pass

    def command(self, cmd, timeout=1.0):
        if cmd == "?":
            return f"Z1:{self.z[1]} Z2:{self.z[2]} Z3:{self.z[3]}"
        if cmd == "V":
            return "VER 1"
        if cmd == "R":
            rpm = {k: (0 if v < 20 else int(v * self.rpm_seed[k] + random.uniform(-25, 25)))
                   for k, v in self.z.items()}
            return f"R1:{rpm[1]} R2:{rpm[2]} R3:{rpm[3]}"
        if cmd.startswith("W:"):
            return "OK W:" + cmd[2:]
        if cmd.startswith("A:"):
            v = max(0, min(255, int(cmd[2:])))
            self.z = {1: v, 2: v, 3: v}
            return f"OK ALL:{v}"
        m = re.match(r"Z([123]):(\d+)$", cmd)
        if m:
            self.z[int(m.group(1))] = max(0, min(255, int(m.group(2))))
            return f"OK Z{m.group(1)}:{m.group(2)}"
        return "ERR unknown command"


# ════════════════════════════════════════════════════════════════════
# Sensors
# ════════════════════════════════════════════════════════════════════

def run(cmd, timeout=10):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout
    except (OSError, subprocess.TimeoutExpired):
        return None, ""


def _sane(t):
    """Unconnected hwmon inputs often read 0, 127 or -128 °C."""
    return t is not None and 0 < t < 125


def _nat_key(s):
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", s)]


GROUP_ORDER = ["Combined", "CPU", "Disks", "NVMe", "GPU", "Board"]


class Sensors:
    HWMON = "/sys/class/hwmon"
    CPU_DRIVERS = ("coretemp", "k10temp", "zenpower")
    GPU_DRIVERS = ("amdgpu", "radeon", "nouveau")

    def __init__(self):
        self._cpu_path = None
        self._cpu_label = None
        self._models = {}
        self._serials = {}
        self._hwmon = []
        self._hwmon_scanned = 0.0

    @staticmethod
    def _read(path):
        try:
            with open(path) as f:
                return f.read().strip()
        except OSError:
            return None

    # ── hwmon discovery: every kernel temperature sensor ──────────
    def _scan_hwmon(self):
        found, seen = [], {}
        try:
            hwmons = sorted(os.listdir(self.HWMON), key=_nat_key)
        except OSError:
            return found
        for h in hwmons:
            d = os.path.join(self.HWMON, h)
            driver = self._read(os.path.join(d, "name"))
            if not driver:
                continue
            # The parent device (PCI address, nvme0, coretemp.0 …) is stable across
            # reboots, unlike the hwmonN number.
            dev_link = os.path.join(d, "device")
            device = os.path.basename(os.path.realpath(dev_link)) if os.path.exists(dev_link) else ""
            model = ""
            if driver == "nvme" and device:
                model = self._read(f"/sys/class/nvme/{device}/model") or ""
            try:
                inputs = sorted((x for x in os.listdir(d) if re.match(r"temp\d+_input$", x)), key=_nat_key)
            except OSError:
                continue
            for inp in inputs:
                label = self._read(os.path.join(d, inp.replace("_input", "_label"))) or inp.split("_")[0]
                sid = f"hwmon:{driver}:{device}:{label}"
                seen[sid] = seen.get(sid, 0) + 1
                if seen[sid] > 1:
                    sid += f"#{seen[sid]}"
                if driver in self.CPU_DRIVERS:
                    group = "CPU"
                elif driver == "nvme":
                    group = "NVMe"
                elif driver == "drivetemp":
                    group = "Disks"
                elif driver in self.GPU_DRIVERS:
                    group = "GPU"
                else:
                    group = "Board"
                who = f"{device} {model}".strip() if driver == "nvme" else driver
                found.append({"id": sid, "path": os.path.join(d, inp), "driver": driver, "device": device,
                              "label": label, "group": group, "name": f"{who} · {label}"})
        return found

    def read_hwmon(self):
        if time.monotonic() - self._hwmon_scanned > 300 or not self._hwmon:
            self._hwmon = self._scan_hwmon()
            self._hwmon_scanned = time.monotonic()
        out = []
        for s in self._hwmon:
            raw = self._read(s["path"])
            try:
                t = int(raw) / 1000.0 if raw is not None else None
            except ValueError:
                t = None
            if _sane(t):
                out.append(dict(s, temp=round(t, 1)))
        return out

    # ── CPU package (the "cpu" sensor) ────────────────────────────
    def _find_cpu_hwmon(self):
        prefer = {"coretemp": ("Package id 0",), "k10temp": ("Tctl", "Tdie"), "zenpower": ("Tdie", "Tctl")}
        candidates = [s for s in (self._hwmon or self._scan_hwmon()) if s["driver"] in prefer]
        for s in candidates:
            if s["label"] in prefer[s["driver"]]:
                return s["path"], f"{s['driver']} {s['label']}"
        if candidates:
            return candidates[0]["path"], f"{candidates[0]['driver']} {candidates[0]['label']}"
        return None, None

    def cpu_temp(self, setting):
        if setting and setting != "auto":
            self._cpu_path, self._cpu_label = setting, setting
        elif self._cpu_path is None:
            self._cpu_path, self._cpu_label = self._find_cpu_hwmon()
        if self._cpu_path:
            raw = self._read(self._cpu_path)
            try:
                return int(raw) / 1000.0
            except (TypeError, ValueError):
                if setting == "auto":
                    self._cpu_path = None
        # fallback: lm-sensors
        rc, out = run(["sensors"])
        m = re.search(r"Package id 0:\s*\+?([\d.]+)", out) or re.search(r"Tctl:\s*\+?([\d.]+)", out)
        if m:
            self._cpu_label = "sensors"
            return float(m.group(1))
        return None

    @property
    def cpu_label(self):
        return self._cpu_label

    # ── SATA/SAS disks via smartctl ───────────────────────────────
    @staticmethod
    def list_disks(cfg_sensors):
        if cfg_sensors["disks"] != "auto":
            return list(cfg_sensors["disks"])
        try:
            return ["/dev/" + n for n in sorted(os.listdir("/dev")) if re.match(r"^sd[a-z]+$", n)]
        except OSError:
            return []

    def disk_temp(self, dev, skip_standby):
        cmd = ["smartctl", "-A", "-i", "-j"]
        if skip_standby:
            cmd += ["-n", "standby"]
        rc, out = run(cmd + [dev])
        if rc is None:
            return None, False
        try:
            j = json.loads(out)
        except ValueError:
            j = None
        if j is not None:
            if dev not in self._models:
                self._models[dev] = j.get("model_name") or j.get("scsi_model_name") or ""
                self._serials[dev] = j.get("serial_number") or ""
            standby = bool(j.get("power_mode", {}).get("is_in_standby")) or \
                any("STANDBY" in m.get("string", "").upper() for m in j.get("smartctl", {}).get("messages", []))
            t = j.get("temperature", {}).get("current")
            if t is not None:
                return float(t), False
            return None, standby
        # old smartctl without -j: parse text
        rc, out = run(["smartctl", "-A", dev])
        for line in out.splitlines():
            if re.search(r"Temperature_Celsius|Airflow_Temperature", line):
                parts = line.split()
                if len(parts) >= 10:
                    m = re.match(r"(\d+)", parts[9])
                    if m:
                        return float(m.group(1)), False
            m = re.search(r"(?:Current Drive Temperature|Temperature):\s+(\d+)", line)
            if m:
                return float(m.group(1)), False
        return None, False

    def model(self, dev):
        return self._models.get(dev, "")

    def serial(self, dev):
        return self._serials.get(dev, "")

    # ── ZFS: scrub/resilver activity and pool → disk membership ───
    @staticmethod
    def zpool_info(detect_resilver):
        """Returns (activity, members). members maps pool → {"sda", "nvme0", …}."""
        rc, out = run(["zpool", "status", "-LP"])
        activity, members, pool = [], {}, None
        for line in out.splitlines():
            m = re.match(r"\s*pool:\s*(\S+)", line)
            if m:
                pool = m.group(1)
                members.setdefault(pool, set())
                continue
            if "scrub in progress" in line or (detect_resilver and "resilver in progress" in line):
                activity.append({"pool": pool, "kind": "resilver" if "resilver" in line else "scrub"})
            m = re.search(r"/dev/(\S+)", line)
            if m and pool:
                name = os.path.basename(m.group(1))
                b = re.match(r"(nvme\d+)n\d+", name) or re.match(r"(sd[a-z]+)", name)
                if b:
                    members[pool].add(b.group(1))
        return activity, {p: d for p, d in members.items() if d}


class SimSensors(Sensors):
    """Plausible fake sensors that react to fan PWM, for --simulate."""

    def __init__(self, controller_ref):
        super().__init__()
        self.ref = controller_ref
        self.t0 = time.time()
        self.disk_base = {"/dev/sda": 33.0, "/dev/sdb": 35.0, "/dev/sdc": 34.0, "/dev/sdd": 36.5}
        self._models = {d: "SIM WD80EFZZ" for d in self.disk_base}
        self._serials = {d: f"SIMSERIAL{i}" for i, d in enumerate(self.disk_base)}
        self._cpu_label = "coretemp Package id 0"
        self.scrub_until = 0
        self._last_cpu = 45.0

    def _pwm(self, zid):
        return self.ref.sent.get(zid, 50) if self.ref else 50

    def cpu_temp(self, setting):
        t = time.time() - self.t0
        load = 45 + 18 * math.sin(t / 90) + 8 * math.sin(t / 13)
        cooling = (self._pwm(2) + self._pwm(3)) / 510 * 12
        self._last_cpu = round(load - cooling + random.uniform(-1.5, 1.5), 1)
        return self._last_cpu

    def read_hwmon(self):
        c = self._last_cpu
        t = time.time() - self.t0
        def s(driver, device, label, group, name, temp):
            return {"id": f"hwmon:{driver}:{device}:{label}", "driver": driver, "device": device, "label": label,
                    "group": group, "name": name, "temp": round(temp, 1)}
        out = [s("coretemp", "coretemp.0", "Package id 0", "CPU", "coretemp · Package id 0", c)]
        out += [s("coretemp", "coretemp.0", f"Core {i}", "CPU", f"coretemp · Core {i}", c - 2 + random.uniform(-2, 2))
                for i in range(4)]
        out.append(s("nvme", "nvme0", "Composite", "NVMe", "nvme0 SIM 980 PRO · Composite",
                     41 + 3 * math.sin(t / 200) - self._pwm(1) / 255 * 3))
        out.append(s("nct6775", "nct6775.656", "SYSTIN", "Board", "nct6775 · SYSTIN", 31 + math.sin(t / 400)))
        out.append(s("acpitz", "LNXTHERM:00", "temp1", "Board", "acpitz · temp1", 27.8))
        return out

    @staticmethod
    def list_disks(cfg_sensors):
        return ["/dev/sda", "/dev/sdb", "/dev/sdc", "/dev/sdd"]

    def disk_temp(self, dev, skip_standby):
        t = time.time() - self.t0
        cooling = self._pwm(1) / 255 * 6
        scrub = 6 if time.time() < self.scrub_until else 0
        return round(self.disk_base[dev] + 4 * math.sin(t / 300) + scrub - cooling + random.uniform(-0.3, 0.3), 1), False

    def zpool_info(self, detect_resilver):
        members = {"tank": {"sda", "sdb", "sdc", "sdd"}, "rpool": {"nvme0"}}
        t = time.time() - self.t0
        if 300 < (t % 900) < 480:        # a fake 3-minute scrub every 15 minutes
            self.scrub_until = time.time() + 60
            return [{"pool": "tank", "kind": "scrub"}], members
        return [], members


# ════════════════════════════════════════════════════════════════════
# Control
# ════════════════════════════════════════════════════════════════════

def interpolate(curve, temp):
    if temp <= curve[0][0]:
        return int(curve[0][1])
    if temp >= curve[-1][0]:
        return int(curve[-1][1])
    for (t0, p0), (t1, p1) in zip(curve, curve[1:]):
        if temp <= t1:
            return int(p0 + (p1 - p0) * (temp - t0) / (t1 - t0))
    return int(curve[-1][1])


class ZoneState:
    def __init__(self):
        self.reset()

    def reset(self):
        self.peak = None
        self.pwm = 0
        self.floor_active = False


HISTORY_FIELDS = ["t", "cpu", "disk", "z1", "z2", "z3", "r1", "r2", "r3", "profile"]


class Controller:
    def __init__(self, cfgm, simulate=False):
        self.cfgm = cfgm
        self.simulate = simulate
        self.sensors = SimSensors(self) if simulate else Sensors()
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stop = threading.Event()

        self.arduino = None
        self.arduino_info = {"connected": False, "port": None, "firmware": None, "tach": False,
                             "watchdog": 0, "error": None, "last_reply": None}
        self._next_connect = 0.0
        self._reconnect_requested = False

        self.avg = {}                  # sensor id -> deque of recent readings
        self.readings = {}             # sensor id -> latest reading dict
        self.disks = {}
        self._last_disk_poll = 0.0
        self.zone_state = {1: ZoneState(), 2: ZoneState(), 3: ZoneState()}
        self.sent = {}
        self.rpm = {1: None, 2: None, 3: None}
        self.overrides = {}            # zid -> {"pwm": int, "until": epoch|None}
        self.last_profile = None
        self.cooldown = 0
        self.status = {}

        cfg = cfgm.get()
        self.history = deque(maxlen=self._history_len(cfg))
        self._load_history(cfg)
        self._last_history_save = time.time()

    # ── history ───────────────────────────────────────────────────
    @staticmethod
    def _history_len(cfg):
        return max(1, int(cfg["history"]["hours"] * 3600 / cfg["interval"]))

    def _load_history(self, cfg):
        path = cfg["history"]["file"]
        try:
            with open(path) as f:
                rows = json.load(f).get("rows", [])
            cutoff = time.time() - cfg["history"]["hours"] * 3600
            self.history.extend(r for r in rows if r and r[0] >= cutoff)
            log.info("loaded %d history rows from %s", len(self.history), path)
        except (OSError, ValueError):
            pass

    def save_history(self):
        cfg = self.cfgm.get()
        path = cfg["history"]["file"]
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with self.lock:
                rows = list(self.history)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"fields": HISTORY_FIELDS, "rows": rows}, f, separators=(",", ":"))
            os.replace(tmp, path)
        except OSError as e:
            log.warning("could not save history: %s", e)

    # ── arduino link ──────────────────────────────────────────────
    def _disconnect(self, err):
        if self.arduino:
            self.arduino.close()
        self.arduino = None
        self.arduino_info.update(connected=False, error=str(err))
        self._next_connect = time.monotonic() + 10
        log.warning("arduino link lost: %s", err)

    def _connect(self, cfg):
        port = resolve_port(cfg["serial"]["port"])
        a = SimArduino() if self.simulate else Arduino(port, cfg["serial"]["baud"])
        try:
            greeting = a.open()
            ver = a.command("V")
            # fw 0 = firmware without the V command: basic PWM only, no tach/watchdog
            fw = 0
            if ver and ver.startswith("VER"):
                fw = int(re.sub(r"\D", "", ver) or 1)
            wd = 0
            if fw >= 1 and cfg["serial"]["watchdog_seconds"]:
                wd = cfg["serial"]["watchdog_seconds"]
                a.command(f"W:{wd}")
            tach = fw >= 1 and (a.command("R") or "").startswith("R1:")
        except OSError as e:
            a.close()
            self.arduino_info.update(connected=False, port=port, error=str(e))
            self._next_connect = time.monotonic() + 10
            log.warning("cannot open arduino at %s: %s (retrying in 10s)", port, e)
            return
        self.arduino = a
        self.arduino_info.update(connected=True, port=a.port, firmware=fw, tach=tach,
                                 watchdog=wd, error=None, last_reply=greeting)
        self.sent = {}
        log.info("arduino connected on %s (firmware v%s, tach=%s, watchdog=%ss, greeting=%r)",
                 a.port, fw, tach, wd, greeting)

    def _cmd(self, cmd):
        try:
            reply = self.arduino.command(cmd)
            if reply == "READY":           # arduino rebooted under us
                self.sent = {}
                reply = self.arduino.command(cmd)
        except OSError as e:
            self._disconnect(e)
            return None
        self.arduino_info["last_reply"] = reply
        return reply

    def reconnect(self):
        """Ask the control thread (which owns the port) to reopen it."""
        self._reconnect_requested = True
        self.wake.set()

    def _sync_arduino(self, cfg, want):
        """Runs on the control thread only, outside self.lock."""
        if self._reconnect_requested:
            self._reconnect_requested = False
            if self.arduino:
                self.arduino.close()
            self.arduino = None
            self._next_connect = 0
        if self.arduino is None and time.monotonic() >= self._next_connect:
            self._connect(cfg)
        if self.arduino is None:
            return
        for zid, pwm in want.items():
            if self.sent.get(zid) != pwm and self.arduino:
                reply = self._cmd(f"Z{zid}:{pwm}")
                if reply and reply.startswith("OK"):
                    self.sent[zid] = pwm
        if self.arduino:
            reply = self._cmd("?")       # heartbeat for the watchdog + verify
            if reply:
                actual = {int(a): int(b) for a, b in re.findall(r"Z(\d):(\d+)", reply)}
                for zid, v in actual.items():
                    if zid in want and v != want[zid]:
                        log.warning("zone %d reads %d, expected %d — resending", zid, v, want[zid])
                        self.sent.pop(zid, None)
        if self.arduino and self.arduino_info["tach"] and any(z.get("tach") for z in cfg["zones"]):
            reply = self._cmd("R")
            if reply:
                for a, b in re.findall(r"R(\d):(\d+)", reply):
                    self.rpm[int(a)] = int(b)

    # ── one control cycle ─────────────────────────────────────────
    def _avg(self, sid, value, n):
        q = self.avg.setdefault(sid, deque())
        if value is None:
            q.clear()                  # sensor gone: report None, don't hide it behind stale values
            return None
        q.append(value)
        while len(q) > n:
            q.popleft()
        return round(sum(q) / len(q), 1)

    def _read_sensors(self, cfg):
        """Read everything. Returns (readings by id, zfs activity)."""
        r = {}

        def add(sid, name, group, temp, **extra):
            r[sid] = dict(extra, id=sid, name=name, group=group, temp=temp)

        cpu = self.sensors.cpu_temp(cfg["sensors"]["cpu"])
        add("cpu", "CPU", "Combined", cpu, detail=self.sensors.cpu_label)

        nvme = {}                      # nvme0 -> [(label, temp), …]
        for s in self.sensors.read_hwmon():
            add(s["id"], s["name"], s["group"], s["temp"])
            if s["driver"] == "nvme" and s["device"]:
                nvme.setdefault(s["device"], []).append((s["label"], s["temp"]))
        # one temp per NVMe drive: its "Composite" reading, else its hottest sensor
        nvme_by_ctrl = {d: next((t for lab, t in v if lab == "Composite"), max(t for _, t in v))
                        for d, v in nvme.items()}

        now = time.monotonic()
        if now - self._last_disk_poll >= cfg["interval"] - 0.5 or not self.disks:
            self._last_disk_poll = now
            found = {}
            excl = set(cfg["sensors"]["disk_exclude"])
            for dev in self.sensors.list_disks(cfg["sensors"]):
                t, standby = self.sensors.disk_temp(dev, cfg["sensors"]["skip_standby"])
                name = os.path.basename(dev)
                if t is None and not standby:
                    continue
                found[name] = {"name": name, "dev": dev, "temp": t, "standby": standby,
                               "model": self.sensors.model(dev), "serial": self.sensors.serial(dev),
                               "used": name not in excl and t is not None}
            self.disks = found
        for d in self.disks.values():
            # disks are identified by serial so a changed sdX letter doesn't move a curve
            add("disk:" + (d["serial"] or d["name"]), f"{d['name']} · {d['model']}".strip(" ·"), "Disks",
                d["temp"], standby=d["standby"])

        used = {n: d["temp"] for n, d in self.disks.items() if d["used"]}
        if used:
            add("disks_max", "Disks (max)", "Combined", max(used.values()))
        else:
            add("disks_max", "Disks (max)", "Combined", None)
        if nvme_by_ctrl:
            add("nvme_max", "NVMe (max)", "Combined", max(nvme_by_ctrl.values()))

        zfs, members = self.sensors.zpool_info(cfg["sensors"]["detect_resilver"])
        for pool, devs in sorted(members.items()):
            temps = [used[d] for d in devs if d in used] + [nvme_by_ctrl[d] for d in devs if d in nvme_by_ctrl]
            if temps:
                add(f"pool:{pool}", f"Pool {pool} (max)", "Combined", max(temps))
        return r, zfs

    def _select_profile(self, cfg, zfs):
        if zfs:
            self.cooldown = cfg["scrub_cooldown_cycles"]
            auto = "scrub"
        elif self.cooldown > 0:
            self.cooldown -= 1
            auto = "scrub"
        else:
            auto = "normal"
        mode = cfg["profile"]["mode"]
        active = auto if mode == "auto" else mode
        if active != self.last_profile:
            if self.last_profile is not None:
                log.info("profile %s → %s", self.last_profile, active)
            for zs in self.zone_state.values():
                zs.reset()
            self.last_profile = active
        return active, auto

    def step(self):
        cfg = self.cfgm.get()
        t_start = time.monotonic()
        H = cfg["hysteresis"]
        n = cfg["temp_avg_samples"]

        readings, zfs = self._read_sensors(cfg)
        cpu_raw = readings["cpu"]["temp"]
        disk_raw = readings["disks_max"]["temp"]

        with self.lock:
            self.readings = readings
            # average every sensor that exists now, plus any a curve/floor refers to
            wanted = set(readings) | {c["sensor"] for c in cfg["curves"]} | \
                {z["floor"]["sensor"] for z in cfg["zones"] if z.get("floor")}
            avg = {sid: self._avg(sid, readings.get(sid, {}).get("temp"), n) for sid in wanted}
            for sid in list(self.avg):
                if sid not in wanted:
                    del self.avg[sid]
            curves = {c["name"]: c for c in cfg["curves"]}
            profile, auto_profile = self._select_profile(cfg, zfs)

            em = cfg["emergency"]
            emergency = []
            if cpu_raw is not None and cpu_raw >= em["cpu"]:
                emergency.append(f"CPU {cpu_raw:.0f}°C ≥ {em['cpu']}°C")
            if disk_raw is not None and disk_raw >= em["disk"]:
                emergency.append(f"Disk {disk_raw:.0f}°C ≥ {em['disk']}°C")

            now = time.time()
            for zid, ov in list(self.overrides.items()):
                if ov["until"] and ov["until"] <= now:
                    del self.overrides[zid]
                    log.info("zone %d manual override expired", zid)

            zones_out = []
            want = {}
            for z in cfg["zones"]:
                zid = z["id"]
                zs = self.zone_state[zid]
                c = curves[z["curve"]]
                sensor = c["sensor"]
                t = avg.get(sensor)
                curve = curve_points(c, profile)
                mode = "auto"
                curve_pwm = None
                enabled = z.get("enabled", True)
                if not enabled:
                    pwm, mode = 0, "off"
                    zs.reset()
                elif t is None:
                    pwm = cfg["sensor_fail_pwm"]
                    mode = "sensor-fail"
                else:
                    curve_pwm = interpolate(curve, t)
                    if zs.peak is None:
                        zs.peak = t
                    if t > zs.peak:
                        zs.peak = t
                    if curve_pwm > zs.pwm:
                        zs.pwm, zs.peak = curve_pwm, t
                    elif curve_pwm < zs.pwm and t <= zs.peak - H:
                        zs.pwm, zs.peak = curve_pwm, t
                    pwm = zs.pwm

                f = z.get("floor") if enabled else None
                if f:
                    ft = avg.get(f["sensor"])
                    if ft is not None:
                        if zs.floor_active:
                            zs.floor_active = not (ft <= f["above"] - H)
                        else:
                            zs.floor_active = ft > f["above"]
                    if zs.floor_active and f["pwm"] > pwm and mode == "auto":
                        pwm = f["pwm"]
                        mode = "floor"

                ov = self.overrides.get(zid) if enabled else None
                if ov:
                    pwm, mode = ov["pwm"], "manual"
                if emergency:
                    pwm, mode = em["pwm"], "emergency"

                pwm = int(max(0, min(255, pwm)))
                want[zid] = pwm
                zones_out.append({
                    "id": zid, "name": z["name"], "curve": c["name"], "sensor": sensor,
                    "sensor_name": readings.get(sensor, {}).get("name", sensor), "temp": t,
                    "pwm": pwm, "percent": round(pwm / 2.55), "curve_pwm": curve_pwm,
                    "hyst_pwm": zs.pwm, "peak": zs.peak, "floor_active": zs.floor_active,
                    "held": curve_pwm is not None and zs.pwm > curve_pwm,
                    "release_below": None if zs.peak is None else round(zs.peak - H, 1),
                    "floor": f, "enabled": enabled, "tach": bool(z.get("tach", False)),
                    "mode": mode, "override": ov,
                })

        # ── talk to the arduino (outside the lock so the web UI never waits on serial) ──
        self._sync_arduino(cfg, want)

        with self.lock:
            for zo in zones_out:
                # only zones marked "RPM wired": an unconnected tach pin just reads 0
                zo["rpm"] = self.rpm.get(zo["id"]) if self.arduino_info["tach"] and zo["tach"] else None
                zo["applied"] = self.sent.get(zo["id"])

            used_by = {}
            for c in cfg["curves"]:
                used_by.setdefault(c["sensor"], []).append(f"curve {c['name']}")
            for z in cfg["zones"]:
                if z.get("floor") and z.get("enabled", True):
                    used_by.setdefault(z["floor"]["sensor"], []).append(f"Z{z['id']} floor")
            sensors_out = [dict(s, avg=avg.get(sid), used_by=used_by.get(sid, []))
                           for sid, s in readings.items()]
            # sensors a curve/floor refers to that aren't present right now
            sensors_out += [{"id": sid, "name": sid, "group": "Missing", "temp": None, "avg": None,
                             "used_by": who} for sid, who in used_by.items() if sid not in readings]
            sensors_out.sort(key=lambda s: (GROUP_ORDER.index(s["group"]) if s["group"] in GROUP_ORDER
                                            else len(GROUP_ORDER), _nat_key(s["name"])))
            self.status = {
                "version": VERSION,
                "host": "pve-demo" if self.simulate else socket.gethostname(),
                "time": now,
                "simulate": self.simulate,
                "interval": cfg["interval"],
                "arduino": dict(self.arduino_info),
                "temps": {
                    "cpu": {"raw": cpu_raw, "avg": avg.get("cpu"), "sensor": self.sensors.cpu_label},
                    "disk": {"raw": disk_raw, "avg": avg.get("disks_max")},
                    "disks": sorted(self.disks.values(), key=lambda d: d["name"]),
                },
                "sensors": sensors_out,
                "profile": {"active": profile, "auto": auto_profile, "mode": cfg["profile"]["mode"],
                            "zfs": zfs, "cooldown": self.cooldown, "load": os.getloadavg()[0] if hasattr(os, "getloadavg") else None},
                "emergency": emergency,
                "zones": zones_out,
                "cycle_ms": round((time.monotonic() - t_start) * 1000),
            }
            r = lambda v: None if v is None else round(v, 1)
            pc = 2 if profile == "scrub" else 1    # history column: 1 = normal, 2 = scrub
            pwm_by = {zo["id"]: zo["pwm"] for zo in zones_out}
            rpm_by = {zo["id"]: zo["rpm"] for zo in zones_out}
            self.history.append([int(now), r(cpu_raw), r(disk_raw),
                                 pwm_by.get(1), pwm_by.get(2), pwm_by.get(3),
                                 rpm_by.get(1), rpm_by.get(2), rpm_by.get(3), pc])
        return self.status

    def run(self):
        last_log = 0
        while not self.stop.is_set():
            try:
                st = self.step()
                if time.monotonic() - last_log > 60:
                    last_log = time.monotonic()
                    zs = " ".join(f"{z['name']}={z['pwm']}({z['mode']})" for z in st["zones"])
                    log.info("cpu=%s/%s disk=%s/%s profile=%s | %s",
                             st["temps"]["cpu"]["raw"], st["temps"]["cpu"]["avg"],
                             st["temps"]["disk"]["raw"], st["temps"]["disk"]["avg"],
                             st["profile"]["active"], zs)
                if time.time() - self._last_history_save > 600:
                    self._last_history_save = time.time()
                    self.save_history()
                if not self.simulate:
                    write_status_file(st)
            except Exception:
                log.exception("control cycle failed")
            self.wake.wait(self.cfgm.get()["interval"])
            self.wake.clear()

    def shutdown(self):
        cfg = self.cfgm.get()
        with self.lock:
            if self.arduino:
                try:
                    if self.arduino_info.get("watchdog"):
                        self.arduino.command("W:0")
                    self.arduino.command(f"A:{cfg['safe_exit_pwm']}")
                    log.info("fans set to safe exit PWM %d", cfg["safe_exit_pwm"])
                except OSError:
                    pass
                self.arduino.close()
        self.save_history()

    # ── API actions ───────────────────────────────────────────────
    def set_override(self, zid, pwm, minutes):
        with self.lock:
            targets = [1, 2, 3] if zid == "all" else [int(zid)]
            for z in targets:
                if pwm is None:
                    self.overrides.pop(z, None)
                else:
                    until = time.time() + minutes * 60 if minutes else None
                    self.overrides[z] = {"pwm": int(max(0, min(255, pwm))), "until": until}
            log.info("override zones=%s pwm=%s minutes=%s", targets, pwm, minutes)
        self.wake.set()

    def on_config_change(self, old, new):
        if old["serial"] != new["serial"]:
            self._reconnect_requested = True
        with self.lock:
            if self._history_len(old) != self._history_len(new):
                self.history = deque(self.history, maxlen=self._history_len(new))
            for zs in self.zone_state.values():
                zs.reset()
        self.wake.set()


def write_status_file(st):
    try:
        os.makedirs("/run/proxmox-fancontrol", exist_ok=True)
        tmp = "/run/proxmox-fancontrol/status.json.tmp"
        with open(tmp, "w") as f:
            json.dump(st, f)
        os.replace(tmp, "/run/proxmox-fancontrol/status.json")
    except OSError:
        pass


# ════════════════════════════════════════════════════════════════════
# Web
# ════════════════════════════════════════════════════════════════════

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def downsample(rows, points):
    if len(rows) <= points:
        return rows
    size = len(rows) / points
    out = []
    for i in range(points):
        chunk = rows[int(i * size):int((i + 1) * size)] or [rows[-1]]
        row = list(chunk[-1])
        for col in range(1, 9):           # max of each numeric column keeps peaks visible
            vals = [c[col] for c in chunk if c[col] is not None]
            row[col] = max(vals) if vals else None
        out.append(row)
    return out


def metrics_text(st):
    lines = []

    def g(name, help_, samples):
        lines.append(f"# HELP {name} {help_}")
        lines.append(f"# TYPE {name} gauge")
        for labels, v in samples:
            if v is None:
                continue
            esc = lambda x: str(x).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
            lab = ",".join(f'{k}="{esc(v2)}"' for k, v2 in labels.items())
            lines.append(f"{name}{{{lab}}} {v}" if lab else f"{name} {v}")

    sensors = st.get("sensors", [])
    g("fancontrol_temp_celsius", "Temperature (raw reading)",
      [({"sensor": s["id"], "name": s["name"], "group": s["group"]}, s["temp"]) for s in sensors])
    g("fancontrol_temp_avg_celsius", "Temperature (rolling average used for control)",
      [({"sensor": s["id"], "name": s["name"], "group": s["group"]}, s.get("avg")) for s in sensors])
    g("fancontrol_zone_pwm", "Zone PWM 0-255", [({"zone": str(z["id"]), "name": z["name"]}, z["pwm"]) for z in st.get("zones", [])])
    g("fancontrol_zone_rpm", "Zone tach RPM", [({"zone": str(z["id"]), "name": z["name"]}, z.get("rpm")) for z in st.get("zones", [])])
    g("fancontrol_arduino_connected", "Arduino link up", [({}, int(st.get("arduino", {}).get("connected", False)))])
    g("fancontrol_emergency", "Emergency override active", [({}, int(bool(st.get("emergency"))))])
    prof = st.get("profile", {}).get("active")
    g("fancontrol_profile_active", "Active profile", [({"profile": p}, int(p == prof)) for p in PROFILES])
    return "\n".join(lines) + "\n"


def make_handler(ctrl, cfgm):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"fancontrold/{VERSION}"

        def log_message(self, fmt, *args):
            log.debug("http %s " + fmt, self.address_string(), *args)

        def _send(self, code, body, ctype="application/json"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self):
            token = cfgm.get()["web"]["token"]
            if not token:
                return True
            given = self.headers.get("X-Auth-Token", "")
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                given = auth[7:]
            return hmac.compare_digest(given.encode(), token.encode())

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1_000_000:
                raise ValueError("body too large")
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path in ("/", "/index.html"):
                try:
                    with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
                        return self._send(200, f.read(), "text/html; charset=utf-8")
                except OSError:
                    return self._send(500, "web/index.html missing", "text/plain")
            if u.path == "/api/status":
                with ctrl.lock:
                    st = copy.deepcopy(ctrl.status)
                st["auth_required"] = bool(cfgm.get()["web"]["token"])
                return self._send(200, st)
            if u.path == "/api/history":
                minutes = float(q.get("minutes", ["60"])[0])
                points = int(q.get("points", ["600"])[0])
                cutoff = time.time() - minutes * 60
                with ctrl.lock:
                    rows = [r for r in ctrl.history if r[0] >= cutoff]
                return self._send(200, {"fields": HISTORY_FIELDS, "profiles": {"1": "normal", "2": "scrub"},
                                        "rows": downsample(rows, max(10, min(points, 5000)))})
            if u.path == "/api/config":
                cfg = cfgm.get()
                cfg["web"]["token"] = "********" if cfg["web"]["token"] else ""
                return self._send(200, cfg)
            if u.path == "/api/defaults":
                return self._send(200, DEFAULT_CONFIG)
            if u.path == "/metrics":
                with ctrl.lock:
                    st = copy.deepcopy(ctrl.status)
                return self._send(200, metrics_text(st), "text/plain; version=0.0.4")
            return self._send(404, {"error": "not found"})

        def _write_api(self, method):
            if not self._authorized():
                return self._send(401, {"error": "token required"})
            u = urlparse(self.path)
            try:
                body = self._body()
                if u.path == "/api/config" and method in ("PUT", "POST"):
                    cur = cfgm.get()
                    if body.get("web", {}).get("token") == "********":
                        body["web"]["token"] = cur["web"]["token"]
                    old, new = cfgm.update(body)
                    ctrl.on_config_change(old, new)
                    restart = old["web"]["port"] != new["web"]["port"] or old["web"]["bind"] != new["web"]["bind"]
                    return self._send(200, {"ok": True, "restart_required": restart})
                if u.path == "/api/override" and method == "POST":
                    zone = body.get("zone")
                    if zone not in (1, 2, 3, "1", "2", "3", "all"):
                        raise ValueError("zone must be 1, 2, 3 or all")
                    pwm = body.get("pwm")
                    if pwm is not None:
                        _num(pwm, "pwm", 0, 255)
                    minutes = body.get("minutes")
                    if minutes is not None:
                        _num(minutes, "minutes", 0, 100000)
                    ctrl.set_override(zone, pwm, minutes)
                    return self._send(200, {"ok": True})
                if u.path == "/api/profile" and method == "POST":
                    cfg = cfgm.get()
                    cfg["profile"]["mode"] = body.get("mode")
                    old, new = cfgm.update(cfg)
                    ctrl.wake.set()
                    return self._send(200, {"ok": True})
                if u.path == "/api/reconnect" and method == "POST":
                    ctrl.reconnect()
                    return self._send(200, {"ok": True})
            except (ValueError, KeyError, TypeError) as e:
                return self._send(400, {"error": str(e)})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            self._write_api("POST")

        def do_PUT(self):
            self._write_api("PUT")

    return Handler


# ════════════════════════════════════════════════════════════════════
# Main
# ════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="Proxmox/ZFS Arduino fan controller with web UI")
    ap.add_argument("--config", help="config file (default /etc/proxmox-fancontrol/config.json)")
    ap.add_argument("--simulate", action="store_true", help="fake sensors and Arduino (for development)")
    ap.add_argument("--port", type=int, help="override web.port")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--print-default-config", action="store_true")
    args = ap.parse_args()

    if args.print_default_config:
        print(json.dumps(DEFAULT_CONFIG, indent=2))
        return 0

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(levelname)s %(message)s" if os.environ.get("INVOCATION_ID")
                        else "%(asctime)s %(levelname)s %(message)s")

    if args.config:
        cfg_path = args.config
    elif args.simulate:
        sim_dir = os.path.join(tempfile.gettempdir(), "proxmox-fancontrol-sim")
        cfg_path = os.path.join(sim_dir, "config.json")
        if not os.path.exists(cfg_path):
            os.makedirs(sim_dir, exist_ok=True)
            c = copy.deepcopy(DEFAULT_CONFIG)
            c["history"]["file"] = os.path.join(sim_dir, "history.json")
            c["web"]["bind"] = "127.0.0.1"
            with open(cfg_path, "w") as f:
                json.dump(c, f, indent=2)
    else:
        cfg_path = "/etc/proxmox-fancontrol/config.json"

    try:
        cfgm = ConfigManager(cfg_path)
    except (ValueError, OSError) as e:
        log.error("bad config %s: %s", cfg_path, e)
        return 1
    cfg = cfgm.get()
    log.info("fancontrold %s starting (config %s%s)", VERSION, cfg_path, ", SIMULATION" if args.simulate else "")

    ctrl = Controller(cfgm, simulate=args.simulate)
    port = args.port or cfg["web"]["port"]
    httpd = ThreadingHTTPServer((cfg["web"]["bind"], port), make_handler(ctrl, cfgm))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()
    log.info("web UI on http://%s:%d/", cfg["web"]["bind"], port)

    def on_signal(signum, _frame):
        log.info("signal %d — shutting down", signum)
        ctrl.stop.set()
        ctrl.wake.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    try:
        ctrl.run()
    finally:
        httpd.shutdown()
        ctrl.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
