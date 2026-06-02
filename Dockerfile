FROM python:3.12-slim

ARG CODEX_CLI_VERSION=0.135.0

ENV HOME=/home/codex \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        git \
        make \
        nodejs \
        npm \
        openssh-client \
        procps \
        ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g "@openai/codex@${CODEX_CLI_VERSION}" \
    && npm cache clean --force

WORKDIR /app

COPY pyproject.toml README.md ./
COPY codex_telegram_bot ./codex_telegram_bot

RUN python -m pip install --no-cache-dir . \
    && useradd --create-home --home-dir /home/codex --shell /bin/bash codex \
    && mkdir -p /home/codex/.codex /home/codex/Projects /home/codex/MyProjects \
    && chown -R codex:codex /home/codex /app

USER codex

CMD ["python", "-m", "codex_telegram_bot"]
