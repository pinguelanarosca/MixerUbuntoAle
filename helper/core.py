"""Policy shared by the service and tests. No audio routing takes place here."""
from dataclasses import dataclass
import math

RATE = 16000
CLASSES = ('VOZ', 'SONS', 'INDEFINIDO')


def classify(vad, speech, music, other):
    """Model scores are evidence, not calibrated probabilities of identity."""
    if vad >= .60 and speech >= .35 and speech > music * 1.25:
        return 'VOZ', min(vad, speech)
    if music >= .45 and music > speech * 1.25:
        return 'SONS', music
    if vad <= .20 and other >= .40 and speech < .25:
        return 'SONS', other
    return 'INDEFINIDO', max(speech, music, other)


@dataclass
class Activity:
    active: bool = False
    last_signal: float = 0
    last_onset: float = 0

    def feed(self, rms, peak, now):
        audible = math.isfinite(rms) and rms >= .0007 and peak >= .002
        onset = False
        if audible:
            # Use the silence interval even if the server stopped sending PCM.
            onset = not self.active or now - self.last_signal >= .30
            self.active = True
            self.last_signal = now
            if onset:
                self.last_onset = now
        elif now - self.last_signal >= .30:
            self.active = False
        return onset


def safe_volume(voice, sounds, provisional):
    return max(0, min(voice, sounds, provisional, 100))


def should_apply(track):
    return not track.get('manual_volume', False) and track.get('manage_volume', False)
