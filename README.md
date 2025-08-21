# astro_jobs_cli.py — Standalone Async Job Runner

This repository contains a minimal standalone job runner that wraps your existing `astro_extract.py` script. It allows you to submit image analysis jobs, check their status, and fetch results — all without running a dedicated API or Celery worker. It is designed to be invoked **from another backend** (e.g., via `subprocess`).

## Features
- **Asynchronous execution**: `submit` launches the analysis in the background and immediately returns a `job_id`.
- **Persistent state**: Each job is stored under `$JOBS_DIR/<job_id>/` with subfolders for inputs, outputs, logs, and JSON metadata.
- **Status tracking**: Query a job to see if it’s `queued`, `processing`, `done`, or `error`.
- **Result retrieval**: Fetch the JSON output produced by `astro_extract.py`.
- **No external services required**: Jobs are executed as detached OS processes.

## Requirements
- Python 3.9+
- Dependencies required by `astro_extract.py` (e.g., `astrometry.net`, `exiftool`, `astropy`, `Pillow`).

## Environment Variables
- **`JOBS_DIR`**: Path where jobs are stored (default: `/srv/astro_jobs`).
- **`ASTRO_EXTRACT_BIN`**: Path to your `astro_extract.py` script (default: `./astro_extract.py` in the current directory).

## CLI Usage
### Submit a new job
```bash
python3 astro_jobs_cli.py submit --image /path/to/image.jpg \
    --scale-low 1 --scale-high 3 --downsample 2 --timeout 600
# → {"ok": true, "job_id": "a1b2c3..."}
```

### Check job status
```bash
python3 astro_jobs_cli.py status --job-id a1b2c3...
# → { "ok": true, "job_id": "...", "state": "processing", ... }
```

### Fetch job result
```bash
python3 astro_jobs_cli.py result --job-id a1b2c3...
# → JSON output from astro_extract.py (if ready)
```

## Job Folder Structure
Each job is stored under `$JOBS_DIR/<job_id>/` with the following contents:
```
<job_id>/
 ├── input/        # Uploaded input image
 ├── output/       # result.json (final output)
 ├── status.json   # Job state metadata
 └── task.log      # Stdout/stderr of astro_extract.py
```

## States
- **queued** → job created, worker process started
- **processing** → astro_extract.py is running
- **done** → analysis complete, JSON available in `output/result.json`
- **error** → failure, see `task.log` and `status.json`

## Integration Example (Python)
If your backend is written in Python, you can call the CLI with `subprocess`:
```python
import subprocess, json

# Submit a job
out = subprocess.check_output([
    "python3", "astro_jobs_cli.py", "submit", "--image", "/tmp/img.jpg"
])
job_id = json.loads(out)["job_id"]

# Poll status
status = subprocess.check_output([
    "python3", "astro_jobs_cli.py", "status", "--job-id", job_id
])
print(status.decode())

# Fetch result
result = subprocess.check_output([
    "python3", "astro_jobs_cli.py", "result", "--job-id", job_id
])
print(result.decode())
```

## Notes
- Logs from each run are written to `task.log` inside the job folder.
- You can implement a cleanup routine to prune old jobs.
- If desired, add a `cancel` feature using PID tracking and signals.

---
This tool makes it easy to integrate heavy astrometric image analysis into an existing backend, without the overhead of managing an additional API or queue system.
