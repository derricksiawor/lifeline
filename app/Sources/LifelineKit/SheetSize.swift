import Foundation

public enum SheetKind: String, Sendable, CaseIterable {
    case settings
    case add
}

public struct SheetSize: Codable, Sendable, Equatable {
    public var width: Double
    public var height: Double

    public init(width: Double, height: Double) {
        self.width = width
        self.height = height
    }
}

public enum SheetSizing {
    public static let minimumHeight = 240.0
    public static let settingsDefault = SheetSize(width: 560, height: 520)

    public static func key(_ kind: SheetKind) -> String {
        "sheetSize." + kind.rawValue
    }

    public static func clamp(_ size: SheetSize, minWidth: Double, maxWidth: Double, maxHeight: Double) -> SheetSize {
        let widest = max(maxWidth, minWidth)
        let tallest = max(maxHeight, minimumHeight)
        return SheetSize(width: min(max(size.width, minWidth), widest), height: min(max(size.height, minimumHeight), tallest))
    }

    public static func load(_ kind: SheetKind, from defaults: UserDefaults) -> SheetSize? {
        guard let values = defaults.array(forKey: key(kind)) as? [Double], values.count == 2,
              values.allSatisfy({ $0.isFinite && $0 > 0 }) else {
            return nil
        }
        return SheetSize(width: values[0], height: values[1])
    }

    public static func save(_ size: SheetSize, _ kind: SheetKind, to defaults: UserDefaults) {
        defaults.set([size.width, size.height], forKey: key(kind))
    }

    public static func dragged(from start: SheetSize, by delta: (x: Double, y: Double), edges: (right: Bool, bottom: Bool)) -> SheetSize {
        SheetSize(width: start.width + (edges.right ? delta.x : 0), height: start.height + (edges.bottom ? delta.y : 0))
    }
}
