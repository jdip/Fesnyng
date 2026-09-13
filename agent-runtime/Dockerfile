FROM node:24-bookworm-slim@sha256:2fe369e969550cde8e867afc3fe370b260140cab4a23d467074295b42163d553
WORKDIR /opt/fesnyng
COPY package.json package-lock.json ./
RUN apt-get update \
    && apt-get install --yes --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && npm ci --omit=dev \
    && mkdir -p /workspace /home/agent
COPY host-auth.mjs ./host-auth.mjs
ENV HOME=/home/agent
ENV XDG_DATA_HOME=/home/agent/.local/share
ENV XDG_CONFIG_HOME=/home/agent/.config
ENV XDG_CACHE_HOME=/home/agent/.cache
WORKDIR /workspace
ENTRYPOINT ["/opt/fesnyng/node_modules/.bin/opencode", "serve", "--hostname", "0.0.0.0", "--port", "4096"]
