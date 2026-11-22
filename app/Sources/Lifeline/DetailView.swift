import LifelineKit
import SwiftUI

public enum HistoryLoad: Equatable, Sendable {
    case waiting
    case ready(HistoryReply)
    case failed
}

public struct DetailHandlers {
    public var use: (String) -> Void
    public var resign: (String) -> Void
    public var rename: (String, [String: String]) async -> String?
    public var remove: (String) async -> String?

    public init(
        use: @escaping (String) -> Void = { _ in },
        resign: @escaping (String) -> Void = { _ in },
        rename: @escaping (String, [String: String]) async -> String? = { _, _ in nil },
        remove: @escaping (String) async -> String? = { _ in nil }
    ) {
        self.use = use
        self.resign = resign
        self.rename = rename
        self.remove = remove
    }
}

struct NameFields: View {
    @Binding var name: String
    @Binding var short: String
    let menuName: String
    var storedShort = ""
    var barHidden = false
    var text: AddText? = nil
    let palette: Palette
    let sheet: SheetPalette
    let width: CGFloat

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            VStack(alignment: .leading, spacing: 8) {
                LineText("Its name", Styles.label, palette.faint)
                SheetField(text: $name, palette: palette, sheet: sheet, width: width)
            }
            VStack(alignment: .leading, spacing: 8) {
                LineText("Short name, for the menu bar", Styles.label, palette.faint)
                SheetField(text: $short, palette: palette, sheet: sheet, width: width)
                let names = AddFlow.shownNames(menuName: menuName, oldShort: storedShort, name: name, short: short)
                Block(SheetText.namesShown(menu: names.menu, bar: names.bar, barHidden: barHidden, text: text), Styles.small, palette.faint, width: width)
            }
        }
    }
}

private struct StillFieldsKey: EnvironmentKey {
    static let defaultValue = false
}

extension EnvironmentValues {
    public var stillFields: Bool {
        get { self[StillFieldsKey.self] }
        set { self[StillFieldsKey.self] = newValue }
    }
}

struct SheetField: View {
    @Binding var text: String
    let palette: Palette
    let sheet: SheetPalette
    let width: CGFloat
    @Environment(\.stillFields) private var still

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: 11, style: .continuous)
        Group {
            if still {
                LineText(text, Styles.field, palette.ink)
            } else {
                TextField("", text: $text)
                    .textFieldStyle(.plain)
                    .font(Styles.field.font)
                    .foregroundStyle(palette.ink)
            }
        }
            .padding(.horizontal, 14)
            .frame(width: width, height: 44, alignment: .leading)
            .background(shape.fill(sheet.input))
            .overlay {
                if let shade = sheet.fieldShadow {
                    InsetShadow(radius: 11, color: shade, y: 1, blur: 1.5)
                }
            }
            .focusEffectDisabled()
    }
}

struct InsetShadow: View {
    let radius: CGFloat
    let color: Color
    let y: CGFloat
    let blur: CGFloat

    var body: some View {
        GeometryReader { proxy in
            let rect = CGRect(origin: .zero, size: proxy.size)
            Path { path in
                path.addRect(rect.insetBy(dx: -12, dy: -12))
                path.addRoundedRect(in: rect.offsetBy(dx: 0, dy: y), cornerSize: CGSize(width: radius, height: radius), style: .continuous)
            }
            .fill(color, style: FillStyle(eoFill: true))
            .blur(radius: blur)
        }
        .mask(RoundedRectangle(cornerRadius: radius, style: .continuous))
        .allowsHitTesting(false)
    }
}

enum SheetButtonLook {
    case primary
    case quiet
    case raised
    case disabled
}

enum ButtonFit {
    static func layout(_ title: String, style: TextStyle, width: CGFloat, height: CGFloat, keep: [String] = []) -> (room: CGFloat, lines: Int, height: CGFloat) {
        let room = width - 2 * Metrics.labelInset
        guard style.width(of: title) > room else { return (room, 1, height) }
        let lines = max(style.lines(title, width: room, keep: keep).count, 1)
        return (room, lines, max(height, CGFloat(lines) * style.lineHeight + 2 * Metrics.labelInset))
    }
}

struct ButtonLabel: View {
    let title: String
    let style: TextStyle
    let color: Color
    let width: CGFloat
    let height: CGFloat
    @Environment(\.keptNames) private var names

    var body: some View {
        let fit = ButtonFit.layout(title, style: style, width: width, height: height, keep: names)
        if fit.lines == 1 {
            LineText(title, style, color)
                .frame(width: width, height: fit.height)
        } else {
            Block(title, style, color, width: fit.room, centered: true)
                .frame(width: width, height: fit.height)
        }
    }
}

struct SheetButton: View {
    let title: String
    let look: SheetButtonLook
    let width: CGFloat
    let height: CGFloat
    let palette: Palette
    let sheet: SheetPalette
    var style: TextStyle? = nil
    let action: (() -> Void)?

    init(
        title: String, look: SheetButtonLook, width: CGFloat, height: CGFloat, palette: Palette, sheet: SheetPalette,
        style: TextStyle? = nil, action: (() -> Void)?
    ) {
        self.title = title
        self.look = look
        self.width = width
        self.height = height
        self.palette = palette
        self.sheet = sheet
        self.style = style
        self.action = action
    }

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: Metrics.innerRadius, style: .continuous)
        let fill: Color
        let ink: Color
        let shadow: ShadowSpec
        let font: TextStyle
        switch look {
        case .primary:
            fill = palette.primary
            ink = palette.primaryInk
            shadow = .none
            font = style ?? Styles.detailPrimary
        case .quiet:
            fill = sheet.quiet
            ink = sheet.quietInk
            shadow = .none
            font = style ?? Styles.detailQuiet
        case .raised:
            fill = sheet.raised
            ink = palette.ink
            shadow = sheet.raisedShadow
            font = style ?? Styles.detailPrimary
        case .disabled:
            fill = sheet.disabled
            ink = palette.faint
            shadow = .none
            font = style ?? Styles.detailQuiet
        }
        return Button {
            action?()
        } label: {
            ButtonLabel(title: title, style: font, color: ink, width: width, height: height)
                .background(shape.fill(fill).cssShadow(shadow))
                .contentShape(shape)
        }
        .buttonStyle(FlatPress())
        .disabled(action == nil)
    }
}

struct WaitingBox: View {
    let sentences: [String]
    let palette: Palette
    let sheet: SheetPalette
    let width: CGFloat

    var body: some View {
        let inner = width - 28
        VStack(alignment: .leading, spacing: 10) {
            if let first = sentences.first {
                Block(first, Styles.sub, palette.text, width: inner)
            }
            TravellingLight(width: inner, palette: palette, sheet: sheet)
            ForEach(Array(sentences.dropFirst().enumerated()), id: \.offset) { item in
                Block(item.element, Styles.small, palette.faint, width: inner)
            }
        }
        .padding(14)
        .frame(width: width, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(sheet.field))
    }
}

/// The 3 pt line with a light travelling along it, which shows that Lifeline is waiting.
/// With Reduce Motion the light rests in place at full strength.
struct TravellingLight: View {
    let width: CGFloat
    let palette: Palette
    let sheet: SheetPalette
    @Environment(\.menuMotion) private var motion
    @Environment(\.reportsText) private var reports
    @State private var travel = TravellingLight.place(glides: false).travel

    nonisolated static let segment = 0.34

    /// Where the light starts and how strong it is: travelling from just off the left edge, or resting at a third.
    nonisolated static func place(glides: Bool) -> (travel: Double, strength: Double) {
        glides ? (-segment, 1) : (0.33, 1)
    }

    var body: some View {
        let strength = Self.place(glides: motion.plan.glide != nil).strength
        ZStack(alignment: .leading) {
            Capsule().fill(palette.track).frame(width: width, height: 3)
            Capsule()
                .fill(LinearGradient(colors: [palette.primary.opacity(0), sheet.waitingLight, palette.primary.opacity(0)], startPoint: .leading, endPoint: .trailing))
                .frame(width: width * Self.segment, height: 3)
                .offset(x: width * travel)
                .opacity(strength)
        }
        .frame(width: width, height: 3, alignment: .leading)
        .clipShape(Capsule())
        .preference(key: DrawnLight.self, value: reports ? 1 : 0)
        .accessibilityHidden(true)
        .onAppear {
            guard motion.plan.glide != nil else { return }
            travel = Self.place(glides: true).travel
            withAnimation(.linear(duration: 1.6).repeatForever(autoreverses: false)) {
                travel = 1
            }
        }
    }
}

/// How many travelling lights a view draws, when the reportsText environment value is on (tests only read it).
struct DrawnLight: PreferenceKey {
    static let defaultValue = 0

    static func reduce(value: inout Int, nextValue: () -> Int) {
        value += nextValue()
    }
}
