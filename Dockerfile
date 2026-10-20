FROM ghcr.io/astral-sh/uv:0.13.3 AS uv
FROM node:24-alpine AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend ./
RUN npm run build

FROM python:3.12-slim
COPY --from=uv /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1
ENV PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY config ./config
COPY coordinator ./coordinator
COPY --from=frontend /build/coordinator/static/coordinator/app ./coordinator/static/coordinator/app
COPY manage.py mcp_server.py ./
COPY scripts ./scripts
RUN python manage.py collectstatic --noinput \
    && useradd --uid 10001 --create-home coordinator \
    && chown -R coordinator:coordinator /app
USER coordinator
EXPOSE 8000
CMD ["uvicorn", "config.asgi:application", "--host", "0.0.0.0", "--port", "8000"]
