import Foundation

public enum ServiceDecision: Sendable, Equatable {
    case keep
    case replace
}

public enum ServiceIdentity {
    public static func decide(expected: String?, reported: String?, ownChild: Bool) -> ServiceDecision {
        guard let expected, !expected.isEmpty, !ownChild else { return .keep }
        return reported == expected ? .keep : .replace
    }

    public static func stopsOnQuit(expected: String?, reported: String?, ownChild: Bool) -> Bool {
        if ownChild {
            return true
        }
        guard let expected, !expected.isEmpty else { return false }
        return reported == expected
    }

    public static func looksLikeService(_ arguments: [String]) -> Bool {
        guard let index = arguments.firstIndex(where: { $0.hasSuffix("/bin/lifeline") }), index + 1 < arguments.count else {
            return false
        }
        return arguments[index + 1] == "serve"
    }

    public static func arguments(fromProcArgs data: Data) -> [String] {
        guard data.count > 4 else { return [] }
        let count = data.prefix(4).withUnsafeBytes { Int(Int32(littleEndian: $0.loadUnaligned(as: Int32.self))) }
        guard count > 0 else { return [] }
        var index = 4
        while index < data.count, data[index] != 0 {
            index += 1
        }
        while index < data.count, data[index] == 0 {
            index += 1
        }
        var found: [String] = []
        var start = index
        while index < data.count, found.count < count {
            if data[index] == 0 {
                found.append(String(decoding: data[start..<index], as: UTF8.self))
                start = index + 1
            }
            index += 1
        }
        return found
    }
}
