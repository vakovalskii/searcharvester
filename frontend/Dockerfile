# Multi-stage build: Node for install+build, tiny static server for runtime.

FROM node:22-alpine AS build
WORKDIR /app
COPY package.json ./
# npm install is fine for v1; lock file will be generated on first run and can
# be committed later for reproducibility.
RUN npm install --no-audit --no-fund
COPY tsconfig.json vite.config.ts tailwind.config.js postcss.config.js index.html ./
COPY src ./src
# Allow override of the backend URL baked into the bundle.
ARG VITE_API_URL=http://localhost:8000
ENV VITE_API_URL=${VITE_API_URL}
ARG VITE_ADMIN_URL=http://localhost:8011
ENV VITE_ADMIN_URL=${VITE_ADMIN_URL}
RUN npm run build

# ---------- Runtime: static files served by a thin node http-server ----------
FROM node:22-alpine AS runtime
WORKDIR /srv
RUN npm install -g serve@14 --no-audit --no-fund
COPY --from=build /app/dist ./dist
EXPOSE 9762
# -s = SPA-friendly 200-fallback to index.html (not critical for us but
# harmless); -l = port; --no-clipboard keeps serve quiet in docker.
CMD ["serve", "-s", "dist", "-l", "9762", "--no-clipboard"]
