"""Phase 2 integration checks: Tweaks row, i18n overlays, server shim,
and baked-JSON build step for the lens profile feature.

Run from the repository root:
    PYTHONPATH=src python3 -m unittest tests.test_lens_integration -v
or directly:  python3 tests/test_lens_integration.py
The server functional test compiles lens-profile-server.c with the host
compiler and a sandboxed worker; it skips cleanly when no compiler is
available or port 18767 is already occupied.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

D = Path(__file__).resolve().parents[1]
SRC = D / "src"
sys.path.insert(0, str(SRC))
from camera_ui_strings import LABELS, english_qml, traditional_qml  # noqa: E402

PAGE = (SRC / "X2dLensProfilePage.qml").read_text(encoding="utf-8")
PLAY = (SRC / "PlayPage.qml").read_text(encoding="utf-8")
SERVER = (SRC / "lens-profile-server.c").read_text(encoding="utf-8")
CONTROLLER = (SRC / "X2dLensProfileController.qml").read_text(encoding="utf-8")

LENS_LABELS = [
    ("镜头配置文件", "Lens profile", "鏡頭配置文件"),
    ("返回", "Back", "返回"),
    ("主开关", "Master", "主開關"),
    ("开", "On", "開"),
    ("关", "Off", "關"),
    ("光圈", "Aperture", "光圈"),
    ("对焦距离", "Focus distance", "對焦距離"),
    ("超焦距", "Hyperfocal", "超焦距"),
    ("近点", "Near", "近點"),
    ("远点", "Far", "遠點"),
    ("景深", "Depth", "景深"),
    ("取景框线", "Frame lines", "取景框線"),
    ("未选择镜头配置文件", "No lens profile selected", "未選擇鏡頭配置文件"),
    ("状态", "Status", "狀態"),
]


class TweaksRowTests(unittest.TestCase):
    def test_row_lives_in_playpage_settings_column(self):
        # The Tweaks page IS PlayPage.qml: its row list is settingsColumn,
        # not PlayMenuRoute/Bootstrap (those only route the Tweaks entry
        # itself, x2dPlayUi, into ResidentPlayHost -> PlayPage).
        self.assertIn('objectName: "X2dPlayLensProfileRow"', PLAY)
        self.assertIn('text: "镜头配置文件"', PLAY)
        self.assertLess(PLAY.index("X2dPlayAutoBrightnessSwitchRow"),
                        PLAY.index("X2dPlayLensProfileRow"))
        self.assertLess(PLAY.index("X2dPlayLensProfileRow"),
                        PLAY.index("settingsList.height * 0.35"))

    def test_loader_opens_lens_page_and_feeds_baked_json(self):
        self.assertIn('id: lensLoader', PLAY)
        self.assertIn('objectName: "X2dLensProfileLoader"', PLAY)
        self.assertIn("active: root.lensProfileOpen", PLAY)
        self.assertIn("X2dLensProfilePage {", PLAY)
        self.assertIn("onBackRequested: root.lensProfileOpen = false", PLAY)
        self.assertIn('property string lensProfilesJsonText: ""', PLAY)
        self.assertIn("item.lensController.loadProfiles(root.lensProfilesJsonText)", PLAY)
        self.assertIn("function requestLensProfile()", PLAY)

    def test_back_closes_lens_page_before_tweaks_page(self):
        back = PLAY[PLAY.index("function requestBack()"):PLAY.index("function requestLensProfile()")]
        self.assertIn("lensProfileOpen", back)
        self.assertLess(back.index("lensProfileOpen"), back.index("backRequested()"))

    def test_qml_performs_no_direct_file_read(self):
        # Strip full-line // comments: the path is named in prose there,
        # but no code line may read it.
        def code(text):
            return "\n".join(ln for ln in text.splitlines()
                              if not ln.strip().startswith("//"))
        for text in (PAGE, CONTROLLER, PLAY):
            self.assertNotIn("Qt.readFile", text)
            self.assertNotIn("file:///system/etc/X2dLensProfiles.json", text)
        self.assertNotIn("/system/etc/X2dLensProfiles.json", code(PAGE))
        self.assertNotIn("/system/etc/X2dLensProfiles.json", code(CONTROLLER))


class I18nTests(unittest.TestCase):
    def test_labels_cover_every_page_string(self):
        labels = dict((zh, en) for zh, en in LABELS)
        for chinese, english, _ in LENS_LABELS:
            self.assertEqual(labels.get(chinese), english, chinese)

    def test_english_overlay_restores_phase1_strings(self):
        english = english_qml(PAGE)
        for chinese, expected, _ in LENS_LABELS:
            self.assertNotIn('"%s"' % chinese, english, chinese)
            self.assertIn('"%s"' % expected, english, expected)

    def test_traditional_overlay_and_quote_preservation(self):
        traditional = traditional_qml(PAGE)
        for chinese, _, expected in LENS_LABELS:
            self.assertIn('"%s"' % expected, traditional, chinese)
        self.assertEqual(PAGE.count('"'), traditional.count('"'))
        self.assertEqual(PAGE.count('"'), english_qml(PAGE).count('"'))

    def test_catalogs_carry_the_new_strings(self):
        en = json.loads((SRC / "translations.json").read_text(encoding="utf-8"))
        hant = json.loads((SRC / "translations_zh_hant.json").read_text(encoding="utf-8"))
        for chinese, english, traditional in LENS_LABELS:
            self.assertEqual(en.get(chinese), english, chinese)
            self.assertEqual(hant.get(chinese), traditional, chinese)


class ServerTests(unittest.TestCase):
    def test_server_shape_mirrors_speed_server(self):
        self.assertIn("18767", SERVER)
        self.assertIn("0x4f49", SERVER)  # 18767 in network byte order
        self.assertIn("0x0100007f", SERVER)  # 127.0.0.1
        self.assertIn('getenv("X2D_LENS_SERVER")', SERVER)
        self.assertIn("GET /status HTTP/1.", SERVER)
        self.assertIn("GET /log HTTP/1.", SERVER)
        self.assertIn("POST /select/", SERVER)
        self.assertIn("/system/etc/x2d-lens-profile/worker", SERVER)
        self.assertIn("127.0.0.1:18767", CONTROLLER)

    def test_server_validates_id_and_aperture(self):
        self.assertIn("valid_id", SERVER)
        self.assertIn("valid_aperture", SERVER)
        for stop in ('"1.4"', '"2.8"', '"11"', '"32"'):
            self.assertIn(stop, SERVER)

    def test_server_functional(self):
        compiler = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
        if not compiler:
            self.skipTest("no host C compiler")
        probe = socket.socket()
        try:
            if probe.connect_ex(("127.0.0.1", 18767)) == 0:
                self.skipTest("port 18767 already occupied")
        finally:
            probe.close()
        with tempfile.TemporaryDirectory(prefix="lens-server-") as tmp:
            tmp = Path(tmp)
            blackbox = tmp / "blackbox"
            blackbox.mkdir()
            worker_text = (SRC / "lens-profile-worker.sh").read_text(encoding="utf-8")
            worker_text = worker_text.replace("/blackbox", str(blackbox))
            worker_text = worker_text.replace(
                "/system/etc/X2dLensProfiles.json", str(SRC / "X2dLensProfiles.json"))
            worker_text = worker_text.replace("/tmp/x2d-lens-profile", str(tmp / "work"))
            worker = tmp / "worker.sh"
            worker.write_text(worker_text, encoding="utf-8")
            lib = tmp / "liblens.so"
            compile_cmd = [compiler, "-fPIC", "-shared",
                           "-DX2D_LENS_WORKER=" + json.dumps(str(worker)),
                           "-DX2D_LENS_SH=" + json.dumps("/bin/sh"),
                           str(SRC / "lens-profile-server.c"), "-o", str(lib)]
            result = subprocess.run(compile_cmd, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            env = dict(os.environ, X2D_LENS_SERVER="1", LD_PRELOAD=str(lib))
            proc = subprocess.Popen(["sleep", "30"], env=env)
            try:
                time.sleep(0.5)

                def req(method, path):
                    request = urllib.request.Request(
                        "http://127.0.0.1:18767" + path, method=method)
                    with urllib.request.urlopen(request, timeout=5) as response:
                        return response.status, response.headers.get_content_type(), \
                            response.read().decode()

                status, ctype, body = req("GET", "/status")
                self.assertEqual((status, ctype), (200, "application/json"))
                self.assertIn('"selectedProfileId":null', body)
                status, _, body = req("POST", "/select/zeiss-planar-80/2.8")
                self.assertIn('"selectedProfileId":"zeiss-planar-80"', body)
                self.assertIn('"aperture":2.8', body)
                _, _, body = req("GET", "/status")
                self.assertIn('"selectedProfileId":"zeiss-planar-80"', body)
                _, ctype, body = req("GET", "/log")
                self.assertEqual(ctype, "text/plain")
                self.assertIn("zeiss-planar-80,2.8", body)
                for bad in ("/select/BAD_ID/2.8", "/select/zeiss-planar-80/3.5"):
                    _, _, body = req("POST", bad)
                    self.assertIn('"ready":false', body)
                _, _, body = req("GET", "/frobnicate")
                self.assertIn('"ready":false', body)
            finally:
                proc.kill()
                proc.wait()


class BuildTests(unittest.TestCase):
    def test_build_bakes_json_into_playpage(self):
        with tempfile.TemporaryDirectory(prefix="lens-bundle-") as tmp:
            result = subprocess.run(
                [sys.executable, str(SRC / "build_lens_profile_bundle.py"),
                 "--output", tmp], capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads((Path(tmp) / "lens-bundle.json").read_text())
            self.assertEqual(manifest["profiles"],
                             ["zeiss-planar-80", "zeiss-distagon-40", "zeiss-sonnar-150"])
            self.assertEqual(manifest["server"]["port"], 18767)
            self.assertFalse(manifest["deviceValidated"])
            targets = {f["target"] for f in manifest["files"]}
            self.assertIn("/system/etc/X2dLensProfiles.json", targets)
            self.assertIn("/system/etc/X2dPlayPage.qml", targets)
            for entry in manifest["files"]:
                data = (Path(tmp) / entry["source"]).read_bytes()
                import hashlib
                self.assertEqual(entry["bytes"], len(data))
                self.assertEqual(entry["sha256"], hashlib.sha256(data).hexdigest())
            baked = (Path(tmp) / "X2dPlayPage.qml").read_text(encoding="utf-8")
            self.assertIn('property string lensProfilesJsonText: "{', baked)
            self.assertIn("zeiss-planar-80", baked)
            self.assertNotIn("Qt.readFile", baked)
            english = (Path(tmp) / "X2dPlayPage.en.qml").read_text(encoding="utf-8")
            self.assertIn('"Lens profile"', english)
            traditional = (Path(tmp) / "X2dPlayPage.zh-Hant.qml").read_text(encoding="utf-8")
            self.assertIn('"鏡頭配置文件"', traditional)

    def test_build_rejects_bad_profiles(self):
        sys.path.insert(0, str(SRC))
        from build_lens_profile_bundle import validate_profiles
        base = {"format": 1, "profiles": [{"id": "a-80", "name": "A", "focalLengthMm": 80,
                                           "maxAperture": 2.8}]}
        self.assertEqual([p["id"] for p in validate_profiles(base)], ["a-80"])
        for bad in ({"format": 2, "profiles": base["profiles"]},
                    {"format": 1, "profiles": []},
                    {"format": 1, "profiles": [dict(base["profiles"][0], id="BAD") ]},
                    {"format": 1, "profiles": [dict(base["profiles"][0], preferredCrop="2:1")]},
                    {"format": 1, "profiles": [base["profiles"][0], base["profiles"][0]]}):
            with self.assertRaises(ValueError):
                validate_profiles(bad)


if __name__ == "__main__":
    unittest.main()
