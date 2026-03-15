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
    row_bright = gray.mean(axis=1)
    col_bright = gray.mean(axis=0)
    return row_bright, col_bright


def find_peaks(profile, prominence=46):
    n = len(profile)
    window = max(10, int(n * 0.04))
    peaks = []
    for i in range(window, n - window):
        left_bg = profile[max(0, i - window):i].mean()
        right_bg = profile[i + 1:min(n, i + window + 1)].mean()
        local_bg = min(left_bg, right_bg)
        if profile[i] - local_bg > prominence:
            peaks.append({"pos": i, "val": float(profile[i])})

    # Merge within 15px
    merged = []
    for p in peaks:
        if merged and p["pos"] - merged[-1]["pos"] < 15:
            if p["val"] > merged[-1]["val"]:
                merged[-1] = p
        else:
            merged.append(dict(p))
    return merged


def peaks_to_cuts(peaks, length):
    cuts = [0] + [p["pos"] for p in peaks] + [length]
    return cuts


def split_image(img_path: Path, prominence: int = 35, padding: int = 0, rotation: int = 0):
    """
    Returns list of (PIL.Image, suffix) tuples.
    suffix is like '_foto1', '_foto2', etc.
    """
    img = Image.open(img_path).convert("RGB")
    arr = np.array(img)
    H, W = arr.shape[:2]

    # Scale down for analysis
    scale = min(1.0, 1400 / max(W, H))
    SW, SH = int(W * scale), int(H * scale)
    small = img.resize((SW, SH), Image.LANCZOS)
    small_arr = np.array(small)

    row_bright, col_bright = compute_profiles(small_arr)
    h_peaks = find_peaks(row_bright, prominence)
    v_peaks = find_peaks(col_bright, prominence)

    # Filter: must be in middle 15-85%
    h_peaks = [p for p in h_peaks if SH * 0.15 < p["pos"] < SH * 0.85]
    v_peaks = [p for p in v_peaks if SW * 0.15 < p["pos"] < SW * 0.85]

    # Keep only peaks that produce balanced segments (each >= 20% of total).
    # This removes scanner-border lines that create tiny edge strips.
    def keep_balanced(peaks, total, min_frac=0.20):
        candidates = sorted(peaks, key=lambda p: -p["val"])
        accepted = []
        for p in candidates:
            test = sorted([q["pos"] for q in accepted] + [p["pos"]])
            cuts = [0] + test + [total]
            segs = [cuts[i+1] - cuts[i] for i in range(len(cuts)-1)]
            if all(s >= total * min_frac for s in segs):
                accepted.append(p)
        return accepted

    h_peaks = keep_balanced(h_peaks, SH)
    v_peaks = keep_balanced(v_peaks, SW)

    h_cuts = peaks_to_cuts(h_peaks, SH)
    v_cuts = peaks_to_cuts(v_peaks, SW)

    results = []
    idx = 1
    for r in range(len(h_cuts) - 1):
        for c in range(len(v_cuts) - 1):
            y0, y1 = h_cuts[r], h_cuts[r + 1]
            x0, x1 = v_cuts[c], v_cuts[c + 1]
            cell_h, cell_w = y1 - y0, x1 - x0
            if cell_h < SH * 0.08 or cell_w < SW * 0.08:
                continue

            # Map back to full res
            fx0 = max(0, int(x0 / scale) - padding)
            fy0 = max(0, int(y0 / scale) - padding)
            fx1 = min(W, int(x1 / scale) + padding)
            fy1 = min(H, int(y1 / scale) + padding)

            crop = img.crop((fx0, fy0, fx1, fy1))

            if rotation != 0:
                # PIL rotate is counter-clockwise, expand=True keeps full image
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
        # Small delay to ensure file is fully written
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

            # Move processed input to a 'done' subfolder
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


def start_watcher(input_dir, output_dir, prominence=35, padding=0, rotation=0, status_callback=None):
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    handler = ScanHandler(input_dir, output_dir, prominence, padding, rotation, status_callback)
    observer = Observer()
    observer.schedule(handler, str(input_dir), recursive=False)
    observer.start()
    logger.info(f"Watching: {input_dir} → {output_dir}")
    return observer
