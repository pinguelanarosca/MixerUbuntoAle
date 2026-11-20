import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'helper'))
from core import Activity, classify, safe_volume, should_apply


class PolicyTests(unittest.TestCase):
    def test_latest_onset_ignores_creation_order(self):
        older, newer = Activity(), Activity()
        self.assertTrue(newer.feed(.1, .2, 10))
        self.assertTrue(older.feed(.1, .2, 11))
        self.assertGreater(older.last_onset, newer.last_onset)
        self.assertFalse(newer.feed(.1, .2, 10.1))

    def test_silence_and_resume_on_same_stream(self):
        a = Activity()
        self.assertFalse(a.feed(0, 0, 1))
        self.assertTrue(a.feed(.02, .04, 2))
        self.assertFalse(a.feed(.02, .04, 2.1))
        a.feed(0, 0, 2.5)
        self.assertFalse(a.active)
        self.assertTrue(a.feed(.02, .04, 3))

    def test_paused_server_without_pcm(self):
        a = Activity()
        a.feed(.02, .04, 2)
        self.assertTrue(a.feed(.02, .04, 3))

    def test_ambiguous_speech_music_stays_unknown(self):
        self.assertEqual(classify(.7, .6, .6, .1)[0], 'INDEFINIDO')
        self.assertEqual(classify(.3, .8, .1, .1)[0], 'INDEFINIDO')

    def test_sustained_speech_and_music(self):
        self.assertEqual(classify(.9, .9, .1, .1)[0], 'VOZ')
        self.assertEqual(classify(.8, .1, .9, .1)[0], 'SONS')
        self.assertEqual(classify(0, .01, .01, .8)[0], 'SONS')

    def test_manual_and_existing_streams_are_protected(self):
        self.assertFalse(should_apply({'manual_volume': True, 'manage_volume': True}))
        self.assertFalse(should_apply({'manual_volume': False, 'manage_volume': False}))
        self.assertTrue(should_apply({'manual_volume': False, 'manage_volume': True}))

    def test_safe_volume_is_bounded_by_both_defaults(self):
        self.assertEqual(safe_volume(73, 17, 20), 17)
        self.assertEqual(safe_volume(73, 59, 20), 20)
        self.assertEqual(safe_volume(0, 59, 20), 0)


if __name__ == '__main__':
    unittest.main()
