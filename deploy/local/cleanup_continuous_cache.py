#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import local_log


SERVICE = "continuous-cache-cleanup"
DEFAULT_CACHE_ROOT = "/mnt/PC801/rm-monitor/records/_continuous_cache"
DEFAULT_NAMESPACE = "rm-monitor"
DEFAULT_POSTGRES = "deployment/postgres"
DEFAULT_DB_USER = "rm_monitor"
DEFAULT_DB_NAME = "rm_monitor"
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
FILE_TIME_FORMAT = "%Y%m%d_%H%M%S"


@dataclass
class MatchWindow:
    match_id: str
    event: str
    zone: str
    order: int
    started_at: datetime
    ended_at: datetime


@dataclass
class Segment:
    lane: str
    role: str
    path: Path
    size: int
    started_at: datetime
    ended_at: datetime


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely clean A/B per-minute continuous cache segments for one finished match.")
    parser.add_argument("--match-id", default="")
    parser.add_argument("--zone", default="")
    parser.add_argument("--order", type=int)
    parser.add_argument("--event", default="", help="Override event name used under the cache root.")
    parser.add_argument("--date", default="", help="Cache date, for example 2026-05-13. Defaults to match start date.")
    parser.add_argument("--start", default="", help=f"Override local start time, format: {TIME_FORMAT}.")
    parser.add_argument("--end", default="", help=f"Override local end time, format: {TIME_FORMAT}.")
    parser.add_argument("--role", action="append", default=[], help="Role to clean. Defaults to final non-part roles for the match.")
    parser.add_argument("--lanes", default="a,b")
    parser.add_argument("--cache-root", default=DEFAULT_CACHE_ROOT)
    parser.add_argument("--segment-time", type=int, default=60)
    parser.add_argument("--padding-seconds", type=int, default=0)
    parser.add_argument("--min-age-seconds", type=int, default=120, help="Do not delete files modified more recently than this.")
    parser.add_argument("--prune-empty-dirs", action="store_true")
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    parser.add_argument("--postgres", default=DEFAULT_POSTGRES)
    parser.add_argument("--db-user", default=DEFAULT_DB_USER)
    parser.add_argument("--db-name", default=DEFAULT_DB_NAME)
    parser.add_argument("--submit", action="store_true")
    parser.add_argument("--enabled", action="store_true", help="Actually allow continuous cache cleanup. Default is a safe no-op.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if not args.enabled:
        local_log.log_event(
            SERVICE,
            "INFO",
            "continuous cache cleanup skipped because it is not enabled",
            match_id=args.match_id,
            zone=args.zone,
            order=args.order,
            submit=args.submit,
        )
        print(json.dumps({"skipped": True, "reason": "continuous cache cleanup is disabled by default"}, ensure_ascii=False, indent=2))
        return 0

    match = load_match_window(args)
    roles = args.role or load_final_roles(args, match.match_id)
    if not roles:
        local_log.log_event(SERVICE, "WARNING", "no final roles found for match", match_id=match.match_id)
        print(json.dumps({"match_id": match.match_id, "segments": 0, "deleted": 0}, ensure_ascii=False, indent=2))
        return 0

    event = args.event or match.event
    started_at = parse_time(args.start) if args.start else match.started_at
    ended_at = parse_time(args.end) if args.end else match.ended_at
    if ended_at <= started_at:
        raise SystemExit("--end must be after --start")
    if args.padding_seconds > 0:
        started_at -= timedelta(seconds=args.padding_seconds)
        ended_at += timedelta(seconds=args.padding_seconds)
    date = args.date or started_at.date().isoformat()
    lanes = [lane.strip() for lane in args.lanes.split(",") if lane.strip()]

    segments = find_segments(args, event, match.zone, date, lanes, roles, started_at, ended_at)
    summary = summarize(segments)
    result = {
        "match_id": match.match_id,
        "zone": match.zone,
        "order": match.order,
        "date": date,
        "window": {"start": started_at.strftime(TIME_FORMAT), "end": ended_at.strftime(TIME_FORMAT)},
        "roles": len(roles),
        "segments": len(segments),
        "bytes": sum(item.size for item in segments),
        "summary": summary,
        "submit": args.submit,
    }
    if args.verbose:
        result["paths"] = [str(item.path) for item in segments]

    deleted = 0
    deleted_bytes = 0
    errors = []
    if args.submit:
        for item in segments:
            try:
                item.path.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                errors.append(f"{item.path}: {exc}")
                continue
            deleted += 1
            deleted_bytes += item.size
            if args.prune_empty_dirs:
                prune_empty_parents(item.path.parent, Path(args.cache_root))
        result["deleted"] = deleted
        result["deleted_bytes"] = deleted_bytes
        result["errors"] = errors[:20]

    level = "ERROR" if errors else "INFO"
    local_log.log_event(
        SERVICE,
        level,
        "continuous cache cleanup finished" if args.submit else "continuous cache cleanup planned",
        match_id=match.match_id,
        zone=match.zone,
        order=match.order,
        segments=len(segments),
        bytes=result["bytes"],
        deleted=deleted,
        deleted_bytes=deleted_bytes,
        errors=len(errors),
        submit=args.submit,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if errors else 0


def load_match_window(args: argparse.Namespace) -> MatchWindow:
    where = ""
    if args.match_id:
        where = f"m.id = '{sql_escape(args.match_id)}'"
    elif args.zone and args.order is not None:
        where = f"m.zone = '{sql_escape(args.zone)}' and m.\"order\" = {int(args.order)}"
    else:
        raise SystemExit("select a match with --match-id or --zone plus --order")
    rows = psql(
        args,
        f"""
        select
            m.id,
            m.event,
            m.zone,
            m."order",
            to_char(min(mr.started_at at time zone 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS'),
            to_char(max(coalesce(mr.ended_at, mr.updated_at) at time zone 'Asia/Shanghai'), 'YYYY-MM-DD HH24:MI:SS')
        from matches m
        join match_rounds mr on mr.match_rounds = m.id
        where {where}
        group by m.id
        order by m."order", m.id;
        """,
    )
    if not rows:
        raise SystemExit("match window not found")
    if len(rows) > 1 and not args.match_id:
        raise SystemExit("match selector is ambiguous; use --match-id")
    row = rows[0]
    if not row[4] or not row[5]:
        raise SystemExit(f"match {row[0]} does not have a complete time window")
    return MatchWindow(
        match_id=row[0],
        event=row[1],
        zone=row[2],
        order=int(row[3]),
        started_at=parse_time(row[4]),
        ended_at=parse_time(row[5]),
    )


def load_final_roles(args: argparse.Namespace, match_id: str) -> list[str]:
    rows = psql(
        args,
        f"""
        select distinct rt.role
        from record_tasks rt
        join match_rounds mr on mr.id = rt.match_round_record_tasks
        join matches m on m.id = mr.match_rounds
        where m.id = '{sql_escape(match_id)}'
          and position('__part' in rt.role) = 0
          and rt.status = 'SUCCEEDED'
        order by rt.role;
        """,
    )
    return [row[0] for row in rows if row and row[0]]


def find_segments(
    args: argparse.Namespace,
    event: str,
    zone: str,
    date: str,
    lanes: list[str],
    roles: list[str],
    started_at: datetime,
    ended_at: datetime,
) -> list[Segment]:
    now_ts = datetime.now().timestamp()
    root = Path(args.cache_root)
    segments = []
    for lane in lanes:
        for role in roles:
            directory = root / event / zone / date / lane / safe_path(role)
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.flv")):
                try:
                    segment_start = datetime.strptime(path.stem, FILE_TIME_FORMAT)
                except ValueError:
                    continue
                segment_end = segment_start + timedelta(seconds=args.segment_time)
                if segment_start >= ended_at or segment_end <= started_at:
                    continue
                try:
                    stat = path.stat()
                except FileNotFoundError:
                    continue
                if now_ts - stat.st_mtime < args.min_age_seconds:
                    continue
                segments.append(
                    Segment(
                        lane=lane,
                        role=role,
                        path=path,
                        size=stat.st_size,
                        started_at=segment_start,
                        ended_at=segment_end,
                    )
                )
    return segments


def summarize(segments: list[Segment]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str], list[Segment]] = defaultdict(list)
    for item in segments:
        grouped[(item.lane, item.role)].append(item)
    out = []
    for (lane, role), items in sorted(grouped.items()):
        out.append(
            {
                "lane": lane,
                "role": role,
                "segments": len(items),
                "bytes": sum(item.size for item in items),
            }
        )
    return out


def prune_empty_parents(path: Path, stop: Path) -> None:
    stop = stop.resolve()
    current = path.resolve()
    while current != stop and stop in current.parents:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def psql(args: argparse.Namespace, query: str) -> list[list[str]]:
    result = subprocess.run(
        [
            "kubectl",
            "exec",
            "-n",
            args.namespace,
            args.postgres,
            "--",
            "psql",
            "-U",
            args.db_user,
            "-d",
            args.db_name,
            "-At",
            "-F",
            "\t",
            "-c",
            query,
        ],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or result.stdout.strip() or "psql failed")
    return [line.split("\t") for line in result.stdout.splitlines() if line.strip()]


def safe_path(name: str) -> str:
    return "".join("_" if ch in '/\\:*?"<>|' else ch for ch in name.strip()) or "unknown"


def parse_time(value: str) -> datetime:
    return datetime.strptime(value, TIME_FORMAT)


def sql_escape(value: str) -> str:
    return value.replace("'", "''")


if __name__ == "__main__":
    raise SystemExit(local_log.run_logged(SERVICE, main))
