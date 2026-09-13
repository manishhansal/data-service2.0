# =============================================================================
# DATA-SERVICE 2.0 — Multi-Stage Dockerfile
# Requirements: 20.1, 20.2, 19.1
#
# Stage 1 (builder): installs all Python dependencies into /install
# Stage 2 (runtime): minimal image with only runtime artefacts; non-root user
#
# Build:  docker build -t data-service:2.0.0 .
# Run:    docker run -p 8200:8200 --env-file .env data-service:2.0.0
# =============================================================================

# ---------------------------------------------------------------------------
# Stage 1 — builder
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS builder

WORKDIR /app

# Install system build dependencies required by some Python packages
# (e.g. asyncpg, hiredis, curl-cffi, pyarrow).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        libffi-dev \
        libssl-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Copy only the dependency manifest first to leverage Docker layer caching.
# Re-run pip only when pyproject.toml changes.
COPY pyproject.toml ./

# Generate a requirements.txt from pyproject.toml for reproducible installs,
# then install everything into /install so the runtime stage has a clean copy.
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir hatchling \
    && pip install --no-cache-dir --prefix=/install . \
    && pip install --no-cache-dir --prefix=/install uvloop

# ---------------------------------------------------------------------------
# Stage 2 — runtime
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

# Metadata labels
LABEL org.opencontainers.image.title="DATA-SERVICE 2.0" \
      org.opencontainers.image.description="Standalone production-grade Market Data Platform" \
      org.opencontainers.image.version="2.0.0" \
      org.opencontainers.image.vendor="AlphaForge Engineering"

WORKDIR /app

# Install minimal runtime system libraries (no build tools).
# libssl is needed by asyncpg/httpx/curl-cffi at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libssl3 \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from the builder stage.
COPY --from=builder /install /usr/local

# Copy application source code.
COPY src/ ./src/
COPY alembic/ ./alembic/

# ---------------------------------------------------------------------------
# Non-root user (Requirement 19.1 — least-privilege execution)
# ---------------------------------------------------------------------------
RUN groupadd --system --gid 1001 appgroup \
    && useradd --system --uid 1001 --gid appgroup --no-create-home appuser \
    # Allow the app user to write nothing outside /app/tmp
    && mkdir -p /app/tmp \
    && chown -R appuser:appgroup /app/tmp

USER appuser

# ---------------------------------------------------------------------------
# Runtime configuration
# ---------------------------------------------------------------------------
# Default port — overridable via DATA_SERVICE_PORT env var.
ENV DATA_SERVICE_PORT=8200 \
    # Keep Python from buffering stdout/stderr (important for structlog).
    PYTHONUNBUFFERED=1 \
    # Prevent Python from writing .pyc files into the image.
    PYTHONDONTWRITEBYTECODE=1 \
    # Add /usr/local/lib to path for installed packages.
    PYTHONPATH=/app

EXPOSE 8200

# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
# /v1/health/live always returns HTTP 200 without blocking on dependencies.
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:${DATA_SERVICE_PORT}/v1/health/live || exit 1

# ---------------------------------------------------------------------------
# Default command — overridden per service in docker-compose.yml
# ---------------------------------------------------------------------------
CMD ["uvicorn", "src.server:app", \
     "--host", "0.0.0.0", \
     "--port", "8200", \
     "--workers", "4", \
     "--loop", "uvloop", \
     "--log-config", "/dev/null"]
