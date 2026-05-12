#!/usr/bin/env python3
import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import local_log


DEFAULT_LIVE_INFO_URL = "https://rm-static.djicdn.com/live_json/live_game_info.json"
DEFAULT_OUTPUT_ROOT = "/mnt/PC801/rm-monitor/emergency"


def main() -> int:
    parser = argparse.ArgumentParser(description="Emergency host-side RM live recorder.")
    parser.add_argument("--zone", default="南部赛区")
    parser.add_argument("--res", default="high", choices=("high", "middle", "low"))
    parser.add_argument("--live-info-url", default=DEFAULT_LIVE_INFO_URL)
    parser.add_argument("--output-root", default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--name", default="", help="Output subdirectory name. Defaults to timestamp.")
    parser.add_argument("--duration", type=int, default=0, help="Optional seconds to record. 0 means until Ctrl-C.")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    roles = load_roles(args.live_info_url, args.zone, args.res)
    if not roles:
        raise SystemExit(f"no live URLs found for zone={args.zone} res={args.res}")

    subdir = args.name.strip() or time.strftime("%Y%m%d-%H%M%S")
    output_dir = Path(args.output_root) / args.zone / subdir
    output_dir.mkdir(parents=True, exist_ok=True)

    commands = []
    for role, url in roles:
        output = output_dir / f"{safe_name(role)}.flv"
        commands.append(
            [
                args.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "info",
                "-user_agent",
                "Mozilla/5.0",
                "-rw_timeout",
                "15000000",
                "-reconnect",
                "1",
                "-reconnect_streamed",
                "1",
                "-reconnect_delay_max",
                "5",
                "-i",
                url,
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-sn",
                "-dn",
                "-c:v",
                "copy",
                "-c:a",
                "copy",
                "-f",
                "flv",
                "-y",
                str(output),
            ]
        )

    print(json.dumps({"output_dir": str(output_dir), "roles": [role for role, _ in roles]}, ensure_ascii=False, indent=2))
    local_log.log_event(
        "emergency-record",
        "INFO",
        "emergency recorder plan ready",
        zone=args.zone,
        res=args.res,
        output_dir=str(output_dir),
        roles=len(roles),
        dry_run=args.dry_run,
        duration=args.duration,
    )
    if args.dry_run:
        for command in commands:
            print(shell_join(command))
        return 0

    processes: list[subprocess.Popen] = []
    stopping = False

    def stop(signum=None, frame=None):
        nonlocal stopping
        if stopping:
            return
        stopping = True
        print("stopping emergency recorders...", file=sys.stderr)
        for process in processes:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    for command in commands:
        log_path = output_dir / (Path(command[-1]).stem + ".log")
        log_file = log_path.open("ab")
        processes.append(subprocess.Popen(command, stdout=log_file, stderr=subprocess.STDOUT))
    local_log.log_event(
        "emergency-record",
        "INFO",
        "emergency recorders started",
        output_dir=str(output_dir),
        roles=len(roles),
    )

    deadline = time.monotonic() + args.duration if args.duration > 0 else None
    while True:
        alive = [p for p in processes if p.poll() is None]
        if not alive:
            break
        if deadline is not None and time.monotonic() >= deadline:
            stop()
        time.sleep(1)

    failed = [p.returncode for p in processes if p.returncode not in (0, 255, -2)]
    if failed:
        print(f"{len(failed)} recorder process(es) exited unexpectedly", file=sys.stderr)
        local_log.log_event(
            "emergency-record",
            "ERROR",
            "recorder process exited unexpectedly",
            output_dir=str(output_dir),
            failed=len(failed),
            return_codes=failed,
        )
        return 1
    local_log.log_event("emergency-record", "INFO", "emergency recorders completed", output_dir=str(output_dir))
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
    return roles


def safe_name(name: str) -> str:
    out = "".join("_" if ch in '/\\:*?"<>|' else ch for ch in name.strip())
    return out or "unknown"


def shell_join(command: list[str]) -> str:
    import shlex

    return " ".join(shlex.quote(part) for part in command)


if __name__ == "__main__":
    raise SystemExit(local_log.run_logged("emergency-record", main))
