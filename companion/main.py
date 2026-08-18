"""Reachy desk companion.

Idles at the desk, turns toward whoever is talking (mic-array direction),
recognizes faces, greets people by name in a per-person voice, and holds
short spoken conversations through local models only:

    ears   -> whisper (Mac)          eyes  -> insightface (Mac)
    brain  -> Qwen3.8 (DGX Spark)    voice -> kokoro (Mac)

Run:  uv run python -m companion.main
"""

import json
import logging
import math
import random
import re
import threading
import time
import urllib.request

from reachy_mini import ReachyMini

from .brain import Brain
from .config import load_config, load_profiles, resolve_brain
from .ears import Ears
from .eyes import Eyes
from .faces import FaceMemory, face_center
from .voice import Voice

log = logging.getLogger("companion")


class Companion:
    def __init__(self):
        cfg = load_config()
        self.cfg = cfg
        self.profiles = load_profiles()
        log.info("loading eyes (insightface)...")
        self.faces = FaceMemory(cfg["faces"]["match_threshold"])
        log.info("loading voice (kokoro)...")
        self.voice = Voice(cfg["voice"])
        self.ears = Ears(cfg["ears"])
        self.brain = Brain(resolve_brain(cfg))
        self.eyes = Eyes(cfg["eyes"]) if "eyes" in cfg else None
        self.greet_cooldown = float(cfg["faces"]["greet_cooldown"])
        self.scan_interval = float(cfg["faces"]["scan_interval"])
        self.idle_interval = float(cfg["behavior"]["idle_interval"])
        self.last_greeted: dict[str, float] = {}
        self.current_person: str | None = None
        # remember who we last saw for a while, even off-camera
        self.last_seen_at = 0.0
        self.identity_memory = 120.0
        # voice switches requested mid-conversation ("change your voice")
        self.voice_overrides: dict[str, str] = {}
        # meeting new people
        self.unknown_streak = 0
        self.last_meet_attempt = 0.0
        self.meet_cooldown = 300.0
        # wake word + vigilante state
        self.last_interaction = 0.0
        self.follow_up_window = 45.0
        self.prev_gray = None
        self.last_motion_alert = 0.0
        # face tracking — one mover at a time, or daemon tasks preempt each
        # other and raise TimeoutError
        self.busy = False
        self.last_tracked = 0.0
        self.motion_lock = threading.Lock()
        # live mic level for the dashboard meter
        self.mic_rms = 0.0
        # connection watchdog
        self.last_frame_at = time.monotonic()
        # what we're doing right now — shown on the dashboard status line
        self.activity = "waking up"

    @staticmethod
    def modes() -> dict:
        """Runtime toggles written by the dashboard — no restart needed."""
        try:
            with open("/tmp/reachy-modes.json") as f:
                return {"wake_word": False, "vigilante": False, **json.load(f)}
        except (FileNotFoundError, json.JSONDecodeError):
            return {"wake_word": False, "vigilante": False}

    def person_profile(self, name: str | None) -> tuple[str, dict]:
        if name and name in self.profiles["people"]:
            return name, self.profiles["people"][name]
        return name or "someone new", self.profiles["unknown"]

    # ---------- behaviors ----------

    def get_doa(self) -> tuple[float, bool] | None:
        """Sound direction from the daemon (SDK's get_DoA is flaky over WebRTC)."""
        url = f"http://{self.cfg['robot']['host']}:{self.cfg['robot']['port']}/api/state/doa"
        try:
            with urllib.request.urlopen(url, timeout=1.0) as r:
                d = json.load(r)
            return d["angle"], d["speech_detected"]
        except Exception:
            return None

    def turn_toward_sound(self, mini, angle: float):
        # face tracking owns the head while someone is in view
        if time.monotonic() - self.last_tracked < 2.0:
            return
        # DOA: 0 = left, pi/2 = front, pi = right. Body yaw: positive = left.
        yaw = max(-1.4, min(1.4, math.pi / 2 - angle))
        if abs(yaw) > 0.25:
            if not self.motion_lock.acquire(blocking=False):
                return
            try:
                mini.goto_target(body_yaw=yaw, duration=0.6)
            except Exception:
                pass
            finally:
                self.motion_lock.release()

    def look_at_face(self, mini, face):
        u, v = face_center(face)
        try:
            mini.look_at_image(u=u, v=v, duration=0.5)
        except Exception:
            pass

    def idle_wiggle(self, mini):
        if not self.motion_lock.acquire(blocking=False):
            return
        try:
            # while watching someone, only the antennas fidget
            if time.monotonic() - self.last_tracked < 5.0:
                a = random.uniform(0.1, 0.4)
                mini.goto_target(antennas=[-a, a], duration=0.6)
                mini.goto_target(antennas=[-0.1745, 0.1745], duration=0.6)
                return
            r = random.random()
            if r < 0.5:
                a = random.uniform(0.1, 0.5)
                mini.goto_target(antennas=[-a, a], duration=0.7)
                mini.goto_target(antennas=[-0.1745, 0.1745], duration=0.7)
            else:
                yaw = random.uniform(-0.5, 0.5)
                mini.goto_target(body_yaw=yaw, duration=1.2)
                mini.goto_target(body_yaw=0.0, duration=1.2)
        except Exception:
            pass
        finally:
            self.motion_lock.release()

    def share_state(self, frame):
        """Drop a snapshot + status file for the dashboard."""
        try:
            if frame is not None:
                import os

                import cv2

                # atomic swap so the dashboard never reads a half-written jpeg
                cv2.imwrite("/tmp/reachy-latest.tmp.jpg", frame)
                os.replace("/tmp/reachy-latest.tmp.jpg", "/tmp/reachy-latest.jpg")
            with open("/tmp/reachy-status.json", "w") as f:
                json.dump(
                    {
                        "person": self.current_person,
                        "voices": self.voice_overrides,
                        "mic": round(self.mic_rms, 4),
                        "activity": self.activity,
                        "updated": time.time(),
                    },
                    f,
                )
        except Exception:
            pass

    def check_motion(self, frame):
        """Vigilante mode: spot movement, snapshot it, send it to your phone."""
        import cv2

        gray = cv2.cvtColor(cv2.resize(frame, (320, 180)), cv2.COLOR_BGR2GRAY)
        prev, self.prev_gray = self.prev_gray, gray
        if prev is None:
            return
        score = float(cv2.absdiff(prev, gray).mean())
        now = time.monotonic()
        if score > 12.0 and now - self.last_motion_alert > 60:
            self.last_motion_alert = now
            import datetime
            import subprocess

            from .config import ROOT

            snapdir = ROOT / "data" / "vigilante"
            snapdir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            path = snapdir / f"{stamp}.jpg"
            cv2.imwrite(str(path), frame)
            log.info("vigilante: motion (score %.1f) → %s", score, path.name)
            subprocess.Popen(
                ["hermes", "send", "-t", "telegram", "-q",
                 f"👁 Reachy vigilante: movement spotted at {stamp[9:11]}:{stamp[11:13]} MEDIA:{path}"],
            )

    def _track_loop(self, mini, stop):
        """Reflex thread: keep the talking face centered.

        The camera image lags the head's motion by a few hundred ms, so we
        never chase the face at full speed — each step corrects a fraction
        of the offset (damped P-control) and small offsets are ignored.
        Full-speed chasing oscillates; this converges.
        """
        GAIN = 0.7
        DEADBAND = 0.11  # fraction of frame size — a wide calm zone
        CMD_SPACING = 0.55  # let each move show up on camera before the next
        last_pos = None  # face position last seen (u, v)
        last_cmd = 0.0
        lost_since = None
        while not stop.is_set():
            stop.wait(0.12)
            if self.busy:
                lost_since = None
                continue
            try:
                frame = mini.media.get_frame()
                if frame is None:
                    continue
                h, w = frame.shape[:2]
                cx, cy = w / 2, h / 2
                boxes = self.faces.detect_boxes(frame)
                if len(boxes) == 0:
                    # face gone — don't hunt (relative glances ratchet the head
                    # away until it faces the wall). Recenter once and wait.
                    now = time.monotonic()
                    if lost_since is None:
                        lost_since = now
                    elif now - lost_since > 2.5:
                        # reset state FIRST so a failed move can't spam retries
                        lost_since = None
                        last_pos = None
                        self.last_tracked = 0.0
                        log.info("track: face lost — recentering")
                        from reachy_mini.reachy_mini import INIT_HEAD_POSE

                        with self.motion_lock:
                            mini.goto_target(
                                head=INIT_HEAD_POSE, body_yaw=0.0, duration=1.2
                            )
                    continue
                lost_since = None

                def center(b):
                    return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)

                # sticky: stay with the face we were following
                if last_pos is not None:
                    box = min(
                        boxes,
                        key=lambda b: (center(b)[0] - last_pos[0]) ** 2
                        + (center(b)[1] - last_pos[1]) ** 2,
                    )
                else:
                    box = max(boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
                u, v = center(box)
                now = time.monotonic()
                last_pos = (u, v)
                # settle with the gaze a touch lower — eye contact, not hairline
                dx, dy = u - cx, v - cy + 0.12 * h
                self.last_tracked = now
                if now - last_cmd < CMD_SPACING:
                    continue  # previous move hasn't reached the camera yet
                if abs(dx) < DEADBAND * w and -0.05 * h < dy < DEADBAND * h:
                    continue  # close enough — hold still
                aim_u = max(0, min(w - 1, cx + GAIN * dx))
                aim_v = max(0, min(h - 1, cy + GAIN * dy))
                with self.motion_lock:
                    mini.look_at_image(u=int(aim_u), v=int(aim_v), duration=0.45)
                last_cmd = now
            except Exception:
                if time.monotonic() - getattr(self, "_track_err_at", 0) > 30:
                    self._track_err_at = time.monotonic()
                    log.exception("track loop error")

    def _snapshot_loop(self, mini, stop):
        """Background thread: keep the dashboard's camera view fresh even
        while the main loop is busy listening or talking."""
        while not stop.is_set():
            try:
                frame = mini.media.get_frame()
                if frame is not None:
                    self.last_frame_at = time.monotonic()
                self.share_state(frame)
            except Exception:
                pass
            stop.wait(0.25)

    def scan_faces(self, mini):
        frame = mini.media.get_frame()
        if frame is None:
            return
        if self.modes()["vigilante"]:
            self.check_motion(frame)
        found = self.faces.biggest_face(frame)
        if found is None:
            return
        name, _sim, face = found
        now = time.monotonic()
        if name is not None:
            self.current_person = name
            self.last_seen_at = now
            self.unknown_streak = 0
        elif now - self.last_seen_at > self.identity_memory:
            # only forget a known person after a couple of minutes unseen
            self.current_person = None
        if name is None:
            # a stranger keeps appearing — introduce ourselves and learn them
            # a familiar person moving fast can look "unknown" for a few
            # frames — only introduce ourselves after a solid streak
            self.unknown_streak += 1
            if (
                self.unknown_streak >= 8
                and now - self.last_seen_at > 30
                and now - self.last_meet_attempt > self.meet_cooldown
                and not self.modes()["wake_word"]  # don't initiate in quiet mode
            ):
                self.meet_new_person(mini)
            return
        self.unknown_streak = 0
        now = time.monotonic()
        # in wake-word mode, only the wake-up greeting is allowed
        if self.modes()["wake_word"] and now - self.started_at > 60:
            return
        if now - self.last_greeted.get(name, -1e9) > self.greet_cooldown:
            self.last_greeted[name] = now
            person, profile = self.person_profile(name)
            log.info("greeting %s", person)
            self.busy = True
            self.activity = "speaking…"
            try:
                text = self.brain.greeting(person, profile["style"])
                self.voice.speak(mini, text, profile["voice"], profile.get("speed"))
            finally:
                self.busy = False
                self.activity = "idle"

    VOICES = [
        "af_heart", "af_bella", "af_nicole", "af_sky",
        "am_michael", "am_adam", "am_eric",
        "bf_emma", "bm_george", "bm_lewis",
    ]

    def assign_voice(self, name: str) -> str:
        """Give a newly met person their own voice and save their profile."""
        used = {p.get("voice") for p in self.profiles.get("people", {}).values()}
        voice = next((v for v in self.VOICES if v not in used), "af_heart")
        self.profiles.setdefault("people", {})[name] = {
            "voice": voice,
            "speed": 1.0,
            "style": f"This is {name}. You just met them — be friendly.",
        }
        import yaml

        from .config import ROOT

        (ROOT / "profiles.yaml").write_text(
            "# People Reachy knows — managed from the dashboard.\n"
            + yaml.safe_dump(self.profiles, sort_keys=False, allow_unicode=True)
        )
        return voice

    def meet_new_person(self, mini):
        """Someone unfamiliar keeps appearing — ask who they are and learn them."""
        self.busy = True
        try:
            self._meet_new_person(mini)
        finally:
            self.busy = False

    def _meet_new_person(self, mini):
        self.last_meet_attempt = time.monotonic()
        self.unknown_streak = 0
        log.info("meeting a new face")
        self.voice.speak(mini, "Hi there! I don't think we've met yet. What's your name?")
        self.ears.drain(mini)
        audio = self.ears.record_utterance(mini)
        text = self.ears.transcribe(audio) if audio is not None else ""
        name = self.brain.extract_name(text) if text else None
        if not name:
            log.info("no name caught (heard: %r)", text)
            self.voice.speak(mini, "No worries — we can meet another time!")
            return
        if name in self.faces.known:
            self.voice.speak(mini, f"Oh, {name} — I know a {name} already! I'll get better at telling you apart.")
            return
        self.voice.speak(
            mini, f"Nice to meet you, {name}! Hold still a moment while I remember your face."
        )
        embeddings = []
        deadline = time.monotonic() + 20
        while len(embeddings) < 10 and time.monotonic() < deadline:
            frame = mini.media.get_frame()
            if frame is not None:
                found = self.faces.biggest_face(frame)
                if found is not None and found[0] is None:
                    embeddings.append(found[2].normed_embedding)
            time.sleep(0.3)
        if len(embeddings) < 5:
            self.voice.speak(mini, "Hmm, I couldn't get a clear look. Another time!")
            return
        self.faces.enroll(name, embeddings)
        voice = self.assign_voice(name)
        self.current_person = name
        self.last_seen_at = time.monotonic()
        log.info("auto-enrolled %s with voice %s", name, voice)
        self.voice.speak(mini, f"Got it — I'll remember you now, {name}!", voice)

    def converse(self, mini):
        self.busy = True
        try:
            self._converse(mini)
        finally:
            self.busy = False
            self.activity = "idle"

    def _converse(self, mini):
        self.activity = "listening…"
        audio = self.ears.record_utterance(mini)
        if audio is None:
            return
        self.activity = "thinking…"
        # take a quick look at who's talking before answering
        anyone_visible = False
        frame = mini.media.get_frame()
        if frame is not None:
            found = self.faces.biggest_face(frame)
            if found:
                anyone_visible = True
                if found[0] is not None:
                    self.current_person = found[0]
                    self.last_seen_at = time.monotonic()
        # no face in view and nobody seen recently → probably a TV or
        # background chatter, not someone talking to us
        if not anyone_visible and (
            self.current_person is None
            or time.monotonic() - self.last_seen_at > self.identity_memory
        ):
            log.info("ignored speech (nobody in view)")
            return
        text = self.ears.transcribe(audio)
        if len(text) < 2:
            return
        # wake-word mode: only respond when addressed (or mid-conversation)
        if self.modes()["wake_word"]:
            lowered = text.lower()
            addressed = any(w in lowered for w in ("reachy", "richie", "ritchie", "reach"))
            in_conversation = time.monotonic() - self.last_interaction < self.follow_up_window
            if not addressed and not in_conversation:
                log.info("ignored (wake-word mode, not addressed): %s", text)
                return
        self.last_interaction = time.monotonic()
        person, profile = self.person_profile(self.current_person)
        log.info("%s said: %s", person, text)
        answer = self.brain.reply(person, profile["style"], text)
        log.info("reachy: %s", answer)
        # vision questions go to the Cosmos eyes with a fresh camera frame
        if answer.lstrip().lower().startswith("[look]") and self.eyes:
            question = answer.lstrip()[6:].strip() or text
            frame = mini.media.get_frame()
            voice = self.voice_overrides.get(person, profile["voice"])
            if frame is None:
                answer = "My camera is coming up blank right now."
            else:
                self.activity = "looking…"
                log.info("looking: %s", question)
                try:
                    answer = self.eyes.look(frame, question)
                except Exception:
                    answer = "My eyes aren't answering right now."
                log.info("eyes: %s", answer)
            self.brain.histories[person][-1]["content"] = answer
            self.voice.speak(mini, answer, voice, profile.get("speed"))
            return
        # real tasks get handed to the Hermes agent
        if answer.lstrip().lower().startswith("[hermes]"):
            task = answer.lstrip()[8:].strip()
            if len(task) < 10:  # tag misfire with no real task — ignore it
                log.info("empty hermes tag, dropping")
                self.brain.histories[person].pop()
                return
            voice = self.voice_overrides.get(person, profile["voice"])
            self.voice.speak(mini, "Let me look into that.", voice, profile.get("speed"))
            self.activity = "asking Hermes…"
            log.info("asking hermes: %s", task)
            answer = self.brain.ask_hermes(person, task)
            log.info("hermes: %s", answer)
            # keep the spoken result in conversation memory, not the tag
            self.brain.histories[person][-1]["content"] = answer
        # the model may ask to switch voice with a leading [voice:name] tag
        voice = self.voice_overrides.get(person, profile["voice"])
        m = re.match(r"\s*\[voice:([a-z]{2}_[a-z]+)\]\s*", answer)
        if m:
            voice = m.group(1)
            self.voice_overrides[person] = voice
            answer = answer[m.end():]
            log.info("voice switched to %s for %s", voice, person)
        if answer:
            self.activity = "speaking…"
            self.voice.speak(mini, answer, voice, profile.get("speed"))

    def check_commands(self, mini):
        """Commands dropped by the dashboard: say something, enroll a face."""
        cmd_path = "/tmp/reachy-cmd.json"
        try:
            with open(cmd_path) as f:
                cmd = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return
        import os

        os.remove(cmd_path)
        self.busy = True
        try:
            self._run_command(mini, cmd)
        finally:
            self.busy = False

    def _run_command(self, mini, cmd: dict):
        if text := cmd.get("say"):
            log.info("dashboard says: %s", text)
            person, profile = self.person_profile(self.current_person)
            voice = cmd.get("voice") or self.voice_overrides.get(person, profile["voice"])
            self.voice.speak(mini, text, voice)
        elif name := cmd.get("enroll"):
            log.info("enrolling %s from dashboard", name)
            self.voice.speak(mini, f"Hold still, {name} — I'm learning your face.")
            embeddings = []
            deadline = time.monotonic() + 30
            while len(embeddings) < 10 and time.monotonic() < deadline:
                frame = mini.media.get_frame()
                if frame is not None:
                    found = self.faces.biggest_face(frame)
                    if found is not None:
                        embeddings.append(found[2].normed_embedding)
                time.sleep(0.3)
            if len(embeddings) >= 5:
                self.faces.enroll(name, embeddings)
                self.voice.speak(mini, f"Got it. Nice to meet you, {name}!")
                log.info("enrolled %s", name)
            else:
                self.voice.speak(mini, "I couldn't see a face clearly, sorry.")
                log.info("enroll failed for %s", name)

    # ---------- main loop ----------

    def run(self):
        self.started_at = time.monotonic()
        log.info("connecting to reachy at %s...", self.cfg["robot"]["host"])
        with ReachyMini(
            host=self.cfg["robot"]["host"],
            port=self.cfg["robot"]["port"],
            connection_mode="network",
            media_backend="webrtc",
        ) as mini:
            mini.enable_motors()
            mini.wake_up()
            mini.media.start_recording()
            self.ears.drain(mini)
            import threading

            snap_stop = threading.Event()
            threading.Thread(
                target=self._snapshot_loop, args=(mini, snap_stop), daemon=True
            ).start()
            threading.Thread(
                target=self._track_loop, args=(mini, snap_stop), daemon=True
            ).start()
            self.activity = "idle"
            log.info("companion is up — ctrl-c to stop")
            last_scan = last_idle = 0.0
            try:
                while True:
                    doa = self.get_doa()
                    if doa is not None:
                        angle, speech = doa
                        if speech:
                            self.turn_toward_sound(mini, angle)
                            self.converse(mini)
                            self.ears.drain(mini)
                            # linger: catch follow-up questions asked right
                            # after the reply, before going back to idle
                            linger = time.monotonic()
                            while time.monotonic() - linger < 2.5:
                                doa2 = self.get_doa()
                                if doa2 is not None and doa2[1]:
                                    self.converse(mini)
                                    self.ears.drain(mini)
                                    linger = time.monotonic()
                                time.sleep(0.05)
                            last_idle = time.monotonic()
                            continue
                    # watchdog: if the camera feed has been dead for 30s the
                    # connection is wedged — bail out so we reconnect fresh
                    if time.monotonic() - self.last_frame_at > 30:
                        raise ConnectionError("media stalled — reconnecting")
                    self.check_commands(mini)
                    # drain idle mic audio and measure the room's loudness
                    peak = 0.0
                    sample = mini.media.get_audio_sample()
                    while sample is not None:
                        mono = sample.mean(axis=1) if sample.ndim == 2 else sample
                        peak = max(peak, float((mono**2).mean() ** 0.5))
                        sample = mini.media.get_audio_sample()
                    if peak:
                        self.mic_rms = peak
                    now = time.monotonic()
                    if now - last_scan > self.scan_interval:
                        last_scan = now
                        self.scan_faces(mini)
                    if now - last_idle > self.idle_interval:
                        last_idle = now
                        self.idle_wiggle(mini)
                    time.sleep(0.05)
            except KeyboardInterrupt:
                log.info("good night")
            finally:
                snap_stop.set()
                for cleanup in (mini.media.stop_recording, mini.goto_sleep):
                    try:
                        cleanup()
                    except Exception:
                        pass


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(message)s"
    )
    # survive robot hiccups: reconnect instead of dying
    while True:
        try:
            Companion().run()
            break  # clean exit (ctrl-c / sleep)
        except KeyboardInterrupt:
            break
        except Exception:
            log.exception("companion crashed — reconnecting in 10s")
            time.sleep(10)


if __name__ == "__main__":
    main()
