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


SERVICE = os.environ.get("RM_MONITOR_SERVICE_NAME", "biliup-auto-queue")
DEFAULT_ZONE = os.environ.get("RM_MONITOR_ZONE", "全国赛")
DEFAULT_LIMIT = "7"
DEFAULT_BILIUP = "deploy/local/biliup_direct_docker.sh"
DEFAULT_RECORDS_ROOT = "/mnt/PC801/rm-monitor/records"
DEFAULT_BILI_SUBMIT = "web"
DEFAULT_TITLE_SUFFIX = os.environ.get("RM_MONITOR_TITLE_SUFFIX", "")
DEFAULT_SEASON_NAME = os.environ.get("RM_MONITOR_BILI_SEASON_NAME", "")
DEFAULT_SEASON_ID = os.environ.get("RM_MONITOR_BILI_SEASON_ID", "")
DEFAULT_SECTION_ID = os.environ.get("RM_MONITOR_BILI_SECTION_ID", "")
DEFAULT_LOCK_FILE = Path(__file__).resolve().parents[2] / "logs" / "biliup-auto-queue.lock"
DEFAULT_UPLOAD_LOCK_FILE = Path(__file__).resolve().parents[2] / "logs" / "biliup-upload-global.lock"
DEFAULT_SUBMISSION_STATE_FILE = Path(__file__).resolve().parents[2] / "logs" / "biliup-auto-queue-submissions.json"
RATE_LIMIT_RETRY_SECONDS = 45 * 60
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
BVID_RE = re.compile(r"\bBV[0-9A-Za-z]{10,}\b")


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
    parser.add_argument("--title-suffix", default=DEFAULT_TITLE_SUFFIX)
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
    parser.add_argument("--upload-lock-file", default=str(DEFAULT_UPLOAD_LOCK_FILE))
    parser.add_argument("--submission-state-file", default=str(DEFAULT_SUBMISSION_STATE_FILE))
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

            upload_lock = try_acquire_upload_lock(Path(args.upload_lock_file))
            if upload_lock is None:
                local_log.log_event(
                    SERVICE,
                    "INFO",
                    "global Bilibili upload lane is busy; waiting",
                    match_id=candidate.match_id,
                    order=candidate.order,
                    lock_file=args.upload_lock_file,
                )
                if args.once:
                    return 0
                time.sleep(args.poll_seconds)
                continue
            if upload_process_running():
                release_lock(upload_lock)
                local_log.log_event(SERVICE, "INFO", "another Bilibili upload started outside the queue; waiting")
                if args.once:
                    return 0
                time.sleep(args.poll_seconds)
                continue
            try:
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
            finally:
                release_lock(upload_lock)
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
    existing_bvid = cached_submission_bvid(Path(args.submission_state_file), candidate, title)
    if not existing_bvid and title:
        existing_bvid, list_ok, rate_limited = find_existing_bvid(args, title)
        if not list_ok:
            # Never submit while the duplicate check is unavailable.  This is
            # especially important for Bilibili -509 responses after a prior
            # submit whose archive/post-processing subsequently failed.
            return 75 if rate_limited else 1
        if existing_bvid:
            remember_submission(Path(args.submission_state_file), candidate, title, existing_bvid)
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
    observed_bvid = ""

    def persist_observed_bvid(value: str) -> None:
        nonlocal observed_bvid
        if value == observed_bvid:
            return
        observed_bvid = value
        remember_submission(Path(args.submission_state_file), candidate, title, value)

    code, output = run_streaming(command, persist_observed_bvid)
    output_bvid = find_bvid_in_text(output)
    if output_bvid:
        remember_submission(Path(args.submission_state_file), candidate, title, output_bvid)
        bvid = output_bvid
    if code != 0 and is_bili_rate_limited(output):
        code = 75
    local_log.log_event(
        SERVICE,
        "INFO" if code == 0 else "ERROR",
        "candidate workflow finished" if code == 0 else "candidate workflow failed",
        match_id=candidate.match_id,
        order=candidate.order,
        bvid=bvid,
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
    if args.title_suffix:
        command.extend(["--title-suffix", args.title_suffix])
    if args.season_id:
        command.extend(["--season-id", str(args.season_id)])
    if args.section_id:
        command.extend(["--section-id", str(args.section_id)])
    return command


def find_existing_bvid(args: argparse.Namespace, title: str) -> tuple[str, bool, bool]:
    result = subprocess.run([args.biliup, "--user-cookie", args.cookie, "list"], text=True, capture_output=True)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        local_log.log_event(
            SERVICE,
            "WARNING",
            "failed to list Bilibili archives",
            exit_code=result.returncode,
            detail=detail,
        )
        return "", False, is_bili_rate_limited(detail)
    for line in result.stdout.splitlines():
        clean = ANSI_RE.sub("", line)
        parts = clean.split("\t")
        if len(parts) >= 2 and parts[1].strip() == title:
            return parts[0].strip(), True, False
    return "", True, False


def run_streaming(command: list[str], on_bvid=None) -> tuple[int, str]:
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert process.stdout is not None
    output: list[str] = []
    for line in process.stdout:
        output.append(line)
        print(line, end="")
        bvid = find_bvid_in_text(line)
        if bvid and on_bvid is not None:
            on_bvid(bvid)
    return process.wait(), "".join(output)


def find_bvid_in_text(output: str) -> str:
    matches = BVID_RE.findall(output)
    return matches[-1] if matches else ""


def is_bili_rate_limited(output: str) -> bool:
    return any(
        token in output
        for token in (
            "投稿过于频繁",
            "请求过于频繁",
            "code: 21566",
            '"code":21566',
            "code: -509",
            '"code":-509',
        )
    )


def cached_submission_bvid(path: Path, candidate: Candidate, title: str) -> str:
    state = load_submission_state(path)
    record = state.get("submissions", {}).get(candidate.match_id, {})
    if record.get("title") != title:
        return ""
    bvid = str(record.get("bvid") or "")
    return bvid if BVID_RE.fullmatch(bvid) else ""


def remember_submission(path: Path, candidate: Candidate, title: str, bvid: str) -> None:
    if not BVID_RE.fullmatch(bvid):
        return
    state = load_submission_state(path)
    state.setdefault("version", 1)
    state.setdefault("submissions", {})[candidate.match_id] = {
        "match_id": candidate.match_id,
        "zone": candidate.zone,
        "order": candidate.order,
        "title": title,
        "bvid": bvid,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{time.time_ns()}")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)


def load_submission_state(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            data.setdefault("submissions", {})
            return data
    except (OSError, ValueError):
        pass
    return {"version": 1, "submissions": {}}


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
