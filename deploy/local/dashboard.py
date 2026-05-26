#!/usr/bin/env python3
import argparse
import json
import re
import subprocess
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


DEFAULT_NAMESPACE = "rm-monitor"
DEFAULT_POSTGRES = "deployment/postgres"
DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOCAL_LOG_DIR = DEFAULT_REPO_ROOT / "logs"
SCHEDULE_URL = "https://pro-robomasters-hz-n5i3.oss-cn-hangzhou.aliyuncs.com/live_json/schedule.json"
LIVE_INFO_URL = "https://rm-static.djicdn.com/live_json/live_game_info.json"
OFFICIAL_BACKUP_CACHE_SERVICE = "rm-monitor-official-backup-cache.service"
OFFICIAL_BACKUP_CACHE_EVENT = "RMUC 2026超级对抗赛"
OFFICIAL_BACKUP_CACHE_ZONE = "东部赛区"
OFFICIAL_BACKUP_CACHE_SESSION = "正赛备用缓存"
OFFICIAL_BACKUP_CACHE_SOURCE_ROOT = Path("/mnt/PC801/rm-monitor/official-backup-cache")
OFFICIAL_BACKUP_CACHE_TARGET_ROOT = Path("/mnt/server_data/rm-monitor/records")
LOG_TARGETS = [
    ("全部服务", "all"),
    ("monitor", "deployment/monitor"),
    ("record-dispatcher", "deployment/record-dispatcher"),
    ("record-job", "job-prefix/record-"),
    ("continuous-cache", "job-prefix/continuous-cache-"),
    ("uploader-dispatcher", "deployment/uploader-dispatcher"),
    ("uploader-job", "job-prefix/upload-"),
    ("lark-notifier", "deployment/lark-notifier"),
    ("transcode-dispatcher", "deployment/transcode-dispatcher"),
    ("transcode-job", "job-prefix/transcode-"),
    ("postgres", "deployment/postgres"),
    ("redis", "deployment/redis"),
    ("local-biliup", "local-log/biliup-upload"),
    ("local-archive-auto", "local-log/archive-auto-queue"),
    ("local-archive", "local-log/archive-artifacts"),
    ("local-emergency-record", "local-log/emergency-record"),
    ("local-continuous-cache", "local-log/continuous-cache"),
    ("local-backup-cache", "local-log/official-backup-cache"),
    ("biliup-download.log", "local-file/download.log"),
    ("biliup-ds_update.log", "local-file/ds_update.log"),
]
LOG_SINCE_OPTIONS = {"5m", "15m", "30m", "1h", "3h", "6h", "12h", "today"}
LEVELS = ["ERROR", "WARN", "INFO", "DEBUG", "OTHER"]
CURRENT_LOG_SECONDS = 15 * 60
LOCAL_ISSUE_SECONDS = 2 * 60 * 60
JOB_ISSUE_SECONDS = 60 * 60
COLLECT_WORKERS = 8
LOG_RESPONSE_LIMIT = 150
LOG_RESPONSE_FIELD_CHARS = 500
CONFIG = None


def main() -> int:
    global CONFIG
    parser = argparse.ArgumentParser(description="Read RM Monitor logs and health signals.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    parser.add_argument("--postgres", default=DEFAULT_POSTGRES)
    parser.add_argument("--db-user", default="rm_monitor")
    parser.add_argument("--db-name", default="rm_monitor")
    parser.add_argument("--repo-root", default=str(DEFAULT_REPO_ROOT))
    parser.add_argument("--local-log-dir", default=str(DEFAULT_LOCAL_LOG_DIR))
    parser.add_argument("--default-since", default="today", choices=sorted(LOG_SINCE_OPTIONS))
    parser.add_argument("--default-tail", type=int, default=400)
    CONFIG = parser.parse_args()

    server = ThreadingHTTPServer((CONFIG.host, CONFIG.port), LogDashboardHandler)
    print(f"RM Monitor dashboard listening on http://{CONFIG.host}:{CONFIG.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


class LogDashboardHandler(BaseHTTPRequestHandler):
    server_version = "RMMonitorLogDashboard/1.0"

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self.send_text(INDEX_HTML, "text/html; charset=utf-8")
            return
        if parsed.path == "/api/logs":
            self.send_json(collect_logs(urllib.parse.parse_qs(parsed.query)))
            return
        if parsed.path == "/healthz":
            self.send_text("ok\n", "text/plain; charset=utf-8")
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def send_json(self, value: object) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_text(self, value: str, content_type: str) -> None:
        body = value.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


def collect_logs(query: dict[str, list[str]]) -> dict:
    started = time.time()
    target = first_query(query, "target", "all")
    since = first_query(query, "since", CONFIG.default_since)
    level = first_query(query, "level", "ALL").upper()
    keyword = first_query(query, "q", "")
    diagnostics = first_query(query, "diagnostics", "0").lower() in ("1", "true", "yes")
    tail = min(max(to_int(first_query(query, "tail", str(CONFIG.default_tail))), 20), 2000)
    if since not in LOG_SINCE_OPTIONS:
        since = CONFIG.default_since
    if level not in {"ALL", *LEVELS}:
        level = "ALL"

    allowed = {value for _, value in LOG_TARGETS}
    if target not in allowed:
        target = "all"

    errors: list[dict] = []
    include_logs = diagnostics or target != "all" or level != "ALL" or bool(keyword.strip())
    with ThreadPoolExecutor(max_workers=COLLECT_WORKERS) as executor:
        logs_future = executor.submit(collect_raw_logs, target, since, tail, errors) if include_logs else None
        history_future = executor.submit(collect_history)
        current_future = executor.submit(collect_current_snapshot)
        pipeline_future = executor.submit(collect_pipeline_progress)

        raw_lines = logs_future.result() if logs_future else []
        parsed_all = [parse_log_line(item["target"], item["line"]) for item in raw_lines]
        issues_future = executor.submit(collect_issues, parsed_all, errors)

        history = history_future.result()
        current = current_future.result()
        pipeline = pipeline_future.result()
        issues = issues_future.result()

    parsed = apply_filters(parsed_all, level, keyword)
    parsed.sort(key=lambda item: item.get("time_sort") or "", reverse=True)
    summary = summarize(parsed_all, errors, issues)

    response = {
        "generated_at": now_text(),
        "namespace": CONFIG.namespace,
        "diagnostics": diagnostics,
        "target": target,
        "since": since,
        "level": level,
        "keyword": keyword,
        "tail": tail,
        "summary": visible_summary(summary, diagnostics),
        "current": current,
        "history": history,
        "pipeline": pipeline,
        "issues": issues,
        "duration_ms": int((time.time() - started) * 1000),
    }
    if diagnostics:
        response["targets"] = [{"name": name, "target": value} for name, value in LOG_TARGETS]
        response["lines"] = [compact_log_line(item) for item in parsed[: min(LOG_RESPONSE_LIMIT, tail)]]
        response["errors"] = errors
    return response


def collect_raw_logs(target: str, since: str, tail: int, errors: list[dict]) -> list[dict]:
    resolved_targets = resolve_targets(target, errors)
    if len(resolved_targets) <= 1:
        return [
            item
            for name, resolved in resolved_targets
            for item in fetch_target_logs(name, resolved, since, tail, errors)
        ]
    raw_lines: list[dict] = []
    workers = min(COLLECT_WORKERS, len(resolved_targets))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(fetch_target_logs, name, resolved, since, tail, errors): name
            for name, resolved in resolved_targets
        }
        for future in as_completed(futures):
            try:
                raw_lines.extend(future.result())
            except Exception as exc:
                errors.append({"label": futures[future], "message": str(exc)})
    return raw_lines


def resolve_targets(target: str, errors: list[dict]) -> list[tuple[str, str]]:
    if target == "all":
        out = []
        for name, value in LOG_TARGETS:
            if value == "all":
                continue
            if value.startswith("job-prefix/"):
                out.extend(resolve_job_prefix(name, value.removeprefix("job-prefix/"), errors))
            else:
                out.append((name, value))
        return out
    name = next((name for name, value in LOG_TARGETS if value == target), target)
    if target.startswith("job-prefix/"):
        return resolve_job_prefix(name, target.removeprefix("job-prefix/"), errors)
    return [(name, target)]


def resolve_job_prefix(name: str, prefix: str, errors: list[dict]) -> list[tuple[str, str]]:
    result = run(["kubectl", "get", "jobs", "-n", CONFIG.namespace, "-o", "json"], timeout=6)
    if not result["ok"]:
        errors.append({"label": f"jobs {prefix}", "message": result["error"]})
        return []
    try:
        data = json.loads(result["stdout"])
    except json.JSONDecodeError as exc:
        errors.append({"label": f"jobs {prefix}", "message": str(exc)})
        return []
    jobs = []
    for item in data.get("items", []):
        job_name = item.get("metadata", {}).get("name", "")
        if job_name.startswith(prefix):
            jobs.append((f"{name}:{job_name}", f"job/{job_name}"))
    return sorted(jobs, key=lambda item: item[0], reverse=True)[:12]


def fetch_target_logs(name: str, target: str, since: str, tail: int, errors: list[dict]) -> list[dict]:
    if target.startswith("local-log/"):
        service = target.removeprefix("local-log/")
        return fetch_local_file_logs(name, Path(CONFIG.local_log_dir) / f"{service}.log", since, tail, errors)
    if target.startswith("local-file/"):
        rel_path = target.removeprefix("local-file/")
        return fetch_local_file_logs(name, Path(CONFIG.repo_root) / rel_path, since, tail, errors)

    result = run(
        [
            "kubectl",
            "logs",
            "-n",
            CONFIG.namespace,
            target,
            "--all-containers=true",
            "--timestamps=true",
            f"--since={kubectl_since(since)}",
            f"--tail={tail}",
        ],
        timeout=8,
    )
    if not result["ok"]:
        text = result["error"]
        if "not found" not in text.lower() and "no pods found" not in text.lower():
            errors.append({"label": name, "message": text})
        return []
    return [{"target": name, "line": line} for line in result["stdout"].splitlines() if line.strip()]


def fetch_local_file_logs(name: str, path: Path, since: str, tail: int, errors: list[dict]) -> list[dict]:
    if not path.exists():
        return []
    if not path.is_file():
        errors.append({"label": name, "message": f"{path} is not a file"})
        return []
    result = run(["tail", "-n", str(min(max(tail, 20), 5000)), str(path)], timeout=4)
    if not result["ok"]:
        errors.append({"label": name, "message": result["error"]})
        return []
    cutoff = time.time() - since_seconds(since)
    lines = []
    for line in result["stdout"].splitlines():
        if not line.strip():
            continue
        item_time = log_line_epoch(line)
        if not item_time:
            continue
        if item_time < cutoff:
            continue
        lines.append({"target": name, "line": line})
    return lines


def parse_log_line(target: str, line: str) -> dict:
    kube_time, text = split_kube_timestamp(line)
    json_fields = parse_json_log(text)
    if json_fields:
        level = str(json_fields.get("level") or "").upper()
        if level not in LEVELS:
            level = detect_level(text, {})
        message = str(json_fields.get("msg") or json_fields.get("message") or text)
        service = str(json_fields.get("service") or target)
        category = detect_category(text, {"service": service}, message)
        timestamp = str(json_fields.get("time") or kube_time or "")
        return {
            "time": timestamp,
            "time_sort": timestamp,
            "target": target,
            "service": service,
            "level": level,
            "category": category,
            "message": message,
            "raw": text,
        }
    fields = parse_keyvals(text)
    level = detect_level(text, fields)
    message = fields.get("msg") or fields.get("message") or text
    service = fields.get("service") or target
    category = detect_category(text, fields, message)
    return {
        "time": kube_time or fields.get("time", ""),
        "time_sort": kube_time or fields.get("time", ""),
        "target": target,
        "service": service,
        "level": level,
        "category": category,
        "message": message,
        "raw": text,
    }


def compact_log_line(item: dict) -> dict:
    return {
        "time": item.get("time", ""),
        "time_sort": item.get("time_sort", ""),
        "target": item.get("target", ""),
        "level": item.get("level", ""),
        "category": item.get("category", ""),
        "message": truncate_text(str(item.get("message") or ""), LOG_RESPONSE_FIELD_CHARS),
        "raw": truncate_text(str(item.get("raw") or ""), LOG_RESPONSE_FIELD_CHARS),
    }


def truncate_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def split_kube_timestamp(line: str) -> tuple[str, str]:
    if " " not in line:
        return "", line
    first, rest = line.split(" ", 1)
    if "T" in first and ":" in first:
        return first, rest
    return "", line


def parse_json_log(text: str) -> dict:
    text = text.strip()
    if not text.startswith("{"):
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def parse_keyvals(text: str) -> dict[str, str]:
    fields = {}
    for match in re.finditer(r'(\w+)=("([^"\\]|\\.)*"|\S+)', text):
        key = match.group(1)
        value = match.group(2)
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1].replace(r"\"", '"')
        fields[key] = value
    return fields


def detect_level(text: str, fields: dict[str, str]) -> str:
    explicit = (fields.get("level") or fields.get("lvl") or "").upper()
    if explicit in LEVELS:
        return explicit
    lower = text.lower()
    if any(token in lower for token in ("level=error", " error", "failed", "panic", "fatal", "exception")):
        return "ERROR"
    if any(token in lower for token in ("level=warn", " warning", "timeout", "retry")):
        return "WARN"
    if "level=debug" in lower or lower.startswith("debug"):
        return "DEBUG"
    if "level=info" in lower or lower.startswith("info"):
        return "INFO"
    return "OTHER"


def detect_category(text: str, fields: dict[str, str], message: str) -> str:
    lower = f"{text} {message}".lower()
    if "record" in lower or "ffmpeg" in lower or "recording" in lower:
        return "record"
    if "upload" in lower or "archive" in lower or "bitable" in lower or "bilibili" in lower or "biliup" in lower or "lark" in lower or "feishu" in lower:
        return "upload"
    if "postgres" in lower or "driver.query" in lower or "sql" in lower:
        return "db"
    if "live" in lower or "schedule" in lower:
        return "live"
    if "transcode" in lower or "encoder" in lower:
        return "transcode"
    if fields.get("service"):
        return fields["service"]
    return "system"


def apply_filters(lines: list[dict], level: str, keyword: str) -> list[dict]:
    lines = list(lines)
    if level != "ALL":
        lines = [item for item in lines if item.get("level") == level]
    keyword = keyword.strip().lower()
    if keyword:
        lines = [
            item
            for item in lines
            if keyword in item.get("raw", "").lower()
            or keyword in item.get("message", "").lower()
            or keyword in item.get("target", "").lower()
        ]
    return lines


def collect_issues(lines: list[dict], log_errors: list[dict]) -> list[dict]:
    issues: list[dict] = []
    for err in log_errors:
        issues.append(issue("bad", "日志采集", err["label"], err["message"]))
    recent_lines = [item for item in lines if is_recent_log(item, CURRENT_LOG_SECONDS) and not is_benign_log_item(item)]
    error_logs = [item for item in recent_lines if item.get("level") == "ERROR"]
    warn_logs = [item for item in recent_lines if item.get("level") == "WARN"]
    if error_logs:
        issues.append(issue("warn", "日志", "近期 ERROR 日志", f"最近 15 分钟有 {len(error_logs)} 条 ERROR"))
    if warn_logs:
        issues.append(issue("warn", "日志", "近期 WARN 日志", f"最近 15 分钟有 {len(warn_logs)} 条 WARN"))

    collect_kubernetes_issues(issues)
    collect_database_issues(issues)
    collect_storage_issues(issues)
    collect_external_issues(issues)
    collect_local_log_issues(issues)
    rank = {"bad": 0, "warn": 1, "ok": 2}
    issues.sort(key=lambda item: (rank.get(item["severity"], 9), item["area"], item["title"]))
    return issues[:80]


def collect_kubernetes_issues(issues: list[dict]) -> None:
    data = kubectl_json(["get", "deploy", "-n", CONFIG.namespace, "-o", "json"], "deployments", issues)
    for item in data.get("items", []) or []:
        name = item.get("metadata", {}).get("name", "")
        desired = int(item.get("spec", {}).get("replicas") or 0)
        ready = int(item.get("status", {}).get("readyReplicas") or 0)
        updated = int(item.get("status", {}).get("updatedReplicas") or 0)
        if desired > 0 and ready < desired:
            issues.append(issue("bad", "Kubernetes", name, f"Deployment ready {ready}/{desired}, updated {updated}"))

    data = kubectl_json(["get", "pods", "-n", CONFIG.namespace, "-o", "json"], "pods", issues)
    for item in data.get("items", []) or []:
        name = item.get("metadata", {}).get("name", "")
        phase = item.get("status", {}).get("phase", "")
        statuses = item.get("status", {}).get("containerStatuses", []) or []
        restarts = sum(int(status.get("restartCount") or 0) for status in statuses)
        waiting = [
            status.get("state", {}).get("waiting", {}).get("reason", "")
            for status in statuses
            if status.get("state", {}).get("waiting")
        ]
        if phase == "Failed" and pod_owned_by_job(item):
            continue
        if phase not in ("Running", "Succeeded"):
            issues.append(issue("bad", "Kubernetes", name, f"Pod phase={phase}"))
        elif waiting:
            issues.append(issue("bad", "Kubernetes", name, "Container waiting: " + ", ".join(filter(None, waiting))))
        elif restarts and has_recent_restart(statuses):
            issues.append(issue("warn", "Kubernetes", name, f"Container restarts={restarts}"))

    compensated_record_tasks = compensated_failed_record_task_ids()
    data = kubectl_json(["get", "jobs", "-n", CONFIG.namespace, "-o", "json"], "jobs", issues)
    for item in data.get("items", []) or []:
        name = item.get("metadata", {}).get("name", "")
        status = item.get("status", {})
        failed = int(status.get("failed") or 0)
        succeeded = int(status.get("succeeded") or 0)
        active = int(status.get("active") or 0)
        record_task = re.fullmatch(r"record-(\d+)", name)
        if record_task and record_task.group(1) in compensated_record_tasks:
            continue
        if failed and not succeeded and (active or job_is_recent(item, JOB_ISSUE_SECONDS)):
            issues.append(issue("bad", "Kubernetes", name, f"Job failed pods={failed}"))


def collect_database_issues(issues: list[dict]) -> None:
    failed_queries = [
        (
            "录制任务",
            """
            select rt.id, rt.role, coalesce(rt.error_message, ''), rt.output_path
            from record_tasks rt
            join match_rounds mr on mr.id = rt.match_round_record_tasks
            join matches m on m.id = mr.match_rounds
            where rt.status = 'FAILED'
              and rt.updated_at > now() - interval '24 hours'
              and m.event not like '%测试%'
              and m.zone not like '%测试%'
              and not (
                rt.role like '%__part%'
                and exists (
                  select 1
                  from record_tasks merged_rt
                  join match_rounds merged_mr on merged_mr.id = merged_rt.match_round_record_tasks
                  join media_artifacts merged_ma on merged_ma.record_task_media_artifacts = merged_rt.id
                  where merged_mr.match_rounds = mr.match_rounds
                    and merged_rt.status = 'SUCCEEDED'
                    and merged_ma.kind = 'source'
                    and merged_rt.role = regexp_replace(rt.role, '__part[0-9]+$', '')
                )
              )
            order by rt.updated_at desc
            limit 10;
            """,
        ),
        (
            "转码任务",
            """
            select tt.id, coalesce(rt.role, ''), coalesce(tt.error_message, ''), coalesce(ma.path, '')
            from transcode_tasks tt
            left join media_artifacts ma on ma.id = tt.media_artifact_source_transcode_task
            left join record_tasks rt on rt.id = ma.record_task_media_artifacts
            left join match_rounds mr on mr.id = rt.match_round_record_tasks
            left join matches m on m.id = mr.match_rounds
            where tt.status = 'FAILED'
              and tt.updated_at > now() - interval '24 hours'
              and coalesce(m.event, '') not like '%测试%'
              and coalesce(m.zone, '') not like '%测试%'
            order by tt.updated_at desc
            limit 10;
            """,
        ),
        (
            "上传任务",
            """
            select ut.id, coalesce(rt.role, ''), coalesce(ut.error_message, ''), ut.source_path
            from upload_tasks ut
            left join record_tasks rt on rt.id = ut.record_task_upload_task
            left join match_rounds mr on mr.id = rt.match_round_record_tasks
            left join matches m on m.id = mr.match_rounds
            where ut.status = 'FAILED'
              and ut.updated_at > now() - interval '24 hours'
              and coalesce(m.event, '') not like '%测试%'
              and coalesce(m.zone, '') not like '%测试%'
            order by ut.updated_at desc
            limit 10;
            """,
        ),
    ]
    for area, query in failed_queries:
        for row in psql(query, area, issues):
            if len(row) >= 4:
                detail = row[2] or row[3]
                issues.append(issue("bad", area, f"{area} #{row[0]} {row[1]}".strip(), detail))

    query = """
        select
          m.zone,
          m."order",
          count(rt.id),
          count(rt.id) filter (where rt.status in ('PENDING', 'DISPATCHING', 'RUNNING'))
        from matches m
        left join match_rounds mr on mr.match_rounds = m.id
        left join record_tasks rt on rt.match_round_record_tasks = mr.id
        where m.latest_status = 'STARTED'
        group by m.id
        having count(rt.id) = 0
            or count(rt.id) filter (where rt.status in ('PENDING', 'DISPATCHING', 'RUNNING')) = 0;
    """
    for row in psql(query, "started_matches_without_record_tasks", issues):
        if len(row) >= 4:
            detail = "比赛已 STARTED，但没有录制任务"
            if to_int(row[2]) > 0:
                detail = f"比赛已 STARTED，但没有运行中的录制任务；现有录制任务 {row[2]} 个"
            issues.append(issue("bad", "录制链路", f"{row[0]}第{row[1]}场", detail))


def compensated_failed_record_task_ids() -> set[str]:
    query = """
        select rt.id
        from record_tasks rt
        join match_rounds mr on mr.id = rt.match_round_record_tasks
        join matches m on m.id = mr.match_rounds
        where rt.status = 'FAILED'
          and rt.updated_at > now() - interval '24 hours'
          and rt.role like '%__part%'
          and m.event not like '%测试%'
          and m.zone not like '%测试%'
          and exists (
            select 1
            from record_tasks merged_rt
            join match_rounds merged_mr on merged_mr.id = merged_rt.match_round_record_tasks
            join media_artifacts merged_ma on merged_ma.record_task_media_artifacts = merged_rt.id
            where merged_mr.match_rounds = mr.match_rounds
              and merged_rt.status = 'SUCCEEDED'
              and merged_ma.kind = 'source'
              and merged_rt.role = regexp_replace(rt.role, '__part[0-9]+$', '')
          );
    """
    return {row[0] for row in psql(query, "compensated_failed_record_tasks", []) if row}


def collect_storage_issues(issues: list[dict]) -> None:
    for mount in ("/mnt/PC801", "/mnt/server_data"):
        result = run(["df", "-B1", "--output=target,size,used,avail,pcent", mount], timeout=4)
        if not result["ok"]:
            issues.append(issue("bad", "存储", mount, result["error"]))
            continue
        lines = [line for line in result["stdout"].splitlines() if line.strip()]
        if len(lines) < 2:
            issues.append(issue("bad", "存储", mount, "df 输出异常"))
            continue
        parts = lines[-1].split()
        if len(parts) < 5:
            issues.append(issue("bad", "存储", mount, "df 输出无法解析"))
            continue
        used_pct = to_int(parts[4].rstrip("%"))
        if used_pct >= 95:
            issues.append(issue("bad", "存储", mount, f"磁盘使用率 {used_pct}%"))
        elif used_pct >= 85:
            issues.append(issue("warn", "存储", mount, f"磁盘使用率 {used_pct}%"))


def collect_local_log_issues(issues: list[dict]) -> None:
    log_dir = Path(CONFIG.local_log_dir)
    if not log_dir.exists():
        return
    cutoff = time.time() - LOCAL_ISSUE_SECONDS
    events = []
    for path in sorted(log_dir.glob("*.log")):
        result = run(["tail", "-n", "1000", str(path)], timeout=4)
        if not result["ok"]:
            issues.append(issue("bad", "本机日志", path.name, result["error"]))
            continue
        for line in result["stdout"].splitlines():
            fields = parse_json_log(line)
            if not fields:
                continue
            timestamp = parse_timestamp_epoch(str(fields.get("time") or ""))
            if timestamp and timestamp < cutoff:
                continue
            events.append({"path": path, "fields": fields, "timestamp": timestamp})

    successful_at: dict[str, float] = {}
    for event in events:
        fields = event["fields"]
        if not is_local_success_event(fields):
            continue
        for key in local_event_keys(fields, event["path"]):
            successful_at[key] = max(successful_at.get(key, 0), event["timestamp"] or 0)

    hits = []
    for event in events:
        fields = event["fields"]
        if is_benign_json_event(fields):
            continue
        level = str(fields.get("level") or "").upper()
        if level not in ("ERROR", "WARN"):
            continue
        keys = local_event_keys(fields, event["path"])
        timestamp = event["timestamp"] or 0
        if any(successful_at.get(key, 0) > timestamp for key in keys):
            continue
        service = str(fields.get("service") or event["path"].stem)
        message = str(fields.get("msg") or fields.get("detail") or fields.get("error") or "")
        detail = message or str(fields)
        if fields.get("detail"):
            detail += f": {fields['detail']}"
        if fields.get("exit_code") is not None:
            detail += f" (exit_code={fields['exit_code']})"
        hits.append((timestamp, issue("bad" if level == "ERROR" else "warn", "本机脚本", service, detail)))
    hits.sort(key=lambda item: item[0])
    issues.extend(item[1] for item in hits[-8:])


def collect_external_issues(issues: list[dict]) -> None:
    for name, url in (("官方赛程", SCHEDULE_URL), ("官方直播", LIVE_INFO_URL)):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "RMMonitorDashboard/1.0"})
            with urllib.request.urlopen(req, timeout=4) as resp:
                resp.read(64)
                if not 200 <= resp.status < 400:
                    issues.append(issue("bad", "外部源", name, f"HTTP {resp.status}"))
        except Exception as exc:
            issues.append(issue("bad", "外部源", name, str(exc)))


def collect_current_snapshot() -> dict:
    current = {
        "deployments": {"ready": 0, "desired": 0},
        "pods": {"running": 0, "succeeded": 0, "other": 0},
        "storage": [],
        "external": [],
    }
    data = kubectl_json(["get", "deploy", "-n", CONFIG.namespace, "-o", "json"], "deployments", [])
    for item in data.get("items", []) or []:
        current["deployments"]["desired"] += int(item.get("spec", {}).get("replicas") or 0)
        current["deployments"]["ready"] += int(item.get("status", {}).get("readyReplicas") or 0)

    data = kubectl_json(["get", "pods", "-n", CONFIG.namespace, "-o", "json"], "pods", [])
    for item in data.get("items", []) or []:
        phase = item.get("status", {}).get("phase", "")
        if phase == "Running":
            current["pods"]["running"] += 1
        elif phase == "Succeeded" or (phase == "Failed" and pod_owned_by_job(item)):
            current["pods"]["succeeded"] += 1
        else:
            current["pods"]["other"] += 1

    for mount in ("/mnt/PC801", "/mnt/server_data"):
        current["storage"].append(storage_snapshot(mount))
    for name, url in (("官方赛程", SCHEDULE_URL), ("官方直播", LIVE_INFO_URL)):
        current["external"].append(external_snapshot(name, url))
    return current


def collect_pipeline_progress() -> dict:
    scope = current_match_scope()
    matches = collect_match_progress(scope)
    by_id = {item["match_id"]: item for item in matches}
    return {
        "scope": scope,
        "steps": [
            record_pipeline_step(matches),
            artifact_pipeline_step(matches),
            archive_pipeline_step(by_id),
            upload_pipeline_step(by_id),
            cleanup_pipeline_step(by_id),
        ],
        "backup_cache": official_backup_cache_step(),
    }


def current_match_scope() -> dict:
    today = time.strftime("%Y-%m-%d")
    zones = active_schedule_zones(today)
    return {
        "date": today,
        "zones": zones,
        "source": "official-schedule" if zones else "updated-today",
    }


def active_schedule_zones(date_text: str) -> list[str]:
    try:
        req = urllib.request.Request(SCHEDULE_URL, headers={"User-Agent": "RMMonitorDashboard/1.0"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.load(resp)
    except Exception:
        return []
    zones: set[str] = set()

    def walk(item):
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("zoneName") or "")
            match_dates = item.get("matchDates")
            if name and isinstance(match_dates, list) and date_text in match_dates:
                zones.add(name)
            for value in item.values():
                walk(value)
        elif isinstance(item, list):
            for value in item:
                walk(value)

    walk(data)
    return sorted(zones)


def official_backup_cache_step() -> dict:
    service = local_systemd_service_status(OFFICIAL_BACKUP_CACHE_SERVICE)
    events = read_json_log_events(Path(CONFIG.local_log_dir) / "official-backup-cache.log", 1000)
    latest_script_start = latest_event(events, "script started")
    start_ts = float((latest_script_start or {}).get("_timestamp") or 0)
    current_events = [item for item in events if not start_ts or float(item.get("_timestamp") or 0) >= start_ts]
    gate = latest_event(current_events, "schedule gate changed")
    last_archive = latest_event(current_events, "segment archived")
    errors = [item for item in current_events if str(item.get("level") or "").upper() == "ERROR"]
    ffmpeg = official_backup_cache_ffmpeg()
    target_stats = flv_tree_stats(official_backup_cache_day_dir(OFFICIAL_BACKUP_CACHE_TARGET_ROOT))
    source_stats = flv_tree_stats(official_backup_cache_day_dir(OFFICIAL_BACKUP_CACHE_SOURCE_ROOT))

    service_active = service.get("active_state") == "active"
    if not service.get("ok"):
        state = "bad"
        metric = "服务未知"
    elif not service_active:
        state = "bad"
        metric = service.get("active_state") or "未运行"
    elif errors:
        state = "bad"
        metric = "脚本报错"
    elif ffmpeg["count"] > 0:
        state = "running"
        metric = f"{ffmpeg['count']} 路录制中"
    else:
        state = "idle"
        reason = str((gate or {}).get("reason") or "")
        metric = "待第一场" if "before first planned match" in reason else "空转等待"

    detail_parts = [
        f"长期归档 {target_stats['files']} 个 / {human_bytes(target_stats['bytes'])}",
        f"本地临时 {source_stats['files']} 个 / {human_bytes(source_stats['bytes'])}",
    ]
    roles = ffmpeg.get("roles") or []
    if roles:
        detail_parts.append("当前 " + "、".join(roles[:4]) + (" 等" if len(roles) > 4 else ""))
    if gate and gate.get("reason"):
        detail_parts.append(backup_cache_reason_text(str(gate["reason"])))
    elif last_archive:
        detail_parts.append("上次归档 " + event_clock(last_archive))
    elif service.get("ok"):
        detail_parts.append(f"systemd {service.get('active_state')}/{service.get('sub_state')}")
    else:
        detail_parts.append(str(service.get("error") or "无法读取 systemd 状态"))

    return {
        "label": "正赛备用缓存",
        "state": state,
        "match_id": "",
        "match_label": "独立链路，不上传 B 站",
        "metric": metric,
        "detail": "；".join(detail_parts),
        "archive_files": target_stats["files"],
        "archive_bytes": target_stats["bytes"],
        "source_files": source_stats["files"],
        "source_bytes": source_stats["bytes"],
        "service_active": service_active,
        "ffmpeg_count": ffmpeg["count"],
    }


def collect_match_progress(scope: dict) -> list[dict]:
    zones = list(scope.get("zones") or [])
    if zones:
        zone_filter = "and m.zone in (" + ", ".join(sql_literal(zone) for zone in zones) + ")"
        time_filter = ""
    else:
        zone_filter = ""
        time_filter = "and m.updated_at >= date_trunc('day', now())"
    query = """
        select
            m.id,
            m.event,
            m.zone,
            m."order",
            m.match_type,
            coalesce(m.match_slug, ''),
            m.latest_status,
            rt.school_name,
            rt.name,
            bt.school_name,
            bt.name,
            count(distinct mr.id) filter (where mr.winner = 'red'),
            count(distinct mr.id) filter (where mr.winner = 'blue'),
            count(distinct rec.id),
            count(distinct rec.id) filter (where rec.status in ('PENDING', 'DISPATCHING', 'RUNNING')),
            count(distinct rec.id) filter (where rec.status = 'FAILED'),
            count(distinct rec.id) filter (where rec.status = 'SUCCEEDED'),
            count(distinct ma.id) filter (where ma.kind = 'source' and rec.role not like '%__part%'),
            coalesce(sum(ma.file_size) filter (where ma.kind = 'source' and rec.role not like '%__part%'), 0),
            count(distinct ma.id) filter (where ma.kind = 'source' and rec.role like '%__part%')
        from matches m
        join teams rt on rt.id = m.team_red_matches
        join teams bt on bt.id = m.team_blue_matches
        left join match_rounds mr on mr.match_rounds = m.id
        left join record_tasks rec on rec.match_round_record_tasks = mr.id
        left join media_artifacts ma on ma.record_task_media_artifacts = rec.id
        where m.event not like '%测试%'
          and m.zone not like '%测试%'
          {zone_filter}
          {time_filter}
        group by m.id, rt.id, bt.id
        order by m."order";
    """.format(zone_filter=zone_filter, time_filter=time_filter)
    matches = []
    for row in psql(query, "pipeline_matches", []):
        if len(row) < 20:
            continue
        item = {
            "match_id": row[0],
            "event": row[1],
            "zone": row[2],
            "order": to_int(row[3]),
            "match_type": row[4],
            "match_slug": row[5],
            "latest_status": row[6],
            "red_school": row[7],
            "red_name": row[8],
            "blue_school": row[9],
            "blue_name": row[10],
            "red_score": to_int(row[11]),
            "blue_score": to_int(row[12]),
            "record_tasks": to_int(row[13]),
            "record_running": to_int(row[14]),
            "record_failed": to_int(row[15]),
            "record_succeeded": to_int(row[16]),
            "source_artifacts": to_int(row[17]),
            "source_bytes": to_int(row[18]),
            "source_part_artifacts": to_int(row[19]),
        }
        item["label"] = match_progress_label(item)
        matches.append(item)
    return matches


def record_pipeline_step(matches: list[dict]) -> dict:
    running = [item for item in matches if item["latest_status"] == "STARTED" or item["record_running"] > 0]
    if running:
        item = running[-1]
        expected = max(14, item["record_tasks"], item["record_running"])
        return pipeline_step(
            "自动录制",
            "running",
            item,
            f"{item['record_running']}/{expected} 路",
            f"源文件 {item['source_artifacts']} 个，失败任务 {item['record_failed']} 个",
        )
    done = latest_match(
        matches,
        lambda item: item["latest_status"] == "DONE"
        or (
            item["record_tasks"] > 0
            and item["record_running"] == 0
            and item["record_failed"] == 0
            and item["record_succeeded"] > 0
        ),
    )
    if done:
        return pipeline_step("自动录制", "done", done, "已收尾", f"成功任务 {done['record_succeeded']} 个")
    return pipeline_step("自动录制", "idle", None, "等待", "暂无正在录制的比赛")


def artifact_pipeline_step(matches: list[dict]) -> dict:
    current = latest_match(matches, lambda item: item["source_artifacts"] > 0)
    if current:
        state = "running" if current["latest_status"] == "STARTED" else "done"
        metric = f"{current['source_artifacts']} 个源文件"
        detail = f"合计 {human_bytes(current['source_bytes'])}"
        return pipeline_step("最终文件", state, current, metric, detail)
    return pipeline_step("最终文件", "idle", None, "等待", "暂无源文件产物")


def archive_pipeline_step(matches_by_id: dict[str, dict]) -> dict:
    queue_status = latest_queue_status(Path(CONFIG.local_log_dir) / "archive-auto-queue.log", matches_by_id)
    status = archive_artifact_status(matches_by_id)
    if (
        queue_status
        and queue_status.get("state") == "done"
        and status
        and status.get("state") == "running"
        and status.get("detail") == "archive plan ready"
        and match_id_of_status(queue_status) == match_id_of_status(status)
    ):
        return pipeline_step("长期归档", "done", queue_status.get("match"), queue_status.get("metric", ""), queue_status.get("detail", ""))
    if status:
        return pipeline_step(
            "长期归档",
            status["state"],
            status.get("match"),
            status.get("metric", ""),
            status.get("detail", ""),
        )
    if queue_status:
        return pipeline_step("长期归档", queue_status["state"], queue_status.get("match"), queue_status.get("metric", ""), queue_status.get("detail", ""))
    return pipeline_step("长期归档", "idle", None, "等待", "暂无归档记录")


def upload_pipeline_step(matches_by_id: dict[str, dict]) -> dict:
    events = read_json_log_events(Path(CONFIG.local_log_dir) / "biliup-upload.log", 1200)
    starts = [
        item
        for item in events
        if event_message(item) == "biliup upload started" and event_match(item, matches_by_id)
    ]
    completions = [
        item
        for item in events
        if event_message(item) in ("biliup submit completed", "biliup upload workflow completed", "existing BVID workflow completed")
        and event_match(item, matches_by_id)
    ]
    failures = [
        item
        for item in events
        if event_message(item) in ("biliup upload failed", "biliup submit rate limited")
        and event_match(item, matches_by_id)
    ]
    latest_start = starts[-1] if starts else None
    latest_completion = completions[-1] if completions else None
    latest_failure = failures[-1] if failures else None
    checkpoint = biliup_checkpoint()
    if latest_start and not event_after_for_match(latest_completion, latest_start):
        if event_after_for_match(latest_failure, latest_start):
            match = event_match(latest_failure, matches_by_id) or event_match(latest_start, matches_by_id)
            message = event_message(latest_failure)
            if message == "biliup submit rate limited":
                return pipeline_step("B站上传", "bad", match, "投稿限流", "B站返回投稿过于频繁，等待自动重试")
            return pipeline_step(
                "B站上传",
                "bad",
                match,
                f"失败 {latest_failure.get('exit_code', '')}".strip(),
                "上传流程已报错，源文件仍保留",
            )
        match = event_match(latest_start, matches_by_id)
        total = to_int(latest_start.get("videos")) or (match or {}).get("source_artifacts") or 14
        done = checkpoint.get("completed")
        if done is not None:
            metric = f"{done}/{total} 分P"
        elif checkpoint.get("all_uploaded"):
            metric = f"{total}/{total} 分P"
        else:
            metric = f"{total} 分P"
        detail_parts = []
        if checkpoint.get("speed"):
            detail_parts.append(f"最近 {checkpoint['speed']}")
        if checkpoint.get("current_file"):
            detail_parts.append(f"当前 {checkpoint['current_file']}")
        return pipeline_step("B站上传", "running", match, metric, "，".join(detail_parts) or "上传中")
    if latest_completion:
        match = event_match(latest_completion, matches_by_id)
        metric = str(latest_completion.get("bvid") or "已投稿")
        return pipeline_step("B站上传", "done", match, metric, "飞书回填和后续清理由上传流程处理")
    return pipeline_step("B站上传", "idle", None, "等待", "暂无上传记录")


def cleanup_pipeline_step(matches_by_id: dict[str, dict]) -> dict:
    events = read_json_log_events(Path(CONFIG.local_log_dir) / "biliup-upload.log", 1200)
    starts = [
        item
        for item in events
        if event_message(item) == "local source deletion started after upload and archive"
        and event_match(item, matches_by_id)
    ]
    completions = [
        item
        for item in events
        if event_message(item) == "local source deletion completed after upload and archive"
        and event_match(item, matches_by_id)
    ]
    latest_start = starts[-1] if starts else None
    latest_completion = completions[-1] if completions else None
    if latest_start and not event_after_for_match(latest_completion, latest_start):
        return pipeline_step("本地清理", "running", event_match(latest_start, matches_by_id), "删除中", "归档可先行；本地源文件删除需等上传完成并确认长期归档")
    if latest_completion:
        return pipeline_step("本地清理", "done", event_match(latest_completion, matches_by_id), "已清理", "上传完成且长期归档已校验，本地源文件已释放")
    return pipeline_step("本地清理", "idle", None, "等待", "长期归档不等上传；这里只等待可安全删除本地源文件")


def local_systemd_service_status(service_name: str) -> dict:
    result = run(
        [
            "systemctl",
            "show",
            service_name,
            "--no-pager",
            "--property=ActiveState",
            "--property=SubState",
            "--property=UnitFileState",
            "--property=ExecMainPID",
        ],
        timeout=3,
    )
    if not result["ok"]:
        return {"ok": False, "error": result["error"]}
    fields = {}
    for line in result["stdout"].splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key] = value
    return {
        "ok": True,
        "active_state": fields.get("ActiveState", ""),
        "sub_state": fields.get("SubState", ""),
        "unit_file_state": fields.get("UnitFileState", ""),
        "pid": to_int(fields.get("ExecMainPID")),
    }


def official_backup_cache_ffmpeg() -> dict:
    result = run(["pgrep", "-af", "ffmpeg"], timeout=3)
    lines = []
    if result["stdout"]:
        lines = [
            line
            for line in result["stdout"].splitlines()
            if "ffmpeg" in line and "official-backup-cache" in line and "正赛备用缓存" in line
        ]
    roles = []
    for line in lines:
        match = re.search(r"正赛备用缓存/([^/]+)/[^/]+\.flv", line)
        if match:
            roles.append(match.group(1))
    return {"count": len(lines), "roles": sorted(set(roles))}


def backup_cache_reason_text(reason: str) -> str:
    match = re.search(r"before first planned match at (.+)$", reason)
    if match:
        return f"第一场前等待：{match.group(1)}"
    match = re.search(r"after daily hard stop for last planned match at (.+)$", reason)
    if match:
        return f"已过当天兜底停止时间：{match.group(1)}"
    if "inside planned match day window" in reason:
        return "正赛窗口内，等待或录制直播信号"
    if "all planned matches for date are terminal" in reason:
        return "当天计划场次已全部结束"
    if "date is not in zone matchDates" in reason:
        return "今天不是该赛区比赛日"
    if "schedule unavailable" in reason:
        return "赛程暂不可用"
    return reason


def official_backup_cache_day_dir(root: Path) -> Path:
    return (
        root
        / OFFICIAL_BACKUP_CACHE_EVENT
        / OFFICIAL_BACKUP_CACHE_ZONE
        / f"{time.strftime('%Y-%m-%d')} {OFFICIAL_BACKUP_CACHE_SESSION}"
    )


def flv_tree_stats(root: Path) -> dict:
    stats = {"files": 0, "bytes": 0}
    if not root.exists() or not root.is_dir():
        return stats
    try:
        files = list(root.rglob("*.flv"))
    except OSError:
        return stats
    for path in files:
        try:
            if not path.is_file():
                continue
            stat = path.stat()
        except OSError:
            continue
        stats["files"] += 1
        stats["bytes"] += stat.st_size
    return stats


def latest_event(events: list[dict], message: str) -> dict | None:
    for item in reversed(events):
        if event_message(item) == message:
            return item
    return None


def event_clock(item: dict) -> str:
    value = str(item.get("time") or "")
    timestamp = parse_timestamp_epoch(value)
    if timestamp:
        return time.strftime("%H:%M:%S", time.localtime(timestamp))
    return value or "--"


def pipeline_step(label: str, state: str, match: dict | None, metric: str, detail: str) -> dict:
    return {
        "label": label,
        "state": state,
        "match_id": match.get("match_id") if match else "",
        "match_label": match.get("label") if match else "暂无场次",
        "metric": metric,
        "detail": detail,
    }


def match_id_of_status(status: dict | None) -> str:
    if not status:
        return ""
    match = status.get("match") or {}
    return str(match.get("match_id") or status.get("match_id") or "")


def latest_match(matches: list[dict], predicate) -> dict | None:
    result = None
    for item in matches:
        if predicate(item):
            result = item
    return result


def latest_queue_status(path: Path, matches_by_id: dict[str, dict]) -> dict | None:
    events = read_json_log_events(path, 1200)
    selected = [
        item
        for item in events
        if event_message(item) in ("archive candidate selected", "candidate selected")
        and event_match(item, matches_by_id)
    ]
    finished = [
        item
        for item in events
        if event_message(item) in ("archive candidate finished", "candidate workflow finished")
        and event_match(item, matches_by_id)
    ]
    if selected and not event_after_for_match(finished[-1] if finished else None, selected[-1]):
        return {"state": "running", "match": event_match(selected[-1], matches_by_id), "metric": "进行中", "detail": event_message(selected[-1])}
    if finished:
        return {"state": "done", "match": event_match(finished[-1], matches_by_id), "metric": "已完成", "detail": event_message(finished[-1])}
    return None


def archive_artifact_status(matches_by_id: dict[str, dict]) -> dict | None:
    events = read_json_log_events(Path(CONFIG.local_log_dir) / "archive-artifacts.log", 1600)
    states: dict[str, dict] = {}
    latest: dict | None = None
    last_copy_match_id = ""
    last_delete_match_id = ""
    for item in events:
        match = archive_event_match(item, matches_by_id)
        msg = event_message(item)
        timestamp = item.get("_timestamp") or 0
        if msg != "archive completed" and match is None:
            continue
        match_id = str((match or {}).get("match_id") or "")
        if msg == "archive plan ready" and match_id:
            states[match_id] = {
                "match": match,
                "total": to_int(item.get("artifacts")) or (match or {}).get("source_artifacts") or 14,
                "copied_ids": set(),
                "deleted_ids": set(),
                "delete_source": bool(item.get("delete_source")),
                "last_ts": timestamp,
            }
        state = states.get(match_id) if match_id else None
        if state is None and match_id:
            state = states.setdefault(
                match_id,
                {
                    "match": match,
                    "total": (match or {}).get("source_artifacts") or 14,
                    "copied_ids": set(),
                    "deleted_ids": set(),
                    "delete_source": False,
                    "last_ts": timestamp,
                },
            )
        if msg == "copy verified":
            if state and state.get("delete_source"):
                continue
            if state is None:
                continue
            if item.get("artifact_id") is not None:
                state["copied_ids"].add(str(item.get("artifact_id")))
            else:
                state["copied_ids"].add(str(len(state["copied_ids"]) + 1))
            state["last_ts"] = timestamp
            copied = len(state["copied_ids"])
            total = state.get("total") or 14
            last_copy_match_id = match_id
            latest = {
                "state": "running",
                "match": state.get("match"),
                "metric": f"{copied}/{total} 文件",
                "detail": f"正在复制 {item.get('role') or ''}".strip(),
                "timestamp": timestamp,
            }
        elif msg == "source deleted after upload and archive":
            if state and item.get("artifact_id") is not None:
                state["deleted_ids"].add(str(item.get("artifact_id")))
                state["last_ts"] = timestamp
                last_delete_match_id = match_id
        elif msg == "archive completed":
            copied = to_int(item.get("copied"))
            deleted = to_int(item.get("deleted"))
            done_match_id = last_copy_match_id if copied else last_delete_match_id
            state = states.get(done_match_id)
            if not state or state.get("delete_source") or not copied:
                continue
            latest = {
                "state": "done",
                "match": state.get("match"),
                "metric": f"{copied or len(state['copied_ids'])} 个文件",
                "detail": "复制校验完成",
                "timestamp": timestamp,
            }
        elif msg in ("archive plan ready", "archive match lock acquired"):
            if state and state.get("delete_source"):
                continue
            if state is None:
                continue
            state["last_ts"] = timestamp
            latest = {
                "state": "running",
                "match": state.get("match"),
                "metric": "准备中",
                "detail": msg,
                "timestamp": timestamp,
            }
    return latest


def archive_event_match(item: dict, matches_by_id: dict[str, dict]) -> dict | None:
    match_id = str(item.get("match_id") or "")
    if match_id:
        return matches_by_id.get(match_id)
    path_text = " ".join(str(item.get(key) or "") for key in ("source", "target"))
    match = re.search(r"/(\d+)\.\s*[^/]+/", path_text)
    if not match:
        return None
    order = int(match.group(1))
    zone_hint = ""
    zone_match = re.search(r"/([^/]*赛区)/\d+\.\s*[^/]+/", path_text)
    if zone_match:
        zone_hint = zone_match.group(1)
    candidates = [item for item in matches_by_id.values() if to_int(item.get("order")) == order]
    if zone_hint:
        zoned = [item for item in candidates if zone_hint in str(item.get("zone") or "")]
        if zoned:
            return zoned[-1]
    return candidates[-1] if candidates else None


def sql_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def read_json_log_events(path: Path, tail: int) -> list[dict]:
    if not path.exists() or not path.is_file():
        return []
    result = run(["tail", "-n", str(tail), str(path)], timeout=4)
    if not result["ok"]:
        return []
    events = []
    for line in result["stdout"].splitlines():
        fields = parse_json_log(line)
        if not fields:
            continue
        fields["_timestamp"] = parse_timestamp_epoch(str(fields.get("time") or ""))
        events.append(fields)
    events.sort(key=lambda item: item.get("_timestamp") or 0)
    return events


def event_message(item: dict | None) -> str:
    if not item:
        return ""
    return str(item.get("msg") or item.get("message") or "")


def event_match(item: dict | None, matches_by_id: dict[str, dict]) -> dict | None:
    if not item:
        return None
    match_id = str(item.get("match_id") or "")
    return matches_by_id.get(match_id) if match_id else None


def event_after_for_match(candidate: dict | None, reference: dict | None) -> bool:
    if not candidate or not reference:
        return False
    candidate_match = str(candidate.get("match_id") or "")
    reference_match = str(reference.get("match_id") or "")
    if candidate_match and reference_match and candidate_match != reference_match:
        return False
    return (candidate.get("_timestamp") or 0) >= (reference.get("_timestamp") or 0)


def biliup_checkpoint() -> dict:
    path = Path(CONFIG.repo_root) / "download.log"
    if not path.exists() or not path.is_file():
        return {}
    result = run(["tail", "-n", "300", str(path)], timeout=4)
    if not result["ok"]:
        return {}
    checkpoint: dict[str, object] = {}
    all_uploaded_ts = 0.0
    for line in result["stdout"].splitlines():
        fields = parse_json_log(line)
        if fields and event_message(fields) == "biliup upload started":
            checkpoint = {}
            all_uploaded_ts = 0.0
            continue
        line_ts = log_line_epoch(line)
        if all_uploaded_ts and line_ts and line_ts < all_uploaded_ts:
            continue
        match = re.search(r"Found checkpoint with (\d+) uploaded files", line)
        if match:
            checkpoint["completed"] = int(match.group(1))
            checkpoint["current_file"] = ""
            checkpoint.pop("all_uploaded", None)
            checkpoint.pop("submit_completed", None)
        if "No checkpoint found, starting fresh upload" in line:
            checkpoint["completed"] = 0
            checkpoint["current_file"] = ""
            checkpoint.pop("all_uploaded", None)
            checkpoint.pop("submit_completed", None)
        match = re.search(r"Upload completed: (.+?) => cost [^,]+, ([0-9.]+ MB/s)", line)
        if match:
            checkpoint["last_file"] = match.group(1)
            checkpoint["speed"] = match.group(2)
            checkpoint.pop("all_uploaded", None)
            checkpoint.pop("submit_completed", None)
        match = re.search(r"Checkpoint saved: (\d+) files uploaded", line)
        if match:
            checkpoint["completed"] = int(match.group(1))
            checkpoint.pop("all_uploaded", None)
            checkpoint.pop("submit_completed", None)
        match = re.search(r'"name":"([^"]+)"', line)
        if "pre_upload" in line and match:
            checkpoint["current_file"] = match.group(1)
            checkpoint.pop("all_uploaded", None)
            checkpoint.pop("submit_completed", None)
        if "All files uploaded successfully" in line:
            checkpoint["completed"] = None
            checkpoint["current_file"] = ""
            checkpoint["all_uploaded"] = True
            all_uploaded_ts = line_ts or all_uploaded_ts
        if "ResponseData" in line and "code: 0" in line:
            checkpoint["submit_completed"] = True
            checkpoint["submit_completed_time"] = line_ts
            bvid_match = re.search(r"\bBV[0-9A-Za-z]{10,}\b", line)
            if bvid_match:
                checkpoint["bvid"] = bvid_match.group(0)
    return checkpoint


def match_progress_label(item: dict) -> str:
    zone = short_zone(str(item.get("zone") or ""))
    stage = stage_label(str(item.get("match_type") or ""), str(item.get("match_slug") or ""))
    prefix = f"{zone}第{item.get('order') or '?'}场"
    if stage:
        prefix = f"{prefix} {stage}"
    red = team_label(str(item.get("red_school") or ""), str(item.get("red_name") or ""))
    blue = team_label(str(item.get("blue_school") or ""), str(item.get("blue_name") or ""))
    score = ""
    if item.get("red_score") or item.get("blue_score") or item.get("latest_status") == "DONE":
        score = f"{item.get('red_score', 0)}:{item.get('blue_score', 0)}"
    versus = f"{red}{score or ' vs '}{blue}"
    return f"{prefix} {versus}".strip()


def short_zone(zone: str) -> str:
    for token in ("赛区", "区域赛", "分区"):
        zone = zone.replace(token, "")
    return zone.strip()


def stage_label(match_type: str, match_slug: str) -> str:
    slug = match_slug.strip()
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
    key = match_type.strip().upper().replace("-", "_").replace(" ", "_")
    return labels.get(key, match_type.strip())


def team_label(school: str, name: str) -> str:
    return (school or name).strip()


def human_bytes(bytes_value: int) -> str:
    value = float(bytes_value or 0)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GiB"


def storage_snapshot(mount: str) -> dict:
    result = run(["df", "-B1", "--output=target,size,used,avail,pcent", mount], timeout=4)
    if not result["ok"]:
        return {"mount": mount, "ok": False, "detail": result["error"]}
    lines = [line for line in result["stdout"].splitlines() if line.strip()]
    if len(lines) < 2:
        return {"mount": mount, "ok": False, "detail": "df 输出异常"}
    parts = lines[-1].split()
    if len(parts) < 5:
        return {"mount": mount, "ok": False, "detail": "df 输出无法解析"}
    return {
        "mount": mount,
        "ok": True,
        "size": to_int(parts[1]),
        "used": to_int(parts[2]),
        "avail": to_int(parts[3]),
        "used_pct": to_int(parts[4].rstrip("%")),
    }


def external_snapshot(name: str, url: str) -> dict:
    started = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "RMMonitorDashboard/1.0"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            resp.read(64)
            return {"name": name, "ok": 200 <= resp.status < 400, "status": resp.status, "latency_ms": int((time.time() - started) * 1000)}
    except Exception as exc:
        return {"name": name, "ok": False, "detail": str(exc), "latency_ms": int((time.time() - started) * 1000)}


def collect_history() -> dict:
    history = {
        "label": "今日",
        "since": time.strftime("%Y-%m-%d 00:00:00"),
        "record_tasks": record_task_counts(),
        "upload_tasks": upload_task_counts(),
        "transcode_tasks": transcode_task_counts(),
        "artifacts": artifact_counts(),
        "matches": match_counts(),
        "local_scripts": local_script_counts(),
    }
    return history


def record_task_counts() -> dict:
    query = """
        select rt.status, count(*)
        from record_tasks rt
        join match_rounds mr on mr.id = rt.match_round_record_tasks
        join matches m on m.id = mr.match_rounds
        where rt.created_at >= date_trunc('day', now())
          and m.event not like '%测试%'
          and m.zone not like '%测试%'
        group by rt.status;
    """
    return rows_to_status_counts(psql(query, "history_record_tasks", []))


def upload_task_counts() -> dict:
    query = """
        select ut.status, count(*)
        from upload_tasks ut
        left join record_tasks rt on rt.id = ut.record_task_upload_task
        left join match_rounds mr on mr.id = rt.match_round_record_tasks
        left join matches m on m.id = mr.match_rounds
        where ut.created_at >= date_trunc('day', now())
          and coalesce(m.event, '') not like '%测试%'
          and coalesce(m.zone, '') not like '%测试%'
        group by ut.status;
    """
    return rows_to_status_counts(psql(query, "history_upload_tasks", []))


def transcode_task_counts() -> dict:
    query = """
        select tt.status, count(*)
        from transcode_tasks tt
        left join media_artifacts ma on ma.id = tt.media_artifact_source_transcode_task
        left join record_tasks rt on rt.id = ma.record_task_media_artifacts
        left join match_rounds mr on mr.id = rt.match_round_record_tasks
        left join matches m on m.id = mr.match_rounds
        where tt.created_at >= date_trunc('day', now())
          and coalesce(m.event, '') not like '%测试%'
          and coalesce(m.zone, '') not like '%测试%'
        group by tt.status;
    """
    return rows_to_status_counts(psql(query, "history_transcode_tasks", []))


def rows_to_status_counts(rows: list[list[str]]) -> dict:
    out = {"total": 0, "by_status": {}}
    for row in rows:
        if len(row) >= 2:
            count = to_int(row[1])
            out["by_status"][row[0]] = count
            out["total"] += count
    return out


def status_counts(table: str, time_column: str) -> dict:
    query = f"""
        select status, count(*)
        from {table}
        where {time_column} >= date_trunc('day', now())
        group by status;
    """
    return rows_to_status_counts(psql(query, f"history_{table}", []))


def artifact_counts() -> dict:
    query = """
        select ma.kind, count(*), coalesce(sum(ma.file_size), 0)
        from media_artifacts ma
        left join record_tasks rt on rt.id = ma.record_task_media_artifacts
        left join match_rounds mr on mr.id = rt.match_round_record_tasks
        left join matches m on m.id = mr.match_rounds
        where ma.created_at >= date_trunc('day', now())
          and coalesce(m.event, '') not like '%测试%'
          and coalesce(m.zone, '') not like '%测试%'
        group by ma.kind;
    """
    out = {"total": 0, "bytes": 0, "by_kind": {}}
    for row in psql(query, "history_artifacts", []):
        if len(row) >= 3:
            count = to_int(row[1])
            size = to_int(row[2])
            out["by_kind"][row[0]] = {"count": count, "bytes": size}
            out["total"] += count
            out["bytes"] += size
    return out


def match_counts() -> dict:
    query = """
        select latest_status, count(*)
        from matches
        where updated_at >= date_trunc('day', now())
          and event not like '%测试%'
          and zone not like '%测试%'
        group by latest_status;
    """
    out = {"total": 0, "by_status": {}}
    for row in psql(query, "history_matches", []):
        if len(row) >= 2:
            count = to_int(row[1])
            out["by_status"][row[0]] = count
            out["total"] += count
    return out


def local_script_counts() -> dict:
    out = {"total": 0, "ERROR": 0, "WARN": 0, "INFO": 0}
    log_dir = Path(CONFIG.local_log_dir)
    if not log_dir.exists():
        return out
    cutoff = today_start_epoch()
    for path in log_dir.glob("*.log"):
        result = run(["tail", "-n", "2000", str(path)], timeout=4)
        if not result["ok"]:
            continue
        for line in result["stdout"].splitlines():
            fields = parse_json_log(line)
            if not fields:
                continue
            timestamp = parse_timestamp_epoch(str(fields.get("time") or ""))
            if timestamp and timestamp < cutoff:
                continue
            level = str(fields.get("level") or "INFO").upper()
            if level not in out:
                continue
            out[level] += 1
            out["total"] += 1
    return out


def kubectl_json(args: list[str], label: str, issues: list[dict]) -> dict:
    result = run(["kubectl", *args], timeout=7)
    if not result["ok"]:
        issues.append(issue("bad", "Kubernetes", label, result["error"]))
        return {}
    try:
        return json.loads(result["stdout"])
    except json.JSONDecodeError as exc:
        issues.append(issue("bad", "Kubernetes", label, str(exc)))
        return {}


def psql(query: str, label: str, issues: list[dict]) -> list[list[str]]:
    result = run(
        [
            "kubectl",
            "exec",
            "-n",
            CONFIG.namespace,
            CONFIG.postgres,
            "--",
            "psql",
            "-U",
            CONFIG.db_user,
            "-d",
            CONFIG.db_name,
            "-At",
            "-F",
            "\t",
            "-c",
            query,
        ],
        timeout=8,
    )
    if not result["ok"]:
        issues.append(issue("bad", "数据库", label, result["error"]))
        return []
    return [line.split("\t") for line in result["stdout"].splitlines() if line.strip()]


def issue(severity: str, area: str, title: str, detail: str) -> dict:
    display_detail, action = describe_issue(area, title, detail)
    item = {"severity": severity, "area": area, "title": title, "detail": display_detail}
    if action:
        item["action"] = action
    return item


def describe_issue(area: str, title: str, detail: str) -> tuple[str, str]:
    text = str(detail or "").strip()
    lower = text.lower()

    if area == "日志":
        if "ERROR" in title:
            return (
                "最近 15 分钟有程序写出了 ERROR 日志，但录制、上传、归档等业务检查没有失败。先按黄色提醒处理。",
                "如果数量持续增加，展开诊断详情看来源服务；如果同时出现红色业务项，优先处理红色项。",
            )
        return (
            "最近 15 分钟有程序写出了 WARN 日志，表示有异常迹象但暂未阻断流程。",
            "继续观察即可；数量变多时再展开诊断详情看来源服务。",
        )

    if area == "日志采集":
        return (
            "dashboard 没能读取这一路日志，所以页面可能少了一部分状态信息。",
            "先刷新页面；仍存在时检查 dashboard 服务权限和对应日志文件。",
        )

    if area == "Kubernetes":
        if title.startswith("record-") and "job failed" in lower:
            return (
                "某个录制容器失败了。已成功续录并合并的分段会被自动忽略；如果还显示在这里，说明对应成品可能没生成。",
                "先看“自动录制”和“最终文件”卡片；成品缺失时立即在群里提醒修复。",
            )
        if "deployment ready" in lower:
            return (
                "有服务副本没有全部启动，相关功能可能不完整。",
                "等待 Kubernetes 自动重启；几分钟不恢复就检查该服务日志。",
            )
        if "pod phase=" in lower:
            return (
                "有容器没有处于正常运行状态，可能导致对应组件不可用。",
                "检查这个 Pod 的事件和日志，确认是启动慢、资源不足还是程序退出。",
            )
        if "container waiting" in lower:
            return (
                "有容器卡在等待状态，服务还没有真正跑起来。",
                "检查等待原因，常见是镜像拉取失败、配置错误或启动崩溃。",
            )
        if "container restarts=" in lower:
            return (
                "有容器刚刚重启过，当前可能已经恢复，但需要观察是否反复重启。",
                "如果重启次数继续增加，再查看该组件日志。",
            )
        return (
            "dashboard 读取 Kubernetes 状态时发现异常，可能影响某个后台组件。",
            "查看对应 Deployment、Pod 或 Job 的日志。",
        )

    if area == "数据库":
        if "timed out" in lower or "timeout" in lower:
            return (
                "dashboard 查询数据库超时，当前状态可能不完整。",
                "刷新看板；如果持续超时，检查 Postgres 容器和机器负载。",
            )
        return (
            "dashboard 查询数据库失败，页面上的流程状态可能不完整。",
            "先刷新看板；仍存在时检查 Postgres 容器日志。",
        )

    if area == "录制任务":
        if "404" in lower or "not found" in lower:
            return (
                "官方直播源当时返回 404，这个视角那一段没有可用视频流，常见原因是官方误切、源刷新或网络抖动。",
                "先确认最终 FLV 是否已由后续分段合并生成；未生成时需要补录或标记缺失。",
            )
        if "403" in lower or "forbidden" in lower:
            return (
                "直播源拒绝访问，这个视角当时没有拉到流。",
                "确认网络规则和直播链接是否刷新；未自动恢复时需要手动介入。",
            )
        if "timed out" in lower or "timeout" in lower:
            return (
                "录制拉流超时，通常是直播源或网络短暂不可达。",
                "看后续是否已自动续录并合并；如果没有成品，手动重录或标记缺失。",
            )
        if "connection" in lower:
            return (
                "录制连接中断或无法建立，可能是网络波动或直播源临时不可用。",
                "确认后续分段是否补上；没有补上时需要尽快处理。",
            )
        return (
            "某个视角录制任务失败，可能影响该视角最终文件。",
            "先看“最终文件”是否已有对应视角成品；没有就按录制故障处理。",
        )

    if area == "录制链路":
        return (
            "官方赛程显示比赛已经开始，但系统没有正在录制的任务，存在漏录风险。",
            "立即检查录制调度器；正式比赛时在群里提醒全体成员协助修复。",
        )

    if area == "转码任务":
        return (
            "压缩转码失败，原始 FLV 不一定受影响，但压缩版不会生成。",
            "若当前策略是上传原始 FLV，可先不阻塞；需要压缩版时再重跑转码。",
        )

    if area == "上传任务":
        if "season not found" in lower:
            return (
                "B站合集没有匹配到，通常是合集改名或配置里写的合集 ID 不对；视频还没有开始上传。",
                "确认当前合集 ID/分区 ID 后更新上传配置，再重启自动上传队列。",
            )
        if "limit" in lower or "rate" in lower or "too many" in lower:
            return (
                "B站上传或投稿触发限流，视频可能需要排队或稍后重试。",
                "保持队列运行，必要时降低并发；不要重复手动投稿同一场。",
            )
        if "duplicate" in lower or "重复" in text:
            return (
                "B站可能认为这个视频已经投过，当前投稿没有继续。",
                "先确认稿件列表和飞书链接，避免重复投稿。",
            )
        return (
            "B站上传或飞书回填失败，对应场次可能暂时没有可打开的视频链接。",
            "检查上传队列；确认失败后重试该场上传。",
        )

    if area == "本机脚本":
        if "season not found" in lower:
            return (
                "B站合集没有匹配到，上传队列在生成投稿计划时停住了；这通常是合集被改名导致按旧名称找不到。",
                "改为使用合集 ID/分区 ID 或更新合集名，然后重启自动上传队列。",
            )
        if "bili" in title.lower() or "upload" in title.lower():
            return (
                "本机 B站上传脚本报错，上传队列可能卡在某个场次或分P。",
                "看上传队列当前场次；必要时重试失败场次。",
            )
        if "archive" in title.lower():
            return (
                "长期归档脚本报错，文件可能还没有复制到长期目录。",
                "确认本地源文件仍在；归档成功前不要清理本地文件。",
            )
        return (
            "某个本机辅助脚本报错，可能影响上传、归档或清理流程。",
            "看流程卡片定位是哪一步，再打开诊断详情查看具体脚本。",
        )

    if area == "存储":
        if "磁盘使用率" in text:
            return (
                f"{title} 空间使用率偏高，继续录制可能逐步挤占可用空间。",
                "优先确认上传和长期归档是否在跑；必要时清理已完成且已归档的本地文件。",
            )
        return (
            "dashboard 读取存储容量失败，无法判断剩余空间是否安全。",
            "检查挂载点是否还在，以及 `df` 是否能正常返回。",
        )

    if area == "外部源":
        return (
            f"dashboard 当前访问不到{title}，自动获取赛程或直播信息可能受影响。",
            "确认网络能直连官方接口；正式比赛中如果持续不可达，需要手动核对赛程和直播。",
        )

    return (
        text or "发现异常，但没有更多上下文。",
        "展开诊断详情查看来源服务和原始日志。",
    )


def pod_owned_by_job(item: dict) -> bool:
    owners = item.get("metadata", {}).get("ownerReferences", []) or []
    return any(owner.get("kind") == "Job" for owner in owners)


def job_is_recent(item: dict, seconds: int) -> bool:
    status = item.get("status", {})
    candidates = []
    for condition in status.get("conditions", []) or []:
        if condition.get("lastTransitionTime"):
            candidates.append(str(condition["lastTransitionTime"]))
    for key in ("completionTime", "startTime"):
        if status.get(key):
            candidates.append(str(status[key]))
    timestamps = [parse_timestamp_epoch(value) for value in candidates]
    timestamps = [value for value in timestamps if value]
    return bool(timestamps and max(timestamps) >= time.time() - seconds)


def is_benign_log_item(item: dict) -> bool:
    fields = parse_json_log(str(item.get("raw") or ""))
    if fields:
        return is_benign_json_event(fields)
    raw = str(item.get("raw") or "").lower()
    return "script interrupted" in raw or "exit_code=130" in raw or '"exit_code": 130' in raw


def is_benign_json_event(fields: dict) -> bool:
    msg = str(fields.get("msg") or fields.get("message") or "").lower()
    exit_code = str(fields.get("exit_code") or "")
    return exit_code == "130" or "script interrupted" in msg


def local_event_keys(fields: dict, path: Path) -> list[str]:
    service = str(fields.get("service") or path.stem)
    keys = [f"{service}:service"]
    match_id = str(fields.get("match_id") or "")
    if match_id:
        keys.append(f"{service}:match:{match_id}")
        return keys
    order = fields.get("order")
    if order is not None and str(order) != "":
        zone = str(fields.get("zone") or "")
        keys.append(f"{service}:order:{zone}:{order}")
    return keys


def is_local_success_event(fields: dict) -> bool:
    if str(fields.get("level") or "").upper() != "INFO":
        return False
    msg = str(fields.get("msg") or fields.get("message") or "").lower()
    return "completed" in msg or "finished" in msg or "script started" in msg


def has_recent_restart(statuses: list[dict]) -> bool:
    cutoff = time.time() - 60 * 60
    saw_restart_without_time = False
    for status in statuses:
        if int(status.get("restartCount") or 0) <= 0:
            continue
        finished_at = status.get("lastState", {}).get("terminated", {}).get("finishedAt", "")
        if not finished_at:
            saw_restart_without_time = True
            continue
        finished_ts = parse_timestamp_epoch(finished_at)
        if not finished_ts or finished_ts >= cutoff:
            return True
    return saw_restart_without_time


def summarize(lines: list[dict], errors: list[dict], issues: list[dict]) -> dict:
    by_level = {level: 0 for level in LEVELS}
    current_by_level = {level: 0 for level in LEVELS}
    by_target = {}
    by_category = {}
    for item in lines:
        by_level[item["level"]] = by_level.get(item["level"], 0) + 1
        if is_recent_log(item, CURRENT_LOG_SECONDS) and not is_benign_log_item(item):
            current_by_level[item["level"]] = current_by_level.get(item["level"], 0) + 1
        by_target[item["target"]] = by_target.get(item["target"], 0) + 1
        by_category[item["category"]] = by_category.get(item["category"], 0) + 1
    severity = "ok"
    bad_issues = sum(1 for item in issues if item["severity"] == "bad")
    warn_issues = sum(1 for item in issues if item["severity"] == "warn")
    if errors or bad_issues:
        severity = "bad"
    elif warn_issues:
        severity = "warn"
    return {
        "severity": severity,
        "total": len(lines),
        "by_level": by_level,
        "current_by_level": current_by_level,
        "by_target": by_target,
        "by_category": by_category,
        "errors": len(errors),
        "bad_issues": bad_issues,
        "warn_issues": warn_issues,
    }


def visible_summary(summary: dict, diagnostics: bool) -> dict:
    fields = {
        "severity": summary.get("severity", "ok"),
        "bad_issues": summary.get("bad_issues", 0),
        "warn_issues": summary.get("warn_issues", 0),
        "errors": summary.get("errors", 0),
    }
    if diagnostics:
        fields.update(
            {
                "total": summary.get("total", 0),
                "by_level": summary.get("by_level", {}),
                "current_by_level": summary.get("current_by_level", {}),
                "by_target": summary.get("by_target", {}),
                "by_category": summary.get("by_category", {}),
            }
        )
    return fields


def first_query(query: dict[str, list[str]], key: str, default: str) -> str:
    values = query.get(key)
    if not values:
        return default
    return values[0].strip() or default


def run(command: list[str], timeout: int) -> dict:
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "stderr": "", "error": "timeout"}
    error = (result.stderr or result.stdout or "").strip()
    return {"ok": result.returncode == 0, "stdout": result.stdout, "stderr": result.stderr, "error": error}


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def kubectl_since(since: str) -> str:
    return f"{max(60, since_seconds(since))}s"


def since_seconds(since: str) -> int:
    if since == "today":
        return max(60, int(time.time() - today_start_epoch()))
    match = re.fullmatch(r"(\d+)([mh])", since)
    if not match:
        return 30 * 60
    value = int(match.group(1))
    return value * 60 if match.group(2) == "m" else value * 60 * 60


def today_start_epoch() -> float:
    now = time.localtime()
    return time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, now.tm_wday, now.tm_yday, now.tm_isdst))


def is_recent_log(item: dict, seconds: int) -> bool:
    timestamp = parse_timestamp_epoch(str(item.get("time_sort") or item.get("time") or ""))
    return bool(timestamp and timestamp >= time.time() - seconds)


def log_line_epoch(line: str) -> float:
    fields = parse_json_log(line)
    if fields:
        return parse_timestamp_epoch(str(fields.get("time") or ""))
    match = re.search(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:[,\.]\d+)?", line)
    if not match:
        return 0
    try:
        return datetime.strptime(f"{match.group(1)} {match.group(2)}", "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return 0


def parse_timestamp_epoch(value: str) -> float:
    if not value:
        return 0
    text = value.strip().replace("Z", "+00:00")
    if re.search(r"[+-]\d{4}$", text):
        text = text[:-5] + text[-5:-2] + ":" + text[-2:]
    text = re.sub(r"(\.\d{6})\d+([+-]\d\d:\d\d)$", r"\1\2", text)
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0


def to_int(value: object) -> int:
    try:
        return int(value)
    except Exception:
        return 0


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>RM Monitor 看板</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f5f7fa;
      --surface: #ffffff;
      --surface-soft: #f8fafc;
      --line: #d8dee8;
      --text: #17202c;
      --muted: #667085;
      --green: #147a4d;
      --green-bg: #eaf7f0;
      --amber: #a66a12;
      --amber-bg: #fff6df;
      --red: #bd3d38;
      --red-bg: #fff1ef;
      --blue: #286fae;
      --blue-bg: #edf5ff;
      --code: #111827;
      --code-line: rgba(255, 255, 255, 0.07);
      --shadow: 0 8px 24px rgba(31, 41, 55, 0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 14px;
      letter-spacing: 0;
    }
    header {
      position: sticky;
      top: 0;
      z-index: 5;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      padding: 12px 22px;
      min-height: 64px;
      border-bottom: 1px solid var(--line);
      background: rgba(255,255,255,0.95);
      backdrop-filter: blur(14px);
    }
    h1 { margin: 0; font-size: 20px; font-weight: 780; }
    h2 { margin: 0 0 12px; font-size: 15px; font-weight: 760; }
    h3 { margin: 0; font-size: 14px; font-weight: 760; }
    main { max-width: 1540px; margin: 0 auto; padding: 18px 22px 28px; }
    button, select, input {
      height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--surface);
      color: var(--text);
      font: inherit;
    }
    button {
      width: 38px;
      cursor: pointer;
      font-size: 18px;
      line-height: 1;
    }
    select { min-width: 124px; padding: 0 10px; }
    input { min-width: 220px; padding: 0 11px; }
    .toolbar { display: flex; flex-wrap: wrap; align-items: center; justify-content: flex-end; gap: 8px; }
    .sub { margin-top: 3px; color: var(--muted); font-size: 13px; }
    .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; background: var(--muted); margin-right: 8px; }
    .dot.ok { background: var(--green); }
    .dot.warn { background: var(--amber); }
    .dot.bad { background: var(--red); }
    .headline {
      display: grid;
      grid-template-columns: minmax(260px, 1.2fr) repeat(4, minmax(120px, 1fr));
      gap: 12px;
      margin-bottom: 14px;
    }
    .card, .panel {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }
    .card {
      position: relative;
      min-height: 116px;
      padding: 16px;
      overflow: hidden;
    }
    .card::before {
      content: "";
      position: absolute;
      inset: 0 auto 0 0;
      width: 5px;
      background: var(--muted);
    }
    .card.ok::before { background: var(--green); }
    .card.warn::before { background: var(--amber); }
    .card.bad::before { background: var(--red); }
    .card .label { color: var(--muted); font-size: 12px; margin-bottom: 10px; }
    .card .value { font-size: 25px; line-height: 1.15; font-weight: 820; }
    .card.primary .value { font-size: 34px; }
    .card.ok .value { color: var(--green); }
    .card.warn .value { color: var(--amber); }
    .card.bad .value { color: var(--red); }
    .hint { margin-top: 8px; color: var(--muted); overflow-wrap: anywhere; }
    .dashboard-grid {
      display: grid;
      grid-template-columns: minmax(0, 1.25fr) minmax(340px, 0.75fr);
      gap: 14px;
      align-items: start;
    }
    .panel { padding: 14px; overflow: hidden; }
    .pipeline-panel { margin-bottom: 14px; }
    .pipeline-grid {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 10px;
    }
    .backup-cache-head {
      margin: 14px 0 8px;
      padding-top: 12px;
      border-top: 1px solid var(--line);
      color: var(--muted);
      font-size: 13px;
      font-weight: 760;
    }
    .pipeline-scope {
      margin: -2px 0 10px;
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }
    .backup-cache-grid {
      grid-template-columns: minmax(0, 1fr);
    }
    .backup-cache-grid .pipeline-step {
      min-height: 112px;
    }
    .pipeline-step {
      min-height: 132px;
      padding: 13px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface-soft);
    }
    .pipeline-step.running { background: var(--blue-bg); border-color: #b9d8f2; }
    .pipeline-step.done { background: var(--green-bg); border-color: #b6e3c9; }
    .pipeline-step.warn { background: var(--amber-bg); border-color: #f0d595; }
    .pipeline-step.bad { background: var(--red-bg); border-color: #f2b4ae; }
    .pipeline-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
    .pipeline-status {
      flex: 0 0 auto;
      border-radius: 999px;
      padding: 3px 8px;
      font-size: 12px;
      background: #eef2f7;
      color: var(--muted);
    }
    .running .pipeline-status { background: #d8ebff; color: var(--blue); }
    .done .pipeline-status { background: #dff4e8; color: var(--green); }
    .warn .pipeline-status { background: #ffedc2; color: var(--amber); }
    .bad .pipeline-status { background: #ffe0dd; color: var(--red); }
    .pipeline-match {
      margin-top: 12px;
      min-height: 40px;
      line-height: 1.35;
      font-weight: 760;
      overflow-wrap: anywhere;
    }
    .pipeline-metric { margin-top: 8px; font-size: 20px; line-height: 1.15; font-weight: 820; }
    .pipeline-detail { margin-top: 7px; color: var(--muted); overflow-wrap: anywhere; }
    .checks {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
    }
    .check {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      background: var(--surface-soft);
      min-height: 128px;
    }
    .check.ok { background: var(--green-bg); border-color: #b6e3c9; }
    .check.warn { background: var(--amber-bg); border-color: #f0d595; }
    .check.bad { background: var(--red-bg); border-color: #f2b4ae; }
    .check-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
    .status-pill {
      flex: 0 0 auto;
      border-radius: 999px;
      padding: 3px 8px;
      font-size: 12px;
      background: #eef2f7;
      color: var(--muted);
    }
    .ok .status-pill { background: #dff4e8; color: var(--green); }
    .warn .status-pill { background: #ffedc2; color: var(--amber); }
    .bad .status-pill { background: #ffe0dd; color: var(--red); }
    .check-state {
      margin-top: 16px;
      font-size: 24px;
      line-height: 1.1;
      font-weight: 820;
    }
    .ok .check-state { color: var(--green); }
    .warn .check-state { color: var(--amber); }
    .bad .check-state { color: var(--red); }
    .check-detail { color: var(--muted); margin-top: 8px; overflow-wrap: anywhere; }
    .issue-list { display: grid; gap: 8px; }
    .issue-item {
      border-left: 4px solid var(--muted);
      background: var(--surface-soft);
      padding: 10px 12px;
      border-radius: 4px;
    }
    .issue-item.bad { border-left-color: var(--red); background: var(--red-bg); }
    .issue-item.warn { border-left-color: var(--amber); background: var(--amber-bg); }
    .issue-title { font-weight: 760; }
    .issue-detail { color: var(--muted); margin-top: 4px; overflow-wrap: anywhere; }
    .issue-action { margin-top: 5px; color: var(--ink); overflow-wrap: anywhere; }
    .empty { color: var(--muted); padding: 14px 2px; }
    .mini-grid { display: grid; gap: 10px; }
    .history-panel { margin-top: 14px; }
    .history-grid {
      display: grid;
      grid-template-columns: repeat(6, minmax(120px, 1fr));
      gap: 10px;
    }
    .stat {
      min-height: 92px;
      padding: 13px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface-soft);
    }
    .stat .label { color: var(--muted); font-size: 12px; margin-bottom: 9px; }
    .stat .value { font-size: 25px; line-height: 1.1; font-weight: 820; }
    .stat .hint { margin-top: 7px; }
    .mini-row {
      display: grid;
      grid-template-columns: 120px 1fr auto;
      gap: 10px;
      align-items: center;
      min-height: 30px;
    }
    .bar-track { height: 9px; background: #edf1f6; border-radius: 999px; overflow: hidden; }
    .bar-fill { height: 100%; border-radius: 999px; background: var(--blue); }
    .bar-fill.ERROR, .bar-fill.bad { background: var(--red); }
    .bar-fill.WARN, .bar-fill.warn { background: var(--amber); }
    .bar-fill.INFO, .bar-fill.ok { background: var(--green); }
    .count { color: var(--muted); font-variant-numeric: tabular-nums; }
    details {
      margin-top: 14px;
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      overflow: hidden;
    }
    summary {
      cursor: pointer;
      padding: 13px 14px;
      font-weight: 760;
      list-style-position: inside;
    }
    .diagnostics-body { padding: 0 14px 14px; }
    .diagnostics-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 14px;
      margin-bottom: 14px;
    }
    .diagnostics-toolbar { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
    .log-box {
      max-height: 430px;
      overflow: auto;
      border: 1px solid #e5e9f0;
      border-radius: 8px;
      background: var(--code);
      color: #dce3ee;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      line-height: 1.5;
    }
    .log-row {
      display: grid;
      grid-template-columns: 214px 170px 64px 110px 1fr;
      gap: 10px;
      min-width: 1160px;
      padding: 3px 10px;
      border-bottom: 1px solid var(--code-line);
    }
    .log-time, .log-target, .log-category { color: #93a4ba; }
    .log-level { font-weight: 760; color: #93c5fd; }
    .log-row.ERROR .log-level { color: #f87171; }
    .log-row.WARN .log-level { color: #fbbf24; }
    .log-row.INFO .log-level { color: #86efac; }
    .log-row.DEBUG .log-level { color: #c4b5fd; }
    .log-message { white-space: pre-wrap; overflow-wrap: anywhere; }
    @media (max-width: 1180px) {
      .headline { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .dashboard-grid { grid-template-columns: 1fr; }
      .pipeline-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .checks, .diagnostics-grid, .history-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 660px) {
      header { align-items: flex-start; flex-direction: column; padding: 12px 14px; }
      main { padding: 14px; }
      .headline, .pipeline-grid, .checks, .diagnostics-grid, .history-grid { grid-template-columns: 1fr; }
      .card.primary .value { font-size: 28px; }
      .toolbar, .diagnostics-toolbar { justify-content: flex-start; width: 100%; }
      input, select { min-width: 0; width: 100%; }
      button { width: 40px; }
      .mini-row { grid-template-columns: 90px 1fr auto; }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1><span id="health-dot" class="dot"></span>RM Monitor 看板</h1>
      <div id="generated" class="sub">--</div>
    </div>
    <div class="toolbar">
      <select id="since" aria-label="时间范围">
        <option value="today" selected>今天</option>
        <option value="5m">最近 5 分钟</option>
        <option value="15m">最近 15 分钟</option>
        <option value="30m">最近 30 分钟</option>
        <option value="1h">最近 1 小时</option>
        <option value="3h">最近 3 小时</option>
        <option value="6h">最近 6 小时</option>
        <option value="12h">最近 12 小时</option>
      </select>
      <button id="refresh" title="刷新" aria-label="刷新">↻</button>
    </div>
  </header>
  <main>
    <section id="headline" class="headline"></section>
    <section class="panel pipeline-panel">
      <h2>按场次流程</h2>
      <div id="pipeline-scope" class="pipeline-scope"></div>
      <div id="pipeline-steps" class="pipeline-grid"></div>
      <div class="backup-cache-head">独立备用缓存</div>
      <div id="backup-cache-step" class="pipeline-grid backup-cache-grid"></div>
    </section>
    <section class="dashboard-grid">
      <section class="panel">
        <h2>环节状态</h2>
        <div id="checks" class="checks"></div>
      </section>
      <section class="panel">
        <h2>需要处理</h2>
        <div id="issues" class="issue-list"></div>
      </section>
    </section>
    <section class="panel history-panel">
      <h2>今日累计</h2>
      <div id="history-metrics" class="history-grid"></div>
    </section>
    <details id="diagnostics">
      <summary>诊断详情</summary>
      <div class="diagnostics-body">
        <div class="diagnostics-grid">
          <section>
            <h2>级别</h2>
            <div id="level-bars" class="mini-grid"></div>
          </section>
          <section>
            <h2>链路</h2>
            <div id="category-bars" class="mini-grid"></div>
          </section>
          <section>
            <h2>服务</h2>
            <div id="target-bars" class="mini-grid"></div>
          </section>
        </div>
        <div class="diagnostics-toolbar">
          <select id="target" aria-label="服务"></select>
          <select id="level" aria-label="级别">
            <option value="ALL">全部级别</option>
            <option value="ERROR">ERROR</option>
            <option value="WARN">WARN</option>
            <option value="INFO">INFO</option>
            <option value="DEBUG">DEBUG</option>
            <option value="OTHER">OTHER</option>
          </select>
          <input id="keyword" placeholder="关键词过滤" aria-label="关键词过滤">
        </div>
        <div id="logs" class="log-box"></div>
      </div>
    </details>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    const statusText = {ok: "正常", warn: "注意", bad: "异常"};
    const pipelineText = {running: "进行中", done: "已完成", idle: "等待", warn: "注意", bad: "异常"};

    function worse(a, b) {
      const rank = {bad: 3, warn: 2, ok: 1};
      return rank[b] > rank[a] ? b : a;
    }

    function statusFor(issues, areas) {
      return issues.filter(item => areas.includes(item.area)).reduce((state, item) => worse(state, item.severity), "ok");
    }

    function issueCount(issues, areas) {
      return issues.filter(item => areas.includes(item.area)).length;
    }

    function firstIssue(issues, areas, fallback) {
      const item = issues.find(issue => areas.includes(issue.area));
      return item ? `${item.title}：${item.detail}${item.action ? ` 建议：${item.action}` : ""}` : fallback;
    }

    function card(label, value, cls, detail, primary = false) {
      return `<div class="card ${esc(cls)} ${primary ? "primary" : ""}"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div><div class="hint">${esc(detail)}</div></div>`;
    }

    function stat(label, value, detail) {
      return `<div class="stat"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div><div class="hint">${esc(detail)}</div></div>`;
    }

    function check(label, cls, detail) {
      return `<div class="check ${esc(cls)}"><div class="check-head"><h3>${esc(label)}</h3><span class="status-pill">${esc(statusText[cls] || cls)}</span></div><div class="check-state">${esc(statusText[cls] || cls)}</div><div class="check-detail">${esc(detail)}</div></div>`;
    }

    function pipelineStep(item) {
      const state = item.state || "idle";
      return `<div class="pipeline-step ${esc(state)}"><div class="pipeline-head"><h3>${esc(item.label || "--")}</h3><span class="pipeline-status">${esc(pipelineText[state] || state)}</span></div><div class="pipeline-match">${esc(item.match_label || "暂无场次")}</div><div class="pipeline-metric">${esc(item.metric || "--")}</div><div class="pipeline-detail">${esc(item.detail || "")}</div></div>`;
    }

    function statusCount(group, ...names) {
      const byStatus = (group && group.by_status) || {};
      return names.reduce((sum, name) => sum + Number(byStatus[name] || 0), 0);
    }

    function formatBytes(bytes) {
      bytes = Number(bytes || 0);
      if (bytes >= 1024 ** 4) return `${(bytes / 1024 ** 4).toFixed(2)} TB`;
      if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
      if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(0)} MB`;
      return `${bytes} B`;
    }

    function storageLine(current) {
      const storage = (current && current.storage) || [];
      if (!storage.length) return "暂无容量数据";
      return storage.map(item => item.ok ? `${item.mount} ${item.used_pct}%` : `${item.mount} 异常`).join("，");
    }

    function deploymentLine(current) {
      const deployments = (current && current.deployments) || {};
      const pods = (current && current.pods) || {};
      return `Deployment ${deployments.ready || 0}/${deployments.desired || 0}，Running Pod ${pods.running || 0}，异常 Pod ${pods.other || 0}`;
    }

    function externalLine(current) {
      const external = (current && current.external) || [];
      if (!external.length) return "暂无外部源数据";
      return external.map(item => item.ok ? `${item.name} ${item.latency_ms}ms` : `${item.name} 不可达`).join("，");
    }

    function barList(counts, preferred = []) {
      counts = counts || {};
      const keys = [...preferred, ...Object.keys(counts).filter(k => !preferred.includes(k)).sort()];
      const total = Object.values(counts).reduce((a, b) => a + Number(b || 0), 0) || 1;
      const rows = keys.filter(k => counts[k]).map(k => {
        const value = Number(counts[k] || 0);
        const pct = Math.max(5, value / total * 100);
        return `<div class="mini-row"><div title="${esc(k)}">${esc(k)}</div><div class="bar-track"><div class="bar-fill ${esc(k)}" style="width:${pct}%"></div></div><div class="count">${value}</div></div>`;
      });
      return rows.join("") || `<div class="empty">暂无数据</div>`;
    }

    function syncTargets(targets) {
      const select = $("target");
      const current = select.value || "all";
      const signature = JSON.stringify(targets);
      if (select.dataset.signature === signature) return;
      select.dataset.signature = signature;
      select.innerHTML = targets.map(item => `<option value="${esc(item.target)}">${esc(item.name)}</option>`).join("");
      if ([...select.options].some(option => option.value === current)) select.value = current;
    }

    function render(data) {
      if (data.targets) syncTargets(data.targets);
      const summary = data.summary || {};
      const levels = summary.by_level || {};
      const currentLevels = summary.current_by_level || {};
      const issues = data.issues || [];
      const current = data.current || {};
      const history = data.history || {};
      const pipeline = data.pipeline || {};
      const backupCache = pipeline.backup_cache || {};
      const pipelineScope = pipeline.scope || {};
      const severity = summary.severity || "ok";
      $("health-dot").className = `dot ${severity}`;
      $("generated").textContent = `${data.generated_at || "--"} / ${data.namespace || ""} / ${data.duration_ms || 0} ms`;

      const k8s = statusFor(issues, ["Kubernetes", "日志采集", "数据库"]);
      const record = statusFor(issues, ["录制任务", "录制链路"]);
      const upload = statusFor(issues, ["上传任务", "本机脚本"]);
      const storage = statusFor(issues, ["存储"]);
      const external = statusFor(issues, ["外部源"]);
      const backupCacheStatus = backupCache.state === "bad" ? "bad" : (backupCache.state === "warn" ? "warn" : "ok");
      const logSignal = summary.bad_issues ? "bad" : ((currentLevels.ERROR || currentLevels.WARN || summary.warn_issues) ? "warn" : "ok");
      const action = severity === "bad" ? "需要立即处理" : (severity === "warn" ? "可以继续，赛前确认" : "可以值守");
      const headlineDetail = severity === "bad"
        ? "下面的“需要处理”就是优先处理列表"
        : (severity === "warn" ? "没有阻断项，但黄色项建议赛前确认" : "自动录制、上传归档、存储和外部源未发现异常");

      $("headline").innerHTML = [
        card("总判断", action, severity, headlineDetail, true),
        card("严重问题", summary.bad_issues || 0, summary.bad_issues ? "bad" : "ok", summary.bad_issues ? "必须处理" : "无"),
        card("风险提示", summary.warn_issues || 0, summary.warn_issues ? "warn" : "ok", summary.warn_issues ? "需要留意" : "无"),
        card("近 15 分钟 ERROR", currentLevels.ERROR || 0, currentLevels.ERROR ? "warn" : "ok", currentLevels.ERROR ? "展开诊断详情查看" : "无"),
        card("近 15 分钟 WARN", currentLevels.WARN || 0, currentLevels.WARN ? "warn" : "ok", currentLevels.WARN ? "展开诊断详情查看" : "无"),
      ].join("");

      $("checks").innerHTML = [
        check("服务运行", k8s, firstIssue(issues, ["Kubernetes", "日志采集", "数据库"], deploymentLine(current))),
        check("官方直播", external, firstIssue(issues, ["外部源"], externalLine(current))),
        check("自动录制", record, firstIssue(issues, ["录制任务", "录制链路"], "未发现录制失败或开赛未录制")),
        check("备用缓存", backupCacheStatus, backupCache.detail || "正赛备用缓存独立运行"),
        check("Bili 上传", upload, firstIssue(issues, ["上传任务", "本机脚本"], "上传和飞书回填未报告异常；长期归档看独立卡片")),
        check("存储空间", storage, firstIssue(issues, ["存储"], storageLine(current))),
        check("告警信号", logSignal, (currentLevels.ERROR || currentLevels.WARN) ? `近 15 分钟 ERROR ${currentLevels.ERROR || 0}，WARN ${currentLevels.WARN || 0}` : "近 15 分钟无 ERROR/WARN"),
      ].join("");

      const steps = pipeline.steps || [];
      $("pipeline-scope").textContent = pipelineScope.zones && pipelineScope.zones.length
        ? `${pipelineScope.date || "--"} / ${pipelineScope.zones.join("、")} / 官方赛程`
        : `${pipelineScope.date || "--"} / 今日更新数据`;
      $("pipeline-steps").innerHTML = steps.length
        ? steps.map(pipelineStep).join("")
        : `<div class="empty">暂无流程数据</div>`;
      $("backup-cache-step").innerHTML = backupCache.label
        ? pipelineStep(backupCache)
        : `<div class="empty">暂无备用缓存数据</div>`;

      $("issues").innerHTML = issues.length
        ? issues.map(item => `<div class="issue-item ${esc(item.severity)}"><div class="issue-title">${esc(item.area)} · ${esc(item.title)}</div><div class="issue-detail">${esc(item.detail)}</div>${item.action ? `<div class="issue-action">建议：${esc(item.action)}</div>` : ""}</div>`).join("")
        : `<div class="empty">暂无问题</div>`;

      const records = history.record_tasks || {};
      const uploads = history.upload_tasks || {};
      const transcodes = history.transcode_tasks || {};
      const artifacts = history.artifacts || {};
      const sourceArtifact = ((artifacts.by_kind || {}).source) || {};
      const scripts = history.local_scripts || {};
      const historyCards = [
        stat("录制任务", records.total || 0, `成功 ${statusCount(records, "SUCCEEDED")}，失败 ${statusCount(records, "FAILED")}，进行中 ${statusCount(records, "RUNNING", "DISPATCHING", "PENDING")}`),
        stat("备用缓存", backupCache.archive_files || 0, `长期归档 ${formatBytes(backupCache.archive_bytes || 0)}，本地临时 ${backupCache.source_files || 0} 个`),
        stat("上传任务", uploads.total || 0, `成功 ${statusCount(uploads, "SUCCEEDED")}，失败 ${statusCount(uploads, "FAILED")}，等待 ${statusCount(uploads, "PENDING", "DISPATCHING")}`),
        stat("转码任务", transcodes.total || 0, `成功 ${statusCount(transcodes, "SUCCEEDED")}，失败 ${statusCount(transcodes, "FAILED")}`),
        stat("源文件", sourceArtifact.count || 0, `今日新增 ${formatBytes(sourceArtifact.bytes || 0)}`),
        stat("脚本事件", scripts.total || 0, `ERROR ${scripts.ERROR || 0}，WARN ${scripts.WARN || 0}`),
      ];
      if (data.diagnostics) {
        historyCards.push(stat("日志窗口", summary.total || 0, `${data.since === "today" ? "今天" : data.since} 采样日志，ERROR ${levels.ERROR || 0}，WARN ${levels.WARN || 0}`));
      }
      $("history-metrics").innerHTML = historyCards.join("");

      $("level-bars").innerHTML = barList(levels, ["ERROR", "WARN", "INFO", "DEBUG", "OTHER"]);
      $("category-bars").innerHTML = barList(summary.by_category || {}, ["record", "upload", "live", "transcode", "db", "system"]);
      $("target-bars").innerHTML = barList(summary.by_target || {});

      $("logs").innerHTML = (data.lines || []).map(line => {
        return `<div class="log-row ${esc(line.level)}"><div class="log-time">${esc(line.time)}</div><div class="log-target">${esc(line.target)}</div><div class="log-level">${esc(line.level)}</div><div class="log-category">${esc(line.category)}</div><div class="log-message" title="${esc(line.raw)}">${esc(line.message)}</div></div>`;
      }).join("") || `<div class="empty">暂无日志</div>`;
    }

    async function load() {
      const diagnosticsOpen = $("diagnostics").open;
      const params = new URLSearchParams({
        target: diagnosticsOpen ? ($("target").value || "all") : "all",
        since: $("since").value || "today",
        level: diagnosticsOpen ? ($("level").value || "ALL") : "ALL",
        q: diagnosticsOpen ? ($("keyword").value || "") : "",
        diagnostics: diagnosticsOpen ? "1" : "0",
        tail: diagnosticsOpen ? "150" : "120",
        t: Date.now().toString(),
      });
      try {
        const resp = await fetch(`/api/logs?${params.toString()}`, {cache: "no-store"});
        render(await resp.json());
      } catch (err) {
        $("health-dot").className = "dot bad";
        $("headline").innerHTML = card("总判断", "看板异常", "bad", err.message, true);
        $("issues").innerHTML = `<div class="issue-item bad"><div class="issue-title">dashboard</div><div class="issue-detail">${esc(err.message)}</div></div>`;
      }
    }

    $("refresh").addEventListener("click", load);
    $("since").addEventListener("change", load);
    $("target").addEventListener("change", load);
    $("level").addEventListener("change", load);
    $("keyword").addEventListener("keydown", (event) => { if (event.key === "Enter") load(); });
    $("diagnostics").addEventListener("toggle", load);
    load();
    setInterval(load, 10000);
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
