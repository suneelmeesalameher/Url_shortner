# syntax=docker/dockerfile:1

# ---- Stage 1: builder --------------------------------------------------------
# Installs dependencies into an isolated virtualenv. Kept separate from the
# runtime stage so a compiler (only needed if a dependency has no prebuilt wheel
# for this platform) never ends up in the final image.
FROM python:3.11-slim AS builder

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ---- Stage 2: runtime ---------------------------------------------------------
# Same base image family as the builder (glibc-compatible, no ABI surprises) but
# with no compiler, no pip cache, and no build-time-only files - just the
# pre-built virtualenv and the application source.
FROM python:3.11-slim AS runtime

# Never run the app as root.
RUN groupadd --system app && useradd --system --gid app --no-create-home app

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY app ./app

USER app

EXPOSE 8000

# Uses Python's stdlib instead of curl/wget so no extra package has to be
# installed just for this - see GET /healthz in app/main.py.
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz', timeout=2)"]

# Structured JSON logs (app.core.logging_config) already go to stdout, so no
# extra log-driver configuration is needed for `docker logs`/log aggregators.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
