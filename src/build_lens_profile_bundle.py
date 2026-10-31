"""Build a lens-profile payload bundle offline; no device access.

The one thing this script exists for is baked JSON feeding. The camera
QML must never read a file, so this script:

1. Validates src/X2dLensProfiles.json (format 1, non-empty, unique ids
   in [a-z0-9-], positive focal length / max aperture, crop 1:1 or 65:24).
2. Copies the lens payload into the output directory and writes a
   payload copy of PlayPage.qml in which the source property
   ``lensProfilesJsonText: ""`` is replaced by the baked JSON text as a
   single QML string literal. PlayPage's lens Loader then feeds that
   text into lensController.loadProfiles() on load -- no file read, no
   hand-edited copy, and the baked text is exactly the validated JSON.
3. Generates the English / Traditional Chinese overlays for the two
   lens QML files with the same camera_ui_strings mechanism as
   build_native_preload.py, and writes lens-bundle.json: sha256/bytes
   for every file plus its /system/etc or /system/lib64 target.

Server compilation is deliberately NOT done here: the C server is
compiled by the existing bundle builders (build_speed_bundle.py style:
clang -target aarch64-linux-gnu -fuse-ld=lld -O2 -g0 -fPIC -shared
-nostdlib, soname libx2d_lens_server.so, linked against the payload
libc.so/libdl.so). This script records that exact command in the
manifest and copies lens-profile-server.c into the output so the
bundle builder can compile it; the manifest marks compiled=false
until that step runs.

Usage:
    python3 src/build_lens_profile_bundle.py --output DIR
    python3 src/build_lens_profile_bundle.py --output DIR --src DIR
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
D = Path(__file__).resolve().parent
sys.path.insert(0, str(D))
from camera_ui_strings import english_qml, traditional_qml

FULL_STOPS = {"1.4", "2", "2.8", "4", "5.6", "8", "11", "16", "22", "32"}
PLAYPAGE_MARKER = 'property string lensProfilesJsonText: ""'
SERVER_BUILD_COMMAND = (
    "clang -target aarch64-linux-gnu -fuse-ld=lld -O2 -g0 -fPIC -shared "
    "-nostdlib -Wl,--strip-all -Wl,--no-undefined -Wl,-z,noexecstack "
    "-Wl,-soname,libx2d_lens_server.so src/lens-profile-server.c "
    "libc.so libdl.so -o libx2d_lens_server.so"
)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def validate_profiles(data):
    if not isinstance(data, dict) or data.get("format") != 1:
        raise ValueError("profiles JSON must be an object with format 1")
    profiles = data.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        raise ValueError("profiles list must be non-empty")
    ids = set()
    for profile in profiles:
        pid = profile.get("id")
        if not isinstance(pid, str) or not pid or pid in ids:
            raise ValueError("profile ids must be unique non-empty strings")
        if any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in pid):
            raise ValueError("profile id %r is not path-safe [a-z0-9-]" % pid)
        ids.add(pid)
        if not isinstance(profile.get("name"), str) or not profile["name"]:
            raise ValueError("profile %r needs a non-empty name" % pid)
        for field in ("focalLengthMm", "maxAperture"):
            value = profile.get(field)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                raise ValueError("profile %r needs a positive %s" % (pid, field))
        crop = profile.get("preferredCrop")
        if crop is not None and crop not in ("1:1", "65:24"):
            raise ValueError("profile %r has unsupported preferredCrop %r" % (pid, crop))
    return profiles


def qml_string_literal(text):
    """Encode text as a double-quoted QML string literal (JSON escaping)."""
    return json.dumps(text, ensure_ascii=False)


def build(src, output):
    src = Path(src)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    profiles_path = src / "X2dLensProfiles.json"
    profiles_text = profiles_path.read_text(encoding="utf-8")
    profiles = validate_profiles(json.loads(profiles_text))

    files = []

    def add(source_name, target, data, source_label=None):
        path = output / source_name
        if isinstance(data, str):
            data = data.encode("utf-8")
        path.write_bytes(data)
        files.append(dict(source=source_label or source_name, target=target,
                          bytes=len(data), sha256=sha(data)))
        return path

    # The baked profiles JSON itself, as shipped on camera.
    add("X2dLensProfiles.json", "/system/etc/X2dLensProfiles.json", profiles_text)
    # Worker + server source, with their camera targets.
    add("lens-profile-worker.sh", "/system/etc/x2d-lens-profile/worker",
        (src / "lens-profile-worker.sh").read_text(encoding="utf-8"))
    add("lens-profile-server.c", "/system/lib64/lens-profile-server.c",
        (src / "lens-profile-server.c").read_text(encoding="utf-8"))

    # Lens QML: base plus language overlays, same shape as
    # build_native_preload.py (base target, .en / .zh-Hant siblings).
    for name, base_target in (("X2dLensProfilePage", "/system/etc/X2dLensProfilePage.qml"),
                              ("X2dLensProfileController", "/system/etc/X2dLensProfileController.qml")):
        text = (src / (name + ".qml")).read_text(encoding="utf-8")
        add(name + ".qml", base_target, text)
        english = english_qml(text)
        if english != text:
            add(name + ".en.qml", base_target, english)
        traditional = traditional_qml(text)
        if traditional != text:
            add(name + ".zh-Hant.qml", base_target, traditional)

    # PlayPage payload copy with the baked JSON fed in. This is the only
    # file whose content differs from source for a reason other than
    # language: the empty default is replaced by the baked text, so the
    # Loader's onLoaded handler can call loadProfiles() with it.
    play_text = (src / "PlayPage.qml").read_text(encoding="utf-8")
    if play_text.count(PLAYPAGE_MARKER) != 1:
        raise ValueError("PlayPage.qml must contain exactly one %r" % PLAYPAGE_MARKER)
    baked = play_text.replace(PLAYPAGE_MARKER,
                              "property string lensProfilesJsonText: " + qml_string_literal(profiles_text))
    add("X2dPlayPage.qml", "/system/etc/X2dPlayPage.qml", baked)
    # Overlays for the payload PlayPage copy as well, so the Tweaks row
    # label translates with the rest of the page.
    for suffix, fn in ((".en", english_qml), (".zh-Hant", traditional_qml)):
        overlay = fn(baked)
        if overlay != baked:
            add("X2dPlayPage" + suffix + ".qml", "/system/etc/X2dPlayPage.qml", overlay)

    manifest = dict(
        format=1,
        feature="lens-profile",
        profiles=[p["id"] for p in profiles],
        files=files,
        server=dict(
            source="lens-profile-server.c",
            target="/system/lib64/libx2d_lens_server.so",
            port=18767,
            buildCommand=SERVER_BUILD_COMMAND,
            compiled=False,
        ),
        worker=dict(source="lens-profile-worker.sh",
                    target="/system/etc/x2d-lens-profile/worker"),
        bakedProfiles=dict(source="X2dLensProfiles.json",
                           target="/system/etc/X2dLensProfiles.json",
                           fedInto="PlayPage.lensProfilesJsonText -> "
                                   "X2dLensProfileController.loadProfiles()"),
        deviceValidated=False,
    )
    (output / "lens-bundle.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--src", type=Path, default=D)
    args = parser.parse_args()
    manifest = build(args.src, args.output)
    print(json.dumps(dict(status="PASS", files=len(manifest["files"]),
                          profiles=manifest["profiles"], deviceValidated=False)))
