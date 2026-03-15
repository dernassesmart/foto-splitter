import os, json, queue, threading, base64, logging, shutil
from pathlib import Path
from io import BytesIO
from flask import Flask, Response, jsonify, request
from PIL import Image
from processor import split_image

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("foto-splitter")
app = Flask(__name__)

INPUT_DIR  = Path(os.environ.get("INPUT_DIR",  "/data/input"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/data/output"))
INPUT_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

SETTINGS = {
    "prominence": int(os.environ.get("PROMINENCE", "20")),
    "padding":    int(os.environ.get("PADDING",    "0")),
    "rotation":   int(os.environ.get("ROTATION",   "0")),
    "dryrun":     False,
}

SUPPORTED = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}

# SSE
_clients: list[queue.Queue] = []
_clients_lock = threading.Lock()
_log: list[dict] = []

def broadcast(data: dict):
    _log.append(data)
    if len(_log) > 200: _log.pop(0)
    with _clients_lock:
        for q in _clients:
            try: q.put_nowait(data)
            except queue.Full: pass

# Pending dry-run items: key -> {key, file, suffix, src_path, b64, size}
_pending: dict[str, dict] = {}
_pending_lock = threading.Lock()

def img_to_b64(pil_img, max_w=500):
    w, h = pil_img.size
    if w > max_w:
        pil_img = pil_img.resize((max_w, int(h * max_w / w)), Image.LANCZOS)
    buf = BytesIO()
    pil_img.save(buf, "JPEG", quality=82)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

def process_file_dryrun(src_path: Path, prominence, padding, rotation):
    """Process a file and store crops in _pending for preview."""
    try:
        broadcast({"event": "start", "file": src_path.name})
        photos = split_image(src_path, prominence, padding, rotation)
        if not photos:
            broadcast({"event": "warning", "file": src_path.name, "msg": "Keine Fotos erkannt"})
            return []
        keys = []
        for crop, suffix in photos:
            key = src_path.stem + suffix
            item = {
                "key": key,
                "file": src_path.stem,
                "suffix": suffix,
                "src_path": str(src_path),
                "b64": img_to_b64(crop),
                "size": list(crop.size),
                "prominence": prominence,
                "padding": padding,
                "rotation": rotation,
            }
            with _pending_lock:
                _pending[key] = item
            keys.append(key)
        broadcast({"event": "preview", "file": src_path.name, "count": len(keys), "keys": keys})
        return keys
    except Exception as e:
        logger.error(f"Dryrun error {src_path.name}: {e}", exc_info=True)
        broadcast({"event": "error", "file": src_path.name, "msg": str(e)})
        return []

def process_file_live(src_path: Path, prominence, padding, rotation):
    """Process a file and save directly to OUTPUT_DIR."""
    try:
        broadcast({"event": "start", "file": src_path.name})
        photos = split_image(src_path, prominence, padding, rotation)
        if not photos:
            broadcast({"event": "warning", "file": src_path.name, "msg": "Keine Fotos erkannt"})
            return
        saved = []
        for crop, suffix in photos:
            out_name = f"{src_path.stem}{suffix}.jpg"
            crop.save(OUTPUT_DIR / out_name, "JPEG", quality=95)
            saved.append(out_name)
        done_dir = INPUT_DIR / "done"
        done_dir.mkdir(exist_ok=True)
        shutil.move(str(src_path), done_dir / src_path.name)
        broadcast({"event": "done", "file": src_path.name, "saved": saved})
    except Exception as e:
        logger.error(f"Live error {src_path.name}: {e}", exc_info=True)
        broadcast({"event": "error", "file": src_path.name, "msg": str(e)})

# Watcher
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import time

class SmartHandler(FileSystemEventHandler):
    def __init__(self):
        self._processing = set()

    def on_created(self, event):
        if event.is_directory: return
        path = Path(event.src_path)
        if path.suffix.lower() not in SUPPORTED: return
        if path in self._processing: return
        self._processing.add(path)
        threading.Thread(target=self._handle, args=(path,), daemon=True).start()

    def _handle(self, path: Path):
        try:
            time.sleep(1.0)
            if not path.exists(): return
            if SETTINGS["dryrun"]:
                process_file_dryrun(path, SETTINGS["prominence"], SETTINGS["padding"], SETTINGS["rotation"])
            else:
                process_file_live(path, SETTINGS["prominence"], SETTINGS["padding"], SETTINGS["rotation"])
        finally:
            self._processing.discard(path)

_handler = SmartHandler()
_observer = Observer()
_observer.schedule(_handler, str(INPUT_DIR), recursive=False)
_observer.start()

# ── HTML ──────────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Foto Splitter</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f0f0f0;color:#1a1a1a}
header{background:#fff;border-bottom:1px solid #ddd;padding:.85rem 2rem;display:flex;align-items:center;gap:.75rem;position:sticky;top:0;z-index:100;box-shadow:0 1px 4px rgba(0,0,0,.06)}
header h1{font-size:1rem;font-weight:600;flex:1}
.dot{width:8px;height:8px;border-radius:50%;background:#4caf50;flex-shrink:0;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.pill{font-size:11px;padding:3px 10px;border-radius:99px;font-weight:500}
.pill.live{background:#e8f5e9;color:#2e7d32}
.pill.dry{background:#fff3e0;color:#e65100}
main{max-width:1060px;margin:1.5rem auto;padding:0 1.25rem;display:flex;flex-direction:column;gap:1.25rem}
.card{background:#fff;border-radius:12px;border:1px solid #e0e0e0;overflow:hidden}
.card-head{padding:.75rem 1.25rem;border-bottom:1px solid #eee;display:flex;align-items:center;justify-content:space-between;gap:.5rem;flex-wrap:wrap}
.card-head h2{font-size:13px;font-weight:600;color:#444}
.card-body{padding:1.25rem}

/* Slider rows */
.srow{display:flex;align-items:center;gap:.75rem;margin-bottom:.85rem}
.srow:last-child{margin-bottom:0}
.srow label{font-size:12px;color:#666;width:120px;flex-shrink:0}
.srow input[type=range]{flex:1;accent-color:#1a1a1a}
.srow input[type=number]{width:62px;padding:5px 8px;border:1px solid #ddd;border-radius:6px;font-size:13px;text-align:center}
.srow select{padding:6px 8px;border:1px solid #ddd;border-radius:6px;font-size:13px;background:#fff;flex:1;max-width:200px}

/* Toggle */
.tog{display:flex;align-items:center;gap:8px;margin-top:.25rem}
.tog span{font-size:13px;font-weight:500}
.toggle{position:relative;width:44px;height:24px;flex-shrink:0}
.toggle input{opacity:0;width:0;height:0}
.sl{position:absolute;inset:0;background:#ccc;border-radius:24px;cursor:pointer;transition:background .2s}
.sl::before{content:'';position:absolute;width:18px;height:18px;left:3px;top:3px;background:#fff;border-radius:50%;transition:transform .2s}
.toggle input:checked+.sl{background:#f57c00}
.toggle input:checked+.sl::before{transform:translateX(20px)}

.btn{padding:7px 16px;border:1px solid #ccc;border-radius:7px;background:#fff;font-size:13px;cursor:pointer;white-space:nowrap;transition:background .15s}
.btn:hover{background:#f5f5f5}
.btn.save{background:#1a1a1a;color:#fff;border-color:#1a1a1a}
.btn.save:hover{background:#333}
.btn.recut{background:#1565c0;color:#fff;border-color:#1565c0}
.btn.recut:hover{background:#1976d2}
.btn.go{background:#2e7d32;color:#fff;border-color:#2e7d32;font-weight:600}
.btn.go:hover{background:#388e3c}
.btn:disabled{opacity:.4;cursor:default}

/* Stats */
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:.75rem}
.stat{background:#fff;border-radius:10px;border:1px solid #e0e0e0;padding:.85rem 1rem}
.stat .lbl{font-size:11px;color:#999;text-transform:uppercase;letter-spacing:.04em;margin-bottom:3px}
.stat .val{font-size:1.5rem;font-weight:600}

/* Preview */
.pgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:10px}
.pc{background:#fff;border-radius:10px;border:2px solid #4caf50;overflow:hidden;transition:border-color .15s,opacity .15s}
.pc.rej{border-color:#e53935;opacity:.4}
.pc img{width:100%;display:block;max-height:190px;object-fit:contain;background:#f5f5f5;cursor:zoom-in}
.pc-foot{padding:7px 10px;display:flex;justify-content:space-between;align-items:center;gap:4px}
.pc-name{font-size:11px;color:#888;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.ico{border:1px solid #ddd;background:#fff;border-radius:5px;padding:2px 9px;font-size:12px;cursor:pointer;line-height:1.5}
.ico:hover{background:#f5f5f5}
.ico.g{border-color:#4caf50;color:#2e7d32}.ico.g:hover{background:#f1f8e9}
.ico.r{border-color:#e53935;color:#c62828}.ico.r:hover{background:#fef2f2}

/* Log */
#log{height:240px;overflow-y:auto;font-family:monospace;font-size:12px}
.lr{padding:3px 1.25rem;display:flex;gap:.75rem;border-left:3px solid transparent}
.lr.done{border-color:#4caf50}.lr.error{border-color:#f44336;background:#fef2f2}
.lr.warning{border-color:#ff9800}.lr.start{border-color:#2196f3}.lr.preview{border-color:#9c27b0}
.lt{color:#bbb;flex-shrink:0}.lf{font-weight:500}.lm{color:#666}

/* Lightbox */
#lb{display:none;position:fixed;inset:0;background:rgba(0,0,0,.88);z-index:999;align-items:center;justify-content:center;cursor:zoom-out}
#lb.on{display:flex}
#lb img{max-width:92vw;max-height:92vh;object-fit:contain;border-radius:4px}
@media(max-width:600px){.stats{grid-template-columns:1fr 1fr}}
</style>
</head>
<body>
<header>
  <span class="dot"></span>
  <h1>Foto Splitter</h1>
  <span class="pill live" id="mode-pill">Live</span>
</header>
<main>

<!-- Settings -->
<div class="card">
  <div class="card-head">
    <h2>Einstellungen</h2>
    <div style="display:flex;gap:8px;align-items:center">
      <span id="save-msg" style="font-size:12px;color:#888"></span>
      <button class="btn save" onclick="saveSettings()">Speichern</button>
    </div>
  </div>
  <div class="card-body">
    <div class="srow">
      <label>Empfindlichkeit</label>
      <input type="range" id="r-prom" min="5" max="100" value="20" oninput="sync('prom',this.value)">
      <input type="number" id="n-prom" min="5" max="100" value="20" oninput="sync('prom',this.value)">
    </div>
    <div class="srow">
      <label>Rand (px)</label>
      <input type="range" id="r-pad" min="0" max="100" value="0" oninput="sync('pad',this.value)">
      <input type="number" id="n-pad" min="0" max="100" value="0" oninput="sync('pad',this.value)">
    </div>
    <div class="srow">
      <label>Rotation</label>
      <select id="s-rot">
        <option value="0">Keine</option>
        <option value="90">90° Uhrzeigersinn</option>
        <option value="-90">90° Gegen-UZS</option>
        <option value="180">180°</option>
      </select>
    </div>
    <div class="srow">
      <label>Modus</label>
      <div class="tog">
        <label class="toggle"><input type="checkbox" id="s-dry" onchange="onDryChange(this.checked)"><span class="sl"></span></label>
        <span id="dry-lbl">Live-Modus</span>
      </div>
    </div>
  </div>
</div>

<!-- Stats -->
<div class="stats">
  <div class="stat"><div class="lbl">Eingang</div><div class="val" id="st-pending">—</div></div>
  <div class="stat"><div class="lbl">Verarbeitet</div><div class="val" id="st-done">0</div></div>
  <div class="stat"><div class="lbl">Fotos gespeichert</div><div class="val" id="st-photos">0</div></div>
  <div class="stat"><div class="lbl">Fehler</div><div class="val" id="st-err" style="color:#e53935">0</div></div>
</div>

<!-- Dry-run preview -->
<div class="card" id="preview-card" style="display:none">
  <div class="card-head">
    <h2>Vorschau</h2>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <span id="sel-count" style="font-size:12px;color:#888"></span>
      <button class="btn" onclick="selAll(true)">Alle ✓</button>
      <button class="btn" onclick="selAll(false)">Alle ✕</button>
      <button class="btn recut" onclick="recut()">↺ Neu schneiden</button>
      <button class="btn go" id="btn-go" onclick="commitSelected()">GO — Speichern</button>
    </div>
  </div>
  <div class="card-body">
    <div class="pgrid" id="pgrid"></div>
  </div>
</div>

<!-- Log -->
<div class="card">
  <div class="card-head">
    <h2>Log</h2>
    <button class="btn" onclick="document.getElementById('log').innerHTML=''" style="padding:3px 10px;font-size:11px">Leeren</button>
  </div>
  <div id="log"></div>
</div>

</main>
<div id="lb" onclick="this.classList.remove('on')"><img id="lb-img" src=""></div>

<script>
let items = {};
let cntDone=0, cntPhotos=0, cntErr=0;

function sync(id, val) {
  document.getElementById('r-'+id).value = val;
  document.getElementById('n-'+id).value = val;
}

function onDryChange(on) {
  document.getElementById('dry-lbl').textContent = on ? 'Dry-Run' : 'Live-Modus';
  document.getElementById('mode-pill').textContent = on ? 'Dry-Run' : 'Live';
  document.getElementById('mode-pill').className = 'pill '+(on?'dry':'live');
  if (!on) {
    document.getElementById('preview-card').style.display = 'none';
    items = {};
  }
}

fetch('/api/settings').then(r=>r.json()).then(d=>{
  sync('prom', d.prominence);
  sync('pad', d.padding);
  document.getElementById('s-rot').value = d.rotation;
  document.getElementById('s-dry').checked = d.dryrun;
  onDryChange(d.dryrun);
  updatePending();
});

function currentParams() {
  return {
    prominence: +document.getElementById('n-prom').value,
    padding:    +document.getElementById('n-pad').value,
    rotation:   +document.getElementById('s-rot').value,
    dryrun:      document.getElementById('s-dry').checked,
  };
}

function saveSettings() {
  fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(currentParams())})
    .then(r=>r.json()).then(()=>{
      const m=document.getElementById('save-msg');
      m.textContent='✓ Gespeichert';
      setTimeout(()=>m.textContent='',2500);
    });
}

function updatePending() {
  fetch('/api/pending').then(r=>r.json()).then(d=>{
    document.getElementById('st-pending').textContent = d.count;
  });
}
setInterval(updatePending, 4000);

// SSE
const es = new EventSource('/events');
es.onmessage = e => {
  const d = JSON.parse(e.data);
  if (d.event==='done') {
    cntDone++; cntPhotos+=d.saved.length;
    document.getElementById('st-done').textContent=cntDone;
    document.getElementById('st-photos').textContent=cntPhotos;
    log('done',d.file,'→ '+d.saved.join(', '));
  } else if (d.event==='error') {
    cntErr++;
    document.getElementById('st-err').textContent=cntErr;
    log('error',d.file,d.msg);
  } else if (d.event==='start') {
    log('start',d.file,'wird verarbeitet…');
  } else if (d.event==='warning') {
    log('warning',d.file,d.msg);
  } else if (d.event==='preview') {
    log('preview',d.file,d.count+' Fotos erkannt');
    loadPendingItems();
  }
};

function log(cls,file,msg){
  const el=document.getElementById('log');
  const t=new Date().toLocaleTimeString('de-DE');
  el.insertAdjacentHTML('beforeend',
    `<div class="lr ${cls}"><span class="lt">${t}</span><span class="lf">${file}</span><span class="lm">${msg}</span></div>`);
  el.scrollTop=el.scrollHeight;
}

function loadPendingItems() {
  fetch('/api/pending-items').then(r=>r.json()).then(data=>{
    // Merge: keep existing selection state, add new items as selected
    data.forEach(item=>{
      if (!items[item.key]) items[item.key]={...item,selected:true};
      else items[item.key].b64=item.b64; // update preview if recut
    });
    renderPreview();
    document.getElementById('preview-card').style.display='';
    document.getElementById('preview-card').scrollIntoView({behavior:'smooth',block:'start'});
  });
}

function renderPreview(){
  const grid=document.getElementById('pgrid');
  grid.innerHTML='';
  const all=Object.values(items);
  if(!all.length){ document.getElementById('preview-card').style.display='none'; return; }
  all.forEach(item=>{
    const div=document.createElement('div');
    div.className='pc'+(item.selected?'':' rej');
    div.id='pc-'+item.key;
    div.innerHTML=`
      <img src="${item.b64}" onclick="openLb('${item.key}')">
      <div class="pc-foot">
        <span class="pc-name">${item.file}${item.suffix}.jpg</span>
        <div style="display:flex;gap:4px">
          <button class="ico g" onclick="toggle('${item.key}')">${item.selected?'✓':'+'}</button>
          <button class="ico r" onclick="del('${item.key}')">✕</button>
        </div>
      </div>`;
    grid.appendChild(div);
  });
  const sel=Object.values(items).filter(i=>i.selected).length;
  document.getElementById('sel-count').textContent=sel+' von '+all.length+' ausgewählt';
}

function toggle(key){ items[key].selected=!items[key].selected; renderPreview(); }
function del(key){ delete items[key]; renderPreview(); }
function selAll(v){ Object.values(items).forEach(i=>i.selected=v); renderPreview(); }
function openLb(key){ document.getElementById('lb-img').src=items[key].b64; document.getElementById('lb').classList.add('on'); }

// Re-cut with current parameters (without re-uploading)
function recut(){
  const srcPaths=[...new Set(Object.values(items).map(i=>i.src_path))];
  if(!srcPaths.length){ log('warning','System','Keine Dateien zum Neu-Schneiden'); return; }
  const btn=document.querySelector('.btn.recut');
  btn.disabled=true; btn.textContent='Läuft…';
  const p=currentParams();
  fetch('/api/recut',{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({src_paths:srcPaths, prominence:p.prominence, padding:p.padding, rotation:p.rotation})
  }).then(r=>r.json()).then(data=>{
    btn.disabled=false; btn.textContent='↺ Neu schneiden';
    // Replace items from recut
    const oldSrcs=new Set(srcPaths);
    // Remove old items for these src_paths
    Object.keys(items).forEach(k=>{ if(oldSrcs.has(items[k].src_path)) delete items[k]; });
    // Add new ones
    data.forEach(item=>{ items[item.key]={...item,selected:true}; });
    renderPreview();
    log('preview','System',data.length+' Fotos neu geschnitten');
  }).catch(e=>{ btn.disabled=false; btn.textContent='↺ Neu schneiden'; log('error','Recut',String(e)); });
}

function commitSelected(){
  const sel=Object.values(items).filter(i=>i.selected);
  if(!sel.length){ alert('Keine Fotos ausgewählt.'); return; }
  const btn=document.getElementById('btn-go');
  btn.disabled=true; btn.textContent='Speichert…';
  fetch('/api/commit',{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(sel.map(i=>({key:i.key,file:i.file,suffix:i.suffix,src_path:i.src_path})))
  }).then(r=>r.json()).then(r=>{
    cntPhotos+=r.saved.length;
    document.getElementById('st-photos').textContent=cntPhotos;
    log('done','Dry-Run','Gespeichert: '+r.saved.join(', '));
    sel.forEach(i=>delete items[i.key]);
    renderPreview();
    btn.disabled=false; btn.textContent='GO — Speichern';
  }).catch(e=>{ btn.disabled=false; btn.textContent='GO — Speichern'; log('error','Commit',String(e)); });
}
</script>
</body>
</html>"""

# ── API ───────────────────────────────────────────────────────────────────────

@app.route("/")
def index(): return HTML

@app.route("/api/settings", methods=["GET"])
def get_settings(): return jsonify(SETTINGS)

@app.route("/api/settings", methods=["POST"])
def post_settings():
    data = request.get_json()
    SETTINGS.update({k: data[k] for k in ("prominence","padding","rotation","dryrun") if k in data})
    broadcast({"event":"start","file":"System","msg":"Einstellungen gespeichert"})
    return jsonify({"msg":"OK"})

@app.route("/api/pending")
def api_pending():
    files = [f for f in INPUT_DIR.iterdir() if f.is_file() and f.suffix.lower() in SUPPORTED]
    return jsonify({"count": len(files)})

@app.route("/api/pending-items")
def api_pending_items():
    with _pending_lock:
        return jsonify(list(_pending.values()))

@app.route("/api/recut", methods=["POST"])
def api_recut():
    """Re-process given src_paths with new parameters, return fresh preview items."""
    data = request.get_json()
    src_paths = data.get("src_paths", [])
    prominence = data.get("prominence", SETTINGS["prominence"])
    padding    = data.get("padding",    SETTINGS["padding"])
    rotation   = data.get("rotation",  SETTINGS["rotation"])

    results = []
    with _pending_lock:
        # Remove old entries for these files
        for key in list(_pending.keys()):
            if _pending[key]["src_path"] in src_paths:
                del _pending[key]

    for src_path_str in src_paths:
        src = Path(src_path_str)
        if not src.exists():
            continue
        try:
            photos = split_image(src, prominence, padding, rotation)
            for crop, suffix in photos:
                key = src.stem + suffix
                item = {
                    "key": key,
                    "file": src.stem,
                    "suffix": suffix,
                    "src_path": src_path_str,
                    "b64": img_to_b64(crop),
                    "size": list(crop.size),
                    "prominence": prominence,
                    "padding": padding,
                    "rotation": rotation,
                }
                with _pending_lock:
                    _pending[key] = item
                results.append(item)
        except Exception as e:
            logger.error(f"Recut {src_path_str}: {e}", exc_info=True)

    return jsonify(results)

@app.route("/api/commit", methods=["POST"])
def api_commit():
    data = request.get_json()  # [{key, file, suffix, src_path}]
    saved = []
    # Group by src_path
    by_src: dict[str, list] = {}
    for item in data:
        by_src.setdefault(item["src_path"], []).append(item)

    for src_path_str, items_list in by_src.items():
        src = Path(src_path_str)
        if not src.exists():
            logger.warning(f"Commit: src not found {src_path_str}")
            continue
        try:
            # Use current settings for final save (or recut params if different)
            # Use params from the last recut/dryrun for this file
            first_item = items_list[0]
            use_prom = first_item.get("prominence", SETTINGS["prominence"])
            use_pad  = first_item.get("padding",    SETTINGS["padding"])
            use_rot  = first_item.get("rotation",   SETTINGS["rotation"])
            # But we need these from _pending, not just the commit request
            with _pending_lock:
                pi = _pending.get(first_item["key"], {})
            use_prom = pi.get("prominence", SETTINGS["prominence"])
            use_pad  = pi.get("padding",    SETTINGS["padding"])
            use_rot  = pi.get("rotation",   SETTINGS["rotation"])
            photos = split_image(src, use_prom, use_pad, use_rot)
            suffix_map = {s: img for img, s in photos}
            for item in items_list:
                suffix = item["suffix"]
                if suffix in suffix_map:
                    out_name = f"{item['file']}{suffix}.jpg"
                    suffix_map[suffix].save(OUTPUT_DIR / out_name, "JPEG", quality=95)
                    saved.append(out_name)
                    with _pending_lock:
                        _pending.pop(item["key"], None)
            # Move source to done/
            done_dir = INPUT_DIR / "done"
            done_dir.mkdir(exist_ok=True)
            shutil.move(str(src), done_dir / src.name)
        except Exception as e:
            logger.error(f"Commit {src_path_str}: {e}", exc_info=True)

    broadcast({"event":"done","file":"Dry-Run","saved":saved})
    return jsonify({"saved": saved})

@app.route("/events")
def events():
    q = queue.Queue(maxsize=50)
    with _clients_lock: _clients.append(q)
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
