# Root filesystem for Firecracker session VMs.
# Built as an image, then exported into an ext4 file by scripts/build.sh.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

# Base tooling the agents use, plus tini as PID-1 helper.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv git curl ca-certificates iproute2 iputils-ping \
        build-essential tini procps less nano unzip \
    && rm -rf /var/lib/apt/lists/*

# Headless Chromium from Playwright's build, with its system dependencies.
RUN python3 -m venv /opt/pw \
    && /opt/pw/bin/pip install --no-cache-dir playwright==1.55.0 \
    && /opt/pw/bin/playwright install --with-deps chromium \
    && ln -sf "$(ls -d /opt/ms-playwright/chromium-*/chrome-linux/chrome | head -1)" /usr/local/bin/fc-chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache

COPY guest/fc_guest_agent.py /opt/fc/fc_guest_agent.py
COPY guest/fc-init /sbin/fc-init
RUN chmod 0755 /sbin/fc-init && mkdir -p /workspace /var/lib/fc-browser \
    && python3 -c "import ast,sys; ast.parse(open('/opt/fc/fc_guest_agent.py').read())"
