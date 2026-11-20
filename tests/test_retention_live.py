#!/usr/bin/python3
"""Same sample oracle on the primary Ubuntu PipeWire; private outputs only."""
import json
import os
from pathlib import Path
import signal
import subprocess as sp
import time
import sys
sys.path.append('/usr/lib/python3/dist-packages')
from gi.repository import Gio
import test_retention as suite

suite.OUT=suite.ROOT/'test-results/retention-live'
suite.OUT.mkdir(exist_ok=True)

class Live(suite.Context):
    def __init__(self):
        self.env=dict(os.environ);self.runtime=Path(os.environ['XDG_RUNTIME_DIR']);self.procs=[]
        self.bus=Gio.bus_get_sync(Gio.BusType.SESSION,None)
        self.cookie=next(o['info']['cookie'] for o in json.loads(self.command('pw-dump','0')) if o['id']==0)
        self.service=type('Service',(),{'pid':self.state()['pid']})()

def main():
    ctx=Live();modules=[];settings={};extension=False
    existing=json.loads(ctx.command('pactl','-f','json','list','sink-inputs'))
    try:
        extension='Enabled: Yes' in ctx.command('gnome-extensions','info','mixer-rapido@alee.local')
        if extension:ctx.command('gnome-extensions','disable','mixer-rapido@alee.local')
        for key,value in [('classification-deadline-ms','500'),('voice-start-volume','73'),('sounds-start-volume','17')]:
            settings[key]=ctx.command('gsettings','--schemadir',str(suite.HELPER.parent/'schemas'),'get','org.gnome.shell.extensions.mixer-rapido',key).strip()
            ctx.command('gsettings','--schemadir',str(suite.HELPER.parent/'schemas'),'set','org.gnome.shell.extensions.mixer-rapido',key,value)
        for name in ('mixer_retention_a','mixer_retention_b'):
            modules.append(ctx.command('pactl','load-module','module-null-sink','sink_name='+name,'rate=48000','channels=2').strip())
        maximum=max(500,ctx.state()['buffer']['max_hold_ms'])
        results=[suite.one(ctx,'live-timeout-500','sounds',500,17,freeze=True,latency_range=(maximum,maximum)),
                 suite.one(ctx,'live-voice','voice',2500,73),
                 suite.one(ctx,'live-manual','voice',2500,37,manual=True)]
        current=json.loads(ctx.command('pactl','-f','json','list','sink-inputs'))
        controls=[]
        for a in existing:
            b=next((b for b in current if b['index']==a['index']),None)
            if b:
                assert a['volume']==b['volume'] and a['mute']==b['mute'],(a['index'],a['volume'],b['volume'])
                controls.append({'index':a['index'],'volume':b['volume'],'mute':b['mute']})
        report={'core_cookie':ctx.cookie,'primary_ubuntu_session':True,'results':results,'existing_controls_preserved':controls}
        (suite.OUT/'results.json').write_text(json.dumps(report,indent=2))
        print('PRIMARY PIPEWIRE RETENTION: PASS',flush=True)
    finally:
        os.kill(ctx.service.pid,signal.SIGCONT)
        ctx.close()
        time.sleep(3)
        for m in modules:ctx.command('pactl','unload-module',m)
        for key,value in settings.items():
            ctx.command('gsettings','--schemadir',str(suite.HELPER.parent/'schemas'),'set','org.gnome.shell.extensions.mixer-rapido',key,value)
        if extension:ctx.command('gnome-extensions','enable','mixer-rapido@alee.local')

if __name__=='__main__':main()
