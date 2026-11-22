import Foundation
import LifelineWidgetCore

/// The core's widget block.
public struct WidgetText: Codable, Sendable, Equatable {
    public var smallLine: String?
    public var headline: String?
    public var sub: String?
    public var staleLine: String?
    public var more: [String]?
}

extension WidgetFeed {
    /// The widgets' snapshot of this Mac's view, all from the core: the view label, the account in use and its menu bar
    /// figure and line, the core's three widget sentences, and each row's name, figure, lines and where line.
    public static func snapshot(_ state: ServiceState, now: Date) -> WidgetSnapshot? {
        guard let mac = state.mac else { return nil }
        let text = state.widget
        let active = mac.activeUuid ?? ""
        let account = state.accounts?.first { $0.uuid == active }
        let names: [String?] = [account?.short, account?.name, mac.heroName]
        let shortName: String = names.compactMap { $0?.trimmingCharacters(in: .whitespaces) }.first { !$0.isEmpty } ?? ""
        let rows: [WidgetSnapshot.Row] = (mac.rows ?? []).map { row -> WidgetSnapshot.Row in
            let figure: String = row.figure ?? row.pct ?? ""
            let note: String = (row.where ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            let fill: Double = Fraction.of(row.fiveW)
            let week: Double = Fraction.of(row.weekW)
            return WidgetSnapshot.Row(
                id: row.uuid, name: row.name ?? "", figure: figure, fill: fill, week: week, tone: row.tone ?? "calm", note: note,
                best: row.isBest == true, active: row.isActive == true, stale: row.stale == true
            )
        }
        return WidgetSnapshot(
            written: now,
            label: mac.viewLabel ?? "",
            name: shortName,
            figure: mac.iconPct ?? "",
            fill: Fraction.of(mac.heroW),
            ahead: Fraction.of(mac.foreW),
            tone: mac.heroTone ?? "calm",
            stale: rows.first { $0.id == active && !active.isEmpty }?.stale == true,
            smallLine: (text?.smallLine ?? "").trimmingCharacters(in: .whitespacesAndNewlines),
            headline: (text?.headline ?? "").trimmingCharacters(in: .whitespacesAndNewlines),
            sub: (text?.sub ?? "").trimmingCharacters(in: .whitespacesAndNewlines),
            staleLine: (text?.staleLine ?? "").trimmingCharacters(in: .whitespacesAndNewlines),
            more: text?.more ?? [],
            rows: rows
        )
    }
}
