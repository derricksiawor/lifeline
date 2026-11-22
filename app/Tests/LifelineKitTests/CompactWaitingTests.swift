import AppKit
import Foundation
import SwiftUI
import Testing
@testable import Lifeline
@testable import LifelineKit

private final class Done: @unchecked Sendable {
    private let lock = NSLock()
    private var value = false

    var on: Bool {
        get { lock.withLock { value } }
        set { lock.withLock { value = newValue } }
    }
}

private func compactState(_ job: String) -> String {
    #"{"version": 1, "mac": {"view": "mac", "rows": [{"uuid": "j", "name": "Alex's Account", "is_active": true}]}, "accounts": [{"uuid": "j", "name": "Alex's Account", "short": "Alex", "number": 1}, {"uuid": "n", "name": "Alexandra", "short": "Alexandra", "number": 2}], "known_accounts": [{"name": "Team Account", "short": "Team", "full": "Example Org Team Account", "email": "sam@example.org", "organization": "Example Org"}], "jobs": ["# + job + "]}"
}

struct CompactWaitingRuleTests {
    @Test func theCompactCardsWordsComeFromTheCore() throws {
        for job in try Fixture.state("job-cards").jobs ?? [] {
            #expect(!(job.compactTitle ?? "").isEmpty, "\(job.id)")
        }
        let add = try #require(try Fixture.state("job-cards").jobs?.first { $0.kind == "add" })
        #expect(add.compact == "Finish the sign-in using Safari. This stays here until it is done.")
        #expect(add.compactTitle == add.known)
    }
}

@MainActor
@Suite(.serialized)
struct CompactWaitingTests {
    @Test func theWaitingCardIsShortAndNamesTheAccount() throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let state = try Fixture.state("add-elsewhere")
        let job = try #require(state.jobs?.first)
        #expect(JobFlow.cardSentence(job) == "Press Authorize in Safari to add Work Account to this Mac.")
        for scheme in [ColorScheme.light, .dark] {
            let host = NSHostingView(rootView: CompactWaiting(job: job, state: state).environment(\.colorScheme, scheme))
            let size = host.fittingSize
            #expect(size.height <= 200 && size.height >= 100, "\(size)")
            #expect(abs(size.width - Metrics.panelWidth) < 1, "\(size)")
        }
        let refused = NSHostingView(rootView: CompactWaiting(job: job, state: state, problem: "That sign-in has no page to open.").environment(\.colorScheme, .light))
        #expect(refused.fittingSize.height > NSHostingView(rootView: CompactWaiting(job: job, state: state)).fittingSize.height)
    }

    @Test func itHoldsAndGrowsWhenTheSignInEnds() async throws {
        let done = Done()
        let posted = Done()
        let server = try FakeServer { head, _ in
            if head.hasPrefix("POST /add") {
                posted.on = true
                return [okReply(#"{"job": "2"}"#, status: "202 Accepted")]
            }
            guard posted.on else {
                return [okReply(compactState(""))]
            }
            let job = done.on
                ? #"{"id": "2", "kind": "add", "state": "done", "uuid": "n", "suggested_name": "Team Account", "suggested_short": "Team"}"#
                : #"{"id": "2", "kind": "add", "state": "running", "url": "https://claude.ai/oauth/authorize?x=1", "opened": true, "browser": "Google Chrome", "known": "Team Account"}"#
            return [okReply(compactState(job))]
        }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        store.opened()
        store.menuOpen = true
        store.screen = .add
        store.startAdd(.add)
        await waitFor { store.addJob != nil }
        #expect(store.addCompact)
        #expect(JobFlow.toggleMove(open: true, holding: store.addHolding) == .raise)
        #expect(JobFlow.outsideMove(holding: store.addHolding) == .stay)
        done.on = true
        await store.refresh()
        await waitFor { store.addNeedsNaming }
        #expect(!store.addCompact)
        #expect(store.screen == .add)
    }
}
