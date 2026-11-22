import AppKit
import Darwin
import Foundation
import SwiftUI
import Testing
import Vision
@testable import Lifeline
@testable import LifelineKit

final class TempDefaults {
    let folder: String
    let defaults: UserDefaults

    init() throws {
        var template = Array("/tmp/lifeline-test-XXXXXX".utf8CString)
        let made = template.withUnsafeMutableBufferPointer { buffer in
            mkdtemp(buffer.baseAddress) != nil
        }
        guard made else { throw CocoaError(.fileWriteUnknown) }
        folder = String(decoding: template.prefix { $0 != 0 }.map { UInt8(bitPattern: $0) }, as: UTF8.self)
        guard let made = UserDefaults(suiteName: folder + "/sizes.plist") else { throw CocoaError(.fileWriteUnknown) }
        defaults = made
    }

    func finish() {
        try? FileManager.default.removeItem(atPath: folder)
    }
}

private let sheetState = #"""
{"version": 1,
 "mac": {"view": "mac", "rows": [{"uuid": "j", "name": "Alex's Account", "is_active": true, "where": "Used by this Mac."},
                                {"uuid": "c", "name": "Work Account", "where": "Not in use."},
                                {"uuid": "p", "name": "Personal Account", "where": "Used by Studio."},
                                {"uuid": "o", "name": "Team Account", "where": "Not in use."}]},
 "accounts": [{"uuid": "j", "name": "Alex's Account", "short": "Alex", "plan": "Max 5x", "signin_ends": "2026-12-16T10:00:00Z", "devbox_signin_ends": "2026-12-18T10:00:00Z"},
              {"uuid": "c", "name": "Work Account", "short": "Work", "plan": "Max 20x", "devbox_needs_signin": false},
              {"uuid": "p", "name": "Personal Account", "short": "Personal", "plan": "Max 20x", "devbox_needs_signin": false},
              {"uuid": "o", "name": "Team Account", "short": "Team", "plan": "Max 5x", "devbox_needs_signin": false}],
 "settings": {"auto_switch": true, "hide_name": true, "alerts": {"will_run_out": true, "limit": true, "back": true, "week": true, "signin_ends": true},
              "channels": {"mac": true}}}
"""#

@MainActor
@Suite(.serialized)
struct SheetPanelTests {
    private let limit: CGFloat = 879

    private func settings(size: SheetSize?) throws -> some View {
        let state = try Fixture.withSettingsText(sheetState)
        return SheetPanel(size: size, limit: limit) { width in
            SettingsView(state: state, local: LocalStatus(notices: .allowed, shortcut: .on, login: .on), problem: nil, width: width)
        }
        .environment(\.menuMotion, Motion(MotionPlan(reduceMotion: true)))
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

    private func texts(_ element: Any, depth: Int = 0) -> [String] {
        guard depth < 40, let element = element as? NSAccessibilityProtocol else { return [] }
        var found: [String] = []
        if let label = element.accessibilityLabel(), !label.isEmpty {
            found.append(label)
        }
        if let value = element.accessibilityValue() as? String, !value.isEmpty {
            found.append(value)
        }
        for child in element.accessibilityChildren() ?? [] {
            found += texts(child, depth: depth + 1)
        }
        return found
    }

    @Test func theSettingsPanelIsCompactAndInsideTheScreen() throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let size = NSHostingView(rootView: try settings(size: nil)).fittingSize
        #expect(size.width == Metrics.panelWidth)
        #expect(size.height <= limit + 0.5)
        let natural = NSHostingView(rootView: SettingsView(state: try Fixture.withSettingsText(sheetState), local: LocalStatus(notices: .allowed, shortcut: .on, login: .on), problem: nil)).fittingSize
        #expect(natural.height > limit)
        let screen = SheetSizing.clamp(SheetSize(width: 3000, height: 3000), minWidth: Double(Metrics.panelWidth), maxWidth: 1512 - 16, maxHeight: Double(limit))
        #expect(screen == SheetSize(width: 1496, height: 879))
        #expect(SheetSizing.clamp(SheetSize(width: 100, height: 50), minWidth: 392, maxWidth: 1496, maxHeight: 879) == SheetSize(width: 392, height: 240))
    }

    @Test func theSettingsPanelScrolls() async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let host = NSHostingView(rootView: try settings(size: nil))
        host.frame = NSRect(origin: .zero, size: host.fittingSize)
        let window = NSWindow(contentRect: NSRect(x: -30000, y: -30000, width: host.frame.width, height: host.frame.height),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = host
        defer { window.contentView = nil }
        for _ in 0..<4 {
            host.layoutSubtreeIfNeeded()
            try await Task.sleep(for: .milliseconds(100))
        }
        guard let scroll = views(NSScrollView.self, in: host).first else {
            Issue.record("no scroll view")
            return
        }
        let document = scroll.documentView?.frame.height ?? 0
        #expect(document > scroll.contentView.bounds.height + 50)
        scroll.contentView.scroll(to: NSPoint(x: 0, y: 200))
        scroll.reflectScrolledClipView(scroll.contentView)
        #expect(abs(scroll.contentView.bounds.origin.y - 200) < 1)
    }

    @Test func aResizedPanelKeepsItsSize() throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let size = NSHostingView(rootView: try settings(size: SheetSize(width: 560, height: 620))).fittingSize
        #expect(abs(size.width - 560) < 0.5)
        #expect(abs(size.height - 620) < 0.5)
        let tall = NSHostingView(rootView: try settings(size: SheetSize(width: 560, height: 2000))).fittingSize
        #expect(tall.height <= limit + 0.5)
    }

    @Test func theSizeIsKeptAndRestored() throws {
        let temp = try TempDefaults()
        defer { temp.finish() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: temp.folder + "/none.sock")), sizes: temp.defaults)
        store.panelLimit = limit
        store.sheetMaxWidth = 1496
        #expect(store.sheetSize(.settings) == SheetSize(width: 560, height: 520))
        store.resizeSheet(.settings, SheetSize(width: 610, height: 5000))
        #expect(store.sheetSize(.settings) == SheetSize(width: 610, height: 879))
        store.commitSheet(.settings)
        store.resizeSheet(.add, SheetSize(width: 450, height: 500))
        let again = StateStore(api: LifelineAPI(client: SocketClient(path: temp.folder + "/none.sock")), sizes: temp.defaults)
        again.panelLimit = 700
        again.sheetMaxWidth = 1496
        #expect(again.sheetSize(.settings) == SheetSize(width: 610, height: 700))
        #expect(again.sheetSize(.add) == nil)
        #expect(SheetSizing.dragged(from: SheetSize(width: 392, height: 500), by: (30, 40), edges: (true, true)) == SheetSize(width: 422, height: 540))
        #expect(SheetSizing.dragged(from: SheetSize(width: 392, height: 500), by: (30, 40), edges: (false, true)) == SheetSize(width: 392, height: 540))
    }

    @Test func escapeClosesTheScreen() {
        #expect(MenuLogic.sheetKeyAction(keyCode: 53, characters: nil, command: false) == .close)
    }

    @Test func aFailedAddShowsItsSentenceAndTryAgain() async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let sentence = "Your browser signed in as Personal Account (sam@example.org), which Lifeline already has. Nothing changed."
        #expect(AddFlow.retry(step: .failed(sentence), run: nil) == .add)
        #expect(AddFlow.retry(step: .failed(sentence), run: AddRun(target: .mac, kind: .adopt, stages: [.macAdopt])) == .adopt)
        #expect(AddFlow.retry(step: .start, run: nil) == nil)
        var started: [AddKind] = []
        let state = try Fixture.withSettingsText(sheetState)
        let view = AddAccountView(state: state, step: .failed(sentence), target: .mac, problem: nil, zone: TimeZone(identifier: "UTC")!,
                                  handlers: AddHandlers(start: { started.append($0) }), width: Metrics.panelWidth)
            .background(Color.white)
        let found = typeset(view)
        #expect(found.contains { $0.contains("Nothing changed") && $0.contains("sam@example.org") }, "\(found)")
        #expect(found.contains("Try again"), "\(found)")
        #expect(found.contains { $0.contains("Lifeline opens Claude's sign-in") }, "\(found)")
    }
}
