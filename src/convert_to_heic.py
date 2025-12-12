#!/usr/bin/env python3
"""
convert_to_heic.py

Convert JPEG/PNG images to HEIC format using pillow-heif (when available).
Preserves the last modified time (mtime) of the original files. Optionally will
attempt to preserve creation time on Windows (best-effort).

Usage examples:
  python src/convert_to_heic.py --src /path/to/photos --dst /path/to/heic_out

Features:
- Dry-run mode to preview conversions
- Overwrite / skip existing files
- Quality control
- Preserve mtime on converted files
- Optional attempt to preserve creation time on Windows

This script is conservative: it will not delete originals unless --delete-original
is explicitly specified.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Tuple

from tqdm import tqdm

try:
    import pillow_heif
    # register the HEIF opener so PIL can save/load HEIC via the plugin
    try:
        pillow_heif.register_heif_opener()
    except Exception:
        # non-fatal; we'll still try to use pillow_heif directly if needed
        pass
    from PIL import Image
    HEIF_AVAILABLE = True
except Exception:
    HEIF_AVAILABLE = False


IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.tiff', '.tif'}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description='Convert JPG/PNG images to HEIC')
    p.add_argument('--src', '-s', type=Path, default=Path('.'), help='Source folder to scan')
    p.add_argument('--dst', '-d', type=Path, default=Path('./heic_out'), help='Destination folder')
    p.add_argument('--quality', type=int, default=90, help='HEIC quality (0-100)')
    p.add_argument('--dry-run', action='store_true', help='Show actions but do not write files')
    p.add_argument('--overwrite', action='store_true', help='Overwrite existing HEIC files in destination')
    p.add_argument('--delete-original', action='store_true', help='Delete original file after successful conversion')
    p.add_argument('--preserve-ctime', action='store_true', help='Attempt to preserve creation time on supported platforms')
    p.add_argument('--verbose', '-v', action='store_true')
    return p.parse_args()


def is_source_image(p: Path) -> bool:
    return p.suffix.lower() in IMAGE_EXTS


def find_images(src: Path):
    for root, _dirs, files in os.walk(src):
        for f in files:
            p = Path(root) / f
            if is_source_image(p):
                yield p


def _set_mtime(path: Path, mtime: float) -> None:
    """Set the file modification time (and access time) to mtime."""
    os.utime(path, (mtime, mtime))


def _preserve_creation_time_windows(dst_path: Path, timestamp: float) -> bool:
    try:
        import ctypes
        from ctypes import wintypes

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

        handle = CreateFileW(str(dst_path), FILE_WRITE_ATTRIBUTES, 0, None, OPEN_EXISTING, 0, None)
        if handle == wintypes.HANDLE(-1).value:
            return False

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


def _convert_with_ffmpeg(src_path: Path, dst_path: Path, quality: int) -> Tuple[bool, str]:
    """Fallback encoder using ffmpeg if available. Returns (ok, message)."""
    if not shutil.which('ffmpeg'):
        return False, 'ffmpeg not found'
    # Map quality (0-100) to CRF (lower is better quality). We'll map 100->18, 50->28, 0->40
    try:
        q = max(0, min(100, int(quality)))
        # linear map: crf = 40 - (q/100)*(22) -> q=100 => 18, q=0 => 40
        crf = int(40 - (q / 100.0) * 22)
        crf = max(18, min(50, crf))
    except Exception:
        crf = 28

    cmd = [
        'ffmpeg', '-y', '-i', str(src_path),
        '-c:v', 'libx265', '-crf', str(crf), '-preset', 'medium',
        '-tag:v', 'hvc1', str(dst_path)
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        return True, proc.stderr.decode(errors='ignore')
    except subprocess.CalledProcessError as e:
        return False, (e.stderr.decode(errors='ignore') or str(e))
    except Exception as e:
        return False, str(e)


def convert_image_to_heic(src_path: Path, dst_path: Path, quality: int) -> Tuple[bool, str]:
    """Convert an image to HEIC. Returns (ok, message)."""
    if not HEIF_AVAILABLE:
        return False, 'pillow_heif not available'
    try:
        # Use Pillow to load (handles many formats) then save via pillow_heif
        img = Image.open(src_path)
        img.load()
        # pillow_heif provides save_heif via its plugin for PIL
        # Construct save args; pillow-heif uses 'quality' in save
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            img.save(dst_path, format='HEIC', quality=quality)
        except Exception as e:
            # If PIL save fails, attempt to use pillow_heif.write_heif as a fallback
            logging.debug('PIL HEIC save failed, trying pillow_heif.write_heif fallback: %s', e)
            try:
                # pillow_heif expects data in a specific mode; prefer RGB
                if img.mode not in ('RGB', 'RGBA'):
                    img = img.convert('RGB')
                # Use pillow_heif to write directly
                # First try pillow_heif API if it exposes a helper
                try:
                    if hasattr(pillow_heif, 'from_pillow'):
                        try:
                            heif = pillow_heif.from_pillow(img)
                            heif.save(dst_path, quality=quality)
                        except Exception as e2:
                            return False, f'PIL save failed: {e}; pillow_heif.from_pillow failed: {e2}'
                    elif hasattr(pillow_heif, 'write_heif'):
                        try:
                            pillow_heif.write_heif(img, dst_path, quality=quality)
                        except Exception as e2:
                            return False, f'PIL save failed: {e}; pillow_heif.write_heif failed: {e2}'
                    else:
                        # If pillow_heif doesn't expose a save helper, fall back to ffmpeg if available
                        logging.debug('pillow_heif has no direct write helper; attempting ffmpeg fallback')
                        ok_ff, msg_ff = _convert_with_ffmpeg(src_path, dst_path, quality)
                        if not ok_ff:
                            return False, f'PIL save failed: {e}; ffmpeg fallback failed: {msg_ff}'
                except Exception as e3:
                    return False, f'PIL save failed: {e}; fallback attempt raised: {e3}'
            except Exception as e3:
                return False, f'PIL save failed: {e}; fallback conversion failed: {e3}'
        return True, 'converted'
    except Exception as e:
        return False, str(e)


def run():
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format='%(message)s')

    if not HEIF_AVAILABLE:
        logging.warning('pillow_heif not available; this script requires pillow-heif to write HEIC files')

    src: Path = args.src.resolve()
    dst: Path = args.dst.resolve()
    dst.mkdir(parents=True, exist_ok=True)

    images = list(find_images(src))
    if not images:
        logging.info('No source images found in %s', src)
        return

    converted = 0
    errors = []

    for src_path in tqdm(images, desc='Converting', unit='file'):
        try:
            rel = src_path.relative_to(src)
        except Exception:
            rel = src_path.name
        dst_path = dst / rel.with_suffix('.heic')

        if dst_path.exists() and not args.overwrite:
            logging.info('Skipping existing: %s', dst_path)
            continue

        logging.info('%s -> %s', src_path, dst_path)
        if args.dry_run:
            continue

        # remember original times
        stat = src_path.stat()
        orig_mtime = stat.st_mtime
        orig_ctime = getattr(stat, 'st_ctime', None)

        ok, msg = convert_image_to_heic(src_path, dst_path, args.quality)
        if not ok:
            errors.append((src_path, msg))
            logging.debug('Conversion failed: %s -> %s : %s', src_path, dst_path, msg)
            continue

        # preserve mtime
        try:
            _set_mtime(dst_path, orig_mtime)
        except Exception:
            logging.debug('Failed to set mtime for %s', dst_path)

        # Try to preserve creation time on Windows if requested
        if args.preserve_ctime and orig_ctime is not None:
            try:
                ok_ct = _preserve_creation_time_windows(dst_path, orig_ctime)
                if not ok_ct:
                    logging.debug('Failed to preserve creation time for %s', dst_path)
            except Exception:
                pass

        # Optionally delete original
        if args.delete_original:
            try:
                src_path.unlink()
            except Exception as e:
                logging.debug('Failed to delete original %s: %s', src_path, e)

        converted += 1

    logging.info('\nDone. Converted: %d, errors: %d', converted, len(errors))
    if errors:
        logging.info('Sample errors:')
        for p, e in errors[:10]:
            logging.info(' - %s : %s', p, e)


if __name__ == '__main__':
    run()
