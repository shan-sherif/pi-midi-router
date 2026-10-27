"""
Device discovery through the real mido backend, with python-rtmidi replaced by a
stand-in that reports ports exactly as ALSA does on the Pi. Guards against
mido listing one bidirectional port as two devices.
"""
import os
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

PORTS = [
    "Midi Through:Midi Through Port-0 14:0",
    "Melodee FM-1:Melodee FM-1 MIDI 1 24:0",       # one port, both in and out
    "PipeWire-System:input 142:0",
    "PipeWire-RT-Event:input 143:0",
]


def _fake_rtmidi():
    m = types.ModuleType("rtmidi")
    m.API_UNSPECIFIED, m.API_LINUX_ALSA = 0, 2
    m.get_compiled_api = lambda: [2]
    m.get_api_name = lambda api: "alsa"
    m.get_api_display_name = lambda api: "ALSA"
    m.get_rtmidi_version = lambda: "6.0.0"

    class _Port:
        def __init__(self, *a, **k):
            self._open = False

        def get_ports(self):
            return list(PORTS)

        def get_port_count(self):
            return len(PORTS)

        def get_port_name(self, i, *a):
            return PORTS[i]

        def get_current_api(self):
            return 2

        def open_port(self, *a, **k):
            self._open = True

        def is_port_open(self):
            return self._open

        def close_port(self):
            self._open = False

        def __getattr__(self, name):  # set_callback, ignore_types, send_message, ...
            return lambda *a, **k: None

    m.MidiIn = type("MidiIn", (_Port,), {})
    m.MidiOut = type("MidiOut", (_Port,), {})
    return m


class MidoListingTests(unittest.TestCase):
    def setUp(self):
        self.fake = _fake_rtmidi()
        self._saved = sys.modules.get("rtmidi")
        sys.modules["rtmidi"] = self.fake
        import mido.backends.rtmidi as rb
        self.rb = rb
        self._saved_attr = getattr(rb, "rtmidi", None)
        rb.rtmidi = self.fake
        from router import MidoBackend, Router
        self.tmp = tempfile.mkdtemp()
        self.Router = Router
        self.backend = MidoBackend()

    def tearDown(self):
        self.rb.rtmidi = self._saved_attr
        if self._saved is None:
            sys.modules.pop("rtmidi", None)
        else:
            sys.modules["rtmidi"] = self._saved

    def test_bidirectional_port_listed_once(self):
        r = self.Router(self.backend, os.path.join(self.tmp, "c.json"))
        keyed_in = r._keyed(self.backend.input_names())
        keyed_out = r._keyed(self.backend.output_names())
        self.assertEqual(list(keyed_in), ["Melodee FM-1:Melodee FM-1 MIDI 1"])
        self.assertEqual(list(keyed_out), ["Melodee FM-1:Melodee FM-1 MIDI 1"])


if __name__ == "__main__":
    unittest.main()
