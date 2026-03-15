import os
import json
import queue
import threading
import logging
from pathlib import Path
from flask import Flask, render_template_string, Response, jsonify, request
from processor import start_watcher

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("foto-splitter")

app = Flask(__name__)

INPUT_DIR = Path(os.environ.get("INPUT_DIR", "/data/input"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/data/output"))
PROMINENCE = int(os.environ.get("PROMINENCE", "35"))
PADDING = int(os.environ.get("PADDING", "0"))
ROTATION = int(os.environ.get("ROTATION", "0"))

INPUT_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# SSE event queue (one per connected client)
_clients: list[queue.Queue] = []
_clients_lock = threading.Lock()
_log: list[dict] = []  # last 200 events

def broadcast(data: dict):
    _log.append(data)
    if len(_log) > 200:
        _log.pop(0)
    with _clients_lock:
        for q in _clients:
            try:
                q.put_nowait(data)
            except queue.Full:
                pass

# Start watcher in background
observer = start_watcher(INPUT_DIR, OUTPUT_DIR, PROMINENCE, PADDING, ROTATION, broadcast)

HTML = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Foto Splitter</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #f5f5f5; color: #1a1a1a; min-height: 100vh; }
  header { background: #fff; border-bottom: 1px solid #e0e0e0;
           padding: 1rem 2rem; display: flex; align-items: center; gap: 1rem; }
  header h1 { font-size: 1.1rem; font-weight: 600; }
  .badge { font-size: 11px; padding: 3px 8px; border-radius: 99px;
           background: #e8f5e9; color: #2e7d32; font-weight: 500; }
  .badge.warn { background: #fff3e0; color: #e65100; }
  main { max-width: 900px; margin: 2rem auto; padding: 0 1.5rem; }
  .cards { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-bottom: 1.5rem; }
  .card { background: #fff; border-radius: 10px; border: 1px solid #e0e0e0;
          padding: 1.25rem 1.5rem; }
  .card h2 { font-size: 12px; color: #666; text-transform: uppercase;
             letter-spacing: .05em; margin-bottom: .5rem; }
  .card .val { font-size: 2rem; font-weight: 600; }
  .card .path { font-size: 12px; color: #888; margin-top: 4px; word-break: break-all; }
  .log-wrap { background: #fff; border-radius: 10px; border: 1px solid #e0e0e0; overflow: hidden; }
  .log-head { padding: .75rem 1.25rem; border-bottom: 1px solid #e0e0e0;
              display: flex; justify-content: space-between; align-items: center; }
  .log-head h2 { font-size: 14px; font-weight: 600; }
  #log { height: 420px; overflow-y: auto; padding: .75rem 0; font-family: monospace; font-size: 13px; }
  .row { padding: 4px 1.25rem; display: flex; gap: .75rem; align-items: baseline;
         border-left: 3px solid transparent; }
  .row.done { border-color: #4caf50; }
  .row.error { border-color: #f44336; background: #fef2f2; }
  .row.warning { border-color: #ff9800; }
  .row.start { border-color: #2196f3; }
  .time { color: #aaa; flex-shrink: 0; }
  .file { font-weight: 500; }
  .saved { color: #666; font-size: 12px; }
  .settings { margin-top: 1.5rem; background: #fff; border-radius: 10px;
              border: 1px solid #e0e0e0; padding: 1.25rem 1.5rem; }
  .settings h2 { font-size: 14px; font-weight: 600; margin-bottom: 1rem; }
  .settings form { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px,1fr)); gap: 1rem; }
  .field label { display: block; font-size: 12px; color: #666; margin-bottom: 4px; }
  .field input, .field select { width: 100%; padding: 7px 10px; border: 1px solid #ddd;
                                border-radius: 6px; font-size: 14px; }
  .btn { margin-top: 1rem; padding: 8px 20px; background: #1a1a1a; color: #fff;
         border: none; border-radius: 6px; font-size: 14px; cursor: pointer; }
  .btn:hover { background: #333; }
  #status-dot { width: 8px; height: 8px; border-radius: 50%; background: #4caf50;
                display: inline-block; animation: pulse 2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
  @media(max-width:600px){ .cards{grid-template-columns:1fr;} }
</style>
</head>
<body>
<header>
  <span id="status-dot"></span>
  <h1>Foto Splitter</h1>
  <span class="badge" id="watcher-badge">Überwacht</span>
</header>
<main>
  <div class="cards">
    <div class="card">
      <h2>Eingang</h2>
      <div class="path">{{ input_dir }}</div>
    </div>
    <div class="card">
      <h2>Ausgang</h2>
      <div class="path">{{ output_dir }}</div>
    </div>
    <div class="card">
      <h2>Verarbeitet heute</h2>
      <div class="val" id="count-done">0</div>
    </div>
    <div class="card">
      <h2>Fotos gespeichert</h2>
      <div class="val" id="count-photos">0</div>
    </div>
  </div>

  <div class="log-wrap">
    <div class="log-head">
      <h2>Live-Log</h2>
      <button class="btn" onclick="clearLog()" style="padding:4px 12px;font-size:12px;">Leeren</button>
    </div>
    <div id="log"></div>
  </div>

  <div class="settings">
    <h2>Einstellungen</h2>
    <form id="settings-form">
      <div class="field">
        <label>Empfindlichkeit (prominence)</label>
        <input type="number" name="prominence" value="{{ prominence }}" min="5" max="100">
      </div>
      <div class="field">
        <label>Rand (px)</label>
        <input type="number" name="padding" value="{{ padding }}" min="0" max="100">
      </div>
      <div class="field">
        <label>Rotation</label>
        <select name="rotation">
          <option value="0" {% if rotation==0 %}selected{% endif %}>Keine</option>
          <option value="90" {% if rotation==90 %}selected{% endif %}>90° im Uhrzeigersinn</option>
          <option value="-90" {% if rotation==-90 %}selected{% endif %}>90° gegen Uhrzeigersinn</option>
          <option value="180" {% if rotation==180 %}selected{% endif %}>180°</option>
        </select>
      </div>
    </form>
    <button class="btn" onclick="saveSettings()">Speichern & neu starten</button>
  </div>
</main>

<script>
let countDone = 0, countPhotos = 0;

function ts() {
  return new Date().toLocaleTimeString('de-DE');
}

function addRow(cls, file, msg) {
  const log = document.getElementById('log');
  const div = document.createElement('div');
  div.className = 'row ' + cls;
  div.innerHTML = `<span class="time">${ts()}</span><span class="file">${file}</span><span class="saved">${msg}</span>`;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

function clearLog() {
  document.getElementById('log').innerHTML = '';
}

const es = new EventSource('/events');
es.onmessage = e => {
  const d = JSON.parse(e.data);
  if (d.event === 'start') {
    addRow('start', d.file, 'wird verarbeitet…');
  } else if (d.event === 'done') {
    countDone++;
    countPhotos += d.saved.length;
    document.getElementById('count-done').textContent = countDone;
    document.getElementById('count-photos').textContent = countPhotos;
    addRow('done', d.file, '→ ' + d.saved.join(', '));
  } else if (d.event === 'error') {
    addRow('error', d.file, '✗ ' + d.msg);
  } else if (d.event === 'warning') {
    addRow('warning', d.file, '⚠ ' + d.msg);
  }
};
es.onerror = () => {
  document.getElementById('watcher-badge').textContent = 'Verbindung getrennt';
  document.getElementById('watcher-badge').className = 'badge warn';
};

function saveSettings() {
  const f = document.getElementById('settings-form');
  const data = {
    prominence: parseInt(f.prominence.value),
    padding: parseInt(f.padding.value),
    rotation: parseInt(f.rotation.value)
  };
  fetch('/settings', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(data) })
    .then(r => r.json())
    .then(d => addRow('start', 'System', d.msg));
}
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML,
        input_dir=INPUT_DIR,
        output_dir=OUTPUT_DIR,
        prominence=PROMINENCE,
        padding=PADDING,
        rotation=ROTATION
    )

@app.route("/events")
def events():
    q = queue.Queue(maxsize=50)
    with _clients_lock:
        _clients.append(q)
    # Send backlog
    def stream():
        try:
            for ev in _log[-20:]:
                yield f"data: {json.dumps(ev)}\n\n"
            while True:
                try:
                    ev = q.get(timeout=30)
                    yield f"data: {json.dumps(ev)}\n\n"
                except queue.Empty:
                    yield ": ping\n\n"
        finally:
            with _clients_lock:
                if q in _clients:
                    _clients.remove(q)
    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/settings", methods=["POST"])
def update_settings():
    global PROMINENCE, PADDING, ROTATION, observer
    data = request.get_json()
    PROMINENCE = data.get("prominence", PROMINENCE)
    PADDING = data.get("padding", PADDING)
    ROTATION = data.get("rotation", ROTATION)
    observer.stop()
    observer.join()
    observer = start_watcher(INPUT_DIR, OUTPUT_DIR, PROMINENCE, PADDING, ROTATION, broadcast)
    broadcast({"event": "start", "file": "System", "msg": "Einstellungen gespeichert, Watcher neu gestartet"})
    return jsonify({"msg": "Einstellungen gespeichert, Watcher neu gestartet"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, threaded=True)
