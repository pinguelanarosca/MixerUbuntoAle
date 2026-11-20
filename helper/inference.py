#!/usr/bin/env python3
"""Bounded, offline-only inference worker; stdout is a JSON-lines protocol."""
import base64
import csv
import json
import os
from pathlib import Path
import sys
import time

os.environ.update(OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1',
                  MKL_NUM_THREADS='1', HF_HUB_OFFLINE='1',
                  HF_HUB_DISABLE_TELEMETRY='1', DO_NOT_TRACK='1')

import numpy as np

from core import classify, RATE

ROOT = Path(__file__).resolve().parent
MODELS = ROOT / 'models'


class Models:
    def __init__(self):
        import onnxruntime as ort
        from ai_edge_litert.interpreter import Interpreter
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = opts.inter_op_num_threads = 1
        self.vad = ort.InferenceSession(str(MODELS / 'silero_vad.onnx'),
                                       sess_options=opts, providers=['CPUExecutionProvider'])
        self.yam = Interpreter(model_path=str(MODELS / 'yamnet.tflite'), num_threads=1)
        self.yam.allocate_tensors()
        self.input = self.yam.get_input_details()[0]
        self.output = next(d for d in self.yam.get_output_details() if d['shape'][-1] == 521)
        with (MODELS / 'yamnet_class_map.csv').open() as f:
            self.labels = [r['display_name'] for r in csv.DictReader(f)]
        self.speech_ids = [i for i, label in enumerate(self.labels) if label in
                           ('Speech', 'Male speech, man speaking', 'Female speech, woman speaking',
                            'Child speech, kid speaking', 'Conversation', 'Narration, monologue')]
        self.music_ids = [i for i, label in enumerate(self.labels) if label in
                          ('Music', 'Singing', 'Musical instrument', 'Background music',
                           'Theme music', 'Video game music')]
        self.speaker = None
        self.references = {}

    def vad_score(self, audio):
        state = np.zeros((2, 1, 128), np.float32)
        context = np.zeros((1, 64), np.float32)
        scores = []
        for start in range(0, len(audio) - 511, 512):
            frame = audio[start:start + 512].reshape(1, -1)
            out, state = self.vad.run(None, {'input': np.concatenate((context, frame), axis=1),
                                           'state': state, 'sr': np.array(RATE, dtype=np.int64)})
            context = frame[:, -64:]
            scores.append(float(out[0][0]))
        # Require sustained speech instead of a single transient.
        return float(np.mean(np.array(scores) >= .5)) if scores else 0.

    def embedding(self, audio):
        if self.speaker is None:
            import torch
            torch.set_num_threads(1)
            from speechbrain.inference.speaker import SpeakerRecognition
            from speechbrain.utils.fetching import FetchConfig
            local = MODELS / 'speechbrain'
            self.speaker = SpeakerRecognition.from_hparams(
                source=str(local), savedir=str(local),
                overrides={'pretrained_path': str(local)},
                fetch_config=FetchConfig(allow_network=False, allow_updates=False),
                run_opts={'device': 'cpu'})
        import torch
        with torch.inference_mode():
            e = self.speaker.encode_batch(torch.from_numpy(audio.copy()).unsqueeze(0))
        e = e.detach().cpu().numpy().reshape(-1)
        return e / max(float(np.linalg.norm(e)), 1e-12)

    def enroll(self, pcm, profile):
        if not 3 * RATE <= len(pcm) <= 60 * RATE:
            raise ValueError('A amostra deve conter entre 3 e 60 segundos de áudio.')
        vad = self.vad_score(pcm)
        if vad < .45:
            raise ValueError('Fala insuficiente na amostra; use uma gravação limpa do narrador.')
        embedding = self.embedding(pcm)
        target = Path(profile)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = target.with_suffix('.tmp')
        with tmp.open('wb') as f:
            np.save(f, embedding, allow_pickle=False)
        tmp.chmod(0o600)
        tmp.replace(target)
        self.references[str(target)] = (target.stat().st_mtime_ns, embedding)
        return {'enrolled': True, 'seconds': len(pcm) / RATE, 'vad': vad}

    def predict(self, audio, profiles, threshold):
        vad = self.vad_score(audio)
        size = int(self.input['shape'][-1])
        vectors = []
        for start in range(0, len(audio) - size + 1, size):
            self.yam.set_tensor(self.input['index'], audio[start:start + size])
            self.yam.invoke()
            vectors.append(self.yam.get_tensor(self.output['index']).reshape(-1))
        if not vectors:
            return {'class': 'INDEFINIDO', 'confidence': 0, 'reason': 'Áudio insuficiente'}
        scores = np.mean(vectors, axis=0)
        speech = float(max(scores[self.speech_ids]))
        music = float(max(scores[self.music_ids]))
        other_ids = [i for i in range(len(scores)) if i not in self.speech_ids + self.music_ids
                     and self.labels[i] != 'Silence']
        other = float(max(scores[other_ids]))
        label, confidence = classify(vad, speech, music, other)
        result = {'class': label, 'confidence': confidence, 'vad': vad,
                  'speech': speech, 'music': music, 'other': other,
                  'top': self.labels[int(np.argmax(scores))], 'narrator': 'SEM_AMOSTRA'}
        if isinstance(profiles, str):
            profiles = [{'id': 'test', 'name': 'Narrador', 'path': profiles}]
        profiles = [p for p in profiles or [] if Path(p['path']).is_file()]
        result['speaker_checked'] = not profiles or label != 'VOZ' or len(audio) >= 3 * RATE
        if profiles:
            result['narrator'] = 'INCERTO'
            if label == 'VOZ' and len(audio) >= 3 * RATE:
                embedding = self.embedding(audio)
                matches = []
                for profile in profiles:
                    path = profile['path']
                    mtime = Path(path).stat().st_mtime_ns
                    if path not in self.references or self.references[path][0] != mtime:
                        self.references[path] = (mtime, np.load(path, allow_pickle=False))
                    score = float(np.dot(embedding, self.references[path][1]))
                    matches.append((score, profile))
                matches.sort(key=lambda x: x[0], reverse=True)
                score, profile = matches[0]
                # Similar voices with tied scores are not an identified narrator.
                ambiguous = len(matches) > 1 and score - matches[1][0] < .05
                compatible = score >= threshold and not ambiguous
                result.update(similarity=score, narrator='COMPATÍVEL' if compatible
                              else 'OUTRA_VOZ' if score < threshold - .15 else 'INCERTO')
                if compatible:
                    result.update(narrator_id=profile['id'], narrator_name=profile['name'])
        return result


def main():
    models = None
    for line in sys.stdin:
        job = {}
        try:
            job = json.loads(line)
            started = time.monotonic()
            if models is None:
                models = Models()
            pcm = np.frombuffer(base64.b64decode(job['pcm'], validate=True), dtype='<i2')
            audio = pcm.astype(np.float32) / 32768.
            if job.get('op') == 'enroll':
                result = models.enroll(audio, job['profile'])
            else:
                result = models.predict(audio, job.get('profiles'), job.get('threshold', .65))
            result['inference_ms'] = round((time.monotonic() - started) * 1000, 2)
            result['id'] = job['id']
        except Exception as e:
            result = {'id': job.get('id'), 'error': f'{type(e).__name__}: {e}'}
        print(json.dumps(result, allow_nan=False), flush=True)


if __name__ == '__main__':
    main()
