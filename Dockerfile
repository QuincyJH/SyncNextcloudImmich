# syntax=docker/dockerfile:1

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=UTC

# Install OS deps and CA certs
RUN apt-get update -y && apt-get install -y --no-install-recommends \
  ca-certificates curl tzdata tar gzip \
    && rm -rf /var/lib/apt/lists/*

# (immich-go optional) Mount a pre-downloaded binary to /usr/local/bin/immich-go

# Copy app and install Python deps
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy scripts
COPY . /app
EXPOSE 8000

# Non-root user
RUN useradd -u 10001 -m appuser
USER appuser

# Default volumes
VOLUME ["/config", "/cache"]

ENV PATH="/app:${PATH}"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
