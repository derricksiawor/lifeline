import AppKit
import Foundation
import SwiftUI
import Testing
@testable import Lifeline
@testable import LifelineKit

struct ScreenMotionRuleTests {
    @Test func everyScreenMoveUsesTheSharedSpring() {
        let plan = MotionPlan(reduceMotion: false)
        #expect(plan.screens.response == 0.32)
        #expect(plan.screens.damping == 1)
        #expect(plan.screens.slides)
        #expect(plan.screens.fade == nil)
        #expect(Motion(plan).screens == Animation.spring(response: 0.32, dampingFraction: 1))
        #expect(plan.still.screens == plan.screens)
    }

    @Test func reduceMotionCrossFadesWithNoSlideAndAnInstantHeight() {
        let plan = MotionPlan(reduceMotion: true)
        #expect(plan.screens.response == nil)
        #expect(plan.screens.fade == 0.15)
        #expect(!plan.screens.slides)
        #expect(!plan.screens.heightEases)
        #expect(Motion(plan).screens == Animation.easeInOut(duration: 0.15))
        #expect(ScreenMotion.offsets(.forward, width: 360, slides: false) == ScreenOffsets(incoming: 0, outgoing: 0))
    }

    @Test func backReversesTheDirection() {
        let forward = ScreenMotion.offsets(.forward, width: 360, slides: true)
        let back = ScreenMotion.offsets(.back, width: 360, slides: true)
        #expect(forward.incoming > 0 && forward.outgoing < 0)
        #expect(back.incoming < 0 && back.outgoing > 0)
        #expect(abs(forward.outgoing) < forward.incoming)
        #expect(back.incoming == forward.outgoing && back.outgoing == forward.incoming)
        #expect(ScreenMotion.move(to: "menu") == .back)
        #expect(ScreenMotion.move(to: "add-start") == .forward)
        #expect(ScreenMotion.move(to: "settings") == .forward)
    }

    @Test func contentIsNeverFullyGoneMidway() {
        for step in 0...20 {
            let progress = Double(step) / 20
            let shown = ScreenMotion.incomingOpacity(progress) + ScreenMotion.outgoingOpacity(progress)
            #expect(shown >= 0.5, "\(progress) \(shown)")
        }
        #expect(ScreenMotion.incomingOpacity(0) == 0 && ScreenMotion.incomingOpacity(1) == 1)
        #expect(ScreenMotion.outgoingOpacity(0) == 1 && ScreenMotion.outgoingOpacity(1) == 0)
    }
}

@MainActor
@Suite(.serialized)
struct ScreenMotionViewTests {
    private func host(_ view: some View) -> (NSWindow, NSHostingView<AnyView>) {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let host = NSHostingView(rootView: AnyView(view.environment(\.colorScheme, .dark)))
        host.frame = NSRect(origin: .zero, size: host.fittingSize)
        let window = NSWindow(contentRect: NSRect(x: -30000, y: -30000, width: host.frame.width, height: host.frame.height), styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = host
        host.layoutSubtreeIfNeeded()
        return (window, host)
    }

    @Test func noFrameOfMenuToAddIsEmptyOrFlat() throws {
        let state = try ServiceState.decode(Data(#"{"version": 1, "mac": {"view": "mac", "view_label": "This Mac", "hero_name": "Alex's Account", "head": "Alex's Account lasts until its 5 hours reset today at 17:00.", "rows": [{"uuid": "j", "name": "Alex's Account", "pct": "42%", "is_active": true, "where": "Used by this Mac."}]}, "accounts": [{"uuid": "j", "name": "Alex's Account", "short": "Alex", "number": 1}]}"#.utf8))
        let menu = AnyView(MenuView(state: state, side: .mac, note: nil, maxHeight: 700).environment(\.sharedSurface, true))
        let add = AnyView(SheetPanel(size: nil, limit: 700) { width in AddAccountView(state: state, step: .start, problem: nil, zone: .current, width: width) }.environment(\.sharedSurface, true))
        let menuSize = host(menu).1.fittingSize
        let addSize = host(add).1.fittingSize
        #expect(menuSize.height > 100 && addSize.height > 100)
        for move in [ScreenMove.forward, .back] {
            for progress in [0.0, 0.25, 0.5, 0.75, 1.0] {
                let stage = ScreenStage(
                    outgoing: move == .forward ? menu : add, incoming: move == .forward ? add : menu,
                    from: move == .forward ? menuSize : addSize, to: move == .forward ? addSize : menuSize,
                    move: move, progress: progress, plan: MotionPlan(reduceMotion: false).screens
                )
                let (window, view) = host(stage)
                defer { window.contentView = nil }
                let size = view.fittingSize
                #expect(size.height >= min(menuSize.height, addSize.height) - 1, "\(move) \(progress) \(size)")
                guard let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else {
                    Issue.record("no image at \(progress)")
                    continue
                }
                view.cacheDisplay(in: view.bounds, to: rep)
                var lit = 0
                var x = 20
                while x < rep.pixelsWide - 20 {
                    var y = 20
                    while y < rep.pixelsHigh - 20 {
                        if let color = rep.colorAt(x: x, y: y)?.usingColorSpace(.sRGB), color.redComponent + color.greenComponent + color.blueComponent > 1.2 {
                            lit += 1
                        }
                        y += 4
                    }
                    x += 4
                }
                #expect(lit > 40, "\(move) \(progress) lit \(lit)")
            }
        }
    }
}
