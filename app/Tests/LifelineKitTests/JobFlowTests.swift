import AppKit
import Foundation
import SwiftUI
import Testing
import UserNotifications
import Vision
@testable import Lifeline
@testable import LifelineKit

private func jobState(_ jobs: String, devbox: String = "") -> String {
    #"{"version": 1, "mac": {"view": "mac", "active_uuid": "j", "rows": [{"uuid": "j", "name": "Alex's Account", "is_active": true}, {"uuid": "o", "name": "Team Account"}]}, "accounts": [{"uuid": "j", "name": "Alex's Account", "short": "Alex"}, {"uuid": "o", "name": "Team Account", "short": "Team"}], "jobs": "# + jobs + devbox + #", "add_text": "# + Fixture.addTextJSON + "}"
}

private func decoded(_ jobs: String, devbox: String = "") throws -> ServiceState {
    try ServiceState.decode(Data(jobState(jobs, devbox: devbox).utf8))
}

@MainActor
private func read(_ view: some View, dark: Bool = false) throws -> [String] {
    typeset(view, dark: dark)
}

struct JobFlowTests {
    @Test func theWaitingSentenceNamesTheBrowserOnceItIsKnown() throws {
        for job in try Fixture.state("job-cards").jobs ?? [] {
            #expect(JobFlow.waiting(job) == (job.waiting ?? ""), "\(job.id)")
        }
        #expect(JobFlow.waiting(JobSummary(id: "1", browser: "Google Chrome")) == "", "the app builds no waiting sentence of its own")
        #expect(JobFlow.waiting(nil) == "")
        #expect(JobFlow.unbroken("the sign-in in Google Chrome") == "the sign-\u{2060}in in Google Chrome")
        #expect(!JobFlow.unbroken("the sign-in").contains("sign-in"))
    }

    @Test func onlyARunningSignInThatIsNotCancelledWaits() throws {
        let state = try decoded(#"""
        [{"id": "a", "kind": "adopt", "state": "running"},
         {"id": "d", "kind": "add", "state": "done", "result": "Lifeline added Team Account."},
         {"id": "r", "kind": "resign", "uuid": "o", "state": "running", "url": "https://claude.ai/oauth/authorize?x=1", "browser": "Google Chrome", "opened": true}]
        """#)
        #expect(JobFlow.waitingJob(state)?.id == "r")
        #expect(JobFlow.breathes(state))
        #expect(!JobFlow.breathes(try decoded("[]")))
        for kind in ["add", "resign", "devbox-add", "devbox-resign"] {
            let one = try decoded(#"[{"id": "x", "kind": "\#(kind)", "state": "running"}]"#)
            #expect(JobFlow.waitingJob(one)?.id == "x", "\(kind)")
        }
        #expect(JobFlow.waitingJob(nil) == nil)
    }

    @Test func thePageIsTheJobsHttpsAddressOnly() throws {
        let state = try decoded(#"[{"id": "r", "kind": "add", "state": "running", "url": "https://claude.ai/oauth/authorize?code=true", "opened": true, "browser": "Google Chrome"}]"#)
        let job = try #require(state.jobs?.first)
        #expect(job.browser == "Google Chrome")
        #expect(job.opened == true)
        #expect(JobFlow.page(job)?.absoluteString == "https://claude.ai/oauth/authorize?code=true")
        #expect(JobFlow.page(JobSummary(id: "1", url: "http://claude.ai/x")) == nil)
        #expect(JobFlow.page(JobSummary(id: "1", url: "file:///etc/hosts")) == nil)
        #expect(JobFlow.page(JobSummary(id: "1")) == nil)
    }

    @Test func aFinishedResultIsShownUntilSeen() throws {
        let state = try decoded(#"""
        [{"id": "1", "kind": "add", "state": "failed", "result": "Your browser signed in as Personal Account (sam@example.org), which Lifeline already has. Nothing changed."},
         {"id": "2", "kind": "resign", "state": "done", "result": "Team Account has a new sign-in."},
         {"id": "3", "kind": "add", "state": "running"},
         {"id": "4", "kind": "add", "state": "failed", "result": " "}]
        """#)
        #expect(JobFlow.results(state, seen: []).map(\.id) == ["1", "2"])
        #expect(JobFlow.results(state, seen: ["1"]).map(\.id) == ["2"])
        #expect(JobFlow.results(state, seen: ["1", "2"]).isEmpty)
    }

    @Test func aJobEndsOnlyAfterTheFirstReading() throws {
        let running = try decoded(#"[{"id": "1", "kind": "add", "state": "running"}, {"id": "2", "kind": "resign", "state": "done", "result": "Old."}]"#)
        let later = try decoded(#"[{"id": "1", "kind": "add", "state": "done", "result": "Lifeline added Team Account."}, {"id": "2", "kind": "resign", "state": "done", "result": "Old."}, {"id": "3", "kind": "devbox-add", "state": "failed", "result": "Fast."}]"#)
        let before = JobFlow.states(running)
        #expect(before == ["1": "running", "2": "done"])
        #expect(JobFlow.ended(before: before, now: later).map(\.id) == ["1", "3"])
        #expect(JobFlow.ended(before: JobFlow.states(later), now: later).isEmpty)
    }

    @Test func theNoticeCarriesTheResultSentence() throws {
        let state = try Fixture.state("failed-jobs")
        for job in state.jobs ?? [] {
            let notice = JobFlow.notice(job, state: state)
            #expect(notice.id == "job-" + job.id)
            #expect(notice.title == job.title && !notice.title.isEmpty, "\(job.id)")
            #expect(notice.body == job.result)
            #expect(notice.action == nil)
        }
    }

    @Test func theGuidanceFitsTheJob() throws {
        let state = try Fixture.state("job-cards")
        let text = try #require(state.addText)
        for job in state.jobs ?? [] {
            #expect(JobFlow.guidance(job, state: state) == (job.lines ?? []), "\(job.id)")
        }
        let add = try #require(state.jobs?.first { $0.kind == "add" })
        #expect(JobFlow.guidance(add, state: state).last == text.stays)
        let devbox = try #require(state.jobs?.first { $0.kind == "devbox-add" })
        #expect(JobFlow.guidance(devbox, state: state).contains(text.forDevbox ?? "-"))
    }

    @Test func aClickOnTheLineRaisesALoweredMenuInsteadOfClosingIt() {
        #expect(JobFlow.toggleMove(open: false, holding: false) == .open)
        #expect(JobFlow.toggleMove(open: true, holding: false) == .close)
        #expect(JobFlow.toggleMove(open: true, holding: true) == .raise)
        #expect(JobFlow.outsideMove(holding: false) == .close)
        #expect(JobFlow.outsideMove(holding: true) == .stay)
    }

    @Test func seenJobsAreKeptOnlyWhileTheServiceStillLists() throws {
        let state = try decoded(#"[{"id": "1", "kind": "add", "state": "done", "result": "A."}]"#)
        #expect(JobFlow.prune(["1", "gone"], state: state) == ["1"])
        #expect(JobFlow.prune(["1", "gone"], state: nil) == ["1", "gone"])
    }

    @Test func theAddSentenceIsTheCoresOne() throws {
        let state = try Fixture.state("job-cards")
        #expect(state.signinBrowser == "Google Chrome")
        #expect(AddFlow.explain(state) == "Lifeline opens Claude's sign-in in Google Chrome, your default browser. Sign in there as the account you are adding. If Claude asks you to choose an organization, choose the one you are adding, then press Authorize.")
    }

    @Test func theDevboxIsOfferedOnlyOnceItAnswers() throws {
        let away = try decoded("[]")
        let here = try decoded("[]", devbox: #", "devbox": {"reachable": true, "answers": true, "can_sign_in": true}"#)
        let noLogin = try decoded("[]", devbox: #", "devbox": {"reachable": true, "answers": true, "can_sign_in": false}"#)
        #expect(!AddFlow.available(.devbox, state: noLogin) && !AddFlow.available(.both, state: noLogin))
        let silent = try decoded("[]", devbox: #", "devbox": {"reachable": false, "answers": false}"#)
        let readOnly = try decoded("[]", devbox: #", "devbox": {"reachable": true}"#)
        for state in [away, silent, readOnly] {
            #expect(AddFlow.available(.mac, state: state))
            #expect(!AddFlow.available(.devbox, state: state))
            #expect(!AddFlow.available(.both, state: state))
            #expect(AddFlow.defaultTarget(state) == .mac)
        }
        #expect(AddFlow.available(.devbox, state: here))
        #expect(AddFlow.available(.both, state: here))
    }

    @Test func theNamingStepStartsFromTheServicesSuggestedPair() throws {
        let account = AccountSummary(uuid: "o", name: "Alexandra", short: "Alexandra")
        func names(_ job: JobSummary?) -> [String] {
            let found = AddFlow.suggestedNames(account, job: job)
            return [found.name, found.short]
        }
        #expect(names(JobSummary(id: "1", suggestedName: "Alexandra's Account", suggestedShort: "Alexandra")) == ["Alexandra's Account", "Alexandra"])
        #expect(names(JobSummary(id: "1", suggestedName: "Example Org Account", suggestedShort: "Example Org")) == ["Example Org Account", "Example Org"])
        #expect(names(JobSummary(id: "1", suggestedName: " ", suggestedShort: nil)) == ["Alexandra", "Alexandra"])
        #expect(names(nil) == ["Alexandra", "Alexandra"])
        #expect(AddFlow.suggestedNames(AccountSummary(uuid: "x"), job: nil) == ("", ""))
        let state = try decoded(#"""
        [{"id": "m", "kind": "add", "state": "done", "uuid": "o", "result": "Lifeline added Alexandra.", "suggested_name": "Alexandra's Account", "suggested_short": "Alexandra"},
         {"id": "x", "kind": "add", "state": "done", "uuid": "o", "suggested_name": "Other's Account", "suggested_short": "Other"}]
        """#)
        var run = AddRun(target: .mac, kind: .add, stages: [.macAdd])
        run.began("m", at: Date())
        let job = AddFlow.addJob(for: "o", run: run, state: state)
        #expect(job?.id == "m")
        #expect(job?.suggestedName == "Alexandra's Account")
        #expect(job?.suggestedShort == "Alexandra")
        #expect(AddFlow.addJob(for: "j", run: run, state: state) == nil)
        #expect(AddFlow.addJob(for: "o", run: nil, state: state) == nil)
    }

    @Test func theAlertOffersOnlyTheServicesBestAccount() throws {
        let at = ISO8601DateFormatter().string(from: Date())
        func state(best: String?) throws -> ServiceState {
            let flag = { (uuid: String) in best == uuid ? #", "is_best": true"# : "" }
            let text = #"{"version": 1, "mac": {"view": "mac", "active_uuid": "j", "rows": [{"uuid": "j", "name": "Alex's Account", "is_active": true, "five": 90\#(flag("j"))}, {"uuid": "o", "name": "Team Account", "five": 10\#(flag("o"))}, {"uuid": "c", "name": "Work Account", "five": 40\#(flag("c"))}]}, "accounts": [{"uuid": "j", "name": "Alex's Account", "short": "Alex"}, {"uuid": "o", "name": "Team Account", "short": "Team"}, {"uuid": "c", "name": "Work Account", "short": "Work"}], "notifications": [{"id": "n1", "kind": "limit", "title": "Alex's Account reached its limit", "body": "", "uuid": "j", "at": "\#(at)"}]}"#
            return try ServiceState.decode(Data(text.utf8))
        }
        let notice = NoticeSummary(id: "n1", kind: "limit", title: "Alex's Account reached its limit", body: "", uuid: "j", at: at)
        #expect(Notices.action(for: notice, state: try state(best: "c"))?.command == .switchTo("c"))
        #expect(Notices.action(for: notice, state: try state(best: nil)) == nil)
        #expect(Notices.action(for: notice, state: try state(best: "j")) == nil)
    }
}

@MainActor
@Suite(.serialized)
struct JobStoreTests {
    private func store(_ server: FakeServer, defaults: UserDefaults? = nil) -> StateStore {
        StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: defaults)
    }

    @Test func aRunningSignInIsACardOnTheNormalMenuAndSwitchingStillWorks() async throws {
        let body = jobState(#"[{"id": "r", "kind": "resign", "uuid": "o", "state": "running", "url": "https://claude.ai/oauth/authorize?x=1", "browser": "Google Chrome", "opened": true}]"#)
        let server = try FakeServer { head, _ in
            head.hasPrefix("POST /switch") ? [okReply(#"{"to_uuid": "o"}"#)] : [okReply(body)]
        }
        defer { server.close() }
        let store = store(server)
        await store.refresh()
        store.opened()
        store.menuOpen = true
        #expect(store.screen == .menu)
        #expect(store.waitingJobs.map(\.id) == ["r"])
        #expect(store.shownJob == nil)
        #expect(store.breathing)
        store.run(.switchTo("o"), via: .row)
        await waitFor { server.requests.contains { $0.head.hasPrefix("POST /switch") } }
        #expect(server.requests.contains { $0.head.hasPrefix("POST /switch") })
        store.panelClosed(visible: true)
        store.opened()
        #expect(store.screen == .menu)
        #expect(store.waitingJobs.map(\.id) == ["r"])
    }

    @Test func aWaitingAddReopensOnTheMenuWithItsCard() async throws {
        let body = jobState(#"[{"id": "1", "kind": "add", "state": "running", "url": "https://claude.ai/oauth/authorize?x=1", "browser": "Google Chrome", "opened": true}]"#)
        let server = try FakeServer { head, _ in
            head.hasPrefix("POST /add") ? [okReply(#"{"job": "1"}"#, status: "202 Accepted")] : [okReply(body)]
        }
        defer { server.close() }
        let store = store(server)
        await store.refresh()
        store.opened()
        store.menuOpen = true
        store.screen = .add
        store.startAdd(.add)
        await waitFor { store.addRun?.job != nil }
        await store.refresh()
        #expect(store.addJob?.id == "1")
        #expect(store.shownJob?.id == "1")
        store.panelClosed(visible: true)
        store.opened()
        #expect(store.screen == .menu)
        #expect(store.waitingJobs.map(\.id) == ["1"])
        #expect(store.waitingJobs.first?.browser == "Google Chrome")
        store.screen = .add
        #expect(store.shownJob?.id == "1")
    }

    @Test func anAddThatSucceedsWhileTheMenuIsOpenEndsOnItsNames() async throws {
        let step = Counter()
        let running = jobState(#"[{"id": "1", "kind": "add", "state": "running"}]"#)
        let done = jobState(#"[{"id": "1", "kind": "add", "state": "done", "uuid": "o", "result": "Lifeline added Team Account.", "suggested_name": "Team Account", "suggested_short": "Team"}]"#)
        let server = try FakeServer { head, _ in
            head.hasPrefix("POST /add") ? [okReply(#"{"job": "1"}"#, status: "202 Accepted")] : [okReply(step.value == 0 ? running : done)]
        }
        defer { server.close() }
        let store = store(server)
        await store.refresh()
        store.startAdd(.add)
        await waitFor { store.addRun?.job != nil }
        store.opened()
        store.menuOpen = true
        #expect(store.screen == .menu)
        step.value = 1
        await store.refresh()
        await waitFor { store.screen == .add }
        #expect(store.screen == .add)
        #expect(store.addStep == .added("o"))
        #expect(store.unseenResults.isEmpty)
    }

    @Test func anAddThatFailsWhileTheMenuIsOpenShowsItsResultCard() async throws {
        let step = Counter()
        let running = jobState(#"[{"id": "1", "kind": "add", "state": "running"}]"#)
        let failed = jobState(#"[{"id": "1", "kind": "add", "state": "failed", "result": "Your browser signed in as Personal Account (sam@example.org), which Lifeline already has. Nothing changed."}]"#)
        let server = try FakeServer { head, _ in
            head.hasPrefix("POST /add") ? [okReply(#"{"job": "1"}"#, status: "202 Accepted")] : [okReply(step.value == 0 ? running : failed)]
        }
        defer { server.close() }
        let store = store(server)
        await store.refresh()
        store.startAdd(.add)
        await waitFor { store.addRun?.job != nil }
        store.opened()
        store.menuOpen = true
        step.value = 1
        await store.refresh()
        await waitFor { store.addRun?.finished == true }
        #expect(store.screen == .menu)
        #expect(store.waitingJobs.isEmpty)
        #expect(store.unseenResults.map(\.id) == ["1"])
    }

    @Test func anOpenMenuGainsTheCardWhenASignInStartsAndTheResultWhenItEnds() async throws {
        let phase = Flag()
        phase.value = false
        let idle = jobState("[]")
        let running = jobState(#"[{"id": "r", "kind": "resign", "uuid": "o", "state": "running"}]"#)
        let ended = jobState(#"[{"id": "r", "kind": "resign", "uuid": "o", "state": "done", "result": "Team Account has a new sign-in."}]"#)
        let step = Counter()
        let server = try FakeServer { _, _ in
            switch step.value {
            case 0: return [okReply(idle)]
            case 1: return [okReply(running)]
            default: return [okReply(ended)]
            }
        }
        defer { server.close() }
        let temp = try TempDefaults()
        defer { temp.finish() }
        let store = store(server, defaults: temp.defaults)
        await store.refresh()
        store.opened()
        store.menuOpen = true
        #expect(store.screen == .menu)
        step.value = 1
        await store.refresh()
        #expect(store.screen == .menu)
        #expect(store.waitingJobs.map(\.id) == ["r"])
        step.value = 2
        await store.refresh()
        #expect(store.screen == .menu)
        #expect(store.waitingJobs.isEmpty)
        #expect(store.unseenResults.map(\.id) == ["r"])
        store.cardsShown(["r"])
        #expect(store.unseenResults.map(\.id) == ["r"])
        store.panelClosed(visible: true)
        #expect(store.unseenResults.isEmpty)
        let again = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: temp.defaults)
        await again.refresh()
        #expect(again.unseenResults.isEmpty)
    }

    @Test func aResultNotShownStaysForTheNextOpening() async throws {
        let body = jobState(#"[{"id": "1", "kind": "add", "state": "failed", "result": "Your browser signed in as Personal Account (sam@example.org), which Lifeline already has. Nothing changed."}]"#)
        let server = try FakeServer { _, _ in [okReply(body)] }
        defer { server.close() }
        let store = store(server)
        await store.refresh()
        store.opened()
        store.screen = .settings
        store.panelClosed(visible: true)
        store.opened()
        #expect(store.unseenResults.map(\.id) == ["1"])
    }

    @Test func cancelStopsTheJobAndEndsTheAdd() async throws {
        let step = Counter()
        let body = jobState(#"[{"id": "1", "kind": "add", "state": "running", "url": "https://claude.ai/oauth/authorize?x=1"}]"#)
        let cancelled = jobState(#"[{"id": "1", "kind": "add", "state": "cancelled", "result": "You cancelled the sign-in. Nothing changed."}]"#)
        let server = try FakeServer { head, _ in
            if head.hasPrefix("POST /add") {
                return [okReply(#"{"job": "1"}"#, status: "202 Accepted")]
            }
            if head.hasPrefix("POST /jobs/") {
                step.value = 1
                return [okReply(#"{"cancelled": true}"#)]
            }
            return [okReply(step.value == 0 ? body : cancelled)]
        }
        defer { server.close() }
        let store = store(server)
        await store.refresh()
        store.opened()
        store.menuOpen = true
        store.screen = .add
        store.startAdd(.add)
        await waitFor { store.addRun?.job != nil }
        await store.refresh()
        #expect(store.shownJob?.id == "1")
        store.cancelJob("1")
        #expect(store.cancelling.contains("1"))
        await waitFor { server.requests.contains { $0.head.hasPrefix("POST /jobs/1/cancel") } }
        #expect(server.requests.contains { $0.head.hasPrefix("POST /jobs/1/cancel ") })
        #expect(store.addStep == .start)
        await waitFor { store.addNote == "You cancelled the sign-in. Nothing changed." }
        #expect(store.shownJob == nil)
        #expect(!store.breathing)
        #expect(store.addStep == .start)
        #expect(store.addNote == "You cancelled the sign-in. Nothing changed.")
        #expect(store.cancelling.isEmpty)
        store.panelClosed(visible: true)
        store.opened()
        #expect(store.screen == .menu)
        #expect(store.unseenResults.isEmpty)
    }

    @Test func cancellingFromTheCardShowsTheServicesSentenceOnceItConfirms() async throws {
        let step = Counter()
        let running = jobState(#"[{"id": "r", "kind": "resign", "uuid": "o", "state": "running"}]"#)
        let cancelled = jobState(#"[{"id": "r", "kind": "resign", "uuid": "o", "state": "cancelled", "result": "You cancelled the sign-in. Nothing changed."}]"#)
        let server = try FakeServer { head, _ in
            if head.hasPrefix("POST /jobs/r/cancel") {
                step.value = 1
                return [okReply(#"{"id": "r", "state": "cancelled", "result": "You cancelled the sign-in. Nothing changed."}"#)]
            }
            return [okReply(step.value == 0 ? running : cancelled)]
        }
        defer { server.close() }
        let store = store(server)
        await store.refresh()
        store.opened()
        store.menuOpen = true
        #expect(store.waitingJobs.map(\.id) == ["r"])
        store.cancelJob("r")
        #expect(store.screen == .menu)
        #expect(store.cancelling.contains("r"))
        await waitFor { step.value == 1 }
        await store.refresh()
        await waitFor { store.cancelling.isEmpty }
        #expect(store.waitingJobs.isEmpty)
        #expect(!store.breathing)
        #expect(store.unseenResults.isEmpty)
    }

    @Test func aSignInCancelledElsewhereShowsItsResultOnce() throws {
        let state = try decoded(#"[{"id": "r", "kind": "resign", "state": "cancelled", "result": "You cancelled the sign-in. Nothing changed.", "title": "Lifeline cancelled the sign-in"}]"#)
        #expect(JobFlow.results(state, seen: []).map(\.id) == ["r"])
        #expect(JobFlow.title(try #require(state.jobs?.first), state: state) == "Lifeline cancelled the sign-in")
        #expect(JobFlow.waitingJobs(state).isEmpty)
    }

    @Test func theAddStaysOnThisMacUntilTheDevboxAnswers() async throws {
        let away = jobState("[]")
        let server = try FakeServer { _, _ in [okReply(away)] }
        defer { server.close() }
        let store = store(server)
        await store.refresh()
        store.addTarget = .both
        #expect(store.chosenTarget == .mac)
        store.addTarget = .devbox
        #expect(store.chosenTarget == .mac)
    }

    @Test func aJobThatEndsWhileTheMenuIsClosedPostsItsResultOnce() async throws {
        let step = Counter()
        let running = jobState(#"[{"id": "1", "kind": "add", "state": "running"}, {"id": "2", "kind": "resign", "state": "running"}]"#)
        let ended = jobState(#"[{"id": "1", "kind": "add", "state": "failed", "result": "Your browser signed in as Personal Account (sam@example.org), which Lifeline already has. Nothing changed.", "title": "Lifeline did not add the account"}, {"id": "2", "kind": "resign", "state": "cancelled", "result": "You cancelled the sign-in. Nothing changed.", "title": "Lifeline cancelled the sign-in"}]"#)
        let server = try FakeServer { _, _ in [okReply(step.value == 0 ? running : ended)] }
        defer { server.close() }
        let store = store(server)
        let poster = RecordingPoster()
        let open = Flag()
        open.value = false
        let shownFlag = Flag()
        shownFlag.value = false
        let notifier = Notifier(store: store, shown: { fresh in shownFlag.value ? ShownMenu(model: fresh.model(for: .mac), note: nil) : nil },
                                open: { open.value = true }, defaults: nil)
        notifier.begin(poster, permission: .allowed)
        await store.refresh()
        #expect(poster.posted.filter { $0.id.hasPrefix("job-") }.isEmpty)
        store.cancelJob("2")
        step.value = 1
        await store.refresh()
        let jobs = poster.posted.filter { $0.id.hasPrefix("job-") }
        #expect(jobs.map(\.id) == ["job-1"])
        #expect(jobs.first?.title == "Lifeline did not add the account")
        #expect(jobs.first?.body.hasSuffix("which Lifeline already has. Nothing changed.") == true)
        await store.refresh()
        #expect(poster.posted.filter { $0.id.hasPrefix("job-") }.count == 1)
        notifier.respond(action: UNNotificationDefaultActionIdentifier, info: [:], id: "job-1")
        #expect(open.value)
    }

    @Test func noJobNoticeWhileTheMenuIsOpen() async throws {
        let step = Counter()
        let running = jobState(#"[{"id": "1", "kind": "add", "state": "running"}]"#)
        let ended = jobState(#"[{"id": "1", "kind": "add", "state": "done", "uuid": "o", "result": "Lifeline added Team Account."}]"#)
        let server = try FakeServer { _, _ in [okReply(step.value == 0 ? running : ended)] }
        defer { server.close() }
        let store = store(server)
        let poster = RecordingPoster()
        let notifier = Notifier(store: store, shown: { fresh in ShownMenu(model: fresh.model(for: .mac), note: nil) }, open: {}, defaults: nil)
        notifier.begin(poster, permission: .allowed)
        await store.refresh()
        step.value = 1
        await store.refresh()
        #expect(poster.posted.filter { $0.id.hasPrefix("job-") }.isEmpty)
    }
}

final class Counter: @unchecked Sendable {
    private let lock = NSLock()
    private var number = 0

    var value: Int {
        get {
            lock.lock()
            defer { lock.unlock() }
            return number
        }
        set {
            lock.lock()
            number = newValue
            lock.unlock()
        }
    }
}

@MainActor
struct BreathTests {
    @Test func theLineBreathesOnlyWhileASignInWaits() {
        let view = NSView(frame: NSRect(x: 0, y: 0, width: 40, height: 22))
        Breath.apply(view, on: true, reduceMotion: false)
        let animation = view.layer?.animation(forKey: Breath.key) as? CABasicAnimation
        #expect(animation?.keyPath == "opacity")
        #expect(animation?.autoreverses == true)
        #expect(animation?.repeatCount == .infinity)
        #expect(view.alphaValue == 1)
        Breath.apply(view, on: true, reduceMotion: false)
        #expect(view.layer?.animationKeys()?.filter { $0 == Breath.key }.count == 1)
        Breath.apply(view, on: false, reduceMotion: false)
        #expect(view.layer?.animation(forKey: Breath.key) == nil)
        #expect(view.alphaValue == 1)
        Breath.apply(view, on: true, reduceMotion: true)
        #expect(view.layer?.animation(forKey: Breath.key) == nil)
        #expect(view.alphaValue == Breath.still)
        Breath.apply(view, on: false, reduceMotion: true)
        #expect(view.alphaValue == 1)
    }
}

@MainActor
@Suite(.serialized)
struct JobScreenTests {
    @Test func theWaitingScreenSaysWhatToDoAndOffersBothButtons() throws {
        let state = try decoded("[]")
        var job = JobSummary(id: "1", kind: "add", state: "running", url: "https://claude.ai/oauth/authorize?x=1", opened: true, browser: "Google Chrome")
        job.label = "Add an account"
        job.waiting = "Waiting for you to finish the sign-in in Google Chrome."
        job.lines = ["Sign in there as the account you are adding. If Claude asks you to choose an organization, choose the one you are adding, then press Authorize."]
        let found = try read(JobWaitingView(job: job, state: state))
        #expect(found.contains { $0.contains("Waiting for you to finish the sign-in in Google Chrome") }, "\(found)")
        #expect(!found.contains { $0.hasSuffix("sign-") }, "\(found)")
        #expect(found.contains("Open the sign-in page again"), "\(found)")
        #expect(found.contains("Cancel"), "\(found)")
        #expect(found.contains { $0.contains("choose an organization") }, "\(found)")
    }

    @Test func theWaitingCardSaysWhereAndOffersBothActions() throws {
        var job = try Fixture.job("job-cards", "k-resign")
        job.opened = false
        var opened: [String] = []
        var cancelled: [String] = []
        let card = WaitingCard(job: job, palette: Palette.of(.light), actions: CardActions(open: { opened.append($0) }, cancel: { cancelled.append($0) }))
        let found = try read(card)
        #expect(found.contains { $0.contains("Press Authorize in Safari") }, "\(found)")
        #expect(found.contains { $0.contains("Open the sign-in page") } && !found.contains { $0.contains("page again") }, "\(found)")
        #expect(found.contains { $0.contains("Cancel") }, "\(found)")
        #expect(!found.contains { $0.contains("choose an organization") }, "\(found)")
        let resign = try #require(try Fixture.state("job-cards").jobs?.first { $0.kind == "resign" })
        #expect(JobFlow.cardLines(resign, state: nil) == resign.cardLines)
        #expect(opened.isEmpty && cancelled.isEmpty)
    }

    @Test func theNamesLineIsTrueWhenTheMenuBarShowsOnlyTheLine() throws {
        let text = try Fixture.state("after-mac-add-org").addText
        #expect(SheetText.namesShown(menu: "Alexandra's Account", bar: "Alexandra", barHidden: true, text: text)
            == "The menu shows Alexandra's Account. The menu bar shows Alexandra when you turn its name on in Alerts and settings.")
        #expect(SheetText.namesShown(menu: "Alexandra's Account", bar: "Alexandra", barHidden: false, text: text)
            == "The menu shows Alexandra's Account, and the menu bar shows Alexandra.")
    }

    @Test func theNamingStepReadsTheHiddenBarSentence() throws {
        var state = try decoded(#"[{"id": "n", "kind": "add", "state": "done", "uuid": "o", "result": "Lifeline added Team Account.", "suggested_name": "Team Account", "suggested_short": "Team"}]"#)
        var run = AddRun(target: .mac, kind: .add, stages: [.macAdd])
        run.began("n", at: Date())
        _ = run.advance(state: state, now: Date())
        let hidden = try read(AddAccountView(state: state, step: run.step, run: run, target: .mac, problem: nil, zone: TimeZone(identifier: "UTC")!))
        #expect(hidden.contains { $0.contains("when you turn its name on") }, "\(hidden)")
        state.settings = SettingsSummary(hideName: false)
        let shown = try read(AddAccountView(state: state, step: run.step, run: run, target: .mac, problem: nil, zone: TimeZone(identifier: "UTC")!))
        #expect(!shown.contains { $0.contains("when you turn its name on") }, "\(shown)")
        #expect(shown.contains { $0.contains("and the menu bar shows Team") }, "\(shown)")
    }

    @Test func theRenameStepReadsTheHiddenBarSentence() throws {
        var state = try decoded("[]")
        let account = try #require(state.accounts?.first { $0.uuid == "j" })
        let row = try #require(state.mac?.rows?.first { $0.uuid == "j" })
        func rename(_ state: ServiceState) -> some View {
            AccountExpansion(
                account: account, row: row, state: state, history: .waiting, now: Date(), zone: TimeZone(identifier: "UTC")!, side: .mac,
                handlers: DetailHandlers(), palette: Palette.of(.light), naming: true
            )
        }
        let hidden = try read(rename(state))
        #expect(hidden.contains { $0.contains("when you turn its name on") }, "\(hidden)")
        state.settings = SettingsSummary(hideName: false)
        let shown = try read(rename(state))
        #expect(shown.contains { $0.contains("and the menu bar shows Alex") }, "\(shown)")
    }

    @Test func theResultLeadsTheMenu() throws {
        let state = try decoded(#"[{"id": "1", "kind": "add", "state": "failed", "result": "Your browser signed in as Personal Account (sam@example.org), which Lifeline already has. Nothing changed.", "result_head": "Your browser signed in as Personal Account (sam@example.org), which Lifeline already has.", "result_rest": "Nothing changed."}]"#)
        let results = JobFlow.results(state, seen: [])
        let found = try read(VStack { ForEach(results, id: \.id) { ResultCard(job: $0, palette: Palette.of(.light)) } })
        #expect(found.contains { $0.contains("Your browser signed in as Personal Account") }, "\(found)")
        #expect(!found.contains { $0.contains("Nothing changed") }, "\(found)")
    }

    @Test func theAddScreenSaysTheDevboxIsNotReady() throws {
        let away = try decoded("[]", devbox: #", "devbox": {"name": "Studio", "reachable": false}"#)
        let here = try decoded("[]", devbox: #", "devbox": {"name": "Studio", "reachable": true, "answers": true}"#)
        let sentence = "Lifeline on Studio is not answering"
        let without = try read(AddAccountView(state: away, step: .start, target: .mac, problem: nil, zone: TimeZone(identifier: "UTC")!))
        #expect(without.contains { $0.contains(sentence) }, "\(without)")
        let with = try read(AddAccountView(state: here, step: .start, target: .both, problem: nil, zone: TimeZone(identifier: "UTC")!))
        #expect(!with.contains { $0.contains(sentence) }, "\(with)")
    }
}
