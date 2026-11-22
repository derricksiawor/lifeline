import Foundation

public struct AlertSwitches: Codable, Sendable, Equatable {
    public var willRunOut: Bool?
    public var limit: Bool?
    public var back: Bool?
    public var week: Bool?
    public var signinEnds: Bool?

    public init(willRunOut: Bool? = nil, limit: Bool? = nil, back: Bool? = nil, week: Bool? = nil, signinEnds: Bool? = nil) {
        self.willRunOut = willRunOut
        self.limit = limit
        self.back = back
        self.week = week
        self.signinEnds = signinEnds
    }
}

public struct ChannelSwitches: Codable, Sendable, Equatable {
    public var mac: Bool?

    public init(mac: Bool? = nil) {
        self.mac = mac
    }
}

public struct SettingsSummary: Codable, Sendable, Equatable {
    public var autoSwitch: Bool?
    public var alerts: AlertSwitches?
    public var channels: ChannelSwitches?
    public var hideName: Bool?
    public var placement: [String: String]?

    public init(
        autoSwitch: Bool? = nil, alerts: AlertSwitches? = nil, channels: ChannelSwitches? = nil, hideName: Bool? = nil,
        placement: [String: String]? = nil
    ) {
        self.autoSwitch = autoSwitch
        self.alerts = alerts
        self.channels = channels
        self.hideName = hideName
        self.placement = placement
    }
}

public enum Placement {
    public static let both = "both"
    public static let mac = "mac"
    public static let devbox = "devbox"

    public static func of(_ uuid: String, in state: ServiceState?) -> String {
        let value = state?.settings?.placement?[uuid] ?? both
        return [both, mac, devbox].contains(value) ? value : both
    }

    public static func includes(_ value: String, chip: String) -> Bool {
        value == both || value == chip
    }

    public static func pressing(_ chip: String, on value: String) -> String? {
        let other = chip == mac ? devbox : mac
        let hasChip = includes(value, chip: chip)
        let hasOther = includes(value, chip: other)
        if hasChip {
            return hasOther ? other : nil
        }
        return hasOther ? both : chip
    }

    public static func body(_ uuid: String, _ value: String) -> [String: Any] {
        ["placement": [uuid: value]]
    }
}

public struct StatuslineSummary: Codable, Sendable, Equatable {
    public var on: Bool?
    public var ours: Bool?
    public var otherPresent: Bool?

    public init(on: Bool? = nil, ours: Bool? = nil, otherPresent: Bool? = nil) {
        self.on = on
        self.ours = ours
        self.otherPresent = otherPresent
    }
}

public enum SettingKey: String, Sendable, Equatable, CaseIterable {
    case willRunOut = "will_run_out"
    case limit
    case back
    case week
    case signinEnds = "signin_ends"
    case mac
    case autoSwitch = "auto_switch"
    case showName = "show_name"

    public var isAlert: Bool {
        [.willRunOut, .limit, .back, .week, .signinEnds].contains(self)
    }

    public var isChannel: Bool {
        self == .mac
    }

    public func body(_ on: Bool) -> [String: Any] {
        if isAlert {
            return ["alerts": [rawValue: on]]
        }
        if isChannel {
            return ["channels": [rawValue: on]]
        }
        if self == .showName {
            return ["hide_name": !on]
        }
        return [rawValue: on]
    }
}

public enum SettingControl: Sendable, Equatable {
    case toggle(SettingKey?, on: Bool, enabled: Bool)
    case placement(String, String)
    case placementLocked(String, String)
    case computer
    case keys
    case none
}

public struct SettingRow: Sendable, Equatable, Identifiable {
    public let id: String
    public let glyph: String
    public let title: String
    public let description: String
    public let control: SettingControl
    /// The core's note for a fact only this Mac knows, drawn under the description; each is at most two lines.
    public let note: String?

    public init(id: String, glyph: String, title: String, description: String, control: SettingControl, note: String? = nil) {
        self.id = id
        self.glyph = glyph
        self.title = title
        self.description = description
        self.control = control
        self.note = note
    }
}

public struct SettingSection: Sendable, Equatable, Identifiable {
    public let title: String
    public let rows: [SettingRow]
    public var note: String? = nil
    public var id: String { title }
}

public enum LoginState: Sendable, Equatable {
    case notInstalled
    case on
    case off
    case needsApproval
    case failed
}

public struct LocalStatus: Sendable, Equatable {
    public var notices: NoticePermission
    public var shortcut: ShortcutState
    public var login: LoginState

    public init(notices: NoticePermission, shortcut: ShortcutState, login: LoginState) {
        self.notices = notices
        self.shortcut = shortcut
        self.login = login
    }
}

public enum SettingsPage {
    /// The page's title when the core sends none yet.
    public static let title = "Alerts and settings"

    public static func pageTitle(_ state: ServiceState?) -> String {
        let found = (state?.settingsText?.page?.title ?? "").trimmingCharacters(in: .whitespaces)
        return found.isEmpty ? title : found
    }

    /// The core's lead; while the service does not answer, the one sentence Swift keeps for that.
    public static func lead(_ state: ServiceState?) -> String {
        guard let state else { return Sentences.notRunning }
        guard let text = state.settingsText else { return Sentences.unreadable }
        return text.page?.lead ?? ""
    }

    /// The note key the core uses for each fact only this Mac knows.
    public static func noteKey(_ notices: NoticePermission) -> String? {
        switch notices {
        case .unavailable: return "unavailable"
        case .banners: return "banners"
        case .listOnly: return "list_only"
        case .refused: return "refused"
        case .failed: return "failed"
        case .waiting, .allowed: return nil
        }
    }

    public static func noteKey(_ shortcut: ShortcutState) -> String? {
        switch shortcut {
        case .taken: return "taken"
        case .refused: return "refused"
        case .waiting, .on: return nil
        }
    }

    public static func noteKey(_ login: LoginState) -> String? {
        switch login {
        case .notInstalled: return "not_installed"
        case .needsApproval: return "needs_approval"
        case .failed: return "failed"
        case .on, .off: return nil
        }
    }

    /// No rows until the core sends their words: a row with no title, or a switch the service cannot take, is never shown.
    public static func sections(state: ServiceState?, local: LocalStatus) -> [SettingSection] {
        guard state?.settingsText != nil else { return [] }
        let settings = state?.settings
        let alerts = settings?.alerts
        let channels = settings?.channels
        let text = state?.settingsText
        let accounts = (state?.accounts ?? []).sorted { ($0.number ?? 99) < ($1.number ?? 99) }
        let devboxReady = JobFlow.devboxReady(state)
        let paired = state?.devbox?.name != nil
        let rows = accounts.map { account in
            let place = Placement.of(account.uuid, in: state)
            return SettingRow(
                id: "account-" + account.uuid,
                glyph: "",
                title: account.name ?? "",
                description: (account.detail?.account ?? "").trimmingCharacters(in: .whitespacesAndNewlines),
                control: !paired ? .none : devboxReady ? .placement(account.uuid, place) : .placementLocked(account.uuid, place)
            )
        }
        func on(_ value: Bool?) -> Bool { value ?? true }
        func noted(_ entry: SettingText?, _ key: String?) -> String? {
            key.flatMap { entry?.notes?[$0] }.flatMap { $0.isEmpty ? nil : $0 }
        }
        func row(_ id: String, _ glyph: String, _ entry: SettingText?, note: String? = nil, _ control: SettingControl) -> SettingRow {
            SettingRow(id: id, glyph: glyph, title: entry?.title ?? "", description: entry?.description ?? "", control: control, note: noted(entry, note))
        }
        let statusRow: SettingRow = {
            let entry = text?.statusline
            let control: SettingControl
            if let line = state?.statusline {
                if line.otherPresent == true {
                    control = .toggle(nil, on: false, enabled: false)
                } else if let current = line.on {
                    control = .toggle(nil, on: current, enabled: true)
                } else {
                    control = .none
                }
            } else {
                control = .none
            }
            let note = (entry?.note ?? "").isEmpty ? nil : entry?.note
            return SettingRow(id: "statusline", glyph: Mark.statusline, title: entry?.title ?? "", description: entry?.description ?? "", control: control, note: note)
        }()
        let loginControl: SettingControl = {
            switch local.login {
            case .notInstalled: return .toggle(nil, on: false, enabled: false)
            case .on, .needsApproval: return .toggle(nil, on: true, enabled: true)
            case .off, .failed: return .toggle(nil, on: false, enabled: true)
            }
        }()
        let sections = [
            SettingSection(title: "Your accounts", rows: rows, note: text?.accountsNote),
            SettingSection(title: "Alerts", rows: [
                row("will_run_out", Mark.willRunOut, text?.willRunOut, .toggle(.willRunOut, on: on(alerts?.willRunOut), enabled: true)),
                row("limit", Mark.limit, text?.limit, .toggle(.limit, on: on(alerts?.limit), enabled: true)),
                row("back", Mark.back, text?.back, .toggle(.back, on: on(alerts?.back), enabled: true)),
                row("week", Mark.week, text?.week, .toggle(.week, on: on(alerts?.week), enabled: true)),
                row("signin_ends", Mark.signinEnds, text?.signinEnds, .toggle(.signinEnds, on: on(alerts?.signinEnds), enabled: true))
            ]),
            SettingSection(title: "Where alerts go", rows: [
                row("mac", Mark.mac, text?.mac, note: noteKey(local.notices), .toggle(.mac, on: on(channels?.mac), enabled: true))
            ]),
            SettingSection(title: "Switching", rows: [
                row("auto_switch", Mark.autoSwitch, text?.autoSwitch, .toggle(.autoSwitch, on: on(settings?.autoSwitch), enabled: true)),
                row("shortcut", Mark.shortcut, text?.shortcut, note: noteKey(local.shortcut), .keys)
            ]),
            SettingSection(title: "Another computer", rows: [
                row("computer", Mark.computer, text?.computer, .computer)
            ]),
            SettingSection(title: "General", rows: [
                row("login", Mark.login, text?.login, note: noteKey(local.login), loginControl),
                row("show_name", Mark.showName, text?.showName, .toggle(.showName, on: !(settings?.hideName ?? true), enabled: true)),
                statusRow
            ])
        ]
        // A row the core sent no words for is left out, switch and all; a section left with no rows goes too.
        return sections.compactMap { section in
            let worded = section.rows.filter { !$0.title.isEmpty && ($0.id.hasPrefix("account-") || !$0.description.isEmpty) }
            return worded.isEmpty ? nil : SettingSection(title: section.title, rows: worded, note: section.note)
        }
    }
}

public enum Mark {
    public static let willRunOut = "chart.line.uptrend.xyaxis"
    public static let limit = "exclamationmark.circle"
    public static let back = "arrow.uturn.backward.circle"
    public static let week = "calendar"
    public static let signinEnds = "key"
    public static let mac = "laptopcomputer"
    public static let computer = "server.rack"
    public static let autoSwitch = "arrow.left.arrow.right"
    public static let shortcut = "keyboard"
    public static let login = "power"
    public static let showName = "menubar.rectangle"
    public static let statusline = "terminal"
}
