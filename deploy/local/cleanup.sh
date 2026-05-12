#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
NAMESPACE=rm-monitor
PV_NAME=rm-monitor-records

RUN=0
DELETE_DATA=0
DELETE_IMAGES=0
DELETE_SECRETS=0
UNINSTALL_BILIUP=0
UNINSTALL_K3S=0

usage() {
  cat <<'EOF'
Usage:
  deploy/local/cleanup.sh [options]

Default mode is dry-run. Add --confirm to execute.

Options:
  --confirm          Execute cleanup. Without this, only prints commands.
  --delete-data      Also delete record data under /mnt/PC801 and /mnt/server_data.
  --delete-images    Also delete local rm-monitor Docker images.
  --delete-secrets   Also delete local cookies/log files in this repo.
  --uninstall-biliup Also run "python3 -m pip uninstall -y biliup".
  --uninstall-k3s    Also uninstall k3s using /usr/local/bin/k3s-uninstall.sh.
  -h, --help         Show this help.

The normal cleanup removes Kubernetes resources, RM Monitor systemd hooks,
runtime ip rules, and temporary files. It keeps long-term recordings by default.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --confirm) RUN=1 ;;
    --delete-data) DELETE_DATA=1 ;;
    --delete-images) DELETE_IMAGES=1 ;;
    --delete-secrets) DELETE_SECRETS=1 ;;
    --uninstall-biliup) UNINSTALL_BILIUP=1 ;;
    --uninstall-k3s) UNINSTALL_K3S=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
  esac
  shift
done

say() {
  printf '%s\n' "$*"
}

run() {
  if [ "$RUN" -eq 1 ]; then
    "$@"
  else
    printf '+'
    for arg in "$@"; do
      printf ' %s' "$arg"
    done
    printf '\n'
  fi
}

run_sh() {
  if [ "$RUN" -eq 1 ]; then
    sh -c "$1"
  else
    printf '+ sh -c %s\n' "$1"
  fi
}

as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    run "$@"
  else
    run sudo -n "$@"
  fi
}

as_root_sh() {
  if [ "$(id -u)" -eq 0 ]; then
    run_sh "$1"
  else
    run sudo -n sh -c "$1"
  fi
}

have() {
  command -v "$1" >/dev/null 2>&1
}

say "== RM Monitor cleanup =="
if [ "$RUN" -eq 0 ]; then
  say "Dry-run only. Re-run with --confirm to execute."
fi

say
say "== Kubernetes resources =="
if have kubectl; then
  run kubectl delete -k "$SCRIPT_DIR" --ignore-not-found=true
  run kubectl delete namespace "$NAMESPACE" --ignore-not-found=true
  run kubectl delete pv "$PV_NAME" --ignore-not-found=true
else
  say "kubectl not found; skipping Kubernetes cleanup."
fi

say
say "== Systemd hooks and route rules =="
if have systemctl; then
  as_root systemctl disable --now rm-monitor-container-net-bypass.service
  as_root rm -f /etc/systemd/system/rm-monitor-container-net-bypass.service
  as_root rm -f /etc/systemd/system/clash-verge-service.service.d/rm-monitor-container-net-bypass.conf
  as_root rmdir --ignore-fail-on-non-empty /etc/systemd/system/clash-verge-service.service.d
  as_root rm -f /usr/local/sbin/rm-monitor-container-net-bypass
  as_root systemctl daemon-reload
else
  say "systemctl not found; skipping systemd cleanup."
fi

if have ip; then
  for cidr in 10.42.0.0/16 172.17.0.0/16 172.18.0.0/16 172.19.0.0/16; do
    as_root_sh "while ip rule show | grep -q 'from ${cidr} lookup main'; do ip rule del pref 8998 from ${cidr} lookup main || break; done"
  done
  as_root ip route flush cache
else
  say "ip command not found; skipping route rule cleanup."
fi

say
say "== Temporary files =="
run rm -rf /tmp/rm-audio-hls /tmp/rm_audioe2e_id /tmp/rm-monitor-local-render.yaml

if [ "$DELETE_IMAGES" -eq 1 ]; then
  say
  say "== Docker images =="
  if have docker; then
    run_sh "docker image ls --format '{{.Repository}}:{{.Tag}}' | grep -E '^(rm-monitor/|ghcr\\.io/scutrobotlab/rm-monitor/)' | xargs -r docker rmi"
  else
    say "docker not found; skipping image cleanup."
  fi
fi

if [ "$DELETE_SECRETS" -eq 1 ]; then
  say
  say "== Local secrets and logs =="
  run rm -f "$SCRIPT_DIR/../../cookies.json" "$SCRIPT_DIR/../../download.log" "$SCRIPT_DIR/../../ds_update.log"
fi

if [ "$UNINSTALL_BILIUP" -eq 1 ]; then
  say
  say "== biliup =="
  if have python3; then
    run python3 -m pip uninstall -y biliup
  else
    say "python3 not found; skipping biliup uninstall."
  fi
fi

if [ "$DELETE_DATA" -eq 1 ]; then
  say
  say "== Record data =="
  run rm -rf /mnt/PC801/rm-monitor/records /mnt/server_data/rm-monitor/records
  run rmdir --ignore-fail-on-non-empty /mnt/PC801/rm-monitor /mnt/server_data/rm-monitor
fi

if [ "$UNINSTALL_K3S" -eq 1 ]; then
  say
  say "== k3s =="
  if [ -x /usr/local/bin/k3s-uninstall.sh ]; then
    as_root /usr/local/bin/k3s-uninstall.sh
  else
    say "/usr/local/bin/k3s-uninstall.sh not found; skipping k3s uninstall."
  fi
fi

say
say "Cleanup plan finished."
