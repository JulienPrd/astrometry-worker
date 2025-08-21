#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
astro_extract.py
- Reads photo metadata (EXIF/GPS for JPEG/PNG via exiftool; Pillow fallback; FITS headers for .fits)
- Runs astrometry.net (solve-field) to solve the image
- Extracts RA/Dec, FOV, pixel scale, orientation
- Parses identified objects from solver output and *.objs, computes angular separations to center
- Emits clean JSON to stdout (handles non-serializable EXIF types like IFDRational)

Usage examples:
  python3 astro_extract.py --image ~/images/IMG_0132.jpg --scale-low 1 --scale-high 3 --downsample 2
  python3 astro_extract.py --image ~/images/capture.fits --scale-low 1 --scale-high 3
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from astropy.io import fits
from PIL import Image, ExifTags

from decimal import Decimal
from fractions import Fraction
import math

try:
    from PIL.TiffImagePlugin import IFDRational
except Exception:
    class IFDRational:  # fallback type if not available
        pass

# ------------------------------
# JSON serialization helper
# ------------------------------

def to_native(obj):
    """Recursively convert any object to JSON-serializable Python natives."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj

    # rationals/decimals -> float
    if isinstance(obj, (Fraction, Decimal)):
        return float(obj)
    if isinstance(obj, IFDRational):
        try:
            return float(obj)
        except Exception:
            return str(obj)

    # bytes -> utf-8 (fallback hex)
    if isinstance(obj, (bytes, bytearray)):
        try:
            return obj.decode("utf-8", errors="replace")
        except Exception:
            return obj.hex()

    # numpy support (optional)
    try:
        import numpy as np  # type: ignore
        if isinstance(obj, (np.generic,)):
            return obj.item()
        if isinstance(obj, (np.ndarray,)):
            return [to_native(x) for x in obj.tolist()]
    except Exception:
        pass

    # dicts
    if isinstance(obj, dict):
        return {str(k): to_native(v) for k, v in obj.items()}

    # lists/tuples/sets
    if isinstance(obj, (list, tuple, set)):
        return [to_native(x) for x in obj]

    # fallback string
    return str(obj)

# ------------------------------
# EXIF helpers (JPEG/PNG)
# ------------------------------

def read_exif_with_exiftool(path: Path) -> Dict[str, Any]:
    """Return EXIF dict using exiftool (-j JSON, -n numeric for GPS)."""
    try:
        out = subprocess.check_output(
            ["exiftool", "-j", "-n", str(path)],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        data = json.loads(out)
        return data[0] if data else {}
    except Exception:
        return {}

def read_exif_with_pillow(path: Path) -> Dict[str, Any]:
    """Minimal fallback if exiftool is unavailable."""
    try:
        img = Image.open(path)
        exif_raw = img._getexif() or {}
        out = {}
        for k, v in exif_raw.items():
            tag = ExifTags.TAGS.get(k, str(k))
            out[tag] = v
        return out
    except Exception:
        return {}

def extract_photo_metadata(path: Path) -> Dict[str, Any]:
    exif = read_exif_with_exiftool(path)
    if not exif:
        exif = read_exif_with_pillow(path)

    capture: Dict[str, Any] = {}

    def pick(*keys):
        for k in keys:
            if k in exif:
                return exif[k]
        return None

    # Common EXIF fields
    capture["datetime"] = pick("DateTimeOriginal", "CreateDate", "ModifyDate")
    capture["exposure"] = pick("ExposureTime")
    capture["iso"] = pick("ISO")
    capture["f_number"] = pick("FNumber", "ApertureValue")
    capture["focal_length_mm"] = pick("FocalLength")
    capture["camera_make"] = pick("Make")
    capture["camera_model"] = pick("Model")

    # GPS (if available)
    gps = {}
    lat = pick("GPSLatitude")
    lon = pick("GPSLongitude")
    alt = pick("GPSAltitude")
    if lat is not None and lon is not None:
        try:
            gps["lat"] = float(lat)
            gps["lon"] = float(lon)
            if alt is not None:
                gps["alt"] = float(alt)
            capture["gps"] = gps
        except Exception:
            pass

    # Image dimensions
    try:
        with Image.open(path) as im:
            capture["image_width"] = im.width
            capture["image_height"] = im.height
    except Exception:
        pass

    # Remove Nones
    return {k: v for k, v in capture.items() if v is not None}

# ------------------------------
# FITS helpers
# ------------------------------

def extract_fits_metadata(path: Path) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    try:
        with fits.open(path) as hdul:
            hdr = hdul[0].header
            out["datetime"] = hdr.get("DATE-OBS")
            out["exposure"] = hdr.get("EXPTIME")
            out["filter"] = hdr.get("FILTER")
            out["telescope"] = hdr.get("TELESCOP")
            out["camera_model"] = hdr.get("INSTRUME")
            out["focal_length_mm"] = hdr.get("FOCALLEN")
            if hdr.get("XBINNING") is not None:
                out["binning"] = [hdr.get("XBINNING"), hdr.get("YBINNING")]

            gps = {}
            if hdr.get("SITELAT") is not None and hdr.get("SITELONG") is not None:
                try:
                    gps["lat"] = float(hdr.get("SITELAT"))
                    gps["lon"] = float(hdr.get("SITELONG"))
                    if hdr.get("SITEELEV") is not None:
                        gps["alt"] = float(hdr.get("SITEELEV"))
                    out["gps"] = gps
                except Exception:
                    pass

            if hdr.get("NAXIS1") and hdr.get("NAXIS2"):
                out["image_width"] = int(hdr.get("NAXIS1"))
                out["image_height"] = int(hdr.get("NAXIS2"))
    except Exception:
        pass

    return {k: v for k, v in out.items() if v is not None}

# ------------------------------
# Angular separation + .objs parsing
# ------------------------------

def angsep_deg(ra1_deg: float, dec1_deg: float, ra2_deg: float, dec2_deg: float) -> float:
    """Great-circle angular separation in degrees."""
    ra1 = math.radians(ra1_deg); dec1 = math.radians(dec1_deg)
    ra2 = math.radians(ra2_deg); dec2 = math.radians(dec2_deg)
    return math.degrees(
        math.acos(
            max(-1.0, min(1.0,
                math.sin(dec1)*math.sin(dec2) + math.cos(dec1)*math.cos(dec2)*math.cos(ra1-ra2)
            ))
        )
    )

def parse_objs_file(objs_path: Path) -> List[Dict[str, Any]]:
    """
    Parse the .objs file emitted by solve-field when plots are enabled.
    It often contains lines with object name and RA/Dec (degrees).
    This parser is defensive and supports a few common formats.
    """
    items: List[Dict[str, Any]] = []
    if not objs_path.exists():
        return items

    with objs_path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            t = line.strip()
            if not t or t.startswith("#"):
                continue

            # Common patterns observed:
            # 1) "<catalog_id> <ra_deg> <dec_deg> ..."
            # 2) "<name> ra=<deg> dec=<deg> ..."
            # 3) CSV-like: name,ra,dec,...
            name = None; ra = None; dec = None; catalog = None; ident = None

            # Try CSV-ish first
            if "," in t and t.count(",") >= 2:
                parts = [p.strip() for p in t.split(",")]
                try:
                    name = parts[0]
                    ra = float(parts[1]); dec = float(parts[2])
                except Exception:
                    pass

            # Try ra= dec= tokens
            if ra is None or dec is None:
                toks = t.replace("=", " ").replace("\t", " ").split()
                # Extract ra/dec as floats if present
                for i in range(len(toks)-1):
                    if toks[i].lower() == "ra":
                        try: ra = float(toks[i+1])
                        except: pass
                    if toks[i].lower() == "dec":
                        try: dec = float(toks[i+1])
                        except: pass

                # If still missing, try position-based parsing
                if (ra is None or dec is None) and len(toks) >= 3:
                    try:
                        ra_cand = float(toks[1]); dec_cand = float(toks[2])
                        ra, dec = ra_cand, dec_cand
                        name = toks[0]
                    except Exception:
                        pass

                if name is None and toks:
                    name = " ".join(toks[0:1]).strip()

            # Catalog / ID extraction (best-effort)
            if name:
                parts = name.split()
                if len(parts) >= 2 and parts[0].isalpha():
                    catalog = parts[0]
                    ident = " ".join(parts[1:])

            if name and (ra is not None) and (dec is not None):
                item = {"name": name, "ra_deg": float(ra), "dec_deg": float(dec)}
                if catalog: item["catalog"] = catalog
                if ident: item["id"] = ident
                items.append(item)
            else:
                items.append({"raw": t})

    return items

# ------------------------------
# Astrometry (solve-field)
# ------------------------------

RE_RADEC = re.compile(
    r"Field center: \(RA,Dec\) = \((?P<ra>[-+0-9\.]+), (?P<dec>[-+0-9\.]+)\) deg\."
)
RE_SIZE = re.compile(
    r"Field size: (?P<w>[-+0-9\.]+) x (?P<h>[-+0-9\.]+) degrees"
)
RE_PXSCALE = re.compile(
    r"pixel scale (?P<px>[-+0-9\.]+) arcsec/pix"
)
RE_ORIENT = re.compile(
    r"Field rotation angle: up is (?P<ang>[-+0-9\.]+) degrees E of N"
)

def run_solve_field(image_path: Path,
                    scale_low: Optional[float],
                    scale_high: Optional[float],
                    downsample: Optional[int],
                    timeout: int = 300) -> Tuple[Dict[str, Any], Path]:
    """
    Run solve-field (with plots enabled so it lists field objects).
    Parse stdout for RA/Dec/FOV/pixel scale/orientation and return a workdir path.
    """
    workdir = Path(tempfile.mkdtemp(prefix="astro_solve_"))
    local_img = workdir / image_path.name
    shutil.copy2(image_path, local_img)

    cmd = ["solve-field", "--overwrite", str(local_img)]
    if downsample:
        cmd += ["--downsample", str(downsample)]
    if scale_low is not None and scale_high is not None:
        cmd += ["--scale-units", "degwidth", "--scale-low", str(scale_low), "--scale-high", str(scale_high)]

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(workdir),
            text=True,
            capture_output=True,
            timeout=timeout
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("solve-field timeout")

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""

    # Return code 0 = solved. 3 = often 'solved with warnings'; treat as OK.
    if proc.returncode not in (0, 3):
        raise RuntimeError(f"solve-field failed (code {proc.returncode}).\n--- STDOUT ---\n{stdout}\n--- STDERR ---\n{stderr}")

    astro: Dict[str, Any] = {}

    # Parse stdout
    m = RE_RADEC.search(stdout)
    if m:
        astro["ra_center"] = float(m.group("ra"))
        astro["dec_center"] = float(m.group("dec"))

    m = RE_SIZE.search(stdout)
    if m:
        astro["fov_deg"] = [float(m.group("w")), float(m.group("h"))]

    m = RE_PXSCALE.search(stdout)
    if m:
        astro["pixscale_arcsec"] = float(m.group("px"))

    m = RE_ORIENT.search(stdout)
    if m:
        astro["orientation_deg"] = float(m.group("ang"))

    # Parse identified objects (block "Your field contains:")
    objects: List[Dict[str, Any]] = []
    if "Your field contains:" in stdout:
        lines = stdout.splitlines()
        try:
            start = lines.index("Your field contains:") + 1
            for i in range(start, min(start + 50, len(lines))):
                line = lines[i].strip()
                if not line:
                    break
                # Example: "NGC 6888 / Crescent Nebula"
                name = line
                catalog = None
                ident = None
                if "/" in line:
                    lhs, rhs = [s.strip() for s in line.split("/", 1)]
                    name = rhs
                    parts = lhs.split()
                    if len(parts) >= 2:
                        catalog = parts[0]
                        ident = " ".join(parts[1:])
                    else:
                        ident = lhs
                else:
                    parts = line.split()
                    if len(parts) >= 2 and parts[0].isalpha():
                        catalog = parts[0]
                        ident = " ".join(parts[1:])

                obj = {"raw": line, "name": name}
                if catalog:
                    obj["catalog"] = catalog
                if ident:
                    obj["id"] = ident
                objects.append(obj)
        except ValueError:
            pass

    astro["objects"] = objects

    # Read the generated .wcs header for CRVAL and CD matrix (if present)
    wcs_file = workdir / (image_path.stem + ".wcs")
    if wcs_file.exists():
        try:
            hdr = fits.getheader(wcs_file)
            astro.setdefault("ra_center", float(hdr.get("CRVAL1")))
            astro.setdefault("dec_center", float(hdr.get("CRVAL2")))
            cd = {k: float(hdr.get(k)) for k in ("CD1_1", "CD1_2", "CD2_1", "CD2_2") if hdr.get(k) is not None}
            if cd:
                astro["cd_matrix"] = cd
        except Exception:
            pass

    # --- Enrich objects with RA/Dec from .objs and compute separation ---
    objs_path = workdir / (image_path.stem + ".objs")
    objs_with_radec = parse_objs_file(objs_path)
    ra_c = astro.get("ra_center"); dec_c = astro.get("dec_center")

    def norm(s: str) -> str:
        return s.lower().strip()

    # Merge stdout-derived list with .objs info by name (best-effort)
    merged: List[Dict[str, Any]] = []
    used = set()

    for o in astro.get("objects", []):
        o2 = dict(o)
        key = norm(o2.get("name", o2.get("raw", "")))
        match = None
        for p in objs_with_radec:
            if "name" in p and norm(p["name"]) == key:
                match = p; break
        if match:
            o2.update({k: v for k, v in match.items() if k not in ("raw",)})
        merged.append(o2)
        used.add(key)

    for p in objs_with_radec:
        if "name" in p:
            key = norm(p["name"])
            if key not in used:
                merged.append(p)

    # Compute separations and sort by proximity to center
    for o in merged:
        if ra_c is not None and dec_c is not None and "ra_deg" in o and "dec_deg" in o:
            sep_deg = angsep_deg(float(ra_c), float(dec_c), float(o["ra_deg"]), float(o["dec_deg"]))
            o["sep_arcmin"] = sep_deg * 60.0

    merged.sort(key=lambda x: x.get("sep_arcmin", 1e9))
    astro["objects"] = merged
    # --- end enrich ---

    return astro, workdir

# ------------------------------
# Main
# ------------------------------

def main():
    p = argparse.ArgumentParser(description="Generate JSON by merging EXIF/FITS metadata and astrometric solution.")
    p.add_argument("--image", required=True, help="Path to image (JPG/PNG/FITS).")
    p.add_argument("--scale-low", type=float, default=None, help="FOV min in degrees (degwidth).")
    p.add_argument("--scale-high", type=float, default=None, help="FOV max in degrees (degwidth).")
    p.add_argument("--downsample", type=int, default=2, help="Downsample factor for faster solve (default 2).")
    p.add_argument("--timeout", type=int, default=300, help="solve-field timeout in seconds (default 300).")
    p.add_argument("--keep", action="store_true", help="Keep the temporary working directory (for debugging).")
    args = p.parse_args()

    img_path = Path(args.image).resolve()
    if not img_path.exists():
        print(json.dumps({"ok": False, "error": f"Image not found: {img_path}"}))
        sys.exit(1)

    # Photo metadata
    if img_path.suffix.lower() in (".fits", ".fit", ".fts"):
        meta_photo = extract_fits_metadata(img_path)
    else:
        meta_photo = extract_photo_metadata(img_path)

    # Astrometry
    try:
        astro, workdir = run_solve_field(
            img_path,
            scale_low=args.scale_low,
            scale_high=args.scale_high,
            downsample=args.downsample,
            timeout=args.timeout
        )
    except Exception as e:
        print(json.dumps(to_native({
            "ok": False,
            "error": str(e),
            "image": str(img_path),
            "capture": meta_photo
        }), ensure_ascii=False, indent=2))
        sys.exit(2)

    # Final result
    result = {
        "ok": True,
        "filename": img_path.name,
        "image_path": str(img_path),
        "capture": meta_photo,   # EXIF/FITS + GPS if present
        "astrometry": astro      # RA/Dec, FOV, pixscale, orientation, objects[] sorted by proximity
    }

    print(json.dumps(to_native(result), ensure_ascii=False, indent=2))

    # Cleanup
    if not args.keep:
        try:
            shutil.rmtree(workdir, ignore_errors=True)
        except Exception:
            pass

if __name__ == "__main__":
    main()

