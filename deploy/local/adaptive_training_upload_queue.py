#!/usr/bin/env python3
import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import local_log


DEFAULT_EVENT = "RMUC 2026超级对抗赛"
DEFAULT_ZONE = "北部赛区"
DEFAULT_SESSION_NAME = "适应性训练"
DEFAULT_TARGET_ROOT = "/mnt/server_data/rm-monitor/records"
DEFAULT_COOKIE = "cookies.json"
DEFAULT_TAGS = "RoboMaster,RMUC2026,机器人竞赛,适应性训练"
DEFAULT_REPOST_SOURCE = "RoboMaster 官方直播"
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_STATE_DIR = Path(__file__).resolve().parents[2] / "logs"
DEFAULT_UPLOAD_LOCK_FILE = DEFAULT_STATE_DIR / "biliup-upload-global.lock"
DEFAULT_SEASON_NAME = os.environ.get("RM_MONITOR_ADAPTIVE_BILI_SEASON_NAME", "RMUC2026北部赛区全视角录制")
DEFAULT_SEASON_ID = int(os.environ.get("RM_MONITOR_ADAPTIVE_BILI_SEASON_ID", "8209772"))
DEFAULT_SECTION_ID = int(os.environ.get("RM_MONITOR_ADAPTIVE_BILI_SECTION_ID", "9124491"))
SERVICE = os.environ.get("RM_MONITOR_SERVICE_NAME", "adaptive-training-upload")


@dataclass
class HourGroup:
    key: str
    hour_start: datetime
    hour_end: datetime
    files: list[Path]


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload adaptive training hourly archives to Bilibili.")
    parser.add_argument("--zone", default=DEFAULT_ZONE)
    parser.add_argument("--event", default=DEFAULT_EVENT)
    parser.add_argument("--session-name", default=DEFAULT_SESSION_NAME)
    parser.add_argument("--date", default="")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument("--target-root", default=DEFAULT_TARGET_ROOT)
    parser.add_argument("--state-file", default="")
    parser.add_argument("--upload-lock-file", default=str(DEFAULT_UPLOAD_LOCK_FILE))
    parser.add_argument("--cookie", default=DEFAULT_COOKIE)
    parser.add_argument("--biliup", default="biliup")
    parser.add_argument("--bili-submit", default="web", choices=["app", "web", "b-cut-android"])
    parser.add_argument("--tid", default="171")
    parser.add_argument("--copyright", default="2")
    parser.add_argument("--source", default=DEFAULT_REPOST_SOURCE)
    parser.add_argument("--tags", default=DEFAULT_TAGS)
    parser.add_argument("--limit", default="7")
    parser.add_argument("--line", default="")
    parser.add_argument("--is-only-self", default="0", help="Passed to biliup --is-only-self. Use 0 for public, 1 for private-only.")
    parser.add_argument("--season-name", default=DEFAULT_SEASON_NAME)
    parser.add_argument("--season-id", type=int, default=DEFAULT_SEASON_ID)
    parser.add_argument("--section-id", type=int, default=DEFAULT_SECTION_ID)
    parser.add_argument("--no-season", action="store_true", help="Do not add uploaded archives to a Bilibili collection.")
    parser.add_argument("--skip-existing-reconcile", action="store_true", help="Do not repair public/collection state for already uploaded records.")
    parser.add_argument("--poll-seconds", type=int, default=300)
    parser.add_argument("--settle-seconds", type=int, default=300)
    parser.add_argument("--hour-delay-seconds", type=int, default=600)
    parser.add_argument("--retry-seconds", type=int, default=1800)
    parser.add_argument("--retry-stale-uploading-seconds", type=int, default=21600)
    parser.add_argument("--stop-at", default="", help="Local ISO timestamp. Example: 2026-05-21T06:00:00+08:00")
    parser.add_argument("--submit", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    timezone = ZoneInfo(args.timezone)
    args.date = args.date or datetime.now(timezone).strftime("%Y-%m-%d")
    stop_at = parse_stop_at(args.stop_at, timezone)
    session = session_dir(Path(args.target_root), args)
    state_path = Path(args.state_file) if args.state_file else DEFAULT_STATE_DIR / f"{SERVICE}-{safe_component(args.zone)}-{args.date}.json"

    local_log.log_event(
        SERVICE,
        "INFO",
        "adaptive training upload queue started",
        zone=args.zone,
        date=args.date,
        session_dir=str(session),
        state_file=str(state_path),
        submit=args.submit,
        is_only_self=args.is_only_self,
    )

    while True:
        if stop_at and time.time() >= stop_at:
            local_log.log_event(SERVICE, "INFO", "stop time reached", stop_at=args.stop_at)
            return 0
        state = load_state(state_path)
        if args.submit and not args.skip_existing_reconcile:
            reconcile_uploaded_records(args, state_path, state)
        groups = discover_hour_groups(session, timezone)
        eligible = [
            group
            for group in groups
            if group_is_eligible(args, group, state, timezone)
        ]
        if args.dry_run or not args.submit:
            print_plan(args, session, state_path, groups, eligible, state)
            return 0
        if eligible:
            group = eligible[0]
            upload_lock = try_acquire_upload_lock(Path(args.upload_lock_file))
            if upload_lock is None:
                local_log.log_event(
                    SERVICE,
                    "INFO",
                    "global Bilibili upload lane is busy; waiting",
                    hour=group.key,
                    lock_file=args.upload_lock_file,
                )
            else:
                try:
                    upload_group(args, state_path, state, group)
                finally:
                    release_lock(upload_lock)
        if args.once:
            return 0
        sleep_for(args.poll_seconds)


def discover_hour_groups(session: Path, timezone: ZoneInfo) -> list[HourGroup]:
    by_key: dict[str, tuple[datetime, list[Path]]] = {}
    if not session.exists():
        return []
    for path in sorted(session.rglob("*.flv")):
        if not path.is_file() or path.stat().st_size <= 0:
            continue
        started = parse_segment_start(path.name, timezone)
        if started is None:
            continue
        hour_start = started.replace(minute=0, second=0, microsecond=0)
        key = hour_start.strftime("%Y%m%d_%H")
        if key not in by_key:
            by_key[key] = (hour_start, [])
        by_key[key][1].append(path)

    groups: list[HourGroup] = []
    for key, (hour_start, files) in by_key.items():
        groups.append(
            HourGroup(
                key=key,
                hour_start=hour_start,
                hour_end=hour_start + timedelta(hours=1),
                files=sorted(files, key=part_sort_key),
            )
        )
    return sorted(groups, key=lambda group: group.hour_start)


def group_is_eligible(args: argparse.Namespace, group: HourGroup, state: dict, timezone: ZoneInfo) -> bool:
    record = state.get("uploads", {}).get(group.key, {})
    now = datetime.now(timezone)
    now_ts = time.time()
    if record.get("status") in {"uploaded", "cancelled", "skipped"}:
        return False
    if record.get("status") == "uploading":
        started_at = float(record.get("started_at_ts") or 0)
        if now_ts - started_at < args.retry_stale_uploading_seconds:
            return False
    if record.get("status") == "failed":
        attempted_at = float(record.get("updated_at_ts") or 0)
        if now_ts - attempted_at < args.retry_seconds:
            return False
    if now < group.hour_end + timedelta(seconds=args.hour_delay_seconds):
        return False
    for path in group.files:
        if now_ts - path.stat().st_mtime < args.settle_seconds:
            return False
    return True


def upload_group(args: argparse.Namespace, state_path: Path, state: dict, group: HourGroup) -> None:
    title = build_title(args, group)
    desc = build_description(args, group)
    command = build_command(args, title, desc, group.files)
    attempts = int(state.get("uploads", {}).get(group.key, {}).get("attempts") or 0) + 1
    set_state(
        state_path,
        state,
        group.key,
        {
            "status": "uploading",
            "attempts": attempts,
            "started_at": timestamp(),
            "started_at_ts": time.time(),
            "title": title,
            "files": [str(path) for path in group.files],
            "sizes": [path.stat().st_size for path in group.files],
        },
    )
    local_log.log_event(
        SERVICE,
        "INFO",
        "adaptive training upload started",
        hour=group.key,
        title=title,
        files=len(group.files),
        is_only_self=args.is_only_self,
    )
    code, output = run_streaming(command)
    bvid = find_bvid_in_text(output)
    if code != 0 and not bvid:
        set_state(
            state_path,
            state,
            group.key,
            {
                "status": "failed",
                "attempts": attempts,
                "updated_at": timestamp(),
                "updated_at_ts": time.time(),
                "title": title,
                "exit_code": code,
                "last_output_tail": output[-4000:],
            },
        )
        local_log.log_event(SERVICE, "ERROR", "adaptive training upload failed", hour=group.key, title=title, exit_code=code)
        return

    # Persist submission identity before any Bilibili post-processing.  A
    # collection/edit API failure must never cause the large video to be
    # submitted a second time on the next queue pass.
    submitted_record = {
        "status": "uploaded",
        "attempts": attempts,
        "updated_at": timestamp(),
        "updated_at_ts": time.time(),
        "title": title,
        "bvid": bvid,
        "files": [str(path) for path in group.files],
        "sizes": [path.stat().st_size for path in group.files],
        "is_only_self": args.is_only_self,
        "submit_exit_code": code,
    }
    set_state(state_path, state, group.key, submitted_record)
    if code != 0:
        local_log.log_event(
            SERVICE,
            "WARN",
            "adaptive training submit returned an error after yielding BVID; duplicate retry suppressed",
            hour=group.key,
            title=title,
            bvid=bvid,
            exit_code=code,
        )
    postprocess = postprocess_uploaded_bvid(args, bvid) if bvid else {}
    set_state(
        state_path,
        state,
        group.key,
        {
            **submitted_record,
            "updated_at": timestamp(),
            "updated_at_ts": time.time(),
            **postprocess,
        },
    )
    local_log.log_event(
        SERVICE,
        "INFO",
        "adaptive training upload completed",
        hour=group.key,
        title=title,
        bvid=bvid,
        season_status=postprocess.get("season_status", ""),
        public_status=postprocess.get("public_status", ""),
    )


def build_command(args: argparse.Namespace, title: str, desc: str, files: list[Path]) -> list[str]:
    command = [
        args.biliup,
        "--user-cookie",
        args.cookie,
        "upload",
        "--copyright",
        str(args.copyright),
        "--tid",
        str(args.tid),
        "--title",
        title,
        "--desc",
        desc,
        "--tag",
        args.tags,
        "--limit",
        str(args.limit),
        "--submit",
        args.bili_submit,
        "--is-only-self",
        str(args.is_only_self),
    ]
    if args.source:
        command.extend(["--source", args.source])
    if args.line:
        command.extend(["--line", args.line])
    command.extend(str(path) for path in files)
    return command


def build_title(args: argparse.Namespace, group: HourGroup) -> str:
    return f"[{args.zone}适应性训练] {group.hour_start:%Y-%m-%d %H:%M}-{group.hour_end:%H:%M} 全视角"


def build_description(args: argparse.Namespace, group: HourGroup) -> str:
    visibility = "仅自己可见" if is_private_upload(args) else "公开"
    return "\n".join(
        [
            build_title(args, group),
            "",
            f"赛事：{args.event}",
            f"赛区：{args.zone}",
            f"类型：{args.session_name}",
            f"时间：{group.hour_start:%Y-%m-%d %H:%M} - {group.hour_end:%H:%M}",
            f"可见性：{visibility}",
            "分P：按视角/片段的原始FLV",
        ]
    )


def print_plan(args: argparse.Namespace, session: Path, state_path: Path, groups: list[HourGroup], eligible: list[HourGroup], state: dict) -> None:
    payload = {
        "zone": args.zone,
        "date": args.date,
        "session_dir": str(session),
        "state_file": str(state_path),
        "submit": args.submit,
        "is_only_self": args.is_only_self,
        "season": None
        if args.no_season
        else {
            "name": args.season_name,
            "season_id": args.season_id,
            "section_id": args.section_id,
        },
        "groups": [
            {
                "hour": group.key,
                "title": build_title(args, group),
                "files": len(group.files),
                "status": state.get("uploads", {}).get(group.key, {}).get("status", "pending"),
                "eligible": any(item.key == group.key for item in eligible),
            }
            for group in groups
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if eligible:
        print()
        print(shell_join(build_command(args, build_title(args, eligible[0]), build_description(args, eligible[0]), eligible[0].files)))


def reconcile_uploaded_records(args: argparse.Namespace, state_path: Path, state: dict) -> None:
    for key, record in sorted(state.get("uploads", {}).items()):
        if record.get("status") != "uploaded" or not record.get("bvid"):
            continue
        if record_needs_reconcile(args, record):
            updates = postprocess_uploaded_bvid(args, record["bvid"])
            if updates:
                merged = {**record, **updates, "reconciled_at": timestamp(), "reconciled_at_ts": time.time()}
                set_state(state_path, state, key, merged)


def record_needs_reconcile(args: argparse.Namespace, record: dict) -> bool:
    needs_public = not is_private_upload(args) and record.get("public_status") != "public"
    needs_season = not args.no_season and record.get("season_status") != "added"
    return needs_public or needs_season


def is_private_upload(args: argparse.Namespace) -> bool:
    return str(args.is_only_self).strip().lower() not in {"0", "false", "no", "off", "public"}


def postprocess_uploaded_bvid(args: argparse.Namespace, bvid: str) -> dict:
    updates: dict = {}
    if not bvid:
        return updates
    if not is_private_upload(args):
        try:
            ensure_archive_public(args, bvid)
            updates.update({"public_status": "public", "is_only_self": "0"})
        except Exception as exc:
            updates.update({"public_status": "failed", "public_error": str(exc)[-1000:]})
            local_log.log_event(SERVICE, "ERROR", "failed to make adaptive training archive public", bvid=bvid, error=str(exc))
    if not args.no_season:
        try:
            add_bvid_to_season(args, bvid)
            updates.update(
                {
                    "season_status": "added",
                    "season_name": args.season_name,
                    "season_id": args.season_id,
                    "section_id": args.section_id,
                }
            )
        except Exception as exc:
            updates.update({"season_status": "failed", "season_error": str(exc)[-1000:]})
            local_log.log_event(SERVICE, "ERROR", "failed to add adaptive training archive to season", bvid=bvid, error=str(exc))
    return updates


def ensure_archive_public(args: argparse.Namespace, bvid: str) -> None:
    detail = load_archive_detail(args, bvid)
    archive = detail.get("archive") or {}
    desc = public_description(archive.get("desc") or "")
    if int(archive.get("is_only_self") or 0) == 0 and desc == (archive.get("desc") or ""):
        return
    session, csrf = load_bili_session(Path(args.cookie))
    payload = build_archive_edit_payload(args, detail, desc, is_only_self=0)
    resp = session.post(
        "https://member.bilibili.com/x/vu/web/edit",
        params={"csrf": csrf},
        json=payload,
        headers={"Content-Type": "application/json; charset=UTF-8"},
        timeout=20,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 0:
        raise RuntimeError(f"edit archive failed: {body}")


def public_description(desc: str) -> str:
    if "可见性：仅自己可见" in desc:
        return desc.replace("可见性：仅自己可见", "可见性：公开")
    return desc


def build_archive_edit_payload(args: argparse.Namespace, detail: dict, desc: str, is_only_self: int) -> dict:
    archive = detail.get("archive") or {}
    videos = detail.get("videos") or []
    aid = archive.get("aid")
    title = archive.get("title")
    if not aid or not title or not videos:
        raise RuntimeError(f"cannot edit archive without aid/title/videos: aid={aid} title={title} videos={len(videos)}")
    cover = archive.get("cover") or ""
    if cover.startswith("http:"):
        cover = cover[len("http:") :]
    if cover.startswith("https:"):
        cover = cover[len("https:") :]
    return {
        "copyright": int(archive.get("copyright") or args.copyright),
        "source": archive.get("source") or args.source,
        "tid": int(archive.get("tid") or args.tid),
        "cover": cover,
        "title": title,
        "desc_format_id": int(archive.get("desc_format_id") or 0),
        "desc": desc,
        "desc_v2": [{"raw_text": desc, "biz_id": "", "type": 1}],
        "dynamic": archive.get("dynamic") or "",
        "tag": archive.get("tag") or args.tags,
        "videos": [
            {
                "filename": video.get("filename"),
                "title": video.get("title") or "",
                "desc": video.get("desc") or "",
            }
            for video in videos
            if video.get("filename")
        ],
        "dtime": archive.get("dtime") or None,
        "subtitle": {"open": 0, "lan": ""},
        "dolby": int(archive.get("dolby") or 0),
        "hires": int(archive.get("hires") or 0),
        "no_reprint": int(archive.get("no_reprint") or 0),
        "is_only_self": int(is_only_self),
        "charging_pay": int(archive.get("charging_pay") or 0),
        "aid": int(aid),
    }


def add_bvid_to_season(args: argparse.Namespace, bvid: str) -> None:
    command = [
        sys.executable,
        str(Path(__file__).with_name("biliup_upload_match.py")),
        "--add-existing-bvid",
        bvid,
        "--cookie",
        args.cookie,
        "--biliup",
        args.biliup,
        "--season-name",
        args.season_name,
        "--season-id",
        str(args.season_id),
        "--section-id",
        str(args.section_id),
        "--no-feishu-link",
        "--no-feishu-topic-reply",
        "--no-archive-after-upload",
        "--submit",
    ]
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"{shell_join(command)} failed"
        if "20080" in detail or "已存在在合集中" in detail:
            return
        raise RuntimeError(detail)


def load_archive_detail(args: argparse.Namespace, bvid: str) -> dict:
    result = subprocess.run(
        [args.biliup, "--user-cookie", args.cookie, "show", bvid],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"biliup show {bvid} failed")
    return parse_json_from_output(result.stdout, f"biliup show {bvid}")


def parse_json_from_output(output: str, label: str) -> dict:
    decoder = json.JSONDecoder()
    for index, char in enumerate(output):
        if char not in "{[":
            continue
        try:
            value, _ = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise RuntimeError(f"{label} did not return JSON")


def load_bili_session(cookie_path: Path) -> tuple[requests.Session, str]:
    with cookie_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    cookies = {item["name"]: item["value"] for item in data.get("cookie_info", {}).get("cookies", [])}
    csrf = cookies.get("bili_jct", "")
    if not csrf:
        raise RuntimeError(f"bili_jct not found in {cookie_path}")
    session = requests.Session()
    session.cookies.update(cookies)
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://member.bilibili.com/platform/upload-manager/article",
        }
    )
    return session, csrf


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "uploads": {}}
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("version", 1)
    data.setdefault("uploads", {})
    return data


def try_acquire_upload_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return None
    lock.seek(0)
    lock.truncate()
    lock.write(f"{SERVICE} {os.getpid()}\n")
    lock.flush()
    return lock


def release_lock(lock) -> None:
    fcntl.flock(lock, fcntl.LOCK_UN)
    lock.close()


def set_state(path: Path, state: dict, key: str, record: dict) -> None:
    state.setdefault("version", 1)
    state.setdefault("uploads", {})[key] = record
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{time.time_ns()}")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def parse_segment_start(name: str, timezone: ZoneInfo) -> datetime | None:
    matches = re.findall(r"(\d{8})_(\d{6})", name)
    if not matches:
        return None
    date_part, time_part = matches[-1]
    try:
        parsed = datetime.strptime(f"{date_part}_{time_part}", "%Y%m%d_%H%M%S")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone)


def part_sort_key(path: Path) -> tuple[str, str]:
    role = path.parent.name
    return role, path.name


def session_dir(root: Path, args: argparse.Namespace) -> Path:
    return root / safe_component(args.event) / safe_component(args.zone) / safe_component(f"{args.date} {args.session_name}")


def parse_stop_at(value: str, timezone: ZoneInfo) -> float | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed.timestamp()


def find_bvid_in_text(text: str) -> str:
    match = re.search(r"\bBV[0-9A-Za-z]{10,}\b", text)
    return match.group(0) if match else ""


def run_streaming(command: list[str]) -> tuple[int, str]:
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=os.environ.copy())
    assert process.stdout is not None
    output: list[str] = []
    for line in process.stdout:
        output.append(line)
        print(line, end="")
    return process.wait(), "".join(output)


def shell_join(command: list[str]) -> str:
    import shlex

    return " ".join(shlex.quote(part) for part in command)


def timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def sleep_for(seconds: int) -> None:
    time.sleep(max(1, seconds))


def safe_component(name: str) -> str:
    safe = "".join("_" if ch in '/\\:*?"<>|' else ch for ch in name.strip())
    return safe or "unknown"


if __name__ == "__main__":
    raise SystemExit(local_log.run_logged(SERVICE, main))
