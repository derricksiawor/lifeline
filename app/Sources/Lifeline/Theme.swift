import AppKit
import LifelineKit
import SwiftUI

public struct ShadowSpec: Sendable {
    public let color: Color
    public let x: CGFloat
    public let y: CGFloat
    public let blur: CGFloat

    public init(_ color: Color, x: CGFloat = 0, y: CGFloat, blur: CGFloat) {
        self.color = color
        self.x = x
        self.y = y
        self.blur = blur
    }

    public static let none = ShadowSpec(.clear, y: 0, blur: 0)

    func weighted(_ weight: Double) -> ShadowSpec {
        ShadowSpec(color.opacity(min(max(weight, 0), 1)), x: x, y: y, blur: blur)
    }
}

extension View {
    func cssShadow(_ spec: ShadowSpec) -> some View {
        shadow(color: spec.color, radius: spec.blur / 2, x: spec.x, y: spec.y)
    }
}

extension Color {
    init(hex: UInt32, alpha: Double = 1) {
        self.init(
            .sRGB,
            red: Double((hex >> 16) & 0xff) / 255,
            green: Double((hex >> 8) & 0xff) / 255,
            blue: Double(hex & 0xff) / 255,
            opacity: alpha
        )
    }

    init(r: Double, g: Double, b: Double, a: Double) {
        self.init(.sRGB, red: r / 255, green: g / 255, blue: b / 255, opacity: a)
    }

    func mixed(with other: Color, _ amount: Double) -> Color {
        let t = min(max(amount, 0), 1)
        if t == 0 { return self }
        if t == 1 { return other }
        guard let a = NSColor(self).usingColorSpace(.sRGB), let b = NSColor(other).usingColorSpace(.sRGB) else {
            return t < 0.5 ? self : other
        }
        func blend(_ x: CGFloat, _ y: CGFloat) -> Double { Double(x + (y - x) * t) }
        return Color(
            .sRGB,
            red: blend(a.redComponent, b.redComponent),
            green: blend(a.greenComponent, b.greenComponent),
            blue: blend(a.blueComponent, b.blueComponent),
            opacity: blend(a.alphaComponent, b.alphaComponent)
        )
    }
}

public struct Tone: Sendable {
    public let fillStart: Color
    public let fillEnd: Color
    public let ink: Color
    public let solid: Color
    public let glow: Color
    public let fore: Color
    public let shadow: ShadowSpec

    public var fill: LinearGradient {
        LinearGradient(colors: [fillStart, fillEnd], startPoint: .leading, endPoint: .trailing)
    }
}

public struct Palette: Sendable {
    public let panelTop: Color
    public let panelBottom: Color
    public let panelShadows: [ShadowSpec]
    public let ink: Color
    public let text: Color
    public let muted: Color
    public let faint: Color
    public let track: Color
    public let weekFill: Color
    public let rowActive: Color
    public let openRow: Double
    /// The fill of the menu's cards (an alert, a sign-in waiting, a result), apart from the open row's.
    public let cardFill: Double
    public let pairTrack: Color
    public let pairSelected: Color
    public let pairSelectedInk: Color
    public let pairInk: Color
    public let pairSelectedShadow: ShadowSpec
    public let primary: Color
    public let primaryInk: Color
    public let primaryShadow: ShadowSpec
    public let quietBackground: Color
    public let quietInk: Color
    public let tagBackground: Color
    public let tagInk: Color
    public let backdrop: Color
    public let orb: Color
    public let landing: ShadowSpec
    public let calm: Tone
    public let low: Tone
    public let limit: Tone

    public var panel: LinearGradient {
        LinearGradient(colors: [panelTop, panelBottom], startPoint: .top, endPoint: .bottom)
    }

    public func tone(_ name: String?) -> Tone {
        switch name {
        case "limit": return limit
        case "low": return low
        default: return calm
        }
    }

    public static func of(_ scheme: ColorScheme) -> Palette {
        scheme == .light ? light : dark
    }

    public static let dark = Palette(
        panelTop: Color(hex: 0x181f37),
        panelBottom: Color(hex: 0x141a2e),
        panelShadows: [
            ShadowSpec(Color(r: 0, g: 0, b: 0, a: 0.55), y: 28, blur: 56),
            ShadowSpec(Color(r: 0, g: 0, b: 0, a: 0.35), y: 2, blur: 8)
        ],
        ink: Color(hex: 0xeef0fa),
        text: Color(hex: 0xc3c8dc),
        muted: Color(hex: 0x9aa1bd),
        faint: Color(hex: 0x8a91ad),
        track: Color(hex: 0x252c48),
        weekFill: Color(hex: 0x6a64c9),
        rowActive: Color(hex: 0x1d2546),
        openRow: 0.3,
        cardFill: 0.3,
        pairTrack: Color(hex: 0x10162a),
        pairSelected: Color(hex: 0x262f52),
        pairSelectedInk: Color(hex: 0xeef0fa),
        pairInk: Color(hex: 0x9aa1bd),
        pairSelectedShadow: .none,
        primary: Color(hex: 0x5f57e8),
        primaryInk: Color(hex: 0xffffff),
        primaryShadow: ShadowSpec(Color(r: 0, g: 0, b: 0, a: 0.3), y: 8, blur: 20),
        quietBackground: Color(hex: 0x1d2546),
        quietInk: Color(hex: 0xc9c6ff),
        tagBackground: Color(r: 109, g: 102, b: 242, a: 0.2),
        tagInk: Color(hex: 0xc9c6ff),
        backdrop: Color(hex: 0x0b0f1d),
        orb: Color(r: 122, g: 115, b: 245, a: 0.9),
        landing: ShadowSpec(Color(r: 122, g: 115, b: 245, a: 0.8), y: 0, blur: 14),
        calm: Tone(
            fillStart: Color(hex: 0x5f57e8), fillEnd: Color(hex: 0xaaa5ff), ink: Color(hex: 0xc9c6ff),
            solid: Color(hex: 0xaaa5ff), glow: Color(r: 122, g: 115, b: 245, a: 0.85),
            fore: Color(r: 170, g: 165, b: 255, a: 0.24),
            shadow: ShadowSpec(Color(r: 122, g: 115, b: 245, a: 0.55), y: 0, blur: 10)
        ),
        low: Tone(
            fillStart: Color(hex: 0xd9962a), fillEnd: Color(hex: 0xf6c76a), ink: Color(hex: 0xf6c76a),
            solid: Color(hex: 0xf6c76a), glow: Color(r: 242, g: 179, b: 61, a: 0.85),
            fore: Color(r: 246, g: 199, b: 106, a: 0.3),
            shadow: ShadowSpec(Color(r: 242, g: 179, b: 61, a: 0.5), y: 0, blur: 10)
        ),
        limit: Tone(
            fillStart: Color(hex: 0xe2553f), fillEnd: Color(hex: 0xff9a8a), ink: Color(hex: 0xff9a8a),
            solid: Color(hex: 0xff9a8a), glow: Color(r: 255, g: 122, b: 102, a: 0.8),
            fore: Color(r: 255, g: 154, b: 138, a: 0.3),
            shadow: ShadowSpec(Color(r: 255, g: 122, b: 102, a: 0.5), y: 0, blur: 10)
        )
    )

    public static let light = Palette(
        panelTop: Color(hex: 0xffffff),
        panelBottom: Color(hex: 0xfafbfd),
        panelShadows: [
            ShadowSpec(Color(r: 15, g: 23, b: 42, a: 0.16), y: 28, blur: 56),
            ShadowSpec(Color(r: 15, g: 23, b: 42, a: 0.08), y: 2, blur: 8)
        ],
        ink: Color(hex: 0x0b1220),
        text: Color(hex: 0x2a3342),
        muted: Color(hex: 0x5b6472),
        faint: Color(hex: 0x6b7280),
        track: Color(hex: 0xe7eaf1),
        weekFill: Color(hex: 0x9893ef),
        rowActive: Color(hex: 0xf0f1fb),
        openRow: 0.55,
        cardFill: 1,
        pairTrack: Color(hex: 0xf0f2f6),
        pairSelected: Color(hex: 0xffffff),
        pairSelectedInk: Color(hex: 0x0b1220),
        pairInk: Color(hex: 0x5b6472),
        pairSelectedShadow: ShadowSpec(Color(r: 15, g: 23, b: 42, a: 0.12), y: 1, blur: 3),
        primary: Color(hex: 0x5f57e8),
        primaryInk: Color(hex: 0xffffff),
        primaryShadow: ShadowSpec(Color(r: 95, g: 87, b: 232, a: 0.25), y: 8, blur: 20),
        quietBackground: Color(hex: 0xeef0fb),
        quietInk: Color(hex: 0x3d36c4),
        tagBackground: Color(r: 95, g: 87, b: 232, a: 0.12),
        tagInk: Color(hex: 0x4a43d6),
        backdrop: Color(hex: 0xe9ecf2),
        orb: Color(r: 95, g: 87, b: 232, a: 0.45),
        landing: ShadowSpec(Color(r: 95, g: 87, b: 232, a: 0.45), y: 0, blur: 14),
        calm: Tone(
            fillStart: Color(hex: 0x5f57e8), fillEnd: Color(hex: 0x8b84f5), ink: Color(hex: 0x4a43d6),
            solid: Color(hex: 0x5f57e8), glow: Color(r: 95, g: 87, b: 232, a: 0.45),
            fore: Color(r: 95, g: 87, b: 232, a: 0.18),
            shadow: ShadowSpec(Color(r: 95, g: 87, b: 232, a: 0.35), y: 0, blur: 8)
        ),
        low: Tone(
            fillStart: Color(hex: 0xc47f0e), fillEnd: Color(hex: 0xe0a432), ink: Color(hex: 0x875900),
            solid: Color(hex: 0xc47f0e), glow: Color(r: 224, g: 164, b: 50, a: 0.5),
            fore: Color(r: 224, g: 164, b: 50, a: 0.25),
            shadow: ShadowSpec(Color(r: 224, g: 164, b: 50, a: 0.35), y: 0, blur: 8)
        ),
        limit: Tone(
            fillStart: Color(hex: 0xd63b27), fillEnd: Color(hex: 0xf0705e), ink: Color(hex: 0xc0291c),
            solid: Color(hex: 0xd63b27), glow: Color(r: 214, g: 59, b: 39, a: 0.4),
            fore: Color(r: 240, g: 112, b: 94, a: 0.22),
            shadow: ShadowSpec(Color(r: 214, g: 59, b: 39, a: 0.3), y: 0, blur: 8)
        )
    )
}

public struct SheetPalette: Sendable {
    public let quiet: Color
    public let quietInk: Color
    public let raised: Color
    public let raisedShadow: ShadowSpec
    public let disabled: Color
    public let field: Color
    public let input: Color
    public let fieldShadow: Color?
    public let group: Color
    public let glyph: Color
    public let toggleOff: Color
    public let knob: Color
    public let knobShadow: ShadowSpec
    public let weekLine: Color
    public let waitingLight: Color
    public let check: Color

    public static func of(_ scheme: ColorScheme) -> SheetPalette {
        scheme == .light ? light : dark
    }

    public static let dark = SheetPalette(
        quiet: Color(hex: 0x1d2546),
        quietInk: Color(hex: 0xc9c6ff),
        raised: Color(hex: 0x262f52),
        raisedShadow: .none,
        disabled: Color(hex: 0x161d33),
        field: Color(hex: 0x10162a),
        input: Color(hex: 0x0b0f1d),
        fieldShadow: Color(r: 0, g: 0, b: 0, a: 0.4),
        group: Color(hex: 0x1a2140),
        glyph: Color(hex: 0x232b4c),
        toggleOff: Color(hex: 0x2a3150),
        knob: Color(hex: 0xeef0fa),
        knobShadow: ShadowSpec(Color(r: 0, g: 0, b: 0, a: 0.4), y: 1, blur: 3),
        weekLine: Color(hex: 0x8b84f5),
        waitingLight: Color(hex: 0xaaa5ff),
        check: Color(hex: 0xc9c6ff)
    )

    public static let light = SheetPalette(
        quiet: Color(hex: 0xeef0fb),
        quietInk: Color(hex: 0x3d36c4),
        raised: Color(hex: 0xffffff),
        raisedShadow: ShadowSpec(Color(r: 15, g: 23, b: 42, a: 0.12), y: 1, blur: 3),
        disabled: Color(hex: 0xf0f2f6),
        field: Color(hex: 0xf0f2f6),
        input: Color(hex: 0xd0d4e6),
        fieldShadow: nil,
        group: Color(hex: 0xf0f2f6),
        glyph: Color(hex: 0xffffff),
        toggleOff: Color(hex: 0xdfe3ea),
        knob: Color(hex: 0xffffff),
        knobShadow: ShadowSpec(Color(r: 15, g: 23, b: 42, a: 0.25), y: 1, blur: 3),
        weekLine: Color(hex: 0x8b84f5),
        waitingLight: Color(hex: 0x8b84f5),
        check: Color(hex: 0x3d36c4)
    )
}

public struct IconPalette: Sendable {
    public let pill: Color
    public let ink: Color
    public let track: Color
    public let amber: Color
    public let calm: Color
    public let low: Color
    public let limit: Color

    public var amberGlow: Color {
        amber.opacity(0.6)
    }

    public static func of(_ appearance: String) -> IconPalette {
        if appearance == "light" {
            return IconPalette(
                pill: Color(hex: 0xcfd4e0), ink: Color(hex: 0x1f2533), track: Color(hex: 0xaeb5c6),
                amber: Color(hex: 0xc47f0e), calm: Color(hex: 0x5f57e8), low: Color(hex: 0xc47f0e),
                limit: Color(hex: 0xd63b27)
            )
        }
        return IconPalette(
            pill: Color(hex: 0x343c5e), ink: Color(hex: 0xeef0fa), track: Color(hex: 0x4a5274),
            amber: Color(hex: 0xf6c76a), calm: Color(hex: 0xaaa5ff), low: Color(hex: 0xf6c76a),
            limit: Color(hex: 0xff9a8a)
        )
    }

    public func color(for tone: String) -> Color {
        switch tone {
        case "limit": return limit
        case "low": return low
        default: return calm
        }
    }
}

public enum Metrics {
    public static let panelWidth: CGFloat = 392
    public static let panelPadding: CGFloat = 10
    public static let panelRadius: CGFloat = 20
    public static let panelGap: CGFloat = 4
    public static let innerWidth: CGFloat = panelWidth - 2 * panelPadding
    public static let innerRadius: CGFloat = panelRadius - panelPadding
    public static let sideInset: CGFloat = 12
    public static let contentWidth: CGFloat = innerWidth - 2 * sideInset
    public static let pairInset: CGFloat = 3
    public static let pairHeight: CGFloat = 30
    public static let pairRadius: CGFloat = innerRadius - pairInset
    public static let tagRadius: CGFloat = 6
    public static let actionHeight: CGFloat = 46
    public static let labelInset: CGFloat = 16
    public static let footerHeight: CGFloat = 38
    public static let barGap: CGFloat = 10
    public static let shadowSide: CGFloat = 84
    public static let shadowTop: CGFloat = barGap
    public static let shadowBottom: CGFloat = 112
    public static let orbInset: CGFloat = 40
    public static let orbHeight: CGFloat = 6
    public static let orbBlur: CGFloat = 6
    public static let glowBlur: CGFloat = 7
    public static let sheetPadding: CGFloat = 24
    public static let expandedRow: Double = 0.3
    public static let busyAction: Double = 0.5
    public static let chevronTarget: CGFloat = 40
    public static let minimumCards: CGFloat = 96
    public static let unavailable: Double = 0.45
    public static let edgeFade: CGFloat = 28
    public static let restFade: CGFloat = 6
    public static let resizeEdge: CGFloat = 6
    public static let resizeCorner: CGFloat = 14
    public static let gripInset: CGFloat = 12
    public static let settingsHeight: Double = 520
    public static let settingTitleMinimum: CGFloat = 130
    public static let minimumList: CGFloat = 120
}

public enum Styles {
    public static let label = TextStyle(.words, 11, weight: 600, line: 16, tracking: 0.12, uppercase: true)
    public static let sessions = TextStyle(.words, 12, line: 18)
    public static let heroName = TextStyle(.words, 15, weight: 500, line: 22)
    public static let heroPlan = TextStyle(.numbers, 12, line: 18)
    public static let head = TextStyle(.words, 21, weight: 500, line: 29, tracking: -0.01)
    public static let sub = TextStyle(.words, 13, line: 20)
    public static let small = TextStyle(.words, 12, line: 18)
    public static let smallAction = TextStyle(.words, 12, weight: 500, line: 18)
    public static let smallNumbers = TextStyle(.numbers, 12, line: 18)
    public static let rowName = TextStyle(.words, 14, weight: 500, line: 21)
    public static let tag = TextStyle(.words, 11, line: 18)
    public static let rowPct = TextStyle(.numbers, 13, weight: 500, line: 20)
    public static let weekPct = TextStyle(.numbers, 11, line: 18)
    public static let dayMark = TextStyle(.words, 10, line: 14)
    public static let action = TextStyle(.words, 14, weight: 500, line: 22)
    public static let pair = TextStyle(.words, 13, weight: 500, line: 20)
    public static let footer = TextStyle(.words, 13, line: 20)
    public static let iconName = TextStyle(.words, 12, line: 18)
    public static let iconPct = TextStyle(.numbers, 12, line: 18)
    public static let detailPrimary = TextStyle(.words, 13, weight: 500, line: 20)
    public static let detailQuiet = TextStyle(.words, 13, line: 20)
    public static let field = TextStyle(.words, 14, line: 22)
    public static let sheetTitle = TextStyle(.words, 20, weight: 600, line: 29)
    public static let sheetLead = TextStyle(.words, 14, line: 22)
    public static let explain = TextStyle(.words, 13, line: 21)
    public static let pageTitle = TextStyle(.words, 22, weight: 600, line: 31)
    public static let rowTitle = TextStyle(.words, 14, weight: 500, line: 22)
    public static let glyph = TextStyle(.numbers, 12, weight: 500, line: 18)
    public static let chip = TextStyle(.words, 12, line: 18)
}

public struct Motion: Sendable {
    public let plan: MotionPlan

    public init(_ plan: MotionPlan) {
        self.plan = plan
    }

    public static let curve = UnitCurve.bezier(
        startControlPoint: UnitPoint(x: MotionPlan.curve.0, y: MotionPlan.curve.1),
        endControlPoint: UnitPoint(x: MotionPlan.curve.2, y: MotionPlan.curve.3)
    )

    static func timed(_ seconds: Double?) -> Animation? {
        guard let seconds else { return nil }
        return .timingCurve(MotionPlan.curve.0, MotionPlan.curve.1, MotionPlan.curve.2, MotionPlan.curve.3, duration: seconds)
    }

    public var glide: Animation? { Self.timed(plan.glide) }
    public var rowFade: Animation? { Self.timed(plan.rowFade) }
    public var pairFade: Animation? { Self.timed(plan.pairFade) }
    public var rise: Animation? { Self.timed(plan.rise) }

    public var screens: Animation {
        if let response = plan.screens.response {
            return .spring(response: response, dampingFraction: plan.screens.damping)
        }
        return .easeInOut(duration: plan.screens.fade ?? 0.15)
    }

    public var breathe: Animation? {
        guard let cycle = plan.breathe else { return nil }
        return .easeInOut(duration: cycle / 2).repeatForever(autoreverses: true)
    }

}

private struct MotionKey: EnvironmentKey {
    static let defaultValue = Motion(MotionPlan(reduceMotion: false))
}

private struct SharedSurfaceKey: EnvironmentKey {
    static let defaultValue = false
}

extension EnvironmentValues {
    public var sharedSurface: Bool {
        get { self[SharedSurfaceKey.self] }
        set { self[SharedSurfaceKey.self] = newValue }
    }
}

extension EnvironmentValues {
    public var menuMotion: Motion {
        get { self[MotionKey.self] }
        set { self[MotionKey.self] = newValue }
    }
}
