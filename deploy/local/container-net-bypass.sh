#!/bin/sh
set -eu

# Keep container-originated traffic on the normal LAN route instead of the
# host's Mihomo fake-ip policy table. This is intentionally source based:
# RM Monitor runs in k3s pods, and local Docker bridges may be used for tools.
CONTAINER_CIDRS="
10.42.0.0/16
172.17.0.0/16
172.18.0.0/16
172.19.0.0/16
"

for cidr in $CONTAINER_CIDRS; do
  if ! ip rule show | grep -q "from ${cidr} lookup main"; then
    ip rule add pref 8998 from "$cidr" lookup main
  fi
done

ip route flush cache 2>/dev/null || true
