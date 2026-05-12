#!/usr/bin/env python3
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Callable


DEFAULT_LOG_DIR = Path(__file__).resolve().parents[2] / "logs"


def log_event(service: str, level: str, msg: str, **fields: object) -> None:
    entry = {
        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "level": level.upper(),
        "service": service,
        "msg": msg,
    }
    entry.update(fields)
    try:
        log_dir().mkdir(parents=True, exist_ok=True)
        with log_path(service).open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception as exc:
        print(f"failed to write local RM Monitor log: {exc}", file=sys.stderr)


def run_logged(service: str, main_func: Callable[[], int]) -> int:
    log_event(service, "INFO", "script started", argv=sys.argv[1:])
    try:
        code = main_func()
    except KeyboardInterrupt:
        log_event(service, "WARN", "script interrupted", exit_code=130)
        return 130
    except SystemExit as exc:
        code, detail = normalize_exit(exc.code)
        if detail:
            print(detail, file=sys.stderr)
        if code:
            log_event(service, "ERROR", "script exited with error", exit_code=code, detail=detail)
        else:
            log_event(service, "INFO", "script completed", exit_code=0)
        return code
    except Exception as exc:
        traceback.print_exc()
        log_event(
            service,
            "ERROR",
            "script crashed",
            exit_code=1,
            error=str(exc),
            traceback=traceback.format_exc(limit=6),
        )
        return 1
    if code:
        log_event(service, "ERROR", "script exited with error", exit_code=code)
    else:
        log_event(service, "INFO", "script completed", exit_code=0)
    return int(code or 0)


def log_dir() -> Path:
    return Path(os.environ.get("RM_MONITOR_LOCAL_LOG_DIR") or DEFAULT_LOG_DIR)


def log_path(service: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in service)
    return log_dir() / f"{safe}.log"


def normalize_exit(code: object) -> tuple[int, str]:
    if code is None:
        return 0, ""
    if isinstance(code, int):
        return code, ""
    return 1, str(code)
