#!/usr/bin/env python3
"""Initial gain on the primary PipeWire, preserving all user settings."""
import json
import time
from pathlib import Path
import test_retention as suite
from test_retention_live import Live
from test_initial_gain import speech

suite.OUT=suite.ROOT/'test-results/initial-gain-live'
suite.OUT.mkdir(exist_ok=True)


def main():
    ctx=Live();module=None
    # Unlike the older integration test, never change preferences or disable UI.
    def deadline(ms):
        assert ctx.state()['buffer']['deadline_ms']==ms
        time.sleep(.2)
    ctx.deadline=deadline
    before=ctx.command('dconf','dump','/org/gnome/shell/extensions/mixer-rapido/')
    try:
        voice=int(ctx.command('gsettings','--schemadir',str(suite.HELPER.parent/'schemas'),'get',
                              'org.gnome.shell.extensions.mixer-rapido','voice-start-volume').split()[-1])
        minimum=ctx.state()['buffer']['deadline_ms'];maximum=ctx.state()['buffer']['max_hold_ms']
        module=ctx.command('pactl','load-module','module-null-sink','sink_name=mixer_initial_gain_live',
                            'rate=48000','channels=2',
                            'sink_properties="node.hidden=true priority.session=-10000 mixer.buffer.serial=test"').strip()
        results=[]
        for name,file,quiet in [('live-narration','example1.wav',False),('live-quiet-words','example2.flac',True)]:
            results.append(suite.one(ctx,name,'voice',minimum,voice,sink='mixer_initial_gain_live',
                                     data_override=speech(file,quiet),latency_range=(minimum,max(minimum,maximum))))
            assert abs(results[-1]['native']['first_volume']-voice)<.01
        after=ctx.command('dconf','dump','/org/gnome/shell/extensions/mixer-rapido/')
        assert before==after,'Preferências mudaram durante o teste; não foram sobrescritas'
        (suite.OUT/'results.json').write_text(json.dumps({'core_cookie':ctx.cookie,
            'voice_setting':voice,'settings_unchanged':True,'results':results},indent=2))
        print('INITIAL GAIN PRIMARY PIPEWIRE: PASS',flush=True)
    finally:
        ctx.close();time.sleep(4)
        if module:ctx.command('pactl','unload-module',module)


if __name__=='__main__':main()
