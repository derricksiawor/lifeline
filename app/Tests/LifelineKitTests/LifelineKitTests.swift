import Foundation
import Testing
@testable import LifelineKit

@Suite(.serialized)
struct StateTests {
    @Test func decodesTheServiceSample() throws {
        let state = try ServiceState.decode(Data(sampleState.utf8))
        #expect(state.version == 1)
        #expect(state.mac?.iconName == "Alex")
        #expect(state.mac?.iconPct == "100%")
        #expect(state.mac?.iconTone == "limit")
        #expect(state.mac?.head == "Switching this Mac to Team Account.")
        #expect(state.mac?.sub == "In 5 seconds. Alex's Account is out until 17:00.")
        #expect(state.accounts?.first?.inUse == ["mac"])
        #expect(state.devbox?.reachable == true)
    }

    @Test func iconFieldsComeFromTheMacView() throws {
        let state = try ServiceState.decode(Data(sampleState.utf8))
        let fields = IconFields.from(state)
        #expect(fields == IconFields(name: "Alex", percent: "100%", fill: 1, tone: "limit", amberLight: false))
    }

    @Test func amberLightFollowsTheViewModelField() throws {
        #expect(!IconFields.from(try ServiceState.decode(Data(sampleState.utf8))).amberLight)
        let hot = sampleState.replacingOccurrences(of: "\"icon_tone\": \"limit\",", with: "\"icon_tone\": \"limit\", \"icon_dot\": true,")
        #expect(IconFields.from(try ServiceState.decode(Data(hot.utf8))).amberLight)
        let off = sampleState.replacingOccurrences(of: "\"icon_tone\": \"limit\",", with: "\"icon_tone\": \"limit\", \"icon_dot\": false,")
        #expect(!IconFields.from(try ServiceState.decode(Data(off.utf8))).amberLight)
    }

    @Test func unknownFieldsAndMissingFieldsAreFine() throws {
        let state = try ServiceState.decode(Data(#"{"version": 2, "future": {"a": 1}, "mac": {"icon_w": "4%"}}"#.utf8))
        let fields = IconFields.from(state)
        #expect(fields.fill == 0.04)
        #expect(fields.name == "")
        #expect(!fields.amberLight)
    }

    @Test func noStateGivesTheQuietIcon() {
        let fields = IconFields.from(nil)
        #expect(fields.name == "Lifeline" && fields.percent == "" && fields.fill == 0)
        #expect(fields.lineOnly)
    }

    @Test func theIconIsOnlyTheLineUnlessTheNameIsShown() throws {
        func fields(_ settings: String) throws -> IconFields {
            IconFields.from(try ServiceState.decode(Data(#"{"mac": {"icon_name": "", "icon_pct": "42%", "icon_w": "42%", "icon_tone": "low"}\#(settings)}"#.utf8)))
        }
        #expect(try fields("").lineOnly)
        #expect(try fields(#", "settings": {}"#).lineOnly)
        #expect(try fields(#", "settings": {"hide_name": true}"#).lineOnly)
        #expect(try !fields(#", "settings": {"hide_name": false}"#).lineOnly)
        #expect(try fields("") == IconFields(name: "", percent: "42%", fill: 0.42, tone: "low", amberLight: false, lineOnly: true))
        #expect(!IconFields.from(try ServiceState.decode(Data(sampleState.utf8))).lineOnly)
    }

    @Test func theIconLabelIsTheCoresSentence() throws {
        func label(_ mac: String) throws -> String { IconFields.label(try ServiceState.decode(Data(#"{"mac": \#(mac)}"#.utf8))) }
        #expect(try label(#"{"icon_label": "Alex's Account has used 42% of its 5 hours.", "head": "Alex's Account lasts until 17:00."}"#)
            == "Alex's Account has used 42% of its 5 hours.")
        #expect(try label(#"{"icon_label": "", "head": "No account is in use on this Mac."}"#) == "No account is in use on this Mac.")
        #expect(try label(#"{"head": "No account is in use on this Mac."}"#) == "No account is in use on this Mac.")
        #expect(IconFields.label(nil) == "Lifeline is waiting for its service to answer.")
        for fixture in ["moment-good", "moment-devneeds", "extra-empty", "extra-trouble"] {
            let state = try Fixture.state(fixture)
            let core = try #require(state.mac?.iconLabel)
            #expect(!core.isEmpty && IconFields.label(state) == core, "\(fixture): \(IconFields.label(state))")
        }
    }

    @Test func notJSONFailsToDecode() {
        #expect(throws: (any Error).self) { try ServiceState.decode(Data("<html>".utf8)) }
    }

    @Test func presenceUsesTheServiceKeys() throws {
        let data = try Presence(idleSeconds: 12.5, locked: true, asleep: false).encoded()
        #expect(String(decoding: data, as: UTF8.self) == #"{"asleep":false,"idle_seconds":12.5,"locked":true}"#)
    }

    @Test func socketPathFollowsTheEnvironment() {
        #expect(SocketClient.defaultPath(environment: ["LIFELINE_SOCKET": "/x/y.sock"], home: "/h") == "/x/y.sock")
        #expect(SocketClient.defaultPath(environment: [:], home: "/h", bundleID: "com.example.lifeline") == "/h/Library/Application Support/com.example.lifeline/lifeline.sock")
        #expect(SocketClient.defaultPath(environment: ["LIFELINE_SOCKET": ""], home: "/h", bundleID: "com.example.lifeline") == "/h/Library/Application Support/com.example.lifeline/lifeline.sock")
        #expect(SocketClient.defaultPath(environment: [:], home: "/h", bundleID: "org.me.other") == "/h/Library/Application Support/org.me.other/lifeline.sock")
    }
}

@Suite(.serialized)
struct SocketTests {
    @Test func getsStateOverTheSocket() throws {
        let server = try FakeServer { _, _ in [okReply(sampleState)] }
        defer { server.close() }
        let api = LifelineAPI(client: SocketClient(path: server.path))
        let state = try api.fetchState(appearance: "light")
        #expect(state.mac?.iconName == "Alex")
        let head = server.requests[0].head
        #expect(head.hasPrefix("GET /state?appearance=light HTTP/1.1"))
        #expect(head.contains("Host: lifeline"))
    }

    @Test func postsPresenceWithABody() throws {
        let server = try FakeServer { _, _ in [okReply(#"{"present": true}"#)] }
        defer { server.close() }
        let api = LifelineAPI(client: SocketClient(path: server.path))
        try api.postPresence(Presence(idleSeconds: 3, locked: false, asleep: true))
        let seen = server.requests[0]
        #expect(seen.head.hasPrefix("POST /presence HTTP/1.1"))
        #expect(seen.head.contains("Content-Length: 47"))
        #expect(String(decoding: seen.body, as: UTF8.self) == #"{"asleep":true,"idle_seconds":3,"locked":false}"#)
    }

    @Test func readsAReplyArrivingInPieces() throws {
        let whole = okReply(sampleState)
        let pieces = stride(from: 0, to: whole.count, by: 97).map { whole.subdata(in: $0..<min($0 + 97, whole.count)) }
        let server = try FakeServer(gap: 2000) { _, _ in pieces }
        defer { server.close() }
        let reply = try SocketClient(path: server.path).request(method: "GET", target: "/state")
        #expect(reply.status == 200)
        #expect(String(decoding: reply.body, as: UTF8.self) == sampleState)
    }

    @Test func readsALargeReply() throws {
        let big = String(repeating: "a", count: 300_000)
        let server = try FakeServer { _, _ in [okReply(big)] }
        defer { server.close() }
        let reply = try SocketClient(path: server.path).request(method: "GET", target: "/")
        #expect(reply.body.count == 300_000)
    }

    @Test func aClosedSlowServerNeverTakesTheNextConnection() throws {
        let slow = try FakeServer { _, _ in
            Thread.sleep(forTimeInterval: 0.3)
            return []
        }
        _ = try? SocketClient(path: slow.path, limit: 0.05).request(method: "GET", target: "/")
        slow.close()
        let big = String(repeating: "b", count: 300_000)
        let began = Date()
        var answered = 0
        while Date().timeIntervalSince(began) < 0.6 {
            let server = try FakeServer { _, _ in [okReply(big)] }
            defer { server.close() }
            let reply = try SocketClient(path: server.path).request(method: "GET", target: "/")
            #expect(reply.body.count == 300_000)
            answered += 1
        }
        #expect(answered > 10)
    }

    @Test func readsAChunkedReply() throws {
        let raw = Data("HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n".utf8)
        let server = try FakeServer { _, _ in [raw] }
        defer { server.close() }
        let reply = try SocketClient(path: server.path).request(method: "GET", target: "/")
        #expect(String(decoding: reply.body, as: UTF8.self) == "hello world")
    }

    @Test func readsABodyEndedByClose() throws {
        let raw = Data("HTTP/1.1 200 OK\r\nConnection: close\r\n\r\nunframed".utf8)
        let server = try FakeServer { _, _ in [raw] }
        defer { server.close() }
        let reply = try SocketClient(path: server.path).request(method: "GET", target: "/")
        #expect(String(decoding: reply.body, as: UTF8.self) == "unframed")
    }

    @Test func errorStatusesAreReported() throws {
        let server = try FakeServer { _, _ in [okReply(#"{"error": "No."}"#, status: "409 Conflict")] }
        defer { server.close() }
        let api = LifelineAPI(client: SocketClient(path: server.path))
        #expect(throws: APIError.status(409)) { try api.fetchState(appearance: "dark") }
        #expect(api.answers())
    }

    @Test func aReplyThatIsNotJSONIsUnreadable() throws {
        let server = try FakeServer { _, _ in [okReply("<html>")] }
        defer { server.close() }
        let api = LifelineAPI(client: SocketClient(path: server.path))
        #expect(throws: APIError.unreadable) { try api.fetchState(appearance: "dark") }
    }

    @Test func aServerThatNeverRepliesTimesOut() throws {
        let server = try FakeServer { _, _ in
            Thread.sleep(forTimeInterval: 3)
            return []
        }
        defer { server.close() }
        let began = Date()
        #expect(throws: SocketError.timedOut) {
            try SocketClient(path: server.path, limit: 0.5).request(method: "GET", target: "/state")
        }
        #expect(Date().timeIntervalSince(began) < 1.5)
    }

    @Test func aServerThatTricklesStillStopsAtTheTotalLimit() throws {
        let slow = (0..<100).map { _ in Data("x".utf8) }
        let head = Data("HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n".utf8)
        let server = try FakeServer(gap: 100_000) { _, _ in [head] + slow }
        defer { server.close() }
        let began = Date()
        #expect(throws: SocketError.timedOut) {
            try SocketClient(path: server.path, limit: 0.6).request(method: "GET", target: "/state")
        }
        #expect(Date().timeIntervalSince(began) < 1.5)
    }

    @Test func closedWithoutReplyIsAnError() throws {
        let server = try FakeServer { _, _ in [] }
        defer { server.close() }
        #expect(throws: SocketError.closedEarly) {
            try SocketClient(path: server.path).request(method: "GET", target: "/state")
        }
    }

    @Test func aReplyOverTheCapIsRefused() throws {
        let head = Data("HTTP/1.1 200 OK\r\nContent-Length: 5000000\r\n\r\n".utf8)
        let server = try FakeServer { _, _ in [head] }
        defer { server.close() }
        #expect(throws: SocketError.tooLarge) {
            try SocketClient(path: server.path).request(method: "GET", target: "/state")
        }
    }

    @Test func aSlowServiceStillCountsAsAnswering() throws {
        let server = try FakeServer { _, _ in
            Thread.sleep(forTimeInterval: 1.5)
            return [okReply(sampleState)]
        }
        defer { server.close() }
        #expect(LifelineAPI(client: SocketClient(path: server.path, limit: 0.3)).answers())
    }

    @Test func garbageIsABadReply() throws {
        let server = try FakeServer { _, _ in [Data("nonsense\r\n\r\n".utf8)] }
        defer { server.close() }
        #expect(throws: SocketError.badReply) {
            try SocketClient(path: server.path).request(method: "GET", target: "/state")
        }
    }

    @Test func noListenerIsCannotConnect() throws {
        let folder = try FakeServer.makeFolder()
        defer { try? FileManager.default.removeItem(atPath: folder) }
        let client = SocketClient(path: folder + "/none.sock")
        #expect(throws: SocketError.cannotConnect) { try client.request(method: "GET", target: "/state") }
        #expect(!LifelineAPI(client: client).answers())
    }

    @Test func aTooLongPathIsRefused() {
        let client = SocketClient(path: "/" + String(repeating: "a", count: 200))
        #expect(throws: SocketError.pathTooLong) { try client.request(method: "GET", target: "/") }
    }
}
