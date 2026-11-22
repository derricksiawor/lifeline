import Foundation
import Testing
@testable import LifelineKit

private let utc = TimeZone(identifier: "UTC")!
private let boardNow = Timestamps.parse("2026-11-18T15:05:00Z")!

private func account(_ json: String) throws -> AccountSummary {
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase
    return try decoder.decode(AccountSummary.self, from: Data(json.utf8))
}

private let boardAccount = """
{"uuid": "j", "name": "Alex's Account", "short": "Alex", "plan": "Max 5x", "five": 88,
 "five_reset": "2026-11-18T17:00:00Z", "week": 22, "week_reset": "2026-11-22T09:00:00Z",
 "forecast": {"at_reset_pct": 130, "runs_out_at": "2026-11-18T15:40:00Z", "pace_per_min": 0.34, "basis": "learned"},
 "needs_signin": false, "in_use": ["mac"], "signin_ends": "2026-12-16T10:00:00Z", "devbox_needs_signin": false,
 "devbox_signin_ends": "2026-12-18T10:00:00Z",
 "projects": [{"cwd": "/home/Sam/projects/project-a", "name": null, "started_at": "2026-11-18T09:00:00Z"},
              {"cwd": "/home/Sam/projects/project-b", "name": "web", "started_at": "2026-11-18T10:00:00Z"},
              {"cwd": "/home/Sam/projects/project-c", "name": null, "started_at": null},
              {"cwd": "/home/Sam/projects/project-b", "name": null, "started_at": null}]}
"""

private func boardReadings() -> [HistoryReading] {
    let points: [(Int, Double)] = [(30, 12), (60, 25), (90, 33), (120, 42), (150, 58), (170, 74), (185, 88)]
    let start = Timestamps.parse("2026-11-18T12:00:00Z")!
    let iso = ISO8601DateFormatter()
    return points.map { minute, five in
        HistoryReading(source: "usage", five: five, fiveReset: "2026-11-18T17:00:00Z", at: iso.string(from: start.addingTimeInterval(Double(minute) * 60)))
    }
}

private func width(_ text: String) -> Double {
    Double(text.count) * 6
}

@Suite
struct DetailChartTests {
    @Test func pointsStayInsideTheChart() {
        #expect(DetailChart.weekY(100) == 8)
        #expect(DetailChart.weekY(0) == 74)
        #expect(DetailChart.weekX(6) == 424)
    }

}

@Suite
struct DetailTextTests {
    @Test func theUseButtonFollowsThisMac() throws {
        let alex = try account(boardAccount)
        #expect(Detail.button(alex, macActive: "j") == .inUseHere)
        #expect(Detail.button(alex, macActive: "other") == .useHere("j"))
        #expect(Detail.button(alex, macActive: nil) == .useHere("j"))
    }

    @Test func aRunningSignInShowsTheWait() {
        var state = ServiceState()
        state.jobs = [JobSummary(id: "1", kind: "resign", uuid: "j", state: "running")]
        #expect(Detail.signingIn("j", state: state))
        #expect(!Detail.signingIn("k", state: state))
        state.jobs = [JobSummary(id: "1", kind: "resign", uuid: "j", state: "done")]
        #expect(!Detail.signingIn("j", state: state))
        #expect(!Detail.signingIn("j", state: nil))
    }

    @Test func arrowsStepThroughTheRows() {
        let rows = ["a", "b", "c", "d"].map { MenuRow(uuid: $0) }
        #expect(Detail.step(from: "b", by: 1, rows: rows) == "c")
        #expect(Detail.step(from: "d", by: 1, rows: rows) == "d")
        #expect(Detail.step(from: "a", by: -1, rows: rows) == "a")
        #expect(Detail.step(from: nil, by: 1, rows: rows) == "a")
        #expect(Detail.step(from: "gone", by: 1, rows: rows) == "a")
        #expect(Detail.step(from: "a", by: 1, rows: []) == nil)
    }

    @Test func theHistoryReplyDecodes() throws {
        let reply = try HistoryReply.decode(Data(#"""
        {"uuid": "j", "days": 7, "readings": [{"source": "usage", "five": 12.5, "five_reset": "2026-11-18T17:00:00Z", "week": 20, "week_reset": null, "at": "2026-11-18T12:30:00Z"}],
         "daily": [{"date": "2026-11-18", "peak_five": 88, "hit_limit": false}]}
        """#.utf8))
        #expect(reply.readings?.first?.fiveReset == "2026-11-18T17:00:00Z")
        #expect(reply.daily?.first?.peakFive == 88)
        #expect(reply.daily?.first?.hitLimit == false)
    }
}

@Suite
struct DetailKeyTests {
    func act(_ code: UInt16, open: Bool, shift: Bool = false, command: Bool = false) -> KeyAction {
        MenuLogic.keyAction(keyCode: code, characters: nil, shift: shift, command: command, option: false, control: false, state: nil, detailOpen: open)
    }

    @Test func theRightArrowOpensTheDetail() {
        #expect(act(124, open: false) == .detail(.open))
        #expect(act(124, open: true) == .ignore)
    }

    @Test func leftArrowAndEscapeCloseTheDetailFirst() {
        #expect(act(123, open: true) == .detail(.close))
        #expect(act(123, open: false) == .ignore)
        #expect(act(53, open: true) == .detail(.close))
        #expect(act(53, open: false) == .close)
    }

    @Test func upAndDownMoveBetweenAccountsOnlyWhileOpen() {
        #expect(act(126, open: true) == .detail(.previous))
        #expect(act(125, open: true) == .detail(.next))
        #expect(act(126, open: false) == .ignore)
        #expect(act(125, open: false) == .ignore)
    }

    @Test func arrowsWithModifiersAreLeftAlone() {
        #expect(act(124, open: false, shift: true) == .ignore)
        #expect(act(124, open: false, command: true) == .ignore)
        #expect(act(125, open: true, shift: true) == .ignore)
    }

    @Test func theDetailKeyExpandsTheAccountInUse() {
        var model = MenuModel()
        model.rows = [MenuRow(uuid: "a"), MenuRow(uuid: "b", isActive: true), MenuRow(uuid: "c")]
        #expect(MenuLogic.detailTarget(model: model) == "b")
        model.rows = [MenuRow(uuid: "a"), MenuRow(uuid: "b")]
        #expect(MenuLogic.detailTarget(model: model) == "a")
        #expect(MenuLogic.detailTarget(model: nil) == nil)
    }

    @Test func keysOneToFourStillWorkWithTheDetailOpen() {
        var state = ServiceState()
        state.accounts = [AccountSummary(uuid: "a", number: 1), AccountSummary(uuid: "b", number: 2)]
        var model = MenuModel()
        model.rows = [MenuRow(uuid: "a", isActive: true), MenuRow(uuid: "b")]
        state.mac = model
        let action = MenuLogic.keyAction(keyCode: 19, characters: "2", shift: false, command: false, option: false, control: false, state: state, detailOpen: true)
        #expect(action == .command(.switchTo("b")))
    }
}
