# Official Python images support both linux/arm64 (Oracle Ampere) and amd64.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 ybot \
    && useradd --uid 10001 --gid ybot --no-create-home ybot

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY bot.py config.py ./
COPY commands ./commands
COPY database ./database
COPY services ./services
COPY utils ./utils
RUN mkdir -p /app/data /app/backups \
    && chown -R 10001:10001 /app/data /app/backups \
    && chmod 700 /app/data /app/backups

USER 10001:10001
STOPSIGNAL SIGTERM
CMD ["python", "bot.py"]
