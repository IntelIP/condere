# Condere API and per-assessment job image for Cloud Run (docs/production.md).
# Build from the repository root: the runtime reads src/, agent/ and infra/smolvm/ by path.
# The same image serves both: the API (default command) and the job, whose command is
# `python -m condere.jobs.run_assessment`. Code analysis runs in Modal, never in this container.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY agent ./agent
COPY infra/smolvm ./infra/smolvm
RUN uv sync --frozen --no-dev \
    && useradd --system --home /app condere \
    && mkdir -p /tmp/condere-artifacts \
    && chown condere /tmp/condere-artifacts
ENV PATH="/app/.venv/bin:${PATH}" CONDERE_ARTIFACT_DIRECTORY=/tmp/condere-artifacts
USER condere
CMD ["python", "-m", "condere.interfaces.server"]
