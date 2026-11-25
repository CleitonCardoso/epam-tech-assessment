# ── build ─────────────────────────────────────────────────────────────────────
# Node 22 LTS to match the host toolchain; `npm ci` is lockfile-exact.
FROM node:22-alpine AS build

WORKDIR /app

# Dependencies first so Docker can cache the install layer independently of source edits.
# `.npmrc` pins `ignore-scripts=true` (see its comment): esbuild's platform binary arrives as
# an optional dependency, so no install script is needed on Linux or Windows.
COPY package.json package-lock.json .npmrc ./
RUN npm ci

COPY . .

# `build` runs `tsc --noEmit && vite build`, so a type error fails the image build.
RUN npm run build

# ── serve ─────────────────────────────────────────────────────────────────────
FROM nginx:alpine

# Same-origin proxy for /api/ and /health/ plus the SPA fallback.
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

# nginx:alpine's own CMD ["nginx", "-g", "daemon off;"] is kept.
