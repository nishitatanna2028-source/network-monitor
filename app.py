"""
Real-time Network & Service Availability Monitor
------------------------------------------------
* A background thread probes every target on a fixed interval using either
  ICMP ping (via the system `ping` binary) or a TCP connect probe.
* For each target it tracks round-trip time (RTT), rolling history and
  uptime percentage.
* Flask exposes the dashboard at "/" and JSON telemetry at "/api/status".

Configuration is done with environment variables (see docker-compose.yml):
    CHECK_INTERVAL   seconds between probe rounds           (default 5)
    CHECK_TIMEOUT    per-probe timeout in seconds           (default 2)
    HISTORY_LEN      samples kept per target for charts     (default 40)
    DEGRADED_MS      RTT above this is flagged "degraded"   (default 250)
    MONITOR_TARGETS  JSON list that replaces DEFAULT_TARGETS
"""

import json
import os
import platform
import re
import socket
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
CHECK_INTERVAL = float(os.getenv("CHECK_INTERVAL", "5"))
CHECK_TIMEOUT = float(os.getenv("CHECK_TIMEOUT", "2"))
HISTORY_LEN = int(os.getenv("HISTORY_LEN", "40"))
DEGRADED_MS = float(os.getenv("DEGRADED_MS", "250"))

# Edit this list to monitor your own hosts/services.
DEFAULT_TARGETS = [
    {"name": "Cloudflare DNS", "host": "1.1.1.1", "type": "icmp"},
    {"name": "Google DNS", "host": "8.8.8.8", "type": "icmp"},
    {"name": "Google Search", "host": "www.google.com", "type": "tcp", "port": 443},
    {"name": "GitHub", "host": "github.com", "type": "tcp", "port": 443},
    {"name": "Cloudflare DNS (TCP)", "host": "1.1.1.1", "type": "tcp", "port": 53},
    # Deliberately unreachable so you can demo the "down" state.
    # Delete this line once you've shown it off.
    {"name": "Demo: offline service", "host": "10.255.255.1", "type": "tcp", "port": 8080},
]

HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-:]*[A-Za-z0-9])?$")
PING_TIME_RE = re.compile(r"time\s*[=<]\s*([\d.]+)\s*ms", re.IGNORECASE)


def load_targets():
    """Use MONITOR_TARGETS (JSON) if provided, else the defaults above."""
    raw = os.getenv("MONITOR_TARGETS")
    if not raw:
        targets = DEFAULT_TARGETS
    else:
        try:
            targets = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[monitor] MONITOR_TARGETS is not valid JSON ({exc}); using defaults")
            targets = DEFAULT_TARGETS

    clean = []
    for t in targets:
        try:
            host = str(t["host"]).strip()
            kind = str(t["type"]).lower()
            if not HOST_RE.match(host):
                raise ValueError(f"invalid host {host!r}")
            if kind not in ("icmp", "tcp"):
                raise ValueError("type must be 'icmp' or 'tcp'")
            port = None
            if kind == "tcp":
                port = int(t["port"])
                if not 1 <= port <= 65535:
                    raise ValueError("port out of range")
            clean.append(
                {
                    "id": f"{kind}-{host}-{port or 0}",
                    "name": str(t.get("name") or host),
                    "host": host,
                    "type": kind,
                    "port": port,
                }
            )
        except (KeyError, ValueError, TypeError) as exc:
            print(f"[monitor] skipping target {t!r}: {exc}")
    return clean


# --------------------------------------------------------------------------- #
# Probes – each returns (ok: bool, rtt_ms: float | None, error: str | None)
# --------------------------------------------------------------------------- #
def _ping_command(host):
    system = platform.system().lower()
    if system == "windows":
        return ["ping", "-n", "1", "-w", str(int(CHECK_TIMEOUT * 1000)), host]
    if system == "darwin":  # macOS: -W is in milliseconds
        return ["ping", "-c", "1", "-W", str(int(CHECK_TIMEOUT * 1000)), host]
    return ["ping", "-c", "1", "-W", str(max(1, int(CHECK_TIMEOUT))), host]  # Linux


def check_icmp(host):
    start = time.perf_counter()
    try:
        proc = subprocess.run(
            _ping_command(host),
            capture_output=True,
            text=True,
            timeout=CHECK_TIMEOUT + 2,
        )
    except FileNotFoundError:
        return False, None, "ping command not installed"
    except subprocess.TimeoutExpired:
        return False, None, "Request timed out"

    output = f"{proc.stdout}\n{proc.stderr}"
    match = PING_TIME_RE.search(output)
    if proc.returncode == 0 and match:
        return True, float(match.group(1)), None

    low = output.lower()
    if "operation not permitted" in low or "permission denied" in low:
        return False, None, "ICMP not permitted (needs NET_RAW)"
    if any(s in low for s in ("name or service not known", "could not find host",
                              "temporary failure in name resolution", "unknown host")):
        return False, None, "DNS lookup failed"
    if proc.returncode == 0:  # replied but RTT could not be parsed
        return True, (time.perf_counter() - start) * 1000, None
    return False, None, "No reply (timed out)"


def check_tcp(host, port):
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=CHECK_TIMEOUT):
            return True, (time.perf_counter() - start) * 1000, None
    except TimeoutError:
        return False, None, "Connection timed out"
    except ConnectionRefusedError:
        return False, None, "Connection refused"
    except socket.gaierror:
        return False, None, "DNS lookup failed"
    except OSError as exc:
        return False, None, exc.strerror or "Network error"


def run_probe(target):
    if target["type"] == "icmp":
        return check_icmp(target["host"])
    return check_tcp(target["host"], target["port"])


# --------------------------------------------------------------------------- #
# Monitor – owns the state and the background thread
# --------------------------------------------------------------------------- #
class TargetState:
    def __init__(self, cfg):
        self.cfg = cfg
        self.history = deque(maxlen=HISTORY_LEN)  # RTT in ms, or None for a failure
        self.checks = 0
        self.successes = 0
        self.rtt_sum = 0.0
        self.rtt_min = None
        self.rtt_max = None
        self.last_ok = None
        self.last_rtt = None
        self.last_error = None
        self.last_checked = None

    def record(self, ok, rtt, error):
        self.checks += 1
        self.last_ok = ok
        self.last_rtt = rtt
        self.last_error = error
        self.last_checked = datetime.now(timezone.utc)
        self.history.append(round(rtt, 2) if ok else None)
        if ok:
            self.successes += 1
            self.rtt_sum += rtt
            self.rtt_min = rtt if self.rtt_min is None else min(self.rtt_min, rtt)
            self.rtt_max = rtt if self.rtt_max is None else max(self.rtt_max, rtt)

    def status(self):
        if self.last_ok is None:
            return "pending"
        if not self.last_ok:
            return "down"
        return "degraded" if self.last_rtt > DEGRADED_MS else "up"

    def to_dict(self):
        def r(v):
            return None if v is None else round(v, 2)

        return {
            "id": self.cfg["id"],
            "name": self.cfg["name"],
            "host": self.cfg["host"],
            "type": self.cfg["type"],
            "port": self.cfg["port"],
            "status": self.status(),
            "rtt_ms": r(self.last_rtt),
            "avg_rtt_ms": r(self.rtt_sum / self.successes) if self.successes else None,
            "min_rtt_ms": r(self.rtt_min),
            "max_rtt_ms": r(self.rtt_max),
            "uptime_pct": round(100 * self.successes / self.checks, 2) if self.checks else None,
            "checks": self.checks,
            "failures": self.checks - self.successes,
            "error": self.last_error,
            "last_checked": self.last_checked.isoformat() if self.last_checked else None,
            "history": list(self.history),
        }


class Monitor:
    def __init__(self, targets):
        self.states = [TargetState(t) for t in targets]
        self._lock = threading.Lock()
        self._started = False
        self._rounds = 0

    def start(self):
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._loop, name="monitor-loop", daemon=True).start()

    def _loop(self):
        workers = max(1, len(self.states))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            while True:
                began = time.monotonic()
                # Probe all targets in parallel so one slow host can't delay the rest.
                results = list(pool.map(lambda s: run_probe(s.cfg), self.states))
                with self._lock:
                    for state, (ok, rtt, err) in zip(self.states, results):
                        state.record(ok, rtt, err)
                    self._rounds += 1
                time.sleep(max(0.0, CHECK_INTERVAL - (time.monotonic() - began)))

    def snapshot(self):
        with self._lock:
            targets = [s.to_dict() for s in self.states]
            rounds = self._rounds

        counts = {"up": 0, "degraded": 0, "down": 0, "pending": 0}
        for t in targets:
            counts[t["status"]] += 1
        live = [t["rtt_ms"] for t in targets if t["rtt_ms"] is not None]
        uptimes = [t["uptime_pct"] for t in targets if t["uptime_pct"] is not None]

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "interval_s": CHECK_INTERVAL,
            "rounds": rounds,
            "summary": {
                "total": len(targets),
                **counts,
                "avg_rtt_ms": round(sum(live) / len(live), 2) if live else None,
                "avg_uptime_pct": round(sum(uptimes) / len(uptimes), 2) if uptimes else None,
            },
            "targets": targets,
        }


# --------------------------------------------------------------------------- #
# Flask app
# --------------------------------------------------------------------------- #
app = Flask(__name__)
monitor = Monitor(load_targets())
monitor.start()  # one background thread per process (run gunicorn with 1 worker)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    response = jsonify(monitor.snapshot())
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/healthz")
def healthz():
    return {"ok": True}


if __name__ == "__main__":
    # debug=False avoids the reloader spawning a second monitoring thread.
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
