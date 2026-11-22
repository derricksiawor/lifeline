import AppKit
import Foundation
import SwiftUI
import Testing
import Vision
@testable import Lifeline
@testable import LifelineKit

private func addState(_ job: String) -> String {
    #"{"version": 1, "mac": {"view": "mac", "rows": [{"uuid": "j", "name": "Alex's Account", "is_active": true}]}, "accounts": [{"uuid": "j", "name": "Alex's Account", "short": "Alex", "number": 1}, {"uuid": "n", "name": "Alexandra", "short": "Alexandra", "number": 2}], "jobs": ["# + job + #"], "add_text": "# + Fixture.addTextJSON + "}"
}

private final class Confirmed: @unchecked Sendable {
    private let lock = NSLock()
    private var value = false
    private var done = false

    var cancelled: Bool {
        get { lock.withLock { value } }
        set { lock.withLock { value = newValue } }
    }

    var finished: Bool {
        get { lock.withLock { done } }
        set { lock.withLock { done = newValue } }
    }
}

@MainActor
private func startButton(_ store: StateStore) throws -> (found: Bool, live: Bool, lines: [String]) {
    NSApplication.shared.setActivationPolicy(.prohibited)
    let view = AddAccountView(
        state: store.state, step: store.addStep, run: store.addRun, target: store.chosenTarget, problem: store.addProblem, zone: .current,
        handlers: AddHandlers(back: {}), note: store.addNote, settling: store.settlingJobs
    )
    .background(Color.white)
    .environment(\.colorScheme, .light)
    _ = testFonts
    let renderer = ImageRenderer(content: view)
    renderer.scale = 1
    guard let image = renderer.cgImage else { return (false, false, []) }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    onCPU(request)
    try performText(request, on: image)
    let results = request.results ?? []
    let lines = results.compactMap { $0.topCandidates(1).first?.string }
    guard let open = results.first(where: { isOpenButton($0.topCandidates(1).first?.string) }) else { return (false, false, lines) }
    let rep = NSBitmapImageRep(cgImage: image)
    let box = open.boundingBox
    let x = Int(box.minX * CGFloat(image.width)) - 12
    let y = image.height - Int(box.midY * CGFloat(image.height))
    guard let color = rep.colorAt(x: x, y: y)?.usingColorSpace(.sRGB),
          let primary = NSColor(Palette.of(.light).primary).usingColorSpace(.sRGB),
          let disabled = NSColor(SheetPalette.of(.light).disabled).usingColorSpace(.sRGB) else { return (true, false, lines) }
    func gap(_ other: NSColor) -> CGFloat {
        abs(color.redComponent - other.redComponent) + abs(color.greenComponent - other.greenComponent) + abs(color.blueComponent - other.blueComponent)
    }
    return (true, gap(primary) < gap(disabled) && color.blueComponent - color.redComponent > 0.3, lines)
}

@MainActor
@Suite(.serialized)
struct CancelReturnsTests {
    @Test func cancelGoesStraightBackToALiveStartStep() async throws {
        let flags = Confirmed()
        let server = try FakeServer { head, _ in
            if head.hasPrefix("POST /add") {
                return [okReply(#"{"job": "5"}"#, status: "202 Accepted")]
            }
            if head.hasPrefix("POST /jobs/5/cancel") {
                return [okReply(#"{"ok": true}"#)]
            }
            let state = flags.cancelled ? #"{"id": "5", "kind": "add", "state": "cancelled", "result": "You cancelled the sign-in. Nothing changed."}"# : #"{"id": "5", "kind": "add", "state": "running", "url": "https://claude.ai/oauth/authorize?x=1", "opened": true}"#
            return [okReply(addState(state))]
        }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        store.opened()
        store.menuOpen = true
        store.screen = .add
        store.startAdd(.add)
        await waitFor { store.addJob != nil }
        store.cancelJob("5")
        #expect(store.screen == .add)
        #expect(store.addStep == .start)
        #expect(!store.addWaiting)
        #expect(store.addNote == "Cancelling the sign-in.")
        var shown = try startButton(store)
        #expect(shown.found && shown.live, "\(shown.lines)")
        #expect(shown.lines.contains { $0.contains("Cancelling the sign-in.") }, "\(shown.lines)")
        await store.refresh()
        #expect(store.addStep == .start)
        shown = try startButton(store)
        #expect(shown.found && shown.live, "\(shown.lines)")
        flags.cancelled = true
        await waitFor { store.addNote == "You cancelled the sign-in. Nothing changed." }
        #expect(store.addNote == "You cancelled the sign-in. Nothing changed.")
        #expect(store.addStep == .start)
        shown = try startButton(store)
        #expect(shown.found && shown.live, "\(shown.lines)")
        #expect(shown.lines.contains { $0.contains("You cancelled the sign-in.") }, "\(shown.lines)")
        #expect(store.unseenResults.isEmpty)
    }

    @Test func anAddThatFinishedFirstShowsTheNamingStep() async throws {
        let flags = Confirmed()
        let server = try FakeServer { head, _ in
            if head.hasPrefix("POST /add") {
                return [okReply(#"{"job": "6"}"#, status: "202 Accepted")]
            }
            if head.hasPrefix("POST /jobs/6/cancel") {
                return [okReply(#"{"error": "That sign-in has nothing to cancel."}"#, status: "409 Conflict")]
            }
            let state = flags.finished ? #"{"id": "6", "kind": "add", "state": "done", "uuid": "n", "suggested_name": "Alex's Account", "suggested_short": "Alex"}"# : #"{"id": "6", "kind": "add", "state": "running", "url": "https://claude.ai/oauth/authorize?x=1", "opened": true}"#
            return [okReply(addState(state))]
        }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        store.opened()
        store.menuOpen = true
        store.screen = .add
        store.startAdd(.add)
        await waitFor { store.addJob != nil }
        flags.finished = true
        store.cancelJob("6")
        #expect(store.addStep == .start)
        await waitFor { store.addNeedsNaming }
        #expect(store.addNeedsNaming)
        #expect(store.addNote == nil)
    }
}

@MainActor
struct ButtonLabelTests {
    private let names = ["Alex's Account", "Work Account", "Personal Account", "Team Account"]

    @Test func everyButtonLabelKeepsSixteenPointsInsideEachSide() throws {
        let sheet = Metrics.panelWidth - 2 * Metrics.sheetPadding
        var labels: [(String, TextStyle, CGFloat)] = []
        var sheetTitles = [JobFlow.openFirst, JobFlow.openAgain, JobFlow.cancel, AddFlow.tryAgain, "Done", "Save", "Quit Lifeline"]
        for name in ["devbox-live-added", "devbox-work-unadded", "mac-live-unadded"] {
            let state = try Fixture.state(name)
            sheetTitles += [state.devbox?.live?.adoptTitle, state.devbox?.live?.bothTitle, state.mac?.live?.adoptTitle].compactMap { $0 }
        }
        labels += sheetTitles.map { ($0, Styles.action, sheet) }
        for name in names {
            for title in ["Switch this Mac to \(name)", "Switch back to \(name)", "Sign in again to \(name)"] {
                labels.append((title, Styles.action, Metrics.innerWidth))
            }
        }
        labels += ["Undo the switch", "Cancel the switch", "Sign in again"].map { ($0, Styles.action, Metrics.innerWidth) }
        for (title, style, width) in labels {
            #expect(style.width(of: title) + 2 * Metrics.labelInset <= width, "\(title) is \(style.width(of: title)) in \(width)")
        }
        let third = (sheet - 2 * Metrics.pairInset) / 3
        for title in ["This Mac", "Devbox", "Both"] {
            #expect(Styles.action.width(of: title) + 2 * Metrics.labelInset <= third, "\(title)")
        }
    }

    @Test func aLabelTooLongForItsButtonWrapsInsideTheInset() {
        let long = "Add the account Claude Code uses on Studio and keep its own sign-in"
        let width = Metrics.panelWidth - 2 * Metrics.sheetPadding
        let fit = ButtonFit.layout(long, style: Styles.action, width: width, height: Metrics.actionHeight)
        #expect(fit.lines > 1)
        #expect(fit.room == width - 2 * Metrics.labelInset)
        #expect(fit.height >= CGFloat(fit.lines) * Styles.action.lineHeight + 2 * Metrics.labelInset)
        let short = ButtonFit.layout("Done", style: Styles.action, width: width, height: Metrics.actionHeight)
        #expect(short.lines == 1 && short.height == Metrics.actionHeight)
    }

    @Test func theAdoptButtonsTakeTheCoresTitles() throws {
        let both = try Fixture.state("mac-live-unadded")
        #expect(AddFlow.adoptTitle([.macAdopt, .devboxAdopt], state: both) == both.devbox?.live?.bothTitle)
        #expect(AddFlow.adoptTitle([.macAdopt], state: both) == both.mac?.live?.adoptTitle)
        #expect(AddFlow.adoptTitle([.devboxAdopt], state: both) == both.devbox?.live?.adoptTitle)
        #expect(AddFlow.adoptTitle([.devboxAdopt], state: nil) == nil)
        #expect(AddFlow.twice(both) == both.addText?.twice && AddFlow.twice(both).hasSuffix("press Authorize twice."))
    }
}

private final class CancelScript: @unchecked Sendable {
    private let lock = NSLock()
    private var jobs: [String: String] = [:]
    private var next = 0
    let replyDelay: Double

    init(replyDelay: Double = 0) {
        self.replyDelay = replyDelay
    }

    func newJob() -> String {
        lock.withLock {
            next += 1
            let id = "c\(next)"
            jobs[id] = "running"
            return id
        }
    }

    func set(_ id: String, _ value: String) {
        lock.withLock { jobs[id] = value }
    }

    func body() -> String {
        let list = lock.withLock { jobs.sorted { $0.key < $1.key } }
        let items = list.map { id, value in
            value == "running"
                ? #"{"id": "\#(id)", "kind": "add", "state": "running", "url": "https://claude.ai/oauth/authorize?x=1", "opened": true}"#
                : #"{"id": "\#(id)", "kind": "add", "state": "cancelled", "result": "You cancelled the sign-in. Nothing changed."}"#
        }
        return addState(items.joined(separator: ", "))
    }
}

@MainActor
@Suite(.serialized)
struct CancelOrderTests {
    private func store(_ script: CancelScript, cancelFirst: Bool) throws -> (FakeServer, StateStore) {
        let server = try FakeServer { head, _ in
            if head.hasPrefix("POST /add") {
                return [okReply(#"{"job": "\#(script.newJob())"}"#, status: "202 Accepted")]
            }
            if head.hasPrefix("POST /jobs/"), head.contains("/cancel") {
                let id = String(head.split(separator: "/")[2])
                if cancelFirst {
                    script.set(id, "cancelled")
                }
                if script.replyDelay > 0 {
                    Thread.sleep(forTimeInterval: script.replyDelay)
                }
                script.set(id, "cancelled")
                return [okReply(#"{"ok": true}"#)]
            }
            return [okReply(script.body())]
        }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        return (server, store)
    }

    private func everythingLive(_ store: StateStore) -> Bool {
        store.cancelling.isEmpty && !store.addWaiting && !store.switching && store.addStep == .start
    }

    private func addAndCancel(_ store: StateStore) async throws {
        store.screen = .add
        store.startAdd(.add)
        await waitFor { store.addJob != nil }
        let id = try #require(store.addJob?.id)
        store.cancelJob(id)
        #expect(!store.addWaiting && store.addStep == .start)
    }

    @Test func theCancelReplyComesBeforeTheStateShowsIt() async throws {
        let script = CancelScript()
        let (server, store) = try store(script, cancelFirst: false)
        defer { server.close() }
        await store.refresh()
        store.opened()
        store.menuOpen = true
        try await addAndCancel(store)
        await waitFor { store.cancelling.isEmpty }
        await store.refresh()
        #expect(everythingLive(store), "\(store.cancelling) \(store.addWaiting) \(store.switching)")
    }

    @Test func theStateShowsCancelledBeforeTheReply() async throws {
        let script = CancelScript(replyDelay: 0.6)
        let (server, store) = try store(script, cancelFirst: true)
        defer { server.close() }
        await store.refresh()
        store.opened()
        store.menuOpen = true
        try await addAndCancel(store)
        await waitFor { store.cancelling.isEmpty }
        await store.refresh()
        #expect(everythingLive(store), "\(store.cancelling) \(store.addWaiting) \(store.switching)")
    }

    @Test func twoAddsCancelledBackToBack() async throws {
        let script = CancelScript()
        let (server, store) = try store(script, cancelFirst: false)
        defer { server.close() }
        await store.refresh()
        store.opened()
        store.menuOpen = true
        try await addAndCancel(store)
        try await addAndCancel(store)
        await waitFor { store.cancelling.isEmpty }
        await store.refresh()
        #expect(everythingLive(store), "\(store.cancelling) \(store.addWaiting) \(store.switching)")
    }

    @Test func aCancelReplyThatTimesOutWhileTheJobIsAlreadyCancelled() async throws {
        let recorded = Signal()
        let reply = DispatchSemaphore(value: 0)
        let deadline = Gate()
        let script = CancelScript()
        let server = try FakeServer(concurrent: true) { head, _ in
            if head.hasPrefix("POST /add") {
                return [okReply(#"{"job": "\#(script.newJob())"}"#, status: "202 Accepted")]
            }
            if head.hasPrefix("POST /jobs/"), head.contains("/cancel") {
                script.set(String(head.split(separator: "/")[2]), "cancelled")
                recorded.raise()
                _ = reply.wait(timeout: .now() + 30)
                return [okReply(#"{"ok": true}"#)]
            }
            return [okReply(script.body())]
        }
        defer {
            reply.signal()
            server.close()
        }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        store.cancelDeadline = { await deadline.wait() }
        await store.refresh()
        store.opened()
        store.menuOpen = true
        try await addAndCancel(store)
        #expect(!store.addWaiting && store.addStep == .start && !store.switching)
        await waitFor { recorded.raised }
        #expect(recorded.raised, "the service never recorded the cancel")
        #expect(store.cancelling.isEmpty == false, "the cancel settled before its deadline")
        await deadline.open()
        await waitFor { store.cancelling.isEmpty }
        #expect(store.cancelling.isEmpty, "the cancel did not settle when its deadline passed")
        #expect(store.addProblem == nil, "a timed-out cancel of a job that is already cancelled showed as a refusal: \(store.addProblem ?? "")")
        reply.signal()
        await store.refresh()
        #expect(everythingLive(store), "\(store.cancelling) \(store.addWaiting) \(store.switching)")
        #expect(server.requests.filter { $0.head.contains("/cancel") }.count == 1)
    }

    @Test func aDisablingFlagLapsesAfterTenSeconds() async throws {
        let script = CancelScript()
        let server = try FakeServer { head, _ in
            if head.hasPrefix("POST /add") {
                return [okReply(#"{"job": "\#(script.newJob())"}"#, status: "202 Accepted")]
            }
            if head.contains("/cancel") {
                return [okReply(#"{"ok": true}"#)]
            }
            return [okReply(script.body())]
        }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        var now = Date()
        store.clock = { now }
        await store.refresh()
        store.opened()
        store.menuOpen = true
        store.screen = .add
        store.startAdd(.add)
        await waitFor { store.addJob != nil }
        let id = try #require(store.addJob?.id)
        store.cancelJob(id)
        await store.refresh()
        #expect(store.cancelling == [id])
        now = now.addingTimeInterval(9)
        await store.refresh()
        #expect(store.cancelling == [id])
        now = now.addingTimeInterval(1.5)
        await store.refresh()
        #expect(store.cancelling.isEmpty)
        #expect(!store.switching)
    }
}

@MainActor
struct PanelHitTests {
    @Test func clicksAnywhereOnThePanelReachIt() {
        let bounds = NSRect(x: 0, y: 0, width: 432, height: 700)
        let panel = MenuLogic.panelArea(bounds: bounds, top: Metrics.shadowTop, side: Metrics.shadowSide, bottom: Metrics.shadowBottom)
        #expect(panel.contains(NSPoint(x: Metrics.shadowSide + 4, y: Metrics.shadowBottom + 4)))
        #expect(panel.contains(NSPoint(x: bounds.width - Metrics.shadowSide - 4, y: bounds.height - Metrics.shadowTop - 4)))
        #expect(!panel.contains(NSPoint(x: 1, y: 1)))
        #expect(MenuLogic.inside(NSPoint(x: 200, y: 600), panel: panel, frames: [NSRect(x: 0, y: 0, width: 50, height: 50)]))
    }
}

@MainActor
@Suite(.serialized)
struct PanelOpenTests {
    @Test func theOpenedPanelIsTheMenusSize() async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let server = try FakeServer { _, _ in [okReply(addState(""))] }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        let panel = MenuPanel(store: store, status: AppStatus(), showsItem: false, unseen: true)
        panel.open()
        defer { panel.close(returningFocus: false) }
        var frame = NSRect.zero
        for _ in 0..<40 {
            try await Task.sleep(for: .milliseconds(50))
            frame = panel.shownFrame ?? .zero
            if frame.width > 0, frame.height > 0 {
                break
            }
        }
        #expect(abs(frame.width - (Metrics.panelWidth + 2 * Metrics.shadowSide)) < 2, "\(frame)")
        #expect(frame.height > Metrics.shadowTop + Metrics.shadowBottom + 100, "\(frame)")
        #expect(frame.height <= (store.panelLimit ?? 10000) + Metrics.shadowTop + Metrics.shadowBottom + 1, "\(frame)")
        #expect(panel.shownAlpha == 0, "the check copy's panel must never be seen")
        #expect(panel.shownIgnoresClicks == true)
        #expect(!NSScreen.screens.contains { $0.frame.intersects(frame) }, "\(frame) must sit off every display")
    }
}

final class Signal: @unchecked Sendable {
    private let lock = NSLock()
    private var flag = false

    var raised: Bool {
        lock.withLock { flag }
    }

    func raise() {
        lock.withLock { flag = true }
    }
}

actor Gate {
    private var opened = false
    private var waiting: [CheckedContinuation<Void, Never>] = []

    func wait() async {
        if opened {
            return
        }
        await withCheckedContinuation { waiting.append($0) }
    }

    func open() {
        opened = true
        waiting.forEach { $0.resume() }
        waiting = []
    }
}
