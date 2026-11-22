import Foundation

public struct HistoryReading: Codable, Sendable, Equatable {
    public var source: String?
    public var five: Double?
    public var fiveReset: String?
    public var week: Double?
    public var weekReset: String?
    public var at: String?

    public init(source: String? = nil, five: Double? = nil, fiveReset: String? = nil, week: Double? = nil, weekReset: String? = nil, at: String? = nil) {
        self.source = source
        self.five = five
        self.fiveReset = fiveReset
        self.week = week
        self.weekReset = weekReset
        self.at = at
    }
}

public struct DailyPeak: Codable, Sendable, Equatable {
    public var date: String
    public var peakFive: Double?
    public var hitLimit: Bool?

    public init(date: String, peakFive: Double? = nil, hitLimit: Bool? = nil) {
        self.date = date
        self.peakFive = peakFive
        self.hitLimit = hitLimit
    }
}

public struct OpenRowText: Equatable, Sendable {
    public var lines: [String]
    public var chartAfter: Int
    public var sparkline: Bool

    public init(lines: [String], chartAfter: Int, sparkline: Bool) {
        self.lines = lines
        self.chartAfter = chartAfter
        self.sparkline = sparkline
    }
}

public struct HistoryReply: Codable, Sendable, Equatable {
    public var uuid: String?
    public var days: Int?
    public var readings: [HistoryReading]?
    public var daily: [DailyPeak]?
    public var peaksOf: String?
    public var weekRanOut: [String]?
    public var summary: String?

    public init(
        uuid: String? = nil, days: Int? = nil, readings: [HistoryReading]? = nil, daily: [DailyPeak]? = nil, peaksOf: String? = nil,
        weekRanOut: [String]? = nil, summary: String? = nil
    ) {
        self.summary = summary
        self.uuid = uuid
        self.days = days
        self.readings = readings
        self.daily = daily
        self.peaksOf = peaksOf
        self.weekRanOut = weekRanOut
    }

    public static func decode(_ data: Data) throws -> HistoryReply {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(HistoryReply.self, from: data)
    }
}

public struct ChartPoint: Sendable, Equatable {
    public let x: Double
    public let y: Double

    public init(_ x: Double, _ y: Double) {
        self.x = x
        self.y = y
    }
}

public struct WeekPlot: Sendable, Equatable {
    public var points: [ChartPoint?]
    public var limits: [ChartPoint]
    public var days: [String]
    public var limitDays: [String]

    public init(points: [ChartPoint?], limits: [ChartPoint], days: [String], limitDays: [String]) {
        self.points = points
        self.limits = limits
        self.days = days
        self.limitDays = limitDays
    }
}

public enum DetailChart {
    public static func dayLabel(_ name: String) -> String {
        String(name.prefix(3))
    }

    public static let width = 424.0
    public static let weekHeight = 80.0

    static func tenth(_ value: Double) -> Double {
        (value * 10).rounded() / 10
    }

    public static func weekX(_ index: Int) -> Double {
        tenth(Double(index) * (width / 6))
    }

    public static func weekY(_ percent: Double) -> Double {
        tenth(74 - min(max(percent, 0), 100) * 0.66)
    }

    public static func week(daily: [DailyPeak], now: Date, zone: TimeZone) -> WeekPlot {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = zone
        let today = calendar.startOfDay(for: now)
        let key = DateFormatter()
        key.locale = Locale(identifier: "en_US_POSIX")
        key.timeZone = zone
        key.dateFormat = "yyyy-MM-dd"
        let short = Detail.formatter("EEE", zone)
        let long = Detail.formatter("EEEE", zone)
        var byDate: [String: DailyPeak] = [:]
        for day in daily {
            byDate[day.date] = day
        }
        var points: [ChartPoint?] = []
        var limits: [ChartPoint] = []
        var days: [String] = []
        var limitDays: [String] = []
        for index in 0..<7 {
            guard let date = calendar.date(byAdding: .day, value: index - 6, to: today) else {
                points.append(nil)
                days.append("")
                continue
            }
            days.append(short.string(from: date))
            guard let found = byDate[key.string(from: date)], let peak = found.peakFive, peak.isFinite else {
                points.append(nil)
                continue
            }
            let point = ChartPoint(weekX(index), weekY(peak))
            points.append(point)
            if found.hitLimit ?? (peak >= 100) {
                limits.append(point)
                limitDays.append(long.string(from: date))
            }
        }
        return WeekPlot(points: points, limits: limits, days: days, limitDays: limitDays)
    }
}

public struct WindowLabels: Sendable, Equatable {
    public var now: String
    public var runOut: String?
    public var reset: String
}

public struct WindowPlot: Sendable, Equatable {
    public var start: Date
    public var points: [ChartPoint]
    public var forecast: [ChartPoint]
    public var now: ChartPoint?
    public var runOut: ChartPoint?
    public var labels: WindowLabels
}

/// The current 5-hour window: the line so far, a faint forecast to the run-out or the reset, a Now mark, and a red
/// mark where it runs out. Same 0 to 100 scale as the 7-day chart.
public enum WindowChart {
    public static let span: TimeInterval = 5 * 3600
    public static let height = 40.0

    public static func x(_ date: Date, start: Date) -> Double {
        DetailChart.tenth(min(max(date.timeIntervalSince(start) / span, 0), 1) * DetailChart.width)
    }

    public static func plot(readings: [HistoryReading], account: AccountSummary, now: Date, zone: TimeZone) -> WindowPlot? {
        guard let text = account.fiveReset, let reset = Timestamps.parse(text), reset > now, let five = account.five, five.isFinite else {
            return nil
        }
        let start = reset.addingTimeInterval(-span)
        var points: [ChartPoint] = readings.compactMap { reading in
            guard let value = reading.five, value.isFinite, let at = reading.at.flatMap(Timestamps.parse), at >= start, at <= now,
                  let ends = reading.fiveReset.flatMap(Timestamps.parse), abs(ends.timeIntervalSince(reset)) < 120 else {
                return nil
            }
            return ChartPoint(x(at, start: start), DetailChart.weekY(value))
        }
        .sorted { $0.x < $1.x }
        let here = ChartPoint(x(now, start: start), DetailChart.weekY(five))
        points.removeAll { $0.x > here.x }
        points.append(here)
        let side = Detail.fiveSide(of: account)
        let out = Detail.runOut(side, before: reset, now: now)
        let end: ChartPoint
        if let out {
            end = ChartPoint(x(out, start: start), DetailChart.weekY(100))
        } else {
            end = ChartPoint(DetailChart.width, DetailChart.weekY(min(max(side.forecast?.atResetPct ?? five, five), 100)))
        }
        return WindowPlot(
            start: start, points: points, forecast: [here, end], now: here, runOut: out == nil ? nil : end,
            labels: WindowLabels(now: "Now", runOut: out.map { Clock.text($0, "HH:mm", zone) }, reset: Clock.text(reset, "HH:mm", zone))
        )
    }
}

public enum Clock {
    public static func rounded(_ date: Date) -> Date {
        Date(timeIntervalSince1970: (date.timeIntervalSince1970 / 60).rounded() * 60)
    }

    public static func text(_ date: Date, _ format: String, _ zone: TimeZone) -> String {
        Detail.formatter(format, zone).string(from: rounded(date))
    }

    public static func daysAhead(_ date: Date, now: Date, zone: TimeZone) -> Int {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = zone
        let from = calendar.startOfDay(for: now)
        let to = calendar.startOfDay(for: rounded(date))
        return calendar.dateComponents([.day], from: from, to: to).day ?? 0
    }

    public static func date(_ date: Date, now: Date, zone: TimeZone) -> String {
        let days = daysAhead(date, now: now, zone: zone)
        switch days {
        case 0:
            return "today"
        case 1:
            return "tomorrow"
        case 2...6:
            return text(date, "EEEE", zone)
        case 7:
            return "next \(text(date, "EEEE", zone))"
        default:
            return text(date, "d\u{00A0}MMMM", zone)
        }
    }

    public static func phrase(_ date: Date, now: Date, zone: TimeZone) -> String {
        whenever(date, now: now, zone: zone, on: "on ")
    }

    public static func until(_ date: Date, now: Date, zone: TimeZone) -> String {
        whenever(date, now: now, zone: zone, on: "")
    }

    static func whenever(_ date: Date, now: Date, zone: TimeZone, on: String) -> String {
        let time = text(date, "HH:mm", zone)
        let days = daysAhead(date, now: now, zone: zone)
        switch days {
        case 0:
            return "today at \(time)"
        case 1:
            return "tomorrow at \(time)"
        case 2...6:
            return "\(on)\(text(date, "EEEE", zone)) at \(time)"
        case 7:
            return "next \(text(date, "EEEE", zone)) at \(time)"
        default:
            return "\(on)\(text(date, "d\u{00A0}MMMM", zone)) at \(time)"
        }
    }
}

public enum DetailButton: Sendable, Equatable {
    case useHere(String)
    case inUseHere
    case needsSignIn
    case hidden
}

public enum Detail {
    public static let chartMinimumDays = 3

    static func formatter(_ format: String, _ zone: TimeZone) -> DateFormatter {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = zone
        formatter.dateFormat = format
        return formatter
    }

    static func whole(_ value: Double?) -> Int {
        guard let value, value.isFinite, value > 0 else { return 0 }
        return Int(value.rounded(.down))
    }

    public static func runOut(_ account: AccountSummary, before reset: Date, now: Date) -> Date? {
        guard let text = account.forecast?.runsOutAt, let out = Timestamps.parse(text), out < reset, out > now else {
            return nil
        }
        return out
    }

    public static func secondInRow(_ row: MenuRow, account: AccountSummary?, now: Date) -> Bool {
        guard row.hasSecond else { return false }
        let other = row.secondWindow == "five" ? "five" : "week"
        let used = account.flatMap { other == "five" ? $0.five : $0.week } ?? Fraction.of(row.secondW) * 100
        if used >= 80 {
            return true
        }
        guard let account else { return false }
        let otherSide = other == "five" ? fiveSide(of: account) : weekSide(of: account)
        let mainSide = other == "five" ? weekSide(of: account) : fiveSide(of: account)
        let otherReset = (other == "five" ? account.fiveReset : account.weekReset).flatMap(Timestamps.parse)
        let mainReset = (other == "five" ? account.weekReset : account.fiveReset).flatMap(Timestamps.parse)
        guard let otherReset, let otherOut = runOut(otherSide, before: otherReset, now: now) else { return false }
        guard let mainReset, let mainOut = runOut(mainSide, before: mainReset, now: now) else { return true }
        return otherOut < mainOut
    }

    public static func fiveSide(of account: AccountSummary) -> AccountSummary {
        var five = account
        five.forecast = account.binding == "week" ? account.otherForecast : account.forecast
        five.otherForecast = nil
        five.binding = "five"
        return five
    }

    public static func weekSide(of account: AccountSummary) -> AccountSummary {
        var week = account
        week.forecast = account.binding == "week" ? account.forecast : account.otherForecast
        week.otherForecast = nil
        week.binding = "week"
        return week
    }

    public static func whoPart(_ text: String?) -> String {
        let trimmed = (text ?? "").trimmingCharacters(in: .whitespaces)
        guard let end = trimmed.range(of: ". ") else { return trimmed }
        return String(trimmed[..<end.lowerBound]) + "."
    }

    public static func readingDays(_ daily: [DailyPeak]) -> [String] {
        daily.filter { $0.peakFive?.isFinite == true }.map(\.date).sorted()
    }

    static func day(_ text: String?, _ zone: TimeZone, now: Date = Date()) -> String? {
        guard let text, let date = Timestamps.parse(text) else { return nil }
        return Clock.until(date, now: now, zone: zone)
    }

    public static func button(_ account: AccountSummary, macActive: String?, side: MenuSide = .mac) -> DetailButton {
        if side == .dev {
            return .hidden
        }
        if account.uuid == macActive {
            return .inUseHere
        }
        if account.needsSignin == true {
            return .needsSignIn
        }
        return .useHere(account.uuid)
    }

    public static func signingIn(_ uuid: String, state: ServiceState?) -> Bool {
        state?.jobs?.contains { $0.kind == "resign" && $0.uuid == uuid && $0.state == "running" } == true
    }

    public static func step(from uuid: String?, by offset: Int, rows: [MenuRow]) -> String? {
        let ids = rows.map(\.uuid)
        guard !ids.isEmpty else { return nil }
        guard let uuid, let index = ids.firstIndex(of: uuid) else { return ids.first }
        return ids[min(max(index + offset, 0), ids.count - 1)]
    }

    public static func whereLine(_ row: MenuRow, open: Bool) -> String {
        open ? whoPart(row.where) : (row.where ?? "").trimmingCharacters(in: .whitespaces)
    }

    /// The open row's few short lines (Gaps 5 and 6): the usage sentence unless the hero above already says it, the 7 days,
    /// one sign-in sentence, and the open projects only on the row in use. The row above already says when a reading is old.
    public static func openRow(_ account: AccountSummary, reply: HistoryReply?, heroShown: Bool = false) -> OpenRowText {
        func clean(_ text: String?) -> String? {
            let trimmed = (text ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            return trimmed.isEmpty ? nil : trimmed
        }
        let before = [heroShown ? nil : account.detail?.usage, account.detail?.models].compactMap(clean)
        let after = [reply?.summary, account.signinText, heroShown ? account.detail?.projects : nil].compactMap(clean)
        return OpenRowText(lines: before + after, chartAfter: before.count, sparkline: readingDays(reply?.daily ?? []).count >= chartMinimumDays)
    }
}

