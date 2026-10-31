import QtQuick
// Lens profile picker page (Phase 1, source only). Plain QtQuick items only:
// the stock MenuHeader/row components are camera-side imports unavailable
// offline, so this page is self-contained and instantiable directly, like
// PrankIbisPage is instantiated by its route. Swapping these plain rows for
// stock components is part of the wiring follow-up, as is registering this
// page in the Tweaks row list (which would require editing existing
// navigation files and is deliberately NOT done in this change set).
//
// Internationalization note: user-visible strings are centralized as str*
// properties in English. The repo's per-language overlay mechanism (base
// QML in Simplified Chinese + generated en/zh-Hant overlays via
// src/camera_ui_strings.py) requires editing that existing file, which this
// change set forbids, so the follow-up is mechanical: convert these
// properties to the Simplified Chinese base, add LABELS pairs, and regenerate
// the overlays at build time. Do not ship this page as translated until
// that follow-up is done.
//
// On-camera profile loading is a build-time integration item: the route or
// the loopback server shim must feed the baked
// /system/etc/X2dLensProfiles.json text into lensController.loadProfiles().
// QML here performs no direct file read.
FocusScope {
    id: root
    objectName: "X2dLensProfilePageRoot"
    property bool showing: false
    visible: showing
    enabled: showing
    focus: showing
    signal backRequested()
    readonly property alias lensController: controller
    X2dLensProfileController { id: controller }
    // Centralized user-visible strings (English base; see note above).
    readonly property string strTitle: "Lens profile"
    readonly property string strBack: "Back"
    readonly property string strMaster: "Master"
    readonly property string strMasterOn: "On"
    readonly property string strMasterOff: "Off"
    readonly property string strAperture: "Aperture"
    readonly property string strDistance: "Focus distance"
    readonly property string strHyperfocal: "Hyperfocal"
    readonly property string strNear: "Near"
    readonly property string strFar: "Far"
    readonly property string strDepth: "Depth"
    readonly property string strFrameLines: "Frame lines"
    readonly property string strNoProfile: "No lens profile selected"
    readonly property string strStatus: "Status"
    function readoutText() {
        var c = controller
        if (c.selectedProfile === null) return strNoProfile
        var h = c.hyperfocalMm()
        var n = c.nearLimitMm()
        var f = c.farLimitMm()
        var lines = [strHyperfocal + ": " + c.formatMm(h),
                     strNear + ": " + c.formatMm(n),
                     strFar + ": " + c.formatMm(f)]
        lines.push(strDepth + ": " + (f >= 0 ? c.formatMm(f - n) : "\u221e"))
        return lines.join("\n")
    }
    Keys.onEscapePressed: backRequested()
    Keys.onPressed: function(event) {
        if (event.key === Qt.Key_Back) { backRequested(); event.accepted = true }
    }
    Column {
        anchors.fill: parent
        spacing: 12
        Text { text: root.strTitle; font.pixelSize: 40 }
        // Back row.
        Item {
            width: parent.width; height: 56
            Text { anchors.verticalCenter: parent.verticalCenter; text: root.strBack; font.pixelSize: 32 }
            MouseArea { anchors.fill: parent; onClicked: root.backRequested() }
        }
        // Master switch row.
        Item {
            objectName: "X2dLensMasterSwitchRow"
            width: parent.width; height: 56
            Text { anchors.verticalCenter: parent.verticalCenter; text: root.strMaster; font.pixelSize: 32 }
            Text {
                anchors.verticalCenter: parent.verticalCenter; anchors.right: parent.right
                text: controller.master ? root.strMasterOn : root.strMasterOff; font.pixelSize: 32
            }
            MouseArea { anchors.fill: parent; onClicked: controller.setMaster(!controller.master) }
        }
        // Profile rows.
        Repeater {
            model: controller.profiles
            Item {
                objectName: "X2dLensProfileRow"
                width: parent.width; height: 56
                property bool isSelected: modelData.id === controller.selectedProfileId
                Rectangle {
                    anchors.fill: parent
                    color: parent.isSelected ? "#333333" : "transparent"
                }
                Text {
                    anchors.verticalCenter: parent.verticalCenter
                    text: modelData.name + "  f/" + modelData.maxAperture; font.pixelSize: 32
                }
                MouseArea { anchors.fill: parent; onClicked: controller.selectProfile(modelData.id) }
            }
        }
        // Aperture stepper (full stops, clamped at the profile max aperture).
        Item {
            width: parent.width; height: 56
            Text { anchors.verticalCenter: parent.verticalCenter; text: root.strAperture; font.pixelSize: 32 }
            Text {
                objectName: "X2dLensApertureDown"
                anchors.verticalCenter: parent.verticalCenter; anchors.right: apertureValue.left; anchors.rightMargin: 24
                text: "-"; font.pixelSize: 36
                MouseArea { anchors.fill: parent; onClicked: controller.apertureDown() }
            }
            Text {
                id: apertureValue
                objectName: "X2dLensApertureValue"
                anchors.verticalCenter: parent.verticalCenter; anchors.horizontalCenter: parent.horizontalCenter
                text: "f/" + controller.effectiveAperture(); font.pixelSize: 32
            }
            Text {
                objectName: "X2dLensApertureUp"
                anchors.verticalCenter: parent.verticalCenter; anchors.left: apertureValue.right; anchors.leftMargin: 24
                text: "+"; font.pixelSize: 36
                MouseArea { anchors.fill: parent; onClicked: controller.apertureUp() }
            }
        }
        // Distance presets + hyperfocal.
        Text { text: root.strDistance; font.pixelSize: 32 }
        Row {
            spacing: 16
            Repeater {
                model: controller.distancePresets
                Text {
                    text: modelData + " m"; font.pixelSize: 30
                    color: modelData === controller.focusDistanceM ? "#ffffff" : "#888888"
                    MouseArea { anchors.fill: parent; onClicked: controller.setDistanceM(modelData) }
                }
            }
            Text {
                text: root.strHyperfocal; font.pixelSize: 30
                color: controller.focusDistanceM === -1 ? "#ffffff" : "#888888"
                MouseArea { anchors.fill: parent; onClicked: controller.setDistanceM(-1) }
            }
        }
        // DOF readout.
        Item {
            objectName: "X2dLensDofReadout"
            width: parent.width; height: 160
            Text { anchors.fill: parent; text: root.readoutText(); font.pixelSize: 30 }
        }
        // Frame-line toggle: records the preference only (see controller).
        Item {
            objectName: "X2dLensFrameToggleRow"
            width: parent.width; height: 56
            Text { anchors.verticalCenter: parent.verticalCenter; text: root.strFrameLines; font.pixelSize: 32 }
            Text {
                anchors.verticalCenter: parent.verticalCenter; anchors.right: parent.right
                text: controller.frameLinesEnabled ? root.strMasterOn : root.strMasterOff; font.pixelSize: 32
            }
            MouseArea { anchors.fill: parent; onClicked: controller.setFrameLinesEnabled(!controller.frameLinesEnabled) }
        }
        // Status line.
        Text { text: root.strStatus + ": " + controller.statusMessage; font.pixelSize: 28; wrapMode: Text.Wrap }
    }
}
