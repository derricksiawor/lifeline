import LifelineKit
import SwiftUI

struct Line: View {
    let text: String?
    let style: TextStyle
    let color: Color

    init(_ text: String?, _ style: TextStyle, _ color: Color) {
        self.text = text
        self.style = style
        self.color = color
    }

    var body: some View {
        if let text, !text.isEmpty {
            LineText(text, style, color)
        } else {
            Color.clear.frame(width: 0, height: 0)
        }
    }
}

struct Block: View {
    let text: String?
    let style: TextStyle
    let color: Color
    let width: CGFloat
    let centered: Bool

    init(_ text: String?, _ style: TextStyle, _ color: Color, width: CGFloat, centered: Bool = false) {
        self.text = text
        self.style = style
        self.color = color
        self.width = width
        self.centered = centered
    }

    var body: some View {
        if let text, !text.isEmpty {
            Paragraph(text, style, color, width: width, centered: centered)
        } else {
            Color.clear.frame(width: width, height: 0)
        }
    }
}

struct Meter: View {
    let used: Double
    let ahead: Double
    let height: CGFloat
    let width: CGFloat
    let track: Color
    let tone: Tone
    let glow: ShadowSpec
    let glowWeight: Double
    let landing: ShadowSpec
    let landingWeight: Double
    let lineAnimation: Animation?
    let glowAnimation: Animation?

    var body: some View {
        let both = glowWeight > 0 && landingWeight > 0
        let top = glowWeight > 0 ? glow.weighted(glowWeight) : landing.weighted(landingWeight)
        ZStack(alignment: .leading) {
            Capsule().fill(track).frame(width: width, height: height)
            Capsule().fill(tone.fore).frame(width: width * ahead, height: height)
                .offset(x: width * used)
                .animation(lineAnimation, value: used)
                .animation(lineAnimation, value: ahead)
            if both {
                Capsule().fill(tone.fill).frame(width: width * used, height: height)
                    .cssShadow(landing.weighted(landingWeight))
            }
            Capsule().fill(tone.fill).frame(width: width * used, height: height)
                .animation(lineAnimation, value: used)
                .cssShadow(top)
                .animation(glowAnimation, value: top.color)
        }
        .frame(width: width, height: height, alignment: .leading)
    }
}

struct WeekLine: View {
    let fraction: Double
    let width: CGFloat
    let track: Color
    let fill: Color

    var body: some View {
        ZStack(alignment: .leading) {
            Capsule().fill(track).frame(width: width, height: 2)
            Capsule().fill(fill).frame(width: width * fraction, height: 2)
        }
        .frame(width: width, height: 2, alignment: .leading)
    }
}

struct HeroBar: View {
    let used: Double
    let ahead: Double
    let palette: Palette
    let tone: Tone
    @Environment(\.menuMotion) private var motion
    @State private var bright = true

    var body: some View {
        let width = Metrics.contentWidth
        let breathing = motion.breathe != nil
        ZStack(alignment: .leading) {
            Capsule().fill(palette.track).frame(width: width, height: 6)
            Capsule().fill(tone.fore).frame(width: width * ahead, height: 6).offset(x: width * used)
            Capsule().fill(tone.glow).frame(width: width * used, height: 6)
                .blur(radius: Metrics.glowBlur)
                .opacity(breathing ? (bright ? 1 : 0.45) : 1)
            Capsule().fill(tone.fill).frame(width: width * used, height: 6)
        }
        .frame(width: width, height: 6, alignment: .leading)
        .onAppear {
            guard let breathe = motion.breathe else { return }
            withAnimation(breathe) {
                bright = false
            }
        }
    }
}

struct TextFade: Equatable {
    var outgoing: Double
    var incoming: Double
    var height: Double

    static let settled = TextFade(outgoing: 0, incoming: 1, height: 1)
}

struct HeroLook: Equatable {
    var old: MenuModel?
    var fade: TextFade
    var headMuted: Double
    var used: Double
    var ahead: Double
    var week: Double
    var lineOpacity: Double

    static func plain(_ model: MenuModel) -> HeroLook {
        HeroLook(
            old: nil,
            fade: .settled,
            headMuted: 0,
            used: Fraction.of(model.heroW),
            ahead: Fraction.of(model.foreW),
            week: Fraction.of(model.weekW),
            lineOpacity: 1
        )
    }
}

struct Swap: View {
    let old: String?
    let new: String?
    let style: TextStyle
    let color: Color
    let oldColor: Color
    let fade: TextFade
    let width: CGFloat?
    let alignment: Alignment
    let centered: Bool
    let gap: CGFloat
    @Environment(\.keptNames) private var names

    init(
        _ old: String?, _ new: String?, _ style: TextStyle, _ color: Color, oldColor: Color? = nil, fade: TextFade,
        width: CGFloat? = nil, alignment: Alignment = .topLeading, centered: Bool = false, gap: CGFloat = 0
    ) {
        self.old = old
        self.new = new
        self.style = style
        self.color = color
        self.oldColor = oldColor ?? color
        self.fade = fade
        self.width = width
        self.alignment = alignment
        self.centered = centered
        self.gap = gap
    }

    private func height(_ text: String?, width: CGFloat) -> CGFloat {
        let lines = style.lines(text ?? "", width: width, keep: names).count
        return lines == 0 ? 0 : CGFloat(lines) * style.lineHeight + gap
    }

    var body: some View {
        if let old, old != new, fade != .settled {
            if let width {
                let before = height(old, width: width)
                let after = height(new, width: width)
                ZStack(alignment: alignment) {
                    piece(old, oldColor).opacity(fade.outgoing)
                    piece(new, color).opacity(fade.incoming)
                }
                .frame(width: width, height: before + (after - before) * fade.height, alignment: alignment)
            } else {
                ZStack(alignment: alignment) {
                    piece(old, oldColor).opacity(fade.outgoing)
                    piece(new, color).opacity(fade.incoming)
                }
            }
        } else if let width, gap > 0, !(new ?? "").isEmpty {
            piece(new, color).padding(.bottom, gap).frame(width: width)
        } else {
            piece(new, color)
        }
    }

    @ViewBuilder
    private func piece(_ text: String?, _ ink: Color) -> some View {
        if let width {
            Block(text, style, ink, width: width, centered: centered)
        } else {
            Line(text, style, ink)
        }
    }
}

enum Plan {
    static func oneForm(_ text: String) -> String {
        text.replacingOccurrences(of: ", ", with: " ")
    }
}

/// A figure is a percentage ("42%"), a price ("$100") or a plan multiple ("5x"). Every other word, "5 hours" included,
/// is set in the text face.
public enum FigureRuns {
    static let figure = try! NSRegularExpression(pattern: #"(?<![\w$.])\$?\d+(?:\.\d+)?(?:%|x)(?!\w)|(?<![\w$.])\$\d+(?:\.\d+)?(?![\w%])"#)

    public static func split(_ text: String) -> [(text: String, figure: Bool)] {
        guard !text.isEmpty else { return [] }
        let whole = text as NSString
        var runs: [(text: String, figure: Bool)] = []
        var cursor = 0
        for match in figure.matches(in: text, range: NSRange(location: 0, length: whole.length)) {
            if match.range.location > cursor {
                runs.append((whole.substring(with: NSRange(location: cursor, length: match.range.location - cursor)), false))
            }
            runs.append((whole.substring(with: match.range), true))
            cursor = match.range.location + match.range.length
        }
        if cursor < whole.length {
            runs.append((whole.substring(from: cursor), false))
        }
        return runs
    }
}

extension TextStyle {
    /// The text face at the same size, weight and line height, for the words beside a figure.
    var wordsTwin: TextStyle {
        TextStyle(.words, size, weight: weight, line: lineHeight)
    }
}

struct MixedLine: View {
    let text: String?
    let color: Color
    var numbers: TextStyle = Styles.smallNumbers

    static func runs(_ text: String, numbers: TextStyle) -> [(text: String, style: TextStyle)] {
        FigureRuns.split(text).map { ($0.text, $0.figure ? numbers : numbers.wordsTwin) }
    }

    static func width(_ text: String?, numbers: TextStyle) -> CGFloat {
        runs(text ?? "", numbers: numbers).reduce(0) { $0 + $1.style.width(of: $1.text) }
    }

    var body: some View {
        let runs = Self.runs(text ?? "", numbers: numbers)
        if runs.isEmpty {
            Color.clear.frame(width: 0, height: 0)
        } else {
            HStack(alignment: .firstTextBaseline, spacing: 0) {
                ForEach(Array(runs.enumerated()), id: \.offset) { item in
                    LineText(item.element.text, item.element.style, color)
                }
            }
        }
    }
}

struct MixedSwap: View {
    let old: String?
    let new: String?
    var numbers: TextStyle = Styles.smallNumbers
    let color: Color
    var oldColor: Color? = nil
    let fade: TextFade
    var alignment: Alignment = .topLeading

    var body: some View {
        if let old, old != new, fade != .settled {
            ZStack(alignment: alignment) {
                MixedLine(text: old, color: oldColor ?? color, numbers: numbers).opacity(fade.outgoing)
                MixedLine(text: new, color: color, numbers: numbers).opacity(fade.incoming)
            }
        } else {
            MixedLine(text: new, color: color, numbers: numbers)
        }
    }
}

struct HeroView: View {
    /// The hero line at 45 percent while the reading for the account it shows is stale; the core's "Last read at"
    /// sentence stays.
    nonisolated static func staleStrength(_ model: MenuModel) -> Double {
        let shown = model.rows?.first { $0.uuid == model.activeUuid && !$0.uuid.isEmpty }
        return shown?.stale == true ? Metrics.unavailable : 1
    }

    let model: MenuModel
    let palette: Palette
    let look: HeroLook
    let animated: Bool
    var countdown: CountdownSummary? = nil
    var countdownFraction: Double? = nil
    @Environment(\.menuMotion) private var motion

    var body: some View {
        if countdown?.endsAt != nil, countdownFraction == nil {
            TimelineView(.periodic(from: Date(), by: 0.25)) { context in
                hero(MenuLogic.counted(model.head, secondsLeft: MenuLogic.countdownLeft(countdown, fixed: nil, now: context.date, left: model.countdownLeft)))
            }
        } else if countdown != nil {
            hero(MenuLogic.counted(model.head, secondsLeft: MenuLogic.countdownLeft(countdown, fixed: countdownFraction, now: Date(), left: model.countdownLeft)))
        } else {
            hero(model.head)
        }
    }

    static func drawsLines(_ model: MenuModel) -> Bool {
        !(model.heroName ?? "").trimmingCharacters(in: .whitespaces).isEmpty
    }

    /// The thin window's figure beside its line, only when the sentence under the line names no figure (a run-out).
    static func thinLabel(_ model: MenuModel) -> String? {
        guard let window = model.thinWindow, !window.isEmpty, let label = model.thinLabel, !label.isEmpty else { return nil }
        let sentence = model.secondText ?? ""
        let figure = label.split(separator: " ").last.map(String.init) ?? label
        guard !sentence.contains("%"), figure != "0%" else { return nil }
        return label
    }

    static func usedRuns(_ text: String) -> [(text: String, style: TextStyle)] {
        MixedLine.runs(text, numbers: Styles.smallNumbers)
    }

    private func hero(_ head: String?) -> some View {
        let tone = palette.tone(model.heroTone)
        let old = look.old
        let fade = old == nil ? TextFade.settled : look.fade
        let lineAnimation = animated ? motion.glide : nil
        let headInk = old == nil ? palette.ink.mixed(with: palette.muted, look.headMuted) : palette.ink
        let lines = Self.drawsLines(model) || (old.map(Self.drawsLines) ?? false)
        return VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .firstTextBaseline, spacing: 0) {
                Swap(old?.viewLabel, model.viewLabel, Styles.label, palette.faint, fade: fade)
                Spacer(minLength: 8)
                Swap(old?.sessions, model.sessions, Styles.sessions, palette.muted, fade: fade, alignment: .topTrailing)
            }
            .frame(width: Metrics.contentWidth)
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Swap(old?.heroName, model.heroName, Styles.heroName, palette.text, fade: fade)
                MixedSwap(old: old?.heroPlan.map(Plan.oneForm), new: model.heroPlan.map(Plan.oneForm), numbers: Styles.heroPlan, color: palette.faint, fade: fade)
            }
            .padding(.top, 6)
            Swap(old?.head, head, Styles.head, headInk, oldColor: palette.muted, fade: fade, width: Metrics.contentWidth)
                .padding(.top, 10)
            Swap(old?.sub, model.sub, Styles.sub, palette.muted, fade: fade, width: Metrics.contentWidth)
                .padding(.top, 4)
            if lines {
                HeroBar(used: look.used, ahead: look.ahead, palette: palette, tone: tone)
                    .opacity(look.lineOpacity * Self.staleStrength(model))
                    .animation(lineAnimation, value: look.used)
                    .animation(lineAnimation, value: look.ahead)
                    .padding(.top, 18)
                HStack(alignment: .firstTextBaseline, spacing: 0) {
                    MixedSwap(old: old?.heroUsed, new: model.heroUsed, color: palette.muted, fade: fade)
                    Spacer(minLength: 8)
                    Swap(old?.heroReset, model.heroReset, Styles.small, palette.muted, fade: fade, alignment: .topTrailing)
                }
                .frame(width: Metrics.contentWidth)
                .padding(.top, 8)
                if model.hasSecond {
                    let label = Self.thinLabel(model)
                    let room = label.map { MixedLine.width($0, numbers: Styles.weekPct) + 8 } ?? 0
                    HStack(alignment: .center, spacing: 0) {
                        WeekLine(fraction: Fraction.of(model.secondW), width: Metrics.contentWidth - room, track: palette.track, fill: palette.weekFill)
                            .animation(lineAnimation, value: Fraction.of(model.secondW))
                        if let label {
                            Spacer(minLength: 0)
                            MixedLine(text: label, color: palette.faint, numbers: Styles.weekPct)
                        }
                    }
                    .frame(width: Metrics.contentWidth, height: label == nil ? 2 : Styles.weekPct.lineHeight)
                    .padding(.top, label == nil ? 14 : 6)
                    Swap(old?.secondText, model.secondText, Styles.small, palette.faint, fade: fade, width: Metrics.contentWidth)
                        .padding(.top, 6)
                } else if model.showsWeekLine {
                    WeekLine(fraction: look.week, width: Metrics.contentWidth, track: palette.track, fill: palette.weekFill)
                        .animation(lineAnimation, value: look.week)
                        .padding(.top, 14)
                    Swap(old?.weekText, model.weekText, Styles.small, palette.faint, fade: fade, width: Metrics.contentWidth)
                        .padding(.top, 6)
                } else if !(model.weekText ?? "").isEmpty {
                    Swap(old?.weekText, model.weekText, Styles.small, palette.faint, fade: fade, width: Metrics.contentWidth)
                        .padding(.top, 14)
                }
            }
        }
        .padding(EdgeInsets(top: 14, leading: Metrics.sideInset, bottom: 8, trailing: Metrics.sideInset))
    }
}
