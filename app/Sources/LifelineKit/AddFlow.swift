import Foundation

public enum AddKind: String, Sendable, Equatable {
    case add
    case adopt
}

public enum AddStep: Sendable, Equatable {
    case start
    case waiting(AddKind)
    case added(String)
    case failed(String)
    case done(String)
}

public enum AddTarget: String, Sendable, Equatable, CaseIterable {
    case mac
    case devbox
    case both
}

public enum AddStage: Sendable, Equatable {
    case macAdd
    case macAdopt
    case devboxAdd
    case devboxAdopt

    public var route: String {
        switch self {
        case .macAdd: return "/add"
        case .macAdopt: return "/adopt"
        case .devboxAdd: return "/devbox/add"
        case .devboxAdopt: return "/devbox/adopt"
        }
    }

    public var onDevbox: Bool {
        self == .devboxAdd || self == .devboxAdopt
    }

    public var opensBrowser: Bool {
        self == .macAdd || self == .devboxAdd
    }

    public var kind: AddKind {
        self == .macAdd || self == .devboxAdd ? .add : .adopt
    }

    public var jobKind: String {
        switch self {
        case .macAdd: return "add"
        case .macAdopt: return "adopt"
        case .devboxAdd: return "devbox-add"
        case .devboxAdopt: return "devbox-adopt"
        }
    }
}

public enum AddAction: Sendable, Equatable {
    case none
    case start(AddStage)
    case place(String, String)
}

public enum JobOutcome: Sendable, Equatable {
    case running
    case done(uuid: String?, result: String)
    case failed(String)
}

public struct AddRun: Sendable, Equatable {
    public let target: AddTarget
    public let kind: AddKind
    public private(set) var stages: [AddStage]
    public private(set) var index = 0
    public private(set) var job: String?
    public private(set) var jobs: [String] = []
    public private(set) var seen = false
    public private(set) var started: Date?
    public private(set) var uuid: String?
    public private(set) var devboxResult: String?
    public private(set) var devboxFailed = false
    public private(set) var devboxSkipped = false
    public private(set) var failure: String?
    public private(set) var finished = false
    public private(set) var pick: KnownAccount?
    public private(set) var expected: String?
    public private(set) var macExpected: String?

    public init(target: AddTarget, kind: AddKind, stages: [AddStage], pick: KnownAccount?, expected: String? = nil, macExpected: String? = nil) {
        self.init(target: target, kind: kind, stages: stages)
        self.pick = pick
        self.expected = expected
        self.macExpected = macExpected
    }

    public init(target: AddTarget, devboxStage: AddStage, uuid: String) {
        self.init(target: target, kind: .add, stages: [devboxStage])
        self.uuid = uuid
    }

    public init(target: AddTarget, kind: AddKind, stages: [AddStage]) {
        self.target = target
        self.kind = kind
        self.stages = stages
        if stages.isEmpty {
            finished = true
            failure = AddFlow.unnamedFailure
        }
    }

    public var current: AddStage? {
        finished || index >= stages.count ? nil : stages[index]
    }

    public var waiting: Bool {
        !finished && current != nil
    }

    public var step: AddStep {
        if finished {
            if let uuid {
                return .added(uuid)
            }
            if let failure {
                return .failed(failure)
            }
            if let devboxResult {
                return devboxFailed ? .failed(devboxResult) : .done(devboxResult)
            }
            return .failed(AddFlow.unnamedFailure)
        }
        return .waiting(current?.kind ?? kind)
    }

    public func body(for stage: AddStage) -> [String: Any] {
        var body: [String: Any] = [:]
        switch stage {
        case .devboxAdd:
            if let uuid {
                body["uuid"] = uuid
            }
        case .macAdd:
            if let pick {
                body["known"] = pick.name
            }
        case .devboxAdopt:
            if let account = kind == .add ? uuid : expected {
                body["expected"] = account
            }
        case .macAdopt:
            if let macExpected {
                body["expected"] = macExpected
            }
        }
        if stages.count > 1, let place = stages.firstIndex(of: stage) {
            body["step"] = [place + 1, stages.count]
        }
        return body
    }

    public mutating func began(_ id: String, at time: Date) {
        job = id
        jobs.append(id)
        seen = false
        started = time
    }

    public mutating func refused(_ sentence: String) -> AddAction {
        if current?.onDevbox == true, uuid != nil || index > 0 {
            devboxResult = sentence
            devboxFailed = true
        } else {
            failure = sentence
        }
        return finish()
    }

    public mutating func advance(state: ServiceState?, now: Date) -> AddAction {
        guard !finished, let stage = current, let job else { return .none }
        if state?.jobs?.contains(where: { $0.id == job }) == true {
            seen = true
        }
        switch AddFlow.outcome(job: job, state: state, seen: seen, started: started, now: now) {
        case .running:
            return .none
        case .failed(let sentence):
            if stage.onDevbox {
                devboxResult = sentence
                devboxFailed = true
            } else {
                failure = sentence
            }
            return finish()
        case .done(let found, let result):
            if stage.onDevbox {
                devboxResult = result
            } else if let found, !found.isEmpty {
                uuid = found
            } else {
                failure = result
                return finish()
            }
            index += 1
            self.job = nil
            seen = false
            started = nil
            guard let next = current else { return finish() }
            guard kind == .add, next.onDevbox else { return .start(next) }
            guard let uuid else {
                failure = AddFlow.failed(state)
                return finish()
            }
            guard let second = AddFlow.secondStage(for: uuid, planned: next, state: state) else {
                devboxResult = state?.accounts?.first { $0.uuid == uuid }?.devboxAlready ?? ""
                devboxSkipped = true
                index += 1
                return finish()
            }
            stages[index] = second
            return .start(second)
        }
    }

    private mutating func finish() -> AddAction {
        finished = true
        job = nil
        if target == .devbox, kind == .add, let uuid, devboxResult != nil, !devboxFailed {
            return .place(uuid, Placement.devbox)
        }
        return .none
    }
}

public enum AddFlow {
    /// The label of the footer button and the sheet's title until the core's add_text arrives.
    public static let title = "Add an account"
    public static let tryAgain = "Try again"
    /// A failure with no sentence of its own yet; the screen draws the core's add_text.failed for it.
    public static let unnamedFailure = ""

    static func core(_ text: String?) -> String {
        (text ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
    }

    public static func sheetTitle(_ state: ServiceState?) -> String {
        let given = core(state?.addText?.title)
        return given.isEmpty ? title : given
    }

    public static func lead(_ state: ServiceState?) -> String {
        core(state?.addText?.lead)
    }

    public static func twice(_ state: ServiceState?) -> String {
        core(state?.addText?.twice)
    }

    public static func forDevbox(_ state: ServiceState?) -> String {
        core(state?.addText?.forDevbox)
    }

    public static func pageNotOpenedAbove(_ state: ServiceState?) -> String {
        core(state?.addText?.pageNotOpenedAbove)
    }

    public static func failed(_ state: ServiceState?) -> String {
        core(state?.addText?.failed)
    }

    public static func lost(_ state: ServiceState?) -> String {
        core(state?.addText?.lost)
    }

    /// A failure sentence to draw: the run's own, or the core's failed sentence for a run that ended without one.
    public static func failure(_ sentence: String, state: ServiceState?) -> String {
        let clean = core(sentence)
        return clean.isEmpty ? failed(state) : clean
    }

    /// The note when a start gets no answer in time: the core's template with the app's own wait, or the service-down
    /// sentence Swift keeps for when no state has arrived.
    public static func slowAnswer(_ state: ServiceState?) -> String {
        let template = core(state?.addText?.slowAnswer)
        guard !template.isEmpty else { return Sentences.slowStart }
        return template.replacingOccurrences(of: "{seconds}", with: String(Int(startLimit)))
    }

    public static func retry(step: AddStep, run: AddRun?) -> AddKind? {
        guard case .failed = step else { return nil }
        return run?.kind ?? .add
    }

    public static func retry(step: AddStep, run: AddRun?, state: ServiceState?, ignoring: Set<String> = []) -> AddKind? {
        guard let kind = retry(step: step, run: run) else { return nil }
        if kind == .adopt {
            let again = run.map(retryStages) ?? stages(target: defaultTarget(state), kind: .adopt, state: state)
            let possible = again.allSatisfy { stage in
                switch stage {
                case .macAdopt: return canAdopt(state)
                case .devboxAdopt: return canAdoptDevbox(state)
                default: return true
                }
            }
            if again.isEmpty || !possible {
                return nil
            }
        }
        if kind == .add {
            if addBusy(state, run: run, ignoring: ignoring) != nil {
                return nil
            }
            if let target = run?.target, !addPossible(target, pick: run?.pick, state: state, run: run, ignoring: ignoring) {
                return nil
            }
        }
        return kind
    }

    public static let devboxJobKinds: Set<String> = ["devbox-add", "devbox-adopt", "devbox-resign"]
    public static let startLimit: TimeInterval = 15

    public static func foreignRunning(_ state: ServiceState?, run: AddRun?, kinds: Set<String>, ignoring: Set<String> = []) -> JobSummary? {
        let mine = Set(run?.jobs ?? []).union(ignoring)
        return state?.jobs?.last { JobFlow.isRunning($0) && kinds.contains($0.kind ?? "") && !mine.contains($0.id) }
    }

    public static func addBusy(_ state: ServiceState?, run: AddRun?, ignoring: Set<String> = []) -> JobSummary? {
        foreignRunning(state, run: run, kinds: ["add"], ignoring: ignoring)
    }

    public static func devboxBusy(_ state: ServiceState?, run: AddRun?, ignoring: Set<String> = []) -> JobSummary? {
        foreignRunning(state, run: run, kinds: devboxJobKinds, ignoring: ignoring)
    }

    public static func card(_ run: AddRun?, state: ServiceState?) -> JobSummary? {
        guard let run, run.waiting, let stage = run.current, stage.opensBrowser || run.index > 0 else { return nil }
        let named = stage == .devboxAdopt && run.kind == .add ? run.uuid : nil
        if let id = run.job, var found = state?.jobs?.first(where: { $0.id == id && JobFlow.isRunning($0) }) {
            if found.uuid == nil, let named {
                found.uuid = named
            }
            return found
        }
        guard !stage.opensBrowser else { return nil }
        var made = JobSummary(id: run.job ?? "", kind: stage.jobKind, uuid: named, state: "running")
        let before = run.jobs.last { $0 != run.job }.flatMap { id in state?.jobs?.first { $0.id == id }?.result }
        let said = (before ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        made.card = said.isEmpty ? nil : said
        return made
    }

    public static func startedJob(_ stage: AddStage, state: ServiceState?, run: AddRun?, since: Date) -> JobSummary? {
        let mine = Set(run?.jobs ?? [])
        return state?.jobs?.last { job in
            guard job.kind == stage.jobKind, !mine.contains(job.id), let text = job.startedAt, let at = Timestamps.parse(text) else { return false }
            return at >= since.addingTimeInterval(-1)
        }
    }

    public static let patience: TimeInterval = 10

    public static func outcome(job: String, state: ServiceState?, seen: Bool, started: Date?, now: Date) -> JobOutcome {
        guard let found = state?.jobs?.first(where: { $0.id == job }) else {
            if seen {
                return .failed(lost(state))
            }
            if let started, now.timeIntervalSince(started) > patience {
                return .failed(lost(state))
            }
            return .running
        }
        switch found.state {
        case nil, "running":
            return .running
        case "done":
            return .done(uuid: found.uuid, result: sentence(found.result, state: state))
        default:
            return .failed(sentence(found.result, state: state))
        }
    }

    public static func step(
        job: String?, kind: AddKind, state: ServiceState?, seen: Bool = false, started: Date? = nil, now: Date = Date()
    ) -> AddStep {
        guard let job, !job.isEmpty else { return .start }
        switch outcome(job: job, state: state, seen: seen, started: started, now: now) {
        case .running:
            return .waiting(kind)
        case .done(let uuid?, _) where !uuid.isEmpty:
            return .added(uuid)
        case .done(_, let result):
            return .failed(result)
        case .failed(let sentence):
            return .failed(sentence)
        }
    }

    public static func stages(target: AddTarget, kind: AddKind, state: ServiceState?, pick: KnownAccount? = nil) -> [AddStage] {
        if kind == .add {
            if target == .mac {
                return [.macAdd]
            }
            return [.macAdd, keepsDevbox(pick, state: state) ? .devboxAdopt : .devboxAdd]
        }
        var found: [AddStage] = []
        if target != .devbox, canAdopt(state) {
            found.append(.macAdopt)
        }
        if target != .mac, canAdoptDevbox(state) {
            found.append(.devboxAdopt)
        }
        return found
    }

    public static func canAdoptDevbox(_ state: ServiceState?) -> Bool {
        guard JobFlow.devboxReady(state), devboxBusy(state, run: nil) == nil, let live = state?.devbox?.live else { return false }
        return live.adoptReady == true && live.held == false
    }

    public static func devboxLiveUuid(_ state: ServiceState?) -> String? {
        let live = (state?.devbox?.live?.uuid ?? state?.devbox?.accountUuid ?? "").trimmingCharacters(in: .whitespaces)
        return live.isEmpty ? nil : live
    }

    public static func devboxLive(_ state: ServiceState?) -> AccountSummary? {
        guard let live = devboxLiveUuid(state) else { return nil }
        return state?.accounts?.first { $0.uuid == live }
    }

    public static func devboxUses(_ state: ServiceState?) -> KnownAccount? {
        guard JobFlow.devboxReady(state), let live = state?.devbox?.live, live.known == true, live.added == false else { return nil }
        let name = (live.name ?? "").trimmingCharacters(in: .whitespaces)
        guard !name.isEmpty else { return nil }
        if let found = state?.knownAccounts?.first(where: { $0.name == name }) {
            return found
        }
        return KnownAccount(name: name, short: live.short, email: live.email, organization: live.organization)
    }

    public static func defaultPick(_ state: ServiceState?) -> KnownAccount? {
        devboxUses(state)
    }

    public static func keepsDevbox(_ pick: KnownAccount?, state: ServiceState?) -> Bool {
        guard let pick, let uses = devboxUses(state) else { return false }
        return pick.name == uses.name
    }

    public static func secondStage(for uuid: String, planned: AddStage, state: ServiceState?) -> AddStage? {
        let held = state?.accounts?.first { $0.uuid == uuid }?.devboxSignedIn == true
        let live = (state?.devbox?.live?.uuid ?? "").trimmingCharacters(in: .whitespaces)
        guard JobFlow.devboxReady(state), !live.isEmpty else {
            return planned == .devboxAdd && held ? nil : planned
        }
        if live == uuid {
            return .devboxAdopt
        }
        return held ? nil : .devboxAdd
    }

    public static func devboxNote(target: AddTarget, pick: KnownAccount?, state: ServiceState?) -> String? {
        guard target != .mac else { return nil }
        if let pick, keepsDevbox(pick, state: state) {
            let note = (state?.devbox?.live?.note ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            return note.isEmpty ? nil : note
        }
        let note = twice(state)
        return note.isEmpty ? nil : note
    }

    public static func adoptTitle(_ stages: [AddStage], state: ServiceState?) -> String? {
        let title: String?
        switch (stages.contains(.macAdopt), stages.contains(.devboxAdopt)) {
        case (true, true): title = state?.devbox?.live?.bothTitle
        case (true, false): title = state?.mac?.live?.adoptTitle
        case (false, true): title = state?.devbox?.live?.adoptTitle
        default: title = nil
        }
        let clean = (title ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return clean.isEmpty ? nil : clean
    }

    public static func defaultTarget(_ state: ServiceState?) -> AddTarget {
        addPossible(.both, pick: defaultPick(state), state: state) ? .both : .mac
    }

    public static func retryStages(_ run: AddRun) -> [AddStage] {
        Array(run.stages[min(run.index, run.stages.count)...])
    }

    public static func devboxFree(_ state: ServiceState?, run: AddRun? = nil, ignoring: Set<String> = []) -> Bool {
        JobFlow.devboxReady(state) && devboxBusy(state, run: run, ignoring: ignoring) == nil
    }

    public static func usable(_ target: AddTarget, state: ServiceState?, run: AddRun? = nil, ignoring: Set<String> = []) -> Bool {
        target == .mac || available(target, state: state, run: run, ignoring: ignoring)
            || (devboxFree(state, run: run, ignoring: ignoring) && (canAdoptDevbox(state) || devboxUses(state) != nil))
    }

    public static func addPossible(_ target: AddTarget, pick: KnownAccount?, state: ServiceState?, run: AddRun? = nil, ignoring: Set<String> = []) -> Bool {
        target == .mac || available(target, state: state, run: run, ignoring: ignoring)
            || (devboxFree(state, run: run, ignoring: ignoring) && keepsDevbox(pick, state: state))
    }

    public static func available(_ target: AddTarget, state: ServiceState?, run: AddRun? = nil, ignoring: Set<String> = []) -> Bool {
        target == .mac || (JobFlow.devboxReady(state) && state?.devbox?.canSignIn == true && devboxBusy(state, run: run, ignoring: ignoring) == nil)
    }

    public static func suggestedNames(_ account: AccountSummary, job: JobSummary?) -> (name: String, short: String) {
        func given(_ text: String?) -> String? {
            let trimmed = (text ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            return trimmed.isEmpty ? nil : trimmed
        }
        return (given(job?.suggestedName) ?? given(account.name) ?? "", given(job?.suggestedShort) ?? given(account.short) ?? "")
    }

    public static func addJob(for uuid: String, run: AddRun?, state: ServiceState?) -> JobSummary? {
        let mine = Set(run?.jobs ?? [])
        return state?.jobs?.last { mine.contains($0.id) && $0.uuid == uuid && $0.kind == "add" }
    }

    /// The sign-in paragraph: the picked account's own (it names the email and how to choose its organization),
    /// or the core's paragraph for another account.
    public static func explain(_ state: ServiceState?, pick: KnownAccount? = nil) -> String {
        let picked = core(pick?.explain)
        return picked.isEmpty ? core(state?.addText?.explain) : picked
    }

    public static let whichAccount = "Which account"
    public static let anotherAccount = "Another account"

    public static func pickLine(_ known: KnownAccount) -> String? {
        let line = core(known.pickLine)
        return line.isEmpty ? nil : line
    }

    /// A result for the 20 pt line and the body under it. A job's result uses the core's own split (result_head and
    /// result_rest, which keeps a name with a full stop whole); any other sentence, such as a refusal, breaks after its
    /// first sentence as before.
    public static func headline(_ result: String, state: ServiceState?) -> (head: String, rest: String) {
        let trimmed = result.trimmingCharacters(in: .whitespacesAndNewlines)
        if let job = state?.jobs?.last(where: { core($0.result) == trimmed && !core($0.resultHead).isEmpty }) {
            return (core(job.resultHead), core(job.resultRest))
        }
        guard let end = trimmed.range(of: ". ") else { return (trimmed, "") }
        return (String(trimmed[..<end.lowerBound]) + ".", String(trimmed[end.upperBound...]))
    }

    /// The waiting box's words. Until the job and its words reach the state, the box leads with the job's card or the
    /// earlier step's result, so it is never a light with no words.
    public static func waitingLines(_ stage: AddStage, state: ServiceState?, job: JobSummary? = nil, run: AddRun? = nil) -> [String] {
        let before = run.flatMap { run in run.jobs.last { $0 != run.job } }.flatMap { id in state?.jobs?.first { $0.id == id } }
        let earlier = core(before?.resultHead).isEmpty ? core(before?.result) : core(before?.resultHead)
        let head = [JobFlow.waiting(job), core(job?.card), earlier].first { !$0.isEmpty } ?? ""
        switch stage {
        case .macAdd:
            return [head, stays(state) ?? ""].filter { !$0.isEmpty }
        case .devboxAdd:
            return [head, forDevbox(state)].filter { !$0.isEmpty }
        case .macAdopt, .devboxAdopt:
            return [job.map(JobFlow.cardSentence) ?? "", earlier].filter { !$0.isEmpty }.prefix(1).map { $0 }
        }
    }

    public static func planName(_ account: AccountSummary) -> String? {
        let plan = (account.plan ?? "").replacingOccurrences(of: ", ", with: " ").trimmingCharacters(in: .whitespaces)
        return plan.isEmpty ? nil : plan
    }

    static func sentence(_ text: String?, state: ServiceState?) -> String {
        let clean = core(text)
        return clean.isEmpty ? failed(state) : clean
    }

    public static func ended(_ step: AddStep) -> Bool {
        switch step {
        case .added, .failed, .done:
            return true
        case .start, .waiting:
            return false
        }
    }

    public static func shownNames(menuName: String, oldShort: String, name: String, short: String) -> (menu: String, bar: String) {
        let typedName = name.trimmingCharacters(in: .whitespacesAndNewlines)
        let typedShort = short.trimmingCharacters(in: .whitespacesAndNewlines)
        let menu = typedName.isEmpty ? menuName : typedName
        let bar = typedShort.isEmpty ? (oldShort.isEmpty ? menu : oldShort) : typedShort
        return (menu, bar)
    }

    public static func renameBody(oldName: String, oldShort: String, name: String, short: String) -> [String: String] {
        let typedName = name.trimmingCharacters(in: .whitespacesAndNewlines)
        let typedShort = short.trimmingCharacters(in: .whitespacesAndNewlines)
        let newName = typedName.isEmpty ? oldName : typedName
        let newShort = typedShort.isEmpty ? (oldShort.isEmpty ? newName : oldShort) : typedShort
        guard !newName.isEmpty, newName != oldName || newShort != oldShort else { return [:] }
        return ["name": newName, "short": newShort]
    }

    public static func canAdopt(_ state: ServiceState?) -> Bool {
        guard let live = state?.mac?.live, foreignRunning(state, run: nil, kinds: ["adopt"]) == nil else { return false }
        return live.adoptReady == true && live.added == false
    }

    public static func stays(_ state: ServiceState?) -> String? {
        let line = core(state?.addText?.stays)
        return line.isEmpty ? nil : line
    }

    /// The naming screen's head: the core's template with the suggested name, or the job's own head without one.
    public static func added(name: String, _ account: AccountSummary, state: ServiceState?, job: JobSummary?) -> String {
        let typed = name.trimmingCharacters(in: .whitespaces)
        let shown = typed.isEmpty ? (account.name ?? "").trimmingCharacters(in: .whitespaces) : typed
        let template = core(state?.addText?.added)
        let head = core(job?.addedHead)
        if !typed.isEmpty || head.isEmpty, !shown.isEmpty, !template.isEmpty {
            return template.replacingOccurrences(of: "{name}", with: shown)
        }
        return head
    }

    /// The naming screen's second paragraph: the core's line for where the account now is, then the other computer's step's own
    /// result when that step failed, adopted or was skipped.
    public static func addedLine(_ account: AccountSummary, run: AddRun?) -> String {
        let devboxDone = run?.devboxResult != nil && run?.devboxFailed == false && run?.kind == .add
        let line: String?
        if devboxDone, run?.target == .devbox {
            line = account.addedText?.devbox
        } else if devboxDone {
            line = account.addedText?.both
        } else {
            line = account.addedText?.mac
        }
        var parts = [core(line)]
        if let run, let extra = run.devboxResult, !extra.isEmpty, run.devboxFailed || run.kind == .adopt || run.devboxSkipped {
            parts.append(extra)
        }
        return parts.filter { !$0.isEmpty }.joined(separator: " ")
    }
}

public enum SheetText {
    /// Where the typed names show, from the core's two templates.
    public static func namesShown(menu: String, bar: String, barHidden: Bool = false, text: AddText?) -> String {
        let template = AddFlow.core(barHidden ? text?.namesShownHidden : text?.namesShown)
        return template.replacingOccurrences(of: "{menu}", with: menu).replacingOccurrences(of: "{bar}", with: bar)
    }
}
