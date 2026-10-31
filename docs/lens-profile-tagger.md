# Lens-profile desktop tagger (Phase 2, strand A — Tier A)

`src/lens_profile_tagger.py` (stdlib-only Python) matches photos to the
lens profile that was active at capture time, using the worker log from
`src/lens-profile-worker.sh` (`/blackbox/x2d-lens-profile.log`), and
writes LensModel, FocalLength and FNumber into EXIF.

## Usage

```sh
# Dry-run (default): report only, nothing written, no backups made.
python3 src/lens_profile_tagger.py --profiles src/X2dLensProfiles.json \
    --log /tmp/x2d-lens-profile.log --manifest photos.json

# Pull the log from the camera first (ADB must already be enabled via
# the existing factory-USB path; see below), then dry-run a directory.
python3 src/lens_profile_tagger.py --profiles src/X2dLensProfiles.json \
    --pull --photos ~/Pictures/x2d

# Actually write EXIF. Requires exiftool on PATH.
python3 src/lens_profile_tagger.py --profiles src/X2dLensProfiles.json \
    --log /tmp/x2d-lens-profile.log --manifest photos.json --apply \
    --report /tmp/tag-report.json
```

Photo sources (exactly one): `--manifest` is a JSON list of
`{"path": ..., "capture_time": ...}` records (relative paths resolve
against the manifest's directory); `--photos` scans a directory for
`.jpg/.jpeg/.dng/.tif/.tiff/.3fr` files and reads capture times from
JPEG EXIF DateTimeOriginal with a stdlib header parser. Non-JPEG files
and JPEGs without DateTimeOriginal get `skipped-no-capture-time` —
never a guessed time.

## Safety rules (do not relax these casually)

- Dry-run is the default; `--apply` is explicit.
- `--apply` copies each photo to a timestamped
  `*.x2d-tagger-backup-*` file first (same directory, or `--backup-dir`)
  and never overwrites an existing backup — it takes the next numbered
  slot, and fails the file if all slots are taken. A failed EXIF write
  restores the photo from its backup.
- Skips, never guesses: unknown profile id, photo before the first
  selection, missing capture time, and (with `--max-selection-age-seconds`)
  stale selections are all reported and left untouched. A blank aperture
  writes lens + focal length only — the profile's max aperture is never
  substituted for a missing f-number.
- Malformed log lines are warned about and skipped; one corrupt line
  does not discard the shoot.

## Clock drift

Worker timestamps are UTC. `--camera-offset-seconds` is added to every
log timestamp before comparison (positive when the photo clock runs
ahead of the camera clock) and is recorded in the JSON report. There is
intentionally no automatic drift estimation: a guessed offset would
silently retag whole shoots. Naive timestamps are treated as UTC.

## Transport

Log pull is `adb -d pull /blackbox/x2d-lens-profile.log <dest>` — the
repository's existing physical-device ADB pattern
(`src/x2d_play_software.py`). ADB must already be enabled through the
existing factory-USB path (`src/transport/collect_x2d_af_usb.py`,
`x2d_play_software.ensure_adb`); the tagger does not enable ADB, touch
the factory interface, or talk to the camera service. Offline use with
a previously pulled `--log` is the normal path.

## EXIF writing

Writing shells out to `exiftool` (`-overwrite_original`, after the
tagger's own backup is made). `exiftool` is not bundled — same shape as
the bundled `adb`. `--apply` aborts before touching anything if
`exiftool` is not on PATH.

## Open items / not validated

- Direct 3FR tagging is unvalidated: the tagger will attempt it through
  exiftool like any other file, but 3FR behaviour has not been checked
  against a disposable copy on device. Tag exported JPEG/DNG derivatives
  for now; treat any 3FR `--apply` as experimental until validated.
- The Phase 1 CoC constant (0.025 mm, PROVISIONAL) is untouched by this
  tool and still needs confirmation; the tagger does not use it.
- Real-device validation is entirely pending: log pull over real ADB,
  capture-time reading from real X2D JPEGs, and an end-to-end dry-run
  against a real shoot have not been done.
- The 3 EXAMPLE profiles in `src/X2dLensProfiles.json` are placeholders;
  replace them with the owner's own V-system lenses before any personal
  build (see the JSON's own comment).
