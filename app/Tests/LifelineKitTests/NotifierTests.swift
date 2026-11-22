import Foundation
import Testing
@testable import Lifeline
@testable import LifelineKit

@MainActor
final class RecordingPoster: NoticePosting {
    var posted: [Notice] = []
    var cleared: [[String]] = []
    var succeed = true

    func register(_ titles: [String]) {}

    func post(_ notice: Notice, done: @escaping @MainActor @Sendable (Bool) -> Void) {
        posted.append(notice)
        done(succeed)
    }

    func clearCancels(_ ids: [String]) {
        cleared.append(ids)
    }
}

private func liveState(head: String, kind: String, title: String, countdown: Bool = false) -> String {
    let at = ISO8601DateFormatter().string(from: Date())
    let countdownJSON = countdown ? #", "countdown": {"target_uuid": "b", "ends_at": "2099-01-01T00:00:00Z", "seconds": 5}"# : ""
    return """
    {"version": 1, "mac": {"view": "mac", "head": "\(head)", "sub": "", "countdown": \(countdown),
      "rows": [{"uuid": "a", "name": "Alex's Account", "is_active": true}, {"uuid": "b", "name": "Team Account", "is_best": true}]},
     "accounts": [{"uuid": "a", "name": "Alex's Account", "short": "Alex", "number": 1}, {"uuid": "b", "name": "Team Account", "short": "Team", "number": 4}],
     "notifications": [{"id": "n-\(kind)", "kind": "\(kind)", "title": "\(title)", "body": "", "uuid": "a", "at": "\(at)"}]\(countdownJSON)}
    """
}

final class Flag: @unchecked Sendable {
    private let lock = NSLock()
    private var on = true

    var value: Bool {
        get {
            lock.lock()
            defer { lock.unlock() }
            return on
        }
        set {
            lock.lock()
            on = newValue
            lock.unlock()
        }
    }
}

@MainActor
func waitFor(_ check: () -> Bool) async {
    for _ in 0..<150 where !check() {
        try? await Task.sleep(for: .milliseconds(20))
    }
}

@Suite(.serialized)
@MainActor
struct NotifierStoreTests {
    @Test func theOpenMenuIsReadFromTheNewStateNotTheOldOne() async throws {
        let body = liveState(head: "Alex's Account runs out at 15:40.", kind: "will_run_out", title: "Alex's Account runs out at 15:40")
        let server = try FakeServer { _, _ in [okReply(body)] }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        let poster = RecordingPoster()
        let notifier = Notifier(store: store, shown: { fresh in ShownMenu(model: fresh.model(for: .mac), note: nil) }, open: {}, defaults: nil)
        notifier.begin(poster, permission: .allowed)
        await store.refresh()
        #expect(store.state != nil)
        #expect(poster.posted.isEmpty)
    }

    @Test func aClosedMenuGetsTheNoticeOnceWithItsButton() async throws {
        let body = liveState(head: "Alex's Account runs out at 15:40.", kind: "will_run_out", title: "Alex's Account runs out at 15:40")
        let server = try FakeServer { _, _ in [okReply(body)] }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        let poster = RecordingPoster()
        let notifier = Notifier(store: store, shown: { _ in nil }, open: {}, defaults: nil)
        notifier.begin(poster, permission: .allowed)
        await store.refresh()
        await store.refresh()
        #expect(poster.posted.map(\.id) == ["n-will_run_out"])
        #expect(poster.posted.first?.action == NoticeAction(command: .switchTo("b"), title: "Switch to Team Account"))
    }

    @Test func aFailedPostIsTriedAgainAndNamedInSettings() async throws {
        let body = liveState(head: "", kind: "report", title: "Lifeline could not save the settings.")
        let server = try FakeServer { _, _ in [okReply(body)] }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        let poster = RecordingPoster()
        poster.succeed = false
        let notifier = Notifier(store: store, shown: { _ in nil }, open: {}, defaults: nil)
        notifier.begin(poster, permission: .allowed)
        await store.refresh()
        #expect(poster.posted.count == 1)
        #expect(notifier.permission == .failed)
        poster.succeed = true
        await store.refresh()
        #expect(poster.posted.count == 2)
        await store.refresh()
        #expect(poster.posted.count == 2)
    }

    @Test func aCancelButtonIsClearedOnceTheCountdownEnds() async throws {
        let counting = Flag()
        let server = try FakeServer { _, _ in
            [okReply(liveState(head: "Switching this Mac to Team Account.", kind: "limit", title: "Alex's Account is out until 17:00", countdown: counting.value))]
        }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        let poster = RecordingPoster()
        let notifier = Notifier(store: store, shown: { _ in nil }, open: {}, defaults: nil)
        notifier.begin(poster, permission: .allowed)
        await store.refresh()
        #expect(poster.posted.first?.action?.command == .cancel)
        #expect(poster.cleared.isEmpty)
        counting.value = false
        await store.refresh()
        #expect(poster.cleared == [["n-limit"]])
        await store.refresh()
        #expect(poster.cleared.count == 1)
    }

    @Test func aRefusedButtonPostsItsSentenceWhileTheMenuIsClosed() async throws {
        let body = liveState(head: "", kind: "report", title: "Old news.")
        let server = try FakeServer { head, _ in
            if head.hasPrefix("POST /undo") {
                return [okReply(#"{"error": "There is no switch to undo."}"#, status: "409 Conflict")]
            }
            return [okReply(body)]
        }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        let poster = RecordingPoster()
        let notifier = Notifier(store: store, shown: { _ in nil }, open: {}, defaults: nil)
        notifier.begin(poster, permission: .allowed)
        await store.refresh()
        let before = poster.posted.count
        notifier.respond(action: Notices.primaryAction, info: ["command": "undo"], id: "x")
        await waitFor { poster.posted.count > before }
        #expect(poster.posted.last?.title == "There is no switch to undo.")
        #expect(poster.posted.last?.action == nil)
        #expect(store.note == "There is no switch to undo.")
    }
}

@Suite(.serialized)
@MainActor
struct StoreEdgeTests {
    private func jobsState(_ jobs: String) -> String {
        #"{"version": 1, "mac": {"view": "mac", "rows": [{"uuid": "j", "name": "Alex's Account", "is_active": true}]}, "accounts": [{"uuid": "j", "name": "Alex's Account", "short": "Alex"}, {"uuid": "o", "name": "Team Account", "short": "Team"}], "jobs": "# + jobs + "}"
    }

    @Test func aSuccessfulAddOpensOnItsNamingStep() async throws {
        let body = jobsState(#"[{"id": "1", "kind": "add", "state": "done", "uuid": "o", "result": "Lifeline added Team Account."}]"#)
        let server = try FakeServer { head, _ in
            head.hasPrefix("POST /add") ? [okReply(#"{"job": "1"}"#, status: "202 Accepted")] : [okReply(body)]
        }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        store.startAdd(.add)
        await waitFor { store.addRun?.job != nil }
        await store.refresh()
        await waitFor { store.addStep == .added("o") }
        #expect(store.addStep == .added("o"))
        store.opened()
        #expect(store.screen == .add)
        #expect(store.addStep == .added("o"))
        #expect(store.unseenResults.isEmpty)
        store.finishAdd()
        store.panelClosed(visible: true)
        store.opened()
        #expect(store.screen == .menu)
        #expect(store.unseenResults.isEmpty)
    }

    @Test func aFailedAddIsForgottenWhenTheMenuOpensAndItsResultLeadsTheMenu() async throws {
        let sentence = "Your browser signed in as Personal Account (sam@example.org), which Lifeline already has. Nothing changed."
        let body = jobsState(#"[{"id": "1", "kind": "add", "state": "failed", "result": "\#(sentence)"}]"#)
        let server = try FakeServer { head, _ in
            head.hasPrefix("POST /add") ? [okReply(#"{"job": "1"}"#, status: "202 Accepted")] : [okReply(body)]
        }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        store.startAdd(.add)
        await waitFor { store.addRun?.job != nil }
        await store.refresh()
        await waitFor { store.addRun?.finished == true }
        store.opened()
        #expect(store.addRun == nil)
        #expect(store.addStep == .start)
        #expect(store.screen == .menu)
        #expect(store.unseenResults.map(\.result) == [sentence])
    }

    @Test func aWaitingAddThatVanishesEndsWithOneSentence() async throws {
        let present = Flag()
        let running = jobsState(#"[{"id": "1", "kind": "add", "state": "running"}]"#)
        let gone = jobsState("[]")
        let server = try FakeServer { head, _ in
            if head.hasPrefix("POST /add") {
                return [okReply(#"{"job": "1"}"#, status: "202 Accepted")]
            }
            return [okReply(present.value ? running : gone)]
        }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        store.startAdd(.add)
        await waitFor { store.addRun?.job != nil }
        await store.refresh()
        #expect(store.addStep == .waiting(.add))
        #expect(store.addWaiting)
        present.value = false
        await store.refresh()
        await waitFor { store.addStep == .failed(AddFlow.lost(store.state)) }
        #expect(store.addStep == .failed(AddFlow.lost(store.state)))
        #expect(!store.addWaiting)
        store.opened()
        #expect(store.addRun == nil)
    }

    @Test func aNoteIsNotKeptWhileTheMenuIsOpen() async throws {
        let server = try FakeServer { head, _ in
            head.hasPrefix("POST /undo") ? [okReply(#"{"error": "There is no switch to undo."}"#, status: "409 Conflict")] : [okReply(#"{"version": 1}"#)]
        }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        store.menuOpen = true
        store.run(.undo, via: .menuUndo, keepNote: true)
        await waitFor { store.note == "There is no switch to undo." }
        #expect(store.note == "There is no switch to undo.")
        store.opened()
        #expect(store.note == nil)
        store.menuOpen = false
        await waitFor { !store.switching }
        store.run(.undo, via: .menuUndo, keepNote: true)
        await waitFor { store.note == "There is no switch to undo." }
        store.opened()
        #expect(store.note == "There is no switch to undo.")
        store.opened()
        #expect(store.note == nil)
    }

    @Test func aCaveatFromANotificationButtonIsPostedWhileTheMenuIsClosed() async throws {
        let body = jobsState("[]")
        let server = try FakeServer { head, _ in
            head.hasPrefix("POST /switch")
                ? [okReply(#"{"message": "This Mac now uses Team Account. Lifeline kept a spare copy of the old sign-in."}"#)]
                : [okReply(body)]
        }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        let poster = RecordingPoster()
        let notifier = Notifier(store: store, shown: { _ in nil }, open: {}, defaults: nil)
        notifier.begin(poster, permission: .allowed)
        await store.refresh()
        notifier.respond(action: Notices.primaryAction, info: ["command": "switch", "uuid": "o"], id: "x")
        await waitFor { !poster.posted.isEmpty }
        #expect(poster.posted.last?.title == "Lifeline kept a spare copy of the old sign-in.")
    }
}

final class Seen: @unchecked Sendable {
    private let lock = NSLock()
    private var heads: [String] = []
    private var bodies: [String] = []

    func add(_ head: String, _ body: Data) {
        lock.lock()
        heads.append(head)
        bodies.append(String(decoding: body, as: UTF8.self))
        lock.unlock()
    }

    func posts(_ prefix: String) -> [String] {
        lock.lock()
        defer { lock.unlock() }
        return zip(heads, bodies).filter { $0.0.hasPrefix(prefix) }.map(\.1)
    }
}

@Suite(.serialized)
@MainActor
struct DevboxAddStoreTests {
    private func stateJSON(_ jobs: String) -> String {
        #"{"version": 1, "devbox": {"reachable": true, "answers": true, "can_sign_in": true, "account_uuid": "c"}, "mac": {"view": "mac", "rows": [{"uuid": "j", "name": "Alex's Account", "is_active": true}]}, "accounts": [{"uuid": "j", "name": "Alex's Account"}, {"uuid": "o", "name": "Team Account"}], "jobs": "# + jobs + "}"
    }

    private func server(_ seen: Seen, macDone: Flag, devboxDone: Flag) throws -> FakeServer {
        let both = stateJSON(#"[{"id": "m", "kind": "add", "state": "done", "uuid": "o", "result": "Lifeline added Team Account."}, {"id": "d", "kind": "devbox-add", "state": "done", "uuid": "o", "result": "Studio signed in."}]"#)
        let first = stateJSON(#"[{"id": "m", "kind": "add", "state": "done", "uuid": "o", "result": "Lifeline added Team Account."}, {"id": "d", "kind": "devbox-add", "state": "running", "uuid": "o"}]"#)
        let none = stateJSON(#"[{"id": "m", "kind": "add", "state": "running"}]"#)
        return try FakeServer { head, body in
            seen.add(head, body)
            if head.hasPrefix("POST /add") {
                return [okReply(#"{"job": "m"}"#, status: "202 Accepted")]
            }
            if head.hasPrefix("POST /devbox/add") {
                return [okReply(#"{"job": "d"}"#, status: "202 Accepted")]
            }
            if head.hasPrefix("POST /settings") {
                return [okReply(#"{"settings": {}}"#)]
            }
            return [okReply(devboxDone.value ? both : (macDone.value ? first : none))]
        }
    }

    @Test func bothAddsOnTheMacThenOnTheDevbox() async throws {
        let seen = Seen()
        let macDone = Flag()
        macDone.value = false
        let devboxDone = Flag()
        devboxDone.value = false
        let fake = try server(seen, macDone: macDone, devboxDone: devboxDone)
        defer { fake.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: fake.path)), sizes: nil)
        await store.refresh()
        #expect(store.chosenTarget == .both)
        store.startAdd(.add)
        await waitFor { store.addRun?.job == "m" }
        macDone.value = true
        await store.refresh()
        await waitFor { store.addRun?.job == "d" }
        #expect(seen.posts("POST /devbox/add") == [#"{"step":[2,2],"uuid":"o"}"#])
        #expect(store.addRun?.current == .devboxAdd)
        devboxDone.value = true
        await store.refresh()
        await waitFor { store.addStep == .added("o") }
        #expect(store.addStep == .added("o"))
        #expect(seen.posts("POST /settings").isEmpty)
    }

    @Test func devboxOnlySavesThePlacementAfterBothSignIns() async throws {
        let seen = Seen()
        let macDone = Flag()
        macDone.value = false
        let devboxDone = Flag()
        devboxDone.value = false
        let fake = try server(seen, macDone: macDone, devboxDone: devboxDone)
        defer { fake.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: fake.path)), sizes: nil)
        await store.refresh()
        store.addTarget = .devbox
        store.startAdd(.add)
        await waitFor { store.addRun?.job == "m" }
        store.startAdd(.add)
        for _ in 0..<25 where seen.posts("POST /add").count < 2 {
            try? await Task.sleep(for: .milliseconds(20))
        }
        #expect(seen.posts("POST /add").count == 1)
        #expect(store.addRun?.job == "m")
        macDone.value = true
        await store.refresh()
        await waitFor { store.addRun?.job == "d" }
        devboxDone.value = true
        await store.refresh()
        await waitFor { !seen.posts("POST /settings").isEmpty }
        #expect(seen.posts("POST /settings") == [#"{"placement":{"o":"devbox"}}"#])
        #expect(store.addStep == .added("o"))
    }
}
