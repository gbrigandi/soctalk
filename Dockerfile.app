# Canonical SocTalk app UI (`frontend/`).
#
# SvelteKit + adapter-node, multi-stage build matching the existing
# Dockerfile.ui pattern but rooted at frontend/ instead of
# frontend/<sub-app>. Listens on $PORT (default 3000) so the chart's
# existing UI container template applies unchanged once the image is
# repointed.
#
#   docker build -f Dockerfile.app -t ghcr.io/soctalk/soctalk-app-ui:0.1.0 .

# ---- Stage 1: build ---------------------------------------------------------
FROM node:22-alpine AS builder

WORKDIR /app
ENV NODE_TLS_REJECT_UNAUTHORIZED=0
RUN corepack enable && corepack prepare pnpm@9.15.0 --activate

# Force pnpm to trust your corporate network certificates:
RUN pnpm config set strict-ssl false

COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY frontend/ ./
RUN pnpm build

# ---- Stage 2: runtime -------------------------------------------------------
FROM node:22-alpine

WORKDIR /app

COPY --from=builder /app/build ./build
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/package.json ./package.json

ENV PORT=3000 \
    NODE_ENV=production \
    HOST=0.0.0.0

EXPOSE 3000
USER 10001
CMD ["node", "build"]
