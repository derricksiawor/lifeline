import AppKit
import Foundation
import SwiftUI
import Testing
@testable import Lifeline
@testable import LifelineKit

private func account(_ name: String, _ uuid: String) throws -> AccountSummary {
    try #require(try Fixture.state(name).accounts?.first { $0.uuid == uuid })
}

private let utc = TimeZone(identifier: "UTC")!

@MainActor
struct OpenRowLinesTests {
    @Test func theRowTheHeroShowsLeavesItsUsageToTheHero() throws {
        let alex = try account("moment-menu", Fixture.alex)
        let usage = try #require(alex.detail?.usage)
        let shown = Detail.openRow(alex, reply: nil, heroShown: true)
        #expect(!shown.lines.contains(usage), "the open row repeats the hero: \(shown.lines)")
        let work = try account("moment-menu", Fixture.work)
        #expect(Detail.openRow(work, reply: nil, heroShown: false).lines.contains(try #require(work.detail?.usage)))
    }

    @Test func anOpenRowIsAFewShortLines() throws {
        let reply = HistoryReply(summary: "It ran out of its 5 hours on Saturday and Monday.")
        for uuid in [Fixture.alex, Fixture.work, Fixture.personal, Fixture.org] {
            let item = try account("moment-menu", uuid)
            let hero = uuid == Fixture.alex
            let text = Detail.openRow(item, reply: reply, heroShown: hero)
            #expect(text.lines.count <= 4, "\(item.name ?? ""): \(text.lines)")
            #expect(text.lines.contains(reply.summary ?? "-"), "\(text.lines)")
            if let signin = item.signinText, !signin.isEmpty {
                #expect(text.lines.contains(signin), "the sign-in sentence is missing: \(text.lines)")
            }
            if let account = item.detail?.account, !account.isEmpty {
                #expect(!text.lines.contains(account), "the Settings sentence shows in the open row: \(text.lines)")
            }
        }
    }

    @Test func projectsShowOnlyOnTheRowInUse() throws {
        let alex = try account("moment-menu", Fixture.alex)
        let projects = try #require(alex.detail?.projects)
        try #require(!projects.isEmpty)
        #expect(Detail.openRow(alex, reply: nil, heroShown: true).lines.contains(projects))
        #expect(!Detail.openRow(alex, reply: nil, heroShown: false).lines.contains(projects))
    }
}

@MainActor
struct WindowChartTests {
    private func readings(_ values: [(Int, Double)], reset: String) -> [HistoryReading] {
        values.map { minutes, five in
            HistoryReading(source: "usage", five: five, fiveReset: reset, at: Timestamps.parse("2026-11-18T12:00:00Z").map {
                ISO8601DateFormatter().string(from: $0.addingTimeInterval(Double(minutes) * 60))
            })
        }
    }

    @Test func theWindowChartDrawsTheLineSoFarTheForecastAndTheMarks() throws {
        var work = try account("moment-menu", Fixture.work)
        let now = Timestamps.parse("2026-11-18T15:05:00Z")!
        work.fiveReset = "2026-11-18T17:30:00Z"
        let history = readings([(40, 10), (80, 25), (120, 40), (150, 55)], reset: "2026-11-18T17:30:00Z")
        let plot = try #require(WindowChart.plot(readings: history, account: work, now: now, zone: utc))
        #expect(plot.points.count >= 4)
        #expect(plot.points == plot.points.sorted { $0.x < $1.x })
        let nowMark = try #require(plot.now)
        #expect(abs(nowMark.x - WindowChart.x(now, start: plot.start)) < 0.01)
        #expect(plot.labels.now == "Now")
        #expect(plot.labels.reset == "17:30")
        let out = try #require(plot.runOut, "Work's 5 hours run out at 16:20")
        #expect(plot.labels.runOut == "16:20")
        #expect(abs(out.y - DetailChart.weekY(100)) < 0.01)
        #expect(plot.forecast.last == out)
        #expect(plot.forecast.first == nowMark)
    }

    @Test func aWindowThatLastsShowsTheForecastToTheReset() throws {
        let alex = try account("moment-menu", Fixture.alex)
        let now = Timestamps.parse("2026-11-18T15:05:00Z")!
        let plot = try #require(WindowChart.plot(readings: [], account: alex, now: now, zone: utc))
        #expect(plot.runOut == nil)
        #expect(plot.labels.runOut == nil)
        #expect(plot.labels.reset == "17:00")
        #expect(abs((plot.forecast.last?.x ?? 0) - DetailChart.width) < 0.01)
        #expect(abs((plot.forecast.last?.y ?? 0) - DetailChart.weekY(71)) < 0.01, "Alex reaches 71% at the reset")
    }

    @Test func anAccountWithoutAFiveHourWindowHasNoWindowChart() throws {
        let personal = try account("moment-menu", Fixture.personal)
        #expect(WindowChart.plot(readings: [], account: personal, now: Date(), zone: utc) == nil)
    }

    @Test func theSevenDayChartUsesTheFullScaleAt40Points() {
        #expect(Sparkline.height == 40)
        #expect(WindowChart.height == 40)
        let gap = (DetailChart.weekY(47) - DetailChart.weekY(100)) * Double(Sparkline.height) / DetailChart.weekHeight
        #expect(gap >= 17, "a 47% day and a 100% day are \(gap) pt apart")
    }
}

@MainActor
@Suite(.serialized)
struct OpenRowDrawnTests {
    private func drawn(_ uuid: String, side: MenuSide = .mac) throws -> [String] {
        let state = try Fixture.state("moment-menu")
        let account = try #require(state.accounts?.first { $0.uuid == uuid })
        let row = try #require(state.model(for: side)?.rows?.first { $0.uuid == uuid })
        let reply = HistoryReply(daily: [], summary: "It ran out of its 5 hours on Saturday and Monday.")
        return drawnLines(AccountExpansion(
            account: account, row: row, state: state, history: .ready(reply), now: Timestamps.parse("2026-11-18T15:05:00Z")!,
            zone: TimeZone(identifier: "UTC")!, side: side, handlers: DetailHandlers(), palette: .dark
        ))
    }

    @Test func theOpenRowOfTheHerosAccountDrawsNoUsageAndTheOthersDo() throws {
        let state = try Fixture.state("moment-menu")
        let alexUsage = try #require(state.accounts?.first { $0.uuid == Fixture.alex }?.detail?.usage)
        let workUsage = try #require(state.accounts?.first { $0.uuid == Fixture.work }?.detail?.usage)
        let alex = try drawn(Fixture.alex).joined(separator: " ")
        let work = try drawn(Fixture.work).joined(separator: " ")
        #expect(!alex.contains(String(alexUsage.prefix(30))), "the open row repeats the hero: \(alex)")
        #expect(work.contains(String(workUsage.prefix(30))), "\(work)")
        #expect(work.contains("Now") && work.contains("16:20") && work.contains("17:30"), "the window chart's marks are missing: \(work)")
    }
}
