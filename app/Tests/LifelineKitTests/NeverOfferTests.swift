import AppKit
import Foundation
import SwiftUI
import Testing
import Vision
@testable import Lifeline
@testable import LifelineKit

struct NeverOfferRuleTests {
    @Test func devboxLiveDecodesTheCoresKeys() throws {
        #expect(try Fixture.state("devbox-away").devbox?.live == nil)
        let tonight = try #require(try Fixture.state("devbox-work-unadded").devbox)
        let live = try #require(tonight.live)
        #expect(tonight.canSignIn == true)
        #expect(live.uuid == Fixture.work && live.added == false && live.known == true && live.adoptReady == false && live.held == false)
        #expect(live.name == "Work Account" && live.short == "Work" && live.organization == "personal")
        #expect(live.note == "Studio already uses Work Account, so you sign in once, here on this Mac. Studio keeps its own sign-in.")
        #expect(live.adoptTitle == "Add Work Account from Studio" && live.bothTitle == "Add both machines' own sign-ins")
        let added = try #require(try Fixture.state("devbox-live-added").devbox?.live)
        #expect(added.added == true && added.adoptReady == true && added.held == false && added.note == nil)
        #expect(try Fixture.state("devbox-live-held").devbox?.live?.held == true)
        #expect(try Fixture.state("devbox-no-login").devbox?.canSignIn == false)
        #expect(try Fixture.state("devbox-away").devbox?.canSignIn == nil)
        let mac = try #require(try Fixture.state("mac-live-unadded").mac?.live)
        #expect(mac.added == false && mac.adoptReady == true && mac.adoptTitle == "Add this Mac's own sign-in")
        let held = try Fixture.state("devbox-live-held")
        #expect(held.accounts?.first { $0.uuid == Fixture.personal }?.devboxAlready == "Studio already has its own sign-in for Personal Account.")
        #expect(try Fixture.state("devbox-live-added").accounts?.first { $0.uuid == Fixture.org }?.devboxAlready == nil)
        #expect(try Fixture.state("devbox-work-step1").jobs?.first?.card == "Step 1 of 2: press Authorize in Safari to add Work Account to this Mac.")
        #expect(try Fixture.state("devbox-work-step1").jobs?.first?.step == [1, 2])
    }

    @Test func theDevboxsOwnUnaddedAccountIsPickedWithBothAndTheCoresNote() throws {
        let state = try Fixture.state("devbox-work-unadded")
        let known = try #require(AddFlow.devboxUses(state))
        #expect(known.name == "Work Account" && known.isPersonal)
        #expect(AddFlow.defaultPick(state) == known)
        #expect(AddFlow.defaultTarget(state) == .both)
        #expect(AddFlow.stages(target: .both, kind: .add, state: state, pick: known) == [.macAdd, .devboxAdopt])
        #expect(AddFlow.stages(target: .devbox, kind: .add, state: state, pick: known) == [.macAdd, .devboxAdopt])
        #expect(AddFlow.stages(target: .mac, kind: .add, state: state, pick: known) == [.macAdd])
        #expect(AddFlow.stages(target: .both, kind: .add, state: state, pick: nil) == [.macAdd, .devboxAdd])
        #expect(AddFlow.devboxNote(target: .both, pick: known, state: state) == state.devbox?.live?.note)
        #expect(AddFlow.devboxNote(target: .devbox, pick: known, state: state) == state.devbox?.live?.note)
        #expect(AddFlow.devboxNote(target: .both, pick: nil, state: state) == state.addText?.twice)
        #expect(AddFlow.devboxNote(target: .mac, pick: known, state: state) == nil)
        for name in ["devbox-away", "devbox-live-added", "devbox-work-step2", "devbox-gone-stale"] {
            #expect(AddFlow.devboxUses(try Fixture.state(name)) == nil, "\(name)")
        }
    }

    @Test func theAdoptButtonsFollowTheCoresKeys() throws {
        for name in Fixture.names {
            let state = try Fixture.state(name)
            let live = state.devbox?.live
            let busy = AddFlow.devboxBusy(state, run: nil) != nil
            let expected = state.devbox?.answers == true && !busy && live?.adoptReady == true && live?.held == false
            #expect(AddFlow.canAdoptDevbox(state) == expected, "\(name)")
            let mac = state.mac?.live
            #expect(AddFlow.canAdopt(state) == (mac?.adoptReady == true && mac?.added == false), "\(name)")
            #expect(AddFlow.available(.both, state: state) == (state.devbox?.answers == true && state.devbox?.canSignIn == true && !busy), "\(name)")
        }
        let ready = try Fixture.state("devbox-live-added")
        #expect(AddFlow.canAdoptDevbox(ready))
        #expect(AddFlow.stages(target: .both, kind: .adopt, state: ready) == [.devboxAdopt])
        #expect(AddFlow.adoptTitle([.devboxAdopt], state: ready) == "Add Personal Account from Studio")
        #expect(!AddFlow.canAdoptDevbox(try Fixture.state("devbox-live-held")))
        #expect(!AddFlow.canAdoptDevbox(try Fixture.state("devbox-live-unready")))
        #expect(!AddFlow.canAdoptDevbox(try Fixture.state("devbox-work-unadded")))
        let both = try Fixture.state("mac-live-unadded")
        #expect(AddFlow.stages(target: .both, kind: .adopt, state: both) == [.macAdopt, .devboxAdopt])
        #expect(AddFlow.adoptTitle([.macAdopt, .devboxAdopt], state: both) == "Add both machines' own sign-ins")
        #expect(AddFlow.adoptTitle([.macAdopt], state: both) == "Add this Mac's own sign-in")
        #expect(AddFlow.adoptTitle([], state: both) == nil)
        #expect(!AddFlow.canAdopt(ready))
        let noLogin = try Fixture.state("devbox-no-login")
        #expect(!AddFlow.available(.both, state: noLogin) && !AddFlow.available(.devbox, state: noLogin))
        #expect(AddFlow.defaultTarget(noLogin) == .mac)
    }

    @Test func stepTwoSendsTheStepAndTheExpectedAccountAndDrawsTheCoresCard() throws {
        let state = try Fixture.state("devbox-work-unadded")
        let known = try #require(AddFlow.devboxUses(state))
        var run = AddRun(target: .both, kind: .add, stages: AddFlow.stages(target: .both, kind: .add, state: state, pick: known), pick: known)
        #expect(run.body(for: .macAdd)["known"] as? String == "Work Account")
        #expect(run.body(for: .macAdd)["step"] as? [Int] == [1, 2])
        run.began("m1", at: Date(timeIntervalSince1970: 5_000))
        let second = try Fixture.state("devbox-work-step2")
        #expect(run.advance(state: second, now: Date(timeIntervalSince1970: 5_002)) == .start(.devboxAdopt))
        #expect(run.body(for: .devboxAdopt)["expected"] as? String == Fixture.work)
        #expect(run.body(for: .devboxAdopt)["step"] as? [Int] == [2, 2])
        #expect(!AddStage.devboxAdopt.opensBrowser)
        let card = try Fixture.job("devbox-work-step2", "d1")
        #expect(JobFlow.cardSentence(card) == "Step 2 of 2: Lifeline on Studio is taking over the sign-in its Claude Code holds for Work Account.")
        #expect(AddFlow.waitingLines(.devboxAdopt, state: second, job: card) == [JobFlow.cardSentence(card)])
        #expect(AddFlow.waitingLines(.devboxAdopt, state: second).isEmpty)
        #expect(!JobFlow.hasActions(card) && JobFlow.cancellable(card))
        let single = AddRun(target: .mac, kind: .add, stages: [.macAdd], pick: known)
        #expect(single.body(for: .macAdd)["step"] == nil)
        let adopt = AddRun(target: .devbox, kind: .adopt, stages: [.devboxAdopt], pick: nil, expected: Fixture.personal)
        #expect(adopt.body(for: .devboxAdopt)["expected"] as? String == Fixture.personal && adopt.body(for: .devboxAdopt)["step"] == nil)
        for job in try Fixture.state("job-cards").jobs ?? [] {
            #expect(!(job.card ?? "").isEmpty && JobFlow.cardSentence(job) == job.card, "\(job.id)")
        }
    }

    @Test func stepTwoFollowsTheAccountTheMacActuallyAdded() throws {
        let start = Date(timeIntervalSince1970: 5_000)
        var other = AddRun(target: .both, kind: .add, stages: [.macAdd, .devboxAdd])
        other.began("m1", at: start)
        #expect(other.advance(state: try Fixture.state("devbox-work-step2"), now: start) == .start(.devboxAdopt))
        #expect(other.stages == [.macAdd, .devboxAdopt])

        let known = try #require(AddFlow.devboxUses(try Fixture.state("devbox-work-unadded")))
        var elsewhere = AddRun(target: .both, kind: .add, stages: [.macAdd, .devboxAdopt], pick: known)
        elsewhere.began("m1", at: start)
        #expect(elsewhere.advance(state: try Fixture.state("after-mac-add-org"), now: start) == .start(.devboxAdd))
        #expect(elsewhere.stages == [.macAdd, .devboxAdd])

        var held = AddRun(target: .both, kind: .add, stages: [.macAdd, .devboxAdd])
        held.began("m1", at: start)
        let saved = try Fixture.state("after-mac-add-held")
        #expect(held.advance(state: saved, now: start) == .none)
        #expect(held.step == .added(Fixture.work))
        #expect(held.devboxResult == saved.accounts?.first { $0.uuid == Fixture.work }?.devboxAlready)
        #expect(held.devboxResult == "Studio already has its own sign-in for Work Account.")
        #expect(held.devboxFailed == false)
    }

    @Test func noTryAgainIsOfferedThatCannotWork() throws {
        let failing = try Fixture.state("devbox-work-unadded")
        let adoptRun = AddRun(target: .both, kind: .adopt, stages: [.devboxAdopt])
        #expect(AddFlow.retry(step: .failed("No."), run: adoptRun, state: failing) == nil)
        let ready = try Fixture.state("devbox-live-added")
        #expect(AddFlow.retry(step: .failed("No."), run: adoptRun, state: ready) == .adopt)
        #expect(AddFlow.retry(step: .failed("No."), run: AddRun(target: .both, kind: .add, stages: [.macAdd, .devboxAdopt]), state: failing) == .add)
        #expect(AddFlow.retry(step: .start, run: adoptRun, state: ready) == nil)
        let devboxAdopt = try Fixture.job("failed-jobs", "fo")
        #expect(JobFlow.retryKind(devboxAdopt, state: failing) == nil)
        #expect(JobFlow.retryKind(devboxAdopt, state: try Fixture.state("failed-jobs")) == .adopt)
        #expect(JobFlow.retryKind(devboxAdopt, state: try Fixture.state("devbox-live-held")) == nil)
        #expect(JobFlow.retryTarget(devboxAdopt) == .devbox)
        let add = try Fixture.job("failed-jobs", "fa")
        #expect(JobFlow.retryKind(add, state: failing) == .add)
        #expect(JobFlow.retryTarget(add) == nil)
    }
}

private func folded(_ lines: [String]) -> String {
    let lookalikes: [Character: Character] = ["В": "B", "а": "a", "с": "c", "к": "k", "е": "e", "о": "o", "\u{2019}": "'"]
    let joined = lines.joined(separator: " ").map { lookalikes[$0] ?? $0 }
    return String(joined).lowercased().split(whereSeparator: \.isWhitespace).joined(separator: " ")
        .replacingOccurrences(of: "step l of", with: "step 1 of")
}

@MainActor
private func panelText(_ store: StateStore) async throws -> String {
    NSApplication.shared.setActivationPolicy(.prohibited)
    _ = testFonts
    let root = MenuRoot(store: store, status: AppStatus(), pickDetail: { _ in }, sized: { _ in }, framed: { _ in })
    let host = NSHostingView(rootView: root.environment(\.menuMotion, Motion(MotionPlan(reduceMotion: true))).environment(\.colorScheme, .light))
    host.frame = NSRect(origin: .zero, size: host.fittingSize)
    let window = NSWindow(contentRect: NSRect(x: -30000, y: -30000, width: max(host.frame.width, 10), height: max(host.frame.height, 10)), styleMask: [.borderless], backing: .buffered, defer: false)
    window.isReleasedWhenClosed = false
    window.contentView = host
    for _ in 0..<8 {
        host.layoutSubtreeIfNeeded()
        let size = host.fittingSize
        if size != host.frame.size, size.width > 0, size.height > 0 {
            window.setContentSize(size)
            host.frame = NSRect(origin: .zero, size: size)
        }
        try await Task.sleep(for: .milliseconds(80))
    }
    guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { return "" }
    host.cacheDisplay(in: host.bounds, to: rep)
    window.contentView = nil
    guard let image = rep.cgImage else { return "" }
    let box = ImageBox(image: image)
    return try await Task.detached { try recognized(box) }.value
}

private struct ImageBox: @unchecked Sendable {
    let image: CGImage
}

private func recognized(_ box: ImageBox) throws -> String {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = false
    request.recognitionLanguages = ["en-US"]
    onCPU(request)
    try performText(request, on: box.image)
    return folded((request.results ?? []).compactMap { $0.topCandidates(1).first?.string })
}

@MainActor
private func settle(_ store: StateStore, until check: () -> Bool) async {
    for _ in 0..<100 where !check() {
        await store.refresh()
        try? await Task.sleep(for: .milliseconds(20))
    }
}

private final class Phase: @unchecked Sendable {
    private let lock = NSLock()
    private var number = 0

    var value: Int {
        get { lock.withLock { number } }
        set { lock.withLock { number = newValue } }
    }
}

@MainActor
@Suite(.serialized)
struct NeverOfferScreenTests {
    @Test func theWorkAddSignsInOnceThenKeepsTheDevboxsOwnSignIn() async throws {
        let phase = Phase()
        let phases = try ["devbox-work-unadded", "devbox-work-step1", "devbox-work-step2", "devbox-work-done"].map { try Fixture.text($0) }
        let server = try FakeServer { head, _ in
            if head.hasPrefix("POST /add") {
                return [okReply(#"{"job": "m1"}"#, status: "202 Accepted")]
            }
            if head.hasPrefix("POST /devbox/adopt") {
                return [okReply(#"{"job": "d1"}"#, status: "202 Accepted")]
            }
            if head.hasPrefix("POST /devbox/add") {
                return [okReply(#"{"error": "No."}"#, status: "409 Conflict")]
            }
            return [okReply(phases[min(phase.value, phases.count - 1)])]
        }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        store.opened()
        store.menuOpen = true
        store.screen = .add
        #expect(store.chosenPick?.name == "Work Account")
        #expect(store.chosenTarget == .both)

        let start = try await panelText(store)
        #expect(start.contains("sign in once"), "\(start)")
        #expect(start.contains("work account"), "\(start)")
        #expect(start.contains("open the sign-in page"), "\(start)")
        #expect(!start.contains("twice"), "\(start)")
        #expect(!start.contains("from Studio"), "\(start)")

        store.startAdd(.add)
        await waitFor { store.addRun?.job == "m1" }
        let added = try #require(server.requests.first { $0.head.hasPrefix("POST /add") })
        #expect(String(decoding: added.body, as: UTF8.self) == #"{"known":"Work Account","step":[1,2]}"#)
        phase.value = 1
        await settle(store) { store.addCard?.id == "m1" }
        #expect(store.addCard?.id == "m1")
        let one = try await panelText(store)
        #expect(one.contains("step 1 of 2"), "\(one)")
        #expect(one.contains("work account"), "\(one)")
        #expect(one.contains("open the sign-in page"), "\(one)")

        phase.value = 2
        await settle(store) { server.requests.contains { $0.head.hasPrefix("POST /devbox/adopt") } }
        await settle(store) { store.addCard?.id == "d1" }
        let adopt = try #require(server.requests.first { $0.head.hasPrefix("POST /devbox/adopt") })
        #expect(String(decoding: adopt.body, as: UTF8.self) == #"{"expected":"\#(Fixture.work)","step":[2,2]}"#)
        #expect(store.addCard?.id == "d1")
        #expect(store.addJob == nil)
        let two = try await panelText(store)
        #expect(two.contains("step 2 of 2"), "\(two)")
        #expect(two.contains("taking over"), "\(two)")
        #expect(two.contains("work account"), "\(two)")
        #expect(!two.contains("open the sign-in page"), "\(two)")
        #expect(two.contains("cancel"), "\(two)")
        #expect(!two.contains("authorize"), "\(two)")
        #expect(store.cardJobs.map(\.id) == ["d1"])

        phase.value = 3
        await settle(store) { store.addNeedsNaming }
        #expect(store.addStep == .added(Fixture.work))
        #expect(!server.requests.contains { $0.head.hasPrefix("POST /devbox/add") })
        let account = try #require(store.state?.accounts?.first { $0.uuid == Fixture.work })
        #expect(AddFlow.addedLine(account, run: store.addRun).hasPrefix("It is signed in on this Mac and on Studio."))
    }

    @Test func aChosenAccountOrTargetWinsOverTheDefaultAndClearsWithTheScreen() async throws {
        let text = try Fixture.text("devbox-work-unadded")
        let server = try FakeServer { _, _ in [okReply(text)] }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        store.screen = .add
        #expect(store.chosenPick?.name == "Work Account")
        store.addPick = nil
        #expect(store.chosenPick == nil)
        store.addTarget = .mac
        #expect(store.chosenTarget == .mac)
        store.finishAdd()
        store.screen = .add
        #expect(store.chosenPick?.name == "Work Account")
        #expect(store.chosenTarget == .both)
    }
}
