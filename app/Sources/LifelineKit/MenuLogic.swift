import CoreGraphics
import Foundation

public enum MenuSide: String, Sendable, Equatable {
    case mac
    case dev
}

public enum ActionStyle: Sendable, Equatable {
    case none
    case primary
    case quiet
}

public enum MenuCommand: Sendable, Equatable {
    case switchTo(String)
    case undo
    case cancel
    case resign(String)

    public var switches: Bool {
        switch self {
        case .switchTo, .undo: return true
        case .cancel, .resign: return false
        }
    }
}

public struct SwitchGuard: Sendable, Equatable {
    public private(set) var busy = false
    private var settleAfter: Int?
    private var started = 0

    public init() {}

    public mutating func begin() -> Bool {
        guard !busy else { return false }
        busy = true
        settleAfter = nil
        return true
    }

    public mutating func fetchStarted() -> Int {
        started += 1
        return started
    }

    public mutating func replied() {
        if busy {
            settleAfter = started
        }
    }

    public mutating func fetched(_ number: Int) {
        if let after = settleAfter, number > after {
            settleAfter = nil
            busy = false
        }
    }
}

public enum Via: String, Sendable, Equatable {
    case row
    case footer
    case key
    case notification
    case menuUndo = "menu-undo"

    public static func footer(_ command: MenuCommand) -> Via {
        command == .undo ? .menuUndo : .footer
    }
}

public enum RowClick: Sendable, Equatable {
    case run(MenuCommand)
    case detail(String)
    case nothing
}

public enum FieldKey: Sendable, Equatable {
    case cancel
    case other
}

public enum DetailMove: Sendable, Equatable {
    case open
    case close
    case previous
    case next
}

public enum KeyAction: Sendable, Equatable {
    case command(MenuCommand)
    case note(String)
    case detail(DetailMove)
    /// Up or Down moved the keyboard highlight to this row.
    case highlight(String)
    /// The right arrow opens the highlighted row.
    case openRow(String)
    /// Return with no highlight presses the footer's add button.
    case heroAdd(HeroAdd)
    case close
    case back
    case quit
    case ignore
}

public enum ActionOutcome: Sendable, Equatable {
    case done(note: String?)
    case accepted(job: String)
    case refused(String)
}

public enum PendingCheck: Sendable, Equatable {
    case switchTo(String)
    case undo(from: String?)
    case cancel
    case resign(String)
}

public enum Sentences {
    public static let notRunning = "Lifeline's service is not running. Lifeline is starting it again."
    public static let noMenu = "Lifeline is running but could not build the menu. It tries again in 2 seconds."
    public static let timedOut = "Lifeline did not answer within 2 seconds, so the result is not known yet."
    public static let slowSwitch = "Lifeline did not answer within 30 seconds, so the result is not known yet."

    public static func unknownResult(_ sentence: String?) -> Bool {
        sentence == timedOut || sentence == slowSwitch || sentence == slowStart
    }

    public static let slowStart = "Lifeline did not answer within 15 seconds, so the result is not known yet."
    public static let unreadable = "Lifeline answered, but the menu could not read the answer."

    public static func lastRead(_ time: String) -> String {
        "The service did not answer. Last read at \(time)."
    }
}

public struct OrbStep: Sendable, Equatable {
    public let seconds: Double
    public let opacity: Double

    public init(_ seconds: Double, _ opacity: Double) {
        self.seconds = seconds
        self.opacity = opacity
    }
}

public enum Curve {
    public static func glide(_ x: Double) -> Double {
        bezier(x, MotionPlan.curve.0, MotionPlan.curve.1, MotionPlan.curve.2, MotionPlan.curve.3)
    }

    public static func easeInOut(_ x: Double) -> Double {
        bezier(x, 0.42, 0, 0.58, 1)
    }

    public static func bezier(_ x: Double, _ x1: Double, _ y1: Double, _ x2: Double, _ y2: Double) -> Double {
        let x = min(max(x, 0), 1)
        if x == 0 || x == 1 {
            return x
        }
        func along(_ s: Double, _ a: Double, _ b: Double) -> Double {
            let u = 1 - s
            return 3 * u * u * s * a + 3 * u * s * s * b + s * s * s
        }
        var low = 0.0
        var high = 1.0
        var s = x
        for _ in 0..<50 {
            let value = along(s, x1, x2)
            if abs(value - x) < 1e-9 {
                break
            }
            if value < x {
                low = s
            } else {
                high = s
            }
            s = (low + high) / 2
        }
        return along(s, y1, y2)
    }
}

public struct SwitchFrame: Sendable, Equatable {
    public let showsOld: Bool
    public let heroGrowth: Double
    public let heroLineOpacity: Double
    public let headMuted: Double
    public let textOut: Double
    public let textIn: Double
    public let textHeight: Double
    public let rowLines: Double
    public let leavingBackground: Double
    public let leavingGlow: Double
    public let arrivingBackground: Double
    public let arrivingGlow: Double
    public let landingGlow: Double
    public let lightTravel: Double
    public let lightOpacity: Double
    public let finished: Bool
}

public struct SwitchTimeline: Sendable, Equatable {
    public let lift: Double
    public let land: Double
    public let landingIn: Double
    public let landingFade: Double
    public let textOutSeconds: Double
    public let textInSeconds: Double
    public let dimmedHero: Double
    public let orb: [OrbStep]

    public static let approved = SwitchTimeline(
        lift: 0.45,
        land: 0.45,
        landingIn: 0.15,
        landingFade: 0.3,
        textOutSeconds: 0.15,
        textInSeconds: 0.3,
        dimmedHero: 0.35,
        orb: [OrbStep(0.12, 0.9), OrbStep(0.33, 0.9), OrbStep(0.25, 0.7), OrbStep(0.2, 0)]
    )

    /// Old text dims to this strength, then the new text takes its place at the same strength and brightens, so the
    /// two are never drawn together and no frame is without words.
    public static let textFloor = 0.5

    public var total: Double { lift + land }
    public var landingStarts: Double { lift }
    public var landingEnds: Double { lift + landingIn + landingFade }
    public var orbEnds: Double { orb.reduce(0) { $0 + $1.seconds } }
    public var textInStarts: Double { lift + textOutSeconds }
    public var textEnds: Double { textInStarts + textInSeconds }

    public func frame(at time: Double) -> SwitchFrame {
        let t = max(time, 0)
        let lifting = t < lift
        let liftDone = lift > 0 ? min(t / lift, 1) : 1
        let landDone = land > 0 ? min(max((t - lift) / land, 0), 1) : 1
        let landingPeak = lift + landingIn
        let landing: Double
        let arrivingGlow: Double
        if t < lift {
            landing = 0
            arrivingGlow = 0
        } else if t < landingPeak {
            landing = landingIn > 0 ? (t - lift) / landingIn : 1
            arrivingGlow = 0
        } else if t < landingEnds {
            let fade = landingFade > 0 ? (t - landingPeak) / landingFade : 1
            landing = 1 - fade
            arrivingGlow = fade
        } else {
            landing = 0
            arrivingGlow = 1
        }
        var light = 0.0
        var elapsed = 0.0
        var previous = 0.0
        for step in orb {
            if t <= elapsed + step.seconds {
                let part = step.seconds > 0 ? (t - elapsed) / step.seconds : 1
                light = previous + (step.opacity - previous) * part
                break
            }
            elapsed += step.seconds
            previous = step.opacity
            light = step.opacity
        }
        return SwitchFrame(
            showsOld: lifting,
            heroGrowth: lifting ? 0 : Curve.glide(landDone),
            heroLineOpacity: lifting ? 1 - (1 - dimmedHero) * liftDone : 1,
            headMuted: lifting ? liftDone : 1,
            textOut: lifting ? 1 : (t < textInStarts ? 1 - (1 - Self.textFloor) * Curve.easeInOut(textOutSeconds > 0 ? min((t - lift) / textOutSeconds, 1) : 1) : 0),
            textIn: t < textInStarts ? 0 : Self.textFloor + (1 - Self.textFloor) * Curve.easeInOut(textInSeconds > 0 ? min((t - textInStarts) / textInSeconds, 1) : 1),
            textHeight: lifting ? 0 : Curve.easeInOut(landDone),
            rowLines: lifting ? 0 : Curve.glide(landDone),
            leavingBackground: lifting ? 1 - liftDone : 0,
            leavingGlow: lifting ? 1 - liftDone : 0,
            arrivingBackground: lifting ? 0 : Curve.easeInOut(landDone),
            arrivingGlow: arrivingGlow,
            landingGlow: max(landing, 0),
            lightTravel: total > 0 ? Curve.glide(min(t / total, 1)) : 1,
            lightOpacity: t >= orbEnds ? 0 : light,
            finished: t >= total
        )
    }
}

public enum ScreenMove: Sendable, Equatable {
    case forward
    case back
}

public struct ScreenOffsets: Sendable, Equatable {
    public let incoming: Double
    public let outgoing: Double

    public init(incoming: Double, outgoing: Double) {
        self.incoming = incoming
        self.outgoing = outgoing
    }
}

public struct ScreenMotion: Sendable, Equatable {
    public let response: Double?
    public let damping: Double
    public let fade: Double?
    public let slides: Bool
    public let heightEases: Bool
    public let settle: Double

    public init(reduceMotion: Bool) {
        if reduceMotion {
            response = nil
            damping = 1
            fade = 0.15
            slides = false
            heightEases = false
            settle = 0.15
        } else {
            response = 0.32
            damping = 1
            fade = nil
            slides = true
            heightEases = true
            settle = 0.45
        }
    }

    public static func move(to key: String) -> ScreenMove {
        key == "menu" ? .back : .forward
    }

    public static func offsets(_ move: ScreenMove, width: Double, slides: Bool) -> ScreenOffsets {
        guard slides else { return ScreenOffsets(incoming: 0, outgoing: 0) }
        let far = (width * 0.22).rounded()
        let near = -(width * 0.08).rounded()
        return move == .forward ? ScreenOffsets(incoming: far, outgoing: near) : ScreenOffsets(incoming: near, outgoing: far)
    }

    /// The incoming screen shows only once the outgoing one is gone, from half strength, so the two never stack.
    public static func incomingOpacity(_ progress: Double) -> Double {
        let p = min(max(progress, 0), 1)
        return p < 0.5 ? 0 : p
    }

    /// The outgoing screen dims to half strength by the middle of the move, then leaves.
    public static func outgoingOpacity(_ progress: Double) -> Double {
        let p = min(max(progress, 0), 1)
        return p < 0.5 ? 1 - p : 0
    }
}

public struct MotionPlan: Sendable, Equatable {
    public static let curve = (0.22, 0.8, 0.24, 1.0)

    public let glide: Double?
    public let rowFade: Double?
    public let pairFade: Double?
    public let rise: Double?
    public let breathe: Double?
    public let orb: Bool
    public let drains: Bool
    public let switching: SwitchTimeline?
    public let countdownStep: Double?
    public let screens: ScreenMotion

    public init(reduceMotion: Bool) {
        screens = ScreenMotion(reduceMotion: reduceMotion)
        if reduceMotion {
            glide = nil
            rowFade = nil
            pairFade = nil
            rise = nil
            breathe = nil
            orb = false
            drains = false
            switching = nil
            countdownStep = 1
        } else {
            glide = 0.9
            rowFade = 0.5
            pairFade = 0.4
            rise = 0.5
            breathe = 2.8
            orb = true
            drains = true
            switching = .approved
            countdownStep = nil
        }
    }

    private init(copying plan: MotionPlan, rise: Double?, breathe: Double?, drains: Bool) {
        glide = plan.glide
        rowFade = plan.rowFade
        pairFade = plan.pairFade
        self.rise = rise
        self.breathe = breathe
        orb = plan.orb
        self.drains = drains
        switching = plan.switching
        countdownStep = plan.countdownStep
        screens = plan.screens
    }

    public var still: MotionPlan {
        MotionPlan(copying: self, rise: nil, breathe: nil, drains: false)
    }
}

public enum HeroAdd: Sendable, Equatable {
    case add(known: String?)
    case adopt
}

public enum ScrollBar {
    public static let shown: Double = 1.2
    public static let fade: Double = 0.3

    public static func strength(since: Date?, now: Date) -> Double {
        guard let since else { return 0 }
        let age = now.timeIntervalSince(since)
        if age <= shown {
            return 1
        }
        return max(0, 1 - (age - shown) / fade)
    }
}

public enum SwitchPhase: Sendable, Equatable {
    case lift(from: String, to: String)
    case land(String)
}

public enum MenuLogic {
    static let digits: [UInt16: Int] = [
        18: 1, 19: 2, 20: 3, 21: 4, 23: 5, 22: 6, 26: 7, 28: 8, 25: 9,
        83: 1, 84: 2, 85: 3, 86: 4, 87: 5, 88: 6, 89: 7, 91: 8, 92: 9
    ]
    static let escape: UInt16 = 53
    static let leftArrow: UInt16 = 123
    static let rightArrow: UInt16 = 124
    static let downArrow: UInt16 = 125
    static let upArrow: UInt16 = 126
    static let returnKey: UInt16 = 36
    static let enterKey: UInt16 = 76

    public static func actionStyle(_ model: MenuModel?, side: MenuSide) -> ActionStyle {
        guard side == .mac, let kind = model?.actionKind else { return .none }
        switch kind {
        case "primary", "resign", "add", "adopt":
            return (model?.actionText ?? "").isEmpty ? .none : .primary
        case "quiet", "undo", "cancel":
            return (model?.actionText ?? "").isEmpty ? .none : .quiet
        default:
            return .none
        }
    }

    public static func actionCommand(_ model: MenuModel?, side: MenuSide) -> MenuCommand? {
        guard side == .mac, let model, let kind = model.actionKind else { return nil }
        let target = model.actionTarget ?? ""
        switch kind {
        case "primary", "quiet":
            return target.isEmpty ? nil : .switchTo(target)
        case "resign":
            return target.isEmpty ? nil : .resign(target)
        case "undo":
            return .undo
        case "cancel":
            return .cancel
        default:
            return nil
        }
    }

    public static func heroAdd(_ model: MenuModel?, side: MenuSide) -> HeroAdd? {
        guard side == .mac, let model, !(model.actionText ?? "").isEmpty else { return nil }
        switch model.actionKind {
        case "add":
            let known = (model.unaddedKnown?.name ?? "").trimmingCharacters(in: .whitespaces)
            return .add(known: known.isEmpty ? nil : known)
        case "adopt":
            return .adopt
        default:
            return nil
        }
    }

    /// The scroll offset that shows every row in keep, the row in use first. When they cannot all fit, the first one wins.
    public static func keepInView(tops: [String: Double], bottoms: [String: Double], keep: [String], viewport: Double, content: Double) -> Double {
        let spans = keep.compactMap { id in tops[id].flatMap { top in bottoms[id].map { (top, $0) } } }
        guard let first = spans.first, viewport > 0 else { return 0 }
        let most = max(content - viewport, 0)
        let top = spans.map(\.0).min() ?? first.0
        let bottom = spans.map(\.1).max() ?? first.1
        if bottom - top <= viewport + 0.5 {
            return min(max(bottom - viewport, 0), most)
        }
        return min(max(first.1 - viewport, 0), min(first.0, most))
    }

    public static func showsCountdown(_ model: MenuModel?, side: MenuSide) -> Bool {
        side == .mac && model?.countdown == true && model?.actionKind == "cancel"
    }

    public static func rowClick(_ row: MenuRow, side: MenuSide, secondary: Bool, open: Bool = false, chevron: Bool = false) -> RowClick {
        guard !row.uuid.isEmpty else { return .nothing }
        if secondary || open || chevron || side == .dev {
            return .detail(row.uuid)
        }
        if let command = rowCommand(row, side: side) {
            return .run(command)
        }
        return .detail(row.uuid)
    }

    /// A row that needs a new sign-in, or that Lifeline cannot read, signs in again; a switch to it could not succeed.
    public static func rowCommand(_ row: MenuRow, side: MenuSide) -> MenuCommand? {
        guard side == .mac, !row.uuid.isEmpty else { return nil }
        if row.needsSignin == true || row.cannotRead == true {
            return .resign(row.uuid)
        }
        guard row.isActive != true else { return nil }
        return .switchTo(row.uuid)
    }

    public static func digit(forKeyCode code: UInt16) -> Int? {
        digits[code]
    }

    public static func keyCommand(digit: Int, state: ServiceState?) -> MenuCommand? {
        guard (1...9).contains(digit), let state else { return nil }
        guard let account = state.accounts?.first(where: { $0.number == digit }) else { return nil }
        if account.needsSignin == true || account.cannotRead == true {
            return .resign(account.uuid)
        }
        if account.uuid == state.mac?.activeUuid {
            return nil
        }
        return .switchTo(account.uuid)
    }

    /// What a key does while the menu shows. Escape cancels a countdown before anything else and keeps the menu
    /// open; Command Z undoes while Undo shows; Up and Down move a highlight between rows (or the open row, when one is
    /// open); Return switches to the highlighted row, or presses the footer's main button when nothing is highlighted;
    /// the right arrow opens the highlighted row.
    public static func keyAction(
        keyCode: UInt16, characters: String?, shift: Bool, command: Bool, option: Bool, control: Bool, state: ServiceState?,
        detailOpen: Bool = false, side: MenuSide = .mac, highlight: String? = nil, undoShows: Bool = true
    ) -> KeyAction {
        let model = state?.model(for: side)
        if keyCode == escape {
            if showsCountdown(state?.mac, side: side) {
                return .command(.cancel)
            }
            return detailOpen ? .detail(.close) : .close
        }
        if command && !option && !control && characters?.lowercased() == "q" {
            return .quit
        }
        if command && !shift && !option && !control && characters?.lowercased() == "z" {
            let undo = side == .mac && model?.actionKind == "undo" && !(model?.actionText ?? "").isEmpty
            return undo && undoShows ? .command(.undo) : .ignore
        }
        if !shift && !command && !option && !control {
            let rows = model?.rows ?? []
            let lit = highlight.flatMap { id in rows.first { $0.uuid == id } }
            switch keyCode {
            case rightArrow:
                if let lit {
                    return .openRow(lit.uuid)
                }
                return detailOpen ? .ignore : .detail(.open)
            case leftArrow:
                return detailOpen ? .detail(.close) : .ignore
            case upArrow, downArrow:
                let move = keyCode == downArrow ? 1 : -1
                if detailOpen {
                    return .detail(move > 0 ? .next : .previous)
                }
                return highlightStep(from: lit?.uuid, by: move, rows: rows).map { .highlight($0) } ?? .ignore
            case returnKey, enterKey:
                if let lit {
                    return rowCommand(lit, side: side).map { .command($0) } ?? .ignore
                }
                if let main = actionCommand(model, side: side) {
                    return .command(main)
                }
                return heroAdd(model, side: side).map { .heroAdd($0) } ?? .ignore
            default:
                break
            }
        }
        guard let digit = digit(forKeyCode: keyCode), !command, !option, !control else {
            return .ignore
        }
        if shift {
            let hint = state?.dev?.hint ?? ""
            return hint.isEmpty ? .ignore : .note(hint)
        }
        guard let found = keyCommand(digit: digit, state: state) else { return .ignore }
        return .command(found)
    }

    /// The next highlighted row: Down from nothing takes the first row and Up the last, and the highlight stops at either
    /// end, as the open row does.
    public static func highlightStep(from uuid: String?, by offset: Int, rows: [MenuRow]) -> String? {
        let ids = rows.map(\.uuid).filter { !$0.isEmpty }
        guard !ids.isEmpty else { return nil }
        guard let uuid, let index = ids.firstIndex(of: uuid) else { return offset > 0 ? ids.first : ids.last }
        return ids[min(max(index + offset, 0), ids.count - 1)]
    }

    public static func sheetKeyAction(keyCode: UInt16, characters: String?, command: Bool, holding: Bool = false, back: Bool = false) -> KeyAction {
        if keyCode == escape {
            return holding || back ? .back : .close
        }
        if command && characters?.lowercased() == "q" {
            return .quit
        }
        return .ignore
    }

    public static func detailTarget(model: MenuModel?) -> String? {
        model?.activeUuid ?? model?.rows?.first?.uuid
    }

    public static func expanded(after click: String, current: String?) -> String? {
        current == click ? nil : click
    }

    public static func menuOrigin(left: Double, top: Double, height: Double, shadow: Double) -> (x: Double, y: Double) {
        (left - shadow, top - height)
    }

    public static func panelLimit(top: Double, visibleBottom: Double, shadowTop: Double) -> Double {
        max(top - shadowTop - visibleBottom - 8, 200)
    }

    public static func listFades(offset: Double, content: Double, limit: Double) -> (top: Bool, bottom: Bool) {
        guard content > limit + 0.5 else { return (false, false) }
        return (offset < -0.5, offset + content > limit + 0.5)
    }

    public static func visibleCards(frames: [String: CGRect], viewport: Double) -> Set<String> {
        Set(frames.compactMap { id, frame in
            let inside = frame.minY >= -0.5 && frame.maxY <= viewport + 0.5
            let covers = frame.minY <= 0.5 && frame.maxY >= viewport - 0.5
            return inside || covers ? id : nil
        })
    }

    public static let flagLapse: Double = 10
    public static let undoWindow: Double = 5

    public static func undoShows(since: Date, until: Date?, now: Date) -> Bool {
        now < (until ?? since.addingTimeInterval(undoWindow))
    }

    public static func sameClick(time: Double, x: Double, y: Double, previous: (time: Double, x: Double, y: Double)?) -> Bool {
        guard let previous else { return false }
        return abs(time - previous.time) < 1 && hypot(x - previous.x, y - previous.y) < 8
    }

    public static func scrollIndicator(offset: Double, content: Double, viewport: Double) -> (top: Double, height: Double)? {
        guard viewport > 0, content > viewport + 0.5 else { return nil }
        let height = max(viewport * viewport / content, 24)
        let travel = viewport - height
        let fraction = min(max(-offset / (content - viewport), 0), 1)
        return (travel * fraction, height)
    }

    public static func panelArea(bounds: CGRect, top: Double, side: Double, bottom: Double) -> CGRect {
        CGRect(x: bounds.minX + side, y: bounds.minY + bottom, width: max(bounds.width - 2 * side, 0), height: max(bounds.height - top - bottom, 0))
    }

    public static let panelSlack: Double = 16

    public static func inside(_ point: CGPoint, panel: CGRect, frames: [CGRect]) -> Bool {
        panel.insetBy(dx: -panelSlack, dy: -panelSlack).contains(point) || frames.contains { $0.contains(point) }
    }

    public static func counted(_ head: String?, secondsLeft: Double?) -> String? {
        guard let head, let secondsLeft, let range = head.range(of: #" in \d+ seconds?\.$"#, options: .regularExpression) else {
            return head
        }
        let left = max(Int(secondsLeft.rounded(.up)), 1)
        return head.replacingCharacters(in: range, with: " in \(left) \(left == 1 ? "second" : "seconds").")
    }

    public static func countdownLeft(_ countdown: CountdownSummary?, fixed: Double?, now: Date, left: Int? = nil) -> Double? {
        if let countdown, let fixed {
            return fixed * (countdown.seconds ?? 5)
        }
        if let end = countdown?.endsAt.flatMap(Timestamps.parse) {
            return max(end.timeIntervalSince(now), 0)
        }
        return left.map(Double.init)
    }

    public static func listStops(tops: [Double], bottoms: [Double], open: Double?, content: Double) -> [Double] {
        guard let open else {
            return bottoms.filter { $0 > 0 }.sorted()
        }
        let below = bottoms.filter { $0 > open + 0.5 }.map { $0 - open }
        let above = tops.filter { $0 < open - 0.5 }.map { content - $0 }
        var stops: [Double] = []
        for stop in (below + above).sorted() where stops.last.map({ abs($0 - stop) > 0.5 }) ?? true {
            stops.append(stop)
        }
        return stops
    }

    /// A list cut short ends its viewport on the last whole row; the rest of its room becomes space above the footer,
    /// so the menu keeps its height whatever a card does.
    public static func rest(list: Double, natural: Double, stops: [Double]) -> (list: Double, spare: Double) {
        guard list < natural - 0.5, let stop = stops.filter({ $0 <= list + 0.5 && $0 > 0 }).max() else { return (list, 0) }
        return (stop, max(list - stop, 0))
    }

    public static func restingEdges(offset: Double, viewport: Double, tops: [Double], bottoms: [Double]) -> (top: Bool, bottom: Bool) {
        let shownTop = -offset
        let shownBottom = shownTop + viewport
        let top = shownTop < 0.5 || tops.contains { abs($0 - shownTop) < 1.5 }
        let bottom = bottoms.contains { abs($0 - shownBottom) < 1.5 }
        return (top, bottom)
    }

    public static func rowOutOfView(top: Double, bottom: Double, viewport: Double) -> Bool {
        top > viewport - 0.5 || bottom < 0.5
    }

    public static func fieldKey(_ keyCode: UInt16) -> FieldKey {
        keyCode == escape ? .cancel : .other
    }

    public static func side(after action: KeyAction, on side: MenuSide) -> MenuSide {
        if case .command = action {
            return .mac
        }
        return side
    }

    public static func switchNote(message: String?, toName: String?) -> String? {
        guard let message = message?.trimmingCharacters(in: .whitespacesAndNewlines), !message.isEmpty else {
            return nil
        }
        if let toName, !toName.isEmpty {
            let lead = "This Mac now uses \(toName)."
            if message.hasPrefix(lead) {
                let rest = message.dropFirst(lead.count).trimmingCharacters(in: .whitespaces)
                return rest.isEmpty ? nil : rest
            }
        }
        return message
    }

    public static let undoDelay: TimeInterval = 1
    public static let undoTarget = "undo"

    public static func armDelay(_ command: MenuCommand?) -> TimeInterval {
        command == .undo ? undoDelay : 0
    }

    public static func armed(shownAt: Date, now: Date, delay: TimeInterval) -> Bool {
        now.timeIntervalSince(shownAt) >= delay
    }

    public static func armed(shownAt: Date, settledAt: Date?, now: Date, delay: TimeInterval) -> Bool {
        armed(shownAt: max(shownAt, settledAt ?? shownAt), now: now, delay: delay)
    }

    public static func cappedHeights(
        cards: Double, top: Double, list: Double, footer: Double, limit: Double, spacing: Double, minimum: Double, cardMinimum: Double,
        listStops: [Double]
    ) -> [Double] {
        var bare = cappedHeights(cards: 0, top: top, list: list, footer: footer, limit: limit, spacing: spacing, minimum: minimum, cardMinimum: cardMinimum)
        if bare[2] < list - 0.5, let stop = listStops.filter({ $0 <= bare[2] + 0.5 && $0 > 0 }).max() {
            bare[2] = stop
        }
        guard cards > 0 else { return bare }
        let bareShown = bare.filter { $0 > 0 }.count
        let total = bare.reduce(0, +) + spacing * Double(max(bareShown - 1, 0))
        let shown = [top, list, footer].filter { $0 > 0 }.count + 1
        let room = total - top - footer - spacing * Double(max(shown - 1, 0))
        var keptCards = max(min(cards, room - min(minimum, list)), min(cards, cardMinimum))
        keptCards = min(keptCards, max(room, min(cards, cardMinimum)))
        var keptList = min(max(room - keptCards, 0), list)
        if keptList < list - 0.5, let stop = listStops.filter({ $0 <= keptList + 0.5 && $0 > 0 }).max(), cards - keptCards >= keptList - stop - 0.5 {
            keptCards += keptList - stop
            keptList = stop
        }
        return [keptCards, top, keptList, footer]
    }

    public static func cappedHeights(
        cards: Double, top: Double, list: Double, footer: Double, limit: Double, spacing: Double, minimum: Double, cardMinimum: Double
    ) -> [Double] {
        let shown = [cards, top, list, footer].filter { $0 > 0 }.count
        let room = limit - top - footer - spacing * Double(max(shown - 1, 0))
        guard cards + list > room else { return [cards, top, list, footer] }
        let keptList = max(min(list, room - cards), min(minimum, list))
        let keptCards = max(min(cards, room - keptList), min(cards, cardMinimum))
        return [keptCards, top, keptList, footer]
    }

    public static func armWait(shownAt: Date, settledAt: Date?, now: Date, delay: TimeInterval) -> TimeInterval {
        max(0, delay - now.timeIntervalSince(max(shownAt, settledAt ?? shownAt)))
    }

    public static func footerTakesClicks(switching: Bool, animating: Bool) -> Bool {
        !switching && !animating
    }

    public static func keepsNote(_ command: MenuCommand) -> Bool {
        switch command {
        case .switchTo, .undo:
            return true
        case .cancel, .resign:
            return false
        }
    }

    public static func freshSwitchNote(_ state: ServiceState, last: String?, showing: String?) -> String? {
        let note = (state.switchNote ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        guard !note.isEmpty, note != last else { return nil }
        if let showing, showing.contains(note) {
            return nil
        }
        return note
    }

    public static func joinNotes(_ first: String?, _ second: String?) -> String? {
        var parts: [String] = []
        for piece in [first, second] {
            let clean = (piece ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            if !clean.isEmpty, !parts.contains(clean) {
                parts.append(clean)
            }
        }
        return parts.isEmpty ? nil : parts.joined(separator: " ")
    }

    public static func switchStarts(
        from old: (side: MenuSide, active: String?), to new: (side: MenuSide, active: String?), plan: MotionPlan
    ) -> SwitchPhase? {
        guard plan.switching != nil, old.side == new.side,
              let gone = old.active, let next = new.active, gone != next else {
            return nil
        }
        return .lift(from: gone, to: next)
    }

    public static func rowIsActive(_ uuid: String, modelSaysActive: Bool, phase: SwitchPhase?) -> Bool {
        switch phase {
        case .lift?:
            return false
        case .land(let next)?:
            return uuid == next
        case nil:
            return modelSaysActive
        }
    }

    public static func rowFadesOut(_ uuid: String, phase: SwitchPhase?) -> Bool {
        if case .lift(let gone, _)? = phase {
            return uuid == gone
        }
        return false
    }

    public static func pendingCheck(for command: MenuCommand, state: ServiceState?) -> PendingCheck {
        switch command {
        case .switchTo(let uuid):
            return .switchTo(uuid)
        case .undo:
            return .undo(from: state?.mac?.activeUuid)
        case .cancel:
            return .cancel
        case .resign(let uuid):
            return .resign(uuid)
        }
    }

    public static func resolves(_ check: PendingCheck, state: ServiceState) -> Bool {
        switch check {
        case .switchTo(let uuid):
            return state.mac?.activeUuid == uuid
        case .undo(let from):
            guard let now = state.mac?.activeUuid else { return false }
            return now != from
        case .cancel:
            return state.countdown == nil && state.mac?.countdown != true
        case .resign(let uuid):
            return state.jobs?.contains { $0.kind == "resign" && $0.uuid == uuid } == true
        }
    }
}
