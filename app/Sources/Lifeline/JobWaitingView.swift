import LifelineKit
import SwiftUI

public struct JobWaitingView: View {
    let job: JobSummary
    let state: ServiceState?
    let width: CGFloat
    let cancelling: Bool
    let reopen: (String) -> Void
    let cancel: (String) -> Void
    let back: (() -> Void)?
    @Environment(\.colorScheme) private var scheme

    public init(
        job: JobSummary, state: ServiceState?, width: CGFloat = Metrics.panelWidth, cancelling: Bool = false, reopen: @escaping (String) -> Void = { _ in },
        cancel: @escaping (String) -> Void = { _ in }, back: (() -> Void)? = nil
    ) {
        self.back = back
        self.job = job
        self.state = state
        self.width = width
        self.cancelling = cancelling
        self.reopen = reopen
        self.cancel = cancel
    }

    public var body: some View {
        content.keepsNames(of: state)
    }

    @ViewBuilder
    private var content: some View {
        let palette = Palette.of(scheme)
        let sheet = SheetPalette.of(scheme)
        let inner = width - 2 * Metrics.sheetPadding
        let ready = JobFlow.page(job) != nil
        VStack(alignment: .leading, spacing: 20) {
            if let back {
                BackButton(palette: palette, action: back)
            }
            VStack(alignment: .leading, spacing: 6) {
                LineText(JobFlow.label(job, state: state), Styles.label, palette.faint)
                Paragraph(JobFlow.unbroken(cancelling ? JobFlow.cancelling(state) : JobFlow.waiting(job)), Styles.sheetTitle, palette.ink, width: inner)
            }
            WaitingBox(sentences: JobFlow.guidance(job, state: state), palette: palette, sheet: sheet, width: inner)
            if !cancelling {
                VStack(spacing: 8) {
                    SheetButton(
                        title: JobFlow.openLabel(job), look: ready ? .primary : .disabled, width: inner, height: Metrics.actionHeight,
                        palette: palette, sheet: sheet, style: Styles.action, action: ready ? { reopen(job.id) } : nil
                    )
                    if JobFlow.cancellable(job) {
                        SheetButton(title: JobFlow.cancel, look: .quiet, width: inner, height: Metrics.actionHeight, palette: palette, sheet: sheet, style: Styles.action) {
                            cancel(job.id)
                        }
                    }
                }
            }
        }
        .padding(Metrics.sheetPadding)
        .frame(width: width, alignment: .topLeading)
    }
}

struct AlertCard: View {
    let notice: NoticeSummary
    let palette: Palette
    var next: (() -> Void)? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            LineText(JobFlow.alertLabel, Styles.label, palette.faint)
            Paragraph(notice.title ?? "", Styles.sub, palette.text, width: Metrics.contentWidth)
            if let body = notice.body, !body.isEmpty {
                Paragraph(body, Styles.small, palette.muted, width: Metrics.contentWidth)
            }
            if let next {
                TextAction(title: JobFlow.nextResult, palette: palette, action: next)
                    .padding(.top, 8)
            }
        }
        .padding(.vertical, 12)
        .padding(.horizontal, Metrics.sideInset)
        .frame(width: Metrics.innerWidth, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: Metrics.innerRadius, style: .continuous).fill(palette.rowActive.opacity(palette.cardFill)))
    }
}

public struct CardActions {
    public var open: (String) -> Void
    public var cancel: (String) -> Void
    public var retry: (JobSummary) -> Void
    public var show: (String) -> Void

    public init(
        open: @escaping (String) -> Void = { _ in }, cancel: @escaping (String) -> Void = { _ in }, retry: @escaping (JobSummary) -> Void = { _ in },
        show: @escaping (String) -> Void = { _ in }
    ) {
        self.open = open
        self.cancel = cancel
        self.retry = retry
        self.show = show
    }
}

public struct CompactWaiting: View {
    let job: JobSummary
    let state: ServiceState?
    var reopen: (String) -> Void = { _ in }
    var cancel: (String) -> Void = { _ in }
    var back: () -> Void = {}
    var problem: String? = nil
    var cancelling = false
    @Environment(\.colorScheme) private var scheme
    @Environment(\.sharedSurface) private var shared

    public var body: some View {
        content.keepsNames(of: state)
    }

    @ViewBuilder
    private var content: some View {
        let palette = Palette.of(scheme)
        let inner = Metrics.panelWidth - 2 * Metrics.sheetPadding
        let ready = JobFlow.page(job) != nil
        VStack(alignment: .leading, spacing: 10) {
            BackButton(palette: palette, action: back)
            Paragraph(JobFlow.unbroken(cancelling ? JobFlow.cancelling(state) : JobFlow.cardSentence(job)), Styles.sub, palette.ink, width: inner)
            if !cancelling {
                TravellingLight(width: inner, palette: palette, sheet: SheetPalette.of(scheme))
                    .padding(.vertical, 2)
            }
            if !cancelling, !job.id.isEmpty, JobFlow.hasActions(job) || JobFlow.cancellable(job) {
                HStack(spacing: 18) {
                    if JobFlow.hasActions(job) {
                        TextAction(title: JobFlow.openLabel(job), palette: palette, strong: true, action: ready ? { reopen(job.id) } : nil)
                    }
                    if JobFlow.cancellable(job) {
                        TextAction(title: JobFlow.cancel, palette: palette) {
                            cancel(job.id)
                        }
                    }
                }
                .padding(.top, 2)
            }
            if let problem, !problem.isEmpty {
                Paragraph(problem, Styles.small, palette.muted, width: inner)
            }
        }
        .padding(.horizontal, Metrics.sheetPadding)
        .padding(.vertical, 18)
        .frame(width: Metrics.panelWidth, alignment: .topLeading)
        .background {
            if !shared {
                PanelSurface(palette: palette)
            }
        }
    }
}

extension CompactWaiting {
    public init(card job: JobSummary, state: ServiceState?) {
        self.init(job: job, state: state, problem: nil)
    }
}

struct BackButton: View {
    let palette: Palette
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 6) {
                Image(systemName: "chevron.left")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(palette.quietInk)
                LineText(JobFlow.back, Styles.smallAction, palette.quietInk)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(FlatPress())
        .accessibilityLabel(JobFlow.back)
    }
}

struct WaitingCard: View {
    let job: JobSummary
    let palette: Palette
    let actions: CardActions
    var state: ServiceState? = nil
    var cancelling = false
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        let ready = JobFlow.page(job) != nil
        VStack(alignment: .leading, spacing: 4) {
            Paragraph(JobFlow.unbroken(cancelling ? JobFlow.cancelling(state) : JobFlow.cardSentence(job)), Styles.sub, palette.text, width: Metrics.contentWidth)
            if !cancelling {
                TravellingLight(width: Metrics.contentWidth, palette: palette, sheet: SheetPalette.of(scheme))
                    .padding(.vertical, 6)
            }
            if !cancelling, JobFlow.hasActions(job) || JobFlow.cancellable(job) {
                HStack(spacing: 18) {
                    if JobFlow.hasActions(job) {
                        TextAction(title: JobFlow.openLabel(job), palette: palette, action: ready ? { actions.open(job.id) } : nil)
                    }
                    if JobFlow.cancellable(job) {
                        TextAction(title: JobFlow.cancel, palette: palette) {
                            actions.cancel(job.id)
                        }
                    }
                }
                .padding(.top, 8)
            }
        }
        .padding(.vertical, 12)
        .padding(.horizontal, Metrics.sideInset)
        .frame(width: Metrics.innerWidth, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: Metrics.innerRadius, style: .continuous).fill(palette.rowActive.opacity(palette.cardFill)))
        .contentShape(RoundedRectangle(cornerRadius: Metrics.innerRadius, style: .continuous))
        .onTapGesture {
            actions.show(job.id)
        }
    }
}

struct ResultCard: View {
    let job: JobSummary
    let palette: Palette
    var actions = CardActions()
    var state: ServiceState? = nil
    var next: (() -> Void)? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Paragraph(JobFlow.resultSentence(job, state: state), Styles.sub, palette.text, width: Metrics.contentWidth)
            let kind = JobFlow.retryKind(job, state: state)
            if kind != nil || next != nil {
                HStack(spacing: 18) {
                    if kind != nil {
                        TextAction(title: AddFlow.tryAgain, palette: palette) {
                            actions.retry(job)
                        }
                    }
                    if let next {
                        TextAction(title: JobFlow.nextResult, palette: palette, action: next)
                    }
                }
                .padding(.top, 8)
            }
        }
        .padding(.vertical, 12)
        .padding(.horizontal, Metrics.sideInset)
        .frame(width: Metrics.innerWidth, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: Metrics.innerRadius, style: .continuous).fill(palette.rowActive.opacity(palette.cardFill)))
    }
}
