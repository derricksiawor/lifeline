import AppKit
import Foundation
import SwiftUI
import Testing
@testable import Lifeline
@testable import LifelineKit

private let fontsFolder = URL(fileURLWithPath: #filePath)
    .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    .appendingPathComponent("Resources/Fonts", isDirectory: true)

private let devboxEmpty = #"""
{"version": 1,
 "dev": {"view": "dev", "view_label": "Studio", "sessions": "0 sessions open", "hero_name": "", "hero_plan": "", "head": "No account is in use on Studio.",
         "sub": "", "hero_w": "0%", "fore_w": "0%", "hero_tone": "calm", "hero_used": "", "hero_reset": "", "week_w": "0%", "week_text": "",
         "second_text": "", "second_w": "0%", "second_window": "", "binding": "", "rows": []},
 "mac": {"view": "mac", "view_label": "This Mac", "sessions": "3 sessions open", "hero_name": "Alex's Account", "hero_plan": "Max 5x",
         "head": "Alex's Account lasts until its 5 hours reset today at 17:00.", "sub": "At this pace it reaches 71% before its 5 hours reset.",
         "hero_w": "42%", "fore_w": "29%", "hero_tone": "calm", "hero_used": "42% of its 5 hours used", "hero_reset": "Resets today at 17:00",
         "second_text": "Its week is 18% used and resets on Sunday at 09:00.", "second_w": "18%", "second_window": "week", "binding": "five",
         "action_kind": "none",
         "rows": [
           {"uuid": "j", "name": "Alex's Account", "pct": "42%", "five_w": "42%", "fore_w": "29%", "tone": "calm", "is_active": true, "where": "Used by this Mac."},
           {"uuid": "o", "name": "Team Account", "pct": "0%", "five_w": "0%", "tone": "calm", "is_best": true, "where": "Not in use."}]},
 "accounts": [{"uuid": "j", "name": "Alex's Account", "short": "Alex", "number": 1}, {"uuid": "o", "name": "Team Account", "short": "Team", "number": 2}],
 "known_accounts": [{"name": "Work Account", "short": "Work", "email": "hello@example.net", "organization": "personal"}],
 "devbox": {"reachable": true, "answers": true, "live": {"name": "Personal Account", "short": "Personal", "added": true}},
 "jobs": [{"id": "a", "kind": "add", "state": "running", "known": "Ama Rivera Account"}]}
"""#

private func sample() throws -> ServiceState {
    try ServiceState.decode(Data(devboxEmpty.utf8))
}

@MainActor
@Suite(.serialized)
struct HeroWithoutAccountTests {
    @Test func aHeroWithNoAccountDrawsNoLines() throws {
        let state = try sample()
        let empty = try #require(state.dev)
        let named = try #require(state.mac)
        #expect(HeroView.drawsLines(empty) == false)
        #expect(HeroView.drawsLines(named) == true)
        var unnamed = named
        unnamed.heroName = nil
        #expect(HeroView.drawsLines(unnamed) == false)
    }

    @Test func theEmptyHeroLeavesOutTheLinesItWouldDraw() throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let state = try sample()
        let empty = try #require(state.dev)
        var named = empty
        named.heroName = "Alex's Account"
        func height(_ model: MenuModel) -> CGFloat {
            NSHostingView(rootView: HeroView(model: model, palette: .dark, look: .plain(model), animated: false)
                .environment(\.menuMotion, Motion(MotionPlan(reduceMotion: true)))).fittingSize.height
        }
        let bar: CGFloat = 18 + 6
        let used: CGFloat = 8 + Styles.small.lineHeight
        let week: CGFloat = 14 + 2
        #expect(height(named) - height(empty) >= bar + used + week - 0.5, "the hero with no account still keeps room for its lines: \(height(named)) against \(height(empty))")
    }
}

@MainActor
@Suite(.serialized)
struct UnbreakableTests {
    init() {
        Fonts.register(folder: fontsFolder)
    }

    @Test func theNamesComeFromEveryPlaceTheCoreNamesAnAccount() throws {
        let names = Unbreakable.names(try sample())
        for name in ["Alex's Account", "Team Account", "Team", "Work Account", "Personal Account", "Personal", "Ama Rivera Account"] {
            #expect(names.contains(name), "\(name) is missing from \(names)")
        }
    }

    @Test func namesDatesTimesAndUnitsAreJoined() {
        let names = ["North Team Account", "North Team"]
        let joined = Unbreakable.apply("This Mac now uses North Team Account. North Team is back at 09:00, until 16 December, in 5 hours.", keep: names)
        #expect(joined.contains("North\u{00A0}Team\u{00A0}Account."))
        #expect(joined.contains("North\u{00A0}Team is"))
        #expect(joined.contains("at\u{00A0}09:00"))
        #expect(joined.contains("16\u{00A0}December"))
        #expect(joined.contains("5\u{00A0}hours"))
        #expect(joined.contains("This Mac now uses"), "ordinary spaces stay breakable")
        #expect(Unbreakable.apply("Alexandra is in use.", keep: names) == "Alexandra is in use.")
    }

    @Test func noWidthSplitsANameADateATimeOrAUnit() {
        let names = ["Team Account", "Alex's Account", "Personal Account", "Work Account"]
        let sentences = [
            "This Mac now uses Team Account.",
            "The menu bar calls it Alex. It is on the Max 5x plan. It is signed in on this Mac until 16 December and on Studio.",
            "Alex's Account lasts until its 5 hours reset today at 17:00.",
            "Its week is 18% used and resets on Sunday at 09:00.",
            "Step 1 of 2: press Authorize in Safari to add Work Account to this Mac.",
            "No other account has room. Personal Account is back on 24 November at 14:00."
        ]
        let kept = names + ["16 December", "24 November", "at 17:00", "at 09:00", "at 14:00", "5 hours"]
        for style in [Styles.head, Styles.small, Styles.sub, Styles.sheetLead, Styles.explain] {
            for width in stride(from: CGFloat(110), through: 348, by: 3) {
                for sentence in sentences {
                    let lines = style.lines(sentence, width: width, keep: names).map { $0.replacingOccurrences(of: "\u{00A0}", with: " ") }
                    #expect(lines.joined(separator: " ") == sentence, "the words changed at \(width): \(lines)")
                    for piece in kept where sentence.contains(piece) && style.width(of: Self.wholeRun(piece, in: sentence)) < width {
                        #expect(lines.contains { $0.contains(piece) }, "\(piece) was split at width \(width): \(lines)")
                    }
                }
            }
        }
    }

    static func wholeRun(_ piece: String, in sentence: String) -> String {
        guard let range = sentence.range(of: piece) else { return piece }
        let rest = sentence[range.upperBound...].prefix { $0 != " " }
        return piece + rest
    }

    @Test func aParagraphReadsTheNamesFromItsScreen() throws {
        let state = try sample()
        var names: [String] = []
        let probe = NamesProbe { names = $0 }
        _ = ImageRenderer(content: probe.keepsNames(of: state)).cgImage
        #expect(names.contains("Team Account"))
    }
}

@MainActor
@Suite(.serialized)
struct ScreensKeepNamesTests {
    init() {
        Fonts.register(folder: fontsFolder)
    }

    @Test func theMenuKeepsAnAccountNameOnOneLine() throws {
        var state = try sample()
        state.mac?.head = "This Mac now uses Ama Rivera Account."
        let head = try #require(state.mac?.head)
        let plain = Styles.head.lines(head, width: Metrics.contentWidth)
        try #require(plain.contains { $0.hasSuffix("Ama") }, "the sentence no longer splits the name without help: \(plain)")
        let lines = drawnLines(MenuView(state: state, side: .mac, note: nil)) { $0.contains { $0.contains("Account.") } }
        #expect(lines.contains { $0.hasSuffix("Ama Rivera Account.") }, "\(lines)")
        #expect(!lines.contains { $0.hasSuffix(" Ama") }, "\(lines)")
    }
}

private struct NamesProbe: View {
    let seen: ([String]) -> Void
    @Environment(\.keptNames) private var names

    var body: some View {
        seen(names)
        return Color.clear.frame(width: 4, height: 4)
    }
}

@MainActor
@Suite(.serialized)
struct HeroFigureTests {
    @Test func theFigureIsSetApartFromTheWords() {
        let runs = FigureRuns.split("42% of its 5 hours used")
        #expect(runs.map(\.text) == ["42%", " of its 5 hours used"])
        #expect(runs.map(\.figure) == [true, false])
        #expect(FigureRuns.split("100% of its week used").first?.text == "100%")
        #expect(FigureRuns.split("Not started").map(\.figure) == [false])
        #expect(FigureRuns.split("").isEmpty)
    }

    @Test func theUsedLineIsMixedNotAllMono() {
        let line = HeroView.usedRuns("42% of its 5 hours used")
        #expect(line.count == 2)
        #expect(line.first?.style.family == .numbers)
        #expect(line.last?.style.family == .words)
    }
}

@MainActor
struct SwitchCrossfadeTests {
    @Test func outgoingTextIsGoneBeforeIncomingTextShows() {
        let timeline = SwitchTimeline.approved
        for step in 0...130 {
            let t = Double(step) / 100
            let frame = timeline.frame(at: t)
            if frame.showsOld { continue }
            if frame.textIn > 0.3 {
                #expect(frame.textOut == 0, "at \(t) the old text is still at \(frame.textOut) while the new one is at \(frame.textIn)")
            }
            if t >= 0.6 {
                #expect(frame.textOut == 0, "the old text is still showing at \(t)")
            }
            #expect(frame.textOut == 0 || frame.textIn == 0, "old and new text share the frame at \(t)")
        }
        #expect(timeline.frame(at: timeline.total).textIn == 1)
    }

    @Test func theFooterLabelChangesOnlyAfterItsFill() {
        let timeline = SwitchTimeline.approved
        for step in 45...100 {
            let frame = timeline.frame(at: Double(step) / 100)
            let cross = Footer.cross(TextFade(outgoing: frame.textOut, incoming: frame.textIn, height: frame.textHeight))
            if cross.newInk > 0 {
                #expect(cross.newFill >= 0.999, "the new label shows at fill \(cross.newFill)")
                #expect(cross.oldInk == 0)
            }
            #expect(cross.oldFill + cross.newFill <= 1.001)
            #expect(cross.oldInk + cross.newInk >= 0.5, "the button has no words at \(Double(step) / 100) s")
            #expect(cross.oldInk == 0 || cross.newInk == 0, "two labels share the button at \(Double(step) / 100) s")
        }
    }
}

@MainActor
struct PendingSwitchTests {
    @Test func whilePendingOnlyTheTargetLooksInUse() throws {
        let state = try sample()
        let model = try #require(state.mac)
        let shown = PendingLook.model(model, state: state, pending: "o", side: .mac)
        #expect((shown.sub ?? "").isEmpty, "the old account's sub still shows: \(shown.sub ?? "")")
        let rows = try #require(model.rows)
        let lit = rows.map { PendingLook.look(RowLook.plain($0), row: $0, pending: "o") }
        #expect(lit[0].background == 0 && lit[0].glow == 0, "the old row stays lit")
        #expect(lit[1].background == 1 && lit[1].glow == 1, "the target row is not lit")
        let still = rows.map { PendingLook.look(RowLook.plain($0), row: $0, pending: nil) }
        #expect(still[0].background == 1 && still[1].background == 0)
        let devbox = PendingLook.model(model, state: state, pending: "o", side: .dev)
        #expect(devbox == model)
    }
}
