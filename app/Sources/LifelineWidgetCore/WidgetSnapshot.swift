import Foundation
import Security

/// What the desktop widgets show. The app writes it into the shared app group container after
/// each reading; the widget extension only reads and draws it. Every sentence in it is the core's own.
public struct WidgetSnapshot: Codable, Sendable, Equatable {
    public struct Row: Codable, Sendable, Equatable, Identifiable {
        public var id: String
        public var name: String
        public var figure: String
        public var fill: Double
        public var week: Double
        public var tone: String
        public var note: String
        public var best: Bool
        public var active: Bool
        public var stale: Bool

        public init(
            id: String, name: String, figure: String, fill: Double, week: Double, tone: String, note: String, best: Bool, active: Bool, stale: Bool
        ) {
            self.id = id
            self.name = name
            self.figure = figure
            self.fill = fill
            self.week = week
            self.tone = tone
            self.note = note
            self.best = best
            self.active = active
            self.stale = stale
        }
    }

    /// When the app wrote it.
    public var written: Date
    /// The view's label ("This Mac").
    public var label: String
    /// The account in use, by its short name.
    public var name: String
    /// The menu bar's figure for it ("42%") and its line.
    public var figure: String
    public var fill: Double
    public var ahead: Double
    public var tone: String
    public var stale: Bool
    /// The core's widget sentences.
    public var smallLine: String
    public var headline: String
    public var sub: String
    /// The core's sentence for a snapshot the app stopped updating ("Lifeline last updated this at 03:40 on 21 November.").
    public var staleLine: String
    /// The core's line for each number of accounts left out for room: more[k - 1] for k left out.
    public var more: [String]
    public var rows: [Row]

    public init(
        written: Date, label: String, name: String, figure: String, fill: Double, ahead: Double, tone: String, stale: Bool, smallLine: String,
        headline: String, sub: String, staleLine: String = "", more: [String] = [], rows: [Row]
    ) {
        self.staleLine = staleLine
        self.more = more
        self.written = written
        self.label = label
        self.name = name
        self.figure = figure
        self.fill = fill
        self.ahead = ahead
        self.tone = tone
        self.stale = stale
        self.smallLine = smallLine
        self.headline = headline
        self.sub = sub
        self.rows = rows
    }

    /// The same snapshot without its write time and stale line, to tell whether anything a widget draws now has changed.
    public var drawn: WidgetSnapshot {
        var copy = self
        copy.written = Date(timeIntervalSince1970: 0)
        copy.staleLine = ""
        return copy
    }

    /// The app wrote it too long ago: it has stopped or quit, so the widget dims and says when it was last updated.
    public func isStale(at now: Date) -> Bool {
        now.timeIntervalSince(written) > WidgetFeed.staleAfter
    }

    /// The core's line for this many accounts left out, or "" when none are.
    public func moreLine(leftOut: Int) -> String {
        leftOut >= 1 && leftOut <= more.count ? more[leftOut - 1] : ""
    }
}

public enum WidgetFeed {
    public static let kind = "LifelineWidget"
    /// The app group this copy is signed with. scripts/bundle.sh names it after the bundle id, so it follows the bundle
    /// id; it is nil in a copy signed without one (an ad hoc build, a test runner), and then nothing is shared.
    public static let group: String? = {
        guard let task = SecTaskCreateFromSelf(nil),
              let value = SecTaskCopyValueForEntitlement(task, "com.apple.security.application-groups" as CFString, nil) else {
            return nil
        }
        return (value as? [String])?.first
    }()
    public static let fileName = "widget.json"
    /// The widget asks again after this long, in case the app is not running to tell it.
    public static let refresh: TimeInterval = 15 * 60
    /// A running app rewrites the snapshot at least this often, even when nothing drawn has changed.
    public static let heartbeat: TimeInterval = 5 * 60
    /// A snapshot older than this came from an app that has stopped or quit.
    public static let staleAfter: TimeInterval = 20 * 60

    /// When a timeline should draw: now, and the moment the snapshot turns stale if that is still ahead, so a widget
    /// dims by itself even when nothing reloads it.
    public static func entryDates(for snapshot: WidgetSnapshot?, now: Date) -> [Date] {
        guard let snapshot else { return [now] }
        let turns = snapshot.written.addingTimeInterval(staleAfter + 1)
        return turns > now ? [now, turns] : [now]
    }

    public static func encode(_ snapshot: WidgetSnapshot) throws -> Data {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        encoder.outputFormatting = [.sortedKeys]
        return try encoder.encode(snapshot)
    }

    public static func decode(_ data: Data) throws -> WidgetSnapshot {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return try decoder.decode(WidgetSnapshot.self, from: data)
    }

    public static func file(in folder: URL) -> URL {
        folder.appendingPathComponent(fileName, isDirectory: false)
    }

    /// The snapshot in the shared container, read by the extension. Nil until the app has written one.
    public static func read(from folder: URL? = group.flatMap { FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: $0) }) -> WidgetSnapshot? {
        guard let folder, let data = try? Data(contentsOf: file(in: folder)) else { return nil }
        return try? decode(data)
    }
}
