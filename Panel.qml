import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
  id: root

  moduleName: "nomdelasociete.dsmn"
  ipcTarget: "nomdelasociete.dsmn"
  manageIpc: false

  property var status: ({})
  property string notice: ""
  property bool customOpen: false
  property int customMinutes: 45
  property double nowMs: Date.now()
  property bool busy: false
  property string view: "home"
  readonly property bool showCountdown: setting("showCountdown", true) !== false
  readonly property string barSection: {
    var config = bar && bar.shell ? bar.shell.barConfig : null
    var layout = config && config.layout ? config.layout : null
    var sections = ["left", "center", "right"]
    if (!layout) return "right"
    for (var s = 0; s < sections.length; s++) {
      var entries = layout[sections[s]] || []
      for (var i = 0; i < entries.length; i++) {
        var entry = entries[i]
        var id = entry && entry.id ? entry.id : entry
        if (id === moduleName) return sections[s]
      }
    }
    return "right"
  }

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property bool preventing: status.prevent === true
  readonly property bool leaseActive: status.active === true
  readonly property bool warning: (status.warning && String(status.warning).length > 0) || (leaseActive && status.inhibitorHeld === false)
  readonly property int liveRemaining: {
    var until = status.manualUntil
    if (!until) return 0
    return Math.max(0, Math.floor((until - nowMs) / 1000))
  }
  readonly property var activity: status.activity || []
  readonly property bool hooksNeedInstall: {
    var hooks = status.hooks || ({})
    var names = ["claude", "cursor", "codex", "grok", "pi"]
    for (var i = 0; i < names.length; i++) {
      var value = hooks[names[i]]
      if (value === "missing" || value === "outdated" || value === "unreadable") return true
    }
    return false
  }

  function program() {
    return Quickshell.env("HOME") + "/.config/omarchy/plugins/nomdelasociete.dsmn/bin/dsmn"
  }

  function formatCountdown(seconds) {
    var value = Math.max(0, seconds)
    var hours = Math.floor(value / 3600)
    var minutes = Math.floor(value / 60) % 60
    var secs = value % 60
    function pad(n) { return (n < 10 ? "0" : "") + n }
    if (hours > 0) return hours + ":" + pad(minutes) + ":" + pad(secs)
    return pad(Math.floor(value / 60)) + ":" + pad(secs)
  }

  function refresh() {
    if (statusProc.running) return
    statusProc.command = [root.program(), "status", "--json"]
    statusProc.running = true
  }

  function run(args) {
    if (root.busy) return
    root.busy = true
    actionProc.begin()
    var command = [root.program()]
    for (var i = 0; i < args.length; i++) command.push(String(args[i]))
    actionProc.command = command
    actionProc.running = true
  }

  function manual(minutes) { root.run(["manual", "--minutes", String(minutes), "--json"]) }
  function stop() { root.run(["stop", "--json"]) }
  function repair() { root.run(["repair", "--json"]) }
  function installHooks() { root.notice = "Installing…"; root.run(["install-hooks", "all"]) }
  function uninstallHooks() { root.notice = "Removing hooks…"; root.run(["uninstall-hooks", "all"]) }
  function doctor() { root.notice = "Checking…"; root.run(["doctor"]) }

  function openTerminal(scriptName) {
    var path = Quickshell.env("HOME") + "/.config/omarchy/plugins/nomdelasociete.dsmn/bin/" + scriptName
    terminalProc.command = ["omarchy", "launch", "terminal", path]
    terminalProc.running = true
  }

  function compactCountdown(seconds) {
    var value = Math.max(0, seconds)
    if (value < 60) return value + "s"
    var minutes = Math.floor(value / 60)
    if (minutes < 60) return minutes + "m"
    var hours = Math.floor(minutes / 60)
    var leftover = minutes % 60
    return leftover === 0 ? hours + "h" : hours + "h" + leftover + "m"
  }

  function moveToSection(section) {
    if (!section || section === root.barSection || moveProc.running) return
    moveProc.command = ["omarchy-shell", "shell", "moveBarWidget", root.moduleName, JSON.stringify({ section: section })]
    moveProc.running = true
  }

  function setShowCountdown(value) {
    var next = ({})
    if (settings) {
      for (var key in settings) next[key] = settings[key]
    }
    next.showCountdown = value
    settings = next
    if (bar && bar.shell && bar.shell.updateEntryInline)
      bar.shell.updateEntryInline(moduleName, next)
  }

  function applyActionOutput(stdout, stderr, code) {
    var body = (stdout || "").trim()
    var error = (stderr || "").trim()
    if (code !== 0 && error.length > 0) {
      root.notice = error
      return
    }
    try {
      var parsed = JSON.parse(body)
      if (parsed.summary) root.notice = parsed.summary
      else root.notice = ""
      return
    } catch (ignore) {}
    root.notice = body || error
  }

  function installedAgents() {
    var hooks = status.hooks || ({})
    var names = [
      ["claude", "Claude"],
      ["cursor", "Cursor"],
      ["codex", "Codex"],
      ["grok", "Grok"],
      ["pi", "Pi"]
    ]
    var found = []
    for (var i = 0; i < names.length; i++)
      if (hooks[names[i][0]] === "current") found.push(names[i][1])
    if (found.length === 0) return ""
    if (found.length === 1) return found[0]
    if (found.length === 2) return found[0] + " and " + found[1]
    return found.slice(0, found.length - 1).join(", ") + ", and " + found[found.length - 1]
  }

  function missingAgents() {
    var hooks = status.hooks || ({})
    var names = [
      ["claude", "Claude"],
      ["cursor", "Cursor"],
      ["codex", "Codex"],
      ["grok", "Grok"],
      ["pi", "Pi"]
    ]
    var found = []
    for (var i = 0; i < names.length; i++) {
      var value = hooks[names[i][0]]
      if (value === "missing" || value === "outdated") found.push(names[i][1])
    }
    return found.join(", ")
  }

  function othersLine() {
    var rows = status.inhibitors || []
    var names = []
    for (var i = 0; i < rows.length; i++) {
      if (!rows[i] || rows[i].who === "dsmn") continue
      if (names.indexOf(rows[i].who) < 0) names.push(rows[i].who)
    }
    if (!names.length) return "Nothing else is blocking sleep."
    return "Also blocking sleep: " + names.join(", ") + "."
  }

  function activityPeak() {
    var peak = 1
    for (var i = 0; i < activity.length; i++)
      if (activity[i] > peak) peak = activity[i]
    return peak
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight
  onOpenedChanged: if (opened) root.refresh()

  Timer { interval: 1000; running: root.leaseActive; repeat: true; onTriggered: root.nowMs = Date.now() }
  Timer { interval: 5000; running: true; repeat: true; triggeredOnStart: true; onTriggered: root.refresh() }

  Process {
    id: statusProc
    stdout: StdioCollector {
      onStreamFinished: { try { root.status = JSON.parse(text.trim()) } catch (ignore) {} }
    }
  }

  Process {
    id: actionProc
    property string out: ""
    property string err: ""
    property int code: 0
    property bool outDone: false
    property bool errDone: false
    property bool procDone: false
    function begin() { out = ""; err = ""; code = 0; outDone = false; errDone = false; procDone = false }
    function maybeFinish() {
      if (!outDone || !errDone || !procDone) return
      root.busy = false
      root.applyActionOutput(out, err, code)
      root.refresh()
    }
    stdout: StdioCollector { onStreamFinished: { actionProc.out = text; actionProc.outDone = true; actionProc.maybeFinish() } }
    stderr: StdioCollector { onStreamFinished: { actionProc.err = text; actionProc.errDone = true; actionProc.maybeFinish() } }
    onExited: function(exitCode) { actionProc.code = exitCode; actionProc.procDone = true; actionProc.maybeFinish() }
  }

  Process { id: terminalProc }

  Process {
    id: moveProc
    stdout: StdioCollector {
      onStreamFinished: {
        var body = text.trim()
        if (body && body !== "ok") root.notice = body
      }
    }
  }

  IpcHandler {
    target: "nomdelasociete.dsmn"
    function open(): void { root.open() }
    function close(): void { root.close() }
    function toggle(): void { root.toggle() }
    function installHooks(): string { root.installHooks(); return "started" }
    function settings(): void { root.view = "settings"; root.open() }
    function status(): string { return JSON.stringify(root.status) }
  }

  Item {
    id: button
    implicitHeight: bar ? bar.barSize : Style.bar.sizeHorizontal
    implicitWidth: face.implicitWidth

    Row {
      id: face
      anchors.verticalCenter: parent.verticalCenter
      spacing: 2

      Item {
        width: Style.bar.iconSlot
        height: button.implicitHeight

        Item {
          id: mark
          width: Style.bar.iconCanvas
          height: Style.bar.iconCanvas
          x: Math.round((parent.width - width) / 2)
          y: Math.round((parent.height - height) / 2)

          Pulse {
            anchors.fill: parent
            strokeColor: root.warning ? root.urgent : root.foreground
            beat: root.preventing ? 1 : 0.45
          }

          Rectangle {
            visible: root.preventing
            width: 2
            height: 2
            x: 13
            y: 2
            color: root.warning ? root.urgent : root.foreground
          }
        }
      }

      Text {
        visible: root.showCountdown && root.leaseActive && !root.vertical
        anchors.verticalCenter: parent.verticalCenter
        text: root.compactCountdown(root.liveRemaining)
        color: root.warning ? root.urgent : root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.bar.iconFont
      }
    }

    MouseArea {
      anchors.fill: parent
      onClicked: root.toggle()
      hoverEnabled: true
      onEntered: if (root.bar) root.bar.showTooltip(button, root.preventing ? "Awake · " + root.compactCountdown(root.liveRemaining) + " left" : "Sleep allowed")
      onExited: if (root.bar) root.bar.hideTooltip(button)
    }

    Component.onCompleted: if (bar && bar.registerClickTarget) bar.registerClickTarget(button)
    Component.onDestruction: if (bar && bar.unregisterClickTarget) bar.unregisterClickTarget(button)
  }

  KeyboardPanel {
    id: popup
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: popup.fittedContentWidth(Style.space(320))
    contentHeight: popup.fittedContentHeight(pages.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(text) {
        if (text === "3") root.manual(30)
        else if (text === "9") root.manual(90)
        else if (text === ".") root.stop()
      }

      Item {
        id: pages
        width: parent.width
        implicitHeight: root.view === "settings" ? settingsBody.implicitHeight : body.implicitHeight

      Column {
        id: body
        visible: root.view === "home"
        width: parent.width
        spacing: Style.space(12)

        Row {
          spacing: Style.space(10)

          Pulse {
            width: 28
            height: 20
            anchors.verticalCenter: nameBlock.verticalCenter
            strokeColor: root.warning ? root.urgent : root.foreground
            beat: 1
          }

          Column {
            id: nameBlock
            spacing: 0

            Text {
              text: "dsmn"
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.display
              font.bold: true
            }
            Text {
              text: "Don't stop me now"
              color: Qt.darker(root.foreground, 1.4)
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }
        }

        Column {
          width: parent.width
          spacing: 0

          Text {
            text: root.preventing ? "Awake" : (root.leaseActive ? "Lease on, sleep allowed" : "Sleep allowed")
            color: root.warning ? root.urgent : root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.title
            font.bold: true
          }
          Text {
            text: root.leaseActive ? root.formatCountdown(root.liveRemaining) + " left" : "No timer"
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.displayLarge
            font.bold: true
          }
        }

        Text {
          width: parent.width
          elide: Text.ElideRight
          text: root.status.requesterLine || "No agent has renewed this."
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
        }

        Text {
          width: parent.width
          wrapMode: Text.WordWrap
          text: {
            if (root.warning && root.status.warning) return root.status.warning
            if (root.leaseActive && root.status.inhibitorHeld === false) return "The timer is running, but the sleep lock is not held. Repair puts that right."
            if (root.preventing) return "Sleep returns when this reaches zero. Agent work can add up to 20 minutes. It does not stack on a longer timer."
            return "The machine can sleep. 30 min or 90 min starts a timer. Agents can extend a timer that is already running."
          }
          color: root.warning ? root.urgent : Qt.darker(root.foreground, 1.25)
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
        }

        Column {
          width: parent.width
          spacing: Style.space(4)
          Text {
            text: "Activity, last 6 hours"
            color: Qt.darker(root.foreground, 1.45)
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            font.bold: true
          }
          Row {
            width: parent.width
            height: Style.space(18)
            spacing: Style.space(3)
            Repeater {
              model: 12
              delegate: Rectangle {
                width: (parent.width - Style.space(3) * 11) / 12
                height: Math.max(2, Style.space(18) * ((root.activity[index] || 0) / root.activityPeak()))
                anchors.bottom: parent.bottom
                radius: 1
                color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, (root.activity[index] || 0) > 0 ? 0.9 : 0.16)
              }
            }
          }
        }

        Item {
          width: parent.width
          height: durations.implicitHeight

          Row {
            id: durations
            width: parent.width
            spacing: Style.space(8)
            visible: !root.customOpen
            Button {
              text: "30 min"
              width: (parent.width - Style.space(16)) / 3
              bordered: true
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: root.manual(30)
            }
            Button {
              text: "90 min"
              width: (parent.width - Style.space(16)) / 3
              bordered: true
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: root.manual(90)
            }
            Button {
              text: "Custom"
              width: (parent.width - Style.space(16)) / 3
              bordered: true
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: root.customOpen = true
            }
          }

          Row {
            width: parent.width
            spacing: Style.space(8)
            visible: root.customOpen
            Button {
              text: "−"
              width: Style.space(44)
              bordered: true
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: root.customMinutes = Math.max(5, root.customMinutes - 15)
            }
            Button {
              text: root.customMinutes + " min"
              width: parent.width - Style.space(44) * 3 - Style.space(24)
              bordered: true
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: { root.manual(root.customMinutes); root.customOpen = false }
            }
            Button {
              text: "+"
              width: Style.space(44)
              bordered: true
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: root.customMinutes = Math.min(480, root.customMinutes + 15)
            }
            Button {
              text: "Back"
              width: Style.space(44)
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: root.customOpen = false
            }
          }
        }

        Button {
          width: parent.width
          text: root.preventing ? "Stop, and allow sleep" : "Stop"
          foreground: root.foreground
          fontFamily: root.fontFamily
          enabled: root.leaseActive
          onClicked: root.stop()
        }

        Text {
          width: parent.width
          wrapMode: Text.WordWrap
          text: root.othersLine()
          color: Qt.darker(root.foreground, 1.35)
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }

        Item {
          width: parent.width
          height: root.hooksNeedInstall ? installBlock.implicitHeight : installedLine.implicitHeight

          Text {
            id: installedLine
            visible: !root.hooksNeedInstall
            width: parent.width
            wrapMode: Text.WordWrap
            text: {
              var names = root.installedAgents()
              if (!names) return "No agent hooks yet."
              return names + " will renew a running timer while they work."
            }
            color: Qt.darker(root.foreground, 1.35)
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          Column {
            id: installBlock
            visible: root.hooksNeedInstall
            width: parent.width
            spacing: Style.space(6)
            Text {
              width: parent.width
              wrapMode: Text.WordWrap
              text: root.notice.length > 0 ? root.notice : (root.missingAgents() + " can keep a timer running, once the hook is installed.")
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
            Button {
              width: parent.width
              text: "Install hooks"
              bordered: true
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: root.installHooks()
            }
          }
        }

        Button {
          visible: root.warning
          width: parent.width
          text: "Repair the sleep lock"
          bordered: true
          foreground: root.urgent
          fontFamily: root.fontFamily
          onClicked: root.repair()
        }

        Button {
          width: parent.width
          text: "Settings"
          foreground: root.foreground
          fontFamily: root.fontFamily
          onClicked: root.view = "settings"
        }
      }

      Column {
        id: settingsBody
        visible: root.view === "settings"
        width: parent.width
        spacing: Style.space(12)

        Button {
          text: "Back"
          foreground: root.foreground
          fontFamily: root.fontFamily
          onClicked: root.view = "home"
        }

        Text {
          text: "Settings"
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.title
          font.bold: true
        }

        Text {
          text: "On the bar"
          color: Qt.darker(root.foreground, 1.4)
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          font.bold: true
        }

        ButtonGroup {
          width: parent.width
          foreground: root.foreground
          fontFamily: root.fontFamily
          options: [
            { value: "left", label: "Left" },
            { value: "center", label: "Center" },
            { value: "right", label: "Right" }
          ]
          value: root.barSection
          onChanged: function(value) { root.moveToSection(value) }
        }

        Text {
          width: parent.width
          wrapMode: Text.WordWrap
          text: root.status.inhibitorHeld ? "Sleep lock is on. The machine stays awake until the timer ends." : "Sleep lock is off. The machine can sleep."
          color: Qt.darker(root.foreground, 1.25)
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
        }

        Button {
          width: parent.width
          text: "Repair sleep lock"
          bordered: true
          foreground: root.foreground
          fontFamily: root.fontFamily
          onClicked: root.repair()
        }

        Text {
          width: parent.width
          wrapMode: Text.WordWrap
          text: {
            var names = root.installedAgents()
            var missing = root.missingAgents()
            if (root.hooksNeedInstall) return missing + " still need a hook."
            if (!names) return "No supported agent is installed."
            return names + " are hooked. They renew a running timer while they work."
          }
          color: Qt.darker(root.foreground, 1.25)
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
        }

        Button {
          visible: root.hooksNeedInstall
          width: parent.width
          text: "Install hooks"
          bordered: true
          foreground: root.foreground
          fontFamily: root.fontFamily
          onClicked: root.installHooks()
        }

        Button {
          width: parent.width
          text: "Remove hooks"
          foreground: root.foreground
          fontFamily: root.fontFamily
          onClicked: root.uninstallHooks()
        }

        Toggle {
          width: parent.width
          label: "Countdown in the bar"
          description: "Show the time left next to the icon."
          checked: root.showCountdown
          foreground: root.foreground
          fontFamily: root.fontFamily
          onClicked: root.setShowCountdown(!root.showCountdown)
        }

        Text {
          width: parent.width
          wrapMode: Text.WordWrap
          text: root.preventing ? "dsmn is holding sleep. " + root.othersLine() : root.othersLine()
          color: Qt.darker(root.foreground, 1.25)
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
        }

        Button {
          width: parent.width
          text: "Doctor"
          foreground: root.foreground
          fontFamily: root.fontFamily
          onClicked: root.doctor()
        }

        Text {
          visible: root.notice.length > 0
          width: parent.width
          wrapMode: Text.WordWrap
          text: root.notice
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }

        Button {
          visible: root.status.lidPresent === true
          width: parent.width
          text: root.status.lidDropIn ? "Lid close obeys the timer" : "Let a closed lid obey the timer"
          foreground: root.foreground
          fontFamily: root.fontFamily
          onClicked: root.openTerminal(root.status.lidDropIn ? "dsmn-lid-disable" : "dsmn-lid-enable")
        }
      }
      }
    }
  }
}
