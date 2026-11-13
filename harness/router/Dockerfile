# Dockerfile
# Place at: D:\Projects\bifrost-router\Dockerfile
# Build:    docker build -t 192.168.x.4:5000/bifrost-router:latest .   (Bifrost)

FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# v114.10 PROGRESSIVE-SKILLS-1: skill registry baked at build time (write-rare default).
# Bind-mount becomes preferred when Session O P3 Memento-Skills generates skills at runtime.
COPY skills/ /app/skills/

# Non-root user
RUN useradd -m bifrost && chown -R bifrost:bifrost /app
USER bifrost

EXPOSE 8080

# ROUTER-V-FINISH: bake the git SHA so /status reports the real commit (was "unknown").
# Pass --build-arg BIFROST_COMMIT=$(git rev-parse --short HEAD) at docker build time.
ARG BIFROST_COMMIT=unknown
ENV BIFROST_COMMIT=${BIFROST_COMMIT}
# Provenance label (2026-07-14): the deployed-source-in-git invariant checks THIS.
# A "-dirty" suffix (stamped by build.ps1 on an uncommitted tree) means the image
# matches no commit -- exactly the failure that shipped bifrost-kb/v214 image-only.
LABEL org.opencontainers.image.revision=${BIFROST_COMMIT}


CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-keep-alive", "75", "--loop", "asyncio"]
