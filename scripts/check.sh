#!/usr/bin/env bash
# Runs every quality gate. Integration tests run only when TEST_DATABASE_URL is set.
set -euo pipefail
cd "$(dirname "$0")/.."

uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q
