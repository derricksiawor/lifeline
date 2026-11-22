import AppKit
import Foundation
import SwiftUI
import Testing
import UserNotifications
import Vision
@testable import Lifeline
@testable import LifelineKit

private func waveState(_ jobs: String, devbox: String = "", extra: String = "") -> String {
    #"{"version": 1, "mac": {"view": "mac", "rows": [{"uuid": "j", "name": "Alex's Account", "is_active": true}, {"uuid": "o", "name": "Team Account"}]}, "accounts": [{"uuid": "j", "name": "Alex's Account", "short": "Alex", "number": 1}, {"uuid": "o", "name": "Team Account", "short": "Team", "number": 2}], "jobs": "# + jobs + devbox + extra + #", "add_text": "# + Fixture.addTextJSON + "}"
}

private func waveDecoded(_ jobs: String, devbox: String = "") throws -> ServiceState {
    try ServiceState.decode(Data(waveState(jobs, devbox: devbox).utf8))
}

@MainActor
private func readText(_ view: some View) throws -> [String] {
    typeset(view)
}

@MainActor
@Suite(.serialized)
struct SeenOnlyWhenShownTests {
    @Test func aCardAddedWhileTheMenuIsHiddenCountsAsSeenOnlyOnceTheMenuShowsIt() async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let step = Counter()
        let idle = waveState("[]")
        let ended = waveState(#"[{"id": "r", "kind": "resign", "uuid": "o", "state": "done", "result": "Team Account has a new sign-in."}]"#)
        let server = try FakeServer { _, _ in [okReply(step.value == 0 ? idle : ended)] }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        let root = MenuRoot(store: store, status: AppStatus(), pickDetail: { _ in }, sized: { _ in }, framed: { _ in })
        let panel = NSPanel(
            contentRect: NSRect(x: -30000, y: -30000, width: 440, height: 900), styleMask: [.borderless, .nonactivatingPanel], backing: .buffered,
            defer: true
        )
        panel.isReleasedWhenClosed = false
        let host = NSHostingView(rootView: root)
        panel.contentView = host
        panel.orderFrontRegardless()
        store.opened()
        store.menuOpen = true
        store.markVisibleResults()
        host.layoutSubtreeIfNeeded()
        try await Task.sleep(for: .milliseconds(300))
        store.panelClosed(visible: true)
        panel.orderOut(nil)
        step.value = 1
        await store.refresh()
        for _ in 0..<6 {
            host.layoutSubtreeIfNeeded()
            try await Task.sleep(for: .milliseconds(150))
        }
        store.panelClosed(visible: false)
        #expect(store.unseenResults.map(\.id) == ["r"])
        panel.orderFrontRegardless()
        store.opened()
        store.menuOpen = true
        store.markVisibleResults()
        host.layoutSubtreeIfNeeded()
        try await Task.sleep(for: .milliseconds(300))
        store.panelClosed(visible: true)
        panel.orderOut(nil)
        #expect(store.unseenResults.isEmpty)
        panel.contentView = nil
    }

    @Test func aCardThatArrivesWhileTheMenuIsOpenIsSeenWhenItCloses() async throws {
        let step = Counter()
        let idle = waveState("[]")
        let ended = waveState(#"[{"id": "r", "kind": "resign", "uuid": "o", "state": "done", "result": "Team Account has a new sign-in."}]"#)
        let server = try FakeServer { _, _ in [okReply(step.value == 0 ? idle : ended)] }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        store.opened()
        store.menuOpen = true
        store.markVisibleResults()
        step.value = 1
        await store.refresh()
        store.cardsShown(["r"])
        store.panelClosed(visible: true)
        #expect(store.unseenResults.isEmpty)
    }

    @Test func aCloseWhileTheWindowIsHiddenRemembersNothing() async throws {
        let body = waveState(#"[{"id": "r", "kind": "resign", "uuid": "o", "state": "done", "result": "Team Account has a new sign-in."}]"#)
        let server = try FakeServer { _, _ in [okReply(body)] }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        store.cardsShown(["r"])
        store.opened()
        store.menuOpen = true
        store.markVisibleResults()
        store.panelClosed(visible: false)
        #expect(store.unseenResults.map(\.id) == ["r"])
        store.opened()
        store.menuOpen = true
        store.markVisibleResults()
        store.panelClosed(visible: true)
        #expect(store.unseenResults.isEmpty)
    }

    @Test func aCardIsNotSeenWhileSettingsIsShowing() async throws {
        let body = waveState(#"[{"id": "r", "kind": "resign", "uuid": "o", "state": "done", "result": "Team Account has a new sign-in."}]"#)
        let server = try FakeServer { _, _ in [okReply(body)] }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        store.opened()
        store.menuOpen = true
        store.screen = .settings
        await store.refresh()
        store.cardsShown(["r"])
        store.markVisibleResults()
        store.panelClosed(visible: true)
        #expect(store.unseenResults.map(\.id) == ["r"])
    }
}

@MainActor
@Suite(.serialized)
struct SignInPageTests {
    @Test func openTheSignInPageAgainAsksTheService() async throws {
        let body = waveState(#"[{"id": "r", "kind": "resign", "uuid": "o", "state": "running", "url": "https://claude.ai/oauth/authorize?x=1", "browser": "Google Chrome", "opened": true}]"#)
        let server = try FakeServer { head, _ in
            head.hasPrefix("POST /jobs/r/open") ? [okReply(#"{"browser": "Google Chrome", "opened": true}"#)] : [okReply(body)]
        }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        store.reopenJob("r")
        await waitFor { server.requests.contains { $0.head.hasPrefix("POST /jobs/r/open ") } }
        #expect(server.requests.contains { $0.head.hasPrefix("POST /jobs/r/open ") })
    }

    @Test func aRefusedReopenSaysWhy() async throws {
        let sentence = "That sign-in has already finished, so there is no page to open."
        let body = waveState(#"[{"id": "r", "kind": "resign", "uuid": "o", "state": "running"}]"#)
        let server = try FakeServer { head, _ in
            head.hasPrefix("POST /jobs/r/open") ? [okReply(#"{"error": "\#(sentence)"}"#, status: "409 Conflict")] : [okReply(body)]
        }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        store.menuOpen = true
        store.reopenJob("r")
        await waitFor { store.note == sentence }
        #expect(store.note == sentence)
    }

    @Test func theAppNeverOpensASignInPageItself() throws {
        let folder = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources/Lifeline")
        let files = try FileManager.default.contentsOfDirectory(at: folder, includingPropertiesForKeys: nil).filter { $0.pathExtension == "swift" }
        #expect(!files.isEmpty)
        for file in files {
            let text = try String(contentsOf: file, encoding: .utf8)
            #expect(!text.contains("NSWorkspace.shared.open("), "\(file.lastPathComponent)")
        }
    }

    @Test func onlyTheJobsOwnBrowserIsBroughtForward() {
        #expect(JobFlow.browserToRaise(JobSummary(id: "1", browser: "Google Chrome")) == "Google Chrome")
        #expect(JobFlow.browserToRaise(JobSummary(id: "1", browser: " ")) == nil)
        #expect(JobFlow.browserToRaise(JobSummary(id: "1")) == nil)
        #expect(JobFlow.browserToRaise(nil) == nil)
    }

    @Test func thePanelLowersOnlyWhenASignInStarts() async throws {
        let body = waveState(#"[{"id": "1", "kind": "add", "state": "running", "url": "https://claude.ai/oauth/authorize?x=1"}]"#)
        let server = try FakeServer { head, _ in
            head.hasPrefix("POST /add") ? [okReply(#"{"job": "2"}"#, status: "202 Accepted")] : [okReply(body)]
        }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        store.opened()
        store.menuOpen = true
        store.screen = .add
        #expect(store.signInStarts == 0)
        store.startAdd(.add)
        #expect(store.signInStarts == 1)
    }
}

@MainActor
@Suite(.serialized)
struct UndoDelayTests {
    @Test func theDelayCountsFromTheLastFinishedSwitch() {
        let shown = Date(timeIntervalSince1970: 1_000)
        let settled = Date(timeIntervalSince1970: 1_005)
        #expect(!MenuLogic.armed(shownAt: shown, settledAt: settled, now: settled.addingTimeInterval(0.5), delay: 1))
        #expect(MenuLogic.armed(shownAt: shown, settledAt: settled, now: settled.addingTimeInterval(1), delay: 1))
        #expect(MenuLogic.armed(shownAt: shown, settledAt: nil, now: shown.addingTimeInterval(1), delay: 1))
        #expect(!MenuLogic.footerTakesClicks(switching: false, animating: true))
        #expect(!MenuLogic.footerTakesClicks(switching: true, animating: false))
        #expect(MenuLogic.footerTakesClicks(switching: false, animating: false))
    }

    @Test func everyFinishedSwitchOrUndoRestartsTheDelay() async throws {
        let server = try FakeServer { head, _ in
            if head.hasPrefix("POST /switch") || head.hasPrefix("POST /undo") {
                return [okReply(#"{"to_uuid": "o"}"#)]
            }
            return [okReply(waveState("[]"))]
        }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        #expect(store.switchSettledAt == nil)
        store.run(.switchTo("o"), via: .row)
        await waitFor { !store.switching }
        let first = try #require(store.switchSettledAt)
        store.run(.undo, via: .menuUndo)
        await waitFor { !store.switching && store.switchSettledAt != first }
        let second = try #require(store.switchSettledAt)
        #expect(second > first)
        store.run(.undo, via: .menuUndo)
        await waitFor { !store.switching && store.switchSettledAt != second }
        #expect(try #require(store.switchSettledAt) > second)
    }
}

@MainActor
@Suite(.serialized)
struct CancelHonestyTests {
    @Test func cancelSaysCancellingUntilTheServiceConfirms() async throws {
        let step = Counter()
        let running = waveState(#"[{"id": "1", "kind": "add", "state": "running", "url": "https://claude.ai/oauth/authorize?x=1"}]"#)
        let cancelled = waveState(#"[{"id": "1", "kind": "add", "state": "cancelled", "result": "You cancelled the sign-in. Nothing changed."}]"#)
        let gate = Flag()
        let server = try FakeServer { head, _ in
            if head.hasPrefix("POST /add") {
                return [okReply(#"{"job": "1"}"#, status: "202 Accepted")]
            }
            if head.hasPrefix("POST /jobs/1/cancel") {
                while gate.value {
                    Thread.sleep(forTimeInterval: 0.01)
                }
                step.value = 1
                return [okReply(#"{"cancelled": true}"#)]
            }
            return [okReply(step.value == 0 ? running : cancelled)]
        }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        store.startAdd(.add)
        await waitFor { store.addRun?.job != nil }
        store.cancelJob("1")
        #expect(store.cancelling.contains("1"))
        #expect(store.addStep == .start)
        #expect(store.addNote == "Cancelling the sign-in.")
        #expect(store.waitingJobs.map(\.id) == ["1"])
        gate.value = false
        await waitFor { step.value == 1 }
        await store.refresh()
        await waitFor { store.addNote == "You cancelled the sign-in. Nothing changed." }
        #expect(store.addStep == .start)
        #expect(store.addNote == "You cancelled the sign-in. Nothing changed.")
        #expect(!store.cancelling.contains("1"))
    }

    @Test func aCancelThatLosesTheRaceEndsOnTheNames() async throws {
        let step = Counter()
        let running = waveState(#"[{"id": "1", "kind": "add", "state": "running"}]"#)
        let done = waveState(#"[{"id": "1", "kind": "add", "state": "done", "uuid": "o", "result": "Lifeline added Team Account."}]"#)
        let server = try FakeServer { head, _ in
            if head.hasPrefix("POST /add") {
                return [okReply(#"{"job": "1"}"#, status: "202 Accepted")]
            }
            if head.hasPrefix("POST /jobs/1/cancel") {
                step.value = 1
                return [okReply(#"{"error": "That sign-in has already finished, so there is nothing to cancel."}"#, status: "409 Conflict")]
            }
            return [okReply(step.value == 0 ? running : done)]
        }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        store.startAdd(.add)
        await waitFor { store.addRun?.job != nil }
        store.cancelJob("1")
        await waitFor { step.value == 1 }
        await store.refresh()
        await waitFor { store.addRun?.finished == true }
        #expect(store.addStep == .added("o"))
        #expect(store.cancelling.isEmpty)
    }

    @Test func theCardSaysCancellingWhileItWaits() throws {
        let job = JobSummary(id: "1", kind: "resign", uuid: "o", state: "running", url: "https://claude.ai/oauth/authorize?x=1", opened: true, browser: "Google Chrome")
        let found = try readText(WaitingCard(job: job, palette: Palette.of(.light), actions: CardActions(), state: try waveDecoded("[]"), cancelling: true))
        #expect(found.contains { $0.contains("Cancelling the sign-in.") }, "\(found)")
        #expect(!found.contains { $0.contains("Open the sign-in page again") }, "\(found)")
    }
}

struct FinishedStateTests {
    @Test func anyStateOtherThanRunningEndsTheAddWithItsResult() throws {
        for state in ["cancelled", "stopped"] {
            let found = try waveDecoded(#"[{"id": "1", "kind": "add", "state": "\#(state)", "result": "You cancelled the sign-in. Nothing changed."}]"#)
            #expect(AddFlow.outcome(job: "1", state: found, seen: true, started: nil, now: Date()) == .failed("You cancelled the sign-in. Nothing changed."), "\(state)")
            #expect(JobFlow.isFinished(try #require(found.jobs?.first)), "\(state)")
        }
        let running = try waveDecoded(#"[{"id": "1", "kind": "add", "state": "running"}]"#)
        #expect(AddFlow.outcome(job: "1", state: running, seen: true, started: nil, now: Date()) == .running)
    }
}

@MainActor
struct SignInAgainCardTests {
    @Test func theCardNamesTheAccountAndTheOrganizationStep() throws {
        let state = try waveDecoded("[]")
        var job = try Fixture.job("job-cards", "k-resign")
        job.opened = true
        #expect(JobFlow.label(job, state: state) == "Sign in again to Work Account")
        #expect(JobFlow.cardLines(job, state: state) == ["If Claude asks you to choose an organization, choose the one marked Personal, then press Authorize."])
        #expect(JobFlow.label(try Fixture.job("job-cards", "k-add"), state: state) == "Add an account")
        let found = try readText(WaitingCard(job: job, palette: Palette.of(.light), actions: CardActions(), state: state))
        #expect(found.contains { $0.contains("sign this Mac in to Work Account again") }, "\(found)")
        #expect(!found.contains { $0.contains("choose an organization") }, "\(found)")
    }
}

@MainActor
struct MinorFixTests {
    @Test func anEmptyShortNameKeepsTheStoredOneAndTheSentenceSaysSo() {
        let kept = AddFlow.shownNames(menuName: "Alexandra", oldShort: "Jac", name: "Alexandra's Account", short: " ")
        #expect(kept.menu == "Alexandra's Account" && kept.bar == "Jac")
        #expect(AddFlow.renameBody(oldName: "Alexandra", oldShort: "Jac", name: "Alexandra's Account", short: " ")["short"] == "Jac")
        let none = AddFlow.shownNames(menuName: "Alexandra", oldShort: "", name: "Alexandra's Account", short: "")
        #expect(none.bar == "Alexandra's Account")
    }

    @Test func theDevboxSentenceIsTrueAndTheChipsShowTheRealPlace() throws {
        let text = waveState("[]", extra: #", "settings": {"placement": {"j": "devbox", "o": "both"}}, "devbox": {"name": "Studio", "reachable": false}"#)
        let state = try Fixture.withSettingsText(text, from: "moment-sheet-away")
        let section = try #require(SettingsPage.sections(state: state, local: LocalStatus(notices: .allowed, shortcut: .on, login: .on)).first)
        #expect(section.rows.map(\.control) == [.placementLocked("j", "devbox"), .placementLocked("o", "both")])
        #expect(section.note == state.settingsText?.accountsNote, "Studio sentence shows once, in the lead")
        #expect(JobFlow.devboxAway(state) == "Lifeline on Studio is not answering, so accounts are added to this Mac.")
    }

    @Test func aFailedAddCardOffersTryAgain() async throws {
        #expect(JobFlow.tryAgainKind(JobSummary(id: "1", kind: "add", state: "failed", result: "No.")) == .add)
        #expect(JobFlow.tryAgainKind(JobSummary(id: "1", kind: "add", state: "cancelled", result: "No.")) == .add)
        #expect(JobFlow.tryAgainKind(JobSummary(id: "1", kind: "adopt", state: "failed", result: "No.")) == .adopt)
        #expect(JobFlow.tryAgainKind(JobSummary(id: "1", kind: "add", state: "done", result: "Yes.")) == nil)
        #expect(JobFlow.tryAgainKind(JobSummary(id: "1", kind: "resign", state: "failed", result: "No.")) == nil)
        let body = waveState(#"[{"id": "f", "kind": "add", "state": "failed", "result": "No."}]"#)
        let server = try FakeServer { head, _ in
            head.hasPrefix("POST /add") ? [okReply(#"{"job": "2"}"#, status: "202 Accepted")] : [okReply(body)]
        }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        store.opened()
        store.menuOpen = true
        store.retry(try #require(store.unseenResults.first))
        #expect(store.screen == .add)
        #expect(store.addRun == nil)
        #expect(store.chosenPick == nil)
        try await Task.sleep(for: .milliseconds(200))
        #expect(!server.requests.contains { $0.head.hasPrefix("POST /add") })
        #expect(store.unseenResults.isEmpty)
    }

    @Test func aNotificationSwitchDuringASwitchSaysOneIsRunning() async throws {
        let gate = Flag()
        let server = try FakeServer { head, _ in
            if head.hasPrefix("POST /switch") {
                while gate.value {
                    Thread.sleep(forTimeInterval: 0.01)
                }
                return [okReply(#"{"to_uuid": "o"}"#)]
            }
            return [okReply(waveState("[]"))]
        }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        let poster = RecordingPoster()
        let notifier = Notifier(store: store, shown: { _ in nil }, open: {}, defaults: nil)
        notifier.begin(poster, permission: .allowed)
        await store.refresh()
        store.run(.switchTo("o"), via: .row)
        notifier.respond(action: Notices.primaryAction, info: ["command": "switch", "uuid": "o"], id: "n")
        await waitFor { poster.posted.contains { $0.title == "A switch is already running." } }
        #expect(poster.posted.contains { $0.title == "A switch is already running." })
        gate.value = false
        await waitFor { !store.switching }
    }

    @Test func noNotificationTitleSaysJob() throws {
        for name in ["failed-jobs", "after-mac-add-org", "devbox-work-done", "devbox-work-step2"] {
            for job in try Fixture.state(name).jobs ?? [] {
                let title = JobFlow.title(job, state: nil)
                #expect(!title.lowercased().contains("job"), "\(name) \(job.id) \(title)")
            }
        }
        let failed = try Fixture.state("failed-jobs")
        #expect(failed.jobs?.first { $0.id == "fa" }.map { JobFlow.title($0, state: failed) } == "Lifeline did not add the account")
    }

    @Test func theFailedAddScreenOffersOnlyTryAgain() throws {
        let state = try waveDecoded("[]")
        let found = try readText(AddAccountView(state: state, step: .failed("No."), target: .mac, problem: nil, zone: TimeZone(identifier: "UTC")!))
        #expect(found.contains("Try again"), "\(found)")
        #expect(!found.dropFirst().contains("Open the sign-in page"), "\(found)")
    }
}

struct SharedLoginTests {
    private func shared() throws -> ServiceState {
        let text = #"{"version": 1, "mac": {"view": "mac", "rows": [{"uuid": "p", "name": "Personal Account", "is_active": true}, {"uuid": "o", "name": "Team Account"}, {"uuid": "j", "name": "Alex's Account"}]}, "accounts": [{"uuid": "p", "name": "Personal Account", "short": "Personal", "email": "sam@example.org", "org_uuid": "org-p", "organization": "Sam's Individual Org"}, {"uuid": "o", "name": "Team Account", "short": "Team", "email": "Sam@Example.org", "org_uuid": "org-o", "organization": "Example Org"}, {"uuid": "j", "name": "Alex's Account", "short": "Alex", "email": "alex@example.com", "org_uuid": "org-j", "organization": "Alex's Org"}]}"#
        return try ServiceState.decode(Data(text.utf8))
    }

    @Test func aServiceStopShowsLikeAnyOtherResult() throws {
        let sentence = "Lifeline stopped, so the sign-in was cancelled. Nothing changed."
        let state = try waveDecoded(#"[{"id": "s", "kind": "add", "state": "cancelled", "result": "\#(sentence)", "title": "Lifeline cancelled the sign-in"}]"#)
        let job = try #require(JobFlow.results(state, seen: []).first)
        #expect(job.result == sentence)
        #expect(JobFlow.title(job, state: state) == "Lifeline cancelled the sign-in")
        #expect(JobFlow.notice(job).body == sentence)
        #expect(AddFlow.outcome(job: "s", state: state, seen: true, started: nil, now: Date()) == .failed(sentence))
    }

    @Test func theCancelSentenceIsTheServicesEverywhere() throws {
        let core = try Fixture.state("job-cards")
        #expect(JobFlow.cancelled(core) == "You cancelled the sign-in. Nothing changed.")
        let folder = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources")
        let walker = FileManager.default.enumerator(at: folder, includingPropertiesForKeys: nil)
        var checked = 0
        while let file = walker?.nextObject() as? URL {
            guard file.pathExtension == "swift", file.lastPathComponent != "Samples.swift" else { continue }
            checked += 1
            let text = try String(contentsOf: file, encoding: .utf8)
            #expect(!text.contains("so nothing changed"), "\(file.lastPathComponent)")
        }
        #expect(checked > 10)
    }
}
