"""
OCR Service using EasyOCR with Vietnamese + English support.
Handles image downloading, caching, and text extraction.

Architecture: OCR runs in a persistent subprocess (ocr_worker.py) to isolate
EasyOCR/PyTorch memory from the main uvicorn process. Models are loaded once
when the worker starts and reused for all subsequent requests.
"""
import os
import re
import asyncio
import hashlib
import json
import logging
import tempfile
from collections import OrderedDict
from typing import Optional, List, Tuple
from io import BytesIO
from urllib.parse import urlparse, parse_qs

import httpx
from PIL import Image

logger = logging.getLogger(__name__)

# Persistent OCR worker process
_worker_process: Optional[asyncio.subprocess.Process] = None
_worker_lock = None  # Created lazily inside event loop
_worker_ready = False

# LRU cache for OCR results: URL -> ocr_text (avoids re-OCR across jobs/retries)
_OCR_CACHE_MAX = 5
_ocr_cache: OrderedDict[str, str] = OrderedDict()


# ── Image Download Helpers ──────────────────────────────────────────────

_GDRIVE_FILE_RE = re.compile(
    r"(?:drive\.google\.com/file/d/|drive\.google\.com/open\?id=|docs\.google\.com/.*?/d/)([a-zA-Z0-9_-]+)"
)
_GDRIVE_EXPORT_RE = re.compile(r"drive\.google\.com/uc\?")


def _resolve_gdrive_url(url: str) -> Optional[str]:
    """Convert various Google Drive share URLs to direct download links."""
    m = _GDRIVE_FILE_RE.search(url)
    if m:
        file_id = m.group(1)
        return f"https://drive.google.com/uc?id={file_id}&export=download"
    if _GDRIVE_EXPORT_RE.search(url):
        return url
    # Fallback: if URL contains drive.google.com but regex didn't match,
    # try to extract any 25+ char alphanumeric string as a potential file ID
    if "drive.google.com" in url:
        fallback = re.search(r"[a-zA-Z0-9_-]{25,}", url)
        if fallback:
            file_id = fallback.group(0)
            logger.info(f"GDrive fallback: extracted file ID {file_id} from {url}")
            return f"https://drive.google.com/uc?id={file_id}&export=download"
    return None


def _resolve_image_url(url: str) -> str:
    """Resolve various image URL formats to direct download URLs."""
    url = url.strip()
    # Google direct image URLs (lh3.googleusercontent.com) — already direct
    if "lh3.googleusercontent.com" in url:
        return url
    # Google Drive
    gdrive = _resolve_gdrive_url(url)
    if gdrive:
        return gdrive
    # Dropbox: replace dl=0 with dl=1
    if "dropbox.com" in url:
        url = url.replace("dl=0", "dl=1")
        if "dl=1" not in url:
            sep = "&" if "?" in url else "?"
            url += f"{sep}dl=1"
        return url
    # Gyazo: gyazo.com/{id} → i.gyazo.com/{id}.png
    gyazo_match = re.match(r"https?://(?:www\.)?gyazo\.com/([a-f0-9]+)(?:\?.*)?$", url)
    if gyazo_match:
        gyazo_id = gyazo_match.group(1)
        direct = f"https://i.gyazo.com/{gyazo_id}.png"
        logger.info(f"Gyazo URL resolved: {url} → {direct}")
        return direct
    # Imgur: imgur.com/{id} → i.imgur.com/{id}.png
    imgur_match = re.match(r"https?://(?:www\.)?imgur\.com/(?:a/)?([a-zA-Z0-9]+)(?:\?.*)?$", url)
    if imgur_match:
        imgur_id = imgur_match.group(1)
        direct = f"https://i.imgur.com/{imgur_id}.png"
        logger.info(f"Imgur URL resolved: {url} → {direct}")
        return direct
    # Prnt.sc / Lightshot — need scraping, pass through for now
    # Direct URL
    return url


_IMAGE_CACHE_DIR = "./data/image_cache"


def _cache_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


async def download_image(
    url: str,
    timeout: float = 60.0,
    max_retries: int = 5,
) -> bytes:
    """Download image from URL with retries and caching."""
    os.makedirs(_IMAGE_CACHE_DIR, exist_ok=True)
    cache_file = os.path.join(_IMAGE_CACHE_DIR, _cache_key(url))

    # Check cache
    if os.path.exists(cache_file):
        with open(cache_file, "rb") as f:
            return f.read()

    resolved = _resolve_image_url(url)
    last_error = None

    _HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(timeout),
            ) as client:
                resp = await client.get(resolved, headers=_HEADERS)

                # Detect throttling (429 / 503) — wait longer before retry
                if resp.status_code in (429, 503):
                    retry_after = int(resp.headers.get("Retry-After", 0))
                    wait = max(retry_after, 5 * (2 ** attempt))  # min 5s, exponential
                    logger.warning(f"Throttled ({resp.status_code}) for {url}, waiting {wait}s…")
                    import asyncio
                    await asyncio.sleep(wait)
                    continue

                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")

                # Google Drive virus scan warning page
                if "text/html" in content_type and "drive.google.com" in resolved:
                    confirm_url = resolved + "&confirm=t"
                    resp = await client.get(confirm_url, headers=_HEADERS)
                    resp.raise_for_status()

                data = resp.content
                # Validate it's actually an image
                try:
                    img = Image.open(BytesIO(data))
                    img.verify()
                except Exception:
                    raise ValueError(f"Downloaded content is not a valid image (content-type: {content_type})")

                # Cache it
                with open(cache_file, "wb") as f:
                    f.write(data)
                return data

        except Exception as e:
            last_error = e
            logger.warning(f"Download attempt {attempt + 1}/{max_retries} failed for {url}: {e}")
            if attempt < max_retries - 1:
                import asyncio
                wait = min(2 ** (attempt + 1), 30)  # 2s, 4s, 8s… capped at 30s
                await asyncio.sleep(wait)

    raise RuntimeError(f"Failed to download image after {max_retries} attempts: {last_error}")


async def _ensure_worker():
    """Start the persistent OCR worker if not already running."""
    global _worker_process, _worker_ready, _worker_lock

    # Create lock lazily (must be inside event loop)
    if _worker_lock is None:
        _worker_lock = asyncio.Lock()

    async with _worker_lock:
        if _worker_process and _worker_process.returncode is None:
            return  # Still alive

        logger.info("Starting persistent OCR worker process...")
        _worker_ready = False
        _worker_process = await asyncio.create_subprocess_exec(
            "python", "-m", "app.ocr_worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Wait for "ready" status
        while True:
            line = await asyncio.wait_for(
                _worker_process.stdout.readline(), timeout=120
            )
            if not line:
                raise RuntimeError("OCR worker died during startup")
            msg = json.loads(line.decode().strip())
            logger.info(f"OCR worker: {msg}")
            if msg.get("status") == "ready":
                _worker_ready = True
                break


async def run_ocr_async(image_data: bytes, cache_key: str = None) -> str:
    """
    Send image to persistent OCR worker process for text extraction.
    The worker loads EasyOCR models once and reuses them — no overhead
    per image. Keeps the uvicorn event loop free for API requests.
    """
    # Check LRU cache first
    if cache_key and cache_key in _ocr_cache:
        _ocr_cache.move_to_end(cache_key)
        logger.info(f"OCR cache hit for {cache_key[:16]}...")
        return _ocr_cache[cache_key]

    await _ensure_worker()

    # Write image to temp file
    with tempfile.NamedTemporaryFile(suffix=".img", delete=False, dir="/tmp") as tmp:
        tmp.write(image_data)
        tmp_path = tmp.name

    try:
        # Send request to worker
        request = json.dumps({"path": tmp_path}) + "\n"
        _worker_process.stdin.write(request.encode())
        await _worker_process.stdin.drain()

        # Read response (with timeout)
        line = await asyncio.wait_for(
            _worker_process.stdout.readline(), timeout=120
        )
        if not line:
            raise RuntimeError("OCR worker returned empty response (may have crashed)")

        resp = json.loads(line.decode().strip())

        if "error" in resp:
            raise RuntimeError(f"OCR worker error: {resp['error']}")

        texts = resp.get("texts", [])
        text = "\n".join(texts)

        # Store in LRU cache
        if cache_key:
            _ocr_cache[cache_key] = text
            if len(_ocr_cache) > _OCR_CACHE_MAX:
                _ocr_cache.popitem(last=False)

        return text

    except (asyncio.TimeoutError, RuntimeError) as e:
        logger.warning(f"OCR worker failed: {e}, will restart and retry with smaller image")
        # Kill worker so it restarts on next call
        global _worker_ready
        _worker_ready = False
        if _worker_process and _worker_process.returncode is None:
            try:
                _worker_process.kill()
            except ProcessLookupError:
                pass

        # Retry with a much smaller image (downscale to reduce memory)
        try:
            logger.info("Retrying OCR with downscaled image (max 800px)...")
            img = Image.open(BytesIO(image_data))
            if img.mode in ("RGBA", "P", "LA"):
                img = img.convert("RGB")
            # Aggressive downscale
            max_dim = 800
            if max(img.size) > max_dim:
                ratio = max_dim / max(img.size)
                new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
                img = img.resize(new_size, Image.LANCZOS)
            img = img.convert("L")
            buf = BytesIO()
            img.save(buf, format='PNG')
            small_data = buf.getvalue()
            del img, buf

            with tempfile.NamedTemporaryFile(suffix=".img", delete=False, dir="/tmp") as tmp2:
                tmp2.write(small_data)
                tmp2_path = tmp2.name
            del small_data

            await _ensure_worker()
            request2 = json.dumps({"path": tmp2_path}) + "\n"
            _worker_process.stdin.write(request2.encode())
            await _worker_process.stdin.drain()
            line2 = await asyncio.wait_for(_worker_process.stdout.readline(), timeout=120)
            os.unlink(tmp2_path)

            if line2:
                resp2 = json.loads(line2.decode().strip())
                if "texts" in resp2:
                    text = "\n".join(resp2["texts"])
                    if cache_key:
                        _ocr_cache[cache_key] = text
                        if len(_ocr_cache) > _OCR_CACHE_MAX:
                            _ocr_cache.popitem(last=False)
                    logger.info(f"Retry succeeded with {len(text)} chars")
                    return text
        except Exception as retry_err:
            logger.error(f"Retry also failed: {retry_err}")

        raise RuntimeError(f"OCR failed even after retry: {e}")

    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def clear_image_cache():
    """Clear the image cache directory."""
    import shutil
    if os.path.exists(_IMAGE_CACHE_DIR):
        shutil.rmtree(_IMAGE_CACHE_DIR)
        os.makedirs(_IMAGE_CACHE_DIR, exist_ok=True)
