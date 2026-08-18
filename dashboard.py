"""Reachy Companion dashboard — a small local web page to watch and control it.

Run:  uv run python dashboard.py
Then open http://localhost:8787 (or http://<mac-ip>:8787 from your phone).
"""

import json
import re
import shutil
import subprocess
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import uvicorn
import yaml
from pydantic import BaseModel
from fastapi import FastAPI
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    StreamingResponse,
)

ROOT = Path(__file__).resolve().parent
LOG = Path("/tmp/reachy-companion.log")
STATUS = Path("/tmp/reachy-status.json")
FRAME = Path("/tmp/reachy-latest.jpg")

app = FastAPI()

LINE = re.compile(
    r"^(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ companion (?P<rest>.*)$"
)


def parse_transcript(max_items: int = 60):
    if not LOG.exists():
        return []
    lines = LOG.read_text(errors="ignore").splitlines()[-600:]
    items = []
    for line in lines:
        m = LINE.match(line)
        if not m:
            continue
        t, rest = m.group("time")[11:16], m.group("rest")
        if rest.startswith("reachy: [hermes]"):
            items.append({"who": "event", "time": t, "text": "asked Hermes for help…"})
        elif rest.startswith("reachy: "):
            text = re.sub(r"^\[voice:[a-z_]+\]\s*", "", rest[8:])
            items.append({"who": "reachy", "time": t, "text": text})
        elif rest.startswith("hermes: "):
            items.append({"who": "reachy", "time": t, "text": rest[8:], "via": "hermes"})
        elif " said: " in rest:
            who, text = rest.split(" said: ", 1)
            items.append({"who": "person", "name": who, "time": t, "text": text})
        elif rest.startswith("greeting "):
            items.append({"who": "event", "time": t, "text": f"spotted {rest[9:]} — saying hi"})
        elif rest.startswith("companion is up"):
            items.append({"who": "event", "time": t, "text": "woke up"})
        elif rest.startswith("good night"):
            items.append({"who": "event", "time": t, "text": "went to sleep"})
        elif rest.startswith("voice switched"):
            items.append({"who": "event", "time": t, "text": rest})
    return items[-max_items:]


def companion_running() -> bool:
    return (
        subprocess.run(["pgrep", "-f", "companion.main"], capture_output=True).returncode
        == 0
    )


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


@app.get("/api/state")
def state():
    status = {}
    if STATUS.exists():
        try:
            status = json.loads(STATUS.read_text())
        except Exception:
            pass
    fresh = time.time() - status.get("updated", 0) < 10
    return {
        "running": companion_running(),
        "person": status.get("person") if fresh else None,
        "activity": status.get("activity") if fresh else None,
        "frame_fresh": FRAME.exists() and time.time() - FRAME.stat().st_mtime < 30,
    }


def _find_battery(obj):
    """Scan robot status JSON for anything battery/charge-shaped."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if any(w in k.lower() for w in ("battery", "charge", "soc")) and isinstance(
                v, (int, float)
            ):
                return v
            found = _find_battery(v)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_battery(item)
            if found is not None:
                return found
    return None


@app.get("/api/transcript")
def transcript():
    return parse_transcript()


@app.get("/api/frame")
def frame():
    if FRAME.exists():
        return FileResponse(FRAME, media_type="image/jpeg")
    return JSONResponse({"error": "no frame"}, status_code=404)


@app.get("/api/stream")
def stream():
    """Live MJPEG stream of what Reachy sees (~5 fps)."""

    def frames():
        last = b""
        while True:
            try:
                data = FRAME.read_bytes()
                if data and data != last:
                    last = data
                    yield (
                        b"--frame\r\nContent-Type: image/jpeg\r\n"
                        + f"Content-Length: {len(data)}\r\n\r\n".encode()
                        + data
                        + b"\r\n"
                    )
            except Exception:
                pass
            time.sleep(0.2)

    return StreamingResponse(
        frames(), media_type="multipart/x-mixed-replace; boundary=frame"
    )


VOICES = [
    "af_heart", "af_bella", "af_nicole", "af_sky",
    "am_michael", "am_adam", "am_eric",
    "bf_emma", "bm_george", "bm_lewis",
]
CMD = Path("/tmp/reachy-cmd.json")


def _cfg() -> dict:
    return yaml.safe_load((ROOT / "config.yaml").read_text())


def _http_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


class Say(BaseModel):
    text: str


class VoicePick(BaseModel):
    voice: str


class Enroll(BaseModel):
    name: str


@app.get("/api/health")
def health():
    cfg = _cfg()
    robot = f"http://{cfg['robot']['host']}:{cfg['robot']['port']}"
    brain_cfg = cfg["brain"]["models"].get(cfg["brain"].get("active"), {})

    def robot_check():
        """Reachability + link quality (ms) + battery if the daemon shows one."""
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(f"{robot}/api/daemon/status", timeout=2) as r:
                data = json.load(r)
            ms = int((time.perf_counter() - t0) * 1000)
            return {"ok": True, "ms": ms, "battery": _find_battery(data)}
        except Exception:
            return {"ok": False, "ms": None, "battery": None}

    checks = {
        "robot": robot_check,
        "brain": lambda: _http_ok(f"{brain_cfg.get('base_url', '')}/models"),
        "eyes": lambda: (
            companion_running()
            if cfg.get("eyes", {}).get("local_model")
            else _http_ok(f"{cfg.get('eyes', {}).get('base_url', '')}/models")
        ),
        "hermes": lambda: shutil.which("hermes") is not None,
    }
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = {k: pool.submit(v) for k, v in checks.items()}
    out = {k: f.result() for k, f in results.items()}
    robot_info = out.pop("robot")
    out["robot"] = robot_info["ok"]
    out["robot_ms"] = robot_info["ms"]
    out["battery"] = robot_info["battery"]
    return out


@app.post("/api/say")
def say(body: Say):
    if not companion_running():
        return JSONResponse({"error": "Reachy is asleep"}, status_code=409)
    CMD.write_text(json.dumps({"say": body.text.strip()[:400]}))
    return {"ok": True}


@app.get("/api/people")
def people():
    profiles = yaml.safe_load((ROOT / "profiles.yaml").read_text())
    known = []
    store = ROOT / "data" / "faces.npz"
    if store.exists():
        with np.load(store) as z:
            known = list(z.files)
    return {
        "voices": VOICES,
        "people": [
            {
                "name": n,
                "voice": profiles.get("people", {}).get(n, {}).get(
                    "voice", profiles.get("unknown", {}).get("voice", "af_sky")
                ),
            }
            for n in known
        ],
    }


@app.post("/api/people/{name}/voice")
def set_voice(name: str, body: VoicePick):
    if body.voice not in VOICES:
        return JSONResponse({"error": "unknown voice"}, status_code=400)
    path = ROOT / "profiles.yaml"
    profiles = yaml.safe_load(path.read_text())
    profiles.setdefault("people", {}).setdefault(
        name, {"speed": 1.0, "style": f"This is {name}. Be friendly with them."}
    )["voice"] = body.voice
    path.write_text(
        "# People Reachy knows — managed from the dashboard.\n"
        + yaml.safe_dump(profiles, sort_keys=False, allow_unicode=True)
    )
    return {"ok": True}


@app.post("/api/people/enroll")
def enroll(body: Enroll):
    name = body.name.strip()[:40]
    if not name:
        return JSONResponse({"error": "empty name"}, status_code=400)
    if not companion_running():
        return JSONResponse({"error": "Reachy is asleep"}, status_code=409)
    CMD.write_text(json.dumps({"enroll": name}))
    return {"ok": True}


class Volume(BaseModel):
    volume: int


@app.get("/api/volume")
def get_volume():
    cfg = _cfg()
    url = f"http://{cfg['robot']['host']}:{cfg['robot']['port']}/api/volume/current"
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return json.load(r)
    except Exception:
        return JSONResponse({"error": "robot unreachable"}, status_code=502)


@app.post("/api/volume")
def set_volume_ep(body: Volume):
    cfg = _cfg()
    url = f"http://{cfg['robot']['host']}:{cfg['robot']['port']}/api/volume/set"
    req = urllib.request.Request(
        url,
        data=json.dumps({"volume": max(0, min(100, body.volume))}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.load(r)
    except Exception:
        return JSONResponse({"error": "robot unreachable"}, status_code=502)


@app.get("/api/mic")
def get_mic():
    cfg = _cfg()
    robot = f"http://{cfg['robot']['host']}:{cfg['robot']['port']}"
    out = {"level": 0.0}
    try:
        with urllib.request.urlopen(f"{robot}/api/volume/microphone/current", timeout=3) as r:
            out.update(json.load(r))
    except Exception:
        pass
    try:
        status = json.loads(STATUS.read_text())
        if time.time() - status.get("updated", 0) < 10:
            out["level"] = status.get("mic", 0.0)
    except Exception:
        pass
    return out


@app.post("/api/mic")
def set_mic(body: Volume):
    cfg = _cfg()
    url = f"http://{cfg['robot']['host']}:{cfg['robot']['port']}/api/volume/microphone/set"
    req = urllib.request.Request(
        url,
        data=json.dumps({"volume": max(0, min(100, body.volume))}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.load(r)
    except Exception:
        return JSONResponse({"error": "robot unreachable"}, status_code=502)


MODES = Path("/tmp/reachy-modes.json")


class Modes(BaseModel):
    wake_word: bool | None = None
    vigilante: bool | None = None


@app.get("/api/modes")
def get_modes():
    try:
        return {"wake_word": False, "vigilante": False, **json.loads(MODES.read_text())}
    except Exception:
        return {"wake_word": False, "vigilante": False}


@app.post("/api/modes")
def set_modes(body: Modes):
    current = get_modes()
    for k, v in body.model_dump(exclude_none=True).items():
        current[k] = v
    MODES.write_text(json.dumps(current))
    return current


@app.post("/api/estop")
def estop():
    subprocess.run(["pkill", "-9", "-f", "companion.main"], capture_output=True)
    cfg = _cfg()
    robot = f"http://{cfg['robot']['host']}:{cfg['robot']['port']}"
    for path in ("/api/move/stop", "/api/motors/set_mode/disabled"):
        try:
            urllib.request.urlopen(
                urllib.request.Request(f"{robot}{path}", method="POST"), timeout=3
            )
        except Exception:
            pass
    return {"ok": True}


@app.get("/api/models")
def models():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    brain = cfg.get("brain", {})
    return {
        "active": brain.get("active"),
        "options": [
            {"key": k, "label": v.get("label", k)}
            for k, v in brain.get("models", {}).items()
        ],
    }


class NewModel(BaseModel):
    label: str
    base_url: str
    model: str
    api_key: str | None = None


@app.post("/api/models/add")
def add_model(body: NewModel):
    label = body.label.strip()[:60]
    base_url = body.base_url.strip().rstrip("/")
    model = body.model.strip()
    if not (label and base_url.startswith("http") and model):
        return JSONResponse({"error": "need label, http(s) url, and model"}, status_code=400)
    key = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:30] or "custom"
    path = ROOT / "config.yaml"
    cfg = yaml.safe_load(path.read_text())
    if key in cfg["brain"]["models"]:
        return JSONResponse({"error": f"'{key}' already exists"}, status_code=400)
    # insert as text right under `  models:` so comments stay intact
    block = (
        f"    {key}:\n"
        f"      label: {json.dumps(label)}\n"
        f"      base_url: {json.dumps(base_url)}\n"
        f"      model: {json.dumps(model)}\n"
    )
    if body.api_key:
        block += f"      api_key: {json.dumps(body.api_key.strip())}\n"
    text = path.read_text()
    new_text = re.sub(
        r"(?m)^(  models:)$",
        lambda m: m.group(1) + "\n" + block.rstrip(),
        text,
        count=1,
    )
    if new_text == text:
        return JSONResponse({"error": "could not find models section"}, status_code=500)
    yaml.safe_load(new_text)  # sanity: still valid yaml
    path.write_text(new_text)
    return {"ok": True, "key": key}


@app.post("/api/models/{key}")
def set_model(key: str):
    path = ROOT / "config.yaml"
    cfg = yaml.safe_load(path.read_text())
    if key not in cfg.get("brain", {}).get("models", {}):
        return JSONResponse({"error": "unknown model"}, status_code=400)
    # line-level replace keeps comments and formatting intact
    text = re.sub(r"(?m)^(  active: ).*$", rf"\g<1>{key}", path.read_text(), count=1)
    path.write_text(text)
    subprocess.Popen([str(ROOT / "restart.sh")])
    return {"ok": True, "active": key}


@app.post("/api/control/{action}")
def control(action: str):
    if action == "sleep":
        subprocess.Popen([str(ROOT / "restart.sh"), "stop"])
    elif action in ("wake", "restart"):
        subprocess.Popen([str(ROOT / "restart.sh")])
    else:
        return JSONResponse({"error": "unknown action"}, status_code=400)
    return {"ok": True}


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Reachy</title>
<style>
  :root {
    --bg: #16130f;
    --panel: #1f1a15;
    --panel-2: #262019;
    --text: #ece4d8;
    --dim: #97897a;
    --warm: #e8a854;
    --warm-soft: rgba(232, 168, 84, .14);
    --green: #7fbf7f;
    --ease-out: cubic-bezier(0.23, 1, 0.32, 1);
  }
  * { box-sizing: border-box; margin: 0; }
  body {
    background: var(--bg); color: var(--text);
    font: 15px/1.5 -apple-system, "SF Pro Text", system-ui, sans-serif;
    min-height: 100dvh; display: flex; justify-content: center;
    padding: 20px 16px calc(20px + env(safe-area-inset-bottom));
  }
  .wrap { width: 100%; max-width: 480px; display: flex; flex-direction: column; gap: 14px; }

  header { display: flex; align-items: center; gap: 12px; padding: 2px 4px; }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: #6b5f50; flex: none;
         transition: background 300ms var(--ease-out), box-shadow 300ms var(--ease-out); }
  .dot.on { background: var(--green); box-shadow: 0 0 10px rgba(127,191,127,.5); }
  h1 { font-size: 19px; font-weight: 650; letter-spacing: -0.02em; }
  .sub { color: var(--dim); font-size: 13px; margin-left: auto; text-align: right; }

  .cam {
    position: relative; border-radius: 16px; overflow: hidden; background: var(--panel);
    aspect-ratio: 16/9;
  }
  .cam img { width: 100%; height: 100%; object-fit: cover; display: block;
             transition: opacity 300ms var(--ease-out); }
  .cam .off { position: absolute; inset: 0; display: flex; align-items: center;
              justify-content: center; color: var(--dim); font-size: 13px;
              background: var(--panel); }

  .chat {
    background: var(--panel); border-radius: 16px; padding: 14px;
    display: flex; flex-direction: column; gap: 8px;
    height: 44dvh; overflow-y: auto; scroll-behavior: smooth;
  }
  .msg { max-width: 86%; padding: 8px 12px; border-radius: 14px; font-size: 14px;
         opacity: 1; transform: translateY(0);
         transition: opacity 250ms var(--ease-out), transform 250ms var(--ease-out); }
  @starting-style { .msg { opacity: 0; transform: translateY(6px); } }
  .msg .meta { font-size: 11px; color: var(--dim); margin-bottom: 2px; }
  .msg.person { align-self: flex-end; background: var(--panel-2); border-bottom-right-radius: 4px; }
  .msg.reachy { align-self: flex-start; background: var(--warm-soft); border-bottom-left-radius: 4px; }
  .msg.reachy .meta { color: var(--warm); }
  .msg.event { align-self: center; background: none; color: var(--dim); font-size: 12px;
               padding: 0 6px; }

  .health { display: flex; gap: 8px; }
  .chip { flex: 1; display: flex; align-items: center; gap: 7px; justify-content: center;
          background: var(--panel); border-radius: 11px; padding: 8px 4px;
          font-size: 12px; color: var(--dim); }
  .chip .light { width: 7px; height: 7px; border-radius: 50%; background: #6b5f50;
                 transition: background 300ms var(--ease-out); }
  .chip.ok .light { background: var(--green); }
  .chip.bad .light { background: #d96a5a; }

  .sayrow { display: flex; gap: 8px; }
  .sayrow input {
    flex: 1; background: var(--panel); color: var(--text); border: 0;
    border-radius: 13px; padding: 12px 14px; font: 14px/1 -apple-system, system-ui, sans-serif;
    outline: none;
  }
  .sayrow input::placeholder { color: var(--dim); }
  #addmodelform input {
    background: var(--panel-2); color: var(--text); border: 0;
    border-radius: 9px; padding: 9px 11px; font: 13px/1 -apple-system, system-ui, sans-serif;
    outline: none;
  }
  #addmodelform input::placeholder { color: var(--dim); }
  .sayrow button { flex: none; padding: 0 18px; }

  .people { background: var(--panel); border-radius: 16px; padding: 6px 14px; }
  .person { display: flex; align-items: center; gap: 10px; padding: 9px 0;
            border-bottom: 1px solid rgba(255,255,255,.04); }
  .person:last-child { border-bottom: 0; }
  .person .pname { flex: 1; font-size: 14px; font-weight: 550; }
  .person select { flex: none; width: 130px; }
  .addrow { padding: 10px 0; }
  .addrow button { width: 100%; background: none; color: var(--dim);
                   border: 1px dashed #3a322a; }

  .estop {
    width: 100%; padding: 14px 0; border-radius: 13px;
    background: rgba(217, 106, 90, .15); color: #e08a7c;
    font-weight: 650; letter-spacing: 0.02em;
  }
  @media (hover: hover) and (pointer: fine) {
    .estop:hover { background: rgba(217, 106, 90, .28); }
  }

  .brainrow { display: flex; align-items: center; gap: 10px;
              background: var(--panel); border-radius: 13px; padding: 10px 14px; }
  .brainrow label { color: var(--dim); font-size: 13px; flex: none; }
  select {
    flex: 1; background: var(--panel-2); color: var(--text); border: 0;
    border-radius: 9px; padding: 8px 10px; font: 500 13px/1 -apple-system, system-ui, sans-serif;
    appearance: none; -webkit-appearance: none; cursor: pointer;
  }
  .controls { display: flex; gap: 10px; }
  button {
    flex: 1; padding: 12px 0; border: 0; border-radius: 13px; cursor: pointer;
    background: var(--panel-2); color: var(--text);
    font: 600 14px/1 -apple-system, system-ui, sans-serif;
    transition: transform 140ms var(--ease-out), background 140ms var(--ease-out);
  }
  button:active { transform: scale(0.97); }
  button.primary { background: var(--warm); color: #241a0d; }
  @media (hover: hover) and (pointer: fine) {
    button:hover { background: #2e2720; }
    button.primary:hover { background: #f0b465; }
  }
  @media (prefers-reduced-motion: reduce) {
    .msg { transition: opacity 200ms ease; transform: none; }
  }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="dot" id="dot"></div>
    <h1>Reachy</h1>
    <div class="sub">
      <span id="batt" style="margin-right:8px;"></span>
      <span id="sub">…</span>
    </div>
  </header>

  <div class="cam">
    <img id="cam" alt="" hidden>
    <div class="off" id="camoff">camera asleep</div>
  </div>

  <div class="chat" id="chat"></div>

  <div class="health" id="health">
    <div class="chip" data-k="robot"><div class="light"></div>robot</div>
    <div class="chip" data-k="brain"><div class="light"></div>brain</div>
    <div class="chip" data-k="eyes"><div class="light"></div>eyes</div>
    <div class="chip" data-k="hermes"><div class="light"></div>hermes</div>
  </div>

  <form class="sayrow" id="sayform">
    <input id="saytext" placeholder="Make Reachy say something…" autocomplete="off">
    <button class="primary" type="submit">Say</button>
  </form>

  <div class="people" id="people"></div>

  <div class="brainrow">
    <label for="vol">Volume</label>
    <input type="range" id="vol" min="0" max="100" step="5"
           style="flex:1; accent-color: var(--warm);">
    <span id="volval" style="color:var(--dim); font-size:13px; width:32px; text-align:right;">–</span>
  </div>

  <div class="brainrow" style="flex-direction:column; align-items:stretch; gap:7px;">
    <div style="display:flex; align-items:center; gap:10px;">
      <label for="mic">Mic</label>
      <input type="range" id="mic" min="0" max="100" step="5"
             style="flex:1; accent-color: var(--warm);">
      <span id="micval" style="color:var(--dim); font-size:13px; width:32px; text-align:right;">–</span>
    </div>
    <div style="height:5px; border-radius:3px; background:var(--panel-2); overflow:hidden;">
      <div id="miclevel" style="height:100%; width:0%; border-radius:3px;
           background:var(--green); transition: width 400ms var(--ease-out);"></div>
    </div>
  </div>

  <div class="health" id="modes">
    <div class="chip toggle" data-m="wake_word"><div class="light"></div>wake word</div>
    <div class="chip toggle" data-m="vigilante"><div class="light"></div>vigilante</div>
  </div>

  <div class="brainrow" style="flex-direction:column; align-items:stretch; gap:8px;">
    <div style="display:flex; align-items:center; gap:10px;">
      <label for="brain">Brain</label>
      <select id="brain"></select>
      <button id="addmodeltoggle" style="flex:none; padding:6px 12px; font-size:12px;">+ add</button>
    </div>
    <div id="addmodelform" hidden style="display:flex; flex-direction:column; gap:6px;">
      <input id="nm-label" placeholder="Name (e.g. GLM on Spark 2)">
      <input id="nm-url" placeholder="Address (e.g. http://10.0.0.5:8000/v1)">
      <input id="nm-model" placeholder="Model name (e.g. glm-5.2)">
      <input id="nm-key" placeholder="API key (leave empty for local)">
      <button class="primary" id="nm-save">Save model</button>
      <div id="nm-msg" style="color:var(--dim); font-size:12px;"></div>
    </div>
  </div>

  <div class="controls">
    <button class="primary" id="wake">Wake up</button>
    <button id="restart">Restart</button>
    <button id="sleep">Sleep</button>
  </div>

  <button class="estop" id="estop">EMERGENCY STOP</button>
</div>

<script>
const chat = document.getElementById('chat');
let lastKey = '';

function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

async function refreshState() {
  try {
    const s = await (await fetch('/api/state')).json();
    document.getElementById('dot').classList.toggle('on', s.running);
    // live status line: what is it doing right now?
    let sub = 'asleep';
    if (s.running) {
      if (s.activity && s.activity !== 'idle') sub = s.activity;
      else sub = s.person ? 'watching ' + s.person : 'watching the room';
    }
    document.getElementById('sub').textContent = sub;
    const cam = document.getElementById('cam'), off = document.getElementById('camoff');
    if (s.running) {
      // keep the stream mounted; only overlay a note when frames lag
      if (!cam.src.includes('/api/stream')) cam.src = '/api/stream';
      cam.hidden = false;
      off.style.display = s.frame_fresh ? 'none' : 'flex';
      off.style.background = s.frame_fresh ? '' : 'transparent';
      off.textContent = s.frame_fresh ? '' : 'catching up…';
    } else {
      cam.removeAttribute('src'); cam.hidden = true;
      off.style.display = 'flex'; off.style.background = '';
      off.textContent = 'camera asleep';
    }
  } catch (e) {}
}

async function refreshChat() {
  try {
    const items = await (await fetch('/api/transcript')).json();
    const key = JSON.stringify(items.slice(-3)) + items.length;
    if (key === lastKey) return;
    lastKey = key;
    chat.innerHTML = items.map(m => {
      if (m.who === 'event')
        return `<div class="msg event">${esc(m.text)}</div>`;
      const name = m.who === 'reachy' ? (m.via ? 'Reachy · via Hermes' : 'Reachy') : esc(m.name || 'You');
      const replay = m.who === 'reachy' ? ' replayable" style="cursor:pointer" title="tap to make Reachy say this again' : '';
      return `<div class="msg ${m.who}${replay}"><div class="meta">${name} · ${m.time}</div>${esc(m.text)}</div>`;
    }).join('');
    // tap a Reachy bubble → it says it again
    chat.querySelectorAll('.msg.replayable').forEach(el => {
      el.onclick = () => fetch('/api/say', { method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ text: el.childNodes[1].textContent }) });
    });
    chat.scrollTop = chat.scrollHeight;
  } catch (e) {}
}

function bind(id, action) {
  document.getElementById(id).onclick = () =>
    fetch('/api/control/' + action, { method: 'POST' }).then(() => setTimeout(refreshState, 1500));
}
bind('wake', 'wake'); bind('restart', 'restart'); bind('sleep', 'sleep');

const brainSel = document.getElementById('brain');
async function loadModels() {
  try {
    const m = await (await fetch('/api/models')).json();
    brainSel.innerHTML = m.options.map(o =>
      `<option value="${o.key}" ${o.key === m.active ? 'selected' : ''}>${esc(o.label)}</option>`
    ).join('');
  } catch (e) {}
}
brainSel.onchange = () =>
  fetch('/api/models/' + brainSel.value, { method: 'POST' })
    .then(() => setTimeout(refreshState, 2000));
loadModels();

// --- add model ---
document.getElementById('addmodeltoggle').onclick = () => {
  const f = document.getElementById('addmodelform');
  f.hidden = !f.hidden;
};
document.getElementById('nm-save').onclick = async () => {
  const msg = document.getElementById('nm-msg');
  const body = {
    label: document.getElementById('nm-label').value,
    base_url: document.getElementById('nm-url').value,
    model: document.getElementById('nm-model').value,
    api_key: document.getElementById('nm-key').value || null,
  };
  const r = await fetch('/api/models/add', { method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body) });
  const j = await r.json();
  if (r.ok) {
    msg.textContent = 'saved — it\\'s in the list now';
    ['nm-label','nm-url','nm-model','nm-key'].forEach(id => document.getElementById(id).value = '');
    loadModels();
  } else msg.textContent = j.error || 'something went wrong';
};

// --- say ---
document.getElementById('sayform').onsubmit = async (e) => {
  e.preventDefault();
  const input = document.getElementById('saytext');
  const text = input.value.trim();
  if (!text) return;
  const r = await fetch('/api/say', { method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ text }) });
  if (r.ok) input.value = '';
  else input.placeholder = 'Reachy is asleep — wake it first';
};

// --- health ---
async function refreshHealth() {
  try {
    const h = await (await fetch('/api/health')).json();
    document.querySelectorAll('#health .chip').forEach(c => {
      const k = c.dataset.k;
      c.classList.toggle('ok', !!h[k]);
      c.classList.toggle('bad', !h[k]);
      if (k === 'robot') {
        // show link quality — tonight's WiFi lesson
        const ms = h.robot_ms;
        c.childNodes[1].textContent = ms == null ? 'robot' :
          'robot ' + ms + 'ms' + (ms > 80 ? ' ⚠' : '');
      }
    });
    const batt = document.getElementById('batt');
    batt.textContent = h.battery != null ? '🔋 ' + Math.round(h.battery) + '%' : '';
  } catch (e) {}
}
refreshHealth();
setInterval(refreshHealth, 10000);

// --- people ---
async function refreshPeople() {
  try {
    const p = await (await fetch('/api/people')).json();
    const rows = p.people.map(person => `
      <div class="person">
        <div class="pname">${esc(person.name)}</div>
        <select data-name="${esc(person.name)}">
          ${p.voices.map(v => `<option ${v === person.voice ? 'selected' : ''}>${v}</option>`).join('')}
        </select>
      </div>`).join('');
    document.getElementById('people').innerHTML = rows +
      `<div class="addrow"><button id="addperson">+ teach Reachy a new face</button></div>`;
    document.querySelectorAll('.person select').forEach(sel => {
      sel.onchange = () => fetch('/api/people/' + encodeURIComponent(sel.dataset.name) + '/voice',
        { method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ voice: sel.value }) });
    });
    document.getElementById('addperson').onclick = async () => {
      const name = prompt('Who is Reachy meeting? (have them sit in front of the camera)');
      if (!name) return;
      const r = await fetch('/api/people/enroll', { method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ name }) });
      alert(r.ok ? 'Reachy is looking — hold still for ~10 seconds!' : 'Reachy is asleep — wake it first.');
      setTimeout(refreshPeople, 15000);
    };
  } catch (e) {}
}
refreshPeople();

// --- volume ---
const vol = document.getElementById('vol'), volval = document.getElementById('volval');
fetch('/api/volume').then(r => r.json()).then(v => {
  if (v.volume !== undefined) { vol.value = v.volume; volval.textContent = v.volume; }
});
vol.oninput = () => volval.textContent = vol.value;
let volTimer;
vol.onchange = () => {
  clearTimeout(volTimer);
  volTimer = setTimeout(() =>
    fetch('/api/volume', { method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ volume: +vol.value }) }), 300);
};

// --- mic ---
const mic = document.getElementById('mic'), micval = document.getElementById('micval');
const miclevel = document.getElementById('miclevel');
fetch('/api/mic').then(r => r.json()).then(v => {
  if (v.volume !== undefined) { mic.value = v.volume; micval.textContent = v.volume; }
});
mic.oninput = () => micval.textContent = mic.value;
let micTimer;
mic.onchange = () => {
  clearTimeout(micTimer);
  micTimer = setTimeout(() =>
    fetch('/api/mic', { method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ volume: +mic.value }) }), 300);
};
setInterval(async () => {
  try {
    const v = await (await fetch('/api/mic')).json();
    // level is RMS 0..~0.3 — map to a useful bar
    miclevel.style.width = Math.min(100, Math.round((v.level || 0) * 400)) + '%';
  } catch (e) {}
}, 1000);

// --- modes ---
async function refreshModes() {
  try {
    const m = await (await fetch('/api/modes')).json();
    document.querySelectorAll('.chip.toggle').forEach(c =>
      c.classList.toggle('ok', !!m[c.dataset.m]));
  } catch (e) {}
}
document.querySelectorAll('.chip.toggle').forEach(c => {
  c.style.cursor = 'pointer';
  c.onclick = async () => {
    const on = !c.classList.contains('ok');
    await fetch('/api/modes', { method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ [c.dataset.m]: on }) });
    refreshModes();
  };
});
refreshModes();

// --- emergency stop ---
document.getElementById('estop').onclick = () =>
  fetch('/api/estop', { method: 'POST' }).then(() => setTimeout(refreshState, 1000));

refreshState(); refreshChat();
setInterval(refreshState, 2000);
setInterval(refreshChat, 2000);
</script>
</body>
</html>"""

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8787, log_level="warning")
