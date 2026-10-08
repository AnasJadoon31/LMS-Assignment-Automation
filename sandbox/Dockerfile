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
# poppler renders pages to check them; graphviz for diagrams; compilers for C, C++ and Java labs
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-writer-nogui libreoffice-impress-nogui \
        fonts-crosextra-carlito fonts-crosextra-caladea fonts-dejavu-core \
        poppler-utils graphviz g++ default-jdk-headless \
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
RUN printf '#!/bin/sh\nexec python3 -m lms_sandbox.notebook "$@"\n' > /usr/local/bin/lms-notebook \
    && chmod +x /usr/local/bin/lms-notebook \
    && useradd --create-home --uid 10001 solver \
    && mkdir -p /work && chown solver /work

USER solver
WORKDIR /work
CMD ["python3", "-m", "lms_sandbox.run"]
