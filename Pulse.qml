import QtQuick

// Axis-aligned pulse. Painted as filled pixels so the bar does not scale a
// hairline stroke and blur it.
Canvas {
  id: root

  property color strokeColor: "#20231e"
  property real beat: 1

  implicitWidth: 28
  implicitHeight: 20
  antialiasing: false
  canvasSize: Qt.size(width, height)

  onStrokeColorChanged: requestPaint()
  onBeatChanged: requestPaint()
  onWidthChanged: requestPaint()
  onHeightChanged: requestPaint()

  onPaint: {
    var ctx = getContext("2d")
    ctx.reset()
    ctx.fillStyle = root.strokeColor
    ctx.globalAlpha = root.beat
    if (width <= 18 && height <= 18) {
      // 1px pulse inside the bar's 16px icon canvas. Same box as the other icons.
      var cells = [
        [0, 8, 4, 1],
        [3, 6, 1, 2],
        [3, 6, 2, 1],
        [4, 4, 1, 2],
        [4, 4, 3, 1],
        [6, 4, 1, 4],
        [6, 8, 2, 1],
        [7, 8, 1, 4],
        [7, 11, 2, 1],
        [8, 8, 1, 3],
        [8, 8, 8, 1]
      ]
      for (var c = 0; c < cells.length; c++)
        ctx.fillRect(cells[c][0], cells[c][1], cells[c][2], cells[c][3])
      return
    }
    var sx = width / 28
    var sy = height / 20
    function px(value, scale) { return Math.round(value * scale) }
    var thickness = Math.max(1, Math.round(2 * sx))
    var half = Math.floor(thickness / 2)
    var points = [
      [1, 11], [7, 11], [7, 7], [9, 7], [9, 3], [11, 3], [11, 7],
      [13, 7], [13, 13], [15, 13], [15, 17], [17, 17], [17, 11], [27, 11]
    ]
    for (var i = 1; i < points.length; i++) {
      var x1 = px(points[i - 1][0], sx)
      var y1 = px(points[i - 1][1], sy)
      var x2 = px(points[i][0], sx)
      var y2 = px(points[i][1], sy)
      if (y1 === y2) {
        var left = Math.min(x1, x2)
        ctx.fillRect(left, y1 - half, Math.max(thickness, Math.abs(x2 - x1)), thickness)
      } else {
        var top = Math.min(y1, y2)
        ctx.fillRect(x1 - half, top, thickness, Math.max(thickness, Math.abs(y2 - y1)))
      }
    }
  }
}
