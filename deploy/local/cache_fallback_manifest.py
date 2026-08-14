#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import local_log


DEFAULT_CACHE_ROOT = "/mnt/PC801/rm-monitor/records/_continuous_cache"
DEFAULT_EVENT = os.environ.get("RM_MONITOR_EVENT_NAME", "RMUC 2026超级对抗赛")
DEFAULT_ZONE = os.environ.get("RM_MONITOR_ZONE", "全国赛")
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
FILE_TIME_FORMAT = "%Y%m%d_%H%M%S"


@dataclass
class Segment:
    lane: str
    path: Path
    start: datetime
    end: datetime
    size: int
    valid: bool
    reason: str = ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Build an A/B continuous-cache fallback manifest.")
    parser.add_argument("--zone", default=DEFAULT_ZONE)
    parser.add_argument("--event", default=DEFAULT_EVENT)
    parser.add_argument("--date", required=True, help="Cache date, for example 2026-05-13.")
    parser.add_argument("--role", required=True)
    parser.add_argument("--start", required=True, help=f"Local time, format: {TIME_FORMAT}")
    parser.add_argument("--end", required=True, help=f"Local time, format: {TIME_FORMAT}")
    parser.add_argument("--cache-root", default=DEFAULT_CACHE_ROOT)
    parser.add_argument("--primary", default="a")
    parser.add_argument("--backup", default="b")
    parser.add_argument("--segment-time", type=int, default=60)
    parser.add_argument("--min-size", type=int, default=1024 * 1024)
    parser.add_argument("--probe", action="store_true", help="Run ffprobe on candidate segments.")
    parser.add_argument("--write-concat", default="", help="Write ffmpeg concat demuxer list.")
    args = parser.parse_args()

    start = datetime.strptime(args.start, TIME_FORMAT)
    end = datetime.strptime(args.end, TIME_FORMAT)
    if end <= start:
        raise SystemExit("--end must be after --start")

    segments = []
    for lane in (args.primary, args.backup):
        segments.extend(load_segments(args, lane))
    if not segments:
        raise SystemExit("no cache segments found")

    chosen, gaps = choose_segments(segments, start, end)
    result = {
        "zone": args.zone,
        "date": args.date,
        "role": args.role,
        "start": args.start,
        "end": args.end,
        "segments": [
            {
                "lane": item.lane,
                "path": str(item.path),
                "start": item.start.strftime(TIME_FORMAT),
                "end": item.end.strftime(TIME_FORMAT),
                "size": item.size,
            }
            for item in chosen
        ],
        "gaps": [{"start": s.strftime(TIME_FORMAT), "end": e.strftime(TIME_FORMAT)} for s, e in gaps],
    }

    if args.write_concat:
        write_concat(Path(args.write_concat), chosen)
        result["concat"] = args.write_concat

    level = "ERROR" if gaps else "INFO"
    local_log.log_event(
        "continuous-cache",
        level,
        "fallback manifest generated",
        zone=args.zone,
        date=args.date,
        role=args.role,
        segments=len(chosen),
        gaps=len(gaps),
        concat=args.write_concat,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if gaps else 0


def load_segments(args: argparse.Namespace, lane: str) -> list[Segment]:
    base = Path(args.cache_root) / args.event / args.zone / args.date / lane / safe_path(args.role)
    out = []
    for path in sorted(base.glob("*.flv")):
        try:
            start = datetime.strptime(path.stem, FILE_TIME_FORMAT)
        except ValueError:
            continue
        size = path.stat().st_size
        valid, reason = validate_segment(path, size, args.min_size, args.probe)
        out.append(
            Segment(
                lane=lane,
                path=path,
                start=start,
                end=start + timedelta(seconds=args.segment_time),
                size=size,
                valid=valid,
                reason=reason,
            )
        )
    return out


def validate_segment(path: Path, size: int, min_size: int, probe: bool) -> tuple[bool, str]:
    if size < min_size:
        return False, "too_small"
    if not probe:
        return True, ""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return False, "ffprobe_failed"
    if "video" not in result.stdout and "audio" not in result.stdout:
        return False, "no_media_stream"
    return True, ""


def choose_segments(segments: list[Segment], start: datetime, end: datetime) -> tuple[list[Segment], list[tuple[datetime, datetime]]]:
    primary_lane = segments[0].lane
    by_preference = sorted(segments, key=lambda item: (item.lane != primary_lane, item.start))
    chosen = []
    gaps = []
    cursor = start
    while cursor < end:
        candidates = [
            item
            for item in by_preference
            if item.valid and item.start <= cursor < item.end and item.path not in {chosen_item.path for chosen_item in chosen[-2:]}
        ]
        if candidates:
            item = candidates[0]
            if not chosen or chosen[-1].path != item.path:
                chosen.append(item)
            cursor = min(item.end, end)
            continue
        future = [item.start for item in by_preference if item.valid and item.start > cursor]
        next_cursor = min(future) if future else end
        gaps.append((cursor, min(next_cursor, end)))
        cursor = min(next_cursor, end)
    return chosen, gaps


def write_concat(path: Path, segments: list[Segment]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for item in segments:
            escaped = str(item.path).replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")


def safe_path(name: str) -> str:
    return "".join("_" if ch in '/\\:*?"<>|' else ch for ch in name.strip()) or "unknown"


if __name__ == "__main__":
    raise SystemExit(local_log.run_logged("continuous-cache", main))
