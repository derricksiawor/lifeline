import AppKit
import Foundation
import SwiftUI
import Testing
@testable import Lifeline
@testable import LifelineKit

private let calm = LocalStatus(notices: .allowed, shortcut: .on, login: .on)

private func row(_ id: String, in sections: [SettingSection]) -> SettingRow? {
    sections.flatMap(\.rows).first { $0.id == id }
}

@MainActor
@Suite(.serialized)
struct SettingsFromTheCoreTests {
    init() {
        Fonts.register(folder: URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Resources/Fonts", isDirectory: true))
    }

    @Test func everySettingsSentenceIsTheCoresOwn() throws {
        let state = try Fixture.state("moment-sheet")
        let text = try #require(state.settingsText)
        let sections = SettingsPage.sections(state: state, local: calm)
        let entries: [(String, SettingText?)] = [
            ("will_run_out", text.willRunOut), ("limit", text.limit), ("back", text.back), ("week", text.week), ("signin_ends", text.signinEnds),
            ("mac", text.mac), ("computer", text.computer), ("auto_switch", text.autoSwitch),
            ("shortcut", text.shortcut), ("login", text.login), ("show_name", text.showName)
        ]
        for (id, entry) in entries {
            let entry = try #require(entry, "the core sent no \(id)")
            let found = try #require(row(id, in: sections), "no \(id) row")
            #expect(found.title == entry.title, "\(id): \(found.title)")
            #expect(found.description == entry.description, "\(id): \(found.description)")
        }
        let status = try #require(row("statusline", in: sections))
        #expect(status.title == text.statusline?.title)
        #expect(status.description.hasPrefix(text.statusline?.description ?? "-"))
        for account in state.accounts ?? [] {
            #expect(row("account-" + account.uuid, in: sections)?.description == account.detail?.account, "\(account.name ?? "")")
        }
        #expect(sections[0].note == text.accountsNote)
        #expect(SettingsPage.lead(state) == text.page?.lead)
        #expect(SettingsPage.pageTitle(state) == text.page?.title)
    }

    @Test func notesForFactsOnlyThisMacKnowsComeFromTheCore() throws {
        let state = try Fixture.state("moment-sheet")
        let text = try #require(state.settingsText)
        let cases: [(LocalStatus, String, String)] = [
            (LocalStatus(notices: .banners, shortcut: .on, login: .on), "mac", "banners"),
            (LocalStatus(notices: .listOnly, shortcut: .on, login: .on), "mac", "list_only"),
            (LocalStatus(notices: .refused, shortcut: .on, login: .on), "mac", "refused"),
            (LocalStatus(notices: .allowed, shortcut: .taken, login: .on), "shortcut", "taken"),
            (LocalStatus(notices: .allowed, shortcut: .on, login: .notInstalled), "login", "not_installed"),
            (LocalStatus(notices: .allowed, shortcut: .on, login: .needsApproval), "login", "needs_approval")
        ]
        for (local, id, key) in cases {
            let entry = id == "mac" ? text.mac : (id == "shortcut" ? text.shortcut : text.login)
            let note = try #require(entry?.notes?[key], "the core sent no \(id) note \(key)")
            let found = try #require(row(id, in: SettingsPage.sections(state: state, local: local)))
            #expect(found.note == note, "\(id) \(key): \(found.note ?? "no note")")
            #expect(found.description == entry?.description, "the note is its own line, not joined to the description")
        }
        #expect(row("mac", in: SettingsPage.sections(state: state, local: calm))?.note == nil)
    }

    @Test func theDrawnPageStartsEachNoteOnItsOwnLine() throws {
        let state = try Fixture.state("moment-sheet")
        let note = try #require(state.settingsText?.mac?.notes?["banners"])
        let local = LocalStatus(notices: .banners, shortcut: .on, login: .on)
        let view = SettingsView(state: state, local: local, problem: nil, width: CGFloat(SheetSizing.settingsDefault.width))
        let lines = drawnLines(view.environment(\.colorScheme, .light)) { $0.count > 10 }
        let start = note.split(separator: " ").prefix(4).joined(separator: " ")
        #expect(lines.contains { $0.hasPrefix(start) }, "\(lines)")
        let quiet = drawnLines(SettingsView(state: state, local: calm, problem: nil, width: 560).environment(\.colorScheme, .light)) { $0.count > 10 }
        #expect(!quiet.contains { $0.contains("Banners") }, "a note shows while notifications are fine")
    }

    @Test func theDevboxAwayLineShowsOnceInItsRow() throws {
        let away = try Fixture.state("moment-sheet-away")
        let sections = SettingsPage.sections(state: away, local: calm)
        let computer = sections.flatMap(\.rows).first { $0.id == "computer" }?.description ?? ""
        #expect(computer.contains("not answering"), "\(computer)")
        for section in sections {
            #expect(!(section.note ?? "").contains("not answering"), "\(section.title) repeats the lead: \(section.note ?? "")")
        }
    }

    @Test func titlesSitOnOneLineAndDescriptionsTakeTwoAtTheDefaultSize() throws {
        #expect(SheetSizing.settingsDefault == SheetSize(width: 560, height: 520))
        let state = try Fixture.state("moment-sheet")
        let width = CGFloat(SheetSizing.settingsDefault.width) - 2 * Metrics.sheetPadding - 12
        for local in [calm, LocalStatus(notices: .banners, shortcut: .taken, login: .needsApproval)] {
            for found in SettingsPage.sections(state: state, local: local).flatMap(\.rows) {
                let room = SettingRowView.titleRoom(found, width: width)
                let lines = Styles.sub.lines(found.description, width: SettingRowView.inner(width)).count
                let noteLines = Styles.sub.lines(found.note ?? "", width: SettingRowView.inner(width)).count
                #expect(Styles.rowTitle.width(of: found.title) <= room, "\(found.id) wraps its title: \(found.title)")
                #expect(lines <= 2, "\(found.id) takes \(lines) lines: \(found.description)")
                #expect(noteLines <= 2, "\(found.id)'s note takes \(noteLines) lines: \(found.note ?? "")")
            }
        }
    }

    @Test func anUnavailableChipIsQuietNotHalfPressed() {
        for scheme in [ColorScheme.light, .dark] {
            let palette = Palette.of(scheme)
            let sheet = SheetPalette.of(scheme)
            let look = SettingRowView.chipLook(away: true, pressed: true, palette: palette, sheet: sheet)
            #expect(look.fill == sheet.quiet)
            #expect(look.ink == palette.faint)
            #expect(look.strength == 1)
            let live = SettingRowView.chipLook(away: false, pressed: true, palette: palette, sheet: sheet)
            #expect(live.fill == palette.primary && live.ink == palette.primaryInk)
        }
    }
}
