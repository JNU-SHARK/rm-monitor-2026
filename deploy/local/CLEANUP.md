# RM Monitor Cleanup

This file documents the helper cleanup script. The authoritative checklist is
the root [README.md](../../README.md), because future operational changes may
not be covered by a script.

```sh
deploy/local/cleanup.sh
```

The script is dry-run by default. To execute the normal cleanup:

```sh
deploy/local/cleanup.sh --confirm
```

Normal cleanup removes:

- Kubernetes resources in the `rm-monitor` namespace.
- The `rm-monitor-records` persistent volume object.
- The RM Monitor systemd route-bypass service.
- The Clash Verge drop-in that reapplies the RM Monitor route-bypass rule.
- The source-based `ip rule` entries added for container networks.
- Temporary test files under `/tmp`.

Normal cleanup keeps:

- Long-term recordings under `/mnt/server_data/rm-monitor/records`.
- Raw record PVC data under `/mnt/PC801/rm-monitor/records`.
- Local Docker images.
- `biliup`, Bilibili cookies, and repo-local logs.
- k3s itself.

Optional destructive cleanup:

```sh
deploy/local/cleanup.sh --confirm --delete-data
deploy/local/cleanup.sh --confirm --delete-images
deploy/local/cleanup.sh --confirm --delete-secrets
deploy/local/cleanup.sh --confirm --uninstall-biliup
deploy/local/cleanup.sh --confirm --uninstall-k3s
```

`--delete-data` deletes both `/mnt/PC801/rm-monitor/records` and the long-term
NAS directory `/mnt/server_data/rm-monitor/records`. It is not appropriate for
a normal season handoff. Follow [HANDOFF_2026.md](HANDOFF_2026.md) and remove
only verified local caches while retaining the NAS archive.

Use `--uninstall-k3s` only if this machine no longer needs the Kubernetes
cluster for anything else.
