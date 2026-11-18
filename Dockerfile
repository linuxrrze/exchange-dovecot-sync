# syntax=docker/dockerfile:1

# --- build stage: resolve the locked dependencies into a venv ---------------------------
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.15 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /src

# Dependencies first, so this layer is cached as long as uv.lock does not change.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# --- runtime stage ----------------------------------------------------------------------
FROM python:3.12-slim

RUN useradd --create-home --uid 1000 exsync \
 && mkdir -p /data/state \
 && chown -R exsync:exsync /data

COPY --from=build /app/.venv /app/.venv

# /data is the working directory: put config.toml there (the default config path) and the
# state files land in /data/state/exsync/ — mount a volume to keep them between runs.
ENV PATH=/app/.venv/bin:$PATH \
    XDG_STATE_HOME=/data/state \
    PYTHONUNBUFFERED=1

WORKDIR /data
VOLUME /data
USER exsync

ENTRYPOINT ["exsync"]
CMD ["--help"]
