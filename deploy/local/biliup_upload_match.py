#!/usr/bin/env python3
import argparse
import base64
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import requests

import local_log


DEFAULT_RECORDS_ROOT = "/mnt/PC801/rm-monitor/records"
DEFAULT_ARCHIVE_TARGET_ROOT = "/mnt/server_data/rm-monitor/records"
DEFAULT_COOKIE = "cookies.json"
DEFAULT_TITLE_SUFFIX = "RMUC2026区域赛"
DEFAULT_TAGS = "RoboMaster,RMUC2026,机器人竞赛"
DEFAULT_SEASON_NAME = "RMUC2026南部赛区录制"
DEFAULT_BITABLE_LINK_FIELD = "视频链接"
DEFAULT_LARK_SECRET_NAME = "rm-monitor-lark"
FEISHU_API_BASE = "https://open.feishu.cn/open-apis"
BITABLE_FIELD_TYPE_TEXT = 1
BITABLE_FIELD_TYPE_URL = 15

ROLE_ORDER = [
    "主视角",
    "主视角（无解说版）",
    "红方英雄第一视角",
    "红方工程第一视角",
    "红方3号步兵第一视角",
    "红方4号步兵第一视角",
    "红方4号兵号第一视角",
    "红方无人机第一视角",
    "红方机器人第一视角合集",
    "蓝方英雄第一视角",
    "蓝方工程第一视角",
    "蓝方3号步兵第一视角",
    "蓝方4号步兵第一视角",
    "蓝方无人机第一视角",
    "蓝方机器人第一视角合集",
]


@dataclass
class MatchInfo:
    match_id: str
    event: str
    zone: str
    order: int
    match_type: str
    match_slug: str
    red_school: str
    red_name: str
    blue_school: str
    blue_name: str
    red_score: int
    blue_score: int


@dataclass
class Artifact:
    role: str
    rel_path: str


@dataclass
class SeasonInfo:
    season_id: int
    section_id: int
    title: str
    section_title: str


@dataclass
class BitableRecord:
    role: str
    app_token: str
    table_id: str
    record_id: str


@dataclass
class LarkCredentials:
    app_id: str
    app_secret: str
    bitable_app_token: str


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate or run a biliup multi-P upload command for one RM match.",
    )
    parser.add_argument("--match-id", help="RM match id. Defaults to the latest match with source artifacts.")
    parser.add_argument("--zone", help="Select a match by zone name.")
    parser.add_argument("--order", type=int, help="Select a match by order number.")
    parser.add_argument("--match-type", help="Select a match by raw match_type, for example GROUP.")
    parser.add_argument("--namespace", default="rm-monitor", help="Kubernetes namespace.")
    parser.add_argument("--postgres", default="deployment/postgres", help="kubectl exec target for Postgres.")
    parser.add_argument("--db-user", default="rm_monitor")
    parser.add_argument("--db-name", default="rm_monitor")
    parser.add_argument("--records-root", default=DEFAULT_RECORDS_ROOT, help="Source records root used for Bilibili upload.")
    parser.add_argument("--archive-target-root", default=DEFAULT_ARCHIVE_TARGET_ROOT, help="Long-term records root copied after successful upload.")
    parser.add_argument("--archive-source-root", default="", help="Source root for post-upload archive. Defaults to --records-root.")
    parser.add_argument("--no-archive-after-upload", action="store_true", help="Do not copy to long-term storage after a successful submit.")
    parser.add_argument("--keep-source-after-archive", action="store_true", help="Keep source files after verified long-term copy.")
    parser.add_argument("--cookie", default=DEFAULT_COOKIE, help="biliup cookies.json path.")
    parser.add_argument("--biliup", default="biliup", help="biliup executable.")
    parser.add_argument("--tid", default="171", help="Bilibili category id. 171 is e-sports.")
    parser.add_argument("--copyright", default="1", help="1 self-made, 2 repost.")
    parser.add_argument("--source", default="", help="Repost source when copyright=2.")
    parser.add_argument("--line", default="", help="Upload line, for example bda2/tx.")
    parser.add_argument("--limit", default="3", help="Per-file upload concurrency.")
    parser.add_argument("--tags", default=DEFAULT_TAGS)
    parser.add_argument("--title-suffix", default=DEFAULT_TITLE_SUFFIX)
    parser.add_argument("--title", help="Override generated Bilibili title.")
    parser.add_argument("--desc", help="Override generated description.")
    parser.add_argument("--dynamic", default="")
    parser.add_argument("--no-stage-in-title", action="store_true", help="Keep titles like [南部第1场] without match stage.")
    parser.add_argument("--season-name", default=DEFAULT_SEASON_NAME, help="Bilibili collection title.")
    parser.add_argument("--season-id", type=int, help="Bilibili collection season id.")
    parser.add_argument("--section-id", type=int, help="Bilibili collection section id.")
    parser.add_argument("--no-season", action="store_true", help="Do not add the uploaded archive to a collection.")
    parser.add_argument("--no-feishu-link", action="store_true", help="Do not write Bilibili links back to Feishu Bitable.")
    parser.add_argument("--no-feishu-topic-reply", action="store_true", help="Do not reply the final Bilibili link in Feishu match threads.")
    parser.add_argument("--bitable-link-field", default=DEFAULT_BITABLE_LINK_FIELD, help="Feishu Bitable field used for Bilibili links.")
    parser.add_argument("--lark-app-id", default="", help="Feishu app id. Defaults to env or Kubernetes secret.")
    parser.add_argument("--lark-app-secret", default="", help="Feishu app secret. Defaults to env or Kubernetes secret.")
    parser.add_argument("--bitable-app-token", default="", help="Feishu Bitable app token fallback.")
    parser.add_argument("--lark-secret-name", default=DEFAULT_LARK_SECRET_NAME, help="Kubernetes secret containing app-id/app-secret/bitable-app-token.")
    parser.add_argument("--add-existing-bvid", help="Skip upload and add an existing BV id to the selected collection.")
    parser.add_argument("--submit", action="store_true", help="Actually run biliup. Without this, only print the plan.")
    parser.add_argument("--allow-duplicates", action="store_true", help="Allow multiple files with the same role.")
    args = parser.parse_args()
    local_log.log_event(
        "biliup-upload",
        "INFO",
        "biliup workflow selected",
        submit=args.submit,
        match_id=args.match_id,
        zone=args.zone,
        order=args.order,
        add_existing_bvid=args.add_existing_bvid or "",
        archive_after_upload=not args.no_archive_after_upload,
        feishu_link=not args.no_feishu_link,
        feishu_topic_reply=not args.no_feishu_topic_reply,
    )

    session, csrf = load_bili_session(Path(args.cookie))
    season = None if args.no_season else resolve_season(session, args)

    if args.add_existing_bvid:
        if season is None:
            raise SystemExit("--add-existing-bvid requires a collection unless --no-season is removed")
        artifacts = []
        bitable_records = []
        match_info = load_match(args) if has_match_selector(args) else None
        if match_info is not None and (not args.no_feishu_link or not args.no_archive_after_upload):
            artifacts = load_artifacts(args, match_info.match_id, args.allow_duplicates)
        if not args.no_feishu_link and match_info is not None:
            bitable_records = load_bitable_records(args, match_info.match_id)
        print(
            json.dumps(
                {
                    "bvid": args.add_existing_bvid,
                    "season": season.__dict__,
                    "feishu": {
                        "enabled": not args.no_feishu_link and has_match_selector(args),
                        "topic_reply": match_info is not None and not args.no_feishu_topic_reply,
                        "field": args.bitable_link_field,
                        "records": [
                            {"role": record.role, "table_id": record.table_id, "record_id": record.record_id}
                            for record in bitable_records
                        ],
                    },
                    "archive_after_upload": {
                        "enabled": match_info is not None and not args.no_archive_after_upload,
                        "source_root": args.archive_source_root or args.records_root,
                        "target_root": args.archive_target_root,
                        "delete_source": not args.keep_source_after_archive,
                    },
                    "submit": args.submit,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        local_log.log_event(
            "biliup-upload",
            "INFO",
            "existing BVID plan ready",
            bvid=args.add_existing_bvid,
            submit=args.submit,
            match_id="" if match_info is None else match_info.match_id,
            season="" if season is None else season.title,
            artifacts=len(artifacts),
        )
        if args.submit:
            archive = load_archive_detail(args, args.add_existing_bvid)
            add_archive_to_season(session, csrf, season, archive)
            if not args.no_feishu_link and has_match_selector(args):
                if not bitable_records:
                    raise SystemExit("no Feishu Bitable records found for this match")
                lark_session = requests.Session()
                lark_token = tenant_access_token(lark_session, load_lark_credentials(args))
                field_types = {
                    (app_token, table_id): ensure_bitable_link_field(
                        lark_session,
                        lark_token,
                        app_token,
                        table_id,
                        args.bitable_link_field,
                    )
                    for app_token, table_id in unique_bitable_tables(args, bitable_records)
                }
                update_feishu_bilibili_links(
                    args,
                    lark_session,
                    lark_token,
                    args.add_existing_bvid,
                    artifacts,
                    bitable_records,
                    field_types,
                )
            if match_info is not None and not args.no_feishu_topic_reply:
                reply_feishu_bilibili_link(args, match_info, args.add_existing_bvid)
            if match_info is not None and not args.no_archive_after_upload:
                archive_after_upload(args, match_info)
            local_log.log_event(
                "biliup-upload",
                "INFO",
                "existing BVID workflow completed",
                bvid=args.add_existing_bvid,
                match_id="" if match_info is None else match_info.match_id,
            )
        return 0

    match_info = load_match(args)
    artifacts = load_artifacts(args, match_info.match_id, args.allow_duplicates)
    video_paths = resolve_video_paths(Path(args.records_root), artifacts)
    title = args.title or build_title(match_info, args.title_suffix, not args.no_stage_in_title)
    desc = args.desc or build_description(match_info, title)
    command = build_command(args, title, desc, video_paths)
    bitable_records = [] if args.no_feishu_link else load_bitable_records(args, match_info.match_id)

    plan = {
        "match_id": match_info.match_id,
        "match_type": match_info.match_type,
        "stage": stage_label(match_info),
        "title": title,
        "season": None if season is None else season.__dict__,
        "feishu": {
            "enabled": not args.no_feishu_link,
            "topic_reply": not args.no_feishu_topic_reply,
            "field": args.bitable_link_field,
            "records": [{"role": record.role, "table_id": record.table_id, "record_id": record.record_id} for record in bitable_records],
        },
        "videos": [{"role": artifact.role, "path": str(path)} for artifact, path in zip(artifacts, video_paths)],
        "archive_after_upload": {
            "enabled": not args.no_archive_after_upload,
            "source_root": args.archive_source_root or args.records_root,
            "target_root": args.archive_target_root,
            "delete_source": not args.keep_source_after_archive,
        },
        "submit": args.submit,
    }
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    print()
    print(shell_join(command))
    local_log.log_event(
        "biliup-upload",
        "INFO",
        "biliup upload plan ready",
        submit=args.submit,
        match_id=match_info.match_id,
        title=title,
        videos=len(video_paths),
        season="" if season is None else season.title,
    )

    if not args.submit:
        return 0

    lark_session = None
    lark_token = ""
    field_types: dict[tuple[str, str], int] = {}
    if not args.no_feishu_link:
        if not bitable_records:
            raise SystemExit("no Feishu Bitable records found for this match; wait for uploader-dispatcher or use --no-feishu-link")
        lark_session = requests.Session()
        lark_token = tenant_access_token(lark_session, load_lark_credentials(args))
        for app_token, table_id in unique_bitable_tables(args, bitable_records):
            field_types[(app_token, table_id)] = ensure_bitable_link_field(
                lark_session,
                lark_token,
                app_token,
                table_id,
                args.bitable_link_field,
            )

    before = set(find_bvids_by_title(session, title))
    local_log.log_event(
        "biliup-upload",
        "INFO",
        "biliup upload started",
        match_id=match_info.match_id,
        title=title,
        videos=len(video_paths),
    )
    code, upload_output = run_streaming(command)
    if code != 0:
        local_log.log_event(
            "biliup-upload",
            "ERROR",
            "biliup upload failed",
            match_id=match_info.match_id,
            title=title,
            exit_code=code,
        )
        send_feishu_alert(
            args,
            "Bilibili 上传失败",
            f"标题：{title}\n比赛：{match_info.zone} 第{match_info.order}场\n返回码：{code}\n请立即提醒席伟杰修复。源文件仍在 {args.records_root}",
        )
        return code
    bvid = ""
    archive = None
    if season is not None or not args.no_feishu_link or not args.no_feishu_topic_reply:
        bvid = wait_for_new_bvid(session, title, before, find_bvid_in_text(upload_output))
        archive = load_archive_detail(args, bvid)
    if season is not None:
        add_archive_to_season(session, csrf, season, archive)
        print(f"added {bvid} to {season.title} / {season.section_title}", file=sys.stderr)
    if not args.no_feishu_link:
        assert lark_session is not None
        update_feishu_bilibili_links(
            args,
            lark_session,
            lark_token,
            bvid,
            artifacts,
            bitable_records,
            field_types,
        )
    if not args.no_feishu_topic_reply:
        reply_feishu_bilibili_link(args, match_info, bvid)
    if not args.no_archive_after_upload:
        archive_after_upload(args, match_info)
    local_log.log_event(
        "biliup-upload",
        "INFO",
        "biliup upload workflow completed",
        match_id=match_info.match_id,
        title=title,
        bvid=bvid,
    )
    return 0


def load_match(args: argparse.Namespace) -> MatchInfo:
    where = []
    if args.match_id:
        where.append(f"m.id = '{sql_escape(args.match_id)}'")
    if args.zone:
        where.append(f"m.zone = '{sql_escape(args.zone)}'")
    if args.order is not None:
        where.append(f'm."order" = {int(args.order)}')
    if args.match_type:
        where.append(f"m.match_type = '{sql_escape(args.match_type)}'")
    where_sql = ""
    if where:
        where_sql = "where " + " and ".join(where)
    else:
        where_sql = """where exists (
            select 1
            from media_artifacts ma
            join record_tasks rt on rt.id = ma.record_task_media_artifacts
            join match_rounds mr on mr.id = rt.match_round_record_tasks
            where mr.match_rounds = m.id and ma.kind = 'source'
        )"""
    query = f"""
        select
            m.id,
            m.event,
            m.zone,
            m."order",
            m.match_type,
            coalesce(m.match_slug, ''),
            rt.school_name,
            rt.name,
            bt.school_name,
            bt.name,
            coalesce(sum(case when mr.winner = 'red' then 1 else 0 end), 0),
            coalesce(sum(case when mr.winner = 'blue' then 1 else 0 end), 0)
        from matches m
        join teams rt on rt.id = m.team_red_matches
        join teams bt on bt.id = m.team_blue_matches
        left join match_rounds mr on mr.match_rounds = m.id
        {where_sql}
        group by m.id, rt.id, bt.id
        order by m.updated_at desc
        limit 1;
    """
    rows = psql(args, query)
    if len(rows) != 1:
        raise SystemExit("no matching match found")
    row = rows[0]
    return MatchInfo(
        match_id=row[0],
        event=row[1],
        zone=row[2],
        order=int(row[3]),
        match_type=row[4],
        match_slug=row[5],
        red_school=row[6],
        red_name=row[7],
        blue_school=row[8],
        blue_name=row[9],
        red_score=int(row[10]),
        blue_score=int(row[11]),
    )


def load_artifacts(args: argparse.Namespace, match_id: str, allow_duplicates: bool) -> list[Artifact]:
    query = f"""
        select rt.role, ma.path
        from media_artifacts ma
        join record_tasks rt on rt.id = ma.record_task_media_artifacts
        join match_rounds mr on mr.id = rt.match_round_record_tasks
        where mr.match_rounds = '{sql_escape(match_id)}'
          and ma.kind = 'source'
          and position('__part' in rt.role) = 0
        order by rt.role, ma.created_at;
    """
    rows = psql(args, query)
    artifacts = [Artifact(role=row[0], rel_path=row[1]) for row in rows]
    if not artifacts:
        raise SystemExit(f"no source FLV artifacts found for match {match_id}")
    seen: dict[str, str] = {}
    duplicates = []
    for artifact in artifacts:
        if artifact.role in seen:
            duplicates.append(artifact.role)
        seen[artifact.role] = artifact.rel_path
    if duplicates and not allow_duplicates:
        names = ", ".join(sorted(set(duplicates)))
        raise SystemExit(f"duplicate role artifacts found ({names}); this match may have round-split recordings")
    return sorted(artifacts, key=lambda item: (role_rank(item.role), item.role, item.rel_path))


def load_bitable_records(args: argparse.Namespace, match_id: str) -> list[BitableRecord]:
    query = f"""
        select
            rt.role,
            coalesce(ut.bitable_app_token, ''),
            coalesce(ut.bitable_table_id, ''),
            coalesce(ut.bitable_record_id, '')
        from upload_tasks ut
        join record_tasks rt on rt.id = ut.record_task_upload_task
        join match_rounds mr on mr.id = rt.match_round_record_tasks
        where mr.match_rounds = '{sql_escape(match_id)}'
          and coalesce(ut.bitable_table_id, '') <> ''
          and coalesce(ut.bitable_record_id, '') <> ''
          and position('__part' in rt.role) = 0
        order by rt.role, ut.created_at;
    """
    rows = psql(args, query)
    records = [BitableRecord(role=row[0], app_token=row[1], table_id=row[2], record_id=row[3]) for row in rows]
    seen: set[str] = set()
    duplicates: set[str] = set()
    for record in records:
        if record.role in seen:
            duplicates.add(record.role)
        seen.add(record.role)
    if duplicates and not args.allow_duplicates:
        names = ", ".join(sorted(duplicates))
        raise SystemExit(f"duplicate Feishu Bitable records found for roles: {names}")
    return sorted(records, key=lambda item: (role_rank(item.role), item.role, item.record_id))


def resolve_video_paths(records_root: Path, artifacts: list[Artifact]) -> list[Path]:
    paths: list[Path] = []
    missing: list[Path] = []
    for artifact in artifacts:
        rel = Path(*Path(artifact.rel_path).parts)
        full = records_root / rel
        paths.append(full)
        if not full.is_file():
            missing.append(full)
    if missing:
        formatted = "\n".join(f"  {path}" for path in missing)
        raise SystemExit(f"missing long-term FLV files:\n{formatted}")
    return paths


def build_title(match_info: MatchInfo, suffix: str, include_stage: bool) -> str:
    zone = short_zone(match_info.zone)
    stage = stage_label(match_info) if include_stage else ""
    prefix = f"{zone}第{match_info.order}场"
    if stage:
        prefix = f"{prefix} {stage}"
    red = team_label(match_info.red_school, match_info.red_name)
    blue = team_label(match_info.blue_school, match_info.blue_name)
    score = f"{match_info.red_score}:{match_info.blue_score}"
    return f"{prefix} {red}{score}{blue} | {suffix}"


def build_description(match_info: MatchInfo, title: str) -> str:
    return "\n".join(
        [
            title,
            "",
            f"赛事：{match_info.event}",
            f"赛区：{match_info.zone}",
            f"阶段：{stage_label(match_info) or match_info.match_type}",
            f"场次：第{match_info.order}场",
            "分P：各视角原始FLV",
        ]
    )


def build_command(args: argparse.Namespace, title: str, desc: str, video_paths: list[Path]) -> list[str]:
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
    ]
    if args.source:
        command.extend(["--source", args.source])
    if args.dynamic:
        command.extend(["--dynamic", args.dynamic])
    if args.line:
        command.extend(["--line", args.line])
    command.extend(str(path) for path in video_paths)
    return command


def run_streaming(command: list[str]) -> tuple[int, str]:
    env = os.environ.copy()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    assert process.stdout is not None
    output: list[str] = []
    for line in process.stdout:
        output.append(line)
        print(line, end="")
    return process.wait(), "".join(output)


def archive_after_upload(args: argparse.Namespace, match_info: MatchInfo) -> None:
    script = Path(__file__).with_name("archive_match_artifacts.py")
    command = [
        sys.executable,
        str(script),
        "--match-id",
        match_info.match_id,
        "--namespace",
        args.namespace,
        "--postgres",
        args.postgres,
        "--db-user",
        args.db_user,
        "--db-name",
        args.db_name,
        "--source-root",
        args.archive_source_root or args.records_root,
        "--target-root",
        args.archive_target_root,
        "--submit",
    ]
    if not args.keep_source_after_archive:
        command.append("--delete-source")
    print("post-upload archive:", shell_join(command), file=sys.stderr)
    local_log.log_event(
        "biliup-upload",
        "INFO",
        "post-upload archive started",
        match_id=match_info.match_id,
        source_root=args.archive_source_root or args.records_root,
        target_root=args.archive_target_root,
        delete_source=not args.keep_source_after_archive,
    )
    code, _ = run_streaming(command)
    if code != 0:
        local_log.log_event(
            "biliup-upload",
            "ERROR",
            "post-upload archive failed",
            match_id=match_info.match_id,
            exit_code=code,
            source_root=args.archive_source_root or args.records_root,
            target_root=args.archive_target_root,
        )
        send_feishu_alert(
            args,
            "长期归档失败",
            f"比赛：{match_info.zone} 第{match_info.order}场\n返回码：{code}\n请立即提醒席伟杰修复。源文件仍在 {args.archive_source_root or args.records_root}",
        )
        raise SystemExit(code)
    local_log.log_event(
        "biliup-upload",
        "INFO",
        "post-upload archive completed",
        match_id=match_info.match_id,
        target_root=args.archive_target_root,
    )


def load_bili_session(cookie_path: Path) -> tuple[requests.Session, str]:
    with cookie_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    cookies = {item["name"]: item["value"] for item in data.get("cookie_info", {}).get("cookies", [])}
    csrf = cookies.get("bili_jct", "")
    if not csrf:
        raise SystemExit(f"bili_jct not found in {cookie_path}")
    session = requests.Session()
    session.cookies.update(cookies)
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://member.bilibili.com/platform/upload-manager/article",
        }
    )
    return session, csrf


def resolve_season(session: requests.Session, args: argparse.Namespace) -> SeasonInfo:
    if args.season_id and args.section_id:
        return SeasonInfo(
            season_id=args.season_id,
            section_id=args.section_id,
            title=args.season_name or str(args.season_id),
            section_title="正片",
        )
    resp = session.get(
        "https://member.bilibili.com/x2/creative/web/seasons",
        params={"pn": 1, "ps": 50, "order": "mtime", "sort": "desc", "draft": 1},
        timeout=15,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 0:
        raise SystemExit(f"list seasons failed: {body}")
    target_name = (args.season_name or "").strip()
    for item in body.get("data", {}).get("seasons", []):
        season = item.get("season", {})
        if args.season_id and int(season.get("id", 0)) != args.season_id:
            continue
        if not args.season_id and season.get("title") != target_name:
            continue
        sections = item.get("sections", {}).get("sections") or []
        if not sections:
            raise SystemExit(f"season {season.get('title')} has no sections")
        section = sections[0]
        return SeasonInfo(
            season_id=int(season["id"]),
            section_id=int(args.section_id or section["id"]),
            title=season.get("title", str(season["id"])),
            section_title=section.get("title", "正片"),
        )
    raise SystemExit(f"season not found: {target_name or args.season_id}")


def find_bvids_by_title(session: requests.Session, title: str) -> list[str]:
    resp = session.get(
        "https://member.bilibili.com/x/web/archives",
        params={"status": "is_pubing,pubed,not_pubed", "pn": 1},
        timeout=20,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 0:
        raise SystemExit(f"list archives failed: {body}")
    out = []
    for item in body.get("data", {}).get("arc_audits", []):
        archive = item.get("Archive", {})
        if archive.get("title") == title and archive.get("bvid"):
            out.append(archive["bvid"])
    return out


def wait_for_new_bvid(session: requests.Session, title: str, before: set[str], fallback_bvid: str = "") -> str:
    for _ in range(30):
        bvids = find_bvids_by_title(session, title)
        new_bvids = [bvid for bvid in bvids if bvid not in before]
        if new_bvids:
            return new_bvids[0]
        if bvids and not before:
            return bvids[0]
        time.sleep(2)
    if fallback_bvid:
        return fallback_bvid
    raise SystemExit(f"uploaded archive not found by title: {title}")


def find_bvid_in_text(text: str) -> str:
    match = re.search(r"\bBV[0-9A-Za-z]{10,}\b", text)
    return match.group(0) if match else ""


def load_archive_detail(args: argparse.Namespace, bvid: str) -> dict:
    result = subprocess.run(
        [args.biliup, "--user-cookie", args.cookie, "show", bvid],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or result.stdout.strip() or f"biliup show {bvid} failed")
    return json.loads(result.stdout)


def add_archive_to_season(session: requests.Session, csrf: str, season: SeasonInfo, detail: dict) -> None:
    archive = detail.get("archive") or {}
    videos = detail.get("videos") or []
    aid = archive.get("aid")
    title = archive.get("title")
    cid = videos[0].get("cid") if videos else None
    if not aid or not cid or not title:
        raise SystemExit(f"cannot determine aid/cid/title from uploaded archive: aid={aid} cid={cid} title={title}")
    if season_contains_aid(session, season.section_id, int(aid)):
        print(f"archive av{aid} already exists in {season.title}", file=sys.stderr)
        return
    resp = session.post(
        "https://member.bilibili.com/x2/creative/web/season/section/episodes/add",
        params={"csrf": csrf},
        json={
            "sectionId": season.section_id,
            "episodes": [
                {
                    "aid": int(aid),
                    "cid": int(cid),
                    "title": title,
                    "charging_pay": int(archive.get("charging_pay") or 0),
                }
            ],
            "csrf": csrf,
        },
        headers={"Content-Type": "application/json; charset=UTF-8"},
        timeout=20,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 0:
        raise SystemExit(f"add to season failed: {body}")


def season_contains_aid(session: requests.Session, section_id: int, aid: int) -> bool:
    resp = session.get(
        "https://member.bilibili.com/x2/creative/web/season/section",
        params={"id": section_id},
        timeout=15,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 0:
        raise SystemExit(f"get season section failed: {body}")
    data = body.get("data", {}) or {}
    section = data.get("section", {}) or {}
    episodes = (
        data.get("episodes")
        or data.get("Episodes")
        or section.get("episodes")
        or section.get("Episodes")
        or []
    )
    return any(int(item.get("aid") or 0) == aid for item in episodes)


def load_lark_credentials(args: argparse.Namespace) -> LarkCredentials:
    secret = load_kubernetes_secret(args)
    app_id = first_non_empty(
        args.lark_app_id,
        os.environ.get("RM_MONITOR_LARK_APP_ID", ""),
        os.environ.get("RM_MONITOR_FEISHU_APP_ID", ""),
        secret.get("app-id", ""),
    )
    app_secret = first_non_empty(
        args.lark_app_secret,
        os.environ.get("RM_MONITOR_LARK_APP_SECRET", ""),
        os.environ.get("RM_MONITOR_FEISHU_APP_SECRET", ""),
        secret.get("app-secret", ""),
    )
    bitable_app_token = first_non_empty(
        args.bitable_app_token,
        os.environ.get("RM_MONITOR_BITABLE_APP_TOKEN", ""),
        os.environ.get("RM_MONITOR_FEISHU_BITABLE_APP_TOKEN", ""),
        secret.get("bitable-app-token", ""),
    )
    if not app_id or not app_secret:
        raise SystemExit("Feishu app id/secret not found; set args/env or keep rm-monitor-lark secret")
    return LarkCredentials(app_id=app_id, app_secret=app_secret, bitable_app_token=bitable_app_token)


def load_kubernetes_secret(args: argparse.Namespace) -> dict[str, str]:
    if not args.lark_secret_name:
        return {}
    result = subprocess.run(
        [
            "kubectl",
            "get",
            "secret",
            args.lark_secret_name,
            "-n",
            args.namespace,
            "-o",
            "json",
        ],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        return {}
    data = json.loads(result.stdout).get("data", {}) or {}
    out = {}
    for key, value in data.items():
        try:
            out[key] = base64.b64decode(value).decode("utf-8")
        except Exception:
            continue
    return out


def tenant_access_token(session: requests.Session, credentials: LarkCredentials) -> str:
    resp = session.post(
        f"{FEISHU_API_BASE}/auth/v3/tenant_access_token/internal",
        json={"app_id": credentials.app_id, "app_secret": credentials.app_secret},
        timeout=15,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 0:
        raise SystemExit(f"get Feishu tenant_access_token failed: {body}")
    token = body.get("tenant_access_token", "")
    if not token:
        raise SystemExit("get Feishu tenant_access_token failed: empty token")
    return token


def unique_bitable_tables(args: argparse.Namespace, records: list[BitableRecord]) -> list[tuple[str, str]]:
    credentials = None
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for record in records:
        app_token = record.app_token
        if not app_token:
            if credentials is None:
                credentials = load_lark_credentials(args)
            app_token = credentials.bitable_app_token
        if not app_token:
            raise SystemExit(f"missing Bitable app token for table {record.table_id}")
        key = (app_token, record.table_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def ensure_bitable_link_field(
    session: requests.Session,
    token: str,
    app_token: str,
    table_id: str,
    field_name: str,
) -> int:
    fields = list_bitable_fields(session, token, app_token, table_id)
    if field_name in fields:
        return fields[field_name]
    body = feishu_api(
        session,
        "POST",
        f"/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
        token,
        json={"field_name": field_name, "type": BITABLE_FIELD_TYPE_URL},
    )
    field = body.get("field", {}) or {}
    return int(field.get("type") or BITABLE_FIELD_TYPE_URL)


def list_bitable_fields(session: requests.Session, token: str, app_token: str, table_id: str) -> dict[str, int]:
    fields: dict[str, int] = {}
    page_token = ""
    while True:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        data = feishu_api(
            session,
            "GET",
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
            token,
            params=params,
        )
        for item in data.get("items", []) or []:
            name = item.get("field_name")
            field_type = item.get("type")
            if name and field_type is not None:
                fields[name] = int(field_type)
        if not data.get("has_more") or not data.get("page_token"):
            return fields
        page_token = data["page_token"]


def update_feishu_bilibili_links(
    args: argparse.Namespace,
    session: requests.Session,
    token: str,
    bvid: str,
    artifacts: list[Artifact],
    records: list[BitableRecord],
    field_types: dict[tuple[str, str], int],
) -> None:
    record_by_role = {record.role: record for record in records}
    missing = [artifact.role for artifact in artifacts if artifact.role not in record_by_role]
    if missing:
        raise SystemExit("missing Feishu Bitable records for roles: " + ", ".join(missing))
    credentials = None
    for index, artifact in enumerate(artifacts, start=1):
        record = record_by_role[artifact.role]
        app_token = record.app_token
        if not app_token:
            if credentials is None:
                credentials = load_lark_credentials(args)
            app_token = credentials.bitable_app_token
        if not app_token:
            raise SystemExit(f"missing Bitable app token for record {record.record_id}")
        field_type = field_types.get((app_token, record.table_id))
        if field_type is None:
            field_type = ensure_bitable_link_field(session, token, app_token, record.table_id, args.bitable_link_field)
        url = bilibili_part_url(bvid, index)
        update_bitable_record_link(
            session,
            token,
            app_token,
            record.table_id,
            record.record_id,
            args.bitable_link_field,
            field_type,
            artifact.role,
            url,
        )
    print(f"updated {len(artifacts)} Feishu Bitable links for {bvid}", file=sys.stderr)


def update_bitable_record_link(
    session: requests.Session,
    token: str,
    app_token: str,
    table_id: str,
    record_id: str,
    field_name: str,
    field_type: int,
    role: str,
    url: str,
) -> None:
    if field_type == BITABLE_FIELD_TYPE_URL:
        value: object = {"text": role, "link": url}
    elif field_type == BITABLE_FIELD_TYPE_TEXT:
        value = url
    else:
        value = url
    feishu_api(
        session,
        "PUT",
        f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}",
        token,
        json={"fields": {field_name: value}},
    )


def feishu_api(
    session: requests.Session,
    method: str,
    path: str,
    token: str,
    **kwargs: object,
) -> dict:
    headers = kwargs.pop("headers", {})
    headers = {**headers, "Authorization": f"Bearer {token}"}
    resp = session.request(method, f"{FEISHU_API_BASE}{path}", headers=headers, timeout=20, **kwargs)
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 0:
        raise SystemExit(f"Feishu API failed {method} {path}: {body}")
    return body.get("data", {}) or {}


def send_feishu_alert(args: argparse.Namespace, title: str, text: str) -> None:
    try:
        session = requests.Session()
        token = tenant_access_token(session, load_lark_credentials(args))
        content = {
            "zh_cn": {
                "title": title,
                "content": [
                    [
                        {"tag": "at", "user_id": "all", "user_name": "所有人"},
                        {"tag": "text", "text": " " + title + "，请立即提醒席伟杰修复"},
                    ],
                    [{"tag": "text", "text": text}],
                ],
            }
        }
        for chat_id in list_feishu_chats(session, token):
            feishu_api(
                session,
                "POST",
                "/im/v1/messages?receive_id_type=chat_id",
                token,
                json={
                    "receive_id": chat_id,
                    "msg_type": "post",
                    "content": json.dumps(content, ensure_ascii=False),
                },
            )
    except Exception as exc:
        print(f"failed to send Feishu alert: {exc}", file=sys.stderr)


def list_feishu_chats(session: requests.Session, token: str) -> list[str]:
    chat_ids: list[str] = []
    page_token = ""
    while True:
        path = "/im/v1/chats?page_size=20"
        if page_token:
            path += "&page_token=" + page_token
        data = feishu_api(session, "GET", path, token)
        for item in data.get("items", []):
            chat_id = item.get("chat_id", "")
            if chat_id:
                chat_ids.append(chat_id)
        if not data.get("has_more"):
            return chat_ids
        page_token = data.get("page_token", "")
        if not page_token:
            return chat_ids


def reply_feishu_bilibili_link(args: argparse.Namespace, match_info: MatchInfo, bvid: str) -> None:
    message_ids = load_lark_message_ids(args, match_info.match_id)
    if not message_ids:
        print(f"no Feishu match thread found for match {match_info.match_id}; skipped topic reply", file=sys.stderr)
        return
    session = requests.Session()
    token = tenant_access_token(session, load_lark_credentials(args))
    url = bilibili_video_url(bvid)
    title = f"{short_zone(match_info.zone)}第{match_info.order}场"
    stage = stage_label(match_info)
    if stage:
        title += f" {stage}"
    content = {
        "zh_cn": {
            "title": "Bilibili 总链接",
            "content": [
                [{"tag": "text", "text": f"{title} 已上传："}],
                [{"tag": "a", "text": url, "href": url}],
            ],
        }
    }
    replied = 0
    for message_id in message_ids:
        feishu_api(
            session,
            "POST",
            f"/im/v1/messages/{quote(message_id, safe='')}/reply",
            token,
            json={
                "msg_type": "post",
                "content": json.dumps(content, ensure_ascii=False),
                "reply_in_thread": True,
                "uuid": short_uuid("rm-bili-reply", match_info.match_id, message_id, bvid),
            },
        )
        replied += 1
    print(f"replied {replied} Feishu topic(s) with {url}", file=sys.stderr)


def load_lark_message_ids(args: argparse.Namespace, match_id: str) -> list[str]:
    query = f"""
        select message_id
        from lark_messages
        where match_lark_messages = '{sql_escape(match_id)}'
        order by id;
    """
    return [row[0] for row in psql(args, query) if row and row[0]]


def bilibili_video_url(bvid: str) -> str:
    return f"https://www.bilibili.com/video/{bvid}"


def bilibili_part_url(bvid: str, index: int) -> str:
    return f"{bilibili_video_url(bvid)}?p={index}"


def first_non_empty(*values: str) -> str:
    for value in values:
        value = (value or "").strip()
        if value:
            return value
    return ""


def has_match_selector(args: argparse.Namespace) -> bool:
    return bool(args.match_id or args.zone or args.order is not None or args.match_type)


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
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return [line.split("\t") for line in lines]


def short_zone(zone: str) -> str:
    for token in ("赛区", "区域赛", "分区"):
        zone = zone.replace(token, "")
    return zone.strip()


def stage_label(match_info: MatchInfo) -> str:
    raw = (match_info.match_type or "").strip()
    slug = (match_info.match_slug or "").strip()
    if slug and any("\u4e00" <= char <= "\u9fff" for char in slug):
        return slug
    labels = {
        "GROUP": "小组赛",
        "GROUP_STAGE": "小组赛",
        "KNOCKOUT": "淘汰赛",
        "KNOCKOUT_STAGE": "淘汰赛",
        "ELIMINATION": "淘汰赛",
        "PLAYOFF": "淘汰赛",
        "PLAY_OFF": "淘汰赛",
        "ROUND_OF_32": "1/16决赛",
        "ROUND_OF_16": "1/8决赛",
        "EIGHTH_FINAL": "1/8决赛",
        "QUARTER_FINAL": "1/4决赛",
        "SEMI_FINAL": "半决赛",
        "FINAL": "决赛",
        "GRAND_FINAL": "总决赛",
        "THIRD_PLACE": "季军赛",
        "BRONZE": "季军赛",
        "TEST": "测试",
    }
    if not raw:
        return ""
    key = raw.upper().replace("-", "_").replace(" ", "_")
    if key in labels:
        return labels[key]
    return raw


def team_label(school: str, name: str) -> str:
    return (school or name).strip()


def role_rank(role: str) -> int:
    try:
        return ROLE_ORDER.index(role)
    except ValueError:
        return len(ROLE_ORDER)


def sql_escape(value: str) -> str:
    return value.replace("'", "''")


def short_uuid(prefix: str, *parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(b"\0")
        h.update(part.encode("utf-8"))
    return f"{prefix}:{h.hexdigest()[:24]}"


def shell_join(command: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


if __name__ == "__main__":
    raise SystemExit(local_log.run_logged("biliup-upload", main))
