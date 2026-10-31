import QtQuick
// Lens profile picker page. Plain QtQuick items only: the stock
// MenuHeader/row components are camera-side imports unavailable offline,
// so this page is self-contained and instantiable directly, like
// PrankIbisPage is instantiated by its route. It is registered in the
// Tweaks row list in PlayPage.qml (settingsColumn), which opens it in a
// Loader overlay; see PlayPage.lensProfileOpen.
//
// Internationalization: user-visible strings are centralized as str*
// properties in Simplified Chinese, the repo base language. The English
// and Traditional Chinese overlays are generated at build time by
// src/camera_ui_strings.py (LABELS pairs) via build_native_preload.py /
// build_stable_speed.py, exactly like the other camera pages. Do not add
// a user-visible string here without a LABELS pair and a catalog entry
// in src/translations_zh_hant.json.
//
// On-camera profile loading is a build-time integration item, done by
// src/build_lens_profile_bundle.py: it bakes the
// /system/etc/X2dLensProfiles.json text into PlayPage's
// lensProfilesJsonText property, which the Loader feeds into
// lensController.loadProfiles() on load. QML here performs no direct
// file read.
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
    // Centralized user-visible strings (Simplified Chinese base; see note above).
    readonly property string strTitle: "镜头配置文件"
    readonly property string strBack: "返回"
    readonly property string strMaster: "主开关"
    readonly property string strMasterOn: "开"
    readonly property string strMasterOff: "关"
    readonly property string strAperture: "光圈"
    readonly property string strDistance: "对焦距离"
    readonly property string strHyperfocal: "超焦距"
    readonly property string strNear: "近点"
    readonly property string strFar: "远点"
    readonly property string strDepth: "景深"
    readonly property string strFrameLines: "取景框线"
    readonly property string strNoProfile: "未选择镜头配置文件"
    readonly property string strStatus: "状态"
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
