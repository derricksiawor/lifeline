import AppKit
import Foundation
import SwiftUI
import Testing
import Vision
@testable import Lifeline
@testable import LifelineKit

private let auditMenu = #"""
{"version": 1,
 "mac": {"view": "mac", "view_label": "This Mac", "sessions": "3 sessions open", "hero_name": "Alex's Account", "hero_plan": "Max 5x",
         "head": "Alex's Account lasts until it resets today at 17:00.", "sub": "At this pace it reaches 71% before its 5 hours reset today at 17:00.",
         "hero_w": "42%", "fore_w": "29%", "hero_tone": "calm", "hero_used": "42% of its 5 hours used", "hero_reset": "Resets today at 17:00",
         "action_kind": "none", "hint": "", "icon_dot": true, "devbox_note": "Studio needs you. Work Account runs out today at 16:20.",
         "rows": [
           {"uuid": "j", "name": "Alex's Account", "binding": "five", "pct": "42%", "five_w": "42%", "week_w": "18%", "week_pct": "Week 18%", "tone": "calm", "is_active": true, "where": "Used by this Mac."},
           {"uuid": "c", "name": "Work Account", "binding": "week", "figure": "Week 94%", "pct": "94%", "five_w": "94%", "week_w": "0%", "week_pct": "", "tone": "low", "where": "Not in use. Resets on Sunday at 09:00.",
            "second_text": "It has used 70% of its 5 hours.", "second_w": "70%", "second_window": "five"},
           {"uuid": "o", "name": "Team Account", "pending_head": "Switching this Mac to Team Account.", "binding": "five", "pct": "9%", "five_w": "9%", "week_w": "5%", "week_pct": "Week 5%", "tone": "calm", "where": "Not in use.",
            "second_text": "It has used 5% of its week.", "second_w": "5%", "second_window": "week"}]},
 "accounts": [{"uuid": "j", "name": "Alex's Account", "short": "Alex", "number": 1}, {"uuid": "c", "name": "Work Account", "short": "Work", "number": 2},
              {"uuid": "o", "name": "Team Account", "short": "Team", "number": 3, "organization": "Example Org"}],
 "known_accounts": [{"name": "Personal Account", "short": "Personal", "email": "sam@example.org", "organization": "sam@example.org's Organization", "personal": true}],
 "jobs": []}
"""#

@MainActor
private func ocrImage(_ image: CGImage) throws -> [String] {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    onCPU(request)
    try performText(request, on: image)
    let lines = (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }
    return [lines.joined(separator: " ")] + lines
}

@MainActor
private func renderText(_ view: some View) throws -> [String] {
    typeset(view)
}

@MainActor
private func hostedText(_ view: some View) async throws -> [String] {
    typeset(view)
}

private func auditState(_ edit: (inout ServiceState) -> Void = { _ in }) throws -> ServiceState {
    var state = try Fixture.withSettingsText(auditMenu)
    edit(&state)
    return state
}

@MainActor
@Suite(.serialized)
struct WordingViewTests {
    @Test func oneResultCardAtATimeWithNextResult() async throws {
        let state = try auditState()
        let results = (1...3).map { JobSummary(id: "f\($0)", kind: "add", state: "failed", result: "Result number \($0) did not add the account.") }
        var shown: Set<String> = []
        let found = try await hostedText(MenuView(state: state, side: .mac, note: nil, maxHeight: 879, results: results, cardsShown: { shown = $0 }))
        let cards = found.dropFirst().filter { $0.contains("Result number") }
        #expect(cards.count == 1, "\(found)")
        #expect(found.contains { $0.contains("Result number 3") }, "\(found)")
        #expect(found.contains { $0.contains("Next result") }, "\(found)")
        #expect(shown.count <= 1, "\(shown)")
    }

    @Test func rowsDrawTheCoresLabel() {
        var row = MenuRow(uuid: "j", name: "Alex's Account", pct: "42%", weekPct: "Week 18%", binding: "five")
        #expect(AccountRow.named(row) == "42%")
        row.pctLabel = "5 hours 42%"
        #expect(AccountRow.named(row) == "5 hours 42%")
        row.pctLabel = ""
        #expect(AccountRow.named(row) == "")
    }

    @Test func rowsNameTheirWindowAndKeepTheWeekFigure() throws {
        let state = try auditState()
        let rows = try #require(state.mac?.rows)
        let palette = Palette.of(.light)
        func text(_ row: MenuRow) throws -> [String] {
            try renderText(AccountRow(row: row, old: nil, fade: .settled, palette: palette, look: RowLook.plain(row), animated: false, action: nil))
        }
        let alex = try text(rows[0])
        #expect(alex.contains { $0.contains("Week 18%") }, "\(alex)")
        let work = try text(rows[1])
        #expect(work.contains { $0.contains("Week 94%") }, "\(work)")
        let org = try text(rows[2])
        #expect(org.contains { $0.contains("Week 5%") }, "\(org)")
    }

    @Test func aClickedRowNamesTheSwitchInTheHeadAtOnce() async throws {
        let found = try await hostedText(MenuView(state: try auditState(), side: .mac, note: nil, maxHeight: 879, pendingSwitch: "o"))
        #expect(found.first?.contains("Switching this Mac to Team Account.") == true, "\(found)")
    }

    @Test func noLineEndsOnASingleWord() {
        let text = "It has used 18% of its week. Its week resets on Sunday at 09:00."
        var width: CGFloat = 150
        while width <= 360 {
            let lines = Styles.small.lines(text, width: width)
            if lines.count >= 2 {
                #expect(lines.last?.contains(" ") == true, "\(width) \(lines)")
            }
            width += 3
        }
    }

    @Test func theDevboxNeedsYouLineShowsUnderTheHero() async throws {
        let found = try await hostedText(MenuView(state: try auditState(), side: .mac, note: nil, maxHeight: 879))
        #expect(found.contains { $0.contains("Studio needs you") }, "\(found)")
    }

    @Test func aFailedAddLeadsWithItsResult() throws {
        let found = try renderText(AddAccountView(state: try auditState(), step: .failed("Lifeline already has Personal Account. Your browser signed in as sam@example.org, so nothing changed."), target: .mac, problem: nil, zone: TimeZone(identifier: "UTC")!))
        let lines = Array(found.dropFirst())
        let head = lines.firstIndex { $0.contains("already has") }
        #expect(head != nil, "\(lines)")
        let instructions = lines.firstIndex { $0.contains("Lifeline opens Claude") } ?? Int.max
        let tryAgain = lines.firstIndex { $0 == "Try again" } ?? Int.max
        #expect((head ?? Int.max) < instructions && instructions < tryAgain, "\(lines)")
        #expect(!lines.contains("Add an account"), "\(lines)")
    }

    @Test func aPageThatNeverOpenedSaysOpenNotReopen() throws {
        let job = JobSummary(id: "1", kind: "add", state: "running", url: "https://claude.ai/oauth/authorize?x=1", opened: false)
        let found = try renderText(JobWaitingView(job: job, state: try auditState()))
        #expect(found.contains("Open the sign-in page"), "\(found)")
        #expect(!found.contains { $0.contains("page again") }, "\(found)")
    }

    @Test func theSwitchNeverShowsAnEmptyHead() {
        let timeline = SwitchTimeline.approved
        var t = 0.0
        while t <= timeline.total {
            let frame = timeline.frame(at: t)
            if !frame.showsOld && !frame.finished {
                #expect(frame.textOut + frame.textIn >= 0.5, "\(t) out \(frame.textOut) in \(frame.textIn)")
            }
            t += 0.005
        }
    }
}
