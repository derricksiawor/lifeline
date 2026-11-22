import Darwin
import Foundation
import Testing
@testable import Lifeline
@testable import LifelineKit

private let fakeService = """
import json, os, signal, socket, threading, time
parent = os.getppid()
def orphaned():
    while True:
        time.sleep(0.5)
        if os.getppid() != parent:
            try:
                os.unlink(path)
            except OSError:
                pass
            os._exit(0)
root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
top = os.path.dirname(root)
path = open(os.path.join(top, "socket.txt")).read().strip()
build = open(os.path.join(root, "build.txt")).read().strip()
mode_file = os.path.join(root, "mode.txt")
mode = open(mode_file).read().strip() if os.path.exists(mode_file) else ""
hits = os.path.join(root, "hits.txt")
reply = {"version": 1}
if build != "none":
    reply["build"] = build
body = json.dumps(reply).encode()
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    os.unlink(path)
except OSError:
    pass
server.bind(path)
server.listen(8)
def stop(*_):
    with open(os.path.join(root, "term.txt"), "a") as handle:
        handle.write("TERM\\n")
    if mode == "stubborn":
        return
    try:
        os.unlink(path)
    except OSError:
        pass
    os._exit(0)
signal.signal(signal.SIGTERM, stop)
threading.Thread(target=orphaned, daemon=True).start()
while True:
    conn, _ = server.accept()
    try:
        conn.settimeout(2)
        data = b""
        while b"\\r\\n\\r\\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
        if data.startswith(b"GET /state"):
            with open(hits, "a") as handle:
                handle.write("hit\\n")
        if data and mode == "fail":
            conn.sendall(b"HTTP/1.1 500 Internal Server Error\\r\\nContent-Length: 0\\r\\nConnection: close\\r\\n\\r\\n")
        elif data and mode == "slow":
            time.sleep(3)
            conn.sendall(b"HTTP/1.1 200 OK\\r\\nContent-Type: application/json\\r\\nContent-Length: %d\\r\\nConnection: close\\r\\n\\r\\n" % len(body) + body)
        elif data:
            conn.sendall(b"HTTP/1.1 200 OK\\r\\nContent-Type: application/json\\r\\nContent-Length: %d\\r\\nConnection: close\\r\\n\\r\\n" % len(body) + body)
    except OSError:
        pass
    finally:
        conn.close()
"""

final class FakeServices {
    let folder: String
    let socket: String

    init() throws {
        signal(SIGPIPE, SIG_IGN)
        var template = Array("/tmp/lifeline-test-XXXXXX".utf8CString)
        let made = template.withUnsafeMutableBufferPointer { buffer in
            mkdtemp(buffer.baseAddress) != nil
        }
        guard made else { throw CocoaError(.fileWriteUnknown) }
        folder = String(decoding: template.prefix { $0 != 0 }.map { UInt8(bitPattern: $0) }, as: UTF8.self)
        socket = folder + "/s.sock"
        try socket.write(toFile: folder + "/socket.txt", atomically: true, encoding: .utf8)
    }

    func service(_ name: String, build: String, mode: String = "") throws -> String {
        let bin = folder + "/" + name + "/bin"
        try FileManager.default.createDirectory(atPath: bin, withIntermediateDirectories: true)
        try build.write(toFile: folder + "/" + name + "/build.txt", atomically: true, encoding: .utf8)
        try mode.write(toFile: folder + "/" + name + "/mode.txt", atomically: true, encoding: .utf8)
        let script = bin + "/lifeline"
        try fakeService.write(toFile: script, atomically: true, encoding: .utf8)
        chmod(script, 0o755)
        return script
    }

    func launch(_ script: String) throws -> Process {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = ServiceSupervisor.arguments(script)
        process.environment = ServiceSupervisor.environment()
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        try process.run()
        return process
    }

    var api: LifelineAPI {
        LifelineAPI(client: SocketClient(path: socket, limit: 1))
    }

    func waitUntil(_ seconds: TimeInterval, _ check: () -> Bool) -> Bool {
        let end = Date().addingTimeInterval(seconds)
        while Date() < end {
            if check() {
                return true
            }
            Thread.sleep(forTimeInterval: 0.1)
        }
        return check()
    }

    func lines(_ name: String, _ file: String) -> Int {
        ((try? String(contentsOfFile: folder + "/" + name + "/" + file, encoding: .utf8)) ?? "").split(separator: "\n").count
    }

    func build() -> String? {
        (try? api.fetchState(appearance: "dark"))?.build
    }

    func finish(_ processes: [Process], _ supervisors: [ServiceSupervisor] = []) {
        for supervisor in supervisors {
            supervisor.stop()
        }
        for process in processes where process.isRunning {
            process.terminate()
            if !waitUntil(3, { !process.isRunning }) {
                kill(process.processIdentifier, SIGKILL)
                _ = waitUntil(1) { !process.isRunning }
            }
        }
        try? FileManager.default.removeItem(atPath: folder)
    }
}

@Suite(.serialized)
struct SupervisorTests {
    @Test func aServiceFromAnotherBuildIsReplacedByTheBundledOne() throws {
        let fakes = try FakeServices()
        let old = try fakes.launch(try fakes.service("old", build: "A"))
        defer { fakes.finish([old]) }
        #expect(fakes.waitUntil(10) { fakes.build() == "A" })
        let ours = try fakes.service("ours", build: "B")
        let supervisor = ServiceSupervisor(api: fakes.api, servicePath: ours, expectedBuild: "B")
        defer { supervisor.stop() }
        supervisor.start()
        #expect(fakes.waitUntil(20) { fakes.build() == "B" })
        #expect(fakes.waitUntil(5) { !old.isRunning })
        supervisor.stop()
        #expect(fakes.waitUntil(5) { !fakes.api.answers() })
    }

    @Test func aServiceThatReportsNoBuildIsReplacedToo() throws {
        let fakes = try FakeServices()
        let old = try fakes.launch(try fakes.service("old", build: "none"))
        defer { fakes.finish([old]) }
        #expect(fakes.waitUntil(10) { fakes.api.answers() })
        #expect(fakes.build() == nil)
        let supervisor = ServiceSupervisor(api: fakes.api, servicePath: try fakes.service("ours", build: "B"), expectedBuild: "B")
        defer { supervisor.stop() }
        supervisor.start()
        #expect(fakes.waitUntil(20) { fakes.build() == "B" })
        #expect(!old.isRunning)
        supervisor.stop()
        #expect(fakes.waitUntil(5) { !fakes.api.answers() })
    }

    @Test func quittingStopsTheBundledServiceEvenWhenSomethingElseStartedIt() throws {
        let fakes = try FakeServices()
        let script = try fakes.service("ours", build: "B")
        let running = try fakes.launch(script)
        defer { fakes.finish([running]) }
        #expect(fakes.waitUntil(10) { fakes.build() == "B" })
        let supervisor = ServiceSupervisor(api: fakes.api, servicePath: script, expectedBuild: "B")
        supervisor.stop()
        #expect(fakes.waitUntil(5) { !running.isRunning })
        #expect(!fakes.api.answers())
    }

    @Test func quittingStopsTheServiceItStarted() throws {
        let fakes = try FakeServices()
        defer { fakes.finish([]) }
        let supervisor = ServiceSupervisor(api: fakes.api, servicePath: try fakes.service("ours", build: "B"), expectedBuild: "B")
        defer { supervisor.stop() }
        supervisor.start()
        #expect(fakes.waitUntil(20) { fakes.build() == "B" })
        supervisor.stop()
        #expect(fakes.waitUntil(5) { !fakes.api.answers() })
    }

    @Test func aBuildWithoutAStampNeverTouchesARunningService() throws {
        let fakes = try FakeServices()
        let running = try fakes.launch(try fakes.service("other", build: "A"))
        defer { fakes.finish([running]) }
        #expect(fakes.waitUntil(10) { fakes.build() == "A" })
        let supervisor = ServiceSupervisor(api: fakes.api, servicePath: nil, expectedBuild: nil)
        #expect(!supervisor.replaceForeignService())
        supervisor.stop()
        Thread.sleep(forTimeInterval: 0.5)
        #expect(running.isRunning)
        #expect(fakes.build() == "A")
    }

    @Test func aDifferentBuildIsNeverStoppedOnQuit() throws {
        let fakes = try FakeServices()
        let running = try fakes.launch(try fakes.service("other", build: "A"))
        defer { fakes.finish([running]) }
        #expect(fakes.waitUntil(10) { fakes.build() == "A" })
        let supervisor = ServiceSupervisor(api: fakes.api, servicePath: nil, expectedBuild: "B")
        supervisor.stop()
        Thread.sleep(forTimeInterval: 0.5)
        #expect(running.isRunning)
    }

    @Test func aProcessThatIsNotTheServiceIsNeverSignalled() throws {
        let server = try FakeServer { _, _ in [okReply(#"{"version": 1, "build": "A"}"#)] }
        defer { server.close() }
        let supervisor = ServiceSupervisor(api: LifelineAPI(client: SocketClient(path: server.path)), servicePath: nil, expectedBuild: "B")
        #expect(SocketClient(path: server.path).peerProcess() == getpid())
        #expect(!supervisor.replaceForeignService())
        supervisor.stop()
    }

    @Test func aFailedOrSlowStateIsNeverReadAsADifferentBuild() throws {
        for mode in ["fail", "slow"] {
            let fakes = try FakeServices()
            let old = try fakes.launch(try fakes.service("old", build: "A", mode: mode))
            defer { fakes.finish([old]) }
            #expect(fakes.waitUntil(10) { fakes.api.answers() })
            let supervisor = ServiceSupervisor(api: fakes.api, servicePath: nil, expectedBuild: "B")
            #expect(!supervisor.replaceForeignService())
            #expect(!supervisor.replaceForeignService())
            #expect(supervisor.checkedPIDs.isEmpty)
            #expect(old.isRunning)
            #expect(fakes.lines("old", "term.txt") == 0)
            supervisor.stop()
            #expect(old.isRunning)
        }
    }

    @Test func eachServiceIsCheckedOnceNotEveryRound() throws {
        let fakes = try FakeServices()
        let running = try fakes.launch(try fakes.service("ours", build: "B"))
        defer { fakes.finish([running]) }
        #expect(fakes.waitUntil(10) { fakes.api.answers() })
        let supervisor = ServiceSupervisor(api: fakes.api, servicePath: nil, expectedBuild: "B")
        for _ in 0..<4 {
            #expect(!supervisor.replaceForeignService())
        }
        #expect(fakes.lines("ours", "hits.txt") == 1)
        #expect(supervisor.checkedPIDs == [running.processIdentifier])
        supervisor.stop()
        #expect(fakes.lines("ours", "hits.txt") == 1)
        #expect(fakes.waitUntil(5) { !running.isRunning })
    }

    @Test func quittingIsShortEvenWhenTheServiceIsSlowOrIgnoresTheSignal() throws {
        let fakes = try FakeServices()
        let slow = try fakes.launch(try fakes.service("slow", build: "B", mode: "slow"))
        #expect(fakes.waitUntil(10) { fakes.api.answers() })
        let first = ServiceSupervisor(api: fakes.api, servicePath: nil, expectedBuild: "B")
        var began = Date()
        first.stop()
        #expect(Date().timeIntervalSince(began) < 2)
        fakes.finish([slow])
        let others = try FakeServices()
        let stubborn = try others.launch(try others.service("stubborn", build: "B", mode: "stubborn"))
        defer { others.finish([stubborn]) }
        #expect(others.waitUntil(10) { others.api.answers() })
        let second = ServiceSupervisor(api: others.api, servicePath: nil, expectedBuild: "B")
        began = Date()
        second.stop()
        #expect(Date().timeIntervalSince(began) < 2.5)
        #expect(others.waitUntil(2) { others.lines("stubborn", "term.txt") == 1 })
    }

    @Test func aSupervisorWithNoServicePathNeverStartsOne() throws {
        let fakes = try FakeServices()
        defer { fakes.finish([]) }
        let supervisor = ServiceSupervisor(api: fakes.api, servicePath: nil, expectedBuild: "B")
        supervisor.start()
        Thread.sleep(forTimeInterval: 2)
        #expect(!fakes.api.answers())
        let began = Date()
        supervisor.stop()
        #expect(Date().timeIntervalSince(began) < 2)
    }

    @Test func aCheckCopyNeverReplacesAService() throws {
        let fakes = try FakeServices()
        let old = try fakes.launch(try fakes.service("old", build: "A"))
        defer { fakes.finish([old]) }
        #expect(fakes.waitUntil(10) { fakes.build() == "A" })
        let before = fakes.lines("old", "hits.txt")
        let supervisor = ServiceSupervisor(api: fakes.api, servicePath: nil, expectedBuild: "B", replaces: false)
        #expect(!supervisor.replaceForeignService())
        #expect(fakes.lines("old", "hits.txt") == before)
        supervisor.start()
        Thread.sleep(forTimeInterval: 3)
        #expect(old.isRunning)
        #expect(fakes.lines("old", "term.txt") == 0)
        #expect(supervisor.checkedPIDs.isEmpty)
        supervisor.stop()
        #expect(old.isRunning)
        #expect(fakes.lines("old", "term.txt") == 0)
    }

    @Test func theRunningServicesArgumentsAreRead() throws {
        let fakes = try FakeServices()
        let script = try fakes.service("ours", build: "B")
        let running = try fakes.launch(script)
        defer { fakes.finish([running]) }
        #expect(fakes.waitUntil(10) { fakes.api.answers() })
        let found = ServiceSupervisor.arguments(of: running.processIdentifier)
        #expect(found.contains("serve"))
        #expect(found.contains(script))
        #expect(ServiceIdentity.looksLikeService(found))
        #expect(fakes.api.client.peerProcess() == running.processIdentifier)
    }
}

@Suite
struct ServiceIdentityTests {
    @Test func whichServiceIsReplacedAndWhichIsStopped() {
        #expect(ServiceIdentity.decide(expected: "B", reported: "A", ownChild: false) == .replace)
        #expect(ServiceIdentity.decide(expected: "B", reported: nil, ownChild: false) == .replace)
        #expect(ServiceIdentity.decide(expected: "B", reported: "B", ownChild: false) == .keep)
        #expect(ServiceIdentity.decide(expected: "B", reported: nil, ownChild: true) == .keep)
        #expect(ServiceIdentity.decide(expected: nil, reported: "A", ownChild: false) == .keep)
        #expect(ServiceIdentity.decide(expected: "", reported: "A", ownChild: false) == .keep)
        #expect(ServiceIdentity.stopsOnQuit(expected: "B", reported: "B", ownChild: false))
        #expect(ServiceIdentity.stopsOnQuit(expected: nil, reported: nil, ownChild: true))
        #expect(!ServiceIdentity.stopsOnQuit(expected: "B", reported: "A", ownChild: false))
        #expect(!ServiceIdentity.stopsOnQuit(expected: nil, reported: "B", ownChild: false))
    }

    @Test func onlyALifelineServeCommandLooksLikeTheService() {
        #expect(ServiceIdentity.looksLikeService(["python3", "-I", "-B", "/Applications/Lifeline.app/Contents/Resources/core/bin/lifeline", "serve"]))
        #expect(!ServiceIdentity.looksLikeService(["python3", "/x/bin/lifeline", "status"]))
        #expect(!ServiceIdentity.looksLikeService(["python3", "/x/bin/other", "serve"]))
        #expect(!ServiceIdentity.looksLikeService([]))
    }

    @Test func theKernelsArgumentListIsParsed() {
        var data = Data()
        var count = Int32(3).littleEndian
        data.append(Data(bytes: &count, count: 4))
        data.append(Data("/usr/bin/python3\0\0\0".utf8))
        data.append(Data("python3\0/a/bin/lifeline\0serve\0PATH=/usr/bin\0".utf8))
        #expect(ServiceIdentity.arguments(fromProcArgs: data) == ["python3", "/a/bin/lifeline", "serve"])
        #expect(ServiceIdentity.arguments(fromProcArgs: Data()) == [])
    }
}

@Suite
struct SupervisorStopTests {
    @Test func stoppingTwiceReturnsAtOnce() {
        let supervisor = ServiceSupervisor(api: LifelineAPI(client: SocketClient(path: "/tmp/lifeline-nothing-here.sock", limit: 0.2)), servicePath: nil)
        supervisor.stop()
        let began = Date()
        supervisor.stop()
        #expect(Date().timeIntervalSince(began) < 0.5)
    }
}
