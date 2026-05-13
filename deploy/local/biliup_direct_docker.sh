#!/usr/bin/env bash
set -euo pipefail

image="${BILIUP_DIRECT_IMAGE:-nvidia/cuda:12.4.0-base-ubuntu22.04}"
repo_root="${BILIUP_DIRECT_REPO_ROOT:-$(pwd)}"

exec docker run --rm \
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
  /home/maverick/.local/bin/biliup "$@"
