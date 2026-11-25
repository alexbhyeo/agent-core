#!/usr/bin/env bash
# Build the guest rootfs and the FcSessionManager image.
#   FC_ARTIFACTS  where rootfs.ext4 is written (default: ~/.cache/fc-artifacts)
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
ART="${FC_ARTIFACTS:-$HOME/.cache/fc-artifacts}"
mkdir -p "$ART"

echo "==> building guest image (fc-guest:dev)"
docker build -f "$HERE/docker/guest.Dockerfile" -t fc-guest:dev "$HERE"

echo "==> building manager image (fc-manager:dev)"
docker build -f "$HERE/docker/manager.Dockerfile" -t fc-manager:dev "$HERE"

echo "==> exporting guest filesystem to $ART/rootfs.ext4"
cid=$(docker create fc-guest:dev /bin/true)
trap 'docker rm -f "$cid" >/dev/null 2>&1 || true' EXIT
docker export "$cid" \
  | docker run -i --rm --entrypoint /usr/local/bin/fc-build-rootfs -v "$ART:/out" fc-manager:dev /out/rootfs.ext4
