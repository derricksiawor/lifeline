import Foundation

public enum PanelMove: Sendable, Equatable {
    case open
    case close
    case raise
    case stay
}

public enum JobFlow {
    public static let openAgain = "Open the sign-in page again"
    public static let cancel = "Cancel"
    public static let signInKinds: Set<String> = ["add", "resign", "devbox-add", "devbox-resign"]
    public static let addKinds: Set<String> = ["add", "devbox-add"]
    public static let adoptKinds: Set<String> = ["adopt", "devbox-adopt"]
    public static let resignKinds: Set<String> = ["resign", "devbox-resign"]
    public static let switchRunning = "A switch is already running."
    public static let openFirst = "Open the sign-in page"
    public static let nextResult = "Next result"
    public static let alertLabel = "An alert you missed"
    public static let openLimit: TimeInterval = 25
    public static let cancelLimit: TimeInterval = 5

    static func core(_ text: String?) -> String {
        (text ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
    }

    public static func devboxAway(_ state: ServiceState?) -> String {
        core(state?.addText?.devboxAway)
    }

    public static func cancelled(_ state: ServiceState?) -> String {
        core(state?.addText?.cancelled)
    }

    /// The core's cancelling sentence; without the core's words, the service-down or unreadable sentence Swift keeps.
    public static func cancelling(_ state: ServiceState?) -> String {
        let given = core(state?.addText?.cancelling)
        if !given.isEmpty {
            return given
        }
        return state == nil ? Sentences.notRunning : Sentences.unreadable
    }

    public static func openLabel(_ job: JobSummary) -> String {
        job.opened == false ? openFirst : openAgain
    }

    /// The notification title of an ended job, from the core; a running job has none.
    public static func title(_ job: JobSummary, state: ServiceState?) -> String {
        core(job.title)
    }

    public static func alertCards(_ state: ServiceState?, seen: Set<String>) -> [NoticeSummary] {
        (state?.notifications ?? []).filter { $0.keep == true && !seen.contains(noticeKey($0.id)) }
    }

    public static func noticeKey(_ id: String) -> String {
        "notice-" + id
    }

    public static func cancellable(_ job: JobSummary) -> Bool {
        ["add", "resign", "devbox-add", "devbox-resign", "devbox-adopt"].contains(job.kind ?? "")
    }

    public static func cardSentence(_ job: JobSummary) -> String {
        (job.card ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// A finished job's one sentence for its card: the core's result_head.
    public static func resultSentence(_ job: JobSummary, state: ServiceState?) -> String {
        let head = core(job.resultHead)
        return head.isEmpty ? core(job.result) : head
    }

    /// The waiting head, from the core ("Waiting for you to finish the sign-in in Safari.").
    public static func waiting(_ job: JobSummary?) -> String {
        core(job?.waiting)
    }

    public static func accountName(_ job: JobSummary, state: ServiceState?) -> String? {
        let name = state?.accounts?.first { $0.uuid == job.uuid }?.name?.trimmingCharacters(in: .whitespaces) ?? ""
        return name.isEmpty ? nil : name
    }

    /// The lines under the waiting box, from the core: what to do in the browser (with how to choose the organization),
    /// then the account that stays in use, the other computer's note or the page that did not open.
    public static func guidance(_ job: JobSummary, state: ServiceState?) -> [String] {
        (job.lines ?? []).map(core).filter { !$0.isEmpty }
    }

    public static func cardLines(_ job: JobSummary, state: ServiceState?) -> [String] {
        (job.cardLines ?? []).map(core).filter { !$0.isEmpty }
    }

    public static func unbroken(_ text: String) -> String {
        text.replacingOccurrences(of: "sign-in", with: "sign-\u{2060}in")
    }

    public static func cancelRoute(_ id: String) -> String {
        "/jobs/" + LifelineAPI.safe(id) + "/cancel"
    }

    public static func openRoute(_ id: String) -> String {
        "/jobs/" + LifelineAPI.safe(id) + "/open"
    }

    public static func isRunning(_ job: JobSummary) -> Bool {
        job.state == "running"
    }

    public static func isFinished(_ job: JobSummary) -> Bool {
        guard let state = job.state, !state.isEmpty else { return false }
        return state != "running"
    }

    public static func waitingJobs(_ state: ServiceState?) -> [JobSummary] {
        (state?.jobs ?? []).filter { job in
            isRunning(job) && signInKinds.contains(job.kind ?? "")
        }
    }

    public static func waitingJob(_ state: ServiceState?) -> JobSummary? {
        waitingJobs(state).last
    }

    public static func cardJobs(_ state: ServiceState?) -> [JobSummary] {
        (state?.jobs ?? []).filter { job in
            isRunning(job) && (signInKinds.contains(job.kind ?? "") || adoptKinds.contains(job.kind ?? ""))
        }
    }

    public static func hasActions(_ job: JobSummary) -> Bool {
        signInKinds.contains(job.kind ?? "")
    }

    public static func page(_ job: JobSummary?) -> URL? {
        guard let text = job?.url, let url = URL(string: text), url.scheme == "https" else { return nil }
        return url
    }

    public static func browserToRaise(_ job: JobSummary?) -> String? {
        let name = (job?.browser ?? "").trimmingCharacters(in: .whitespaces)
        return name.isEmpty ? nil : name
    }

    public static func results(_ state: ServiceState?, seen: Set<String>) -> [JobSummary] {
        (state?.jobs ?? []).filter { job in
            isFinished(job) && !(job.result ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !seen.contains(job.id)
        }
    }

    public static func tryAgainKind(_ job: JobSummary) -> AddKind? {
        guard isFinished(job), job.state != "done" else { return nil }
        let kind = job.kind ?? ""
        if addKinds.contains(kind) {
            return .add
        }
        if adoptKinds.contains(kind) {
            return .adopt
        }
        return nil
    }

    public static func retryKind(_ job: JobSummary, state: ServiceState?) -> AddKind? {
        guard let kind = tryAgainKind(job) else { return nil }
        switch job.kind ?? "" {
        case "adopt":
            return AddFlow.canAdopt(state) ? kind : nil
        case "devbox-adopt":
            return AddFlow.canAdoptDevbox(state) ? kind : nil
        case "add":
            return AddFlow.addBusy(state, run: nil) == nil ? kind : nil
        case "devbox-add":
            guard JobFlow.devboxReady(state), AddFlow.devboxBusy(state, run: nil) == nil else { return nil }
            guard let uuid = job.uuid, state?.accounts?.contains(where: { $0.uuid == uuid }) == true else { return nil }
            return AddFlow.secondStage(for: uuid, planned: .devboxAdd, state: state) == nil ? nil : kind
        default:
            return kind
        }
    }

    public static func retryTarget(_ job: JobSummary) -> AddTarget? {
        switch job.kind ?? "" {
        case "adopt": return .mac
        case "devbox-adopt": return .devbox
        default: return nil
        }
    }

    public static func states(_ state: ServiceState?) -> [String: String] {
        var found: [String: String] = [:]
        for job in state?.jobs ?? [] {
            found[job.id] = job.state ?? ""
        }
        return found
    }

    public static func ended(before: [String: String], now state: ServiceState?) -> [JobSummary] {
        (state?.jobs ?? []).filter { job in
            isFinished(job) && (before[job.id] == nil || before[job.id] == "running")
        }
    }

    public static func label(_ job: JobSummary, state: ServiceState?) -> String {
        core(job.label)
    }

    public static func notice(_ job: JobSummary, state: ServiceState? = nil) -> Notice {
        Notice(id: "job-" + job.id, title: title(job, state: state), body: job.result ?? "", account: job.uuid, action: nil)
    }

    public static func devboxReady(_ state: ServiceState?) -> Bool {
        state?.devbox?.answers == true
    }

    public static func breathes(_ state: ServiceState?) -> Bool {
        waitingJob(state) != nil
    }

    public static let back = "Back"

    public static func toggleMove(open: Bool, holding: Bool) -> PanelMove {
        guard open else { return .open }
        return holding ? .raise : .close
    }

    public static func outsideMove(holding: Bool) -> PanelMove {
        holding ? .stay : .close
    }

    public static func prune(_ seen: Set<String>, state: ServiceState?) -> Set<String> {
        guard let jobs = state?.jobs else { return seen }
        let notices = (state?.notifications ?? []).map { noticeKey($0.id) }
        return seen.intersection(jobs.map(\.id) + notices)
    }
}
