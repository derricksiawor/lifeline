import Foundation
import Testing
@testable import LifelineKit

private func sheetState(_ extra: [String: Any] = [:], accounts: [[String: Any]]? = nil, rows: [[String: Any]]? = nil) throws -> ServiceState {
    var object: [String: Any] = [
        "version": 1,
        "mac": ["view": "mac", "rows": rows ?? [["uuid": "j", "name": "Alex's Account", "is_active": true, "where": "Used by this Mac."]], "hero_name": "Alex's Account"],
        "accounts": accounts ?? [["uuid": "j", "number": 1, "name": "Alex's Account", "short": "Alex", "plan": "Max 5x"]],
        "settings": [
            "auto_switch": true, "hide_name": false,
            "alerts": ["will_run_out": true, "limit": false, "back": true, "week": true, "signin_ends": true],
            "channels": ["mac": false]
        ],
        "settings_text": (try? Fixture.object("moment-sheet"))?["settings_text"] ?? [:]
    ]
    for (key, value) in extra {
        object[key] = value
    }
    return try ServiceState.decode(try JSONSerialization.data(withJSONObject: object))
}

private let calm = LocalStatus(notices: .allowed, shortcut: .on, login: .on)

private func core() throws -> SettingsText {
    try #require(try sheetState().settingsText)
}

private func row(_ id: String, in sections: [SettingSection]) -> SettingRow? {
    sections.flatMap(\.rows).first { $0.id == id }
}

@Suite
struct SettingsPageTests {
    @Test func theSettingsDecodeWithSnakeCaseNames() throws {
        let state = try sheetState()
        #expect(state.settings?.alerts?.willRunOut == true)
        #expect(state.settings?.alerts?.limit == false)
        #expect(state.settings?.alerts?.signinEnds == true)
        #expect(state.settings?.channels?.mac == false)
        #expect(state.settings?.autoSwitch == true)
    }

    @Test func theSectionsFollowTheBoard() throws {
        let sections = SettingsPage.sections(state: try sheetState(), local: calm)
        #expect(sections.map(\.title) == ["Your accounts", "Alerts", "Where alerts go", "Switching", "Another computer", "General"])
        #expect(sections[1].rows.map(\.glyph) == [Mark.willRunOut, Mark.limit, Mark.back, Mark.week, Mark.signinEnds])
        #expect(sections[2].rows.map(\.glyph) == [Mark.mac])
        #expect(sections[3].rows.map(\.glyph) == [Mark.autoSwitch, Mark.shortcut])
        #expect(sections[4].rows.map(\.glyph) == [Mark.computer])
        #expect(sections[4].rows[0].control == .computer)
        #expect(sections[5].rows.map(\.glyph) == [Mark.login, Mark.showName, Mark.statusline])
    }

    @Test func eachSwitchShowsTheServicesValue() throws {
        let sections = SettingsPage.sections(state: try sheetState(), local: calm)
        #expect(row("limit", in: sections)?.control == .toggle(.limit, on: false, enabled: true))
        #expect(row("will_run_out", in: sections)?.control == .toggle(.willRunOut, on: true, enabled: true))
        #expect(row("mac", in: sections)?.control == .toggle(.mac, on: false, enabled: true))
        #expect(row("auto_switch", in: sections)?.control == .toggle(.autoSwitch, on: true, enabled: true))
        #expect(row("show_name", in: sections)?.control == .toggle(.showName, on: true, enabled: true))
    }

    @Test func theAlertAndStatusLineSentencesAreTheCoresOwn() throws {
        let sections = SettingsPage.sections(state: try sheetState(), local: calm)
        let text = try core()
        #expect(row("back", in: sections)?.description == text.back?.description)
        #expect(row("week", in: sections)?.description == text.week?.description)
        #expect(row("statusline", in: sections)?.description.hasPrefix(text.statusline?.description ?? "-") == true)
    }

    @Test func theNameAndPercentageAreOffUnlessTheServiceSaysOtherwise() throws {
        let missing = SettingsPage.sections(state: try sheetState(["settings": [String: Any]()]), local: calm)
        #expect(row("show_name", in: missing)?.control == .toggle(.showName, on: false, enabled: true))
        let named = row("show_name", in: SettingsPage.sections(state: try sheetState(), local: calm))
        #expect(named?.title == (try core()).showName?.title)
        #expect(named?.description == (try core()).showName?.description)
        let hidden = SettingsPage.sections(state: try sheetState(["settings": ["hide_name": true]]), local: calm)
        #expect(row("show_name", in: hidden)?.control == .toggle(.showName, on: false, enabled: true))
        let shown = SettingsPage.sections(state: try sheetState(["settings": ["hide_name": false]]), local: calm)
        #expect(row("show_name", in: shown)?.control == .toggle(.showName, on: true, enabled: true))
    }

    @Test func refusedNoticesAndShortcutsAddTheCoresNote() throws {
        let text = try core()
        let local = LocalStatus(notices: .banners, shortcut: .taken, login: .on)
        let sections = SettingsPage.sections(state: try sheetState(), local: local)
        #expect(row("mac", in: sections)?.description == text.mac?.description)
        #expect(row("mac", in: sections)?.note == text.mac?.notes?["banners"])
        #expect(row("shortcut", in: sections)?.description == text.shortcut?.description)
        #expect(row("shortcut", in: sections)?.note == text.shortcut?.notes?["taken"])
        #expect(row("shortcut", in: SettingsPage.sections(state: try sheetState(), local: calm))?.note == nil)
    }

    @Test func startAtLoginFollowsWhereLifelineRuns() throws {
        let notes = (try core()).login?.notes ?? [:]
        func login(_ state: LoginState) throws -> SettingRow? {
            row("login", in: SettingsPage.sections(state: try sheetState(), local: LocalStatus(notices: .allowed, shortcut: .on, login: state)))
        }
        #expect(try login(.notInstalled)?.control == .toggle(nil, on: false, enabled: false))
        #expect(try login(.notInstalled)?.note == notes["not_installed"])
        #expect(try login(.on)?.control == .toggle(nil, on: true, enabled: true))
        #expect(try login(.off)?.control == .toggle(nil, on: false, enabled: true))
        #expect(try login(.needsApproval)?.note == notes["needs_approval"])
        #expect(try login(.failed)?.note == notes["failed"])
        #expect(try login(.on)?.note == nil)
    }

    @Test func theStatusLineIsReadOnlyUntilTheServiceOffersIt() throws {
        let missing = row("statusline", in: SettingsPage.sections(state: try sheetState(), local: calm))
        #expect(missing?.control == SettingControl.none)
        let on = row("statusline", in: SettingsPage.sections(state: try sheetState(["statusline": ["on": true, "ours": true, "other_present": false]]), local: calm))
        #expect(on?.control == .toggle(nil, on: true, enabled: true))
        let other = row("statusline", in: SettingsPage.sections(state: try sheetState(["statusline": ["on": true, "ours": false, "other_present": true]]), local: calm))
        #expect(other?.control == .toggle(nil, on: false, enabled: false))
        let unknown = row("statusline", in: SettingsPage.sections(state: try sheetState(["statusline": ["on": NSNull(), "ours": NSNull(), "other_present": NSNull()]]), local: calm))
        #expect(unknown?.control == SettingControl.none)
    }

    @Test func eachSwitchPostsTheServicesShape() throws {
        func json(_ key: SettingKey, _ on: Bool) throws -> String {
            String(decoding: try JSONSerialization.data(withJSONObject: key.body(on), options: [.sortedKeys]), as: UTF8.self)
        }
        #expect(try json(.willRunOut, false) == #"{"alerts":{"will_run_out":false}}"#)
        #expect(try json(.signinEnds, true) == #"{"alerts":{"signin_ends":true}}"#)
        #expect(try json(.mac, false) == #"{"channels":{"mac":false}}"#)
        #expect(try json(.autoSwitch, false) == #"{"auto_switch":false}"#)
        #expect(try json(.showName, false) == #"{"hide_name":true}"#)
        #expect(try json(.showName, true) == #"{"hide_name":false}"#)
    }

}

@Suite
struct AddFlowTests {
    private func state(_ jobs: [[String: Any]], active: Bool = true) throws -> ServiceState {
        let rows: [[String: Any]] = [["uuid": "j", "name": "Alex's Account", "is_active": active]]
        let object: [String: Any] = [
            "mac": ["view": "mac", "rows": rows, "hero_name": active ? "Alex's Account" : ""],
            "accounts": [["uuid": "j", "name": "Alex's Account", "short": "Alex"]],
            "jobs": jobs,
            "add_text": try Fixture.object(active ? "moment-sheet" : "mac-live-unadded")["add_text"] ?? [:]
        ]
        return try ServiceState.decode(try JSONSerialization.data(withJSONObject: object))
    }

    @Test func theStepFollowsTheJob() throws {
        #expect(AddFlow.step(job: nil, kind: .add, state: try state([])) == .start)
        #expect(AddFlow.step(job: "1", kind: .add, state: try state([])) == .waiting(.add))
        #expect(AddFlow.step(job: "1", kind: .adopt, state: try state([["id": "1", "kind": "adopt", "state": "running"]])) == .waiting(.adopt))
        #expect(AddFlow.step(job: "1", kind: .add, state: try state([["id": "1", "kind": "add", "state": "done", "uuid": "new", "result": "Lifeline added X."]])) == .added("new"))
        #expect(AddFlow.step(job: "1", kind: .add, state: try state([["id": "1", "kind": "add", "state": "failed", "result": "The sign-in was cancelled."]])) == .failed("The sign-in was cancelled."))
        #expect(AddFlow.step(job: "1", kind: .add, state: try state([["id": "1", "kind": "add", "state": "failed", "result": ""]])) == .failed(AddFlow.failed(try state([]))))
        #expect(AddFlow.failed(try state([])) == "Lifeline could not finish adding the account.")
        #expect(AddFlow.step(job: "1", kind: .add, state: try state([["id": "1", "kind": "add", "state": "done", "result": "Odd."]])) == .failed("Odd."))
    }

    @Test func adoptIsOfferedOnlyWhenTheCoreSaysTheMacsSignInIsReadyAndNotAdded() throws {
        #expect(AddFlow.canAdopt(try Fixture.state("mac-live-unadded")))
        #expect(!AddFlow.canAdopt(try Fixture.state("devbox-live-added")))
        #expect(!AddFlow.canAdopt(try state([], active: false)))
        #expect(!AddFlow.canAdopt(nil))
    }

    @Test func theWaitingBoxNamesTheAccountThatStays() throws {
        #expect(AddFlow.stays(try state([])) == "Alex's Account stays signed in and in use on this Mac while you do this.")
        #expect(AddFlow.stays(try state([], active: false)) == nil, "the core sends no stays sentence while no account is in use")
    }

    @Test func theAddedSentencesFollowTheBoard() throws {
        let state = try Fixture.state("after-mac-add-org")
        let text = try #require(state.addText)
        let job = try #require(state.jobs?.first)
        let account = try #require(state.accounts?.first { $0.uuid == job.uuid })
        #expect(AddFlow.added(name: "Team Account", account, state: state, job: job) == "Team Account is added.")
        #expect(AddFlow.added(name: "", account, state: state, job: job) == job.addedHead)
        #expect(AddFlow.addedLine(account, run: nil) == account.addedText?.mac)
        #expect(AddFlow.addedLine(account, run: nil).hasPrefix("It is signed in on this Mac."))
        #expect(SheetText.namesShown(menu: "Team Account", bar: "Team", text: text) == "The menu shows Team Account, and the menu bar shows Team.")
    }
}

@Suite
struct SheetEdgeTests {
    private func jobState(_ jobs: [[String: Any]]) throws -> ServiceState {
        try ServiceState.decode(try JSONSerialization.data(withJSONObject: ["jobs": jobs, "add_text": try Fixture.object("moment-sheet")["add_text"] ?? [:]]))
    }

    @Test func aJobThatDropsOutOfTheStateHasEnded() throws {
        let empty = try jobState([])
        let start = Date(timeIntervalSince1970: 1_000)
        #expect(AddFlow.step(job: "1", kind: .add, state: empty, seen: true, started: start, now: start) == .failed(AddFlow.lost(empty)))
        #expect(AddFlow.step(job: "1", kind: .add, state: empty, seen: false, started: start, now: start.addingTimeInterval(5)) == .waiting(.add))
        #expect(AddFlow.step(job: "1", kind: .add, state: empty, seen: false, started: start, now: start.addingTimeInterval(10.5)) == .failed(AddFlow.lost(empty)))
        let running = try jobState([["id": "1", "kind": "add", "state": "running"]])
        #expect(AddFlow.step(job: "1", kind: .add, state: running, seen: true, started: start, now: start.addingTimeInterval(500)) == .waiting(.add))
        #expect(AddFlow.lost(empty) == "Lifeline lost track of that sign-in, so open the sign-in page again.")
    }

    @Test func onlyAddedAndFailedStepsHaveEnded() {
        #expect(AddFlow.ended(.added("x")))
        #expect(AddFlow.ended(.failed("No.")))
        #expect(!AddFlow.ended(.start))
        #expect(!AddFlow.ended(.waiting(.adopt)))
    }

    @Test func aRenamePostsOnlyWhatChangedAndNeverAnEmptyName() {
        #expect(AddFlow.renameBody(oldName: "A", oldShort: "B", name: "A", short: "B").isEmpty)
        #expect(AddFlow.renameBody(oldName: "A", oldShort: "B", name: " New ", short: "B") == ["name": "New", "short": "B"])
        #expect(AddFlow.renameBody(oldName: "A", oldShort: "B", name: "A", short: "C") == ["name": "A", "short": "C"])
        #expect(AddFlow.renameBody(oldName: "A", oldShort: "B", name: "X", short: "  ") == ["name": "X", "short": "B"])
        #expect(AddFlow.renameBody(oldName: "A", oldShort: "B", name: "", short: "") == [:])
        #expect(AddFlow.renameBody(oldName: "Alexandra", oldShort: "Alex's Account", name: "Alex's Account", short: "Alex") == ["name": "Alex's Account", "short": "Alex"])
        #expect(AddFlow.renameBody(oldName: "Alexandra", oldShort: "", name: "Alex's Account", short: "") == ["name": "Alex's Account", "short": "Alex's Account"])
        #expect(AddFlow.renameBody(oldName: "", oldShort: "", name: " ", short: "J") == [:])
    }

    @Test func placementDefaultsToBothAndReadsTheService() throws {
        let none = try ServiceState.decode(Data(#"{"settings": {}}"#.utf8))
        #expect(Placement.of("j", in: none) == "both")
        #expect(Placement.of("j", in: nil) == "both")
        let set = try ServiceState.decode(Data(#"{"settings": {"placement": {"11111111-aaaa-4aaa-8aaa-000000000001": "mac", "x": "nowhere"}}}"#.utf8))
        #expect(Placement.of("11111111-aaaa-4aaa-8aaa-000000000001", in: set) == "mac")
        #expect(Placement.of("x", in: set) == "both")
    }

    @Test func pressingAChipNeverLeavesAnAccountNowhere() {
        #expect(Placement.pressing("mac", on: "both") == "devbox")
        #expect(Placement.pressing("devbox", on: "both") == "mac")
        #expect(Placement.pressing("mac", on: "mac") == nil)
        #expect(Placement.pressing("devbox", on: "mac") == "both")
        #expect(Placement.pressing("mac", on: "devbox") == "both")
        #expect(Placement.pressing("devbox", on: "devbox") == nil)
    }

    @Test func aPlacementPostsTheAgreedShape() throws {
        let data = try JSONSerialization.data(withJSONObject: Placement.body("j", "devbox"), options: [.sortedKeys])
        #expect(String(decoding: data, as: UTF8.self) == #"{"placement":{"j":"devbox"}}"#)
    }

    @Test func theAccountRowsCarryTheirPlacementOnceTheDevboxAnswers() throws {
        let state = try sheetState(["settings": ["placement": ["j": "devbox"]], "devbox": ["name": "Studio", "reachable": true, "answers": true]])
        let section = SettingsPage.sections(state: state, local: calm)[0]
        #expect(section.rows[0].control == .placement("j", "devbox"))
        #expect(section.note == (try core()).accountsNote)
    }

    @Test func untilTheDevboxAnswersAccountsStayOnThisMacAndTheLeadSaysWhy() throws {
        for devbox in [["name": "Studio", "reachable": false], ["name": "Studio", "reachable": true, "answers": false]] as [[String: Any]] {
            let extra: [String: Any] = ["settings": ["placement": ["j": "devbox"]], "devbox": devbox]
            let section = SettingsPage.sections(state: try sheetState(extra), local: calm)[0]
            #expect(section.rows[0].control == .placementLocked("j", "devbox"))
            #expect(section.note == (try core()).accountsNote, "Studio sentence is repeated under the lead")
        }
        let unpaired = SettingsPage.sections(state: try sheetState(["devbox": ["reachable": true, "answers": true]]), local: calm)[0]
        #expect(unpaired.rows[0].control == SettingControl.none, "with no computer paired there is nowhere else to place an account")
    }

    @Test func theLeadAndTheComputerSentenceAreTheCoresOwn() throws {
        let state = try sheetState()
        #expect(SettingsPage.lead(state) == (try core()).page?.lead)
        #expect(row("computer", in: SettingsPage.sections(state: state, local: calm))?.description == (try core()).computer?.description)
    }

    @Test func useOnThisMacWaitsForASignInAndStaysOutOfTheDevboxView() throws {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let dead = try decoder.decode(AccountSummary.self, from: Data(#"{"uuid": "d", "needs_signin": true}"#.utf8))
        let fine = try decoder.decode(AccountSummary.self, from: Data(#"{"uuid": "f"}"#.utf8))
        #expect(Detail.button(dead, macActive: "other") == .needsSignIn)
        #expect(Detail.button(fine, macActive: "other", side: .dev) == .hidden)
        #expect(Detail.button(fine, macActive: "f", side: .dev) == .hidden)
        #expect(Detail.button(fine, macActive: "other", side: .mac) == .useHere("f"))
    }
}

@Suite
struct AddRunTests {
    private func jobs(_ list: [[String: Any]], devbox: [String: Any]? = nil, active: Bool = true) throws -> ServiceState {
        var object: [String: Any] = [
            "mac": ["view": "mac", "rows": [["uuid": "j", "name": "Alex's Account", "is_active": active]]],
            "accounts": [["uuid": "j", "name": "Alex's Account"], ["uuid": "o", "name": "Team Account", "plan": "Max 5x", "devbox_signed_in": false, "added_text": [
                "mac": "It is signed in on this Mac. It is on the Max 5x plan.",
                "both": "It is signed in on this Mac and on Studio. It is on the Max 5x plan.",
                "devbox": "It is signed in on Studio, and this Mac will not use it. It is on the Max 5x plan."
            ]]],
            "jobs": list,
            "add_text": try Fixture.object("moment-sheet")["add_text"] ?? [:]
        ]
        if let devbox {
            object["devbox"] = devbox
        }
        return try ServiceState.decode(try JSONSerialization.data(withJSONObject: object))
    }

    private let now = Date(timeIntervalSince1970: 5_000)

    @Test func theStagesFollowTheTargetAndKind() throws {
        let plain = try jobs([])
        #expect(AddFlow.stages(target: .mac, kind: .add, state: plain) == [.macAdd])
        #expect(AddFlow.stages(target: .both, kind: .add, state: plain) == [.macAdd, .devboxAdd])
        #expect(AddFlow.stages(target: .devbox, kind: .add, state: plain) == [.macAdd, .devboxAdd])
        let open = try Fixture.state("mac-live-unadded")
        #expect(AddFlow.stages(target: .both, kind: .adopt, state: open) == [.macAdopt, .devboxAdopt])
        #expect(AddFlow.stages(target: .devbox, kind: .adopt, state: open) == [.devboxAdopt])
        #expect(AddFlow.stages(target: .mac, kind: .adopt, state: open) == [.macAdopt])
        #expect(AddFlow.stages(target: .devbox, kind: .adopt, state: try Fixture.state("devbox-away")).isEmpty)
        #expect(AddFlow.stages(target: .both, kind: .adopt, state: try Fixture.state("devbox-work-unadded")).isEmpty)
        #expect(AddFlow.adoptTitle([.macAdopt, .devboxAdopt], state: open) == "Add both machines' own sign-ins")
        #expect(AddFlow.adoptTitle([.devboxAdopt], state: open) == "Add Personal Account from Studio")
        #expect(AddFlow.adoptTitle([], state: open) == nil)
        #expect(AddStage.devboxAdd.route == "/devbox/add" && AddStage.devboxAdopt.route == "/devbox/adopt")
        #expect(AddStage.macAdd.opensBrowser && AddStage.devboxAdd.opensBrowser && !AddStage.devboxAdopt.opensBrowser)
    }

    @Test func bothRunsTheMacFirstThenTheDevboxForTheSameAccount() throws {
        var run = AddRun(target: .both, kind: .add, stages: [.macAdd, .devboxAdd])
        run.began("m", at: now)
        #expect(run.advance(state: try jobs([["id": "m", "state": "running"]]), now: now) == AddAction.none)
        #expect(run.step == .waiting(.add) && run.current == .macAdd)
        let next = run.advance(state: try jobs([["id": "m", "state": "done", "uuid": "o", "result": "Lifeline added Team Account."]]), now: now)
        #expect(next == .start(.devboxAdd))
        #expect(run.body(for: .devboxAdd)["uuid"] as? String == "o")
        #expect(run.body(for: .devboxAdd)["step"] as? [Int] == [2, 2])
        run.began("d", at: now)
        #expect(run.current == .devboxAdd && run.waiting)
        let last = run.advance(state: try jobs([["id": "d", "state": "done", "uuid": "o", "result": "Studio signed in."]]), now: now)
        #expect(last == AddAction.none)
        #expect(run.step == .added("o"))
        let account = try jobs([]).accounts![1]
        #expect(AddFlow.addedLine(account, run: run) == "It is signed in on this Mac and on Studio. It is on the Max 5x plan.")
    }

    @Test func devboxOnlyMarksTheAccountForTheDevbox() throws {
        var run = AddRun(target: .devbox, kind: .add, stages: [.macAdd, .devboxAdd])
        run.began("m", at: now)
        _ = run.advance(state: try jobs([["id": "m", "state": "done", "uuid": "o", "result": "Added."]]), now: now)
        run.began("d", at: now)
        let last = run.advance(state: try jobs([["id": "d", "state": "done", "uuid": "o", "result": "Done."]]), now: now)
        #expect(last == .place("o", "devbox"))
        let account = try jobs([]).accounts![1]
        #expect(AddFlow.addedLine(account, run: run).hasPrefix("It is signed in on Studio, and this Mac will not use it."))
    }

    @Test func aDevboxFailureKeepsTheMacAccountAndSaysWhy() throws {
        var run = AddRun(target: .devbox, kind: .add, stages: [.macAdd, .devboxAdd])
        run.began("m", at: now)
        _ = run.advance(state: try jobs([["id": "m", "state": "done", "uuid": "o", "result": "Added."]]), now: now)
        run.began("d", at: now)
        let last = run.advance(state: try jobs([["id": "d", "state": "failed", "result": "Studio did not answer."]]), now: now)
        #expect(last == AddAction.none)
        #expect(run.step == .added("o"))
        let account = try jobs([]).accounts![1]
        #expect(AddFlow.addedLine(account, run: run) == "It is signed in on this Mac. It is on the Max 5x plan. Studio did not answer.")
    }

    @Test func aRefusedFirstStageEndsTheRun() {
        var run = AddRun(target: .both, kind: .add, stages: [.macAdd, .devboxAdd])
        #expect(run.refused("Lifeline is already adding an account.") == AddAction.none)
        #expect(run.step == .failed("Lifeline is already adding an account."))
        #expect(!run.waiting)
    }

    @Test func aDevboxAdoptAloneEndsOnItsSentence() throws {
        var run = AddRun(target: .devbox, kind: .adopt, stages: [.devboxAdopt])
        run.began("a", at: now)
        _ = run.advance(state: try jobs([["id": "a", "state": "done", "result": "Lifeline took over Studio's own sign-in."]]), now: now)
        #expect(run.step == .done("Lifeline took over Studio's own sign-in."))
        var failed = AddRun(target: .devbox, kind: .adopt, stages: [.devboxAdopt])
        failed.began("a", at: now)
        _ = failed.advance(state: try jobs([["id": "a", "state": "failed", "result": "No."]]), now: now)
        #expect(failed.step == .failed("No."))
    }

    @Test func aStageThatVanishesEndsWithTheLostSentence() throws {
        var run = AddRun(target: .both, kind: .add, stages: [.macAdd, .devboxAdd])
        run.began("m", at: now)
        _ = run.advance(state: try jobs([["id": "m", "state": "running"]]), now: now)
        _ = run.advance(state: try jobs([]), now: now)
        #expect(run.step == .failed(AddFlow.lost(try jobs([]))))
        var never = AddRun(target: .mac, kind: .add, stages: [.macAdd])
        never.began("m", at: now)
        _ = never.advance(state: try jobs([]), now: now.addingTimeInterval(5))
        #expect(never.waiting)
        _ = never.advance(state: try jobs([]), now: now.addingTimeInterval(11))
        #expect(never.step == .failed(AddFlow.lost(try jobs([]))))
    }

    @Test func noStagesMeansNothingToDo() throws {
        let run = AddRun(target: .devbox, kind: .adopt, stages: [])
        #expect(run.step == .failed(AddFlow.unnamedFailure))
        let state = try jobs([])
        #expect(AddFlow.failure(AddFlow.unnamedFailure, state: state) == AddFlow.failed(state) && !AddFlow.failed(state).isEmpty)
        #expect(!run.waiting)
    }

    @Test func theWordsFollowTheTarget() throws {
        let core = try Fixture.state("job-cards")
        #expect(AddFlow.explain(nil) == "", "the app builds no sign-in paragraph of its own")
        #expect(AddFlow.explain(core) == core.addText?.explain)
        #expect(AddFlow.twice(core) == "Studio gets its own sign-in too, so you press Authorize twice.")
        #expect(AddFlow.defaultTarget(try jobs([], devbox: ["reachable": true, "answers": true, "can_sign_in": true])) == .both)
        #expect(AddFlow.defaultTarget(try Fixture.state("devbox-no-login")) == .mac)
        #expect(AddFlow.defaultTarget(try jobs([], devbox: ["reachable": true])) == .mac)
        #expect(AddFlow.defaultTarget(try jobs([], devbox: ["reachable": false])) == .mac)
        let devboxJob = try #require(core.jobs?.first { $0.kind == "devbox-add" })
        #expect(AddFlow.waitingLines(.devboxAdd, state: core, job: devboxJob) == ["Waiting for you to finish the sign-in in Safari.", "This sign-in is for Studio, so choose the same account again."])
        #expect(AddFlow.waitingLines(.devboxAdopt, state: nil).isEmpty)
    }
}
