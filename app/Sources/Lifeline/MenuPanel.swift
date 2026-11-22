import AppKit
import Combine
import LifelineKit
import SwiftUI

final class FirstClickHost<Content: View>: NSHostingView<Content> {
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool {
        true
    }
}

final class MenuWindow: NSPanel {
    var panelRects: () -> [NSRect] = { [] }
    var outside: () -> Void = {}

    init() {
        super.init(contentRect: .zero, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: true)
        isOpaque = false
        backgroundColor = .clear
        hasShadow = false
        level = .popUpMenu
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .transient, .ignoresCycle]
        isMovable = false
        hidesOnDeactivate = false
        becomesKeyOnlyIfNeeded = false
        isReleasedWhenClosed = false
        animationBehavior = .none
    }

    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }

    override func sendEvent(_ event: NSEvent) {
        let presses: [NSEvent.EventType] = [.leftMouseDown, .rightMouseDown, .otherMouseDown]
        if presses.contains(event.type), !panelRects().contains(where: { $0.contains(event.locationInWindow) }) {
            outside()
            return
        }
        super.sendEvent(event)
    }
}

struct SizeKey: PreferenceKey {
    static let defaultValue: CGSize = .zero

    static func reduce(value: inout CGSize, nextValue: () -> CGSize) {
        value = nextValue()
    }
}

struct PanelFrames: PreferenceKey {
    static let defaultValue: [CGRect] = []

    static func reduce(value: inout [CGRect], nextValue: () -> [CGRect]) {
        value.append(contentsOf: nextValue())
    }
}

extension View {
    func panelFrame() -> some View {
        background(GeometryReader { proxy in
            Color.clear.preference(key: PanelFrames.self, value: [proxy.frame(in: .named(MenuRoot.space))])
        })
    }
}

struct MenuRoot: View {
    static let space = "panels"

    @ObservedObject var store: StateStore
    @ObservedObject var status: AppStatus
    let pickDetail: (String) -> Void
    let sized: (CGSize) -> Void
    let framed: ([CGRect]) -> Void
    var dismiss: () -> Void = {}
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.colorScheme) private var scheme
    @State private var surface: CGSize = .zero
    @State private var sizedKey = ""

    private var screenKey: String {
        switch store.screen {
        case .menu:
            return "menu"
        case .settings:
            return "settings"
        case .add:
            if store.addCard != nil {
                return "add-waiting"
            }
            switch store.addStep {
            case .start: return "add-start"
            case .waiting: return "add-working"
            case .failed: return "add-result"
            case .done: return "add-done"
            case .added: return "add-naming"
            }
        }
    }

    @ViewBuilder
    private var screen: some View {
        switch store.screen {
        case .menu:
            menu
        case .add:
            if let job = store.addCard {
                compact(job)
            } else {
                addSheet
            }
        case .settings:
            settingsSheet
        }
    }

    var body: some View {
        let motion = Motion(MotionPlan(reduceMotion: reduceMotion))
        let key = screenKey
        ScreenSurface(size: surface == .zero ? nil : surface, palette: Palette.of(scheme)) {
            ZStack(alignment: .topLeading) {
                screen
                    .fixedSize()
                    .background(GeometryReader { proxy in
                        Color.clear.preference(key: ScreenSizes.self, value: [key: proxy.size])
                    })
                    .id(key)
                    .transition(ScreenSlide.transition(direction: store.screenDirection, plan: motion.plan.screens, width: Double(Metrics.panelWidth)))
            }
        }
        .animation(motion.screens, value: key)
        .onPreferenceChange(ScreenSizes.self) { sizes in
            guard let size = sizes[key], size.width > 0, size.height > 0, size != surface else { return }
            if surface == .zero || !motion.plan.screens.heightEases {
                var quiet = Transaction()
                quiet.disablesAnimations = true
                withTransaction(quiet) {
                    surface = size
                }
            } else if key != sizedKey {
                withAnimation(motion.screens) {
                    surface = size
                }
            } else {
                surface = size
            }
            sizedKey = key
        }
        .environment(\.menuMotion, motion)
        .environment(\.sharedSurface, true)
        .padding(EdgeInsets(top: Metrics.shadowTop, leading: Metrics.shadowSide, bottom: Metrics.shadowBottom, trailing: Metrics.shadowSide))
        .fixedSize()
        .coordinateSpace(name: Self.space)
        .background(GeometryReader { proxy in
            Color.clear.preference(key: SizeKey.self, value: proxy.size)
        })
        .onPreferenceChange(SizeKey.self) { size in
            sized(size)
        }
        .onPreferenceChange(PanelFrames.self) { frames in
            framed(frames)
        }
    }

    private func compact(_ job: JobSummary) -> some View {
        CompactWaiting(
            job: job,
            state: store.state,
            reopen: { id in store.reopenJob(id) },
            cancel: { id in store.cancelJob(id) },
            back: { store.backFromAdd() },
            problem: store.addProblem,
            cancelling: store.cancelling.contains(job.id)
        )
        .panelFrame()
    }

    private var addSheet: some View {
        SheetPanel(
            size: store.sheetSize(.add),
            limit: store.panelLimit,
            resize: { size in store.resizeSheet(.add, size) },
            commit: { store.commitSheet(.add) }
        ) { width in
            AddAccountView(
                state: store.state,
                step: store.addStep,
                run: store.addRun,
                target: store.chosenTarget,
                page: store.addPage,
                problem: store.addProblem,
                zone: .current,
                handlers: AddHandlers(
                    start: { kind in store.startAdd(kind) },
                    choose: { target in store.addTarget = target },
                    reopen: { id in store.reopenJob(id) },
                    finish: { uuid, body in await store.rename(uuid, body: body) },
                    close: { store.finishAdd() },
                    pick: { known in store.addPick = known },
                    back: { store.backFromAdd() },
                    cancel: { id in store.cancelJob(id) },
                    retry: { kind in store.retryAdd(kind) }
                ),
                width: width,
                pick: store.chosenPick,
                note: store.addNote,
                settling: store.settlingJobs
            )
        }
        .panelFrame()
    }

    private var settingsSheet: some View {
        SheetPanel(
            size: store.sheetSize(.settings),
            limit: store.panelLimit,
            resize: { size in store.resizeSheet(.settings, size) },
            commit: { store.commitSheet(.settings) }
        ) { width in
            SettingsView(
                state: store.state,
                local: status.local,
                problem: store.settingsProblem,
                width: width,
                handlers: SettingsHandlers(
                    change: { key, on in store.change(key, on: on) },
                    login: { on in
                        status.setLogin(on)
                        store.showSettingsProblem(nil)
                    },
                    statusline: { on in store.statusline(on: on) },
                    place: { uuid, value in store.place(uuid, value) },
                    pair: { address, code in store.pair(address: address, code: code) },
                    quit: { NSApplication.shared.terminate(nil) },
                    reset: { store.reset() },
                    back: { store.backFromSettings() }
                )
            )
        }
        .panelFrame()
    }

    private var menu: some View {
        MenuView(
            state: store.state,
            side: store.side,
            note: store.note,
            handlers: MenuHandlers(
                pick: { side in store.side = side },
                run: { command, via in store.run(command, via: via) },
                addAccount: {
                    store.closeDetail()
                    store.screen = .add
                },

                settings: {
                    store.closeDetail()
                    status.refreshLogin()
                    store.screen = .settings
                },
                detail: pickDetail,
                heroAdd: { add in
                    store.closeDetail()
                    store.heroAdd(add)
                }
            ),
            openDetail: store.detail,
            expansion: ExpansionInputs(
                history: { uuid in store.histories[uuid] ?? .waiting },
                now: Date(),
                zone: .current,
                handlers: DetailHandlers(
                    use: { target in store.run(.switchTo(target), via: .row) },
                    resign: { target in store.run(.resign(target), via: .row) },
                    rename: { target, body in await store.rename(target, body: body) },
                    remove: { target in await store.remove(target) }
                ),
                cancelRename: store.renameCancel
            ),
            maxHeight: store.panelLimit,
            results: store.unseenResults,
            waiting: store.cardJobs,
            cardActions: CardActions(
                open: { id in
                    store.reopenJob(id)
                    dismiss()
                },
                cancel: { id in store.cancelJob(id) },
                retry: { job in store.retry(job) },
                show: { id in store.openWaiting(id) }
            ),
            cancelling: store.cancelling,
            switching: store.switching,
            switchSettledAt: store.switchSettledAt,
            cardsShown: { ids in store.cardsShown(ids) },
            alerts: store.unseenAlerts,
            pendingSwitch: store.pendingSwitch,
            undoGone: store.undoGone,
            highlight: store.highlight
        )
        .task(id: store.undoSince) {
            guard let since = store.undoSince else { return }
            let end = store.undoUntil ?? since.addingTimeInterval(MenuLogic.undoWindow)
            let left = end.timeIntervalSinceNow
            if left > 0 {
                try? await Task.sleep(for: .seconds(left))
            }
            store.undoTimeUp()
        }
        .panelFrame()
    }
}

@MainActor
final class MenuPanel: NSObject, NSWindowDelegate {
    private let store: StateStore
    private let status: AppStatus
    private let item: NSStatusItem
    private var window: MenuWindow?
    private var size = CGSize.zero
    private var top: CGFloat = 0
    private var menuLeft: CGFloat = 0
    private var anchorLeft: CGFloat = 0
    private var visible = NSRect.zero
    private var screenWatch: AnyCancellable?
    private var frames: [CGRect] = []
    private var monitors: [Any] = []
    private var watch: AnyCancellable?
    private var closedAt = Date.distantPast
    private var previousApp: NSRunningApplication?
    private var drawn: (IconFields, String)?
    private var iconTimer: Timer?
    private var shrink: Timer?
    private var startsSeen = 0
    private var raisedFor: String?

    private let unseen: Bool

    init(store: StateStore, status: AppStatus, showsItem: Bool = true, unseen: Bool = false) {
        self.store = store
        self.status = status
        self.unseen = unseen
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.isVisible = showsItem
        super.init()
        screenWatch = store.$screen.sink { [weak self] _ in
            Task { @MainActor in
                self?.reframe()
            }
        }
        if let button = item.button {
            button.target = self
            button.action = #selector(toggle)
            button.sendAction(on: [.leftMouseDown, .rightMouseDown])
            button.imagePosition = .imageOnly
            button.setAccessibilityLabel("Lifeline")
        }
        drawIcon()
        NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.activeSpaceDidChangeNotification, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated {
                self?.closeUnlessWaiting(returningFocus: false)
            }
        }
        watch = store.objectWillChange.sink { [weak self] _ in
            Task { @MainActor in
                self?.drawIcon()
                self?.breathe()
                self?.followSignIn()
            }
        }
    }

    var isOpen: Bool {
        window?.isVisible == true
    }

    func shownMenu(for fresh: ServiceState) -> ShownMenu? {
        guard isOpen else { return nil }
        return ShownMenu(model: fresh.model(for: store.side), note: store.note)
    }

    private func drawIcon() {
        guard let button = item.button else { return }
        let light = button.effectiveAppearance.bestMatch(from: [.darkAqua, .aqua]) == .aqua
        let appearance = light ? "light" : "dark"
        let fields = IconFields.from(store.state)
        let label = IconFields.label(store.state)
        if button.accessibilityLabel() != label {
            button.setAccessibilityLabel(label)
        }
        if let drawn, drawn.0 == fields, drawn.1 == appearance {
            return
        }
        let before = drawn?.0
        drawn = (fields, appearance)
        iconTimer?.invalidate()
        iconTimer = nil
        let plan = MotionPlan(reduceMotion: NSWorkspace.shared.accessibilityDisplayShouldReduceMotion)
        guard let before, before.fill != fields.fill, let seconds = plan.glide else {
            paintIcon(fields, appearance: appearance)
            return
        }
        let start = Date()
        let from = before.fill
        paintIcon(fields.withFill(from), appearance: appearance)
        iconTimer = Timer.scheduledTimer(withTimeInterval: 1.0 / 30, repeats: true) { [weak self] timer in
            let finished = MainActor.assumeIsolated { () -> Bool in
                guard let self else { return true }
                let progress = min(Date().timeIntervalSince(start) / seconds, 1)
                let eased = Motion.curve.value(at: progress)
                self.paintIcon(fields.withFill(from + (fields.fill - from) * eased), appearance: appearance)
                if progress >= 1 {
                    self.iconTimer = nil
                    return true
                }
                return false
            }
            if finished {
                timer.invalidate()
            }
        }
    }

    private func breathe() {
        guard let button = item.button else { return }
        Breath.apply(button, on: store.breathing, reduceMotion: NSWorkspace.shared.accessibilityDisplayShouldReduceMotion)
    }

    private func paintIcon(_ fields: IconFields, appearance: String) {
        guard let button = item.button else { return }
        button.image = MenuBarIcon.image(fields: fields, appearance: appearance, scale: button.window?.backingScaleFactor)
    }

    @objc func toggle() {
        switch JobFlow.toggleMove(open: isOpen, holding: store.addHolding) {
        case .close:
            close()
        case .raise:
            raise()
        default:
            if Date().timeIntervalSince(closedAt) < 0.25 {
                return
            }
            open()
        }
    }

    func open() {
        var quiet = Transaction()
        quiet.disablesAnimations = true
        withTransaction(quiet) {
            store.opened()
        }
        store.menuOpen = true
        store.markVisibleResults()
        startsSeen = store.signInStarts
        let window = self.window ?? makeWindow()
        self.window = window
        if size == .zero, let host = window.contentView {
            size = host.fittingSize
        }
        place(window)
        let front = NSWorkspace.shared.frontmostApplication
        if let front, front.processIdentifier != ProcessInfo.processInfo.processIdentifier {
            previousApp = front
        }
        takeFocus(window)
        startMonitors()
        Task { await store.refresh() }
        store.prefetchHistories()
    }

    func close(returningFocus: Bool = true) {
        store.panelClosed(visible: window?.isVisible == true)
        startsSeen = store.signInStarts
        let previous = previousApp
        previousApp = nil
        guard let window, window.isVisible else {
            store.closeDetail()
            return
        }
        window.orderOut(nil)
        stopMonitors()
        closedAt = Date()
        store.closeDetail()
        if returningFocus, NSApp.isActive, let previous, !previous.isTerminated {
            previous.activate()
        }
    }

    var shownFrame: NSRect? {
        window?.isVisible == true ? window?.frame : nil
    }

    var shownAlpha: CGFloat? {
        window?.alphaValue
    }

    var shownIgnoresClicks: Bool? {
        window?.ignoresMouseEvents
    }

    private func takeFocus(_ window: MenuWindow) {
        guard !unseen else {
            window.alphaValue = 0
            window.ignoresMouseEvents = true
            window.orderFrontRegardless()
            return
        }
        NSApp.activate()
        window.makeKeyAndOrderFront(nil)
    }

    private func makeWindow() -> MenuWindow {
        let window = MenuWindow()
        let root = MenuRoot(
            store: store,
            status: status,
            pickDetail: { [weak self] uuid in
                self?.toggleDetail(uuid)
            },
            sized: { [weak self] measured in
                self?.resize(measured)
            },
            framed: { [weak self] found in
                self?.frames = found
            },
            dismiss: { [weak self] in
                self?.close(returningFocus: false)
            }
        )
        let host = FirstClickHost(rootView: root)
        window.contentView = host
        window.delegate = self
        window.panelRects = { [weak self, weak window] in
            MainActor.assumeIsolated {
                guard let self, let host = window?.contentView else { return [] }
                let panel = MenuLogic.panelArea(
                    bounds: host.frame, top: Double(Metrics.shadowTop), side: Double(Metrics.shadowSide), bottom: Double(Metrics.shadowBottom)
                )
                return [panel] + self.frames.map { host.convert($0, to: nil) }
            }
        }
        window.outside = { [weak self] in
            MainActor.assumeIsolated {
                self?.closeUnlessWaiting()
            }
        }
        return window
    }

    private func place(_ window: MenuWindow) {
        guard let anchor = (unseen ? NSRect(x: -10000, y: 2000, width: 0, height: 0) : nil) ?? item.button?.window?.frame ?? hiddenAnchor() else { return }
        visible = visibleFrame(fallback: anchor)
        anchorLeft = anchor.minX
        menuLeft = left(for: Metrics.panelWidth)
        top = anchor.minY
        store.panelLimit = MenuLogic.panelLimit(top: top, visibleBottom: visible.minY, shadowTop: Metrics.shadowTop)
        store.sheetMaxWidth = max(visible.maxX - menuLeft - 8, Metrics.panelWidth)
        window.setFrame(frame(), display: true)
    }

    private func hiddenAnchor() -> NSRect? {
        guard !item.isVisible, let screen = NSScreen.main else { return nil }
        let area = screen.visibleFrame
        return NSRect(x: area.maxX - Metrics.panelWidth - 8, y: area.maxY, width: 0, height: 0)
    }

    private func left(for width: CGFloat) -> CGFloat {
        var left = min(anchorLeft, visible.maxX - width - 8)
        left = max(left, visible.minX + 8)
        return left
    }

    private func reframe() {
        guard let window, window.isVisible else { return }
        window.setFrame(frame(), display: true)
    }

    private func visibleFrame(fallback: NSRect) -> NSRect {
        if unseen {
            return NSRect(x: -10008, y: 800, width: 2400, height: 1200)
        }
        let screen = item.button?.window?.screen ?? NSScreen.main
        return screen?.visibleFrame ?? fallback
    }

    private func frame() -> NSRect {
        let origin = MenuLogic.menuOrigin(left: menuLeft, top: top, height: size.height, shadow: Metrics.shadowSide)
        return NSRect(x: origin.x, y: origin.y, width: size.width, height: size.height)
    }

    private func followSignIn() {
        guard let window, window.isVisible else { return }
        if store.signInStarts != startsSeen {
            startsSeen = store.signInStarts
            raisedFor = nil
        }
        guard store.addHolding else { return }
        if let job = store.addJob, raisedFor != job.id, let name = JobFlow.browserToRaise(job) {
            raisedFor = job.id
            raiseBrowser(named: name)
            window.orderFrontRegardless()
        }
    }

    private func raise() {
        guard let window else { return }
        takeFocus(window)
    }

    private func raiseBrowser(named name: String) {
        NSWorkspace.shared.runningApplications.first { $0.localizedName == name }?.activate()
    }

    private func closeUnlessWaiting(returningFocus: Bool = true) {
        if JobFlow.outsideMove(holding: store.addHolding) == .close {
            close(returningFocus: returningFocus)
        }
    }

    private func resize(_ measured: CGSize) {
        guard measured.width > 0, measured.height > 0, measured != size else { return }
        shrink?.invalidate()
        shrink = nil
        let plan = MotionPlan(reduceMotion: NSWorkspace.shared.accessibilityDisplayShouldReduceMotion).screens
        if plan.heightEases, let window, window.isVisible, measured.height < size.height || measured.width < size.width {
            shrink = Timer.scheduledTimer(withTimeInterval: plan.settle, repeats: false) { [weak self] _ in
                MainActor.assumeIsolated {
                    self?.shrink = nil
                    self?.apply(measured)
                }
            }
            return
        }
        apply(measured)
    }

    private func apply(_ measured: CGSize) {
        size = measured
        guard let window, window.isVisible else { return }
        window.setFrame(frame(), display: true)
    }

    func openDetail(_ uuid: String, keyboard: Bool = false) {
        guard isOpen, store.state?.accounts?.contains(where: { $0.uuid == uuid }) == true else { return }
        store.showDetail(uuid, keyboard: keyboard)
    }

    private func toggleDetail(_ uuid: String) {
        if MenuLogic.expanded(after: uuid, current: store.detail) == nil {
            store.closeDetail()
        } else {
            openDetail(uuid)
        }
    }

    private func startMonitors() {
        stopMonitors()
        if let keys = NSEvent.addLocalMonitorForEvents(matching: .keyDown, handler: { [weak self] event in
            let code = event.keyCode
            let characters = event.charactersIgnoringModifiers
            let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask).rawValue
            let used = MainActor.assumeIsolated { () -> Bool in
                guard let self, let window = self.window, window.isKeyWindow else { return false }
                if window.firstResponder is NSText {
                    guard MenuLogic.fieldKey(code) == .cancel else { return false }
                    self.store.renameCancel += 1
                    window.makeFirstResponder(nil)
                    return true
                }
                return self.handle(code: code, characters: characters, flags: NSEvent.ModifierFlags(rawValue: flags))
            }
            return used ? nil : event
        }) {
            monitors.append(keys)
        }
        if let clicks = NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown, .otherMouseDown], handler: { [weak self] _ in
            MainActor.assumeIsolated {
                self?.closeUnlessWaiting(returningFocus: false)
            }
        }) {
            monitors.append(clicks)
        }
    }

    private func stopMonitors() {
        for monitor in monitors {
            NSEvent.removeMonitor(monitor)
        }
        monitors.removeAll()
    }

    private func handle(code: UInt16, characters: String?, flags: NSEvent.ModifierFlags) -> Bool {
        if store.screen != .menu {
            let action = MenuLogic.sheetKeyAction(
                keyCode: code, characters: characters, command: flags.contains(.command), holding: store.addHolding, back: store.screen == .settings
            )
            switch action {
            case .back:
                if store.screen == .settings {
                    store.backFromSettings()
                } else {
                    store.backFromAdd()
                }
            case .close:
                close()
            case .quit:
                NSApplication.shared.terminate(nil)
            default:
                return false
            }
            return true
        }
        let action = MenuLogic.keyAction(
            keyCode: code,
            characters: characters,
            shift: flags.contains(.shift),
            command: flags.contains(.command),
            option: flags.contains(.option),
            control: flags.contains(.control),
            state: store.state,
            detailOpen: store.detail != nil,
            side: store.side,
            highlight: store.highlight,
            undoShows: !store.undoGone
        )
        switch action {
        case .close:
            close()
        case .highlight(let uuid):
            store.highlight = uuid
        case .openRow(let uuid):
            if store.detail != uuid {
                openDetail(uuid, keyboard: true)
            }
        case .heroAdd(let add):
            store.closeDetail()
            store.heroAdd(add)
        case .quit:
            NSApplication.shared.terminate(nil)
        case .command(let command):
            store.side = MenuLogic.side(after: action, on: store.side)
            store.run(command, via: .key)
        case .note(let sentence):
            store.show(sentence)
        case .detail(let move):
            let rows = store.state?.model(for: store.side)?.rows ?? []
            switch move {
            case .open:
                if let target = MenuLogic.detailTarget(model: store.state?.model(for: store.side)) {
                    openDetail(target, keyboard: true)
                }
            case .close:
                store.closeDetail()
            case .previous, .next:
                if let target = Detail.step(from: store.detail, by: move == .next ? 1 : -1, rows: rows) {
                    openDetail(target, keyboard: true)
                    store.highlight = target
                }
            }
        case .back, .ignore:
            return false
        }
        return true
    }

    func windowDidResignKey(_ notification: Notification) {
        closeUnlessWaiting(returningFocus: false)
    }
}
