import AppKit
import Foundation
import SwiftUI
import Testing
@testable import Lifeline
@testable import LifelineKit

/// Remove and Reset: the open row's "Remove from Lifeline" and the settings page's "Reset Lifeline" each open a
/// confirm card with the core's sentence, and only the card's own button sends the change.
@MainActor
@Suite(.serialized)
struct RemoveResetTests {
    init() {
        Fonts.register(folder: URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Resources/Fonts", isDirectory: true))
    }

    private func openRow(_ uuid: String, state: ServiceState, confirming: Bool) throws -> String {
        let account = try #require(state.accounts?.first { $0.uuid == uuid })
        let row = try #require(state.mac?.rows?.first { $0.uuid == uuid })
        return drawnLines(AccountExpansion(
            account: account, row: row, state: state, history: .ready(HistoryReply(daily: [], summary: "")),
            now: Timestamps.parse("2026-11-18T15:05:00Z")!, zone: TimeZone(identifier: "UTC")!, side: .mac, handlers: DetailHandlers(),
            palette: .dark, confirming: confirming
        )).joined(separator: " ")
    }

    @Test func theCoreSendsEachConfirmSentence() throws {
        let state = try Fixture.state("devbox-away")
        let alex = try #require(state.accounts?.first { $0.uuid == Fixture.alex }?.removeText)
        let work = try #require(state.accounts?.first { $0.uuid == Fixture.work }?.removeText)
        #expect(alex.hasSuffix("and Claude Code keeps using it."), "\(alex)")
        #expect(work == "Lifeline removes Work Account, its saved sign-in and its history, here and on Studio.")
        let reset = try #require(try Fixture.state("devbox-away").settingsText?.reset)
        #expect(reset.hasPrefix("Lifeline saves a copy of its history and settings") && reset.hasSuffix("Claude Code keeps the account it uses."))
    }

    @Test func theOpenRowOffersRemoveAndItsCardShowsTheCoresSentence() throws {
        let state = try Fixture.state("devbox-away")
        let sentence = try #require(state.accounts?.first { $0.uuid == Fixture.work }?.removeText)
        let closed = try openRow(Fixture.work, state: state, confirming: false)
        #expect(closed.contains("Remove from Lifeline") && !closed.contains(sentence), "\(closed)")
        let card = try openRow(Fixture.work, state: state, confirming: true)
        #expect(card.contains(sentence), "\(card)")
        #expect(card.contains("Remove") && card.contains("Cancel") && !card.contains("Remove from Lifeline"), "\(card)")
        var older = state
        older.accounts = state.accounts?.map { account in
            var account = account
            account.removeText = nil
            return account
        }
        #expect(!(try openRow(Fixture.work, state: older, confirming: false)).contains("Remove from Lifeline"), "a core without remove")
    }

    @Test func settingsEndsWithResetAndItsCardShowsTheCoresSentence() throws {
        let state = try Fixture.state("devbox-away")
        let sentence = try #require(state.settingsText?.reset)
        let local = LocalStatus(notices: .allowed, shortcut: .on, login: .on)
        let closed = drawnLines(SettingsView(state: state, local: local, problem: nil, width: 560)) { $0.count > 10 }
        #expect(closed.last == "Reset Lifeline", "\(closed.suffix(3))")
        let card = drawnLines(SettingsView(state: state, local: local, problem: nil, width: 560, confirmingReset: true)) { $0.count > 10 }
        let joined = card.joined(separator: " ")
        #expect(joined.contains(sentence) && card.suffix(2) == ["Reset", "Cancel"], "\(card.suffix(6))")
        #expect(!card.contains("Reset Lifeline"))
    }

    @Test func removeAndResetSendTheChangeAndShowTheCoresSentence() async throws {
        let body = try Fixture.text("devbox-away")
        let removed = "Lifeline removed Work Account here and on Studio."
        let refused = "Lifeline is switching this Mac now, so nothing was changed. Try again when the switch ends."
        let back = "Lifeline is back to its first run here and on Studio."
        let server = try FakeServer { head, _ in
            if head.hasPrefix("POST /remove/\(Fixture.work) ") {
                return [okReply(#"{"message": "\#(removed)"}"#)]
            }
            if head.hasPrefix("POST /remove/") {
                return [okReply(#"{"error": "\#(refused)"}"#, status: "409 Conflict")]
            }
            if head.hasPrefix("POST /reset ") {
                return [okReply(#"{"message": "\#(back)"}"#)]
            }
            return [okReply(body)]
        }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        store.showDetail(Fixture.work)
        #expect(await store.remove(Fixture.work) == nil)
        #expect(store.note == removed)
        #expect(store.detail == nil, "the removed account's row stays open")
        #expect(await store.remove(Fixture.personal) == refused)
        store.reset()
        await waitFor { store.settingsProblem == back }
        #expect(store.settingsProblem == back)
        let sent = server.requests.map { $0.head.split(separator: "\r\n").first.map(String.init) ?? "" }.filter { $0.hasPrefix("POST") }
        #expect(sent == ["POST /remove/\(Fixture.work) HTTP/1.1", "POST /remove/\(Fixture.personal) HTTP/1.1", "POST /reset HTTP/1.1"])
    }
}
