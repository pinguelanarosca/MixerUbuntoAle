#!/usr/bin/env python3
"""Full marker/payload playback through a real virtual PipeWire sink.

Compares a monitored Chrome-labelled stream with a non-monitored baseline.
This does not claim to test a retention buffer or physical/game A/V latency.
"""
import array
import json
import os
from pathlib import Path
import subprocess as sp
import sys
import threading
import time

import numpy as np
from scipy.signal import correlate, butter, sosfilt

sys.path.append('/usr/lib/python3/dist-packages')  # Installed Ubuntu PyGObject, same Python 3.14 ABI.
from test_pipewire import Playback, call, state, command, wait

ROOT = Path(__file__).resolve().parents[1]
RATE = 16000


def main():
    was_enabled = 'Enabled: Yes' in command('gnome-extensions', 'info', 'mixer-rapido@alee.local')
    module = None
    players = []
    recorders = []
    results = []
    try:
        command('gnome-extensions', 'disable', 'mixer-rapido@alee.local')
        module = command('pactl', 'load-module', 'module-null-sink', 'sink_name=mixer_integrity',
                         'rate=16000', 'channels=2').strip()
        rng = np.random.default_rng(73519)
        beginning = sosfilt(butter(4, 2000, fs=RATE, output='sos'), rng.uniform(-.5, .5, 1600))
        ending = sosfilt(butter(4, 2000, fs=RATE, output='sos'), rng.uniform(-.5, .5, 1600))
        middle = .25 * np.sin(2 * np.pi * 440 * np.arange(48000) / RATE)
        payload = (np.concatenate((beginning, middle, ending)) * 32767).astype('<i2')
        reference = payload.astype(np.float32) / 32768
        duration = len(reference) / RATE
        for label in ('baseline', 'monitored'):
            # The baseline has a distinct application name, so the helper ignores it.
            sink = 'mixer_integrity'
            player = Playback('MixerIntegrity-' + label, sink,
                              application_name='MixerIntegrityBaseline' if label == 'baseline' else 'Google Chrome')
            players.append(player)
            if label == 'baseline':
                time.sleep(.25)
                streams = json.loads(command('pactl', '-f', 'json', 'list', 'sink-inputs'))
                pulse_index = next(str(s['index']) for s in streams if s['properties'].get('media.name') == player.name)
                command('pactl', 'set-sink-input-volume', pulse_index, '100%')
            else:
                t = wait(lambda: next((t for t in state()['tracks'] if t['name'] == player.name), None),
                         'fluxo de continuidade descoberto')
                call('SetVolume', '(sd)', (t['serial'], 100.))
            chunks = []
            data = bytearray()
            recorder = sp.Popen(['parec', '--raw', '--rate=16000', '--channels=1', '--format=float32le',
                                 '--latency-msec=40', '--process-time-msec=10',
                                 '--device=mixer_integrity.monitor'], stdout=sp.PIPE)
            recorders.append(recorder)
            def read():
                while True:
                    block = os.read(recorder.stdout.fileno(), 16384)
                    if not block:
                        return
                    data.extend(block)
                    chunks.append((len(data) // 4, time.monotonic()))
            reader = threading.Thread(target=read, daemon=True); reader.start()
            time.sleep(.3)
            sent = time.monotonic()
            player.set(array.array('h', np.concatenate((payload, np.zeros(RATE * 6, dtype='<i2'))).tobytes()))
            time.sleep(duration + .7)
            recorder.terminate(); recorder.wait(); reader.join(timeout=2)
            recorders.remove(recorder)
            captured = np.frombuffer(bytes(data), dtype='<f4')
            corr = correlate(captured, reference, mode='valid', method='fft')
            offset = int(np.argmax(corr))
            observed = captured[offset:offset + len(reference)]
            similarity = float(np.corrcoef(observed, reference)[0, 1])
            first_offset = int(np.argmax(correlate(captured, reference[:1600], mode='valid', method='fft')))
            last_offset = int(np.argmax(correlate(captured, reference[-1600:], mode='valid', method='fft')))
            first_match = float(np.corrcoef(captured[first_offset:first_offset + 1600], reference[:1600])[0, 1])
            last_match = float(np.corrcoef(captured[last_offset:last_offset + 1600], reference[-1600:])[0, 1])
            duration_error_ms = ((last_offset - first_offset) - (len(reference) - 1600)) / RATE * 1000
            chunk_end, received = next((end, stamp) for end, stamp in chunks if end >= first_offset + 1)
            delivery_ms = (received - (chunk_end - first_offset) / RATE - sent) * 1000
            result = dict(label=label, correlation=similarity, first_marker=first_match,
                          last_marker=last_match, duration_error_ms=duration_error_ms,
                          approximate_monitor_delivery_ms=round(delivery_ms, 2),
                          payload_seconds=duration)
            results.append(result)
            print(json.dumps(result), flush=True)
            assert similarity > .99 and first_match > .95 and last_match > .95
            assert abs(duration_error_ms) <= 5
            player.close(); players.remove(player)
        (ROOT / 'test-results/continuity.json').write_text(json.dumps(results, indent=2))
        print('VERIFICADO: começo e fim presentes, payload contínuo e duração preservada no sumidouro virtual.', flush=True)
        print('NÃO VERIFICADO: retenção (desativada), saída física, jogos e sincronização audiovisual.', flush=True)
    finally:
        for recorder in recorders:
            recorder.terminate(); recorder.wait()
        for player in players:
            player.close()
        if module:
            command('pactl', 'unload-module', module)
        if was_enabled:
            command('gnome-extensions', 'enable', 'mixer-rapido@alee.local')


if __name__ == '__main__':
    main()
