import AppKit
import Foundation
import SwiftUI
import Testing
@testable import Lifeline
@testable import LifelineKit

private let capState = #"""
{"version": 1,
 "mac": {"view": "mac", "view_label": "This Mac", "sessions": "3 sessions open", "hero_name": "Alex's Account", "hero_plan": "Max 5x",
         "head": "Alex's Account lasts until it resets today at 17:00.", "sub": "At this pace it ends the window 71% used.",
         "hero_w": "42%", "fore_w": "29%", "hero_tone": "calm", "hero_used": "42% used", "hero_reset": "Resets today at 17:00",
         "second_text": "It has used 18% of its week. Its week resets on Sunday at 09:00.", "second_w": "18%", "second_window": "week",
         "action_kind": "none", "hint": "",
         "rows": [
           {"uuid": "j", "name": "Alex's Account", "pct": "42%", "five_w": "42%", "fore_w": "29%", "tone": "calm", "is_active": true,
            "where": "Used by this Mac. Resets today at 17:00.", "second_text": "It has used 18% of its week. Its week resets on Sunday at 09:00.",
            "second_w": "18%", "second_window": "week"},
           {"uuid": "c", "name": "Work Account", "pct": "94%", "five_w": "94%", "tone": "low", "where": "Not in use. Resets on Sunday at 09:00.",
            "second_text": "It has used 70% of its 5 hours. Its 5 hours run out today at 16:20.", "second_w": "70%", "second_window": "five"},
           {"uuid": "p", "name": "Personal Account", "pct": "76%", "five_w": "76%", "tone": "calm", "where": "Not in use. Resets on 26 November at 02:00."},
           {"uuid": "o", "name": "Team Account", "pct": "9%", "five_w": "9%", "tone": "calm", "is_best": true, "where": "Not in use. Resets on Sunday at 09:00.",
            "second_text": "It has used 0% of its 5 hours.", "second_w": "0%", "second_window": "five"}]},
 "accounts": [
   {"uuid": "j", "name": "Alex's Account", "short": "Alex", "binding": "five", "five": 42, "week": 18,
    "five_reset": "2026-11-18T17:00:00Z", "week_reset": "2026-11-22T09:00:00Z", "forecast": {"at_reset_pct": 71}, "in_use": ["mac"],
    "signin_ends": "2026-12-16T10:00:00Z", "devbox_needs_signin": false, "devbox_signin_ends": "2026-12-18T10:00:00Z",
    "projects": [{"cwd": "/home/Sam/projects/project-a"}, {"cwd": "/home/Sam/projects/project-b"}], "detail": {"usage": "It has used 42% of its 5 hours and 18% of its week. At this pace it reaches 71% before its 5 hours reset.", "models": "", "account": "It is signed in on this Mac until 16 December. Studio has no sign-in for it yet.", "projects": "Open on this Mac: project-a and project-b."}},
   {"uuid": "c", "name": "Work Account", "short": "Work", "binding": "week", "five": 70, "week": 94, "five_reset": "2026-11-18T17:30:00Z",
    "week_reset": "2026-11-22T09:00:00Z", "forecast": {"at_reset_pct": 131, "runs_out_at": "2026-11-20T21:00:00Z"},
    "other_forecast": {"at_reset_pct": 118, "runs_out_at": "2026-11-18T16:20:00Z"}, "signin_ends": "2026-12-16T10:00:00Z", "detail": {"usage": "It has used 94% of its week and 70% of its 5 hours. At this pace its week runs out on Friday at 21:00.", "models": "", "account": "It is signed in on this Mac until 16 December. Studio has no sign-in for it yet.", "projects": "Open on this Mac: project-a and project-b."}},
   {"uuid": "p", "name": "Personal Account", "short": "Personal", "binding": "week", "week": 76, "week_reset": "2026-11-26T02:00:00Z",
    "signin_ends": "2026-12-12T08:00:00Z", "detail": {"usage": "It has used 76% of its week.", "models": "", "account": "It is signed in on this Mac until 16 December. Studio has no sign-in for it yet.", "projects": "Open on this Mac: project-a and project-b."}},
   {"uuid": "o", "name": "Team Account", "short": "Team", "binding": "week", "five": 0, "week": 9, "week_reset": "2026-11-22T09:00:00Z",
    "forecast": {"at_reset_pct": 12}, "signin_ends": "2026-12-16T10:00:00Z", "detail": {"usage": "It has used 9% of its week and has all of its 5 hours. At this pace it reaches 12% before its week resets.", "models": "", "account": "It is signed in on this Mac until 16 December. Studio has no sign-in for it yet.", "projects": "Open on this Mac: project-a and project-b."}}],
 "devbox": {"name": "Studio", "reachable": false}}
"""#

@MainActor
@Suite(.serialized)
struct MenuCapTests {
    private let now = Timestamps.parse("2026-11-18T15:05:00Z")!
    private let screenHeight = 982.0
    private let menuTop = 939.0

    private var limit: CGFloat {
        CGFloat(MenuLogic.panelLimit(top: menuTop + Metrics.shadowTop, visibleBottom: 0, shadowTop: Metrics.shadowTop))
    }

    private func histories() -> [String: HistoryReply] {
        let dates = ["2026-11-12", "2026-11-13", "2026-11-14", "2026-11-15", "2026-11-16", "2026-11-17", "2026-11-18"]
        let daily = zip(dates, [64.0, 100, 47, 100, 72, 55, 42]).map { DailyPeak(date: $0, peakFive: $1, hitLimit: $1 >= 100) }
        return [
            "j": HistoryReply(uuid: "j", days: 7, readings: [], daily: daily, peaksOf: "five", weekRanOut: ["2026-11-14"]),
            "c": HistoryReply(uuid: "c", days: 7, readings: [], daily: daily, peaksOf: "five"),
            "p": HistoryReply(uuid: "p", days: 7, readings: [], daily: [DailyPeak(date: "2026-11-18", peakFive: 76)], peaksOf: "week"),
            "o": HistoryReply(uuid: "o", days: 7, readings: [], daily: daily, peaksOf: "five")
        ]
    }

    private func menu(open: String?, naming: Bool = false, cancel: Int = 0, cap: CGFloat?, longSub: Bool = false, sub: String? = nil) throws -> some View {
        let added = sub ?? (longSub ? "At this pace it ends the window 71% used. Team Account has the most room, and Work Account runs out first today." : nil)
        let text = added.map { capState.replacingOccurrences(of: "At this pace it ends the window 71% used.", with: $0) } ?? capState
        let state = try ServiceState.decode(Data(text.utf8))
        let replies = histories()
        return MenuView(
            state: state,
            side: .mac,
            note: nil,
            openDetail: open,
            expansion: ExpansionInputs(
                history: { uuid in replies[uuid].map { HistoryLoad.ready($0) } ?? .waiting },
                now: now,
                zone: TimeZone(identifier: "UTC")!,
                naming: naming,
                cancelRename: cancel
            ),
            maxHeight: cap
        )
        .environment(\.menuMotion, Motion(MotionPlan(reduceMotion: true)))
    }

    private func hosted<V: View>(_ view: V) -> (NSWindow, NSHostingView<V>) {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let host = NSHostingView(rootView: view)
        host.frame = NSRect(origin: .zero, size: host.fittingSize)
        let window = NSWindow(contentRect: NSRect(x: -30000, y: -30000, width: host.frame.width, height: host.frame.height),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = host
        settle(host, window)
        return (window, host)
    }

    private func settle<V: View>(_ host: NSHostingView<V>, _ window: NSWindow) {
        for _ in 0..<4 {
            host.layoutSubtreeIfNeeded()
            RunLoop.main.run(until: Date().addingTimeInterval(0.15))
            let size = host.fittingSize
            if size != host.frame.size {
                window.setContentSize(size)
                host.frame = NSRect(origin: .zero, size: size)
            }
        }
    }

    private func settleLetting<V: View>(_ host: NSHostingView<V>, _ window: NSWindow) async throws {
        for _ in 0..<6 {
            host.layoutSubtreeIfNeeded()
            try await Task.sleep(for: .milliseconds(120))
            let size = host.fittingSize
            if size != host.frame.size {
                window.setContentSize(size)
                host.frame = NSRect(origin: .zero, size: size)
            }
        }
        host.layoutSubtreeIfNeeded()
    }

    private func views<T: NSView>(_ type: T.Type, in root: NSView) -> [T] {
        var found: [T] = []
        var queue: [NSView] = [root]
        while let view = queue.popLast() {
            if let match = view as? T {
                found.append(match)
            }
            queue.append(contentsOf: view.subviews)
        }
        return found
    }

    @Test func unseenCardsStayInsideTheCapAndTheListKeepsItsRoom() throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let state = try ServiceState.decode(Data(capState.utf8))
        let sentence = "Your browser signed in as Personal Account (sam@example.org), which Lifeline already has. Nothing changed."
        let results = (1...4).map { JobSummary(id: "f\($0)", kind: "add", state: "failed", result: sentence) }
        let waiting = (1...2).map { JobSummary(id: "w\($0)", kind: "resign", uuid: "o", state: "running", opened: true, browser: "Google Chrome") }
        for cap: CGFloat in [879, 931] {
            let view = MenuView(state: state, side: .mac, note: nil, maxHeight: cap, results: results, waiting: waiting)
                .environment(\.menuMotion, Motion(MotionPlan(reduceMotion: true)))
            let height = NSHostingView(rootView: view).fittingSize.height
            #expect(height <= cap, "\(cap) \(height)")
        }
    }

    @Test func everyStateStaysInsideAScreen982PointsHigh() throws {
        #expect(limit == CGFloat(menuTop - 8))
        let dock = 52.0
        let withDock = CGFloat(MenuLogic.panelLimit(top: menuTop + Metrics.shadowTop, visibleBottom: dock, shadowTop: Metrics.shadowTop))
        #expect(withDock == 879)
        let states: [(String?, Bool)] = [(nil, false), ("p", false), ("c", false), ("j", false), ("o", false), ("j", true)]
        for (cap, floor) in [(limit, 0.0), (withDock, dock)] {
            for (open, naming) in states {
                let capped = NSHostingView(rootView: try menu(open: open, naming: naming, cap: cap)).fittingSize.height
                #expect(capped <= cap + 0.5, "cap \(cap) \(open ?? "closed") naming \(naming): \(capped)")
                let bottom = menuTop - Double(capped)
                #expect(bottom >= floor + 8 - 0.5)
                let x = MenuLogic.menuOrigin(left: 900, top: menuTop, height: Double(capped), shadow: 56).x
                #expect(x == 844)
            }
        }
        let renaming = NSHostingView(rootView: try menu(open: "j", naming: true, cap: nil)).fittingSize.height
        #expect(renaming > limit)
        let collapsed = NSHostingView(rootView: try menu(open: nil, cap: limit)).fittingSize.height
        let natural = NSHostingView(rootView: try menu(open: nil, cap: nil)).fittingSize.height
        #expect(abs(collapsed - natural) < 0.5)
    }

    @Test func theOpenRowIsScrolledIntoView() async throws {
        // An open row is a few short lines, so the full-height menu no longer needs to scroll; a shorter screen does.
        let short: CGFloat = 600
        let (window, host) = hosted(try menu(open: "o", cap: short))
        defer { window.contentView = nil }
        try await settleLetting(host, window)
        #expect(host.frame.height <= short + 0.5)
        let scrolled = views(NSScrollView.self, in: host).map { $0.contentView.bounds.origin.y }
        #expect(scrolled.contains { $0 > 20 }, "\(scrolled)")
        let list = views(NSScrollView.self, in: host).filter { $0.contentView.bounds.origin.y > 20 }
        #expect(!list.isEmpty && list.allSatisfy { !$0.hasVerticalScroller || $0.verticalScroller?.isHidden == true || ($0.verticalScroller?.alphaValue ?? 0) == 0 },
                "the system scroll bar shows beside Lifeline's own, which fades after 1.2 s")
        let (closedWindow, closedHost) = hosted(try menu(open: nil, cap: 640))
        defer { closedWindow.contentView = nil }
        #expect(views(NSScrollView.self, in: closedHost).allSatisfy { $0.contentView.bounds.origin.y < 1 })
    }

    @Test func aHeightChangeKeepsTheUsersOwnScroll() async throws {
        let (window, host) = hosted(try menu(open: "j", cap: 879))
        defer { window.contentView = nil }
        try await settleLetting(host, window)
        guard let scroll = views(NSScrollView.self, in: host).first else {
            Issue.record("no scroll view")
            return
        }
        scroll.contentView.scroll(to: NSPoint(x: 0, y: 150))
        scroll.reflectScrolledClipView(scroll.contentView)
        try await settleLetting(host, window)
        let before = scroll.contentView.bounds.origin.y
        #expect(before > 100)
        host.rootView = try menu(open: "j", cap: 879, longSub: true)
        try await settleLetting(host, window)
        let after = (views(NSScrollView.self, in: host).first ?? scroll).contentView.bounds.origin.y
        #expect(abs(after - before) < 1.5, "before \(before) after \(after)")
    }

    @Test func anOpenRowPushedBelowTheListIsScrolledBackIntoView() async throws {
        let (window, host) = hosted(try menu(open: "o", cap: 600))
        defer { window.contentView = nil }
        try await settleLetting(host, window)
        guard let scroll = views(NSScrollView.self, in: host).first else {
            Issue.record("no scroll view")
            return
        }
        let rowTop = scroll.contentView.bounds.origin.y
        let viewport = scroll.contentView.bounds.height
        let deepest = (scroll.documentView?.frame.height ?? 0) - viewport
        #expect(rowTop > 20, "opening the row scrolled it to the top")
        #expect(rowTop < deepest - 1, "the row's top is its real top, not the end of the list")
        let start = max(0, rowTop - viewport + 40)
        scroll.contentView.scroll(to: NSPoint(x: 0, y: start))
        scroll.reflectScrolledClipView(scroll.contentView)
        try await settleLetting(host, window)
        #expect(abs(scroll.contentView.bounds.origin.y - start) < 1.5)
        #expect(rowTop - start > 0 && rowTop - start < viewport, "the row's top starts in view")
        let tall = "At this pace it ends the window 71% used. " + String(repeating: "Lifeline reads this account every few minutes and keeps its forecast current. ", count: 4)
        host.rootView = try menu(open: "o", cap: 600, sub: tall)
        try await settleLetting(host, window)
        let after = (views(NSScrollView.self, in: host).first ?? scroll).contentView.bounds
        #expect(after.height < viewport - 40, "the taller hero shrank the list: \(viewport) to \(after.height)")
        let topInView = rowTop - after.origin.y
        #expect(topInView >= -1 && topInView <= after.height, "the row's top is back in view: \(topInView) of \(after.height)")
    }

    @Test func escapeInTheNameFieldsCancelsTheRename() throws {
        let (window, host) = hosted(try menu(open: "j", naming: true, cancel: 0, cap: limit))
        defer { window.contentView = nil }
        #expect(views(NSTextField.self, in: host).filter { $0.isEditable }.count == 2)
        host.rootView = try menu(open: "j", naming: true, cancel: 1, cap: limit)
        settle(host, window)
        #expect(views(NSTextField.self, in: host).filter { $0.isEditable }.isEmpty)
        #expect(MenuLogic.fieldKey(53) == .cancel)
        #expect(MenuLogic.fieldKey(36) == .other)
    }
}

@MainActor
@Suite
struct OpenRowClickTests {
    @Test func aClickOnAnOpenRowClosesItWithoutASwitch() throws {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let other = try decoder.decode(MenuRow.self, from: Data(#"{"uuid": "o", "is_active": false}"#.utf8))
        #expect(MenuLogic.rowClick(other, side: .mac, secondary: false, open: true) == .detail("o"))
        #expect(MenuLogic.rowClick(other, side: .mac, secondary: false, open: false) == .run(.switchTo("o")))
        #expect(MenuLogic.rowClick(other, side: .mac, secondary: true, open: true) == .detail("o"))
        #expect(MenuLogic.expanded(after: "o", current: "o") == nil)
    }

    @Test func aRowIsOutOfViewOnlyWhenItCannotBeSeen() {
        #expect(!MenuLogic.rowOutOfView(top: -200, bottom: 100, viewport: 400))
        #expect(!MenuLogic.rowOutOfView(top: 50, bottom: 300, viewport: 400))
        #expect(MenuLogic.rowOutOfView(top: 450, bottom: 700, viewport: 400))
        #expect(MenuLogic.rowOutOfView(top: -300, bottom: -10, viewport: 400))
    }

    @Test func theListFadesOnlyWhereContentIsHidden() {
        func fades(_ offset: Double, _ content: Double) -> [Bool] {
            let found = MenuLogic.listFades(offset: offset, content: content, limit: 500)
            return [found.top, found.bottom]
        }
        #expect(fades(0, 400) == [false, false])
        #expect(fades(0, 800) == [false, true])
        #expect(fades(-100, 800) == [true, true])
        #expect(fades(-300, 800) == [true, false])
    }

    @Test func theCatcherTakesThePressAndTheRelease() {
        #expect(SecondaryClick.catches(.rightMouseDown, flags: []))
        #expect(SecondaryClick.catches(.rightMouseUp, flags: []))
        #expect(SecondaryClick.catches(.leftMouseDown, flags: .control))
        #expect(SecondaryClick.catches(.leftMouseUp, flags: .control))
        #expect(!SecondaryClick.catches(.leftMouseUp, flags: []))
    }
}
