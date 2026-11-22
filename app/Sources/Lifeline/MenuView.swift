import AppKit
import LifelineKit
import SwiftUI

enum CardEntry {
    case waiting(JobSummary)
    case alert(NoticeSummary)
    case result(JobSummary)

    var key: String {
        switch self {
        case .waiting(let job), .result(let job):
            return job.id
        case .alert(let notice):
            return JobFlow.noticeKey(notice.id)
        }
    }

    static func ordered(waiting: [JobSummary], alerts: [NoticeSummary], results: [JobSummary]) -> [CardEntry] {
        waiting.reversed().map(CardEntry.waiting) + alerts.reversed().map(CardEntry.alert) + results.reversed().map(CardEntry.result)
    }
}

struct CappedColumn: Layout {
    let limit: CGFloat?
    let spacing: CGFloat
    let minimum: CGFloat
    var stops: [CGFloat] = []

    func heights(_ subviews: Subviews, width: CGFloat?) -> [CGFloat] {
        let natural = subviews.map { $0.sizeThatFits(ProposedViewSize(width: width, height: nil)).height }
        guard let limit else { return natural }
        if natural.count == 3 {
            let room = limit - natural[0] - natural[2] - spacing * 2
            return [natural[0], min(natural[1], max(room, minimum)), natural[2]]
        }
        guard natural.count == 4 else { return natural }
        return MenuLogic.cappedHeights(
            cards: natural[0], top: natural[1], list: natural[2], footer: natural[3], limit: limit, spacing: spacing, minimum: minimum,
            cardMinimum: Metrics.minimumCards, listStops: stops.map { Double($0) }
        ).map { CGFloat($0) }
    }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = subviews.map { $0.sizeThatFits(ProposedViewSize(width: proposal.width, height: nil)).width }.max() ?? 0
        let heights = heights(subviews, width: proposal.width)
        let shown = heights.filter { $0 > 0 }.count
        let total = heights.reduce(0, +) + spacing * CGFloat(max(shown - 1, 0))
        return CGSize(width: proposal.width ?? width, height: total)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var heights = heights(subviews, width: bounds.width)
        var spare: CGFloat = 0
        if heights.count == 4 {
            let natural = subviews[2].sizeThatFits(ProposedViewSize(width: bounds.width, height: nil)).height
            let rested = MenuLogic.rest(list: Double(heights[2]), natural: Double(natural), stops: stops.map { Double($0) })
            heights[2] = CGFloat(rested.list)
            spare = CGFloat(rested.spare)
        }
        var y = bounds.minY
        for (index, subview) in subviews.enumerated() {
            let height = heights[index]
            subview.place(at: CGPoint(x: bounds.minX, y: y), anchor: .topLeading, proposal: ProposedViewSize(width: bounds.width, height: height))
            if height > 0 {
                y += height + spacing
            }
            if index == 2 {
                y += spare
            }
        }
    }
}

struct ListHeight: PreferenceKey {
    static let defaultValue: CGFloat = 0

    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = max(value, nextValue())
    }
}

struct CardsOffset: PreferenceKey {
    static let defaultValue: CGFloat = 0

    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = nextValue()
    }
}

struct CardFrames: PreferenceKey {
    static let defaultValue: [String: CGRect] = [:]

    static func reduce(value: inout [String: CGRect], nextValue: () -> [String: CGRect]) {
        value.merge(nextValue()) { _, new in new }
    }
}

struct CardFrame: View {
    let id: String

    var body: some View {
        GeometryReader { proxy in
            Color.clear.preference(key: CardFrames.self, value: [id: proxy.frame(in: .named(MenuView.cardsSpace))])
        }
    }
}

struct CardsHeight: PreferenceKey {
    static let defaultValue: CGFloat = 0

    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = max(value, nextValue())
    }
}

struct ListOffset: PreferenceKey {
    static let defaultValue: CGFloat = 0

    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = nextValue()
    }
}

struct ClipOffset: NSViewRepresentable {
    let changed: @MainActor (CGFloat) -> Void

    func makeNSView(context: Context) -> ClipProbe {
        ClipProbe()
    }

    func updateNSView(_ view: ClipProbe, context: Context) {
        view.changed = changed
        view.attach()
    }
}

final class ClipProbe: NSView {
    var changed: (@MainActor (CGFloat) -> Void)?
    private weak var clip: NSClipView?

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        attach()
    }

    func attach() {
        guard let found = enclosingScrollView?.contentView, found !== clip else { return }
        if let clip {
            NotificationCenter.default.removeObserver(self, name: NSView.boundsDidChangeNotification, object: clip)
        }
        clip = found
        enclosingScrollView?.hasVerticalScroller = false // legacy always-on bars ignore .scrollIndicators(.hidden); the list draws its own
        found.postsBoundsChangedNotifications = true
        NotificationCenter.default.addObserver(self, selector: #selector(moved(_:)), name: NSView.boundsDidChangeNotification, object: found)
    }

    @objc private func moved(_ note: Notification) {
        guard let clip else { return }
        let offset = -clip.bounds.origin.y
        DispatchQueue.main.async { [weak self] in
            MainActor.assumeIsolated {
                self?.changed?(offset)
            }
        }
    }
}

struct Measured<Key: PreferenceKey>: View where Key.Value == CGFloat {
    let key: Key.Type

    var body: some View {
        GeometryReader { proxy in
            Color.clear.preference(key: key, value: proxy.size.height)
        }
    }
}

struct EdgeFade: View {
    let top: Bool
    let bottom: Bool
    var topHeight: CGFloat = Metrics.edgeFade
    var bottomHeight: CGFloat = Metrics.edgeFade

    var body: some View {
        VStack(spacing: 0) {
            LinearGradient(colors: [top ? .clear : .black, .black], startPoint: .top, endPoint: .bottom)
                .frame(height: top ? topHeight : 0)
            Color.black
            LinearGradient(colors: [.black, bottom ? .clear : .black], startPoint: .top, endPoint: .bottom)
                .frame(height: bottom ? bottomHeight : 0)
        }
    }
}

public struct MenuHandlers {
    public var pick: (MenuSide) -> Void
    public var run: (MenuCommand, Via) -> Void
    public var addAccount: (() -> Void)?
    public var settings: (() -> Void)?
    public var detail: ((String) -> Void)?
    public var heroAdd: ((HeroAdd) -> Void)?

    public init(
        pick: @escaping (MenuSide) -> Void = { _ in },
        run: @escaping (MenuCommand, Via) -> Void = { _, _ in },
        addAccount: (() -> Void)? = nil,
        settings: (() -> Void)? = nil,
        detail: ((String) -> Void)? = nil,
        heroAdd: ((HeroAdd) -> Void)? = nil
    ) {
        self.pick = pick
        self.run = run
        self.addAccount = addAccount
        self.settings = settings
        self.detail = detail
        self.heroAdd = heroAdd
    }

    func perform(_ click: RowClick) {
        switch click {
        case .run(let command):
            run(command, .row)
        case .detail(let uuid):
            detail?(uuid)
        case .nothing:
            break
        }
    }
}

struct SwitchRun: Equatable {
    let old: ServiceState
    let new: ServiceState
    let side: MenuSide
    let from: String
    let to: String
    let start: Date

    static func make(old: ServiceState?, new: ServiceState?, side: MenuSide, plan: MotionPlan, start: Date) -> SwitchRun? {
        guard let old, let new,
              case .lift(let from, let to)? = MenuLogic.switchStarts(
                  from: (side, old.model(for: side)?.activeUuid),
                  to: (side, new.model(for: side)?.activeUuid),
                  plan: plan
              ) else {
            return nil
        }
        return SwitchRun(old: old, new: new, side: side, from: from, to: to, start: start)
    }
}

public struct MenuView: View {
    let state: ServiceState?
    let side: MenuSide
    let note: String?
    let countdownFraction: Double?
    let handlers: MenuHandlers
    let stillFrom: ServiceState?
    let stillAt: Double
    let openDetail: String?
    let highlight: String?
    let expansion: ExpansionInputs?
    let maxHeight: CGFloat?
    let results: [JobSummary]
    let waiting: [JobSummary]
    let cardActions: CardActions
    let cancelling: Set<String>
    let switching: Bool
    let switchSettledAt: Date?
    let cardsShown: (Set<String>) -> Void
    let alerts: [NoticeSummary]
    let pendingSwitch: String?
    let undoGone: Bool

    @Environment(\.colorScheme) private var scheme
    @Environment(\.menuMotion) private var motion
    @Environment(\.sharedSurface) private var shared
    @Environment(\.stillFields) private var still
    @State private var shown: ServiceState?
    @State private var run: SwitchRun?
    @State private var arriving: ServiceState?
    @State private var listHeight: CGFloat = 0
    @State private var listOffset: CGFloat = 0
    @State private var viewport: CGFloat = 0
    @State private var rowFrames: [String: CGRect] = [:]
    @State private var cardsHeight: CGFloat = 0
    @State private var cardsViewport: CGFloat = 0
    @State private var cardsOffset: CGFloat = 0
    @State private var cardFrames: [String: CGRect] = [:]
    @State private var cardIndex = 0
    @State private var barSince: Date?
    @State private var barLive = false

    public init(
        state: ServiceState?,
        side: MenuSide,
        note: String?,
        countdownFraction: Double? = nil,
        handlers: MenuHandlers = MenuHandlers(),
        switchFrom: ServiceState? = nil,
        at time: Double = 0,
        openDetail: String? = nil,
        expansion: ExpansionInputs? = nil,
        maxHeight: CGFloat? = nil,
        results: [JobSummary] = [],
        waiting: [JobSummary] = [],
        cardActions: CardActions = CardActions(),
        cancelling: Set<String> = [],
        switching: Bool = false,
        switchSettledAt: Date? = nil,
        cardsShown: @escaping (Set<String>) -> Void = { _ in },
        alerts: [NoticeSummary] = [],
        pendingSwitch: String? = nil,
        undoGone: Bool = false,
        highlight: String? = nil
    ) {
        self.highlight = highlight
        self.undoGone = undoGone
        self.state = state
        self.side = side
        self.note = note
        self.countdownFraction = countdownFraction
        self.handlers = handlers
        self.stillFrom = switchFrom
        self.stillAt = time
        self.openDetail = openDetail
        self.expansion = expansion
        self.maxHeight = maxHeight
        self.results = results
        self.waiting = waiting
        self.cardActions = cardActions
        self.cancelling = cancelling
        self.switching = switching
        self.switchSettledAt = switchSettledAt
        self.cardsShown = cardsShown
        self.alerts = alerts
        self.pendingSwitch = pendingSwitch
        _shown = State(initialValue: state)
    }

    public var body: some View {
        let palette = Palette.of(scheme)
        TimelineView(.animation(minimumInterval: nil, paused: run == nil)) { context in
            panel(palette: palette, now: context.date)
        }
        .keepsNames(of: state)
        .onChange(of: state) { _, new in
            arrive(new)
        }
    }

    private func active(now: Date) -> (SwitchRun, SwitchFrame)? {
        guard let timeline = motion.plan.switching else { return nil }
        if let stillFrom {
            guard let made = SwitchRun.make(old: stillFrom, new: state, side: side, plan: motion.plan, start: now) else {
                return nil
            }
            return (made, timeline.frame(at: stillAt))
        }
        guard let run, run.side == side else { return nil }
        return (run, timeline.frame(at: now.timeIntervalSince(run.start)))
    }

    @ViewBuilder
    private func panel(palette: Palette, now: Date) -> some View {
        let moving = active(now: now)
        let showing: ServiceState? = {
            if case let (made, frame)? = moving {
                return frame.showsOld ? made.old : made.new
            }
            if stillFrom != nil {
                return state
            }
            return run == nil ? shown : (arriving ?? shown)
        }()
        let model = showing?.model(for: side)
        Group {
            if let showing, let model {
                CappedColumn(
                    limit: maxHeight.map { $0 - 2 * Metrics.panelPadding }, spacing: Metrics.panelGap, minimum: Metrics.minimumList,
                    stops: MenuLogic.listStops(
                        tops: rowFrames.values.map { Double($0.minY) },
                        bottoms: rowFrames.values.map { Double($0.maxY) },
                        open: openDetail.flatMap { rowFrames[$0].map { Double($0.minY) } },
                        content: max(Double(listHeight), rowFrames.values.map { Double($0.maxY) }.max() ?? 0)
                    ).map { CGFloat($0) }
                ) {
                cards(showing, palette: palette)
                VStack(alignment: .leading, spacing: Metrics.panelGap) {
                    if let computer = showing.devbox?.name {
                        PairControl(side: side, palette: palette, pick: handlers.pick, computer: computer, devboxDot: showing.mac?.iconDot == true)
                    }
                    HeroView(
                        model: PendingLook.model(model, state: showing, pending: pendingSwitch, side: side), palette: palette, look: heroLook(model, moving), animated: moving == nil,
                        countdown: MenuLogic.showsCountdown(model, side: side) && pendingSwitch == nil ? showing.countdown : nil,
                        countdownFraction: countdownFraction
                    )
                    if side == .mac, let note = showing.mac?.devboxNote, !note.isEmpty {
                        Block(note, Styles.small, palette.low.ink, width: Metrics.contentWidth)
                            .padding(.horizontal, Metrics.sideInset)
                    }
                    if Self.showsAccountsLabel(model) {
                        LineText("Your accounts", Styles.label, palette.faint)
                            .padding(EdgeInsets(top: 8, leading: Metrics.sideInset, bottom: 2, trailing: Metrics.sideInset))
                    }
                }
                list(model, palette: palette, moving: moving)
                Footer(
                    model: model,
                    old: footerOld(moving),
                    fade: textFade(moving),
                    side: side,
                    countdown: showing.countdown,
                    countdownFraction: countdownFraction,
                    note: note,
                    palette: palette,
                    handlers: handlers,
                    switching: switching,
                    animating: moving != nil,
                    settledAt: switchSettledAt,
                    undoGone: undoGone
                )
                .animation(motion.screens, value: undoGone)
                .padding(.top, 6)
                }
            } else {
                VStack(alignment: .leading, spacing: Metrics.panelGap) {
                    Block(showing == nil ? Sentences.notRunning : Sentences.noMenu, Styles.head, palette.ink, width: Metrics.contentWidth)
                        .padding(EdgeInsets(top: 14, leading: Metrics.sideInset, bottom: 8, trailing: Metrics.sideInset))
                    VStack(alignment: .leading, spacing: 6) {
                        if let note, !note.isEmpty {
                            Block(note, Styles.small, palette.muted, width: Metrics.innerWidth, centered: true)
                        }
                        FooterButtons(palette: palette, handlers: handlers)
                    }
                    .padding(.top, 6)
                }
            }
        }
        .animation(motion.screens, value: CardEntry.ordered(waiting: waiting, alerts: alerts, results: results).first?.key ?? "")
        .padding(Metrics.panelPadding)
        .frame(width: Metrics.panelWidth, alignment: .topLeading)
        .background {
            if !shared {
                PanelSurface(palette: palette)
            }
        }
        .focusEffectDisabled()
        .onPreferenceChange(ListHeight.self) { height in
            listHeight = height
        }
        .onPreferenceChange(ListOffset.self) { offset in
            listOffset = offset
        }
    }

    static let listSpace = "list"
    static let cardsSpace = "cards"

    /// The travelling light rides the 3 pt line of each row, so it glides between the lines and never sits on a word.
    static func orbY(from: CGRect, to: CGRect, travel: Double) -> CGFloat {
        let start = from.minY + AccountRow.lineY
        return start + (to.minY + AccountRow.lineY - start) * CGFloat(travel)
    }

    static func showsAccountsLabel(_ model: MenuModel) -> Bool {
        !(model.rows ?? []).isEmpty
    }

    @ViewBuilder
    private func cards(_ showing: ServiceState, palette: Palette) -> some View {
        let entries = CardEntry.ordered(waiting: waiting, alerts: alerts, results: results)
        if entries.isEmpty {
            Color.clear.frame(width: Metrics.innerWidth, height: 0)
        } else {
            let index = min(cardIndex, entries.count - 1)
            let entry = entries[index]
            let next: (() -> Void)? = index < entries.count - 1 ? { cardIndex = index + 1 } : nil
            let stack = VStack(alignment: .leading, spacing: 0) {
                Group {
                    switch entry {
                    case .waiting(let job):
                        WaitingCard(job: job, palette: palette, actions: cardActions, state: showing, cancelling: cancelling.contains(job.id))
                    case .alert(let notice):
                        AlertCard(notice: notice, palette: palette, next: next)
                    case .result(let job):
                        ResultCard(job: job, palette: palette, actions: cardActions, state: showing, next: next)
                    }
                }
                .background(CardFrame(id: entry.key))
                .id(entry.key)
                .transition(.asymmetric(insertion: .opacity.combined(with: .offset(y: motion.plan.screens.slides ? -10 : 0)), removal: .opacity))
            }
            .animation(motion.screens, value: entry.key)
            .background(Measured(key: CardsHeight.self))
            .background(GeometryReader { proxy in
                Color.clear.preference(key: CardsOffset.self, value: proxy.frame(in: .named(Self.cardsSpace)).minY)
            })
            if maxHeight != nil {
                let fades = MenuLogic.listFades(offset: cardsOffset, content: cardsHeight, limit: cardsViewport)
                ScrollView(.vertical) {
                    stack
                }
                .coordinateSpace(name: Self.cardsSpace)
                .scrollIndicators(.hidden)
                .background(GeometryReader { box in
                    Color.clear
                        .onAppear { cardsViewport = box.size.height }
                        .onChange(of: box.size.height) { _, height in cardsViewport = height }
                })
                .mask(EdgeFade(top: fades.top, bottom: fades.bottom))
                .onPreferenceChange(CardsHeight.self) { height in
                    cardsHeight = height
                }
                .onPreferenceChange(CardsOffset.self) { offset in
                    cardsOffset = offset
                }
                .onPreferenceChange(CardFrames.self) { frames in
                    cardFrames = frames
                    cardsShown(MenuLogic.visibleCards(frames: frames, viewport: cardsViewport))
                }
                .onChange(of: cardsViewport) { _, height in
                    cardsShown(MenuLogic.visibleCards(frames: cardFrames, viewport: height))
                }
            } else {
                stack
                    .onAppear { cardsShown([entry.key]) }
                    .onChange(of: entry.key) { _, now in cardsShown([now]) }
            }
        }
    }

    @ViewBuilder
    private func list(_ model: MenuModel, palette: Palette, moving: (SwitchRun, SwitchFrame)?) -> some View {
        let content = rows(model, palette: palette, moving: moving)
            .onPreferenceChange(RowFrames.self) { frames in
                rowFrames = frames
            }
            .background(Measured(key: ListHeight.self))
            .background(ClipOffset { [offsetBinding = $listOffset] offset in
                if abs(offset - offsetBinding.wrappedValue) > 0.5 {
                    offsetBinding.wrappedValue = offset
                }
            })
            .background(GeometryReader { proxy in
                Color.clear.preference(key: ListOffset.self, value: proxy.frame(in: .named(Self.listSpace)).minY)
            })
        if maxHeight != nil {
            let fades = MenuLogic.listFades(offset: listOffset, content: listHeight, limit: viewport)
            let edges = MenuLogic.restingEdges(
                offset: listOffset, viewport: viewport, tops: rowFrames.values.map { Double($0.minY) }, bottoms: rowFrames.values.map { Double($0.maxY) }
            )
            ScrollViewReader { proxy in
                ScrollView(.vertical) {
                    content
                }
                .coordinateSpace(name: Self.listSpace)
                .scrollIndicators(.hidden)
                .background(GeometryReader { box in
                    Color.clear
                        .onAppear { viewport = box.size.height }
                        .onChange(of: box.size.height) { _, height in viewport = height }
                })
                .mask(EdgeFade(top: fades.top, bottom: fades.bottom, topHeight: edges.top ? Metrics.restFade : Metrics.edgeFade, bottomHeight: edges.bottom ? Metrics.restFade : Metrics.edgeFade))
                .overlay(alignment: .topTrailing) {
                    if let bar = MenuLogic.scrollIndicator(offset: Double(listOffset), content: Double(listHeight), viewport: Double(viewport)) {
                        TimelineView(.animation(minimumInterval: 1.0 / 30, paused: !barLive)) { context in
                            Capsule()
                                .fill(palette.faint.opacity(0.55))
                                .frame(width: 3, height: CGFloat(bar.height))
                                .offset(x: -1, y: CGFloat(bar.top))
                                .opacity(ScrollBar.strength(since: barSince, now: context.date))
                        }
                        .allowsHitTesting(false)
                    }
                }
                .onAppear {
                    showBar()
                    if openDetail == nil {
                        keepRowsInView(model, proxy: proxy)
                    }
                    reveal(openDetail, proxy: proxy, animated: false)
                }
                .onChange(of: openDetail) { _, opened in
                    showBar()
                    reveal(opened, proxy: proxy, animated: true)
                }
                .onChange(of: listOffset) { _, _ in
                    showBar()
                }
                .onChange(of: viewport) { _, height in
                    guard let opened = openDetail, let frame = rowFrames[opened],
                          MenuLogic.rowOutOfView(top: frame.minY + listOffset, bottom: frame.maxY + listOffset, viewport: height) else {
                        return
                    }
                    reveal(opened, proxy: proxy, animated: false)
                }
            }
        } else {
            content
        }
    }

    private func showBar() {
        guard maxHeight != nil, !still else { return }
        let now = Date()
        barSince = now
        barLive = true
        Task { @MainActor in
            try? await Task.sleep(for: .seconds(ScrollBar.shown + ScrollBar.fade + 0.05))
            if barSince == now {
                barLive = false
            }
        }
    }

    /// When the list scrolls, the row in use and the Most room row start in view, the lower one's bottom on the list's edge.
    private func keepRowsInView(_ model: MenuModel, proxy: ScrollViewProxy) {
        let keep = [model.activeUuid, model.rows?.first { $0.isBest == true }?.uuid].compactMap { $0 }
        guard !keep.isEmpty else { return }
        Task { @MainActor in
            await Task.yield()
            let tops = rowFrames.mapValues { Double($0.minY) }
            let bottoms = rowFrames.mapValues { Double($0.maxY) }
            let offset = MenuLogic.keepInView(tops: tops, bottoms: bottoms, keep: keep, viewport: Double(viewport), content: Double(listHeight))
            guard offset > 0.5 else { return }
            let lowest = keep.max { (bottoms[$0] ?? 0) < (bottoms[$1] ?? 0) }
            let fits = (bottoms[lowest ?? ""] ?? 0) - offset <= Double(viewport) + 0.5
            if fits, let lowest {
                proxy.scrollTo(lowest, anchor: .bottom)
            } else if let first = keep.first {
                proxy.scrollTo(first, anchor: .bottom)
            }
        }
    }

    private func reveal(_ uuid: String?, proxy: ScrollViewProxy, animated: Bool) {
        guard let uuid else { return }
        let rise = animated ? motion.rise : nil
        Task { @MainActor in
            for pause in [0, 160] {
                if pause > 0 {
                    try? await Task.sleep(for: .milliseconds(pause))
                } else {
                    await Task.yield()
                }
                if let rise {
                    withAnimation(rise) {
                        proxy.scrollTo(uuid, anchor: .top)
                    }
                } else {
                    proxy.scrollTo(uuid, anchor: .top)
                }
            }
        }
    }

    private func textFade(_ moving: (SwitchRun, SwitchFrame)?) -> TextFade {
        guard case let (_, frame)? = moving, !frame.showsOld, !frame.finished else { return .settled }
        return TextFade(outgoing: frame.textOut, incoming: frame.textIn, height: frame.textHeight)
    }

    private func footerOld(_ moving: (SwitchRun, SwitchFrame)?) -> MenuModel? {
        guard case let (made, frame)? = moving, !frame.showsOld, !frame.finished else { return nil }
        return made.old.model(for: side)
    }

    private func heroLook(_ model: MenuModel, _ moving: (SwitchRun, SwitchFrame)?) -> HeroLook {
        guard case let (made, frame)? = moving else { return .plain(model) }
        if frame.showsOld {
            var look = HeroLook.plain(model)
            look.headMuted = frame.headMuted
            look.lineOpacity = frame.heroLineOpacity
            return look
        }
        let old = made.old.model(for: side)
        let target = HeroLook.plain(model)
        let before = old.map { Fraction.of($0.weekW) } ?? target.week
        return HeroLook(
            old: old,
            fade: textFade(moving),
            headMuted: 1,
            used: target.used * frame.heroGrowth,
            ahead: target.ahead * frame.heroGrowth,
            week: before + (target.week - before) * frame.rowLines,
            lineOpacity: frame.heroLineOpacity
        )
    }

    private func oldRow(_ row: MenuRow, _ moving: (SwitchRun, SwitchFrame)?) -> MenuRow? {
        guard case let (made, frame)? = moving, !frame.showsOld, !frame.finished else { return nil }
        return made.old.model(for: side)?.rows?.first { $0.uuid == row.uuid }
    }

    private func rowLook(_ row: MenuRow, _ moving: (SwitchRun, SwitchFrame)?) -> RowLook {
        guard case let (made, frame)? = moving else {
            return PendingLook.look(RowLook.plain(row), row: row, pending: side == .mac ? pendingSwitch : nil)
        }
        var look = RowLook.plain(row)
        if frame.showsOld {
            let leaving = row.uuid == made.from
            look.background = leaving ? frame.leavingBackground : 0
            look.glow = leaving ? frame.leavingGlow : 0
            look.landing = 0
            return look
        }
        let arriving = row.uuid == made.to
        if let before = made.old.model(for: side)?.rows?.first(where: { $0.uuid == row.uuid }) {
            let start = RowLook.plain(before)
            look.five = start.five + (look.five - start.five) * frame.rowLines
            look.ahead = start.ahead + (look.ahead - start.ahead) * frame.rowLines
            look.week = start.week + (look.week - start.week) * frame.rowLines
        }
        look.background = arriving ? frame.arrivingBackground : 0
        look.glow = arriving ? frame.arrivingGlow : 0
        look.landing = arriving ? frame.landingGlow : 0
        return look
    }

    private func showingAccount(_ uuid: String) -> AccountSummary? {
        state?.accounts?.first { $0.uuid == uuid }
    }

    private func expanded(_ row: MenuRow, palette: Palette, moving: (SwitchRun, SwitchFrame)?) -> AnyView? {
        guard moving == nil, openDetail == row.uuid, let expansion,
              let account = state?.accounts?.first(where: { $0.uuid == row.uuid }) else {
            return nil
        }
        return AnyView(
            AccountExpansion(
                account: account,
                row: row,
                state: state,
                history: expansion.history(row.uuid),
                now: expansion.now,
                zone: expansion.zone,
                side: side,
                handlers: expansion.handlers,
                palette: palette,
                naming: expansion.naming,
                cancelRename: expansion.cancelRename
            )
            .id(row.uuid)
        )
    }

    private func rows(_ model: MenuModel, palette: Palette, moving: (SwitchRun, SwitchFrame)?) -> some View {
        VStack(alignment: .leading, spacing: Metrics.panelGap) {
            ForEach(model.rows ?? []) { row in
                let click = MenuLogic.rowClick(row, side: side, secondary: false, open: openDetail == row.uuid)
                let other = MenuLogic.rowClick(row, side: side, secondary: true)
                let arrow = MenuLogic.rowClick(row, side: side, secondary: false, open: openDetail == row.uuid, chevron: true)
                AccountRow(
                    row: row,
                    old: oldRow(row, moving),
                    fade: textFade(moving),
                    palette: palette,
                    look: rowLook(row, moving),
                    animated: moving == nil,
                    open: openDetail == row.uuid,
                    highlighted: moving == nil && highlight == row.uuid,
                    action: click == .nothing ? nil : { handlers.perform(click) },
                    secondary: { handlers.perform(other) },
                    expansion: expanded(row, palette: palette, moving: moving),
                    showsSecond: Detail.secondInRow(row, account: showingAccount(row.uuid), now: expansion?.now ?? Date()),
                    whereText: Detail.whereLine(row, open: openDetail == row.uuid),
                    chevron: { handlers.perform(arrow) }
                )
                .id(row.uuid)
            }
        }
        .animation(motion.rise, value: openDetail)
        .coordinateSpace(name: AccountRow.space)
        .backgroundPreferenceValue(RowFrames.self) { frames in
            if case let (made, frame)? = moving, let from = frames[made.from], let to = frames[made.to] {
                let y = Self.orbY(from: from, to: to, travel: frame.lightTravel)
                RoundedRectangle(cornerRadius: Metrics.orbHeight / 2, style: .continuous)
                    .fill(palette.orb)
                    .frame(width: Metrics.innerWidth - 2 * Metrics.orbInset, height: Metrics.orbHeight)
                    .blur(radius: Metrics.orbBlur)
                    .opacity(frame.lightOpacity)
                    .position(x: Metrics.innerWidth / 2, y: y)
                    .allowsHitTesting(false)
            }
        }
    }

    private func arrive(_ new: ServiceState?) {
        if run != nil {
            arriving = new
            return
        }
        guard let timeline = motion.plan.switching,
              let made = SwitchRun.make(old: shown, new: new, side: side, plan: motion.plan, start: Date()) else {
            shown = new
            return
        }
        run = made
        arriving = new
        Task { @MainActor in
            try? await Task.sleep(for: .seconds(timeline.total))
            guard run == made else { return }
            shown = arriving
            arriving = nil
            run = nil
        }
    }
}

struct PanelSurface: View {
    let palette: Palette

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: Metrics.panelRadius, style: .continuous)
        ZStack {
            ForEach(Array(palette.panelShadows.enumerated()), id: \.offset) { item in
                shape.fill(palette.panelBottom).cssShadow(item.element)
            }
            shape.fill(palette.panel)
        }
    }
}

struct PairControl: View {
    let side: MenuSide
    let palette: Palette
    let pick: (MenuSide) -> Void
    let computer: String
    var devboxDot = false
    @Environment(\.menuMotion) private var motion

    var body: some View {
        HStack(spacing: Metrics.pairInset) {
            choice("This Mac", .mac)
            choice(computer, .dev)
        }
        .padding(Metrics.pairInset)
        .background(RoundedRectangle(cornerRadius: Metrics.innerRadius, style: .continuous).fill(palette.pairTrack))
    }

    private func choice(_ title: String, _ which: MenuSide) -> some View {
        let selected = side == which
        return Button {
            pick(which)
        } label: {
            HStack(spacing: 6) {
                LineText(title, Styles.pair, selected ? palette.pairSelectedInk : palette.pairInk)
                if which == .dev, devboxDot {
                    Circle().fill(palette.low.solid).frame(width: 6, height: 6)
                        .accessibilityLabel("\(computer) needs you")
                }
            }
                .frame(maxWidth: .infinity)
                .frame(height: Metrics.pairHeight)
                .background(
                    RoundedRectangle(cornerRadius: Metrics.pairRadius, style: .continuous)
                        .fill(selected ? palette.pairSelected : Color.clear)
                        .cssShadow(selected ? palette.pairSelectedShadow : .none)
                        .animation(motion.pairFade, value: selected)
                )
                .contentShape(RoundedRectangle(cornerRadius: Metrics.pairRadius, style: .continuous))
        }
        .buttonStyle(FlatPress())
    }
}

struct Rise: ViewModifier {
    let enabled: Bool
    @Environment(\.menuMotion) private var motion
    @State private var shown = false

    func body(content: Content) -> some View {
        let still = motion.rise == nil || !enabled
        content
            .opacity(still || shown ? 1 : 0)
            .offset(y: still || shown ? 0 : 6)
            .onAppear {
                guard enabled, let rise = motion.rise else {
                    shown = true
                    return
                }
                withAnimation(rise) {
                    shown = true
                }
            }
    }
}

struct ActionButton: View {
    let title: String
    let style: ActionStyle
    let palette: Palette
    var fill: Double = 1
    var ink: Double = 1
    var armAfter: TimeInterval = 0
    var settledAt: Date? = nil
    let action: () -> Void
    @State private var shownAt = Date()
    @State private var armedNow = true

    var body: some View {
        let primary = style == .primary
        Button {
            if MenuLogic.armed(shownAt: shownAt, settledAt: settledAt, now: Date(), delay: armAfter) {
                action()
            }
        } label: {
            ButtonLabel(title: title, style: Styles.action, color: primary ? palette.primaryInk : palette.quietInk, width: Metrics.innerWidth, height: Metrics.actionHeight)
                .opacity(ink)
                .background(
                    RoundedRectangle(cornerRadius: Metrics.innerRadius, style: .continuous)
                        .fill(primary ? palette.primary : palette.quietBackground)
                        .cssShadow(primary ? palette.primaryShadow : .none)
                        .opacity(fill)
                )
                .contentShape(RoundedRectangle(cornerRadius: Metrics.innerRadius, style: .continuous))
        }
        .buttonStyle(FlatPress())
        .opacity(armedNow ? 1 : Metrics.busyAction)
        .onAppear {
            shownAt = Date()
            arm()
        }
        .onChange(of: settledAt) { _, _ in
            arm()
        }
    }

    private func arm() {
        let wait = MenuLogic.armWait(shownAt: shownAt, settledAt: settledAt, now: Date(), delay: armAfter)
        guard wait > 0 else {
            armedNow = true
            return
        }
        armedNow = false
        Task { @MainActor in
            try? await Task.sleep(for: .milliseconds(Int(wait * 1000) + 20))
            armedNow = MenuLogic.armed(shownAt: shownAt, settledAt: settledAt, now: Date(), delay: armAfter)
        }
    }
}

struct Footer: View {
    let model: MenuModel
    let old: MenuModel?
    let fade: TextFade
    let side: MenuSide
    let countdown: CountdownSummary?
    let countdownFraction: Double?
    let note: String?
    let palette: Palette
    let handlers: MenuHandlers
    var switching = false
    var animating = false
    var settledAt: Date? = nil
    var undoGone = false
    @Environment(\.menuMotion) private var motion

    var body: some View {
        let style = model.actionKind == "undo" && undoGone ? ActionStyle.none : MenuLogic.actionStyle(model, side: side)
        let command = MenuLogic.actionCommand(model, side: side)
        let oldStyle = MenuLogic.actionStyle(old, side: side)
        let fading = old != nil && fade != .settled
        let oldTitle = old?.actionText ?? ""
        let newTitle = model.actionText ?? ""
        let showOld = fading && oldStyle != .none && (oldTitle != newTitle || oldStyle != style)
        VStack(alignment: .leading, spacing: 0) {
            if MenuLogic.showsCountdown(model, side: side) {
                CountdownLine(countdown: countdown, fixed: countdownFraction, palette: palette)
                    .padding(.bottom, 6)
            }
            if style != .none || showOld {
                ZStack {
                    let cross = Self.cross(fade)
                    if showOld {
                        ActionButton(title: oldTitle, style: oldStyle, palette: palette, fill: cross.oldFill, ink: cross.oldInk) {}
                            .allowsHitTesting(false)
                    }
                    if style != .none {
                        ActionButton(
                            title: newTitle, style: style, palette: palette,
                            fill: showOld ? cross.newFill : 1, ink: showOld ? cross.newInk : 1, armAfter: 0,
                            settledAt: settledAt
                        ) {
                            if let command {
                                handlers.run(command, Via.footer(command))
                            } else if let add = MenuLogic.heroAdd(model, side: side) {
                                handlers.heroAdd?(add)
                            }
                        }
                        .opacity(switching ? Metrics.busyAction : 1)
                        .allowsHitTesting(MenuLogic.footerTakesClicks(switching: switching, animating: animating))
                        .modifier(Rise(enabled: old == nil))
                        .id("\(side.rawValue)|\(model.actionKind ?? "")|\(newTitle)")
                    }
                }
                .padding(.bottom, 6)
            }
            Swap(
                old?.hint, model.hint, Styles.small, palette.faint, fade: old == nil ? .settled : fade,
                width: Metrics.innerWidth, alignment: .top, centered: true, gap: 6
            )
            if let note, !note.isEmpty {
                Block(note, Styles.small, palette.muted, width: Metrics.innerWidth, centered: true)
                    .padding(.bottom, 6)
            }
            FooterButtons(palette: palette, handlers: handlers)
        }
    }
}

extension Footer {
    static func cross(_ fade: TextFade) -> (oldFill: Double, oldInk: Double, newFill: Double, newInk: Double) {
        let out = min(max(fade.outgoing, 0), 1)
        if out > 0 {
            return (1, out, 0, 0)
        }
        return (0, 0, 1, min(max(fade.incoming, 0), 1))
    }
}

enum PendingLook {
    static func model(_ model: MenuModel, state: ServiceState, pending: String?, side: MenuSide) -> MenuModel {
        guard let target = pending, side == .mac else { return model }
        var shown = model
        let head = target == MenuLogic.undoTarget ? model.pendingBack : model.rows?.first { $0.uuid == target }?.pendingHead
        if let head, !head.isEmpty {
            shown.head = head
        }
        shown.sub = ""
        return shown
    }

    static func look(_ look: RowLook, row: MenuRow, pending: String?) -> RowLook {
        guard let pending, pending != MenuLogic.undoTarget else { return look }
        var look = look
        let target = row.uuid == pending
        look.background = target ? 1 : 0
        look.glow = target ? 1 : 0
        return look
    }
}

struct FooterButtons: View {
    let palette: Palette
    let handlers: MenuHandlers

    var body: some View {
        HStack(spacing: Metrics.panelGap) {
            FooterButton(title: "Add an account", icon: .plus, palette: palette, action: handlers.addAccount)
            FooterButton(title: "Alerts and settings", icon: .bell, palette: palette, action: handlers.settings)
        }
    }
}

enum FooterIcon {
    case plus
    case bell
}

struct FooterButton: View {
    let title: String
    let icon: FooterIcon
    let palette: Palette
    let action: (() -> Void)?

    var body: some View {
        Button {
            action?()
        } label: {
            HStack(spacing: 8) {
                Glyph(icon: icon)
                    .stroke(palette.muted, style: StrokeStyle(lineWidth: 1.75 * 16 / 24, lineCap: .round, lineJoin: .round))
                    .frame(width: 16, height: 16)
                LineText(title, Styles.footer, palette.text)
            }
            .frame(maxWidth: .infinity)
            .frame(height: Metrics.footerHeight)
            .contentShape(RoundedRectangle(cornerRadius: Metrics.innerRadius, style: .continuous))
        }
        .buttonStyle(FlatPress())
    }
}

struct Glyph: Shape {
    let icon: FooterIcon

    func path(in rect: CGRect) -> Path {
        var path = Path()
        switch icon {
        case .plus:
            path.move(to: CGPoint(x: 12, y: 5))
            path.addLine(to: CGPoint(x: 12, y: 19))
            path.move(to: CGPoint(x: 5, y: 12))
            path.addLine(to: CGPoint(x: 19, y: 12))
        case .bell:
            path.move(to: CGPoint(x: 6, y: 8))
            path.addArc(center: CGPoint(x: 12, y: 8), radius: 6, startAngle: .degrees(180), endAngle: .degrees(360), clockwise: false)
            path.addCurve(to: CGPoint(x: 21, y: 17), control1: CGPoint(x: 18, y: 15), control2: CGPoint(x: 21, y: 17))
            path.addLine(to: CGPoint(x: 3, y: 17))
            path.addCurve(to: CGPoint(x: 6, y: 8), control1: CGPoint(x: 3, y: 17), control2: CGPoint(x: 6, y: 15))
            let rise = (1.94 * 1.94 - 1.7 * 1.7).squareRoot()
            let center = CGPoint(x: 12, y: 21 - rise)
            let start = atan2(21 - center.y, 10.3 - center.x)
            let end = atan2(21 - center.y, 13.7 - center.x)
            path.move(to: CGPoint(x: 10.3, y: 21))
            path.addArc(center: center, radius: 1.94, startAngle: .radians(start), endAngle: .radians(end), clockwise: true)
        }
        let scale = min(rect.width, rect.height) / 24
        return path.applying(CGAffineTransform(scaleX: scale, y: scale).translatedBy(x: rect.minX / scale, y: rect.minY / scale))
    }
}
