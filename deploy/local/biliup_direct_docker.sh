#!/usr/bin/env bash
set -euo pipefail

image="${BILIUP_DIRECT_IMAGE:-nvidia/cuda:12.4.0-base-ubuntu22.04}"
repo_root="${BILIUP_DIRECT_REPO_ROOT:-$(pwd)}"
cid_dir="$(mktemp -d /tmp/rm-monitor-biliup-direct.XXXXXX)"
cidfile="$cid_dir/container.cid"
docker_pid=""

stop_container() {
  if [[ -s "$cidfile" ]]; then
    container_id="$(<"$cidfile")"
    docker stop --timeout 10 "$container_id" >/dev/null 2>&1 || true
  fi
  if [[ -n "$docker_pid" ]]; then
    kill -TERM "$docker_pid" >/dev/null 2>&1 || true
  fi
}

cleanup() {
  rm -f "$cidfile"
  rmdir "$cid_dir" >/dev/null 2>&1 || true
}

trap stop_container TERM INT HUP
trap cleanup EXIT

docker run --rm \
  --cidfile "$cidfile" \
  --network bridge \
  --dns "${BILIUP_DIRECT_DNS1:-223.5.5.5}" \
  --dns "${BILIUP_DIRECT_DNS2:-114.114.114.114}" \
  --user "$(id -u):$(id -g)" \
  -e HOME=/home/maverick \
  -e TZ=Asia/Shanghai \
  -v /home/maverick:/home/maverick:rw \
  -v /mnt:/mnt:rw \
  -w "$repo_root" \
  "$image" \
  /home/maverick/.local/bin/biliup "$@" &
docker_pid="$!"

if wait "$docker_pid"; then
  status=0
else
  status=$?
fi
docker_pid=""
exit "$status"
