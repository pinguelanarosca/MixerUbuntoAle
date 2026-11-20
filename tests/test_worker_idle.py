#!/usr/bin/python3
"""Actual model worker: idle release and subsequent reactivation."""
import base64
from pathlib import Path
import sys
import time
import wave

from gi.repository import GLib

ROOT = Path.home() / '.local/share/gnome-shell/extensions/mixer-rapido@alee.local/helper'
sys.path.insert(0, str(ROOT))
from service import Inference

results = []
worker = Inference(lambda r: results.append(r), idle_seconds=2)
with wave.open(str(ROOT / 'models/speechbrain/example1.wav')) as source:
    pcm = source.readframes(source.getnframes())
job = {'id': 'idle-test', 'pcm': base64.b64encode(pcm).decode(), 'profiles': []}
context = GLib.MainContext.default()


def wait(check, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        while context.pending():
            context.iteration(False)
        if check():
            return
        time.sleep(.05)
    raise AssertionError('Timeout na liberação/reativação do worker')


try:
    assert worker.submit(job)
    wait(lambda: len(results) == 1)
    assert results[-1]['class'] == 'VOZ', results[-1]
    first = worker.proc.pid
    wait(lambda: worker.proc is None, timeout=6)
    assert worker.submit(job)
    wait(lambda: len(results) == 2)
    assert results[-1]['class'] == 'VOZ', results[-1]
    assert worker.proc.pid != first
    print('VERIFICADO: inferência real; liberação por ociosidade; novo processo e nova classificação de VOZ.')
finally:
    worker.close()
