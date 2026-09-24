# Recut — Python 3.12 slim with FFmpeg (future video features)
FROM python:3.12-slim

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-dejavu-core \
    ffmpeg \
    gcc \
    libc-dev \
    libavcodec-dev \
    libavformat-dev \
    libavutil-dev \
    libswscale-dev \
    libswresample-dev \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd -m -s /bin/bash recut && mkdir -p /tmp/recut && chown recut:recut /tmp/recut

WORKDIR /app

# Copy only dependencies first for layer caching
COPY pyproject.toml .
RUN pip install --no-cache-dir -e . 2>&1 | tail -3

# Copy source
COPY app/ ./app/

# Environment
ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    LOG_LEVEL=INFO

USER recut

# Railway injects PORT; bind to all interfaces
EXPOSE 8080

# Health endpoint for Railway
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/healthz || exit 1

CMD ["python", "-m", "app.main"]