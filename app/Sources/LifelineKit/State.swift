import Foundation

public struct MenuRow: Codable, Sendable, Equatable, Identifiable {
    public var uuid: String
    public var name: String?
    public var pct: String?
    public var weekPct: String?
    public var pctWindow: String?
    public var pctLabel: String?
    public var fiveW: String?
    public var foreW: String?
    public var weekW: String?
    public var tone: String?
    public var unused: Bool?
    public var isBest: Bool?
    public var isActive: Bool?
    public var needsSignin: Bool?
    public var `where`: String?
    public var binding: String?
    public var secondText: String?
    public var secondW: String?
    public var secondWindow: String?
    public var display: String?
    public var figure: String?
    public var thinWindow: String?
    public var thinW: String?
    public var thinLabel: String?
    public var stale: Bool?
    public var readNote: String?
    public var cannotRead: Bool?
    public var pendingHead: String?

    public var id: String { uuid }

    public var hasSecond: Bool {
        !(secondWindow ?? "").isEmpty && !(secondText ?? "").isEmpty
    }

    public var showsSecondLine: Bool {
        binding != "week" && !hasSecond
    }
}

public struct MenuModel: Codable, Sendable, Equatable {
    public var view: String?
    public var viewLabel: String?
    public var sessions: String?
    public var iconName: String?
    public var iconPct: String?
    public var iconW: String?
    public var iconTone: String?
    public var iconDot: Bool?
    public var devboxNote: String?
    public var countdownLeft: Int?
    public var unaddedKnown: UnaddedKnown?
    public var undoUntil: String?
    public var live: DevboxLive?
    public var heroName: String?
    public var heroPlan: String?
    public var head: String?
    public var sub: String?
    public var heroW: String?
    public var foreW: String?
    public var heroTone: String?
    public var heroUsed: String?
    public var heroReset: String?
    public var weekW: String?
    public var weekText: String?
    public var rows: [MenuRow]?
    public var actionKind: String?
    public var actionText: String?
    public var actionTarget: String?
    public var hint: String?
    public var countdown: Bool?
    public var binding: String?
    public var iconBinding: String?
    public var secondText: String?
    public var secondW: String?
    public var secondWindow: String?
    public var display: String?
    public var thinWindow: String?
    public var thinW: String?
    public var thinLabel: String?
    public var iconDisplay: String?
    public var pendingBack: String?
    public var iconLabel: String?

    public var hasSecond: Bool {
        !(secondWindow ?? "").isEmpty && !(secondText ?? "").isEmpty
    }

    public var showsWeekLine: Bool {
        binding != "week" && !hasSecond
    }

    public var activeUuid: String? {
        rows?.first { $0.isActive == true }?.uuid
    }
}

public struct ForecastSummary: Codable, Sendable, Equatable {
    public var atResetPct: Double?
    public var runsOutAt: String?
    public var pacePerMin: Double?
    public var basis: String?
}

public struct ModelLimit: Codable, Sendable, Equatable {
    public var model: String?
    public var week: Double?
    public var weekReset: String?
}

public struct ProjectSummary: Codable, Sendable, Equatable {
    public var cwd: String?
    public var name: String?
    public var startedAt: String?
}

public struct AccountSummary: Codable, Sendable, Equatable {
    public var uuid: String
    public var number: Int?
    public var name: String?
    public var short: String?
    public var needsSignin: Bool?
    public var five: Double?
    public var inUse: [String]?
    public var email: String?
    public var full: String?
    public var orgUuid: String?
    public var organization: String?
    public var organizationKind: String?
    public var detail: AccountDetail?
    public var signinText: String?
    public var devboxSignedIn: Bool?
    public var devboxAlready: String?
    public var plan: String?
    public var signinEnds: String?
    public var week: Double?
    public var fiveReset: String?
    public var weekReset: String?
    public var forecast: ForecastSummary?
    public var otherForecast: ForecastSummary?
    public var cannotRead: Bool?
    public var readingAt: String?
    public var devboxNeedsSignin: Bool?
    public var devboxSigninEnds: String?
    public var projects: [ProjectSummary]?
    public var binding: String?
    public var modelLimits: [ModelLimit]?
    public var addedText: AddedText?
    /// The confirm card's one sentence for Remove from Lifeline.
    public var removeText: String?
}

/// The paired computer: its name and address, and whether it answers. Name and address are nil until one is paired.
public struct DevboxSummary: Codable, Sendable, Equatable {
    public var name: String?
    public var address: String?
    public var reachable: Bool?
    public var answers: Bool?
    public var accountUuid: String?
    public var liveRegistered: Bool?
    public var canSignIn: Bool?
    public var live: DevboxLive?
}

public struct DevboxLive: Codable, Sendable, Equatable {
    public var uuid: String?
    public var added: Bool?
    public var known: Bool?
    public var name: String?
    public var short: String?
    public var email: String?
    public var organization: String?
    public var adoptReady: Bool?
    public var held: Bool?
    public var note: String?
    public var adoptTitle: String?
    public var bothTitle: String?
}

/// Every sentence on the add, waiting and naming screens, from the core. The four templates carry {name},
/// {menu}, {bar} or {seconds}, which the app fills with what was typed or how long it waited.
public struct AddText: Codable, Sendable, Equatable {
    public var title: String?
    public var lead: String?
    public var explain: String?
    public var twice: String?
    public var devboxAway: String?
    public var forDevbox: String?
    public var stays: String?
    public var pageNotOpened: String?
    public var pageNotOpenedAbove: String?
    public var failed: String?
    public var lost: String?
    public var cancelling: String?
    public var cancelled: String?
    public var namesShown: String?
    public var namesShownHidden: String?
    public var added: String?
    public var slowAnswer: String?
    public var unreadable: String?
}

/// The naming screen's second paragraph for each place the account was added to.
public struct AddedText: Codable, Sendable, Equatable {
    public var mac: String?
    public var both: String?
    public var devbox: String?
}

public struct SettingText: Codable, Sendable, Equatable {
    public var title: String?
    public var description: String?
    public var notes: [String: String]?
    public var note: String?
}

public struct PageText: Codable, Sendable, Equatable {
    public var title: String?
    public var lead: String?
}

/// Every sentence on Alerts and settings, from the core. Section labels stay in Swift.
public struct SettingsText: Codable, Sendable, Equatable {
    public var autoSwitch: SettingText?
    public var page: PageText?
    public var accountsNote: String?
    public var willRunOut: SettingText?
    public var limit: SettingText?
    public var back: SettingText?
    public var week: SettingText?
    public var signinEnds: SettingText?
    public var mac: SettingText?
    public var computer: SettingText?
    public var shortcut: SettingText?
    public var login: SettingText?
    public var showName: SettingText?
    public var statusline: SettingText?
    /// The confirm card's sentence for Reset Lifeline.
    public var reset: String?
}

public struct CountdownSummary: Codable, Sendable, Equatable {
    public var targetUuid: String?
    public var endsAt: String?
    public var seconds: Double?

    public func remaining(at now: Date) -> Double {
        let total = seconds ?? 5
        guard total > 0, let endsAt, let end = Timestamps.parse(endsAt) else {
            return 1
        }
        return min(max(end.timeIntervalSince(now) / total, 0), 1)
    }

    public func secondsLeft(at now: Date) -> Double {
        guard let endsAt, let end = Timestamps.parse(endsAt) else {
            return 0
        }
        return max(end.timeIntervalSince(now), 0)
    }
}

public enum Timestamps {
    public static func parse(_ text: String) -> Date? {
        let whole = ISO8601DateFormatter()
        whole.formatOptions = [.withInternetDateTime]
        if let date = whole.date(from: text) {
            return date
        }
        guard let dot = text.firstIndex(of: ".") else {
            return nil
        }
        let rest = text[text.index(after: dot)...]
        let digits = rest.prefix { $0.isNumber }
        guard !digits.isEmpty, let fraction = Double("0." + digits) else {
            return nil
        }
        let trimmed = String(text[..<dot]) + String(rest.dropFirst(digits.count))
        return whole.date(from: trimmed)?.addingTimeInterval(fraction)
    }
}

public struct JobSummary: Codable, Sendable, Equatable {
    public var id: String
    public var kind: String?
    public var uuid: String?
    public var state: String?
    public var result: String?
    public var url: String?
    public var opened: Bool?
    public var browser: String?
    public var suggestedName: String?
    public var suggestedShort: String?
    public var known: String?
    public var organization: String?
    public var guidance: String?
    public var startedAt: String?
    public var card: String?
    public var step: [Int]?
    /// The core's sentences for this job: its notification title once it has ended, its label, the waiting head,
    /// the compact card's words, the lines under the waiting box and on the small card, and its result split for the 20 pt line.
    public var title: String?
    public var compactTitle: String?
    public var label: String?
    public var waiting: String?
    public var compact: String?
    public var lines: [String]?
    public var cardLines: [String]?
    public var addedHead: String?
    public var resultHead: String?
    public var resultRest: String?
    public var alreadyHas: Bool?

    public init(
        id: String, kind: String? = nil, uuid: String? = nil, state: String? = nil, result: String? = nil, url: String? = nil,
        opened: Bool? = nil, browser: String? = nil, suggestedName: String? = nil, suggestedShort: String? = nil,
        known: String? = nil, organization: String? = nil, guidance: String? = nil
    ) {
        self.id = id
        self.kind = kind
        self.uuid = uuid
        self.state = state
        self.result = result
        self.url = url
        self.opened = opened
        self.browser = browser
        self.suggestedName = suggestedName
        self.suggestedShort = suggestedShort
        self.known = known
        self.organization = organization
        self.guidance = guidance
    }
}

public struct AccountDetail: Codable, Sendable, Equatable {
    public var usage: String?
    public var models: String?
    public var account: String?
    public var projects: String?

    public init(usage: String? = nil, models: String? = nil, account: String? = nil, projects: String? = nil) {
        self.usage = usage
        self.models = models
        self.account = account
        self.projects = projects
    }
}

public struct UnaddedKnown: Codable, Sendable, Equatable {
    public var name: String?
    public var short: String?
    public var email: String?

    public init(name: String? = nil, short: String? = nil, email: String? = nil) {
        self.name = name
        self.short = short
        self.email = email
    }
}

public struct KnownAccount: Codable, Sendable, Equatable, Identifiable {
    public var name: String
    public var short: String?
    public var full: String?
    public var email: String?
    public var organization: String?
    /// The core's line under the account in the picker, and its sign-in paragraph when it is picked.
    public var pickLine: String?
    public var explain: String?
    public var id: String { name }

    public var isPersonal: Bool {
        (organization ?? "").trimmingCharacters(in: .whitespaces).lowercased() == "personal"
    }

    public init(name: String, short: String? = nil, full: String? = nil, email: String? = nil, organization: String? = nil) {
        self.name = name
        self.short = short
        self.full = full
        self.email = email
        self.organization = organization
    }
}

public struct NoticeSummary: Codable, Sendable, Equatable {
    public var id: String
    public var kind: String?
    public var title: String?
    public var body: String?
    public var uuid: String?
    public var at: String?
    public var keep: Bool?

    public init(id: String, kind: String? = nil, title: String? = nil, body: String? = nil, uuid: String? = nil, at: String? = nil) {
        self.id = id
        self.kind = kind
        self.title = title
        self.body = body
        self.uuid = uuid
        self.at = at
    }
}

public struct ServiceState: Codable, Sendable, Equatable {
    public var version: Int?
    public var at: String?
    public var appearance: String?
    public var mac: MenuModel?
    public var dev: MenuModel?
    public var accounts: [AccountSummary]?
    public var devbox: DevboxSummary?
    public var countdown: CountdownSummary?
    public var jobs: [JobSummary]?
    public var notifications: [NoticeSummary]?
    public var settings: SettingsSummary?
    public var statusline: StatuslineSummary?
    public var knownAccounts: [KnownAccount]?
    public var signinBrowser: String?
    public var settingsText: SettingsText?
    public var addText: AddText?
    public var widget: WidgetText?
    public var switchNote: String?
    public var build: String?

    public static func decode(_ data: Data) throws -> ServiceState {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(ServiceState.self, from: data)
    }

    public func model(for view: MenuSide) -> MenuModel? {
        view == .mac ? mac : dev
    }
}

public enum Fraction {
    public static func of(_ text: String?) -> Double {
        guard let text else { return 0 }
        let digits = text.trimmingCharacters(in: .whitespaces).replacingOccurrences(of: "%", with: "")
        guard let value = Double(digits), value.isFinite else { return 0 }
        return min(max(value / 100, 0), 1)
    }
}

public struct IconFields: Sendable, Equatable {
    public var name: String
    public var percent: String
    public var fill: Double
    public var tone: String
    public var amberLight: Bool
    public var lineOnly: Bool
    /// The reading for the account in use is older than 10 minutes (the core's stale flag), so the line is dimmed.
    public var stale: Bool

    public init(name: String, percent: String, fill: Double, tone: String, amberLight: Bool, lineOnly: Bool = false, stale: Bool = false) {
        self.name = name
        self.percent = percent
        self.fill = fill
        self.tone = tone
        self.amberLight = amberLight
        self.lineOnly = lineOnly
        self.stale = stale
    }

    public func withFill(_ value: Double) -> IconFields {
        var copy = self
        copy.fill = min(max(value, 0), 1)
        return copy
    }

    public static func from(_ state: ServiceState?) -> IconFields {
        let lineOnly = state?.settings?.hideName ?? true
        guard let state, let mac = state.mac else {
            return IconFields(name: "Lifeline", percent: "", fill: 0, tone: "calm", amberLight: false, lineOnly: lineOnly)
        }
        let width = (mac.iconW ?? "4%").replacingOccurrences(of: "%", with: "")
        let fill = min(max((Double(width) ?? 4) / 100, 0), 1)
        return IconFields(
            name: mac.iconName ?? "",
            percent: mac.iconPct ?? "",
            fill: fill,
            tone: mac.iconTone ?? "calm",
            amberLight: mac.iconDot == true,
            lineOnly: lineOnly,
            stale: mac.rows?.first { $0.uuid == mac.activeUuid && !$0.uuid.isEmpty }?.stale == true
        )
    }

    /// The menu bar item's VoiceOver sentence, from the core's icon_label. The app keeps only its own sentence for the
    /// moment before any /state has arrived, and falls back to the core's hero head when an older core sends no label.
    public static func label(_ state: ServiceState?) -> String {
        guard let mac = state?.mac else {
            return "Lifeline is waiting for its service to answer."
        }
        let label = (mac.iconLabel ?? "").trimmingCharacters(in: .whitespaces)
        return label.isEmpty ? (mac.head ?? "") : label
    }
}

public struct Presence: Codable, Sendable, Equatable {
    public var idleSeconds: Double
    public var locked: Bool
    public var asleep: Bool

    public init(idleSeconds: Double, locked: Bool, asleep: Bool) {
        self.idleSeconds = idleSeconds
        self.locked = locked
        self.asleep = asleep
    }

    public func encoded() throws -> Data {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        encoder.outputFormatting = [.sortedKeys]
        return try encoder.encode(self)
    }
}
