# AuditLens backend image.
#
# Two targets share one base: `api` stays lean, while `worker` additionally
# installs the OCR toolchain and the self-hosted embedding model (ADR-005,
# ADR-009). Keeping torch out of the API image is worth the extra stage — it is
# roughly 2GB that the request path never touches.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Runtime libraries needed for passive document parsing.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libjpeg62-turbo \
        zlib1g \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./

RUN pip install --no-cache-dir .

# Run as a non-root user: uploaded files are untrusted input, and the extraction
# libraries parse them (05_SECURITY.md §10.5).
RUN useradd --create-home --uid 10001 auditlens \
    && mkdir -p /data/evidence \
    && chown -R auditlens:auditlens /data /app
USER auditlens


# --- API target --------------------------------------------------------------
FROM base AS api
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]


# --- Worker target -----------------------------------------------------------
FROM base AS worker
USER root
# Tesseract backs the OCR path for images and scanned PDFs
# (01_REQUIREMENTS.md § Evidence Document Ingestion, processing rule 2).
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "sentence-transformers>=3.3"
ENV HF_HOME=/models
RUN mkdir -p /models && chown -R auditlens:auditlens /models
USER auditlens
CMD ["python", "-m", "app.pipelines.worker"]
