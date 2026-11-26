# FcSessionManager image: Firecracker + jailer + the manager service.
# Run with --privileged and --device /dev/kvm (see scripts/run_manager.sh).
FROM ubuntu:24.04

ARG FIRECRACKER_VERSION=v1.18.0
ARG KERNEL_URL=https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/20261118-6d7cfa9c6b53-0/x86_64/vmlinux-6.1.188
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-aiohttp iproute2 iptables curl ca-certificates e2fsprogs procps \
        conntrack zstd openssl util-linux \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; arch="$(uname -m)"; \
    curl -fsSL "https://github.com/firecracker-microvm/firecracker/releases/download/${FIRECRACKER_VERSION}/firecracker-${FIRECRACKER_VERSION}-${arch}.tgz" \
      | tar -xz -C /tmp; \
    install -m 0755 "/tmp/release-${FIRECRACKER_VERSION}-${arch}/firecracker-${FIRECRACKER_VERSION}-${arch}" /usr/local/bin/firecracker; \
    install -m 0755 "/tmp/release-${FIRECRACKER_VERSION}-${arch}/jailer-${FIRECRACKER_VERSION}-${arch}" /usr/local/bin/jailer; \
    install -m 0755 "/tmp/release-${FIRECRACKER_VERSION}-${arch}/snapshot-editor-${FIRECRACKER_VERSION}-${arch}" /usr/local/bin/snapshot-editor; \
    mkdir -p /opt/fc/cpu-templates; \
    for t in T2 T2CL T2S C3; do \
      [ -f "/tmp/release-${FIRECRACKER_VERSION}-${arch}/${t}-${FIRECRACKER_VERSION}.json" ] \
        && cp "/tmp/release-${FIRECRACKER_VERSION}-${arch}/${t}-${FIRECRACKER_VERSION}.json" "/opt/fc/cpu-templates/${t}.json"; \
    done; \
    rm -rf /tmp/release-*; \
    mkdir -p /opt/fc; curl -fsSL -o /opt/fc/vmlinux "${KERNEL_URL}"; chmod 0644 /opt/fc/vmlinux; \
    firecracker --version

COPY manager/fc_session_manager /opt/fc/manager/fc_session_manager
COPY manager/entrypoint.sh /usr/local/bin/fc-entrypoint
COPY manager/build_rootfs.sh /usr/local/bin/fc-build-rootfs
RUN chmod 0755 /usr/local/bin/fc-entrypoint /usr/local/bin/fc-build-rootfs \
    && python3 -m compileall -q /opt/fc/manager

EXPOSE 8700
ENTRYPOINT ["/usr/local/bin/fc-entrypoint"]
