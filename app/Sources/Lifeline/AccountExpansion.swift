import LifelineKit
import SwiftUI

public struct ExpansionInputs {
    public var history: (String) -> HistoryLoad
    public var now: Date
    public var zone: TimeZone
    public var handlers: DetailHandlers
    public var naming: Bool
    public var cancelRename: Int

    public init(
        history: @escaping (String) -> HistoryLoad = { _ in .waiting }, now: Date = Date(), zone: TimeZone = .current,
        handlers: DetailHandlers = DetailHandlers(), naming: Bool = false, cancelRename: Int = 0
    ) {
        self.history = history
        self.now = now
        self.zone = zone
        self.handlers = handlers
        self.naming = naming
        self.cancelRename = cancelRename
    }
}

struct AccountExpansion: View {
    let account: AccountSummary
    let row: MenuRow
    let state: ServiceState?
    let history: HistoryLoad
    let now: Date
    let zone: TimeZone
    let side: MenuSide
    let handlers: DetailHandlers
    let palette: Palette
    let naming: Bool
    let cancelRename: Int
    @Environment(\.colorScheme) private var scheme
    @State private var editing: Bool
    @State private var name: String
    @State private var short: String
    @State private var problem: String?
    @State private var saving = false
    @State private var confirming: Bool

    init(
        account: AccountSummary, row: MenuRow, state: ServiceState?, history: HistoryLoad, now: Date, zone: TimeZone,
        side: MenuSide, handlers: DetailHandlers, palette: Palette, naming: Bool = false, cancelRename: Int = 0, confirming: Bool = false
    ) {
        self.account = account
        self.row = row
        self.state = state
        self.history = history
        self.now = now
        self.zone = zone
        self.side = side
        self.handlers = handlers
        self.palette = palette
        self.naming = naming
        self.cancelRename = cancelRename
        _editing = State(initialValue: naming)
        _confirming = State(initialValue: confirming)
        _name = State(initialValue: account.name ?? "")
        _short = State(initialValue: account.short ?? "")
    }

    var body: some View {
        Group {
            if editing {
                renaming
            } else {
                reading
            }
        }
        .frame(width: Metrics.contentWidth, alignment: .leading)
        .onChange(of: cancelRename) { _, _ in
            cancel()
        }
    }

    private var renaming: some View {
        let width = Metrics.contentWidth
        return VStack(alignment: .leading, spacing: 6) {
            NameFields(
                name: $name, short: $short, menuName: account.name ?? "", storedShort: account.short ?? "", barHidden: state?.settings?.hideName ?? true,
                text: state?.addText, palette: palette,
                sheet: SheetPalette.of(scheme), width: width
            )
            HStack(spacing: 18) {
                TextAction(title: "Save", palette: palette, strong: true, action: saving ? nil : { save() })
                TextAction(title: "Cancel", palette: palette) {
                    cancel()
                }
            }
            .padding(.top, 8)
            if let problem, !problem.isEmpty {
                Paragraph(problem, Styles.small, palette.muted, width: width)
            }
        }
    }

    private func cancel() {
        editing = false
        problem = nil
    }

    private var reading: some View {
        let width = Metrics.contentWidth
        let heroShown = account.uuid == state?.model(for: side)?.activeUuid
        let text = Detail.openRow(account, reply: reply, heroShown: heroShown)
        let window = WindowChart.plot(readings: reply?.readings ?? [], account: account, now: now, zone: zone)
        return VStack(alignment: .leading, spacing: 6) {
            ForEach(Array(text.lines.prefix(text.chartAfter).enumerated()), id: \.offset) { item in
                Paragraph(item.element, Styles.small, palette.text, width: width)
            }
            if let window {
                WindowChartView(plot: window, tone: palette.tone(row.tone), palette: palette, width: width)
                    .padding(.vertical, 4)
            }
            if text.sparkline, let reply {
                Sparkline(
                    plot: DetailChart.week(daily: reply.daily ?? [], now: now, zone: zone), line: SheetPalette.of(scheme).weekLine, limit: palette.limit.solid,
                    width: width, dayInk: palette.faint, dayLit: palette.limit.ink
                )
                    .padding(.vertical, 4)
            }
            ForEach(Array(text.lines.dropFirst(text.chartAfter).enumerated()), id: \.offset) { item in
                Paragraph(item.element, Styles.small, palette.muted, width: width)
            }
            if let job = state?.jobs?.last(where: { JobFlow.resignKinds.contains($0.kind ?? "") && $0.uuid == account.uuid && JobFlow.isRunning($0) }) {
                Paragraph(JobFlow.waiting(job), Styles.small, palette.muted, width: width)
            }
            actions
                .padding(.top, 4)
            if let sentence = account.removeText, !sentence.isEmpty {
                if confirming {
                    ConfirmCard(sentence: sentence, confirm: "Remove", palette: palette, width: width, action: saving ? nil : { remove() }) {
                        confirming = false
                        problem = nil
                    }
                    .padding(.top, 6)
                } else {
                    TextAction(title: "Remove from Lifeline", palette: palette) {
                        problem = nil
                        confirming = true
                    }
                    .padding(.top, 6)
                }
            }
            if let problem, !problem.isEmpty {
                Paragraph(problem, Styles.small, palette.muted, width: width)
            }
        }
    }

    private var reply: HistoryReply? {
        if case .ready(let reply) = history {
            return reply
        }
        return nil
    }

    private var actions: some View {
        HStack(spacing: 18) {
            if case .useHere(let uuid) = Detail.button(account, macActive: state?.mac?.activeUuid, side: side) {
                TextAction(title: "Use on this Mac", palette: palette) {
                    handlers.use(uuid)
                }
            }
            TextAction(title: side == .dev ? "Sign in again on this Mac" : "Sign in again", palette: palette) {
                problem = nil
                handlers.resign(account.uuid)
            }
            TextAction(title: "Rename", palette: palette) {
                name = account.name ?? ""
                short = account.short ?? ""
                problem = nil
                editing = true
            }
        }
    }

    private func remove() {
        saving = true
        let uuid = account.uuid
        let remove = handlers.remove
        Task { @MainActor in
            problem = await remove(uuid)
            saving = false
            confirming = problem != nil
        }
    }

    private func save() {
        let uuid = account.uuid
        let body = AddFlow.renameBody(oldName: account.name ?? "", oldShort: account.short ?? "", name: name, short: short)
        if body.isEmpty {
            editing = false
            return
        }
        saving = true
        let rename = handlers.rename
        Task { @MainActor in
            let refusal = await rename(uuid, body)
            saving = false
            problem = refusal
            if refusal == nil {
                editing = false
            }
        }
    }
}

/// A confirm card: the core's one sentence, then the button that makes the change and Cancel.
struct ConfirmCard: View {
    let sentence: String
    let confirm: String
    let palette: Palette
    let width: CGFloat
    let action: (() -> Void)?
    let cancel: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Paragraph(sentence, Styles.small, palette.text, width: width)
            HStack(spacing: 18) {
                TextAction(title: confirm, palette: palette, strong: true, action: action)
                TextAction(title: "Cancel", palette: palette, action: cancel)
            }
        }
    }
}

struct TextAction: View {
    let title: String
    let palette: Palette
    var strong = false
    let action: (() -> Void)?

    var body: some View {
        Button {
            action?()
        } label: {
            LineText(title, Styles.smallAction, action == nil ? palette.faint : (strong ? palette.ink : palette.quietInk))
                .contentShape(Rectangle())
        }
        .buttonStyle(FlatPress())
        .disabled(action == nil)
    }
}
