import Foundation

public enum APIError: Error, Equatable {
    case status(Int)
    case unreadable
}

public enum PostOutcome: Sendable, Equatable {
    case done(job: String?, message: String?)
    case refused(String)
}

public struct LifelineAPI: Sendable {
    public static let switchLimit: TimeInterval = 30

    public let client: SocketClient
    public let switchLimit: TimeInterval

    public init(client: SocketClient, switchLimit: TimeInterval = LifelineAPI.switchLimit) {
        self.client = client
        self.switchLimit = switchLimit
    }

    public func limit(for command: MenuCommand) -> TimeInterval? {
        switch command {
        case .switchTo, .undo, .cancel:
            return switchLimit
        case .resign:
            return nil
        }
    }

    public func fetchState(appearance: String) throws -> ServiceState {
        let name = appearance == "light" ? "light" : "dark"
        let reply = try client.request(method: "GET", target: "/state?appearance=\(name)")
        guard reply.status == 200 else {
            throw APIError.status(reply.status)
        }
        do {
            return try ServiceState.decode(reply.body)
        } catch {
            throw APIError.unreadable
        }
    }

    public func postPresence(_ presence: Presence) throws {
        let reply = try client.request(method: "POST", target: "/presence", body: try presence.encoded())
        guard reply.status == 200 else {
            throw APIError.status(reply.status)
        }
    }

    public func answers() -> Bool {
        client.canConnect()
    }

    public static func safe(_ uuid: String) -> String {
        uuid.addingPercentEncoding(withAllowedCharacters: .alphanumerics.union(CharacterSet(charactersIn: "-"))) ?? uuid
    }

    public func fetchHistory(uuid: String, days: Int = 7) throws -> HistoryReply {
        let reply = try client.request(method: "GET", target: "/history/\(Self.safe(uuid))?days=\(days)")
        guard reply.status == 200 else {
            throw APIError.status(reply.status)
        }
        do {
            return try HistoryReply.decode(reply.body)
        } catch {
            throw APIError.unreadable
        }
    }

    public func post(_ target: String, object: [String: Any]) -> PostOutcome {
        guard let body = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]) else {
            return .refused(Sentences.unreadable)
        }
        return post(target, body: body)
    }

    public func post(_ target: String, body: Data?, limit: TimeInterval? = nil) -> PostOutcome {
        let reply: SocketReply
        do {
            reply = try client.request(method: "POST", target: target, body: body, limit: limit)
        } catch SocketError.timedOut {
            return .refused(limit == AddFlow.startLimit ? Sentences.slowStart : Sentences.timedOut)
        } catch SocketError.cannotConnect, SocketError.pathTooLong {
            return .refused(Sentences.notRunning)
        } catch {
            return .refused(Sentences.unreadable)
        }
        let object = (try? JSONSerialization.jsonObject(with: reply.body)) as? [String: Any]
        guard (200..<300).contains(reply.status) else {
            if let sentence = object?["error"] as? String, !sentence.isEmpty {
                return .refused(sentence)
            }
            return .refused(Sentences.unreadable)
        }
        return .done(job: object?["job"] as? String, message: object?["message"] as? String)
    }

    public func change(_ key: SettingKey, on: Bool) -> PostOutcome {
        post("/settings", object: key.body(on))
    }

    public func statusline(on: Bool) -> PostOutcome {
        post("/statusline", object: ["on": on])
    }

    public func place(_ uuid: String, _ value: String) -> PostOutcome {
        post("/settings", object: Placement.body(uuid, value))
    }

    /// Pairs with another computer (an address and the code it showed), or forgets it when address is nil.
    public func pair(address: String?, code: String) -> PostOutcome {
        guard let address else { return post("/computer/forget", body: nil) }
        let body = try? JSONSerialization.data(withJSONObject: ["address": address, "code": code], options: [.sortedKeys])
        return post("/computer", body: body, limit: AddFlow.startLimit)
    }

    public func rename(uuid: String, body: [String: String]) -> String? {
        guard !body.isEmpty else { return nil }
        switch post("/rename/\(Self.safe(uuid))", object: body) {
        case .done:
            return nil
        case .refused(let sentence):
            return sentence
        }
    }

    public func perform(_ command: MenuCommand, via: Via, names: [String: String] = [:]) -> ActionOutcome {
        let target: String
        var fields: [String: String]?
        var toName: String?
        switch command {
        case .switchTo(let uuid):
            target = "/switch"
            fields = ["uuid": uuid, "via": via.rawValue]
            toName = names[uuid]
        case .undo:
            target = "/undo"
            fields = ["via": via.rawValue]
        case .cancel:
            target = "/cancel"
        case .resign(let uuid):
            target = "/resign/\(Self.safe(uuid))"
        }
        let body = fields.flatMap { try? JSONSerialization.data(withJSONObject: $0, options: [.sortedKeys]) }
        let reply: SocketReply
        let custom = limit(for: command)
        do {
            reply = try client.request(method: "POST", target: target, body: body, limit: custom)
        } catch SocketError.timedOut {
            return .refused(custom == nil ? Sentences.timedOut : Sentences.slowSwitch)
        } catch SocketError.cannotConnect, SocketError.pathTooLong {
            return .refused(Sentences.notRunning)
        } catch {
            return .refused(Sentences.unreadable)
        }
        let object = (try? JSONSerialization.jsonObject(with: reply.body)) as? [String: Any]
        guard (200..<300).contains(reply.status) else {
            if let sentence = object?["error"] as? String, !sentence.isEmpty {
                return .refused(sentence)
            }
            return .refused(Sentences.unreadable)
        }
        if let job = object?["job"] as? String, !job.isEmpty {
            return .accepted(job: job)
        }
        if case .undo = command, let to = object?["to_uuid"] as? String {
            toName = names[to]
        }
        let caveat = MenuLogic.switchNote(message: object?["message"] as? String, toName: toName)
        return .done(note: MenuLogic.joinNotes(caveat, object?["note"] as? String))
    }
}
