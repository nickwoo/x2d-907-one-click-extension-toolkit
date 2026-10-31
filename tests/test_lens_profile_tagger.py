"""Tier A desktop tagger tests (Phase 2, strand A). Offline only: no
camera, no adb binary, no exiftool binary is ever invoked for real --
the apply/pull runners are fakes. Run from the repository root:
  python3 -m unittest tests.test_lens_profile_tagger -v
or:  python3 tests/test_lens_profile_tagger.py
"""
import json
import struct
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

D = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(D / "src"))

import lens_profile_tagger as tagger

FIX = D / "tests" / "fixtures" / "lens_tagger"
PROFILES = D / "src" / "X2dLensProfiles.json"


def utc(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def photo(name, time_text):
    return tagger.PhotoRecord(Path(name), utc(time_text) if time_text else None)


def make_jpeg_with_datetime(path, exif_time):
    """Minimal JPEG: SOI, APP1 Exif TIFF, EOI. No image data needed for
    the tagger's header-only capture-time reader."""
    value = exif_time.encode("ascii") + b"\x00"
    # TIFF layout (little-endian): header 8, IFD0 18 (count+1 entry+next),
    # ExifIFD 18, string at the end.
    tiff_header_len = 8
    ifd0_len = 2 + 12 + 4
    exif_ifd_off = tiff_header_len + ifd0_len
    string_off = exif_ifd_off + ifd0_len
    tiff = bytearray()
    tiff += b"II" + struct.pack("<H", 42) + struct.pack("<I", tiff_header_len)
    tiff += struct.pack("<H", 1)
    tiff += struct.pack("<HHI", 0x8769, 4, 1) + struct.pack("<I", exif_ifd_off)
    tiff += struct.pack("<I", 0)
    tiff += struct.pack("<H", 1)
    tiff += struct.pack("<HHI", 0x9003, 2, len(value)) + struct.pack("<I", string_off)
    tiff += struct.pack("<I", 0)
    tiff += value
    app1 = b"Exif\x00\x00" + bytes(tiff)
    jpeg = b"\xff\xd8" + b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1 + b"\xff\xd9"
    path.write_bytes(jpeg)


class FakeRun:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.calls = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        return SimpleNamespace(returncode=self.returncode, stdout=self.stdout, stderr=self.stderr)


class TaggerTests(unittest.TestCase):
    def setUp(self):
        self.profiles = tagger.load_profiles(PROFILES)
        self.entries, self.warnings = tagger.load_log(FIX / "sample_log.txt")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def match(self, time_text, **kwargs):
        return tagger.match_photo(photo("x.jpg", time_text), self.entries, self.profiles, **kwargs)

    def test_log_parsing_keeps_good_entries_and_warns_on_garbage(self):
        self.assertEqual(len(self.entries), 5)  # garbage line dropped
        self.assertEqual(len(self.warnings), 1)
        self.assertEqual(self.entries[0].profile_id, "zeiss-planar-80")
        self.assertIsNone(self.entries[1].aperture)  # blank aperture preserved

    def test_basic_match_is_latest_selection_at_or_before_photo(self):
        result = self.match("2026-10-27T08:15:00Z")
        self.assertEqual(result.status, "matched")
        self.assertEqual(result.profile.name, "Planar 80/2.8")
        self.assertEqual(result.profile.focal_length_mm, 80)
        self.assertEqual(result.aperture, 2.8)

    def test_photo_before_first_selection_is_skipped(self):
        result = self.match("2026-10-27T07:00:00Z")
        self.assertEqual(result.status, "skipped-before-first-selection")
        self.assertIsNone(result.profile)

    def test_blank_aperture_is_partial_never_max_aperture(self):
        result = self.match("2026-10-27T10:00:00Z")
        self.assertEqual(result.status, "partial-blank-aperture")
        self.assertEqual(result.profile.name, "Distagon 40/4")
        self.assertIsNone(result.aperture)  # NOT maxAperture 4.0 substituted

    def test_unknown_profile_id_is_skipped_not_guessed(self):
        result = self.match("2026-10-27T12:30:00Z")
        self.assertEqual(result.status, "skipped-unknown-profile")
        self.assertIsNone(result.profile)

    def test_no_capture_time_is_skipped(self):
        result = tagger.match_photo(photo("x.jpg", None), self.entries, self.profiles)
        self.assertEqual(result.status, "skipped-no-capture-time")

    def test_empty_log_is_skipped(self):
        result = tagger.match_photo(photo("x.jpg", "2026-10-27T08:15:00Z"), [], self.profiles)
        self.assertEqual(result.status, "skipped-empty-log")

    def test_clock_drift_offset_shifts_the_boundary(self):
        # Offset is added to log timestamps to put them on photo-clock
        # time (positive = photo clock ahead of camera clock). Photo at
        # 09:40, Distagon logged 09:30: with a +30 minute offset the log
        # entry lands at 10:00 in photo time, after the photo, so the
        # still-active Planar selection applies instead.
        plain = self.match("2026-10-27T09:40:00Z")
        self.assertEqual(plain.profile.name, "Distagon 40/4")
        shifted = self.match("2026-10-27T09:40:00Z",
                             camera_offset=timedelta(minutes=30))
        self.assertEqual(shifted.profile.name, "Planar 80/2.8")
        # And a -60 minute offset pulls a later selection into range:
        # Sonnar logged 11:00 becomes 10:00, before a 10:30 photo.
        later = self.match("2026-10-27T10:30:00Z",
                           camera_offset=timedelta(minutes=-60))
        self.assertEqual(later.profile.name, "Sonnar 150/4")

    def test_selection_persists_across_a_long_gap_by_default(self):
        # Last real selection is 13:00 Planar; a photo days later still
        # matches it unless a max age is set (worker has no deselect).
        result = self.match("2026-10-30T13:00:00Z")
        self.assertEqual(result.status, "matched")
        self.assertEqual(result.profile.name, "Planar 80/2.8")

    def test_stale_gap_flag_expires_old_selections(self):
        result = self.match("2026-10-30T13:00:00Z",
                            max_selection_age=timedelta(hours=24))
        self.assertEqual(result.status, "skipped-stale-gap")
        result = self.match("2026-10-27T13:30:00Z",
                            max_selection_age=timedelta(hours=24))
        self.assertEqual(result.status, "matched")

    def test_fixture_manifest_plans_every_case(self):
        photos = tagger.load_manifest(FIX / "sample_manifest.json")
        results = tagger.plan_tags(photos, self.entries, self.profiles)
        by_name = {r.photo.path.name: r.status for r in results}
        self.assertEqual(by_name, {
            "before-first.jpg": "skipped-before-first-selection",
            "planar.jpg": "matched",
            "distagon-blank-aperture.jpg": "partial-blank-aperture",
            "sonnar.jpg": "matched",
            "unknown.jpg": "skipped-unknown-profile",
            "no-capture-time.jpg": "skipped-no-capture-time",
        })

    def test_jpeg_datetime_reader(self):
        jpeg = self.root / "shot.jpg"
        make_jpeg_with_datetime(jpeg, "2026:10:27 08:15:00")
        self.assertEqual(tagger.read_jpeg_datetime(jpeg), utc("2026-10-27T08:15:00Z"))
        plain = self.root / "plain.jpg"
        plain.write_bytes(b"\xff\xd8\xff\xd9")
        self.assertIsNone(tagger.read_jpeg_datetime(plain))

    def test_dry_run_writes_nothing_and_makes_no_backup(self):
        shot = self.root / "shot.jpg"
        shot.write_bytes(b"original-bytes")
        result = tagger.match_photo(tagger.PhotoRecord(shot, utc("2026-10-27T08:15:00Z")),
                                    self.entries, self.profiles)
        # plan_tags is the dry-run path: it must not touch the file.
        self.assertEqual(result.status, "matched")
        self.assertEqual(shot.read_bytes(), b"original-bytes")
        self.assertEqual(list(self.root.iterdir()), [shot])

    def test_apply_backs_up_then_calls_exiftool_without_fnumber_when_blank(self):
        shot = self.root / "shot.jpg"
        shot.write_bytes(b"original-bytes")
        fake = FakeRun()
        blank = tagger.match_photo(tagger.PhotoRecord(shot, utc("2026-10-27T10:00:00Z")),
                                   self.entries, self.profiles)
        out = tagger.apply_result(blank, "exiftool", None, "20261027T000000Z", run=fake)
        self.assertEqual(out.status, "applied-partial")
        self.assertIsNotNone(out.backup)
        self.assertEqual(out.backup.read_bytes(), b"original-bytes")
        command = fake.calls[0]
        self.assertIn("-LensModel=Distagon 40/4", command)
        self.assertIn("-FocalLength=40", command)
        self.assertFalse(any(a.startswith("-FNumber=") for a in command))

    def test_apply_writes_fnumber_when_known_and_restores_on_failure(self):
        shot = self.root / "shot.jpg"
        shot.write_bytes(b"original-bytes")
        matched = tagger.match_photo(tagger.PhotoRecord(shot, utc("2026-10-27T08:15:00Z")),
                                     self.entries, self.profiles)
        fake = FakeRun()
        out = tagger.apply_result(matched, "exiftool", None, "20261027T000000Z", run=fake)
        self.assertEqual(out.status, "applied")
        self.assertIn("-FNumber=2.8", fake.calls[0])

        # Simulate a writer that corrupts the file and then fails: the
        # backup (taken before the write) must be restored over it.
        def corrupting_run(command, **kwargs):
            shot.write_bytes(b"corrupted-by-failed-write")
            return SimpleNamespace(returncode=1, stdout="", stderr="exiftool: bad file")
        matched2 = tagger.match_photo(tagger.PhotoRecord(shot, utc("2026-10-27T08:15:00Z")),
                                      self.entries, self.profiles)
        out2 = tagger.apply_result(matched2, "exiftool", None, "20261027T000001Z", run=corrupting_run)
        self.assertEqual(out2.status, "apply-failed")
        self.assertEqual(shot.read_bytes(), b"original-bytes")  # restored from backup

    def test_apply_never_overwrites_an_existing_backup(self):
        shot = self.root / "shot.jpg"
        shot.write_bytes(b"original-bytes")
        (self.root / "shot.x2d-tagger-backup-20261027T000000Z.jpg").write_bytes(b"older-backup")
        matched = tagger.match_photo(tagger.PhotoRecord(shot, utc("2026-10-27T08:15:00Z")),
                                     self.entries, self.profiles)
        fake = FakeRun()
        out = tagger.apply_result(matched, "exiftool", None, "20261027T000000Z", run=fake)
        # First candidate collides, so it takes the -1 slot rather than overwriting.
        self.assertEqual(out.status, "applied")
        self.assertTrue(out.backup.name.endswith("-1.jpg"))
        self.assertEqual((self.root / "shot.x2d-tagger-backup-20261027T000000Z.jpg").read_bytes(),
                         b"older-backup")

    def test_pull_uses_physical_adb_pull_of_the_fixed_log_path(self):
        fake = FakeRun()
        dest = self.root / "pulled.log"
        dest.write_text("2026-10-27T08:00:00Z,zeiss-planar-80,2.8\n")
        tagger.pull_log("adb", None, dest, run=fake)
        self.assertEqual(fake.calls[0],
                         ["adb", "-d", "pull", "/blackbox/x2d-lens-profile.log", str(dest)])
        fake_fail = FakeRun(returncode=1, stderr="device offline")
        with self.assertRaises(tagger.TaggerError):
            tagger.pull_log("adb", None, dest, run=fake_fail)

    def test_cli_dry_run_end_to_end(self):
        report = self.root / "report.json"
        proc = subprocess.run(
            [sys.executable, str(D / "src" / "lens_profile_tagger.py"),
             "--profiles", str(PROFILES), "--log", str(FIX / "sample_log.txt"),
             "--manifest", str(FIX / "sample_manifest.json"), "--report", str(report)],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(report.read_text())
        self.assertTrue(data["dry_run"])
        self.assertEqual(data["summary"]["matched"], 2)
        self.assertEqual(data["summary"]["partial-blank-aperture"], 1)
        self.assertEqual(len(data["warnings"]), 1)


if __name__ == "__main__":
    unittest.main()
