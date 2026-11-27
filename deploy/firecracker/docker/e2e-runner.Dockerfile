# Test runner for the Firecracker end-to-end tests (build context: repository root).
# agent-core's locked dependencies live in /opt/venv; the source tree is mounted at /src.
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates git nodejs npm \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.9.5

WORKDIR /deps
COPY pyproject.toml uv.lock ./
# 'observability' is needed because importing the harness pulls in
# agent_evolving -> extensions.observability (OTLP exporter).
RUN uv sync --frozen --no-install-project --extra sandbox --extra observability --group test \
    && uv pip install --python /opt/venv/bin/python "playwright==1.55.0"

# Warm npx's cache with the Playwright MCP version the browser subagent pins.
RUN npx -y @playwright/mcp@0.0.78 --help >/dev/null 2>&1 || true

# A local headless Chromium for the Playground UI tests (the browser opens the frontend like a user).
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN /opt/venv/bin/python -m playwright install --with-deps chromium && rm -rf /var/lib/apt/lists/*

WORKDIR /src
ENV PYTHONPATH=/src
