# syntax=docker/dockerfile:1
# Two production targets from one build:
#   --target web     Next.js standalone server
#   --target worker  tsx-run background worker on the Playwright image (headless Chromium)

ARG NODE_VERSION=24
ARG PLAYWRIGHT_VERSION=1.63.0

# ---------- shared dependency layer ----------
FROM node:${NODE_VERSION}-bookworm-slim AS base
ENV PNPM_HOME=/pnpm PATH=/pnpm:$PATH NEXT_TELEMETRY_DISABLED=1
RUN corepack enable && corepack prepare pnpm@10 --activate
WORKDIR /app

FROM base AS deps
COPY package.json pnpm-lock.yaml ./
RUN --mount=type=cache,id=pnpm,target=/pnpm/store pnpm install --frozen-lockfile

# ---------- build the Next app ----------
FROM deps AS build
COPY . .
# env.ts validation is skipped at build time; the running container validates for real.
ENV SKIP_ENV_VALIDATION=1
RUN pnpm build

# ---------- web ----------
FROM node:${NODE_VERSION}-bookworm-slim AS web
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
WORKDIR /app
RUN useradd --system --uid 1001 --create-home foray
COPY --from=build --chown=foray:foray /app/.next/standalone ./
COPY --from=build --chown=foray:foray /app/.next/static ./.next/static
COPY --from=build --chown=foray:foray /app/public ./public
RUN mkdir -p /app/data && chown foray:foray /app/data
USER foray
EXPOSE 3000
CMD ["node", "server.js"]

# ---------- worker ----------
FROM mcr.microsoft.com/playwright:v${PLAYWRIGHT_VERSION}-noble AS worker
ENV NODE_ENV=production PNPM_HOME=/pnpm PATH=/pnpm:$PATH
RUN corepack enable && corepack prepare pnpm@10 --activate
WORKDIR /app
COPY package.json pnpm-lock.yaml ./
RUN --mount=type=cache,id=pnpm,target=/pnpm/store pnpm install --frozen-lockfile
COPY . .
RUN mkdir -p /app/data
CMD ["node_modules/.bin/tsx", "src/worker/main.ts"]
