#!/usr/bin/env python3
"""Real PipeWire PCM oracle for initial speech gain and bounded retention."""
import json
import signal
import subprocess as sp
import sys
import time
from pathlib import Path

import numpy as np
import test_retention as suite

suite.OUT=suite.ROOT/'test-results/initial-gain-private'
suite.OUT.mkdir(exist_ok=True)


def settings(ctx,key,value):
    ctx.command('gsettings','--schemadir',str(suite.HELPER.parent/'schemas'),'set',
                'org.gnome.shell.extensions.mixer-rapido',key,str(value))


def speech(name,quiet=False):
    data=sp.check_output(['ffmpeg','-v','error','-i',str(suite.HELPER/'models/speechbrain'/name),
                         '-ar','48000','-ac','2','-f','f32le','-'])
    x=np.frombuffer(data,dtype='<f4').reshape(-1,2).copy()
    if quiet:x*=.12
    # Non-zero, unique markers do not replace the start of a real word.
    return np.concatenate((np.array([[.03125,.0625]],dtype='<f4'),x,
                           np.array([[.09375,-.03125]],dtype='<f4')))


def music():
    raw=sp.check_output(['ffmpeg','-v','error','-i',str(suite.ROOT/'test-results/fixtures/music.ogg'),
                         '-t','4','-ar','48000','-ac','2','-f','f32le','-'])
    x=np.frombuffer(raw,dtype='<f4').reshape(-1,2).copy()
    return np.concatenate((np.array([[.03125,.0625]],dtype='<f4'),x,
                           np.array([[.09375,-.03125]],dtype='<f4')))


def with_profile(ctx):
    # Register a genuine embedding in the PRIVATE configuration, never in the
    # user's profiles. Classification must not wait for its recognition.
    import soundfile as sf
    sys.path.insert(0,str(suite.HELPER))
    from inference import Models
    config=Path(ctx.env['XDG_CONFIG_HOME'])/'mixer-rapido'
    pid='b'*32
    audio,rate=sf.read(suite.HELPER/'models/speechbrain/example1.wav',dtype='float32')
    assert rate==16000
    Models().enroll(audio,str(config/'voices'/(pid+'.npy')))
    ctx.service.terminate();ctx.service.wait(timeout=5)
    (config/'voices.json').write_text(json.dumps([{'id':pid,'name':'Amostra real privada'}]))
    ctx.service=ctx.start('service-with-profile',['/usr/bin/python3',str(suite.HELPER/'service.py')])
    suite.wait(lambda:ctx.state()['pid']==ctx.service.pid and len(ctx.state()['profiles'])==1)
    return suite.one(ctx,'narrator-enrolled-first-voice','voice',500,79,
                     data_override=speech('example1.wav'),latency_range=(500,2000))


def main():
    ctx=None
    try:
        ctx=suite.Context.__new__(suite.Context);ctx.procs=[];ctx.__init__()
        settings(ctx,'voice-start-volume',79);settings(ctx,'sounds-start-volume',28)
        settings(ctx,'classification-max-hold-ms',2000)
        time.sleep(.3)
        results=[]
        for name,file,quiet in [('narration-1','example1.wav',False),
                                ('narration-2','example2.flac',False),
                                ('quiet-first-words','example1.wav',True)]:
            results.append(suite.one(ctx,name,'voice',500,79,data_override=speech(file,quiet),latency_range=(500,2000)))
            assert results[-1]['native']['first_decision']==2
            assert abs(results[-1]['native']['first_volume']-79)<.01
        results.append(suite.one(ctx,'sounds-at-own-volume','sounds',500,28,latency_range=(500,2000)))
        results.append(suite.one(ctx,'music-at-own-volume','sounds',500,28,data_override=music(),latency_range=(500,2000)))
        # No inference at all: retain up to the bound, never raise safe gain.
        results.append(suite.one(ctx,'classifier-stalled','sounds',500,20,freeze=True,latency_range=(2000,2000)))
        results.append(suite.one(ctx,'manual-before-release','voice',500,37,manual=True,
                                 data_override=speech('example1.wav'),latency_range=(500,2000)))
        results.append(with_profile(ctx))
        (suite.OUT/'results.json').write_text(json.dumps(results,indent=2))
        print('INITIAL GAIN REAL PIPEWIRE: PASS',flush=True)
    finally:
        if ctx:ctx.close()


if __name__=='__main__':main()
