import LifelineKit
import SwiftUI

public struct AddHandlers {
    public var start: (AddKind) -> Void
    public var choose: (AddTarget) -> Void
    public var reopen: (String) -> Void
    public var finish: (String, [String: String]) async -> String?
    public var close: () -> Void
    public var pick: (KnownAccount?) -> Void
    public var back: (() -> Void)?
    public var cancel: (String) -> Void
    public var retry: ((AddKind) -> Void)?

    public init(
        start: @escaping (AddKind) -> Void = { _ in },
        choose: @escaping (AddTarget) -> Void = { _ in },
        reopen: @escaping (String) -> Void = { _ in },
        finish: @escaping (String, [String: String]) async -> String? = { _, _ in nil },
        close: @escaping () -> Void = {},
        pick: @escaping (KnownAccount?) -> Void = { _ in },
        back: (() -> Void)? = nil,
        cancel: @escaping (String) -> Void = { _ in },
        retry: ((AddKind) -> Void)? = nil
    ) {
        self.cancel = cancel
        self.retry = retry
        self.back = back
        self.start = start
        self.choose = choose
        self.reopen = reopen
        self.finish = finish
        self.close = close
        self.pick = pick
    }
}

public struct AddAccountView: View {
    let state: ServiceState?
    let step: AddStep
    let run: AddRun?
    let target: AddTarget
    let page: URL?
    let problem: String?
    let zone: TimeZone
    let handlers: AddHandlers
    let width: CGFloat
    let pick: KnownAccount?
    let note: String?
    let settling: Set<String>
    @Environment(\.colorScheme) private var scheme

    public init(
        state: ServiceState?, step: AddStep, run: AddRun? = nil, target: AddTarget = .mac, page: URL? = nil, problem: String?,
        zone: TimeZone, handlers: AddHandlers = AddHandlers(), width: CGFloat = Metrics.panelWidth, pick: KnownAccount? = nil, note: String? = nil,
        settling: Set<String> = []
    ) {
        self.settling = settling
        self.pick = pick
        self.note = note
        self.state = state
        self.step = step
        self.run = run
        self.target = target
        self.page = page
        self.problem = problem
        self.zone = zone
        self.handlers = handlers
        self.width = width
    }

    private var content: CGFloat {
        width - 2 * Metrics.sheetPadding
    }

    public var body: some View {
        screen.keepsNames(of: state)
    }

    @ViewBuilder
    private var screen: some View {
        let palette = Palette.of(scheme)
        let sheet = SheetPalette.of(scheme)
        Group {
            switch step {
            case .added(let uuid):
                if let account = state?.accounts?.first(where: { $0.uuid == uuid }) {
                    AddedStep(
                        account: account, job: AddFlow.addJob(for: uuid, run: run, state: state), barHidden: state?.settings?.hideName ?? true, run: run,
                        problem: problem, zone: zone, handlers: handlers, palette: palette, sheet: sheet, width: content, state: state
                    )
                        .id(uuid)
                } else {
                    signIn(palette: palette, sheet: sheet)
                }
            case .done(let sentence):
                DoneStep(sentence: sentence, handlers: handlers, palette: palette, sheet: sheet, width: content)
            default:
                signIn(palette: palette, sheet: sheet)
            }
        }
        .padding(Metrics.sheetPadding)
        .frame(width: width, alignment: .topLeading)
    }

    private func signIn(palette: Palette, sheet: SheetPalette) -> some View {
        let width = content
        let retry = AddFlow.retry(step: step, run: run, state: state, ignoring: settling)
        let waiting = run?.waiting == true ? run?.current : nil
        let adopt = AddFlow.adoptTitle(AddFlow.stages(target: target, kind: .adopt, state: state), state: state)
        let failure: String? = {
            if case .failed(let sentence) = step { return AddFlow.failure(sentence, state: state) }
            return problem
        }()
        let known = state?.knownAccounts ?? []
        let chosen = run?.pick ?? pick
        let addBusy = waiting == nil ? AddFlow.addBusy(state, run: run, ignoring: settling) : nil
        let devboxBusy = waiting == nil ? AddFlow.devboxBusy(state, run: run, ignoring: settling) : nil
        let addable = waiting != nil || AddFlow.addPossible(target, pick: chosen, state: state, run: run, ignoring: settling)
        return VStack(alignment: .leading, spacing: 20) {
            if let back = handlers.back {
                BackButton(palette: palette, action: back)
            }
            if let failure, !failure.isEmpty {
                let parts = AddFlow.headline(failure, state: state)
                VStack(alignment: .leading, spacing: 6) {
                    LineText(AddFlow.sheetTitle(state), Styles.label, palette.faint)
                    Paragraph(parts.head, Styles.sheetTitle, palette.ink, width: width)
                    if !parts.rest.isEmpty {
                        Paragraph(parts.rest, Styles.sheetLead, palette.text, width: width)
                    }
                }
            } else {
                VStack(alignment: .leading, spacing: 6) {
                    LineText(AddFlow.sheetTitle(state), Styles.sheetTitle, palette.ink)
                    Paragraph(AddFlow.lead(state), Styles.sheetLead, palette.text, width: width)
                    if let note, !note.isEmpty {
                        Paragraph(note, Styles.small, palette.muted, width: width)
                            .padding(.top, 4)
                    }
                }
            }
            if !known.isEmpty, waiting == nil {
                VStack(alignment: .leading, spacing: 8) {
                    LineText(AddFlow.whichAccount, Styles.label, palette.faint)
                    VStack(spacing: 4) {
                        ForEach(known) { account in
                            pickRow(account.name, AddFlow.pickLine(account), selected: chosen?.name == account.name, palette: palette, sheet: sheet, width: width) {
                                handlers.pick(account)
                            }
                        }
                        pickRow(AddFlow.anotherAccount, nil, selected: chosen == nil, palette: palette, sheet: sheet, width: width) {
                            handlers.pick(nil)
                        }
                    }
                }
            }
            if let computer = state?.devbox?.name {
            VStack(alignment: .leading, spacing: 8) {
                LineText("Add it to", Styles.label, palette.faint)
                HStack(spacing: Metrics.pairInset) {
                    choice("This Mac", .mac, locked: waiting != nil, palette: palette)
                    choice(computer, .devbox, locked: waiting != nil, palette: palette)
                    choice("Both", .both, locked: waiting != nil, palette: palette)
                }
                .padding(Metrics.pairInset)
                .background(RoundedRectangle(cornerRadius: Metrics.innerRadius, style: .continuous).fill(palette.pairTrack))
                if !JobFlow.devboxReady(state) {
                    Paragraph(JobFlow.devboxAway(state), Styles.small, palette.muted, width: width)
                } else if let devboxBusy {
                    busyCard(devboxBusy, palette: palette, sheet: sheet, width: width)
                } else if addable, let devboxNote = AddFlow.devboxNote(target: target, pick: chosen, state: state) {
                    Paragraph(devboxNote, Styles.small, palette.muted, width: width)
                }
            }
            }
            Paragraph(AddFlow.explain(state, pick: chosen), Styles.explain, palette.muted, width: width)
            if retry == nil, waiting?.opensBrowser != false {
                VStack(spacing: 8) {
                    if let addBusy {
                        busyCard(addBusy, palette: palette, sheet: sheet, width: width)
                    } else if addable {
                        SheetButton(title: "Open the sign-in page", look: .primary, width: width, height: Metrics.actionHeight, palette: palette, sheet: sheet, style: Styles.action) {
                            if waiting == nil {
                                handlers.start(.add)
                            } else if let job = run?.job {
                                handlers.reopen(job)
                            }
                        }
                    }
                    if let adopt, waiting == nil {
                        SheetButton(title: adopt, look: .quiet, width: width, height: Metrics.actionHeight, palette: palette, sheet: sheet, style: Styles.action) {
                            handlers.start(.adopt)
                        }
                    }
                }
            }
            if let waiting {
                WaitingBox(
                    sentences: AddFlow.waitingLines(waiting, state: state, job: run?.job.flatMap { id in state?.jobs?.first { $0.id == id } }, run: run),
                    palette: palette, sheet: sheet, width: width
                )
                if page != nil {
                    Block(AddFlow.pageNotOpenedAbove(state), Styles.small, palette.muted, width: width, centered: true)
                }
            }
            if let retry {
                SheetButton(title: AddFlow.tryAgain, look: .primary, width: width, height: Metrics.actionHeight, palette: palette, sheet: sheet, style: Styles.action) {
                    (handlers.retry ?? handlers.start)(retry)
                }
            }
        }
    }

    private func busyCard(_ job: JobSummary, palette: Palette, sheet: SheetPalette, width: CGFloat) -> some View {
        let ready = JobFlow.page(job) != nil
        return VStack(alignment: .leading, spacing: 8) {
            Paragraph(JobFlow.unbroken(JobFlow.cardSentence(job)), Styles.small, palette.text, width: width - 28)
            if JobFlow.hasActions(job) || JobFlow.cancellable(job) {
                HStack(spacing: 18) {
                    if JobFlow.hasActions(job) {
                        TextAction(title: JobFlow.openLabel(job), palette: palette, action: ready ? { handlers.reopen(job.id) } : nil)
                    }
                    if JobFlow.cancellable(job) {
                        TextAction(title: JobFlow.cancel, palette: palette) {
                            handlers.cancel(job.id)
                        }
                    }
                }
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .frame(width: width, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: Metrics.innerRadius, style: .continuous).fill(sheet.quiet))
    }

    /// The picked account takes the tag fill and a small check at the right; the others stay quiet.
    nonisolated static func pickLook(selected: Bool, palette: Palette, sheet: SheetPalette) -> (fill: Color, check: Bool) {
        selected ? (palette.tagBackground, true) : (sheet.quiet, false)
    }

    private func pickRow(
        _ title: String, _ line: String?, selected: Bool, palette: Palette, sheet: SheetPalette, width: CGFloat, action: @escaping () -> Void
    ) -> some View {
        let look = Self.pickLook(selected: selected, palette: palette, sheet: sheet)
        let check: CGFloat = 14
        return Button(action: action) {
            HStack(alignment: .center, spacing: 10) {
                VStack(alignment: .leading, spacing: 2) {
                    LineText(title, Styles.rowTitle, palette.ink)
                    if let line {
                        Paragraph(line, Styles.small, palette.muted, width: width - 28 - check - 10)
                    }
                }
                Spacer(minLength: 0)
                Image(systemName: "checkmark")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(sheet.quietInk)
                    .frame(width: check, height: check)
                    .opacity(look.check ? 1 : 0)
                    .accessibilityHidden(true)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .frame(width: width, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: Metrics.innerRadius, style: .continuous).fill(look.fill))
            .contentShape(RoundedRectangle(cornerRadius: Metrics.innerRadius, style: .continuous))
        }
        .buttonStyle(FlatPress())
        .accessibilityValue(selected ? "Selected" : "")
    }

    private func choice(_ title: String, _ value: AddTarget, locked: Bool, palette: Palette) -> some View {
        let selected = target == value
        let usable = AddFlow.usable(value, state: state, run: run, ignoring: settling)
        return Button {
            if !locked, usable {
                handlers.choose(value)
            }
        } label: {
            LineText(title, Styles.pair, selected ? palette.pairSelectedInk : (usable ? palette.pairInk : palette.faint))
                .opacity(usable ? 1 : Metrics.unavailable)
                .frame(maxWidth: .infinity)
                .frame(height: 32)
                .background(
                    RoundedRectangle(cornerRadius: Metrics.pairRadius, style: .continuous)
                        .fill(selected ? palette.pairSelected : Color.clear)
                        .cssShadow(selected ? palette.pairSelectedShadow : .none)
                )
                .contentShape(RoundedRectangle(cornerRadius: Metrics.pairRadius, style: .continuous))
        }
        .buttonStyle(FlatPress())
        .disabled(!usable)
        .accessibilityValue(selected ? "Selected" : (usable ? "" : "Unavailable"))
    }
}

struct DoneStep: View {
    let sentence: String
    let handlers: AddHandlers
    let palette: Palette
    let sheet: SheetPalette
    let width: CGFloat

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            if let back = handlers.back {
                BackButton(palette: palette, action: back)
            }
            CheckTile(sheet: sheet)
            Paragraph(sentence, Styles.sheetLead, palette.text, width: width)
            SheetButton(title: "Done", look: .primary, width: width, height: Metrics.actionHeight, palette: palette, sheet: sheet, style: Styles.action) {
                handlers.close()
            }
        }
    }
}

struct CheckTile: View {
    let sheet: SheetPalette

    var body: some View {
        RoundedRectangle(cornerRadius: 14, style: .continuous)
            .fill(sheet.raised)
            .cssShadow(sheet.raisedShadow)
            .frame(width: 44, height: 44)
            .overlay(
                Check()
                    .stroke(sheet.check, style: StrokeStyle(lineWidth: 2 * 22 / 24, lineCap: .round, lineJoin: .round))
                    .frame(width: 22, height: 22)
            )
    }
}

struct AddedStep: View {
    let account: AccountSummary
    let job: JobSummary?
    let state: ServiceState?
    let headline: String
    let barHidden: Bool
    let run: AddRun?
    let given: String?
    let zone: TimeZone
    let handlers: AddHandlers
    let palette: Palette
    let sheet: SheetPalette
    let width: CGFloat
    @State private var name: String
    @State private var short: String
    @State private var problem: String?
    @State private var saving = false

    init(
        account: AccountSummary, job: JobSummary?, barHidden: Bool, run: AddRun?, problem given: String?, zone: TimeZone, handlers: AddHandlers,
        palette: Palette, sheet: SheetPalette, width: CGFloat, state: ServiceState? = nil
    ) {
        self.account = account
        self.job = job
        self.state = state
        self.barHidden = barHidden
        self.run = run
        self.given = given
        self.zone = zone
        self.handlers = handlers
        self.palette = palette
        self.sheet = sheet
        self.width = width
        let names = AddFlow.suggestedNames(account, job: job)
        headline = names.name
        _name = State(initialValue: names.name)
        _short = State(initialValue: names.short)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            if let back = handlers.back {
                BackButton(palette: palette, action: back)
            }
            CheckTile(sheet: sheet)
            VStack(alignment: .leading, spacing: 6) {
                Paragraph(AddFlow.added(name: headline, account, state: state, job: job), Styles.sheetTitle, palette.ink, width: width)
                Paragraph(AddFlow.addedLine(account, run: run), Styles.sheetLead, palette.text, width: width)
            }
            NameFields(
                name: $name, short: $short, menuName: account.name ?? "", storedShort: account.short ?? "", barHidden: barHidden, text: state?.addText, palette: palette,
                sheet: sheet, width: width
            )
            SheetButton(title: "Save", look: .primary, width: width, height: Metrics.actionHeight, palette: palette, sheet: sheet, style: Styles.action) {
                done()
            }
            .disabled(saving)
            if let shown = problem ?? given, !shown.isEmpty {
                Block(shown, Styles.small, palette.muted, width: width, centered: true)
            }
        }
    }

    private func done() {
        let body = AddFlow.renameBody(oldName: account.name ?? "", oldShort: account.short ?? "", name: name, short: short)
        if body.isEmpty {
            handlers.close()
            return
        }
        saving = true
        let finish = handlers.finish
        let uuid = account.uuid
        let close = handlers.close
        Task { @MainActor in
            let refusal = await finish(uuid, body)
            saving = false
            problem = refusal
            if refusal == nil {
                close()
            }
        }
    }
}

struct Check: Shape {
    func path(in rect: CGRect) -> Path {
        var path = Path()
        path.move(to: CGPoint(x: 20, y: 6))
        path.addLine(to: CGPoint(x: 9, y: 17))
        path.addLine(to: CGPoint(x: 4, y: 12))
        let scale = min(rect.width, rect.height) / 24
        return path.applying(CGAffineTransform(scaleX: scale, y: scale).translatedBy(x: rect.minX / scale, y: rect.minY / scale))
    }
}
