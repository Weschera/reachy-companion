# Reachy Companion

Reachy Mini as a desk companion — 100% local AI, no cloud.

- **Eyes**: robot camera → insightface on the Mac. Recognizes who's at the desk.
- **Ears**: robot mic array (echo-cancelled) → mlx-whisper on the Mac. The
  DOA sensor tells Reachy which direction speech came from, so it turns to face you.
- **Brain**: Qwen3.8-27B on the DGX Spark (`10.0.0.109:8219`, the Hermes provider).
- **Voice**: kokoro TTS on the Mac → robot speaker, with head-wobble while talking.
  **Each person gets their own voice** — mapped in `profiles.yaml`.

## Run

```bash
cd ~/code/reachy-companion
uv run python -m companion.main
```

First run downloads the insightface model pack (~300MB) and the whisper model.

## Teach it a face

```bash
uv run python enroll.py "Raul"
```

Stand in front of the robot, it captures 10 frames. Then edit `profiles.yaml`
to pick that person's voice and personality flavor.

## Files

| file | what |
|---|---|
| `config.yaml` | robot IP, brain endpoint, thresholds |
| `profiles.yaml` | people → voice + personality |
| `data/faces.npz` | learned face embeddings |
| `models/` | kokoro TTS model files |

## Next up (phase 2)

- **Spatial awareness**: NVIDIA Cosmos-Reason2 on a spare Spark — Reachy
  glances around and actually understands the scene ("Raul just picked up
  the tattoo machine"). Hook goes in `companion/main.py` where faces are
  scanned.
- Proactive chiming-in (time, reminders) via Hermes.
