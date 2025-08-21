#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
astro_jobs_cli.py — Minimal standalone async job runner for astro_extract.py

This script is designed to be called by *your backend* (no web server here).
It exposes a tiny CLI:

  1) Submit a job (non-blocking):
     python3 astro_jobs_cli.py submit --image /path/img.jpg \
         [--scale-low 1] [--scale-high 3] [--downsample 2] [--timeout 600]
     -> prints JSON: {"job_id": "..."}

  2) Get job status:
     python3 astro_jobs_cli.py status --job-id <id>
     -> prints JSON: { job_id, state, message, created_at, updated_at, params }

  3) Get job result:
     python3 astro_jobs_cli.py result --job-id <id>
     -> prints the JSON produced by astro_extract.py (or error if not ready)

States: queued → processing → done | error

Environment:
  JOBS_DIR           (default: /srv/astro_jobs)
  ASTRO_EXTRACT_BIN  (default: ./astro_extract.py in current working directory)

All job artifacts are under: $JOBS_DIR/<job_id>/{input,output,status.json,task.log}

Implementation notes:
- "submit" spawns a detached subprocess of *this* script with the hidden
  subcommand "_worker" to process a single job, redirecting stdout/stderr to a log.
- No daemon or Celery required. One OS process per job.
"""

from __future__ import annotations
import argparse
import json
import os
import shutil
import sys
import time
import uuid
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

# ------------------------------
# Config
# ------------------------------
JOBS_DIR = Path(os.getenv("JOBS_DIR", "/srv/astro_jobs")).resolve()
ASTRO_EXTRACT_BIN = os.getenv("ASTRO_EXTRACT_BIN", str(Path.cwd() / "astro_extract.py"))

# ------------------------------
# Helpers
# ------------------------------
@dataclass
class JobPaths:
    root: Path
    input: Path
    output: Path
    status_file: Path
    result_file: Path
    log_file: Path


def ensure_job_dirs(job_id: str) -> JobPaths:
    root = JOBS_DIR / job_id
    input_dir = root / "input"
    output_dir = root / "output"
    status_file = root / "status.json"
    result_file = output_dir / "result.json"
    log_file = root / "task.log"
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    return JobPaths(root, input_dir, output_dir, status_file, result_file, log_file)


def write_status(job_id: str, state: str, message: Optional[str] = None, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    paths = ensure_job_dirs(job_id)
    now = time.time()
    if paths.status_file.exists():
        try:
            current = json.loads(paths.status_file.read_text())
        except Exception:
            current = {}
    else:
        current = {"job_id": job_id, "created_at": now}
    current.update({
        "job_id": job_id,
        "state": state,
        "message": message,
        "params": params or current.get("params"),
        "updated_at": now,
    })
    paths.status_file.write_text(json.dumps(current, ensure_ascii=False, indent=2))
    return current


def read_status(job_id: str) -> Optional[Dict[str, Any]]:
    paths = ensure_job_dirs(job_id)
    if not paths.status_file.exists():
        return None
    try:
        return json.loads(paths.status_file.read_text())
    except Exception:
        return None


# ------------------------------
# Worker sub-command (internal)
# ------------------------------

def run_worker(job_id: str, image_path: str, scale_low: Optional[float], scale_high: Optional[float], downsample: Optional[int], timeout: Optional[int]) -> int:
    params: Dict[str, Any] = {
        "scale_low": scale_low,
        "scale_high": scale_high,
        "downsample": downsample,
        "timeout": timeout,
    }
    paths = ensure_job_dirs(job_id)
    write_status(job_id, state="processing", message="Solving image…", params=params)

    # Build command to run astro_extract.py
    cmd_parts = [
        shlex.quote(ASTRO_EXTRACT_BIN),
        "--image", shlex.quote(image_path),
    ]
    if scale_low is not None:
        cmd_parts += ["--scale-low", shlex.quote(str(scale_low))]
    if scale_high is not None:
        cmd_parts += ["--scale-high", shlex.quote(str(scale_high))]
    if downsample is not None:
        cmd_parts += ["--downsample", shlex.quote(str(downsample))]
    if timeout is not None:
        cmd_parts += ["--timeout", shlex.quote(str(timeout))]

    cmd = " ".join(cmd_parts)

    # Execute and capture
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        # Append logs
        with paths.log_file.open("a", encoding="utf-8") as lf:
            lf.write(f"[cmd] {cmd}\n")
            lf.write("[stdout]\n" + (proc.stdout or "") + "\n")
            lf.write("[stderr]\n" + (proc.stderr or "") + "\n")
    except Exception as e:
        write_status(job_id, state="error", message=f"Execution failed: {e}")
        return 1

    # Interpret stdout -> result.json
    if proc.returncode in (0, 2):  # 0: ok ; 2: astro_extract prints JSON error payload
        try:
            result = json.loads(proc.stdout)
        except Exception:
            result = {"ok": False, "error": "Invalid JSON from astro_extract", "raw": (proc.stdout or "")[-2000:]}
    else:
        result = {"ok": False, "error": f"astro_extract exit code {proc.returncode}"}

    paths.result_file.write_text(json.dumps(result, ensure_ascii=False, indent=2))

    if result.get("ok"):
        write_status(job_id, state="done", message="Analysis complete")
        return 0
    else:
        write_status(job_id, state="error", message=str(result.get("error")))
        return 2


# ------------------------------
# Public CLI commands
# ------------------------------

def cmd_submit(args: argparse.Namespace) -> int:
    # Prepare job
    job_id = uuid.uuid4().hex
    paths = ensure_job_dirs(job_id)

    # Copy input file into job folder
    src = Path(args.image).resolve()
    if not src.exists():
        print(json.dumps({"ok": False, "error": f"Image not found: {src}"}))
        return 1
    dst = paths.input / src.name
    shutil.copy2(src, dst)

    params = {
        "scale_low": args.scale_low,
        "scale_high": args.scale_high,
        "downsample": args.downsample,
        "timeout": args.timeout,
    }
    write_status(job_id, state="queued", message="Job created", params=params)

    # Spawn detached worker: call this script with _worker
    py = shlex.quote(sys.executable)
    me = shlex.quote(str(Path(__file__).resolve()))
    worker_cmd = [
        py, me, "_worker",
        "--job-id", job_id,
        "--image", str(dst),
    ]
    if args.scale_low is not None:
        worker_cmd += ["--scale-low", str(args.scale_low)]
    if args.scale_high is not None:
        worker_cmd += ["--scale-high", str(args.scale_high)]
    if args.downsample is not None:
        worker_cmd += ["--downsample", str(args.downsample)]
    if args.timeout is not None:
        worker_cmd += ["--timeout", str(args.timeout)]

    # Open log for the child process
    log_fh = paths.log_file.open("a")

    # Detach: new session so it survives parent exit
    subprocess.Popen(
        worker_cmd,
        stdout=log_fh,
        stderr=log_fh,
        start_new_session=True,
        cwd=str(paths.root),
    )

    print(json.dumps({"ok": True, "job_id": job_id}))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    st = read_status(args.job_id)
    if not st:
        print(json.dumps({"ok": False, "error": "Job not found", "job_id": args.job_id}))
        return 1
    print(json.dumps({"ok": True, **st}, ensure_ascii=False, indent=2))
    return 0


def cmd_result(args: argparse.Namespace) -> int:
    paths = ensure_job_dirs(args.job_id)
    if not paths.result_file.exists():
        st = read_status(args.job_id)
        state = st.get("state") if st else "unknown"
        print(json.dumps({"ok": False, "error": f"Job not ready (state={state})", "job_id": args.job_id}))
        return 1
    try:
        data = json.loads(paths.result_file.read_text())
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"Invalid result JSON: {e}", "job_id": args.job_id}))
        return 2
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


# Hidden subcommand used by the detached worker

def cmd__worker(args: argparse.Namespace) -> int:
    return run_worker(
        job_id=args.job_id,
        image_path=args.image,
        scale_low=args.scale_low,
        scale_high=args.scale_high,
        downsample=args.downsample,
        timeout=args.timeout,
    )


# ------------------------------
# Entry point
# ------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Standalone async job runner for astro_extract.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    # submit
    s = sub.add_parser("submit", help="Create a job and start processing asynchronously")
    s.add_argument("--image", required=True, help="Path to input image (JPG/PNG/FITS)")
    s.add_argument("--scale-low", type=float, default=None)
    s.add_argument("--scale-high", type=float, default=None)
    s.add_argument("--downsample", type=int, default=2)
    s.add_argument("--timeout", type=int, default=600)
    s.set_defaults(func=cmd_submit)

    # status
    s = sub.add_parser("status", help="Get status for a job")
    s.add_argument("--job-id", required=True)
    s.set_defaults(func=cmd_status)

    # result
    s = sub.add_parser("result", help="Get final JSON result for a job")
    s.add_argument("--job-id", required=True)
    s.set_defaults(func=cmd_result)

    # hidden worker
    s = sub.add_parser("_worker")
    s.add_argument("--job-id", required=True)
    s.add_argument("--image", required=True)
    s.add_argument("--scale-low", type=float, default=None)
    s.add_argument("--scale-high", type=float, default=None)
    s.add_argument("--downsample", type=int, default=2)
    s.add_argument("--timeout", type=int, default=600)
    s.set_defaults(func=cmd__worker)

    return p


def main() -> int:
    # Ensure base dir exists
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
