#!/usr/bin/env bash
# Restrict the egress of an agent-core container to its LLM endpoints and FcSessionManager.
#
# Run inside the agent-core container (needs CAP_NET_ADMIN), e.g. as the entrypoint's first step:
#   agent_core_egress.sh api.deepseek.com [other-llm-host[:port] ...]
#
#   FCM_MANAGER_HOST  manager host name (default: fc-manager); all its TCP ports are allowed
#                     (REST API and the per-session proxy ports)
#
# Everything else is rejected. Host names are resolved once, when the script runs: re-run it
# (e.g. from cron) if an LLM endpoint changes addresses, or put an HTTPS proxy in front instead.
set -euo pipefail
[ "$#" -ge 1 ] || { echo "usage: $0 <llm-host>[:port] ..." >&2; exit 2; }
MANAGER_HOST="${FCM_MANAGER_HOST:-fc-manager}"
CHAIN=AGENT-EGRESS
ipt() { iptables -w "$@"; }

resolve() { getent ahostsv4 "$1" | awk '{print $1}' | sort -u; }

ipt -N "$CHAIN" 2>/dev/null || ipt -F "$CHAIN"
ipt -A "$CHAIN" -o lo -j ACCEPT
ipt -A "$CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
for ns in $(awk '/^nameserver/ {print $2}' /etc/resolv.conf); do
  ipt -A "$CHAIN" -d "$ns" -p udp --dport 53 -j ACCEPT
  ipt -A "$CHAIN" -d "$ns" -p tcp --dport 53 -j ACCEPT
done
manager_ips=$(resolve "$MANAGER_HOST")
[ -n "$manager_ips" ] || { echo "cannot resolve $MANAGER_HOST" >&2; exit 1; }
for ip in $manager_ips; do
  ipt -A "$CHAIN" -d "$ip" -p tcp -j ACCEPT
done
for spec in "$@"; do
  host="${spec%%:*}"
  port="${spec##*:}"
  [ "$port" = "$spec" ] && port=443
  ips=$(resolve "$host")
  [ -n "$ips" ] || { echo "cannot resolve $host" >&2; exit 1; }
  for ip in $ips; do
    ipt -A "$CHAIN" -d "$ip" -p tcp --dport "$port" -j ACCEPT
  done
  echo "allowed $host:$port -> $(echo "$ips" | tr '\n' ' ')"
done
ipt -A "$CHAIN" -j REJECT --reject-with icmp-port-unreachable
ipt -C OUTPUT -j "$CHAIN" 2>/dev/null || ipt -I OUTPUT 1 -j "$CHAIN"
echo "agent-core egress limited to: $MANAGER_HOST and $*"
