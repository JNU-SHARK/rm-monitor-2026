#!/usr/bin/env python3
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import local_log


SERVICE = "cluster-dns-guard"
DEFAULT_NAMESPACE = "rm-monitor"
DEFAULT_EXEC_TARGET = "deploy/monitor"
DEFAULT_API_EXEC_TARGET = "deploy/record-dispatcher"
DEFAULT_API_URL = "https://10.43.0.1:443/livez"
DEFAULT_NAS_MOUNT = "/mnt/server_data"
DEFAULT_NAS_PROBE_DIR = "/mnt/server_data/rm-monitor/records"
DEFAULT_SCHEDULE_URL = "https://pro-robomasters-hz-n5i3.oss-cn-hangzhou.aliyuncs.com/live_json/schedule.json"
DEFAULT_STATE_FILE = Path(__file__).resolve().parents[2] / "logs" / "cluster-dns-guard-state.json"
DEFAULT_UPSTREAMS = ("223.5.5.5", "223.6.6.6")
BAD_FORWARD_TOKENS = ("/etc/resolv.conf", "127.0.0.53")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Guard cluster DNS and official schedule access.")
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    parser.add_argument("--exec-target", default=DEFAULT_EXEC_TARGET)
    parser.add_argument("--api-exec-target", default=DEFAULT_API_EXEC_TARGET)
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument("--nas-mount", default=DEFAULT_NAS_MOUNT)
    parser.add_argument("--nas-probe-dir", default=DEFAULT_NAS_PROBE_DIR)
    parser.add_argument("--schedule-url", default=DEFAULT_SCHEDULE_URL)
    parser.add_argument("--upstream", action="append", default=[])
    parser.add_argument("--timeout", type=int, default=10)
    parser.add_argument("--repair", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--alert", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--alert-cooldown-seconds", type=int, default=900)
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE))
    parser.add_argument("--lark-app-id", default="")
    parser.add_argument("--lark-app-secret", default="")
    parser.add_argument("--bitable-app-token", default="")
    parser.add_argument("--lark-secret-name", default="rm-monitor-lark")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    upstreams = tuple(args.upstream or DEFAULT_UPSTREAMS)
    issues: list[str] = []
    repairs: list[str] = []

    corefile = load_coredns_corefile(args.timeout)
    if needs_corefile_repair(corefile):
        issues.append("CoreDNS forwards to host stub resolver")
        if args.repair:
            repaired = repair_corefile(corefile, upstreams)
            apply_coredns_corefile(repaired, args.timeout)
            repairs.append("patched coredns configmap")
            corefile = load_coredns_corefile(args.timeout)
            if needs_corefile_repair(corefile):
                issues.append("CoreDNS repair did not take effect")
            else:
                issues = [item for item in issues if item != "CoreDNS forwards to host stub resolver"]

    if not coredns_ready(args.timeout):
        issues.append("CoreDNS pod is not ready")

    schedule_error = probe_schedule_from_cluster(args)
    if schedule_error:
        issues.append(schedule_error)

    api_error = probe_kubernetes_api_from_cluster(args)
    if api_error:
        issues.append(api_error)

    nas_error = probe_nas_write(args)
    if nas_error:
        issues.append(nas_error)

    if issues:
        local_log.log_event(SERVICE, "ERROR", "cluster DNS guard failed", issues=issues, repairs=repairs)
        maybe_send_alert(args, issues, repairs)
        for item in issues:
            print(item, file=sys.stderr)
        return 1

    if repairs:
        local_log.log_event(SERVICE, "WARN", "cluster DNS guard repaired issue", repairs=repairs)
    local_log.log_event(
        SERVICE,
        "INFO",
        "cluster DNS guard completed",
        repairs=repairs,
        schedule_url=args.schedule_url,
    )
    return 0


def load_coredns_corefile(timeout: int) -> str:
    result = run(
        ["kubectl", "-n", "kube-system", "get", "configmap", "coredns", "-o", "json"],
        timeout=timeout,
    )
    if result.returncode != 0:
        raise SystemExit(f"failed to read CoreDNS configmap: {summarize_result(result)}")
    data = json.loads(result.stdout)
    return data.get("data", {}).get("Corefile", "")


def needs_corefile_repair(corefile: str) -> bool:
    return any(token in corefile for token in BAD_FORWARD_TOKENS)


def repair_corefile(corefile: str, upstreams: tuple[str, ...]) -> str:
    upstream_text = " ".join(upstreams)
    out = []
    replaced = False
    for line in corefile.splitlines():
        if re.match(r"^\s*forward\s+\.\s+", line):
            indent = line[: len(line) - len(line.lstrip())]
            out.append(f"{indent}forward . {upstream_text}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        raise SystemExit("CoreDNS Corefile has no forward stanza to repair")
    return "\n".join(out) + "\n"


def apply_coredns_corefile(corefile: str, timeout: int) -> None:
    payload = json.dumps({"data": {"Corefile": corefile}}, ensure_ascii=False)
    result = run(
        ["kubectl", "-n", "kube-system", "patch", "configmap", "coredns", "--type", "merge", "-p", payload],
        timeout=timeout,
    )
    if result.returncode != 0:
        raise SystemExit(f"failed to patch CoreDNS configmap: {summarize_result(result)}")
    result = run(["kubectl", "-n", "kube-system", "rollout", "restart", "deployment/coredns"], timeout=timeout)
    if result.returncode != 0:
        raise SystemExit(f"failed to restart CoreDNS: {summarize_result(result)}")
    result = run(
        ["kubectl", "-n", "kube-system", "rollout", "status", "deployment/coredns", "--timeout=120s"],
        timeout=130,
    )
    if result.returncode != 0:
        raise SystemExit(f"CoreDNS rollout did not complete: {summarize_result(result)}")


def coredns_ready(timeout: int) -> bool:
    result = run(
        ["kubectl", "-n", "kube-system", "get", "pods", "-l", "k8s-app=kube-dns", "-o", "json"],
        timeout=timeout,
    )
    if result.returncode != 0:
        local_log.log_event(SERVICE, "ERROR", "failed to read CoreDNS pods", detail=summarize_result(result))
        return False
    data = json.loads(result.stdout)
    for item in data.get("items", []) or []:
        statuses = item.get("status", {}).get("containerStatuses", []) or []
        if any(status.get("ready") for status in statuses):
            return True
    return False


def probe_schedule_from_cluster(args: argparse.Namespace) -> str:
    host = urllib.parse.urlparse(args.schedule_url).hostname or ""
    if not host:
        return "schedule URL has no host"
    command = (
        f"getent hosts {shlex.quote(host)} >/dev/null "
        f"&& wget -q --spider -T {int(args.timeout)} {shlex.quote(args.schedule_url)}"
    )
    result = run(
        ["kubectl", "exec", "-n", args.namespace, args.exec_target, "--", "sh", "-lc", command],
        timeout=args.timeout + 10,
    )
    if result.returncode == 0:
        return ""
    return f"cluster cannot resolve/fetch schedule.json: {summarize_result(result)}"


def probe_kubernetes_api_from_cluster(args: argparse.Namespace) -> str:
    token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    command = (
        f"token=$(cat {shlex.quote(token_path)}) "
        f"&& test \"$(wget -qO- --no-check-certificate -T {int(args.timeout)} "
        f"--header=\"Authorization: Bearer $token\" {shlex.quote(args.api_url)})\" = ok"
    )
    result = run(
        ["kubectl", "exec", "-n", args.namespace, args.api_exec_target, "--", "sh", "-lc", command],
        timeout=args.timeout + 10,
    )
    if result.returncode == 0:
        return ""
    return f"record dispatcher cannot reach Kubernetes API: {summarize_result(result)}"


def probe_nas_write(args: argparse.Namespace) -> str:
    mount = Path(args.nas_mount)
    root = Path(args.nas_probe_dir)
    if not os.path.ismount(mount):
        return f"NAS mount is absent: {mount}"
    last_error = "unknown error"
    for attempt in range(3):
        probe = root / f".rm-monitor-write-probe.{os.getpid()}.{time.time_ns()}"
        try:
            probe.mkdir()
            probe.rmdir()
            return ""
        except OSError as exc:
            last_error = str(exc)
            try:
                probe.rmdir()
            except OSError:
                pass
            if attempt < 2:
                time.sleep(1)
    return f"NAS record target is not writable after 3 attempts: {last_error}"


def maybe_send_alert(args: argparse.Namespace, issues: list[str], repairs: list[str]) -> None:
    if not args.alert:
        return
    fingerprint = "\n".join(sorted(issues))
    state_path = Path(args.state_file)
    state = read_state(state_path)
    now = time.time()
    if (
        state.get("last_alert_fingerprint") == fingerprint
        and now - float(state.get("last_alert_at_ts") or 0) < args.alert_cooldown_seconds
    ):
        return
    try:
        from biliup_upload_match import send_feishu_alert

        text = "\n".join(
            [
                "Cluster DNS / schedule / storage guard failed.",
                "Issues:",
                *[f"- {item}" for item in issues],
                "Repairs:",
                *[f"- {item}" for item in (repairs or ["none"])],
            ]
        )
        send_feishu_alert(args, "RM Monitor infrastructure guard failed", text)
        state["last_alert_fingerprint"] = fingerprint
        state["last_alert_at_ts"] = now
        write_state(state_path, state)
    except Exception as exc:
        local_log.log_event(SERVICE, "WARN", "failed to send DNS guard alert", error=str(exc))


def read_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    tmp.replace(path)


def run(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, timeout=timeout)


def summarize_result(result: subprocess.CompletedProcess[str]) -> str:
    text = (result.stderr or result.stdout or "").strip()
    if len(text) > 500:
        text = text[:497] + "..."
    return f"exit={result.returncode} {text}".strip()


if __name__ == "__main__":
    raise SystemExit(local_log.run_logged(SERVICE, main))
