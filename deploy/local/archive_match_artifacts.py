#!/usr/bin/env python3
import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import local_log


DEFAULT_SOURCE_ROOT = "/mnt/PC801/rm-monitor/records"
DEFAULT_TARGET_ROOT = "/mnt/server_data/rm-monitor/records"


@dataclass
class Artifact:
    artifact_id: int
    role: str
    rel_path: str
    size: int
    checksum: str


def main() -> int:
    parser = argparse.ArgumentParser(description="Copy match source artifacts to long-term storage after upload.")
    parser.add_argument("--match-id", default="")
    parser.add_argument("--zone", default="")
    parser.add_argument("--order", type=int)
    parser.add_argument("--namespace", default="rm-monitor")
    parser.add_argument("--postgres", default="deployment/postgres")
    parser.add_argument("--db-user", default="rm_monitor")
    parser.add_argument("--db-name", default="rm_monitor")
    parser.add_argument("--source-root", default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--target-root", default=DEFAULT_TARGET_ROOT)
    parser.add_argument("--delete-source", action="store_true", help="Delete source files and mark artifacts deleted after verified copy.")
    parser.add_argument("--submit", action="store_true", help="Actually copy/delete. Without this, print the plan only.")
    args = parser.parse_args()

    artifacts = load_artifacts(args)
    if not artifacts:
        raise SystemExit("no available source artifacts matched")

    print(f"artifacts={len(artifacts)}")
    local_log.log_event(
        "archive-artifacts",
        "INFO",
        "archive plan ready",
        submit=args.submit,
        match_id=args.match_id,
        zone=args.zone,
        order=args.order,
        artifacts=len(artifacts),
        source_root=args.source_root,
        target_root=args.target_root,
        delete_source=args.delete_source,
    )
    for artifact in artifacts:
        source = resolve(Path(args.source_root), artifact.rel_path)
        target = resolve(Path(args.target_root), artifact.rel_path)
        status = "ready" if target_ok(target, artifact) else "copy"
        print(f"{status}\t{artifact.role}\t{source}\t=>\t{target}")

    if not args.submit:
        return 0

    copied = 0
    deleted = 0
    for artifact in artifacts:
        source = resolve(Path(args.source_root), artifact.rel_path)
        target = resolve(Path(args.target_root), artifact.rel_path)
        local_log.log_event(
            "archive-artifacts",
            "INFO",
            "copy started",
            artifact_id=artifact.artifact_id,
            role=artifact.role,
            source=str(source),
            target=str(target),
        )
        copy_verified(source, target, artifact)
        copied += 1
        local_log.log_event(
            "archive-artifacts",
            "INFO",
            "copy verified",
            artifact_id=artifact.artifact_id,
            role=artifact.role,
            target=str(target),
            size=artifact.size,
        )
        if args.delete_source:
            if source.exists():
                source.unlink()
            psql_exec(args, f"update media_artifacts set status='DELETED', deleted_at=now(), updated_at=now() where id={artifact.artifact_id};")
            deleted += 1
            local_log.log_event(
                "archive-artifacts",
                "INFO",
                "source deleted after archive",
                artifact_id=artifact.artifact_id,
                role=artifact.role,
                source=str(source),
            )
    print(f"verified={copied} deleted={deleted}")
    local_log.log_event("archive-artifacts", "INFO", "archive completed", copied=copied, deleted=deleted)
    return 0


def load_artifacts(args: argparse.Namespace) -> list[Artifact]:
    where = ["ma.kind = 'source'", "ma.status = 'AVAILABLE'"]
    if args.match_id:
        where.append(f"m.id = '{sql_escape(args.match_id)}'")
    if args.zone:
        where.append(f"m.zone = '{sql_escape(args.zone)}'")
    if args.order is not None:
        where.append(f'm."order" = {int(args.order)}')
    if len(where) == 2:
        raise SystemExit("select a match with --match-id or --zone/--order")
    query = f"""
        select ma.id, rt.role, ma.path, coalesce(ma.file_size, 0), coalesce(ma.checksum, '')
        from media_artifacts ma
        join record_tasks rt on rt.id = ma.record_task_media_artifacts
        join match_rounds mr on mr.id = rt.match_round_record_tasks
        join matches m on m.id = mr.match_rounds
        where {' and '.join(where)}
        order by rt.role, ma.created_at;
    """
    rows = psql_rows(args, query)
    return [Artifact(int(row[0]), row[1], row[2], int(row[3] or 0), row[4]) for row in rows]


def copy_verified(source: Path, target: Path, artifact: Artifact) -> None:
    if target_ok(target, artifact):
        return
    if not source.is_file():
        raise SystemExit(f"missing source file: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.tmp.{os.getpid()}.{time.time_ns()}")
    try:
        with source.open("rb") as src, tmp.open("wb") as dst:
            shutil.copyfileobj(src, dst, length=8 * 1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        tmp.replace(target)
    finally:
        if tmp.exists():
            tmp.unlink()
    if not target_ok(target, artifact):
        raise SystemExit(f"verification failed after copy: {target}")


def target_ok(target: Path, artifact: Artifact) -> bool:
    if not target.is_file():
        return False
    if artifact.size and target.stat().st_size != artifact.size:
        return False
    if artifact.checksum and sha256_file(target) != artifact.checksum:
        return False
    return True


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve(root: Path, rel_path: str) -> Path:
    rel = Path(rel_path)
    if rel.is_absolute() or ".." in rel.parts:
        raise SystemExit(f"unsafe artifact path: {rel_path}")
    return root / rel


def psql_rows(args: argparse.Namespace, query: str) -> list[list[str]]:
    cmd = [
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
    result = subprocess.run(cmd, text=True, capture_output=True)
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or result.stdout.strip() or "psql failed")
    return [line.split("\t") for line in result.stdout.splitlines() if line.strip()]


def psql_exec(args: argparse.Namespace, query: str) -> None:
    psql_rows(args, query)


def sql_escape(value: str) -> str:
    return value.replace("'", "''")


if __name__ == "__main__":
    raise SystemExit(local_log.run_logged("archive-artifacts", main))
