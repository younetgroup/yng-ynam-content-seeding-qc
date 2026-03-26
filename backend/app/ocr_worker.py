"""
Persistent OCR worker process.
Loads EasyOCR models ONCE, then processes images via stdin/stdout JSON protocol.
This isolates the OCR memory from the main uvicorn process while avoiding
the overhead of reloading models for each image.

Memory-optimized: aggressively resizes images and runs GC between requests.

Protocol:
  Request:  {"path": "/tmp/image.png"}\n
  Response: {"texts": ["line1", "line2", ...]}\n
  Error:    {"error": "message"}\n
"""
import gc
import sys
import json
import traceback
from io import BytesIO
from PIL import Image

# Limit PyTorch threads to reduce memory
import os
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"

import torch
torch.set_num_threads(1)
import easyocr

# Image size bounds for OCR quality vs memory trade-off
MAX_DIM = 1280   # Downscale large images to this (memory limit)
MIN_DIM = 960    # Upscale small images to this (EasyOCR needs ≥600px for Vietnamese diacritics)


def main():
    # Load models once at startup
    print(json.dumps({"status": "loading"}), flush=True)
    reader = easyocr.Reader(['vi', 'en'], gpu=False, verbose=False)
    gc.collect()
    print(json.dumps({"status": "ready"}), flush=True)

    # Process requests from stdin
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            path = req["path"]

            with open(path, "rb") as f:
                data = f.read()

            texts = _process_image(reader, data, MAX_DIM)

            del data
            gc.collect()

            print(json.dumps({"texts": texts}), flush=True)

        except Exception as e:
            gc.collect()
            print(json.dumps({"error": f"{type(e).__name__}: {str(e)}"}), flush=True)


def _get_col_saturation(img, x_step=20, y_step=20):
    """
    Compute per-column average saturation (chroma range R-B) sampled at x_step/y_step.
    Returns list of floats indexed by x (sampled, not every pixel).
    Fast enough for 1440×900 images (~3k pixel reads).
    """
    w, h = img.size
    # Work in RGB to compute chroma range (max-min of R,G,B)
    rgb = img.convert('RGB')
    y_start, y_end = int(h * 0.15), int(h * 0.85)  # skip top/bottom chrome
    col_sats = {}
    for x in range(0, w, x_step):
        sats = []
        for y in range(y_start, y_end, y_step):
            r, g, b = rgb.getpixel((x, y))
            sats.append(max(r, g, b) - min(r, g, b))
        col_sats[x] = sum(sats) / len(sats) if sats else 0
    return col_sats


def _is_tiktok_desktop(img):
    """
    Detect full TikTok desktop screenshot (3-column layout: sidebar | video | comments).
    Works for both light and dark themes.

    3-column structure check:
      Left  10-15%  : sidebar — low saturation (icons/text on neutral bg)
      Middle 15-65% : video   — HIGH saturation (colorful video frame)
      Right  65-95% : comments— low saturation (text on neutral bg)

    The presence of a high-saturation VIDEO COLUMN strictly between 15% and 65%
    with low-saturation regions on both sides is the reliable fingerprint.

    Validated:
      Light mode 1440×900 → True   (video max_sat=88, left_sat≈12, right_sat≈8)
      Dark  mode 1440×900 → True   (video max_sat=75, left_sat≈10, right_sat≈6)
      Comment-only 360×155 → False (overall max_sat=13, too small and no video region)
      Comment-only 358×161 → False (overall max_sat=17)
    """
    w, h = img.size
    ratio = w / h
    # Must be large landscape (desktop screenshot resolution)
    if w < 1200 or not (1.4 <= ratio <= 1.95):
        return False

    col_sats = _get_col_saturation(img, x_step=15, y_step=15)
    if not col_sats:
        return False

    # Split into 3 bands
    left_xs   = [x for x in col_sats if x < w * 0.15]
    video_xs  = [x for x in col_sats if w * 0.15 <= x <= w * 0.68]
    right_xs  = [x for x in col_sats if x > w * 0.68]

    def avg(xs):
        return sum(col_sats[x] for x in xs) / len(xs) if xs else 0

    left_sat  = avg(left_xs)
    video_sat = max((col_sats[x] for x in video_xs), default=0)  # peak, not avg
    right_sat = avg(right_xs)

    # TikTok desktop: video band has MUCH higher saturation than both flanks
    return video_sat > 40 and video_sat > left_sat * 2.5 and video_sat > right_sat * 2.5


def _find_comments_panel_start(img):
    """
    Auto-detect the comments panel x-position using saturation transition.
    Works for both light and dark TikTok themes: the video frame has high
    saturation, the comments panel (text on neutral background) has low sat.

    Strategy: find where the sustained high-saturation video region ends.
    Scan left→right from 40% of width; find where sat drops below threshold
    for 3+ consecutive columns (the gap between video and comments).
    Falls back to 60% if transition not detected.
    """
    w, h = img.size
    col_sats = _get_col_saturation(img, x_step=15, y_step=15)
    xs = sorted(col_sats.keys())
    if not xs:
        return int(w * 0.60)

    # Find peak saturation in the video area (15-65%)
    video_xs = [x for x in xs if w * 0.15 <= x <= w * 0.65]
    if not video_xs:
        return int(w * 0.60)
    peak_sat = max(col_sats[x] for x in video_xs)
    if peak_sat < 30:
        return int(w * 0.60)  # no clear video region

    threshold = peak_sat * 0.30  # 30% of peak

    # Scan left→right starting from 40% of width
    # Find where saturation drops below threshold for 3+ consecutive columns
    start_idx = 0
    for i, x in enumerate(xs):
        if x >= w * 0.40:
            start_idx = i
            break

    consecutive_low = 0
    for i in range(start_idx, len(xs)):
        x = xs[i]
        if x > w * 0.92:
            break
        if col_sats[x] < threshold:
            consecutive_low += 1
            if consecutive_low >= 3:
                # Found the transition — return the first low-sat column
                return xs[i - 2]
        else:
            consecutive_low = 0

    return int(w * 0.60)  # fallback


def _crop_tiktok_comments(img):
    """
    Only crop when image is confirmed TikTok desktop (large landscape + colorful video).
    Auto-detects comments panel start via saturation transition — handles both
    light mode (white sidebar) and dark mode (dark sidebar) correctly.
    Comment-only screenshots and mobile screenshots are untouched.
    """
    if not _is_tiktok_desktop(img):
        return img
    cut_x = _find_comments_panel_start(img)
    return img.crop((cut_x, 0, img.size[0], img.size[1]))


def _process_image(reader, data, max_dim):
    """Process image with preprocessing and OCR. Returns list of text strings."""
    from PIL import ImageEnhance

    img = Image.open(BytesIO(data))

    # Convert to RGB
    if img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGB")

    # Smart crop: TikTok desktop screenshots → keep only comments panel (right ~40%)
    img = _crop_tiktok_comments(img)

    # Resize: downscale large images (memory), upscale tiny images (OCR quality)
    # EasyOCR needs ≥600px for Vietnamese diacritics; ≥960px for reliable results
    current_max = max(img.size)
    if current_max > max_dim:
        scale = max_dim / current_max
        img = img.resize((int(img.size[0] * scale), int(img.size[1] * scale)), Image.LANCZOS)
    elif current_max < MIN_DIM:
        scale = MIN_DIM / current_max
        img = img.resize((int(img.size[0] * scale), int(img.size[1] * scale)), Image.LANCZOS)

    # Enhance contrast + sharpen — helps with Vietnamese diacritics
    img = ImageEnhance.Contrast(img).enhance(1.4)
    img = ImageEnhance.Sharpness(img).enhance(2.0)

    # Convert to grayscale for cleaner OCR
    img = img.convert("L")

    # Auto-invert dark-background images (white text on dark bg)
    # EasyOCR is trained on dark-text-light-bg; dark backgrounds → empty results
    from PIL import ImageOps
    pixels = list(img.getdata())
    avg_brightness = sum(pixels) / len(pixels)
    sys.stderr.write(f"[OCR] img size={img.size}, avg_brightness={avg_brightness:.1f}\n")
    sys.stderr.flush()

    if avg_brightness < 127:
        img = ImageOps.invert(img)
        sys.stderr.write(f"[OCR] INVERTED (dark background detected)\n")
        sys.stderr.flush()

    # Dual-pass OCR: try normal first, if empty try inverted (or vice versa)
    # This handles edge cases where brightness detection alone isn't enough
    def _ocr_image(pil_img):
        buf = BytesIO()
        pil_img.save(buf, format='PNG')
        img_bytes = buf.getvalue()
        del buf
        results = reader.readtext(
            img_bytes, detail=1, paragraph=False,
            contrast_ths=0.3, adjust_contrast=0.7,
            text_threshold=0.5, low_text=0.3,
            width_ths=0.7,
        )
        del img_bytes
        texts = []
        for item in results:
            if len(item) >= 2:
                text = item[1].strip()
                if text:
                    texts.append(text)
        del results
        return texts

    texts = _ocr_image(img)
    sys.stderr.write(f"[OCR] first pass: {len(texts)} texts\n")
    sys.stderr.flush()

    # If first pass returns nothing, try the opposite (inverted/normal)
    if not texts:
        img_alt = ImageOps.invert(img)
        texts = _ocr_image(img_alt)
        sys.stderr.write(f"[OCR] fallback pass (inverted): {len(texts)} texts\n")
        sys.stderr.flush()
        del img_alt

    del img
    gc.collect()
    return texts


if __name__ == "__main__":
    main()
