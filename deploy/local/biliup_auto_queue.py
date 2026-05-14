#!/usr/bin/env python3
import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

import local_log


SERVICE = "biliup-auto-queue"
DEFAULT_ZONE = "南部赛区"
DEFAULT_LIMIT = "7"
DEFAULT_BILIUP = "deploy/local/biliup_direct_docker.sh"
DEFAULT_RECORDS_ROOT = "/mnt/PC801/rm-monitor/records"
DEFAULT_BILI_SUBMIT = "web"
DEFAULT_SEASON_NAME = os.environ.get("RM_MONITOR_BILI_SEASON_NAME", "")
DEFAULT_SEASON_ID = os.environ.get("RM_MONITOR_BILI_SEASON_ID", "")
DEFAULT_SECTION_ID = os.environ.get("RM_MONITOR_BILI_SECTION_ID", "")
DEFAULT_LOCK_FILE = Path(__file__).resolve().parents[2] / "logs" / "biliup-auto-queue.lock"
RATE_LIMIT_RETRY_SECONDS = 45 * 60
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


@dataclass
class Candidate:
    match_id: str
    zone: str
    order: int
    artifacts: int
    bitable_records: int


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a single-lane automatic Bilibili upload queue for finished RM matches.")
    parser.add_argument("--zone", default=DEFAULT_ZONE)
    parser.add_argument("--start-order", type=int, default=1)
    parser.add_argument("--max-order", type=int)
    parser.add_argument("--limit", default=DEFAULT_LIMIT)
    parser.add_argument("--biliup", default=DEFAULT_BILIUP)
    parser.add_argument(
        "--bili-submit",
        default=DEFAULT_BILI_SUBMIT,
        choices=["app", "web", "b-cut-android"],
        help="biliup final submit API.",
    )
    parser.add_argument("--records-root", default=DEFAULT_RECORDS_ROOT)
    parser.add_argument("--season-name", default=DEFAULT_SEASON_NAME)
    parser.add_argument("--season-id", default=DEFAULT_SEASON_ID)
    parser.add_argument("--section-id", default=DEFAULT_SECTION_ID)
    parser.add_argument("--namespace", default="rm-monitor")
    parser.add_argument("--postgres", default="deployment/postgres")
    parser.add_argument("--db-user", default="rm_monitor")
    parser.add_argument("--db-name", default="rm_monitor")
    parser.add_argument("--cookie", default="cookies.json")
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--settle-seconds", type=int, default=300)
    parser.add_argument("--lock-file", default=str(DEFAULT_LOCK_FILE))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    lock = acquire_lock(Path(args.lock_file))
    failures: dict[str, float] = {}
    local_log.log_event(
        SERVICE,
        "INFO",
        "auto queue started",
        zone=args.zone,
        start_order=args.start_order,
        max_order=args.max_order,
        limit=args.limit,
        bili_submit=args.bili_submit,
        biliup=args.biliup,
        settle_seconds=args.settle_seconds,
        dry_run=args.dry_run,
    )

    try:
        while True:
            if upload_process_running():
                local_log.log_event(SERVICE, "INFO", "another biliup upload is running; waiting")
                if args.once:
                    return 0
                time.sleep(args.poll_seconds)
                continue

            candidate = next_candidate(args)
            if candidate is None:
                local_log.log_event(SERVICE, "INFO", "no finished match is ready for Bilibili upload")
                if args.once:
                    return 0
                time.sleep(args.poll_seconds)
                continue

            retry_at = failures.get(candidate.match_id, 0)
            if retry_at > time.time():
                if args.once:
                    return 0
                time.sleep(min(args.poll_seconds, max(1, int(retry_at - time.time()))))
                continue

            try:
                code = handle_candidate(args, candidate)
            except SystemExit as exc:
                code, detail = local_log.normalize_exit(exc.code)
                if code == 0 and detail:
                    code = 1
                local_log.log_event(
                    SERVICE,
                    "ERROR" if code else "INFO",
                    "candidate workflow failed" if code else "candidate workflow finished",
                    match_id=candidate.match_id,
                    order=candidate.order,
                    exit_code=code,
                    detail=detail,
                )
            except Exception as exc:
                code = 1
                local_log.log_event(
                    SERVICE,
                    "ERROR",
                    "candidate workflow crashed",
                    match_id=candidate.match_id,
                    order=candidate.order,
                    exit_code=code,
                    error=str(exc),
                    traceback=traceback.format_exc(limit=6),
                )
            if code != 0:
                failures[candidate.match_id] = time.time() + (RATE_LIMIT_RETRY_SECONDS if code == 75 else 10 * 60)
            if args.once:
                return code
            time.sleep(2 if code == 0 else args.poll_seconds)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def acquire_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(f"another auto queue is already running: {path}")
    lock.write(str(os.getpid()))
    lock.truncate()
    lock.flush()
    return lock


def upload_process_running() -> bool:
    result = subprocess.run(["ps", "-eo", "pid=,args="], text=True, capture_output=True)
    if result.returncode != 0:
        return False
    own_pid = os.getpid()
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid == own_pid:
            continue
        cmd = parts[1]
        if "biliup_auto_queue.py" in cmd:
            continue
        if "biliup_upload_match.py" in cmd and "--submit" in cmd:
            return True
        if "/home/maverick/.local/bin/biliup" in cmd and " upload " in f" {cmd} ":
            return True
    return False


def next_candidate(args: argparse.Namespace) -> Candidate | None:
    max_order = "" if args.max_order is None else f'and m."order" <= {int(args.max_order)}'
    settle_seconds = max(0, int(args.settle_seconds))
    query = f"""
        select
            m.id,
            m.zone,
            m."order",
            count(distinct ma.id) as artifacts,
            count(distinct nullif(ut.bitable_record_id, '')) as bitable_records
        from matches m
        join match_rounds mr on mr.match_rounds = m.id
        join record_tasks rt on rt.match_round_record_tasks = mr.id
        join media_artifacts ma on ma.record_task_media_artifacts = rt.id
        left join upload_tasks ut on ut.media_artifact_upload_task = ma.id
        where m.zone = '{sql_escape(args.zone)}'
          and m.latest_status = 'DONE'
          and m."order" >= {int(args.start_order)}
          {max_order}
          and ma.kind = 'source'
          and ma.status = 'AVAILABLE'
          and position('__part' in rt.role) = 0
        group by m.id
        having count(distinct ma.id) > 0
           and max(coalesce(rt.completed_at, ma.created_at)) <= now() - interval '{settle_seconds} seconds'
        order by m."order"
        limit 1;
    """
    rows = psql(args, query)
    if not rows:
        return None
    row = rows[0]
    return Candidate(row[0], row[1], int(row[2]), int(row[3]), int(row[4]))


def handle_candidate(args: argparse.Namespace, candidate: Candidate) -> int:
    local_log.log_event(
        SERVICE,
        "INFO",
        "candidate selected",
        match_id=candidate.match_id,
        zone=candidate.zone,
        order=candidate.order,
        artifacts=candidate.artifacts,
        bitable_records=candidate.bitable_records,
    )
    if candidate.bitable_records < candidate.artifacts:
        local_log.log_event(
            SERVICE,
            "WARNING",
            "candidate is waiting for Feishu Bitable records",
            match_id=candidate.match_id,
            order=candidate.order,
            artifacts=candidate.artifacts,
            bitable_records=candidate.bitable_records,
        )
        return 0

    plan = upload_plan(args, candidate)
    title = str(plan.get("title") or "")
    existing_bvid = find_existing_bvid(args, title) if title else ""
    if existing_bvid:
        command = base_command(args, candidate) + ["--biliup", args.biliup, "--add-existing-bvid", existing_bvid, "--submit"]
        action = "complete existing Bilibili upload"
        bvid = existing_bvid
    else:
        command = base_command(args, candidate) + [
            "--limit",
            str(args.limit),
            "--biliup",
            args.biliup,
            "--bili-submit",
            args.bili_submit,
            "--submit",
        ]
        action = "upload match to Bilibili"
        bvid = ""

    local_log.log_event(
        SERVICE,
        "INFO",
        action,
        match_id=candidate.match_id,
        order=candidate.order,
        title=title,
        bvid=bvid,
        command=shell_join(command),
        dry_run=args.dry_run,
    )
    if args.dry_run:
        print(shell_join(command))
        return 0
    code = run_streaming(command)
    local_log.log_event(
        SERVICE,
        "INFO" if code == 0 else "ERROR",
        "candidate workflow finished" if code == 0 else "candidate workflow failed",
        match_id=candidate.match_id,
        order=candidate.order,
        exit_code=code,
    )
    return code


def upload_plan(args: argparse.Namespace, candidate: Candidate) -> dict:
    command = (
        base_command(args, candidate)
        + [
            "--limit",
            str(args.limit),
            "--biliup",
            args.biliup,
            "--bili-submit",
            args.bili_submit,
            "--no-archive-after-upload",
            "--no-feishu-link",
            "--no-feishu-topic-reply",
        ]
    )
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or result.stdout.strip() or "failed to build upload plan")
    decoder = json.JSONDecoder()
    return decoder.raw_decode(result.stdout.lstrip())[0]


def base_command(args: argparse.Namespace, candidate: Candidate) -> list[str]:
    command = [
        sys.executable,
        str(Path(__file__).with_name("biliup_upload_match.py")),
        "--match-id",
        candidate.match_id,
        "--namespace",
        args.namespace,
        "--postgres",
        args.postgres,
        "--db-user",
        args.db_user,
        "--db-name",
        args.db_name,
        "--records-root",
        args.records_root,
        "--cookie",
        args.cookie,
    ]
    if args.season_name:
        command.extend(["--season-name", args.season_name])
    if args.season_id:
        command.extend(["--season-id", str(args.season_id)])
    if args.section_id:
        command.extend(["--section-id", str(args.section_id)])
    return command


def find_existing_bvid(args: argparse.Namespace, title: str) -> str:
    result = subprocess.run([args.biliup, "--user-cookie", args.cookie, "list"], text=True, capture_output=True)
    if result.returncode != 0:
        local_log.log_event(
            SERVICE,
            "WARNING",
            "failed to list Bilibili archives",
            exit_code=result.returncode,
            detail=result.stderr.strip() or result.stdout.strip(),
        )
        return ""
    for line in result.stdout.splitlines():
        clean = ANSI_RE.sub("", line)
        parts = clean.split("\t")
        if len(parts) >= 2 and parts[1].strip() == title:
            return parts[0].strip()
    return ""


def run_streaming(command: list[str]) -> int:
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="")
    return process.wait()


def psql(args: argparse.Namespace, query: str) -> list[list[str]]:
    command = [
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
    ]
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or result.stdout.strip() or "psql failed")
    return [line.split("\t") for line in result.stdout.splitlines() if line.strip()]


def sql_escape(value: str) -> str:
    return value.replace("'", "''")


def shell_join(command: list[str]) -> str:
    return " ".join(quote_shell(part) for part in command)


def quote_shell(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:=+-]+", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


if __name__ == "__main__":
    raise SystemExit(local_log.run_logged(SERVICE, main))
