# Astro Jobs CLI — Async Astrometry Data Extraction using Astrometry.net

This project provides a **standalone asynchronous job runner** for astrometric image analysis. It wraps your existing `astro_extract.py` (which calls `astrometry.net`’s `solve-field`, extracts EXIF/FITS metadata, RA/Dec, FOV, pixel scale, orientation, and detected objects) into a simple job system that can be triggered by any backend.

## Purpose
- Automate **astrometric solving** of images (JPG/PNG/FITS).
- Extract metadata (EXIF, FITS headers, GPS, camera/telescope info).
- Run `solve-field` to compute **RA/Dec center, field of view, pixel scale, orientation**.
- Parse identified **catalog objects**, sorted by distance to the image center.
- Store the results as **JSON** retrievable by job ID.

## How it Works
- You call the CLI with `submit` → it creates a **job**, copies the image, and spawns a background process.
- The background worker runs `astro_extract.py` and writes logs, status, and results.
- You can later query the job state (`status`) or fetch the JSON output (`result`).

## Why Jobs?
Astrometric solving can take **minutes** depending on image size and catalog indexes. Instead of blocking, each analysis runs asynchronously and persists results in a structured job folder.

## Environment
- **JOBS_DIR** (default: `/srv/astro_jobs`) → Root folder for jobs.
- **ASTRO_EXTRACT_BIN** (default: `./astro_extract.py`) → Path to the astrometry extraction script.

## Workflow
1. **Submit a job**
```bash
python3 astro_jobs_cli.py submit --image /path/to/image.jpg --scale-low 1 --scale-high 3
# → {"ok": true, "job_id": "abc123..."}
```

2. **Check status**
```bash
python3 astro_jobs_cli.py status --job-id abc123
# → { "job_id": "abc123", "state": "processing", ... }
```

3. **Retrieve result**
```bash
python3 astro_jobs_cli.py result --job-id abc123
# → JSON with metadata + astrometry solution + objects[]
```

## Job Storage Layout
```
<job_id>/
 ├── input/        # Original uploaded image
 ├── output/       # result.json (astrometry data)
 ├── status.json   # Current state, params, timestamps
 └── task.log      # Full stdout/stderr from astro_extract.py
```

## States
- **queued** → job created, worker spawned
- **processing** → astrometry running
- **done** → result.json available
- **error** → failure (details in task.log)

## Integration
- **Backend usage**: call `astro_jobs_cli.py submit` from your API, store the `job_id`, then poll `status` and fetch `result`.
- **Result**: final structured JSON includes EXIF/FITS capture metadata and astrometric solution (RA/Dec, FOV, orientation, objects sorted by proximity).

## Example Output (simplified)
```json
{
  "ok": true,
  "filename": "IMG_0132.jpg",
  "capture": {
    "datetime": "2024-08-21T23:45:00",
    "camera_model": "Canon EOS",
    "gps": {"lat": 43.5, "lon": 5.4}
  },
  "astrometry": {
    "ra_center": 312.3,
    "dec_center": 45.2,
    "fov_deg": [1.2, 0.8],
    "pixscale_arcsec": 2.1,
    "orientation_deg": 178.9,
    "objects": [
      {"name": "Crescent Nebula", "catalog": "NGC", "id": "6888", "sep_arcmin": 2.3},
      {"name": "Star HD12345", "sep_arcmin": 5.1}
    ]
  }
}
```

## Summary
This tool turns **astrometric solving** into a reliable, asynchronous job system:
- Submit → Process → Poll → Retrieve JSON.
- Suitable for integration into any backend API.
- Keeps analysis isolated per job with logs and persistent outputs.

It is the simplest way to convert raw astrophotography images into **structured astrometry data** consumable by applications.
