import CoreText
import Foundation
import LifelineWidgetCore
import SwiftUI

/// The fonts the widgets draw with, registered once from a bundle's Resources/Fonts (the extension's own bundle).
public enum WidgetFonts {
    public static func register(folder: URL?) {
        guard let folder, let names = try? FileManager.default.contentsOfDirectory(atPath: folder.path) else { return }
        for name in names.sorted() where name.hasSuffix(".ttf") {
            CTFontManagerRegisterFontsForURL(folder.appendingPathComponent(name) as CFURL, .process, nil)
        }
    }

    static func words(_ size: CGFloat, weight: Int = 400) -> Font {
        let face: String
        switch weight {
        case 300: face = "Poppins-Light"
        case 500: face = "Poppins-Medium"
        case 600: face = "Poppins-SemiBold"
        default: face = "Poppins-Regular"
        }
        return .custom(face, fixedSize: size)
    }

    static func numbers(_ size: CGFloat, weight: Int = 400) -> Font {
        let base = CTFontCreateWithName("ChivoMono-Medium" as CFString, size, nil)
        let attributes = [kCTFontVariationAttribute: [0x7767_6874: weight]] as CFDictionary
        return Font(CTFontCreateCopyWithAttributes(base, size, nil, CTFontDescriptorCreateWithAttributes(attributes)))
    }
}

/// The widget's colours for dark, and the menu's light colours for light.
public struct WidgetPalette: Sendable {
    let top: Color
    let bottom: Color
    let ink: Color
    let text: Color
    let muted: Color
    let label: Color
    let track: Color
    let week: Color
    let tag: Color
    let tagInk: Color
    let calm: (Color, Color, Color)
    let low: (Color, Color, Color)
    let limit: (Color, Color, Color)

    public static func of(_ scheme: ColorScheme) -> WidgetPalette {
        scheme == .dark ? dark : light
    }

    static let dark = WidgetPalette(
        top: Color(red: 0x18 / 255, green: 0x1f / 255, blue: 0x37 / 255), bottom: Color(red: 0x14 / 255, green: 0x1a / 255, blue: 0x2e / 255),
        ink: Color(red: 0xee / 255, green: 0xf0 / 255, blue: 0xfa / 255), text: Color(red: 0x9a / 255, green: 0xa1 / 255, blue: 0xbd / 255),
        muted: Color(red: 0x9a / 255, green: 0xa1 / 255, blue: 0xbd / 255), label: Color(red: 0x8a / 255, green: 0x91 / 255, blue: 0xad / 255),
        track: Color(red: 0x25 / 255, green: 0x2c / 255, blue: 0x48 / 255), week: Color(red: 0x6a / 255, green: 0x64 / 255, blue: 0xc9 / 255),
        tag: Color(red: 109 / 255, green: 102 / 255, blue: 242 / 255).opacity(0.2), tagInk: Color(red: 0xc9 / 255, green: 0xc6 / 255, blue: 0xff / 255),
        calm: (Color(red: 0x5f / 255, green: 0x57 / 255, blue: 0xe8 / 255), Color(red: 0xaa / 255, green: 0xa5 / 255, blue: 0xff / 255), Color(red: 0xc9 / 255, green: 0xc6 / 255, blue: 0xff / 255)),
        low: (Color(red: 0xd9 / 255, green: 0x96 / 255, blue: 0x2a / 255), Color(red: 0xf6 / 255, green: 0xc7 / 255, blue: 0x6a / 255), Color(red: 0xf6 / 255, green: 0xc7 / 255, blue: 0x6a / 255)),
        limit: (Color(red: 0xe2 / 255, green: 0x55 / 255, blue: 0x3f / 255), Color(red: 0xff / 255, green: 0x9a / 255, blue: 0x8a / 255), Color(red: 0xff / 255, green: 0x9a / 255, blue: 0x8a / 255))
    )

    static let light = WidgetPalette(
        top: Color(red: 1, green: 1, blue: 1), bottom: Color(red: 0xf3 / 255, green: 0xf4 / 255, blue: 0xf9 / 255),
        ink: Color(red: 0x0b / 255, green: 0x12 / 255, blue: 0x20 / 255), text: Color(red: 0x2a / 255, green: 0x33 / 255, blue: 0x42 / 255),
        muted: Color(red: 0x5b / 255, green: 0x64 / 255, blue: 0x72 / 255), label: Color(red: 0x6b / 255, green: 0x72 / 255, blue: 0x80 / 255),
        track: Color(red: 0xe7 / 255, green: 0xea / 255, blue: 0xf1 / 255), week: Color(red: 0x98 / 255, green: 0x93 / 255, blue: 0xef / 255),
        tag: Color(red: 95 / 255, green: 87 / 255, blue: 232 / 255).opacity(0.12), tagInk: Color(red: 0x4a / 255, green: 0x43 / 255, blue: 0xd6 / 255),
        calm: (Color(red: 0x5f / 255, green: 0x57 / 255, blue: 0xe8 / 255), Color(red: 0x8b / 255, green: 0x84 / 255, blue: 0xf5 / 255), Color(red: 0x4a / 255, green: 0x43 / 255, blue: 0xd6 / 255)),
        low: (Color(red: 0xc4 / 255, green: 0x7f / 255, blue: 0x0e / 255), Color(red: 0xe0 / 255, green: 0xa4 / 255, blue: 0x32 / 255), Color(red: 0x87 / 255, green: 0x59 / 255, blue: 0x00 / 255)),
        limit: (Color(red: 0xd6 / 255, green: 0x3b / 255, blue: 0x27 / 255), Color(red: 0xf0 / 255, green: 0x70 / 255, blue: 0x5e / 255), Color(red: 0xc0 / 255, green: 0x29 / 255, blue: 0x1c / 255))
    )

    func tone(_ name: String) -> (start: Color, end: Color, ink: Color) {
        switch name {
        case "limit": return limit
        case "low": return low
        default: return calm
        }
    }

    public var background: LinearGradient {
        LinearGradient(colors: [top, bottom], startPoint: .top, endPoint: .bottom)
    }
}

/// The widget sizes, in points.
public enum WidgetSize: String, CaseIterable, Sendable {
    case small, medium, large

    public var size: CGSize {
        switch self {
        case .small: return CGSize(width: 170, height: 170)
        case .medium: return CGSize(width: 364, height: 170)
        case .large: return CGSize(width: 364, height: 382)
        }
    }
}

/// The stale strength, as the menu bar line.
let staleStrength = 0.45

struct WidgetLine: View {
    let fill: Double
    let ahead: Double
    let height: CGFloat
    let colors: (start: Color, end: Color, ink: Color)
    let track: Color

    var body: some View {
        GeometryReader { proxy in
            let width = proxy.size.width
            ZStack(alignment: .leading) {
                Capsule().fill(track)
                if ahead > 0 {
                    Capsule().fill(colors.end.opacity(0.24)).frame(width: width * min(fill + ahead, 1))
                }
                Capsule()
                    .fill(LinearGradient(colors: [colors.start, colors.end], startPoint: .leading, endPoint: .trailing))
                    .frame(width: width * min(max(fill, 0), 1))
            }
        }
        .frame(height: height)
        .clipShape(Capsule())
    }
}

/// What a face drew, for tests: how many account rows, whether the core's "more in the menu" line (and which), and
/// whether the stale line. Only the layout a face settles on reports, so a row that does not fit is not counted.
public struct WidgetDrawn: Equatable, Sendable {
    public var rows = 0
    public var more = false
    public var moreText = ""
    public var stale = false

    public init(rows: Int = 0, more: Bool = false, moreText: String = "", stale: Bool = false) {
        self.rows = rows
        self.more = more
        self.moreText = moreText
        self.stale = stale
    }
}

public struct WidgetDrawnKey: PreferenceKey {
    public static let defaultValue = WidgetDrawn()

    public static func reduce(value: inout WidgetDrawn, nextValue: () -> WidgetDrawn) {
        let next = nextValue()
        value.rows += next.rows
        value.more = value.more || next.more
        value.moreText = value.moreText.isEmpty ? next.moreText : value.moreText
        value.stale = value.stale || next.stale
    }
}

/// The face for each size. Every sentence comes from the snapshot, which carries the core's words.
public struct WidgetFace: View {
    let snapshot: WidgetSnapshot?
    let size: WidgetSize
    let now: Date
    /// The sentence for no snapshot yet: the one Swift keeps for a service that has not answered.
    public static let waiting = "Lifeline is waiting for its service to answer."
    @Environment(\.colorScheme) private var scheme

    public init(snapshot: WidgetSnapshot?, size: WidgetSize, now: Date = Date()) {
        self.snapshot = snapshot
        self.size = size
        self.now = now
    }

    public var body: some View {
        let palette = WidgetPalette.of(scheme)
        Group {
            if let snapshot {
                let old = snapshot.isStale(at: now) && !snapshot.staleLine.isEmpty
                switch size {
                case .small: small(snapshot, palette, old: old)
                case .medium: medium(snapshot, palette, old: old)
                case .large: large(snapshot, palette, old: old)
                }
            } else {
                Text(Self.waiting)
                    .font(WidgetFonts.words(12))
                    .foregroundStyle(palette.muted)
                    .padding(16)
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            }
        }
    }

    /// The core's stale sentence, drawn where the live line was once the app has stopped updating the widget.
    private func staleText(_ snapshot: WidgetSnapshot, _ palette: WidgetPalette, size: CGFloat) -> some View {
        Text(snapshot.staleLine)
            .font(WidgetFonts.words(size))
            .foregroundStyle(palette.muted)
            .fixedSize(horizontal: false, vertical: true)
            .preference(key: WidgetDrawnKey.self, value: WidgetDrawn(stale: true))
    }

    private func small(_ snapshot: WidgetSnapshot, _ palette: WidgetPalette, old: Bool) -> some View {
        let tone = palette.tone(snapshot.tone)
        return VStack(alignment: .leading, spacing: 0) {
            Group {
                Text(snapshot.label.uppercased())
                    .font(WidgetFonts.words(11, weight: 600))
                    .tracking(11 * 0.12)
                    .foregroundStyle(palette.label)
                    .lineLimit(1)
                Text(snapshot.name)
                    .font(WidgetFonts.words(14, weight: 500))
                    .foregroundStyle(palette.ink)
                    .lineLimit(1)
                Text(snapshot.figure)
                    .font(WidgetFonts.numbers(34, weight: 300))
                    .tracking(-34 * 0.04)
                    .foregroundStyle(tone.ink)
                    .lineLimit(1)
                    .minimumScaleFactor(0.6)
                    .padding(.top, 4)
                WidgetLine(fill: snapshot.fill, ahead: snapshot.ahead, height: 4, colors: tone, track: palette.track)
                    .opacity(snapshot.stale ? staleStrength : 1)
                    .padding(.vertical, 8)
            }
            .opacity(old ? staleStrength : 1)
            if old {
                staleText(snapshot, palette, size: 12)
            } else {
                Text(snapshot.smallLine)
                    .font(WidgetFonts.words(12))
                    .foregroundStyle(palette.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
        .padding(16)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    /// The core's line for the accounts with no room, under the rows that fit.
    private func moreText(_ snapshot: WidgetSnapshot, _ palette: WidgetPalette, shown: Int) -> some View {
        let line = snapshot.moreLine(leftOut: snapshot.rows.count - shown)
        return Text(line)
            .font(WidgetFonts.words(11))
            .foregroundStyle(palette.muted)
            .lineLimit(1)
            .preference(key: WidgetDrawnKey.self, value: WidgetDrawn(more: true, moreText: line))
    }

    /// Every count of rows from all of them down to one; the first that fits is drawn, with the core's line for the rest.
    private func counts(_ snapshot: WidgetSnapshot) -> [Int] {
        Array((1...max(snapshot.rows.count, 1)).reversed())
    }

    private func mediumRow(_ row: WidgetSnapshot.Row, _ palette: WidgetPalette) -> some View {
        let tone = palette.tone(row.tone)
        return VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .center, spacing: 6) {
                Text(row.name)
                    .font(WidgetFonts.words(12, weight: row.active ? 600 : 500))
                    .foregroundStyle(palette.ink)
                    .lineLimit(1)
                if row.best {
                    Text("Most room")
                        .font(WidgetFonts.words(10))
                        .foregroundStyle(palette.tagInk)
                        .padding(.horizontal, 6)
                        .background(RoundedRectangle(cornerRadius: 6, style: .continuous).fill(palette.tag))
                }
                Spacer(minLength: 6)
                Text(row.figure)
                    .font(WidgetFonts.numbers(12))
                    .foregroundStyle(tone.ink)
                    .lineLimit(1)
            }
            WidgetLine(fill: row.fill, ahead: 0, height: 3, colors: tone, track: palette.track)
                .opacity(row.stale ? staleStrength : 1)
        }
        .preference(key: WidgetDrawnKey.self, value: WidgetDrawn(rows: 1))
    }

    private func medium(_ snapshot: WidgetSnapshot, _ palette: WidgetPalette, old: Bool) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            if old {
                staleText(snapshot, palette, size: 11)
            }
            ViewThatFits(in: .vertical) {
                ForEach(counts(snapshot), id: \.self) { shown in
                    VStack(alignment: .leading, spacing: 0) {
                        ForEach(Array(snapshot.rows.prefix(shown).enumerated()), id: \.element.id) { item in
                            if item.offset > 0 {
                                Spacer(minLength: 4)
                            }
                            mediumRow(item.element, palette)
                        }
                        if shown < snapshot.rows.count {
                            Spacer(minLength: 4)
                            moreText(snapshot, palette, shown: shown)
                        }
                    }
                }
            }
            .opacity(old ? staleStrength : 1)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 14)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private func largeRow(_ row: WidgetSnapshot.Row, _ palette: WidgetPalette) -> some View {
        let tone = palette.tone(row.tone)
        return VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .firstTextBaseline) {
                Text(row.name)
                    .font(WidgetFonts.words(13, weight: 500))
                    .foregroundStyle(palette.ink)
                    .lineLimit(1)
                Spacer(minLength: 6)
                Text(row.figure)
                    .font(WidgetFonts.numbers(13))
                    .foregroundStyle(tone.ink)
                    .lineLimit(1)
            }
            Group {
                WidgetLine(fill: row.fill, ahead: 0, height: 3, colors: tone, track: palette.track)
                    .padding(.top, 6)
                    .padding(.bottom, 4)
                WidgetLine(fill: row.week, ahead: 0, height: 2, colors: (palette.week, palette.week, palette.week), track: palette.track)
                    .padding(.bottom, 6)
            }
            .opacity(row.stale ? staleStrength : 1)
            Text(row.note)
                .font(WidgetFonts.words(11))
                .foregroundStyle(palette.muted)
                .lineLimit(1)
        }
        .preference(key: WidgetDrawnKey.self, value: WidgetDrawn(rows: 1))
    }

    private func large(_ snapshot: WidgetSnapshot, _ palette: WidgetPalette, old: Bool) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 2) {
                Text(snapshot.headline)
                    .font(WidgetFonts.words(15, weight: 500))
                    .foregroundStyle(palette.ink)
                    .fixedSize(horizontal: false, vertical: true)
                    .opacity(old ? staleStrength : 1)
                if old {
                    staleText(snapshot, palette, size: 12)
                } else if !snapshot.sub.isEmpty {
                    Text(snapshot.sub)
                        .font(WidgetFonts.words(12))
                        .foregroundStyle(palette.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            // The board's 14 pt between rows assumes a one-line headline; the core's headline can take two, so the rows
            // sit 10 pt apart. Every account that fits is drawn; the rest are counted in the core's line.
            ViewThatFits(in: .vertical) {
                ForEach(counts(snapshot), id: \.self) { shown in
                    VStack(alignment: .leading, spacing: 10) {
                        ForEach(snapshot.rows.prefix(shown)) { row in
                            largeRow(row, palette)
                        }
                        if shown < snapshot.rows.count {
                            moreText(snapshot, palette, shown: shown)
                        }
                    }
                }
            }
            .opacity(old ? staleStrength : 1)
            Spacer(minLength: 0)
        }
        .padding(18)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}

/// The widget's background: a soft gradient, with continuous corners when drawn outside WidgetKit.
public struct WidgetBackground: View {
    @Environment(\.colorScheme) private var scheme

    public init() {}

    public var body: some View {
        WidgetPalette.of(scheme).background
    }
}
