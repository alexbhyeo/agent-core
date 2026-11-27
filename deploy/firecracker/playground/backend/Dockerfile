# Firecracker Playground backend (build context: repository root).
# Runs agent-core (DeepAgent / AgentTeam) and serves only the HTTP API /api/v1 for the
# separately hosted frontend. Deploy it next to FcSessionManager (docker network fc-net).
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/opt/agent-core:/opt/playground \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.9.5

WORKDIR /deps
COPY pyproject.toml uv.lock ./
# observability: importing the harness pulls in the OTLP exporter; sqlite: AgentTeam storage.
RUN uv sync --frozen --no-install-project --no-dev --extra sandbox --extra observability --extra sqlite

COPY openjiuwen /opt/agent-core/openjiuwen
COPY deploy/firecracker/playground/backend/fc_playground /opt/playground/fc_playground
RUN python -m compileall -q /opt/playground && mkdir -p /var/lib/fc-playground

WORKDIR /var/lib/fc-playground
EXPOSE 8800
HEALTHCHECK --interval=15s --timeout=5s CMD curl -fsS http://127.0.0.1:8800/healthz || exit 1
CMD ["python", "-m", "fc_playground"]
