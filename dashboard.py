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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

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
        "frame_fresh": FRAME.exists() and time.time() - FRAME.stat().st_mtime < 10,
    }


@app.get("/api/transcript")
def transcript():
    return parse_transcript()


@app.get("/api/frame")
def frame():
    if FRAME.exists():
        return FileResponse(FRAME, media_type="image/jpeg")
    return JSONResponse({"error": "no frame"}, status_code=404)


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
    checks = {
        "robot": lambda: _http_ok(f"{robot}/api/daemon/status"),
        "brain": lambda: _http_ok(f"{brain_cfg.get('base_url', '')}/models"),
        "eyes": lambda: _http_ok(f"{cfg.get('eyes', {}).get('base_url', '')}/models"),
        "hermes": lambda: shutil.which("hermes") is not None,
    }
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = {k: pool.submit(v) for k, v in checks.items()}
    return {k: f.result() for k, f in results.items()}


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
    <div class="sub" id="sub">…</div>
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
    <label for="brain">Brain</label>
    <select id="brain"></select>
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
    document.getElementById('sub').textContent =
      !s.running ? 'asleep' : (s.person ? 'with ' + s.person : 'watching the room');
    const cam = document.getElementById('cam'), off = document.getElementById('camoff');
    if (s.frame_fresh) {
      cam.src = '/api/frame?t=' + Date.now();
      cam.hidden = false; off.style.display = 'none';
    } else { cam.hidden = true; off.style.display = 'flex'; }
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
      return `<div class="msg ${m.who}"><div class="meta">${name} · ${m.time}</div>${esc(m.text)}</div>`;
    }).join('');
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
    document.querySelectorAll('.chip').forEach(c => {
      const k = c.dataset.k;
      c.classList.toggle('ok', !!h[k]);
      c.classList.toggle('bad', !h[k]);
    });
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
