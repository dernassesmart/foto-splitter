import os, json, queue, threading, base64, logging, shutil
from pathlib import Path
from io import BytesIO
from flask import Flask, Response, jsonify, request, send_file
from PIL import Image
from processor import split_image
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("foto-splitter")
app = Flask(__name__)

INPUT_DIR   = Path(os.environ.get("INPUT_DIR",   "/data/input"))
OUTPUT_DIR  = Path(os.environ.get("OUTPUT_DIR",  "/data/output"))
PENDING_DIR = Path(os.environ.get("PENDING_DIR", "/data/pending"))
for d in (INPUT_DIR, OUTPUT_DIR, PENDING_DIR):
    d.mkdir(parents=True, exist_ok=True)

SUPPORTED = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}

SETTINGS = {
    "prominence": int(os.environ.get("PROMINENCE", "20")),
    "padding":    int(os.environ.get("PADDING",    "0")),
    "rotation":   int(os.environ.get("ROTATION",   "0")),
    "dryrun":     False,
}

# ── SSE ──────────────────────────────────────────────────────────────────────
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

# ── Disk-backed pending store ─────────────────────────────────────────────────
# Each pending item is a JSON file in PENDING_DIR.
# b64 is NOT stored on disk — regenerated on demand to save space.
_pending_lock = threading.Lock()

def _ppath(key: str) -> Path:
    safe = key.replace('/', '_').replace('\\', '_')
    return PENDING_DIR / f"{safe}.json"

def _pdisk_load(key: str) -> dict | None:
    p = _ppath(key)
    if not p.exists(): return None
    try: return json.loads(p.read_text())
    except: return None

def _pdisk_save(key: str, item: dict):
    data = {k: v for k, v in item.items() if k != 'b64'}
    _ppath(key).write_text(json.dumps(data))

def _pdisk_delete(key: str):
    p = _ppath(key)
    if p.exists(): p.unlink()

def _pdisk_clear_for_src(src_path: str):
    for p in list(PENDING_DIR.glob("*.json")):
        try:
            item = json.loads(p.read_text())
            if item.get("src_path") == src_path:
                p.unlink()
        except: pass

def _pdisk_list_all() -> list[dict]:
    items = []
    for p in sorted(PENDING_DIR.glob("*.json")):
        try:
            item = json.loads(p.read_text())
            if Path(item.get("src_path", "")).exists():
                items.append(item)
            else:
                p.unlink()  # stale entry
        except: pass
    return items

# ── Image helpers ─────────────────────────────────────────────────────────────
def img_to_b64(pil_img, max_w=500):
    w, h = pil_img.size
    if w > max_w:
        pil_img = pil_img.resize((max_w, int(h * max_w / w)), Image.LANCZOS)
    buf = BytesIO()
    pil_img.save(buf, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

def _get_crop_for_item(item: dict) -> Image.Image | None:
    """Run split_image and return the crop for this item's suffix."""
    src = Path(item["src_path"])
    if not src.exists(): return None
    photos = split_image(src, item["prominence"], item["padding"], item["rotation"])
    suffix_map = {s: img for img, s in photos}
    crop = suffix_map.get(item["suffix"])
    if crop is None: return None
    mc = item.get("manual_crop")
    if mc:
        iw, ih = crop.size
        cx = max(0, min(mc["x"], iw-1)); cy = max(0, min(mc["y"], ih-1))
        cw = max(1, min(mc["w"], iw-cx)); ch = max(1, min(mc["h"], ih-cy))
        crop = crop.crop((cx, cy, cx+cw, cy+ch))
    return crop

def _make_b64(item: dict) -> str:
    crop = _get_crop_for_item(item)
    return img_to_b64(crop) if crop else ""

# ── Core processing ───────────────────────────────────────────────────────────
def do_split_and_store(src: Path, prominence: int, padding: int, rotation: int) -> list[str]:
    photos = split_image(src, prominence, padding, rotation)
    with _pending_lock:
        _pdisk_clear_for_src(str(src))
    keys = []
    for crop, suffix in photos:
        key = src.stem + suffix
        item = {
            "key": key, "file": src.stem, "suffix": suffix,
            "src_path": str(src), "size": list(crop.size),
            "prominence": prominence, "padding": padding, "rotation": rotation,
        }
        with _pending_lock:
            _pdisk_save(key, item)
        keys.append(key)
    return keys

def save_directly(src: Path, prominence: int, padding: int, rotation: int):
    photos = split_image(src, prominence, padding, rotation)
    if not photos:
        broadcast({"event": "warning", "file": src.name, "msg": "Keine Fotos erkannt"})
        return
    saved = []
    for crop, suffix in photos:
        out_name = f"{src.stem}{suffix}.jpg"
        crop.save(OUTPUT_DIR / out_name, "JPEG", quality=95)
        saved.append(out_name)
    done_dir = INPUT_DIR / "done"
    done_dir.mkdir(exist_ok=True)
    shutil.move(str(src), done_dir / src.name)
    broadcast({"event": "done", "file": src.name, "saved": saved})

# ── Watcher ───────────────────────────────────────────────────────────────────
class SmartHandler(FileSystemEventHandler):
    def __init__(self): self._processing = set()

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
            broadcast({"event": "start", "file": path.name})
            if SETTINGS["dryrun"]:
                try:
                    keys = do_split_and_store(path,
                        SETTINGS["prominence"], SETTINGS["padding"], SETTINGS["rotation"])
                    if not keys:
                        broadcast({"event": "warning", "file": path.name, "msg": "Keine Fotos erkannt"})
                    else:
                        broadcast({"event": "preview", "file": path.name, "count": len(keys)})
                except Exception as e:
                    logger.error(f"Dryrun {path.name}: {e}", exc_info=True)
                    broadcast({"event": "error", "file": path.name, "msg": str(e)})
            else:
                try:
                    save_directly(path,
                        SETTINGS["prominence"], SETTINGS["padding"], SETTINGS["rotation"])
                except Exception as e:
                    logger.error(f"Live {path.name}: {e}", exc_info=True)
                    broadcast({"event": "error", "file": path.name, "msg": str(e)})
        finally:
            self._processing.discard(path)

_observer = Observer()
_observer.schedule(SmartHandler(), str(INPUT_DIR), recursive=False)
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
.card-head{padding:.85rem 1.25rem;border-bottom:1px solid #eee;display:flex;align-items:center;justify-content:space-between;gap:.5rem;flex-wrap:wrap}
.card-head h2{font-size:13px;font-weight:600;color:#444}
.card-body{padding:1.25rem}
.srow{display:flex;align-items:center;gap:.75rem;margin-bottom:.9rem}
.srow:last-child{margin-bottom:0}
.srow label{font-size:12px;color:#666;width:130px;flex-shrink:0}
.srow input[type=range]{flex:1;accent-color:#1a1a1a;cursor:pointer}
.srow input[type=number]{width:64px;padding:5px 8px;border:1px solid #ddd;border-radius:6px;font-size:13px;text-align:center}
.srow select{flex:1;max-width:220px;padding:6px 10px;border:1px solid #ddd;border-radius:6px;font-size:13px;background:#fff}
.tog{display:flex;align-items:center;gap:10px}
.toggle{position:relative;width:44px;height:24px;flex-shrink:0}
.toggle input{opacity:0;width:0;height:0}
.sl{position:absolute;inset:0;background:#ccc;border-radius:24px;cursor:pointer;transition:background .2s}
.sl::before{content:'';position:absolute;width:18px;height:18px;left:3px;top:3px;background:#fff;border-radius:50%;transition:transform .2s}
.toggle input:checked+.sl{background:#f57c00}
.toggle input:checked+.sl::before{transform:translateX(20px)}
.btn{padding:7px 16px;border:1px solid #ccc;border-radius:7px;background:#fff;font-size:13px;cursor:pointer;white-space:nowrap;transition:all .15s}
.btn:hover{background:#f5f5f5}
.btn.save{background:#1a1a1a;color:#fff;border-color:#1a1a1a}.btn.save:hover{background:#333}
.btn.recut{background:#1565c0;color:#fff;border-color:#1565c0}.btn.recut:hover{background:#1976d2}
.btn.go{background:#2e7d32;color:#fff;border-color:#2e7d32;font-weight:600}.btn.go:hover{background:#388e3c}
.btn:disabled{opacity:.4;cursor:default}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:.75rem}
.stat{background:#fff;border-radius:10px;border:1px solid #e0e0e0;padding:.85rem 1rem}
.stat .lbl{font-size:11px;color:#999;text-transform:uppercase;letter-spacing:.04em;margin-bottom:3px}
.stat .val{font-size:1.5rem;font-weight:600}
.pgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:10px}
.pc{background:#fff;border-radius:10px;border:2px solid #4caf50;overflow:hidden;transition:all .15s}
.pc.rej{border-color:#e53935;opacity:.4}
.pc img{width:100%;display:block;max-height:190px;object-fit:contain;background:#f5f5f5;cursor:zoom-in}
.pc-foot{padding:7px 10px;display:flex;justify-content:space-between;align-items:center;gap:4px}
.pc-name{font-size:11px;color:#888;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.ico{border:1px solid #ddd;background:#fff;border-radius:5px;padding:2px 9px;font-size:12px;cursor:pointer;line-height:1.5}
.ico:hover{background:#f5f5f5}
.ico.g{border-color:#4caf50;color:#2e7d32}.ico.g:hover{background:#f1f8e9}
.ico.r{border-color:#e53935;color:#c62828}.ico.r:hover{background:#fef2f2}
.ico.b{border-color:#1565c0;color:#1565c0}.ico.b:hover{background:#e3f2fd}
#log{height:240px;overflow-y:auto;font-family:monospace;font-size:12px}
.lr{padding:3px 1.25rem;display:flex;gap:.75rem;border-left:3px solid transparent}
.lr.done{border-color:#4caf50}.lr.error{border-color:#f44336;background:#fef2f2}
.lr.warning{border-color:#ff9800}.lr.start{border-color:#2196f3}.lr.preview{border-color:#9c27b0}
.lt{color:#bbb;flex-shrink:0}.lf{font-weight:500}.lm{color:#666}
/* Crop editor */
#ce{display:none;position:fixed;inset:0;background:rgba(0,0,0,.92);z-index:999;flex-direction:column;align-items:center;justify-content:center;gap:12px}
#ce.on{display:flex}
#ce-info{color:#aaa;font-size:12px}
#ce-wrap{position:relative;display:inline-block;user-select:none}
#ce-img{display:block;max-width:88vw;max-height:72vh;object-fit:contain;cursor:crosshair}
#ce-sel{position:absolute;border:2px solid #fff;background:rgba(255,255,255,.1);pointer-events:none;display:none;box-sizing:border-box}
.ce-h{position:absolute;width:12px;height:12px;background:#fff;border:2px solid #333;border-radius:2px}
.ce-h.tl{top:-6px;left:-6px;cursor:nwse-resize}.ce-h.tr{top:-6px;right:-6px;cursor:nesw-resize}
.ce-h.bl{bottom:-6px;left:-6px;cursor:nesw-resize}.ce-h.br{bottom:-6px;right:-6px;cursor:nwse-resize}
#ce-btns{display:flex;gap:8px}
#ce-btns button{padding:8px 20px;border-radius:7px;border:none;font-size:13px;cursor:pointer;font-weight:500}
#ce-apply{background:#2e7d32;color:#fff}
#ce-reset{background:#555;color:#fff}
#ce-cancel{background:#333;color:#fff}
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

<div class="card">
  <div class="card-head">
    <h2>Einstellungen</h2>
    <div style="display:flex;gap:8px;align-items:center">
      <span id="save-msg" style="font-size:12px;color:#2e7d32"></span>
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
        <label class="toggle">
          <input type="checkbox" id="s-dry" onchange="onDryChange(this.checked)">
          <span class="sl"></span>
        </label>
        <span id="dry-lbl" style="font-size:13px;font-weight:500">Live-Modus</span>
      </div>
    </div>
  </div>
</div>

<div class="stats">
  <div class="stat"><div class="lbl">Eingang</div><div class="val" id="st-pending">—</div></div>
  <div class="stat"><div class="lbl">Verarbeitet</div><div class="val" id="st-done">0</div></div>
  <div class="stat"><div class="lbl">Fotos gespeichert</div><div class="val" id="st-photos">0</div></div>
  <div class="stat"><div class="lbl">Fehler</div><div class="val" id="st-err" style="color:#e53935">0</div></div>
</div>

<div class="card" id="preview-card" style="display:none">
  <div class="card-head">
    <h2>Vorschau — Dry-Run</h2>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <span id="sel-count" style="font-size:12px;color:#888"></span>
      <button class="btn" onclick="selAll(true)">Alle ✓</button>
      <button class="btn" onclick="selAll(false)">Alle ✕</button>
      <button class="btn recut" id="btn-recut" onclick="recut()">↺ Neu schneiden</button>
      <button class="btn go" id="btn-go" onclick="commitSelected()">GO — Speichern</button>
    </div>
  </div>
  <div class="card-body">
    <div class="pgrid" id="pgrid"></div>
  </div>
</div>

<div class="card">
  <div class="card-head">
    <h2>Log</h2>
    <button class="btn" onclick="document.getElementById('log').innerHTML=''" style="padding:3px 10px;font-size:11px">Leeren</button>
  </div>
  <div id="log"></div>
</div>

</main>

<!-- Crop editor -->
<div id="ce">
  <div id="ce-info">Ziehe einen Rahmen — Ecken zum Anpassen</div>
  <div id="ce-wrap">
    <img id="ce-img" src="" draggable="false">
    <div id="ce-sel">
      <div class="ce-h tl"></div><div class="ce-h tr"></div>
      <div class="ce-h bl"></div><div class="ce-h br"></div>
    </div>
  </div>
  <div id="ce-btns">
    <button id="ce-apply" onclick="cropApply()">✓ Übernehmen</button>
    <button id="ce-reset" onclick="cropReset()">↺ Reset</button>
    <button id="ce-cancel" onclick="document.getElementById('ce').classList.remove('on')">✕ Abbrechen</button>
  </div>
</div>

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
  document.getElementById('mode-pill').className = 'pill ' + (on ? 'dry' : 'live');
}
function currentParams() {
  return {
    prominence: +document.getElementById('n-prom').value,
    padding:    +document.getElementById('n-pad').value,
    rotation:   +document.getElementById('s-rot').value,
    dryrun:      document.getElementById('s-dry').checked,
  };
}

// Init
fetch('/api/settings').then(r=>r.json()).then(d=>{
  sync('prom', d.prominence); sync('pad', d.padding);
  document.getElementById('s-rot').value = d.rotation;
  document.getElementById('s-dry').checked = d.dryrun;
  onDryChange(d.dryrun);
});

function loadPendingItems() {
  fetch('/api/pending-items').then(r=>r.json()).then(data=>{
    if (!data.length) return;
    data.forEach(item => {
      if (!items[item.key]) items[item.key] = {...item, selected: true};
      else { items[item.key].b64 = item.b64; items[item.key].size = item.size; }
    });
    renderPreview();
    document.getElementById('preview-card').style.display = '';
  });
}
loadPendingItems();

function saveSettings() {
  fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(currentParams())})
    .then(r=>r.json()).then(()=>{
      const m=document.getElementById('save-msg');
      m.textContent='✓ Gespeichert'; setTimeout(()=>m.textContent='',2500);
    });
}
function updatePending() {
  fetch('/api/pending').then(r=>r.json()).then(d=>{
    document.getElementById('st-pending').textContent=d.count;
  });
}
updatePending(); setInterval(updatePending, 4000);

const es = new EventSource('/events');
es.onmessage = e => {
  const d = JSON.parse(e.data);
  if (d.event==='preview') {
    log('preview',d.file,d.count+' Fotos erkannt');
    loadPendingItems();
  } else if (d.event==='done') {
    cntDone++; cntPhotos+=d.saved.length;
    document.getElementById('st-done').textContent=cntDone;
    document.getElementById('st-photos').textContent=cntPhotos;
    log('done',d.file,'→ '+d.saved.join(', '));
  } else if (d.event==='error') {
    cntErr++; document.getElementById('st-err').textContent=cntErr;
    log('error',d.file,d.msg);
  } else if (d.event==='start') {
    log('start',d.file,'wird verarbeitet…');
  } else if (d.event==='warning') {
    log('warning',d.file,d.msg);
  }
};

function log(cls,file,msg){
  const el=document.getElementById('log');
  const t=new Date().toLocaleTimeString('de-DE');
  el.insertAdjacentHTML('beforeend',
    `<div class="lr ${cls}"><span class="lt">${t}</span><span class="lf">${file}</span><span class="lm">${msg}</span></div>`);
  el.scrollTop=el.scrollHeight;
}

function renderPreview(){
  const grid=document.getElementById('pgrid');
  const all=Object.values(items);
  if(!all.length){document.getElementById('preview-card').style.display='none';return;}
  grid.innerHTML='';
  all.forEach(item=>{
    const div=document.createElement('div');
    div.className='pc'+(item.selected?'':' rej');
    div.innerHTML=`
      <img src="${item.b64}" onclick="openCrop('${item.key}')" title="Klicken zum Zuschneiden">
      <div class="pc-foot">
        <span class="pc-name">${item.file}${item.suffix}.jpg<br>
          <span style="color:#aaa">${item.size[0]}×${item.size[1]}px</span>
        </span>
        <div style="display:flex;gap:4px">
          <button class="ico b" onclick="openCrop('${item.key}')" title="Zuschneiden">✂</button>
          <button class="ico g" onclick="toggle('${item.key}')">${item.selected?'✓':'+'}</button>
          <button class="ico r" onclick="removeItem('${item.key}')">✕</button>
        </div>
      </div>`;
    grid.appendChild(div);
  });
  const sel=all.filter(i=>i.selected).length;
  document.getElementById('sel-count').textContent=sel+' von '+all.length+' ausgewählt';
  document.getElementById('preview-card').style.display='';
}

function toggle(key){items[key].selected=!items[key].selected;renderPreview();}
function removeItem(key){delete items[key];renderPreview();}
function selAll(v){Object.values(items).forEach(i=>i.selected=v);renderPreview();}

function recut(){
  const srcPaths=[...new Set(Object.values(items).map(i=>i.src_path))];
  if(!srcPaths.length)return;
  const btn=document.getElementById('btn-recut');
  btn.disabled=true;btn.textContent='Läuft…';
  const p=currentParams();
  fetch('/api/recut',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({src_paths:srcPaths,prominence:p.prominence,padding:p.padding,rotation:p.rotation})
  }).then(r=>r.json()).then(data=>{
    btn.disabled=false;btn.textContent='↺ Neu schneiden';
    const prev={};Object.values(items).forEach(i=>prev[i.key]=i.selected);
    Object.keys(items).forEach(k=>{if(srcPaths.includes(items[k].src_path))delete items[k];});
    data.forEach(item=>{items[item.key]={...item,selected:prev[item.key]!==false};});
    renderPreview();
    log('preview','System',data.length+' Fotos neu geschnitten');
  }).catch(e=>{btn.disabled=false;btn.textContent='↺ Neu schneiden';log('error','Recut',String(e));});
}

function commitSelected(){
  const sel=Object.values(items).filter(i=>i.selected);
  if(!sel.length){alert('Keine Fotos ausgewählt.');return;}
  const btn=document.getElementById('btn-go');
  btn.disabled=true;btn.textContent='Speichert…';
  fetch('/api/commit',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(sel.map(i=>({key:i.key,file:i.file,suffix:i.suffix,src_path:i.src_path})))
  }).then(r=>r.json()).then(r=>{
    cntPhotos+=r.saved.length;
    document.getElementById('st-photos').textContent=cntPhotos;
    log('done','Dry-Run','Gespeichert: '+r.saved.join(', '));
    sel.forEach(i=>delete items[i.key]);
    renderPreview();
    btn.disabled=false;btn.textContent='GO — Speichern';
  }).catch(e=>{btn.disabled=false;btn.textContent='GO — Speichern';log('error','Commit',String(e));});
}

// ── Crop editor ───────────────────────────────────────────────────────────────
let _ceKey=null, _ceSel={x:0,y:0,w:0,h:0}, _ceDrag=null;

function openCrop(key){
  _ceKey=key;
  const img=document.getElementById('ce-img');
  document.getElementById('ce-info').textContent='Originalbild wird geladen…';
  img.onload=()=>{
    const iw=img.naturalWidth,ih=img.naturalHeight;
    _ceSel={x:0,y:0,w:iw,h:ih};
    updateSel();
    document.getElementById('ce').classList.add('on');
    document.getElementById('ce-info').textContent=
      `${iw}×${ih}px — Ziehen zum Zuschneiden, Ecken zum Anpassen`;
  };
  img.onerror=()=>{img.src=items[key].b64;};
  img.src='/api/original/'+encodeURIComponent(key);
}

function cropReset(){
  const img=document.getElementById('ce-img');
  _ceSel={x:0,y:0,w:img.naturalWidth,h:img.naturalHeight};
  updateSel();
}

function updateSel(){
  const img=document.getElementById('ce-img');
  const ir=img.getBoundingClientRect();
  const wr=document.getElementById('ce-wrap').getBoundingClientRect();
  const sx=ir.width/img.naturalWidth, sy=ir.height/img.naturalHeight;
  const sel=document.getElementById('ce-sel');
  sel.style.display='block';
  sel.style.left=(_ceSel.x*sx+(ir.left-wr.left))+'px';
  sel.style.top=(_ceSel.y*sy+(ir.top-wr.top))+'px';
  sel.style.width=(_ceSel.w*sx)+'px';
  sel.style.height=(_ceSel.h*sy)+'px';
  document.getElementById('ce-info').textContent=
    `Auswahl: ${Math.round(_ceSel.w)}×${Math.round(_ceSel.h)}px`;
}

function clamp(v,lo,hi){return Math.max(lo,Math.min(hi,v));}

function getScales(){
  const img=document.getElementById('ce-img');
  const ir=img.getBoundingClientRect();
  return {sx:img.naturalWidth/ir.width, sy:img.naturalHeight/ir.height, ir};
}

const ceWrap=document.getElementById('ce-wrap');
ceWrap.addEventListener('mousedown',e=>{
  e.preventDefault();
  const img=document.getElementById('ce-img');
  const {sx,sy,ir}=getScales();
  const wr=ceWrap.getBoundingClientRect();
  // Check handles first
  const handles=document.querySelectorAll('.ce-h');
  for(const h of handles){
    const hr=h.getBoundingClientRect();
    if(Math.abs(e.clientX-(hr.left+hr.width/2))<10 && Math.abs(e.clientY-(hr.top+hr.height/2))<10){
      const type=[...h.classList].find(c=>['tl','tr','bl','br'].includes(c));
      _ceDrag={type,sx,sy,origSel:{..._ceSel},sx0:e.clientX,sy0:e.clientY};
      return;
    }
  }
  // New selection
  const ix=(e.clientX-ir.left)*sx, iy=(e.clientY-ir.top)*sy;
  if(ix<0||iy<0||ix>img.naturalWidth||iy>img.naturalHeight)return;
  _ceSel={x:ix,y:iy,w:0,h:0};
  _ceDrag={type:'new',sx,sy,origSel:{x:ix,y:iy,w:0,h:0},sx0:e.clientX,sy0:e.clientY};
  updateSel();
});

document.addEventListener('mousemove',e=>{
  if(!_ceDrag)return;
  const {type,sx,sy,origSel,sx0,sy0}=_ceDrag;
  const dx=(e.clientX-sx0)*sx, dy=(e.clientY-sy0)*sy;
  const img=document.getElementById('ce-img');
  const NW=img.naturalWidth,NH=img.naturalHeight;
  const o=origSel;
  if(type==='new'){
    const x1=clamp(o.x+dx,0,NW),y1=clamp(o.y+dy,0,NH);
    _ceSel={x:Math.min(o.x,x1),y:Math.min(o.y,y1),w:Math.abs(x1-o.x),h:Math.abs(y1-o.y)};
  }else if(type==='br'){_ceSel={x:o.x,y:o.y,w:clamp(o.w+dx,10,NW-o.x),h:clamp(o.h+dy,10,NH-o.y)};}
  else if(type==='tl'){const nx=clamp(o.x+dx,0,o.x+o.w-10),ny=clamp(o.y+dy,0,o.y+o.h-10);_ceSel={x:nx,y:ny,w:o.w-(nx-o.x),h:o.h-(ny-o.y)};}
  else if(type==='tr'){const ny=clamp(o.y+dy,0,o.y+o.h-10);_ceSel={x:o.x,y:ny,w:clamp(o.w+dx,10,NW-o.x),h:o.h-(ny-o.y)};}
  else if(type==='bl'){const nx=clamp(o.x+dx,0,o.x+o.w-10);_ceSel={x:nx,y:o.y,w:o.w-(nx-o.x),h:clamp(o.h+dy,10,NH-o.y)};}
  updateSel();
});
document.addEventListener('mouseup',()=>_ceDrag=null);

// Touch
ceWrap.addEventListener('touchstart',e=>{const t=e.touches[0];ceWrap.dispatchEvent(new MouseEvent('mousedown',{clientX:t.clientX,clientY:t.clientY,bubbles:true}));},{passive:false});
document.addEventListener('touchmove',e=>{const t=e.touches[0];document.dispatchEvent(new MouseEvent('mousemove',{clientX:t.clientX,clientY:t.clientY}));e.preventDefault();},{passive:false});
document.addEventListener('touchend',()=>document.dispatchEvent(new MouseEvent('mouseup')));

function cropApply(){
  if(!_ceKey||_ceSel.w<5||_ceSel.h<5){document.getElementById('ce').classList.remove('on');return;}
  document.getElementById('ce-apply').disabled=true;
  document.getElementById('ce-apply').textContent='…';
  fetch('/api/crop',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({key:_ceKey,x:Math.round(_ceSel.x),y:Math.round(_ceSel.y),
                         w:Math.round(_ceSel.w),h:Math.round(_ceSel.h)})
  }).then(r=>r.json()).then(d=>{
    items[_ceKey].b64=d.b64; items[_ceKey].size=d.size;
    renderPreview();
    document.getElementById('ce').classList.remove('on');
    document.getElementById('ce-apply').disabled=false;
    document.getElementById('ce-apply').textContent='✓ Übernehmen';
    log('preview',items[_ceKey].file+items[_ceKey].suffix,'Manuell zugeschnitten: '+d.size[0]+'×'+d.size[1]+'px');
  });
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
    for k in ("prominence","padding","rotation","dryrun"):
        if k in data: SETTINGS[k] = data[k]
    return jsonify({"msg":"OK"})

@app.route("/api/pending")
def api_pending():
    files = [f for f in INPUT_DIR.iterdir()
             if f.is_file() and f.suffix.lower() in SUPPORTED]
    return jsonify({"count": len(files)})

@app.route("/api/pending-items")
def api_pending_items():
    with _pending_lock:
        items = _pdisk_list_all()
    result = []
    for item in items:
        try:
            b64 = _make_b64(item)
            if b64: result.append({**item, "b64": b64})
        except Exception as e:
            logger.error(f"b64 {item.get('key')}: {e}")
    return jsonify(result)

@app.route("/api/recut", methods=["POST"])
def api_recut():
    data = request.get_json()
    src_paths  = data.get("src_paths", [])
    prominence = int(data.get("prominence", SETTINGS["prominence"]))
    padding    = int(data.get("padding",    SETTINGS["padding"]))
    rotation   = int(data.get("rotation",  SETTINGS["rotation"]))
    results = []
    for sp in src_paths:
        src = Path(sp)
        if not src.exists(): continue
        try:
            keys = do_split_and_store(src, prominence, padding, rotation)
            for key in keys:
                with _pending_lock: item = _pdisk_load(key)
                if item:
                    b64 = _make_b64(item)
                    results.append({**item, "b64": b64})
        except Exception as e:
            logger.error(f"Recut {sp}: {e}", exc_info=True)
    return jsonify(results)

@app.route("/api/original/<key>")
def api_original(key):
    with _pending_lock: item = _pdisk_load(key)
    if not item: return jsonify({"error":"not found"}), 404
    src = Path(item["src_path"])
    if not src.exists(): return jsonify({"error":"source not found"}), 404
    try:
        photos = split_image(src, item["prominence"], item["padding"], item["rotation"])
        suffix_map = {s: img for img, s in photos}
        img = suffix_map.get(item["suffix"])
        if not img: return jsonify({"error":"suffix not found"}), 404
        buf = BytesIO()
        img.save(buf, "JPEG", quality=97)
        buf.seek(0)
        return send_file(buf, mimetype="image/jpeg",
                         download_name=f"{key}.jpg")
    except Exception as e:
        logger.error(f"Original {key}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route("/api/crop", methods=["POST"])
def api_crop():
    data = request.get_json()
    key = data["key"]
    x,y,w,h = int(data["x"]),int(data["y"]),int(data["w"]),int(data["h"])
    with _pending_lock: item = _pdisk_load(key)
    if not item: return jsonify({"error":"not found"}), 404
    src = Path(item["src_path"])
    if not src.exists(): return jsonify({"error":"source not found"}), 404
    try:
        photos = split_image(src, item["prominence"], item["padding"], item["rotation"])
        suffix_map = {s: img for img, s in photos}
        base = suffix_map.get(item["suffix"])
        if not base: return jsonify({"error":"suffix not found"}), 404
        iw,ih = base.size
        x=max(0,min(x,iw-1)); y=max(0,min(y,ih-1))
        w=max(1,min(w,iw-x)); h=max(1,min(h,ih-y))
        cropped = base.crop((x,y,x+w,y+h))
        b64 = img_to_b64(cropped, max_w=800)
        item["size"] = list(cropped.size)
        item["manual_crop"] = {"x":x,"y":y,"w":w,"h":h}
        with _pending_lock: _pdisk_save(key, item)
        return jsonify({"b64":b64,"size":list(cropped.size)})
    except Exception as e:
        logger.error(f"Crop {key}: {e}", exc_info=True)
        return jsonify({"error":str(e)}), 500

@app.route("/api/commit", methods=["POST"])
def api_commit():
    data = request.get_json()
    by_src: dict[str,list] = {}
    for item in data: by_src.setdefault(item["src_path"],[]).append(item["key"])
    saved = []
    for sp, keys in by_src.items():
        src = Path(sp)
        if not src.exists():
            logger.warning(f"Commit: src not found {sp}"); continue
        with _pending_lock:
            pending_items = [_pdisk_load(k) for k in keys]
        pending_items = [p for p in pending_items if p]
        if not pending_items: continue
        p = pending_items[0]
        try:
            photos = split_image(src, p["prominence"], p["padding"], p["rotation"])
            suffix_map = {s: img for img, s in photos}
            for pi in pending_items:
                img = suffix_map.get(pi["suffix"])
                if img is None: continue
                mc = pi.get("manual_crop")
                if mc:
                    iw,ih = img.size
                    cx=max(0,min(mc["x"],iw-1)); cy=max(0,min(mc["y"],ih-1))
                    cw=max(1,min(mc["w"],iw-cx)); ch=max(1,min(mc["h"],ih-cy))
                    img = img.crop((cx,cy,cx+cw,cy+ch))
                out_name = f"{pi['file']}{pi['suffix']}.jpg"
                img.save(OUTPUT_DIR / out_name, "JPEG", quality=95)
                saved.append(out_name)
            with _pending_lock:
                for k in keys: _pdisk_delete(k)
            done_dir = INPUT_DIR / "done"
            done_dir.mkdir(exist_ok=True)
            shutil.move(str(src), done_dir / src.name)
        except Exception as e:
            logger.error(f"Commit {sp}: {e}", exc_info=True)
    broadcast({"event":"done","file":"Dry-Run","saved":saved})
    return jsonify({"saved":saved})

@app.route("/events")
def events():
    q: queue.Queue = queue.Queue(maxsize=50)
    with _clients_lock: _clients.append(q)
    def stream():
        try:
            for ev in _log[-20:]: yield f"data: {json.dumps(ev)}\n\n"
            while True:
                try: ev=q.get(timeout=30); yield f"data: {json.dumps(ev)}\n\n"
                except queue.Empty: yield ": ping\n\n"
        finally:
            with _clients_lock:
                if q in _clients: _clients.remove(q)
    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, threaded=True)
