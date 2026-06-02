# RM Monitor Local Operations

This note is the quick runbook for future local deployments on `maverick-server`.
The root `README.md` is still the detailed deployment ledger.

## Fixed Local Paths

- Repo: `/home/maverick/RM/rm-monitor-2026`
- Kubernetes namespace: `rm-monitor`
- Local recording source root: `/mnt/PC801/rm-monitor/records`
- Long-term archive root: `/mnt/server_data/rm-monitor/records`
- Dashboard: `http://127.0.0.1:18080` or `http://192.168.16.3:18080`
- Bilibili cookie file: `/home/maverick/RM/rm-monitor-2026/cookies.json`

Do not treat the normal Docker containers `wechatpad`, `langbot`,
`mysql_wxpad`, and `redis_wxpad` as RM Monitor containers. They were still
running after RM Monitor was shut down and appear to belong to a different
service.

## Normal Startup

Apply local Kubernetes resources:

```sh
kubectl apply -k deploy/local
```

Start local services for a live event:

```sh
sudo systemctl start rm-monitor-container-net-bypass.service
sudo systemctl start rm-monitor-dashboard.service
sudo systemctl start rm-monitor-archive-auto-queue.service
sudo systemctl start rm-monitor-biliup-auto-queue.service
sudo systemctl start rm-monitor-cluster-dns-guard.timer
```

For a new region, update the two queue service files before starting:

- `deploy/local/rm-monitor-archive-auto-queue.service`
- `deploy/local/rm-monitor-biliup-auto-queue.service`

The important fields are `--zone`, `--start-order`, `--season-name`,
`--season-id`, and `--section-id`. The most recent committed service command
was for North:

```sh
--zone 北部赛区 --season-name RMUC2026北部赛区全视角录制 --season-id 8209772 --section-id 9124491
```

Reload systemd after editing installed unit files:

```sh
sudo systemctl daemon-reload
```

## Health Checks

Fast service checks:

```sh
systemctl list-units --type=service --all 'rm-monitor*' --no-pager --plain
systemctl list-timers --all 'rm-monitor*' --no-pager --plain
kubectl get deploy,pods,jobs -n rm-monitor
```

Dashboard JSON can be queried without opening the browser:

```sh
python3 - <<'PY'
import json, urllib.request
url = 'http://127.0.0.1:18080/api/logs?target=all&since=30m&level=ALL&diagnostics=1&tail=300'
with urllib.request.urlopen(url, timeout=10) as r:
    data = json.load(r)
print(data.get('summary'))
for step in data.get('pipeline', {}).get('steps', []):
    print(step)
for issue in data.get('issues', []):
    print('issue', issue.get('severity'), issue.get('area'), issue.get('title'))
PY
```

Completed `record-*` or `record-merge-*` Pods are not active containers. Failed
`record-*` Jobs can be harmless only when the base final file exists and was
uploaded. This happens when the official live source closes immediately after a
match and continuation `__partN` jobs get HTTP 404.

## Completion Audit

Run this before shutdown. It starts from every `DONE` match, not only matches
that already have artifacts. That distinction matters: an inner-join audit can
hide matches with zero tasks.

```sh
kubectl exec -n rm-monitor deployment/postgres -- psql -U rm_monitor -d rm_monitor -P pager=off -F $'\t' -A -c "
with final_artifacts as (
  select m.id,m.zone,m.\"order\",m.latest_status,
         count(distinct ma.id) filter (
           where rt.status='SUCCEEDED'
             and ma.kind='source'
             and position('__part' in rt.role)=0
         ) as final_sources,
         count(distinct ut.id) filter (where ut.status='SUCCEEDED') as uploads_succeeded
  from matches m
  left join match_rounds mr on mr.match_rounds=m.id
  left join record_tasks rt on rt.match_round_record_tasks=mr.id
  left join media_artifacts ma on ma.record_task_media_artifacts=rt.id
  left join upload_tasks ut on ut.media_artifact_upload_task=ma.id
  where m.event='RMUC 2026超级对抗赛' and m.latest_status='DONE'
  group by m.id,m.zone,m.\"order\",m.latest_status
)
select zone,\"order\",id,final_sources,uploads_succeeded
from final_artifacts
where final_sources < 14 or uploads_succeeded < final_sources
order by zone,\"order\";
"
```

Also verify local source cleanup and long-term archive size:

```sh
find /mnt/PC801/rm-monitor/records -type f \( -name '*.flv' -o -name '*.mp4' -o -name '*.part*.flv' -o -name '*.tmp.*' \) -print
du -sh /mnt/PC801/rm-monitor/records
du -sh /mnt/server_data/rm-monitor/records/RMUC\ 2026超级对抗赛/*赛区
```

As of 2026-06-02 19:12 CST:

- South: 88 matches DONE; all final source artifacts uploaded; local source artifacts deleted.
- East: 88 matches DONE; all final source artifacts uploaded; local source artifacts deleted.
- North: matches 5-90 recorded/uploaded/archived; matches 1-4 are DONE in the schedule but have zero record tasks and zero uploads.
- Local `/mnt/PC801/rm-monitor/records` had only per-match `README.md` files, no media files.
- Long-term archive sizes were roughly South 1.7T, East 1.8T, North 2.0T.

Known compensated anomalies from the 2026 regional run:

- Some matches have more than 14 final artifacts because of replay/extra final files.
- Some failed `__partN` tasks were caused by the official stream closing after the match. They are acceptable only when the corresponding base role final file exists and upload succeeded.
- North match 60 was a typical close-stream case: final 14 files existed, but later continuation parts saw DJI HTTP 404.

## Shutdown

Use this order at the end of a region or event:

```sh
sudo systemctl stop \
  rm-monitor-archive-auto-queue.service \
  rm-monitor-biliup-auto-queue.service \
  rm-monitor-dashboard.service \
  rm-monitor-cluster-dns-guard.timer \
  rm-monitor-adaptive-training-recorder.timer \
  rm-monitor-adaptive-training-upload.timer \
  rm-monitor-cluster-dns-guard.service \
  rm-monitor-adaptive-training-recorder.service \
  rm-monitor-adaptive-training-upload.service \
  rm-monitor-container-net-bypass.service \
  rm-monitor-proxy-relay.service

kubectl delete jobs --all -n rm-monitor --wait=false
kubectl scale deployment --all -n rm-monitor --replicas=0
kubectl delete pods --all -n rm-monitor --wait=false
sudo systemctl reset-failed rm-monitor-proxy-relay.service
```

Final shutdown check:

```sh
kubectl get pods,jobs -n rm-monitor
kubectl get deploy -n rm-monitor -o custom-columns=NAME:.metadata.name,READY:.status.readyReplicas,AVAILABLE:.status.availableReplicas,DESIRED:.spec.replicas
systemctl list-units --type=service --all 'rm-monitor*' --no-pager --plain
systemctl list-timers --all 'rm-monitor*' --no-pager --plain
docker ps --format 'table {{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Status}}'
```

Expected stopped state:

- `kubectl get pods,jobs -n rm-monitor` returns no resources.
- RM Monitor deployments still exist but have desired replicas `0`.
- RM Monitor systemd services are `inactive dead`.
- RM Monitor timers have no next run.
- Non-RM Docker containers may still be running.

## Temporary Database Access After Shutdown

If the namespace has been scaled to zero but audit data is needed, temporarily
start only Postgres:

```sh
kubectl scale deployment/postgres -n rm-monitor --replicas=1
kubectl rollout status deployment/postgres -n rm-monitor --timeout=120s
```

After queries, scale it back down:

```sh
kubectl scale deployment/postgres -n rm-monitor --replicas=0
```
