#!/usr/bin/env python3
"""Small offline Silero worker. Speech evidence only, never speaker identity."""
import base64
import json
import os
from pathlib import Path
import sys
import time

os.environ.update(OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1')
import numpy as np
import onnxruntime as ort


class FastVAD:
    def __init__(self):
        options = ort.SessionOptions()
        options.intra_op_num_threads = options.inter_op_num_threads = 1
        self.model = ort.InferenceSession(str(Path(__file__).parent / 'models/silero_vad.onnx'),
            sess_options=options, providers=['CPUExecutionProvider'])

    def predict(self, audio):
        state = np.zeros((2, 1, 128), np.float32)
        context = np.zeros((1, 64), np.float32)
        scores = []
        for start in range(0, len(audio)-511, 512):
            frame = audio[start:start+512].reshape(1, -1)
            result, state = self.model.run(None, {
                'input': np.concatenate((context, frame), axis=1), 'state': state,
                'sr': np.array(16000, dtype=np.int64)})
            context = frame[:, -64:]
            scores.append(float(result[0][0]))
        # At least 192ms of sustained high evidence. One impulse or absence of
        # speech cannot select the higher voice gain, nor declare SONS.
        window = scores[-6:]
        confidence = float(np.mean(window)) if window else 0.
        speech = len(window) == 6 and min(window) >= .80 and confidence >= .90
        return {'fast_voice': speech, 'fast_vad': confidence, 'frames': len(scores)}


def main():
    vad = FastVAD()
    for line in sys.stdin:
        job = {}
        try:
            job = json.loads(line)
            started = time.monotonic()
            if job.get('op') == 'warm':
                result = {'ready': True}
            else:
                audio = np.frombuffer(base64.b64decode(job['pcm'], validate=True), dtype='<i2').astype(np.float32)/32768.
                result = vad.predict(audio)
            result.update(id=job['id'], inference_ms=round((time.monotonic()-started)*1000, 2))
        except Exception as error:
            result = {'id': job.get('id'), 'error': str(error)}
        print(json.dumps(result, allow_nan=False), flush=True)


if __name__ == '__main__':
    main()
