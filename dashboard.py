"""Reachy Companion dashboard — a small local web page to watch and control it.

Run:  uv run python dashboard.py
Then open http://localhost:8787 (or http://<mac-ip>:8787 from your phone).
"""

import json
import re
import subprocess
import time
from pathlib import Path

import uvicorn
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

  <div class="controls">
    <button class="primary" id="wake">Wake up</button>
    <button id="restart">Restart</button>
    <button id="sleep">Sleep</button>
  </div>
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

refreshState(); refreshChat();
setInterval(refreshState, 2000);
setInterval(refreshChat, 2000);
</script>
</body>
</html>"""

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8787, log_level="warning")
