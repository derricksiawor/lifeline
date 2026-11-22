import Darwin
import Foundation

final class FakeServer: @unchecked Sendable {
    typealias Handler = @Sendable (String, Data) -> [Data]

    let folder: String
    let path: String
    private let listener: Int32
    private let handler: Handler
    private let lock = NSLock()
    private var seen: [(head: String, body: Data)] = []
    private var running = true
    private var gap: useconds_t
    private let concurrent: Bool

    init(gap: useconds_t = 0, concurrent: Bool = false, handler: @escaping Handler) throws {
        signal(SIGPIPE, SIG_IGN)
        self.concurrent = concurrent
        folder = try Self.makeFolder()
        path = folder + "/s.sock"
        self.handler = handler
        self.gap = gap
        listener = socket(AF_UNIX, SOCK_STREAM, 0)
        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        let bytes = Array(path.utf8)
        withUnsafeMutableBytes(of: &address.sun_path) { target in
            for (index, byte) in bytes.enumerated() { target[index] = byte }
            target[bytes.count] = 0
        }
        address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
        let bound = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(listener, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        precondition(bound == 0 && Darwin.listen(listener, 8) == 0)
        let flags = fcntl(listener, F_GETFL)
        _ = fcntl(listener, F_SETFL, flags | O_NONBLOCK)
        let thread = Thread { [self] in serve() }
        thread.start()
    }

    static func makeFolder() throws -> String {
        var template = Array("/tmp/lifeline-test-XXXXXX".utf8CString)
        let made = template.withUnsafeMutableBufferPointer { buffer in
            mkdtemp(buffer.baseAddress) != nil
        }
        guard made else {
            throw CocoaError(.fileWriteUnknown)
        }
        return String(decoding: template.prefix { $0 != 0 }.map { UInt8(bitPattern: $0) }, as: UTF8.self)
    }

    var requests: [(head: String, body: Data)] {
        lock.lock()
        defer { lock.unlock() }
        return seen
    }

    func close() {
        lock.lock()
        running = false
        lock.unlock()
        unlink(path)
        try? FileManager.default.removeItem(atPath: folder)
    }

    private var live: Bool {
        lock.lock()
        defer { lock.unlock() }
        return running
    }

    private func serve() {
        defer { Darwin.close(listener) }
        while live {
            var entry = pollfd(fd: listener, events: Int16(POLLIN), revents: 0)
            guard poll(&entry, 1, 20) > 0 else { continue }
            let connection = accept(listener, nil, nil)
            if connection < 0 { continue }
            let flags = fcntl(connection, F_GETFL)
            _ = fcntl(connection, F_SETFL, flags & ~O_NONBLOCK)
            var quiet: Int32 = 1
            setsockopt(connection, SOL_SOCKET, SO_NOSIGPIPE, &quiet, socklen_t(MemoryLayout<Int32>.size))
            if !live {
                Darwin.close(connection)
                return
            }
            if concurrent {
                Thread { [self] in
                    handle(connection)
                    Darwin.close(connection)
                }.start()
            } else {
                handle(connection)
                Darwin.close(connection)
            }
        }
    }

    private func handle(_ connection: Int32) {
        var buffer = Data()
        var chunk = [UInt8](repeating: 0, count: 4096)
        let marker = Data("\r\n\r\n".utf8)
        var headEnd: Range<Data.Index>?
        while headEnd == nil {
            let count = Darwin.read(connection, &chunk, chunk.count)
            if count <= 0 { return }
            buffer.append(chunk, count: count)
            headEnd = buffer.range(of: marker)
        }
        let head = String(decoding: buffer[0..<headEnd!.lowerBound], as: UTF8.self)
        var length = 0
        for line in head.components(separatedBy: "\r\n") where line.lowercased().hasPrefix("content-length:") {
            length = Int(line.dropFirst(15).trimmingCharacters(in: .whitespaces)) ?? 0
        }
        var body = Data(buffer[headEnd!.upperBound...])
        while body.count < length {
            let count = Darwin.read(connection, &chunk, chunk.count)
            if count <= 0 { break }
            body.append(chunk, count: count)
        }
        lock.lock()
        seen.append((head, body))
        lock.unlock()
        for piece in handler(head, body) {
            piece.withUnsafeBytes { _ = Darwin.write(connection, $0.baseAddress, piece.count) }
            if gap > 0 { usleep(gap) }
        }
    }
}

func okReply(_ body: String, status: String = "200 OK") -> Data {
    Data("HTTP/1.1 \(status)\r\nContent-Type: application/json\r\nContent-Length: \(body.utf8.count)\r\nConnection: close\r\n\r\n\(body)".utf8)
}
