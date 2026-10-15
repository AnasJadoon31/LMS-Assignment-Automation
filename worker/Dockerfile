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
# Dependencies first, from pyproject.toml alone: a deploy that only changes code reuses this
# layer instead of downloading and installing every package again
COPY pyproject.toml ./
RUN python3 -c "import tomllib; print('\\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" \
        > /tmp/requirements.txt \
    && pip install --break-system-packages -r /tmp/requirements.txt \
    && useradd --create-home --uid 10001 worker \
    && mkdir -p /data /claude-home && chown worker /data /claude-home
# Then the code, which changes on most deploys; /claude-home holds the claude.ai login, and the
# sandbox's solver user has the same uid
COPY lms_worker ./lms_worker
RUN pip install --break-system-packages --no-deps .

USER worker
VOLUME ["/data"]
CMD ["sh", "-c", "lms-worker db-upgrade && exec lms-worker run"]
