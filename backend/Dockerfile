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

# Torch from the CPU index, before sentence-transformers pulls the default.
# The default wheel bundles CUDA libraries — roughly 2.5GB of them — which are
# dead weight on the documented deployment target (a small self-hosted Ubuntu
# box with no GPU, ADR-007). The CPU build is a fraction of the size and is
# what actually runs.
#
# The generous timeout and retries are not superstition: these are large wheels
# over a single connection, and a read timeout part-way through fails the whole
# image build.
RUN pip install --no-cache-dir --timeout 300 --retries 5 \
        --index-url https://download.pytorch.org/whl/cpu \
        torch \
    && pip install --no-cache-dir --timeout 300 --retries 5 \
        "sentence-transformers>=3.3"
ENV HF_HOME=/models
RUN mkdir -p /models && chown -R auditlens:auditlens /models
USER auditlens
CMD ["python", "-m", "app.pipelines.worker"]
