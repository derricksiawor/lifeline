import Foundation
import Testing
@testable import Lifeline
@testable import LifelineKit

@Suite(.serialized)
struct RouteLimitTests {
    @Test func switchUndoAndCancelWaitLongerThanEverythingElse() throws {
        let server = try FakeServer { _, _ in
            Thread.sleep(forTimeInterval: 0.6)
            return [okReply(#"{"message": "This Mac now uses Team Account.", "note": "Lifeline kept a spare copy of the old sign-in."}"#)]
        }
        defer { server.close() }
        let api = LifelineAPI(client: SocketClient(path: server.path, limit: 0.3), switchLimit: 1.5)
        #expect(api.perform(.switchTo("o"), via: .row, names: ["o": "Team Account"]) == .done(note: "Lifeline kept a spare copy of the old sign-in."))
        #expect(api.perform(.undo, via: .menuUndo) != .refused(Sentences.timedOut))
        #expect(api.perform(.cancel, via: .footer) != .refused(Sentences.timedOut))
        #expect(api.perform(.resign("o"), via: .row) == .refused(Sentences.timedOut))
        #expect(throws: SocketError.timedOut) { try api.fetchState(appearance: "dark") }
        #expect(throws: SocketError.timedOut) { try SocketClient(path: server.path, limit: 0.3).request(method: "GET", target: "/state") }
    }

    @Test func aSwitchPastItsOwnLimitSaysThirtySeconds() throws {
        let server = try FakeServer { _, _ in
            Thread.sleep(forTimeInterval: 0.6)
            return [okReply(#"{"message": "ok"}"#)]
        }
        defer { server.close() }
        let api = LifelineAPI(client: SocketClient(path: server.path, limit: 0.3), switchLimit: 0.3)
        #expect(api.perform(.switchTo("o"), via: .key) == .refused(Sentences.slowSwitch))
        #expect(Sentences.slowSwitch == "Lifeline did not answer within 30 seconds, so the result is not known yet.")
        #expect(Sentences.unknownResult(Sentences.slowSwitch) && Sentences.unknownResult(Sentences.timedOut))
    }

    @Test func theLimitsAreThirtyAndTwoSeconds() {
        let api = LifelineAPI(client: SocketClient(path: "/nowhere"))
        #expect(api.client.limit == 2)
        #expect(api.limit(for: .switchTo("x")) == 30)
        #expect(api.limit(for: .undo) == 30)
        #expect(api.limit(for: .cancel) == 30)
        #expect(api.limit(for: .resign("x")) == nil)
    }
}

@Suite
struct SwitchNoteTests {
    private func state(_ note: String?) throws -> ServiceState {
        var object: [String: Any] = ["version": 1]
        object["switch_note"] = note ?? NSNull()
        return try ServiceState.decode(try JSONSerialization.data(withJSONObject: object))
    }

    @Test func aNewSwitchNoteIsShownOnce() throws {
        let note = "Lifeline kept a spare copy of the old sign-in."
        #expect(MenuLogic.freshSwitchNote(try state(note), last: nil, showing: nil) == note)
        #expect(MenuLogic.freshSwitchNote(try state(note), last: note, showing: nil) == nil)
        #expect(MenuLogic.freshSwitchNote(try state(note), last: nil, showing: "This was said. " + note) == nil)
        #expect(MenuLogic.freshSwitchNote(try state(nil), last: nil, showing: nil) == nil)
        #expect(MenuLogic.freshSwitchNote(try state("  "), last: nil, showing: nil) == nil)
    }

    @Test func switchAndUndoKeepTheirNote() {
        #expect(MenuLogic.keepsNote(.switchTo("x")) && MenuLogic.keepsNote(.undo))
        #expect(!MenuLogic.keepsNote(.cancel) && !MenuLogic.keepsNote(.resign("x")))
        #expect(MenuLogic.joinNotes("A.", "B.") == "A. B.")
        #expect(MenuLogic.joinNotes("A.", "A.") == "A.")
        #expect(MenuLogic.joinNotes(nil, " ") == nil)
    }
}

@Suite
struct SmallTests {
    @Test func theMacChannelRowSaysNotify() throws {
        let rows = SettingsPage.sections(state: try Fixture.state("moment-sheet"), local: LocalStatus(notices: .allowed, shortcut: .on, login: .on))[2].rows
        #expect(rows.map(\.title) == ["Notify me on this Mac while I am using it"])
    }

    @Test func withNoServiceThePageOffersNoWordlessRows() {
        #expect(SettingsPage.sections(state: nil, local: LocalStatus(notices: .allowed, shortcut: .on, login: .on)).isEmpty)
        #expect(SettingsPage.lead(nil) == Sentences.notRunning)
    }

    @Test func devboxAdoptFollowsAdoptReadyAndHeld() throws {
        #expect(AddFlow.canAdoptDevbox(try Fixture.state("devbox-live-added")))
        for name in ["devbox-live-held", "devbox-live-unready", "devbox-work-unadded", "devbox-away", "devbox-gone-stale", "devbox-job-elsewhere"] {
            #expect(!AddFlow.canAdoptDevbox(try Fixture.state(name)), "\(name)")
        }
    }

    @Test func theServiceRunsWithoutWritingBytecode() {
        #expect(ServiceSupervisor.arguments("/Applications/Lifeline.app/Contents/Resources/core/bin/lifeline")
            == ["python3", "-I", "-B", "/Applications/Lifeline.app/Contents/Resources/core/bin/lifeline", "serve"])
    }
}

@Suite(.serialized)
@MainActor
struct SwitchNoteStoreTests {
    @Test func theStatesSwitchNoteBecomesAKeptNote() async throws {
        let note = Flag()
        let server = try FakeServer { _, _ in
            [okReply(note.value ? #"{"version": 1, "switch_note": "Lifeline kept a spare copy of the old sign-in."}"# : #"{"version": 1}"#)]
        }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        #expect(store.note == "Lifeline kept a spare copy of the old sign-in.")
        store.opened()
        #expect(store.note == "Lifeline kept a spare copy of the old sign-in.")
        await store.refresh()
        store.opened()
        #expect(store.note == nil)
        note.value = false
        await store.refresh()
        note.value = true
        await store.refresh()
        #expect(store.note == "Lifeline kept a spare copy of the old sign-in.")
    }

    @Test func aSwitchReplysNoteIsKept() async throws {
        let server = try FakeServer { head, _ in
            head.hasPrefix("POST /switch")
                ? [okReply(#"{"message": "This Mac now uses Team Account.", "note": "Lifeline kept a spare copy of the old sign-in."}"#)]
                : [okReply(#"{"version": 1, "accounts": [{"uuid": "o", "name": "Team Account"}]}"#)]
        }
        defer { server.close() }
        let store = StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil)
        await store.refresh()
        store.run(.switchTo("o"), via: .row)
        await waitFor { store.note != nil }
        store.opened()
        #expect(store.note == "Lifeline kept a spare copy of the old sign-in.")
    }
}

@Suite
struct ServicePathTests {
    let resources = "/home/Sam/Applications/Lifeline.app/Contents/Resources"

    @Test func aRelativeValueResolvesInsideTheRunningBundle() {
        #expect(ServicePath.resolve("core/bin/lifeline", resources: resources) == resources + "/core/bin/lifeline")
        #expect(ServicePath.resolve("core//bin/./lifeline", resources: resources) == resources + "/core/bin/lifeline")
        #expect(ServicePath.resolve("core/bin/lifeline", resources: resources + "/") == resources + "/core/bin/lifeline")
    }

    @Test func aRelativeValueNeverLeavesTheBundle() {
        #expect(ServicePath.resolve("../core/bin/lifeline", resources: resources) == nil)
        #expect(ServicePath.resolve("core/../../MacOS/Lifeline", resources: resources) == nil)
        #expect(ServicePath.resolve("core/bin/..", resources: resources) == nil)
        #expect(ServicePath.resolve("..", resources: resources) == nil)
        #expect(ServicePath.resolve(".", resources: resources) == nil)
        #expect(ServicePath.resolve("./", resources: resources) == nil)
        #expect(ServicePath.resolve("core/bin/lifeline", resources: nil) == nil)
        #expect(ServicePath.resolve("core/bin/lifeline", resources: "relative/Resources") == nil)
    }

    @Test func anAbsoluteValueIsUsedAsItIsForDevelopmentBuilds() {
        #expect(ServicePath.resolve("/home/Sam/projects/lifeline/bin/lifeline", resources: resources) == "/home/Sam/projects/lifeline/bin/lifeline")
        #expect(ServicePath.resolve("/x", resources: nil) == "/x")
    }

    @Test func noValueMeansNoService() {
        #expect(ServicePath.resolve(nil, resources: resources) == nil)
        #expect(ServicePath.resolve("", resources: resources) == nil)
    }
}

@Suite
struct BindingTests {
    private func row(_ json: String) throws -> MenuRow {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(MenuRow.self, from: Data(json.utf8))
    }

    @Test func aWeekBindingRowDrawsItsMainLineAndNoSecondLine() throws {
        let week = try row(#"{"uuid": "p", "binding": "week", "pct": "57%", "five_w": "57%", "week_pct": "", "week_w": "0%"}"#)
        #expect(week.fiveW == "57%")
        #expect(!week.showsSecondLine)
        let five = try row(#"{"uuid": "j", "binding": "five", "pct": "42%", "five_w": "42%", "week_pct": "Week 18%", "week_w": "18%"}"#)
        #expect(five.fiveW == "42%" && five.showsSecondLine)
        let today = try row(#"{"uuid": "j", "five_w": "42%", "week_w": "20%"}"#)
        #expect(today.showsSecondLine)
    }

    @Test func aWeekBindingHeroUsesTheMainFieldsAndNoWeekLine() throws {
        let state = try ServiceState.decode(Data(#"""
        {"mac": {"binding": "week", "hero_w": "57%", "hero_used": "57% used", "week_w": "0%", "week_text": "", "icon_pct": "57%", "icon_w": "57%", "icon_binding": "week"},
         "dev": {"binding": "five", "hero_w": "40%", "week_w": "10%", "week_text": "This week: 10% used."}}
        """#.utf8))
        #expect(state.mac?.heroW == "57%")
        #expect(state.mac?.showsWeekLine == false)
        #expect(state.mac?.iconBinding == "week")
        #expect(IconFields.from(state).percent == "57%")
        #expect(IconFields.from(state).fill == 0.57)
        #expect(state.dev?.showsWeekLine == true)
        let none = try ServiceState.decode(Data(#"{"mac": {"binding": "", "hero_w": "0%"}}"#.utf8))
        #expect(none.mac?.showsWeekLine == true)
    }

}

@Suite
struct ClockTests {
    private let utc = TimeZone(identifier: "UTC")!

    @Test func timesRoundToTheNearestMinute() {
        let late = Timestamps.parse("2026-11-19T10:59:59.6Z")!
        #expect(Clock.text(late, "HH:mm", utc) == "11:00")
        #expect(Clock.text(Timestamps.parse("2026-11-19T10:59:29Z")!, "HH:mm", utc) == "10:59")
        #expect(Clock.text(Timestamps.parse("2026-11-19T10:59:30Z")!, "HH:mm", utc) == "11:00")
        #expect(Clock.text(Timestamps.parse("2026-11-22T23:59:45Z")!, "EEEE", utc) == "Monday")
        #expect(Clock.text(Timestamps.parse("2026-11-30T23:59:50Z")!, "d MMMM", utc) == "1 December")
    }

    private func at(_ text: String) -> Date { Timestamps.parse(text)! }

    @Test func dayPhrasesCountCalendarDaysNotHours() {
        let now = at("2026-11-19T16:35:00Z")
        func phrase(_ text: String) -> String { Clock.phrase(at(text), now: now, zone: utc) }
        func until(_ text: String) -> String { Clock.until(at(text), now: now, zone: utc) }
        #expect(phrase("2026-11-26T02:00:00Z") == "next Thursday at 02:00")
        #expect(phrase("2026-11-20T01:00:00Z") == "tomorrow at 01:00")
        #expect(phrase("2026-11-19T16:35:00Z") == "today at 16:35")
        #expect(phrase("2026-11-19T23:59:29Z") == "today at 23:59")
        #expect(phrase("2026-11-19T23:59:30Z") == "tomorrow at 00:00")
        #expect(phrase("2026-11-20T00:00:00Z") == "tomorrow at 00:00")
        #expect(phrase("2026-11-20T23:59:00Z") == "tomorrow at 23:59")
        #expect(phrase("2026-11-20T23:59:59.6Z") == "on Saturday at 00:00")
        #expect(phrase("2026-11-21T00:00:00Z") == "on Saturday at 00:00")
        #expect(phrase("2026-11-25T23:59:00Z") == "on Wednesday at 23:59")
        #expect(phrase("2026-11-25T23:59:30Z") == "next Thursday at 00:00")
        #expect(phrase("2026-11-26T00:00:00Z") == "next Thursday at 00:00")
        #expect(phrase("2026-11-26T23:59:00Z") == "next Thursday at 23:59")
        #expect(phrase("2026-11-26T23:59:30Z") == "on 27\u{00A0}November at 00:00")
        #expect(phrase("2026-11-27T00:00:00Z") == "on 27\u{00A0}November at 00:00")
        #expect(phrase("2026-12-01T09:00:00Z") == "on 1\u{00A0}December at 09:00")
        #expect(until("2026-11-19T18:00:00Z") == "today at 18:00")
        #expect(until("2026-11-20T01:00:00Z") == "tomorrow at 01:00")
        #expect(until("2026-11-21T00:00:00Z") == "Saturday at 00:00")
        #expect(until("2026-11-26T02:00:00Z") == "next Thursday at 02:00")
        #expect(until("2026-11-27T00:00:00Z") == "27\u{00A0}November at 00:00")
    }

    @Test func dayPhrasesHoldAtMidnightAndAcrossMonthsAndYears() {
        func phrase(_ text: String, from now: String, _ zone: TimeZone? = nil) -> String {
            Clock.phrase(at(text), now: at(now), zone: zone ?? utc)
        }
        #expect(phrase("2026-11-19T23:59:00Z", from: "2026-11-19T00:00:00Z") == "today at 23:59")
        #expect(phrase("2026-11-20T00:00:00Z", from: "2026-11-19T00:00:00Z") == "tomorrow at 00:00")
        #expect(phrase("2026-11-20T00:00:00Z", from: "2026-11-19T23:59:59Z") == "tomorrow at 00:00")
        #expect(phrase("2026-11-20T00:00:20Z", from: "2026-11-20T00:00:00Z") == "today at 00:00")
        #expect(phrase("2026-11-26T23:59:00Z", from: "2026-11-19T00:00:00Z") == "next Thursday at 23:59")
        #expect(phrase("2026-12-01T00:00:00Z", from: "2026-11-30T23:00:00Z") == "tomorrow at 00:00")
        #expect(phrase("2026-12-07T08:00:00Z", from: "2026-11-30T23:00:00Z") == "next Monday at 08:00")
        #expect(phrase("2026-12-08T08:00:00Z", from: "2026-11-30T23:00:00Z") == "on 8\u{00A0}December at 08:00")
        #expect(phrase("2027-01-01T09:00:00Z", from: "2026-12-31T20:00:00Z") == "tomorrow at 09:00")
        #expect(phrase("2027-01-04T09:00:00Z", from: "2026-12-28T20:00:00Z") == "next Monday at 09:00")
        #expect(phrase("2027-01-05T09:00:00Z", from: "2026-12-28T20:00:00Z") == "on 5\u{00A0}January at 09:00")
        let tokyo = TimeZone(identifier: "Asia/Tokyo")!
        #expect(phrase("2026-11-20T14:00:00Z", from: "2026-11-19T16:35:00Z", tokyo) == "today at 23:00")
        #expect(phrase("2026-11-20T15:00:00Z", from: "2026-11-19T16:35:00Z", tokyo) == "tomorrow at 00:00")
        let london = TimeZone(identifier: "Europe/London")!
        #expect(phrase("2027-03-28T23:30:00Z", from: "2027-03-27T12:00:00Z", london) == "on Monday at 00:30")
        #expect(phrase("2027-03-28T22:30:00Z", from: "2027-03-27T12:00:00Z", london) == "tomorrow at 23:30")
    }

    @Test func serveMustFollowTheLifelineCommand() {
        #expect(ServiceIdentity.looksLikeService(["python3", "-I", "-B", "/a/bin/lifeline", "serve"]))
        #expect(!ServiceIdentity.looksLikeService(["python3", "serve", "/a/bin/lifeline", "status"]))
        #expect(!ServiceIdentity.looksLikeService(["python3", "/a/bin/lifeline"]))
        #expect(!ServiceIdentity.looksLikeService(["python3", "/a/bin/lifeline", "status", "serve"]))
    }
}

@Suite
struct NothingOpensByItselfTests {
    private func row(_ uuid: String, active: Bool) throws -> MenuRow {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(MenuRow.self, from: Data(#"{"uuid": "\#(uuid)", "name": "A", "is_active": \#(active)}"#.utf8))
    }

    private func appSources() throws -> String {
        let tests = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
        let folder = tests.deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("Sources/Lifeline")
        let names = try FileManager.default.contentsOfDirectory(atPath: folder.path).filter { $0.hasSuffix(".swift") }
        #expect(names.count > 10)
        return try names.map { try String(contentsOf: folder.appendingPathComponent($0), encoding: .utf8) }.joined(separator: "\n")
    }

    @Test func hoverOpensNothing() throws {
        let source = try appSources()
        #expect(!source.contains("onHover"))
        #expect(!source.contains("onContinuousHover"))
        #expect(!source.contains("asyncAfter"))
        #expect(!source.contains("hoverDelay"))
        #expect(!source.contains("mouseEntered"))
    }

    @Test func aClickOnTheRowInUseExpandsIt() throws {
        #expect(MenuLogic.rowClick(try row("j", active: true), side: .mac, secondary: false) == .detail("j"))
        #expect(MenuLogic.rowClick(try row("c", active: true), side: .dev, secondary: false) == .detail("c"))
    }

    @Test @MainActor func aRightClickOnAnotherRowExpandsItWithoutSwitching() throws {
        #expect(MenuLogic.rowClick(try row("o", active: false), side: .mac, secondary: true) == .detail("o"))
        #expect(MenuLogic.rowClick(try row("j", active: true), side: .mac, secondary: true) == .detail("j"))
        #expect(SecondaryClick.catches(.rightMouseDown, flags: []))
        #expect(SecondaryClick.catches(.leftMouseDown, flags: .control))
        #expect(!SecondaryClick.catches(.leftMouseDown, flags: []))
        #expect(!SecondaryClick.catches(.mouseMoved, flags: []))
        #expect(!SecondaryClick.catches(nil, flags: .control))
    }

    @Test func aPlainClickOnAnotherRowSwitchesAndDoesNotExpand() throws {
        #expect(MenuLogic.rowClick(try row("o", active: false), side: .mac, secondary: false) == .run(.switchTo("o")))
        #expect(MenuLogic.rowClick(try row("o", active: false), side: .dev, secondary: false) == .detail("o"))
        #expect(MenuLogic.rowClick(try row("", active: false), side: .mac, secondary: true) == .nothing)
    }

    @Test func escapeCollapsesTheRowBeforeClosingTheMenu() {
        #expect(MenuLogic.keyAction(keyCode: 53, characters: nil, shift: false, command: false, option: false, control: false, state: nil, detailOpen: true) == .detail(.close))
        #expect(MenuLogic.keyAction(keyCode: 53, characters: nil, shift: false, command: false, option: false, control: false, state: nil, detailOpen: false) == .close)
    }

    @Test func onlyOneRowIsExpandedAtATime() {
        #expect(MenuLogic.expanded(after: "a", current: nil) == "a")
        #expect(MenuLogic.expanded(after: "b", current: "a") == "b")
        #expect(MenuLogic.expanded(after: "b", current: "b") == nil)
    }

    @Test func theMenusLeftEdgeNeverMoves() {
        let collapsed = MenuLogic.menuOrigin(left: 900, top: 980, height: 700, shadow: 56)
        let expanded = MenuLogic.menuOrigin(left: 900, top: 980, height: 1100, shadow: 56)
        #expect(collapsed.x == expanded.x)
        #expect(collapsed.x == 844)
        #expect(expanded.y == 980 - 1100)
        #expect(collapsed.y + 700 == expanded.y + 1100)
    }

    @Test func eachRowClickReachesOnlyItsOwnHandler() throws {
        var ran: [MenuCommand] = []
        var vias: [Via] = []
        var opened: [String] = []
        let handlers = MenuHandlers(
            run: { command, via in
                ran.append(command)
                vias.append(via)
            },
            detail: { opened.append($0) }
        )
        handlers.perform(.run(.switchTo("o")))
        #expect(ran == [.switchTo("o")] && opened.isEmpty)
        #expect(vias == [.row])
        handlers.perform(.detail("j"))
        #expect(ran == [.switchTo("o")] && opened == ["j"])
        handlers.perform(.nothing)
        #expect(ran.count == 1 && opened.count == 1)
    }
}

@Suite
struct ExpansionTextTests {
    private let now = Timestamps.parse("2026-11-19T16:35:00Z")!
    private let utc = TimeZone(identifier: "UTC")!

    private func decode<T: Decodable>(_ json: String) throws -> T {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(T.self, from: Data(json.utf8))
    }

    @Test func aClosedRowShowsItsSecondWindowOnlyWhenItMatters() throws {
        let row: MenuRow = try decode(#"{"uuid": "c", "second_text": "It has used 70% of its 5 hours.", "second_w": "70%", "second_window": "five"}"#)
        func account(_ json: String) throws -> AccountSummary { try decode(json) }
        let calm = try account(#"{"uuid": "c", "binding": "week", "five": 40, "five_reset": "2026-11-19T17:30:00Z", "week": 94, "week_reset": "2026-11-22T09:00:00Z"}"#)
        #expect(!Detail.secondInRow(row, account: calm, now: now))
        let full = try account(#"{"uuid": "c", "binding": "week", "five": 85, "five_reset": "2026-11-19T17:30:00Z", "week": 94, "week_reset": "2026-11-22T09:00:00Z"}"#)
        #expect(Detail.secondInRow(row, account: full, now: now))
        let first = try account(#"""
        {"uuid": "c", "binding": "week", "five": 40, "five_reset": "2026-11-19T17:30:00Z", "week": 94, "week_reset": "2026-11-22T09:00:00Z",
         "forecast": {"runs_out_at": "2026-11-20T21:00:00Z"}, "other_forecast": {"runs_out_at": "2026-11-19T17:00:00Z"}}
        """#)
        #expect(Detail.secondInRow(row, account: first, now: now))
        let later = try account(#"""
        {"uuid": "c", "binding": "week", "five": 40, "five_reset": "2026-11-19T17:30:00Z", "week": 94, "week_reset": "2026-11-22T09:00:00Z",
         "forecast": {"runs_out_at": "2026-11-19T16:50:00Z"}, "other_forecast": {"runs_out_at": "2026-11-19T17:00:00Z"}}
        """#)
        #expect(!Detail.secondInRow(row, account: later, now: now))
        let mainNone = try account(#"""
        {"uuid": "c", "binding": "week", "five": 40, "five_reset": "2026-11-19T17:30:00Z", "week": 94, "week_reset": "2026-11-22T09:00:00Z",
         "other_forecast": {"runs_out_at": "2026-11-19T17:00:00Z"}}
        """#)
        #expect(Detail.secondInRow(row, account: mainNone, now: now))
        let passed = try account(#"""
        {"uuid": "c", "binding": "week", "five": 40, "five_reset": "2026-11-19T17:30:00Z", "week": 94, "week_reset": "2026-11-22T09:00:00Z",
         "other_forecast": {"runs_out_at": "2026-11-19T16:00:00Z"}}
        """#)
        #expect(!Detail.secondInRow(row, account: passed, now: now))
        let noSecond: MenuRow = try decode(#"{"uuid": "c"}"#)
        #expect(!Detail.secondInRow(noSecond, account: full, now: now))
        let high: MenuRow = try decode(#"{"uuid": "c", "second_text": "It has used 90% of its week.", "second_w": "90%", "second_window": "week"}"#)
        #expect(Detail.secondInRow(high, account: nil, now: now))
    }

    @Test func theNewStateFieldsDecodeAndMissingMeansNone() throws {
        let state = try ServiceState.decode(Data(#"""
        {"mac": {"second_text": "It has used 18% of its week.", "second_w": "18%", "second_window": "week",
                 "rows": [{"uuid": "a", "second_text": "", "second_w": "0%", "second_window": ""}, {"uuid": "b"}]},
         "accounts": [{"uuid": "a", "other_forecast": {"at_reset_pct": 40}}, {"uuid": "b", "other_forecast": null}]}
        """#.utf8))
        #expect(state.mac?.hasSecond == true)
        #expect(state.mac?.showsWeekLine == false)
        #expect(state.mac?.rows?[0].hasSecond == false)
        #expect(state.mac?.rows?[1].hasSecond == false)
        #expect(state.mac?.rows?[1].showsSecondLine == true)
        #expect(state.accounts?[0].otherForecast?.atResetPct == 40)
        #expect(state.accounts?[1].otherForecast == nil)
        let old = try ServiceState.decode(Data(#"{"mac": {"week_w": "18%"}}"#.utf8))
        #expect(old.mac?.hasSecond == false && old.mac?.showsWeekLine == true)
    }
}
