import Foundation
import LifelineKit
import LifelineWidgetCore
import WidgetKit

/// Writes the widgets' snapshot into the shared app group container after each reading, and asks WidgetKit to redraw
/// only when something the widgets draw has changed.
@MainActor
final class WidgetPublisher {
    private let folder: URL
    private let reload: () -> Void
    private var last: WidgetSnapshot?
    private var lastWritten: Date?

    init(folder: URL, reload: @escaping () -> Void) {
        self.folder = folder
        self.reload = reload
    }

    /// The publisher for the running app, or nil when this copy is not signed into the widgets' app group (a test build
    /// without the widget). Touching a group container without the entitlement can make macOS ask for access, so the
    /// app never does.
    static func live() -> WidgetPublisher? {
        guard let group = WidgetFeed.group, let folder = FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: group) else {
            return nil
        }
        return WidgetPublisher(folder: folder) {
            WidgetCenter.shared.reloadTimelines(ofKind: WidgetFeed.kind)
        }
    }

    @discardableResult
    func publish(_ state: ServiceState?, now: Date = Date()) -> Bool {
        guard let state, let snapshot = WidgetFeed.snapshot(state, now: now) else { return false }
        let redraw = last?.drawn != snapshot.drawn
        // The same words are written again only as a heartbeat, so a running app's snapshot never looks stale.
        if !redraw, let lastWritten, now.timeIntervalSince(lastWritten) < WidgetFeed.heartbeat {
            return false
        }
        guard let data = try? WidgetFeed.encode(snapshot) else { return false }
        do {
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            try data.write(to: WidgetFeed.file(in: folder), options: .atomic)
        } catch {
            return false
        }
        last = snapshot
        lastWritten = now
        if redraw {
            reload()
        }
        return true
    }
}
