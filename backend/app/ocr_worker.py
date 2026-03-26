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

# Resize to keep memory manageable but keep enough detail for Vietnamese diacritics
# 1280px balances memory (~1.9GB Docker limit) with diacritic accuracy
MAX_DIM = 1280


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


def _crop_tiktok_comments(img):
    """
    Detect TikTok desktop layout and crop to the comments panel only.

    TikTok web desktop screenshots are landscape (e.g. 1440×900, ratio ~1.6).
    Layout: [left nav ~11%] [video ~49%] [comments panel ~40%]
    Cropping to the right 40% removes noise and improves OCR accuracy.

    Mobile/portrait screenshots are returned unchanged.
    """
    w, h = img.size
    ratio = w / h
    # Landscape threshold: wider than ~4:3 → likely desktop TikTok
    if ratio > 1.4:
        # Crop right 40% — where the comment panel lives
        left = int(w * 0.60)
        img = img.crop((left, 0, w, h))
    return img


def _process_image(reader, data, max_dim):
    """Process image with preprocessing and OCR. Returns list of text strings."""
    from PIL import ImageEnhance

    img = Image.open(BytesIO(data))

    # Convert to RGB
    if img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGB")

    # Smart crop: TikTok desktop screenshots → keep only comments panel (right 40%)
    img = _crop_tiktok_comments(img)

    # Resize large images but keep enough detail for diacritics
    if max(img.size) > max_dim:
        ratio = max_dim / max(img.size)
        new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
        img = img.resize(new_size, Image.LANCZOS)

    # Enhance contrast + sharpen — helps with Vietnamese diacritics
    img = ImageEnhance.Contrast(img).enhance(1.4)
    img = ImageEnhance.Sharpness(img).enhance(2.0)

    # Convert to grayscale for cleaner OCR
    img = img.convert("L")

    # Convert to bytes for EasyOCR — use PNG (lossless, better for OCR)
    buf = BytesIO()
    img.save(buf, format='PNG')
    img_bytes = buf.getvalue()

    del img, buf
    gc.collect()

    # Tuned params for Vietnamese diacritics
    results = reader.readtext(
        img_bytes, detail=1, paragraph=False,
        contrast_ths=0.3, adjust_contrast=0.7,
        text_threshold=0.5, low_text=0.3,
        width_ths=0.7,
    )

    del img_bytes
    gc.collect()

    texts = []
    for item in results:
        if len(item) >= 2:
            text = item[1].strip()
            if text:
                texts.append(text)

    del results
    gc.collect()
    return texts


if __name__ == "__main__":
    main()
