# Worker: LMS adapter (Playwright + Chromium), poller, notifier, solver jobs.
FROM mcr.microsoft.com/playwright/python:v1.56.0-noble

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    STATE_DIR=/data/state

WORKDIR /app
COPY pyproject.toml ./
COPY lms_worker ./lms_worker
RUN pip install --break-system-packages . && useradd --create-home --uid 10001 worker \
    && mkdir -p /data && chown worker /data

USER worker
VOLUME ["/data"]
CMD ["sh", "-c", "lms-worker db-upgrade && exec lms-worker run"]
