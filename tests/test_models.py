"""Run with the installed helper's venv. Uses actual local model weights."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path.home() / '.local/share/gnome-shell/extensions/mixer-rapido@alee.local/helper'
sys.path.insert(0, str(ROOT))
import numpy as np
import soundfile as sf
from inference import Models


class ModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.models = Models()
        cls.speech, cls.rate = sf.read(ROOT / 'models/speechbrain/example1.wav', dtype='float32')

    def test_01_silence_is_unknown(self):
        r = self.models.predict(np.zeros(16000, np.float32), None, .65)
        print('silence:', json.dumps(r), flush=True)
        self.assertEqual(r['class'], 'INDEFINIDO')
        self.assertEqual(r['narrator'], 'SEM_AMOSTRA')

    def test_02_real_speech_not_assumed_narrator(self):
        r = self.models.predict(self.speech, None, .65)
        print('speech:', json.dumps(r), flush=True)
        self.assertEqual(r['class'], 'VOZ')
        self.assertEqual(r['narrator'], 'SEM_AMOSTRA')

    def test_03_sine_is_sound(self):
        x = (.6 * np.sin(2 * np.pi * 440 * np.arange(48000) / 16000)).astype(np.float32)
        r = self.models.predict(x, None, .65)
        print('tone:', json.dumps(r), flush=True)
        self.assertEqual(r['class'], 'SONS')

    def test_04_music_is_sound(self):
        import subprocess
        path = Path(__file__).resolve().parents[1] / 'test-results/fixtures/music.ogg'
        raw = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path), '-t', '4',
                                      '-ac', '1', '-ar', '16000', '-f', 'f32le', '-'])
        r = self.models.predict(np.frombuffer(raw, dtype='<f4'), None, .65)
        print('music:', json.dumps(r), flush=True)
        self.assertEqual(r['class'], 'SONS')

    def test_05_reference_and_different_voice(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            profile = str(Path(tmp) / 'narrator.npy')
            r = self.models.enroll(self.speech, profile)
            self.assertTrue(r['enrolled'])
            r = self.models.predict(self.speech, profile, .65)
            print('enrolled voice:', json.dumps(r), flush=True)
            self.assertEqual(r['narrator'], 'COMPATÍVEL')
            path = Path(__file__).resolve().parents[1] / 'test-results/fixtures/other-speaker.wav'
            raw = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path), '-t', '3.5',
                                          '-ac', '1', '-ar', '16000', '-f', 'f32le', '-'])
            other = np.frombuffer(raw, dtype='<f4')
            r = self.models.predict(other, profile, .65)
            print('different voice:', json.dumps(r), flush=True)
            self.assertEqual(r['class'], 'VOZ')
            self.assertNotEqual(r['narrator'], 'COMPATÍVEL')


if __name__ == '__main__':
    unittest.main(verbosity=2)
