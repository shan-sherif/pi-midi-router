"""
MIDI routing engine.

Watches for USB MIDI devices (via ALSA, through mido + python-rtmidi),
opens every input and output it finds, and forwards messages between them
according to user-defined routes. Each route can:

  * listen on all channels or a single source channel
  * keep the channel or rewrite it to a different one
  * pass or block whole groups of message types (notes, CC, clock, ...)
  * transpose notes up or down

Routes, device nicknames and hidden message types are saved to a JSON file so
they survive reboots.
Routes refer to devices by a stable name (without the ALSA client:port
numbers), so a device can be unplugged and plugged back in - even into a
different USB socket - and its routes pick straight back up.
"""

import collections
import itertools
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid

log = logging.getLogger("pi-midi-router")

# --------------------------------------------------------------------------
# Message type groups shown as toggles in the dashboard
# --------------------------------------------------------------------------

TYPE_GROUPS = collections.OrderedDict([
    ("notes", ("note_on", "note_off")),
    ("cc", ("control_change",)),
    ("program", ("program_change",)),
    ("pitchbend", ("pitchwheel",)),
    ("aftertouch", ("aftertouch", "polytouch")),
    ("clock", ("clock", "start", "stop", "continue", "songpos", "song_select")),
    ("sysex", ("sysex",)),
    ("other", ("quarter_frame", "tune_request", "active_sensing", "reset")),
])
ALL_GROUPS = list(TYPE_GROUPS)
TYPE_TO_GROUP = {t: g for g, types in TYPE_GROUPS.items() for t in types}

# Messages that arrive many times a second and would drown the monitor log.
_NOISY = {"clock", "active_sensing"}

# ALSA port names look like "Client Name:Port Name 20:0". The trailing
# numbers change when devices are re-plugged, so we strip them.
_ALSA_SUFFIX = re.compile(r"\s+\d+:\d+$")

# Ports we never offer as devices (system ports, our own connections, and the
# PipeWire sound server that the desktop edition of Raspberry Pi OS runs).
_IGNORED_PREFIXES = ("Midi Through", "RtMidi", "PipeWire-")

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def port_key(raw_name):
    """Stable identifier for a port: the ALSA name without client:port numbers."""
    return _ALSA_SUFFIX.sub("", raw_name).strip()


def display_name(key):
    """Friendlier label: 'KeyStep 32:KeyStep 32 MIDI 1' -> 'KeyStep 32 MIDI 1'."""
    if ":" in key:
        client, port = key.split(":", 1)
        client, port = client.strip(), port.strip()
        if port.startswith(client):
            return port
        return "%s - %s" % (client, port)
    return key


def note_name(n):
    return "%s%d" % (NOTE_NAMES[n % 12], n // 12 - 1)


def describe(msg):
    """Short human-readable description of a mido message for the monitor."""
    t = msg.type
    ch = getattr(msg, "channel", None)
    chs = " ch%d" % (ch + 1) if ch is not None else ""
    if t == "note_on" and msg.velocity > 0:
        return "Note On  %-4s vel %3d%s" % (note_name(msg.note), msg.velocity, chs)
    if t in ("note_on", "note_off"):
        return "Note Off %-4s%s" % (note_name(msg.note), chs)
    if t == "control_change":
        return "CC %3d = %3d%s" % (msg.control, msg.value, chs)
    if t == "program_change":
        return "Program %d%s" % (msg.program + 1, chs)
    if t == "pitchwheel":
        return "Pitch bend %+d%s" % (msg.pitch, chs)
    if t == "aftertouch":
        return "Aftertouch %d%s" % (msg.value, chs)
    if t == "polytouch":
        return "Poly AT %s %d%s" % (note_name(msg.note), msg.value, chs)
    if t == "sysex":
        return "SysEx (%d bytes)" % (len(msg.data) + 2)
    if t == "songpos":
        return "Song position %d" % msg.pos
    return t.replace("_", " ").capitalize()


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------

class MidoBackend:
    """Real hardware via mido + python-rtmidi (ALSA sequencer on Linux)."""

    def __init__(self):
        import mido
        self.mido = mido

    def input_names(self):
        return self.mido.get_input_names()

    def output_names(self):
        return self.mido.get_output_names()

    def open_input(self, name, callback):
        return self.mido.open_input(name, callback=callback)

    def open_output(self, name):
        return self.mido.open_output(name)

    def message(self, *args, **kwargs):
        return self.mido.Message(*args, **kwargs)


# --------------------------------------------------------------------------
# Router
# --------------------------------------------------------------------------

class ValidationError(ValueError):
    pass


def _channel(value, field):
    """API uses channels 1-16 (or null for 'all'/'same')."""
    if value is None or value == "" or value == "all" or value == "same":
        return None
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise ValidationError("%s must be 1-16 or empty" % field)
    if not 1 <= v <= 16:
        raise ValidationError("%s must be 1-16 or empty" % field)
    return v


class _CompiledRoute:
    """Pre-processed route used on the hot path (inside MIDI callbacks)."""
    __slots__ = ("id", "dst", "src_ch", "dst_ch", "types", "transpose", "hits")

    def __init__(self, r):
        self.id = r["id"]
        self.dst = r["dst"]
        self.src_ch = None if r["src_channel"] is None else r["src_channel"] - 1
        self.dst_ch = None if r["dst_channel"] is None else r["dst_channel"] - 1
        self.types = frozenset(r["types"])
        self.transpose = r.get("transpose", 0)
        self.hits = 0


class Router:
    def __init__(self, backend, config_path, poll_interval=2.0):
        self.backend = backend
        self.config_path = config_path
        self.poll_interval = poll_interval

        self._lock = threading.RLock()          # guards config + port tables
        self.inputs = {}                        # key -> dict(raw, port, count)
        self.outputs = {}                       # key -> dict(raw, port, lock, count)
        self.routes = []                        # list of route dicts (API form)
        self.aliases = {}                       # key -> nickname
        self.known = {"inputs": {}, "outputs": {}}  # key -> first-seen time
        self.hidden_types = []                  # groups hidden in the UI and blocked on every route
        self._by_src = {}                       # key -> tuple(_CompiledRoute)
        self._hits = {}                         # route id -> _CompiledRoute

        self.monitor = collections.deque(maxlen=300)
        self._seq = itertools.count(1)
        self.last_seq = 0

        self._stop = threading.Event()
        self._thread = None
        self._load()
        self._compile()

    # ---------------- persistence ----------------

    def _load(self):
        if not os.path.exists(self.config_path):
            return
        try:
            with open(self.config_path) as f:
                data = json.load(f)
        except Exception as e:
            log.error("Could not read %s (%s) - starting with no routes", self.config_path, e)
            return
        hidden = data.get("hidden_types", [])
        self.hidden_types = [g for g in ALL_GROUPS if isinstance(hidden, list) and g in hidden]
        routes = []
        for r in data.get("routes", []):
            try:
                routes.append(self._validate(r, partial=False, keep_id=True))
            except ValidationError as e:
                log.warning("Skipping invalid saved route %r: %s", r, e)
        self.routes = routes
        self.aliases = {k: str(v) for k, v in data.get("aliases", {}).items() if v}
        known = data.get("known", {})
        self.known = {kind: dict(known.get(kind, {})) for kind in ("inputs", "outputs")}

    def _save(self):
        data = {
            "version": 1,
            "routes": self.routes,
            "aliases": self.aliases,
            "known": self.known,
            "hidden_types": self.hidden_types,
        }
        directory = os.path.dirname(os.path.abspath(self.config_path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".config-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.config_path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---------------- routes ----------------

    def _validate(self, data, partial, existing=None, keep_id=False):
        if not isinstance(data, dict):
            raise ValidationError("route must be a JSON object")
        r = dict(existing) if existing else {
            "id": None, "src": None, "dst": None,
            "src_channel": None, "dst_channel": None,
            "types": [g for g in ALL_GROUPS if g not in self.hidden_types], "transpose": 0, "enabled": True,
        }
        if keep_id and data.get("id"):
            r["id"] = str(data["id"])
        for field in ("src", "dst"):
            if field in data:
                v = data[field]
                if not isinstance(v, str) or not v.strip():
                    raise ValidationError("%s must be a device name" % field)
                r[field] = v.strip()
            elif not partial and not existing:
                raise ValidationError("%s is required" % field)
        if "src_channel" in data:
            r["src_channel"] = _channel(data["src_channel"], "src_channel")
        if "dst_channel" in data:
            r["dst_channel"] = _channel(data["dst_channel"], "dst_channel")
        if "types" in data:
            types = data["types"]
            if not isinstance(types, list) or any(t not in TYPE_GROUPS for t in types):
                raise ValidationError("types must be a list drawn from %s" % ", ".join(ALL_GROUPS))
            r["types"] = [g for g in ALL_GROUPS if g in types and g not in self.hidden_types]
        if "transpose" in data:
            try:
                tr = int(data["transpose"])
            except (TypeError, ValueError):
                raise ValidationError("transpose must be a whole number")
            if not -48 <= tr <= 48:
                raise ValidationError("transpose must be between -48 and 48")
            r["transpose"] = tr
        if "enabled" in data:
            r["enabled"] = bool(data["enabled"])
        if not r["id"]:
            r["id"] = uuid.uuid4().hex[:10]
        return r

    def _compile(self):
        """Rebuild the lookup table used by MIDI callbacks (atomic swap)."""
        by_src = {}
        hits = {}
        for r in self.routes:
            if not r["enabled"]:
                continue
            c = _CompiledRoute(r)
            old = self._hits.get(r["id"])
            if old is not None:
                c.hits = old.hits
            hits[r["id"]] = c
            by_src.setdefault(r["src"], []).append(c)
        self._hits = hits
        self._by_src = {k: tuple(v) for k, v in by_src.items()}

    def add_route(self, data):
        with self._lock:
            r = self._validate(data, partial=False)
            self.routes.append(r)
            self._compile()
            self._save()
            return dict(r)

    def update_route(self, route_id, data):
        with self._lock:
            for i, existing in enumerate(self.routes):
                if existing["id"] == route_id:
                    data = {k: v for k, v in data.items() if k != "id"}
                    r = self._validate(data, partial=True, existing=existing)
                    self.routes[i] = r
                    self._compile()
                    self._save()
                    return dict(r)
        raise KeyError(route_id)

    def delete_route(self, route_id):
        with self._lock:
            before = len(self.routes)
            self.routes = [r for r in self.routes if r["id"] != route_id]
            if len(self.routes) == before:
                raise KeyError(route_id)
            self._compile()
            self._save()

    def set_hidden_types(self, hidden):
        """Hide message groups from the dashboard and block them on every route.
        A group that is shown again is switched back on for every route."""
        if not isinstance(hidden, list) or any(g not in TYPE_GROUPS for g in hidden):
            raise ValidationError("hidden_types must be a list drawn from %s" % ", ".join(ALL_GROUPS))
        with self._lock:
            shown_again = [g for g in self.hidden_types if g not in hidden]
            self.hidden_types = [g for g in ALL_GROUPS if g in hidden]
            for r in self.routes:
                r["types"] = [g for g in ALL_GROUPS
                              if (g in r["types"] or g in shown_again) and g not in self.hidden_types]
            self._compile()
            self._save()
            return list(self.hidden_types)

    def set_alias(self, key, alias):
        with self._lock:
            alias = (alias or "").strip()[:40]
            if alias:
                self.aliases[key] = alias
            else:
                self.aliases.pop(key, None)
            self._save()

    def forget_device(self, kind, key):
        """Remove an offline device from the remembered list."""
        with self._lock:
            table = self.inputs if kind == "inputs" else self.outputs
            if key in table:
                raise ValidationError("device is connected")
            self.known[kind].pop(key, None)
            self._save()

    # ---------------- device management ----------------

    def _keyed(self, raw_names):
        """Map stable key -> raw ALSA name, skipping system ports, de-duplicating."""
        result = {}
        seen = set()
        for raw in raw_names:
            if raw.startswith(_IGNORED_PREFIXES):
                continue
            # mido 1.2.x lists a port that is both input and output twice under
            # the identical name; that's one port, not two devices.
            if raw in seen:
                continue
            seen.add(raw)
            key = port_key(raw)
            if key in result:  # two identical devices plugged in
                n = 2
                while "%s #%d" % (key, n) in result:
                    n += 1
                key = "%s #%d" % (key, n)
            result[key] = raw
        return result

    def refresh(self):
        """Open newly connected ports, close ones that disappeared."""
        try:
            cur_in = self._keyed(self.backend.input_names())
            cur_out = self._keyed(self.backend.output_names())
        except Exception as e:
            log.warning("Could not list MIDI ports: %s", e)
            return
        changed = False
        with self._lock:
            for key in list(self.inputs):
                if cur_in.get(key) != self.inputs[key]["raw"]:
                    self._close(self.inputs.pop(key), key, "input")
            for key in list(self.outputs):
                if cur_out.get(key) != self.outputs[key]["raw"]:
                    self._close(self.outputs.pop(key), key, "output")
            for key, raw in cur_in.items():
                if key not in self.inputs:
                    try:
                        port = self.backend.open_input(raw, self._callback_for(key))
                    except Exception as e:
                        log.warning("Could not open input %s: %s", raw, e)
                        continue
                    self.inputs[key] = {"raw": raw, "port": port, "count": 0}
                    log.info("Input connected: %s", key)
                    if key not in self.known["inputs"]:
                        self.known["inputs"][key] = time.time()
                        changed = True
            for key, raw in cur_out.items():
                if key not in self.outputs:
                    try:
                        port = self.backend.open_output(raw)
                    except Exception as e:
                        log.warning("Could not open output %s: %s", raw, e)
                        continue
                    self.outputs[key] = {"raw": raw, "port": port, "lock": threading.Lock(), "count": 0}
                    log.info("Output connected: %s", key)
                    if key not in self.known["outputs"]:
                        self.known["outputs"][key] = time.time()
                        changed = True
            if changed:
                try:
                    self._save()
                except Exception as e:
                    log.warning("Could not save config: %s", e)

    @staticmethod
    def _close(entry, key, kind):
        log.info("%s disconnected: %s", kind.capitalize(), key)
        try:
            entry["port"].close()
        except Exception:
            pass

    def _callback_for(self, key):
        def callback(msg):
            try:
                self.handle(key, msg)
            except Exception:
                log.exception("Error handling message from %s", key)
        return callback

    # ---------------- the hot path ----------------

    def handle(self, src, msg):
        """Called for every incoming message (from the rtmidi thread)."""
        inp = self.inputs.get(src)
        if inp is not None:
            inp["count"] += 1

        group = TYPE_TO_GROUP.get(msg.type, "other")
        ch = getattr(msg, "channel", None)
        delivered = []
        for r in self._by_src.get(src, ()):
            if group not in r.types:
                continue
            if ch is not None and r.src_ch is not None and ch != r.src_ch:
                continue
            out = msg
            if ch is not None and r.dst_ch is not None and r.dst_ch != ch:
                out = out.copy(channel=r.dst_ch)
            if r.transpose and out.type in ("note_on", "note_off", "polytouch"):
                note = out.note + r.transpose
                if not 0 <= note <= 127:
                    continue
                out = out.copy(note=note)
            if self._send(r.dst, out):
                r.hits += 1
                delivered.append(r.dst)

        if msg.type not in _NOISY:
            seq = next(self._seq)
            self.monitor.append({
                "seq": seq, "t": time.time(), "src": src,
                "text": describe(msg), "to": delivered,
            })
            self.last_seq = seq
        return delivered

    def _send(self, key, msg):
        out = self.outputs.get(key)
        if out is None:
            return False
        try:
            with out["lock"]:
                out["port"].send(msg)
        except Exception as e:
            log.warning("Send to %s failed: %s", key, e)
            return False
        out["count"] += 1
        return True

    def panic(self):
        """Sustain pedal off, All Sound Off and All Notes Off on every channel of every output."""
        sent = 0
        for key in list(self.outputs):
            for ch in range(16):
                for control in (64, 120, 123):  # sustain off, all sound off, all notes off
                    msg = self.backend.message("control_change", channel=ch, control=control, value=0)
                    if self._send(key, msg):
                        sent += 1
        return sent

    # ---------------- state for the dashboard ----------------

    def state(self, since=0):
        with self._lock:
            def devices(kind, live):
                keys = set(self.known[kind]) | set(live)
                for r in self.routes:
                    keys.add(r["src"] if kind == "inputs" else r["dst"])
                out = []
                for key in keys:
                    entry = live.get(key)
                    out.append({
                        "key": key,
                        "name": display_name(key),
                        "alias": self.aliases.get(key, ""),
                        "online": entry is not None,
                        "count": entry["count"] if entry else 0,
                    })
                out.sort(key=lambda d: (not d["online"], (d["alias"] or d["name"]).lower()))
                return out

            routes = []
            for r in self.routes:
                c = self._hits.get(r["id"])
                routes.append(dict(r, hits=c.hits if c else 0))
            monitor = [m for m in list(self.monitor) if m["seq"] > since]
            return {
                "inputs": devices("inputs", self.inputs),
                "outputs": devices("outputs", self.outputs),
                "routes": routes,
                "monitor": monitor[-200:],
                "seq": self.last_seq,
                "types": ALL_GROUPS,
                "hidden_types": list(self.hidden_types),
            }

    # ---------------- background polling ----------------

    def start(self):
        self.refresh()
        self._thread = threading.Thread(target=self._run, name="device-poll", daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.wait(self.poll_interval):
            self.refresh()

    def stop(self):
        self._stop.set()
        with self._lock:
            for key, entry in list(self.inputs.items()):
                self._close(entry, key, "input")
            for key, entry in list(self.outputs.items()):
                self._close(entry, key, "output")
            self.inputs.clear()
            self.outputs.clear()
