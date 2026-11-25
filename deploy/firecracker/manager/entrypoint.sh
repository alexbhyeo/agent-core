#!/bin/bash
# Container entrypoint for FcSessionManager (run with --privileged and /dev/kvm).
set -euo pipefail

[ -c /dev/kvm ] || { echo "/dev/kvm missing: run on a Linux host with KVM and pass --device /dev/kvm" >&2; exit 1; }

# cgroup v2 "no internal processes": move everything into a leaf group so the
# root can delegate controllers to the jailer's per-VM groups under /fc.
CG=/sys/fs/cgroup
if [ -f "$CG/cgroup.controllers" ]; then
  mkdir -p "$CG/init"
  while read -r pid; do echo "$pid" > "$CG/init/cgroup.procs" 2>/dev/null || true; done < "$CG/cgroup.procs"
  echo "+cpu +memory +pids" > "$CG/cgroup.subtree_control"
  mkdir -p "$CG/fc"
  echo "+cpu +memory +pids" > "$CG/fc/cgroup.subtree_control"
fi

mkdir -p /var/run/netns
[ -f "${FCM_ROOTFS:-/var/lib/fc-images/rootfs.ext4}" ] \
  || { echo "rootfs image missing; build it with deploy/firecracker/scripts/build.sh" >&2; exit 1; }

cd /opt/fc/manager
exec python3 -m fc_session_manager
