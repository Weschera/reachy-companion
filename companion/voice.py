"""Text-to-speech through the robot's speaker.

Kokoro synthesizes at 24 kHz mono; the robot speaker wants 16 kHz stereo
float32, so we resample and duplicate channels before pushing.
"""

import numpy as np

from .config import ROOT

ROBOT_RATE = 16000


class Voice:
    def __init__(self, cfg: dict):
        from kokoro_onnx import Kokoro

        self.kokoro = Kokoro(
            str(ROOT / cfg["model"]),
            str(ROOT / cfg["voices"]),
        )
        self.default_voice = cfg.get("default_voice", "af_heart")
        self.default_speed = float(cfg.get("default_speed", 1.0))

    def synthesize(self, text: str, voice: str | None = None, speed: float | None = None):
        """Return float32 stereo samples at 16 kHz, shape (N, 2)."""
        samples, rate = self.kokoro.create(
            text,
            voice=voice or self.default_voice,
            speed=speed or self.default_speed,
        )
        samples = np.asarray(samples, dtype=np.float32)
        if rate != ROBOT_RATE:
            n_out = int(len(samples) * ROBOT_RATE / rate)
            samples = np.interp(
                np.linspace(0, len(samples) - 1, n_out),
                np.arange(len(samples)),
                samples,
            ).astype(np.float32)
        return np.stack([samples, samples], axis=1)

    def speak(self, mini, text: str, voice: str | None = None, speed: float | None = None):
        """Speak through the robot, with the head wobbling along."""
        import time

        stereo = self.synthesize(text, voice, speed)
        duration = len(stereo) / ROBOT_RATE
        mini.enable_wobbling()
        try:
            mini.media.start_playing()
            # push in ~0.5s chunks so wobble tracking stays lively
            chunk = ROBOT_RATE // 2
            for i in range(0, len(stereo), chunk):
                mini.media.push_audio_sample(stereo[i : i + chunk])
            # let the audio actually finish playing before tearing down
            time.sleep(duration + 0.3)
            mini.media.stop_playing()
        finally:
            mini.disable_wobbling()
        return duration  # seconds of speech
