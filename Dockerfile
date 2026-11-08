# Stage 1: Build virtual environment using uv
FROM ghcr.io/astral-sh/uv:latest AS uv

FROM python:3.13-slim-bookworm AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Copy uv binary
COPY --from=uv /uv /uvx /bin/

# Install dependencies into /app/.venv without project
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

# Copy application source code and install project
COPY app ./app
RUN uv sync --frozen --no-dev


# Stage 2: Runtime environment
FROM python:3.13-slim-bookworm AS runner

# Install ffmpeg, curl (for container healthcheck), and ca-certificates
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Deno (MIT, official denoland image, multi-arch): JS runtime for yt-dlp's YouTube
# challenge solver (yt-dlp-ejs). Pinned version.
COPY --from=denoland/deno:bin-2.9.8 /deno /usr/local/bin/deno

# Create non-root user with UID:GID 1000:1000
RUN groupadd -g 1000 app && \
    useradd -u 1000 -g app -s /bin/bash -m app

# Create required volume and application directories
RUN mkdir -p /data /media /app && \
    chown -R 1000:1000 /data /media /app

WORKDIR /app

# Copy virtual environment and application code from builder
COPY --from=builder --chown=1000:1000 /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH="/data/app.db" \
    MEDIA_PATH="/media"

USER 1000:1000

EXPOSE 8000

VOLUME ["/data", "/media"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
