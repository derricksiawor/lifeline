import AppKit
import LifelineKit
import SwiftUI

public struct MenuBarIconView: View {
    let fields: IconFields
    let palette: IconPalette

    public init(fields: IconFields, palette: IconPalette) {
        self.fields = fields
        self.palette = palette
    }

    /// The line's strength: full, or 45 percent while the reading is stale.
    nonisolated static func lineStrength(_ fields: IconFields) -> Double {
        fields.stale ? Metrics.unavailable : 1
    }

    public var body: some View {
        if fields.lineOnly {
            lineOnly
        } else {
            pill
        }
    }

    private var lineOnly: some View {
        ZStack(alignment: .topLeading) {
            ZStack(alignment: .leading) {
                Capsule().fill(palette.track).frame(width: 20, height: 3)
                Capsule().fill(palette.color(for: fields.tone)).frame(width: 20 * fields.fill, height: 3)
            }
            .frame(width: 20, height: 3, alignment: .leading)
            .clipShape(Capsule())
            .opacity(Self.lineStrength(fields))
            .offset(x: 1, y: 9)
            if fields.amberLight {
                Circle()
                    .fill(palette.amber)
                    .frame(width: 6, height: 6)
                    .shadow(color: palette.amberGlow, radius: 3)
                    .offset(x: 27, y: 7.5)
            }
        }
        .frame(width: fields.amberLight ? 36 : 22, height: 22, alignment: .topLeading)
        .fixedSize()
    }

    private var pill: some View {
        HStack(alignment: .center, spacing: 6) {
            if !fields.name.isEmpty {
                LineText(fields.name, Styles.iconName, palette.ink)
            }
            ZStack(alignment: .leading) {
                Capsule().fill(palette.track).frame(width: 20, height: 3)
                Capsule().fill(palette.color(for: fields.tone)).frame(width: 20 * fields.fill, height: 3)
            }
            .frame(width: 20, height: 3, alignment: .leading)
            .clipShape(Capsule())
            .opacity(Self.lineStrength(fields))
            if !fields.percent.isEmpty {
                LineText(fields.percent, Styles.iconPct, palette.ink)
            }
            if fields.amberLight {
                Circle()
                    .fill(palette.amber)
                    .frame(width: 6, height: 6)
                    .shadow(color: palette.amberGlow, radius: 3)
            }
        }
        .padding(.horizontal, 8)
        .frame(height: 22)
        .background(RoundedRectangle(cornerRadius: 6, style: .continuous).fill(palette.pill))
        .fixedSize()
    }
}

@MainActor
public enum MenuBarIcon {
    public static func image(fields: IconFields, appearance: String, scale: CGFloat? = nil) -> NSImage {
        let renderer = ImageRenderer(content: MenuBarIconView(fields: fields, palette: IconPalette.of(appearance)))
        renderer.scale = scale ?? NSScreen.main?.backingScaleFactor ?? 2
        let image = renderer.nsImage ?? NSImage(size: NSSize(width: 1, height: 22))
        image.isTemplate = false
        return image
    }
}
