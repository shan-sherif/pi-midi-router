#!/usr/bin/env python3
"""
Pi MIDI Router - web dashboard + JSON API.

Serves the dashboard on every network interface (default port 8080) so any
phone, tablet or computer on the same home network can open
    http://<pi-hostname>.local:8080   or   http://<pi-ip-address>:8080

Only requests from private/local network addresses are accepted.
Uses only the Python standard library apart from mido/python-rtmidi.
"""

import argparse
import glob
import ipaddress
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from router import MidoBackend, Router, ValidationError

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
MAX_BODY = 64 * 1024

log = logging.getLogger("pi-midi-router")


def client_is_local(addr):
    """True for loopback, private LAN (10/8, 172.16/12, 192.168/16, fc00::/7) and link-local."""
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_private or ip.is_link_local


class Handler(BaseHTTPRequestHandler):
    server_version = "PiMidiRouter/1.0"
    protocol_version = "HTTP/1.1"

    # Don't log every request to the journal (the dashboard polls twice a second);
    # they only show with --verbose.
    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.client_address[0], fmt % args)

    @property
    def router(self):
        return self.server.router

    # ---------------- helpers ----------------

    def _send(self, status, body=b"", ctype="application/json", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj).encode())

    def _error(self, status, message):
        self._json(status, {"error": message})

    def _allowed(self):
        if self.server.allow_any or client_is_local(self.client_address[0]):
            return True
        self._error(403, "Only devices on the local network may use the MIDI router")
        return False

    def _mutation_allowed(self):
        """Block cross-site requests: a web page on the internet can't change routes."""
        origin = self.headers.get("Origin")
        if origin:
            if urlparse(origin).netloc != self.headers.get("Host", ""):
                self._error(403, "Cross-origin request refused")
                return False
        return True

    def _consume_body(self):
        """Always read the request body first so the kept-alive connection stays in sync."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self.close_connection = True
            self._body = None
            return
        self._body = self.rfile.read(length) if length else b""

    def _read_json(self):
        if self._body is None:
            raise ValidationError("Request too large")
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ValidationError("Content-Type must be application/json")
        try:
            return json.loads(self._body.decode("utf-8") or "{}")
        except ValueError:
            raise ValidationError("Body is not valid JSON")

    # ---------------- routing ----------------

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._allowed():
            return
        url = urlparse(self.path)
        if url.path == "/api/state":
            try:
                since = int(parse_qs(url.query).get("since", ["0"])[0])
            except ValueError:
                since = 0
            state = self.router.state(since)
            state["hostname"] = self.server.hostname
            power = getattr(self.server, "power", None)
            state["power"] = power.status() if power else None
            return self._json(200, state)
        if url.path in STATIC_FILES:
            fname, ctype = STATIC_FILES[url.path]
            try:
                with open(os.path.join(STATIC_DIR, fname), "rb") as f:
                    body = f.read()
            except OSError:
                return self._error(404, "Not found")
            return self._send(200, body, ctype)
        self._error(404, "Not found")

    def do_POST(self):
        self._consume_body()
        if not (self._allowed() and self._mutation_allowed()):
            return
        path = urlparse(self.path).path
        try:
            data = self._read_json()
            if path == "/api/routes":
                return self._json(201, self.router.add_route(data))
            if path == "/api/panic":
                return self._json(200, {"sent": self.router.panic()})
            if path == "/api/alias":
                key = data.get("key")
                if not isinstance(key, str) or not key:
                    raise ValidationError("key is required")
                self.router.set_alias(key, data.get("alias", ""))
                return self._json(200, {"ok": True})
            if path == "/api/hidden-types":
                return self._json(200, {"hidden_types": self.router.set_hidden_types(data.get("types"))})
            if path == "/api/forget":
                kind = data.get("kind")
                if kind not in ("inputs", "outputs"):
                    raise ValidationError("kind must be inputs or outputs")
                self.router.forget_device(kind, str(data.get("key", "")))
                return self._json(200, {"ok": True})
        except ValidationError as e:
            return self._error(400, str(e))
        self._error(404, "Not found")

    def do_PUT(self):
        self._consume_body()
        if not (self._allowed() and self._mutation_allowed()):
            return
        path = urlparse(self.path).path
        if path.startswith("/api/routes/"):
            try:
                data = self._read_json()
                return self._json(200, self.router.update_route(path.rsplit("/", 1)[1], data))
            except ValidationError as e:
                return self._error(400, str(e))
            except KeyError:
                return self._error(404, "Route not found")
        self._error(404, "Not found")

    def do_DELETE(self):
        self._consume_body()
        if not (self._allowed() and self._mutation_allowed()):
            return
        path = urlparse(self.path).path
        if path.startswith("/api/routes/"):
            try:
                self.router.delete_route(path.rsplit("/", 1)[1])
                return self._json(200, {"ok": True})
            except KeyError:
                return self._error(404, "Route not found")
        self._error(404, "Not found")


class DualStackServer(ThreadingHTTPServer):
    """Listens on IPv6 and IPv4 at once (falls back to IPv4-only if needed)."""
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # A browser tab closing mid-request is normal; don't fill the log with it.
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)

    def server_bind(self):
        if self.address_family == socket.AF_INET6:
            try:
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            except (AttributeError, OSError):
                pass
        super().server_bind()


def make_server(host, port):
    if host in ("", "::", "0.0.0.0"):
        try:
            DualStackServer.address_family = socket.AF_INET6
            return DualStackServer(("::", port), Handler)
        except OSError:
            DualStackServer.address_family = socket.AF_INET
            return DualStackServer(("0.0.0.0", port), Handler)
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    DualStackServer.address_family = family
    return DualStackServer((host, port), Handler)


# Raspberry Pi firmware "throttled" flags - the same value `vcgencmd get_throttled` shows.
UNDERVOLT_NOW = 1 << 0
UNDERVOLT_SINCE_BOOT = 1 << 16


def read_throttled():
    """The Pi's throttled flags, or None when they can't be read (e.g. not a Pi).
    A Pi 5 has no get_throttled file in /sys, so there this goes through vcgencmd."""
    for path in glob.glob("/sys/devices/platform/soc*/soc*:firmware/get_throttled"):
        try:
            with open(path) as f:
                return int(f.read().strip(), 16)
        except (OSError, ValueError):
            pass
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                             text=True, timeout=2).stdout
        return int(out.strip().split("=", 1)[1], 16)
    except (OSError, IndexError, ValueError, subprocess.SubprocessError):
        pass
    # Last resort: the Pi's voltage sensor only knows about "now", not "since boot".
    for name_file in glob.glob("/sys/class/hwmon/hwmon*/name"):
        try:
            with open(name_file) as f:
                if f.read().strip() != "rpi_volt":
                    continue
            with open(os.path.join(os.path.dirname(name_file), "in0_lcrit_alarm")) as f:
                return UNDERVOLT_NOW if f.read().strip() == "1" else 0
        except OSError:
            pass
    return None


def power_status(flags):
    """What the dashboard shows about the power supply; None hides it."""
    if flags is None:
        return None
    return {"low_now": bool(flags & UNDERVOLT_NOW),
            "low_since_boot": bool(flags & (UNDERVOLT_NOW | UNDERVOLT_SINCE_BOOT))}


class PowerMonitor:
    """Caches the power status so a dashboard poll doesn't run vcgencmd every time."""

    def __init__(self, reader=read_throttled, ttl=5.0):
        self._reader = reader
        self._ttl = ttl
        self._lock = threading.Lock()
        self._at = None
        self._value = None

    def status(self):
        with self._lock:
            now = time.monotonic()
            if self._at is None or now - self._at >= self._ttl:
                self._value = power_status(self._reader())
                self._at = now
            return self._value


def lan_addresses():
    """Best-effort list of this machine's LAN IPv4 addresses (for the startup log)."""
    addrs = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.0.1", 9))  # no packets are sent
        addrs.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(a for a in addrs if not a.startswith("127."))


def main():
    p = argparse.ArgumentParser(description="Pi MIDI Router - MIDI routing with web dashboard")
    p.add_argument("--host", default=os.environ.get("PI_MIDI_ROUTER_HOST", "::"),
                   help="address to listen on (default: all interfaces)")
    p.add_argument("--port", type=int, default=int(os.environ.get("PI_MIDI_ROUTER_PORT", "8080")))
    p.add_argument("--config", default=os.environ.get(
        "PI_MIDI_ROUTER_CONFIG", os.path.join(HERE, "data", "config.json")))
    p.add_argument("--allow-any", action="store_true",
                   default=os.environ.get("PI_MIDI_ROUTER_ALLOW_ANY") == "1",
                   help="accept requests from non-private addresses (e.g. a VPN)")
    p.add_argument("--demo", action="store_true", help="use pretend devices (no hardware needed)")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if args.demo:
        from demo_backend import DemoBackend
        backend = DemoBackend()
        log.info("Demo mode: using pretend MIDI devices")
    else:
        try:
            backend = MidoBackend()
            import rtmidi  # noqa: F401  (mido's default backend)
        except ImportError as e:
            log.error("MIDI libraries missing: %s", e)
            log.error("Install with: sudo apt install python3-mido python3-rtmidi")
            sys.exit(1)
        try:
            backend.input_names()
        except Exception as e:
            # e.g. ALSA sequencer not ready yet; the device poller keeps retrying
            log.warning("MIDI system not ready yet (%s) - will keep trying", e)

    router = Router(backend, args.config)
    router.start()

    httpd = make_server(args.host, args.port)
    httpd.router = router
    httpd.allow_any = args.allow_any
    httpd.hostname = socket.gethostname()
    # Demo mode pretends the power is fine so the badge can be previewed.
    httpd.power = PowerMonitor(reader=lambda: 0) if args.demo else PowerMonitor()
    if httpd.power.status() is None:
        log.warning("Power status unavailable - not a Raspberry Pi, or vcgencmd can't open "
                    "/dev/vcio (the service user needs to be in the 'video' group)")
    else:
        log.info("Power status available - low-voltage warnings will show on the dashboard")

    def shutdown(*_):
        threading.Thread(target=httpd.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log.info("Dashboard listening on port %d", args.port)
    log.info("  http://%s.local:%d", httpd.hostname, args.port)
    for a in lan_addresses():
        log.info("  http://%s:%d", a, args.port)
    try:
        httpd.serve_forever()
    finally:
        router.stop()
        httpd.server_close()
        log.info("Stopped")


if __name__ == "__main__":
    main()
