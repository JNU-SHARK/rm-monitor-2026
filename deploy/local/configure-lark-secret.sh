#!/usr/bin/env bash
set -euo pipefail

namespace="${NAMESPACE:-rm-monitor}"

read -r -p "Feishu App ID: " app_id
read -r -s -p "Feishu App Secret: " app_secret
printf '\n'
read -r -p "Bitable App Token: " bitable_app_token

kubectl -n "${namespace}" create secret generic rm-monitor-lark \
  --from-literal=app-id="${app_id}" \
  --from-literal=app-secret="${app_secret}" \
  --from-literal=bitable-app-token="${bitable_app_token}" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n "${namespace}" rollout restart deploy/lark-notifier deploy/uploader-dispatcher
kubectl -n "${namespace}" rollout status deploy/lark-notifier --timeout=120s
kubectl -n "${namespace}" rollout status deploy/uploader-dispatcher --timeout=120s
