# syntax=docker/dockerfile:1
# XMPP bot gateway image. Config at /app/config.toml (mount read-only), state in /app/data (volume).
#   docker run -d -v ./config.toml:/app/config.toml:ro -v ./data:/app/data ghcr.io/brokenstring314/xmpp-bot-gateway

FROM python:3.12-slim AS build
# EXTRAS="" builds an image without the optional OMEMO dependencies (AGPL-3.0)
ARG EXTRAS=omemo
RUN apt-get update && apt-get install -y --no-install-recommends build-essential curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# slixmpp ships no prebuilt wheel for some architectures (e.g. arm64); its Rust part then needs a toolchain
RUN if [ "$(uname -m)" != "x86_64" ]; then \
      curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal; fi
ENV PATH=/root/.cargo/bin:$PATH
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip wheel --no-cache-dir --wheel-dir /wheels ".${EXTRAS:+[$EXTRAS]}"

FROM python:3.12-slim
ARG EXTRAS=omemo
LABEL org.opencontainers.image.source="https://github.com/brokenstring314/xmpp-bot-gateway" \
      org.opencontainers.image.description="XMPP bot gateway: direct and group chats over OneBot v11" \
      org.opencontainers.image.licenses="Apache-2.0"
RUN --mount=from=build,source=/wheels,target=/wheels \
    pip install --no-cache-dir --no-index --find-links /wheels "xmpp-bot-gateway${EXTRAS:+[$EXTRAS]}"
RUN useradd --system --uid 10001 --home-dir /app gateway && mkdir -p /app/data && chown gateway /app/data
WORKDIR /app
USER gateway
ENV PYTHONUNBUFFERED=1
VOLUME /app/data
ENTRYPOINT ["xmpp-gateway"]
CMD ["-c", "/app/config.toml"]
