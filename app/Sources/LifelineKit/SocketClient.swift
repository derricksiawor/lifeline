import Darwin
import Foundation

public enum SocketError: Error, Equatable {
    case pathTooLong
    case cannotConnect
    case timedOut
    case closedEarly
    case badReply
    case tooLarge
}

public struct SocketReply: Sendable, Equatable {
    public let status: Int
    public let body: Data

    public init(status: Int, body: Data) {
        self.status = status
        self.body = body
    }
}

public struct SocketClient: Sendable {
    public let path: String
    public let limit: TimeInterval
    public static let replyCap = 4 * 1024 * 1024

    public init(path: String, limit: TimeInterval = 2) {
        self.path = path
        self.limit = limit
    }

    public static func defaultPath(environment: [String: String] = ProcessInfo.processInfo.environment, home: String = NSHomeDirectory(), bundleID: String = Bundle.main.bundleIdentifier ?? "com.example.lifeline") -> String {
        if let set = environment["LIFELINE_SOCKET"], !set.isEmpty {
            return set
        }
        return home + "/Library/Application Support/\(bundleID)/lifeline.sock"
    }

    public func canConnect() -> Bool {
        let deadline = DispatchTime.now().uptimeNanoseconds + UInt64(limit * 1_000_000_000)
        guard let descriptor = try? open(deadline: deadline) else {
            return false
        }
        Darwin.close(descriptor)
        return true
    }

    public func peerProcess() -> pid_t? {
        let deadline = DispatchTime.now().uptimeNanoseconds + UInt64(limit * 1_000_000_000)
        guard let descriptor = try? open(deadline: deadline) else {
            return nil
        }
        defer { Darwin.close(descriptor) }
        var pid: pid_t = 0
        var size = socklen_t(MemoryLayout<pid_t>.size)
        guard getsockopt(descriptor, SOL_LOCAL, LOCAL_PEERPID, &pid, &size) == 0, pid > 0 else {
            return nil
        }
        return pid
    }

    public func request(method: String, target: String, body: Data? = nil, limit custom: TimeInterval? = nil) throws -> SocketReply {
        let deadline = DispatchTime.now().uptimeNanoseconds + UInt64((custom ?? limit) * 1_000_000_000)
        let descriptor = try open(deadline: deadline)
        defer { Darwin.close(descriptor) }

        var head = "\(method) \(target) HTTP/1.1\r\nHost: lifeline\r\nConnection: close\r\nAccept: application/json\r\n"
        if let body {
            head += "Content-Type: application/json\r\nContent-Length: \(body.count)\r\n"
        }
        head += "\r\n"
        var outgoing = Data(head.utf8)
        if let body {
            outgoing.append(body)
        }
        try send(outgoing, on: descriptor, deadline: deadline)
        return try receive(from: descriptor, deadline: deadline)
    }

    private func open(deadline: UInt64) throws -> Int32 {
        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        let bytes = Array(path.utf8)
        let capacity = MemoryLayout.size(ofValue: address.sun_path)
        guard !bytes.isEmpty, bytes.count < capacity else {
            throw SocketError.pathTooLong
        }
        withUnsafeMutableBytes(of: &address.sun_path) { target in
            for (index, byte) in bytes.enumerated() {
                target[index] = byte
            }
            target[bytes.count] = 0
        }
        address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)

        let descriptor = socket(AF_UNIX, SOCK_STREAM, 0)
        guard descriptor >= 0 else {
            throw SocketError.cannotConnect
        }
        var quiet: Int32 = 1
        setsockopt(descriptor, SOL_SOCKET, SO_NOSIGPIPE, &quiet, socklen_t(MemoryLayout<Int32>.size))
        let flags = fcntl(descriptor, F_GETFL)
        _ = fcntl(descriptor, F_SETFL, flags | O_NONBLOCK)

        let result = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { generic in
                Darwin.connect(descriptor, generic, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        if result == 0 {
            return descriptor
        }
        if errno == EINPROGRESS {
            do {
                try wait(for: Int16(POLLOUT), on: descriptor, deadline: deadline)
                var failure: Int32 = 0
                var size = socklen_t(MemoryLayout<Int32>.size)
                getsockopt(descriptor, SOL_SOCKET, SO_ERROR, &failure, &size)
                if failure == 0 {
                    return descriptor
                }
            } catch {
                Darwin.close(descriptor)
                throw error
            }
        }
        Darwin.close(descriptor)
        throw SocketError.cannotConnect
    }

    private func wait(for events: Int16, on descriptor: Int32, deadline: UInt64) throws {
        while true {
            let now = DispatchTime.now().uptimeNanoseconds
            guard now < deadline else {
                throw SocketError.timedOut
            }
            let milliseconds = Int32(min((deadline - now) / 1_000_000 + 1, 2000))
            var entry = pollfd(fd: descriptor, events: events, revents: 0)
            let ready = poll(&entry, 1, milliseconds)
            if ready > 0 {
                return
            }
            if ready < 0 && errno != EINTR {
                throw SocketError.closedEarly
            }
        }
    }

    private func send(_ data: Data, on descriptor: Int32, deadline: UInt64) throws {
        var sent = 0
        while sent < data.count {
            try wait(for: Int16(POLLOUT), on: descriptor, deadline: deadline)
            let count = data.withUnsafeBytes { buffer -> Int in
                Darwin.write(descriptor, buffer.baseAddress! + sent, data.count - sent)
            }
            if count > 0 {
                sent += count
            } else if count < 0 && (errno == EAGAIN || errno == EINTR) {
                continue
            } else {
                throw SocketError.closedEarly
            }
        }
    }

    private func receive(from descriptor: Int32, deadline: UInt64) throws -> SocketReply {
        var buffer = Data()
        var chunk = [UInt8](repeating: 0, count: 16384)
        var parsed: (status: Int, bodyStart: Int, length: Int?, chunked: Bool)?
        var finished = false
        while !finished {
            if parsed == nil, let found = buffer.range(of: Data("\r\n\r\n".utf8)) {
                parsed = try Self.parseHead(buffer.subdata(in: 0..<found.lowerBound), bodyStart: found.upperBound)
            }
            if buffer.count > Self.replyCap + 16384 {
                throw SocketError.tooLarge
            }
            if let parsed {
                if let length = parsed.length, length > Self.replyCap {
                    throw SocketError.tooLarge
                }
                if parsed.chunked {
                    if buffer.count - parsed.bodyStart >= 5, buffer.suffix(5) == Data("0\r\n\r\n".utf8),
                       Self.decodeChunks(buffer.subdata(in: parsed.bodyStart..<buffer.count)) != nil {
                        break
                    }
                } else if let length = parsed.length, buffer.count - parsed.bodyStart >= length {
                    break
                }
            }
            try wait(for: Int16(POLLIN), on: descriptor, deadline: deadline)
            let count = chunk.withUnsafeMutableBytes { Darwin.read(descriptor, $0.baseAddress, $0.count) }
            if count > 0 {
                buffer.append(chunk, count: count)
            } else if count == 0 {
                finished = true
            } else if errno != EAGAIN && errno != EINTR {
                throw SocketError.closedEarly
            }
        }
        guard let parsed else {
            throw SocketError.closedEarly
        }
        let rest = buffer.subdata(in: parsed.bodyStart..<buffer.count)
        if parsed.chunked {
            guard let decoded = Self.decodeChunks(rest) else {
                throw SocketError.closedEarly
            }
            return SocketReply(status: parsed.status, body: decoded)
        }
        if let length = parsed.length {
            guard rest.count >= length else {
                throw SocketError.closedEarly
            }
            return SocketReply(status: parsed.status, body: rest.prefix(length))
        }
        return SocketReply(status: parsed.status, body: rest)
    }

    static func parseHead(_ head: Data, bodyStart: Int) throws -> (status: Int, bodyStart: Int, length: Int?, chunked: Bool) {
        guard let text = String(data: head, encoding: .utf8) else {
            throw SocketError.badReply
        }
        let lines = text.components(separatedBy: "\r\n")
        let first = lines[0].split(separator: " ", maxSplits: 2, omittingEmptySubsequences: true)
        guard first.count >= 2, first[0].hasPrefix("HTTP/1."), let status = Int(first[1]) else {
            throw SocketError.badReply
        }
        var length: Int?
        var chunked = false
        for line in lines.dropFirst() {
            guard let colon = line.firstIndex(of: ":") else { continue }
            let name = line[..<colon].lowercased()
            let value = line[line.index(after: colon)...].trimmingCharacters(in: .whitespaces)
            if name == "content-length" {
                guard let number = Int(value), number >= 0 else {
                    throw SocketError.badReply
                }
                length = number
            } else if name == "transfer-encoding", value.lowercased().contains("chunked") {
                chunked = true
            }
        }
        return (status, bodyStart, length, chunked)
    }

    static func decodeChunks(_ data: Data) -> Data? {
        var output = Data()
        var index = data.startIndex
        let marker = Data("\r\n".utf8)
        while true {
            guard let lineEnd = data.range(of: marker, in: index..<data.endIndex) else {
                return nil
            }
            let sizeText = String(decoding: data[index..<lineEnd.lowerBound], as: UTF8.self)
                .split(separator: ";").first.map(String.init) ?? ""
            guard let size = Int(sizeText.trimmingCharacters(in: .whitespaces), radix: 16) else {
                return nil
            }
            index = lineEnd.upperBound
            if size == 0 {
                return output
            }
            guard data.endIndex - index >= size + 2 else {
                return nil
            }
            output.append(data[index..<(index + size)])
            index += size + 2
        }
    }
}
