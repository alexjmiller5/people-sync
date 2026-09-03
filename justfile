set shell := ["bash", "-cu"]

default:
    @just --list

test:
    uv run pytest

# All static analysis (read-only, CI-safe)
check:
    uv run ruff check . && uv run ruff format --check .

fmt:
    uv run ruff format . && uv run ruff check --fix .

# --- project-specific recipes below (one-offs live in scripts/, run directly) ---
