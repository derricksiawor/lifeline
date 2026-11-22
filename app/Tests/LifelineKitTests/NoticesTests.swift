import Foundation
import Testing
@testable import LifelineKit

private let personal = solo
private let org = crew

private func noticeState(
    notices: [[String: Any]],
    head: String = "Alex's Account runs out at 15:40.",
    sub: String = "That is 1 hour 20 minutes before it resets at 17:00. Team Account has the most room.",
    countdown: Bool = false,
    actionKind: String = "primary",
    best: String? = org
) throws -> ServiceState {
    let names = [(alex, "Alex's Account"), (work, "Work Account"), (personal, "Personal Account"), (org, "Team Account")]
    let shorts = [alex: "Alex", work: "Work", personal: "Personal", org: "Team"]
    let rows: [[String: Any]] = names.map { item in
        ["uuid": item.0, "name": item.1, "is_active": item.0 == alex, "is_best": item.0 == best, "needs_signin": false]
    }
    let accounts: [[String: Any]] = names.enumerated().map { index, item in
        ["uuid": item.0, "name": item.1, "short": shorts[item.0] ?? "", "number": index + 1, "in_use": item.0 == alex ? ["mac"] : (item.0 == work ? ["devbox"] : ["idle"])]
    }
    var object: [String: Any] = [
        "version": 1,
        "mac": [
            "view": "mac", "head": head, "sub": sub, "rows": rows, "hero_name": "Alex's Account",
            "action_kind": actionKind, "action_text": "Switch this Mac to Team Account", "action_target": org,
            "countdown": countdown
        ],
        "accounts": accounts,
        "notifications": notices
    ]
    if countdown {
        object["countdown"] = ["target_uuid": org, "ends_at": "2026-11-19T15:41:05Z", "seconds": 5]
    }
    let data = try JSONSerialization.data(withJSONObject: object)
    return try ServiceState.decode(data)
}

private func notice(_ id: String, kind: String, title: String, body: String = "", uuid: String? = alex, at: String = "2026-11-19T15:40:00Z") -> [String: Any] {
    var item: [String: Any] = ["id": id, "kind": kind, "title": title, "body": body, "at": at]
    item["uuid"] = uuid ?? NSNull()
    return item
}

private let now = Timestamps.parse("2026-11-19T15:41:00Z")!

@Suite
struct NoticePlanTests {
    @Test func decodesTheServiceNotificationList() throws {
        let state = try ServiceState.decode(Data(#"""
        {"notifications": [{"id": "limit:abc:2026-11-19T12:00:00+00:00", "kind": "limit", "title": "Alex's Account is out until 17:00",
          "body": "Team Account has all of its 5 hours.", "uuid": "abc", "at": "2026-11-19T15:40:00.123Z"},
          {"id": "report-1", "kind": "report", "title": "Lifeline could not save the settings.", "body": "", "uuid": null, "at": "2026-11-19T15:40:01Z"}]}
        """#.utf8))
        #expect(state.notifications?.count == 2)
        #expect(state.notifications?[0].id == "limit:abc:2026-11-19T12:00:00+00:00")
        #expect(state.notifications?[0].kind == "limit")
        #expect(state.notifications?[0].body == "Team Account has all of its 5 hours.")
        #expect(state.notifications?[1].uuid == nil)
    }

    @Test func eachIdIsPostedOnce() throws {
        let state = try noticeState(notices: [
            notice("a", kind: "limit", title: "Alex's Account is out until 17:00"),
            notice("b", kind: "report", title: "Lifeline could not save the settings.", uuid: nil)
        ])
        let first = Notices.plan(state: state, seen: [], shown: nil, now: now)
        #expect(first.post.map(\.id) == ["a", "b"])
        #expect(first.quiet.isEmpty)
        let second = Notices.plan(state: state, seen: ["a"], shown: nil, now: now)
        #expect(second.post.map(\.id) == ["b"])
        let third = Notices.plan(state: state, seen: ["a", "b"], shown: nil, now: now)
        #expect(third.post.isEmpty && third.quiet.isEmpty)
    }

    @Test func aRepeatedIdInOneListIsPostedOnce() throws {
        let state = try noticeState(notices: [
            notice("a", kind: "report", title: "First.", uuid: nil),
            notice("a", kind: "report", title: "Second.", uuid: nil)
        ])
        let plan = Notices.plan(state: state, seen: [], shown: nil, now: now)
        #expect(plan.post.count == 1)
    }

    @Test func noticesFollowTheirTimesNotTheListOrder() throws {
        let state = try noticeState(notices: [
            notice("late", kind: "report", title: "Late.", uuid: nil, at: "2026-11-19T15:40:30Z"),
            notice("early", kind: "report", title: "Early.", uuid: nil, at: "2026-11-19T15:39:00Z")
        ])
        #expect(Notices.plan(state: state, seen: [], shown: nil, now: now).post.map(\.id) == ["early", "late"])
    }

    @Test func oldNoticesAreMarkedWithoutShowing() throws {
        let state = try noticeState(notices: [
            notice("edge", kind: "report", title: "Edge.", uuid: nil, at: "2026-11-19T15:11:00Z"),
            notice("old", kind: "report", title: "Old.", uuid: nil, at: "2026-11-19T15:10:59Z"),
            notice("future", kind: "report", title: "Future.", uuid: nil, at: "2026-11-19T16:00:00Z"),
            notice("timeless", kind: "report", title: "Timeless.", uuid: nil, at: "not a time")
        ])
        let plan = Notices.plan(state: state, seen: [], shown: nil, now: now)
        #expect(plan.quiet == ["old"])
        #expect(Set(plan.post.map(\.id)) == ["edge", "future", "timeless"])
    }

    @Test func aBlankTitleIsNeverShown() throws {
        let state = try noticeState(notices: [notice("blank", kind: "report", title: "   ", uuid: nil)])
        let plan = Notices.plan(state: state, seen: [], shown: nil, now: now)
        #expect(plan.post.isEmpty && plan.quiet == ["blank"])
    }

    @Test func theOpenMenuShowingTheSameSentenceStopsTheNotice() throws {
        let state = try noticeState(notices: [notice("w", kind: "will_run_out", title: "Alex's Account runs out at 15:40")])
        let open = ShownMenu(model: state.mac, note: nil)
        let plan = Notices.plan(state: state, seen: [], shown: open, now: now)
        #expect(plan.post.isEmpty && plan.quiet == ["w"])
        let closed = Notices.plan(state: state, seen: [], shown: nil, now: now)
        #expect(closed.post.map(\.id) == ["w"])
    }

    @Test func theCountdownSentenceInTheMenuCoversTheLimitNotice() throws {
        let state = try noticeState(
            notices: [notice("l", kind: "limit", title: "Alex's Account is out until 17:00")],
            head: "Switching this Mac to Team Account.",
            sub: "In 5 seconds. Alex's Account is out until 17:00.",
            countdown: true,
            actionKind: "cancel"
        )
        #expect(Notices.plan(state: state, seen: [], shown: ShownMenu(model: state.mac, note: nil), now: now).post.isEmpty)
    }

    @Test func anotherAccountsNoticeStillShowsWhileTheMenuIsOpen() throws {
        let state = try noticeState(notices: [notice("d", kind: "limit", title: "Work Account is out until 18:30", uuid: work)])
        #expect(Notices.plan(state: state, seen: [], shown: ShownMenu(model: state.mac, note: nil), now: now).post.map(\.id) == ["d"])
    }

    @Test func theSwitchReportIsQuietWhileTheMenuShowsThatSwitch() throws {
        let state = try noticeState(
            notices: [
                notice("r1", kind: "report", title: "Lifeline switched this Mac to Team Account because Alex's Account reached its limit.", uuid: nil),
                notice("r2", kind: "report", title: "Lifeline could not save the settings.", uuid: nil)
            ],
            head: "This Mac now uses Team Account.",
            sub: "No open sessions moved with it.",
            actionKind: "undo"
        )
        var shown = ShownMenu(model: state.mac, note: nil)
        shown.justSwitchedTo = "Team Account"
        let plan = Notices.plan(state: state, seen: [], shown: shown, now: now)
        #expect(plan.quiet == ["r1"])
        #expect(plan.post.map(\.id) == ["r2"])
    }

    @Test func theMenuNoteCountsAsShown() throws {
        let state = try noticeState(notices: [notice("n", kind: "report", title: "Lifeline could not save the settings.", uuid: nil)])
        let shown = ShownMenu(model: state.mac, note: "Lifeline could not save the settings.")
        #expect(Notices.plan(state: state, seen: [], shown: shown, now: now).quiet == ["n"])
    }

    @Test func shownMenuReadsTheVisibleSentences() throws {
        let state = try noticeState(notices: [], actionKind: "undo")
        let shown = ShownMenu(model: state.mac, note: "A note.")
        #expect(shown.texts == [state.mac!.head!, state.mac!.sub!, "A note."])
        #expect(shown.justSwitchedTo == "Alex's Account")
        #expect(ShownMenu(model: try noticeState(notices: []).mac, note: nil).justSwitchedTo == nil)
        #expect(ShownMenu(model: nil, note: "").texts.isEmpty)
    }
}

@Suite
struct NoticeActionTests {
    @Test func runningOutOnThisMacOffersTheAccountWithTheMostRoom() throws {
        let state = try noticeState(notices: [])
        let action = Notices.action(for: NoticeSummary(id: "w", kind: "will_run_out", title: "t", uuid: alex), state: state)
        #expect(action == NoticeAction(command: .switchTo(org), title: "Switch to Team Account"))
    }

    @Test func aLimitOnThisMacOffersTheSwitch() throws {
        let state = try noticeState(notices: [])
        let action = Notices.action(for: NoticeSummary(id: "l", kind: "limit", title: "t", uuid: alex), state: state)
        #expect(action?.command == .switchTo(org))
    }

    @Test func aLimitDuringTheCountdownOffersCancel() throws {
        let state = try noticeState(notices: [], countdown: true, actionKind: "cancel")
        let action = Notices.action(for: NoticeSummary(id: "l", kind: "limit", title: "t", uuid: alex), state: state)
        #expect(action == NoticeAction(command: .cancel, title: "Cancel the switch"))
        let week = Notices.action(for: NoticeSummary(id: "k", kind: "week", title: "t", uuid: alex), state: state)
        #expect(week?.command == .switchTo(org))
    }

    @Test func otherMachinesAccountsAreNeverSwitchedFromANotice() throws {
        let state = try noticeState(notices: [])
        for kind in ["will_run_out", "limit", "week"] {
            #expect(Notices.action(for: NoticeSummary(id: "x", kind: kind, title: "t", uuid: work), state: state) == nil)
        }
    }

    @Test func noAccountWithRoomMeansNoSwitch() throws {
        let state = try noticeState(notices: [], best: nil)
        #expect(Notices.action(for: NoticeSummary(id: "w", kind: "will_run_out", title: "t", uuid: alex), state: state) == nil)
        let self_best = try noticeState(notices: [], best: alex)
        #expect(Notices.action(for: NoticeSummary(id: "w", kind: "will_run_out", title: "t", uuid: alex), state: self_best) == nil)
    }

    @Test func anAccountThatIsBackOffersTheSwitchBack() throws {
        let state = try noticeState(notices: [])
        let back = Notices.action(for: NoticeSummary(id: "b", kind: "back", title: "t", uuid: org), state: state)
        #expect(back == NoticeAction(command: .switchTo(org), title: "Switch back to Team Account"))
        let alexBack = Notices.action(for: NoticeSummary(id: "b", kind: "back", title: "t", uuid: work), state: state)
        #expect(alexBack?.title == "Switch back to Work Account")
        #expect(Notices.action(for: NoticeSummary(id: "b", kind: "back", title: "t", uuid: alex), state: state) == nil)
        #expect(Notices.action(for: NoticeSummary(id: "b", kind: "back", title: "t", uuid: "unknown"), state: state) == nil)
    }

    @Test func onlyThisMacsSignInEndingOffersSignInAgain() throws {
        let state = try noticeState(notices: [])
        let mine = Notices.action(for: NoticeSummary(id: "signin-ends:mac:\(work):2026-11-22", kind: "signin_ends", title: "t", uuid: work), state: state)
        #expect(mine == NoticeAction(command: .resign(work), title: "Sign in again"))
        let devbox = Notices.action(for: NoticeSummary(id: "signin-ends:devbox:\(work):2026-11-22", kind: "signin_ends", title: "t", uuid: work), state: state)
        #expect(devbox == nil)
    }

    @Test func reportsAndUnknownKindsHaveNoButtons() throws {
        let state = try noticeState(notices: [])
        #expect(Notices.action(for: NoticeSummary(id: "r", kind: "report", title: "t", uuid: alex), state: state) == nil)
        #expect(Notices.action(for: NoticeSummary(id: "r", kind: "later_kind", title: "t", uuid: alex), state: state) == nil)
        #expect(Notices.action(for: NoticeSummary(id: "r", kind: "limit", title: "t", uuid: nil), state: state) == nil)
        #expect(Notices.action(for: NoticeSummary(id: "r", kind: nil, title: "t", uuid: alex), state: state) == nil)
    }

    @Test func everyActionTitleIsRegisteredOnce() throws {
        let state = try noticeState(notices: [])
        let titles = Notices.titles(for: state)
        #expect(titles.first == "Cancel the switch")
        #expect(titles.contains("Sign in again"))
        #expect(titles.contains("Switch to Team Account"))
        #expect(titles.contains("Switch back to Alex's Account"))
        #expect(Set(titles).count == titles.count)
        #expect(titles.count == 2 + 4 * 2)
    }

    @Test func commandsSurviveTheTripThroughTheNotification() {
        for command in [MenuCommand.switchTo(org), .undo, .cancel, .resign(work)] {
            #expect(Notices.command(from: Notices.userInfo(for: command)) == command)
        }
        #expect(Notices.command(from: [:]) == nil)
        #expect(Notices.command(from: ["command": "switch"]) == nil)
        #expect(Notices.command(from: ["command": "resign", "uuid": ""]) == nil)
        #expect(Notices.command(from: ["command": "explode", "uuid": org]) == nil)
    }

    @Test func seenIdsAreKeptForTwoDays() {
        let kept = Notices.prune(["new": now.addingTimeInterval(-3600), "edge": now.addingTimeInterval(-48 * 3600), "gone": now.addingTimeInterval(-48 * 3600 - 1)], now: now)
        #expect(Set(kept.keys) == ["new", "edge"])
    }

    @Test func permissionNotesComeFromTheCore() throws {
        let notes = try #require(try Fixture.state("moment-sheet").settingsText?.mac?.notes)
        #expect(SettingsPage.noteKey(NoticePermission.allowed) == nil)
        #expect(SettingsPage.noteKey(NoticePermission.waiting) == nil)
        for refused in [NoticePermission.refused, .listOnly, .failed, .unavailable, .banners] {
            let note = notes[SettingsPage.noteKey(refused) ?? "-"] ?? ""
            #expect(note.hasSuffix("."), "\(refused)")
        }
        #expect(NoticePermission.allowed.posts && NoticePermission.listOnly.posts && NoticePermission.failed.posts && NoticePermission.banners.posts)
        #expect(!NoticePermission.refused.posts && !NoticePermission.waiting.posts && !NoticePermission.unavailable.posts)
    }
}

@Suite
struct ShortcutTests {
    @Test func theSystemsAnswerBecomesTheShortcutState() {
        #expect(Shortcut.state(forStatus: 0) == .on)
        #expect(Shortcut.state(forStatus: -9878) == .taken)
        #expect(Shortcut.state(forStatus: -50) == .refused)
    }

    @Test func aRefusedShortcutSaysSoInTheCoresSentence() throws {
        let notes = try #require(try Fixture.state("moment-sheet").settingsText?.shortcut?.notes)
        #expect(SettingsPage.noteKey(ShortcutState.on) == nil)
        #expect(SettingsPage.noteKey(ShortcutState.waiting) == nil)
        #expect(!(notes[SettingsPage.noteKey(ShortcutState.taken) ?? "-"] ?? "").isEmpty)
        #expect(!(notes[SettingsPage.noteKey(ShortcutState.refused) ?? "-"] ?? "").isEmpty)
        #expect(Shortcut.keys == ["Control", "Option", "C"])
        #expect(Shortcut.keyCode == 8)
    }
}

@Suite
struct NoticeEdgeTests {
    @Test func theSystemsAnswerBecomesOnePermission() {
        #expect(NoticePermission.from(authorized: true, denied: false, style: .alert) == .allowed)
        #expect(NoticePermission.from(authorized: true, denied: false, style: .banner) == .banners)
        #expect(NoticePermission.from(authorized: true, denied: false, style: .none) == .listOnly)
        #expect(NoticePermission.from(authorized: false, denied: true, style: .alert) == .refused)
        #expect(NoticePermission.from(authorized: false, denied: false, style: .alert) == .waiting)
    }

    @Test func theOpenMenuCheckComparesWholeSentences() throws {
        let state = try noticeState(notices: [notice("p", kind: "report", title: "Account runs out at 15:40", uuid: nil)])
        let shown = ShownMenu(model: state.mac, note: nil)
        #expect(Notices.plan(state: state, seen: [], shown: shown, now: now).post.map(\.id) == ["p"])
        #expect(Notices.sentences("Alex's Account runs out at 15:40. That is 1.5 hours away. Done") == ["Alex's Account runs out at 15:40.", "That is 1.5 hours away.", "Done"])
        #expect(Notices.sentences("") == [])
    }

    @Test func anIdIsSeenOnlyOnceItsPostSucceeds() throws {
        let state = try noticeState(notices: [notice("a", kind: "report", title: "One.", uuid: nil), notice("old", kind: "report", title: "Old.", uuid: nil, at: "2026-11-19T10:00:00Z")])
        var ledger = NoticeLedger(seen: [:], now: now)
        let first = ledger.take(state: state, shown: nil, now: now)
        #expect(first.post.map(\.id) == ["a"])
        #expect(ledger.seen["old"] != nil)
        #expect(ledger.seen["a"] == nil)
        #expect(ledger.take(state: state, shown: nil, now: now).post.isEmpty)
        ledger.failed("a")
        #expect(ledger.take(state: state, shown: nil, now: now).post.map(\.id) == ["a"])
        ledger.sent("a", at: now)
        #expect(ledger.seen["a"] == now)
        #expect(ledger.take(state: state, shown: nil, now: now).post.isEmpty)
    }

    @Test func theLedgerForgetsAfterTwoDays() {
        let ledger = NoticeLedger(seen: ["x": now.addingTimeInterval(-49 * 3600), "y": now], now: now)
        #expect(Set(ledger.seen.keys) == ["y"])
    }

    @Test func aStaleCancelIsFoundWhenTheCountdownIsGone() throws {
        #expect(Notices.countdownGone(try noticeState(notices: [])))
        #expect(!Notices.countdownGone(try noticeState(notices: [], countdown: true, actionKind: "cancel")))
        var flagOnly = try noticeState(notices: [])
        flagOnly.mac?.countdown = true
        #expect(!Notices.countdownGone(flagOnly))
    }

    @Test func aRefusalBecomesAPlainNotice() {
        let notice = Notices.refusal("There is no switch to undo.", id: "refused-1")
        #expect(notice.title == "There is no switch to undo.")
        #expect(notice.body.isEmpty && notice.action == nil && notice.id == "refused-1")
    }

    @Test func switchBackUsesTheMenusNameAsTheMenuDoes() {
        #expect(Notices.backTitle("Alex's Account") == "Switch back to Alex's Account")
        #expect(Notices.backTitle("Chris") == "Switch back to Chris")
    }
}
