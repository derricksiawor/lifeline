import AppKit
import Foundation
import SwiftUI
import Testing
@testable import Lifeline
@testable import LifelineKit

private func rows(_ name: String, side: MenuSide = .mac) throws -> [MenuRow] {
    try #require(try Fixture.state(name).model(for: side)?.rows)
}

private func row(_ name: String, _ uuid: String, side: MenuSide = .mac) throws -> MenuRow {
    try #require(try rows(name, side: side).first { $0.uuid == uuid })
}

@MainActor
struct RowFigureTests {
    @Test func everyRowShowsTheCoresFigureInTheBoardsForm() throws {
        for fixture in ["moment-good", "moment-menu", "extra-noroom", "extra-trouble"] {
            for item in try rows(fixture) {
                #expect(AccountRow.named(item) == item.figure, "\(fixture) \(item.name ?? "") shows \(AccountRow.named(item) ?? "nil")")
            }
        }
        #expect(AccountRow.named(try row("moment-good", Fixture.alex)) == "42%")
        #expect(AccountRow.named(try row("moment-menu", Fixture.personal)) == "Week 76%")
    }

    @Test func theThinLineFollowsTheOtherWindow() throws {
        let alex = try row("moment-good", Fixture.alex)
        let thin = try #require(AccountRow.thin(alex))
        #expect(thin.label == "Week 18%")
        #expect(abs(thin.fraction - 0.18) < 1e-9)
        #expect(abs(RowLook.plain(alex).week - 0.18) < 1e-9)
        let work = try row("extra-noroom", Fixture.work)
        #expect(AccountRow.thin(work)?.label == "5 hours 30%")
        #expect(abs((AccountRow.thin(work)?.fraction ?? 0) - 0.30) < 1e-9)
        #expect(AccountRow.thin(try row("moment-menu", Fixture.personal)) == nil, "a row with one window draws no thin line")
    }

    @Test func aClosedRowHasOneWhereLineAndNoSecondSentence() throws {
        let state = try Fixture.state("moment-menu")
        let work = try row("moment-menu", Fixture.work)
        let second = try #require(work.secondText)
        try #require(!second.isEmpty)
        let drawn = drawnLines(AccountRow(row: work, old: nil, fade: .settled, palette: .dark, look: .plain(work), animated: false, action: nil, showsSecond: true))
        #expect(!drawn.contains { second.hasPrefix($0) || $0.hasPrefix(String(second.prefix(12))) }, "the closed row draws its second sentence: \(drawn)")
        #expect(drawn.contains(AccountRow.named(work) ?? "-"), "\(drawn)")
        #expect(drawn.joined(separator: "|").contains("Week |94%"), "the thin label is missing: \(drawn)")
        _ = state
    }

    @Test func theHeroNamesTheThinFigureOnlyWhenItsSentenceDoesNot() throws {
        var model = try #require(try Fixture.state("moment-good").mac)
        #expect(HeroView.thinLabel(model) == nil, "the reset sentence already says 18%")
        model.secondText = "At this pace its week runs out on Saturday at 16:35."
        #expect(HeroView.thinLabel(model) == "Week 18%")
        model.thinWindow = ""
        #expect(HeroView.thinLabel(model) == nil)
    }
}

@MainActor
struct TroubleRowTests {
    @Test func aRowThatNeedsASignInStartsSignInAgainOnClick() throws {
        let org = try row("extra-trouble", Fixture.org)
        try #require(org.needsSignin == true)
        #expect(MenuLogic.rowClick(org, side: .mac, secondary: false) == .run(.resign(Fixture.org)))
        #expect(MenuLogic.rowClick(org, side: .dev, secondary: false) == .detail(Fixture.org))
        #expect(MenuLogic.rowClick(org, side: .mac, secondary: true) == .detail(Fixture.org))
        let alex = try row("moment-good", Fixture.work)
        #expect(MenuLogic.rowClick(alex, side: .mac, secondary: false) == .run(.switchTo(Fixture.work)))
    }

    @Test func troubleRowsStandApart() throws {
        let org = try row("extra-trouble", Fixture.org)
        let work = try row("extra-trouble", Fixture.work)
        let personal = try row("extra-trouble", Fixture.personal)
        let alex = try row("extra-trouble", Fixture.alex)
        #expect(AccountRow.whereInk(org, palette: .dark) == Palette.dark.low.ink, "a row that needs a sign-in keeps the quiet ink")
        #expect(AccountRow.whereInk(alex, palette: .dark) == Palette.dark.muted)
        #expect(AccountRow.lineStrength(work) == Metrics.unavailable, "an unreadable row's empty line is not dimmed")
        #expect(AccountRow.lineStrength(org) == Metrics.unavailable)
        #expect(personal.stale == true)
        #expect(AccountRow.lineStrength(personal) == Metrics.unavailable, "a stale row is drawn at full strength")
        #expect(AccountRow.lineStrength(alex) == 1)
    }
}

@MainActor
struct OpenRowLightTests {
    @Test func anOpenRowIsLighterThanTheRowInUseInLight() {
        #expect(Palette.light.openRow == 0.55)
        #expect(Palette.light.openRow < 1)
        #expect(Palette.dark.openRow < 1)
    }
}

@MainActor
struct FirstRunActionTests {
    @Test func theFirstRunHeroOffersTheAdd() throws {
        for fixture in ["extra-firstrun", "extra-empty"] {
            let model = try #require(try Fixture.state(fixture).mac)
            #expect(model.actionKind == "add")
            #expect(MenuLogic.actionStyle(model, side: .mac) == .primary, "\(fixture) shows no button")
            #expect(MenuLogic.heroAdd(model, side: .mac) != nil)
            #expect(MenuLogic.heroAdd(model, side: .dev) == nil)
            #expect(MenuView.showsAccountsLabel(model) == false, "\(fixture) labels an empty list")
        }
        let first = try Fixture.state("extra-firstrun")
        #expect(MenuLogic.heroAdd(try #require(first.mac), side: .mac) == .add(known: "Work Account"))
        let empty = try Fixture.state("extra-empty")
        #expect(MenuLogic.heroAdd(try #require(empty.mac), side: .mac) == .add(known: nil))
        var adopt = try #require(first.mac)
        adopt.actionKind = "adopt"
        #expect(MenuLogic.heroAdd(adopt, side: .mac) == .adopt)
        #expect(MenuLogic.actionStyle(adopt, side: .mac) == .primary)
        #expect(MenuView.showsAccountsLabel(try #require(try Fixture.state("moment-good").mac)))
    }
}

@MainActor
@Suite(.serialized)
struct MenuHeightTests {
    @Test func fourAccountsAndNoCardFitIn760Points() throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        func height(_ fixture: String) throws -> CGFloat {
            let view = MenuView(state: try Fixture.state(fixture), side: .mac, note: nil, maxHeight: 879)
                .environment(\.menuMotion, Motion(MotionPlan(reduceMotion: true)))
            return NSHostingView(rootView: view).fittingSize.height
        }
        for fixture in ["moment-good", "moment-menu"] {
            let tall = try height(fixture)
            #expect(tall <= 760, "\(fixture) is \(tall) pt tall")
        }
        // The other computer-needs-you moment adds its one amber note under the hero, two lines of 12 pt text.
        #expect(try height("moment-devneeds") <= (try height("moment-good")) + 2 * Styles.small.lineHeight + Metrics.panelGap)
        // A moment with a Switch or Undo button and its hint is taller.
        // It must still show every row without scrolling inside the 879 pt cap.
        for fixture in ["moment-low", "moment-limit", "moment-auto", "moment-switched", "moment-back"] {
            let view = MenuView(state: try Fixture.state(fixture), side: .mac, note: nil)
                .environment(\.menuMotion, Motion(MotionPlan(reduceMotion: true)))
            let natural = NSHostingView(rootView: view).fittingSize.height
            #expect(natural <= 879, "\(fixture) needs \(natural) pt, so its list would scroll")
        }
    }

    @Test func aScrollingListKeepsTheRowInUseAndMostRoomInView() {
        let tops: [String: Double] = ["a": 0, "b": 80, "c": 160, "d": 240]
        let bottoms: [String: Double] = ["a": 74, "b": 154, "c": 234, "d": 314]
        let both = MenuLogic.keepInView(tops: tops, bottoms: bottoms, keep: ["a", "d"], viewport: 320, content: 314)
        #expect(both == 0)
        let low = MenuLogic.keepInView(tops: tops, bottoms: bottoms, keep: ["c", "d"], viewport: 160, content: 314)
        #expect(low <= 160 && low + 160 >= 314, "\(low)")
        let apart = MenuLogic.keepInView(tops: tops, bottoms: bottoms, keep: ["a", "d"], viewport: 160, content: 314)
        #expect(apart == 0, "when both cannot fit, the row in use wins")
        let none = MenuLogic.keepInView(tops: tops, bottoms: bottoms, keep: [], viewport: 160, content: 314)
        #expect(none == 0)
    }
}

@MainActor
struct ListRestTests {
    @Test func aCutListAlwaysEndsOnARowBoundary() {
        let stops: [Double] = [74, 154, 234, 314]
        for cards in stride(from: 0.0, through: 400, by: 7) {
            let heights = MenuLogic.cappedHeights(
                cards: cards, top: 330, list: 314, footer: 60, limit: 700, spacing: 4, minimum: 120, cardMinimum: 96, listStops: stops
            )
            let rested = MenuLogic.rest(list: heights[2], natural: 314, stops: stops)
            #expect(abs(rested.list + rested.spare - heights[2]) < 0.01, "the menu changed height")
            if rested.list < 314 - 0.5 {
                #expect(stops.contains { abs($0 - rested.list) < 0.5 }, "cards \(cards): the list stops at \(rested.list)")
            }
        }
        let whole = MenuLogic.rest(list: 314, natural: 314, stops: stops)
        #expect(whole.list == 314 && whole.spare == 0)
    }

    @Test func theScrollBarShowsBrieflyThenFades() {
        let start = Date(timeIntervalSince1970: 1000)
        #expect(ScrollBar.strength(since: start, now: start) == 1)
        #expect(ScrollBar.strength(since: start, now: start.addingTimeInterval(1.1)) == 1)
        #expect(ScrollBar.strength(since: start, now: start.addingTimeInterval(1.2 + ScrollBar.fade + 0.01)) == 0)
        let middle = ScrollBar.strength(since: start, now: start.addingTimeInterval(1.2 + ScrollBar.fade / 2))
        #expect(middle > 0 && middle < 1)
        #expect(ScrollBar.strength(since: nil, now: start) == 0)
    }
}

@MainActor
@Suite(.serialized)
struct HeroAddStoreTests {
    private func store(_ fixture: String, adopt: Bool = false) throws -> (FakeServer, StateStore) {
        var object = try Fixture.object(fixture)
        if adopt, var mac = object["mac"] as? [String: Any] {
            mac["action_kind"] = "adopt"
            object["mac"] = mac
        }
        let body = String(decoding: try JSONSerialization.data(withJSONObject: object), as: UTF8.self)
        let server = try FakeServer { head, _ in
            if head.hasPrefix("POST /add") || head.hasPrefix("POST /adopt") {
                return [okReply(#"{"job": "h1"}"#, status: "202 Accepted")]
            }
            return [okReply(body)]
        }
        return (server, StateStore(api: LifelineAPI(client: SocketClient(path: server.path)), sizes: nil))
    }

    @Test func theAddButtonOpensAddAnAccountWithTheAccountPickedAndStartsTheSignIn() async throws {
        let (server, store) = try store("extra-firstrun")
        defer { server.close() }
        await store.refresh()
        store.heroAdd(.add(known: "Work Account"))
        #expect(store.screen == .add)
        #expect(store.chosenPick?.name == "Work Account")
        #expect(store.chosenTarget == .mac)
        await waitFor { server.requests.contains { $0.head.hasPrefix("POST /add") } }
        let add = try #require(server.requests.first { $0.head.hasPrefix("POST /add") })
        let sent = try JSONSerialization.jsonObject(with: add.body) as? [String: Any]
        #expect(sent?["known"] as? String == "Work Account" || sent?["email"] as? String == "hello@example.net", "\(String(decoding: add.body, as: UTF8.self))")
        #expect(!server.requests.contains { $0.head.hasPrefix("POST /devbox") })
    }

    @Test func theAdoptButtonTakesThisMacsSignInWithNoBrowser() async throws {
        let (server, store) = try store("mac-live-unadded", adopt: true)
        defer { server.close() }
        await store.refresh()
        try #require(store.state?.mac?.live?.adoptReady == true, "the fixture's Mac sign-in is not ready to adopt")
        let before = store.signInStarts
        store.heroAdd(.adopt)
        await waitFor { server.requests.contains { $0.head.hasPrefix("POST /adopt") } }
        #expect(server.requests.contains { $0.head.hasPrefix("POST /adopt") })
        #expect(!server.requests.contains { $0.head.hasPrefix("POST /add") })
        #expect(store.signInStarts == before, "an adopt opened a browser")
    }
}
