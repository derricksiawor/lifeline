import Foundation
import Testing
@testable import Lifeline
@testable import LifelineKit

private let utc = TimeZone(identifier: "UTC")!
private let now = Timestamps.parse("2026-11-19T15:05:00Z")!
private let nb = "\u{00A0}"

private func account(_ json: String) throws -> AccountSummary {
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase
    return try decoder.decode(AccountSummary.self, from: Data(json.utf8))
}

private func row(_ json: String) throws -> MenuRow {
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase
    return try decoder.decode(MenuRow.self, from: Data(json.utf8))
}

struct WordingWindowTests {
}

struct WordingSignInTests {
    @Test func datesNeverBreakBetweenDayAndMonth() {
        let far = Timestamps.parse("2026-12-16T10:00:00Z")!
        #expect(Clock.phrase(far, now: now, zone: utc) == "on 16\(nb)December at 10:00")
        #expect(Clock.date(far, now: now, zone: utc) == "16\(nb)December")
        #expect(Clock.date(Timestamps.parse("2026-11-20T10:00:00Z")!, now: now, zone: utc) == "tomorrow")
        #expect(Clock.date(Timestamps.parse("2026-11-23T10:00:00Z")!, now: now, zone: utc) == "Monday")
    }

}

struct WordingPromptTests {
    @Test func eachKnownAccountIsDescribedInAFullSentence() throws {
        let known = try #require(try Fixture.state("devbox-work-unadded").knownAccounts?.first)
        let email = try #require(known.email)
        #expect(AddFlow.pickLine(known) == "It is the personal account for \(email).")
        var named = KnownAccount(name: "Team Account", email: "sam@example.org", organization: "Example Org")
        named.pickLine = "It signs in as sam@example.org and belongs to the Example Org organization."
        #expect(AddFlow.pickLine(named) == named.pickLine)
        #expect(AddFlow.pickLine(KnownAccount(name: "X")) == nil, "no line of the app's own")
    }

    private func state() throws -> ServiceState {
        try ServiceState.decode(Data(#"{"version": 1, "accounts": [{"uuid": "o", "name": "Team Account", "organization": "Example Org"}, {"uuid": "j", "name": "Alex's Account"}], "signin_browser": "Brave Browser", "known_accounts": [{"name": "Team Account", "short": "Team", "full": "Example Org Team Account", "email": "sam@example.org", "organization": "Example Org", "personal": false}, {"name": "Personal Account", "short": "Personal", "email": "sam@example.org", "organization": "personal"}]}"#.utf8))
    }

    @Test func theOrganizationStepNamesTheOrganization() throws {
        let state = try Fixture.state("job-cards")
        for job in state.jobs ?? [] where JobFlow.resignKinds.contains(job.kind ?? "") {
            #expect(JobFlow.cardLines(job, state: state) == (job.cardLines ?? []), "\(job.id)")
            #expect(JobFlow.cardLines(job, state: state).joined().contains("If Claude asks you to choose an organization"), "\(job.id)")
        }
    }

    @Test func aPageThatNeverOpenedIsOpenedNotReopened() {
        #expect(JobFlow.openLabel(JobSummary(id: "1", opened: false)) == "Open the sign-in page")
        #expect(JobFlow.openLabel(JobSummary(id: "1", opened: true)) == "Open the sign-in page again")
    }

    @Test func titlesNameTheAccountAndTheActor() throws {
        for name in ["failed-jobs", "after-mac-add-org", "devbox-work-done"] {
            let state = try Fixture.state(name)
            for job in state.jobs ?? [] {
                #expect(JobFlow.title(job, state: state) == (job.title ?? ""), "\(name) \(job.id)")
                #expect(!JobFlow.title(job, state: state).isEmpty, "an ended job has a title: \(name) \(job.id)")
            }
        }
        for job in try Fixture.state("job-cards").jobs ?? [] {
            #expect(JobFlow.title(job, state: nil).isEmpty, "a running job has no title: \(job.id)")
        }
    }

    @Test func theNamingStepLeadsWithThePrefilledName() throws {
        let state = try Fixture.state("after-mac-add-org")
        let job = try #require(state.jobs?.first)
        let profile = AccountSummary(uuid: "j", name: "Alexandra")
        #expect(AddFlow.added(name: "Alex's Account", profile, state: state, job: job) == "Alex's Account is added.")
        #expect(AddFlow.added(name: " ", profile, state: state, job: job) == job.addedHead)
    }

    @Test func theAddScreenNamesTheBrowserAndThePickedAccount() throws {
        let s = try Fixture.state("devbox-work-unadded")
        let browser = try #require(s.signinBrowser)
        #expect(AddFlow.explain(s).hasPrefix("Lifeline opens Claude's sign-in in \(browser), your default browser."))
        let known = try #require(s.knownAccounts)
        #expect(known.map(\.name) == ["Work Account"])
        let email = try #require(known[0].email)
        #expect(AddFlow.explain(s, pick: known[0]).contains("Sign in there as \(email)."))
        #expect(AddFlow.explain(s, pick: known[0]).contains("choose the one marked Personal"))
        #expect(AddFlow.twice(s) == "Studio gets its own sign-in too, so you press Authorize twice.")
        var run = AddRun(target: .mac, kind: .add, stages: [.macAdd], pick: known[0])
        #expect(run.body(for: .macAdd) as? [String: String] == ["known": "Work Account"])
    }

    @Test func everyWaitingSentenceIsTheCardsSentence() throws {
        let state = try Fixture.state("job-cards")
        let job = try #require(state.jobs?.first { $0.kind == "devbox-add" })
        #expect(AddFlow.waitingLines(.devboxAdd, state: state, job: job).first == job.waiting)
        #expect(AddFlow.waitingLines(.macAdd, state: state, job: nil) == [state.addText?.stays ?? "-"], "no waiting head until the job and its words arrive")
    }

    @Test func theListEndsOnARowBoundary() {
        let heights = MenuLogic.cappedHeights(cards: 0, top: 400, list: 600, footer: 100, limit: 880, spacing: 10, minimum: 120, cardMinimum: 96, listStops: [90, 180, 270, 360, 450, 540, 600])
        #expect(heights[2] == 360)
        let roomy = MenuLogic.cappedHeights(cards: 0, top: 200, list: 300, footer: 100, limit: 879, spacing: 10, minimum: 120, cardMinimum: 96, listStops: [100, 200, 300])
        #expect(roomy[2] == 300)
    }
}

struct WordingSettingsTests {
    private func sections(_ fixture: String) throws -> [SettingSection] {
        SettingsPage.sections(state: try Fixture.state(fixture), local: LocalStatus(notices: .allowed, shortcut: .on, login: .on))
    }

    @Test func theDevboxRowsAreTrueTonight() throws {
        let away = try Fixture.state("moment-sheet-away")
        let rows = try sections("moment-sheet-away").flatMap(\.rows)
        #expect(rows.first { $0.id == "mac" }?.description == away.settingsText?.mac?.description)
        #expect(rows.first { $0.id == "computer" }?.description == away.settingsText?.computer?.description)
        let here = try Fixture.state("moment-sheet")
        #expect(SettingsPage.lead(here) == here.settingsText?.page?.lead)
    }

    @Test func everyRowSaysTheCoresWords() throws {
        let state = try Fixture.state("moment-sheet")
        let rows = try sections("moment-sheet").flatMap(\.rows)
        let text = try #require(state.settingsText)
        #expect(rows.first { $0.id == "shortcut" }?.description == text.shortcut?.description)
        #expect(rows.first { $0.id == "signin_ends" }?.description == text.signinEnds?.description)
        #expect(rows.first { $0.id == "show_name" }?.description == text.showName?.description)
        #expect(rows.first { $0.id == "will_run_out" }?.description == text.willRunOut?.description)
        #expect(rows.allSatisfy { !$0.title.isEmpty && !$0.description.isEmpty }, "\(rows.filter { $0.title.isEmpty || $0.description.isEmpty }.map(\.id))")
    }

    @Test func accountRowsReadNaturallyAndCarryNoTile() throws {
        let state = try Fixture.state("moment-sheet")
        let account = try #require(try sections("moment-sheet").first?.rows.first)
        #expect(account.glyph == "")
        #expect(account.description == state.accounts?.first?.detail?.account, "\(account.description)")
        #expect(!account.description.contains("Press 1"), "the key sentence belongs to the section note")
    }

    @Test func eachRowHasItsOwnDrawnMark() throws {
        let rows = try sections("moment-sheet").dropFirst().flatMap(\.rows)
        let marks = rows.map(\.glyph)
        #expect(marks.allSatisfy { $0.count > 2 }, "\(marks)")
        #expect(Set(marks).count == marks.count, "\(marks)")
    }
}

struct WordingNoticeTests {
    @Test func switchBackUsesTheMenusName() {
        #expect(Notices.backTitle("Alex's Account") == "Switch back to Alex's Account")
    }

    @Test func listOnlyNotificationsAreNotCalledOff() throws {
        let note = try #require(try Fixture.state("moment-sheet").settingsText?.mac?.notes?["list_only"])
        #expect(SettingsPage.noteKey(NoticePermission.listOnly) == "list_only")
        #expect(!note.contains(" off"), "\(note)")
    }

    @Test func anUndeliveredAlertStaysAsACardUntilSeen() throws {
        let s = try ServiceState.decode(Data(#"{"version": 1, "notifications": [{"id": "n1", "kind": "limit", "title": "Alex's Account reached its 5 hours", "body": "Lifeline could not send this alert by email.", "keep": true, "at": "2026-11-19T08:00:00Z"}, {"id": "n2", "kind": "limit", "title": "Delivered", "keep": false}]}"#.utf8))
        #expect(JobFlow.alertCards(s, seen: []).map(\.id) == ["n1"])
        #expect(JobFlow.alertCards(s, seen: ["notice-n1"]).isEmpty)
    }

    @Test func theCoresFinalFieldsDecodeAndDrive() throws {
        let s = try ServiceState.decode(Data(#"""
        {"version": 1, "signin_browser": "Safari",
         "mac": {"view": "mac", "countdown_left": 4, "devbox_note": "Studio needs you. Work Account runs out today at 16:20.",
                 "head": "Switching this Mac to Team Account in 4 seconds.",
                 "rows": [{"uuid": "j", "name": "Alex's Account", "pct": "42%", "pct_window": "5 hours", "pct_label": "5 hours 42%", "week_pct": "Week 18%"},
                          {"uuid": "c", "name": "Work Account", "pct": "94%", "pct_window": "week", "pct_label": "Week 94%", "week_pct": ""}]},
         "accounts": [{"uuid": "c", "name": "Work Account", "full": "Example Team's Account", "organization": "Example Org", "organization_kind": "Example Org"},
                      {"uuid": "p", "name": "Personal Account", "organization": "sam@example.org's Organization", "organization_kind": "personal"}],
         "known_accounts": [{"name": "Team Account", "short": "Team", "full": "Example Org Team Account", "email": "sam@example.org", "organization": "Example Org"},
                            {"name": "Personal Account", "short": "Personal", "full": "Example Org Personal Account", "email": "sam@example.org", "organization": "personal"}],
         "jobs": [{"id": "a", "kind": "add", "state": "running", "known": "Team Account", "organization": "Example Org",
                   "guidance": "If Claude asks you to choose an organization, choose Example Org, then press Authorize."},
                  {"id": "r", "kind": "resign", "uuid": "c", "state": "running",
                   "guidance": "If Claude asks you to choose an organization, choose Example Team, then press Authorize."}]}
        """#.utf8))
        let rows = try #require(s.mac?.rows)
        #expect(rows[0].pctLabel == "5 hours 42%" && rows[0].pctWindow == "5 hours" && rows[0].weekPct == "Week 18%")
        #expect(rows[1].pctLabel == "Week 94%")
        #expect(s.mac?.countdownLeft == 4)
        #expect(s.accounts?[1].organizationKind == "personal")
        let resign = try #require(s.jobs?.last)
        #expect(resign.guidance == "If Claude asks you to choose an organization, choose Example Team, then press Authorize.")
        let personal = try #require(s.knownAccounts?.last)
        #expect(personal.isPersonal)
        let run = AddRun(target: .mac, kind: .add, stages: [.macAdd], pick: s.knownAccounts?.first)
        #expect(run.body(for: .macAdd) as? [String: String] == ["known": "Team Account"])
        #expect(MenuLogic.countdownLeft(nil, fixed: nil, now: Date(), left: s.mac?.countdownLeft) == 4)
        #expect(MenuLogic.counted(s.mac?.head, secondsLeft: nil) == "Switching this Mac to Team Account in 4 seconds.")
    }

    @Test func theDevboxNoteDecodes() throws {
        let s = try ServiceState.decode(Data(#"{"version": 1, "mac": {"view": "mac", "icon_dot": true, "devbox_note": "Studio needs you. Work Account runs out today at 16:20."}}"#.utf8))
        #expect(s.mac?.devboxNote == "Studio needs you. Work Account runs out today at 16:20.")
    }
}
