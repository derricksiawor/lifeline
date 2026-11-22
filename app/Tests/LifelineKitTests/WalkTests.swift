import AppKit
import Foundation
import SwiftUI
import Testing
@testable import Lifeline
@testable import LifelineKit

private func walkState(_ job: String = "") -> String {
    #"{"version": 1, "mac": {"view": "mac", "head": "Alex's Account lasts until its 5 hours reset today at 17:00.", "rows": [{"uuid": "j", "name": "Alex's Account", "pct": "42%", "is_active": true, "where": "Used by this Mac."}, {"uuid": "o", "name": "Team Account", "pct": "9%", "where": "Not in use."}]}, "accounts": [{"uuid": "j", "name": "Alex's Account", "short": "Alex", "number": 1}, {"uuid": "o", "name": "Team Account", "short": "Team", "number": 2}], "jobs": ["# + job + "]}"
}

private final class Flip: @unchecked Sendable {
    private let lock = NSLock()
    private var value = false
    var on: Bool {
        get { lock.withLock { value } }
        set { lock.withLock { value = newValue } }
    }
}

struct WalkRuleTests {
    @Test func undoShowsForFiveSecondsThenGoes() {
        let start = Date()
        #expect(MenuLogic.undoShows(since: start, until: nil, now: start))
        #expect(MenuLogic.undoShows(since: start, until: nil, now: start.addingTimeInterval(4.9)))
        #expect(!MenuLogic.undoShows(since: start, until: nil, now: start.addingTimeInterval(5.1)))
        #expect(!MenuLogic.undoShows(since: start, until: start.addingTimeInterval(3), now: start.addingTimeInterval(3.2)))
        #expect(MenuLogic.undoWindow == 5)
    }

    @Test func onlyTheClickThatSwitchedIsIgnored() {
        let previous = (time: 100.0, x: 200.0, y: 50.0)
        #expect(MenuLogic.sameClick(time: 100.2, x: 201, y: 51, previous: previous))
        #expect(!MenuLogic.sameClick(time: 100.2, x: 200, y: 120, previous: previous))
        #expect(!MenuLogic.sameClick(time: 101.5, x: 200, y: 50, previous: previous))
        #expect(!MenuLogic.sameClick(time: 100.2, x: 200, y: 50, previous: nil))
    }

    @Test func aDragJustOffTheCardStillCountsAsThePanel() {
        let bounds = CGRect(x: 0, y: 0, width: 560, height: 700)
        let panel = MenuLogic.panelArea(bounds: bounds, top: 6, side: 84, bottom: 112)
        #expect(MenuLogic.inside(CGPoint(x: 84 - 10, y: 300), panel: panel, frames: []))
        #expect(!MenuLogic.inside(CGPoint(x: 20, y: 300), panel: panel, frames: []))
        #expect(Metrics.gripInset >= 12)
    }

    @Test func aHiddenRowShowsAScrollIndicator() {
        #expect(MenuLogic.scrollIndicator(offset: 0, content: 300, viewport: 300) == nil)
        let bar = MenuLogic.scrollIndicator(offset: 0, content: 600, viewport: 300)
        #expect(bar != nil && bar!.top == 0 && abs(bar!.height - 150) < 0.5)
        let lower = MenuLogic.scrollIndicator(offset: -300, content: 600, viewport: 300)
        #expect(lower != nil && abs(lower!.top - 150) < 0.5)
    }

    @Test func noChartUnderThreeDaysAndDayLabelsAreClear() {
        #expect(Detail.chartMinimumDays == 3)
        let names = ["Thursday", "Friday", "Saturday", "Sunday", "Monday", "Tuesday", "Wednesday"]
        let labels = names.map(DetailChart.dayLabel)
        #expect(Set(labels).count == labels.count, "\(labels)")
        #expect(labels.first == "Thu")
    }
}

@MainActor
@Suite(.serialized)
struct WalkPanelTests {
    @Test func theFirstClickOnTheMenuIsNotSwallowed() {
        let host = FirstClickHost(rootView: AnyView(Text("Undo the switch")))
        #expect(host.acceptsFirstMouse(for: nil))
    }

    @Test func settingsOpensAt520PointsWithBack() throws {
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: "/nonexistent/lifeline.sock")), sizes: nil)
        store.panelLimit = 879
        #expect(store.sheetSize(.settings)?.height == 520)
        store.screen = .settings
        store.backFromSettings()
        #expect(store.screen == .menu)
        #expect(MenuLogic.sheetKeyAction(keyCode: 53, characters: nil, command: false, holding: false, back: true) == .back)
    }

    @Test func thePanelsLeftAndTopNeverMove() async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let running = Flip()
        let server = try FakeServer { head, _ in
            if head.hasPrefix("POST /add") {
                running.on = true
                return [okReply(#"{"job": "2"}"#, status: "202 Accepted")]
            }
            let job = running.on ? #"{"id": "2", "kind": "add", "state": "running", "url": "https://claude.ai/oauth/authorize?x=1", "opened": true, "browser": "Safari"}"# : ""
            return [okReply(walkState(job))]
        }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        let panel = MenuPanel(store: store, status: AppStatus(), showsItem: false, unseen: true)
        panel.open()
        defer { panel.close(returningFocus: false) }
        func settled() async throws -> NSRect {
            var frame = NSRect.zero
            for _ in 0..<30 {
                try await Task.sleep(for: .milliseconds(60))
                frame = panel.shownFrame ?? .zero
            }
            return frame
        }
        let menu = try await settled()
        #expect(menu.width > 0)
        store.screen = .settings
        let settings = try await settled()
        store.screen = .add
        let add = try await settled()
        store.startAdd(.add)
        await waitFor { store.addCompact }
        let compact = try await settled()
        store.backFromAdd()
        let back = try await settled()
        for (name, frame) in [("settings", settings), ("add", add), ("compact", compact), ("menu again", back)] {
            #expect(abs(frame.minX - menu.minX) < 1, "\(name) left \(frame.minX) vs \(menu.minX)")
            #expect(abs(frame.maxY - menu.maxY) < 1, "\(name) top \(frame.maxY) vs \(menu.maxY)")
        }
    }

    @Test func aRealDragOnTheGrabberResizesTheSheet() async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        var sizes: [SheetSize] = []
        let sheet = SheetPanel(size: SheetSize(width: 392, height: 300), limit: 879, resize: { sizes.append($0) }) { width in
            Color.clear.frame(width: width, height: 900)
        }
        .environment(\.colorScheme, .dark)
        let host = NSHostingView(rootView: sheet)
        host.frame = NSRect(origin: .zero, size: host.fittingSize)
        let window = NSWindow(contentRect: NSRect(x: -30000, y: -30000, width: host.frame.width, height: host.frame.height), styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = host
        window.orderFrontRegardless()
        defer {
            window.orderOut(nil)
            window.contentView = nil
        }
        for _ in 0..<5 {
            host.layoutSubtreeIfNeeded()
            try await Task.sleep(for: .milliseconds(40))
        }
        let start = NSPoint(x: host.frame.width / 2, y: Metrics.gripInset + 2)
        func event(_ type: NSEvent.EventType, _ point: NSPoint) -> NSEvent? {
            NSEvent.mouseEvent(with: type, location: point, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: window.windowNumber, context: nil, eventNumber: 0, clickCount: 1, pressure: 1)
        }
        for item in [event(.leftMouseDown, start), event(.leftMouseDragged, NSPoint(x: start.x, y: start.y - 120)), event(.leftMouseDragged, NSPoint(x: start.x, y: start.y - 278)), event(.leftMouseUp, NSPoint(x: start.x, y: start.y - 278))] {
            if let item {
                window.sendEvent(item)
            }
        }
        let hit = host.hitTest(host.convert(start, from: nil)).map { String(describing: type(of: $0)) } ?? "nothing"
        let last = try #require(sizes.last, "hit \(hit) at \(start) in \(host.frame)")
        #expect(abs(last.height - (300 + 278)) < 3, "\(sizes)")
    }
}
