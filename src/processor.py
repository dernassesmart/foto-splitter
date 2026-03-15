import os
import time
import logging
import shutil
import numpy as np
from pathlib import Path
from PIL import Image
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

logger = logging.getLogger("foto-splitter")

SUPPORTED = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}


def compute_profiles(arr):
    gray = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    return gray.mean(axis=1), gray.mean(axis=0)


def trim_white_border(row_b, col_b, white_thresh=242):
    """Trim rows/cols that are pure-white scanner border."""
    cr = np.where(row_b < white_thresh)[0]
    cc = np.where(col_b < white_thresh)[0]
    if len(cr) == 0 or len(cc) == 0:
        return 0, len(row_b), 0, len(col_b)
    return int(cr[0]), int(cr[-1]), int(cc[0]), int(cc[-1])


def _raw_peaks(profile, prominence, merge_px=40):
    """Find local maxima with given prominence, merge nearby ones."""
    n = len(profile)
    window = max(10, int(n * 0.05))
    peaks = []
    for i in range(window, n - window):
        left_bg = profile[max(0, i - window):i].mean()
        right_bg = profile[i + 1:min(n, i + window + 1)].mean()
        local_bg = min(left_bg, right_bg)
        prom = profile[i] - local_bg
        if prom > prominence:
            peaks.append({"pos": i, "val": float(profile[i]), "prom": prom})
    merged = []
    for p in peaks:
        if merged and p["pos"] - merged[-1]["pos"] < merge_px:
            if p["val"] > merged[-1]["val"]:
                merged[-1] = p
        else:
            merged.append(dict(p))
    return merged


def _keep_balanced(peaks, total, min_frac):
    """Greedily accept peaks that keep all segments >= min_frac of total."""
    candidates = sorted(peaks, key=lambda p: -p["val"])
    accepted = []
    for p in candidates:
        test = sorted([q["pos"] for q in accepted] + [p["pos"]])
        cuts = [0] + test + [total]
        segs = [cuts[i + 1] - cuts[i] for i in range(len(cuts) - 1)]
        if all(s >= total * min_frac for s in segs):
            accepted.append(p)
    return sorted(accepted, key=lambda p: p["pos"])


def find_separators(profile, total, prominence=20):
    """
    Find photo separator lines in a brightness profile.

    Strategy:
    1. Find all bright peaks with given prominence.
    2. Filter to those that are 'bright enough' relative to the profile maximum
       (true white separator lines are near-white, not just locally bright).
    3. If that finds nothing, fall back to prominence-only with strict balance filter.
    """
    max_val = float(profile.max())
    merge_px = max(20, int(total * 0.03))

    # Strategy A: peaks that are clearly bright (>= 88% of max brightness in profile)
    # This handles color scans on white background where separator = near-white strip
    all_peaks = _raw_peaks(profile, prominence=max(prominence - 10, 5), merge_px=merge_px)
    bright_peaks = [p for p in all_peaks if p["val"] >= max_val * 0.88]
    result = _keep_balanced(bright_peaks, total, min_frac=0.20)

    if result:
        return result

    # Strategy B: fallback — use prominence alone but require balanced segments (>=30%)
    # This handles cases where the separator isn't near-white but is still relatively bright
    fallback_peaks = _raw_peaks(profile, prominence=prominence, merge_px=merge_px)
    return _keep_balanced(fallback_peaks, total, min_frac=0.30)


def split_image(img_path: Path, prominence: int = 20, padding: int = 0, rotation: int = 0):
    """Returns list of (PIL.Image, suffix) tuples."""
    img = Image.open(img_path).convert("RGB")
    arr = np.array(img)
    H, W = arr.shape[:2]

    scale = min(1.0, 1400 / max(W, H))
    SW, SH = int(W * scale), int(H * scale)
    small = img.resize((SW, SH), Image.LANCZOS)
    row_bright, col_bright = compute_profiles(np.array(small))

    # Trim white scanner border
    r0, r1, c0, c1 = trim_white_border(row_bright, col_bright)

    # Find separators within content area
    row_content = row_bright[r0:r1]
    col_content = col_bright[c0:c1]
    CH, CW = r1 - r0, c1 - c0

    h_seps = find_separators(row_content, CH, prominence)
    v_seps = find_separators(col_content, CW, prominence)

    # Convert back to full image coordinates
    h_cuts = [r0] + [s["pos"] + r0 for s in h_seps] + [r1]
    v_cuts = [c0] + [s["pos"] + c0 for s in v_seps] + [c1]

    results = []
    idx = 1
    for r in range(len(h_cuts) - 1):
        for c in range(len(v_cuts) - 1):
            y0, y1 = h_cuts[r], h_cuts[r + 1]
            x0, x1 = v_cuts[c], v_cuts[c + 1]
            cell_h, cell_w = y1 - y0, x1 - x0
            if cell_h < CH * 0.10 or cell_w < CW * 0.10:
                continue
            fx0 = max(0, int(x0 / scale) - padding)
            fy0 = max(0, int(y0 / scale) - padding)
            fx1 = min(W, int(x1 / scale) + padding)
            fy1 = min(H, int(y1 / scale) + padding)
            crop = img.crop((fx0, fy0, fx1, fy1))
            if rotation != 0:
                crop = crop.rotate(-rotation, expand=True)
            results.append((crop, f"_foto{idx}"))
            idx += 1

    return results


class ScanHandler(FileSystemEventHandler):
    def __init__(self, input_dir, output_dir, prominence, padding, rotation, status_callback=None):
        self.input_dir = Path(input_dir)
        self.output_dir = Path(output_dir)
        self.prominence = prominence
        self.padding = padding
        self.rotation = rotation
        self.status_callback = status_callback
        self._processing = set()

    def on_created(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix.lower() not in SUPPORTED:
            return
        if path in self._processing:
            return
        time.sleep(1.0)
        self._process(path)

    def _process(self, path: Path):
        self._processing.add(path)
        try:
            logger.info(f"Processing: {path.name}")
            self._emit({"event": "start", "file": path.name})
            photos = split_image(path, self.prominence, self.padding, self.rotation)
            if not photos:
                logger.warning(f"No photos found in {path.name}")
                self._emit({"event": "warning", "file": path.name, "msg": "Keine Fotos erkannt"})
                return
            stem = path.stem
            saved = []
            for photo_img, suffix in photos:
                out_name = f"{stem}{suffix}.jpg"
                out_path = self.output_dir / out_name
                photo_img.save(out_path, "JPEG", quality=95)
                saved.append(out_name)
                logger.info(f"  Saved: {out_name}")
            done_dir = self.input_dir / "done"
            done_dir.mkdir(exist_ok=True)
            shutil.move(str(path), done_dir / path.name)
            self._emit({"event": "done", "file": path.name, "saved": saved})
        except Exception as e:
            logger.error(f"Error processing {path.name}: {e}", exc_info=True)
            self._emit({"event": "error", "file": path.name, "msg": str(e)})
        finally:
            self._processing.discard(path)

    def _emit(self, data):
        if self.status_callback:
            self.status_callback(data)


def start_watcher(input_dir, output_dir, prominence=20, padding=0, rotation=0, status_callback=None):
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    handler = ScanHandler(input_dir, output_dir, prominence, padding, rotation, status_callback)
    observer = Observer()
    observer.schedule(handler, str(input_dir), recursive=False)
    observer.start()
    logger.info(f"Watching: {input_dir} → {output_dir}")
    return observer
