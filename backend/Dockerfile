FROM python:3.11-slim
WORKDIR /app

# System packages + Node 18
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libreoffice-impress \
        curl \
        git \
        ripgrep \
        ffmpeg \
        unzip \
        xz-utils \
    && curl -fsSL https://deb.nodesource.com/setup_18.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Claude Code + Codex use system Node 18
RUN npm install -g @anthropic-ai/claude-code @openai/codex

# Private Node 24 for OpenClaw and Hermes.
# This does NOT replace the system Node 18.
RUN curl -fsSL https://nodejs.org/dist/v24.16.0/node-v24.16.0-linux-x64.tar.xz \
    | tar -xJ -C /opt \
    && mv /opt/node-v24.16.0-linux-x64 /opt/node24

RUN export PATH="/opt/node24/bin:$PATH" \
    && npm install -g --prefix /opt/node24 openclaw@latest \
    && printf '#!/bin/sh\nexport PATH="/opt/node24/bin:$PATH"\nexec /opt/node24/bin/openclaw "$@"\n' \
       > /usr/local/bin/openclaw \
    && chmod +x /usr/local/bin/openclaw \
    && /usr/local/bin/openclaw --version

RUN useradd --create-home --home-dir /home/appuser appuser

# Hermes.
# Installed as the app user: its dependency state lives under ~/.hermes and
# must be owned by the user that runs it. Node 24 is only on PATH for the
# installer, without changing the system Node 18 used elsewhere.
RUN su appuser -s /bin/bash -c 'export HOME=/home/appuser PATH="/opt/node24/bin:$PATH" \
    && cd "$HOME" \
    && curl -fsSL https://hermes-agent.nousresearch.com/install.sh -o /tmp/hermes-install.sh \
    && bash /tmp/hermes-install.sh --skip-setup --skip-browser --skip-computer-use' \
    && ln -sf /home/appuser/.local/bin/hermes /usr/local/bin/hermes

# OpenCode
RUN curl -fsSL https://opencode.ai/install | bash \
    && mv "$HOME/.opencode" /opt/opencode \
    && chmod -R a+rX /opt/opencode \
    && ln -sf /opt/opencode/bin/opencode /usr/local/bin/opencode \
    && opencode --version

# Python dependencies
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ .

RUN chown -R appuser:appuser /app

RUN su appuser -s /bin/bash -c 'export HOME=/home/appuser \
    && T=$(mktemp -d) \
    && for d in installs source-checks; do ln -s "$HOME/.hermes/$d" "$T/$d"; done \
    && HERMES_RUNTIME_DIR="$(readlink -f "$HOME/.hermes/tools")" HERMES_HOME="$T" hermes --version \
    && openclaw --version'

ENV HOME=/home/appuser
USER appuser

EXPOSE 8420

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8420}"]