import Foundation

public enum ServicePath {
    public static func resolve(_ value: String?, resources: String?) -> String? {
        guard let value, !value.isEmpty else { return nil }
        if value.hasPrefix("/") {
            return value
        }
        let parts = value.split(separator: "/", omittingEmptySubsequences: false)
        guard !parts.contains(".."), let resources, resources.hasPrefix("/") else { return nil }
        let base = (resources as NSString).standardizingPath
        let joined = ((base as NSString).appendingPathComponent(value) as NSString).standardizingPath
        guard joined.hasPrefix(base + "/"), joined.count > base.count + 1 else { return nil }
        return joined
    }
}
