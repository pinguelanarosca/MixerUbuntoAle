#!/usr/bin/python3
"""Real PipeWire integration. Sends audio exclusively to a temporary null sink.

Temporarily changes/restores mixer defaults and disables/enables the cached old UI.
Never changes the physical sink, other streams, or the narrator profiles it found.
"""
import array
import json
import math
import os
from pathlib import Path
import signal
import subprocess as sp
import sys
import threading
import time

from gi.repository import Gio, GLib

ROOT = Path(__file__).resolve().parents[1]
INSTALLED = Path.home() / '.local/share/gnome-shell/extensions/mixer-rapido@alee.local'
BUS = 'org.gnome.Shell.Extensions.MixerRapido'
PATH = '/org/gnome/Shell/Extensions/MixerRapido'
connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)


def command(*args):
    return sp.check_output(args, timeout=8, stderr=sp.STDOUT).decode()


def call(method, signature=None, args=()):
    r = connection.call_sync(BUS, PATH, BUS, method, GLib.Variant(signature, args) if signature else None,
                             None, Gio.DBusCallFlags.NONE, 5000, None)
    return r.unpack()


def state():
    return json.loads(call('GetState')[0])


def wait(check, message, timeout=12):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            result = check()
            if result:
                print('VERIFICADO:', message, flush=True)
                return result
        except GLib.Error:
            pass
        time.sleep(.1)
    print(json.dumps(state(), indent=2), flush=True)
    raise AssertionError(message)


class Playback:
    def __init__(self, name, sink, application_name='Google Chrome'):
        self.name = name
        self.data = array.array('h', [0] * 320)
        self.pos = 0
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.proc = sp.Popen(['paplay', '--raw', '--rate=16000', '--format=s16le', '--channels=1',
                             '--latency-msec=40', '--process-time-msec=10', '--device=' + sink,
                             '--client-name=' + application_name, '--stream-name=' + name,
                             '--property=application.process.binary=mixer-integration-test'], stdin=sp.PIPE)
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def loop(self):
        deadline = time.monotonic()
        while not self.stop.is_set():
            with self.lock:
                values = [self.data[(self.pos + i) % len(self.data)] for i in range(320)]
                self.pos = (self.pos + 320) % len(self.data)
            try:
                self.proc.stdin.write(array.array('h', values).tobytes())
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                break
            deadline += .02
            self.stop.wait(max(0, deadline - time.monotonic()))

    def set(self, samples):
        with self.lock:
            self.data = samples
            self.pos = 0

    def close(self):
        self.stop.set()
        self.proc.terminate()
        self.proc.wait()
        self.thread.join(timeout=2)


def pcm(path, seconds=None):
    args = ['ffmpeg', '-v', 'error', '-i', str(path)]
    if seconds:
        args += ['-t', str(seconds)]
    a = array.array('h')
    a.frombytes(sp.check_output(args + ['-ar', '16000', '-ac', '1', '-f', 's16le', '-']))
    return a


def children_matching(fragment):
    result = []
    for p in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            parts = p.read_bytes().split(b'\0')
            if parts and fragment.encode() in b' '.join(parts) and int(p.parent.name) != os.getpid():
                if parts[0] in (b'parec', str(INSTALLED / 'helper/.venv/bin/python').encode()):
                    result.append(int(p.parent.name))
        except (OSError, ProcessLookupError):
            pass
    return result


def main():
    schema = Gio.SettingsSchemaSource.new_from_directory(str(INSTALLED / 'schemas'),
                                                         Gio.SettingsSchemaSource.get_default(), False)
    settings = Gio.Settings.new_full(schema.lookup('org.gnome.shell.extensions.mixer-rapido', False), None, None)
    keys = ['voice-start-volume', 'sounds-start-volume', 'provisional-volume']
    old = {k: settings.get_value(k) for k in keys}
    old_profiles = {p['id'] for p in state()['profiles']}
    was_enabled = 'Enabled: Yes' in command('gnome-extensions', 'info', 'mixer-rapido@alee.local')
    real_before = json.loads(command('pactl', '-f', 'json', 'list', 'sink-inputs'))
    real_controls = {s['index']: (s['volume'], s['mute'], s['sink']) for s in real_before}
    players, module = [], None
    try:
        command('gnome-extensions', 'disable', 'mixer-rapido@alee.local')
        for key, value in zip(keys, [73, 17, 20]):
            settings.set_uint(key, value)
        Gio.Settings.sync()
        module = command('pactl', 'load-module', 'module-null-sink',
                         'sink_name=mixer_integration', 'rate=16000', 'channels=2',
                         'sink_properties=device.description=MixerIntegration').strip()
        older = Playback('MixerTestOlder', 'mixer_integration'); players.append(older)
        time.sleep(.3)
        newer = Playback('MixerTestNewer', 'mixer_integration'); players.append(newer)
        def track(name):
            return next((t for t in state()['tracks'] if t['name'] == name), None)
        wait(lambda: track(older.name) and track(newer.name), 'dois fluxos individuais descobertos')
        a, b = track(older.name), track(newer.name)
        ids = (a['serial'], b['serial'])
        track_numbers = (a['track'], b['track'])
        wait(lambda: abs(track(older.name)['volume'] - 17) < .1 and abs(track(newer.name)['volume'] - 17) < .1,
             'volume provisório limitado pelos padrões (17%)')
        time.sleep(.4)
        assert not track(older.name)['last_activity'] and not track(newer.name)['last_activity']
        print('VERIFICADO: criar fluxo silencioso não indica atividade', flush=True)
        tone = array.array('h', [int(20000 * math.sin(2 * math.pi * 440 * i / 16000)) for i in range(16000)])
        newer.set(tone)
        wait(lambda: state()['latest'] == ids[1], 'atividade mais recente pela primeira emissão de PCM')
        wait(lambda: track(newer.name)['class'] == 'SONS', 'sinal sonoro classificado por Silero + YAMNet')
        assert track(older.name)['last_activity'] == 0
        print('VERIFICADO: captura não mistura o fluxo silencioso com o ativo', flush=True)
        speech = pcm(INSTALLED / 'helper/models/speechbrain/example1.wav')
        older.set(speech)
        wait(lambda: state()['latest'] == ids[0], 'fluxo criado antes passa a ser o mais recente ao emitir áudio')
        wait(lambda: track(older.name)['class'] == 'VOZ', 'fala classificada como VOZ sem presumir narrador')
        wait(lambda: abs(track(older.name)['volume'] - 73) < .1, 'volume de VOZ independente (73%)')
        assert abs(track(newer.name)['volume'] - 17) < .1
        assert track(older.name)['narrator'] == 'SEM_AMOSTRA'
        call('SetVolume', '(sd)', (ids[0], 37.))
        call('SetClass', '(ss)', (ids[0], 'SONS'))
        assert abs(track(older.name)['volume'] - 37) < .1
        call('SetMute', '(sb)', (ids[0], True))
        call('SetClass', '(ss)', (ids[0], 'VOZ'))
        assert track(older.name)['mute'] and abs(track(older.name)['volume'] - 37) < .1
        call('SetMute', '(sb)', (ids[0], False))
        print('VERIFICADO: correção manual, volume e mute têm prioridade', flush=True)
        call('ResetAutomatic', '(s)', (ids[0],))
        wait(lambda: track(older.name)['identified'] and abs(track(older.name)['volume'] - 73) < .1,
             'Auto retoma classificação e padrão de VOZ')
        # Register two voices directly from live streams. No narrator audio file is given to the service.
        call('EnrollTrack', '(ss)', (ids[0], 'Teste mixer — voz A'))
        wait(lambda: not state()['enrolling'], 'cadastro da primeira voz pela própria faixa', timeout=35)
        p1 = next(p for p in state()['profiles'] if p['name'] == 'Teste mixer — voz A')
        wait(lambda: track(older.name)['narrator'] == 'COMPATÍVEL', 'voz cadastrada reconhecida no fluxo', timeout=15)
        assert track(older.name)['narrator_id'] == p1['id']
        other = pcm(ROOT / 'test-results/fixtures/other-speaker.wav')
        newer.set(other)
        call('ResetAutomatic', '(s)', (ids[1],))
        wait(lambda: track(newer.name)['class'] == 'VOZ', 'voz não cadastrada continua classificada como VOZ', timeout=15)
        assert track(newer.name)['narrator'] != 'COMPATÍVEL'
        wait(lambda: abs(track(newer.name)['volume'] - 73) < .1, 'voz não cadastrada usa o mesmo padrão de VOZ')
        call('EnrollTrack', '(ss)', (ids[1], 'Teste mixer — voz B'))
        wait(lambda: not state()['enrolling'], 'cadastro da segunda voz sem substituir a primeira', timeout=35)
        assert len({p['id'] for p in state()['profiles']} - old_profiles) == 2
        wait(lambda: track(newer.name)['narrator'] == 'COMPATÍVEL', 'segunda voz cadastrada reconhecida', timeout=15)
        call('RenameNarrator', '(ss)', (p1['id'], 'Teste mixer — renomeado'))
        assert any(p['name'] == 'Teste mixer — renomeado' for p in state()['profiles'])
        print('VERIFICADO: múltiplas vozes e renomeação persistentes', flush=True)
        # Capture child recovery while audio continues through its original links.
        old_capture = children_matching('--monitor-stream=' + ids[0])
        assert len(old_capture) == 1
        os.kill(old_capture[0], signal.SIGKILL)
        wait(lambda: children_matching('--monitor-stream=' + ids[0]) and
             children_matching('--monitor-stream=' + ids[0]) != old_capture and track(older.name)['capture'] == 'OK',
             'captura individual reiniciada automaticamente após SIGKILL')
        call('SetVolume', '(sd)', (ids[0], 41.))
        time.sleep(.3)
        before_pid = state()['pid']
        command('systemctl', '--user', 'kill', '--kill-whom=main', '--signal=KILL', 'mixer-rapido.service')
        wait(lambda: state()['pid'] != before_pid, 'serviço reiniciado automaticamente pelo systemd', timeout=15)
        assert (track(older.name)['track'], track(newer.name)['track']) == track_numbers
        assert abs(track(older.name)['volume'] - 41) < .1 and track(older.name)['manual_volume']
        assert len({p['id'] for p in state()['profiles']} - old_profiles) == 2
        assert settings.get_uint('voice-start-volume') == 73 and settings.get_uint('sounds-start-volume') == 17
        print('VERIFICADO: identidades, proteção manual, vozes e padrões preservados após falha', flush=True)
        # Fail the inference process, then request an actual new analysis.
        inference = children_matching('inference.py')
        if inference:
            os.kill(inference[0], signal.SIGKILL)
        call('ResetAutomatic', '(s)', (ids[0],))
        wait(lambda: track(older.name)['identified'] and not track(older.name).get('analysis_error'),
             'inferência recupera e classifica novamente', timeout=25)
        actual = json.loads(command('pw-dump'))
        nodes = {o['id']: o.get('info', {}).get('props', {}) for o in actual if o['type'].endswith(':Node')}
        for serial in ids:
            tid = int(next(t['node_id'] for t in state()['tracks'] if t['serial'] == serial))
            outputs = [o['info']['input-node-id'] for o in actual if o['type'].endswith(':Link')
                       and o['info']['output-node-id'] == tid]
            assert any(nodes.get(target, {}).get('media.class') == 'Audio/Sink' and
                       nodes[target].get('node.name') == 'mixer_integration' for target in outputs)
        assert not state()['buffer']['enabled']
        print('VERIFICADO: links de reprodução diretos preservados; retenção explicitamente desativada', flush=True)
        for s in json.loads(command('pactl', '-f', 'json', 'list', 'sink-inputs')):
            if s['index'] in real_controls:
                assert (s['volume'], s['mute'], s['sink']) == real_controls[s['index']]
        print('VERIFICADO: controles e saída dos fluxos preexistentes preservados', flush=True)
        (ROOT / 'test-results/pipewire-state.json').write_text(json.dumps(state(), indent=2))
        print('PIPEWIRE INTEGRATION: PASS', flush=True)
    finally:
        for p in players:
            p.close()
        if module:
            command('pactl', 'unload-module', module)
        try:
            for profile in state()['profiles']:
                if profile['id'] not in old_profiles:
                    call('RemoveNarrator', '(s)', (profile['id'],))
        finally:
            for key, value in old.items():
                settings.set_value(key, value)
            Gio.Settings.sync()
            # The caller may have disabled the cached UI before starting this test.
            if was_enabled or os.environ.get('MIXER_RESTORE_EXTENSION') == '1':
                command('gnome-extensions', 'enable', 'mixer-rapido@alee.local')


if __name__ == '__main__':
    main()
