#!/usr/bin/env python3
import argparse
import fcntl
import json
import os
import signal
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import adaptive_training_recorder as recorder
import local_log


SERVICE = os.environ.get("RM_MONITOR_SERVICE_NAME", "official-backup-cache")
DEFAULT_EVENT = os.environ.get("RM_MONITOR_EVENT_NAME", "RMUC 2026超级对抗赛")
DEFAULT_ZONE = os.environ.get("RM_MONITOR_ZONE", "全国赛")
DEFAULT_SESSION_NAME = "正赛备用缓存"
DEFAULT_LIVE_INFO_URL = "https://rm-static.djicdn.com/live_json/live_game_info.json"
DEFAULT_SCHEDULE_URL = "https://pro-robomasters-hz-n5i3.oss-cn-hangzhou.aliyuncs.com/live_json/schedule.json"
DEFAULT_SOURCE_ROOT = "/mnt/PC801/rm-monitor/official-backup-cache"
DEFAULT_TARGET_ROOT = "/mnt/server_data/rm-monitor/records"
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_LOCK_DIR = Path(__file__).resolve().parents[2] / "logs"
DEFAULT_TERMINAL_STATUSES = "DONE,CANCELED,CANCELLED,SKIPPED"


@dataclass
class ScheduleState:
    match_dates: set[str]
    daily_matches: dict[str, list[dict]]


def main() -> int:
    recorder.SERVICE = SERVICE

    parser = argparse.ArgumentParser(
        description="Maintain an hourly official-match backup cache and archive it without Bilibili upload."
    )
    parser.add_argument("--zone", default=DEFAULT_ZONE)
    parser.add_argument("--event", default=DEFAULT_EVENT)
    parser.add_argument("--session-name", default=DEFAULT_SESSION_NAME)
    parser.add_argument("--date", default="", help="Fixed local date. Empty means follow the current local date.")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument("--res", default="high", choices=("high", "middle", "low"))
    parser.add_argument("--live-info-url", default=DEFAULT_LIVE_INFO_URL)
    parser.add_argument("--schedule-url", default=DEFAULT_SCHEDULE_URL)
    parser.add_argument("--source-root", default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--target-root", default=DEFAULT_TARGET_ROOT)
    parser.add_argument("--segment-time", type=int, default=3600)
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--settle-seconds", type=int, default=180)
    parser.add_argument("--schedule-refresh-seconds", type=int, default=300)
    parser.add_argument("--pre-start-seconds", type=int, default=0)
    parser.add_argument("--post-last-start-seconds", type=int, default=3 * 3600)
    parser.add_argument("--terminal-statuses", default=DEFAULT_TERMINAL_STATUSES)
    parser.add_argument("--probe-live", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--probe-timeout", type=int, default=8)
    parser.add_argument("--probe-cooldown-seconds", type=int, default=60)
    parser.add_argument("--record-on-schedule-error", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--ignore-schedule-dates", action="store_true")
    parser.add_argument("--keep-local-cache", action="store_true")
    parser.add_argument("--exit-after-last-date", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--lock-file", default="")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    timezone = ZoneInfo(args.timezone)
    fixed_date = args.date.strip()
    args.fixed_date = fixed_date
    lock_path = Path(args.lock_file) if args.lock_file else DEFAULT_LOCK_DIR / f"{SERVICE}-{recorder.safe_component(args.zone)}.lock"
    lock = acquire_lock(lock_path)

    recorders: dict[str, recorder.Recorder] = {}
    probe_state: dict[str, dict] = {}
    schedule: ScheduleState | None = None
    schedule_error = ""
    next_schedule_refresh = 0.0
    active_date = ""
    source_session: Path | None = None
    target_session: Path | None = None
    last_active_state: tuple[str, bool, str] | None = None
    stopping = False

    def request_stop(signum=None, frame=None):
        nonlocal stopping
        if stopping:
            return
        stopping = True
        local_log.log_event(SERVICE, "WARN", "stop requested", signal=signum)

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    try:
        if args.dry_run:
            runtime_date = fixed_date or today(timezone)
            schedule, schedule_error = refresh_schedule(args, timezone)
            args.date = runtime_date
            source_session, target_session = build_sessions(args)
            roles = recorder.load_roles(args.live_info_url, args.zone, args.res)
            active, reason = is_active_date(args, runtime_date, schedule, schedule_error, timezone)
            print(
                json.dumps(
                    {
                        "zone": args.zone,
                        "date": runtime_date,
                        "active": active,
                        "reason": reason,
                        "match_dates": sorted(schedule.match_dates) if schedule is not None else None,
                        "daily_window": describe_daily_window(schedule, runtime_date),
                        "session": args.session_name,
                        "res": args.res,
                        "source_dir": str(source_session),
                        "target_dir": str(target_session),
                        "segment_time": args.segment_time,
                        "pre_start_seconds": args.pre_start_seconds,
                        "post_last_start_seconds": args.post_last_start_seconds,
                        "keep_local_cache": args.keep_local_cache,
                        "roles": sorted(roles),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        local_log.log_event(
            SERVICE,
            "INFO",
            "official backup cache recorder started",
            zone=args.zone,
            fixed_date=fixed_date,
            res=args.res,
            source_root=args.source_root,
            target_root=args.target_root,
            segment_time=args.segment_time,
            poll_seconds=args.poll_seconds,
            settle_seconds=args.settle_seconds,
            schedule_url=args.schedule_url,
            pre_start_seconds=args.pre_start_seconds,
            post_last_start_seconds=args.post_last_start_seconds,
            terminal_statuses=args.terminal_statuses,
            keep_local_cache=args.keep_local_cache,
        )

        while not stopping:
            now = time.monotonic()
            if now >= next_schedule_refresh:
                try:
                    schedule, schedule_error = refresh_schedule(args, timezone)
                except Exception as exc:
                    schedule_error = str(exc)
                    local_log.log_event(SERVICE, "ERROR", "failed to load schedule", error=schedule_error)
                next_schedule_refresh = now + max(30, args.schedule_refresh_seconds)

            runtime_date = fixed_date or today(timezone)
            if should_exit_after_schedule(args, runtime_date, schedule):
                local_log.log_event(SERVICE, "INFO", "schedule finished; recorder exiting", date=runtime_date)
                break

            if runtime_date != active_date:
                if source_session is not None and target_session is not None:
                    recorder.stop_recorders(recorders)
                    archive_and_prune(args, source_session, target_session, recorders, force=True)
                args.date = runtime_date
                source_session, target_session = build_sessions(args)
                active_date = runtime_date

            active, reason = is_active_date(args, runtime_date, schedule, schedule_error, timezone)
            state = (runtime_date, active, reason)
            if state != last_active_state:
                local_log.log_event(SERVICE, "INFO", "schedule gate changed", date=runtime_date, active=active, reason=reason)
                last_active_state = state

            if source_session is None or target_session is None:
                raise SystemExit("internal error: session directories were not initialized")

            if not active:
                if recorders:
                    recorder.stop_recorders(recorders)
                    archive_and_prune(args, source_session, target_session, recorders, force=True)
                else:
                    archive_and_prune(args, source_session, target_session, recorders, force=False)
                if args.once:
                    break
                recorder.sleep_until_next_poll(args.poll_seconds, lambda: stopping)
                continue

            source_session.mkdir(parents=True, exist_ok=True)
            recorder.ensure_archive_target(target_session)
            try:
                desired = recorder.load_roles(args.live_info_url, args.zone, args.res)
            except Exception as exc:
                local_log.log_event(SERVICE, "ERROR", "failed to load live info", error=str(exc))
                archive_and_prune(args, source_session, target_session, recorders, force=False)
                if args.once:
                    break
                recorder.sleep_until_next_poll(args.poll_seconds, lambda: stopping)
                continue

            desired = recorder.filter_live_roles(args, desired, recorders, probe_state)
            recorder.reconcile_recorders(args, source_session, desired, recorders)
            archive_and_prune(args, source_session, target_session, recorders, force=False)

            if args.once:
                break
            recorder.sleep_until_next_poll(args.poll_seconds, lambda: stopping)
    finally:
        recorder.stop_recorders(recorders)
        if source_session is not None and target_session is not None:
            archive_and_prune(args, source_session, target_session, recorders, force=True)
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()

    local_log.log_event(SERVICE, "INFO", "official backup cache recorder completed")
    return 0


def refresh_schedule(args: argparse.Namespace, timezone: ZoneInfo) -> tuple[ScheduleState | None, str]:
    if args.ignore_schedule_dates or not args.schedule_url:
        return None, ""
    try:
        schedule = load_schedule_state(args.schedule_url, args.zone, timezone)
    except Exception as exc:
        if args.record_on_schedule_error:
            return None, str(exc)
        raise
    if not schedule.match_dates:
        return ScheduleState(set(), {}), f"no matchDates found for zone {args.zone}"
    return schedule, ""


def load_schedule_state(schedule_url: str, zone: str, timezone: ZoneInfo) -> ScheduleState:
    request = urllib.request.Request(schedule_url, headers={"User-Agent": recorder.USER_AGENT})
    with urllib.request.urlopen(request, timeout=10) as response:
        data = json.load(response)

    dates: set[str] = set()
    daily_matches: dict[str, list[dict]] = {}
    zone_nodes: list[dict] = []

    def walk(item):
        if isinstance(item, dict):
            name = item.get("name") or item.get("zoneName")
            if name == zone and ("matchDates" in item or "groupMatches" in item or "knockoutMatches" in item):
                zone_nodes.append(item)
                for value in item.get("matchDates", []) or []:
                    if isinstance(value, str) and valid_date(value):
                        dates.add(value)
            for value in item.values():
                walk(value)
        elif isinstance(item, list):
            for value in item:
                walk(value)

    walk(data)
    for zone_node in zone_nodes:
        for key in ("groupMatches", "knockoutMatches"):
            container = zone_node.get(key) or {}
            nodes = container.get("nodes", []) if isinstance(container, dict) else []
            for match in nodes or []:
                if not isinstance(match, dict):
                    continue
                planned = parse_plan_started_at(match.get("planStartedAt"), timezone)
                if planned is None:
                    continue
                date_key = planned.strftime("%Y-%m-%d")
                dates.add(date_key)
                daily_matches.setdefault(date_key, []).append(
                    {
                        "id": str(match.get("id") or ""),
                        "order": to_int(match.get("orderNumber")),
                        "status": str(match.get("status") or "").upper(),
                        "planned": planned,
                    }
                )
    for matches in daily_matches.values():
        matches.sort(key=lambda item: (item["planned"], item["order"]))
    return ScheduleState(dates, daily_matches)


def parse_plan_started_at(value: object, timezone: ZoneInfo) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed.astimezone(timezone)


def valid_date(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def is_active_date(
    args: argparse.Namespace,
    runtime_date: str,
    schedule: ScheduleState | None,
    schedule_error: str,
    timezone: ZoneInfo,
) -> tuple[bool, str]:
    if args.ignore_schedule_dates or not args.schedule_url:
        return True, "schedule gate disabled"
    if schedule is None:
        if args.record_on_schedule_error:
            detail = schedule_error or "schedule unavailable"
            return True, f"schedule unavailable; fail-open: {detail}"
        return False, schedule_error or "schedule unavailable"
    if runtime_date not in schedule.match_dates:
        return False, "date is not in zone matchDates"

    matches = schedule.daily_matches.get(runtime_date) or []
    if not matches:
        return False, "date is in matchDates but has no planned match window"

    now = datetime.now(timezone)
    first_start = matches[0]["planned"]
    last_start = matches[-1]["planned"]
    terminal = terminal_statuses(args)
    statuses = [str(item.get("status") or "").upper() for item in matches]
    started_by_status = any(status in {"STARTED", "RUNNING", "PLAYING"} for status in statuses)

    if now.timestamp() < first_start.timestamp() - max(0, args.pre_start_seconds) and not started_by_status:
        return False, f"before first planned match at {first_start.strftime('%Y-%m-%d %H:%M:%S %Z')}"
    if statuses and all(status in terminal for status in statuses):
        return False, "all planned matches for date are terminal"
    hard_stop = last_start.timestamp() + max(0, args.post_last_start_seconds)
    if now.timestamp() > hard_stop:
        return False, f"after daily hard stop for last planned match at {last_start.strftime('%Y-%m-%d %H:%M:%S %Z')}"

    active_count = sum(1 for status in statuses if status in {"STARTED", "RUNNING", "PLAYING"})
    done_count = sum(1 for status in statuses if status in terminal)
    return True, f"inside planned match day window; active={active_count}, terminal={done_count}/{len(matches)}"


def should_exit_after_schedule(args: argparse.Namespace, runtime_date: str, schedule: ScheduleState | None) -> bool:
    if not args.exit_after_last_date or args.fixed_date or schedule is None or not schedule.match_dates:
        return False
    return runtime_date > max(schedule.match_dates)


def terminal_statuses(args: argparse.Namespace) -> set[str]:
    return {item.strip().upper() for item in args.terminal_statuses.split(",") if item.strip()}


def describe_daily_window(schedule: ScheduleState | None, runtime_date: str) -> dict:
    if schedule is None:
        return {}
    matches = schedule.daily_matches.get(runtime_date) or []
    if not matches:
        return {"matches": 0}
    statuses: dict[str, int] = {}
    for item in matches:
        status = str(item.get("status") or "")
        statuses[status] = statuses.get(status, 0) + 1
    return {
        "matches": len(matches),
        "first_start": matches[0]["planned"].strftime("%Y-%m-%d %H:%M:%S %Z"),
        "last_start": matches[-1]["planned"].strftime("%Y-%m-%d %H:%M:%S %Z"),
        "statuses": statuses,
    }


def to_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def build_sessions(args: argparse.Namespace) -> tuple[Path, Path]:
    return (
        recorder.session_dir(Path(args.source_root), args),
        recorder.session_dir(Path(args.target_root), args),
    )


def archive_and_prune(
    args: argparse.Namespace,
    source_session: Path,
    target_session: Path,
    recorders: dict[str, recorder.Recorder],
    force: bool,
) -> None:
    recorder.archive_segments(source_session, target_session, recorders, args.settle_seconds, force=force)
    if not args.keep_local_cache:
        prune_archived_sources(source_session, target_session, recorders, args.settle_seconds, force=force)


def prune_archived_sources(
    source_session: Path,
    target_session: Path,
    recorders: dict[str, recorder.Recorder],
    settle_seconds: int,
    force: bool,
) -> None:
    if not source_session.exists():
        return
    if source_session.resolve() == target_session.resolve():
        return

    running_current = set()
    if not force:
        for active in recorders.values():
            if active.process.poll() is None:
                newest = recorder.newest_flv(active.role_dir)
                if newest:
                    running_current.add(newest)

    now = time.time()
    removed = 0
    for source in sorted(source_session.rglob("*.flv")):
        if not source.is_file():
            continue
        if not force and source in running_current:
            continue
        if not force and now - source.stat().st_mtime < settle_seconds:
            continue
        target = target_session / source.relative_to(source_session)
        try:
            if target.is_file() and target.stat().st_size == source.stat().st_size:
                source.unlink()
                removed += 1
        except OSError as exc:
            local_log.log_event(
                SERVICE,
                "WARN",
                "local cache pruning deferred; archive target unavailable",
                source=str(source),
                target=str(target),
                error=str(exc),
            )
            return
    if removed:
        local_log.log_event(SERVICE, "INFO", "local archived segments pruned", removed=removed, source_dir=str(source_session))


def today(timezone: ZoneInfo) -> str:
    return datetime.now(timezone).strftime("%Y-%m-%d")


def acquire_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(f"another official backup cache recorder is already running: {path}")
    lock.write(str(os.getpid()))
    lock.truncate()
    lock.flush()
    return lock


if __name__ == "__main__":
    raise SystemExit(local_log.run_logged(SERVICE, main))
