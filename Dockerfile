# syntax=docker/dockerfile:1
# Two production targets from one build (design §13.2):
#   --target backend  Django: one image for the api (uvicorn), the worker and migrate. It sits on
#                     the Playwright image, whose Chromium matches playwright==1.63.0, because the
#                     worker renders PDFs; the api carries that Chromium unused.
#   --target web      the Next.js standalone server. It is the frontend only: /api/* is rewritten
#                     to Django, and the rewrite target is frozen into the build (DJANGO_ORIGIN).

ARG NODE_VERSION=24

# ---------- backend: api, worker, migrate ----------
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble AS backend
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/
# UV_PYTHON_INSTALL_DIR: otherwise root installs Python under /root (mode 700), and the venv's
# python symlink is unreadable once the image switches to USER foray. The image's own Python is
# 3.12; the project needs 3.14, which uv installs.
# UV_COMPILE_BYTECODE: foray cannot write __pycache__ into the root-owned venv, so compile at build.
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 PYTHONUNBUFFERED=1 PYDANTIC_AI_NO_BANNER=1 \
    UV_PYTHON_INSTALL_DIR=/opt/uv/python
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv python install 3.14 && uv sync --frozen --no-dev --no-install-project
COPY backend/ ./
# uid 1001 is the base image's pwuser, so foray takes 1002. Its home holds Chromium's and
# fontconfig's caches.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev && .venv/bin/python -m compileall -q foray apps \
    && useradd --uid 1002 --create-home foray && mkdir -p /data && chown foray /data
USER foray
ENV PATH=/app/backend/.venv/bin:$PATH STORAGE_DIR=/data

# ---------- web: shared dependency layer ----------
FROM node:${NODE_VERSION}-bookworm-slim AS base
ENV PNPM_HOME=/pnpm PATH=/pnpm:$PATH NEXT_TELEMETRY_DISABLED=1
RUN corepack enable && corepack prepare pnpm@10 --activate
WORKDIR /app

FROM base AS deps
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN --mount=type=cache,id=pnpm,target=/pnpm/store pnpm install --frozen-lockfile

# ---------- web: build the Next app ----------
FROM deps AS build
# Rewrites are frozen at build: /api/* goes to this origin in the built routes manifest.
ARG DJANGO_ORIGIN=http://api:8000
COPY . .
RUN pnpm build

# ---------- web ----------
FROM node:${NODE_VERSION}-bookworm-slim AS web
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
WORKDIR /app
RUN useradd --system --uid 1001 --create-home foray
COPY --from=build --chown=foray:foray /app/.next/standalone ./
COPY --from=build --chown=foray:foray /app/.next/static ./.next/static
COPY --from=build --chown=foray:foray /app/public ./public
USER foray
EXPOSE 3000
CMD ["node", "server.js"]
