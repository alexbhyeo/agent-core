# Root filesystem for Firecracker session VMs.
# Built as an image, then exported into an ext4 file by scripts/build.sh.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

# Base tooling the agents use, plus tini as PID-1 helper.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv git curl ca-certificates iproute2 iputils-ping \
        build-essential tini procps psmisc less nano unzip \
    && rm -rf /var/lib/apt/lists/*

# Headless Chromium from Playwright's build, with its system dependencies.
RUN python3 -m venv /opt/pw \
    && /opt/pw/bin/pip install --no-cache-dir playwright==1.55.0 \
    && /opt/pw/bin/playwright install --with-deps chromium \
    && ln -sf "$(ls -d /opt/ms-playwright/chromium-*/chrome-linux/chrome | head -1)" /usr/local/bin/fc-chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache

# Runtime for user-defined MCP servers: mcp-proxy serves stdio servers as
# streamable HTTP inside the VM (the guest agent bridges it to vsock).
RUN python3 -m venv /opt/mcp \
    && /opt/mcp/bin/pip install --no-cache-dir mcp-proxy==0.15.0 \
    && ln -sf /opt/mcp/bin/mcp-proxy /usr/local/bin/mcp-proxy

COPY guest/fc_guest_agent.py /opt/fc/fc_guest_agent.py
COPY guest/demo_mcp_server.py /opt/fc/demo_mcp_server.py
COPY guest/fc-init /sbin/fc-init
COPY guest/fc-msg /usr/local/bin/fc-msg
# fc-notify / fc-ask: tasks in the VM message the session's user (see guest/fc-msg).
RUN chmod 0755 /usr/local/bin/fc-msg \
    && ln -sf fc-msg /usr/local/bin/fc-notify && ln -sf fc-msg /usr/local/bin/fc-ask
RUN chmod 0755 /sbin/fc-init && mkdir -p /workspace /var/lib/fc-browser /var/lib/fc-mcp \
    && python3 -c "import ast,sys; ast.parse(open('/opt/fc/fc_guest_agent.py').read())"
