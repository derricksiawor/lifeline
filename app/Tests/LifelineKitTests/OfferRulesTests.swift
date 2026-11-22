import AppKit
import Foundation
import SwiftUI
import Testing
import Vision
@testable import Lifeline
@testable import LifelineKit

private func now() -> String {
    let format = ISO8601DateFormatter()
    format.formatOptions = [.withInternetDateTime]
    return format.string(from: Date())
}

private func fixtureJob(_ name: String, _ id: String) throws -> [String: Any] {
    guard let found = try Fixture.jobs(name).first(where: { $0["id"] as? String == id }) else {
        throw CocoaError(.fileReadNoSuchFile)
    }
    return found
}

struct CoreStateTests {
    @Test func everyFixtureIsTheCoresOwnState() throws {
        for name in Fixture.names {
            let object = try Fixture.object(name)
            #expect(object["version"] != nil && object["devbox"] != nil && object["accounts"] != nil && object["settings_text"] != nil, "\(name)")
            let state = try Fixture.state(name)
            #expect(state.settingsText?.autoSwitch?.title == "Switch this Mac for me before an account runs out", "\(name)")
            for job in state.jobs ?? [] where JobFlow.isRunning(job) {
                #expect(!(job.card ?? "").isEmpty, "\(name) \(job.id)")
            }
        }
    }
}

struct CoreSentenceTests {
    @Test func theAutoSwitchRowDrawsTheCoresSentences() throws {
        let state = try Fixture.state("devbox-live-added")
        let local = LocalStatus(notices: .allowed, shortcut: .on, login: .on)
        let row = try #require(SettingsPage.sections(state: state, local: local).flatMap(\.rows).first { $0.id == "auto_switch" })
        #expect(!row.title.isEmpty && !row.description.isEmpty)
        #expect(row.title == state.settingsText?.autoSwitch?.title && row.description == state.settingsText?.autoSwitch?.description)
        #expect(row.description.contains("99%"))
        #expect(!row.description.contains("Undo stays"))
    }

    @Test func theMacAdoptFollowsTheCoresReadiness() throws {
        let ready = try Fixture.state("mac-live-unadded")
        let unready = try Fixture.state("mac-live-unready")
        #expect(AddFlow.canAdopt(ready) && !AddFlow.canAdopt(unready))
        #expect(AddFlow.stages(target: .mac, kind: .adopt, state: unready).isEmpty)
        #expect(AddFlow.adoptTitle(AddFlow.stages(target: .mac, kind: .adopt, state: ready), state: ready) == ready.mac?.live?.adoptTitle)
    }
}

struct OfferRuleMoreTests {
    @Test func aDevboxAdoptNeedsNoDevboxSignIn() throws {
        let state = try Fixture.state("devbox-no-login")
        #expect(state.devbox?.canSignIn == false)
        #expect(AddFlow.canAdoptDevbox(state))
        #expect(!AddFlow.available(.both, state: state) && !AddFlow.available(.devbox, state: state))
        #expect(AddFlow.usable(.both, state: state) && AddFlow.usable(.devbox, state: state) && AddFlow.usable(.mac, state: state))
        #expect(!AddFlow.addPossible(.both, pick: nil, state: state) && !AddFlow.addPossible(.devbox, pick: nil, state: state))
        #expect(AddFlow.addPossible(.mac, pick: nil, state: state))
        #expect(AddFlow.defaultTarget(state) == .mac)
        #expect(AddFlow.stages(target: .devbox, kind: .adopt, state: state) == [.devboxAdopt])
        #expect(AddFlow.adoptTitle(AddFlow.stages(target: .devbox, kind: .adopt, state: state), state: state) == "Add Personal Account from Studio")
        let failed = try Fixture.job("failed-jobs", "fo")
        #expect(JobFlow.retryKind(failed, state: state) == .adopt)
        #expect(JobFlow.retryTarget(failed) == .devbox)
        #expect(AddFlow.retry(step: .failed("No."), run: AddRun(target: .devbox, kind: .adopt, stages: [.devboxAdopt]), state: state) == .adopt)
        #expect(AddFlow.retry(step: .failed("No."), run: AddRun(target: .both, kind: .add, stages: [.macAdd, .devboxAdd]), state: state) == nil)
    }

    @Test func theWorkAddStillWorksWhenTheDevboxCannotSignIn() throws {
        let state = try Fixture.state("devbox-work-no-login")
        let work = try #require(AddFlow.devboxUses(state))
        #expect(AddFlow.addPossible(.both, pick: work, state: state))
        #expect(!AddFlow.addPossible(.both, pick: nil, state: state))
        #expect(AddFlow.defaultTarget(state) == .both)
        #expect(AddFlow.usable(.both, state: state))
        #expect(AddFlow.stages(target: .both, kind: .add, state: state, pick: work) == [.macAdd, .devboxAdopt])
        let ready = try Fixture.state("devbox-live-added")
        #expect(AddFlow.addPossible(.both, pick: nil, state: ready) && AddFlow.usable(.both, state: ready))
        let away = try Fixture.state("devbox-away")
        #expect(!AddFlow.usable(.both, state: away) && !AddFlow.usable(.devbox, state: away))
        let busy = try Fixture.state("devbox-job-elsewhere")
        #expect(!AddFlow.usable(.both, state: busy))
    }

    @Test func aSkippedStepTwoShowsTheCoresAlreadySentence() throws {
        var held = AddRun(target: .both, kind: .add, stages: [.macAdd, .devboxAdd])
        held.began("m1", at: Date(timeIntervalSince1970: 5_000))
        let saved = try Fixture.state("after-mac-add-held")
        _ = held.advance(state: saved, now: Date(timeIntervalSince1970: 5_002))
        #expect(held.devboxSkipped)
        let account = try #require(saved.accounts?.first { $0.uuid == Fixture.work })
        let line = AddFlow.addedLine(account, run: held)
        #expect(line.contains("Studio already has its own sign-in for Work Account."), "\(line)")
        #expect(line.hasPrefix("It is signed in on this Mac and on Studio."), "\(line)")
    }

    @Test func theStepTwoCardShowsStepOnesResultUntilItsOwnArrives() throws {
        let unadded = try Fixture.state("devbox-work-unadded")
        let work = try #require(AddFlow.devboxUses(unadded))
        var run = AddRun(target: .both, kind: .add, stages: [.macAdd, .devboxAdopt], pick: work)
        run.began("m1", at: Date(timeIntervalSince1970: 5_000))
        let afterOne = try Fixture.state("devbox-work-step2", jobs: [try fixtureJob("devbox-work-step2", "m1")])
        #expect(run.advance(state: afterOne, now: Date(timeIntervalSince1970: 5_002)) == .start(.devboxAdopt))
        let held = try #require(AddFlow.card(run, state: afterOne))
        #expect(held.id.isEmpty && held.kind == "devbox-adopt")
        let result = try #require(try fixtureJob("devbox-work-step2", "m1")["result"] as? String)
        #expect(JobFlow.cardSentence(held) == result)
        #expect(result == "Lifeline added Work Account as number 2.")
        #expect(!JobFlow.cardSentence(held).contains("Authorize"))
        run.began("d1", at: Date(timeIntervalSince1970: 5_003))
        let own = try #require(AddFlow.card(run, state: try Fixture.state("devbox-work-step2")))
        #expect(JobFlow.cardSentence(own) == "Step 2 of 2: Lifeline on Studio is taking over the sign-in its Claude Code holds for Work Account.")
    }
}

struct OfferRuleLastTests {
    @Test func everyMacAdoptNamesTheAccountItExpects() throws {
        let state = try Fixture.state("mac-live-unadded")
        let mac = try #require(state.mac?.live?.uuid)
        let single = AddRun(target: .mac, kind: .adopt, stages: [.macAdopt], pick: nil, macExpected: mac)
        #expect(single.body(for: .macAdopt)["expected"] as? String == mac)
        #expect(single.body(for: .macAdopt)["step"] == nil)
        let both = AddRun(target: .both, kind: .adopt, stages: [.macAdopt, .devboxAdopt], pick: nil, expected: Fixture.personal, macExpected: mac)
        #expect(both.body(for: .macAdopt)["expected"] as? String == mac && both.body(for: .macAdopt)["step"] as? [Int] == [1, 2])
        #expect(both.body(for: .devboxAdopt)["expected"] as? String == Fixture.personal)
    }

    @Test func tryAgainRepeatsOnlyTheFailedAdoptSteps() throws {
        let adoptable = try Fixture.state("mac-live-unadded")
        #expect(AddFlow.canAdopt(adoptable) && AddFlow.canAdoptDevbox(adoptable))
        var devboxOnly = AddRun(target: .both, kind: .adopt, stages: [.devboxAdopt])
        _ = devboxOnly.refused("No.")
        #expect(AddFlow.retry(step: devboxOnly.step, run: devboxOnly, state: adoptable) == .adopt)
        #expect(AddFlow.retryStages(devboxOnly) == [.devboxAdopt])
        #expect(AddFlow.retry(step: devboxOnly.step, run: devboxOnly, state: try Fixture.state("devbox-live-held")) == nil)
        var both = AddRun(target: .both, kind: .adopt, stages: [.macAdopt, .devboxAdopt])
        _ = both.refused("No.")
        #expect(AddFlow.retryStages(both) == [.macAdopt, .devboxAdopt])
        #expect(AddFlow.retry(step: both.step, run: both, state: try Fixture.state("devbox-live-added")) == nil)
        #expect(AddFlow.retry(step: both.step, run: both, state: adoptable) == .adopt)
    }
}

struct OfferRuleTests {
    @Test func anotherRunningAddHidesStartingOne() throws {
        let busy = try Fixture.state("add-elsewhere")
        #expect(AddFlow.addBusy(busy, run: nil)?.id == "cli")
        let failed = try Fixture.job("failed-jobs", "fa")
        #expect(JobFlow.retryKind(failed, state: busy) == nil)
        #expect(AddFlow.retry(step: .failed("No."), run: AddRun(target: .mac, kind: .add, stages: [.macAdd]), state: busy) == nil)
        var mine = AddRun(target: .mac, kind: .add, stages: [.macAdd])
        mine.began("cli", at: Date())
        #expect(AddFlow.addBusy(busy, run: mine) == nil)
        #expect(JobFlow.retryKind(failed, state: try Fixture.state("devbox-work-unadded")) == .add)
    }

    @Test func aRunningDevboxJobHidesBothDevboxAndTheAdopt() throws {
        let busy = try Fixture.state("devbox-job-elsewhere")
        let free = try Fixture.state("devbox-live-added")
        #expect(AddFlow.canAdoptDevbox(free) && !AddFlow.canAdoptDevbox(busy))
        #expect(AddFlow.available(.both, state: free) && AddFlow.available(.devbox, state: free))
        #expect(!AddFlow.available(.both, state: busy) && !AddFlow.available(.devbox, state: busy) && AddFlow.available(.mac, state: busy))
        #expect(!AddFlow.stages(target: .both, kind: .adopt, state: busy).contains(.devboxAdopt))
        #expect(AddFlow.retry(step: .failed("No."), run: AddRun(target: .both, kind: .add, stages: [.macAdd, .devboxAdd]), state: busy) == nil)
        #expect(AddFlow.retry(step: .failed("No."), run: AddRun(target: .mac, kind: .add, stages: [.macAdd]), state: busy) == .add)
        let failedDevbox = try Fixture.job("failed-jobs", "fd")
        #expect(JobFlow.retryKind(failedDevbox, state: busy) == nil)
        #expect(JobFlow.retryKind(failedDevbox, state: try Fixture.state("failed-jobs")) == .add)
        let heldOne = JobSummary(id: "h", kind: "devbox-add", uuid: Fixture.work, state: "failed", result: "No.")
        #expect(JobFlow.retryKind(heldOne, state: free) == nil)
        var own = AddRun(target: .both, kind: .add, stages: [.macAdd, .devboxAdd])
        own.began("dr", at: Date())
        #expect(AddFlow.available(.both, state: busy, run: own))
    }

    @Test func theStepCardNamesNothingOfItsOwnAndOnlyKeepsAnAddRunsAccount() throws {
        let start = Date(timeIntervalSince1970: 5_000)
        var both = AddRun(target: .both, kind: .adopt, stages: [.macAdopt, .devboxAdopt], pick: nil, expected: Fixture.personal)
        both.began("a1", at: start)
        let macDone = try Fixture.state("devbox-live-added", jobs: [["id": "a1", "kind": "adopt", "uuid": Fixture.alex, "state": "done", "started_at": now()]])
        #expect(both.advance(state: macDone, now: start) == .start(.devboxAdopt))
        #expect(both.body(for: .devboxAdopt)["expected"] as? String == Fixture.personal)
        let pending = try #require(AddFlow.card(both, state: macDone))
        #expect(pending.uuid == nil && pending.kind == "devbox-adopt" && JobFlow.cardSentence(pending).isEmpty)
        both.began("d1", at: start)
        var running = try fixtureJob("job-cards", "k-devbox-adopt-step")
        running["id"] = "d1"
        let runningState = try Fixture.state("devbox-live-added", jobs: [running])
        let card = try #require(AddFlow.card(both, state: runningState))
        #expect(card.uuid == Fixture.personal)
        #expect(JobFlow.cardSentence(card) == "Step 2 of 2: Lifeline on Studio is taking over the sign-in its Claude Code holds for Personal Account.")

        var single = AddRun(target: .devbox, kind: .adopt, stages: [.devboxAdopt])
        single.began("d1", at: start)
        #expect(AddFlow.card(single, state: runningState) == nil)
    }

    @Test func aStaleDevboxAccountNeverPicksTheAdopt() throws {
        let stale = try Fixture.state("devbox-gone-stale")
        #expect(stale.devbox?.answers == false && stale.devbox?.accountUuid == Fixture.personal && stale.devbox?.live == nil)
        #expect(AddFlow.secondStage(for: Fixture.personal, planned: .devboxAdd, state: stale) == .devboxAdd)
    }

    @Test func aSlowStartFindsTheJobTheCoreStarted() throws {
        let sent = Date()
        let state = try Fixture.state("devbox-work-unadded", jobs: [
            ["id": "old", "kind": "add", "state": "running", "started_at": "2026-11-20T09:00:00Z"],
            ["id": "new", "kind": "add", "state": "running", "started_at": now()],
        ])
        #expect(AddFlow.startedJob(.macAdd, state: state, run: nil, since: sent)?.id == "new")
        #expect(AddFlow.startedJob(.devboxAdd, state: state, run: nil, since: sent) == nil)
        #expect(Sentences.unknownResult(Sentences.slowStart))
    }

    @Test func theCoresAdoptTitlesStayInsideTheirButton() throws {
        let width = Metrics.panelWidth - 2 * Metrics.sheetPadding
        var titles: [String] = []
        for name in ["devbox-live-added", "devbox-work-unadded", "mac-live-unadded", "devbox-away"] {
            let state = try Fixture.state(name)
            titles += [state.devbox?.live?.adoptTitle, state.devbox?.live?.bothTitle, state.mac?.live?.adoptTitle].compactMap { $0 }
        }
        #expect(titles.count >= 6)
        for title in titles {
            let fit = ButtonFit.layout(title, style: Styles.action, width: width, height: Metrics.actionHeight)
            #expect(fit.room == width - 2 * Metrics.labelInset, "\(title)")
            #expect(fit.lines == 1 || fit.height >= CGFloat(fit.lines) * Styles.action.lineHeight + 2 * Metrics.labelInset, "\(title)")
        }
    }
}

private final class Script: @unchecked Sendable {
    private let lock = NSLock()
    private var jobs: [[String: Any]]?
    private var count = 0
    var base = "devbox-work-unadded"
    var delay: TimeInterval = 0
    var refuseAdopt: String?
    var refuseMacAdopt: String?

    var current: [[String: Any]] {
        get { lock.withLock { jobs } ?? ((try? Fixture.jobs(base)) ?? []) }
        set { lock.withLock { jobs = newValue } }
    }

    func next() -> Int {
        lock.withLock {
            count += 1
            return count
        }
    }
}

@MainActor
private func settle(_ store: StateStore, until check: () -> Bool) async {
    for _ in 0..<100 where !check() {
        await store.refresh()
        try? await Task.sleep(for: .milliseconds(20))
    }
}

@MainActor
private func lines(_ view: some View) async throws -> [String] {
    NSApplication.shared.setActivationPolicy(.prohibited)
    _ = testFonts
    let renderer = ImageRenderer(content: view.background(Color.white).environment(\.colorScheme, .light))
    renderer.scale = 2
    guard let image = renderer.cgImage else { return [] }
    let box = ShotBox(image: image)
    return try await Task.detached { try read(box) }.value
}

private struct ShotBox: @unchecked Sendable {
    let image: CGImage
}

private func read(_ box: ShotBox) throws -> [String] {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = false
    request.recognitionLanguages = ["en-US"]
    onCPU(request)
    try performText(request, on: box.image)
    let lookalikes: [Character: Character] = ["В": "B", "а": "a", "с": "c", "к": "k", "е": "e", "о": "o", "\u{2019}": "'"]
    return (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }.map { String($0.map { lookalikes[$0] ?? $0 }) }
}

@MainActor
@Suite(.serialized)
struct OfferStoreTests {
    private func served(_ script: Script) throws -> FakeServer {
        let cancelled = try fixtureJob("failed-jobs", "fc")["result"] as? String ?? ""
        return try FakeServer { head, _ in
            if head.hasPrefix("POST /jobs/"), let id = head.split(separator: " ").dropFirst().first?.split(separator: "/").dropFirst().first.map(String.init) {
                script.current = script.current.map { job in
                    guard job["id"] as? String == id, job["state"] as? String == "running" else { return job }
                    var ended = job
                    ended["state"] = "cancelled"
                    ended["result"] = cancelled
                    return ended
                }
                return [okReply(#"{"cancelled": true}"#)]
            }
            if head.hasPrefix("POST /adopt"), let refusal = script.refuseMacAdopt {
                let body = (try? JSONSerialization.data(withJSONObject: ["error": refusal])) ?? Data()
                return [okReply(String(decoding: body, as: UTF8.self), status: "409 Conflict")]
            }
            if head.hasPrefix("POST /devbox/adopt"), let refusal = script.refuseAdopt {
                let body = (try? JSONSerialization.data(withJSONObject: ["error": refusal])) ?? Data()
                return [okReply(String(decoding: body, as: UTF8.self), status: "409 Conflict")]
            }
            if head.hasPrefix("POST /add") || head.hasPrefix("POST /devbox/add") || head.hasPrefix("POST /adopt") || head.hasPrefix("POST /devbox/adopt") {
                if script.delay > 0 {
                    Thread.sleep(forTimeInterval: script.delay)
                }
                let id = "j\(script.next())"
                let kind = head.hasPrefix("POST /devbox/add") ? "devbox-add" : head.hasPrefix("POST /add") ? "add" : head.hasPrefix("POST /devbox/adopt") ? "devbox-adopt" : "adopt"
                script.current = script.current + [["id": id, "kind": kind, "state": "running", "started_at": now()]]
                return [okReply(#"{"job": "\#(id)"}"#, status: "202 Accepted")]
            }
            let text = (try? Fixture.text(script.base, jobs: script.current)) ?? "{}"
            return [okReply(text)]
        }
    }

    private func fail(_ script: Script, _ id: String) throws {
        let result = try fixtureJob("failed-jobs", "fa")["result"] as? String ?? ""
        script.current = script.current.map { job in
            guard job["id"] as? String == id else { return job }
            var ended = job
            ended["state"] = "failed"
            ended["result"] = result
            return ended
        }
    }

    private func finish(_ script: Script, _ id: String, uuid: String) throws {
        let result = try fixtureJob("devbox-work-step2", "m1")["result"] as? String ?? ""
        script.current = script.current.map { job in
            guard job["id"] as? String == id else { return job }
            var ended = job
            ended["state"] = "done"
            ended["uuid"] = uuid
            ended["result"] = result
            return ended
        }
    }

    private func posts(_ server: FakeServer, _ route: String) -> [String] {
        server.requests.filter { $0.head.hasPrefix("POST " + route + " ") }.map { String(decoding: $0.body, as: UTF8.self) }
    }

    @Test func cancelOnStepTwoEndsOnTheNamesAndEveryControlStillResponds() async throws {
        let script = Script()
        let server = try served(script)
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        store.opened()
        store.menuOpen = true
        store.screen = .add
        store.startAdd(.add)
        await waitFor { store.addRun?.job == "j1" }
        try finish(script, "j1", uuid: Fixture.work)
        await settle(store) { store.addRun?.job == "j2" }
        #expect(posts(server, "/devbox/adopt") == [#"{"expected":"\#(Fixture.work)","step":[2,2]}"#])
        await settle(store) { store.addCard?.id == "j2" }
        let card = try #require(store.addCard)
        #expect(card.kind == "devbox-adopt" && JobFlow.cancellable(card) && !JobFlow.hasActions(card))
        let offered = try await lines(CompactWaiting(job: card, state: store.state))
        #expect(offered.contains { $0.contains("Cancel") }, "\(offered)")
        #expect(!offered.contains { $0.hasPrefix("Open the sign-in page") }, "\(offered)")
        store.cancelJob("j2")
        #expect(store.addRun != nil && store.screen == .add && store.cancelling.contains("j2"))
        let cancelling = try await lines(CompactWaiting(job: card, state: store.state, cancelling: true))
        #expect(cancelling.joined(separator: " ").contains("Cancelling the sign-in."), "\(cancelling)")
        #expect(!cancelling.contains { $0.contains("Cancel") && !$0.contains("Cancelling") }, "\(cancelling)")
        await settle(store) { store.addStep == .added(Fixture.work) }
        #expect(store.addStep == .added(Fixture.work))
        #expect(posts(server, "/jobs/j2/cancel").count == 1)
        #expect(store.addRun?.devboxResult == "You cancelled the sign-in. Nothing changed." && store.addRun?.devboxFailed == true)
        await settle(store) { store.cancelling.isEmpty }
        #expect(store.cancelling.isEmpty)
        store.backFromAdd()
        #expect(store.screen == .menu && store.addRun == nil)
        store.screen = .add
        store.startAdd(.add)
        await waitFor { store.addRun?.job == "j3" }
        #expect(posts(server, "/add").count == 2)
    }

    @Test func cancelOnStepTwoOfATwoMachineAdoptEndsOnTheNames() async throws {
        let script = Script()
        script.base = "mac-live-unadded"
        let server = try served(script)
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        store.opened()
        store.menuOpen = true
        store.screen = .add
        store.addTarget = .both
        let mac = try #require(store.state?.mac?.live?.uuid)
        store.startAdd(.adopt)
        await waitFor { store.addRun?.job == "j1" }
        #expect(store.addRun?.stages == [.macAdopt, .devboxAdopt])
        #expect(posts(server, "/adopt") == [#"{"expected":"\#(mac)","step":[1,2]}"#])
        try finish(script, "j1", uuid: mac)
        await settle(store) { store.addRun?.job == "j2" }
        #expect(posts(server, "/devbox/adopt") == [#"{"expected":"\#(Fixture.personal)","step":[2,2]}"#])
        store.cancelJob("j2")
        #expect(store.addRun != nil && store.addNote == nil && store.cancelling.contains("j2"))
        await settle(store) { store.addStep == .added(mac) }
        #expect(store.addStep == .added(mac))
        #expect(store.addRun?.devboxFailed == true && store.addRun?.devboxResult == "You cancelled the sign-in. Nothing changed.")
        #expect(store.addNote != JobFlow.cancelled(store.state))
        #expect(posts(server, "/jobs/j2/cancel").count == 1)
    }

    @Test func tryAgainOnAFailedDevboxAdoptStaysOnTheDevboxWhenItCannotSignIn() async throws {
        let script = Script()
        script.base = "mac-unadded-no-login"
        script.current = [try fixtureJob("failed-jobs", "fo")]
        let server = try served(script)
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        store.opened()
        store.menuOpen = true
        let failed = try #require(store.state?.jobs?.first { $0.id == "fo" })
        #expect(JobFlow.retryKind(failed, state: store.state) == .adopt)
        store.retry(failed)
        await waitFor { store.addRun?.job != nil }
        #expect(store.addRun?.target == .devbox && store.addRun?.stages == [.devboxAdopt])
        #expect(posts(server, "/devbox/adopt") == [#"{"expected":"\#(Fixture.personal)"}"#])
        #expect(posts(server, "/adopt").isEmpty)
        try fail(script, "j1")
        await settle(store) { store.addRun?.finished == true }
        #expect(AddFlow.retry(step: store.addStep, run: store.addRun, state: store.state) == .adopt)
        store.retryAdd(.adopt)
        await waitFor { store.addRun?.job == "j2" }
        #expect(store.addRun?.target == .devbox && store.addRun?.stages == [.devboxAdopt])
        #expect(posts(server, "/adopt").isEmpty && posts(server, "/devbox/adopt").count == 2)
    }

    @Test func aMacAdoptSendsTheExpectedAccountAndShowsTheCoresMismatch() async throws {
        let script = Script()
        script.base = "mac-live-unadded"
        let refusal = try fixtureJob("failed-jobs", "fm")["result"] as? String ?? ""
        let server = try served(script)
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        store.screen = .add
        store.addTarget = .mac
        let mac = try #require(store.state?.mac?.live?.uuid)
        store.startAdd(.adopt)
        await waitFor { store.addRun?.job == "j1" }
        #expect(posts(server, "/adopt") == [#"{"expected":"\#(mac)"}"#])
        script.refuseMacAdopt = refusal
        try fail(script, "j1")
        await settle(store) { store.addRun?.finished == true }
        store.retryAdd(.adopt)
        await waitFor { store.addStep == .failed(refusal) }
        #expect(store.addStep == .failed(refusal))
        #expect(refusal == "Claude Code on this Mac uses Ama's Account, not Alex's Account, so nothing was changed.")
        #expect(posts(server, "/adopt") == [#"{"expected":"\#(mac)"}"#, #"{"expected":"\#(mac)"}"#])
    }

    @Test func tryAgainOnADevboxAdoptStartedAsBothNeverAddsAMacAdopt() async throws {
        let script = Script()
        script.base = "devbox-live-added"
        let server = try served(script)
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        store.opened()
        store.menuOpen = true
        store.screen = .add
        store.addTarget = .both
        store.startAdd(.adopt)
        await waitFor { store.addRun?.job == "j1" }
        #expect(store.addRun?.stages == [.devboxAdopt])
        try fail(script, "j1")
        await settle(store) { store.addRun?.finished == true }
        script.base = "mac-live-unadded"
        await settle(store) { AddFlow.canAdopt(store.state) }
        #expect(AddFlow.canAdopt(store.state))
        #expect(AddFlow.retry(step: store.addStep, run: store.addRun, state: store.state) == .adopt)
        store.retryAdd(.adopt)
        await waitFor { store.addRun?.job == "j2" }
        #expect(store.addRun?.stages == [.devboxAdopt] && store.addRun?.target == .both)
        #expect(posts(server, "/adopt").isEmpty)
        #expect(posts(server, "/devbox/adopt").count == 2)
    }

    @Test func theDevboxAdoptIsOfferedWhileTheDevboxCannotSignIn() async throws {
        let script = Script()
        script.base = "devbox-no-login"
        let server = try served(script)
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        store.screen = .add
        #expect(store.chosenTarget == .mac)
        store.addTarget = .devbox
        #expect(store.chosenTarget == .devbox)
        let seen = try await lines(AddAccountView(state: store.state, step: store.addStep, run: store.addRun, target: store.chosenTarget, problem: nil, zone: .current, handlers: AddHandlers(back: {}), pick: store.chosenPick))
        #expect(seen.contains { $0.contains("Add Personal Account from Studio") }, "\(seen)")
        #expect(!seen.contains("Open the sign-in page"), "\(seen)")
        store.addTarget = .mac
        let mac = try await lines(AddAccountView(state: store.state, step: store.addStep, run: store.addRun, target: store.chosenTarget, problem: nil, zone: .current, handlers: AddHandlers(back: {}), pick: store.chosenPick))
        #expect(mac.contains("Open the sign-in page"), "\(mac)")
    }

    @Test func cancelOnASingleDevboxAdoptGoesBackToALiveStart() async throws {
        let script = Script()
        script.base = "devbox-live-added"
        let server = try served(script)
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        store.opened()
        store.menuOpen = true
        store.screen = .add
        store.addTarget = .devbox
        store.startAdd(.adopt)
        await waitFor { store.addRun?.job == "j1" }
        #expect(store.addRun?.stages == [.devboxAdopt])
        #expect(posts(server, "/devbox/adopt") == [#"{"expected":"\#(Fixture.personal)"}"#])
        store.cancelJob("j1")
        #expect(store.addRun == nil && store.addNote == JobFlow.cancelling(store.state) && store.screen == .add)
        #expect(store.addNote == "Cancelling the sign-in.")
        await settle(store) { store.addNote == JobFlow.cancelled(store.state) }
        #expect(store.addNote == "You cancelled the sign-in. Nothing changed.")
        #expect(posts(server, "/jobs/j1/cancel").count == 1)
        store.startAdd(.adopt)
        await waitFor { store.addRun?.job == "j2" }
        #expect(posts(server, "/devbox/adopt").count == 2)
    }

    @Test func aMismatchedAdoptShowsTheCoresSentenceAsGiven() async throws {
        let script = Script()
        script.base = "devbox-live-added"
        let refusal = try fixtureJob("failed-jobs", "fo")["result"] as? String ?? ""
        script.refuseAdopt = refusal
        let server = try served(script)
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        store.screen = .add
        store.addTarget = .devbox
        store.startAdd(.adopt)
        await waitFor { store.addRun?.finished == true }
        #expect(store.addStep == .failed(refusal))
        #expect(refusal == "Studio's Claude Code uses Work Account, not Personal Account, so nothing was changed.")
    }

    @Test func tryAgainOnTheMenuRepeatsTheFailedAccountAndTarget() async throws {
        let script = Script()
        let server = try served(script)
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        store.opened()
        store.menuOpen = true
        store.screen = .add
        #expect(store.chosenPick?.name == "Work Account" && store.chosenTarget == .both)
        store.addPick = nil
        store.addTarget = .mac
        store.startAdd(.add)
        await waitFor { store.addRun?.job == "j1" }
        #expect(posts(server, "/add") == ["{}"])
        try fail(script, "j1")
        await settle(store) { store.addRun?.finished == true }
        store.backFromAdd()
        store.opened()
        store.menuOpen = true
        #expect(store.addRun == nil)
        let failed = try #require(store.state?.jobs?.first { $0.id == "j1" })
        #expect(JobFlow.retryKind(failed, state: store.state) == .add)
        store.retry(failed)
        await waitFor { store.addRun?.job == "j2" }
        #expect(posts(server, "/add") == ["{}", "{}"])
        #expect(store.addRun?.target == .mac && store.addRun?.stages == [.macAdd])
        #expect(store.addRun?.pick == nil)
        #expect(posts(server, "/devbox/add").isEmpty && posts(server, "/devbox/adopt").isEmpty)
    }

    @Test func tryAgainOnTheSheetRepeatsTheFailedRunAndANewChoiceClearsIt() async throws {
        let script = Script()
        let server = try served(script)
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        store.opened()
        store.menuOpen = true
        store.screen = .add
        store.startAdd(.add)
        await waitFor { store.addRun?.job == "j1" }
        try fail(script, "j1")
        await settle(store) { store.addRun?.finished == true }
        #expect(AddFlow.retry(step: store.addStep, run: store.addRun, state: store.state) == .add)
        store.retryAdd(.add)
        await waitFor { store.addRun?.job == "j2" }
        let body = #"{"known":"Work Account","step":[1,2]}"#
        #expect(posts(server, "/add") == [body, body])
        #expect(store.addRun?.target == .both && store.addRun?.stages == [.macAdd, .devboxAdopt])
        try fail(script, "j2")
        await settle(store) { store.addRun?.finished == true }
        store.addPick = nil
        #expect(store.addRun == nil && store.addStep == .start && store.chosenPick == nil)
    }

    @Test func tryAgainOnAnAddFromElsewhereOnlyOpensTheStartWithItsAccount() async throws {
        let script = Script()
        script.current = [try fixtureJob("failed-jobs", "fa")]
        let server = try served(script)
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        store.opened()
        store.menuOpen = true
        let failed = try #require(store.state?.jobs?.first)
        store.retry(failed)
        #expect(store.screen == .add)
        #expect(store.addRun == nil)
        #expect(store.chosenPick?.name == "Work Account")
        try await Task.sleep(for: .milliseconds(200))
        #expect(posts(server, "/add").isEmpty)
    }

    @Test func tryAgainOnAFailedDevboxStepRetriesOnlyThatStep() async throws {
        let script = Script()
        script.base = "failed-jobs"
        let server = try served(script)
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        store.opened()
        store.menuOpen = true
        let failed = try #require(store.state?.jobs?.first { $0.id == "fd" })
        store.retry(failed)
        await waitFor { store.addRun?.job != nil }
        #expect(posts(server, "/devbox/add") == [#"{"uuid":"\#(Fixture.org)"}"#])
        #expect(posts(server, "/add").isEmpty)
        #expect(store.addRun?.target == .both && store.addRun?.stages == [.devboxAdd])
    }

    @Test func tryAgainThatCannotWorkDoesNothing() async throws {
        let script = Script()
        script.current = [try fixtureJob("failed-jobs", "fo")]
        let server = try served(script)
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        store.opened()
        store.menuOpen = true
        let failed = try #require(store.state?.jobs?.first)
        store.retry(failed)
        #expect(store.screen == .menu)
        #expect(store.addRun == nil)
        try await Task.sleep(for: .milliseconds(200))
        #expect(server.requests.allSatisfy { !$0.head.hasPrefix("POST") })
    }

    @Test func anAddRunningElsewhereReplacesOpenWithItsCard() async throws {
        let script = Script()
        script.base = "add-elsewhere"
        let server = try served(script)
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        store.screen = .add
        let seen = try await lines(AddAccountView(state: store.state, step: store.addStep, run: store.addRun, target: store.chosenTarget, problem: nil, zone: .current, handlers: AddHandlers(back: {}), pick: store.chosenPick))
        #expect(!seen.contains("Open the sign-in page"), "\(seen)")
        #expect(seen.contains { $0.hasPrefix("Open the sign-in page again") }, "\(seen)")
        #expect(seen.joined(separator: " ").lowercased().contains("press authorize in safari"), "\(seen)")
        script.current = []
        await store.refresh()
        let free = try await lines(AddAccountView(state: store.state, step: store.addStep, run: store.addRun, target: store.chosenTarget, problem: nil, zone: .current, handlers: AddHandlers(back: {}), pick: store.chosenPick))
        #expect(free.contains("Open the sign-in page"), "\(free)")
    }

    @Test func aDevboxJobRunningElsewhereKeepsTheAddOnThisMac() async throws {
        let script = Script()
        script.base = "devbox-job-elsewhere"
        let server = try served(script)
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        store.screen = .add
        #expect(store.chosenTarget == .mac)
        store.addTarget = .both
        #expect(store.chosenTarget == .mac)
        let seen = try await lines(AddAccountView(state: store.state, step: store.addStep, run: store.addRun, target: store.chosenTarget, problem: nil, zone: .current, handlers: AddHandlers(back: {}), pick: store.chosenPick))
        #expect(!seen.contains { $0.hasPrefix("Add Personal Account") }, "\(seen)")
        #expect(seen.joined(separator: " ").lowercased().contains("sign studio in to"), "\(seen)")
        store.startAdd(.add)
        await waitFor { store.addRun?.job != nil }
        #expect(store.addRun?.stages == [.macAdd])
    }

    @Test func aStartThatAnswersLateFollowsTheJobTheCoreStarted() async throws {
        let script = Script()
        script.delay = 0.8
        let server = try served(script)
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        store.startLimit = 0.3
        await store.refresh()
        store.screen = .add
        store.addPick = nil
        store.addTarget = .mac
        store.startAdd(.add)
        await waitFor { store.addRun?.job != nil || store.addRun?.finished == true }
        #expect(store.addRun?.job == "j1")
        #expect(store.addRun?.finished == false)
    }
}
