"""
Pretend MIDI devices, for trying the dashboard on a laptop without a Pi or
any MIDI hardware:   python3 server.py --demo

Requires only `mido` (pip install mido) - not python-rtmidi.
"""

import random
import threading
import time

import mido

DEMO_INPUTS = [
    "Arturia KeyStep 32:Arturia KeyStep 32 MIDI 1 20:0",
    "nanoKONTROL2:nanoKONTROL2 _ CTRL 24:0",
]
DEMO_OUTPUTS = [
    "Arturia KeyStep 32:Arturia KeyStep 32 MIDI 1 20:0",
    "minilogue xd:minilogue xd _ SOUND 28:0",
    "TR-8S:TR-8S MIDI 1 32:0",
]


class FakeInput:
    def __init__(self, name, callback, generate):
        self.name = name
        self.callback = callback
        self.closed = False
        if generate:
            threading.Thread(target=self._play, daemon=True).start()

    def _play(self):
        scale = [60, 62, 63, 65, 67, 68, 70, 72]
        is_keys = "KeyStep" in self.name
        while not self.closed:
            time.sleep(random.uniform(0.6, 2.5))
            if self.closed:
                break
            if is_keys:
                note = random.choice(scale)
                self.callback(mido.Message("note_on", note=note, velocity=random.randint(60, 120)))
                time.sleep(0.25)
                self.callback(mido.Message("note_off", note=note))
            else:
                self.callback(mido.Message("control_change", channel=0,
                                           control=random.choice([0, 1, 2, 16, 17]),
                                           value=random.randint(0, 127)))

    def close(self):
        self.closed = True


class FakeOutput:
    def __init__(self, name):
        self.name = name
        self.sent = []
        self.closed = False

    def send(self, msg):
        if self.closed:
            raise IOError("port closed")
        self.sent.append(msg)
        del self.sent[:-1000]

    def close(self):
        self.closed = True


class DemoBackend:
    def __init__(self, inputs=None, outputs=None, generate=True):
        self.inputs = list(DEMO_INPUTS if inputs is None else inputs)
        self.outputs = list(DEMO_OUTPUTS if outputs is None else outputs)
        self.generate = generate
        self.open_inputs = {}
        self.open_outputs = {}

    def input_names(self):
        return list(self.inputs)

    def output_names(self):
        return list(self.outputs)

    def open_input(self, name, callback):
        port = FakeInput(name, callback, self.generate)
        self.open_inputs[name] = port
        return port

    def open_output(self, name):
        port = FakeOutput(name)
        self.open_outputs[name] = port
        return port

    def message(self, *args, **kwargs):
        return mido.Message(*args, **kwargs)
