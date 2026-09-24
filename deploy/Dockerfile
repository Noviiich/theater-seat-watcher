FROM ghcr.io/astral-sh/uv:0.8.22 AS uv

FROM python:3.12-slim AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim AS runtime
ENV PATH="/app/.venv/bin:$PATH" \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY --from=builder /app/.venv ./.venv
RUN playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright
RUN groupadd --gid 10001 theater-tickets \
    && useradd --uid 10001 --gid 10001 --create-home theater-tickets
COPY alembic.ini ./
COPY migrations ./migrations
COPY config ./config
RUN mkdir -p /app/data /app/backups \
    && chown -R theater-tickets:theater-tickets /app/data /app/backups
USER theater-tickets
VOLUME ["/app/data", "/app/backups"]
ENTRYPOINT ["theater-tickets"]
CMD ["run"]
