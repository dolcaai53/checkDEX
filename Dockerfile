FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgmp-dev \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 appuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data && chown appuser:appuser /data && \
    chown -R appuser:appuser /app

USER appuser

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import os, time; f='/tmp/healthy'; exit(0 if os.path.exists(f) and time.time()-os.path.getmtime(f)<60 else 1)"

CMD ["python", "-m", "app.main"]
