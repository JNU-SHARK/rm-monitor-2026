#!/usr/bin/env python3
import argparse
import fcntl
import json
import os
import signal
import shutil
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from zoneinfo import ZoneInfo

import local_log


DEFAULT_EVENT = "RMUC 2026超级对抗赛"
DEFAULT_ZONE = "北部赛区"
DEFAULT_SESSION_NAME = "适应性训练"
DEFAULT_LIVE_INFO_URL = "https://rm-static.djicdn.com/live_json/live_game_info.json"
DEFAULT_SOURCE_ROOT = "/mnt/PC801/rm-monitor/adaptive-training"
DEFAULT_TARGET_ROOT = "/mnt/server_data/rm-monitor/records"
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_LOCK_DIR = Path(__file__).resolve().parents[2] / "logs"
SERVICE = "adaptive-training-recorder"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36"


@dataclass
class Recorder:
    role: str
    url: str
    role_dir: Path
    process: subprocess.Popen
    log_handle: object


def main() -> int:
    parser = argparse.ArgumentParser(description="Record adaptive training streams without match-status dependency.")
    parser.add_argument("--zone", default=DEFAULT_ZONE)
    parser.add_argument("--event", default=DEFAULT_EVENT)
    parser.add_argument("--session-name", default=DEFAULT_SESSION_NAME)
    parser.add_argument("--date", default="")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument("--res", default="high", choices=("high", "middle", "low"))
    parser.add_argument("--live-info-url", default=DEFAULT_LIVE_INFO_URL)
    parser.add_argument("--source-root", default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--target-root", default=DEFAULT_TARGET_ROOT)
    parser.add_argument("--segment-time", type=int, default=3600)
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--settle-seconds", type=int, default=180)
    parser.add_argument("--probe-live", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--probe-timeout", type=int, default=8)
    parser.add_argument("--probe-cooldown-seconds", type=int, default=60)
    parser.add_argument("--stop-at", default="", help="Local ISO timestamp. Example: 2026-05-21T00:00:00+08:00")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--lock-file", default="")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    timezone = ZoneInfo(args.timezone)
    args.date = args.date or datetime.now(timezone).strftime("%Y-%m-%d")
    stop_at = parse_stop_at(args.stop_at, timezone)
    source_session = session_dir(Path(args.source_root), args)
    target_session = session_dir(Path(args.target_root), args)
    lock = acquire_lock(Path(args.lock_file) if args.lock_file else DEFAULT_LOCK_DIR / f"{SERVICE}-{safe_component(args.zone)}-{args.date}.lock")

    recorders: dict[str, Recorder] = {}
    probe_state: dict[str, dict] = {}
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
            roles = load_roles(args.live_info_url, args.zone, args.res)
            print(
                json.dumps(
                    {
                        "zone": args.zone,
                        "date": args.date,
                        "session": args.session_name,
                        "res": args.res,
                        "source_dir": str(source_session),
                        "target_dir": str(target_session),
                        "segment_time": args.segment_time,
                        "stop_at": args.stop_at,
                        "roles": sorted(roles),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        source_session.mkdir(parents=True, exist_ok=True)
        target_session.mkdir(parents=True, exist_ok=True)
        local_log.log_event(
            SERVICE,
            "INFO",
            "adaptive training recorder started",
            zone=args.zone,
            date=args.date,
            res=args.res,
            source_dir=str(source_session),
            target_dir=str(target_session),
            segment_time=args.segment_time,
            poll_seconds=args.poll_seconds,
            settle_seconds=args.settle_seconds,
            stop_at=args.stop_at,
        )

        while not stopping:
            if stop_at and time.time() >= stop_at:
                local_log.log_event(SERVICE, "INFO", "stop time reached", stop_at=args.stop_at)
                break
            try:
                desired = load_roles(args.live_info_url, args.zone, args.res)
            except Exception as exc:
                local_log.log_event(SERVICE, "ERROR", "failed to load live info", error=str(exc))
                archive_segments(source_session, target_session, recorders, args.settle_seconds, force=False)
                if args.once:
                    break
                sleep_until_next_poll(args.poll_seconds, lambda: stopping)
                continue
            desired = filter_live_roles(args, desired, recorders, probe_state)
            reconcile_recorders(args, source_session, desired, recorders)
            archive_segments(source_session, target_session, recorders, args.settle_seconds, force=False)
            if args.once:
                break
            sleep_until_next_poll(args.poll_seconds, lambda: stopping)
    finally:
        stop_recorders(recorders)
        archive_segments(source_session, target_session, recorders, args.settle_seconds, force=True)
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()

    local_log.log_event(SERVICE, "INFO", "adaptive training recorder completed", source_dir=str(source_session), target_dir=str(target_session))
    return 0


def load_roles(live_info_url: str, zone: str, res: str) -> dict[str, str]:
    with urllib.request.urlopen(live_info_url, timeout=10) as response:
        data = json.load(response)
    zone_info = next((item for item in data.get("eventData", []) if item.get("zoneName") == zone), None)
    if zone_info is None:
        return {}

    roles: list[tuple[str, str]] = []
    for item in zone_info.get("fpvData", []) or []:
        source = next((source for source in item.get("sources", []) or [] if source.get("res") == res), None)
        if source and source.get("src"):
            roles.append((item.get("role") or "unknown", source["src"]))
    main = next((source for source in zone_info.get("zoneLiveString", []) or [] if source.get("res") == res), None)
    if main and main.get("src"):
        roles.append(("主视角", main["src"]))

    out: dict[str, str] = {}
    counts: dict[str, int] = {}
    for role, url in sorted(roles, key=lambda item: item[0]):
        safe_role = role.strip() or "unknown"
        counts[safe_role] = counts.get(safe_role, 0) + 1
        name = safe_role if counts[safe_role] == 1 else f"{safe_role}_{counts[safe_role]}"
        out[name] = url
    return out


def filter_live_roles(
    args: argparse.Namespace,
    desired: dict[str, str],
    recorders: dict[str, Recorder],
    probe_state: dict[str, dict],
) -> dict[str, str]:
    if not args.probe_live:
        return desired
    now = time.time()
    live: dict[str, str] = {}
    for role, url in desired.items():
        running = recorders.get(role)
        if running is not None and running.url == url and running.process.poll() is None:
            live[role] = url
            continue
        entry = probe_state.setdefault(role, {})
        cached_url = entry.get("url")
        cached_live = bool(entry.get("live")) and cached_url == url
        next_probe_at = float(entry.get("next_probe_at") or 0)
        if now < next_probe_at:
            if cached_live:
                live[role] = url
            continue
        ok, detail = probe_hls_url(url, args.probe_timeout)
        entry.update(
            {
                "url": url,
                "live": ok,
                "detail": detail,
                "next_probe_at": now + max(1, args.probe_cooldown_seconds),
            }
        )
        status = "live" if ok else f"not-ready:{detail}"
        if entry.get("last_logged_status") != status:
            local_log.log_event(SERVICE, "INFO", "live probe state changed", role=role, status=status)
            entry["last_logged_status"] = status
        if ok:
            live[role] = url
    return live


def probe_hls_url(url: str, timeout: int) -> tuple[bool, str]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.getcode()
            sample = response.read(512)
    except HTTPError as exc:
        return False, f"http-{exc.code}"
    except URLError as exc:
        return False, f"url-error:{exc.reason}"
    except TimeoutError:
        return False, "timeout"
    except Exception as exc:
        return False, type(exc).__name__
    if not 200 <= int(status or 0) < 400:
        return False, f"http-{status}"
    if url.lower().split("?", 1)[0].endswith(".m3u8") and b"#EXTM3U" not in sample:
        return False, "not-m3u8"
    return True, "ok"


def reconcile_recorders(args: argparse.Namespace, source_session: Path, desired: dict[str, str], recorders: dict[str, Recorder]) -> None:
    for role in list(recorders):
        recorder = recorders[role]
        return_code = recorder.process.poll()
        if return_code is not None:
            close_log(recorder)
            del recorders[role]
            local_log.log_event(SERVICE, "WARN", "ffmpeg exited", role=role, return_code=return_code)
            continue
        if role not in desired:
            stop_recorder(recorder, reason="stream disappeared")
            del recorders[role]
            continue
        if desired[role] != recorder.url:
            stop_recorder(recorder, reason="stream url changed")
            del recorders[role]

    for role, url in desired.items():
        if role in recorders:
            continue
        recorders[role] = start_recorder(args, source_session, role, url)


def start_recorder(args: argparse.Namespace, source_session: Path, role: str, url: str) -> Recorder:
    role_dir = source_session / safe_component(role)
    role_dir.mkdir(parents=True, exist_ok=True)
    run_stamp = time.strftime("%Y%m%d_%H%M%S")
    output_pattern = role_dir / f"{run_stamp}_%Y%m%d_%H%M%S.flv"
    command = [
        args.ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-user_agent",
        USER_AGENT,
        "-rw_timeout",
        "15000000",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_on_network_error",
        "1",
        "-reconnect_on_http_error",
        "404,408,429,500,502,503,504",
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
        "segment",
        "-segment_format",
        "flv",
        "-segment_time",
        str(args.segment_time),
        "-segment_atclocktime",
        "1",
        "-reset_timestamps",
        "1",
        "-strftime",
        "1",
        str(output_pattern),
    ]
    log_path = role_dir / f"{run_stamp}.ffmpeg.log"
    log_handle = log_path.open("ab")
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log_handle, stderr=subprocess.STDOUT)
    local_log.log_event(SERVICE, "INFO", "ffmpeg started", role=role, pid=process.pid, output_pattern=str(output_pattern), log=str(log_path))
    return Recorder(role=role, url=url, role_dir=role_dir, process=process, log_handle=log_handle)


def stop_recorders(recorders: dict[str, Recorder]) -> None:
    for role in list(recorders):
        stop_recorder(recorders[role], reason="recorder stopping")
        del recorders[role]


def stop_recorder(recorder: Recorder, reason: str) -> None:
    process = recorder.process
    if process.poll() is None:
        local_log.log_event(SERVICE, "INFO", "stopping ffmpeg", role=recorder.role, pid=process.pid, reason=reason)
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    local_log.log_event(SERVICE, "INFO", "ffmpeg stopped", role=recorder.role, return_code=process.poll(), reason=reason)
    close_log(recorder)


def archive_segments(source_session: Path, target_session: Path, recorders: dict[str, Recorder], settle_seconds: int, force: bool) -> None:
    if not source_session.exists():
        return
    running_current = set()
    if not force:
        for recorder in recorders.values():
            if recorder.process.poll() is None:
                newest = newest_flv(recorder.role_dir)
                if newest:
                    running_current.add(newest)

    now = time.time()
    copied = 0
    for source in sorted(source_session.rglob("*.flv")):
        if not source.is_file() or source.stat().st_size <= 0:
            continue
        if not force and source in running_current:
            continue
        if not force and now - source.stat().st_mtime < settle_seconds:
            continue
        target = target_session / source.relative_to(source_session)
        if copy_if_needed(source, target, force=force):
            copied += 1
            local_log.log_event(SERVICE, "INFO", "segment archived", source=str(source), target=str(target), size=target.stat().st_size)
    if copied:
        local_log.log_event(SERVICE, "INFO", "archive pass completed", copied=copied, force=force)


def copy_if_needed(source: Path, target: Path, force: bool) -> bool:
    before = source.stat()
    if target.is_file() and target.stat().st_size == before.st_size:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.tmp.{os.getpid()}.{time.time_ns()}")
    try:
        with source.open("rb") as src, tmp.open("wb") as dst:
            shutil.copyfileobj(src, dst, length=8 * 1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        after = source.stat()
        if not force and (after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns):
            return False
        tmp.replace(target)
    finally:
        if tmp.exists():
            tmp.unlink()
    if target.stat().st_size != after.st_size:
        raise SystemExit(f"archive copy size mismatch: {source} => {target}")
    return True


def newest_flv(path: Path) -> Path | None:
    files = [item for item in path.glob("*.flv") if item.is_file()]
    if not files:
        return None
    return max(files, key=lambda item: item.stat().st_mtime_ns)


def session_dir(root: Path, args: argparse.Namespace) -> Path:
    return root / safe_component(args.event) / safe_component(args.zone) / safe_component(f"{args.date} {args.session_name}")


def acquire_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(f"another adaptive training recorder is already running: {path}")
    lock.write(str(os.getpid()))
    lock.truncate()
    lock.flush()
    return lock


def parse_stop_at(value: str, timezone: ZoneInfo) -> float | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed.timestamp()


def sleep_until_next_poll(seconds: int, should_stop) -> None:
    deadline = time.monotonic() + max(1, seconds)
    while time.monotonic() < deadline:
        if should_stop():
            return
        time.sleep(min(1, deadline - time.monotonic()))


def close_log(recorder: Recorder) -> None:
    try:
        recorder.log_handle.close()
    except Exception:
        pass


def safe_component(name: str) -> str:
    safe = "".join("_" if ch in '/\\:*?"<>|' else ch for ch in name.strip())
    return safe or "unknown"


if __name__ == "__main__":
    raise SystemExit(local_log.run_logged(SERVICE, main))
