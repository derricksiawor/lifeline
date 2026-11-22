import Foundation

public enum ShortcutState: Sendable, Equatable {
    case waiting
    case on
    case taken
    case refused
}

public enum Shortcut {
    public static let keys = ["Control", "Option", "C"]
    public static let keyCode: UInt32 = 8
    public static let takenStatus: Int32 = -9878

    public static func state(forStatus status: Int32) -> ShortcutState {
        switch status {
        case 0:
            return .on
        case takenStatus:
            return .taken
        default:
            return .refused
        }
    }
}
