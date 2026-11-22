import CoreText
import LifelineKit
import SwiftUI

public struct TextStyle: Sendable {
    public enum Family: Sendable {
        case words
        case numbers
    }

    public let family: Family
    public let size: CGFloat
    public let weight: Int
    public let lineHeight: CGFloat
    public let trackingEm: CGFloat
    public let uppercase: Bool

    public init(_ family: Family, _ size: CGFloat, weight: Int = 400, line: CGFloat, tracking: CGFloat = 0, uppercase: Bool = false) {
        self.family = family
        self.size = size
        self.weight = weight
        self.lineHeight = line
        self.trackingEm = tracking
        self.uppercase = uppercase
    }

    public var ctFont: CTFont {
        family == .words ? Fonts.wordsFont(size, weight: weight) : Fonts.numbersFont(size, weight: weight)
    }

    public var font: Font {
        Font(ctFont)
    }

    public var tracking: CGFloat {
        trackingEm * size
    }

    public func baseline(scale: CGFloat) -> CGFloat {
        let face = ctFont
        let unit = max(scale, 1)
        let ascent = (CTFontGetAscent(face) * unit).rounded()
        let descent = (CTFontGetDescent(face) * unit).rounded()
        let line = lineHeight * unit
        return (((line - ascent - descent) / 2).rounded(.down) + ascent) / unit
    }

    func shaped(_ text: String) -> String {
        uppercase ? text.uppercased() : text
    }

    func attributed(_ text: String) -> NSAttributedString {
        var attributes: [NSAttributedString.Key: Any] = [.font: ctFont as Any]
        if tracking != 0 {
            attributes[NSAttributedString.Key(kCTKernAttributeName as String)] = tracking
        }
        return NSAttributedString(string: shaped(text), attributes: attributes)
    }

    public func width(of text: String) -> CGFloat {
        let line = CTLineCreateWithAttributedString(attributed(text))
        return CGFloat(CTLineGetTypographicBounds(line, nil, nil, nil))
    }

    public func lines(_ text: String, width: CGFloat, keep names: [String] = []) -> [String] {
        let joined = fitted(Unbreakable.apply(text, keep: names), width: width)
        let shapedText = shaped(joined)
        guard !shapedText.isEmpty else { return [] }
        let source = attributed(joined)
        let typesetter = CTTypesetterCreateWithAttributedString(source)
        let whole = shapedText as NSString
        var result: [String] = []
        var start = 0
        while start < whole.length {
            var count = CTTypesetterSuggestLineBreak(typesetter, start, Double(width))
            if count <= 0 {
                count = whole.length - start
            }
            let piece = whole.substring(with: NSRange(location: start, length: count))
            result.append(piece.trimmingCharacters(in: .whitespaces))
            start += count
        }
        return noWidow(result, limit: width, names: names)
    }

    /// A joined name, date or time that is wider than the line falls back to ordinary spaces, so no word is ever cut.
    func fitted(_ joined: String, width: CGFloat) -> String {
        guard joined.contains(Unbreakable.space) else { return joined }
        return joined.components(separatedBy: " ").map { token in
            token.contains(Unbreakable.space) && self.width(of: token) > width
                ? token.replacingOccurrences(of: Unbreakable.space, with: " ") : token
        }.joined(separator: " ")
    }

    /// A last line of one word or one joined time pulls a word down from the line above. A last line that is a whole
    /// account name stays where it fell, so "This Mac now uses" keeps its last word.
    func noWidow(_ lines: [String], limit: CGFloat, names: [String] = []) -> [String] {
        guard lines.count >= 2, let last = lines.last, !last.contains(" ") else { return lines }
        let plain = last.replacingOccurrences(of: Unbreakable.space, with: " ").trimmingCharacters(in: CharacterSet(charactersIn: ".,;:!?"))
        if plain.contains(" "), names.contains(plain) {
            return lines
        }
        let previous = lines[lines.count - 2]
        guard let cut = previous.range(of: " ", options: .backwards) else { return lines }
        let moved = String(previous[cut.upperBound...]) + " " + last
        guard width(of: moved) <= limit else { return lines }
        var result = lines
        result[result.count - 2] = String(previous[..<cut.lowerBound])
        result[result.count - 1] = moved
        return result
    }
}

struct LineText: View {
    let text: String
    let style: TextStyle
    let color: Color
    @Environment(\.displayScale) private var scale
    @Environment(\.reportsText) private var reports

    init(_ text: String, _ style: TextStyle, _ color: Color) {
        self.text = text
        self.style = style
        self.color = color
    }

    var body: some View {
        let baseline = style.baseline(scale: scale)
        Text(style.shaped(text))
            .font(style.font)
            .tracking(style.tracking)
            .foregroundStyle(color)
            .lineLimit(1)
            .fixedSize()
            .alignmentGuide(.lineTop) { d in d[.firstTextBaseline] - baseline }
            .frame(width: style.width(of: text), height: style.lineHeight, alignment: Alignment(horizontal: .leading, vertical: .lineTop))
            .preference(key: DrawnText.self, value: reports ? [DrawnLine(text: style.shaped(text), mono: style.family == .numbers)] : [])
    }
}

/// Every line of text a view draws, in drawing order, when the reportsText environment value is on. Tests read what was
/// typeset from this instead of reading pixels back.
struct DrawnLine: Equatable, Sendable {
    let text: String
    let mono: Bool
}

struct DrawnText: PreferenceKey {
    static let defaultValue: [DrawnLine] = []

    static func reduce(value: inout [DrawnLine], nextValue: () -> [DrawnLine]) {
        value += nextValue()
    }
}

private struct ReportsTextKey: EnvironmentKey {
    static let defaultValue = false
}

extension EnvironmentValues {
    var reportsText: Bool {
        get { self[ReportsTextKey.self] }
        set { self[ReportsTextKey.self] = newValue }
    }
}

extension VerticalAlignment {
    private enum LineTop: AlignmentID {
        static func defaultValue(in dimensions: ViewDimensions) -> CGFloat {
            dimensions[.top]
        }
    }

    static let lineTop = VerticalAlignment(LineTop.self)
}

struct Paragraph: View {
    let text: String
    let style: TextStyle
    let color: Color
    let width: CGFloat
    let centered: Bool
    @Environment(\.keptNames) private var names

    init(_ text: String, _ style: TextStyle, _ color: Color, width: CGFloat, centered: Bool = false) {
        self.text = text
        self.style = style
        self.color = color
        self.width = width
        self.centered = centered
    }

    var body: some View {
        let pieces = style.lines(text, width: width, keep: names)
        VStack(alignment: centered ? .center : .leading, spacing: 0) {
            ForEach(Array(pieces.enumerated()), id: \.offset) { item in
                LineText(item.element, style, color)
            }
        }
        .frame(width: width, alignment: centered ? .top : .topLeading)
    }
}

public enum Unbreakable {
    public static let space = "\u{00A0}"
    static let months = "January|February|March|April|May|June|July|August|September|October|November|December"
    static let joins: [(pattern: String, template: String)] = [
        (#"\b(\d{1,2}) ("# + months + #")\b"#, "$1\u{00A0}$2"),
        (#"\b("# + months + #") (\d{1,2})\b"#, "$1\u{00A0}$2"),
        (#"\bat (\d{1,2}:\d{2})"#, "at\u{00A0}$1"),
        (#"\b(\d+) (hours?|minutes?|seconds?|days?|weeks?)\b"#, "$1\u{00A0}$2")
    ]

    public static func names(_ state: ServiceState?) -> [String] {
        guard let state else { return [] }
        var found: [String?] = []
        for account in state.accounts ?? [] {
            found += [account.name, account.short, account.full]
        }
        for known in state.knownAccounts ?? [] {
            found += [known.name, known.short, known.full]
        }
        for live in [state.devbox?.live, state.mac?.live, state.dev?.live] {
            found += [live?.name, live?.short]
        }
        for job in state.jobs ?? [] {
            found += [job.known, job.suggestedName, job.suggestedShort]
        }
        for model in [state.mac, state.dev] {
            found.append(model?.heroName)
            found += (model?.rows ?? []).map(\.name)
        }
        var seen = Set<String>()
        return found.compactMap { $0?.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty && seen.insert($0).inserted }
            .sorted { $0.count > $1.count }
    }

    public static func apply(_ text: String, keep names: [String]) -> String {
        var result = text
        for name in names.sorted(by: { $0.count > $1.count }) where name.contains(" ") {
            result = result.replacingOccurrences(of: name, with: name.replacingOccurrences(of: " ", with: space))
        }
        for join in joins {
            result = result.replacingOccurrences(of: join.pattern, with: join.template, options: .regularExpression)
        }
        return result
    }
}

private struct KeptNamesKey: EnvironmentKey {
    static let defaultValue: [String] = []
}

extension EnvironmentValues {
    public var keptNames: [String] {
        get { self[KeptNamesKey.self] }
        set { self[KeptNamesKey.self] = newValue }
    }
}

extension View {
    func keepsNames(of state: ServiceState?) -> some View {
        environment(\.keptNames, Unbreakable.names(state))
    }
}
