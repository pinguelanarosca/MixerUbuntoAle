#!/usr/bin/env python3
"""Real late classifier response: continuous PCM and bounded gain slope."""
import json
import os
import signal
import subprocess as sp
import threading
import time
import numpy as np
import test_retention as suite
from test_initial_gain import speech,settings

suite.OUT=suite.ROOT/'test-results/initial-gain-ramp'
suite.OUT.mkdir(exist_ok=True)


def main():
    ctx=None
    try:
        ctx=suite.Context.__new__(suite.Context);ctx.procs=[];ctx.__init__()
        settings(ctx,'voice-start-volume',79);settings(ctx,'sounds-start-volume',28)
        ctx.deadline(500)
        x=np.concatenate([speech('example1.wav')]*2)
        out=suite.OUT/'late-classification.f32'
        rec=sp.Popen(['parec','--raw','--rate=48000','--channels=2','--format=float32le',
                      '--latency-msec=10','--device=mixer_retention_a.monitor'],env=ctx.env,stdout=out.open('wb'));ctx.procs.append(rec)
        time.sleep(.15);os.kill(ctx.service.pid,signal.SIGSTOP)
        p=sp.Popen(['paplay','--raw','--rate=48000','--channels=2','--format=float32le',
                    '--client-name=Google Chrome','--stream-name=late-classification',
                    '--device=mixer_retention_a'],env=ctx.env,stdin=sp.PIPE);ctx.procs.append(p)
        threading.Thread(target=lambda:p.communicate(x.tobytes()),daemon=True).start()
        stream=suite.wait(lambda:next((s for s in json.loads(ctx.command('pactl','-f','json','list','sink-inputs')) if s['properties'].get('media.name')=='late-classification'),None))
        serial=str(stream['properties']['object.serial']);path=ctx.runtime/'mixer-buffer'/('status-'+serial+'.json')
        first=suite.wait(lambda:(s if (s:=json.loads(path.read_text())).get('cookie')==ctx.cookie
                                and s.get('first_output_ns') else None))
        assert first['first_decision']==1 and abs(first['first_volume']-20)<.01
        time.sleep(.15);os.kill(ctx.service.pid,signal.SIGCONT)
        ramp=suite.wait(lambda:(s if (s:=json.loads(path.read_text())).get('cookie')==ctx.cookie
                               and s.get('ramp_left',0)>0 and abs(s['volume']-79)<.01 else None))
        time.sleep(max(0,len(x)/48000+2+.4-(time.monotonic_ns()-first['first_input_ns'])/1e9))
        rec.terminate();rec.wait()
        y=np.fromfile(out,dtype='<f4').reshape(-1,2)
        k=int(np.flatnonzero(np.any(y!=0,axis=1))[0]);actual=y[k:k+len(x)]
        assert len(actual)==len(x) and not np.count_nonzero(y[:k]) and not np.count_nonzero(y[k+len(x):])
        a=np.float32(.2);g0=float(a*a*a);a=np.float32(.79);g1=float(a*a*a)
        mask=np.abs(x[:,0])>.01
        ratio=actual[mask,0]/x[mask,0];indices=np.flatnonzero(mask)
        middle=(ratio>g0+(g1-g0)*.1)&(ratio<g0+(g1-g0)*.9)
        assert np.count_nonzero(middle)>100
        start=int(round(float(np.median(indices[middle]+1-(ratio[middle]-g0)/(g1-g0)*5760))))
        best=None
        for candidate in range(start-4,start+5):
            gain=g0+(g1-g0)*np.clip((np.arange(len(x))-candidate+1)/5760,0,1)
            error=float(np.max(abs(actual-x*gain[:,None])))
            if best is None or error<best[0]:best=(error,candidate)
        assert best[0]<2e-6,best
        assert np.max(abs(actual[:1024]-x[:1024]*g0))<1e-7
        assert np.max(abs(actual[-1024:]-x[-1024:]*g1))<1e-7
        result={'frames_per_channel':len(x),'initial_volume':20,'final_volume':79,'ramp_ms':120,
                'ramp_start_frame':best[1],'max_waveform_error':best[0],
                'native_ramp_remaining_observed':ramp['ramp_left'],'first_native':first}
        (suite.OUT/'results.json').write_text(json.dumps(result,indent=2));print('GAIN RAMP REAL PIPEWIRE: PASS',json.dumps(result),flush=True)
    finally:
        if ctx:
            if hasattr(ctx,'service'):os.kill(ctx.service.pid,signal.SIGCONT)
            ctx.close()


if __name__=='__main__':main()
