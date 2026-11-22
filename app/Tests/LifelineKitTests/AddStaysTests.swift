import AppKit
import Foundation
import SwiftUI
import Testing
import Vision
@testable import Lifeline
@testable import LifelineKit

private let addWaitingState = #"{"version": 1, "mac": {"view": "mac", "rows": [{"uuid": "j", "name": "Alex's Account", "is_active": true}]}, "accounts": [{"uuid": "j", "name": "Alex's Account", "short": "Alex", "number": 1}], "jobs": [{"id": "2", "kind": "add", "state": "running", "url": "https://claude.ai/oauth/authorize?x=1", "opened": true, "browser": "Google Chrome"}]}"#

@MainActor
private func seen(_ view: some View) throws -> [String] {
    Array(typeset(view).dropFirst())
}

struct AddStaysRuleTests {
    @Test func theIconBringsAWaitingAddForwardAndNeverClosesIt() {
        #expect(JobFlow.toggleMove(open: true, holding: true) == .raise)
        #expect(JobFlow.toggleMove(open: true, holding: false) == .close)
        #expect(JobFlow.toggleMove(open: false, holding: false) == .open)
        #expect(JobFlow.toggleMove(open: false, holding: true) == .open)
    }

    @Test func focusLossOutsideClicksAndSpacesKeepAWaitingAdd() {
        #expect(JobFlow.outsideMove(holding: true) == .stay)
        #expect(JobFlow.outsideMove(holding: false) == .close)
    }

    @Test func escapeOnAWaitingAddGoesBack() {
        #expect(MenuLogic.sheetKeyAction(keyCode: 53, characters: nil, command: false, holding: true) == .back)
        #expect(MenuLogic.sheetKeyAction(keyCode: 53, characters: nil, command: false, holding: false) == .close)
        #expect(MenuLogic.sheetKeyAction(keyCode: 12, characters: "q", command: true, holding: true) == .quit)
    }

    @Test func thePanelNeverDropsBelowOtherWindows() throws {
        let file = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources/Lifeline/MenuPanel.swift")
        let text = try String(contentsOf: file, encoding: .utf8)
        #expect(!text.contains(".normal"))
        #expect(text.contains("level = .popUpMenu"))
    }
}

@MainActor
@Suite(.serialized)
struct AddStaysStoreTests {
    @Test func backShowsTheMenuWithTheCardAndTheCardReturnsToTheScreen() async throws {
        let server = try FakeServer { head, _ in
            head.hasPrefix("POST /add") ? [okReply(#"{"job": "2"}"#, status: "202 Accepted")] : [okReply(addWaitingState)]
        }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        store.opened()
        store.menuOpen = true
        store.screen = .add
        store.startAdd(.add)
        await waitFor { store.addJob != nil }
        #expect(store.addHolding)
        store.backFromAdd()
        #expect(store.screen == .menu)
        #expect(store.addWaiting)
        #expect(!store.addHolding)
        #expect(store.waitingJobs.map(\.id) == ["2"])
        #expect(!store.openWaiting("other"))
        #expect(store.screen == .menu)
        #expect(store.openWaiting("2"))
        #expect(store.screen == .add)
        #expect(store.addHolding)
    }

    @Test func everyAddStepHoldsAndOnlyBackLeaves() async throws {
        let start = StateStore(api: LifelineAPI(client: SocketClient(path: "/nonexistent/lifeline.sock")), sizes: nil)
        start.screen = .add
        #expect(start.addHolding)
        #expect(JobFlow.outsideMove(holding: start.addHolding) == .stay)
        #expect(JobFlow.toggleMove(open: true, holding: start.addHolding) == .raise)
        start.backFromAdd()
        #expect(start.screen == .menu)

        let failed = #"{"version": 1, "mac": {"view": "mac", "rows": []}, "accounts": [], "jobs": [{"id": "3", "kind": "add", "state": "failed", "result": "Lifeline already has Personal Account."}]}"#
        let failing = try FakeServer { head, _ in
            head.hasPrefix("POST /add") ? [okReply(#"{"job": "3"}"#, status: "202 Accepted")] : [okReply(failed)]
        }
        defer { failing.close() }
        let result = StateStore(api: LifelineAPI(client: SocketClient(path: failing.path)), sizes: nil)
        await result.refresh()
        result.opened()
        result.menuOpen = true
        result.screen = .add
        result.startAdd(.add)
        await waitFor { if case .failed = result.addStep { return true } else { return false } }
        #expect(result.addHolding)
        result.backFromAdd()
        #expect(result.screen == .menu)
        #expect(result.addRun == nil)
    }

    @Test func theNamingStepHoldsAndItsBackKeepsTheSuggestedNames() async throws {
        let named = #"{"version": 1, "mac": {"view": "mac", "rows": []}, "accounts": [{"uuid": "n", "name": "Alexandra", "short": "Alexandra", "number": 1}], "jobs": [{"id": "4", "kind": "add", "state": "done", "uuid": "n", "suggested_name": "Alex's Account", "suggested_short": "Alex"}]}"#
        let server = try FakeServer { head, _ in
            if head.hasPrefix("POST /add") {
                return [okReply(#"{"job": "4"}"#, status: "202 Accepted")]
            }
            if head.hasPrefix("POST /rename") {
                return [okReply(#"{"ok": true}"#)]
            }
            return [okReply(named)]
        }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        store.opened()
        store.menuOpen = true
        store.screen = .add
        store.startAdd(.add)
        await waitFor { store.addNeedsNaming }
        #expect(store.addHolding)
        #expect(MenuLogic.sheetKeyAction(keyCode: 53, characters: nil, command: false, holding: store.addHolding) == .back)
        store.backFromAdd()
        #expect(store.screen == .menu)
        await waitFor { server.requests.contains { $0.head.hasPrefix("POST /rename") } }
        let rename = try #require(server.requests.first { $0.head.hasPrefix("POST /rename") })
        let body = try JSONSerialization.jsonObject(with: rename.body) as? [String: String]
        #expect(body?["name"] == "Alex's Account" && body?["short"] == "Alex", "\(String(describing: body))")
        #expect(store.addRun == nil)
    }

    @Test func theWaitingScreensCarryBack() throws {
        let state = try ServiceState.decode(Data(addWaitingState.utf8))
        let job = try #require(state.jobs?.first)
        let with = try seen(JobWaitingView(job: job, state: state, back: {}))
        #expect(with.contains { $0.contains("Back") }, "\(with)")
        let without = try seen(JobWaitingView(job: job, state: state))
        #expect(!without.contains { $0.contains("Back") }, "\(without)")
        var run = AddRun(target: .mac, kind: .add, stages: [.devboxAdopt])
        run.began("9", at: Date())
        let sheet = try seen(AddAccountView(state: state, step: run.step, run: run, problem: nil, zone: .current, handlers: AddHandlers(back: {})))
        #expect(sheet.contains { $0.contains("Back") }, "\(sheet)")
        let first = try seen(AddAccountView(state: state, step: .start, problem: nil, zone: .current, handlers: AddHandlers(back: {})))
        #expect(first.contains { $0.contains("Back") }, "\(first)")
        let done = try seen(AddAccountView(state: state, step: .done("Lifeline took over Studio's own sign-in for Work Account."), problem: nil, zone: .current, handlers: AddHandlers(back: {})))
        #expect(done.contains { $0.contains("Back") }, "\(done)")
        let naming = try seen(AddAccountView(state: state, step: .added("j"), problem: nil, zone: .current, handlers: AddHandlers(back: {})))
        #expect(naming.contains { $0.contains("Back") }, "\(naming)")
    }
}
