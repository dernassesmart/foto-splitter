"""
processor.py — Photo scanner splitter using MobileSAM for robust segmentation.

MobileSAM is downloaded once at Docker build time into /app/models/.
Falls back to classical OpenCV if model not available.
"""
import os
import logging
import numpy as np
from pathlib import Path
from PIL import Image

logger = logging.getLogger("foto-splitter")

SUPPORTED = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}
MODEL_PATH = Path(os.environ.get("SAM_MODEL", "/app/models/mobile_sam.pt"))

# ── MobileSAM loader (lazy, singleton) ───────────────────────────────────────
_sam = None
_predictor = None

def _load_sam():
    global _sam, _predictor
    if _predictor is not None:
        return _predictor
    if not MODEL_PATH.exists():
        logger.warning(f"MobileSAM model not found at {MODEL_PATH}, using fallback")
        return None
    try:
        from mobile_sam import sam_model_registry, SamAutomaticMaskGenerator
        logger.info("Loading MobileSAM model...")
        sam = sam_model_registry["vit_t"](checkpoint=str(MODEL_PATH))
        sam.eval()
        # CPU only — no CUDA needed
        _sam = sam
        _predictor = SamAutomaticMaskGenerator(
            model=sam,
            points_per_side=16,          # fewer points = faster
            pred_iou_thresh=0.88,
            stability_score_thresh=0.92,
            min_mask_region_area=5000,   # ignore tiny segments
        )
        logger.info("MobileSAM loaded successfully")
        return _predictor
    except Exception as e:
        logger.error(f"Failed to load MobileSAM: {e}")
        return None


# ── SAM-based segmentation ────────────────────────────────────────────────────
def _split_with_sam(img_pil: Image.Image, padding: int, rotation: int) -> list:
    """Use MobileSAM to find photo regions."""
    predictor = _load_sam()
    if predictor is None:
        return None  # signal fallback

    import cv2

    W, H = img_pil.size

    # Scale down for SAM (faster, SAM works well at ~1024px)
    scale = min(1.0, 1024 / max(W, H))
    SW, SH = int(W * scale), int(H * scale)
    small_pil = img_pil.resize((SW, SH), Image.LANCZOS)
    img_np = np.array(small_pil)

    try:
        masks = predictor.generate(img_np)
    except Exception as e:
        logger.error(f"SAM inference failed: {e}")
        return None

    if not masks:
        return None

    # Filter masks: keep only large rectangular regions (photos)
    # A photo on a scanner should be:
    # - Large (>5% of image area)
    # - Roughly rectangular (bbox fill ratio > 0.7)
    # - Not the entire image (< 95% of image area)
    total_area = SW * SH
    photo_masks = []

    for mask in masks:
        seg = mask['segmentation']
        area = mask['area']
        bbox = mask['bbox']  # x, y, w, h
        bbox_area = bbox[2] * bbox[3]

        area_frac = area / total_area
        fill_ratio = area / bbox_area if bbox_area > 0 else 0

        if area_frac < 0.05:   # too small
            continue
        if area_frac > 0.95:   # entire image = background/scanner
            continue
        if fill_ratio < 0.65:  # too irregular (not a photo)
            continue
        if bbox[2] < SW * 0.10 or bbox[3] < SH * 0.10:  # too narrow
            continue

        photo_masks.append({
            'bbox': bbox,  # in scaled coords
            'area_frac': area_frac,
            'fill_ratio': fill_ratio,
        })

    if not photo_masks:
        logger.info("SAM found no valid photo regions, using fallback")
        return None

    # Remove heavily overlapping masks (keep larger one)
    def iou(a, b):
        ax1,ay1,aw,ah = a['bbox']
        bx1,by1,bw,bh = b['bbox']
        ax2,ay2 = ax1+aw, ay1+ah
        bx2,by2 = bx1+bw, by1+bh
        ix1,iy1 = max(ax1,bx1), max(ay1,by1)
        ix2,iy2 = min(ax2,bx2), min(ay2,by2)
        if ix2<=ix1 or iy2<=iy1: return 0
        inter = (ix2-ix1)*(iy2-iy1)
        union = aw*ah + bw*bh - inter
        return inter/union if union>0 else 0

    filtered = []
    photo_masks.sort(key=lambda m: -m['area_frac'])
    for m in photo_masks:
        overlap = any(iou(m, f) > 0.4 for f in filtered)
        if not overlap:
            filtered.append(m)

    logger.info(f"SAM found {len(filtered)} photo regions")

    # Convert scaled bbox back to full resolution and crop
    results = []
    filtered.sort(key=lambda m: (m['bbox'][1], m['bbox'][0]))  # top-left order
    for i, m in enumerate(filtered):
        x, y, w, h = m['bbox']
        # Scale back to full resolution
        fx0 = max(0, int(x / scale) - padding)
        fy0 = max(0, int(y / scale) - padding)
        fx1 = min(W, int((x + w) / scale) + padding)
        fy1 = min(H, int((y + h) / scale) + padding)
        crop = img_pil.crop((fx0, fy0, fx1, fy1))
        if rotation != 0:
            crop = crop.rotate(-rotation, expand=True)
        results.append((crop, f"_foto{i+1}"))

    return results


# ── Classical fallback (OpenCV + scipy) ──────────────────────────────────────
def _split_classical(img_pil: Image.Image, prominence: int, padding: int, rotation: int) -> list:
    """Fallback: brightness profile + scipy peak detection."""
    import cv2
    from scipy.signal import find_peaks as sp_peaks

    img_cv = cv2.cvtColor(np.array(img_pil), cv2.COLOR_RGB2BGR)
    H, W = img_cv.shape[:2]
    gray = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY).astype(float)

    row_raw = gray.mean(axis=1)
    col_raw = gray.mean(axis=0)
    row_s = np.convolve(row_raw, np.ones(7)/7, mode='same')
    col_s = np.convolve(col_raw, np.ones(7)/7, mode='same')

    # Trim white border
    cr = np.where(row_s < 241)[0]
    cc = np.where(col_s < 241)[0]
    r0, r1 = (int(cr[0]), int(cr[-1])) if len(cr) else (0, H)
    c0, c1 = (int(cc[0]), int(cc[-1])) if len(cc) else (0, W)
    CH, CW = r1 - r0, c1 - c0
    if CH < 20 or CW < 20:
        return []

    min_frac = max(0.12, min(0.35, 0.18 + (prominence - 20) * 0.002))

    def find_cuts(profile, total):
        lo, hi = profile.min(), profile.max()
        if hi - lo < 3:
            return []
        norm = (profile - lo) / (hi - lo)
        min_dist = max(5, int(total * min_frac))
        all_found = {}
        for height in [0.70, 0.80, 0.90]:
            for prom in [0.05, 0.10, 0.15, 0.20]:
                peaks, props = sp_peaks(norm, height=height, prominence=prom, distance=min_dist)
                for p, pr in zip(peaks, props['prominences']):
                    if p < total * 0.12 or p > total * 0.88:
                        continue
                    score = pr + norm[p] * 0.3
                    all_found[int(p)] = max(all_found.get(int(p), 0), score)
        if not all_found:
            return []
        sorted_by_pos = sorted(all_found.items())
        merged = []
        for pos, score in sorted_by_pos:
            if merged and pos - merged[-1][0] <= 5:
                if score > merged[-1][1]:
                    merged[-1] = (pos, score)
            else:
                merged.append((pos, score))
        candidates = sorted(merged, key=lambda x: -x[1])

        def balanced(ps):
            cs = [0] + sorted(ps) + [total]
            return all((cs[i+1]-cs[i]) >= total*min_frac for i in range(len(cs)-1))

        accepted = []
        for pos, _ in candidates:
            if balanced(accepted + [pos]):
                accepted.append(pos)
        return sorted(accepted)

    h_cuts = [r0] + [r0+c for c in find_cuts(row_raw[r0:r1], CH)] + [r1]
    v_cuts = [c0] + [c0+c for c in find_cuts(col_raw[c0:c1], CW)] + [c1]

    results = []
    idx = 1
    for r in range(len(h_cuts)-1):
        for c in range(len(v_cuts)-1):
            y0, y1 = h_cuts[r], h_cuts[r+1]
            x0, x1 = v_cuts[c], v_cuts[c+1]
            if (y1-y0) < CH*0.10 or (x1-x0) < CW*0.10:
                continue
            crop = img_pil.crop((
                max(0, x0-padding), max(0, y0-padding),
                min(W, x1+padding), min(H, y1+padding)
            ))
            if rotation != 0:
                crop = crop.rotate(-rotation, expand=True)
            results.append((crop, f"_foto{idx}"))
            idx += 1
    return results


# ── Public API ────────────────────────────────────────────────────────────────
def split_image(img_path: Path, prominence: int = 20, padding: int = 0, rotation: int = 0):
    """
    Returns list of (PIL.Image, suffix) tuples.
    Uses MobileSAM if available, falls back to classical OpenCV+scipy.
    """
    img_pil = Image.open(img_path).convert("RGB")

    # Try SAM first
    results = _split_with_sam(img_pil, padding, rotation)
    if results is not None:
        return results

    # Fallback
    logger.info("Using classical fallback for segmentation")
    return _split_classical(img_pil, prominence, padding, rotation)
