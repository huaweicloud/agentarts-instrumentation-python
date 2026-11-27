# syntax=docker/dockerfile:1
# 纯净 jiuwenswarm standalone 宿主：不含探针、凭据与持久卷，供 AgentArts 探针回归。
# 构建上下文固定为 openjiuwen/docker；宿主源码通过 git clone 获取，不依赖本机 checkout。

ARG PYTHON_IMAGE=python:3.11-slim-bookworm
ARG NODE_IMAGE=node:20-alpine
ARG JIUWENSWARM_REPO=https://gitcode.com/openJiuwen/jiuwenswarm.git
# 钉定 dev-stable commit（2026-11-20 !10703）；升级时改这里。
ARG JIUWENSWARM_REF=da61667dc49669268bbb5ec7991c808c6c0d27e3

# ---------- Stage 1: 源码 ----------
FROM ${PYTHON_IMAGE} AS source
ARG JIUWENSWARM_REPO
ARG JIUWENSWARM_REF
ARG DEBIAN_MIRROR=mirrors.aliyun.com
RUN sed -i "s|deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources 2>/dev/null \
    || sed -i "s|deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --filter=blob:none --no-checkout "${JIUWENSWARM_REPO}" /src \
    && git -C /src checkout "${JIUWENSWARM_REF}" \
    && test "$(git -C /src rev-parse HEAD)" = "${JIUWENSWARM_REF}" \
    && git -C /src log -1 --oneline \
    && rm -rf /src/.git

# ---------- Stage 2: 前端 dist（仓库不提交 dist） ----------
FROM ${NODE_IMAGE} AS web
ARG NPM_REGISTRY=https://registry.npmmirror.com
ENV NPM_CONFIG_REGISTRY=${NPM_REGISTRY}
COPY --from=source /src /src
RUN cd /src/jiuwenswarm/channels/web/frontend \
    && npm install \
    && npm run build

# ---------- Stage 3: 运行时 ----------
FROM ${PYTHON_IMAGE}
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG DEBIAN_MIRROR=mirrors.aliyun.com
ARG JIUWENSWARM_REF

RUN sed -i "s|deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources 2>/dev/null \
    || sed -i "s|deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# WEB_HOST / FRONTEND_HOST 在运行时 ENV 中设置，避免改动此处导致依赖层缓存失效。
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    UV_DEFAULT_INDEX=${PIP_INDEX_URL} \
    UV_HTTP_TIMEOUT=120 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1 \
    JIUWENSWARM_AGENT_SERVER_PORT=18092 \
    JIUWENSWARM_WEB_PORT=19000 \
    JIUWENSWARM_GATEWAY_PORT=19001 \
    JIUWENSWARM_FRONTEND_PORT=5173 \
    JIUWENSWARM_HTTP_API_PORT=8766

WORKDIR /app/jiuwenswarm
COPY --from=source /src /app/jiuwenswarm
COPY --from=web /src/jiuwenswarm/channels/web/frontend/dist /app/jiuwenswarm/jiuwenswarm/channels/web/frontend/dist

# 依赖按仓库自带 uv.lock 安装：pyproject 里 openjiuwen / agent-shield 指向多个 git URL，
# pip 与 uv pip 都会报 URL 冲突，只有 --frozen 读锁能给出唯一解。
RUN pip install --no-cache-dir "uv>=0.9,<1.0"
RUN uv venv --seed /opt/venv --python /usr/local/bin/python3
RUN cd /app/jiuwenswarm && uv sync --frozen --no-dev --no-cache

# 构建期固化 ~/.jiuwenswarm（非 TTY 自动默认 zh）；不含任何凭据。
RUN jiuwenswarm-init

LABEL org.opencontainers.image.description="Clean jiuwenswarm standalone host for AgentArts probe testing"
LABEL agentarts.jiuwenswarm.ref=${JIUWENSWARM_REF}

COPY entrypoint.sh /usr/local/bin/jiuwenswarm-entrypoint
RUN chmod +x /usr/local/bin/jiuwenswarm-entrypoint

# 宿主默认只绑 127.0.0.1；容器内必须绑 0.0.0.0，否则 -p 映射访问不到。
ENV WEB_HOST=0.0.0.0 \
    FRONTEND_HOST=0.0.0.0

# 5173 = Web UI，19002 = Gateway Web HTTP (/api/v1)
EXPOSE 5173 19002

ENTRYPOINT ["jiuwenswarm-entrypoint"]
CMD ["jiuwenswarm-start"]
