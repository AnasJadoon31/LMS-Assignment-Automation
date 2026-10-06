# Worker and api: LMS adapter (Playwright + Chromium), poller, notifier, WhatsApp menu, the
# setup page, and the solver jobs, which run in sandbox containers. Claude Code is here only to
# sign in to your claude.ai account from the setup page; solving happens in the sandbox.

FROM node:22-bookworm-slim AS claude
ARG CLAUDE_CODE_VERSION=2.1.290
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
    && cp "$(readlink -f "$(npm root -g)/@anthropic-ai/claude-code/bin/claude.exe")" /claude \
    && /claude --version

FROM mcr.microsoft.com/playwright/python:v1.56.0-noble

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    STATE_DIR=/data/state \
    CLAUDE_HOME=/claude-home \
    DISABLE_AUTOUPDATER=1

COPY --from=claude /claude /usr/local/bin/claude
WORKDIR /app
COPY pyproject.toml ./
COPY lms_worker ./lms_worker
# /claude-home holds the claude.ai login; the sandbox's solver user has the same uid
RUN pip install --break-system-packages . && useradd --create-home --uid 10001 worker \
    && mkdir -p /data /claude-home && chown worker /data /claude-home

USER worker
VOLUME ["/data"]
CMD ["sh", "-c", "lms-worker db-upgrade && exec lms-worker run"]
