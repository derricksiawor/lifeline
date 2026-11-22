import Foundation

public struct NoticeAction: Sendable, Equatable {
    public let command: MenuCommand
    public let title: String

    public init(command: MenuCommand, title: String) {
        self.command = command
        self.title = title
    }
}

public struct Notice: Sendable, Equatable {
    public let id: String
    public let title: String
    public let body: String
    public let account: String?
    public let action: NoticeAction?

    public init(id: String, title: String, body: String, account: String?, action: NoticeAction?) {
        self.id = id
        self.title = title
        self.body = body
        self.account = account
        self.action = action
    }
}

public struct ShownMenu: Sendable, Equatable {
    public var texts: [String]
    public var justSwitchedTo: String?

    public init(texts: [String], justSwitchedTo: String? = nil) {
        self.texts = texts
        self.justSwitchedTo = justSwitchedTo
    }

    public init(model: MenuModel?, note: String?) {
        texts = [model?.head, model?.sub, note].compactMap { text in
            guard let text, !text.isEmpty else { return nil }
            return text
        }
        let name = model?.heroName ?? ""
        justSwitchedTo = model?.actionKind == "undo" && !name.isEmpty ? name : nil
    }
}

public struct NoticePlan: Sendable, Equatable {
    public var post: [Notice]
    public var quiet: [String]

    public init(post: [Notice] = [], quiet: [String] = []) {
        self.post = post
        self.quiet = quiet
    }
}

public enum NoticePermission: Sendable, Equatable {
    case unavailable
    case waiting
    case allowed
    case banners
    case listOnly
    case refused
    case failed

    public var posts: Bool {
        self == .allowed || self == .banners || self == .listOnly || self == .failed
    }

    public static func from(authorized: Bool, denied: Bool, style: NoticeStyle) -> NoticePermission {
        if denied {
            return .refused
        }
        guard authorized else { return .waiting }
        switch style {
        case .alert: return .allowed
        case .banner: return .banners
        case .none: return .listOnly
        }
    }
}

public enum NoticeStyle: Sendable, Equatable {
    case none
    case banner
    case alert
}

public struct NoticeLedger: Sendable, Equatable {
    public private(set) var seen: [String: Date]
    public private(set) var sending: Set<String> = []

    public init(seen: [String: Date], now: Date) {
        self.seen = Notices.prune(seen, now: now)
    }

    public mutating func take(state: ServiceState, shown: ShownMenu?, now: Date) -> NoticePlan {
        let plan = Notices.plan(state: state, seen: Set(seen.keys).union(sending), shown: shown, now: now)
        for id in plan.quiet {
            seen[id] = now
        }
        for notice in plan.post {
            sending.insert(notice.id)
        }
        seen = Notices.prune(seen, now: now)
        return plan
    }

    public mutating func sent(_ id: String, at: Date) {
        sending.remove(id)
        seen[id] = at
    }

    public mutating func failed(_ id: String) {
        sending.remove(id)
    }
}

public enum Notices {
    public static let freshFor: TimeInterval = 30 * 60
    public static let keepFor: TimeInterval = 48 * 60 * 60
    public static let later = "Later"
    public static let cancelTitle = "Cancel the switch"
    public static let signInTitle = "Sign in again"
    public static let primaryAction = "go"
    public static let laterAction = "later"
    public static let laterRoute = "/notifications/later"
    static let macSignIn = "signin-ends:mac:"

    public static func plan(state: ServiceState, seen: Set<String>, shown: ShownMenu?, now: Date) -> NoticePlan {
        var plan = NoticePlan()
        var taken = seen
        let ordered = (state.notifications ?? []).enumerated().sorted { left, right in
            let a = left.element.at.flatMap(Timestamps.parse) ?? .distantPast
            let b = right.element.at.flatMap(Timestamps.parse) ?? .distantPast
            return a == b ? left.offset < right.offset : a < b
        }
        for (_, notice) in ordered {
            guard !notice.id.isEmpty, !taken.contains(notice.id) else { continue }
            taken.insert(notice.id)
            let title = (notice.title ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            if title.isEmpty || !isFresh(notice, now: now) {
                plan.quiet.append(notice.id)
                continue
            }
            if let shown, duplicates(notice, shown: shown) {
                plan.quiet.append(notice.id)
                continue
            }
            plan.post.append(
                Notice(
                    id: notice.id,
                    title: title,
                    body: (notice.body ?? "").trimmingCharacters(in: .whitespacesAndNewlines),
                    account: notice.uuid,
                    action: action(for: notice, state: state)
                )
            )
        }
        return plan
    }

    public static func isFresh(_ notice: NoticeSummary, now: Date) -> Bool {
        guard let text = notice.at, let at = Timestamps.parse(text) else { return true }
        return now.timeIntervalSince(at) <= freshFor
    }

    public static func sentences(_ text: String) -> [String] {
        var found: [String] = []
        var current = ""
        let characters = Array(text)
        for (index, character) in characters.enumerated() {
            current.append(character)
            let ends = ".!?".contains(character) && (index + 1 == characters.count || characters[index + 1] == " ")
            if ends {
                let piece = current.trimmingCharacters(in: .whitespacesAndNewlines)
                if !piece.isEmpty {
                    found.append(piece)
                }
                current = ""
            }
        }
        let rest = current.trimmingCharacters(in: .whitespacesAndNewlines)
        if !rest.isEmpty {
            found.append(rest)
        }
        return found
    }

    public static func duplicates(_ notice: NoticeSummary, shown: ShownMenu) -> Bool {
        let title = (notice.title ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        guard !title.isEmpty else { return false }
        let sentence = title.hasSuffix(".") ? title : title + "."
        if shown.texts.flatMap(sentences).contains(sentence) {
            return true
        }
        if notice.kind == "report", let name = shown.justSwitchedTo, title.contains("this Mac to \(name)") {
            return true
        }
        return false
    }

    public static func action(for notice: NoticeSummary, state: ServiceState) -> NoticeAction? {
        guard let kind = notice.kind, let uuid = notice.uuid, !uuid.isEmpty else { return nil }
        let mac = state.mac
        let active = mac?.activeUuid
        switch kind {
        case "will_run_out", "limit", "week":
            guard uuid == active else { return nil }
            if kind == "limit", state.countdown != nil || mac?.countdown == true {
                return NoticeAction(command: .cancel, title: cancelTitle)
            }
            guard let best = mac?.rows?.first(where: { $0.isBest == true && $0.uuid != uuid && $0.needsSignin != true }),
                  let name = name(of: best.uuid, in: state) else {
                return nil
            }
            return NoticeAction(command: .switchTo(best.uuid), title: "Switch to \(name)")
        case "back":
            guard uuid != active, let name = name(of: uuid, in: state) else { return nil }
            return NoticeAction(command: .switchTo(uuid), title: backTitle(name))
        case "signin_ends":
            guard notice.id.hasPrefix(macSignIn) else { return nil }
            return NoticeAction(command: .resign(uuid), title: signInTitle)
        default:
            return nil
        }
    }

    public static func name(of uuid: String, in state: ServiceState) -> String? {
        let fromAccounts = state.accounts?.first { $0.uuid == uuid }?.name
        let fromRows = state.mac?.rows?.first { $0.uuid == uuid }?.name
        guard let name = fromAccounts ?? fromRows, !name.isEmpty else { return nil }
        return name
    }

    public static func short(of uuid: String, in state: ServiceState) -> String? {
        if let short = state.accounts?.first(where: { $0.uuid == uuid })?.short, !short.isEmpty {
            return short
        }
        return name(of: uuid, in: state)
    }

    public static func backTitle(_ name: String) -> String {
        "Switch back to " + name
    }

    public static func countdownGone(_ state: ServiceState) -> Bool {
        state.countdown == nil && state.mac?.countdown != true
    }

    public static func refusal(_ sentence: String, id: String) -> Notice {
        Notice(id: id, title: sentence, body: "", account: nil, action: nil)
    }

    public static func titles(for state: ServiceState) -> [String] {
        var titles = [cancelTitle, signInTitle]
        let uuids = (state.accounts ?? []).map(\.uuid) + (state.mac?.rows ?? []).map(\.uuid)
        for uuid in uuids {
            if let name = name(of: uuid, in: state) {
                titles.append("Switch to \(name)")
            }
            if let name = name(of: uuid, in: state) {
                titles.append(backTitle(name))
            }
        }
        var seen = Set<String>()
        return titles.filter { seen.insert($0).inserted }
    }

    public static func category(for title: String) -> String {
        "go:" + title
    }

    public static func userInfo(for command: MenuCommand) -> [String: String] {
        switch command {
        case .switchTo(let uuid):
            return ["command": "switch", "uuid": uuid]
        case .undo:
            return ["command": "undo"]
        case .cancel:
            return ["command": "cancel"]
        case .resign(let uuid):
            return ["command": "resign", "uuid": uuid]
        }
    }

    public static func command(from info: [String: String]) -> MenuCommand? {
        let uuid = info["uuid"] ?? ""
        switch info["command"] {
        case "switch":
            return uuid.isEmpty ? nil : .switchTo(uuid)
        case "undo":
            return .undo
        case "cancel":
            return .cancel
        case "resign":
            return uuid.isEmpty ? nil : .resign(uuid)
        default:
            return nil
        }
    }

    public static func prune(_ seen: [String: Date], now: Date) -> [String: Date] {
        seen.filter { now.timeIntervalSince($0.value) <= keepFor }
    }
}
