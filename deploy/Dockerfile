# Stage 1: build Python dependencies
#
# The venv is built at /opt/venv — the SAME absolute path it occupies at
# runtime — because uv bakes an absolute shebang into every console script
# (#!/<venv>/bin/python). Building at /build/.venv and copying to /app/.venv
# left `alembic` and `uvicorn` pointing at a /build/.venv/bin/python that does
# not exist in stage 2; the kernel reports that as
#   exec /app/.venv/bin/alembic: no such file or directory
# naming the script rather than the missing interpreter. `python -m ...` was
# unaffected (bin/python is a symlink, not a shebang), which is why the worker
# and the one-off scripts ran while the api container could not boot at all.
#
# /opt/venv rather than /app/.venv for a second reason: deploy/compose.test.yml
# bind-mounts the repo over /app, which would shadow a venv living inside it.
FROM python:3.12-slim-bookworm AS builder
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev && rm -rf /var/lib/apt/lists/*
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev --no-install-project

# Stage 2: runtime
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr ocrmypdf libreoffice-writer-nogui libgl1 \
    fonts-liberation fonts-liberation2 ghostscript poppler-utils postgresql-client && \
    rm -rf /var/lib/apt/lists/*
COPY --from=builder /opt/venv /opt/venv
COPY app/ /app/app/
COPY alembic/ /app/alembic/
COPY alembic.ini /app/alembic.ini
COPY scripts/ /app/scripts/
COPY pyproject.toml /app/pyproject.toml
ENV PATH="/opt/venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')" || exit 1
CMD ["uvicorn", "app.web:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
