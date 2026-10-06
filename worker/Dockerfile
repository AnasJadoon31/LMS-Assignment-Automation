# Worker: LMS adapter (Playwright + Chromium), poller, notifier, report renderer.
FROM mcr.microsoft.com/playwright/python:v1.56.0-noble

# LibreOffice turns the .docx into a PDF. Carlito and Caladea have the same metrics as
# Calibri and Cambria, so lines break as in Word; put licensed Calibri, Cambria and
# Consolas .ttf files in worker/fonts/ to use the real ones.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-writer-nogui fonts-crosextra-carlito fonts-crosextra-caladea fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
COPY fonts/ /usr/share/fonts/truetype/local/
RUN fc-cache -f > /dev/null

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
