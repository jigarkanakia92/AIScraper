# --- Stage 1: builder -----------------------------------------------
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build deps for lxml / psycopg / aiohttp
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        libxml2-dev \
        libxslt-dev \
        libpq-dev \
        libffi-dev \
        libssl-dev \
        cargo \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN pip install --upgrade pip && pip install --prefix=/install -r requirements.txt


# --- Stage 2: runtime -----------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Runtime libs needed by Playwright + lxml + psycopg
RUN apt-get update && apt-get install -y --no-install-recommends \
        libxml2 \
        libxslt1.1 \
        libpq5 \
        libnss3 \
        libnspr4 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libdrm2 \
        libxkbcommon0 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxrandr2 \
        libgbm1 \
        libasound2 \
        libpangocairo-1.0-0 \
        libpango-1.0-0 \
        libcairo2 \
        curl \
        dumb-init \
    && rm -rf /var/lib/apt/lists/*

# Non-root user
RUN groupadd --system --gid 1000 appuser \
    && useradd --system --uid 1000 --gid appuser --create-home --shell /bin/bash appuser

WORKDIR /app
COPY --from=builder /install /usr/local
COPY --chown=appuser:appuser . /app
USER appuser

# Install Playwright browsers (chromium only — fastest)
RUN python -m playwright install chromium 2>&1 | tail -5 || true

# Entrypoint
COPY --chown=appuser:appuser docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh
ENTRYPOINT ["/usr/bin/dumb-init", "--", "/app/docker-entrypoint.sh"]

# Healthcheck — curl a localhost status endpoint if you add one.
# Here we just verify the process is alive (ps works) and that we can import.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import app.main" || exit 1

CMD ["python", "-m", "app.main"]
