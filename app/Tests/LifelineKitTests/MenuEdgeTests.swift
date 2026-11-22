import AppKit
import Foundation
import SwiftUI
import Testing
@testable import Lifeline
@testable import LifelineKit
import LifelineWidgetCore
@testable import LifelineWidgetViews

private final class DrawnBoxThree: @unchecked Sendable {
    private let lock = NSLock()
    private var stored = WidgetDrawn()

    var value: WidgetDrawn {
        get { lock.withLock { stored } }
        set { lock.withLock { stored = newValue } }
    }
}

/// What a widget face drew: how many rows, whether the "more in the menu" line, whether the stale line.
@MainActor
private func drawn(_ face: some View, size: WidgetSize) -> (WidgetDrawn, CGFloat) {
    NSApplication.shared.setActivationPolicy(.prohibited)
    let box = DrawnBoxThree()
    let root = face
        .frame(width: size.size.width, height: size.size.height)
        .onPreferenceChange(WidgetDrawnKey.self) { value in box.value = value }
    let host = NSHostingView(rootView: root)
    host.frame = NSRect(origin: .zero, size: size.size)
    let window = NSWindow(contentRect: NSRect(x: -30000, y: -30000, width: size.size.width, height: size.size.height), styleMask: [.borderless], backing: .buffered, defer: false)
    window.isReleasedWhenClosed = false
    window.contentView = host
    defer { window.contentView = nil }
    for _ in 0..<30 {
        host.layoutSubtreeIfNeeded()
        RunLoop.main.run(until: Date().addingTimeInterval(0.01))
    }
    return (box.value, host.fittingSize.height)
}

/// A snapshot with `count` rows, made from the core's own good moment, its rows repeated.
private func snapshot(rows count: Int) throws -> WidgetSnapshot {
    var base = try #require(WidgetFeed.snapshot(try Fixture.state("moment-good"), now: Date(timeIntervalSince1970: 1_800_000_000)))
    let rows = base.rows
    base.rows = (0..<count).map { index in
        var row = rows[index % rows.count]
        row.id = "row-\(index)"
        return row
    }
    base.more = (1..<max(count, 2)).map { $0 == 1 ? "1 more account is in the menu." : "\($0) more accounts are in the menu." }
    return base
}

@MainActor
@Suite(.serialized)
struct MenuEdgeTests {
    init() {
        Fonts.register(folder: URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Resources/Fonts", isDirectory: true))
    }

    // Later's sentence waits for you when the menu is closed.
    @Test func latersSentenceWaitsUntilTheMenuIsOpened() async throws {
        let body = try Fixture.text("moment-menu")
        let server = try FakeServer { head, _ in
            if head.hasPrefix("POST /notifications/later") {
                return [okReply(#"{"reminder": "2026-12-15T10:00:00Z", "message": "Lifeline reminds you again on 15 December at 10:00."}"#)]
            }
            return [okReply(body)]
        }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        #expect(!store.menuOpen)
        store.later("signin-ends:mac:\(Fixture.alex):2026-12-16")
        await waitFor { store.note != nil }
        await store.refresh()
        store.opened()
        #expect(store.note == "Lifeline reminds you again on 15 December at 10:00.", "the sentence was gone before you opened the menu")
        store.opened()
        #expect(store.note == nil, "once seen, it goes")
    }

    // a snapshot the app stopped updating dims and says how old it is.
    @Test func anOldSnapshotDimsAndSaysWhenItWasLastUpdated() throws {
        let fresh = try snapshot(rows: 4)
        #expect(fresh.staleLine == "Lifeline last updated this at 14:20 on 11 November.")
        #expect(!fresh.isStale(at: fresh.written.addingTimeInterval(WidgetFeed.staleAfter - 1)))
        #expect(fresh.isStale(at: fresh.written.addingTimeInterval(WidgetFeed.staleAfter + 1)))
        for size in WidgetSize.allCases {
            let now = drawn(WidgetFace(snapshot: fresh, size: size, now: fresh.written.addingTimeInterval(60)), size: size).0
            #expect(!now.stale, "\(size) shows the stale line while the snapshot is fresh")
            let old = drawn(WidgetFace(snapshot: fresh, size: size, now: fresh.written.addingTimeInterval(WidgetFeed.staleAfter + 60)), size: size).0
            #expect(old.stale, "\(size) does not say how old its snapshot is")
        }
        let entries = WidgetFeed.entryDates(for: fresh, now: fresh.written.addingTimeInterval(60))
        #expect(entries.count == 2 && entries[1] > fresh.written.addingTimeInterval(WidgetFeed.staleAfter), "the widget turns stale by itself")
    }

    @Test func thePublisherKeepsTheSnapshotFreshWithoutRedrawing() throws {
        let place = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("lifeline-widget-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: place, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: place) }
        var reloads = 0
        let publisher = WidgetPublisher(folder: place) { reloads += 1 }
        let good = try Fixture.state("moment-good")
        let start = Date(timeIntervalSince1970: 1_800_000_000)
        #expect(publisher.publish(good, now: start))
        #expect(!publisher.publish(good, now: start.addingTimeInterval(60)))
        #expect(publisher.publish(good, now: start.addingTimeInterval(WidgetFeed.heartbeat + 1)), "a running app rewrites the snapshot so it never looks stale")
        #expect(reloads == 1, "the same words are not redrawn")
        #expect(WidgetFeed.read(from: place)?.written == start.addingTimeInterval(WidgetFeed.heartbeat + 1))
    }

    // every account that fits shows, and the rest are counted in the core's sentence.
    @Test func everyAccountThatFitsShowsAndTheRestAreCounted() throws {
        for size in [WidgetSize.medium, .large] {
            for count in [1, 4, 5, 6, 9] {
                let full = try snapshot(rows: count)
                let (seen, height) = drawn(WidgetFace(snapshot: full, size: size, now: full.written), size: size)
                #expect(height <= size.size.height + 0.5, "\(size) with \(count) accounts needs \(height) pt")
                #expect(seen.rows >= 1 && seen.rows <= count, "\(size) \(count): \(seen.rows)")
                #expect(seen.more == (seen.rows < count), "\(size) with \(count) accounts drops \(count - seen.rows) without saying so")
                if seen.rows < count {
                    #expect(seen.moreText == full.more[count - seen.rows - 1], "\(size) \(count): \(seen.moreText)")
                }
            }
            let four = drawn(WidgetFace(snapshot: try snapshot(rows: 4), size: size, now: Date(timeIntervalSince1970: 1_800_000_000)), size: size).0
            #expect(four.rows == 4 && !four.more, "\(size): four accounts all fit")
        }
        let five = try snapshot(rows: 5)
        let medium = drawn(WidgetFace(snapshot: five, size: .medium, now: five.written), size: .medium).0
        #expect(medium.rows == 5 && !medium.more, "five accounts fit the medium widget, so none is left out: \(medium.rows)")
    }

    // opening a row with the mouse clears the keyboard highlight.
    @Test func aMouseOpenClearsTheKeyboardHighlight() throws {
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: "/tmp/none.sock")), sizes: nil)
        store.highlight = "a"
        store.showDetail("b", keyboard: false)
        #expect(store.highlight == nil)
        store.highlight = "a"
        store.showDetail("a", keyboard: true)
        #expect(store.highlight == "a", "a keyboard open keeps its own highlight")
    }
}
