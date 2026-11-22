import Foundation
import Testing
@testable import Lifeline
@testable import LifelineKit

private let switchedState = #"{"version": 1, "mac": {"view": "mac", "rows": [{"uuid": "j", "name": "Alex's Account", "is_active": true}, {"uuid": "p", "name": "Personal Account"}], "action_kind": "undo", "action_text": "Undo the switch"}, "accounts": [{"uuid": "j", "name": "Alex's Account", "short": "Alex", "number": 1}, {"uuid": "p", "name": "Personal Account", "short": "Personal", "number": 2}]}"#

@MainActor
@Suite(.serialized)
struct SwitchGuardTests {
    private func server(gate: Flag) throws -> FakeServer {
        try FakeServer { head, _ in
            if head.hasPrefix("POST /switch") || head.hasPrefix("POST /undo") {
                while gate.value {
                    Thread.sleep(forTimeInterval: 0.01)
                }
                return [okReply(#"{"to_uuid": "j", "message": "This Mac now uses Alex's Account."}"#)]
            }
            return [okReply(switchedState)]
        }
    }

    private func posts(_ server: FakeServer) -> [String] {
        server.requests.filter { $0.head.hasPrefix("POST ") }.map { String($0.head.split(separator: " ")[1]) }
    }

    @Test func aRowClickAnswersAtOnceUntilTheServiceReplies() async throws {
        let gate = Flag()
        let fake = try server(gate: gate)
        defer { fake.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: fake.path)), sizes: nil)
        await store.refresh()
        store.run(.switchTo("p"), via: .row)
        #expect(store.pendingSwitch == "p")
        gate.value = false
        await waitFor { !store.switching && store.pendingSwitch == nil }
        #expect(store.pendingSwitch == nil)
    }

    @Test func aDoubleClickOnSwitchSendsOneSwitchAndNoUndo() async throws {
        let gate = Flag()
        let fake = try server(gate: gate)
        defer { fake.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: fake.path)), sizes: nil)
        await store.refresh()
        store.run(.switchTo("j"), via: .footer)
        store.run(.switchTo("j"), via: .footer)
        store.run(.undo, via: .menuUndo)
        #expect(store.switching)
        gate.value = false
        await waitFor { !store.switching }
        #expect(!store.switching)
        #expect(posts(fake) == ["/switch"])
        let body = try JSONSerialization.jsonObject(with: try #require(fake.requests.first { $0.head.hasPrefix("POST /switch") }).body) as? [String: String]
        #expect(body == ["uuid": "j", "via": "footer"])
    }

    @Test func keysOneToFourAreIgnoredDuringASwitch() async throws {
        let gate = Flag()
        let fake = try server(gate: gate)
        defer { fake.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: fake.path)), sizes: nil)
        await store.refresh()
        store.run(.switchTo("p"), via: .row)
        var pressed: [MenuCommand] = []
        for code: UInt16 in [18, 19, 20, 21] {
            let action = MenuLogic.keyAction(keyCode: code, characters: nil, shift: false, command: false, option: false, control: false, state: store.state, detailOpen: false)
            if case .command(let command) = action {
                pressed.append(command)
                store.run(command, via: .key)
            }
        }
        #expect(pressed == [.switchTo("p")])
        store.run(.undo, via: .notification)
        gate.value = false
        await waitFor { !store.switching }
        #expect(posts(fake) == ["/switch"])
    }

    @Test func theGuardLiftsOnlyWhenAStateFetchedAfterTheReplyArrives() async throws {
        let gate = Flag()
        let fake = try server(gate: gate)
        defer { fake.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: fake.path)), sizes: nil)
        await store.refresh()
        store.run(.switchTo("j"), via: .row)
        await store.refresh()
        #expect(store.switching)
        gate.value = false
        await waitFor { !store.switching }
        store.run(.undo, via: .menuUndo)
        await waitFor { !store.switching }
        #expect(posts(fake) == ["/switch", "/undo"])
        let undo = try #require(fake.requests.last { $0.head.hasPrefix("POST /undo") })
        #expect(String(decoding: undo.body, as: UTF8.self) == #"{"via":"menu-undo"}"#)
    }

    @Test func aCancelOrSignInIsNotHeldBackByTheGuard() async throws {
        let gate = Flag()
        let fake = try FakeServer { head, _ in
            if head.hasPrefix("POST /switch") {
                while gate.value {
                    Thread.sleep(forTimeInterval: 0.01)
                }
                return [okReply(#"{"to_uuid": "j"}"#)]
            }
            if head.hasPrefix("POST /resign") {
                return [okReply(#"{"job": "r"}"#, status: "202 Accepted")]
            }
            return [okReply(switchedState)]
        }
        defer { fake.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: fake.path)), sizes: nil)
        await store.refresh()
        store.run(.switchTo("j"), via: .row)
        #expect(store.switching)
        store.run(.resign("p"), via: .row)
        gate.value = false
        await waitFor { fake.requests.contains { $0.head.hasPrefix("POST /resign/p") } }
        await waitFor { !store.switching }
        #expect(Set(posts(fake)) == ["/switch", "/resign/p"])
    }

    @Test func aFetchThatStartedBeforeTheReplyDoesNotLiftTheGuard() {
        var guarded = SwitchGuard()
        let first = guarded.begin()
        let second = guarded.begin()
        #expect(first && !second)
        let early = guarded.fetchStarted()
        guarded.replied()
        guarded.fetched(early)
        #expect(guarded.busy)
        let late = guarded.fetchStarted()
        guarded.fetched(late)
        #expect(!guarded.busy)
        let again = guarded.begin()
        #expect(again)
        let before = guarded.fetchStarted()
        guarded.fetched(before)
        #expect(guarded.busy)
    }

    @Test func undoWaitsOneSecondAfterItAppears() {
        let shown = Date(timeIntervalSince1970: 1_000)
        #expect(MenuLogic.armDelay(.undo) == 1)
        #expect(MenuLogic.armDelay(.switchTo("j")) == 0)
        #expect(MenuLogic.armDelay(.cancel) == 0)
        #expect(MenuLogic.armDelay(nil) == 0)
        #expect(!MenuLogic.armed(shownAt: shown, now: shown, delay: 1))
        #expect(!MenuLogic.armed(shownAt: shown, now: shown.addingTimeInterval(0.99), delay: 1))
        #expect(MenuLogic.armed(shownAt: shown, now: shown.addingTimeInterval(1), delay: 1))
        #expect(MenuLogic.armed(shownAt: shown, now: shown, delay: 0))
    }

    @Test func eachControlNamesItself() {
        #expect(Via.footer(.undo) == .menuUndo)
        #expect(Via.footer(.switchTo("j")) == .footer)
        #expect(Via.footer(.cancel) == .footer)
        #expect([Via.row, .footer, .key, .notification, .menuUndo].map(\.rawValue) == ["row", "footer", "key", "notification", "menu-undo"])
        #expect(MenuCommand.switchTo("j").switches && MenuCommand.undo.switches)
        #expect(!MenuCommand.cancel.switches && !MenuCommand.resign("j").switches)
    }
}

struct ServiceEnvironmentTests {
    @Test func theServiceGetsOnlyItsOwnVariables() {
        let dirty = [
            "PATH": "/usr/bin:/bin",
            "HOME": "/home/someone",
            "USER": "someone",
            "LOGNAME": "someone",
            "TMPDIR": "/var/folders/xy/T/",
            "LANG": "en_GB.UTF-8",
            "LC_ALL": "en_GB.UTF-8",
            "LC_CTYPE": "UTF-8",
            "LIFELINE_SOCKET": "/tmp/x.sock",
            "LIFELINE_DEVBOX": "192.0.2.10:7433",
            "CLAUDE_CODE_MESSAGING_TOKEN": "secret",
            "CLAUDECODE": "1",
            "CLAUDE_CONFIG_DIR": "/home/someone/.claude-x",
            "ANTHROPIC_API_KEY": "secret",
            "ANTHROPIC_BASE_URL": "https://example.com",
            "SSH_AUTH_SOCK": "/tmp/agent",
            "TERM_PROGRAM": "ghostty",
            "OTEL_EXPORTER": "x",
            "NODE_OPTIONS": "--x",
            "PWD": "/home/someone/project"
        ]
        let built = ServiceSupervisor.environment(from: dirty)
        #expect(Set(built.keys) == ["PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "LIFELINE_SOCKET", "LIFELINE_DEVBOX"])
        #expect(!built.keys.contains { $0.hasPrefix("CLAUDE") || $0.hasPrefix("ANTHROPIC") })
        #expect(!built.values.contains("secret"))
        #expect(built["HOME"] == "/home/someone")
        #expect(built["LIFELINE_SOCKET"] == "/tmp/x.sock")
        #expect(built["PATH"]?.hasSuffix(":/usr/bin:/bin") == true)
        #expect(built["PATH"]?.hasPrefix("/Library/Frameworks/Python.framework/Versions/3.14/bin:/Library/Frameworks/Python.framework/Versions/3.13/bin:/home/someone/.local/bin:") == true)
    }

    @Test func missingBasicsAreFilledIn() {
        let built = ServiceSupervisor.environment(from: ["CLAUDECODE": "1"])
        #expect(Set(built.keys) == ["PATH", "HOME", "USER", "LOGNAME", "TMPDIR"])
        #expect(built["HOME"] == NSHomeDirectory())
        #expect(built["USER"] == NSUserName())
        #expect(built["PATH"]?.hasSuffix("/usr/bin:/bin:/usr/sbin:/sbin") == true)
    }

    @Test func aStartedServiceSeesNoneOfTheSessionVariables() throws {
        let built = ServiceSupervisor.environment(from: ["PATH": "/usr/bin:/bin", "HOME": NSHomeDirectory(), "CLAUDE_CODE_MESSAGING_TOKEN": "secret", "ANTHROPIC_API_KEY": "secret"])
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.environment = built
        let pipe = Pipe()
        process.standardOutput = pipe
        try process.run()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        let names = String(decoding: data, as: UTF8.self).split(separator: "\n").compactMap { $0.split(separator: "=").first.map(String.init) }
        #expect(Set(names) == Set(built.keys))
        #expect(!names.contains { $0.hasPrefix("CLAUDE") || $0.hasPrefix("ANTHROPIC") })
    }
}
