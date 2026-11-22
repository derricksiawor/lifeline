import LifelineKit
import SwiftUI

public struct SettingsHandlers {
    public var change: (SettingKey, Bool) -> Void
    public var login: (Bool) -> Void
    public var statusline: (Bool) -> Void
    public var place: (String, String) -> Void
    public var pair: (String?, String) -> Void
    public var quit: () -> Void
    public var reset: () -> Void
    public var back: (() -> Void)?

    public init(
        change: @escaping (SettingKey, Bool) -> Void = { _, _ in },
        login: @escaping (Bool) -> Void = { _ in },
        statusline: @escaping (Bool) -> Void = { _ in },
        place: @escaping (String, String) -> Void = { _, _ in },
        pair: @escaping (String?, String) -> Void = { _, _ in },
        quit: @escaping () -> Void = {},
        reset: @escaping () -> Void = {},
        back: (() -> Void)? = nil
    ) {
        self.back = back
        self.reset = reset
        self.change = change
        self.login = login
        self.statusline = statusline
        self.place = place
        self.pair = pair
        self.quit = quit
    }
}

public struct SettingsView: View {
    let state: ServiceState?
    let local: LocalStatus
    let problem: String?
    let width: CGFloat
    let handlers: SettingsHandlers
    @Environment(\.colorScheme) private var scheme
    @State private var confirmingReset: Bool

    public init(
        state: ServiceState?, local: LocalStatus, problem: String?, width: CGFloat = Metrics.panelWidth, handlers: SettingsHandlers = SettingsHandlers(),
        confirmingReset: Bool = false
    ) {
        self.state = state
        self.local = local
        self.problem = problem
        self.width = width
        self.handlers = handlers
        _confirmingReset = State(initialValue: confirmingReset)
    }

    public var body: some View {
        content.keepsNames(of: state)
    }

    @ViewBuilder
    private var content: some View {
        let palette = Palette.of(scheme)
        let sheet = SheetPalette.of(scheme)
        page(palette: palette, sheet: sheet)
    }

    private func page(palette: Palette, sheet: SheetPalette) -> some View {
        let width = self.width - 2 * Metrics.sheetPadding
        return VStack(alignment: .leading, spacing: 22) {
            if let back = handlers.back {
                BackButton(palette: palette, action: back)
            }
            VStack(alignment: .leading, spacing: 4) {
                LineText(SettingsPage.pageTitle(state), Styles.pageTitle, palette.ink)
                Block(SettingsPage.lead(state), Styles.sheetLead, palette.muted, width: width)
            }
            ForEach(SettingsPage.sections(state: state, local: local)) { section in
                VStack(alignment: .leading, spacing: 10) {
                    LineText(section.title, Styles.label, palette.faint)
                        .padding(.horizontal, 4)
                    if let note = section.note {
                        Paragraph(note, Styles.small, palette.muted, width: width - 8)
                            .padding(.horizontal, 4)
                    }
                    VStack(alignment: .leading, spacing: 2) {
                        ForEach(section.rows) { row in
                            SettingRowView(row: row, palette: palette, sheet: sheet, width: width - 12, handlers: handlers, computer: state?.devbox?.name)
                        }
                    }
                    .padding(6)
                    .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(sheet.group))
                }
            }
            if let problem, !problem.isEmpty {
                Block(problem, Styles.small, palette.muted, width: width, centered: true)
            }
            SheetButton(title: "Quit Lifeline", look: .quiet, width: width, height: Metrics.actionHeight, palette: palette, sheet: sheet, style: Styles.action) {
                handlers.quit()
            }
            if let sentence = state?.settingsText?.reset, !sentence.isEmpty {
                if confirmingReset {
                    ConfirmCard(sentence: sentence, confirm: "Reset", palette: palette, width: width - 28, action: {
                        confirmingReset = false
                        handlers.reset()
                    }) {
                        confirmingReset = false
                    }
                    .padding(14)
                    .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(sheet.group))
                } else {
                    SheetButton(title: "Reset Lifeline", look: .quiet, width: width, height: Metrics.actionHeight, palette: palette, sheet: sheet, style: Styles.action) {
                        confirmingReset = true
                    }
                }
            }
        }
        .padding(Metrics.sheetPadding)
        .frame(width: self.width, alignment: .topLeading)
    }
}

struct SettingRowView: View {
    let row: SettingRow
    let palette: Palette
    let sheet: SheetPalette
    let width: CGFloat
    let handlers: SettingsHandlers
    var computer: String? = nil
    @State private var address = ""
    @State private var code = ""

    static func inner(_ width: CGFloat) -> CGFloat {
        width - 28
    }

    /// The room for a row's title: beside its icon and control, or the full row when the control drops below it. The row
    /// draws its title in exactly this room, so a test of the room is a test of what is drawn.
    static func titleRoom(_ row: SettingRow, width: CGFloat) -> CGFloat {
        layout(row, width: width).room
    }

    static func layout(_ row: SettingRow, width: CGFloat, computer: String? = nil) -> (room: CGFloat, below: Bool) {
        let control = controlWidth(row.control, computer: computer)
        let tile: CGFloat = row.glyph.isEmpty ? 0 : 44
        let beside = inner(width) - tile - control - (control > 0 ? 12 : 0)
        let below = control > 0 && beside < Metrics.settingTitleMinimum
        return (below ? inner(width) - tile : beside, below)
    }

    /// A chip that cannot be pressed (the other computer does not answer) is quiet, with faint ink at full strength.
    static func chipLook(away: Bool, pressed: Bool, palette: Palette, sheet: SheetPalette) -> (fill: Color, ink: Color, strength: Double) {
        if away {
            return (sheet.quiet, palette.faint, 1)
        }
        return pressed ? (palette.primary, palette.primaryInk, 1) : (sheet.quiet, sheet.quietInk, 1)
    }

    var body: some View {
        let inner = Self.inner(width)
        let controlWidth = Self.controlWidth(row.control, computer: computer)
        let tile: CGFloat = row.glyph.isEmpty ? 0 : 44
        let layout = Self.layout(row, width: width, computer: computer)
        let below = layout.below
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .center, spacing: 12) {
                if !row.glyph.isEmpty {
                    Image(systemName: row.glyph)
                        .font(.system(size: 14, weight: .light))
                        .foregroundStyle(sheet.quietInk)
                        .frame(width: 32, height: 32)
                        .background(RoundedRectangle(cornerRadius: 9, style: .continuous).fill(sheet.glyph).cssShadow(sheet.raisedShadow))
                        .accessibilityHidden(true)
                }
                Block(row.title, Styles.rowTitle, palette.ink, width: layout.room)
                if controlWidth > 0 && !below {
                    control
                }
            }
            .frame(width: inner, alignment: .leading)
            if below {
                control
                    .padding(.leading, tile)
                    .padding(.vertical, 4)
            }
            Paragraph(row.description, Styles.sub, palette.muted, width: inner)
            if let note = row.note {
                Paragraph(note, Styles.sub, palette.text, width: inner)
                    .padding(.top, 2)
            }
            if row.control == .computer {
                computerForm(inner)
            }
        }
        .padding(14)
        .frame(width: width, alignment: .leading)
    }

    static func controlWidth(_ control: SettingControl, computer: String? = nil) -> CGFloat {
        switch control {
        case .toggle: return 42
        case .placement, .placementLocked: return Styles.chip.width(of: "This Mac") + Styles.chip.width(of: computer ?? "") + 40 + 6
        case .keys: return Shortcut.keys.map { Styles.chip.width(of: $0) + 20 }.reduce(0, +) + 12
        case .computer, .none: return 0
        }
    }

    /// Add a computer: its address and the pairing code it showed. A paired computer gets a button to forget it.
    @ViewBuilder
    private func computerForm(_ width: CGFloat) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            if computer == nil {
                LineText("Address", Styles.label, palette.faint)
                SheetField(text: $address, palette: palette, sheet: sheet, width: width)
                LineText("Pairing code", Styles.label, palette.faint)
                SheetField(text: $code, palette: palette, sheet: sheet, width: width)
                SheetButton(title: "Add a computer", look: .primary, width: width, height: Metrics.actionHeight, palette: palette, sheet: sheet, style: Styles.action) {
                    handlers.pair(address, code)
                    code = ""
                }
            } else {
                SheetButton(title: "Forget \(computer ?? "")", look: .quiet, width: width, height: Metrics.actionHeight, palette: palette, sheet: sheet, style: Styles.action) {
                    handlers.pair(nil, "")
                }
            }
        }
        .padding(.top, 6)
    }

    @ViewBuilder
    private var control: some View {
        switch row.control {
        case .toggle(let key, let on, let enabled):
            Toggle42(on: on, enabled: enabled, sheet: sheet, palette: palette) {
                if let key {
                    handlers.change(key, !on)
                } else if row.id == "login" {
                    handlers.login(!on)
                } else if row.id == "statusline" {
                    handlers.statusline(!on)
                }
            }
        case .placement(let uuid, let value):
            HStack(spacing: 6) {
                ForEach([("This Mac", Placement.mac), (computer ?? "", Placement.devbox)], id: \.1) { item in
                    let pressed = Placement.includes(value, chip: item.1)
                    Button {
                        if let next = Placement.pressing(item.1, on: value) {
                            handlers.place(uuid, next)
                        }
                    } label: {
                        LineText(item.0, Styles.chip, pressed ? palette.primaryInk : sheet.quietInk)
                            .padding(.horizontal, 10)
                            .frame(height: 28)
                            .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(pressed ? palette.primary : sheet.quiet))
                            .contentShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                    }
                    .buttonStyle(FlatPress())
                    .accessibilityValue(pressed ? "On" : "Off")
                }
            }
        case .placementLocked(_, let value):
            HStack(spacing: 6) {
                ForEach([("This Mac", Placement.mac), (computer ?? "", Placement.devbox)], id: \.1) { item in
                    let pressed = Placement.includes(value, chip: item.1)
                    let away = item.1 == Placement.devbox
                    let look = Self.chipLook(away: away, pressed: pressed, palette: palette, sheet: sheet)
                    LineText(item.0, Styles.chip, look.ink)
                        .padding(.horizontal, 10)
                        .frame(height: 28)
                        .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(look.fill))
                        .opacity(look.strength)
                        .accessibilityValue(away ? "Unavailable" : (pressed ? "On" : "Off"))
                }
            }
        case .keys:
            HStack(spacing: 6) {
                ForEach(Shortcut.keys, id: \.self) { key in
                    LineText(key, Styles.chip, palette.ink)
                        .padding(.horizontal, 10)
                        .frame(height: 28)
                        .background(RoundedRectangle(cornerRadius: 7, style: .continuous).fill(sheet.raised).cssShadow(sheet.raisedShadow))
                }
            }
        case .computer, .none:
            EmptyView()
        }
    }
}

struct Toggle42: View {
    let on: Bool
    let enabled: Bool
    let sheet: SheetPalette
    let palette: Palette
    let action: () -> Void
    @Environment(\.menuMotion) private var motion

    var body: some View {
        Button {
            if enabled {
                action()
            }
        } label: {
            ZStack(alignment: .leading) {
                Capsule().fill(on ? palette.primary : sheet.toggleOff)
                Circle()
                    .fill(sheet.knob)
                    .cssShadow(sheet.knobShadow)
                    .frame(width: 18, height: 18)
                    .offset(x: on ? 21 : 3)
            }
            .frame(width: 42, height: 24)
            .opacity(enabled ? 1 : 0.6)
            .animation(motion.pairFade, value: on)
            .contentShape(Capsule())
        }
        .buttonStyle(FlatPress())
        .disabled(!enabled)
        .accessibilityValue(on ? "On" : "Off")
    }
}
