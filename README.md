# Reachy Companion

Turn a [Reachy Mini](https://huggingface.co/docs/reachy_mini) into a living desk
companion — powered entirely by **local AI**. No cloud. Your robot, your models,
your network.

https://github.com/Weschera/reachy-companion

## What it does

- 👀 **Recognizes people** — face recognition on every camera frame; greets
  people by name, tracks the speaker's face with smooth, predictive head motion
- 🗣 **Per-person voices** — every person gets their own TTS voice and
  personality flavor; Reachy can even switch its voice when asked
- 🧠 **Local brain** — any OpenAI-compatible endpoint (a model on your own
  hardware); fast replies with conversation memory per person
- 🤝 **Agent handoff** — real tasks ("search my notes", "look that up") are
  delegated to a [Hermes](https://github.com/NousResearch) agent via a `[hermes]`
  tag, then spoken back
- 🙋 **Meets strangers** — an unfamiliar face gets "Hi, I don't think we've
  met — what's your name?", then face + voice are remembered automatically
- 👁 **Vision hook** — `[look]` questions route a camera frame to a VLM
  (e.g. NVIDIA Cosmos-Reason2) for real scene understanding
- 🖥 **Web dashboard** — live camera, conversation transcript, health lights,
  speak-through-the-robot box, people manager, model picker, volume + mic
  meter, wake-word & vigilante toggles, emergency stop
- 🦹 **Vigilante mode** — motion detection → snapshot → sent to your phone
  (Telegram via Hermes)
- 🎙 **Wake-word mode** — Siri-style: only responds when addressed by name

## The stack (all local)

| Sense | Runs on | Model |
|---|---|---|
| Ears | your computer | mlx-whisper (large-v3-turbo) |
| Eyes (faces) | your computer | insightface buffalo_l |
| Eyes (understanding) | any GPU box | Cosmos-Reason2 / any VLM via vLLM |
| Voice | your computer | kokoro TTS (50+ voices) |
| Brain | any GPU box | any OpenAI-compatible endpoint |

Built on a Mac Studio + DGX Spark boxes, but any setup with an
OpenAI-compatible LLM endpoint works.

## Setup

```bash
git clone https://github.com/Weschera/reachy-companion
cd reachy-companion
brew install gstreamer pygobject3 gobject-introspection cairo  # macOS
uv sync

# voice model files (~350MB)
mkdir -p models
curl -L -o models/kokoro-v1.0.onnx  https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx
curl -L -o models/voices-v1.0.bin   https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin

cp config.example.yaml config.yaml     # then edit: robot IP, brain endpoint
cp profiles.example.yaml profiles.yaml # then edit: people + voices
```

## Run

```bash
./restart.sh                     # start (or restart) the companion
uv run python dashboard.py       # dashboard at http://localhost:8787
uv run python enroll.py "Name"   # teach it a face (or use the dashboard)
./restart.sh stop                # sleep
```

`install-autostart.sh` installs launchd jobs (macOS) so the dashboard is always
up and a supervisor revives the companion if it crashes — while still
respecting the Sleep button.

## Troubleshooting

- **GStreamer segfault at startup**: rename
  `.venv/.../gstreamer_python/lib/gstreamer-1.0/libgstpython.dylib` out of the
  way — it's not needed and crashes on macOS.
- **SDK `get_DoA()` returns None over WebRTC**: the companion polls the
  daemon's REST endpoint (`/api/state/doa`) instead.
- **Robot audio format**: 16 kHz stereo float32 — kokoro's 24 kHz mono output
  is resampled automatically.

## Status

**v0.1** — first working version. One evening from unboxing to a talking,
face-tracking desk companion. Expect rough edges; issues and PRs welcome.
