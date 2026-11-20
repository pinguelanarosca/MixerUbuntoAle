#!/usr/bin/python3
"""Activate/deactivate the buffer on the existing installation, never clone it."""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess as sp
import time

ROOT=Path.home()/'.local/share/gnome-shell/extensions/mixer-rapido@alee.local'
HELPER=ROOT/'helper'
FRAGMENT=Path.home()/'.config/wireplumber/wireplumber.conf.d/95-mixer-buffer.conf'
UNIT=Path.home()/'.config/systemd/user/mixer-buffer.service'
POLICY=HELPER/'buffer-policy.json'
RUNTIME=Path(os.environ['XDG_RUNTIME_DIR'])/'mixer-buffer'
MARKER='# Managed by Mixer Rapido native buffer; preserve personal fragments.'

def run(*args):return sp.check_output(args,stderr=sp.STDOUT,timeout=20).decode()
def write(path,text):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix('.tmp');tmp.write_text(text);tmp.replace(path)
def dump():return json.loads(run('pw-dump'))
def cookie(objects):return next(o['info']['cookie'] for o in objects if o['id']==0)
def restore_legacy():
    path=Path.home()/'.local/state/mixer-rapido/buffer-activation/legacy-control.json'
    if path.exists():
        current=run('gsettings','--schemadir',str(ROOT/'schemas'),'get','org.gnome.shell.extensions.mixer-rapido','chrome-start-volume').strip()
        if current=='uint32 100':
            run('gsettings','--schemadir',str(ROOT/'schemas'),'set','org.gnome.shell.extensions.mixer-rapido','chrome-start-volume',json.loads(path.read_text())['previous'])

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['enable','disable','check'])
    args=parser.parse_args()
    assert json.loads((ROOT/'metadata.json').read_text())['uuid']=='mixer-rapido@alee.local'
    if FRAGMENT.exists() and not FRAGMENT.read_text().startswith(MARKER):
        raise SystemExit('Fragmento não pertence ao mixer; não foi alterado.')
    if args.action=='check':
        print(json.dumps({'policy':json.loads(POLICY.read_text()) if POLICY.exists() else {},
            'interceptor':run('pw-metadata','-n','default'),
            'service':run('systemctl','--user','is-active','mixer-buffer.service')},indent=2));return
    if args.action=='disable':
        if list(RUNTIME.glob('status-*.json')):
            raise SystemExit('Há filas retidas. Encerre as faixas do Chrome e aguarde a cauda antes de desativar.')
        FRAGMENT.unlink(missing_ok=True)
        write(POLICY,json.dumps({'enabled':False,'cookie':cookie(dump()),'minimum_serial':0}))
        run('systemctl','--user','restart','wireplumber.service')
        run('systemctl','--user','disable','--now','mixer-buffer.service')
        restore_legacy()
        print('EXECUTADO: interceptação removida, roteamento padrão restaurado.');return
    manifest=json.loads((HELPER/'buffer-build.json').read_text())
    for path,key in [(HELPER/'buffer_engine.c','source_sha256'),(HELPER/'mixer-buffer-engine','binary_sha256')]:
        assert hashlib.sha256(path.read_bytes()).hexdigest()==manifest[key],str(path)+' não corresponde ao build'
    if FRAGMENT.exists():raise SystemExit('Interceptação já instalada. Use check para consultar.')
    # Starting the engine first closes the interval in which the hook could
    # accept a source but have no consumer. Existing sources remain exempt.
    shutil.copy2(HELPER/'mixer-buffer.service',UNIT)
    run('systemctl','--user','daemon-reload')
    run('systemctl','--user','restart','mixer-rapido.service')
    run('systemctl','--user','enable','--now','mixer-buffer.service')
    before=dump()
    recovery=Path.home()/'.local/state/mixer-rapido/buffer-activation'
    recovery.mkdir(parents=True,exist_ok=True)
    (recovery/'graph-before.json').write_text(json.dumps(before))
    (recovery/'pulse-before.json').write_text(run('pactl','-f','json','list','sink-inputs'))
    (recovery/'settings-before.dconf').write_text(run('dconf','dump','/org/gnome/shell/extensions/mixer-rapido/'))
    # GNOME caches imported extension modules until logout. The v2 timer writes
    # the deprecated Chrome volume 150ms after creation, corrupting input PCM.
    # Unity for that retired control keeps transport intact until the next login.
    from gi.repository import Gio, GLib
    bus=Gio.bus_get_sync(Gio.BusType.SESSION,None)
    loaded=bus.call_sync('org.gnome.Shell.Extensions','/org/gnome/Shell/Extensions',
        'org.gnome.Shell.Extensions','GetExtensionInfo',GLib.Variant('(s)',('mixer-rapido@alee.local',)),
        None,0,3000,None).unpack()[0]
    if loaded.get('version')==2:
        old=run('gsettings','--schemadir',str(ROOT/'schemas'),'get','org.gnome.shell.extensions.mixer-rapido','chrome-start-volume').strip()
        write(recovery/'legacy-control.json',json.dumps({'previous':old,'temporary_transport_unity':True}))
        run('gsettings','--schemadir',str(ROOT/'schemas'),'set','org.gnome.shell.extensions.mixer-rapido','chrome-start-volume','100')
    # WirePlumber is stopped before the cutoff is taken. No source can link
    # between taking the cutoff and loading the interception hook.
    run('systemctl','--user','stop','wireplumber.service')
    try:
        objects=dump();core_cookie=cookie(objects)
        cutoff=max(int(o.get('info',{}).get('props',{}).get('object.serial',0)) for o in objects)+1
        text=MARKER+'''\nwireplumber.profiles = { main = { mixer.buffer.policy = required } }
wireplumber.components = [ {
  name = "'''+str(HELPER/'buffer-policy.lua')+'''", type = script/lua,
  provides = mixer.buffer.policy,
  requires = [ support.standard-event-source hooks.linking.target.link ],
  arguments = { initial.cookie = '''+str(core_cookie)+' minimum.serial = '+str(cutoff)+''' }
} ]
'''
        write(FRAGMENT,text)
        write(POLICY,json.dumps({'enabled':True,'cookie':core_cookie,'minimum_serial':cutoff}))
        run('systemctl','--user','start','wireplumber.service')
        end=time.monotonic()+15
        while time.monotonic()<end:
            ready=re.search(r"key:'mixer.buffer.interceptor' value:'([^']+)'",run('pw-metadata','-n','default'))
            if ready:
                state=json.loads(ready.group(1))
                if int(state['cookie'])==core_cookie and int(state['minimum_serial'])==cutoff:break
            time.sleep(.1)
        else:raise RuntimeError('WirePlumber não confirmou o interceptador')
    except BaseException:
        FRAGMENT.unlink(missing_ok=True)
        write(POLICY,json.dumps({'enabled':False,'cookie':cookie(dump()),'minimum_serial':0}))
        run('systemctl','--user','restart','wireplumber.service')
        run('systemctl','--user','disable','--now','mixer-buffer.service')
        restore_legacy()
        raise
    print('EXECUTADO: retenção ativada para novas faixas; cookie',core_cookie,'serial mínimo',cutoff)

if __name__=='__main__':main()
