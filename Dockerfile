# Two images from one file (#50):
#   docker build --target web -t agents-task-manager-web .   SPA + Caddy front door
#   docker build -t agents-task-manager .                     Django API (default)

FROM node:24-alpine AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend ./
RUN npm run build

FROM caddy:2.10-alpine AS web
COPY deploy/web/Caddyfile /etc/caddy/Caddyfile
COPY --from=frontend /build/frontend/dist /srv
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --retries=12 \
    CMD wget -q -O /dev/null http://127.0.0.1:8080/ || exit 1

FROM python:3.12-slim AS api
RUN pip install --no-cache-dir "uv==0.13.3"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1
ENV PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY config ./config
COPY accounts ./accounts
COPY coordinator ./coordinator
COPY manage.py mcp_server.py ./
COPY scripts ./scripts
# Collects admin and DRF assets only; the SPA lives in the web image.
RUN python manage.py collectstatic --noinput \
    && useradd --uid 10001 --create-home coordinator \
    && chown -R coordinator:coordinator /app
USER coordinator
EXPOSE 8000
CMD ["uvicorn", "config.asgi:application", "--host", "0.0.0.0", "--port", "8000"]
