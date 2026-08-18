"""Listening: record from the robot's mics until silence, then transcribe.

The robot streams 16 kHz stereo float32 with echo cancellation already
applied, so Reachy's own voice doesn't count as speech.
"""

import time

import numpy as np

RATE = 16000


class Ears:
    def __init__(self, cfg: dict):
        self.whisper_model = cfg["whisper_model"]
        self.silence_after = float(cfg["silence_after"])
        self.max_utterance = float(cfg["max_utterance"])
        self.min_rms = float(cfg["min_rms"])

    def drain(self, mini):
        """Throw away any buffered mic audio."""
        while mini.media.get_audio_sample() is not None:
            pass

    def record_utterance(self, mini) -> np.ndarray | None:
        """Record until the speaker goes quiet. Returns 16 kHz mono float32."""
        chunks = []
        started = time.monotonic()
        last_loud = started
        while True:
            sample = mini.media.get_audio_sample()
            if sample is None:
                time.sleep(0.01)
            else:
                mono = sample.mean(axis=1) if sample.ndim == 2 else sample
                chunks.append(mono.astype(np.float32))
                rms = float(np.sqrt(np.mean(mono**2)))
                if rms > self.min_rms:
                    last_loud = time.monotonic()
            now = time.monotonic()
            if now - last_loud > self.silence_after and now - started > 1.0:
                break
            if now - started > self.max_utterance:
                break
        if not chunks:
            return None
        audio = np.concatenate(chunks)
        if len(audio) < RATE // 2:  # under half a second — noise
            return None
        return audio

    def transcribe(self, audio: np.ndarray) -> str:
        import mlx_whisper

        result = mlx_whisper.transcribe(
            audio, path_or_hf_repo=self.whisper_model, language="en"
        )
        text = result["text"].strip()
        # whisper sometimes hallucinates loops ("face face face...") on noise
        words = text.lower().split()
        if len(words) > 8 and len(set(words)) / len(words) < 0.3:
            return ""
        return text
