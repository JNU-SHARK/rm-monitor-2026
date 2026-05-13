#!/usr/bin/env python3
import argparse
import hashlib
import json
import subprocess
import time
import urllib.request
from pathlib import Path

import local_log


DEFAULT_LIVE_INFO_URL = "https://rm-static.djicdn.com/live_json/live_game_info.json"
DEFAULT_EVENT = "RMUC 2026超级对抗赛"
DEFAULT_NAMESPACE = "rm-monitor"
DEFAULT_IMAGE = "rm-monitor/record-job:latest"
DEFAULT_PVC = "rm-monitor-records"
DEFAULT_MOUNT = "/records"


def main() -> int:
    parser = argparse.ArgumentParser(description="Start redundant zone-level continuous cache jobs.")
    parser.add_argument("--zone", default="南部赛区")
    parser.add_argument("--event", default=DEFAULT_EVENT)
    parser.add_argument("--date", default=time.strftime("%Y-%m-%d"))
    parser.add_argument("--res", default="high", choices=("high", "middle", "low"))
    parser.add_argument("--live-info-url", default=DEFAULT_LIVE_INFO_URL)
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--pvc", default=DEFAULT_PVC)
    parser.add_argument("--mount", default=DEFAULT_MOUNT)
    parser.add_argument("--segment-time", type=int, default=60)
    parser.add_argument("--lanes", default="a,b")
    parser.add_argument("--stagger-seconds", type=int, default=30)
    parser.add_argument("--apply", action="store_true", help="Apply jobs to Kubernetes. Default prints JSON.")
    parser.add_argument("--replace", action="store_true", help="Delete same-named A/B jobs before applying.")
    args = parser.parse_args()

    roles = load_roles(args.live_info_url, args.zone, args.res)
    if not roles:
        raise SystemExit(f"no live URLs found for zone={args.zone} res={args.res}")
    lanes = [lane.strip().lower() for lane in args.lanes.split(",") if lane.strip()]
    if not lanes:
        raise SystemExit("at least one lane is required")

    jobs = []
    for lane_index, lane in enumerate(lanes):
        offset = args.stagger_seconds * lane_index
        for role, url in roles:
            jobs.append(build_job(args, lane, role, url, offset))

    local_log.log_event(
        "continuous-cache",
        "INFO",
        "continuous cache jobs prepared",
        zone=args.zone,
        res=args.res,
        lanes=lanes,
        roles=len(roles),
        jobs=len(jobs),
        segment_time=args.segment_time,
        date=args.date,
        apply=args.apply,
        replace=args.replace,
    )

    if not args.apply:
        print(json.dumps({"jobs": jobs}, ensure_ascii=False, indent=2))
        return 0

    for job in jobs:
        name = job["metadata"]["name"]
        if args.replace:
            run(["kubectl", "delete", "job", "-n", args.namespace, name, "--ignore-not-found=true"])
        apply_job(job)
    print(f"applied {len(jobs)} continuous cache jobs in namespace {args.namespace}")
    return 0


def load_roles(live_info_url: str, zone: str, res: str) -> list[tuple[str, str]]:
    with urllib.request.urlopen(live_info_url, timeout=10) as response:
        data = json.load(response)
    zone_info = next((item for item in data.get("eventData", []) if item.get("zoneName") == zone), None)
    if zone_info is None:
        return []
    roles: list[tuple[str, str]] = []
    for item in zone_info.get("fpvData", []):
        source = next((source for source in item.get("sources", []) if source.get("res") == res), None)
        if source and source.get("src"):
            roles.append((item.get("role") or "unknown", source["src"]))
    main = next((source for source in zone_info.get("zoneLiveString", []) if source.get("res") == res), None)
    if main and main.get("src"):
        roles.append(("主视角", main["src"]))
    return sorted(roles, key=lambda item: item[0])


def build_job(args: argparse.Namespace, lane: str, role: str, url: str, offset: int) -> dict:
    digest = hashlib.sha1(f"{args.zone}:{args.date}:{lane}:{role}".encode("utf-8")).hexdigest()[:10]
    job_name = f"continuous-cache-{lane}-{digest}"
    cache_dir = f"{args.mount}/_continuous_cache/{args.event}/{args.zone}/{args.date}/{lane}/{safe_path(role)}"
    command = r'''
set -eu
if [ "${OFFSET_SECONDS:-0}" -gt 0 ]; then
  sleep "$OFFSET_SECONDS"
fi
mkdir -p "$CACHE_DIR"
while true; do
  ffmpeg -hide_banner -loglevel warning \
    -user_agent 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36' \
    -rw_timeout 15000000 \
    -reconnect 1 -reconnect_streamed 1 -reconnect_on_network_error 1 \
    -reconnect_on_http_error 404,408,429,500,502,503,504 \
    -reconnect_delay_max 5 \
    -i "$SRC" \
    -map 0:v:0 -map 0:a:0? -sn -dn -c:v copy -c:a copy \
    -f segment -segment_format flv -segment_time "$SEGMENT_TIME" -reset_timestamps 1 -strftime 1 \
    "$CACHE_DIR/%Y%m%d_%H%M%S.flv" || true
  echo "$(date -Is) ffmpeg restarted lane=$LANE role=$ROLE" >&2
  sleep 3
done
'''.strip()
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "namespace": args.namespace,
            "labels": {
                "app.kubernetes.io/name": "continuous-cache-ab",
                "rm-monitor/job": "continuous-cache-ab",
                "rm-monitor/cache-lane": lane,
            },
            "annotations": {
                "rm-monitor/zone": args.zone,
                "rm-monitor/date": args.date,
                "rm-monitor/role": role,
            },
        },
        "spec": {
            "backoffLimit": 100000,
            "template": {
                "metadata": {
                    "labels": {
                        "app.kubernetes.io/name": "continuous-cache-ab",
                        "rm-monitor/job": "continuous-cache-ab",
                        "rm-monitor/cache-lane": lane,
                    }
                },
                "spec": {
                    "restartPolicy": "OnFailure",
                    "serviceAccountName": "rm-monitor-dispatcher",
                    "nodeSelector": {"rm-monitor/storage": "true"},
                    "containers": [
                        {
                            "name": "continuous-cache",
                            "image": args.image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["/bin/sh", "-lc"],
                            "args": [command],
                            "env": [
                                {"name": "SRC", "value": url},
                                {"name": "ROLE", "value": role},
                                {"name": "LANE", "value": lane},
                                {"name": "CACHE_DIR", "value": cache_dir},
                                {"name": "SEGMENT_TIME", "value": str(args.segment_time)},
                                {"name": "OFFSET_SECONDS", "value": str(offset)},
                            ],
                            "volumeMounts": [{"name": "records", "mountPath": args.mount}],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "records",
                            "persistentVolumeClaim": {"claimName": args.pvc},
                        }
                    ],
                },
            },
        },
    }


def apply_job(job: dict) -> None:
    payload = json.dumps(job, ensure_ascii=False).encode("utf-8")
    subprocess.run(["kubectl", "apply", "-f", "-"], input=payload, check=True)


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def safe_path(name: str) -> str:
    return "".join("_" if ch in '/\\:*?"<>|' else ch for ch in name.strip()) or "unknown"


if __name__ == "__main__":
    raise SystemExit(local_log.run_logged("continuous-cache", main))
