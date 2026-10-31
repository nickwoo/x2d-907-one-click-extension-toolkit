import QtQuick
// Lens profile controller (Phase 1, source only). Mirrors SpeedBuffController:
// QtQuick only (no camera-side imports, so it loads offline), XHR to a
// loopback worker server, in-flight serialization, 15 s request deadline,
// 2.5 s status poll, fail-closed on backend error. The server shim that binds
// the shell worker to this port follows speed-buff-server.c at build time
// and is NOT part of this change set; the proposed base is
// http://127.0.0.1:18767/ with actions status (GET), select (POST,
// path-encoded args: /select/<id>/<aperture>), log (GET). Path-encoded args
// were chosen because the speed-buff server maps action-only request lines to
// worker argv with no body parsing; the id charset ([a-z0-9-]) and the
// full-stop aperture set make the path URL-safe by construction.
QtObject {
    id: root
    property var profiles: []
    property string selectedProfileId: ""
    property real aperture: 0
    property real focusDistanceM: 3
    property bool frameLinesEnabled: false
    property bool loaded: false
    property bool ready: false
    property bool faulted: false
    property bool busy: false
    property bool inFlight: false
    property bool master: false
    property string statusMessage: "Reading lens profile state"
    property string logText: ""
    property var currentRequest: null
    property string pendingSelectId: ""
    property bool initialSyncDone: false
    signal selectionConfirmed()
    // PROVISIONAL circle of confusion, 0.025 mm. This MUST be confirmed
    // against Hasselblad's 44x33 convention before any device use; until
    // then every DOF number this controller produces is an estimate.
    // Kept as one named constant so the decision lands in exactly one place.
    readonly property real circleOfConfusionMm: 0.025
    readonly property var fullStops: [1.4, 2, 2.8, 4, 5.6, 8, 11, 16, 22, 32]
    readonly property var distancePresets: [0.5, 1, 2, 3, 5, 10]
    readonly property var selectedProfile: profileById(selectedProfileId)
    function profileById(id) {
        for (var i = 0; i < profiles.length; i++)
            if (profiles[i].id === id) return profiles[i]
        return null
    }
    function refreshSelection() {
        if (selectedProfile !== null &&
            (fullStops.indexOf(aperture) < 0 || aperture < selectedProfile.maxAperture))
            aperture = selectedProfile.maxAperture
    }
    function effectiveAperture() {
        if (selectedProfile === null) return 0
        if (fullStops.indexOf(aperture) >= 0 && aperture >= selectedProfile.maxAperture)
            return aperture
        return selectedProfile.maxAperture
    }
    function loadProfiles(text) {
        var data
        try { data = JSON.parse(text) } catch (e) { return false }
        if (data.format !== 1 || !Array.isArray(data.profiles) || data.profiles.length === 0)
            return false
        for (var i = 0; i < data.profiles.length; i++) {
            var p = data.profiles[i]
            if (typeof p.id !== "string" || p.id === "" ||
                typeof p.name !== "string" || p.name === "" ||
                typeof p.focalLengthMm !== "number" || !(p.focalLengthMm > 0) ||
                typeof p.maxAperture !== "number" || !(p.maxAperture > 0))
                return false
            if (p.preferredCrop !== undefined && p.preferredCrop !== "1:1" && p.preferredCrop !== "65:24")
                return false
        }
        profiles = data.profiles
        loaded = true
        if (profileById(selectedProfileId) === null) {
            selectedProfileId = profiles[0].id
            aperture = profiles[0].maxAperture
        }
        refreshSelection()
        return true
    }
    // DOF math, all lengths mm. Hyperfocal H = f^2/(N*c) + f;
    // near Dn = H*d/(H + (d - f)); far Df = H*d/(H - (d - f)).
    // farLimitMm returns -1 for infinity (d >= H, or far denominator <= 0).
    // focusDistanceM === -1 means "focused at hyperfocal": near = H/2, far = infinity.
    function hyperfocalMm() {
        if (selectedProfile === null) return -1
        var f = selectedProfile.focalLengthMm
        var n = effectiveAperture()
        if (!(n > 0)) return -1
        return (f * f) / (n * circleOfConfusionMm) + f
    }
    function nearLimitMm() {
        var h = hyperfocalMm()
        if (h <= 0 || selectedProfile === null) return -1
        if (focusDistanceM === -1) return h / 2
        var d = focusDistanceM * 1000
        var f = selectedProfile.focalLengthMm
        return (h * d) / (h + (d - f))
    }
    function farLimitMm() {
        var h = hyperfocalMm()
        if (h <= 0 || selectedProfile === null) return -1
        if (focusDistanceM === -1) return -1
        var d = focusDistanceM * 1000
        var f = selectedProfile.focalLengthMm
        if (d >= h) return -1
        var den = h - (d - f)
        if (den <= 0) return -1
        return (h * d) / den
    }
    function formatMm(v) {
        if (v < 0) return "\u221e"
        if (v >= 1000) return (v / 1000).toFixed(2) + " m"
        return Math.round(v) + " mm"
    }
    function takeOverInFlight() {
        // A confirmed user choice takes precedence over a quiet status read,
        // mirroring SpeedBuffController: abort the poll, then send the action.
        if (!inFlight) return
        var xhr = currentRequest
        currentRequest = null; requestDeadline.stop(); inFlight = false; busy = false
        if (xhr !== null) xhr.abort()
    }
    function request(action) {
        if (inFlight) return false
        // The action string doubles as the URL path, so a select carries its
        // arguments ("select/<id>/<aperture>"); derive the kind up front and
        // use it for every branch below, never the raw action string.
        var kind = action === "status" ? "status" : (action === "log" ? "log" : "select")
        inFlight = true
        busy = kind !== "status"
        var xhr = new XMLHttpRequest()
        currentRequest = xhr
        requestDeadline.restart()
        xhr.onreadystatechange = function() {
            if (xhr.readyState !== XMLHttpRequest.DONE || currentRequest !== xhr) return
            requestDeadline.stop()
            currentRequest = null
            inFlight = false
            busy = false
            try {
                if (xhr.status !== 200) throw new Error("backend unavailable")
                if (kind === "log") {
                    // Worker log lines are raw text, not JSON: store, don't parse.
                    logText = xhr.responseText
                    return
                }
                var state = JSON.parse(xhr.responseText)
                if (state.ready === true) {
                    // Worker selection state applies on initial sync and on
                    // select confirmations only: later status polls update
                    // readiness/message but never revert a local aperture
                    // change whose worker sync is still in flight.
                    var applyState = (kind === "select") ||
                                     (kind === "status" && !initialSyncDone)
                    if (applyState) {
                        if (typeof state.selectedProfileId === "string" &&
                            profileById(state.selectedProfileId) !== null) {
                            selectedProfileId = state.selectedProfileId
                            refreshSelection()
                        }
                        if (typeof state.aperture === "number" && selectedProfile &&
                            fullStops.indexOf(state.aperture) >= 0 &&
                            state.aperture >= selectedProfile.maxAperture)
                            aperture = state.aperture
                    }
                    if (kind === "status")
                        initialSyncDone = true
                    if (kind === "select" && state.selectedProfileId === pendingSelectId)
                        selectionConfirmed()
                }
                ready = state.ready === true
                faulted = !ready
                statusMessage = state.message
            } catch (e) {
                ready = false; faulted = true
                statusMessage = "Lens profile service unavailable"
            }
        }
        var method = (action === "status" || action === "log") ? "GET" : "POST"
        xhr.open(method, "http://127.0.0.1:18767/" + action)
        xhr.send()
        return true
    }
    function requestStatus() { return request("status") }
    function requestLog() { return request("log") }
    function requestSelect(id, ap) {
        var profile = profileById(id)
        if (profile === null) return false
        if (typeof ap !== "number" || fullStops.indexOf(ap) < 0 || ap < profile.maxAperture)
            ap = profile.maxAperture
        pendingSelectId = id
        return request("select/" + id + "/" + ap)
    }
    function selectProfile(id) {
        // Master-off gates writes: local state is untouched, nothing is sent.
        if (!master || !loaded || profileById(id) === null) return false
        takeOverInFlight()
        return requestSelect(id, effectiveAperture())
    }
    function setAperture(value) {
        if (selectedProfile === null) return false
        if (typeof value !== "number" || fullStops.indexOf(value) < 0 ||
            value < selectedProfile.maxAperture) return false
        aperture = value
        if (!master) return true
        takeOverInFlight()
        return requestSelect(selectedProfileId, value)
    }
    function apertureUp() {
        var cur = effectiveAperture()
        var i = fullStops.indexOf(cur)
        if (i < 0 || i + 1 >= fullStops.length) return false
        return setAperture(fullStops[i + 1])
    }
    function apertureDown() {
        var cur = effectiveAperture()
        var i = fullStops.indexOf(cur)
        if (i <= 0) return false
        return setAperture(fullStops[i - 1])
    }
    function setDistanceM(value) {
        if (value !== -1 && (typeof value !== "number" || !(value > 0))) return false
        focusDistanceM = value
        return true
    }
    function setFrameLinesEnabled(value) {
        // Records the preference only. The live-view overlay itself comes
        // from a future overlay engine (X-Pan reuse); this ships no overlay.
        frameLinesEnabled = value === true
        return true
    }
    function setMaster(value) {
        // Master is a page-side gate for the lens worker, which has no master
        // action: nothing is sent, so a master toggle can never exit 35.
        master = value === true
        return true
    }
    property Timer poll: Timer { interval: 2500; running: true; repeat: true; onTriggered: root.request("status") }
    property Timer requestDeadline: Timer {
        interval: 15000
        onTriggered: {
            var xhr = root.currentRequest
            root.currentRequest = null; root.inFlight = false; root.busy = false
            if (xhr !== null) xhr.abort()
            root.ready = false; root.faulted = true
            root.statusMessage = "Lens profile request timed out"
        }
    }
    Component.onCompleted: request("status")
}
