import Foundation
@testable import LifelineKit

enum Fixture {
    static let alex = "11111111-aaaa-4aaa-8aaa-000000000001"
    static let work = "22222222-bbbb-4bbb-8bbb-000000000002"
    static let personal = "33333333-cccc-4ccc-8ccc-000000000003"
    static let org = "44444444-dddd-4ddd-8ddd-000000000004"
    static let names = [
        "devbox-away", "devbox-work-unadded", "devbox-work-step1", "devbox-work-step2", "devbox-work-done", "devbox-live-added", "devbox-live-held",
        "devbox-live-unready", "devbox-no-login", "devbox-gone-stale", "mac-live-unadded", "mac-live-unready", "add-elsewhere", "devbox-job-elsewhere", "failed-jobs",
        "job-cards", "after-mac-add-org", "after-mac-add-held", "devbox-work-no-login", "mac-unadded-no-login",
    ]

    static func data(_ name: String) throws -> Data {
        let folder = URL(fileURLWithPath: #filePath).deletingLastPathComponent().appendingPathComponent("CoreStates")
        return try Data(contentsOf: folder.appendingPathComponent(name + ".json"))
    }

    static func object(_ name: String, jobs: [[String: Any]]? = nil) throws -> [String: Any] {
        var object = try JSONSerialization.jsonObject(with: try data(name)) as? [String: Any] ?? [:]
        if let jobs {
            object["jobs"] = jobs
        }
        return object
    }

    static func text(_ name: String, jobs: [[String: Any]]? = nil) throws -> String {
        String(decoding: try JSONSerialization.data(withJSONObject: try object(name, jobs: jobs)), as: UTF8.self)
    }

    /// A hand-written state with the core's Settings and add words added, as the service always sends them.
    static func withSettingsText(_ text: String, from name: String = "moment-sheet") throws -> ServiceState {
        var object = try JSONSerialization.jsonObject(with: Data(text.utf8)) as? [String: Any] ?? [:]
        object["settings_text"] = try Self.object(name)["settings_text"]
        object["add_text"] = try Self.object(name)["add_text"]
        return try ServiceState.decode(try JSONSerialization.data(withJSONObject: object))
    }

    /// The core's add_text block as JSON, for hand-written test states (the service always sends it).
    static var addTextJSON: String {
        guard let block = try? object("moment-sheet")["add_text"], let data = try? JSONSerialization.data(withJSONObject: block, options: [.sortedKeys]) else {
            return "{}"
        }
        return String(decoding: data, as: UTF8.self)
    }

    static func state(_ name: String, jobs: [[String: Any]]? = nil) throws -> ServiceState {
        try ServiceState.decode(Data(try text(name, jobs: jobs).utf8))
    }

    static func jobs(_ name: String) throws -> [[String: Any]] {
        try object(name)["jobs"] as? [[String: Any]] ?? []
    }

    static func job(_ name: String, _ id: String) throws -> JobSummary {
        guard let found = try state(name).jobs?.first(where: { $0.id == id }) else {
            throw CocoaError(.fileReadNoSuchFile)
        }
        return found
    }
}
