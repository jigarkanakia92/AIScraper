# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# AIScraper image.
#
# Base image note: python:3.14-slim is built on Debian 13 ("trixie"). Trixie's
# 64-bit time_t transition renamed many shared libraries (libasound2 ->
# libasound2t64, libgtk-3-0 -> libgtk-3-0t64, ...), which is why Chromium's
# system libraries are deliberately NOT hardcoded below: `crawl4ai-setup`
# installs them through `playwright install --with-deps`, and Playwright keeps
# that per-distribution mapping up to date.
# ---------------------------------------------------------------------------

# --- Stage 1: builder -----------------------------------------------
FROM python:3.14-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build deps for the handful of C extensions (lxml, psycopg2, PyYAML, ...).
# `cargo` is intentionally absent: every dependency in requirements.txt ships a
# cp314-compatible wheel except jstyleson (pure Python, needs no compiler).
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        libxml2-dev \
        libxslt1-dev \
        libpq-dev \
        libffi-dev \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    python -m pip install --upgrade pip \
    && python -m pip install --prefix=/install -r requirements.txt


# --- Stage 2: runtime -----------------------------------------------
FROM python:3.14-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    CRAWL4_AI_BASE_DIRECTORY=/home/appuser

# Runtime libs for lxml / psycopg2 / libpq, plus init + CA roots.
# Chromium's own shared libraries are installed further down (see comment).
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        libxml2 \
        libxslt1.1 \
        libpq5 \
        ca-certificates \
        curl \
        dumb-init \
    && rm -rf /var/lib/apt/lists/*

# Non-root user. `useradd`/`groupadd` come from the `passwd` package, which is
# part of the Debian base image (Priority: required).
RUN groupadd --system --gid 1000 appuser \
    && useradd --system --uid 1000 --gid appuser --create-home --shell /bin/bash appuser

WORKDIR /app

# Python packages + console scripts (alembic, crawl4ai-setup, pytest, ...).
COPY --from=builder /install /usr/local

# Browsers and their OS dependencies, installed as root before privileges are
# dropped. PLAYWRIGHT_BROWSERS_PATH (set above) sends the download to a shared
# image path that the non-root runtime user can read; the default
# ~/.cache/ms-playwright would be owned by root and invisible to appuser, which
# is the classic "Executable doesn't exist" failure at runtime.
# crawl4ai-setup == playwright install --with-deps chromium
#                + patchright install --with-deps chromium
#                + ~/.crawl4ai layout + Crawl4AI's SQLite init.
RUN crawl4ai-setup \
    && chmod -R a+rX "${PLAYWRIGHT_BROWSERS_PATH}" \
    && chown -R appuser:appuser /home/appuser

# Guard: crawl4ai-setup swallows browser-install errors, so fail the build
# loudly here instead of crash-looping at runtime.
RUN python -c "import os,pathlib,sys; b=pathlib.Path(os.environ['PLAYWRIGHT_BROWSERS_PATH']); f=[str(p) for p in b.glob('chromium*/*/chrome')]+[str(p) for p in b.glob('chromium*/**/headless_shell')]; print('Chromium binaries:', f or 'NONE'); sys.exit(0 if f else 'ERROR: no Chromium binary under '+str(b))"

# Application source, owned by the runtime user.
COPY --chown=appuser:appuser . /app

# Entrypoint — normalise line endings and permissions *inside* the image.
# A Windows checkout produces CRLF, which turns the shebang into "#!/bin/sh\r"
# and yields the classic
#   [dumb-init] /usr/local/bin/docker-entrypoint.sh: No such file or directory
# error. `sh -n` then fails the build if the script is malformed.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh \
    && chmod 0755 /usr/local/bin/docker-entrypoint.sh \
    && sh -n /usr/local/bin/docker-entrypoint.sh

USER appuser

ENTRYPOINT ["/usr/bin/dumb-init", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "app.main"]

# Cheap liveness probe: imports the app (builds the SQLAlchemy engine but opens
# no connection) without needing pgrep/procps.
HEALTHCHECK --interval=60s --timeout=10s --start-period=45s --retries=3 \
    CMD python -c "import app.main"
