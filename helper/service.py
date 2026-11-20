#!/usr/bin/python3
"""GNOME companion. Captures copies of Chrome output; never moves audio links."""
import array
import base64
import json
import math
import mmap
import os
from pathlib import Path
import queue
import select
import signal
import subprocess
import sys
import threading
import time
import uuid

import gi
from gi.repository import Gio, GLib, GLibUnix

from core import Activity, CLASSES, RATE, safe_volume, should_apply

ROOT = Path(__file__).resolve().parent
RUNTIME = Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')) / 'mixer-rapido'
CONFIG = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'mixer-rapido'
BUFFER = Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')) / 'mixer-buffer'
BUS = 'org.gnome.Shell.Extensions.MixerRapido'
OBJECT = '/org/gnome/Shell/Extensions/MixerRapido'
XML = '''<node><interface name="org.gnome.Shell.Extensions.MixerRapido">
<method name="GetState"><arg type="s" direction="out"/></method>
<method name="SetVolume"><arg type="s" direction="in"/><arg type="d" direction="in"/></method>
<method name="SetMute"><arg type="s" direction="in"/><arg type="b" direction="in"/></method>
<method name="SetClass"><arg type="s" direction="in"/><arg type="s" direction="in"/></method>
<method name="ResetAutomatic"><arg type="s" direction="in"/></method>
<method name="EnrollTrack"><arg type="s" direction="in"/><arg type="s" direction="in"/></method>
<method name="CancelEnrollment"/>
<method name="RemoveNarrator"><arg type="s" direction="in"/></method>
<method name="RenameNarrator"><arg type="s" direction="in"/><arg type="s" direction="in"/></method>
<signal name="StateChanged"><arg type="s"/></signal>
</interface></node>'''


def run(*args):
    return subprocess.check_output(args, timeout=3, stderr=subprocess.PIPE)


def atomic_json(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False))
    tmp.chmod(0o600)
    tmp.replace(path)


def chrome(props):
    name = str(props.get('application.name', '')).lower()
    binary = str(props.get('application.process.binary', '')).lower()
    appid = str(props.get('application.id', '')).lower()
    return name in ('google chrome', 'chromium', 'chromium-browser', 'google-chrome') or \
        binary in ('chrome', 'chromium', 'chromium-browser', 'google-chrome') or \
        appid in ('com.google.chrome', 'org.chromium.chromium')


class Inference:
    """One worker shared across tracks; failures cannot block the GLib loop."""
    def __init__(self, callback, idle_seconds=60, worker='inference.py'):
        self.callback = callback
        self.jobs = queue.Queue(maxsize=8)
        self.proc = None
        self.stopped = threading.Event()
        self.idle_seconds = idle_seconds
        self.worker = worker
        self.last_job = time.monotonic()
        threading.Thread(target=self.loop, daemon=True).start()

    def submit(self, job):
        try:
            self.jobs.put_nowait(job)
            return True
        except queue.Full:
            return False

    def loop(self):
        while not self.stopped.is_set():
            try:
                job = self.jobs.get(timeout=.5)
            except queue.Empty:
                if self.proc is not None and self.idle_seconds is not None and time.monotonic() - self.last_job >= self.idle_seconds:
                    proc, self.proc = self.proc, None
                    if proc.poll() is None:
                        proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    proc.stdin.close()
                    proc.stdout.close()
                continue
            try:
                if self.proc is None or self.proc.poll() is not None:
                    self.proc = subprocess.Popen([str(ROOT / '.venv/bin/python'), str(ROOT / self.worker)],
                                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                 text=True, bufsize=1)
                self.proc.stdin.write(json.dumps(job) + '\n')
                self.proc.stdin.flush()
                if not select.select([self.proc.stdout], [], [], 30)[0]:
                    raise TimeoutError('Inferência excedeu 30 segundos')
                line = self.proc.stdout.readline()
                if not line:
                    raise RuntimeError('O processo de inferência terminou')
                result = json.loads(line)
            except Exception as e:
                result = {'id': job['id'], 'error': str(e)}
                if self.proc is not None:
                    if self.proc.poll() is None:
                        self.proc.kill()
                    self.proc.wait()
                    self.proc.stdin.close()
                    self.proc.stdout.close()
                    self.proc = None
            self.last_job = time.monotonic()
            GLib.idle_add(self.callback, result)

    def close(self):
        self.stopped.set()
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()


class Capture:
    def __init__(self, service, track):
        self.service, self.track = service, track
        self.tail = b''
        self.pcm = bytearray()
        self.activity = Activity(last_onset=track.get('last_activity', 0))
        self.started = time.monotonic()
        # PipeWire-Pulse implements per-sink-input monitoring. The native pw-cat
        # stream-output tap produced distorted analysis PCM on this installed runtime.
        self.proc = subprocess.Popen([
            'parec', '--record', '--raw', '--rate=16000', '--channels=2', '--format=float32le',
            '--latency-msec=40', '--process-time-msec=10',
            '--monitor-stream=' + track['pulse_index'], '--device=' + track['monitor'],
            '--property=application.name=Mixer Rápido · análise',
            '--property=application.id=org.gnome.Shell.Extensions.MixerRapido'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        os.set_blocking(self.proc.stdout.fileno(), False)
        self.watch = GLib.io_add_watch(self.proc.stdout.fileno(), GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR,
                                      self.read)

    def read(self, fd, condition):
        try:
            data = os.read(fd, 16384)
        except BlockingIOError:
            return True
        if not data:
            self.watch = 0
            self.track['capture'] = 'RECONECTANDO'
            self.service.abort_enrollment(self.track['serial'], 'Captura interrompida; cadastre novamente.')
            self.service.changed()
            return False
        return self.consume(data)

    def consume(self, data, timestamp=None):
        data = self.tail + data
        block = 2560  # 20 ms, stereo float32 at 16 kHz; preserve quiet samples.
        count = len(data) // block
        now = time.monotonic() if timestamp is None else timestamp
        for n in range(count):
            samples = array.array('f')
            samples.frombytes(data[n * block:(n + 1) * block])
            left, right = samples[::2], samples[1::2]
            channel = 0 if sum(x*x for x in left) >= sum(x*x for x in right) else 1
            selected = left if channel == 0 else right
            gains = self.track.get('channel_gains', [1., 1.])
            gain = gains[min(channel, len(gains) - 1)]
            # PipeWire's stream output includes PulseAudio's cubic volume gain.
            # Undo it only in the analysis copy; physical playback is untouched.
            inverse = 1 / gain if gain > 1e-12 else 1
            a = array.array('h', [round(max(-1., min(1., x * inverse)) * 32767)
                                   if math.isfinite(x) else 0 for x in selected])
            rms = math.sqrt(sum(x*x for x in a) / len(a)) / 32768
            peak = max(map(abs, a), default=0) / 32768
            stamp = now - (count - n - 1) * .020
            if self.activity.feed(rms, peak, stamp):
                self.track['last_activity'] = stamp
                if not self.track.get('first_activity'):
                    self.track['first_activity'] = stamp
                self.service.changed()
            if self.track['active'] != self.activity.active:
                self.service.changed()
            self.track['active'] = self.activity.active
            self.track['capture'] = 'OK'
            self.track['rms'] = round(rms, 5)
            self.track['samples_seen'] = self.track.get('samples_seen', 0) + len(a)
            self.service.record(self.track, a.tobytes())
            # Audio in RAM is exclusively for analysis, never a playback buffer.
            if self.service.needs_analysis(self.track) and not self.track.get('override'):
                if self.activity.active:
                    self.pcm.extend(a.tobytes())
                    del self.pcm[:-RATE * 2 * 4]
                self.service.maybe_infer(self.track, self.pcm)
                self.service.maybe_fast_vad(self.track, self.pcm)
        self.tail = data[count * block:]
        return True

    def alive(self):
        return self.proc.poll() is None and bool(self.watch)

    def close(self):
        if self.watch:
            GLib.source_remove(self.watch)
            self.watch = 0
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.proc.stdout.close()


class BufferedCapture(Capture):
    """Analysis copy from the native pre-output ring, independent of playback."""
    def __init__(self, service, track):
        self.service, self.track = service, track
        self.tail = b''
        self.pcm = bytearray()
        self.activity = Activity(last_onset=track.get('last_activity', 0))
        self.started = time.monotonic()
        self.mapping = None
        self.cursor = 0
        self.proc = None
        self.watch = GLib.timeout_add(20, self.poll)

    def poll(self):
        try:
            status = json.loads((BUFFER / f"status-{self.track['serial']}.json").read_text())
            if status['cookie'] != self.service.server_cookie:
                return True
            if self.mapping is None:
                with (BUFFER / f"audio-{self.track['serial']}.shm").open('rb') as f:
                    self.mapping = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
                self.cursor = max(0, status['write'] - 48000 * 4)
                self.cursor -= self.cursor % 960
            w, rate = status['write'], status['rate']
            if rate != 48000:
                self.track['capture'] = f'ERRO: taxa de grafo não suportada: {rate}'
                return True
            capacity = 48000 * 32
            self.cursor = max(self.cursor, w - capacity + 960)
            while w - self.cursor >= 960:
                start = self.cursor % capacity
                if start + 960 <= capacity:
                    data = self.mapping[4096 + start*8:4096 + (start+960)*8]
                else:
                    data = self.mapping[4096 + start*8:4096 + capacity*8] + self.mapping[4096:4096+(960-capacity+start)*8]
                raw = array.array('f'); raw.frombytes(data)
                # The graph is 48 kHz; only the analysis copy is sampled at 16 kHz.
                stereo = array.array('f', (raw[i+c] for i in range(0,len(raw),6) for c in (0,1)))
                end = (status['last_process_ns']/1e9 + status['quantum']/rate
                       - (w-self.cursor-960)/rate)
                self.consume(stereo.tobytes(), timestamp=end)
                self.cursor += 960
            self.track['buffer_status'] = status
        except (OSError, ValueError, KeyError):
            self.track['capture'] = 'AGUARDANDO_BUFFER'
        return True

    def alive(self):
        return bool(self.watch)

    def close(self):
        if self.watch:
            GLib.source_remove(self.watch); self.watch = 0
        if self.mapping is not None:
            self.mapping.close()


class Service:
    def __init__(self):
        RUNTIME.mkdir(exist_ok=True, mode=0o700)
        source = Gio.SettingsSchemaSource.new_from_directory(str(ROOT.parent / 'schemas'),
                                                             Gio.SettingsSchemaSource.get_default(), False)
        self.settings = Gio.Settings.new_full(source.lookup('org.gnome.shell.extensions.mixer-rapido', False),
                                             None, None)
        if not self.settings.get_boolean('classification-migrated'):
            old = self.settings.get_uint('chrome-start-volume')
            self.settings.delay()
            self.settings.set_uint('voice-start-volume', old)
            self.settings.set_uint('sounds-start-volume', old)
            self.settings.set_boolean('classification-migrated', True)
            self.settings.apply()
        self.tracks, self.captures = {}, {}
        self.monitors = {}
        self.next_track = 1
        self.dirty = True
        self.error = ''
        self.enrollment = ''
        self.enrolling = False
        self.enroll_target = None
        self.record_pcm = bytearray()
        CONFIG.mkdir(exist_ok=True, parents=True, mode=0o700)
        self.profiles = self.load_profiles()
        self.started = time.monotonic()
        self.server_cookie = self.get_cookie()
        self.buffer_policy = None
        self.update_buffer_policy()
        self.inference = Inference(self.result)
        self.fast_vad = Inference(self.fast_result, idle_seconds=None, worker='fast_vad.py')
        self.fast_vad.submit({'id': 'warm', 'op': 'warm'})
        self.connection = None
        self.previous = {}
        try:
            saved = json.loads((RUNTIME / 'state.json').read_text())
            if saved.get('server_cookie') == self.server_cookie:
                self.previous = {t['serial']: t for t in saved['tracks']}
                if saved.get('analysis_version') != 2:
                    for t in self.previous.values():
                        if not t.get('override'):
                            t.update(identified=False, attempts=0, pending=False)
            self.next_track = max([t['track'] for t in self.previous.values()] + [0]) + 1
        except (OSError, ValueError, KeyError):
            pass
        self.refresh(initial=True)
        self.subscription = None
        self.subscription_watch = 0
        self.refresh_pending = 0
        self.subscribe()
        self.settings.connect('changed', lambda *_: (self.changed(), self.update_buffer_policy()))
        self.timer = GLib.timeout_add(1000, self.tick)
        self.emit_timer = GLib.timeout_add(150, self.publish)
        self.owner = Gio.bus_own_name(Gio.BusType.SESSION, BUS, Gio.BusNameOwnerFlags.NONE,
                                      self.bus_acquired, None, None)

    def bus_acquired(self, connection, _name):
        self.connection = connection
        self.registration = connection.register_object(OBJECT, Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0],
                                                        self.method, None, None)
        self.changed()

    def method(self, _connection, _sender, _object, _interface, method, params, invocation):
        try:
            args = params.unpack()
            if method == 'GetState':
                invocation.return_value(GLib.Variant('(s)', (json.dumps(self.state(), ensure_ascii=False),)))
                return
            if method == 'EnrollTrack':
                if self.enrolling:
                    raise ValueError('Já existe um cadastro em andamento.')
                t = self.tracks.get(args[0])
                if not t or t['capture'] != 'OK':
                    raise ValueError('A captura desta faixa está indisponível.')
                if len(self.profiles) >= 32:
                    raise ValueError('Limite de 32 vozes; remova uma amostra antes de cadastrar outra.')
                self.current(t)
                name = args[1].strip()[:80] or f'Narrador {len(self.profiles) + 1}'
                self.enroll_target = {'id': uuid.uuid4().hex, 'name': name, 'serial': t['serial'],
                                      'started': time.monotonic(), 'submitted': False}
                self.record_pcm.clear()
                self.enrolling, self.enrollment = True, 'Capturando 10 s desta faixa…'
            elif method == 'CancelEnrollment':
                if self.enroll_target and self.enroll_target['submitted']:
                    raise ValueError('A amostra já está sendo processada; aguarde.')
                if self.enroll_target:
                    self.abort_enrollment(self.enroll_target['serial'], 'Cadastro cancelado')
            elif method == 'RemoveNarrator':
                if self.enrolling:
                    raise ValueError('Aguarde o cadastro em andamento.')
                profile = next((p for p in self.profiles if p['id'] == args[0]), None)
                if not profile:
                    raise ValueError('Amostra inexistente.')
                self.profiles.remove(profile)
                self.save_profiles()
                (CONFIG / 'voices' / (profile['id'] + '.npy')).unlink(missing_ok=True)
                self.enrollment = 'Voz removida'
                self.reidentify()
            elif method == 'RenameNarrator':
                profile = next((p for p in self.profiles if p['id'] == args[0]), None)
                if not profile or not args[1].strip():
                    raise ValueError('Informe o nome de uma voz cadastrada.')
                profile['name'] = args[1].strip()[:80]
                self.save_profiles()
                for t in self.tracks.values():
                    if t.get('narrator_id') == profile['id']:
                        t['narrator_name'] = profile['name']
            else:
                t = self.tracks.get(args[0])
                if not t:
                    raise ValueError('O fluxo não existe mais.')
                self.current(t)
                if method == 'SetVolume':
                    if not math.isfinite(args[1]) or not 0 <= args[1] <= 150:
                        raise ValueError('Volume fora do intervalo permitido.')
                    t['manual_volume'] = True
                    self.set_volume(t, args[1])
                elif method == 'SetMute':
                    t['manual_volume'] = True
                    t['mute'] = args[1]
                    if t.get('buffered'):
                        self.set_volume(t, t['volume'])
                    else:
                        run('pactl', 'set-sink-input-mute', t['pulse_index'], str(int(args[1])))
                elif method == 'SetClass':
                    if args[1] not in CLASSES:
                        raise ValueError('Classificação inválida.')
                    t.update(override=args[1], **{'class': args[1]}, confidence=1., identified=True)
                    if args[1] != 'VOZ':
                        t.update(narrator='NÃO_SE_APLICA')
                        t.pop('similarity', None)
                    self.apply_class(t)
                elif method == 'ResetAutomatic':
                    t.update(override='', manual_volume=False, manage_volume=True,
                             identified=False, attempts=0, pending=False, confidence=0., **{'class': 'INDEFINIDO'})
                    t['generation'] += 1
                    t.update(fast_voice=False, fast_pending=False, fast_last_samples=0, speaker_checked=False)
                    if t['serial'] in self.captures:
                        self.captures[t['serial']].pcm.clear()
                else:
                    raise ValueError('Método desconhecido.')
            self.changed()
            invocation.return_value(None)
        except Exception as e:
            invocation.return_dbus_error(BUS + '.Error', str(e))

    @staticmethod
    def load_profiles():
        try:
            profiles = json.loads((CONFIG / 'voices.json').read_text())
            return [p for p in profiles if len(p['id']) == 32 and all(c in '0123456789abcdef' for c in p['id'])
                    and (CONFIG / 'voices' / (p['id'] + '.npy')).is_file()]
        except (OSError, ValueError, KeyError, TypeError):
            return []

    def save_profiles(self):
        atomic_json(CONFIG / 'voices.json', self.profiles)

    def reidentify(self):
        for t in self.tracks.values():
            t.update(narrator='INCERTO' if self.profiles else 'SEM_AMOSTRA')
            for key in ('similarity', 'narrator_name', 'narrator_id'):
                t.pop(key, None)
            if not t.get('override'):
                t.update(identified=False, attempts=0, pending=False, speaker_checked=False,
                         fast_pending=False, fast_last_samples=0, fast_voice=False)
                t['generation'] += 1
                if t['serial'] in self.captures:
                    self.captures[t['serial']].pcm.clear()

    def record(self, t, pcm):
        target = self.enroll_target
        if not target or target['serial'] != t['serial'] or target['submitted']:
            return
        self.record_pcm.extend(pcm)
        if len(self.record_pcm) >= RATE * 2 * 10:
            job = {'id': 'enroll:' + target['id'], 'op': 'enroll',
                   'pcm': base64.b64encode(self.record_pcm[:RATE * 2 * 10]).decode(),
                   'profile': str(CONFIG / 'voices' / (target['id'] + '.npy'))}
            if self.inference.submit(job):
                target['submitted'] = True
                self.enrollment = 'Processando voz capturada…'
            else:
                self.abort_enrollment(t['serial'], 'Fila de análise ocupada; tente novamente.')
            self.record_pcm.clear()
            self.changed()

    def abort_enrollment(self, serial, reason):
        if self.enroll_target and self.enroll_target['serial'] == serial and not self.enroll_target['submitted']:
            self.enroll_target = None
            self.enrolling = False
            self.record_pcm.clear()
            self.enrollment = reason
            self.changed()

    def subscribe(self):
        self.subscription = subprocess.Popen(['pactl', 'subscribe'], stdout=subprocess.PIPE,
                                              stderr=subprocess.DEVNULL)
        os.set_blocking(self.subscription.stdout.fileno(), False)
        def event(fd, condition):
            try:
                data = os.read(fd, 65536)
            except BlockingIOError:
                return True
            if not data:
                self.subscription_watch = 0
                return False
            if b'sink-input' in data and not self.refresh_pending:
                self.refresh_pending = GLib.timeout_add(60, self.scheduled_refresh)
            return True
        self.subscription_watch = GLib.io_add_watch(self.subscription.stdout.fileno(),
                                                     GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, event)

    def scheduled_refresh(self):
        self.refresh_pending = 0
        self.refresh()
        return False

    def refresh(self, initial=False):
        try:
            streams = json.loads(run('pactl', '-f', 'json', 'list', 'sink-inputs'))
            self.monitors = {str(s['index']): s['monitor_source']
                             for s in json.loads(run('pactl', '-f', 'json', 'list', 'sinks'))}
        except Exception as e:
            self.error = 'Servidor de áudio indisponível: ' + str(e)
            self.changed()
            return
        if self.error:
            cookie = self.get_cookie()
            if cookie != self.server_cookie:
                for capture in self.captures.values():
                    capture.close()
                self.tracks.clear()
                self.captures.clear()
                self.previous.clear()
                self.server_cookie = cookie
                initial = True
        self.error = ''
        present = set()
        for stream in streams:
            p = stream['properties']
            if not chrome(p):
                continue
            serial = str(p['object.serial'])
            present.add(serial)
            volume = sum(v['value'] for v in stream['volume'].values()) / len(stream['volume']) / 655.36
            source_volume = volume
            native = None
            expected_buffer = self.expects_buffer(serial)
            try:
                native = json.loads((BUFFER / f'status-{serial}.json').read_text())
                if native['cookie'] != self.server_cookie:
                    native = None
            except (OSError, ValueError, KeyError):
                pass
            if native:
                volume = native['volume']
            if serial not in self.tracks:
                saved = self.previous.get(serial)
                # Serial plus server object identity prevents accidental reuse on restart.
                if saved and saved.get('process_id') == p.get('application.process.id') \
                        and saved.get('node_id') == str(p['object.id']):
                    t = saved.copy()
                    t['pending'] = False
                    t.update(fast_pending=False, fast_last_samples=0)
                    t['generation'] = t.get('generation', 0) + 1
                    if abs(volume - saved.get('expected_volume', volume)) > .15 or stream['mute'] != saved['mute']:
                        t['manual_volume'] = True
                else:
                    t = {'serial': serial, 'track': self.next_track, 'class': 'INDEFINIDO',
                         'confidence': 0., 'last_activity': 0., 'first_activity': 0.,
                         'override': '', 'manual_volume': (native['decision'] >= 4 if native else False if expected_buffer else initial), 'manage_volume': expected_buffer or bool(native) or not initial,
                         'identified': False, 'attempts': 0, 'pending': False, 'generation': 0,
                         'narrator': 'SEM_AMOSTRA', 'capture': 'INICIANDO', 'active': False,
                         'volume': volume, 'mute': stream['mute']}
                    self.next_track += 1
                t.update(pulse_index=str(stream['index']), process_id=p.get('application.process.id'),
                         node_id=str(p['object.id']), created=time.monotonic(), volume=volume,
                         expected_volume=volume, mute=stream['mute'], name=p.get('media.name', 'Chrome'))
                t.update(buffered=bool(native) or expected_buffer, source_volume=source_volume)
                if native:
                    t.update(mute=native['mute'], buffer_status=native)
                t['monitor'] = self.monitors.get(str(stream['sink']), '')
                t['channel_gains'] = [(v['value'] / 65536.) ** 3 for v in stream['volume'].values()]
                self.tracks[serial] = t
                self.start_capture(t)
                if not initial and not saved and not native and not expected_buffer:
                    GLib.timeout_add(150, self.initial_volume, serial)
                self.changed()
            t = self.tracks[serial]
            if native and not t.get('buffered'):
                old = self.captures.pop(serial, None)
                if old: old.close()
                t.update(buffered=True, manage_volume=True, manual_volume=False)
                self.start_capture(t)
            if native:
                # The engine sets unity once before activating a new source.
                # This is transport setup; subsequent external changes are manual.
                first_native = not t.get('buffer_status')
                if abs(source_volume - t.get('source_volume', source_volume)) > .15 and not (first_native and abs(source_volume - 100) < .15):
                    t['manual_volume'] = True
                t.update(volume=volume, source_volume=source_volume, mute=native['mute'], buffer_status=native,
                         expected_volume=volume,
                         channel_gains=[(v['value']/65536.)**3 for v in stream['volume'].values()])
                if native['decision'] >= 4:
                    t['manual_volume'] = True
                self.changed()
                continue
            if t.get('buffered'):
                # No volume decision on the upstream node while native setup runs.
                continue
            monitor = self.monitors.get(str(stream['sink']), '')
            if t.get('monitor') != monitor:
                self.abort_enrollment(serial, 'Saída de áudio alterada durante o cadastro; tente novamente.')
                capture = self.captures.pop(serial, None)
                if capture:
                    capture.close()
                t['monitor'] = monitor
                self.start_capture(t)
            # Detect adjustments from GNOME Settings, wpctl, Chrome, etc.
            if abs(volume - t.get('expected_volume', volume)) > .15 or stream['mute'] != t['mute']:
                t['manual_volume'] = True
            if abs(t['volume'] - volume) > .05 or t['mute'] != stream['mute']:
                self.changed()
            t.update(volume=volume, expected_volume=volume, mute=stream['mute'],
                     channel_gains=[(v['value'] / 65536.) ** 3 for v in stream['volume'].values()])
        for serial in set(self.tracks) - present:
            self.abort_enrollment(serial, 'Faixa encerrada durante o cadastro; tente novamente.')
            capture = self.captures.pop(serial, None)
            if capture:
                capture.close()
            del self.tracks[serial]
            self.previous.pop(serial, None)
            self.changed()

    def start_capture(self, t):
        try:
            if t.get('buffered'):
                self.captures[t['serial']] = BufferedCapture(self, t)
                return
            if not t.get('monitor'):
                raise RuntimeError('Aguardando o monitor da saída deste fluxo')
            self.captures[t['serial']] = Capture(self, t)
        except Exception as e:
            t['capture'] = 'ERRO: ' + str(e)
            self.changed()

    def initial_volume(self, serial):
        t = self.tracks.get(serial)
        if t and should_apply(t) and not t.get('buffered'):
            self.set_volume(t, safe_volume(self.settings.get_uint('voice-start-volume'),
                                           self.settings.get_uint('sounds-start-volume'),
                                           self.settings.get_uint('provisional-volume')), automatic=True)
        return False

    @staticmethod
    def get_cookie():
        try:
            objects = json.loads(run('pw-dump', '0'))
            return next(o['info']['cookie'] for o in objects if o['id'] == 0)
        except Exception:
            return None

    def current(self, t):
        # Resolve the exact live serial before issuing a destructive-to-state control.
        streams = json.loads(run('pactl', '-f', 'json', 'list', 'sink-inputs'))
        current = next((s for s in streams if str(s['index']) == t['pulse_index'] and
                        str(s['properties'].get('object.serial')) == t['serial']), None)
        if current is None:
            raise RuntimeError('O fluxo foi encerrado.')
        return current

    def set_volume(self, t, volume, automatic=False):
        if t.get('buffered'):
            if automatic and not should_apply(t):
                return
            self.current(t)
            atomic_json(BUFFER / f"control-{t['serial']}.json", {
                'volume': volume, 'decision': (2 if t['class'] == 'VOZ' else 3) if automatic else 4,
                'mute': bool(t['mute'])})
            t.update(volume=volume, expected_volume=volume)
            self.changed()
            return
        current = self.current(t)
        live_volume = sum(v['value'] for v in current['volume'].values()) / len(current['volume']) / 655.36
        if automatic and (not should_apply(t) or abs(live_volume - t['expected_volume']) > .15
                          or current['mute'] != t['mute']):
            t['manual_volume'] = True
            self.changed()
            return
        run('pactl', 'set-sink-input-volume', t['pulse_index'], f'{volume:.3f}%')
        t.update(volume=volume, expected_volume=volume)
        t['channel_gains'] = [(volume / 100.) ** 3] * 2
        self.changed()

    def apply_class(self, t):
        if should_apply(t) and t['class'] in ('VOZ', 'SONS'):
            key = 'voice-start-volume' if t['class'] == 'VOZ' else 'sounds-start-volume'
            self.set_volume(t, self.settings.get_uint(key), automatic=True)

    def maybe_infer(self, t, pcm):
        needed = 3 if t.get('identified') else 1
        if t['pending'] or not self.needs_analysis(t) or t.get('override') or len(pcm) < RATE * 2 * needed:
            return
        if t['attempts'] >= 3 or time.monotonic() - t.get('last_infer', 0) < 2:
            return
        job = {'id': t['serial'] + ':' + str(t['generation']),
               'pcm': base64.b64encode(pcm).decode(),
               'profiles': [dict(p, path=str(CONFIG / 'voices' / (p['id'] + '.npy'))) for p in self.profiles],
               'threshold': self.settings.get_double('narrator-threshold')}
        if self.inference.submit(job):
            t.update(pending=True, attempts=t['attempts'] + 1, last_infer=time.monotonic())

    def needs_analysis(self, t):
        return not t.get('identified') or (bool(self.profiles) and t['class'] == 'VOZ' and not t.get('speaker_checked'))

    def maybe_fast_vad(self, t, pcm):
        if not t.get('buffered') or t.get('fast_voice') or t.get('identified') or t.get('fast_pending') or t.get('override'):
            return
        samples = len(pcm)//2
        if samples < 3072 or samples-t.get('fast_last_samples', 0) < 1536:
            return
        if self.fast_vad.submit({'id': t['serial']+':'+str(t['generation']),
                                 'pcm': base64.b64encode(pcm).decode()}):
            t.update(fast_pending=True, fast_last_samples=samples)

    def fast_result(self, result):
        serial, _, generation = str(result.get('id', '')).partition(':')
        t = self.tracks.get(serial)
        if not t or str(t['generation']) != generation:
            return False
        t['fast_pending'] = False
        if not result.get('error') and not t.get('override') and not t.get('identified'):
            t['fast_vad'] = result['fast_vad']
            if result.get('fast_voice'):
                t.update(fast_voice=True, confidence=result['fast_vad'], **{'class': 'VOZ'},
                         fast_decision_ns=time.monotonic_ns(), narrator='INCERTO' if self.profiles else 'SEM_AMOSTRA')
                self.apply_class(t)
        self.changed()
        return False

    def result(self, result):
        if str(result.get('id', '')).startswith('enroll:'):
            if not self.enroll_target or result['id'] != 'enroll:' + self.enroll_target['id']:
                return False
            self.enrolling = False
            self.enrollment = result.get('error') or 'Amostra cadastrada'
            if not result.get('error'):
                self.profiles.append({k: self.enroll_target[k] for k in ('id', 'name')})
                self.save_profiles()
                self.reidentify()
            self.enroll_target = None
            self.changed()
            return False
        serial, _, generation = str(result.get('id', '')).partition(':')
        t = self.tracks.get(serial)
        if not t or str(t['generation']) != generation or t.get('override'):
            return False
        t['pending'] = False
        if 'error' in result:
            t['analysis_error'] = result['error']
        else:
            t.pop('analysis_error', None)
            if t.get('fast_voice') and result.get('class') == 'INDEFINIDO':
                # Ambiguity from a longer window does not undo sustained speech.
                result.update(**{'class': 'VOZ'}, confidence=t['confidence'], speaker_checked=not self.profiles)
            for key in ('similarity', 'narrator_name', 'narrator_id'):
                t.pop(key, None)
            t.update({k: v for k, v in result.items() if k != 'id'})
            t['identified'] = result['class'] != 'INDEFINIDO'
            self.apply_class(t)
        self.changed()
        return False

    def tick(self):
        self.update_buffer_policy()
        self.refresh()
        if self.subscription.poll() is not None:
            self.subscription.stdout.close()
            self.subscribe()
        now = time.monotonic()
        if self.enroll_target and not self.enroll_target['submitted']:
            if now - self.enroll_target['started'] > 30:
                self.abort_enrollment(self.enroll_target['serial'], 'Não chegou áudio suficiente em 30 s.')
            else:
                self.changed()
        for serial, t in self.tracks.items():
            capture = self.captures.get(serial)
            if capture is None or not capture.alive():
                if capture:
                    capture.close()
                self.start_capture(t)
            if t['active'] and now - (capture.activity.last_signal if capture else 0) > .4:
                t['active'] = False
                self.changed()
            if t.get('first_activity') and not t['identified'] and not t.get('deadline_exceeded') \
                    and (now - t['first_activity']) * 1000 > self.settings.get_uint('classification-deadline-ms'):
                t['deadline_exceeded'] = True
                self.changed()
        return True

    def state(self):
        latest = max(self.tracks.values(), key=lambda t: t['last_activity'], default=None)
        return {'version': 1, 'analysis_version': 2, 'server_cookie': self.server_cookie, 'tracks': list(self.tracks.values()),
                'latest': latest['serial'] if latest and latest['last_activity'] else '',
                'buffer': {'enabled': self.expects_buffer(str(2**64-1)),
                           'reason': 'Retenção nativa por fluxo; fluxos anteriores preservados',
                           'deadline_ms': self.settings.get_uint('classification-deadline-ms'),
                           'max_hold_ms': self.settings.get_uint('classification-max-hold-ms')},
                'narrator_enrolled': bool(self.profiles), 'profiles': self.profiles,
                'enrollment': self.enrollment, 'enrolling': self.enrolling,
                'enrollment_track': self.enroll_target['serial'] if self.enroll_target else '',
                'enrollment_seconds': round(len(self.record_pcm) / (RATE * 2), 1),
                'error': self.error, 'pid': os.getpid()}

    def update_buffer_policy(self):
        policy = {'delay_ms': self.settings.get_uint('classification-deadline-ms'),
                  'max_hold_ms': self.settings.get_uint('classification-max-hold-ms'),
                  'safe_volume': safe_volume(self.settings.get_uint('voice-start-volume'),
                     self.settings.get_uint('sounds-start-volume'), self.settings.get_uint('provisional-volume'))}
        if policy == self.buffer_policy:
            return
        try:
            atomic_json(CONFIG / 'buffer-defaults.json', policy)
            run('pw-metadata', '-n', 'default', '0', 'mixer.buffer.policy', json.dumps(policy), 'Spa:String:JSON')
            self.buffer_policy = policy
        except Exception:
            pass

    def expects_buffer(self, serial):
        path = Path(os.environ.get('MIXER_BUFFER_POLICY_FILE', str(ROOT / 'buffer-policy.json')))
        try:
            policy = json.loads(path.read_text())
            return policy['enabled'] and (policy['cookie'] != self.server_cookie or int(serial) >= policy['minimum_serial'])
        except (OSError, ValueError, KeyError):
            return False

    def changed(self):
        self.dirty = True

    def publish(self):
        if not self.dirty:
            return True
        self.dirty = False
        state = self.state()
        atomic_json(RUNTIME / 'state.json', state)
        if self.connection:
            self.connection.emit_signal(None, OBJECT, BUS, 'StateChanged',
                                         GLib.Variant('(s)', (json.dumps(state, ensure_ascii=False),)))
        return True

    def close(self):
        self.publish()
        for capture in self.captures.values():
            capture.close()
        if self.subscription.poll() is None:
            self.subscription.terminate()
        self.subscription.wait()
        self.inference.close()
        self.fast_vad.close()


if __name__ == '__main__':
    loop = GLib.MainLoop()
    service = Service()
    def stop():
        loop.quit()
        return False
    GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, stop)
    GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, stop)
    try:
        loop.run()
    finally:
        service.close()
