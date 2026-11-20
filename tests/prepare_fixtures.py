#!/usr/bin/python3
"""Explicitly prepare public/synthetic audio fixtures; never called by the service."""
from pathlib import Path
import subprocess
import urllib.request

target = Path(__file__).resolve().parents[1] / 'test-results/fixtures'
target.mkdir(parents=True, exist_ok=True)
url = 'https://librosa.org/data/audio/Kevin_MacLeod_-_Vibe_Ace.ogg'
music = target / 'music.ogg'
if not music.exists():
    print('Download explícito da amostra pública:', url, flush=True)
    urllib.request.urlretrieve(url, music)
other = target / 'other-speaker.wav'
if not other.exists():
    text = ('This is a different narrator speaking clearly. We are testing independent voices and the audio mixer. '
            'Every new sentence should be identified as speech and keep its own volume.')
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi',
                    '-i', f"flite=text='{text}':voice=slt", '-ar', '16000', '-ac', '1', str(other)], check=True)
print('Amostras preparadas em', target)
