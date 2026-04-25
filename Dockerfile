# --- builder ---------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip wheel \
 && /opt/venv/bin/pip install -r /app/requirements.txt

# --- runtime ---------------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    LABFLOW_LOG_JSON=true \
    LABFLOW_ENVIRONMENT=production

# Run as a non-root user — never run servers as root in production.
RUN groupadd --system --gid 1000 labflow \
 && useradd --system --uid 1000 --gid labflow --create-home labflow

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=labflow:labflow . /app

USER labflow
EXPOSE 8000

# Healthcheck hits the lightweight /healthz endpoint (no DB).
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2); sys.exit(0)" || exit 1

# Run migrations (idempotent) then start the API. Migrations are required
# in production — `init_db()`'s create_all is for dev/tests only.
CMD ["sh", "-c", "alembic upgrade head && uvicorn labflow.main:app --host 0.0.0.0 --port 8000 --workers ${LABFLOW_WORKERS:-2}"]
