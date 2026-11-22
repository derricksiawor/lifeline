import AppKit
import CoreGraphics
import Foundation
import LifelineKit

final class PresenceReporter: @unchecked Sendable {
    private let api: LifelineAPI
    private let queue = DispatchQueue(label: "presence")
    private var timer: DispatchSourceTimer?

    init(api: LifelineAPI) {
        self.api = api
    }

    func start() {
        let workspace = NSWorkspace.shared.notificationCenter
        workspace.addObserver(forName: NSWorkspace.willSleepNotification, object: nil, queue: nil) { [weak self] _ in
            self?.post(locked: nil, asleep: true)
        }
        workspace.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: nil) { [weak self] _ in
            self?.post(locked: nil, asleep: false)
        }
        let distributed = DistributedNotificationCenter.default()
        distributed.addObserver(forName: Notification.Name("com.apple.screenIsLocked"), object: nil, queue: nil) { [weak self] _ in
            self?.post(locked: true, asleep: false)
        }
        distributed.addObserver(forName: Notification.Name("com.apple.screenIsUnlocked"), object: nil, queue: nil) { [weak self] _ in
            self?.post(locked: false, asleep: false)
        }
        let source = DispatchSource.makeTimerSource(queue: queue)
        source.schedule(deadline: .now() + 60, repeating: 60)
        source.setEventHandler { [weak self] in
            self?.send(locked: nil, asleep: false)
        }
        source.resume()
        timer = source
        post(locked: nil, asleep: false)
    }

    private func post(locked: Bool?, asleep: Bool) {
        queue.async { [self] in
            send(locked: locked, asleep: asleep)
        }
    }

    private func send(locked: Bool?, asleep: Bool) {
        let presence = Presence(idleSeconds: Self.idleSeconds(), locked: locked ?? Self.screenLocked(), asleep: asleep)
        try? api.postPresence(presence)
    }

    static func idleSeconds() -> Double {
        let kinds: [CGEventType] = [
            .keyDown, .flagsChanged, .mouseMoved, .leftMouseDown, .rightMouseDown,
            .otherMouseDown, .leftMouseDragged, .rightMouseDragged, .scrollWheel
        ]
        let seconds = kinds.map { CGEventSource.secondsSinceLastEventType(.combinedSessionState, eventType: $0) }
        let smallest = seconds.min() ?? 0
        return smallest.isFinite && smallest >= 0 ? smallest : 0
    }

    static func screenLocked() -> Bool {
        guard let session = CGSessionCopyCurrentDictionary() as? [String: Any] else {
            return false
        }
        return (session["CGSSessionScreenIsLocked"] as? Bool) ?? false
    }
}
