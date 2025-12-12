#!/usr/bin/env python3
"""
organise_photos.py

Scan a source directory for image files, extract date and GPS from EXIF,
reverse-geocode country using geopy, and copy files into a
Year/Month/Country folder structure.

Defaults to copying (not moving) so you can verify results on first run.

Usage examples:
  python src/organise_photos.py --src /path/to/unsorted --dst /path/to/organized

Notes:
 - If EXIF DateTimeOriginal is missing, falls back to file modified time.
 - If GPS is missing, country is set to 'NoLocation' (or 'Unknown').
 - Geocoding results are cached in <dst>/.geocode_cache.json to reduce API calls.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple

import exifread
from PIL import Image, UnidentifiedImageError
@contextlib.contextmanager
def _suppress_c_stderr():
    """Context manager to suppress C-level stderr output (e.g. from native libraries)."""
    try:
        devnull_fd = os.open(os.devnull, os.O_RDWR)
        old_stderr_fd = os.dup(2)
        os.dup2(devnull_fd, 2)
        os.close(devnull_fd)
        yield
    finally:
        try:
            os.dup2(old_stderr_fd, 2)
            os.close(old_stderr_fd)
        except Exception:
            pass

# Try importing pillow_heif but silence any native library stderr during import/registration
HEIF_AVAILABLE = False
try:
    with _suppress_c_stderr():
        import pillow_heif
        pillow_heif.register_heif_opener()
    HEIF_AVAILABLE = True
except Exception:
    HEIF_AVAILABLE = False

import csv
from geopy import Nominatim
from geopy.distance import distance as geopy_distance
from geopy.extra.rate_limiter import RateLimiter
from tqdm import tqdm

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.tiff', '.tif', '.heic', '.heif', '.mp4', '.mov'}

# Maximum number of recent cache entries to check for proximity matching.
# Nearby photos are typically processed together, so recent entries are most relevant.
MAX_CACHE_PROXIMITY_ENTRIES = 100


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description='Organise photos into Year/Month[/Country] folders')
    p.add_argument('--src', '-s', type=Path, default=Path('.'), help='Source folder to scan')
    p.add_argument('--dst', '-d', type=Path, default=Path('./organized'), help='Destination root folder')
    p.add_argument('--two-level', action='store_true', help='Only create Year/Month (skip country level)')
    p.add_argument('--report-only', action='store_true', help='Do not copy/move; write CSV report of planned operations')
    p.add_argument('--report-file', type=Path, help='Path to CSV report file (default: <dst>/report.csv)')
    p.add_argument('--no-heif', action='store_true', help='Disable using pillow-heif and force raw fallback for HEIC/HEIF files')
    p.add_argument('--move', action='store_true', help='Move files instead of copying (default: copy)')
    p.add_argument('--preserve-ctime', action='store_true', help='Attempt to preserve creation time (platform-dependent; Windows supported)')
    p.add_argument('--dry-run', action='store_true', help='Print actions but do not copy/move')
    p.add_argument('--cache-file', type=Path, help='Path to geocode cache file (default: <dst>/.geocode_cache.json)')
    p.add_argument('--verbose', '-v', action='store_true')
    return p.parse_args()


def is_image_file(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTENSIONS


def find_files(src: Path):
    for root, _dirs, files in os.walk(src):
        for f in files:
            p = Path(root) / f
            if is_image_file(p):
                yield p


def _dms_to_decimal(dms, ref) -> Optional[float]:
    # dms is a list of exifread Ratio objects
    try:
        degrees = float(dms[0].num) / float(dms[0].den)
        minutes = float(dms[1].num) / float(dms[1].den)
        seconds = float(dms[2].num) / float(dms[2].den)
        dec = degrees + minutes / 60.0 + seconds / 3600.0
        if ref in ['S', 'W']:
            dec = -dec
        return dec
    except Exception:
        return None


def extract_exif_date_and_gps(path: Path, use_heif: bool = True) -> Tuple[datetime, Optional[Tuple[float, float]]]:
    """Return (datetime, (lat, lon)|None). Date may be from EXIF or file mtime.
    Supports HEIC/HEIF via pillow-heif if available.
    """
    tags = {}
    try:
        suffix = path.suffix.lower()
        if suffix in ('.heic', '.heif') and HEIF_AVAILABLE and use_heif:
            # Use Pillow opener for HEIF and extract EXIF bytes if present.
            # If PIL cannot identify the file, fall back to safe binary read so we don't print noisy errors.
            try:
                # Some underlying libheif implementations print errors directly to C stderr
                # (e.g. "File format not recognized.") even when Python raises OSError. To
                # avoid that noisy output during scans, temporarily redirect fd=2 to /dev/null.
                with _suppress_c_stderr():
                    img = Image.open(path)
            except (UnidentifiedImageError, OSError):
                # Fallback: try to read raw bytes with exifread (may yield no tags)
                try:
                    logging.debug('Pillow failed to open HEIC; falling back to raw byte EXIF read: %s', path)
                    with open(path, 'rb') as fh:
                        tags = exifread.process_file(fh, details=False, stop_tag='GPS GPSLongitude')
                except Exception:
                    tags = {}
            else:
                try:
                    exif_bytes = img.info.get('exif')
                    if exif_bytes:
                        buf = io.BytesIO(exif_bytes)
                        tags = exifread.process_file(buf, details=False, stop_tag='GPS GPSLongitude')
                    else:
                        # Save a JPEG copy to memory and run exifread on it
                        buf = io.BytesIO()
                        img.save(buf, format='JPEG')
                        buf.seek(0)
                        tags = exifread.process_file(buf, details=False, stop_tag='GPS GPSLongitude')
                except Exception:
                    tags = {}
        else:
            with open(path, 'rb') as fh:
                tags = exifread.process_file(fh, details=False, stop_tag='GPS GPSLongitude')
    except Exception:
        tags = {}
    # Date
    date = None
    for tag in ('EXIF DateTimeOriginal', 'EXIF DateTimeDigitized', 'Image DateTime'):
        if tag in tags:
            try:
                val = str(tags[tag])
                # format: YYYY:MM:DD HH:MM:SS
                date = datetime.strptime(val, '%Y:%m:%d %H:%M:%S')
                break
            except Exception:
                date = None

    if date is None:
        # Fallback to file modified time
        try:
            ts = path.stat().st_mtime
            date = datetime.fromtimestamp(ts)
        except Exception:
            date = datetime.now()

    # GPS
    gps = None
    if 'GPS GPSLatitude' in tags and 'GPS GPSLongitude' in tags:
        lat = _dms_to_decimal(tags['GPS GPSLatitude'].values, str(tags.get('GPS GPSLatitudeRef', 'N')))
        lon = _dms_to_decimal(tags['GPS GPSLongitude'].values, str(tags.get('GPS GPSLongitudeRef', 'E')))
        if lat is not None and lon is not None:
            gps = (lat, lon)

    return date, gps


def load_cache(path: Path) -> Dict[str, str]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            return {}
    return {}


def save_cache(path: Path, cache: Dict[str, str]) -> None:
    try:
        path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        pass


def get_country_for_coords(lat: float, lon: float, reverse_func, cache: Dict[str, str]) -> str:
    """Reverse-geocode (lat, lon) using the provided reverse_func callable.

    reverse_func should be a callable with signature reverse_func((lat, lon), **kwargs)
    — the RateLimiter wrapper produced earlier is suitable.
    """
    key = f"{lat:.6f},{lon:.6f}"
    
    # Check for exact match first (fastest)
    if key in cache:
        return cache[key]
    
    # If a nearby cached coordinate exists within 20 km, reuse its country.
    # Optimization: Only check the most recent entries since nearby photos
    # are typically processed together. This reduces O(n) to O(1) for typical usage.
    cache_items = list(cache.items())
    recent_entries = cache_items[-MAX_CACHE_PROXIMITY_ENTRIES:] if len(cache_items) > MAX_CACHE_PROXIMITY_ENTRIES else cache_items
    
    for k, v in recent_entries:
        try:
            parts = k.split(',')
            if len(parts) != 2:
                continue
            clat = float(parts[0])
            clon = float(parts[1])
        except Exception:
            continue
        try:
            d_km = geopy_distance((lat, lon), (clat, clon)).km
        except Exception:
            continue
        if d_km <= 20.0 and v and v != 'Unknown':
            logging.debug('Using nearby cached country for %s,%s -> %s (cached at %s,%s %0.1f km)', lat, lon, v, clat, clon, d_km)
            return v
    try:
        # Call the provided reverse function (rate-limited wrapper expected)
        loc = reverse_func((lat, lon), language='en', exactly_one=True)
        logging.debug('Reverse geocode result for %s,%s: %r', lat, lon, loc)
        country = None
        # loc.raw is commonly present and contains 'address'
        if hasattr(loc, 'raw') and loc.raw:
            addr = loc.raw.get('address', {}) if isinstance(loc.raw, dict) else {}
            # try common keys
            country = addr.get('country') or addr.get('country_name') or addr.get('country_code')
            logging.debug('Address dict keys: %s', list(addr.keys()) if isinstance(addr, dict) else None)
        # fallback to attributes like display_name or address
        if not country:
            country = getattr(loc, 'address', None) or getattr(loc, 'display_name', None)
        # Last resort: stringify loc
        if not country and loc is not None:
            country = str(loc)
        country = country or 'Unknown'
    except Exception as exc:
        logging.debug('Reverse geocode failed for %s,%s: %s', lat, lon, exc)
        country = 'Unknown'
    # Only cache meaningful results; avoid caching 'Unknown' so transient failures can be retried
    if country and country != 'Unknown':
        cache[key] = country
    else:
        logging.debug('Not caching Unknown result for %s', key)
    return country


def _set_creation_time_windows(path: Path, timestamp: float) -> bool:
    """Set file creation time on Windows. Returns True on success."""
    try:
        import ctypes
        from ctypes import wintypes

        # constants
        FILE_WRITE_ATTRIBUTES = 0x0100
        OPEN_EXISTING = 3

        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

        CreateFileW = kernel32.CreateFileW
        CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        CreateFileW.restype = wintypes.HANDLE

        SetFileTime = kernel32.SetFileTime
        SetFileTime.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME)]
        SetFileTime.restype = wintypes.BOOL

        CloseHandle = kernel32.CloseHandle
        CloseHandle.argtypes = [wintypes.HANDLE]
        CloseHandle.restype = wintypes.BOOL

        handle = CreateFileW(str(path), FILE_WRITE_ATTRIBUTES, 0, None, OPEN_EXISTING, 0, None)
        if handle == wintypes.HANDLE(-1).value:
            return False

        # Convert Unix epoch to Windows FILETIME (100-ns intervals since 1601)
        windows_ts = int((timestamp + 11644473600) * 10000000)
        low = windows_ts & 0xFFFFFFFF
        high = windows_ts >> 32

        class FILETIME(ctypes.Structure):
            _fields_ = [('dwLowDateTime', wintypes.DWORD), ('dwHighDateTime', wintypes.DWORD)]

        c_time = FILETIME(low, high)

        res = SetFileTime(handle, ctypes.byref(c_time), None, None)
        CloseHandle(handle)
        return bool(res)
    except Exception:
        return False


def set_creation_time(path: Path, timestamp: float) -> bool:
    """Best-effort: set creation time where supported. Returns True on success."""
    if os.name == 'nt':
        return _set_creation_time_windows(path, timestamp)
    # On POSIX there's no portable API to set 'birth time'. We preserve atime/mtime via copy2.
    logging.debug('Creation time preservation not supported on this platform: %s', os.name)
    return False


def slugify_folder_name(s: str) -> str:
    # Keep it simple: remove problematic chars
    return ''.join(c for c in s if c.isalnum() or c in (' ', '-', '_')).strip()


def run():
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format='%(message)s')

    src: Path = args.src.resolve()
    dst: Path = args.dst.resolve()
    dst.mkdir(parents=True, exist_ok=True)

    cache_file = args.cache_file or (dst / '.geocode_cache.json')
    geocode_cache = load_cache(cache_file)

    # Setup geolocator with rate limiter
    geolocator = Nominatim(user_agent='photo-organiser-script-1')
    geolocator_reverse = RateLimiter(geolocator.reverse, min_delay_seconds=1, max_retries=2, error_wait_seconds=2.0)

    files = list(find_files(src))
    if not files:
        logging.info('No image files found under %s', src)
        return

    errors = []
    records = []
    for path in tqdm(files, desc='Scanning files'):
        try:
            date, gps = extract_exif_date_and_gps(path, use_heif=(not args.no_heif))
            year = f"{date.year:04d}"
            month_num = f"{date.month:02d}"
            if args.two_level:
                # Year-Month only
                folder_name = f"{year}-{month_num}"
                target_dir = dst / folder_name
                country = ''
            else:
                country = 'NoLocation'
                if gps is not None:
                    country = get_country_for_coords(gps[0], gps[1], geolocator_reverse, geocode_cache)
                country = slugify_folder_name(country)
                folder_name = f"{year}-{month_num}-{country}"
                target_dir = dst / folder_name

            target_dir.mkdir(parents=True, exist_ok=True)

            target_path = target_dir / path.name
            # If collision, append a counter
            if target_path.exists():
                base = target_path.stem
                ext = target_path.suffix
                i = 1
                while (target_dir / f"{base}-{i}{ext}").exists():
                    i += 1
                target_path = target_dir / f"{base}-{i}{ext}"

            records.append({
                'src': str(path),
                'date': date.isoformat(),
                'lat': gps[0] if gps else '',
                'lon': gps[1] if gps else '',
                'country': country,
                'target': str(target_path),
                'error': ''
            })
        except Exception as e:
            records.append({
                'src': str(path),
                'date': '',
                'lat': '',
                'lon': '',
                'country': '',
                'target': '',
                'error': str(e)
            })

    # Summary
    logging.info('\nPlanned operations: %d files', len(records))

    # If report-only, write CSV and exit
    if args.report_only:
        report_path = args.report_file or (dst / 'report.csv')
        with open(report_path, 'w', newline='', encoding='utf-8') as csvfh:
            writer = csv.DictWriter(csvfh, fieldnames=['src', 'date', 'lat', 'lon', 'country', 'target', 'error'])
            writer.writeheader()
            for r in records:
                writer.writerow(r)
        logging.info('Report written to %s', report_path)
        return

    if args.dry_run:
        for r in records[:200]:
            logging.info('%s -> %s', r['src'], r['target'])
        logging.info('Dry run: no files copied/moved.')
        return

    # Execute copy/move
    copied = 0
    for r in tqdm(records, desc='Copying files'):
        src_path = Path(r['src'])
        dst_path = Path(r['target'])
        try:
            if not dst_path.parent.exists():
                dst_path.parent.mkdir(parents=True, exist_ok=True)
            if args.move:
                shutil.move(str(src_path), str(dst_path))
            else:
                # copy2 preserves mtime/atime; creation time preservation is platform dependent
                shutil.copy2(str(src_path), str(dst_path))
                if args.preserve_ctime:
                    try:
                        # Attempt to read original creation time; fall back to mtime
                        stat = src_path.stat()
                        # On Windows st_ctime is creation time; on POSIX it's metadata change
                        orig_ctime = getattr(stat, 'st_ctime', None) or getattr(stat, 'st_mtime', None)
                        if orig_ctime is not None:
                            ok = set_creation_time(dst_path, orig_ctime)
                            if not ok:
                                logging.debug('Failed to set creation time for %s', dst_path)
                    except Exception:
                        logging.debug('Error preserving creation time for %s', src_path)
            copied += 1
        except Exception as e:
            errors.append((src_path, str(e)))

    save_cache(cache_file, geocode_cache)

    logging.info('\nDone. Files processed: %d, errors: %d', copied, len(errors))
    if errors:
        logging.info('Sample errors:')
        for p, e in errors[:10]:
            logging.info(' - %s : %s', p, e)


if __name__ == '__main__':
    run()
