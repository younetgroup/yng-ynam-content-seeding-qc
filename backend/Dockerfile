FROM python:3.11-slim

# System deps for EasyOCR, Pillow, and general utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
# Install torch CPU-only FIRST (avoid downloading the full 420MB CUDA torch)
COPY requirements.txt .
RUN pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r requirements.txt

# Pre-download EasyOCR models for Vietnamese + English during build
# Non-fatal: models may be cached in the ocr_models volume at runtime
RUN python -c "import easyocr; easyocr.Reader(['vi', 'en'], gpu=False, verbose=True)" || echo "Model pre-download skipped (will download on first use)"

# Copy application code
COPY app/ ./app/

# Create data directories
RUN mkdir -p /app/data/uploads /app/data/results /app/data/image_cache

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
