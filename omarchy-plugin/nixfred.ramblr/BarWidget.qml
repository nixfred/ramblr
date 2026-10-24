import QtQuick
import Quickshell
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// Ramblr's mic dot. Teal + slow pulse = listening; magenta + slash = muted; amber = recording a fallback mic;
// dim = service down. Click toggles mute (muted stops the recorder entirely).
BarWidget {
  id: root
  moduleName: "nixfred.ramblr"

  readonly property var svc: bar && bar.shell ? bar.shell.serviceFor(moduleName) : null
  readonly property var st: svc && svc.ready ? svc.st : ({ mode: "down" })
  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  // fixed status colours: some themes map urgent colours to green, so state never derives from the palette
  readonly property color teal: "#2de2e6"
  readonly property color magenta: "#ff3ea5"
  readonly property color amber: "#f5a623"
  readonly property bool listening: st.mode === "listening" && !st.warning
  readonly property color dotColor: st.mode === "down" ? Util.alpha(foreground, 0.3)
    : (st.muted || st.mode === "muted" || st.mode === "muting") ? magenta
    : st.warning ? amber : teal

  implicitWidth: vertical ? barSize : Style.space(24)
  implicitHeight: vertical ? Style.space(24) : barSize

  function micName(s) {
    if (!s) return "no mic"
    if (s.indexOf("BRIO") >= 0) return "BRIO webcam mic"
    return s.replace(/^alsa_input\./, "").replace(/\.(analog|mono|iec958).*$/, "")
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: ""
    labelVisible: false
    hasVisualContent: true
    active: false
    useActiveColor: false
    tooltipText: {
      var s = root.st
      if (s.mode === "down") return "Ramblr: service down (systemctl --user status ramblr-capture)"
      var head = (s.muted || s.mode === "muted") ? "Ramblr: MUTED, nothing is being recorded" : "Ramblr: listening on " + root.micName(s.source)
      var lines = head + "\n" + (s.today_lines || 0) + " lines today"
      if (s.warning) lines += "\nWarning: " + s.warning
      return lines + "\nClick to " + ((s.muted || s.mode === "muted") ? "unmute" : "mute")
    }

    Item {
      anchors.centerIn: parent
      width: Style.space(14); height: width
      Rectangle {
        id: dot
        anchors.centerIn: parent
        width: parent.width; height: width; radius: width / 2
        color: "transparent"; border.width: Math.max(2, width * 0.18); border.color: root.dotColor
        Behavior on border.color { ColorAnimation { duration: 300 } }
        SequentialAnimation on opacity {
          running: root.listening; loops: Animation.Infinite
          NumberAnimation { to: 0.5; duration: 1600; easing.type: Easing.InOutSine }
          NumberAnimation { to: 1.0; duration: 1600; easing.type: Easing.InOutSine }
          onRunningChanged: if (!running) dot.opacity = 1
        }
      }
      Rectangle {  // the mic capsule inside the ring
        anchors.centerIn: parent
        width: parent.width * 0.28; height: parent.width * 0.46; radius: width / 2; border.width: 0
        color: root.dotColor
      }
      Rectangle {  // slash when muted
        visible: root.st.muted || root.st.mode === "muted" || root.st.mode === "muting"
        anchors.centerIn: parent
        width: parent.width * 1.15; height: Math.max(2, parent.width * 0.14); radius: height / 2
        rotation: -45; color: root.dotColor; border.width: 0
      }
    }

    onPressed: function(code) {
      if (root.bar) root.bar.hideTooltip(root)
      if (root.svc) root.svc.toggleMute()
    }
  }
}
