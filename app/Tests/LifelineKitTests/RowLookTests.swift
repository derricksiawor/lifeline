import AppKit
import Foundation
import SwiftUI
import Testing
@testable import Lifeline
@testable import LifelineKit

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
    let x = width - 6
    let y = height / 2
    return Double(data[(y * width + x) * 4 + 3]) / 255
}

@MainActor
@Suite(.serialized)
struct CardFillTests {
    @Test func menuCardsKeepTheirOwnFillApartFromTheOpenRow() throws {
        #expect(Palette.light.cardFill == 1)
        #expect(Palette.dark.cardFill == 0.3)
        #expect(Palette.light.openRow == 0.55, "an open row is still lightened in light")
        let notice = NoticeSummary(id: "n", kind: "limit", title: "Alex's Account reached its 5-hour limit", body: "")
        let failed = try Fixture.job("failed-jobs", "fa")
        let running = try Fixture.job("job-cards", "k-add")
        for scheme in [ColorScheme.light, .dark] {
            let palette = Palette.of(scheme)
            let cards: [(String, AnyView)] = [
                ("alert", AnyView(AlertCard(notice: notice, palette: palette))),
                ("result", AnyView(ResultCard(job: failed, palette: palette))),
                ("waiting", AnyView(WaitingCard(job: running, palette: palette, actions: CardActions())))
            ]
            for (name, card) in cards {
                let alpha = alphaInside(card, scheme: scheme)
                #expect(abs(alpha - palette.cardFill) < 0.03, "the \(name) card in \(scheme) is filled at \(alpha), not \(palette.cardFill)")
            }
        }
    }
}

@MainActor
@Suite(.serialized)
struct StaleRowWordsTests {
    @Test func aStaleRowSaysWhenItWasLastRead() throws {
        let row = try #require(try Fixture.state("extra-trouble").mac?.rows?.first { $0.uuid == Fixture.personal })
        let note = try #require(row.readNote)
        try #require(row.stale == true && !note.isEmpty)
        let drawn = drawnLines(AccountRow(row: row, old: nil, fade: .settled, palette: .dark, look: .plain(row), animated: false, action: nil))
        #expect(drawn.contains(note), "the stale row hides why it is dim: \(drawn)")
        let fresh = try #require(try Fixture.state("extra-trouble").mac?.rows?.first { $0.uuid == Fixture.alex })
        let calm = drawnLines(AccountRow(row: fresh, old: nil, fade: .settled, palette: .dark, look: .plain(fresh), animated: false, action: nil))
        #expect(!calm.contains { $0.hasPrefix("Last read") }, "\(calm)")
    }
}

@MainActor
struct TroubleNeverSwitchesTests {
    @Test func anUnreadableOrSignedOutRowSignsInAgainByClickAndByKey() throws {
        let state = try Fixture.state("extra-trouble")
        let rows = try #require(state.mac?.rows)
        let work = try #require(rows.first { $0.uuid == Fixture.work })
        let org = try #require(rows.first { $0.uuid == Fixture.org })
        #expect(work.cannotRead == true)
        for row in [work, org] {
            #expect(MenuLogic.rowClick(row, side: .mac, secondary: false) == .run(.resign(row.uuid)), "\(row.name ?? "")")
            #expect(MenuLogic.rowCommand(row, side: .mac) == .resign(row.uuid))
        }
        for (uuid, number) in [(Fixture.work, 2), (Fixture.org, 4)] {
            #expect(MenuLogic.keyCommand(digit: number, state: state) == .resign(uuid), "key \(number) still switches")
        }
        #expect(MenuLogic.keyCommand(digit: 3, state: state) == .switchTo(Fixture.personal))
        #expect(MenuLogic.rowClick(try #require(rows.first { $0.uuid == Fixture.personal }), side: .mac, secondary: false) == .run(.switchTo(Fixture.personal)))
    }
}

@MainActor
struct LightClearOfTextTests {
    @Test func theTravellingLightRidesTheLinesAndClearsTheText() {
        let nameBottom = AccountRow.padding + Styles.rowName.lineHeight
        let whereTop = AccountRow.lineY + 1.5 + 5 + 2 + 8
        let reach = Metrics.orbHeight / 2 + Metrics.orbBlur
        #expect(AccountRow.lineY - reach >= nameBottom, "the light reaches the name line")
        #expect(AccountRow.lineY + reach <= whereTop, "the light reaches the where line")
        let from = CGRect(x: 0, y: 0, width: 372, height: 85)
        let to = CGRect(x: 0, y: 178, width: 372, height: 85)
        #expect(MenuView.orbY(from: from, to: to, travel: 0) == AccountRow.lineY)
        #expect(MenuView.orbY(from: from, to: to, travel: 1) == 178 + AccountRow.lineY)
    }
}
