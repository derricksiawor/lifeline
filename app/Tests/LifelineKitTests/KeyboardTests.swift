import AppKit
import Foundation
import SwiftUI
import Testing
@testable import Lifeline
@testable import LifelineKit

private let escape: UInt16 = 53
private let returnKey: UInt16 = 36
private let enter: UInt16 = 76
private let up: UInt16 = 126
private let down: UInt16 = 125
private let right: UInt16 = 124
private let zKey: UInt16 = 6

private func key(
    _ code: UInt16, _ state: ServiceState?, characters: String? = nil, command: Bool = false, detailOpen: Bool = false, side: MenuSide = .mac,
    highlight: String? = nil, undoShows: Bool = true
) -> KeyAction {
    MenuLogic.keyAction(
        keyCode: code, characters: characters, shift: false, command: command, option: false, control: false, state: state, detailOpen: detailOpen,
        side: side, highlight: highlight, undoShows: undoShows
    )
}

@MainActor
private func alphaInside(_ view: some View, scheme: ColorScheme) -> Double {
    let renderer = ImageRenderer(content: view.environment(\.colorScheme, scheme).environment(\.stillFields, true)
        .environment(\.menuMotion, Motion(MotionPlan(reduceMotion: true))))
    renderer.scale = 1
    guard let image = renderer.cgImage else { return -1 }
    let width = image.width
    let height = image.height
    var data = [UInt8](repeating: 0, count: width * height * 4)
    data.withUnsafeMutableBytes { buffer in
        guard let context = CGContext(
            data: buffer.baseAddress, width: width, height: height, bitsPerComponent: 8, bytesPerRow: width * 4,
            space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        ) else { return }
        context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
    }
    return Double(data[((height / 2) * width + width - 6) * 4 + 3]) / 255
}

/// The menu drawn in an offscreen window, as raw pixels, so two states can be compared.
@MainActor
private func menuPixels(_ view: some View) -> [UInt8] {
    NSApplication.shared.setActivationPolicy(.prohibited)
    let host = NSHostingView(rootView: view.environment(\.colorScheme, .light).environment(\.menuMotion, Motion(MotionPlan(reduceMotion: true))))
    host.frame = NSRect(origin: .zero, size: host.fittingSize)
    let window = NSWindow(contentRect: NSRect(x: -30000, y: -30000, width: host.frame.width, height: host.frame.height), styleMask: [.borderless], backing: .buffered, defer: false)
    window.isReleasedWhenClosed = false
    window.contentView = host
    defer { window.contentView = nil }
    for _ in 0..<20 {
        host.layoutSubtreeIfNeeded()
        RunLoop.main.run(until: Date().addingTimeInterval(0.01))
    }
    guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { return [] }
    host.cacheDisplay(in: host.bounds, to: rep)
    guard let data = rep.bitmapData else { return [] }
    return Array(UnsafeBufferPointer(start: data, count: rep.bytesPerRow * rep.pixelsHigh))
}

@MainActor
@Suite(.serialized)
struct KeyboardTests {
    @Test func theMenuDrawsTheHighlightOnItsRow() throws {
        let state = try Fixture.state("moment-menu")
        let lit = try #require(state.mac?.rows?.first { $0.isActive != true }?.uuid)
        let plain = menuPixels(MenuView(state: state, side: .mac, note: nil, maxHeight: 879))
        let keyed = menuPixels(MenuView(state: state, side: .mac, note: nil, maxHeight: 879, highlight: lit))
        #expect(!plain.isEmpty && plain.count == keyed.count)
        let changed = zip(plain, keyed).filter { $0 != $1 }.count
        #expect(changed > 1000, "the highlighted row looks the same as before: \(changed) bytes differ")
    }

    @Test func escapeDuringTheCountdownCancelsTheSwitchAndKeepsTheMenuOpen() throws {
        let counting = try Fixture.state("moment-auto")
        #expect(MenuLogic.showsCountdown(counting.mac, side: .mac))
        #expect(key(escape, counting) == .command(.cancel))
        #expect(key(escape, counting, detailOpen: true) == .command(.cancel), "the countdown comes before an open row")
        #expect(key(escape, counting, side: .dev) == .close, "Studio view has no countdown")
        let calm = try Fixture.state("moment-menu")
        #expect(key(escape, calm) == .close)
        #expect(key(escape, calm, detailOpen: true) == .detail(.close))
    }

    @Test func commandZUndoesOnlyWhileUndoShows() throws {
        let switched = try Fixture.state("moment-switched")
        #expect(key(zKey, switched, characters: "z", command: true) == .command(.undo))
        #expect(key(zKey, switched, characters: "z", command: true, undoShows: false) == .ignore)
        #expect(key(zKey, switched, characters: "z", command: true, side: .dev) == .ignore)
        #expect(key(zKey, try Fixture.state("moment-menu"), characters: "z", command: true) == .ignore)
        #expect(key(zKey, switched, characters: "z") == .ignore, "z alone does nothing")
    }

    @Test func upAndDownMoveAHighlightBetweenRows() throws {
        let state = try Fixture.state("moment-menu")
        let ids = try #require(state.mac?.rows?.map(\.uuid))
        #expect(key(down, state) == .highlight(ids[0]))
        #expect(key(up, state) == .highlight(ids[ids.count - 1]))
        #expect(key(down, state, highlight: ids[0]) == .highlight(ids[1]))
        #expect(key(up, state, highlight: ids[1]) == .highlight(ids[0]))
        #expect(key(down, state, highlight: ids[ids.count - 1]) == .highlight(ids[ids.count - 1]), "it stops at the last row, as the open row does")
        #expect(key(down, state, detailOpen: true) == .detail(.next), "with a row open, the arrows move the open row")
        #expect(key(down, try Fixture.state("extra-firstrun")) == .ignore, "no rows, nothing to highlight")
    }

    @Test func returnSwitchesToTheHighlightedRowAndRightOpensIt() throws {
        let state = try Fixture.state("moment-menu")
        let rows = try #require(state.mac?.rows)
        let other = try #require(rows.first { $0.isActive != true })
        let active = try #require(rows.first { $0.isActive == true })
        #expect(key(returnKey, state, highlight: other.uuid) == .command(.switchTo(other.uuid)))
        #expect(key(enter, state, highlight: other.uuid) == .command(.switchTo(other.uuid)))
        #expect(key(returnKey, state, highlight: active.uuid) == .ignore, "the row in use is already in use")
        #expect(key(returnKey, state, side: .dev, highlight: other.uuid) == .ignore, "Studio view never switches")
        #expect(key(right, state, highlight: other.uuid) == .openRow(other.uuid))
        #expect(key(right, state) == .detail(.open))
        let trouble = try Fixture.state("extra-trouble")
        let unreadable = try #require(trouble.mac?.rows?.first { $0.cannotRead == true })
        #expect(key(returnKey, trouble, highlight: unreadable.uuid) == .command(.resign(unreadable.uuid)), "a row that cannot be read signs in again")
    }

    @Test func returnWithNoHighlightPressesTheFootersMainButton() throws {
        let limit = try Fixture.state("moment-limit")
        let target = try #require(limit.mac?.actionTarget)
        #expect(key(returnKey, limit) == .command(.switchTo(target)))
        #expect(key(returnKey, try Fixture.state("moment-switched")) == .command(.undo))
        #expect(key(returnKey, try Fixture.state("moment-auto")) == .command(.cancel))
        let first = try Fixture.state("extra-firstrun")
        #expect(key(returnKey, first) == .heroAdd(try #require(MenuLogic.heroAdd(first.mac, side: .mac))))
        #expect(key(returnKey, try Fixture.state("moment-menu")) == .ignore, "no main button, nothing to press")
    }

    @Test func theHighlightedRowTakesTheRowFillAtSixtyPercentWithNoOutline() throws {
        let row = try #require(try Fixture.state("moment-menu").mac?.rows?.first { $0.isActive != true })
        #expect(AccountRow.fillStrength(look: 0, open: false, highlighted: true, palette: .light) == 0.6)
        #expect(AccountRow.fillStrength(look: 0, open: false, highlighted: false, palette: .light) == 0)
        #expect(AccountRow.fillStrength(look: 1, open: false, highlighted: true, palette: .light) == 1, "the row in use keeps its own fill")
        for scheme in [ColorScheme.light, .dark] {
            let palette = Palette.of(scheme)
            let lit = alphaInside(
                AccountRow(row: row, old: nil, fade: .settled, palette: palette, look: .plain(row), animated: false, highlighted: true, action: nil), scheme: scheme
            )
            let plain = alphaInside(AccountRow(row: row, old: nil, fade: .settled, palette: palette, look: .plain(row), animated: false, action: nil), scheme: scheme)
            #expect(abs(lit - 0.6) < 0.03, "\(scheme): \(lit)")
            #expect(plain < 0.03, "\(scheme): \(plain)")
        }
    }
}
