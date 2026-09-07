FROM python:3.11-slim AS base

WORKDIR /app

# System deps for PostgreSQL driver, Tesseract OCR, and Playwright browser libs
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    wget \
    ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcairo2 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libexpat1 \
    libfontconfig1 \
    libgbm1 \
    libgl1 \
    libglib2.0-0 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libsm6 \
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxrender1 \
    libxfixes3 \
    libxrandr2 \
    libxext6 \
    libxshmfence1 \
    xdg-utils \
    tesseract-ocr \
    tesseract-ocr-rus \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# ── API target: no Playwright needed ──────────────────────────────────────
FROM base AS api
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

# ── Runtime target: Playwright + Chromium for scheduler/collectors ────────
FROM base AS runtime
ARG PLAYWRIGHT_DOWNLOAD_HOST=
ENV PLAYWRIGHT_DOWNLOAD_HOST=${PLAYWRIGHT_DOWNLOAD_HOST}
RUN playwright install chromium
CMD ["python", "scheduler/scheduler.py"]

# ── Collector target: inherits runtime with Playwright ────────────────────
FROM runtime AS collector
CMD ["python", "-m", "src.pipeline.runner"]
