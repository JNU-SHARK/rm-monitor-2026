#!/usr/bin/env python3
import argparse
import fcntl
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import local_log


SERVICE = os.environ.get("RM_MONITOR_SERVICE_NAME", "archive-auto-queue")
DEFAULT_ZONE = os.environ.get("RM_MONITOR_ZONE", "全国赛")
DEFAULT_RECORDS_ROOT = "/mnt/PC801/rm-monitor/records"
DEFAULT_ARCHIVE_TARGET_ROOT = "/mnt/server_data/rm-monitor/records"
DEFAULT_LOCK_FILE = Path(__file__).resolve().parents[2] / "logs" / "archive-auto-queue.lock"


@dataclass
class Candidate:
    match_id: str
    zone: str
    order: int
    artifacts: int


def main() -> int:
    parser = argparse.ArgumentParser(description="Copy finished RM match artifacts to long-term storage without waiting for Bilibili upload.")
    parser.add_argument("--zone", default=DEFAULT_ZONE)
    parser.add_argument("--start-order", type=int, default=1)
    parser.add_argument("--max-order", type=int)
    parser.add_argument("--records-root", default=DEFAULT_RECORDS_ROOT)
    parser.add_argument("--archive-target-root", default=DEFAULT_ARCHIVE_TARGET_ROOT)
    parser.add_argument("--namespace", default="rm-monitor")
    parser.add_argument("--postgres", default="deployment/postgres")
    parser.add_argument("--db-user", default="rm_monitor")
    parser.add_argument("--db-name", default="rm_monitor")
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--settle-seconds", type=int, default=300)
    parser.add_argument("--retry-seconds", type=int, default=600)
    parser.add_argument("--lock-file", default=str(DEFAULT_LOCK_FILE))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--lark-app-id", default="")
    parser.add_argument("--lark-app-secret", default="")
    parser.add_argument("--bitable-app-token", default="")
    parser.add_argument("--lark-secret-name", default="rm-monitor-lark")
    args = parser.parse_args()

    lock = acquire_lock(Path(args.lock_file))
    completed: set[str] = set()
    failures: dict[str, float] = {}
    local_log.log_event(
        SERVICE,
        "INFO",
        "archive queue started",
        zone=args.zone,
        start_order=args.start_order,
        max_order=args.max_order,
        settle_seconds=args.settle_seconds,
        records_root=args.records_root,
        archive_target_root=args.archive_target_root,
        dry_run=args.dry_run,
    )

    try:
        while True:
            candidate = next_candidate(args, completed, failures)
            if candidate is None:
                local_log.log_event(SERVICE, "INFO", "no finished match is ready for long-term archive")
                if args.once:
                    return 0
                time.sleep(args.poll_seconds)
                continue

            code = handle_candidate(args, candidate)
            if code == 0:
                completed.add(candidate.match_id)
            else:
                failures[candidate.match_id] = time.time() + max(1, int(args.retry_seconds))
                send_archive_failure_alert(args, candidate, code)
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
        raise SystemExit(f"another archive queue is already running: {path}")
    lock.write(str(os.getpid()))
    lock.truncate()
    lock.flush()
    return lock


def next_candidate(args: argparse.Namespace, completed: set[str], failures: dict[str, float]) -> Candidate | None:
    max_order = "" if args.max_order is None else f'and m."order" <= {int(args.max_order)}'
    completed_filter = ""
    if completed:
        ids = ", ".join(f"'{sql_escape(match_id)}'" for match_id in sorted(completed))
        completed_filter = f"and m.id not in ({ids})"
    retry_filter = ""
    blocked = [match_id for match_id, retry_at in failures.items() if retry_at > time.time()]
    if blocked:
        ids = ", ".join(f"'{sql_escape(match_id)}'" for match_id in sorted(blocked))
        retry_filter = f"and m.id not in ({ids})"
    settle_seconds = max(0, int(args.settle_seconds))
    query = f"""
        select
            m.id,
            m.zone,
            m."order",
            count(distinct ma.id) as artifacts
        from matches m
        join match_rounds mr on mr.match_rounds = m.id
        join record_tasks rt on rt.match_round_record_tasks = mr.id
        join media_artifacts ma on ma.record_task_media_artifacts = rt.id
        where m.zone = '{sql_escape(args.zone)}'
          and m.latest_status = 'DONE'
          and m."order" >= {int(args.start_order)}
          {max_order}
          {completed_filter}
          {retry_filter}
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
    return Candidate(row[0], row[1], int(row[2]), int(row[3]))


def handle_candidate(args: argparse.Namespace, candidate: Candidate) -> int:
    command = archive_command(args, candidate)
    local_log.log_event(
        SERVICE,
        "INFO",
        "archive candidate selected",
        match_id=candidate.match_id,
        zone=candidate.zone,
        order=candidate.order,
        artifacts=candidate.artifacts,
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
        "archive candidate finished" if code == 0 else "archive candidate failed",
        match_id=candidate.match_id,
        order=candidate.order,
        exit_code=code,
    )
    return code


def archive_command(args: argparse.Namespace, candidate: Candidate) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).with_name("archive_match_artifacts.py")),
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
        "--source-root",
        args.records_root,
        "--target-root",
        args.archive_target_root,
        "--submit",
    ]


def send_archive_failure_alert(args: argparse.Namespace, candidate: Candidate, exit_code: int) -> None:
    try:
        from biliup_upload_match import send_feishu_alert

        send_feishu_alert(
            args,
            "长期归档失败",
            (
                f"比赛：{candidate.zone} 第{candidate.order}场\n"
                f"返回码：{exit_code}\n"
                f"源目录：{args.records_root}\n"
                f"长期目录：{args.archive_target_root}\n"
                "请立即提醒席伟杰修复。源文件不会被删除。"
            ),
        )
    except Exception as exc:
        local_log.log_event(
            SERVICE,
            "ERROR",
            "failed to send archive failure alert",
            match_id=candidate.match_id,
            order=candidate.order,
            error=str(exc),
        )


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
