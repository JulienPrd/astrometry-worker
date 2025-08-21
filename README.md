# Astro Jobs CLI — Async Astrometry Data Extraction

A **standalone** toolset to extract astrometry data from astrophotography images and to manage results as **asynchronous jobs**. It wraps your existing `astro_extract.py` (which runs `solve-field` from astrometry.net, parses EXIF/FITS, computes RA/Dec, FOV, pixel scale, orientation, and nearby catalog objects) and exposes a tiny CLI your backend can call.

---
## 0) What you get
- `astro_extract.py` — the core extractor (prints clean JSON to stdout).
- `astro_jobs_cli.py` — a minimal **job runner** with three commands: `submit`, `status`, `result`.
- A filesystem layout that persists inputs, logs, status, and final JSON per job ID.

---
## 1) Prerequisites
### System packages (Ubuntu/Debian)
```bash
sudo apt update
sudo apt install -y python3-venv astrometry.net libcfitsio-dev exiftool
# Optional but useful: baseline star catalogs (indexes)
sudo apt install -y astrometry-data-tycho2 astrometry-data-2mass
```
- **`solve-field`** will be placed in your PATH by `astrometry.net`.
- Index files are typically stored in `/usr/share/astrometry/` (you can add more later).

### Python packages
Create a virtualenv and install runtime deps used by `astro_extract.py`:
```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install astropy pillow numpy
```

---
## 2) Installation from your GitHub repo
```bash
# Choose a service directory
sudo mkdir -p /srv/astrometry-worker
sudo chown -R $USER:$USER /srv/astrometry-worker
cd /srv/astrometry-worker

# Clone your repo here
git clone https://github.com/JulienPrd/astrometry-worker

# Python env
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Optional: create a global wrapper so your backend can call it easily:
```bash
sudo tee /usr/local/bin/astro-jobs >/dev/null <<'EOF'
#!/usr/bin/env bash
export JOBS_DIR="/srv/astro_jobs"
export ASTRO_EXTRACT_BIN="/srv/astrometry-worker/astro_extract.py"
exec /srv/astrometry-worker/venv/bin/python /srv/astrometry-worker/astro_jobs_cli.py "$@"
EOF
sudo chmod +x /usr/local/bin/astro-jobs
sudo mkdir -p /srv/astro_jobs && sudo chown -R www-data:www-data /srv/astro_jobs
```

---
## 3) Verify your setup
```bash
# 1) Verify binaries and indexes
which solve-field
ls /usr/share/astrometry/ | head

# 2) Run extractor directly (fast smoke test)
source venv/bin/activate
python3 astro_extract.py --image /path/to/image.jpg --downsample 2 --timeout 300 \
  --scale-low 0.5 --scale-high 5
# Expect: JSON to stdout. If error mentions solve-field or indexes, see Troubleshooting.
```

---
## 4) Usage Scenarios
### A) Direct extraction (synchronous)
Use this when you want **immediate** JSON and can block the caller.
```bash
python3 astro_extract.py --image /path/to/image.(jpg|png|fits) \
  --downsample 2 --timeout 600 --scale-low 0.5 --scale-high 5
```
**Output:** JSON like:
```json
{
  "ok": true,
  "filename": "IMG_0132.jpg",
  "capture": { "camera_model": "...", "gps": {"lat": 43.5, "lon": 5.4} },
  "astrometry": {
    "ra_center": 312.3,
    "dec_center": 45.2,
    "fov_deg": [1.2, 0.8],
    "pixscale_arcsec": 2.1,
    "orientation_deg": 178.9,
    "objects": [ {"name": "NGC 6888", "sep_arcmin": 2.3} ]
  }
}
```

### B) Job-based extraction (asynchronous)
Use this when solving can take **time** and you prefer polling.
```bash
# 1) Submit (returns job_id immediately)
astro-jobs submit --image /path/to/image.jpg --scale-low 0.5 --scale-high 5 --downsample 2 --timeout 600
# -> {"ok": true, "job_id": "a1b2c3..."}

# 2) Check status
astro-jobs status --job-id a1b2c3
# -> {"ok": true, "state": "processing"}

# 3) Get result (when done)
astro-jobs result --job-id a1b2c3
# -> JSON from astro_extract.py
```
**Job storage layout** (`$JOBS_DIR/<job_id>/`):
```
input/        # original uploaded file
output/result.json
status.json   # state + timestamps + parameters
task.log      # stdout/stderr of astro_extract.py
```

### C) Integration from your backend (Python snippet)
```python
import subprocess, json, tempfile, pathlib

# Save upload to a temp path your worker can read
img_path = "/path/to/upload.jpg"

# Submit
raw = subprocess.check_output(["astro-jobs", "submit", "--image", img_path, "--scale-low", "0.5", "--scale-high", "5"])
job_id = json.loads(raw)["job_id"]

# Poll status
while True:
    st = json.loads(subprocess.check_output(["astro-jobs", "status", "--job-id", job_id]))
    if st.get("state") in ("done", "error"): break

# Fetch result
result = json.loads(subprocess.check_output(["astro-jobs", "result", "--job-id", job_id]))
```

### D) Deployment & updates from GitHub
```bash
cd /srv/astrometry-worker
# first install: see Section 2
# updates later:
git pull
source venv/bin/activate
pip install -r requirements.txt  # if changed
```

---
## 5) Configuration
Environment variables used by the job runner:
- **`JOBS_DIR`** — where jobs are stored (default: `/srv/astro_jobs`).
- **`ASTRO_EXTRACT_BIN`** — absolute path to `astro_extract.py` (default: `./astro_extract.py` relative to CWD).

You can also tune runtime parameters per job:
- `--scale-low` / `--scale-high` — approximate field-of-view in **degrees** (helps the solver). If unknown, omit; solving may be slower but more robust.
- `--downsample` — speed up solving for large images (default 2).
- `--timeout` — max seconds allowed for solving.

---
## 6) Troubleshooting
**`[Errno 2] No such file or directory: 'solve-field'`**
- Install astrometry: `sudo apt install -y astrometry.net libcfitsio-dev`.
- Verify: `which solve-field`.

**`Index not found` / solver fails to match**
- Install baseline indexes: `sudo apt install -y astrometry-data-tycho2 astrometry-data-2mass`.
- If your lens is **very wide** or **very narrow**, you may need additional index ranges. Place them in `/usr/share/astrometry/` or use `solve-field --dir /path/to/index`.

**Exit code `127` from job runner**
- The extractor wasn’t executed. Ensure `ASTRO_EXTRACT_BIN` points to a valid file, and call it via Python. In `astro_jobs_cli.py` we **recommend** building the command as:
  ```python
  cmd_parts = [sys.executable, ASTRO_EXTRACT_BIN, "--image", image_path]
  ```
  so the same interpreter runs the script.

**Permissions**
- Make sure the user running `astro-jobs` can read the image and write to `$JOBS_DIR`. Typical setup:
  ```bash
  sudo mkdir -p /srv/astro_jobs
  sudo chown -R www-data:www-data /srv/astro_jobs
  ```

**Where are my logs?**
- Per job: `$JOBS_DIR/<job_id>/task.log` (contains stdout/stderr from `astro_extract.py`).

---
## 7) Performance tips
- Use `--downsample 2` (or 4 for very large images) to speed up solving.
- Provide a realistic FOV bracket (`--scale-low`/`--scale-high` in degrees). Example: DSLR + 250 mm lens might be around 4–6° width; small telescopes can be <1°. Start wide, then tighten.
- Set a sensible `--timeout` (e.g. 600 s). If you hit timeouts, verify indexes and FOV.

---
## 8) Housekeeping
**Prune old jobs** with a simple cron (example: keep 14 days):
```bash
sudo tee /etc/cron.daily/astro-jobs-prune >/dev/null <<'EOF'
#!/usr/bin/env bash
find /srv/astro_jobs -maxdepth 1 -mindepth 1 -type d -mtime +14 -exec rm -rf {} +
EOF
sudo chmod +x /etc/cron.daily/astro-jobs-prune
```

---
## 9) Security notes
- If called from a public-facing backend, validate file types and sizes before calling the extractor.
- Consider scanning uploads and enforcing size limits (web server + application layer).
- If images come from untrusted users, avoid executing any external tools beyond `solve-field`/`exiftool` you trust and keep them updated.

---
## 10) Summary
- **Direct mode**: run `astro_extract.py` and get JSON immediately.
- **Job mode**: `astro_jobs_cli.py` lets you submit, check, and retrieve results asynchronously.
- Minimal ops, no extra API or queue manager. Scales by OS processes and a simple job folder.
