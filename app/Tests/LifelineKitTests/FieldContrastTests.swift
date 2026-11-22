import AppKit
import SwiftUI
import Testing
@testable import Lifeline
@testable import LifelineKit

private func srgb(_ color: Color) -> (Double, Double, Double) {
    let found = NSColor(color).usingColorSpace(.sRGB) ?? .black
    return (Double(found.redComponent), Double(found.greenComponent), Double(found.blueComponent))
}

private func over(_ top: Color, _ alpha: Double, _ base: Color) -> (Double, Double, Double) {
    let a = srgb(top)
    let b = srgb(base)
    return (a.0 * alpha + b.0 * (1 - alpha), a.1 * alpha + b.1 * (1 - alpha), a.2 * alpha + b.2 * (1 - alpha))
}

private func luminance(_ rgb: (Double, Double, Double)) -> Double {
    func lin(_ c: Double) -> Double { c <= 0.04045 ? c / 12.92 : pow((c + 0.055) / 1.055, 2.4) }
    return 0.2126 * lin(rgb.0) + 0.7152 * lin(rgb.1) + 0.0722 * lin(rgb.2)
}

private func ratio(_ a: (Double, Double, Double), _ b: (Double, Double, Double)) -> Double {
    let x = luminance(a)
    let y = luminance(b)
    return (max(x, y) + 0.05) / (min(x, y) + 0.05)
}

private func lightness(_ rgb: (Double, Double, Double)) -> Double {
    let y = luminance(rgb)
    return y > 216.0 / 24389.0 ? 116 * pow(y, 1.0 / 3.0) - 16 : y * 24389.0 / 27.0
}

@MainActor
private func pixel(_ view: some View, x: CGFloat, fromBottom: CGFloat) -> (Double, Double, Double)? {
    let renderer = ImageRenderer(content: view.environment(\.colorScheme, .light))
    renderer.scale = 1
    guard let image = renderer.cgImage else { return nil }
    let rep = NSBitmapImageRep(cgImage: image)
    guard let color = rep.colorAt(x: Int(x), y: rep.pixelsHigh - Int(fromBottom))?.usingColorSpace(.sRGB) else { return nil }
    return (Double(color.redComponent), Double(color.greenComponent), Double(color.blueComponent))
}

@MainActor
struct ClipProbeTests {
    @Test func theListOffsetFollowsTheRealScroll() async {
        let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 200, height: 100))
        let document = FlippedDocument(frame: NSRect(x: 0, y: 0, width: 200, height: 600))
        scroll.documentView = document
        let probe = ClipProbe(frame: NSRect(x: 0, y: 0, width: 10, height: 10))
        var seen: [CGFloat] = []
        probe.changed = { seen.append($0) }
        document.addSubview(probe)
        probe.attach()
        scroll.contentView.scroll(to: NSPoint(x: 0, y: 140))
        scroll.contentView.scroll(to: NSPoint(x: 0, y: 260))
        await waitFor { seen.last == -260 }
        #expect(seen.last == -260, "\(seen)")
        #expect(seen.contains(-140), "\(seen)")
    }
}

private final class FlippedDocument: NSView {
    override var isFlipped: Bool { true }
}

@MainActor
struct SoleActionTests {
    @Test func theOnlyActionOnASheetIsPrimary() throws {
        let palette = Palette.of(.light)
        let sheet = SheetPalette.of(.light)
        let done = DoneStep(sentence: "Lifeline added Alex's Account.", handlers: AddHandlers(), palette: palette, sheet: sheet, width: 320)
            .padding(20)
            .background(palette.panelTop)
        let found = try #require(pixel(done, x: 34, fromBottom: 20 + Metrics.actionHeight / 2))
        func gap(_ color: Color) -> Double {
            let other = srgb(color)
            return abs(found.0 - other.0) + abs(found.1 - other.1) + abs(found.2 - other.2)
        }
        #expect(gap(palette.primary) < gap(sheet.raised) && gap(palette.primary) < gap(sheet.quiet), "\(found)")
        #expect(found.2 - found.0 > 0.3, "\(found)")
    }
}

struct FieldContrastTests {
    @Test func textFieldsSitAClearStepDeeperThanTheirCard() {
        for scheme in [ColorScheme.light, .dark] {
            let palette = Palette.of(scheme)
            let sheet = SheetPalette.of(scheme)
            let field = srgb(sheet.input)
            let openActiveRow = srgb(palette.rowActive)
            let openRow = over(palette.rowActive, palette.openRow, palette.panelTop)
            let sheetTop = srgb(palette.panelTop)
            let sheetBottom = srgb(palette.panelBottom)
            for (place, card) in [("open row in use", openActiveRow), ("open row", openRow)] {
                #expect(ratio(field, card) >= (scheme == .light ? 1.3 : 1.19), "\(scheme) \(place) \(ratio(field, card))")
                #expect(lightness(card) - lightness(field) >= (scheme == .light ? 10 : 8.5), "\(scheme) \(place) \(lightness(card) - lightness(field))")
            }
            for card in [sheetTop, sheetBottom] {
                #expect(ratio(field, card) >= (scheme == .light ? 1.4 : 1.1), "\(scheme) sheet \(ratio(field, card))")
                #expect(lightness(card) - lightness(field) >= (scheme == .light ? 13 : 5), "\(scheme) sheet \(lightness(card) - lightness(field))")
            }
            #expect(ratio(field, srgb(palette.ink)) >= 7, "\(scheme) ink \(ratio(field, srgb(palette.ink)))")
        }
    }
}
