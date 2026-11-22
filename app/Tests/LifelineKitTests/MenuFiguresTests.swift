import AppKit
import Foundation
import SwiftUI
import Testing
@testable import Lifeline
@testable import LifelineKit

private struct RGB {
    var r: Double
    var g: Double
    var b: Double

    init(_ color: Color) {
        let found = NSColor(color).usingColorSpace(.sRGB) ?? .black
        r = Double(found.redComponent)
        g = Double(found.greenComponent)
        b = Double(found.blueComponent)
    }

    init(r: Double, g: Double, b: Double) {
        self.r = r
        self.g = g
        self.b = b
    }

    func over(_ ground: RGB, _ alpha: Double) -> RGB {
        RGB(r: ground.r + (r - ground.r) * alpha, g: ground.g + (g - ground.g) * alpha, b: ground.b + (b - ground.b) * alpha)
    }

    var luminance: Double {
        func linear(_ c: Double) -> Double { c <= 0.03928 ? c / 12.92 : pow((c + 0.055) / 1.055, 2.4) }
        return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b)
    }

    func contrast(_ other: RGB) -> Double {
        let (a, b) = (luminance, other.luminance)
        return (max(a, b) + 0.05) / (min(a, b) + 0.05)
    }
}

@MainActor
struct FooterContrastTests {
    @Test func theFooterLabelStaysLegibleThroughTheWholeSwitch() {
        let timeline = SwitchTimeline.approved
        for palette in [Palette.light, Palette.dark] {
            let looks: [(fill: Color, ink: Color)] = [(palette.primary, palette.primaryInk), (palette.quietBackground, palette.quietInk)]
            for old in looks {
                for new in looks {
                    var worst = 99.0
                    for step in 0...200 {
                        let frame = timeline.frame(at: Double(step) / 200 * timeline.total)
                        if frame.showsOld || frame.finished { continue }
                        let cross = Footer.cross(TextFade(outgoing: frame.textOut, incoming: frame.textIn, height: frame.textHeight))
                        let ground = RGB(palette.panelBottom)
                        let fill = RGB(new.fill).over(RGB(old.fill).over(ground, cross.oldFill), cross.newFill)
                        #expect(max(cross.oldInk, cross.newInk) >= 0.5, "the button has no words at \(frame)")
                        if cross.oldInk > 0 {
                            worst = min(worst, RGB(old.ink).over(fill, cross.oldInk).contrast(fill))
                        }
                        if cross.newInk > 0 {
                            worst = min(worst, RGB(new.ink).over(fill, cross.newInk).contrast(fill))
                        }
                    }
                    #expect(worst >= 2.4, "a footer label falls to a contrast of \(worst)")
                }
            }
        }
    }
}

@MainActor
@Suite(.serialized)
struct FiguresAndWordsTests {
    init() {
        Fonts.register(folder: URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Resources/Fonts", isDirectory: true))
    }

    @Test func labelsSplitIntoFiguresAndWords() {
        #expect(FigureRuns.split("Week 94%").map(\.text) == ["Week ", "94%"])
        #expect(FigureRuns.split("Week 94%").map(\.figure) == [false, true])
        #expect(FigureRuns.split("5 hours 70%").map(\.text) == ["5 hours ", "70%"])
        #expect(FigureRuns.split("$100 Max 5x").map(\.text) == ["$100", " Max ", "5x"])
        #expect(FigureRuns.split("$100 Max 5x").map(\.figure) == [true, false, true])
        #expect(FigureRuns.split("42%").map(\.figure) == [true])
        #expect(FigureRuns.split("42% of its 5 hours used").map(\.text) == ["42%", " of its 5 hours used"])
    }

    @Test func aJoinedNameOnTheLastLineDoesNotPullAWordDown() {
        let lines = Styles.head.lines("This Mac now uses Team Account.", width: Metrics.contentWidth, keep: ["Team Account"])
            .map { $0.replacingOccurrences(of: "\u{00A0}", with: " ") }
        #expect(lines == ["This Mac now uses", "Team Account."], "\(lines)")
    }

    @Test func noWordIsSetInTheMonoFaceOnAnyScreen() throws {
        let figure = try Regex(#"^\$?\d+(\.\d+)?(%|x)?$"#)
        var screens: [(String, AnyView)] = []
        for name in ["moment-good", "moment-menu", "moment-limit", "moment-switched", "extra-noroom", "extra-trouble"] {
            let state = try Fixture.state(name)
            screens.append((name, AnyView(MenuView(state: state, side: .mac, note: nil))))
            screens.append((name + " devbox", AnyView(MenuView(state: state, side: .dev, note: nil))))
        }
        let sheet = try Fixture.state("moment-sheet")
        screens.append(("settings", AnyView(SettingsView(state: sheet, local: LocalStatus(notices: .allowed, shortcut: .on, login: .on), problem: nil))))
        screens.append(("add", AnyView(AddAccountView(state: sheet, step: .start, target: .mac, problem: nil, zone: .current))))
        for (name, view) in screens {
            let mono = drawnMono(view)
            #expect(!mono.isEmpty || name.contains("settings") || name.contains("add"), "\(name) drew no figure")
            for text in mono {
                let trimmed = text.trimmingCharacters(in: .whitespaces)
                #expect(trimmed.wholeMatch(of: figure) != nil, "\(name) sets \"\(text)\" in the mono face")
            }
        }
    }
}

@MainActor
struct ScreenChangeTests {
    @Test func twoScreensNeverShareAFrameAndNoFrameIsEmpty() {
        for step in 0...1000 {
            let progress = Double(step) / 1000
            let out = ScreenMotion.outgoingOpacity(progress)
            let into = ScreenMotion.incomingOpacity(progress)
            #expect(out == 0 || into == 0, "both screens show at \(progress)")
            #expect(out + into >= 0.5, "the panel is nearly empty at \(progress)")
            #expect(ScreenSlide.opacity(.outgoing, amount: progress) == out)
            #expect(ScreenSlide.opacity(.incoming, amount: 1 - progress) == into)
        }
        #expect(ScreenMotion.incomingOpacity(1) == 1 && ScreenMotion.outgoingOpacity(0) == 1)
        #expect(ScreenMotion.incomingOpacity(0) == 0 && ScreenMotion.outgoingOpacity(1) == 0)
    }
}

@MainActor
struct CorePendingHeadTests {
    @Test func thePendingHeadIsTheCoresOwn() throws {
        let state = try Fixture.state("moment-good")
        let model = try #require(state.mac)
        let work = try #require(model.rows?.first { $0.uuid == Fixture.work }?.pendingHead)
        #expect(!work.isEmpty)
        #expect(PendingLook.model(model, state: state, pending: Fixture.work, side: .mac).head == work)
        #expect(PendingLook.model(model, state: state, pending: MenuLogic.undoTarget, side: .mac).head == model.pendingBack)
        #expect(!(model.pendingBack ?? "").isEmpty)
        var older = model
        older.rows = older.rows?.map { var row = $0; row.pendingHead = nil; return row }
        #expect(PendingLook.model(older, state: state, pending: Fixture.work, side: .mac).head == model.head, "the app built its own pending head")
    }
}
