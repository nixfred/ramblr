import QtQuick
import Quickshell
import Quickshell.Io

// `python -m ramblr.ctl watch` prints one JSON status line per interval; this keeps the latest.
// Mute toggles run as one-shot processes so a slow call never blocks the bar.
Item {
  id: root

  property var settings: ({})
  readonly property string project: Quickshell.env("HOME") + "/Projects/ramblr"
  readonly property var py: ["env", "PYTHONPATH=" + project, project + "/.venv/bin/python", "-m", "ramblr.ctl"]
  readonly property int intervalSec: {
    var n = parseInt(String(settings && settings.intervalSec !== undefined ? settings.intervalSec : 2), 10)
    return isFinite(n) ? Math.max(1, Math.min(30, n)) : 2
  }

  property bool ready: false
  property var st: ({ mode: "down", muted: false, source: "", chosen: "", warning: "", today_lines: 0 })

  function apply(line) {
    var d
    try { d = JSON.parse(line) } catch (e) { return }
    if (!d || d.mode === undefined) return
    st = d
    ready = true
  }

  function toggleMute() {
    var p = actComp.createObject(root, { command: py.concat(["mute", "toggle"]) })
    p.running = true
  }
  Component {
    id: actComp
    Process { onRunningChanged: if (!running) destroy() }
  }

  Process {
    id: proc
    command: root.py.concat(["watch", "--interval", String(root.intervalSec)])
    running: true
    stdout: SplitParser { splitMarker: "\n"; onRead: function(line) { root.apply(line) } }
    stderr: SplitParser { splitMarker: "\n"; onRead: function(line) { console.warn("ramblr:", line) } }
    onRunningChanged: if (!running) { root.ready = false; respawn.restart() }
  }
  onIntervalSecChanged: { proc.running = false; respawn.restart() }
  Timer { id: respawn; interval: 5000; onTriggered: if (!proc.running) proc.running = true }
}
