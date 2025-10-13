# camera-roll-photo-organiser

A small Python utility to scan a source folder of camera photos and videos, extract capture
date and optional GPS coordinates from EXIF metadata, reverse-geocode the GPS into a
country name, and copy files into an organized folder layout.

By default the script copies files (safe first-run behaviour) into folders named
`YYYY-MM-Country` (for example `2024-10-Japan`). A two-level mode is also available
that creates only `YYYY-MM` folders.

This repository contains a single main script at `src/organise_photos.py` which is
intended to be small and self-contained for personal use. It includes optional
HEIC/HEIF support (via `pillow-heif`), EXIF parsing (`exifread`), and reverse
geocoding using `geopy` + Nominatim.

## Features

- Extracts EXIF DateTimeOriginal (falls back to file modified time if missing)
- Extracts GPS coordinates from EXIF when present and reverse-geocodes to country
- Caches geocoding results to reduce API calls
- Uses a proximity cache heuristic: if a new coordinate is within 20 km of a cached
	coordinate the same country is reused
- HEIC/HEIF optional handling (falls back to raw EXIF read when Pillow can't open)
- Copy-by-default; optionally move files with `--move`
- `--report-only` mode writes a CSV describing planned operations without copying
- Option to attempt creation-time preservation on Windows (`--preserve-ctime`)

## Installation

Create a virtual environment and install dependencies from `requirements.txt`.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

If your system Python does not include `venv` (e.g. some minimal Docker images),
install the OS package that provides it (for Debian/Ubuntu: `sudo apt install python3-venv`).

## Basic usage

Scan a folder and copy files into `./organized` (default destination):

```bash
python src/organise_photos.py --src /path/to/unsorted --dst ./organized
```

Dry-run (show first 200 planned operations without copying):

```bash
python src/organise_photos.py --src /path/to/unsorted --dst ./organized --dry-run
```

Report-only (write CSV report and don't copy):

```bash
python src/organise_photos.py --src /path/to/unsorted --dst ./organized --report-only --report-file report.csv
```

Disable HEIC helper (force raw EXIF fallback):

```bash
python src/organise_photos.py --no-heif --src /path/to/unsorted --dst ./organized
```

Move instead of copy:

```bash
python src/organise_photos.py --move --src /path/to/unsorted --dst ./organized
```

Verbose output for debugging:

```bash
python src/organise_photos.py --verbose --src /path/to/unsorted --dst ./organized
```

## Approach / Implementation notes

The script follows a straightforward pipeline:

1. Recursively scan the source directory for files with common image/video extensions.
2. For each file, attempt to read EXIF metadata using `exifread`.
	 - If the file is HEIC/HEIF and `pillow-heif` is available the script will try to
		 open it with Pillow to extract embedded EXIF. If that fails it falls back to a
		 raw byte EXIF parse.
3. Determine the date (EXIF DateTimeOriginal preferred, fallback to file mtime) and
	 extract GPS coordinates when available.
4. If GPS coordinates are present, reverse-geocode with `geopy` + Nominatim. Results
	 are cached in a JSON file under the destination folder to avoid repeated API calls.
	 To improve robustness, 'Unknown' results are not cached and nearby cached coordinates
	 within 20 km will reuse their country to avoid extra reverse lookups.
5. Build a destination path `YYYY-MM-Country` (or `YYYY-MM` in two-level mode) and
	 copy (or move) the file. Filenames are deduplicated by appending a counter on
	 collisions.

Notes and caveats:

- Nominatim is meant for low-volume personal use. For large collections or
	commercial use, consider a paid geocoding provider or an offline country lookup
	(e.g. shapefiles / point-in-polygon) to avoid rate-limits.
- Preservation of creation time is platform dependent; the script attempts to set
	creation time on Windows but POSIX platforms generally do not expose a portable
	API to set 'birth time' (see script docs for suggested alternatives).
- The script is intentionally conservative (copy-by-default) so you can verify
	results before deleting or moving originals.

## Development / Contributing

Contributions welcome.