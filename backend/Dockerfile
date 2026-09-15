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

# Private Node 22 for OpenClaw and Hermes.
# This does NOT replace the system Node 18.
RUN curl -fsSL https://nodejs.org/dist/v22.23.2/node-v22.23.2-linux-x64.tar.xz \
    | tar -xJ -C /opt \
    && mv /opt/node-v22.23.2-linux-x64 /opt/node22

RUN export PATH="/opt/node22/bin:$PATH" \
    && npm install -g --prefix /opt/node22 openclaw@latest \
    && printf '#!/bin/sh\nexport PATH="/opt/node22/bin:$PATH"\nexec /opt/node22/bin/openclaw "$@"\n' \
       > /usr/local/bin/openclaw \
    && chmod +x /usr/local/bin/openclaw \
    && /usr/local/bin/openclaw --version

# Hermes.
# Give Hermes Node 22 for its npm dependency installation,
# without changing the system Node 18 used elsewhere.
RUN curl -fsSL https://hermes-agent.nousresearch.com/install.sh \
    -o /tmp/hermes-install.sh \
    && chmod +x /tmp/hermes-install.sh \
    && PATH="/opt/node22/bin:$PATH" \
       bash /tmp/hermes-install.sh \
       --skip-setup \
       --skip-browser \
       --skip-computer-use \
    && (command -v hermes || { \
        mv "$HOME/.local" /opt/hermes-local \
        && chmod -R a+rX /opt/hermes-local \
        && ln -sf /opt/hermes-local/bin/hermes /usr/local/bin/hermes; \
    }) \
    && hermes --version

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

RUN useradd --create-home --home-dir /home/appuser appuser \
    && chown -R appuser:appuser /app

ENV HOME=/home/appuser
USER appuser

EXPOSE 8420

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8420}"]