FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

RUN pip install --no-cache-dir "uv==0.11.32" \
    && useradd --create-home --uid 10001 desk

WORKDIR /app

# Dependencies first, for layer caching.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY migrations ./migrations
COPY backend ./backend
RUN uv sync --frozen --no-dev && chown -R desk:desk /app

USER desk
ENV PATH="/app/.venv/bin:${PATH}"

EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && exec python -m backend"]
