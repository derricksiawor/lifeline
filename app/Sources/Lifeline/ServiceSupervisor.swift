import Foundation
import LifelineKit

final class ServiceSupervisor: @unchecked Sendable {
    private let api: LifelineAPI
    private let servicePath: String?
    private let expectedBuild: String?
    private let lock = NSLock()
    private var stopping = false
    private var stopped = false
    private var child: Process?
    private var childPID: pid_t = 0
    private var signalled: Set<pid_t> = []
    private var checked: Set<pid_t> = []
    private var sameBuild: Set<pid_t> = []
    private var thread: Thread?
    static let quitWait: TimeInterval = 1
    private let wake = DispatchSemaphore(value: 0)
    private let finished = DispatchSemaphore(value: 0)

    private let replaces: Bool

    init(api: LifelineAPI, servicePath: String?, expectedBuild: String? = nil, replaces: Bool = true) {
        self.api = api
        self.servicePath = servicePath
        self.expectedBuild = expectedBuild
        self.replaces = replaces
    }

    func start() {
        let runner = Thread { [self] in
            run()
            finished.signal()
        }
        runner.name = "service-supervisor"
        lock.lock()
        thread = runner
        lock.unlock()
        runner.start()
    }

    func stop() {
        lock.lock()
        if stopped {
            lock.unlock()
            return
        }
        stopped = true
        stopping = true
        let running = child
        let started = thread != nil
        lock.unlock()
        wake.signal()
        if let running, running.isRunning {
            running.terminate()
            let end = Date().addingTimeInterval(Self.quitWait)
            while running.isRunning, Date() < end {
                Thread.sleep(forTimeInterval: 0.05)
            }
        }
        if started {
            _ = finished.wait(timeout: .now() + Self.quitWait)
        }
        stopAnsweringService()
    }

    private var quickAPI: LifelineAPI {
        LifelineAPI(client: SocketClient(path: api.client.path, limit: 0.5))
    }

    private var ownChildPID: pid_t {
        lock.lock()
        defer { lock.unlock() }
        return childPID
    }

    private func peer(_ api: LifelineAPI) -> pid_t? {
        guard let pid = api.client.peerProcess(), pid != getpid() else { return nil }
        return pid
    }

    static func reportedBuild(_ api: LifelineAPI) -> String?? {
        guard let state = try? api.fetchState(appearance: "dark") else { return nil }
        return .some(state.build)
    }

    private func signal(_ pid: pid_t, waitingUpTo seconds: TimeInterval, polling api: LifelineAPI) {
        lock.lock()
        signalled.insert(pid)
        lock.unlock()
        kill(pid, SIGTERM)
        let end = Date().addingTimeInterval(seconds)
        while Date() < end, api.answers() {
            Thread.sleep(forTimeInterval: 0.05)
        }
    }

    var checkedPIDs: Set<pid_t> {
        lock.lock()
        defer { lock.unlock() }
        return checked
    }

    func replaceForeignService() -> Bool {
        guard replaces, expectedBuild != nil, let pid = peer(api) else { return false }
        lock.lock()
        let known = checked.contains(pid) || signalled.contains(pid)
        lock.unlock()
        if known {
            return false
        }
        if pid == ownChildPID {
            remember(pid, same: true)
            return false
        }
        guard let reported = Self.reportedBuild(api) else {
            return false
        }
        guard ServiceIdentity.decide(expected: expectedBuild, reported: reported, ownChild: false) == .replace else {
            remember(pid, same: true)
            return false
        }
        guard ServiceIdentity.looksLikeService(Self.arguments(of: pid)) else {
            remember(pid, same: false)
            return false
        }
        signal(pid, waitingUpTo: 10, polling: api)
        return true
    }

    private func remember(_ pid: pid_t, same: Bool) {
        lock.lock()
        checked.insert(pid)
        if same {
            sameBuild.insert(pid)
        }
        lock.unlock()
    }

    func stopAnsweringService() {
        let quick = quickAPI
        guard let pid = peer(quick) else { return }
        lock.lock()
        let known = sameBuild.contains(pid)
        lock.unlock()
        var stops = pid == ownChildPID || (known && expectedBuild?.isEmpty == false)
        if !stops, let reported = Self.reportedBuild(quick) {
            stops = ServiceIdentity.stopsOnQuit(expected: expectedBuild, reported: reported, ownChild: false)
        }
        guard stops, ServiceIdentity.looksLikeService(Self.arguments(of: pid)) else {
            return
        }
        signal(pid, waitingUpTo: Self.quitWait, polling: quick)
    }

    static func arguments(of pid: pid_t) -> [String] {
        var names: [Int32] = [CTL_KERN, KERN_PROCARGS2, pid]
        var size = 0
        guard sysctl(&names, 3, nil, &size, nil, 0) == 0, size > 0 else { return [] }
        var buffer = [UInt8](repeating: 0, count: size)
        guard sysctl(&names, 3, &buffer, &size, nil, 0) == 0 else { return [] }
        return ServiceIdentity.arguments(fromProcArgs: Data(buffer.prefix(size)))
    }

    private var isStopping: Bool {
        lock.lock()
        defer { lock.unlock() }
        return stopping
    }

    private func pause(_ seconds: TimeInterval) {
        _ = wake.wait(timeout: .now() + seconds)
    }

    private func run() {
        var delay: TimeInterval = 2
        while !isStopping {
            if api.answers() {
                if replaceForeignService() {
                    continue
                }
                delay = 2
                pause(5)
                continue
            }
            let errors = ErrorText()
            guard let process = launch(errors) else {
                pause(30)
                continue
            }
            let began = Date()
            process.waitUntilExit()
            lock.lock()
            child = nil
            lock.unlock()
            if isStopping {
                break
            }
            if Self.reportsRunning(errors) {
                delay = 2
                pause(5)
                continue
            }
            if Date().timeIntervalSince(began) > 60 {
                delay = 2
            }
            pause(delay)
            delay = min(delay * 2, 30)
        }
    }

    private func launch(_ errors: ErrorText) -> Process? {
        guard let servicePath, FileManager.default.isExecutableFile(atPath: servicePath) else {
            return nil
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = Self.arguments(servicePath)
        process.environment = Self.environment()
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle.nullDevice
        let pipe = Pipe()
        process.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            if data.isEmpty {
                handle.readabilityHandler = nil
            } else {
                errors.add(data)
            }
        }
        lock.lock()
        defer { lock.unlock() }
        if stopping {
            return nil
        }
        do {
            try process.run()
        } catch {
            return nil
        }
        child = process
        childPID = process.processIdentifier
        return process
    }

    static let runningSentence = "Lifeline is already running."

    static func arguments(_ servicePath: String) -> [String] {
        ["python3", "-I", "-B", servicePath, "serve"]
    }

    static func reportsRunning(_ errors: ErrorText) -> Bool {
        Thread.sleep(forTimeInterval: 0.2)
        return errors.text.contains(runningSentence)
    }

    static let passed = ["HOME", "USER", "LOGNAME", "TMPDIR", "LANG"]

    static func environment(from inherited: [String: String] = ProcessInfo.processInfo.environment) -> [String: String] {
        var values: [String: String] = [:]
        for (key, value) in inherited where passed.contains(key) || key.hasPrefix("LC_") || key.hasPrefix("LIFELINE_") {
            values[key] = value
        }
        let home = values["HOME"] ?? NSHomeDirectory()
        values["HOME"] = home
        values["USER"] = values["USER"] ?? NSUserName()
        values["LOGNAME"] = values["LOGNAME"] ?? values["USER"]
        values["TMPDIR"] = values["TMPDIR"] ?? NSTemporaryDirectory()
        let leading = [
            "/Library/Frameworks/Python.framework/Versions/3.14/bin",
            "/Library/Frameworks/Python.framework/Versions/3.13/bin",
            home + "/.local/bin",
            "/opt/homebrew/bin",
            "/usr/local/bin"
        ]
        let existing = (inherited["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin").split(separator: ":").map(String.init)
        var seen = Set<String>()
        let merged = (leading + existing).filter { seen.insert($0).inserted }
        values["PATH"] = merged.joined(separator: ":")
        return values
    }
}

final class ErrorText: @unchecked Sendable {
    private let lock = NSLock()
    private var collected = Data()

    func add(_ data: Data) {
        lock.lock()
        defer { lock.unlock() }
        if collected.count < 4096 {
            collected.append(data.prefix(4096 - collected.count))
        }
    }

    var text: String {
        lock.lock()
        defer { lock.unlock() }
        return String(decoding: collected, as: UTF8.self)
    }
}
