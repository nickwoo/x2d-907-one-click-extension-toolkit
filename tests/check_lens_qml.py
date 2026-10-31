"""Lens profile checks: offline JSON/worker/DOF validation plus an optional
Qt controller/page check that skips cleanly when the Qt runtime is absent.

Run from the repository root:  python3 tests/check_lens_qml.py
Exit 0 prints PASS (full or partial) or SKIP (Qt part only); any assertion
failure exits non-zero. This file is a standalone script like
check_brightness_qml.py, not a unittest module.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

D = Path(__file__).resolve().parents[1]
SRC = D / "src"
FAILURES = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + str(detail)) if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def code_text(text, comment_prefix):
    """Strip full-line comments so banned-token checks see code, not prose."""
    return "\n".join(ln for ln in text.splitlines()
                      if not ln.strip().startswith(comment_prefix))


def readf(path):
    return path.read_text() if path.exists() else ""


def approx(a, b, rel=1e-9):
    if a is None or b is None:
        return a is b
    if a == b:
        return True
    return abs(a - b) <= rel * max(abs(a), abs(b))


# ---------------------------------------------------------------- Part A: pure Python (always runs)
profiles_path = SRC / "X2dLensProfiles.json"
profiles_text = profiles_path.read_text(encoding="utf-8")
profiles_data = json.loads(profiles_text)
check("profiles JSON parses with format 1", profiles_data.get("format") == 1)
profs = profiles_data.get("profiles", [])
check("profiles list non-empty", len(profs) >= 1)
ids = [p.get("id") for p in profs]
check("profile ids unique and non-empty", len(ids) == len(set(ids)) and all(isinstance(i, str) and i for i in ids))
check("profile fields valid",
      all(isinstance(p.get("name"), str) and p["name"] and
          isinstance(p.get("focalLengthMm"), (int, float)) and p["focalLengthMm"] > 0 and
          isinstance(p.get("maxAperture"), (int, float)) and p["maxAperture"] > 0 and
          ("preferredCrop" not in p or p["preferredCrop"] in ("1:1", "65:24"))
          for p in profs))
check("example profiles present",
      {"zeiss-planar-80", "zeiss-distagon-40", "zeiss-sonnar-150"} <= set(ids))
check("profiles header marks examples",
      "EXAMPLE" in profiles_data.get("_comment", "") and "NOT the owner" in profiles_data.get("_comment", ""))

controller_text = (SRC / "X2dLensProfileController.qml").read_text(encoding="utf-8")
coc_match = re.search(r"circleOfConfusionMm\s*:\s*([0-9.]+)", controller_text)
check("CoC constant parseable from controller", coc_match is not None)
COC = float(coc_match.group(1)) if coc_match else 0.025
check("CoC in sane range", 0 < COC < 0.1)
check("CoC documented with its basis",
      "0.032" in controller_text and "1730" in controller_text)


def hyper(f, n):
    return f * f / (n * COC) + f


def near(h, f, d):
    return h * d / (h + (d - f))


def far(h, f, d):
    if d >= h:
        return None
    den = h - (d - f)
    if den <= 0:
        return None
    return h * d / den


h1 = hyper(80, 2.8)
check("hyperfocal positive", h1 > 0)
check("hyperfocal shrinks as aperture narrows", hyper(80, 2.8) > hyper(80, 8) > hyper(80, 16))
n1, f1 = near(h1, 80, 3000), far(h1, 80, 3000)
check("near < distance < far", n1 < 3000 < f1)
check("far infinite at/beyond hyperfocal", far(h1, 80, h1 + 1000) is None)
check("far finite inside hyperfocal", far(h1, 80, 3000) is not None)
check("depth positive", f1 - n1 > 0)

worker_text = (SRC / "lens-profile-worker.sh").read_text(encoding="utf-8")
check("worker shebang + set -eu",
      worker_text.startswith("#!/system/bin/sh") and "set -eu" in worker_text)
check("worker has transaction lock", 'mkdir "$D/lock"' in worker_text)
check("worker has marker + log paths",
      "/blackbox/x2d-lens-profile.selected" in worker_text and "/blackbox/x2d-lens-profile.log" in worker_text)
check("worker has status/select/log actions",
      " status)" in worker_text and " log)" in worker_text and " select)" in worker_text)
worker_code = code_text(worker_text, "#")
for banned in ("odindb", "/proc/", ".3fr", ".jpg", ".jpeg", "camera-service"):
    check("worker never touches " + banned, banned not in worker_code)

controller_code = code_text(controller_text, "//")
for needle in ("hyperfocalMm", "nearLimitMm", "farLimitMm", "loadProfiles", "selectProfile",
               "18767", "selectionConfirmed"):
    check("controller contains " + needle, needle in controller_code)
check("controller imports QtQuick only (offline loadable)",
      "com.hasselblad" not in controller_code and "qrc:/app" not in controller_code)

page_text = (SRC / "X2dLensProfilePage.qml").read_text(encoding="utf-8")
page_code = code_text(page_text, "//")
check("page objectName present", '"X2dLensProfilePageRoot"' in page_text)
check("page has backRequested", "backRequested" in page_text)
check("page imports QtQuick only (offline loadable)",
      "com.hasselblad" not in page_code and "qrc:/app" not in page_code)
check("page strings centralized", "strTitle" in page_text and "strHyperfocal" in page_text)
check("page is Simplified Chinese base, not English",
      '"镜头配置文件"' in page_text and '"Lens profile"' not in page_code)
check("page documents i18n mechanism",
      "camera_ui_strings" in page_text and "LABELS" in page_text)

# Scope (Phase 2): the five Phase 1 files must still be present and
# unrenamed; integration edits to existing files are expected now, so
# the Phase 1 "only five untracked files" gate is retired here (the
# integration test suite asserts the exact wiring instead).
for _name in ("src/X2dLensProfiles.json", "src/X2dLensProfileController.qml",
              "src/X2dLensProfilePage.qml", "src/lens-profile-worker.sh",
              "tests/check_lens_qml.py"):
    check("Phase 1 file still present: " + _name, (D / _name).exists())

# Worker functional test in a sandbox (paths rewritten to temp dirs only).
with tempfile.TemporaryDirectory(prefix="lens-worker-") as tmp:
    tmp = Path(tmp)
    blackbox = tmp / "blackbox"
    blackbox.mkdir()
    workd = tmp / "work"
    workd.mkdir()
    sandboxed = worker_text.replace("/blackbox", str(blackbox))
    sandboxed = sandboxed.replace("/system/etc/X2dLensProfiles.json", str(profiles_path))
    sandboxed = sandboxed.replace("/tmp/x2d-lens-profile", str(workd))
    worker = tmp / "lens-profile-worker.sh"
    worker.write_text(sandboxed, encoding="utf-8")

    def run(*args):
        return subprocess.run(["sh", str(worker), *args], capture_output=True, text=True, timeout=30)

    syn = subprocess.run(["sh", "-n", str(worker)], capture_output=True, text=True, timeout=30)
    check("worker passes sh -n syntax check", syn.returncode == 0, syn.stderr)
    marker = blackbox / "x2d-lens-profile.selected"
    logf = blackbox / "x2d-lens-profile.log"
    r = run("status")
    check("worker status with no selection", r.returncode == 0 and '"selectedProfileId":null' in r.stdout, r.stdout + r.stderr)
    r = run("select", "zeiss-planar-80", "2.8")
    check("worker select valid profile", r.returncode == 0 and '"selectedProfileId":"zeiss-planar-80"' in r.stdout, r.stdout + r.stderr)
    check("worker marker content", readf(marker) == "zeiss-planar-80 2.8", readf(marker) or "missing")
    check("worker log line", logf.exists() and re.search(r",zeiss-planar-80,2\.8$", readf(logf).strip()) is not None)
    r = run("status")
    check("worker status after select", r.returncode == 0 and '"aperture":2.8' in r.stdout, r.stdout)
    before = readf(marker)
    r = run("select", "bogus-id")
    check("worker rejects unknown id (39, marker unchanged)", r.returncode == 39 and readf(marker) == before, r.returncode)
    r = run("select", "zeiss-planar-80", "3.5")
    check("worker rejects non-stop aperture (39, marker unchanged)", r.returncode == 39 and readf(marker) == before, r.returncode)
    r = run("select", "zeiss-sonnar-150")
    check("worker select without aperture", r.returncode == 0 and readf(marker) == "zeiss-sonnar-150", r.stdout + r.stderr)
    check("worker log line with empty aperture", readf(logf).strip().endswith(",zeiss-sonnar-150,"), readf(logf))
    r = run("log")
    check("worker log prints lines", r.returncode == 0 and "zeiss-planar-80" in r.stdout, r.stdout)
    r = run("frobnicate")
    check("worker rejects unknown action (35)", r.returncode == 35, r.returncode)
    # Rollback: make the log path a directory so the append fails.
    old_marker = readf(marker)
    logf.unlink()
    logf.mkdir()
    r = run("select", "zeiss-distagon-40", "4")
    check("worker log failure rolls marker back (42)", r.returncode == 42 and readf(marker) == old_marker,
          (r.returncode, readf(marker)))
    logf.rmdir()

if FAILURES:
    print("PART A FAILURES: " + ", ".join(FAILURES))
    sys.exit(1)
print("PART A PASS: profiles, CoC-derived DOF mirror, worker static + functional checks")

# ---------------------------------------------------------------- Part B: Qt (skip cleanly without a usable Qt runtime)
os.environ["QT_QPA_PLATFORM"] = "offscreen"
try:
    from PySide6.QtCore import QObject, QUrl
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlComponent, QQmlEngine
except ImportError as e:
    print("SKIP Qt part: PySide6/Qt runtime unavailable in this environment (%s)" % e)
    print("PASS: lens profile checks (Part A only; Qt part skipped)")
    sys.exit(0)

import json as _json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

posts = []
state = {"selectedProfileId": None, "aperture": None}


class LensFixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/status":
            self._reply(_json.dumps({"ready": True, "message": "ok"}))
        elif self.path == "/log":
            self._reply("2026-01-01T00:00:00Z,zeiss-planar-80,2.8\n", "text/plain")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parts = self.path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "select":
            posts.append(self.path)
            state["selectedProfileId"] = parts[1]
            state["aperture"] = float(parts[2])
            self._reply(_json.dumps({"ready": True, "selectedProfileId": parts[1],
                                     "aperture": float(parts[2]), "message": "ok"}))
        else:
            self.send_response(404)
            self.end_headers()


server = ThreadingHTTPServer(("127.0.0.1", 18767), LensFixture)
threading.Thread(target=server.serve_forever, daemon=True).start()

harness = """
import QtQuick
Item {
  width: 1024; height: 768
  property string jsonText: ""
  property bool loadTrigger: false
  property bool loadResult: false
  property string selectId: ""
  property bool selectTrigger: false
  property bool selectResult: false
  property real apertureValue: 0
  property bool apertureTrigger: false
  property bool apertureResult: false
  property bool masterTrigger: false
  property bool masterValue: false
  property real distanceValue: -1
  property bool distanceTrigger: false
  property bool distanceResult: false
  property bool dofTrigger: false
  property real dofHyper: 0
  property real dofNear: 0
  property real dofFar: 0
  property bool logTrigger: false
  property string readout: ""
  property bool readoutTrigger: false
  // Mirrors of controller state. PySide6 cannot convert a QML-defined
  // object type back to Python, so the test reads these plain-typed
  // harness properties instead of the controller object itself.
  property bool ctrlLoaded: page.lensController.loaded
  property int ctrlProfileCount: page.lensController.profiles.length
  property string ctrlSelected: page.lensController.selectedProfileId
  property real ctrlAperture: page.lensController.aperture
  property string ctrlLogText: page.lensController.logText
  onLoadTriggerChanged: loadResult = page.lensController.loadProfiles(jsonText)
  onSelectTriggerChanged: selectResult = page.lensController.selectProfile(selectId)
  onApertureTriggerChanged: apertureResult = page.lensController.setAperture(apertureValue)
  onMasterTriggerChanged: page.lensController.setMaster(masterValue)
  onDistanceTriggerChanged: distanceResult = page.lensController.setDistanceM(distanceValue)
  onDofTriggerChanged: {
    dofHyper = page.lensController.hyperfocalMm()
    dofNear = page.lensController.nearLimitMm()
    dofFar = page.lensController.farLimitMm()
  }
  onLogTriggerChanged: page.lensController.requestLog()
  onReadoutTriggerChanged: readout = page.readoutText()
  X2dLensProfilePage { id: page; showing: true }
}
"""
app = QGuiApplication([])
engine = QQmlEngine()
component = QQmlComponent(engine)
component.setData(harness.encode(), QUrl.fromLocalFile(str(SRC / "LensCheckHarness.qml")))
root = component.create()
assert root is not None, [str(e) for e in component.errors()]
page = root.findChild(QObject, "X2dLensProfilePageRoot")
assert page is not None, "lens page not found"


def wait(test, what):
    end = time.monotonic() + 8
    while time.monotonic() < end:
        app.processEvents()
        if test():
            return
        time.sleep(0.01)
    raise AssertionError("Qt wait timed out: " + what)


def flip(prop):
    root.setProperty(prop, not root.property(prop))


def qcheck(name, cond, detail=""):
    check("Qt " + name, cond, detail)


# Load profiles through the real QML parser/validator.
root.setProperty("jsonText", profiles_text)
flip("loadTrigger")
wait(lambda: root.property("loadResult") is True, "loadProfiles")
qcheck("profiles load", root.property("ctrlLoaded") is True)
qcheck("profiles count", root.property("ctrlProfileCount") == 3, root.property("ctrlProfileCount"))
qcheck("first profile selected", root.property("ctrlSelected") == "zeiss-planar-80")
qcheck("aperture defaults to profile max", root.property("ctrlAperture") == 2.8)

# DOF values from the real QML functions vs the Python mirror.
root.setProperty("distanceValue", 3.0)
flip("distanceTrigger")
flip("dofTrigger")
wait(lambda: root.property("dofHyper") > 0, "dof compute")
qh = hyper(80, 2.8)
qcheck("QML hyperfocal matches mirror", approx(root.property("dofHyper"), qh), (root.property("dofHyper"), qh))
qcheck("QML near matches mirror", approx(root.property("dofNear"), near(qh, 80, 3000)), root.property("dofNear"))
qcheck("QML far matches mirror", approx(root.property("dofFar"), far(qh, 80, 3000)), root.property("dofFar"))
root.setProperty("distanceValue", -1)
flip("distanceTrigger")
flip("dofTrigger")
wait(lambda: root.property("dofFar") == -1, "hyperfocal focus far infinite")
qcheck("hyperfocal focus near is H/2", approx(root.property("dofNear"), qh / 2), root.property("dofNear"))

# Master-off gates writes: no POST recorded, selection unchanged.
root.setProperty("masterValue", False)
flip("masterTrigger")
root.setProperty("selectId", "zeiss-sonnar-150")
posts.clear()
flip("selectTrigger")
app.processEvents()
time.sleep(0.3)
app.processEvents()
qcheck("master-off select returns false", root.property("selectResult") is False)
qcheck("master-off select sends nothing", posts == [], posts)
qcheck("master-off selection unchanged", root.property("ctrlSelected") == "zeiss-planar-80")

# Master-on: frozen arguments arrive exactly, confirmation applies.
# Note: the request clamps the aperture to the TARGET profile's max, so
# selecting the Sonnar (max f/4) while f/2.8 is set posts /4, not /2.8.
root.setProperty("masterValue", True)
flip("masterTrigger")
posts.clear()
flip("selectTrigger")
wait(lambda: root.property("ctrlSelected") == "zeiss-sonnar-150", "select confirmation")
qcheck("master-on select returns true", root.property("selectResult") is True)
qcheck("select POST path is frozen args", posts == ["/select/zeiss-sonnar-150/4"], posts)

# Aperture change re-selects with the new aperture.
root.setProperty("apertureValue", 8.0)
posts.clear()
flip("apertureTrigger")
wait(lambda: root.property("ctrlAperture") == 8.0, "aperture apply")
wait(lambda: posts == ["/select/zeiss-sonnar-150/8"], "aperture POST")
qcheck("aperture POST carries new value", posts == ["/select/zeiss-sonnar-150/8"], posts)

# Malformed JSON is rejected and prior good state is kept.
root.setProperty("jsonText", "{bad json")
flip("loadTrigger")
app.processEvents()
time.sleep(0.2)
app.processEvents()
qcheck("malformed JSON rejected", root.property("loadResult") is False)
qcheck("malformed JSON keeps prior profiles", root.property("ctrlProfileCount") == 3)
qcheck("malformed JSON keeps selection", root.property("ctrlSelected") == "zeiss-sonnar-150")

# Log action returns raw lines to the controller.
flip("logTrigger")
wait(lambda: "zeiss-planar-80" in (root.property("ctrlLogText") or ""), "log text")
qcheck("log text received", "zeiss-planar-80" in root.property("ctrlLogText"))

# Page readout renders the DOF block.
flip("readoutTrigger")
wait(lambda: "超焦距" in (root.property("readout") or ""), "readout")
qcheck("page readout renders", "超焦距" in root.property("readout"))

root.deleteLater()
app.processEvents()
server.shutdown()
server.server_close()
if FAILURES:
    print("PART B FAILURES: " + ", ".join(FAILURES))
    sys.exit(1)
print("PASS: lens profile checks (Part A + Qt controller/page checks)")
