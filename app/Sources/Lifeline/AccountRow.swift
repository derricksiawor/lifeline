import LifelineKit
import SwiftUI

struct FlatPress: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
    }
}

struct SecondaryClick: NSViewRepresentable {
    let action: (() -> Void)?

    func makeNSView(context: Context) -> Catcher {
        let view = Catcher()
        view.action = action
        return view
    }

    func updateNSView(_ view: Catcher, context: Context) {
        view.action = action
    }

    static func catches(_ type: NSEvent.EventType?, flags: NSEvent.ModifierFlags) -> Bool {
        switch type {
        case .rightMouseDown, .rightMouseUp:
            return true
        case .leftMouseDown, .leftMouseUp:
            return flags.contains(.control)
        default:
            return false
        }
    }

    final class Catcher: NSView {
        var action: (() -> Void)?

        override func hitTest(_ point: NSPoint) -> NSView? {
            guard action != nil, let event = NSApp.currentEvent, SecondaryClick.catches(event.type, flags: event.modifierFlags) else {
                return nil
            }
            return super.hitTest(point)
        }

        override func rightMouseDown(with event: NSEvent) {
            action?()
        }

        override func rightMouseUp(with event: NSEvent) {}

        override func mouseDown(with event: NSEvent) {
            if event.modifierFlags.contains(.control) {
                action?()
            }
        }

        override func mouseUp(with event: NSEvent) {}
    }
}

struct RowFrames: PreferenceKey {
    static let defaultValue: [String: CGRect] = [:]

    static func reduce(value: inout [String: CGRect], nextValue: () -> [String: CGRect]) {
        value.merge(nextValue()) { _, new in new }
    }
}

struct RowLook: Equatable {
    var background: Double
    var glow: Double
    var landing: Double
    var five: Double
    var ahead: Double
    var week: Double

    static func plain(_ row: MenuRow) -> RowLook {
        let active = row.isActive == true ? 1.0 : 0
        return RowLook(
            background: active,
            glow: active,
            landing: 0,
            five: Fraction.of(row.fiveW),
            ahead: Fraction.of(row.foreW),
            week: AccountRow.thin(row)?.fraction ?? 0
        )
    }
}

struct AccountRow: View {
    static let space = "rows"
    /// The keyboard highlight: the row fill at 60 percent, with no outline.
    nonisolated static let highlightStrength = 0.6

    nonisolated static func fillStrength(look: Double, open: Bool, highlighted: Bool, palette: Palette) -> Double {
        max(look, open ? palette.openRow : 0, highlighted ? highlightStrength : 0)
    }

    let row: MenuRow
    let old: MenuRow?
    let fade: TextFade
    let palette: Palette
    let look: RowLook
    let animated: Bool
    var open = false
    var highlighted = false
    let action: (() -> Void)?
    var secondary: (() -> Void)?
    var expansion: AnyView? = nil
    var showsSecond = false
    var whereText: String? = nil
    var chevron: (() -> Void)? = nil
    @Environment(\.menuMotion) private var motion
    @Environment(\.stillFields) private var still

    private var tagTop: CGFloat {
        (Styles.rowName.lineHeight - Styles.tag.lineHeight) / 2
    }

    private var pctTop: CGFloat {
        (Styles.rowName.lineHeight - Styles.rowPct.lineHeight) / 2
    }

    var body: some View {
        let tone = palette.tone(row.tone)
        let width = Metrics.contentWidth
        let thin = Self.thin(row)
        let oldThin = old.flatMap { Self.thin($0) }
        let weekShown = thin != nil
        let weekWidth: CGFloat = weekShown ? MixedLine.width(thin?.label, numbers: Styles.weekPct) + 8 : 0
        let strength = Self.lineStrength(row)
        let mainPct: String? = Self.named(row)
        let oldPct: String? = old.flatMap { Self.named($0) }
        let lineAnimation = animated ? motion.glide : nil
        let fadeAnimation = animated ? motion.rowFade : nil
        let text = old == nil ? TextFade.settled : fade
        let oldTone = palette.tone(old?.tone)
        let oldInk = old?.unused == true ? palette.muted : oldTone.ink
        let tagNow = row.isBest == true
        let tagBefore = old?.isBest == true
        let tagOpacity = tagNow && tagBefore ? 1 : (tagNow ? text.incoming : text.outgoing)
        VStack(alignment: .leading, spacing: 0) {
        Button {
            action?()
        } label: {
            VStack(alignment: .leading, spacing: 0) {
                HStack(alignment: .top, spacing: 0) {
                    HStack(alignment: .top, spacing: 8) {
                        Line(row.name, Styles.rowName, palette.ink)
                        if tagNow || (tagBefore && text != .settled) {
                            LineText("Most room", Styles.tag, palette.tagInk)
                                .padding(.horizontal, 7)
                                .background(RoundedRectangle(cornerRadius: Metrics.tagRadius, style: .continuous).fill(palette.tagBackground))
                                .padding(.top, tagTop)
                                .opacity(tagOpacity)
                        }
                    }
                    Spacer(minLength: 8)
                    HStack(alignment: .center, spacing: 8) {
                        MixedSwap(
                            old: oldPct, new: mainPct, numbers: Styles.rowPct, color: row.unused == true ? palette.muted : tone.ink, oldColor: oldInk,
                            fade: text, alignment: .topTrailing
                        )
                        Chevron()
                            .stroke(open ? palette.quietInk : palette.faint, style: StrokeStyle(lineWidth: 2 * 14 / 24, lineCap: .round, lineJoin: .round))
                            .frame(width: 14, height: 14)
                            .rotationEffect(.degrees(open ? 90 : 0))
                    }
                    .padding(.top, pctTop)
                }
                .frame(width: width, height: Styles.rowName.lineHeight, alignment: .top)
                Meter(
                    used: look.five,
                    ahead: look.ahead,
                    height: 3,
                    width: width,
                    track: palette.track,
                    tone: tone,
                    glow: tone.shadow,
                    glowWeight: look.glow,
                    landing: palette.landing,
                    landingWeight: look.landing,
                    lineAnimation: lineAnimation,
                    glowAnimation: fadeAnimation
                )
                .opacity(strength)
                .padding(.top, 8)
                if weekShown {
                    WeekLine(fraction: look.week, width: width, track: palette.track, fill: palette.weekFill)
                        .animation(lineAnimation, value: look.week)
                        .opacity(strength)
                        .padding(.top, 5)
                }
                HStack(alignment: .center, spacing: 0) {
                    Swap(old?.where, whereText ?? row.where, Styles.small, Self.whereInk(row, palette: palette), fade: text, width: max(width - weekWidth, 0))
                    if weekShown {
                        Spacer(minLength: 0)
                        MixedSwap(old: oldThin?.label, new: thin?.label, numbers: Styles.weekPct, color: palette.faint, fade: text, alignment: .topTrailing)
                    }
                }
                .frame(width: width, alignment: .leading)
                .padding(.top, 8)
                if let note = row.readNote, !note.isEmpty {
                    Block(note, Styles.small, palette.faint, width: width)
                        .padding(.top, 2)
                }
            }
            .padding(.vertical, Self.padding)
            .padding(.horizontal, Metrics.sideInset)
            .frame(width: Metrics.innerWidth, alignment: .leading)
            .contentShape(RoundedRectangle(cornerRadius: Metrics.innerRadius, style: .continuous))
        }
        .buttonStyle(FlatPress())
        .overlay {
            if !still {
                SecondaryClick(action: secondary)
            }
        }
        .overlay(alignment: .topTrailing) {
            if !still, let chevron {
                ChevronTarget(open: open, name: row.name ?? "this account", action: chevron)
            }
        }
        if let expansion {
            expansion
                .padding(.horizontal, Metrics.sideInset)
                .padding(.bottom, 14)
                .transition(.opacity)
        }
        }
        .frame(width: Metrics.innerWidth, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: Metrics.innerRadius, style: .continuous)
                .fill(palette.rowActive.opacity(Self.fillStrength(look: look.background, open: expansion != nil, highlighted: highlighted, palette: palette)))
                .animation(fadeAnimation, value: look.background)
        )
        .background(
            GeometryReader { proxy in
                Color.clear.preference(key: RowFrames.self, value: [row.uuid: proxy.frame(in: .named(Self.space))])
            }
        )
    }
}

struct ChevronTarget: View {
    let open: Bool
    let name: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Color.clear
                .frame(width: Metrics.chevronTarget, height: Metrics.chevronTarget)
                .contentShape(Rectangle())
        }
        .buttonStyle(FlatPress())
        .accessibilityLabel(open ? "Close the details of \(name)" : "Open the details of \(name)")
    }
}

extension AccountRow {
    nonisolated static func named(_ row: MenuRow) -> String? {
        if let figure = row.figure {
            return figure
        }
        if let label = row.pctLabel {
            return label
        }
        return row.pct
    }
}

extension AccountRow {
    static let padding: CGFloat = 10
    /// The middle of the row's 3 pt line, from the row's top: the height the travelling light rides at.
    static let lineY: CGFloat = padding + Styles.rowName.lineHeight + 8 + 1.5

    /// The 2 pt line: the other window when the account reports both (C2's thin_window), as a fill and the label beside the where line.
    nonisolated static func thin(_ row: MenuRow) -> (fraction: Double, label: String)? {
        if let window = row.thinWindow {
            guard !window.isEmpty else { return nil }
            return (Fraction.of(row.thinW), row.thinLabel ?? "")
        }
        guard let label = row.weekPct, !label.isEmpty else { return nil }
        return (Fraction.of(row.weekW), label)
    }

    /// A row that needs a new sign-in keeps its where line in the quiet amber ink, so it stands apart from a calm idle row.
    nonisolated static func whereInk(_ row: MenuRow, palette: Palette) -> Color {
        row.needsSignin == true ? palette.low.ink : palette.muted
    }

    /// A stale reading, or no reading at all, draws its lines dimmed.
    nonisolated static func lineStrength(_ row: MenuRow) -> Double {
        if row.stale == true {
            return Metrics.unavailable
        }
        if let display = row.display, display.isEmpty {
            return Metrics.unavailable
        }
        return 1
    }
}

struct Chevron: Shape {
    func path(in rect: CGRect) -> Path {
        var path = Path()
        path.move(to: CGPoint(x: 9, y: 18))
        path.addLine(to: CGPoint(x: 15, y: 12))
        path.addLine(to: CGPoint(x: 9, y: 6))
        let scale = min(rect.width, rect.height) / 24
        return path.applying(CGAffineTransform(scaleX: scale, y: scale).translatedBy(x: rect.minX / scale, y: rect.minY / scale))
    }
}
