"""Demo mode — for filming. Follows your face, and when you speak it says
its one perfect line. Nothing else can go wrong.

    uv run python demo.py
"""

import logging
import time

from reachy_mini import ReachyMini

from companion.config import load_config
from companion.faces import FaceMemory
from companion.voice import Voice

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("demo")

LINE = (
    "I'm running on Qwen three point eight, twenty seven B, served from a "
    "DGX Spark right over there. My eyes, "
    "ears and voice all run on the Mac Studio. Everything is one hundred "
    "percent local — nothing ever leaves this room. No cloud at all."
)

GAIN = 0.7
DEADBAND = 0.11
CMD_SPACING = 0.55


def main():
    cfg = load_config()
    log.info("loading eyes + voice...")
    faces = FaceMemory()
    voice = Voice(cfg["voice"])
    log.info("connecting...")
    with ReachyMini(
        host=cfg["robot"]["host"],
        port=cfg["robot"]["port"],
        connection_mode="network",
        media_backend="webrtc",
    ) as mini:
        mini.enable_motors()
        mini.wake_up()
        log.info("DEMO READY — it follows you; speak and it says its line")
        last_cmd = 0.0
        said_at = 0.0
        last_seen = 0.0
        last_dbg = 0.0
        import json
        import math
        import urllib.request

        doa_url = f"http://{cfg['robot']['host']}:{cfg['robot']['port']}/api/state/doa"
        while True:
            try:
                # --- listen: turn toward the voice, and say the line ---
                try:
                    with urllib.request.urlopen(doa_url, timeout=0.5) as r:
                        d = json.load(r)
                    if d["speech_detected"]:
                        # no face in view? swing the body toward the voice
                        if time.monotonic() - last_seen > 2.0:
                            yaw = max(-1.4, min(1.4, math.pi / 2 - d["angle"]))
                            log.info("voice from angle %.2f — turning body %.2f", d["angle"], yaw)
                            mini.goto_target(body_yaw=yaw, duration=0.7)
                        if time.monotonic() - said_at > 12:
                            time.sleep(2.0)  # let them finish the question
                            log.info("speaking the line")
                            voice.speak(mini, LINE, "am_michael")
                            said_at = time.monotonic()
                except Exception:
                    pass
                # --- follow the face ---
                frame = mini.media.get_frame()
                if frame is None:
                    time.sleep(0.1)
                    continue
                h, w = frame.shape[:2]
                cx, cy = w / 2, h / 2
                boxes = faces.detect_boxes(frame)
                now = time.monotonic()
                if now - last_dbg > 2:
                    last_dbg = now
                    log.info("faces in view: %d", len(boxes))
                    import cv2

                    dbg = frame.copy()
                    for b in boxes:
                        cv2.rectangle(dbg, (int(b[0]), int(b[1])), (int(b[2]), int(b[3])), (0, 255, 0), 3)
                        cv2.putText(dbg, f"{b[4]:.2f}", (int(b[0]), int(b[1]) - 8),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
                    cv2.imwrite("/tmp/reachy-demo-view.tmp.jpg", dbg)
                    import os

                    os.replace("/tmp/reachy-demo-view.tmp.jpg", "/tmp/reachy-demo-view.jpg")
                if len(boxes) == 0:
                    # lost you (probably stood up out of frame) — look back
                    # up to neutral so it can find you again
                    if last_seen and now - last_seen > 1.5:
                        log.info("face lost — recentering to neutral")
                        from reachy_mini.reachy_mini import INIT_HEAD_POSE

                        try:
                            mini.goto_target(head=INIT_HEAD_POSE, duration=0.8)
                        except Exception:
                            pass
                        last_seen = 0.0
                    time.sleep(0.1)
                    continue
                x1, y1, x2, y2, _ = max(
                    boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1])
                )
                u, v = (x1 + x2) / 2, (y1 + y2) / 2
                last_seen = now
                dx, dy = u - cx, v - cy + 0.12 * h
                if now - last_cmd < CMD_SPACING:
                    time.sleep(0.05)
                    continue
                if abs(dx) < DEADBAND * w and -0.05 * h < dy < DEADBAND * h:
                    time.sleep(0.05)
                    continue
                mini.look_at_image(
                    u=int(max(0, min(w - 1, cx + GAIN * dx))),
                    v=int(max(0, min(h - 1, cy + GAIN * dy))),
                    duration=0.45,
                )
                last_cmd = now
            except KeyboardInterrupt:
                break
            except Exception:
                time.sleep(0.3)
        mini.goto_sleep()


if __name__ == "__main__":
    main()
