import AppKit
import Foundation
import SwiftUI
import Testing
@testable import Lifeline
@testable import LifelineKit

private func staleInUse(_ name: String) throws -> ServiceState {
    var state = try Fixture.state(name)
    let active = try #require(state.mac?.activeUuid)
    let rows = state.mac?.rows?.map { row in
        var row = row
        if row.uuid == active {
            row.stale = true
        }
        return row
    }
    state.mac?.rows = rows
    return state
}

@MainActor
@Suite(.serialized)
struct StaleLineTests {
    @Test func aStaleReadingDimsTheMenuBarLineAndTheHeroLine() throws {
        let fresh = try Fixture.state("moment-menu")
        #expect(!IconFields.from(fresh).stale)
        #expect(MenuBarIconView.lineStrength(IconFields.from(fresh)) == 1)
        #expect(HeroView.staleStrength(try #require(fresh.mac)) == 1)
        let stale = try staleInUse("moment-menu")
        #expect(IconFields.from(stale).stale)
        #expect(MenuBarIconView.lineStrength(IconFields.from(stale)) == 0.45)
        #expect(HeroView.staleStrength(try #require(stale.mac)) == 0.45)
    }
}

@MainActor
@Suite(.serialized)
struct LaterTests {
    @Test func laterOnASignInWarningAsksForOneReminderAndSaysWhen() async throws {
        let body = try Fixture.text("moment-menu")
        let server = try FakeServer { head, _ in
            if head.hasPrefix("POST /notifications/later") {
                return [okReply(#"{"reminder": "2026-12-15T10:00:00Z", "message": "Lifeline reminds you again on 15 December at 10:00."}"#)]
            }
            return [okReply(body)]
        }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        let notifier = Notifier(store: store, shown: { _ in nil }, open: {}, defaults: nil)
        notifier.begin(RecordingPoster(), permission: .allowed)
        await store.refresh()
        notifier.respond(action: Notices.laterAction, info: [:], id: "signin|mac|\(Fixture.alex)|2026-12-16")
        await waitFor { store.note != nil }
        let sent = server.requests.filter { $0.head.hasPrefix("POST /notifications/later ") }
        #expect(sent.count == 1)
        let request = try #require(sent.first)
        let object = try #require(try JSONSerialization.jsonObject(with: request.body) as? [String: String])
        #expect(object == ["id": "signin|mac|\(Fixture.alex)|2026-12-16"])
        #expect(store.note == "Lifeline reminds you again on 15 December at 10:00.")
    }

    @Test func laterOnAnyOtherAlertJustLetsItGo() async throws {
        let body = try Fixture.text("moment-menu")
        let server = try FakeServer { head, _ in
            if head.hasPrefix("POST /notifications/later") {
                return [okReply(#"{"reminder": null, "message": null}"#)]
            }
            return [okReply(body)]
        }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        let notifier = Notifier(store: store, shown: { _ in nil }, open: {}, defaults: nil)
        notifier.begin(RecordingPoster(), permission: .allowed)
        await store.refresh()
        notifier.respond(action: Notices.laterAction, info: [:], id: "limit-1")
        await waitFor { server.requests.contains { $0.head.hasPrefix("POST /notifications/later ") } }
        await store.refresh()
        #expect(store.note == nil)
    }
}

struct ContinuousCornerTests {
    @Test func everyRoundedShapeUsesContinuousCornersAndTheRadiiStayConcentric() throws {
        let sources = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources")
        let walker = FileManager.default.enumerator(at: sources, includingPropertiesForKeys: nil)
        var shapes = 0
        while let file = walker?.nextObject() as? URL {
            guard file.pathExtension == "swift", file.lastPathComponent != "Samples.swift" else { continue }
            let text = try String(contentsOf: file, encoding: .utf8)
            #expect(!text.contains(".circular"), "\(file.lastPathComponent) still draws a circular corner")
            for line in text.split(separator: "\n") where line.contains("RoundedRectangle(") {
                shapes += 1
                #expect(line.contains("style: .continuous"), "\(file.lastPathComponent): \(line.trimmingCharacters(in: .whitespaces))")
            }
        }
        #expect(shapes > 20)
        #expect(Metrics.panelRadius == 20 && Metrics.innerRadius == 10 && Metrics.pairRadius == 7)
    }
}

@MainActor
@Suite(.serialized)
struct MinorViewTests {
    init() {
        Fonts.register(folder: URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Resources/Fonts", isDirectory: true))
    }

    @Test func theRunOutLabelIsKeptWhenItMeetsNow() {
        let now = ChartPoint(100, 20)
        let out = ChartPoint(104, 0)
        let plot = WindowPlot(
            start: Date(), points: [now], forecast: [now, out], now: now, runOut: out,
            labels: WindowLabels(now: "Now", runOut: "16:20", reset: "17:30")
        )
        let placed = WindowChartView.place(plot, width: 312)
        #expect(placed.contains { $0.text == "16:20" && $0.lit }, "\(placed.map(\.text))")
        #expect(!placed.contains { $0.text == "Now" }, "\(placed.map(\.text))")
        #expect(placed.contains { $0.text == "17:30" })
        for (a, b) in zip(placed, placed.dropFirst()) {
            #expect(a.x + a.size + 6 <= b.x + 0.5, "\(a.text) touches \(b.text)")
        }
    }

    @Test func settingsShowsNoRowWithoutWordsAndQWithItsSwitchOffUntilItIsReady() throws {
        var object = try Fixture.object("moment-sheet")
        var text = object["settings_text"] as? [String: Any] ?? [:]
        text["week"] = nil
        object["settings_text"] = text
        let state = try ServiceState.decode(try JSONSerialization.data(withJSONObject: object))
        let rows = SettingsPage.sections(state: state, local: LocalStatus(notices: .allowed, shortcut: .on, login: .on)).flatMap(\.rows)
        #expect(!rows.contains { $0.id == "week" }, "a row with no words and a live switch")
        #expect(rows.allSatisfy { !$0.title.isEmpty })
        let bare = try ServiceState.decode(Data(#"{"version": 1}"#.utf8))
        #expect(SettingsPage.lead(bare) == Sentences.unreadable, "a page with no core words says why it is empty")
    }

    @Test func everySettingsTitleIsDrawnOnOneLineAtTheDefaultSize() throws {
        let state = try Fixture.state("moment-sheet")
        let local = LocalStatus(notices: .banners, shortcut: .taken, login: .needsApproval)
        let view = SettingsView(state: state, local: local, problem: nil, width: CGFloat(SheetSizing.settingsDefault.width))
            .environment(\.colorScheme, .light)
        let lines = drawnLines(view) { $0.count > 20 }
        for row in SettingsPage.sections(state: state, local: local).flatMap(\.rows) {
            #expect(lines.contains(row.title), "\(row.id)'s title is not one drawn line: \(row.title)")
        }
    }

    @Test func theWaitingBoxAlwaysHasWords() throws {
        let state = try Fixture.state("devbox-work-step2")
        var run = AddRun(target: .both, kind: .add, stages: [.macAdd, .devboxAdd])
        run.began("m1", at: Date(timeIntervalSince1970: 5_000))
        _ = run.advance(state: state, now: Date(timeIntervalSince1970: 5_001))
        let stage = try #require(run.current)
        run.began("not-yet", at: Date(timeIntervalSince1970: 5_002))
        let lines = AddFlow.waitingLines(stage, state: state, job: nil, run: run)
        #expect(lines.first == "Lifeline added Work Account.", "\(lines)")
        for signIn in [AddStage.devboxAdd, .macAdd] {
            let words = AddFlow.waitingLines(signIn, state: state, job: nil, run: run)
            #expect(words.first == "Lifeline added Work Account.", "\(signIn): \(words)")
        }
        #expect(JobFlow.cancelling(nil) == Sentences.notRunning)
        let added = try Fixture.state("after-mac-add-org")
        let account = try #require(added.accounts?.first { $0.uuid == added.jobs?.first?.uuid })
        #expect(AddFlow.added(name: "", account, state: added, job: nil) == "\(account.name ?? "") is added.")
    }
}
