"""Tests for the routing engine and web API. Run: python3 -m unittest discover tests"""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import mido  # noqa: E402

from demo_backend import DemoBackend  # noqa: E402
from router import Router, ValidationError, port_key, display_name  # noqa: E402
import server  # noqa: E402

KEYS_RAW = "KeyStep:KeyStep MIDI 1 20:0"
SYNTH_RAW = "Synth:Synth MIDI 1 24:0"
DRUMS_RAW = "Drums:Drums MIDI 1 28:0"
KEYS, SYNTH, DRUMS = "KeyStep:KeyStep MIDI 1", "Synth:Synth MIDI 1", "Drums:Drums MIDI 1"


def make(tmp, **kw):
    be = DemoBackend(inputs=[KEYS_RAW, "Midi Through:Midi Through Port-0 14:0"],
                     outputs=[SYNTH_RAW, DRUMS_RAW], generate=False)
    r = Router(be, os.path.join(tmp, "config.json"), **kw)
    r.refresh()
    return be, r


class NamingTests(unittest.TestCase):
    def test_port_key_strips_alsa_numbers(self):
        self.assertEqual(port_key("KeyStep:KeyStep MIDI 1 20:0"), "KeyStep:KeyStep MIDI 1")
        self.assertEqual(display_name("KeyStep:KeyStep MIDI 1"), "KeyStep MIDI 1")
        self.assertEqual(display_name("Foo:Bar"), "Foo - Bar")


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.be, self.r = make(self.tmp)

    def sent(self, raw):
        return self.be.open_outputs[raw].sent

    def test_ignores_midi_through(self):
        self.assertEqual(list(self.r.inputs), [KEYS])

    def test_channel_remap(self):
        self.r.add_route({"src": KEYS, "dst": SYNTH, "dst_channel": 5})
        self.r.handle(KEYS, mido.Message("note_on", channel=0, note=60, velocity=100))
        self.assertEqual(self.sent(SYNTH_RAW)[0].channel, 4)
        self.assertEqual(self.sent(DRUMS_RAW), [])

    def test_source_channel_filter(self):
        self.r.add_route({"src": KEYS, "dst": DRUMS, "src_channel": 10})
        self.r.handle(KEYS, mido.Message("note_on", channel=0, note=36))
        self.r.handle(KEYS, mido.Message("note_on", channel=9, note=36))
        self.assertEqual(len(self.sent(DRUMS_RAW)), 1)
        self.assertEqual(self.sent(DRUMS_RAW)[0].channel, 9)

    def test_type_filter_and_channelless_messages(self):
        self.r.add_route({"src": KEYS, "dst": SYNTH, "src_channel": 2, "types": ["clock", "notes"]})
        self.r.handle(KEYS, mido.Message("control_change", channel=1, control=1, value=5))
        self.r.handle(KEYS, mido.Message("clock"))     # no channel -> passes despite channel filter
        self.r.handle(KEYS, mido.Message("note_on", channel=1, note=60))
        self.assertEqual([m.type for m in self.sent(SYNTH_RAW)], ["clock", "note_on"])

    def test_fan_out_and_disable(self):
        a = self.r.add_route({"src": KEYS, "dst": SYNTH})
        self.r.add_route({"src": KEYS, "dst": DRUMS, "dst_channel": 10})
        out = self.r.handle(KEYS, mido.Message("note_on", note=60))
        self.assertEqual(sorted(out), sorted([SYNTH, DRUMS]))
        self.r.update_route(a["id"], {"enabled": False})
        self.r.handle(KEYS, mido.Message("note_on", note=61))
        self.assertEqual(len(self.sent(SYNTH_RAW)), 1)
        self.assertEqual(len(self.sent(DRUMS_RAW)), 2)

    def test_transpose(self):
        self.r.add_route({"src": KEYS, "dst": SYNTH, "transpose": 12})
        self.r.handle(KEYS, mido.Message("note_on", note=60))
        self.r.handle(KEYS, mido.Message("note_on", note=120))  # would exceed 127 -> dropped
        self.assertEqual([m.note for m in self.sent(SYNTH_RAW)], [72])

    def test_validation(self):
        for bad in [{"src": KEYS}, {"src": KEYS, "dst": SYNTH, "dst_channel": 17},
                    {"src": KEYS, "dst": SYNTH, "types": ["bogus"]},
                    {"src": "", "dst": SYNTH}]:
            with self.assertRaises(ValidationError):
                self.r.add_route(bad)

    def test_persistence(self):
        self.r.add_route({"src": KEYS, "dst": SYNTH, "src_channel": 3, "dst_channel": 4})
        self.r.set_alias(SYNTH, "Minilogue")
        be2, r2 = make(self.tmp)
        self.assertEqual(len(r2.routes), 1)
        self.assertEqual(r2.routes[0]["dst_channel"], 4)
        self.assertEqual(r2.aliases[SYNTH], "Minilogue")
        r2.handle(KEYS, mido.Message("note_on", channel=2, note=60))
        self.assertEqual(be2.open_outputs[SYNTH_RAW].sent[0].channel, 3)

    def test_hidden_types_block_and_restore(self):
        a = self.r.add_route({"src": KEYS, "dst": SYNTH, "types": ["notes", "cc"]})
        self.r.set_hidden_types(["cc", "sysex"])
        self.assertEqual(self.r.routes[0]["types"], ["notes"])
        self.r.handle(KEYS, mido.Message("control_change", control=1, value=5))
        self.assertEqual(self.sent(SYNTH_RAW), [])
        # hidden types can't be switched back on, and new routes leave them out
        self.assertEqual(self.r.update_route(a["id"], {"types": ["notes", "cc"]})["types"], ["notes"])
        self.assertNotIn("sysex", self.r.add_route({"src": KEYS, "dst": DRUMS})["types"])
        # survives a restart; showing a type again switches it back on
        be2, r2 = make(self.tmp)
        self.assertEqual(r2.state()["hidden_types"], ["cc", "sysex"])
        r2.set_hidden_types(["sysex"])
        self.assertEqual(r2.routes[0]["types"], ["notes", "cc"])
        with self.assertRaises(ValidationError):
            r2.set_hidden_types(["bogus"])

    def test_hotplug_with_new_alsa_numbers(self):
        self.r.add_route({"src": KEYS, "dst": SYNTH})
        self.be.outputs.remove(SYNTH_RAW)
        self.r.refresh()
        st = self.r.state()
        synth = [d for d in st["outputs"] if d["key"] == SYNTH][0]
        self.assertFalse(synth["online"])
        self.assertEqual(self.r.handle(KEYS, mido.Message("note_on", note=60)), [])
        # plugged back in on a different USB socket -> different ALSA numbers
        self.be.outputs.append("Synth:Synth MIDI 1 32:0")
        self.r.refresh()
        self.assertEqual(self.r.handle(KEYS, mido.Message("note_on", note=60)), [SYNTH])

    def test_panic(self):
        n = self.r.panic()
        self.assertEqual(n, 2 * 16 * 3)

    def test_monitor_skips_clock(self):
        self.r.handle(KEYS, mido.Message("clock"))
        self.r.handle(KEYS, mido.Message("note_on", note=60, velocity=90))
        mon = self.r.state()["monitor"]
        self.assertEqual(len(mon), 1)
        self.assertIn("C4", mon[0]["text"])


class PowerTests(unittest.TestCase):
    def test_flags(self):
        self.assertIsNone(server.power_status(None))
        self.assertEqual(server.power_status(0), {"low_now": False, "low_since_boot": False})
        self.assertEqual(server.power_status(0x50000), {"low_now": False, "low_since_boot": True})
        self.assertEqual(server.power_status(0x50005), {"low_now": True, "low_since_boot": True})

    def test_monitor_caches_reads(self):
        calls = []
        mon = server.PowerMonitor(reader=lambda: calls.append(1) or 0x1, ttl=60)
        self.assertTrue(mon.status()["low_now"])
        mon.status()
        self.assertEqual(len(calls), 1)


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.be, cls.router = make(cls.tmp)
        cls.httpd = server.make_server("127.0.0.1", 0)
        cls.httpd.router = cls.router
        cls.httpd.allow_any = False
        cls.httpd.hostname = "testpi"
        cls.httpd.power = server.PowerMonitor(reader=lambda: 0x50000)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        hdrs = {"Content-Type": "application/json"}
        hdrs.update(headers or {})
        c.request(method, path, None if body is None else json.dumps(body), hdrs)
        resp = c.getresponse()
        data = resp.read()
        c.close()
        try:
            return resp.status, json.loads(data)
        except ValueError:
            return resp.status, data

    def test_state_includes_power(self):
        status, st = self.req("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(st["power"], {"low_now": False, "low_since_boot": True})

    def test_dashboard_served(self):
        for path in ("/", "/app.js", "/style.css", "/favicon.svg"):
            status, _ = self.req("GET", path)
            self.assertEqual(status, 200, path)
        self.assertEqual(self.req("GET", "/../server.py")[0], 404)

    def test_route_crud(self):
        status, r = self.req("POST", "/api/routes", {"src": KEYS, "dst": DRUMS, "dst_channel": 10})
        self.assertEqual(status, 201)
        status, r2 = self.req("PUT", "/api/routes/" + r["id"], {"src_channel": 2, "types": ["notes"]})
        self.assertEqual((status, r2["src_channel"], r2["types"]), (200, 2, ["notes"]))
        status, st = self.req("GET", "/api/state")
        self.assertEqual(st["hostname"], "testpi")
        self.assertIn(r["id"], [x["id"] for x in st["routes"]])
        self.assertEqual(self.req("DELETE", "/api/routes/" + r["id"])[0], 200)
        self.assertEqual(self.req("DELETE", "/api/routes/" + r["id"])[0], 404)

    def test_bad_input_rejected(self):
        self.assertEqual(self.req("POST", "/api/routes", {"src": KEYS, "dst": SYNTH, "src_channel": 0})[0], 400)
        self.assertEqual(self.req("POST", "/api/routes", {}, {"Content-Type": "text/plain"})[0], 400)

    def test_cross_site_blocked(self):
        status, _ = self.req("POST", "/api/panic", {}, {"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, _ = self.req("POST", "/api/panic", {}, {"Origin": "http://127.0.0.1:%d" % self.port})
        self.assertEqual(status, 200)

    def test_keepalive_after_rejection(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", "/api/routes", "{}", {"Content-Type": "text/plain"})
        r = c.getresponse(); r.read()
        self.assertEqual(r.status, 400)
        c.request("GET", "/api/state")
        r = c.getresponse(); r.read()
        self.assertEqual(r.status, 200)
        c.close()

    def test_lan_only(self):
        for ok in ("127.0.0.1", "192.168.1.20", "10.0.0.5", "172.20.1.1", "::1", "fe80::1", "fd00::5",
                   "::ffff:192.168.1.9", "169.254.10.10"):
            self.assertTrue(server.client_is_local(ok), ok)
        for bad in ("8.8.8.8", "1.1.1.1", "2001:4860::1", "::ffff:8.8.8.8"):
            self.assertFalse(server.client_is_local(bad), bad)


if __name__ == "__main__":
    unittest.main()
