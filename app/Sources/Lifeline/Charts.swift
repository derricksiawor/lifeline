import LifelineKit
import SwiftUI

struct Sparkline: View {
    let plot: WeekPlot
    let line: Color
    let limit: Color
    let width: CGFloat
    var dayInk: Color = .clear
    var dayLit: Color = .clear
    static let height: CGFloat = 40

    var body: some View {
        VStack(spacing: 0) {
            chart
        }
    }

    @ViewBuilder
    private var chart: some View {
        let scaleX = width / DetailChart.width
        let scaleY = Self.height / DetailChart.weekHeight
        Canvas { context, _ in
            context.translateBy(x: 4, y: 4)
            var path = Path()
            var drawing = false
            for point in plot.points {
                guard let point else {
                    drawing = false
                    continue
                }
                let here = CGPoint(x: point.x * scaleX, y: point.y * scaleY)
                if drawing {
                    path.addLine(to: here)
                } else {
                    path.move(to: here)
                    drawing = true
                }
            }
            context.stroke(path, with: .color(line), style: StrokeStyle(lineWidth: 1.5, lineCap: .round, lineJoin: .round))
            for point in plot.limits {
                context.fill(Path(ellipseIn: CGRect(x: point.x * scaleX - 2.5, y: point.y * scaleY - 2.5, width: 5, height: 5)), with: .color(limit))
            }
        }
        .frame(width: width + 8, height: Self.height + 8)
        .padding(-4)
        if !plot.days.isEmpty {
            HStack(spacing: 0) {
                ForEach(Array(plot.days.enumerated()), id: \.offset) { item in
                    let lit = !item.element.isEmpty && plot.limitDays.contains { $0.hasPrefix(item.element) }
                    LineText(DetailChart.dayLabel(item.element), Styles.dayMark, lit ? dayLit : dayInk)
                        .frame(maxWidth: .infinity)
                        .accessibilityLabel(item.element + (lit ? ", ran out" : ""))
                }
            }
            .frame(width: width)
            .padding(.top, 2)
        }
    }
}

struct WindowChartView: View {
    let plot: WindowPlot
    let tone: Tone
    let palette: Palette
    let width: CGFloat
    static let height = CGFloat(WindowChart.height)

    var body: some View {
        let scaleX = width / DetailChart.width
        let scaleY = Self.height / DetailChart.weekHeight
        func at(_ point: ChartPoint) -> CGPoint {
            CGPoint(x: point.x * scaleX, y: point.y * scaleY)
        }
        return VStack(spacing: 0) {
            Canvas { context, _ in
                context.translateBy(x: 4, y: 4)
                var line = Path()
                for (index, point) in plot.points.enumerated() {
                    index == 0 ? line.move(to: at(point)) : line.addLine(to: at(point))
                }
                if plot.forecast.count == 2 {
                    var ahead = Path()
                    ahead.move(to: at(plot.forecast[0]))
                    ahead.addLine(to: at(plot.forecast[1]))
                    context.stroke(ahead, with: .color(tone.fore), style: StrokeStyle(lineWidth: 1.5, lineCap: .round))
                }
                context.stroke(line, with: .color(tone.solid), style: StrokeStyle(lineWidth: 1.5, lineCap: .round, lineJoin: .round))
                if let here = plot.now {
                    let mark = at(here)
                    context.fill(Path(ellipseIn: CGRect(x: mark.x - 3, y: mark.y - 3, width: 6, height: 6)), with: .color(tone.solid))
                }
                if let out = plot.runOut {
                    let mark = at(out)
                    context.fill(Path(ellipseIn: CGRect(x: mark.x - 3, y: mark.y - 3, width: 6, height: 6)), with: .color(palette.limit.solid))
                }
            }
            .frame(width: width + 8, height: Self.height + 8)
            .padding(-4)
            ZStack(alignment: .topLeading) {
                ForEach(Array(labels.enumerated()), id: \.offset) { item in
                    LineText(item.element.text, Styles.dayMark, item.element.lit ? palette.limit.ink : palette.faint)
                        .offset(x: item.element.x)
                }
            }
            .frame(width: width, height: Styles.dayMark.lineHeight, alignment: .topLeading)
            .padding(.top, 2)
        }
        .accessibilityElement(children: .combine)
    }

    /// Now, the run-out time and the reset time, each under its point, kept inside the chart and apart from each other.
    private var labels: [(text: String, x: CGFloat, lit: Bool)] {
        Self.place(plot, width: width).map { ($0.text, $0.x, $0.lit) }
    }

    /// The labels by importance: the red run-out time first, then the reset, then Now. A label that would touch one already
    /// placed (both widths and a 6 pt gap) is left out, so the run-out is never the one dropped.
    nonisolated static func place(_ plot: WindowPlot, width: CGFloat) -> [(text: String, x: CGFloat, size: CGFloat, lit: Bool)] {
        let scaleX = width / DetailChart.width
        var placed: [(text: String, x: CGFloat, size: CGFloat, lit: Bool)] = []
        let wanted: [(String?, Double?, Bool)] = [
            (plot.labels.runOut, plot.runOut?.x, true), (plot.labels.reset, DetailChart.width, false), (plot.labels.now, plot.now?.x, false)
        ]
        for (text, x, lit) in wanted {
            guard let text, let x else { continue }
            let size = Styles.dayMark.width(of: text)
            let left = min(max(CGFloat(x) * scaleX - size / 2, 0), width - size)
            let overlaps = placed.contains { other in
                let otherEnd: CGFloat = other.x + other.size + 6
                let end: CGFloat = left + size + 6
                return left < otherEnd && other.x < end
            }
            if overlaps {
                continue
            }
            placed.append((text, left, size, lit))
        }
        return placed.sorted { $0.x < $1.x }
    }
}
