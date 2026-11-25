#!/bin/bash
# Turn an exported container filesystem (tar on stdin) into an ext4 rootfs.
# Usage: docker export <cid> | docker run -i --rm -v <out>:/out fc-manager:dev fc-build-rootfs /out/rootfs.ext4
set -euo pipefail
OUT=${1:?output path}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
tar -C "$WORK" -xf -
rm -f "$WORK/.dockerenv"
USED_MB=$(du -sm "$WORK" | cut -f1)
SIZE_MB=$(( USED_MB + 3072 ))   # free space for pip installs, clones and the browser profile
truncate -s "${SIZE_MB}M" "$OUT.tmp"
mkfs.ext4 -q -F -d "$WORK" -L fcroot "$OUT.tmp"
e2fsck -fn "$OUT.tmp" >/dev/null
mv "$OUT.tmp" "$OUT"
chmod 0644 "$OUT"
echo "rootfs: $OUT (${SIZE_MB} MiB, ${USED_MB} MiB used)"
