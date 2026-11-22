import AppKit
import Foundation
import LifelineKit

enum PanelScreen: Equatable {
    case menu
    case add
    case settings
}

@MainActor
final class StateStore: ObservableObject {
    @Published private(set) var state: ServiceState?
    @Published private(set) var lastGood: Date?
    @Published private(set) var failing = false
    @Published private(set) var appearance = "dark"
    @Published var side: MenuSide = .mac {
        didSet {
            if side != oldValue {
                highlight = nil
            }
        }
    }
    /// The row the keyboard highlights; it starts empty each time the menu opens or the view changes.
    @Published var highlight: String?
    @Published private(set) var actionNote: String?
    @Published private(set) var detail: String?
    @Published private(set) var histories: [String: HistoryLoad] = [:]
    let screenDirection = ScreenDirection()
    @Published var screen: PanelScreen = .menu {
        didSet {
            screenDirection.move = screen == .menu ? .back : .forward
            markVisibleResults()
        }
    }
    @Published var addTarget: AddTarget? {
        didSet {
            dropFailedRun()
        }
    }
    @Published var addPick: KnownAccount? {
        didSet {
            addPicked = true
            dropFailedRun()
        }
    }
    @Published private(set) var addPicked = false
    @Published private(set) var pendingSwitch: String?
    @Published private(set) var addRun: AddRun?
    @Published private(set) var addProblem: String?
    @Published private(set) var addNote: String?
    private var addCancel: (job: String, run: AddRun)?
    private var jobRuns: [String: AddRun] = [:]
    var clock: () -> Date = { Date() }
    /// When a cancel stops waiting for the service's reply. The app waits JobFlow.cancelLimit; a test fires it by hand.
    var cancelDeadline: @Sendable () async -> Void = { try? await Task.sleep(for: .seconds(JobFlow.cancelLimit)) }
    private var cancellingSince: [String: Date] = [:]
    private var switchSince: Date?
    @Published private(set) var settingsProblem: String?
    @Published var panelLimit: CGFloat?
    @Published var renameCancel = 0
    var menuOpen = false
    @Published private(set) var switching = false
    private var switchGuard = SwitchGuard()
    private var lastSwitchNote: String?

    private let api: LifelineAPI
    private var polling: Task<Void, Never>?
    private var inFlight = false
    private var again = false
    private var actionRun = 0
    private var pending: PendingCheck?
    private var noteStays = false
    private var historyAt: [String: Date] = [:]
    private var historyLoading: Set<String> = []

    @Published private(set) var seenJobs: Set<String> = []
    @Published private(set) var cancelling: Set<String> = []
    @Published private(set) var switchSettledAt: Date?
    @Published private(set) var signInStarts = 0
    private var shownResults: Set<String> = []
    static let seenJobsKey = "seenJobs"
    @Published private(set) var sheetSizes: [SheetKind: SheetSize] = [:]
    var sheetMaxWidth: CGFloat = 1200
    private let sizes: UserDefaults?

    init(api: LifelineAPI, sizes: UserDefaults? = .standard) {
        self.api = api
        self.sizes = sizes
        if let sizes {
            for kind in SheetKind.allCases {
                sheetSizes[kind] = SheetSizing.load(kind, from: sizes)
            }
            seenJobs = Set(sizes.stringArray(forKey: Self.seenJobsKey) ?? [])
        }
    }

    var waitingJobs: [JobSummary] {
        JobFlow.waitingJobs(state)
    }

    var cardJobs: [JobSummary] {
        JobFlow.cardJobs(state)
    }

    var settlingJobs: Set<String> {
        cancelling.union([addCancel?.job].compactMap { $0 })
    }

    var chosenPick: KnownAccount? {
        addPicked ? addPick : AddFlow.defaultPick(state)
    }

    var addCard: JobSummary? {
        AddFlow.card(addRun, state: state)
    }

    var startLimit: TimeInterval = AddFlow.startLimit

    var addJob: JobSummary? {
        guard let run = addRun, run.waiting, run.current?.opensBrowser == true, let id = run.job else { return nil }
        return state?.jobs?.first { $0.id == id && JobFlow.isRunning($0) }
    }

    var shownJob: JobSummary? {
        screen == .add ? addJob : nil
    }

    var breathing: Bool {
        JobFlow.breathes(state)
    }

    var unseenAlerts: [NoticeSummary] {
        JobFlow.alertCards(state, seen: seenJobs)
    }

    var unseenResults: [JobSummary] {
        JobFlow.results(state, seen: seenJobs.union(screen == .add ? addRun?.jobs ?? [] : []))
    }

    var refusalPosted: ((String) -> Void)?
    private var visibleCards: Set<String> = []

    func cardsShown(_ ids: Set<String>) {
        visibleCards = ids
        markVisibleResults()
    }

    func markVisibleResults() {
        guard menuOpen, screen == .menu else { return }
        let keys = unseenResults.map(\.id) + unseenAlerts.map { JobFlow.noticeKey($0.id) }
        shownResults.formUnion(keys.filter { visibleCards.contains($0) })
    }

    func panelClosed(visible: Bool) {
        menuOpen = false
        if visible {
            remember(shownResults)
        }
        shownResults = []
    }

    private func remember(_ ids: some Sequence<String>) {
        let fresh = Set(ids).subtracting(seenJobs)
        guard !fresh.isEmpty else { return }
        seenJobs = JobFlow.prune(seenJobs, state: state).union(fresh)
        sizes?.set(Array(seenJobs).sorted(), forKey: Self.seenJobsKey)
    }

    private func refusedOpen(_ sentence: String) {
        if screen == .add, menuOpen {
            addProblem = sentence
            return
        }
        actionRun += 1
        actionNote = sentence
        noteStays = true
        refusalPosted?(sentence)
    }

    private func refused(_ sentence: String) {
        if screen == .add {
            addProblem = sentence
        } else {
            show(sentence)
        }
    }

    func cancelJob(_ id: String) {
        guard !cancelling.contains(id) else { return }
        cancelling.insert(id)
        cancellingSince[id] = clock()
        if let run = addRun, run.waiting, run.job == id, run.index > 0, run.current?.onDevbox == true {
            addProblem = nil
        } else if let run = addRun, run.waiting, run.job == id || run.jobs.contains(id) {
            addCancel = (id, run)
            addRun = nil
            addProblem = nil
            addNote = JobFlow.cancelling(state)
        }
        let api = self.api
        let deadline = cancelDeadline
        Task { @MainActor [weak self] in
            let outcome = await FirstOutcome.race(
                { await Task.detached { api.post(JobFlow.cancelRoute(id), body: Data("{}".utf8), limit: JobFlow.cancelLimit + 1) }.value },
                or: {
                    await deadline()
                    return .refused(Sentences.timedOut)
                }
            )
            guard let self else { return }
            await self.refresh()
            if case .refused(let sentence) = outcome {
                self.cancelling.remove(id)
                if self.state?.jobs?.contains(where: { $0.id == id && JobFlow.isRunning($0) }) == true {
                    if let pending = self.addCancel, pending.job == id, self.addRun == nil {
                        self.addCancel = nil
                        self.addNote = nil
                        self.addRun = pending.run
                    }
                    self.refused(sentence)
                }
            }
        }
    }

    private func expireLapsed() {
        let now = clock()
        let lapsed = cancelling.filter { id in
            now.timeIntervalSince(cancellingSince[id] ?? now) > MenuLogic.flagLapse
        }
        if !lapsed.isEmpty {
            cancelling.subtract(lapsed)
        }
        cancellingSince = cancellingSince.filter { cancelling.contains($0.key) }
        if switchGuard.busy, let since = switchSince, now.timeIntervalSince(since) > MenuLogic.flagLapse {
            switchGuard = SwitchGuard()
            switching = false
            pendingSwitch = nil
            switchSince = nil
        }
        if !switchGuard.busy {
            switchSince = nil
        }
    }

    var addCompact: Bool {
        screen == .add && addCard != nil
    }

    private func followCancel(_ fresh: ServiceState) {
        guard let pending = addCancel else { return }
        guard let job = fresh.jobs?.first(where: { $0.id == pending.job }) else {
            addCancel = nil
            if addNote == JobFlow.cancelling(fresh) {
                addNote = nil
            }
            return
        }
        guard !JobFlow.isRunning(job) else { return }
        addCancel = nil
        if job.state == "done", addRun == nil {
            var run = pending.run
            let action = run.advance(state: fresh, now: Date())
            addRun = run
            addNote = nil
            follow(action)
            return
        }
        remember([job.id])
        guard addNote == JobFlow.cancelling(fresh) else { return }
        if job.state == "cancelled" {
            addNote = JobFlow.cancelled(fresh)
        } else {
            addNote = nil
            addProblem = job.result
        }
    }

    func reopenJob(_ id: String) {
        if screen == .add {
            signInStarts += 1
        }
        let api = self.api
        Task { @MainActor [weak self] in
            let outcome = await Task.detached { api.post(JobFlow.openRoute(id), body: Data("{}".utf8), limit: JobFlow.openLimit) }.value
            guard let self else { return }
            if case .refused(let sentence) = outcome {
                self.refusedOpen(sentence)
            }
            await self.refresh()
        }
    }

    func retry(_ job: JobSummary) {
        guard let kind = JobFlow.retryKind(job, state: state) else { return }
        remember([job.id])
        closeDetail()
        screen = .add
        guard addRun?.waiting != true else { return }
        let earlier = jobRuns[job.id]
        clearAdd()
        switch job.kind ?? "" {
        case "add":
            guard let earlier else {
                addPick = state?.knownAccounts?.first { $0.name == job.known }
                return
            }
            addPick = earlier.pick
            guard AddFlow.available(earlier.target, state: state) else { return }
            addTarget = earlier.target
            startAdd(.add)
        case "devbox-add":
            guard let uuid = job.uuid, let stage = AddFlow.secondStage(for: uuid, planned: .devboxAdd, state: state) else { return }
            let target = earlier?.target ?? .both
            addTarget = target
            begin(AddRun(target: target, devboxStage: stage, uuid: uuid))
        case "devbox-adopt":
            addTarget = .devbox
            begin(AddRun(target: .devbox, kind: .adopt, stages: [.devboxAdopt], pick: nil, expected: state?.devbox?.live?.uuid))
        default:
            addTarget = .mac
            begin(AddRun(target: .mac, kind: kind, stages: [.macAdopt], pick: nil, macExpected: state?.mac?.live?.uuid))
        }
    }

    func retryAdd(_ kind: AddKind) {
        guard let run = addRun, case .failed = run.step else {
            startAdd(kind)
            return
        }
        if kind == .adopt {
            let stages = AddFlow.retryStages(run)
            guard !stages.isEmpty, AddFlow.retry(step: run.step, run: run, state: state, ignoring: settlingJobs) == .adopt else { return }
            begin(AddRun(target: run.target, kind: .adopt, stages: stages, pick: nil, expected: state?.devbox?.live?.uuid, macExpected: state?.mac?.live?.uuid))
            return
        }
        let pick = run.pick
        let target = AddFlow.addPossible(run.target, pick: pick, state: state, run: run, ignoring: settlingJobs) ? run.target : .mac
        begin(AddRun(target: target, kind: .add, stages: AddFlow.stages(target: target, kind: .add, state: state, pick: pick), pick: pick))
    }

    private func dropFailedRun() {
        guard let run = addRun, case .failed = run.step else { return }
        addRun = nil
        addProblem = nil
    }

    private func followJobs() {
        if menuOpen, screen == .menu, addNeedsNaming {
            closeDetail()
            screen = .add
        }
        if menuOpen, screen == .add, let run = addRun, run.finished {
            remember(run.jobs)
        }
        let running = Set((state?.jobs ?? []).filter(JobFlow.isRunning).map(\.id))
        let still = cancelling.intersection(running)
        if still != cancelling {
            let confirmed = (state?.jobs ?? []).filter { cancelling.contains($0.id) && $0.state == "cancelled" }.map(\.id)
            remember(confirmed)
            cancelling = still
        }
        markVisibleResults()
    }

    func sheetSize(_ kind: SheetKind) -> SheetSize? {
        if let stored = sheetSizes[kind] {
            return fitted(stored)
        }
        return kind == .settings ? fitted(SheetSizing.settingsDefault) : nil
    }

    func backFromSettings() {
        guard screen == .settings else { return }
        screen = .menu
    }

    @Published private(set) var undoSince: Date?
    private(set) var undoUntil: Date?
    @Published private(set) var undoGone = false
    private(set) var switchClick: (time: Double, x: Double, y: Double)?

    func noteUndo(_ fresh: ServiceState) {
        guard fresh.mac?.actionKind == "undo" else {
            undoSince = nil
            undoUntil = nil
            undoGone = false
            return
        }
        undoUntil = fresh.mac?.undoUntil.flatMap(Timestamps.parse)
        if undoSince == nil {
            undoSince = clock()
            undoGone = false
        }
    }

    func undoTimeUp() {
        guard let since = undoSince, !MenuLogic.undoShows(since: since, until: undoUntil, now: clock()) else { return }
        undoGone = true
        Task { await refresh() }
    }

    private func isSwitchClick(_ event: NSEvent?) -> Bool {
        guard let event, [.leftMouseDown, .leftMouseUp].contains(event.type) else { return false }
        return MenuLogic.sameClick(time: event.timestamp, x: Double(event.locationInWindow.x), y: Double(event.locationInWindow.y), previous: switchClick)
    }

    func resizeSheet(_ kind: SheetKind, _ size: SheetSize) {
        sheetSizes[kind] = fitted(size)
    }

    func commitSheet(_ kind: SheetKind) {
        guard let sizes, let size = sheetSizes[kind] else { return }
        SheetSizing.save(fitted(size), kind, to: sizes)
    }

    private func fitted(_ size: SheetSize) -> SheetSize {
        SheetSizing.clamp(size, minWidth: Double(Metrics.panelWidth), maxWidth: Double(sheetMaxWidth), maxHeight: Double(panelLimit ?? 879))
    }

    func start() {
        guard polling == nil else { return }
        NotificationCenter.default.addObserver(forName: NSWindow.didBecomeKeyNotification, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in
                await self?.refresh()
            }
        }
        polling = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }

    func refresh() async {
        if inFlight {
            again = true
            return
        }
        inFlight = true
        defer { inFlight = false }
        repeat {
            again = false
            await fetch()
        } while again
    }

    private func fetch() async {
        let number = switchGuard.fetchStarted()
        let isDark = NSApp?.effectiveAppearance.bestMatch(from: [.darkAqua, .aqua]) != .aqua
        let name = isDark ? "dark" : "light"
        appearance = name
        let api = self.api
        let result: Result<ServiceState, Error> = await Task.detached {
            Result { try api.fetchState(appearance: name) }
        }.value
        switch result {
        case .success(let fresh):
            if let note = MenuLogic.freshSwitchNote(fresh, last: lastSwitchNote, showing: actionNote) {
                actionRun += 1
                actionNote = note
                noteStays = !menuOpen
            }
            let said = (fresh.switchNote ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            lastSwitchNote = said.isEmpty ? nil : said
            switchGuard.fetched(number)
            if switching != switchGuard.busy {
                switching = switchGuard.busy
                if !switching {
                    switchSettledAt = Date()
                    pendingSwitch = nil
                }
            }
            state = fresh
            if var run = addRun {
                let action = run.advance(state: fresh, now: Date())
                addRun = run
                follow(action)
            }
            followCancel(fresh)
            followJobs()
            expireLapsed()
            noteUndo(fresh)
            lastGood = Date()
            failing = false
            settle(fresh)
        case .failure:
            failing = true
        }
    }

    private func settle(_ fresh: ServiceState) {
        if let pending, MenuLogic.resolves(pending, state: fresh) {
            self.pending = nil
            if Sentences.unknownResult(actionNote) {
                actionNote = nil
            }
        }
    }

    var addStep: AddStep {
        addRun?.step ?? .start
    }

    var addWaiting: Bool {
        addRun?.waiting == true
    }

    var addHolding: Bool {
        screen == .add
    }

    func backFromAdd() {
        guard screen == .add else { return }
        if addWaiting {
            screen = .menu
            return
        }
        if case .added(let uuid) = addStep, let account = state?.accounts?.first(where: { $0.uuid == uuid }) {
            let names = AddFlow.suggestedNames(account, job: AddFlow.addJob(for: uuid, run: addRun, state: state))
            let body = AddFlow.renameBody(oldName: account.name ?? "", oldShort: account.short ?? "", name: names.name, short: names.short)
            if !body.isEmpty {
                Task { @MainActor [weak self] in
                    _ = await self?.rename(uuid, body: body)
                }
            }
        }
        finishAdd()
    }

    @discardableResult
    func openWaiting(_ id: String) -> Bool {
        guard let run = addRun, run.waiting, run.job == id || run.jobs.contains(id) else { return false }
        screen = .add
        return true
    }

    var addNeedsNaming: Bool {
        if case .added = addStep {
            return true
        }
        return false
    }

    var chosenTarget: AddTarget {
        if let run = addRun, run.waiting {
            return run.target
        }
        let wanted = addTarget ?? addRun?.target ?? AddFlow.defaultTarget(state)
        return AddFlow.usable(wanted, state: state, run: addRun, ignoring: settlingJobs) ? wanted : .mac
    }

    var addPage: URL? {
        guard let job = addRun?.job, let found = state?.jobs?.first(where: { $0.id == job }),
              found.opened == false, let text = found.url, let url = URL(string: text), url.scheme == "https" else {
            return nil
        }
        return url
    }

    func heroAdd(_ add: HeroAdd) {
        screen = .add
        addTarget = .mac
        switch add {
        case .add(let known):
            if let known {
                addPick = state?.knownAccounts?.first { $0.name == known }
                    ?? state?.mac?.unaddedKnown.map { KnownAccount(name: $0.name ?? known, short: $0.short, email: $0.email) }
            }
            startAdd(.add)
        case .adopt:
            startAdd(.adopt)
        }
    }

    func startAdd(_ kind: AddKind) {
        guard !addWaiting else { return }
        let target = chosenTarget
        let pick = kind == .add ? chosenPick : nil
        begin(AddRun(target: target, kind: kind, stages: AddFlow.stages(target: target, kind: kind, state: state, pick: pick), pick: pick, expected: kind == .adopt ? state?.devbox?.live?.uuid : nil, macExpected: kind == .adopt ? state?.mac?.live?.uuid : nil))
    }

    private func begin(_ run: AddRun) {
        addProblem = nil
        addNote = nil
        addRun = run
        if let first = run.current {
            if first.opensBrowser {
                signInStarts += 1
            }
            post(first)
        }
    }

    private func post(_ stage: AddStage) {
        let api = self.api
        let body = addRun?.body(for: stage) ?? [:]
        let data = (try? JSONSerialization.data(withJSONObject: body, options: [.sortedKeys])) ?? Data("{}".utf8)
        let sent = Date()
        Task { @MainActor [weak self] in
            let limit = self?.startLimit ?? AddFlow.startLimit
            let outcome = await Task.detached { api.post(stage.route, body: data, limit: limit) }.value
            guard let self, self.addRun?.current == stage else { return }
            switch outcome {
            case .done(let job, _):
                if let job, !job.isEmpty {
                    self.began(job, stage: stage)
                } else {
                    self.follow(self.refuse(AddFlow.failed(self.state)))
                }
            case .refused(let sentence):
                if Sentences.unknownResult(sentence) {
                    await self.refresh()
                    if self.addRun?.current == stage, self.addRun?.job == nil,
                       let found = AddFlow.startedJob(stage, state: self.state, run: self.addRun, since: sent) {
                        self.began(found.id, stage: stage)
                        return
                    }
                }
                guard self.addRun?.current == stage else { return }
                // The core words the slow answer; Swift keeps its own sentence only for when no state has arrived.
                self.follow(self.refuse(sentence == Sentences.slowStart ? AddFlow.slowAnswer(self.state) : sentence))
            }
            await self.refresh()
        }
    }

    private func began(_ job: String, stage: AddStage) {
        guard var run = addRun, run.current == stage else { return }
        run.began(job, at: Date())
        addRun = run
        jobRuns[job] = run
    }

    private func refuse(_ sentence: String) -> AddAction {
        guard var run = addRun else { return .none }
        let action = run.refused(sentence)
        addRun = run
        return action
    }

    private func follow(_ action: AddAction) {
        switch action {
        case .none:
            break
        case .start(let stage):
            post(stage)
        case .place(let uuid, let value):
            let api = self.api
            Task { @MainActor [weak self] in
                let outcome = await Task.detached { api.place(uuid, value) }.value
                guard let self else { return }
                if case .refused(let sentence) = outcome {
                    self.addProblem = sentence
                }
                await self.refresh()
            }
        }
    }

    func finishAdd() {
        clearAdd()
        screen = .menu
    }

    private func clearAdd() {
        addRun = nil
        addProblem = nil
        addNote = nil
        addPick = nil
        addPicked = false
        addTarget = nil
    }

    func place(_ uuid: String, _ value: String) {
        settingsProblem = nil
        let api = self.api
        Task { @MainActor [weak self] in
            let outcome = await Task.detached { api.place(uuid, value) }.value
            guard let self else { return }
            if case .refused(let sentence) = outcome {
                self.settingsProblem = sentence
            }
            await self.refresh()
        }
    }

    func pair(address: String?, code: String) {
        settingsProblem = nil
        let api = self.api
        Task { @MainActor [weak self] in
            let outcome = await Task.detached { api.pair(address: address, code: code) }.value
            guard let self else { return }
            switch outcome {
            case .done(_, let message): self.settingsProblem = message
            case .refused(let sentence): self.settingsProblem = sentence
            }
            await self.refresh()
        }
    }

    func change(_ key: SettingKey, on: Bool) {
        settingsProblem = nil
        let api = self.api
        Task { @MainActor [weak self] in
            let outcome = await Task.detached { api.change(key, on: on) }.value
            guard let self else { return }
            if case .refused(let sentence) = outcome {
                self.settingsProblem = sentence
            }
            await self.refresh()
        }
    }

    func statusline(on: Bool) {
        settingsProblem = nil
        let api = self.api
        Task { @MainActor [weak self] in
            let outcome = await Task.detached { api.statusline(on: on) }.value
            guard let self else { return }
            if case .refused(let sentence) = outcome {
                self.settingsProblem = sentence
            }
            await self.refresh()
        }
    }

    func showSettingsProblem(_ sentence: String?) {
        settingsProblem = sentence
    }

    func opened() {
        highlight = nil
        screen = .menu
        settingsProblem = nil
        shownResults = []
        if addRun?.finished == true, !addNeedsNaming {
            clearAdd()
        }
        if addNeedsNaming, let run = addRun {
            screen = .add
            remember(run.jobs)
        }
        side = .mac
        if !noteStays {
            actionNote = nil
        }
        noteStays = false
    }

    /// Later on a notification. The service sets one reminder the day before a sign-in ends and says when; any
    /// other alert just goes. The banner is removed either way, and the service's sentence shows in the menu.
    func later(_ id: String) {
        guard !id.isEmpty else { return }
        let api = self.api
        Task { @MainActor [weak self] in
            let outcome = await Task.detached { api.post(Notices.laterRoute, object: ["id": id]) }.value
            guard let self else { return }
            switch outcome {
            case .done(_, let message):
                if let message, !message.isEmpty {
                    self.show(message)
                    self.noteStays = !self.menuOpen
                }
            case .refused(let sentence):
                self.show(sentence)
                self.noteStays = !self.menuOpen
            }
        }
    }

    func show(_ sentence: String?) {
        actionRun += 1
        actionNote = sentence
        noteStays = false
    }

    func run(_ command: MenuCommand, via: Via, keepNote: Bool = false, then: ((ActionOutcome) -> Void)? = nil) {
        if command == .undo, isSwitchClick(NSApp?.currentEvent) {
            return
        }
        if case .switchTo = command, let event = NSApp?.currentEvent, [.leftMouseDown, .leftMouseUp].contains(event.type) {
            switchClick = (event.timestamp, Double(event.locationInWindow.x), Double(event.locationInWindow.y))
        }
        if command.switches {
            guard switchGuard.begin() else {
                then?(.refused(JobFlow.switchRunning))
                return
            }
            switchSince = clock()
            switching = true
            if case .switchTo(let uuid) = command {
                pendingSwitch = uuid
            } else {
                pendingSwitch = MenuLogic.undoTarget
            }
        }
        actionRun += 1
        let run = actionRun
        actionNote = nil
        noteStays = false
        let check = MenuLogic.pendingCheck(for: command, state: state)
        pending = nil
        let api = self.api
        var names: [String: String] = [:]
        for account in state?.accounts ?? [] {
            if let name = account.name {
                names[account.uuid] = name
            }
        }
        let known = names
        Task { @MainActor [weak self] in
            let outcome = await Task.detached {
                api.perform(command, via: via, names: known)
            }.value
            guard let self else { return }
            if command.switches {
                self.switchGuard.replied()
            }
            if run == self.actionRun {
                switch outcome {
                case .done(let note):
                    self.actionNote = note
                    self.noteStays = (keepNote || MenuLogic.keepsNote(command)) && note != nil && !self.menuOpen
                case .accepted:
                    self.actionNote = nil
                case .refused(let sentence):
                    self.actionNote = sentence
                    self.noteStays = keepNote && !self.menuOpen
                    self.pending = Sentences.unknownResult(sentence) ? check : nil
                }
            }
            then?(outcome)
            await self.refresh()
        }
    }

    /// Opens a row. A row opened with the mouse clears the keyboard highlight, so Return never acts on a row you moved
    /// away from; a keyboard open keeps its own highlight.
    func showDetail(_ uuid: String, keyboard: Bool = false) {
        if !keyboard {
            highlight = nil
        }
        detail = uuid
        loadHistory(uuid)
    }

    func closeDetail() {
        detail = nil
    }

    func prefetchHistories() {
        for account in state?.accounts ?? [] {
            loadHistory(account.uuid)
        }
    }

    func loadHistory(_ uuid: String) {
        if let at = historyAt[uuid], Date().timeIntervalSince(at) < 60, histories[uuid] != nil {
            return
        }
        guard !historyLoading.contains(uuid) else { return }
        historyLoading.insert(uuid)
        let api = self.api
        Task { @MainActor [weak self] in
            let result: Result<HistoryReply, Error> = await Task.detached {
                Result { try api.fetchHistory(uuid: uuid) }
            }.value
            guard let self else { return }
            self.historyLoading.remove(uuid)
            switch result {
            case .success(let reply):
                self.histories[uuid] = .ready(reply)
                self.historyAt[uuid] = Date()
            case .failure:
                if case .ready? = self.histories[uuid] {
                    return
                }
                self.histories[uuid] = .failed
            }
        }
    }

    /// Remove from Lifeline: the core's sentence shows in the menu, and a refusal goes back to the open row.
    func remove(_ uuid: String) async -> String? {
        let api = self.api
        let outcome = await Task.detached {
            api.post("/remove/\(LifelineAPI.safe(uuid))", body: nil, limit: LifelineAPI.switchLimit)
        }.value
        await refresh()
        switch outcome {
        case .done(_, let message):
            closeDetail()
            show(message)
            return nil
        case .refused(let sentence):
            return sentence
        }
    }

    /// Reset Lifeline: the core's sentence, or its refusal, shows on the settings page.
    func reset() {
        settingsProblem = nil
        let api = self.api
        Task { @MainActor [weak self] in
            let outcome = await Task.detached { api.post("/reset", body: nil, limit: LifelineAPI.switchLimit) }.value
            guard let self else { return }
            switch outcome {
            case .done(_, let message):
                self.settingsProblem = message
            case .refused(let sentence):
                self.settingsProblem = sentence
            }
            await self.refresh()
        }
    }

    func rename(_ uuid: String, body: [String: String]) async -> String? {
        let api = self.api
        let refusal = await Task.detached {
            api.rename(uuid: uuid, body: body)
        }.value
        await refresh()
        return refusal
    }

    var note: String? {
        if let actionNote, !actionNote.isEmpty {
            return actionNote
        }
        if failing, state != nil, let time = lastGoodText {
            return Sentences.lastRead(time)
        }
        return nil
    }

    var lastGoodText: String? {
        guard let lastGood else { return nil }
        return Clock.text(lastGood, "HH:mm", .current)
    }
}

/// Whichever of two answers comes first; the other is dropped.
enum FirstOutcome {
    private final class Once: @unchecked Sendable {
        private let lock = NSLock()
        private var continuation: CheckedContinuation<PostOutcome, Never>?

        init(_ continuation: CheckedContinuation<PostOutcome, Never>) {
            self.continuation = continuation
        }

        func resume(_ outcome: PostOutcome) {
            lock.lock()
            let found = continuation
            continuation = nil
            lock.unlock()
            found?.resume(returning: outcome)
        }
    }

    static func race(_ first: @escaping @Sendable () async -> PostOutcome, or second: @escaping @Sendable () async -> PostOutcome) async -> PostOutcome {
        await withCheckedContinuation { continuation in
            let once = Once(continuation)
            Task { once.resume(await first()) }
            Task { once.resume(await second()) }
        }
    }
}
