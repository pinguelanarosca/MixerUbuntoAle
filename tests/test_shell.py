#!/usr/bin/python3
"""Runs the installed files in GNOME 50 headless with the real helper forwarded.

Private D-Bus and keyfile settings; no extension copy or parallel installation.
The temporary resource overlay enables Eval only inside the test compositor.
"""
import json
import os
from pathlib import Path
import subprocess as sp
import sys
import tempfile
import threading
import time

from gi.repository import Gio, GLib

ROOT = Path(__file__).resolve().parents[1]
HELPER = Path.home() / '.local/share/gnome-shell/extensions/mixer-rapido@alee.local/helper'
sys.path.insert(0, str(HELPER))
from service import BUS, OBJECT, XML
from test_pipewire import Playback, pcm, state, call, command

OUT = ROOT / 'test-results'


def wait(test, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = test()
            if value:
                return value
        except GLib.Error:
            pass
        time.sleep(.1)
    raise AssertionError('Timeout no GNOME de teste')


def main():
    OUT.mkdir(exist_ok=True)
    upstream = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    procs = []
    players = []
    module = None
    test_profile = None
    loop = GLib.MainLoop()
    try:
        with tempfile.TemporaryDirectory(prefix='mixer-shell-') as temporary:
            tmp = Path(temporary)
            ui = tmp / 'ui'; ui.mkdir()
            init = sp.check_output(['gresource', 'extract', '/usr/lib/gnome-shell/libshell-18.so',
                                    '/org/gnome/shell/ui/init.js']).decode()
            (ui / 'init.js').write_text(init.replace("import('./main.js').then",
                "global.context.unsafe_mode = true;\n        import('./main.js').then"))
            env = dict(os.environ, GSETTINGS_BACKEND='keyfile', XDG_CONFIG_HOME=str(tmp / 'config'),
                       XDG_CACHE_HOME=str(tmp / 'cache'), LIBGL_ALWAYS_SOFTWARE='1', GTK_A11Y='none',
                       G_RESOURCE_OVERLAYS='/org/gnome/shell/ui=' + str(ui),
                       GI_TYPELIB_PATH='/usr/lib/gnome-shell/girepository-1.0:/usr/lib/gnome-shell',
                       LD_LIBRARY_PATH='/usr/lib/gnome-shell', MIXER_TEST_OUTPUT=str(OUT))
            daemon = sp.Popen(['dbus-daemon', '--session', '--nofork', '--print-address=1'], env=env,
                              stdout=sp.PIPE, stderr=open(OUT / 'shell-bus.log', 'w'), text=True)
            procs.append(daemon)
            address = daemon.stdout.readline().strip()
            env['DBUS_SESSION_BUS_ADDRESS'] = address
            down = Gio.DBusConnection.new_for_address_sync(address, 9, None, None)
            def forward(_c, _s, _o, _i, method, params, invocation):
                def done(c, result):
                    try:
                        invocation.return_value(c.call_finish(result))
                    except Exception as e:
                        invocation.return_dbus_error(BUS + '.Error', str(e))
                upstream.call(BUS, OBJECT, BUS, method, params, None, Gio.DBusCallFlags.NONE,
                              5000, None, done)
            registration = down.register_object(OBJECT, Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0],
                                                forward, None, None)
            owner = Gio.bus_own_name_on_connection(down, BUS, Gio.BusNameOwnerFlags.NONE, None, None)
            subscription = upstream.signal_subscribe(BUS, BUS, 'StateChanged', OBJECT, None,
                Gio.DBusSignalFlags.NONE, lambda _c, _s, _o, _i, _n, p:
                    down.emit_signal(None, OBJECT, BUS, 'StateChanged', p))
            threading.Thread(target=loop.run, daemon=True).start()
            for key, value in [('enabled-extensions', "['mixer-rapido@alee.local']"),
                               ('disable-user-extensions', 'false')]:
                sp.run(['gsettings', 'set', 'org.gnome.shell', key, value], env=env, check=True)
            display = 'mixer-qa-' + str(os.getpid())
            shell = sp.Popen(['gnome-shell', '--headless', '--wayland', '--no-x11',
                              '--virtual-monitor=1280x800', '--wayland-display=' + display, '--mode=user'],
                             env=env, stdout=open(OUT / 'shell.log', 'w'), stderr=sp.STDOUT)
            procs.append(shell)
            def evaluate(script):
                r = down.call_sync('org.gnome.Shell', '/org/gnome/Shell', 'org.gnome.Shell', 'Eval',
                                   GLib.Variant('(s)', (script,)), None, 0, 5000, None).unpack()
                if not r[0]:
                    raise GLib.Error(r[1])
                return json.loads(r[1])
            wait(lambda: evaluate("import('resource:///org/gnome/shell/ui/main.js').then(m=>Boolean(m.panel?.statusArea['mixer-rapido@alee.local']?._helper.state))"))
            script = """import('resource:///org/gnome/shell/ui/main.js').then(m=>{
                const b=m.panel.statusArea['mixer-rapido@alee.local']; b.menu.open();
                const texts=[]; function walk(w){if(w.text)texts.push(w.text); for(const c of w.get_children())walk(c);} walk(b.menu.box);
                return {version:m.extensionManager.lookup('mixer-rapido@alee.local').metadata.version,
                    rows:[...b._rows.values()].filter(r=>r._chromeTrack).map(r=>({name:r._name.text,
                    serial:r._info?.serial, classification:r._classification.text, record:r._enroll.label})),
                    texts, state:b._helper.state};})"""
            def ready_ui():
                result = evaluate(script)
                (OUT / 'shell-ui.json').write_text(json.dumps(result, indent=2, ensure_ascii=False))
                if not all(r['serial'] for r in result['rows']):
                    return False
                if result['state']['tracks'] and not result['rows']:
                    return False
                if result['state']['latest'] and not any(r['serial'] == result['state']['latest']
                        and r['name'].startswith('●') for r in result['rows']):
                    return False
                return result
            result = wait(ready_ui)
            assert result['version'] == 4
            assert any('Novas faixas de voz' in s for s in result['texts'])
            assert any('Novas faixas de sons' in s for s in result['texts'])
            assert all(r['serial'] for r in result['rows'])
            if result['state']['latest']:
                assert any(r['serial'] == result['state']['latest'] and r['name'].startswith('●')
                           for r in result['rows'])
            evaluate("import('resource:///org/gnome/shell/ui/main.js').then(m=>{m.overview.hide();return true;})")
            time.sleep(.6)
            # Exercise the actual Shell button against a controlled live stream.
            module = command('pactl', 'load-module', 'module-null-sink', 'sink_name=mixer_shell_test',
                             'rate=16000', 'channels=2').strip()
            player = Playback('MixerShellVoice', 'mixer_shell_test'); players.append(player)
            track = wait(lambda: next((t for t in state()['tracks'] if t['name'] == 'MixerShellVoice'), None))
            time.sleep(.3)  # Let the cached version-2 UI finish its initial-volume timer.
            call('SetVolume', '(sd)', (track['serial'], 100.))
            player.set(pcm(HELPER / 'models/speechbrain/example1.wav'))
            old_profiles = {p['id'] for p in state()['profiles']}
            target = json.dumps(track['serial'])
            wait(lambda: evaluate("import('resource:///org/gnome/shell/ui/main.js').then(m=>Boolean([...m.panel.statusArea['mixer-rapido@alee.local']._rows.values()].find(r=>r._info?.serial===" + target + ")))"))
            evaluate("import('resource:///org/gnome/shell/ui/main.js').then(m=>{const r=[...m.panel.statusArea['mixer-rapido@alee.local']._rows.values()].find(r=>r._info?.serial===" + target + ");r.slider.value=.37;return true;})")
            wait(lambda: any(t['serial']==track['serial'] and abs(t['volume']-37)<.1 and t['buffered'] for t in state()['tracks']))
            evaluate("import('resource:///org/gnome/shell/ui/main.js').then(m=>{[...m.panel.statusArea['mixer-rapido@alee.local']._rows.values()].find(r=>r._info?.serial===" + target + ")._muteButton.emit('clicked',1);return true;})")
            wait(lambda: evaluate("import('resource:///org/gnome/shell/ui/main.js').then(m=>{const r=[...m.panel.statusArea['mixer-rapido@alee.local']._rows.values()].find(r=>r._info?.serial===" + target + ");return r._info.mute&&r._muteIcon.icon_name==='audio-volume-muted-symbolic';})"))
            evaluate("import('resource:///org/gnome/shell/ui/main.js').then(m=>{[...m.panel.statusArea['mixer-rapido@alee.local']._rows.values()].find(r=>r._info?.serial===" + target + ")._muteButton.emit('clicked',1);return true;})")
            wait(lambda: any(t['serial']==track['serial'] and not t['mute'] for t in state()['tracks']))
            call('SetVolume','(sd)',(track['serial'],100.))
            print('VERIFICADO: slider 37% e mute do GNOME controlam o ganho após o buffer.',flush=True)
            evaluate("import('resource:///org/gnome/shell/ui/main.js').then(m=>{[...m.panel.statusArea['mixer-rapido@alee.local']._rows.values()].find(r=>r._info?.serial===" + target + ")._enroll.emit('clicked',1);return true;})")
            wait(lambda: state()['enrollment_track'] == track['serial'] and state()['enrolling'])
            wait(lambda: not state()['enrolling'], timeout=30)
            test_profile = next(p for p in state()['profiles'] if p['id'] not in old_profiles)
            test_name = 'Teste UI ' + str(os.getpid())
            call('RenameNarrator', '(ss)', (test_profile['id'], test_name))
            wait(lambda: next(t for t in state()['tracks'] if t['serial'] == track['serial']).get('narrator_id') == test_profile['id'])
            print('VERIFICADO: botão Cadastrar voz do GNOME capturou a própria faixa e reconheceu a voz.', flush=True)
            (OUT / 'shell-ui.json').write_text(json.dumps(result, indent=2, ensure_ascii=False))
            def screenshot(name):
                script = """Promise.all([import('gi://Shell'),import('gi://Gio')]).then(async ([{default:Shell},{default:Gio}])=>{
                    let out=Gio.File.new_for_path(FILE).replace(null,false,Gio.FileCreateFlags.REPLACE_DESTINATION,null);
                    await new Shell.Screenshot().screenshot(false,out);out.close(null);return true;})"""
                evaluate(script.replace('FILE', json.dumps(str(OUT / name))))
            screenshot('menu-gnome50.png')
            print('VERIFICADO: GNOME 50 carrega a versão 4 da instalação existente; sliders, identidade e destaque reais.', flush=True)
            env['WAYLAND_DISPLAY'] = display
            env['MIXER_TEST_VOICE_NAME'] = test_name
            env['MIXER_TEST_VOICE_RENAME'] = test_name + ' renomeada'
            evaluate("import('resource:///org/gnome/shell/ui/main.js').then(m=>{m.panel.statusArea['mixer-rapido@alee.local'].menu.close();return true;})")
            (OUT / 'preferences-widgets.json').unlink(missing_ok=True)
            preferences = sp.Popen(['gjs', '-m', str(ROOT / 'tests/test_preferences.js')], env=env,
                                   stdout=open(OUT / 'preferences.log', 'w'), stderr=sp.STDOUT)
            procs.append(preferences)
            wait(lambda: (OUT / 'preferences-widgets.json').exists(), timeout=10)
            widgets = json.loads((OUT / 'preferences-widgets.json').read_text())
            assert any(w['title'] == 'Prazo de identificação (ms)' and w['value'] == 500 for w in widgets)
            assert any(w['title'] == 'Similaridade mínima do narrador' for w in widgets)
            assert any(w['title'].endswith('voz(es) cadastrada(s)') for w in widgets)
            wait(lambda: any(p['id'] == test_profile['id'] and p['name'] == env['MIXER_TEST_VOICE_RENAME']
                             for p in state()['profiles']))
            screenshot('preferences-gnome50.png')
            print('VERIFICADO: preferências GTK/Adwaita reais; lista de vozes e configurações.', flush=True)
            for name in ['shell.log', 'preferences.log']:
                assert 'JS ERROR' not in (OUT / name).read_text(), name
            upstream.signal_unsubscribe(subscription)
            Gio.bus_unown_name(owner)
            down.unregister_object(registration)
            print('SHELL INTEGRATION: PASS', flush=True)
    finally:
        loop.quit()
        for proc in reversed(procs):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except sp.TimeoutExpired:
                    proc.kill(); proc.wait()
        sp.run(['systemctl', '--user', 'start', 'mixer-rapido.service'], check=True)
        for player in players:
            player.close()
        if players:
            time.sleep(3)  # retain the output until the buffered tail is drained
        if module:
            command('pactl', 'unload-module', module)
        if test_profile:
            call('RemoveNarrator', '(s)', (test_profile['id'],))


if __name__ == '__main__':
    main()
