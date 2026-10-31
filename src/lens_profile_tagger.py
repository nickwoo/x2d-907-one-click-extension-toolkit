#!/usr/bin/env python3
"""Desktop tagger, Tier A (Phase 2, strand A): lens-profile EXIF tagging.

Reads the camera-side selection log written by src/lens-profile-worker.sh
(/blackbox/x2d-lens-profile.log, one line per selection:
``timestamp,id,aperture`` with the timestamp in UTC, the aperture possibly
blank) and writes the lens that was active at each photo's capture time
into the photo's EXIF: LensModel, FocalLength, and FNumber when known.

Safety, in order:
  * Dry-run is the default. Nothing is written and no backup is made
    unless ``--apply`` is given explicitly.
  * ``--apply`` first copies each photo to a timestamped backup (same
    directory by default, ``--backup-dir`` to move them) and refuses to
    overwrite an existing backup. If the EXIF write then fails, the
    backup is restored over the photo.
  * Nothing is ever guessed. An unknown profile id, a photo taken before
    any selection, a blank aperture, or a missing capture time is
    reported as skipped/partial, never filled in from another lens, from
    the profile's max aperture, or from a neighbouring photo.

Transport: the tagger does not invent a new camera channel. Pulling the
log uses the repository's existing ADB pattern (``adb -d pull`` for the
single physical USB device, as in src/x2d_play_software.py), and ADB
itself must already be enabled through the existing factory-USB path
(src/transport/collect_x2d_af_usb.py, x2d_play_software.ensure_adb).
Offline use is the norm: pass a previously pulled log with ``--log``.
The tagger never enables ADB, never touches the factory interface, and
never talks to the camera service.

EXIF writing: the tagger shells out to ``exiftool`` (not bundled; the
same "bring your own platform tool" shape as the bundled ``adb``).
Direct 3FR rewriting is deliberately NOT attempted here: Hasselblad raw
files are not hand-edited, and exiftool support for them is unvalidated
on device (see docs/lens-profile-tagger.md). For 3FR originals the
dry-run report / JSON plan is the safe output; apply to exported
JPEG/DNG derivatives, or to 3FR only once exiftool behaviour on 3FR has
been validated against a disposable copy.

Clock handling: worker log timestamps are UTC. Photo capture times are
compared after adding ``--camera-offset-seconds`` to each log timestamp
(positive when the photo clock runs ahead of the camera clock). There is
no automatic drift estimation: a guessed offset would silently retag
whole shoots, so the offset is explicit and recorded in the report.
Naive (timezone-free) timestamps are treated as UTC; supply the offset
to correct a local-time photo clock.

Selection persistence: the worker has no "deselect" action, so a
selection stays active until the next selection. By default the tagger
mirrors that: a photo long after the last selection still matches it.
``--max-selection-age-seconds`` optionally turns that into a stale-gap
skip, for shoots where a stale selection is more likely wrong than
right. That flag is off by default because expiring a selection is a
judgement call, not a worker semantic.

CoC: this tool does not use, compute, or change the Phase 1 circle-of-
confusion constant (0.025 mm, PROVISIONAL). It is out of scope here.

Run from the repository root, e.g.:
  python3 src/lens_profile_tagger.py --profiles src/X2dLensProfiles.json \
      --log /tmp/x2d-lens-profile.log --manifest photos.json
  python3 src/lens_profile_tagger.py --profiles src/X2dLensProfiles.json \
      --pull --photos ~/Pictures/x2d --apply
"""

from __future__ import annotations

import argparse
import json
import shutil
import struct
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOG_PATH_ON_CAMERA = "/blackbox/x2d-lens-profile.log"
DEFAULT_ADB = "adb"
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".dng", ".tif", ".tiff", ".3fr"}
# EXIF tag ids used by the minimal JPEG reader below.
_EXIF_IFD_POINTER = 0x8769
_DATETIME_ORIGINAL = 0x9003


class TaggerError(RuntimeError):
    """Fatal, run-aborting error (missing input, failed pull, no exiftool)."""


@dataclass(frozen=True)
class Profile:
    id: str
    name: str
    focal_length_mm: float
    max_aperture: float


@dataclass(frozen=True)
class LogEntry:
    timestamp: datetime  # aware, UTC, as written by the worker
    profile_id: str
    aperture: float | None  # None when the worker line left it blank
    line_no: int

    @property
    def effective_time(self) -> datetime:
        return self.timestamp


@dataclass
class PhotoRecord:
    path: Path
    capture_time: datetime | None  # aware UTC, or None when unknown


@dataclass
class TagResult:
    photo: PhotoRecord
    status: str  # matched | partial-blank-aperture | skipped-*
    profile: Profile | None = None
    aperture: float | None = None
    log_entry: LogEntry | None = None
    backup: Path | None = None
    reason: str = ""
    exiftool_args: list = field(default_factory=list)


def parse_timestamp(text: str) -> datetime:
    """Parse an ISO-8601 or EXIF datetime into an aware UTC datetime.

    Naive values are treated as UTC (see module docstring: correct a
    local-time photo clock with --camera-offset-seconds).
    """
    text = text.strip()
    if not text:
        raise ValueError("empty timestamp")
    if " " in text and "T" not in text and ":" in text and text[4] == ":":
        # EXIF form: YYYY:MM:DD HH:MM:SS
        value = datetime.strptime(text, "%Y:%m:%d %H:%M:%S")
        return value.replace(tzinfo=timezone.utc)
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_profiles(data: dict) -> dict[str, Profile]:
    if data.get("format") != 1:
        raise TaggerError("profiles file has unexpected format (expected 1)")
    profiles: dict[str, Profile] = {}
    for item in data.get("profiles", []):
        profile = Profile(
            id=str(item["id"]),
            name=str(item["name"]),
            focal_length_mm=float(item["focalLengthMm"]),
            max_aperture=float(item["maxAperture"]),
        )
        if not profile.id or not profile.name:
            raise TaggerError("profile with empty id or name")
        if profile.focal_length_mm <= 0 or profile.max_aperture <= 0:
            raise TaggerError(f"profile {profile.id} has non-positive focal/aperture")
        if profile.id in profiles:
            raise TaggerError(f"duplicate profile id {profile.id}")
        profiles[profile.id] = profile
    if not profiles:
        raise TaggerError("profiles file contains no profiles")
    return profiles


def load_profiles(path: Path) -> dict[str, Profile]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise TaggerError(f"cannot read profiles {path}: {error}") from error
    return parse_profiles(data)


def parse_log(text: str) -> tuple[list[LogEntry], list[str]]:
    """Parse worker log text. Malformed lines are warned about, not fatal:
    one corrupt line must not discard a whole shoot's selections."""
    entries: list[LogEntry] = []
    warnings: list[str] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) != 3:
            warnings.append(f"log line {line_no}: expected 3 fields, got {len(parts)}")
            continue
        timestamp_text, profile_id, aperture_text = (p.strip() for p in parts)
        try:
            timestamp = parse_timestamp(timestamp_text)
        except ValueError:
            warnings.append(f"log line {line_no}: unparseable timestamp {timestamp_text!r}")
            continue
        if not profile_id:
            warnings.append(f"log line {line_no}: empty profile id")
            continue
        aperture: float | None = None
        if aperture_text:
            try:
                aperture = float(aperture_text)
            except ValueError:
                warnings.append(f"log line {line_no}: unparseable aperture {aperture_text!r}")
                continue
            if aperture <= 0:
                warnings.append(f"log line {line_no}: non-positive aperture")
                continue
        entries.append(LogEntry(timestamp, profile_id, aperture, line_no))
    entries.sort(key=lambda e: (e.timestamp, e.line_no))
    return entries, warnings


def load_log(path: Path) -> tuple[list[LogEntry], list[str]]:
    try:
        return parse_log(path.read_text(encoding="utf-8", errors="replace"))
    except OSError as error:
        raise TaggerError(f"cannot read log {path}: {error}") from error


def match_photo(
    photo: PhotoRecord,
    entries: list[LogEntry],
    profiles: dict[str, Profile],
    camera_offset: timedelta = timedelta(0),
    max_selection_age: timedelta | None = None,
) -> TagResult:
    """Match one photo to the selection active at its capture time."""
    if photo.capture_time is None:
        return TagResult(photo, "skipped-no-capture-time",
                         reason="no capture time available; refusing to guess")
    if not entries:
        return TagResult(photo, "skipped-empty-log",
                         reason="worker log contains no selections")
    shifted = [(e.timestamp + camera_offset, e) for e in entries]
    active = [(t, e) for t, e in shifted if t <= photo.capture_time]
    if not active:
        return TagResult(photo, "skipped-before-first-selection",
                         reason="photo predates the first logged selection")
    effective_time, entry = active[-1]
    if max_selection_age is not None and photo.capture_time - effective_time > max_selection_age:
        return TagResult(photo, "skipped-stale-gap",
                         reason=f"last selection is {photo.capture_time - effective_time} "
                                f"older than --max-selection-age-seconds")
    profile = profiles.get(entry.profile_id)
    if profile is None:
        return TagResult(photo, "skipped-unknown-profile", log_entry=entry,
                         reason=f"profile id {entry.profile_id!r} is not in the profiles file")
    if entry.aperture is None:
        # Never substitute maxAperture for a blank aperture: that would
        # write a plausible-looking but invented f-number into EXIF.
        return TagResult(photo, "partial-blank-aperture", profile=profile,
                         aperture=None, log_entry=entry,
                         reason="selection logged no aperture; lens and focal length only")
    return TagResult(photo, "matched", profile=profile,
                     aperture=entry.aperture, log_entry=entry)


def plan_tags(
    photos: list[PhotoRecord],
    entries: list[LogEntry],
    profiles: dict[str, Profile],
    camera_offset: timedelta = timedelta(0),
    max_selection_age: timedelta | None = None,
) -> list[TagResult]:
    return [match_photo(p, entries, profiles, camera_offset, max_selection_age)
            for p in photos]


def read_jpeg_datetime(path: Path) -> datetime | None:
    """Minimal stdlib JPEG EXIF DateTimeOriginal reader (APP1/TIFF).

    Returns None for non-JPEGs, missing EXIF, or malformed structures
    rather than raising: a missing capture time is a skip, not a crash.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) < 4 or data[0:2] != b"\xff\xd8":
        return None
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            return None
        marker = data[pos + 1]
        if marker == 0xD9 or marker == 0xDA:  # EOI / SOS: no more headers
            return None
        length = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        if length < 2 or pos + 2 + length > len(data):
            return None
        segment = data[pos + 4:pos + 2 + length]
        if marker == 0xE1 and segment.startswith(b"Exif\x00\x00"):
            return _tiff_datetime_original(segment[6:])
        pos += 2 + length
    return None


def _tiff_datetime_original(tiff: bytes) -> datetime | None:
    if len(tiff) < 8:
        return None
    endian = {"II": "<", "MM": ">"}.get(tiff[0:2].decode("ascii", "replace"))
    if endian is None or struct.unpack(endian + "H", tiff[2:4])[0] != 42:
        return None
    try:
        ifd0_off = struct.unpack(endian + "I", tiff[4:8])[0]
        exif_off = _ifd_value(tiff, endian, ifd0_off, _EXIF_IFD_POINTER)
        if exif_off is None:
            return None
        raw = _ifd_ascii(tiff, endian, exif_off, _DATETIME_ORIGINAL)
        if raw is None:
            return None
        return parse_timestamp(raw)
    except (struct.error, ValueError, IndexError, OverflowError):
        return None


def _ifd_entries(tiff: bytes, endian: str, offset: int):
    count = struct.unpack(endian + "H", tiff[offset:offset + 2])[0]
    for i in range(count):
        base = offset + 2 + i * 12
        if base + 12 > len(tiff):
            return
        tag, typ, num = struct.unpack(endian + "HHI", tiff[base:base + 8])
        yield tag, typ, num, tiff[base + 8:base + 12]


def _ifd_value(tiff: bytes, endian: str, offset: int, wanted: int) -> int | None:
    for tag, _typ, _num, value_bytes in _ifd_entries(tiff, endian, offset):
        if tag == wanted:
            return struct.unpack(endian + "I", value_bytes)[0]
    return None


def _ifd_ascii(tiff: bytes, endian: str, offset: int, wanted: int) -> str | None:
    for tag, typ, num, value_bytes in _ifd_entries(tiff, endian, offset):
        if tag != wanted or typ != 2:  # 2 = ASCII
            continue
        if num <= 4:
            raw = value_bytes[:num]
        else:
            ptr = struct.unpack(endian + "I", value_bytes)[0]
            raw = tiff[ptr:ptr + num]
        return raw.rstrip(b"\x00").decode("ascii", "replace")
    return None


def load_manifest(path: Path) -> list[PhotoRecord]:
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise TaggerError(f"cannot read manifest {path}: {error}") from error
    photos: list[PhotoRecord] = []
    for item in items:
        raw_path = Path(str(item["path"]))
        photo_path = raw_path if raw_path.is_absolute() else path.parent / raw_path
        capture = item.get("capture_time")
        capture_time = parse_timestamp(capture) if capture else None
        photos.append(PhotoRecord(photo_path, capture_time))
    return photos


def discover_photos(directory: Path) -> list[PhotoRecord]:
    if not directory.is_dir():
        raise TaggerError(f"photos directory not found: {directory}")
    photos: list[PhotoRecord] = []
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        capture = read_jpeg_datetime(path) if path.suffix.lower() in (".jpg", ".jpeg") else None
        photos.append(PhotoRecord(path, capture))
    return photos


def pull_log(adb: str, serial: str | None, dest: Path, run=subprocess.run) -> Path:
    """Pull the worker log over ADB, physical device only (``adb -d``).

    Mirrors the repository's ADB pattern; enabling ADB on the camera is
    the caller's job (factory-USB path), not this function's.
    """
    command = [adb]
    if serial:
        command += ["-s", serial]
    command += ["-d", "pull", LOG_PATH_ON_CAMERA, str(dest)]
    try:
        result = run(command, capture_output=True, text=True, timeout=60)
    except FileNotFoundError as error:
        raise TaggerError(f"adb not found ({adb}); install platform-tools or pass --adb-path") from error
    except subprocess.TimeoutExpired as error:
        raise TaggerError("adb pull timed out") from error
    if result.returncode != 0:
        raise TaggerError(f"adb pull failed: {(result.stderr or result.stdout or '').strip()}")
    if not dest.exists():
        raise TaggerError("adb pull reported success but produced no log file")
    return dest


def exiftool_args(result: TagResult) -> list[str]:
    if result.profile is None:
        return []
    args = [f"-LensModel={result.profile.name}",
            f"-FocalLength={result.profile.focal_length_mm:g}"]
    if result.aperture is not None:
        args.append(f"-FNumber={result.aperture:g}")
    return args


def make_backup(photo: Path, backup_dir: Path | None, stamp: str) -> Path:
    target_dir = backup_dir if backup_dir is not None else photo.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-1", "-2", "-3", "-4", "-5"):
        candidate = target_dir / f"{photo.stem}.x2d-tagger-backup-{stamp}{suffix}{photo.suffix}"
        if not candidate.exists():
            shutil.copy2(photo, candidate)
            return candidate
    raise TaggerError(f"backup already exists for {photo}; refusing to overwrite it")


def apply_result(result: TagResult, exiftool: str, backup_dir: Path | None,
                 stamp: str, run=subprocess.run) -> TagResult:
    """Write one planned tag, restoring from backup on any failure."""
    if result.profile is None:
        return result
    photo = result.photo.path
    if not photo.exists():
        result.status = "apply-failed"
        result.reason = "photo file not found at apply time"
        return result
    try:
        backup = make_backup(photo, backup_dir, stamp)
    except (OSError, TaggerError) as error:
        result.status = "apply-failed"
        result.reason = f"backup failed: {error}"
        return result
    result.backup = backup
    command = [exiftool, "-overwrite_original"] + exiftool_args(result) + [str(photo)]
    result.exiftool_args = command
    try:
        proc = run(command, capture_output=True, text=True, timeout=120)
    except FileNotFoundError as error:
        raise TaggerError(f"exiftool not found ({exiftool}); install it to use --apply") from error
    if proc.returncode != 0:
        try:
            shutil.copy2(backup, photo)
        except OSError:
            pass
        result.status = "apply-failed"
        result.reason = f"exiftool failed: {(proc.stderr or proc.stdout or '').strip()}; restored from backup"
        return result
    result.status = "applied" if result.aperture is not None else "applied-partial"
    return result


def build_report(results: list[TagResult], warnings: list[str], dry_run: bool,
                 camera_offset_seconds: float) -> dict:
    summary: dict[str, int] = {}
    for result in results:
        summary[result.status] = summary.get(result.status, 0) + 1
    return {
        "dry_run": dry_run,
        "camera_offset_seconds": camera_offset_seconds,
        "warnings": warnings,
        "summary": summary,
        "photos": [{
            "path": str(r.photo.path),
            "capture_time": r.photo.capture_time.isoformat() if r.photo.capture_time else None,
            "status": r.status,
            "profile_id": r.profile.id if r.profile else None,
            "lens_name": r.profile.name if r.profile else None,
            "focal_length_mm": r.profile.focal_length_mm if r.profile else None,
            "aperture": r.aperture,
            "log_time": r.log_entry.timestamp.isoformat() if r.log_entry else None,
            "backup": str(r.backup) if r.backup else None,
            "reason": r.reason,
        } for r in results],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tag photos with the lens profile active at capture time.")
    parser.add_argument("--profiles", type=Path, required=True)
    group = parser.add_argument_group("worker log source (exactly one)")
    group.add_argument("--log", type=Path, help="previously pulled worker log file")
    group.add_argument("--pull", action="store_true", help="pull the log from the camera over ADB")
    parser.add_argument("--adb-path", default=DEFAULT_ADB)
    parser.add_argument("--adb-serial", default=None)
    photos = parser.add_argument_group("photos (exactly one)")
    photos.add_argument("--photos", type=Path, help="directory to scan (JPEG capture times read from EXIF)")
    photos.add_argument("--manifest", type=Path, help="JSON list of {path, capture_time} records")
    parser.add_argument("--camera-offset-seconds", type=float, default=0.0,
                        help="seconds added to log timestamps (photo clock ahead of camera clock)")
    parser.add_argument("--max-selection-age-seconds", type=float, default=None,
                        help="optional: selections older than this at capture time are stale-gap skips")
    parser.add_argument("--apply", action="store_true", help="actually write EXIF (default is dry-run)")
    parser.add_argument("--backup-dir", type=Path, default=None)
    parser.add_argument("--exiftool", default="exiftool")
    parser.add_argument("--report", type=Path, default=None, help="write JSON report here")
    args = parser.parse_args(argv)

    if bool(args.log) == bool(args.pull):
        parser.error("pass exactly one of --log or --pull")
    if bool(args.photos) == bool(args.manifest):
        parser.error("pass exactly one of --photos or --manifest")
    try:
        profiles = load_profiles(args.profiles)
        if args.pull:
            import tempfile
            with tempfile.TemporaryDirectory(prefix="x2d-lens-log-") as tmp:
                dest = Path(tmp) / "x2d-lens-profile.log"
                pull_log(args.adb_path, args.adb_serial, dest)
                entries, warnings = load_log(dest)
                return _run(args, profiles, entries, warnings)
        entries, warnings = load_log(args.log)
        return _run(args, profiles, entries, warnings)
    except TaggerError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


def _run(args, profiles, entries, warnings) -> int:
    if args.apply and shutil.which(args.exiftool) is None:
        print(f"ERROR: exiftool not found ({args.exiftool}); refusing --apply without it",
              file=sys.stderr)
        return 1
    photo_list = load_manifest(args.manifest) if args.manifest else discover_photos(args.photos)
    offset = timedelta(seconds=args.camera_offset_seconds)
    max_age = timedelta(seconds=args.max_selection_age_seconds) if args.max_selection_age_seconds is not None else None
    results = plan_tags(photo_list, entries, profiles, offset, max_age)
    if args.apply:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for result in results:
            if result.status in ("matched", "partial-blank-aperture"):
                try:
                    apply_result(result, args.exiftool, args.backup_dir, stamp)
                except TaggerError as error:
                    print(f"ERROR: {error}", file=sys.stderr)
                    return 1
    report = build_report(results, warnings, not args.apply, args.camera_offset_seconds)
    for entry in report["photos"]:
        lens = entry["lens_name"] or "-"
        aperture = f"f/{entry['aperture']:g}" if entry["aperture"] is not None else "aperture unknown"
        print(f"{entry['status']:28s} {entry['path']}  {lens}  {aperture}"
              + (f"  ({entry['reason']})" if entry["reason"] else ""))
    print(f"summary: {json.dumps(report['summary'], sort_keys=True)}"
          f"  warnings: {len(warnings)}  dry_run: {report['dry_run']}")
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if args.report:
        args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
