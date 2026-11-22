import AppKit
import Foundation
import SwiftUI
import Testing
import Vision
@testable import Lifeline
@testable import LifelineKit

private let fullMenu = #"""
{"version": 1,
 "mac": {"view": "mac", "view_label": "This Mac", "sessions": "3 sessions open", "hero_name": "Alex's Account", "hero_plan": "Max 5x",
         "head": "Alex's Account lasts until it resets today at 17:00.", "sub": "At this pace it ends the window 71% used.",
         "hero_w": "42%", "fore_w": "29%", "hero_tone": "calm", "hero_used": "42% used", "hero_reset": "Resets today at 17:00",
         "second_text": "It has used 18% of its week. Its week resets on Sunday at 09:00.", "second_w": "18%", "second_window": "week",
         "action_kind": "none", "hint": "",
         "rows": [
           {"uuid": "j", "name": "Alex's Account", "pct": "42%", "five_w": "42%", "tone": "calm", "is_active": true, "where": "Used by this Mac. Resets today at 17:00."},
           {"uuid": "c", "name": "Work Account", "pct": "94%", "five_w": "94%", "tone": "low", "where": "Not in use. Resets on Sunday at 09:00."},
           {"uuid": "p", "name": "Personal Account", "pct": "76%", "five_w": "76%", "tone": "calm", "where": "Not in use. Resets on 26 November at 02:00."},
           {"uuid": "o", "name": "Team Account", "pct": "9%", "five_w": "9%", "tone": "calm", "where": "Not in use. Resets on Sunday at 09:00."}]},
 "accounts": [{"uuid": "j", "name": "Alex's Account", "short": "Alex", "number": 1}, {"uuid": "c", "name": "Work Account", "short": "Work", "number": 2},
              {"uuid": "p", "name": "Personal Account", "short": "Personal", "number": 3}, {"uuid": "o", "name": "Team Account", "short": "Team", "number": 4}],
 "jobs": JOBS}
"""#

private func sixResults() -> String {
    let sentence = "Your browser signed in as Personal Account (sam@example.org), which Lifeline already has. Nothing changed."
    let jobs = (1...6).map { #"{"id": "f\#($0)", "kind": "add", "state": "failed", "result": "\#(sentence)"}"# }.joined(separator: ", ")
    return fullMenu.replacingOccurrences(of: "JOBS", with: "[" + jobs + "]")
}

@MainActor
@Suite(.serialized)
struct CardsSeenOnlyWhenInViewTests {
    @Test func onlyCardsInsideTheVisibleCardsAreaReport() {
        let frames: [String: CGRect] = [
            "a": CGRect(x: 0, y: 0, width: 300, height: 100),
            "b": CGRect(x: 0, y: 108, width: 300, height: 100),
            "c": CGRect(x: 0, y: 216, width: 300, height: 100),
            "d": CGRect(x: 0, y: -60, width: 300, height: 100)
        ]
        #expect(MenuLogic.visibleCards(frames: frames, viewport: 210) == ["a", "b"])
        #expect(MenuLogic.visibleCards(frames: ["tall": CGRect(x: 0, y: -20, width: 300, height: 400)], viewport: 210) == ["tall"])
    }

    @Test func sixCardsAtTheCapReportOnlyTheOnesInView() async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let state = try ServiceState.decode(Data(sixResults().utf8))
        let results = JobFlow.results(state, seen: [])
        #expect(results.count == 6)
        var shown: Set<String> = []
        let view = MenuView(state: state, side: .mac, note: nil, maxHeight: 879, results: results, cardsShown: { shown = $0 })
            .environment(\.menuMotion, Motion(MotionPlan(reduceMotion: true)))
        let host = NSHostingView(rootView: view)
        let window = NSWindow(contentRect: NSRect(x: -30000, y: -30000, width: 440, height: 900), styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = host
        for _ in 0..<8 {
            host.layoutSubtreeIfNeeded()
            try await Task.sleep(for: .milliseconds(100))
        }
        #expect(!shown.isEmpty)
        #expect(shown.count < 6, "\(shown)")
        let newest = results.reversed().map(\.id)
        #expect(Set(newest.prefix(shown.count)) == shown, "\(shown)")
        window.contentView = nil
    }

    @Test func cardsScrolledOutOfViewStayUnseen() async throws {
        let server = try FakeServer { _, _ in [okReply(sixResults())] }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        store.opened()
        store.menuOpen = true
        store.cardsShown(["f6", "f5"])
        store.markVisibleResults()
        store.panelClosed(visible: true)
        #expect(Set(store.unseenResults.map(\.id)) == ["f1", "f2", "f3", "f4"])
    }

    @Test func theCardsAreaFadesAtWhicheverEdgeHidesContent() {
        #expect(MenuLogic.listFades(offset: 0, content: 600, limit: 200) == (false, true))
        #expect(MenuLogic.listFades(offset: -200, content: 600, limit: 200) == (true, true))
        #expect(MenuLogic.listFades(offset: -400, content: 600, limit: 200) == (true, false))
    }
}

@MainActor
@Suite(.serialized)
struct RefusedOpenTests {
    @Test func aRefusedOpenFromTheCardIsKeptAndPosted() async throws {
        let sentence = "That sign-in has already finished, so there is no page to open."
        let body = fullMenu.replacingOccurrences(of: "JOBS", with: #"[{"id": "r", "kind": "resign", "uuid": "o", "state": "running"}]"#)
        let server = try FakeServer { head, _ in
            head.hasPrefix("POST /jobs/r/open") ? [okReply(#"{"error": "\#(sentence)"}"#, status: "409 Conflict")] : [okReply(body)]
        }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        let poster = RecordingPoster()
        let notifier = Notifier(store: store, shown: { _ in nil }, open: {}, defaults: nil)
        notifier.begin(poster, permission: .allowed)
        await store.refresh()
        store.opened()
        store.menuOpen = true
        store.reopenJob("r")
        store.panelClosed(visible: true)
        await waitFor { store.note == sentence }
        await waitFor { poster.posted.contains { $0.title == sentence } }
        #expect(poster.posted.contains { $0.title == sentence })
        store.opened()
        #expect(store.note == sentence)
    }

    @Test func openWaitsUpToTwentyFiveSecondsAndCancelFive() async throws {
        #expect(JobFlow.openLimit == 25)
        #expect(JobFlow.cancelLimit == 5)
        let body = fullMenu.replacingOccurrences(of: "JOBS", with: #"[{"id": "r", "kind": "resign", "uuid": "o", "state": "running"}]"#)
        let server = try FakeServer { head, _ in
            if head.hasPrefix("POST /jobs/r/open") {
                Thread.sleep(forTimeInterval: 2.5)
                return [okReply(#"{"browser": "Google Chrome", "opened": true}"#)]
            }
            return [okReply(body)]
        }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        store.opened()
        store.menuOpen = true
        store.reopenJob("r")
        try await Task.sleep(for: .milliseconds(3200))
        #expect(store.note == nil, "\(store.note ?? "")")
    }
}

@MainActor
struct DevboxCancelTests {
    @Test func aDevboxSignInOffersCancel() throws {
        #expect(JobFlow.cancellable(JobSummary(id: "1", kind: "devbox-add")))
        #expect(JobFlow.cancellable(JobSummary(id: "1", kind: "devbox-resign")))
        #expect(JobFlow.cancellable(JobSummary(id: "1", kind: "resign")))
        #expect(JobFlow.cancellable(JobSummary(id: "1", kind: "add")))
        let state = try ServiceState.decode(Data(fullMenu.replacingOccurrences(of: "JOBS", with: "[]").utf8))
        let job = JobSummary(id: "1", kind: "devbox-resign", uuid: "o", state: "running", url: "https://claude.ai/oauth/authorize?x=1", opened: true)
        let card = try ocr(WaitingCard(job: job, palette: Palette.of(.light), actions: CardActions(), state: state))
        #expect(card.dropFirst().contains { $0.hasSuffix("Cancel") }, "\(card)")
        #expect(!card.contains { $0.contains("cannot stop") }, "\(card)")
    }
}

@MainActor
@Suite(.serialized)
struct OwnCancelTests {
    @Test func aCancelTheAppSentIsSeenWhenTheServiceConfirms() async throws {
        let step = Counter()
        let running = fullMenu.replacingOccurrences(of: "JOBS", with: #"[{"id": "r", "kind": "resign", "uuid": "o", "state": "running"}]"#)
        let cancelled = fullMenu.replacingOccurrences(of: "JOBS", with: #"[{"id": "r", "kind": "resign", "uuid": "o", "state": "cancelled", "result": "You cancelled the sign-in. Nothing changed."}]"#)
        let server = try FakeServer { head, _ in
            if head.hasPrefix("POST /jobs/r/cancel") {
                step.value = 1
                return [okReply(#"{"cancelled": true}"#)]
            }
            return [okReply(step.value == 0 ? running : cancelled)]
        }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        let poster = RecordingPoster()
        let notifier = Notifier(store: store, shown: { _ in nil }, open: {}, defaults: nil)
        notifier.begin(poster, permission: .allowed)
        await store.refresh()
        store.cancelJob("r")
        await waitFor { step.value == 1 }
        await store.refresh()
        await waitFor { store.cancelling.isEmpty }
        #expect(store.unseenResults.isEmpty)
        #expect(!poster.posted.contains { $0.id == "job-r" })
    }
}

@MainActor
struct WordingTests {
    @Test func theDevboxSentenceIsTrueTonight() throws {
        let away = try Fixture.state("devbox-away")
        #expect(JobFlow.devboxAway(away) == away.addText?.devboxAway)
        #expect(JobFlow.devboxAway(away) == "Lifeline on Studio is not answering, so accounts are added to this Mac.")
    }

    @Test func aResultCardIsOneSentenceWithTryAgain() throws {
        var job = JobSummary(id: "f", kind: "add", state: "failed", result: "Claude did not finish. Nothing changed.")
        job.resultHead = "Claude did not finish."
        job.resultRest = "Nothing changed."
        let found = try ocr(ResultCard(job: job, palette: Palette.of(.light)))
        #expect(!found.contains { $0.contains("Lifeline opens Claude's sign-in") }, "\(found)")
        #expect(!found.contains { $0.contains("Nothing changed") }, "\(found)")
        #expect(found.contains("Try again"), "\(found)")
        let adopt = try ocr(ResultCard(job: JobSummary(id: "a", kind: "adopt", state: "failed", result: "No."), palette: Palette.of(.light)))
        #expect(!adopt.contains { $0.contains("Lifeline opens Claude's sign-in") }, "\(adopt)")
    }

}

@MainActor
private func ocr(_ view: some View) throws -> [String] {
    typeset(view)
}
