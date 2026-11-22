import AppKit
import Foundation
import SwiftUI
import Testing
import Vision
@testable import Lifeline
@testable import LifelineKit

private let cardState = #"""
{"version": 1,
 "mac": {"view": "mac", "view_label": "This Mac", "hero_name": "Alex's Account", "head": "Alex's Account lasts until its 5 hours reset today at 17:00.",
         "rows": [
           {"uuid": "j", "name": "Alex's Account", "pct": "42%", "pct_label": "5 hours 42%", "is_active": true, "where": "Used by this Mac."},
           {"uuid": "c", "name": "Work Account", "pct": "94%", "pct_label": "Week 94%", "where": "Used by Studio."},
           {"uuid": "p", "name": "Personal Account", "pct": "76%", "pct_label": "Week 76%", "where": "Not in use."},
           {"uuid": "o", "name": "Team Account", "pct": "9%", "pct_label": "Week 9%", "where": "Not in use."}]},
 "accounts": [{"uuid": "j", "name": "Alex's Account", "number": 1}, {"uuid": "c", "name": "Work Account", "number": 2},
              {"uuid": "p", "name": "Personal Account", "number": 3}, {"uuid": "o", "name": "Team Account", "number": 4, "organization": "Example Org"}],
 "jobs": []}
"""#

private func decoded() throws -> ServiceState {
    try ServiceState.decode(Data(cardState.utf8))
}

struct OneSentenceRuleTests {
    @Test func eachWaitingCardIsTheCoresSentence() throws {
        let state = try Fixture.state("job-cards")
        for job in state.jobs ?? [] {
            #expect(!(job.card ?? "").isEmpty && JobFlow.cardSentence(job) == job.card, "\(job.id)")
        }
        #expect(JobFlow.cardSentence(try Fixture.job("job-cards", "k-add-step")) == "Step 1 of 2: press Authorize in Safari to add Team Account to this Mac.")
        #expect(JobFlow.cardSentence(try Fixture.job("job-cards", "k-devbox-add")) == "Step 2 of 2: press Authorize in Safari to give Studio Team Account.")
        #expect(JobFlow.cardSentence(try Fixture.job("job-cards", "k-resign")) == "Press Authorize in Safari to sign this Mac in to Work Account again.")
        #expect(JobFlow.cardSentence(JobSummary(id: "x", kind: "add", state: "running")) == "")
    }

    @Test func devboxSignInsCanBeCancelled() {
        #expect(JobFlow.cancellable(JobSummary(id: "1", kind: "devbox-add")))
        #expect(JobFlow.cancellable(JobSummary(id: "1", kind: "devbox-resign")))
        #expect(JobFlow.cancellable(JobSummary(id: "1", kind: "add")))
        #expect(JobFlow.cancellable(JobSummary(id: "1", kind: "devbox-adopt")))
        #expect(!JobFlow.cancellable(JobSummary(id: "1", kind: "adopt")))
    }

    @Test func aResultCardIsOneSentence() throws {
        let failed = try Fixture.state("failed-jobs")
        let cancelled = try #require(failed.jobs?.first { $0.id == "fc" })
        #expect(cancelled.result == "You cancelled the sign-in. Nothing changed.")
        #expect(JobFlow.resultSentence(cancelled, state: failed) == "You cancelled the sign-in.")
        let added = try Fixture.state("after-mac-add-org")
        let done = try #require(added.jobs?.first)
        #expect(JobFlow.resultSentence(done, state: added) == "Lifeline added Team Account.")
    }

    @Test func cardsNeverMakeTheMenuTaller() {
        for limit in [879.0, 2000.0] {
            for list in [180.0, 420.0, 900.0] {
                let stops = stride(from: 80.0, through: list, by: 82).map { $0 }
                let bare = MenuLogic.cappedHeights(cards: 0, top: 300, list: list, footer: 60, limit: limit, spacing: 8, minimum: 120, cardMinimum: 96, listStops: stops)
                let with = MenuLogic.cappedHeights(cards: 140, top: 300, list: list, footer: 60, limit: limit, spacing: 8, minimum: 120, cardMinimum: 96, listStops: stops)
                func total(_ heights: [Double]) -> Double {
                    heights.reduce(0, +) + 8 * Double(max(heights.filter { $0 > 0 }.count - 1, 0))
                }
                #expect(abs(total(with) - total(bare)) <= 1, "\(limit) \(list) \(bare) \(with)")
                #expect(with[0] >= 96, "\(with)")
            }
        }
    }
}

@MainActor
@Suite(.serialized)
struct OneSentenceViewTests {
    private func host(_ view: some View) -> NSHostingView<AnyView> {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let host = NSHostingView(rootView: AnyView(view.environment(\.colorScheme, .dark)))
        host.frame = NSRect(origin: .zero, size: host.fittingSize)
        host.layoutSubtreeIfNeeded()
        return host
    }

    @Test func aWaitingCardLeavesTheMenuItsHeight() throws {
        let state = try decoded()
        let job = JobSummary(id: "2", kind: "devbox-add", uuid: "o", state: "running", url: "https://claude.ai/oauth/authorize?x=1", opened: true, browser: "Safari")
        for cap in [879.0, 600.0] {
            let bare = host(MenuView(state: state, side: .mac, note: nil, maxHeight: cap)).fittingSize.height
            let carded = host(MenuView(state: state, side: .mac, note: nil, maxHeight: cap, waiting: [job])).fittingSize.height
            #expect(abs(bare - carded) <= 1, "\(cap) \(bare) \(carded)")
        }
    }
}
