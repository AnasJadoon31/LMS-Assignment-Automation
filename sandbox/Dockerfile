# Solver sandbox: one throwaway container per assignment. Claude Code solves the assignment
# and builds the report from the theme; everything it needs for that is installed here.

# The Claude Code CLI is a single native binary; take it from the npm package
FROM node:22-bookworm-slim AS claude
ARG CLAUDE_CODE_VERSION=2.1.290
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
    && cp "$(readlink -f "$(npm root -g)/@anthropic-ai/claude-code/bin/claude.exe")" /claude \
    && /claude --version

# Python 3, Chromium and Playwright for the notebook screenshots
FROM mcr.microsoft.com/playwright/python:v1.56.0-noble

# LibreOffice for the PDF; Carlito and Caladea have Calibri's and Cambria's metrics, so pages
# break as in Word (put licensed Calibri, Cambria and Consolas in fonts/ to use the real ones);
# poppler renders pages to check them; graphviz for diagrams; compilers for C, C++ and Java labs;
# nano, tmux and the usual shell tools for Operating Systems labs done in a real terminal (lms-terminal)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-writer-nogui libreoffice-impress-nogui \
        fonts-crosextra-carlito fonts-crosextra-caladea fonts-dejavu-core \
        poppler-utils graphviz g++ gcc make default-jdk-headless \
        nano tmux procps psmisc less file tree \
    && rm -rf /var/lib/apt/lists/*
COPY fonts/ /usr/share/fonts/truetype/local/
RUN fc-cache -f > /dev/null

COPY --from=claude /claude /usr/local/bin/claude
RUN pip install --break-system-packages --no-cache-dir \
        python-docx matplotlib networkx graphviz schemdraw numpy pandas scipy sympy scikit-learn pillow \
        notebook nbclient nbformat ipykernel

COPY lms_sandbox /opt/lms/lms_sandbox
# Claude Code with every feature on ("essential traffic only" would turn off the Artifact tool
# and feature flags). Only self-updates are off: the image pins the version.
ENV PYTHONPATH=/opt/lms \
    MPLBACKEND=Agg \
    DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_ARTIFACT=1
# The user's name is what the terminal prompt shows (name@host); the home folder and the uid stay
# the same whatever the name, since the login volume is mounted there and the worker shares the uid
ARG SOLVER_USER=anas
RUN printf '#!/bin/sh\nexec python3 -m lms_sandbox.notebook "$@"\n' > /usr/local/bin/lms-notebook \
    && printf '#!/bin/sh\nexec python3 -m lms_sandbox.terminal "$@"\n' > /usr/local/bin/lms-terminal \
    && chmod +x /usr/local/bin/lms-notebook /usr/local/bin/lms-terminal \
    && useradd --create-home --home-dir /home/solver --shell /bin/bash --uid 10001 "$SOLVER_USER" \
    && mkdir -p /work && chown 10001 /work

USER 10001
WORKDIR /work
CMD ["python3", "-m", "lms_sandbox.run"]
