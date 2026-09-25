import QtQuick
import Quickshell
import Quickshell.Io

// Headless keeper. The bar panel is a view of the same command.
Item {
  id: root

  property var shell: null
  property bool engaged: true
  property string statusJson: ""
  property var status: ({})

  function program() {
    var home = Quickshell.env("HOME")
    if (home && home.length > 0)
      return home + "/.config/omarchy/plugins/nomdelasociete.dsmn/bin/dsmn"
    var url = Qt.resolvedUrl("bin/dsmn").toString()
    if (url.indexOf("file://") === 0)
      url = decodeURIComponent(url.substring(7))
    return url
  }

  function stateFile() {
    var override = Quickshell.env("DSMN_STATE_DIR")
    if (override && override.length > 0)
      return override + "/state.json"
    var xdg = Quickshell.env("XDG_STATE_HOME")
    var base = xdg && xdg.length > 0 ? xdg : Quickshell.env("HOME") + "/.local/state"
    return base + "/dsmn/state.json"
  }

  function refresh() {
    if (!root.engaged || statusProc.running)
      return
    statusProc.command = [root.program(), "status", "--json"]
    statusProc.running = true
  }

  function ensure() {
    if (!root.engaged || ensureProc.running)
      return
    ensureProc.command = [root.program(), "ensure"]
    ensureProc.running = true
  }

  function run(args) {
    if (actionProc.running)
      return
    var command = [root.program()]
    for (var i = 0; i < args.length; i++)
      command.push(String(args[i]))
    actionProc.command = command
    actionProc.running = true
  }

  function manual(minutes) { root.run(["manual", "--minutes", String(minutes), "--json"]) }
  function stop() { root.run(["stop", "--json"]) }
  function repair() { root.run(["repair", "--json"]) }
  function installHooks() { root.run(["install-hooks", "all"]) }
  function uninstallHooks() { root.run(["uninstall-hooks", "all"]) }
  function linkCli() { root.run(["link-cli"]) }

  function openTerminal(scriptName) {
    var url = Qt.resolvedUrl("bin/" + scriptName).toString()
    if (url.indexOf("file://") === 0)
      url = decodeURIComponent(url.substring(7))
    if (terminalProc.running)
      return
    terminalProc.command = ["omarchy", "launch", "terminal", url]
    terminalProc.running = true
  }

  Process {
    id: statusProc
    stdout: StdioCollector {
      onStreamFinished: {
        var body = text.trim()
        root.statusJson = body
        try {
          root.status = JSON.parse(body)
        } catch (error) {
          root.status = ({})
        }
      }
    }
  }

  Process { id: ensureProc }
  Process {
    id: actionProc
    onExited: function(exitCode) { root.refresh() }
  }
  Process { id: terminalProc }

  FileView {
    path: root.stateFile()
    watchChanges: true
    printErrors: false
    onFileChanged: root.refresh()
  }

  Timer {
    interval: 5000
    running: root.engaged
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  Timer {
    interval: 30000
    running: root.engaged
    repeat: true
    triggeredOnStart: true
    onTriggered: root.ensure()
  }

}
