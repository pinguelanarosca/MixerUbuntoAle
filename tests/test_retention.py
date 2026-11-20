#!/usr/bin/env python3
"""Actual PipeWire/WirePlumber/Pulse and installed buffer, with virtual outputs.

Private server/runtime for destructive failure tests; no extension installation
is copied. All binaries, models and service code come from the original install.
"""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess as sp
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from scipy.signal import correlate
sys.path.append('/usr/lib/python3/dist-packages')
from gi.repository import Gio, GLib

ROOT=Path(__file__).resolve().parents[1]
HELPER=Path.home()/'.local/share/gnome-shell/extensions/mixer-rapido@alee.local/helper'
OUT=ROOT/'test-results/retention'
OUT.mkdir(parents=True,exist_ok=True)
RATE=48000


def wait(test, timeout=15):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        try:
            value=test()
            if value:return value
        except (OSError,ValueError,KeyError,GLib.Error,sp.CalledProcessError):pass
        time.sleep(.02)
    raise AssertionError('Timeout na retenção real')


class Context:
    def __init__(self):
        self.env=dict(os.environ)
        # Never reuse control files, profiles or queues from a previous server.
        self.workspace=Path(tempfile.mkdtemp(prefix='run-',dir=OUT))
        self.runtime=self.workspace/'runtime';self.runtime.mkdir(mode=0o700)
        config=self.workspace/'config';fragment=config/'wireplumber/wireplumber.conf.d/90-mixer-buffer.conf'
        fragment.parent.mkdir(parents=True,exist_ok=True)
        fragment.write_text('''wireplumber.profiles = { policy = { mixer.buffer.policy = required } }
wireplumber.components = [ {
  name = "'''+str(HELPER/'buffer-policy.lua')+'''", type = script/lua,
  provides = mixer.buffer.policy,
  requires = [ support.standard-event-source hooks.linking.target.link ],
  arguments = { initial.cookie = 0 minimum.serial = 0 }
} ]
''')
        self.env.update(XDG_RUNTIME_DIR=str(self.runtime),PIPEWIRE_RUNTIME_DIR=str(self.runtime),
                        PULSE_RUNTIME_PATH=str(self.runtime/'pulse'),XDG_CONFIG_HOME=str(config),
                        XDG_STATE_HOME=str(self.workspace/'state'),XDG_CACHE_HOME=str(self.workspace/'cache'),
                        PULSE_SERVER='unix:'+str(self.runtime/'pulse/native'),GSETTINGS_BACKEND='keyfile',
                        MIXER_BUFFER_POLICY_FILE=str(self.workspace/'policy.json'))
        self.procs=[]
        self.pw=self.start('pipewire',['pipewire'])
        wait(lambda:self.command('pw-dump'))
        self.pulse=self.start('pulse',['pipewire-pulse'])
        wait(lambda:self.command('pactl','info'))
        self.wp=self.start('wireplumber',['wireplumber','--profile=policy'])
        wait(lambda:'mixer.buffer.interceptor' in self.command('pw-metadata','-n','default'))
        self.cookie=next(o['info']['cookie'] for o in json.loads(self.command('pw-dump','0')) if o['id']==0)
        (self.workspace/'policy.json').write_text(json.dumps({'enabled':True,'cookie':self.cookie,'minimum_serial':0}))
        for name,value in [('classification-migrated','true'),('voice-start-volume','73'),('sounds-start-volume','17'),('provisional-volume','20'),('classification-deadline-ms','500'),('classification-max-hold-ms','2000')]:
            self.command('gsettings','--schemadir',str(HELPER.parent/'schemas'),'set',
                         'org.gnome.shell.extensions.mixer-rapido',name,value)
        self.service=self.start('service',['/usr/bin/python3',str(HELPER/'service.py')])
        self.bus=Gio.bus_get_sync(Gio.BusType.SESSION,None)
        wait(self.state)
        self.engine=self.start('engine',[str(HELPER/'mixer-buffer-engine')])
        self.command('pactl','load-module','module-null-sink','sink_name=mixer_retention_a','rate=48000','channels=2')
        self.command('pactl','load-module','module-null-sink','sink_name=mixer_retention_b','rate=48000','channels=2')
        wait(lambda:len(json.loads(self.command('pactl','-f','json','list','sinks')))==2)

    def start(self,name,args):
        p=sp.Popen(args,env=self.env,stdout=open(OUT/(name+'.log'),'w'),stderr=sp.STDOUT)
        self.procs.append(p);return p

    def command(self,*args):
        return sp.check_output(args,env=self.env,stderr=sp.STDOUT,timeout=5).decode()

    def call(self,method,signature=None,args=()):
        r=self.bus.call_sync('org.gnome.Shell.Extensions.MixerRapido','/org/gnome/Shell/Extensions/MixerRapido',
            'org.gnome.Shell.Extensions.MixerRapido',method,GLib.Variant(signature,args) if signature else None,
            None,0,3000,None)
        return r.unpack()

    def state(self):return json.loads(self.call('GetState')[0])

    def deadline(self,ms):
        self.command('gsettings','--schemadir',str(HELPER.parent/'schemas'),'set',
                     'org.gnome.shell.extensions.mixer-rapido','classification-deadline-ms',str(ms))
        wait(lambda:self.state()['buffer']['deadline_ms']==ms)
        time.sleep(1.1)

    def close(self):
        for p in reversed(self.procs):
            if p.poll() is None:
                p.terminate()
                try:p.wait(timeout=4)
                except sp.TimeoutExpired:p.kill();p.wait()


def signal_samples(kind):
    if kind=='voice':
        raw=sp.check_output(['ffmpeg','-v','error','-i',str(HELPER/'models/speechbrain/example1.wav'),
                            '-ar','48000','-ac','2','-f','f32le','-'])
        x=np.frombuffer(raw,dtype='<f4').reshape(-1,2).copy()
    else:
        t=np.arange(48000*3)/48000
        x=np.column_stack((.2*np.sin(2*np.pi*440*t),.15*np.cos(2*np.pi*660*t))).astype('<f4')
    x[0]=[.125,.25];x[-1]=[.375,-.125]
    return x


def one(ctx,name,kind,delay,volume,freeze=False,manual=False,sink='mixer_retention_a',engine_fail=False,kill_delay=.10,data_override=None,latency_range=None):
    ctx.deadline(delay)
    data=signal_samples(kind) if data_override is None else data_override
    record_path=OUT/(name+'-output.f32')
    recorder=sp.Popen(['parec','--raw','--rate=48000','--channels=2','--format=float32le',
                       '--latency-msec=10','--device='+sink+'.monitor'],env=ctx.env,stdout=open(record_path,'wb'))
    ctx.procs.append(recorder)
    time.sleep(.15)
    if freeze:os.kill(ctx.service.pid,signal.SIGSTOP)
    player=sp.Popen(['paplay','--raw','--rate=48000','--channels=2','--format=float32le',
                    '--client-name=Google Chrome','--stream-name='+name,'--device='+sink],
                   stdin=sp.PIPE,env=ctx.env)
    ctx.procs.append(player)
    threading.Thread(target=lambda:player.communicate(data.tobytes()),daemon=True).start()
    try:
        stream=wait(lambda:next((s for s in json.loads(ctx.command('pactl','-f','json','list','sink-inputs'))
                                if s['properties'].get('media.name')==name),None))
        serial=str(stream['properties']['object.serial'])
        path=ctx.runtime/'mixer-buffer'/('status-'+serial+'.json')
        native=wait(lambda:(s if (s:=json.loads(path.read_text()))['cookie']==ctx.cookie else None))
        try:
            wait(lambda:json.loads(path.read_text())['first_input_ns'])
        except Exception:
            (OUT/'failed-graph.json').write_text(ctx.command('pw-dump'))
            raise
        early=json.loads(path.read_text())
        assert not early['first_output_ns'],early
        if engine_fail:
            time.sleep(kill_delay)
            ctx.engine.kill();ctx.engine.wait()
            ctx.engine=ctx.start('engine-recovered',[str(HELPER/'mixer-buffer-engine')])
            wait(lambda:json.loads(path.read_text())['pid']==ctx.engine.pid)
        if manual:
            wait(lambda:any(t['serial']==serial and t.get('buffered') for t in ctx.state()['tracks']))
            ctx.call('SetVolume','(sd)',(serial,float(volume)))
        if not freeze:
            expected='VOZ' if kind=='voice' else 'SONS'
            wait(lambda:next((t for t in ctx.state()['tracks'] if t['serial']==serial and t['class']==expected),None))
        wait(lambda:json.loads(path.read_text())['first_output_ns'], timeout=8)
        native=json.loads(path.read_text())
        dump=json.loads(ctx.command('pw-dump'))
        source_id=int(stream['properties']['object.id'])
        peers=[o['info']['input-node-id'] for o in dump if o['type'].endswith(':Link') and o['info']['output-node-id']==source_id]
        assert peers and set(peers)=={native['node_id']}, peers
        time.sleep(max(0,len(data)/RATE+native.get('actual_delay_ms',delay)/1000+.35-(time.monotonic_ns()-early['first_input_ns'])/1e9))
        recorder.terminate();recorder.wait()
        y=np.fromfile(record_path,dtype='<f4').reshape(-1,2)
        expected=(data*np.float32((volume/100)**3)).astype('<f4')
        k=int(np.argmax(correlate(y[:,0],expected[:,0],mode='valid',method='fft')))
        actual=y[k:k+len(data)]
        assert actual.shape==expected.shape,(actual.shape,expected.shape)
        error=float(np.max(abs(actual-expected)))
        if not engine_fail:
            assert error<1e-7,error
            assert np.count_nonzero(y[k+len(data):])==0,'Reprodução duplicada após a cauda'
        else:
            # SIGKILL is not a transactional PipeWire handoff. Report any lost
            # in-flight quantum rather than describing restart as lossless.
            k=int(np.flatnonzero(np.any(y != 0,axis=1))[0])
            actual=y[k:k+len(data)]
            error=float(np.max(abs(actual-expected)))
            assert np.max(abs(actual[:1024]-expected[:1024]))<1e-7
            last=np.flatnonzero(np.all(abs(y-expected[-1])<1e-7,axis=1))
            assert len(last),'A cauda não foi liberada após a recuperação'
        assert np.count_nonzero(y[:k])==0,'Áudio antes da primeira amostra retida'
        assert native['first_decision_ns']<=native['first_output_ns'],native
        measured=(native['first_output_ns']-native['first_input_ns'])/1e6
        if latency_range:
            assert latency_range[0]-3<=measured<=latency_range[1]+3,(measured,latency_range)
        else:
            assert abs(measured-delay)<3 if not engine_fail else delay-3<=measured<=delay+1500,(measured,delay)
        assert native['overruns']==0
        result={'case':name,'classification':kind,'timeout':freeze,'manual':manual,'engine_fail':engine_fail,'delay_ms':measured,
                'frames_per_channel':len(data),'max_error':error,'first_input':native['first_input'],
                'first_output':native['first_output'],'decision':native['decision'],
                'decision_ns':native['decision_ns'],'first_output_ns':native['first_output_ns'],
                'volume':native['volume'],'direct_source_to_physical':False,'native':native}
        if engine_fail:
            result.update(lossless=error<1e-7 and int(last[-1]-k)==len(data)-1, tail_frame=int(last[-1]-k),
                          length_difference_frames=int(last[-1]-k+1-len(data)),
                          note='Reinício seguro confirmado; SIGKILL pode perder o quantum em trânsito.')
        (OUT/(name+'.json')).write_text(json.dumps(result,indent=2))
        print('VERIFICADO:',json.dumps(result),flush=True)
        return result
    finally:
        if freeze:os.kill(ctx.service.pid,signal.SIGCONT)
        if player.poll() is None:player.terminate();player.wait()
        if recorder.poll() is None:recorder.terminate();recorder.wait()


def main():
    ctx=None
    try:
        ctx=Context.__new__(Context);ctx.procs=[];ctx.__init__()
        results=[]
        if '--recovery-stress' in sys.argv:
            for i,delay in enumerate((.05,.07,.11,.17,.23)):
                results.append(one(ctx,'recovery-stress-'+str(i),'sounds',2500,17,freeze=True,engine_fail=True,kill_delay=delay))
            (OUT/'recovery-stress.json').write_text(json.dumps(results,indent=2))
            print('RECOVERY SAFETY: PASS; lossless cases:',sum(r['lossless'] for r in results),'/',len(results),flush=True)
            return
        if '--extended-only' not in sys.argv and '--engine-only' not in sys.argv:
            results.extend([one(ctx,'timeout-500','sounds',500,17,freeze=True,latency_range=(2000,2000)),
                            one(ctx,'voice-classified','voice',2500,73),
                            one(ctx,'sounds-classified','sounds',2500,17)])
        if '--engine-only' not in sys.argv:
            results.append(one(ctx,'manual-priority','voice',2500,37,manual=True))
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending=[] if '--engine-only' in sys.argv else [pool.submit(one,ctx,'simultaneous-voice','voice',2500,73,sink='mixer_retention_a'),
                     pool.submit(one,ctx,'simultaneous-sounds','sounds',2500,17,sink='mixer_retention_b')]
            results.extend(f.result() for f in pending)
        results.append(one(ctx,'engine-recovery','sounds',2500,17,freeze=True,engine_fail=True))
        for name in ('timeout-500','voice-classified','sounds-classified'):
            if '--extended-only' in sys.argv and (OUT/(name+'.json')).exists():
                results.insert(0,json.loads((OUT/(name+'.json')).read_text()))
        (OUT/'results.json').write_text(json.dumps(results,indent=2))
        print('RETENTION INTEGRATION: PASS',flush=True)
    finally:
        if ctx:ctx.close()


if __name__=='__main__':main()
