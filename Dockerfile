# syntax=docker/dockerfile:1.7

FROM python:3.13-slim-bookworm AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build

# Keep the resolver version explicit so the lockfile is the build contract.
RUN pip install --no-cache-dir uv==0.8.9

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --locked --no-dev --no-editable


FROM python:3.13-slim-bookworm AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install --no-install-recommends --yes \
        poppler-utils \
        tesseract-ocr \
        tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 openwikirag \
    && useradd --system --uid 10001 --gid 10001 --home-dir /app --create-home openwikirag

WORKDIR /app

COPY --from=builder --chown=openwikirag:openwikirag /opt/venv /opt/venv
COPY --chown=openwikirag:openwikirag src ./src
COPY --chown=openwikirag:openwikirag apps ./apps
COPY --chown=openwikirag:openwikirag migrations ./migrations
COPY --chown=openwikirag:openwikirag alembic.ini pyproject.toml ./

RUN mkdir -p /data/objects \
    && chown -R openwikirag:openwikirag /data

USER openwikirag:openwikirag

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"
