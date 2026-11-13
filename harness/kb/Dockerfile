# RECONSTRUCTED 2026-07-14 from running image bifrost/knowledge-base:1.18 (Hearth).
# The image carries NO original build context; this Dockerfile is INFERRED from the
# image's runtime facts and is UNVERIFIED until a rebuild proves /app matches 1.18
# (Brief 3 Phase 0, step 3). Do NOT treat as known-good until that check passes.
#
# Evidence driving each choice:
#   - base 3.11 (NOT 3.12): patch_chroma.py hardcodes /usr/local/lib/python3.11/...
#     and `docker exec` confirmed the image runs python 3.11.
#   - install from the FROZEN lock for exact reproduction (111 pkgs, pip freeze of 1.18).
#   - torch==2.11.0+cpu resolves only from the PyTorch CPU wheel index (keeps the image
#     ~556 MB; a default torch pull would be multi-GB CUDA).
#   - patch_chroma.py monkeypatches chromadb 0.5.20's posthog telemetry (0.5.x capture()
#     signature crash). It rewrites a site-packages file, so it RUNS at build time, after
#     pip install (needs chromadb + `overrides`), before the app is copied.
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Exact dependency lock captured from the running image (pip freeze of 1.18).
COPY requirements-frozen-1.18.txt .
RUN pip install --no-cache-dir \
    --extra-index-url https://download.pytorch.org/whl/cpu \
    -r requirements-frozen-1.18.txt

# ChromaDB 0.5.x telemetry-crash shim — must run after chromadb + overrides are installed.
COPY patch_chroma.py .
RUN python patch_chroma.py

COPY pipeline.py .

# Data directories (base inbox + auto-detected /data/inbox-* for multi-inbox).
RUN mkdir -p /data/chroma /data/inbox /data/processed /data/rejected

CMD ["uvicorn", "pipeline:app", "--host", "0.0.0.0", "--port", "8090", "--log-level", "info"]
