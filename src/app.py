import os
import json
import queue
import threading
import base64
import logging
from pathlib import Path
from io import BytesIO
from flask import Flask, render_template_string, Response, jsonify, request
from PIL import Image
from processor import start_watcher, split_image

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("foto-splitter")

app = Flask(__name__)

INPUT_DIR  = Path(os.environ.get("INPUT_DIR",  "/data/input"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/data/output"))

SETTINGS = {
    "prominence": int(os.environ.get("PROMINENCE", "20")),
    "padding":    int(os.environ.get("PADDING",    "0")),
    "rotation":   int(os.environ.get("ROTATION",   "0")),
    "dryrun":     False,
}

INPUT_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

_clients: list[queue.Queue] = []
_clients_lock = threading.Lock()
_log: list[dict] = []

def broadcast(data: dict):
    _log.append(data)
    if len(_log) > 200:
        _log.pop(0)
    with _clients_lock:
        for q in _clients:
            try: q.put_nowait(data)
            except queue.Full: pass

observer = start_watcher(INPUT_DIR, OUTPUT_DIR,
    SETTINGS["prominence"], SETTINGS["padding"], SETTINGS["rotation"],
    broadcast)

def img_to_b64(pil_img, max_w=400):
    w, h = pil_img.size
    if w > max_w:
        pil_img = pil_img.resize((max_w, int(h * max_w / w)), Image.LANCZOS)
    buf = BytesIO()
    pil_img.save(buf, "JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Foto Splitter</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f0f0f0;color:#1a1a1a;min-height:100vh}
header{background:#fff;border-bottom:1px solid #ddd;padding:.85rem 2rem;display:flex;align-items:center;gap:1rem;position:sticky;top:0;z-index:100}
header h1{font-size:1rem;font-weight:600;flex:1}
.pill{font-size:11px;padding:3px 10px;border-radius:99px;font-weight:500;background:#e8f5e9;color:#2e7d32}
.pill.dry{background:#fff3e0;color:#e65100}
.pill.off{background:#eee;color:#999}
main{max-width:1000px;margin:1.5rem auto;padding:0 1.25rem;display:flex;flex-direction:column;gap:1.25rem}

/* Settings card */
.card{background:#fff;border-radius:12px;border:1px solid #e0e0e0;overflow:hidden}
.card-head{padding:.75rem 1.25rem;border-bottom:1px solid #eee;display:flex;align-items:center;justify-content:space-between}
.card-head h2{font-size:13px;font-weight:600;color:#444}
.card-body{padding:1.25rem}
.settings-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:1rem}
.field label{display:block;font-size:11px;color:#888;margin-bottom:4px;text-transform:uppercase;letter-spacing:.04em}
.field input[type=number],.field select{width:100%;padding:7px 10px;border:1px solid #ddd;border-radius:7px;font-size:14px;background:#fff}
.field input[type=number]:focus,.field select:focus{outline:none;border-color:#1a1a1a}

/* Dry-run toggle */
.toggle-wrap{display:flex;align-items:center;gap:10px;padding:.5rem 0}
.toggle-wrap label{font-size:14px;font-weight:500;cursor:pointer}
.toggle{position:relative;width:44px;height:24px;flex-shrink:0}
.toggle input{opacity:0;width:0;height:0}
.slider{position:absolute;inset:0;background:#ccc;border-radius:24px;cursor:pointer;transition:background .2s}
.slider::before{content:'';position:absolute;width:18px;height:18px;left:3px;top:3px;background:#fff;border-radius:50%;transition:transform .2s}
.toggle input:checked+.slider{background:#f57c00}
.toggle input:checked+.slider::before{transform:translateX(20px)}

.btn{padding:8px 20px;border:1px solid #ccc;border-radius:7px;background:#fff;font-size:13px;cursor:pointer;transition:background .15s}
.btn:hover{background:#f5f5f5}
.btn.primary{background:#1a1a1a;color:#fff;border-color:#1a1a1a}
.btn.primary:hover{background:#333}
.btn.go{background:#2e7d32;color:#fff;border-color:#2e7d32}
.btn.go:hover{background:#388e3c}
.btn:disabled{opacity:.4;cursor:default}
.actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:1rem}

/* Stats row */
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:.75rem}
.stat{background:#fff;border-radius:10px;border:1px solid #e0e0e0;padding:.85rem 1rem}
.stat .label{font-size:11px;color:#999;text-transform:uppercase;letter-spacing:.04em;margin-bottom:4px}
.stat .val{font-size:1.6rem;font-weight:600}

/* Preview grid */
.preview-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px}
.photo-card{background:#fff;border-radius:10px;border:2px solid #e0e0e0;overflow:hidden;transition:border-color .15s}
.photo-card.selected{border-color:#1a1a1a}
.photo-card.rejected{border-color:#f44336;opacity:.5}
.photo-thumb{width:100%;display:block;max-height:200px;object-fit:contain;background:#f5f5f5;cursor:pointer}
.photo-foot{padding:8px 10px;display:flex;justify-content:space-between;align-items:center;gap:6px}
.photo-name{font-size:11px;color:#888;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.photo-actions{display:flex;gap:4px}
.ico-btn{border:1px solid #ddd;background:#fff;border-radius:5px;padding:3px 8px;font-size:12px;cursor:pointer;line-height:1.4}
.ico-btn:hover{background:#f5f5f5}
.ico-btn.danger{border-color:#f44336;color:#f44336}
.ico-btn.danger:hover{background:#fef2f2}
.ico-btn.ok{border-color:#4caf50;color:#2e7d32}
.ico-btn.ok:hover{background:#f1f8e9}

/* Preview section header */
.preview-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:.75rem;flex-wrap:wrap;gap:.5rem}
.preview-head h3{font-size:14px;font-weight:600}
.preview-meta{font-size:12px;color:#888}

/* Log */
#log{height:280px;overflow-y:auto;font-family:monospace;font-size:12px}
.log-row{padding:3px 1.25rem;display:flex;gap:.75rem;align-items:baseline;border-left:3px solid transparent}
.log-row.done{border-color:#4caf50}
.log-row.error{border-color:#f44336;background:#fef2f2}
.log-row.warning{border-color:#ff9800}
.log-row.start{border-color:#2196f3}
.log-time{color:#bbb;flex-shrink:0}
.log-file{font-weight:500}
.log-msg{color:#666}

/* Lightbox */
#lightbox{display:none;position:fixed;inset:0;background:rgba(0,0,0,.85);z-index:999;align-items:center;justify-content:center;cursor:zoom-out}
#lightbox.open{display:flex}
#lightbox img{max-width:90vw;max-height:90vh;object-fit:contain;border-radius:4px}

@media(max-width:600px){.stats{grid-template-columns:1fr 1fr}.settings-grid{grid-template-columns:1fr}}
</style>
</head>
<body>

<header>
  <span id="dot" style="width:8px;height:8px;border-radius:50%;background:#4caf50;flex-shrink:0"></span>
  <h1>Foto Splitter</h1>
  <span id="mode-pill" class="pill off">Watcher aktiv</span>
</header>

<main>

  <!-- Settings -->
  <div class="card">
    <div class="card-head"><h2>Einstellungen</h2></div>
    <div class="card-body">
      <div class="settings-grid">
        <div class="field">
          <label>Empfindlichkeit</label>
          <input type="number" id="prominence" value="20" min="5" max="100">
        </div>
        <div class="field">
          <label>Rand (px)</label>
          <input type="number" id="padding" value="0" min="0" max="100">
        </div>
        <div class="field">
          <label>Rotation</label>
          <select id="rotation">
            <option value="0">Keine</option>
            <option value="90">90° Uhrzeigersinn</option>
            <option value="-90">90° Gegenuhrzeigersinn</option>
            <option value="180">180°</option>
          </select>
        </div>
        <div class="field">
          <label>Modus</label>
          <div class="toggle-wrap">
            <label class="toggle">
              <input type="checkbox" id="dryrun-toggle">
              <span class="slider"></span>
            </label>
            <label for="dryrun-toggle" id="dryrun-label">Dry-Run aus</label>
          </div>
        </div>
      </div>

      <div class="actions">
        <button class="btn primary" onclick="saveSettings()">Speichern</button>
        <button class="btn" id="btn-dryrun" onclick="runDryRun()" style="display:none">▶ Dry-Run starten</button>
        <button class="btn go" id="btn-go" onclick="commitAll()" style="display:none">✓ Alle speichern</button>
        <span id="settings-msg" style="font-size:12px;color:#888"></span>
      </div>
    </div>
  </div>

  <!-- Stats -->
  <div class="stats">
    <div class="stat"><div class="label">Eingang</div><div class="val" id="stat-pending">—</div></div>
    <div class="stat"><div class="label">Verarbeitet</div><div class="val" id="stat-done">0</div></div>
    <div class="stat"><div class="label">Fotos heute</div><div class="val" id="stat-photos">0</div></div>
    <div class="stat"><div class="label">Fehler</div><div class="val" id="stat-errors" style="color:#f44336">0</div></div>
  </div>

  <!-- Dry-Run Preview -->
  <div class="card" id="preview-card" style="display:none">
    <div class="card-head">
      <h2>Dry-Run Vorschau</h2>
      <span id="preview-stats" class="preview-meta"></span>
    </div>
    <div class="card-body">
      <div class="preview-head">
        <div style="display:flex;gap:6px;">
          <button class="btn" onclick="selectAll(true)">Alle auswählen</button>
          <button class="btn" onclick="selectAll(false)">Alle abwählen</button>
        </div>
        <div style="display:flex;gap:6px;">
          <button class="btn go" id="btn-go2" onclick="commitAll()">✓ Ausgewählte speichern</button>
        </div>
      </div>
      <div class="preview-grid" id="preview-grid"></div>
    </div>
  </div>

  <!-- Log -->
  <div class="card">
    <div class="card-head">
      <h2>Log</h2>
      <button class="btn" onclick="document.getElementById('log').innerHTML=''" style="padding:3px 10px;font-size:11px;">Leeren</button>
    </div>
    <div id="log"></div>
  </div>

</main>

<!-- Lightbox -->
<div id="lightbox" onclick="this.classList.remove('open')">
  <img id="lightbox-img" src="">
</div>

<script>
let dryRunData = []; // [{file, suffix, b64, selected}]
let cntDone=0, cntPhotos=0, cntErrors=0;

// Init from server
fetch('/api/settings').then(r=>r.json()).then(d=>{
  document.getElementById('prominence').value = d.prominence;
  document.getElementById('padding').value = d.padding;
  document.getElementById('rotation').value = d.rotation;
  setDryRun(d.dryrun);
  updatePending();
});

function setDryRun(on) {
  document.getElementById('dryrun-toggle').checked = on;
  document.getElementById('dryrun-label').textContent = on ? 'Dry-Run ein' : 'Dry-Run aus';
  document.getElementById('btn-dryrun').style.display = on ? '' : 'none';
  document.getElementById('btn-go').style.display = 'none';
  document.getElementById('mode-pill').textContent = on ? 'Dry-Run Modus' : 'Watcher aktiv';
  document.getElementById('mode-pill').className = 'pill ' + (on ? 'dry' : 'off');
  if (!on) {
    document.getElementById('preview-card').style.display = 'none';
    dryRunData = [];
  }
}

document.getElementById('dryrun-toggle').addEventListener('change', e => {
  setDryRun(e.target.checked);
});

function saveSettings() {
  const d = {
    prominence: parseInt(document.getElementById('prominence').value),
    padding: parseInt(document.getElementById('padding').value),
    rotation: parseInt(document.getElementById('rotation').value),
    dryrun: document.getElementById('dryrun-toggle').checked,
  };
  fetch('/api/settings', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)})
    .then(r=>r.json()).then(r=>{
      document.getElementById('settings-msg').textContent = r.msg;
      setTimeout(()=>document.getElementById('settings-msg').textContent='', 3000);
    });
}

function updatePending() {
  fetch('/api/pending').then(r=>r.json()).then(d=>{
    document.getElementById('stat-pending').textContent = d.count;
  });
}
setInterval(updatePending, 5000);

// Dry-Run
function runDryRun() {
  const btn = document.getElementById('btn-dryrun');
  btn.disabled = true; btn.textContent = 'Läuft…';
  const params = {
    prominence: parseInt(document.getElementById('prominence').value),
    padding: parseInt(document.getElementById('padding').value),
    rotation: parseInt(document.getElementById('rotation').value),
  };
  fetch('/api/dryrun', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(params)})
    .then(r=>r.json())
    .then(data => {
      btn.disabled=false; btn.textContent='▶ Dry-Run starten';
      showPreview(data);
    })
    .catch(e => {
      btn.disabled=false; btn.textContent='▶ Dry-Run starten';
      addLog('error','System','Dry-Run Fehler: '+e);
    });
}

function showPreview(data) {
  dryRunData = data.map(item => ({...item, selected: true}));
  renderPreview();
  document.getElementById('preview-card').style.display = '';
  document.getElementById('btn-go').style.display = '';
  document.getElementById('preview-stats').textContent =
    dryRunData.length + ' Fotos aus ' + new Set(dryRunData.map(d=>d.file)).size + ' Scans';
}

function renderPreview() {
  const grid = document.getElementById('preview-grid');
  grid.innerHTML = '';
  dryRunData.forEach((item, i) => {
    const card = document.createElement('div');
    card.className = 'photo-card' + (item.selected ? ' selected' : ' rejected');
    card.innerHTML = `
      <img class="photo-thumb" src="${item.b64}" onclick="openLightbox('${item.b64}')">
      <div class="photo-foot">
        <span class="photo-name">${item.file}${item.suffix}</span>
        <div class="photo-actions">
          <button class="ico-btn ok" onclick="toggleSelect(${i})" title="Auswählen/Abwählen">${item.selected ? '✓' : '+'}</button>
          <button class="ico-btn danger" onclick="rejectOne(${i})" title="Ablehnen">✕</button>
        </div>
      </div>`;
    grid.appendChild(card);
  });
  const sel = dryRunData.filter(d=>d.selected).length;
  document.getElementById('preview-stats').textContent =
    sel + ' von ' + dryRunData.length + ' ausgewählt';
}

function toggleSelect(i) {
  dryRunData[i].selected = !dryRunData[i].selected;
  renderPreview();
}
function rejectOne(i) {
  dryRunData[i].selected = false;
  renderPreview();
}
function selectAll(val) {
  dryRunData.forEach(d => d.selected = val);
  renderPreview();
}

function openLightbox(src) {
  document.getElementById('lightbox-img').src = src;
  document.getElementById('lightbox').classList.add('open');
}

function commitAll() {
  const toSave = dryRunData.filter(d => d.selected);
  if (!toSave.length) { alert('Keine Fotos ausgewählt.'); return; }
  const btn = document.getElementById('btn-go');
  const btn2 = document.getElementById('btn-go2');
  btn.disabled=true; btn2.disabled=true;
  btn.textContent='Speichert…'; btn2.textContent='Speichert…';
  fetch('/api/commit', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify(toSave.map(d=>({file:d.file, suffix:d.suffix})))
  }).then(r=>r.json()).then(r=>{
    addLog('done','Dry-Run','Gespeichert: '+r.saved.join(', '));
    cntPhotos += r.saved.length;
    document.getElementById('stat-photos').textContent = cntPhotos;
    document.getElementById('preview-card').style.display='none';
    dryRunData=[];
    btn.disabled=false; btn2.disabled=false;
    btn.textContent='✓ Alle speichern'; btn2.textContent='✓ Ausgewählte speichern';
    btn.style.display='none';
  }).catch(e=>{
    btn.disabled=false; btn2.disabled=false;
    addLog('error','System','Commit Fehler: '+e);
  });
}

// SSE live log
const es = new EventSource('/events');
es.onmessage = e => {
  const d = JSON.parse(e.data);
  if (d.event==='done') {
    cntDone++; cntPhotos+=d.saved.length;
    document.getElementById('stat-done').textContent=cntDone;
    document.getElementById('stat-photos').textContent=cntPhotos;
    addLog('done', d.file, '→ '+d.saved.join(', '));
  } else if (d.event==='error') {
    cntErrors++;
    document.getElementById('stat-errors').textContent=cntErrors;
    addLog('error', d.file, '✗ '+d.msg);
  } else if (d.event==='start') {
    addLog('start', d.file, 'wird verarbeitet…');
  } else if (d.event==='warning') {
    addLog('warning', d.file, '⚠ '+d.msg);
  }
};

function addLog(cls, file, msg) {
  const log = document.getElementById('log');
  const t = new Date().toLocaleTimeString('de-DE');
  const row = document.createElement('div');
  row.className = 'log-row '+cls;
  row.innerHTML = `<span class="log-time">${t}</span><span class="log-file">${file}</span><span class="log-msg">${msg}</span>`;
  log.appendChild(row);
  log.scrollTop = log.scrollHeight;
}
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return HTML

@app.route("/api/settings", methods=["GET"])
def get_settings():
    return jsonify(SETTINGS)

@app.route("/api/settings", methods=["POST"])
def post_settings():
    global observer
    data = request.get_json()
    SETTINGS.update({
        "prominence": data.get("prominence", SETTINGS["prominence"]),
        "padding":    data.get("padding",    SETTINGS["padding"]),
        "rotation":   data.get("rotation",   SETTINGS["rotation"]),
        "dryrun":     data.get("dryrun",     SETTINGS["dryrun"]),
    })
    # Restart watcher (only active in non-dryrun mode)
    if not SETTINGS["dryrun"]:
        observer.stop(); observer.join()
        observer = start_watcher(INPUT_DIR, OUTPUT_DIR,
            SETTINGS["prominence"], SETTINGS["padding"], SETTINGS["rotation"], broadcast)
    broadcast({"event": "start", "file": "System", "msg": "Einstellungen gespeichert"})
    return jsonify({"msg": "Gespeichert"})

@app.route("/api/pending")
def pending():
    files = [f for f in INPUT_DIR.iterdir()
             if f.is_file() and f.suffix.lower() in {".jpg",".jpeg",".png",".tif",".tiff",".bmp"}]
    return jsonify({"count": len(files)})

@app.route("/api/dryrun", methods=["POST"])
def dryrun():
    params = request.get_json()
    prominence = params.get("prominence", SETTINGS["prominence"])
    padding    = params.get("padding",    SETTINGS["padding"])
    rotation   = params.get("rotation",  SETTINGS["rotation"])

    files = sorted([f for f in INPUT_DIR.iterdir()
                    if f.is_file() and f.suffix.lower() in {".jpg",".jpeg",".png",".tif",".tiff",".bmp"}])

    results = []
    for f in files:
        try:
            photos = split_image(f, prominence, padding, rotation)
            for img, suffix in photos:
                results.append({
                    "file": f.stem,
                    "suffix": suffix,
                    "b64": img_to_b64(img),
                    "src_path": str(f),
                })
        except Exception as e:
            logger.error(f"Dryrun error {f.name}: {e}")

    return jsonify(results)

# In-memory store for dryrun results to commit later
_dryrun_cache: dict = {}  # key: file_stem+suffix → (src_path, PIL crop)

@app.route("/api/dryrun", methods=["POST"])
def dryrun_post():
    pass  # handled above

@app.route("/api/commit", methods=["POST"])
def commit():
    params = request.get_json()  # [{file, suffix}]
    prominence = SETTINGS["prominence"]
    padding    = SETTINGS["padding"]
    rotation   = SETTINGS["rotation"]

    # Re-process to get crops (stateless — re-run split_image)
    # Group by file
    by_file: dict = {}
    for item in params:
        by_file.setdefault(item["file"], []).append(item["suffix"])

    saved = []
    for stem, suffixes in by_file.items():
        # Find source file
        candidates = list(INPUT_DIR.glob(f"{stem}.*"))
        if not candidates:
            continue
        src = candidates[0]
        try:
            photos = split_image(src, prominence, padding, rotation)
            suffix_map = {s: img for img, s in photos}
            for suffix in suffixes:
                if suffix in suffix_map:
                    out_name = f"{stem}{suffix}.jpg"
                    suffix_map[suffix].save(OUTPUT_DIR / out_name, "JPEG", quality=95)
                    saved.append(out_name)
            # Move to done
            done_dir = INPUT_DIR / "done"
            done_dir.mkdir(exist_ok=True)
            import shutil
            shutil.move(str(src), done_dir / src.name)
        except Exception as e:
            logger.error(f"Commit error {stem}: {e}")

    broadcast({"event": "done", "file": "Dry-Run", "saved": saved})
    return jsonify({"saved": saved})

@app.route("/events")
def events():
    q = queue.Queue(maxsize=50)
    with _clients_lock:
        _clients.append(q)
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
                if q in _clients: _clients.remove(q)
    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, threaded=True)
