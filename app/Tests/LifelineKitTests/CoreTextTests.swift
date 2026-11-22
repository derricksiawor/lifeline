import Foundation
import Testing
@testable import Lifeline
@testable import LifelineKit

private let coreState = #"""
{"version": 1,
 "mac": {"view": "mac", "action_kind": "undo", "action_text": "Undo the switch", "undo_until": "2026-11-20T12:00:05Z",
         "rows": [{"uuid": "j", "name": "Alex's Account", "is_active": true, "where": "Used by this Mac. Last read at 11:40."}]},
 "accounts": [
   {"uuid": "j", "name": "Alex's Account", "short": "Alex", "number": 1, "plan": "Max 5x", "devbox_signed_in": false,
    "signin_text": "It is signed in on this Mac until 18 December. Studio has no sign-in for it yet.",
    "detail": {"usage": "It has used 42% of its 5 hours and 18% of its week.", "models": "",
               "account": "Its full name is Alexandra Rivera. It is signed in on this Mac until 18 December. Studio has no sign-in for it yet.",
               "projects": "Open on this Mac: project-b."}},
   {"uuid": "c", "name": "Work Account", "short": "Work", "number": 2, "devbox_signed_in": null,
    "signin_text": "It is signed in on this Mac. Lifeline on Studio is not answering, so its sign-in there is not shown."},
   {"uuid": "p", "name": "Personal Account", "short": "Personal", "number": 3, "devbox_signed_in": true}]}
"""#

@MainActor
struct CoreTextTests {
    private func state() throws -> ServiceState {
        try ServiceState.decode(Data(coreState.utf8))
    }

    @Test func theCoresFieldsDecode() throws {
        let s = try state()
        let alex = try #require(s.accounts?.first)
        #expect(alex.detail?.usage == "It has used 42% of its 5 hours and 18% of its week.")
        #expect(alex.detail?.models == "")
        #expect(alex.signinText?.hasPrefix("It is signed in on this Mac until 18 December.") == true)
        #expect(alex.devboxSignedIn == false)
        #expect(s.accounts?[1].devboxSignedIn == nil)
        #expect(s.accounts?[2].devboxSignedIn == true)
        #expect(s.mac?.undoUntil == "2026-11-20T12:00:05Z")
        let reply = try HistoryReply.decode(Data(#"{"uuid": "j", "summary": "It ran out of its 5 hours once in the last 7 days, on Friday.", "daily": []}"#.utf8))
        #expect(reply.summary == "It ran out of its 5 hours once in the last 7 days, on Friday.")
    }

    @Test func theOpenRowDrawsTheCoresLinesInOrder() throws {
        let s = try state()
        let alex = try #require(s.accounts?.first)
        let reply = HistoryReply(daily: [DailyPeak(date: "2026-11-18", peakFive: 40), DailyPeak(date: "2026-11-19", peakFive: 60)], summary: "It ran out once, on Friday.")
        let row = Detail.openRow(alex, reply: reply)
        #expect(row.lines == [
            "It has used 42% of its 5 hours and 18% of its week.",
            "It ran out once, on Friday.",
            try #require(alex.signinText)
        ])
        let inUse = Detail.openRow(alex, reply: reply, heroShown: true)
        #expect(inUse.lines == ["It ran out once, on Friday.", try #require(alex.signinText), "Open on this Mac: project-b."])
        #expect(row.chartAfter == 1)
        #expect(!row.sparkline)
        let older = AccountSummary(uuid: "x", name: "Old")
        #expect(Detail.openRow(older, reply: nil).lines.isEmpty)
    }

    @Test func theWhereLineIsTheCoresOwn() throws {
        let s = try state()
        let row = try #require(s.mac?.rows?.first)
        #expect(Detail.whereLine(row, open: false) == "Used by this Mac. Last read at 11:40.")
        #expect(Detail.whereLine(row, open: true) == "Used by this Mac.")
    }

    @Test func settingsUsesTheCoresAccountSentence() throws {
        let s = try Fixture.withSettingsText(coreState)
        let rows = try #require(SettingsPage.sections(state: s, local: LocalStatus(notices: .allowed, shortcut: .on, login: .on)).first).rows
        for account in s.accounts ?? [] {
            #expect(rows.first { $0.id == "account-" + account.uuid }?.description == (account.detail?.account ?? ""))
        }
    }

    @Test func undoEndsAtTheCoresTime() throws {
        let since = Date()
        let until = since.addingTimeInterval(3)
        #expect(MenuLogic.undoShows(since: since, until: until, now: since.addingTimeInterval(2.9)))
        #expect(!MenuLogic.undoShows(since: since, until: until, now: since.addingTimeInterval(3.1)))
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: "/nonexistent/lifeline.sock")), sizes: nil)
        store.noteUndo(try state())
        #expect(store.undoUntil == Timestamps.parse("2026-11-20T12:00:05Z"))
    }

    @Test func detailBuildsNoSentences() throws {
        let file = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources/LifelineKit/Detail.swift")
        let text = try String(contentsOf: file, encoding: .utf8)
        let literals = text.split(separator: "\"").enumerated().filter { $0.offset % 2 == 1 }.map { String($0.element) }
        let sentences = literals.filter { $0.contains(" ") && ($0.hasSuffix(".") || $0.hasSuffix(". ")) }
        #expect(sentences.isEmpty, "\(sentences)")
    }
}
