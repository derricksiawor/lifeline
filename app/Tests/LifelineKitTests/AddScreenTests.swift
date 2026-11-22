import AppKit
import Foundation
import SwiftUI
import Testing
@testable import Lifeline
@testable import LifelineKit

private final class LightCount: @unchecked Sendable {
    private let lock = NSLock()
    private var stored = 0

    var count: Int {
        get { lock.withLock { stored } }
        set { lock.withLock { stored = newValue } }
    }
}

/// How many travelling lights a view draws.
@MainActor
private func drawnLights(_ view: some View) -> Int {
    NSApplication.shared.setActivationPolicy(.prohibited)
    let box = LightCount()
    let root = view
        .environment(\.menuMotion, Motion(MotionPlan(reduceMotion: true)))
        .environment(\.reportsText, true)
        .onPreferenceChange(DrawnLight.self) { count in box.count = count }
    let host = NSHostingView(rootView: root)
    host.frame = NSRect(origin: .zero, size: host.fittingSize)
    let window = NSWindow(contentRect: NSRect(x: -30000, y: -30000, width: host.frame.width, height: host.frame.height), styleMask: [.borderless], backing: .buffered, defer: false)
    window.isReleasedWhenClosed = false
    window.contentView = host
    defer { window.contentView = nil }
    for _ in 0..<60 {
        host.layoutSubtreeIfNeeded()
        RunLoop.main.run(until: Date().addingTimeInterval(0.01))
    }
    return box.count
}

private func filled(_ template: String?, _ pairs: [String: String]) -> String? {
    guard var text = template else { return nil }
    for (key, value) in pairs {
        text = text.replacingOccurrences(of: "{" + key + "}", with: value)
    }
    return text
}

@MainActor
@Suite(.serialized)
struct AddScreensFromTheCoreTests {
    init() {
        Fonts.register(folder: URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Resources/Fonts", isDirectory: true))
    }

    @Test func theAddScreenSentencesAreTheCoresOwn() throws {
        let state = try Fixture.state("devbox-work-unadded")
        let text = try #require(state.addText)
        #expect(AddFlow.sheetTitle(state) == text.title)
        #expect(AddFlow.lead(state) == text.lead)
        #expect(AddFlow.explain(state, pick: nil) == text.explain)
        let known = try #require(state.knownAccounts?.first)
        #expect(AddFlow.pickLine(known) == known.pickLine)
        #expect(AddFlow.explain(state, pick: known) == known.explain)
        #expect(AddFlow.twice(state) == text.twice)
        #expect(JobFlow.devboxAway(state) == text.devboxAway)
        #expect(AddFlow.stays(state) == text.stays)
        #expect(AddFlow.failed(state) == text.failed)
        #expect(AddFlow.lost(state) == text.lost)
        #expect(AddFlow.pageNotOpenedAbove(state) == text.pageNotOpenedAbove)
        #expect(JobFlow.cancelled(state) == text.cancelled)
        #expect(JobFlow.cancelling(state) == text.cancelling)
        #expect(AddFlow.slowAnswer(state) == filled(text.slowAnswer, ["seconds": String(Int(AddFlow.startLimit))]))
        #expect(AddFlow.slowAnswer(nil) == Sentences.slowStart, "with no state the service-down sentence stays in Swift")
    }

    @Test func aPersonalAccountIsChosenByItsPersonalMark() throws {
        let state = try Fixture.state("devbox-work-unadded")
        let known = try #require(state.knownAccounts?.first { $0.isPersonal })
        let explain = AddFlow.explain(state, pick: known)
        #expect(explain.contains("choose the one marked Personal"), "\(explain)")
        #expect(!explain.contains("'s Organization"), "\(explain)")
        let resign = try #require(try Fixture.state("job-cards").jobs?.first { $0.kind == "resign" })
        #expect(JobFlow.guidance(resign, state: nil).joined(separator: " ").contains("marked Personal"))
    }

    @Test func waitingAndResultSentencesAreTheJobsOwn() throws {
        let cards = try Fixture.state("job-cards")
        for job in cards.jobs ?? [] {
            #expect(JobFlow.waiting(job) == (job.waiting ?? ""), "\(job.id)")
            #expect(JobFlow.label(job, state: cards) == (job.label ?? ""), "\(job.id)")
            #expect(JobFlow.guidance(job, state: cards) == (job.lines ?? []), "\(job.id)")
            #expect(JobFlow.cardLines(job, state: cards) == (job.cardLines ?? []), "\(job.id)")
        }
        let failed = try Fixture.state("failed-jobs")
        for job in failed.jobs ?? [] {
            #expect(JobFlow.resultSentence(job, state: failed) == job.resultHead, "\(job.id)")
            #expect(JobFlow.title(job, state: failed) == job.title, "\(job.id)")
            let parts = AddFlow.headline(job.result ?? "", state: failed)
            #expect(parts.head == job.resultHead && parts.rest == (job.resultRest ?? ""), "\(job.id): \(parts)")
        }
    }

    @Test func aNameWithAFullStopStaysWholeOnTheResultLine() throws {
        let result = "Lifeline already has Dr. Lee Account. Safari signed in as lee@example.com, so nothing changed."
        let text = #"{"version": 1, "jobs": [{"id": "f", "kind": "add", "state": "failed", "result": "\#(result)", "result_head": "Lifeline already has Dr. Lee Account.", "result_rest": "Safari signed in as lee@example.com, so nothing changed."}]}"#
        let state = try ServiceState.decode(Data(text.utf8))
        let parts = AddFlow.headline(result, state: state)
        #expect(parts.head == "Lifeline already has Dr. Lee Account.", "\(parts)")
        #expect(parts.rest == "Safari signed in as lee@example.com, so nothing changed.")
    }

    @Test func theNamingScreenDrawsTheCoresWords() throws {
        let state = try Fixture.state("after-mac-add-org")
        let text = try #require(state.addText)
        let job = try #require(state.jobs?.first)
        let account = try #require(state.accounts?.first { $0.uuid == job.uuid })
        #expect(AddFlow.added(name: "Team", account, state: state, job: job) == filled(text.added, ["name": "Team"]))
        #expect(AddFlow.added(name: " ", account, state: state, job: job) == job.addedHead)
        #expect(AddFlow.addedLine(account, run: nil) == account.addedText?.mac)
        #expect(SheetText.namesShown(menu: "A", bar: "B", text: text) == filled(text.namesShown, ["menu": "A", "bar": "B"]))
        #expect(SheetText.namesShown(menu: "A", bar: "B", barHidden: true, text: text) == filled(text.namesShownHidden, ["menu": "A", "bar": "B"]))
    }

    @Test func theStartScreenGivesItsInstructionsBeforeTheButton() throws {
        let state = try Fixture.state("devbox-work-unadded")
        let text = try #require(state.addText)
        let view = AddAccountView(state: state, step: .start, target: .both, problem: nil, zone: TimeZone(identifier: "UTC")!, width: 560)
            .environment(\.colorScheme, .light)
        let lines = drawnLines(view) { $0.contains("Open the sign-in page") }
        func at(_ found: (String) -> Bool, _ what: String) -> Int {
            let index = lines.firstIndex(where: found)
            #expect(index != nil, "\(what) is not drawn: \(lines)")
            return index ?? -1
        }
        let lead = at({ text.lead?.hasPrefix($0) == true && !$0.isEmpty }, "the lead")
        let which = at({ $0.uppercased() == AddFlow.whichAccount.uppercased() }, "Which account")
        let where_ = at({ $0.uppercased() == "ADD IT TO" }, "Add it to")
        let twice = at({ text.twice?.hasPrefix($0) == true && $0.count > 8 }, "Studio note")
        let explain = at({ text.explain?.hasPrefix($0) == true && $0.count > 8 }, "the browser sentence")
        let button = at({ $0 == "Open the sign-in page" }, "the button")
        #expect(lead < which && which < where_ && where_ < twice && twice < explain && explain < button, "\(lines)")
    }

    @Test func theSelectedAccountStandsApartWithTheTagFillAndACheck() {
        for scheme in [ColorScheme.light, .dark] {
            let palette = Palette.of(scheme)
            let sheet = SheetPalette.of(scheme)
            let picked = AddAccountView.pickLook(selected: true, palette: palette, sheet: sheet)
            #expect(picked.fill == palette.tagBackground)
            #expect(picked.check)
            let other = AddAccountView.pickLook(selected: false, palette: palette, sheet: sheet)
            #expect(other.fill == sheet.quiet)
            #expect(!other.check)
        }
    }

    @Test func everyWaitingCardShowsTheTravellingLight() throws {
        let state = try Fixture.state("job-cards")
        let job = try #require(state.jobs?.first { $0.kind == "add" })
        let palette = Palette.of(.dark)
        #expect(drawnLights(WaitingCard(job: job, palette: palette, actions: CardActions(), state: state)) == 1)
        #expect(drawnLights(CompactWaiting(card: job, state: state)) == 1)
        let done = try #require(try Fixture.state("after-mac-add-org").jobs?.first)
        #expect(drawnLights(ResultCard(job: done, palette: palette)) == 0)
        let resting = TravellingLight.place(glides: false)
        #expect(resting.strength == 1, "with Reduce Motion the light rests at full strength")
        #expect(resting.travel >= 0 && resting.travel + TravellingLight.segment <= 1, "the resting light sits inside its track")
    }
}
