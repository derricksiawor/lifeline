import Foundation
import Testing
@testable import LifelineKit

let alex = "11111111-aaaa-4aaa-8aaa-000000000001"
let work = "22222222-bbbb-4bbb-8bbb-000000000002"
let solo = "33333333-cccc-4ccc-8ccc-000000000003"
let crew = "44444444-dddd-4ddd-8ddd-000000000004"

func model(_ kind: String, text: String = "", target: String = "", countdown: Bool = false, active: String = alex, hint: String = "") -> MenuModel {
    var made = MenuModel()
    made.actionKind = kind
    made.actionText = text
    made.actionTarget = target
    made.countdown = countdown
    made.hint = hint
    made.rows = [alex, work, solo, crew].map { uuid in
        var row = MenuRow(uuid: uuid)
        row.isActive = uuid == active
        return row
    }
    return made
}

func fourAccounts(mac: String = alex) -> ServiceState {
    var state = ServiceState()
    state.accounts = [alex, work, solo, crew].enumerated().map { index, uuid in
        AccountSummary(uuid: uuid, number: index + 1, name: nil, short: nil, needsSignin: false, five: 0, inUse: nil)
    }
    state.mac = model("none", active: mac)
    state.dev = model("none", active: work, hint: "Lifeline switches only this Mac, so switch Studio there.")
    return state
}

@Suite
struct MomentActionTests {
    @Test func allGoodShowsNoAction() {
        let good = model("none")
        #expect(MenuLogic.actionStyle(good, side: .mac) == .none)
        #expect(MenuLogic.actionCommand(good, side: .mac) == nil)
        #expect(!MenuLogic.showsCountdown(good, side: .mac))
    }

    @Test func runningLowAndLimitReachedShowTheSwitch() {
        var low = model("primary", text: "Switch this Mac to Team Account", target: crew, hint: "Or press 4.")
        low.head = "Alex's Account runs out at 15:40."
        low.heroTone = "low"
        var limit = model("primary", text: "Switch this Mac to Personal Account", target: solo, hint: "Or press 3.")
        limit.head = "Alex's Account is out until 17:00."
        limit.heroTone = "limit"
        limit.countdown = false
        #expect(MenuLogic.actionStyle(low, side: .mac) == .primary)
        #expect(MenuLogic.actionCommand(low, side: .mac) == .switchTo(crew))
        #expect(!MenuLogic.showsCountdown(low, side: .mac))
        #expect(MenuLogic.actionStyle(limit, side: .mac) == .primary)
        #expect(MenuLogic.actionCommand(limit, side: .mac) == .switchTo(solo))
        #expect(!MenuLogic.showsCountdown(limit, side: .mac))
        var stuck = limit
        stuck.actionKind = "none"
        stuck.actionText = ""
        stuck.actionTarget = ""
        #expect(MenuLogic.actionStyle(stuck, side: .mac) == .none)
        #expect(MenuLogic.actionCommand(stuck, side: .mac) == nil)
    }

    @Test func switchingForYouShowsCancelAndTheCountdown() {
        let auto = model("cancel", text: "Cancel the switch", countdown: true)
        #expect(MenuLogic.actionStyle(auto, side: .mac) == .quiet)
        #expect(MenuLogic.actionCommand(auto, side: .mac) == .cancel)
        #expect(MenuLogic.showsCountdown(auto, side: .mac))
    }

    @Test func justSwitchedShowsUndo() {
        let switched = model("undo", text: "Undo the switch", active: crew)
        #expect(MenuLogic.actionStyle(switched, side: .mac) == .quiet)
        #expect(MenuLogic.actionCommand(switched, side: .mac) == .undo)
    }

    @Test func backAgainShowsSwitchBack() {
        let back = model("quiet", text: "Switch back to Alex's Account", target: alex, active: crew)
        #expect(MenuLogic.actionStyle(back, side: .mac) == .quiet)
        #expect(MenuLogic.actionCommand(back, side: .mac) == .switchTo(alex))
    }

    @Test func aDeadSignInShowsSignInAgain() {
        let dead = model("resign", text: "Sign in again", target: alex)
        #expect(MenuLogic.actionStyle(dead, side: .mac) == .primary)
        #expect(MenuLogic.actionCommand(dead, side: .mac) == .resign(alex))
    }

    @Test func theServiceSampleIsTheCountdownMoment() throws {
        let state = try ServiceState.decode(Data(sampleState.utf8))
        #expect(MenuLogic.actionStyle(state.mac, side: .mac) == .quiet)
        #expect(MenuLogic.actionCommand(state.mac, side: .mac) == .cancel)
        #expect(MenuLogic.showsCountdown(state.mac, side: .mac))
        #expect(state.mac?.rows?.first?.where == "Used by this Mac. Back at 17:00.")
        #expect(state.mac?.rows?.first?.isActive == true)
        #expect(state.mac?.heroReset == "Back at 17:00")
        #expect(state.countdown?.targetUuid == crew)
    }

    @Test func aMissingTargetOrTextShowsNothing() {
        #expect(MenuLogic.actionCommand(model("primary", text: "Switch", target: ""), side: .mac) == nil)
        #expect(MenuLogic.actionStyle(model("primary", text: "", target: crew), side: .mac) == .none)
        #expect(MenuLogic.actionStyle(model("unknown", text: "Something", target: crew), side: .mac) == .none)
        #expect(MenuLogic.actionCommand(model("unknown", text: "Something", target: crew), side: .mac) == nil)
        #expect(MenuLogic.actionStyle(nil, side: .mac) == .none)
        #expect(MenuLogic.actionCommand(nil, side: .mac) == nil)
    }

    @Test func aCountdownNeedsBothTheFlagAndCancel() {
        #expect(!MenuLogic.showsCountdown(model("cancel", text: "Cancel the switch", countdown: false), side: .mac))
        #expect(!MenuLogic.showsCountdown(model("undo", text: "Undo the switch", countdown: true), side: .mac))
    }
}

@Suite
struct DevboxViewTests {
    @Test func theDevboxViewHasNoActionsWhateverTheModelSays() {
        let kinds: [(String, String)] = [
            ("primary", crew), ("quiet", alex), ("undo", ""), ("cancel", ""), ("resign", work), ("none", "")
        ]
        for (kind, target) in kinds {
            let dev = model(kind, text: "Something", target: target, countdown: true, active: work)
            #expect(MenuLogic.actionStyle(dev, side: .dev) == .none)
            #expect(MenuLogic.actionCommand(dev, side: .dev) == nil)
            #expect(!MenuLogic.showsCountdown(dev, side: .dev))
        }
    }

    @Test func rowsInTheDevboxViewDoNothing() {
        for uuid in [alex, work, solo, crew] {
            var row = MenuRow(uuid: uuid)
            row.isActive = uuid == work
            #expect(MenuLogic.rowCommand(row, side: .dev) == nil)
        }
    }

    @Test func rowsInTheMacViewSwitchExceptTheCurrentOne() {
        var current = MenuRow(uuid: alex)
        current.isActive = true
        #expect(MenuLogic.rowCommand(current, side: .mac) == nil)
        #expect(MenuLogic.rowCommand(MenuRow(uuid: crew), side: .mac) == .switchTo(crew))
        #expect(MenuLogic.rowCommand(MenuRow(uuid: ""), side: .mac) == nil)
    }
}

@Suite
struct KeyTests {
    @Test func keysOneToFourSwitchTheMacByAccountNumber() {
        let state = fourAccounts(mac: alex)
        #expect(MenuLogic.keyCommand(digit: 1, state: state) == nil)
        #expect(MenuLogic.keyCommand(digit: 2, state: state) == .switchTo(work))
        #expect(MenuLogic.keyCommand(digit: 3, state: state) == .switchTo(solo))
        #expect(MenuLogic.keyCommand(digit: 4, state: state) == .switchTo(crew))
        let moved = fourAccounts(mac: crew)
        #expect(MenuLogic.keyCommand(digit: 1, state: moved) == .switchTo(alex))
        #expect(MenuLogic.keyCommand(digit: 4, state: moved) == nil)
    }

    @Test func numbersFollowTheRegistryNotTheRowOrder() {
        var state = fourAccounts()
        state.accounts = state.accounts?.reversed()
        state.accounts?[0].number = 4
        state.accounts?[3].number = 1
        #expect(MenuLogic.keyCommand(digit: 4, state: state) == .switchTo(crew))
        #expect(MenuLogic.keyCommand(digit: 1, state: state) == nil)
    }

    @Test func otherDigitsAndMissingAccountsDoNothing() {
        let state = fourAccounts()
        for digit in [0, 5, 9, -1] {
            #expect(MenuLogic.keyCommand(digit: digit, state: state) == nil)
        }
        var three = fourAccounts()
        three.accounts?.removeLast()
        #expect(MenuLogic.keyCommand(digit: 4, state: three) == nil)
        #expect(MenuLogic.keyCommand(digit: 2, state: nil) == nil)
    }

    @Test func topRowAndKeypadKeyCodesMapToDigits() {
        let codes: [(UInt16, Int)] = [(18, 1), (19, 2), (20, 3), (21, 4), (23, 5), (22, 6), (26, 7), (28, 8), (25, 9), (83, 1), (84, 2), (85, 3), (86, 4), (87, 5)]
        for (code, digit) in codes {
            #expect(MenuLogic.digit(forKeyCode: code) == digit)
        }
        for code: UInt16 in [29, 82, 0, 12, 53] {
            #expect(MenuLogic.digit(forKeyCode: code) == nil)
        }
    }

    @Test func keyActionsWhileTheMenuIsOpen() {
        let state = fourAccounts()
        func act(_ code: UInt16, _ characters: String? = nil, shift: Bool = false, command: Bool = false, option: Bool = false, control: Bool = false) -> KeyAction {
            MenuLogic.keyAction(keyCode: code, characters: characters, shift: shift, command: command, option: option, control: control, state: state)
        }
        #expect(act(21) == .command(.switchTo(crew)))
        #expect(act(86) == .command(.switchTo(crew)))
        #expect(act(19) == .command(.switchTo(work)))
        #expect(act(18) == .ignore)
        #expect(act(21, shift: true) == .note("Lifeline switches only this Mac, so switch Studio there."))
        #expect(act(21, command: true) == .ignore)
        #expect(act(21, option: true) == .ignore)
        #expect(act(21, control: true) == .ignore)
        #expect(act(53) == .close)
        #expect(act(53, shift: true) == .close)
        #expect(act(12, "q", command: true) == .quit)
        #expect(act(12, "Q", command: true) == .quit)
        #expect(act(0, "q", command: true) == .quit)
        #expect(act(12, "a", command: true) == .ignore)
        #expect(act(12, "q") == .ignore)
        #expect(act(12, "q", command: true, option: true) == .ignore)
        #expect(act(0) == .ignore)
    }

    @Test func shiftWithNoDevboxSentenceDoesNothing() {
        var state = fourAccounts()
        state.dev = nil
        #expect(MenuLogic.keyAction(keyCode: 21, characters: "4", shift: true, command: false, option: false, control: false, state: state) == .ignore)
    }

    @Test func aDigitFromTheDevboxViewBringsBackThisMac() {
        #expect(MenuLogic.side(after: .command(.switchTo(crew)), on: .dev) == .mac)
        #expect(MenuLogic.side(after: .command(.switchTo(crew)), on: .mac) == .mac)
        #expect(MenuLogic.side(after: .note("Lifeline switches only this Mac, so switch Studio there."), on: .dev) == .dev)
        #expect(MenuLogic.side(after: .ignore, on: .dev) == .dev)
        #expect(MenuLogic.side(after: .close, on: .dev) == .dev)
    }
}

@Suite
struct MotionTests {
    @Test func reduceMotionTurnsEveryMotionOff() {
        let still = MotionPlan(reduceMotion: true)
        #expect(still.glide == nil)
        #expect(still.rowFade == nil)
        #expect(still.pairFade == nil)
        #expect(still.rise == nil)
        #expect(still.breathe == nil)
        #expect(!still.orb)
        #expect(!still.drains)
        #expect(still.switching == nil)
        #expect(still.countdownStep == 1)
    }

    @Test func theSwitchFollowsTheBoardFrameByFrame() throws {
        let timeline = try #require(MotionPlan(reduceMotion: false).switching)
        #expect(timeline.lift == 0.45)
        #expect(timeline.land == 0.45)
        #expect(timeline.total == 0.9)
        #expect(timeline.total == MotionPlan(reduceMotion: false).glide)
        #expect(timeline.landingEnds <= timeline.total)
        #expect(abs(timeline.landingEnds - 0.9) < 1e-9)
        #expect(abs(timeline.orbEnds - 0.9) < 1e-9)
        #expect(timeline.orb.map(\.opacity) == [0.9, 0.9, 0.7, 0])
        #expect(abs(timeline.orb.prefix(2).reduce(0) { $0 + $1.seconds } - timeline.lift) < 1e-9)
        #expect(timeline.dimmedHero == 0.35)
        #expect(MotionPlan(reduceMotion: false).countdownStep == nil)
    }

    @Test func theLandingGlowStartsAndEndsInsideTheSwitch() {
        let timeline = SwitchTimeline.approved
        #expect(timeline.landingStarts == timeline.lift)
        #expect(timeline.landingStarts + timeline.landingIn + timeline.landingFade <= timeline.total + 1e-9)
        #expect(timeline.frame(at: timeline.lift - 0.001).landingGlow == 0)
        #expect(abs(timeline.frame(at: timeline.lift + timeline.landingIn).landingGlow - 1) < 1e-9)
        for step in stride(from: timeline.total, through: 2.0, by: 0.01) {
            #expect(timeline.frame(at: step).landingGlow == 0)
            #expect(timeline.frame(at: step).arrivingGlow == 1)
        }
        #expect(timeline.frame(at: 0.89).landingGlow > 0)
    }

    @Test func theFirstFramesStillShowTheOldAccount() {
        let timeline = SwitchTimeline.approved
        for time in [0, 1.0 / 60, 0.12, 0.449] {
            let frame = timeline.frame(at: time)
            #expect(frame.showsOld)
            #expect(frame.rowLines == 0)
            #expect(frame.heroGrowth == 0)
            #expect(frame.arrivingBackground == 0)
            #expect(frame.arrivingGlow == 0)
            #expect(frame.landingGlow == 0)
            #expect(frame.textIn == 0)
            #expect(frame.textHeight == 0)
        }
        let first = timeline.frame(at: 0)
        #expect(first.leavingBackground == 1 && first.leavingGlow == 1 && first.heroLineOpacity == 1 && first.lightOpacity == 0)
        #expect(timeline.frame(at: 1.0 / 60).leavingBackground > 0.95)
    }

    @Test func linesHoldDuringTheLiftAndGlideAfterIt() {
        let timeline = SwitchTimeline.approved
        let release = timeline.frame(at: timeline.lift)
        #expect(!release.showsOld)
        #expect(release.heroGrowth == 0)
        #expect(release.rowLines == 0)
        #expect(release.heroLineOpacity == 1)
        var last = 0.0
        for step in stride(from: timeline.lift, through: timeline.total, by: 0.01) {
            let frame = timeline.frame(at: step)
            #expect(frame.rowLines >= last - 1e-9)
            #expect(frame.heroGrowth == frame.rowLines)
            last = frame.rowLines
        }
        let end = timeline.frame(at: timeline.total)
        #expect(end.finished && end.rowLines == 1 && end.heroGrowth == 1 && end.arrivingBackground == 1)
        #expect(end.textIn == 1 && end.textOut == 0 && end.textHeight == 1)
        #expect(end.lightOpacity == 0 && end.leavingBackground == 0 && end.leavingGlow == 0)
        #expect(timeline.frame(at: 1.05) == end)
        #expect(abs(timeline.frame(at: 0.2).heroLineOpacity - (1 - 0.65 * 0.2 / 0.45)) < 1e-9)
        #expect(abs(timeline.frame(at: 0.45 - 1e-6).heroLineOpacity - 0.35) < 1e-4)
    }

    @Test func outgoingAndIncomingTextNeverShareAFrameAndNoFrameIsEmpty() {
        let timeline = SwitchTimeline.approved
        #expect(abs(timeline.textEnds - 0.9) < 1e-9)
        #expect(abs(timeline.textInStarts - 0.6) < 1e-9)
        for step in stride(from: 0.0, through: 2.0, by: 0.001) {
            let frame = timeline.frame(at: step)
            #expect(frame.textOut + frame.textIn >= 0.5, "the head is nearly empty at \(step) s")
            #expect(frame.textOut == 0 || frame.textIn == 0 || frame.showsOld, "old and new text share the frame at \(step) s")
        }
        #expect(timeline.frame(at: 0.45).textOut == 1)
        #expect(timeline.frame(at: 0.55).textOut > 0.5 && timeline.frame(at: 0.55).textIn == 0)
        #expect(timeline.frame(at: 0.6).textOut == 0 && timeline.frame(at: 0.6).textIn == 0.5)
        #expect(timeline.frame(at: 0.75).textIn > 0.5 && timeline.frame(at: 0.75).textIn < 1)
        #expect(timeline.frame(at: 0.9).textIn == 1)
        let heights = stride(from: 0.45, through: 0.9, by: 0.01).map { timeline.frame(at: $0).textHeight }
        #expect(heights.first == 0 && heights.last == 1)
        #expect(zip(heights, heights.dropFirst()).allSatisfy { $0 <= $1 })
    }

    @Test func theLightFollowsTheBoardsOpacitySteps() {
        let timeline = SwitchTimeline.approved
        #expect(abs(timeline.frame(at: 0.12).lightOpacity - 0.9) < 1e-9)
        #expect(abs(timeline.frame(at: 0.45).lightOpacity - 0.9) < 1e-9)
        #expect(abs(timeline.frame(at: 0.7).lightOpacity - 0.7) < 1e-9)
        #expect(timeline.frame(at: 0.9).lightOpacity == 0)
        #expect(timeline.frame(at: 0).lightTravel == 0)
        #expect(timeline.frame(at: 0.9).lightTravel == 1)
        let short = SwitchTimeline(
            lift: 0.45, land: 0.45, landingIn: 0.15, landingFade: 0.3, textOutSeconds: 0.15, textInSeconds: 0.3,
            dimmedHero: 0.35, orb: [OrbStep(0.5, 0.9)]
        )
        #expect(abs(short.frame(at: 0.25).lightOpacity - 0.45) < 1e-9)
        #expect(short.frame(at: 0.6).lightOpacity == 0)
    }

    @Test func theGlideCurveIsTheApprovedEaseOut() {
        #expect(Curve.glide(0) == 0)
        #expect(Curve.glide(1) == 1)
        #expect(Curve.glide(0.5) > 0.8)
        var last = 0.0
        for step in stride(from: 0.0, through: 1.0, by: 0.05) {
            #expect(Curve.glide(step) >= last)
            last = Curve.glide(step)
        }
        #expect(abs(Curve.easeInOut(0.5) - 0.5) < 1e-6)
    }

    @Test func aStillPlanKeepsTheSwitchButDropsLoops() {
        let still = MotionPlan(reduceMotion: false).still
        #expect(still.switching == .approved)
        #expect(still.rise == nil && still.breathe == nil && !still.drains)
        #expect(still.glide == 0.9)
        #expect(MotionPlan(reduceMotion: true).still.switching == nil)
    }

    @Test func aSwitchStartsOnlyWhenTheSameViewChangesAccount() {
        let moving = MotionPlan(reduceMotion: false)
        #expect(MenuLogic.switchStarts(from: (.mac, alex), to: (.mac, crew), plan: moving) == .lift(from: alex, to: crew))
        #expect(MenuLogic.switchStarts(from: (.mac, alex), to: (.mac, alex), plan: moving) == nil)
        #expect(MenuLogic.switchStarts(from: (.mac, alex), to: (.dev, work), plan: moving) == nil)
        #expect(MenuLogic.switchStarts(from: (.mac, nil), to: (.mac, crew), plan: moving) == nil)
        #expect(MenuLogic.switchStarts(from: (.mac, alex), to: (.mac, nil), plan: moving) == nil)
        #expect(MenuLogic.switchStarts(from: (.mac, alex), to: (.mac, crew), plan: MotionPlan(reduceMotion: true)) == nil)
    }

    @Test func rowsHoldTheirBackgroundsThroughTheSwitch() {
        let lift = SwitchPhase.lift(from: alex, to: crew)
        #expect(!MenuLogic.rowIsActive(alex, modelSaysActive: false, phase: lift))
        #expect(!MenuLogic.rowIsActive(crew, modelSaysActive: true, phase: lift))
        #expect(MenuLogic.rowFadesOut(alex, phase: lift))
        #expect(!MenuLogic.rowFadesOut(crew, phase: lift))
        let land = SwitchPhase.land(crew)
        #expect(MenuLogic.rowIsActive(crew, modelSaysActive: true, phase: land))
        #expect(!MenuLogic.rowIsActive(alex, modelSaysActive: false, phase: land))
        #expect(!MenuLogic.rowFadesOut(alex, phase: land))
        #expect(MenuLogic.rowIsActive(crew, modelSaysActive: true, phase: nil))
        #expect(!MenuLogic.rowIsActive(work, modelSaysActive: false, phase: nil))
    }

    @Test func theGlideTakes0Point9SecondsOnTheApprovedCurve() {
        let moving = MotionPlan(reduceMotion: false)
        #expect(moving.glide == 0.9)
        #expect(moving.rowFade == 0.5)
        #expect(moving.pairFade == 0.4)
        #expect(moving.rise == 0.5)
        #expect(moving.breathe == 2.8)
        #expect(moving.orb)
        #expect(moving.drains)
        #expect(MotionPlan.curve == (0.22, 0.8, 0.24, 1.0))
    }
}

@Suite
struct FieldTests {
    @Test func percentStringsBecomeFractions() {
        #expect(Fraction.of("42%") == 0.42)
        #expect(Fraction.of("0%") == 0)
        #expect(Fraction.of("100%") == 1)
        #expect(Fraction.of("137%") == 1)
        #expect(Fraction.of("-5%") == 0)
        #expect(Fraction.of(nil) == 0)
        #expect(Fraction.of("") == 0)
        #expect(Fraction.of("nan%") == 0)
        #expect(Fraction.of("abc") == 0)
    }

    @Test func theCountdownDrainsFromItsEndTime() throws {
        let countdown = CountdownSummary(targetUuid: crew, endsAt: "2026-11-19T15:41:05Z", seconds: 5)
        let start = try #require(Timestamps.parse("2026-11-19T15:41:00Z"))
        #expect(countdown.remaining(at: start) == 1)
        #expect(abs(countdown.remaining(at: start.addingTimeInterval(2.1)) - 0.58) < 0.0001)
        #expect(countdown.remaining(at: start.addingTimeInterval(9)) == 0)
        #expect(countdown.remaining(at: start.addingTimeInterval(-3)) == 1)
        #expect(countdown.secondsLeft(at: start.addingTimeInterval(1)) == 4)
        #expect(countdown.secondsLeft(at: start.addingTimeInterval(7)) == 0)
        let fractional = CountdownSummary(targetUuid: crew, endsAt: "2026-11-19T15:41:05.250000+00:00", seconds: 5)
        #expect(abs(fractional.secondsLeft(at: start) - 5.25) < 0.0001)
        #expect(CountdownSummary(targetUuid: nil, endsAt: "not a time", seconds: 5).remaining(at: start) == 1)
    }

    @Test func theSwitchNoteKeepsOnlyWhatTheHeadlineDoesNotSay() {
        let name = "Team Account"
        #expect(MenuLogic.switchNote(message: "This Mac now uses Team Account.", toName: name) == nil)
        #expect(MenuLogic.switchNote(
            message: "This Mac now uses Team Account. A spare copy of its saved sign-in is still in Lifeline.",
            toName: name
        ) == "A spare copy of its saved sign-in is still in Lifeline.")
        #expect(MenuLogic.switchNote(message: "This Mac now uses the chosen account.", toName: nil) == "This Mac now uses the chosen account.")
        #expect(MenuLogic.switchNote(message: nil, toName: name) == nil)
        #expect(MenuLogic.switchNote(message: "  ", toName: name) == nil)
    }

    @Test func theIconLineStaysWithinItsTrackWhileItGlides() {
        let fields = IconFields(name: "Alex", percent: "42%", fill: 0.42, tone: "calm", amberLight: true)
        #expect(fields.withFill(0.5).fill == 0.5)
        #expect(fields.withFill(1.4).fill == 1)
        #expect(fields.withFill(-0.2).fill == 0)
        #expect(fields.withFill(0.5).name == "Alex")
        #expect(fields.withFill(0.5).amberLight)
    }

    @Test func theMacsActiveRowIsTheCurrentAccount() {
        #expect(model("none", active: crew).activeUuid == crew)
        #expect(MenuModel().activeUuid == nil)
    }
}

@Suite(.serialized)
struct ActionPostTests {
    @Test func aSwitchPostsTheUuid() throws {
        let reply = #"{"from_uuid": "\#(alex)", "to_uuid": "\#(crew)", "confirmed": true, "message": "This Mac now uses Team Account."}"#
        let server = try FakeServer { _, _ in [okReply(reply)] }
        defer { server.close() }
        let api = LifelineAPI(client: SocketClient(path: server.path))
        #expect(api.perform(.switchTo(crew), via: .row, names: [crew: "Team Account"]) == .done(note: nil))
        let seen = server.requests[0]
        #expect(seen.head.hasPrefix("POST /switch HTTP/1.1"))
        let body = try JSONSerialization.jsonObject(with: seen.body) as? [String: String]
        #expect(body == ["uuid": crew, "via": "row"])
    }

    @Test func aSwitchWithACaveatShowsIt() throws {
        let reply = #"{"to_uuid": "\#(crew)", "message": "This Mac now uses Team Account. A spare copy of its saved sign-in is still in Lifeline."}"#
        let server = try FakeServer { _, _ in [okReply(reply)] }
        defer { server.close() }
        let api = LifelineAPI(client: SocketClient(path: server.path))
        #expect(api.perform(.switchTo(crew), via: .footer, names: [crew: "Team Account"]) == .done(note: "A spare copy of its saved sign-in is still in Lifeline."))
    }

    @Test func signInAgainReturnsItsJob() throws {
        let server = try FakeServer { _, _ in [okReply(#"{"job": "abc123"}"#, status: "202 Accepted")] }
        defer { server.close() }
        let api = LifelineAPI(client: SocketClient(path: server.path))
        #expect(api.perform(.resign(alex), via: .row) == .accepted(job: "abc123"))
    }

    @Test func undoCancelAndSignInAgainUseTheirAddresses() throws {
        let server = try FakeServer { head, _ in
            if head.hasPrefix("POST /undo") {
                return [okReply(#"{"to_uuid": "\#(alex)", "message": "This Mac now uses Alex's Account."}"#)]
            }
            if head.hasPrefix("POST /resign/") {
                return [okReply(#"{"job": "abc"}"#, status: "202 Accepted")]
            }
            return [okReply(#"{"cancelled": true}"#)]
        }
        defer { server.close() }
        let api = LifelineAPI(client: SocketClient(path: server.path))
        #expect(api.perform(.undo, via: .menuUndo, names: [alex: "Alex's Account"]) == .done(note: nil))
        #expect(api.perform(.cancel, via: .notification) == .done(note: nil))
        #expect(api.perform(.resign(alex), via: .key) == .accepted(job: "abc"))
        let heads = server.requests.map(\.head)
        #expect(heads[0].hasPrefix("POST /undo HTTP/1.1"))
        #expect(heads[1].hasPrefix("POST /cancel HTTP/1.1"))
        #expect(heads[2].hasPrefix("POST /resign/\(alex) HTTP/1.1"))
        let bodies = server.requests.map { String(decoding: $0.body, as: UTF8.self) }
        #expect(bodies == [#"{"via":"menu-undo"}"#, "", ""])
    }

    @Test func aRefusalShowsTheServiceSentence() throws {
        let server = try FakeServer { _, _ in [okReply(#"{"error": "There is no switch to undo."}"#, status: "409 Conflict")] }
        defer { server.close() }
        let api = LifelineAPI(client: SocketClient(path: server.path))
        #expect(api.perform(.undo, via: .menuUndo) == .refused("There is no switch to undo."))
    }

    @Test func aRefusalWithoutASentenceStillGivesOne() throws {
        let server = try FakeServer { _, _ in [okReply("<html>", status: "500 Internal Server Error")] }
        defer { server.close() }
        let api = LifelineAPI(client: SocketClient(path: server.path))
        #expect(api.perform(.cancel, via: .footer) == .refused(Sentences.unreadable))
    }

    @Test func aSlowServiceStopsAtTheLimit() throws {
        let server = try FakeServer { _, _ in
            Thread.sleep(forTimeInterval: 1.5)
            return []
        }
        defer { server.close() }
        let began = Date()
        let api = LifelineAPI(client: SocketClient(path: server.path, limit: 0.4), switchLimit: 0.4)
        #expect(api.perform(.switchTo(crew), via: .row) == .refused(Sentences.slowSwitch))
        #expect(Date().timeIntervalSince(began) < 1.2)
    }

    @Test func noServiceSaysSo() throws {
        let folder = try FakeServer.makeFolder()
        defer { try? FileManager.default.removeItem(atPath: folder) }
        let api = LifelineAPI(client: SocketClient(path: folder + "/none.sock"))
        #expect(api.perform(.switchTo(crew), via: .row) == .refused(Sentences.notRunning))
    }

    @Test func everySentenceIsAFullSentence() {
        for sentence in [Sentences.notRunning, Sentences.noMenu, Sentences.timedOut, Sentences.unreadable, Sentences.lastRead("15:41")] {
            #expect(sentence.first?.isUppercase == true)
            #expect(sentence.hasSuffix("."))
        }
    }
}

@Suite
struct PendingResultTests {
    func withActive(_ uuid: String?) -> ServiceState {
        var state = fourAccounts()
        state.mac = uuid.map { model("none", active: $0) } ?? MenuModel()
        return state
    }

    @Test func aSlowSwitchIsSettledWhenTheMacShowsTheTarget() {
        let check = MenuLogic.pendingCheck(for: .switchTo(crew), state: withActive(alex))
        #expect(check == .switchTo(crew))
        #expect(!MenuLogic.resolves(check, state: withActive(alex)))
        #expect(!MenuLogic.resolves(check, state: withActive(work)))
        #expect(MenuLogic.resolves(check, state: withActive(crew)))
        #expect(!MenuLogic.resolves(check, state: withActive(nil)))
    }

    @Test func aSlowUndoIsSettledWhenTheAccountChanges() {
        let check = MenuLogic.pendingCheck(for: .undo, state: withActive(crew))
        #expect(check == .undo(from: crew))
        #expect(!MenuLogic.resolves(check, state: withActive(crew)))
        #expect(MenuLogic.resolves(check, state: withActive(alex)))
        #expect(!MenuLogic.resolves(check, state: withActive(nil)))
    }

    @Test func aSlowCancelIsSettledWhenTheCountdownIsGone() {
        var counting = withActive(alex)
        counting.mac?.countdown = true
        counting.countdown = CountdownSummary(targetUuid: crew, endsAt: "2026-11-19T15:41:05Z", seconds: 5)
        #expect(!MenuLogic.resolves(.cancel, state: counting))
        #expect(MenuLogic.resolves(.cancel, state: withActive(alex)))
    }

    @Test func aSlowSignInIsSettledWhenItsJobAppears() {
        var state = withActive(alex)
        #expect(!MenuLogic.resolves(.resign(alex), state: state))
        state.jobs = [JobSummary(id: "other", kind: "resign", uuid: work, state: "running", result: "")]
        #expect(!MenuLogic.resolves(.resign(alex), state: state))
        state.jobs?.append(JobSummary(id: "mine", kind: "resign", uuid: alex, state: "running", result: ""))
        #expect(MenuLogic.resolves(.resign(alex), state: state))
    }

    @Test func aFinishedJobGivesItsSentenceOnce() {
        var state = withActive(alex)
        #expect(JobFlow.results(state, seen: []).isEmpty)
        state.jobs = [JobSummary(id: "j1", kind: "resign", uuid: alex, state: "running", result: "")]
        #expect(JobFlow.results(state, seen: []).isEmpty)
        state.jobs = [JobSummary(id: "j1", kind: "resign", uuid: alex, state: "done", result: "Alex's Account has a new sign-in.")]
        #expect(JobFlow.results(state, seen: []).map(\.id) == ["j1"])
        #expect(JobFlow.results(state, seen: ["j1"]).isEmpty)
        state.jobs = [JobSummary(id: "j1", kind: "resign", uuid: alex, state: "failed", result: "")]
        #expect(JobFlow.results(state, seen: []).isEmpty)
    }

    @Test func jobsDecodeFromTheState() throws {
        let text = sampleState.replacingOccurrences(
            of: "\"jobs\": [],",
            with: "\"jobs\": [{\"id\": \"j1\", \"kind\": \"resign\", \"uuid\": \"\(alex)\", \"state\": \"done\", \"result\": \"Alex's Account has a new sign-in.\", \"started_at\": \"2026-11-19T15:41:00Z\", \"finished_at\": null}],"
        )
        let state = try ServiceState.decode(Data(text.utf8))
        #expect(state.jobs?.count == 1)
        #expect(JobFlow.results(state, seen: []).first?.result == "Alex's Account has a new sign-in.")
    }
}
