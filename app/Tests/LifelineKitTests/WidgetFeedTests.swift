import AppKit
import Foundation
import SwiftUI
import Testing
@testable import Lifeline
@testable import LifelineKit
import LifelineWidgetCore
@testable import LifelineWidgetViews

private final class Reloads: @unchecked Sendable {
    private let lock = NSLock()
    private var stored = 0

    var count: Int {
        get { lock.withLock { stored } }
        set { lock.withLock { stored = newValue } }
    }
}

private func folder() throws -> URL {
    let url = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("lifeline-widget-\(UUID().uuidString)", isDirectory: true)
    try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
    return url
}

@MainActor
@Suite(.serialized)
struct WidgetFeedTests {
    @Test func theSnapshotIsTheCoresOwnWords() throws {
        let state = try Fixture.state("moment-good")
        let text = try #require(state.widget)
        let mac = try #require(state.mac)
        let snapshot = try #require(WidgetFeed.snapshot(state, now: Date()))
        #expect(snapshot.smallLine == text.smallLine && !snapshot.smallLine.isEmpty)
        #expect(snapshot.headline == text.headline && !snapshot.headline.isEmpty)
        #expect(snapshot.sub == text.sub)
        #expect(snapshot.label == mac.viewLabel)
        #expect(snapshot.figure == mac.iconPct)
        let active = try #require(state.accounts?.first { $0.uuid == mac.activeUuid })
        #expect(snapshot.name == active.short)
        #expect(snapshot.rows.map(\.note) == (mac.rows ?? []).map { ($0.where ?? "").trimmingCharacters(in: .whitespaces) })
        #expect(snapshot.rows.map(\.figure) == (mac.rows ?? []).map { $0.figure ?? $0.pct ?? "" })
        #expect(snapshot.rows.contains { $0.best })
    }

    @Test func theSmallLineFollowsTheBoardForEachMoment() throws {
        #expect(try Fixture.state("moment-good").widget?.smallLine == "Lasts until its reset at 17:00.")
        #expect(try Fixture.state("moment-low").widget?.smallLine?.hasPrefix("Lasts until ") == true)
        #expect(try Fixture.state("moment-limit").widget?.smallLine?.hasPrefix("Out until ") == true)
        #expect(try Fixture.state("moment-good").widget?.sub?.hasPrefix("Studio uses ") == true)
    }

    @Test func theSnapshotSurvivesTheTripThroughTheFile() throws {
        let snapshot = try #require(WidgetFeed.snapshot(try Fixture.state("moment-low"), now: Date(timeIntervalSince1970: 1_800_000_000)))
        let back = try WidgetFeed.decode(try WidgetFeed.encode(snapshot))
        #expect(back == snapshot)
        let place = try folder()
        defer { try? FileManager.default.removeItem(at: place) }
        try WidgetFeed.encode(snapshot).write(to: WidgetFeed.file(in: place))
        #expect(WidgetFeed.read(from: place) == snapshot)
        #expect(WidgetFeed.read(from: place.appendingPathComponent("none")) == nil)
    }

    @Test func thePublisherWritesOnlyWhenWhatIsDrawnChanges() throws {
        let place = try folder()
        defer { try? FileManager.default.removeItem(at: place) }
        let reloads = Reloads()
        let publisher = WidgetPublisher(folder: place) { reloads.count += 1 }
        let good = try Fixture.state("moment-good")
        #expect(publisher.publish(good, now: Date(timeIntervalSince1970: 100)))
        #expect(!publisher.publish(good, now: Date(timeIntervalSince1970: 200)), "the same words a minute later are not a change")
        #expect(publisher.publish(try Fixture.state("moment-limit"), now: Date(timeIntervalSince1970: 300)))
        #expect(reloads.count == 2)
        #expect(WidgetFeed.read(from: place)?.smallLine == (try Fixture.state("moment-limit")).widget?.smallLine)
        #expect(!publisher.publish(nil))
    }

    @Test func aCopyNotSignedIntoTheGroupNeverTouchesTheContainer() {
        #expect(WidgetFeed.group == nil, "the test runner has no app group, so the app must not publish")
        #expect(WidgetPublisher.live() == nil)
    }

    @Test func everyFaceFitsItsSizeOnTheBoard() throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        Fonts.register(folder: URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Resources/Fonts", isDirectory: true))
        #expect(WidgetSize.small.size == CGSize(width: 170, height: 170))
        #expect(WidgetSize.medium.size == CGSize(width: 364, height: 170))
        #expect(WidgetSize.large.size == CGSize(width: 364, height: 382))
        for name in ["moment-good", "moment-low", "moment-limit", "moment-back", "extra-noroom", "extra-trouble", "extra-firstrun", "devbox-away"] {
            let snapshot = try #require(WidgetFeed.snapshot(try Fixture.state(name), now: Date()))
            for size in WidgetSize.allCases {
                for scheme in [ColorScheme.dark, .light] {
                    let face = WidgetFace(snapshot: snapshot, size: size).environment(\.colorScheme, scheme)
                    let host = NSHostingView(rootView: face.frame(width: size.size.width))
                    let natural = host.fittingSize
                    #expect(natural.height <= size.size.height + 0.5, "\(name) \(size) in \(scheme) needs \(natural.height) pt")
                }
            }
        }
    }
}
