import LifelineKit
import SwiftUI

struct ScreenSurface<Content: View>: View {
    let size: CGSize?
    let palette: Palette
    @ViewBuilder let content: () -> Content

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: Metrics.panelRadius, style: .continuous)
        content()
            .frame(width: size?.width, height: size?.height, alignment: .topLeading)
            .clipShape(shape)
            .background(PanelSurface(palette: palette))
    }
}

public struct ScreenStage: View {
    let outgoing: AnyView
    let incoming: AnyView
    let from: CGSize
    let to: CGSize
    let move: ScreenMove
    let progress: Double
    let plan: ScreenMotion
    @Environment(\.colorScheme) private var scheme

    public init(outgoing: AnyView, incoming: AnyView, from: CGSize, to: CGSize, move: ScreenMove, progress: Double, plan: ScreenMotion) {
        self.outgoing = outgoing
        self.incoming = incoming
        self.from = from
        self.to = to
        self.move = move
        self.progress = progress
        self.plan = plan
    }

    public var body: some View {
        let width = from.width + (to.width - from.width) * progress
        let height = plan.heightEases ? from.height + (to.height - from.height) * progress : to.height
        let offsets = ScreenMotion.offsets(move, width: Double(max(from.width, to.width)), slides: plan.slides)
        ScreenSurface(size: CGSize(width: width, height: height), palette: Palette.of(scheme)) {
            ZStack(alignment: .topLeading) {
                outgoing
                    .fixedSize()
                    .offset(x: offsets.outgoing * progress)
                    .opacity(ScreenMotion.outgoingOpacity(progress))
                incoming
                    .fixedSize()
                    .offset(x: offsets.incoming * (1 - progress))
                    .opacity(ScreenMotion.incomingOpacity(progress))
            }
        }
    }
}

@MainActor
final class ScreenDirection {
    var move: ScreenMove = .forward
}

struct ScreenSizes: PreferenceKey {
    static let defaultValue: [String: CGSize] = [:]

    static func reduce(value: inout [String: CGSize], nextValue: () -> [String: CGSize]) {
        value.merge(nextValue()) { _, new in new }
    }
}

struct ScreenSlide: ViewModifier, Animatable {
    enum Role {
        case incoming
        case outgoing
    }

    let role: Role
    var amount: Double
    let direction: ScreenDirection
    let plan: ScreenMotion
    let width: Double

    init(role: Role, active: Bool, direction: ScreenDirection, plan: ScreenMotion, width: Double) {
        self.role = role
        amount = active ? 1 : 0
        self.direction = direction
        self.plan = plan
        self.width = width
    }

    nonisolated var animatableData: Double {
        get { amount }
        set { amount = newValue }
    }

    /// amount is 1 where the screen is away and 0 where it rests. The outgoing screen leaves as amount rises; the incoming
    /// one arrives as amount falls. The opacities never overlap (ScreenMotion).
    static func opacity(_ role: Role, amount: Double) -> Double {
        role == .outgoing ? ScreenMotion.outgoingOpacity(amount) : ScreenMotion.incomingOpacity(1 - amount)
    }

    func body(content: Content) -> some View {
        let offsets = ScreenMotion.offsets(direction.move, width: width, slides: plan.slides)
        let x = (role == .incoming ? offsets.incoming : offsets.outgoing) * amount
        content
            .offset(x: x)
            .opacity(Self.opacity(role, amount: amount))
            .allowsHitTesting(role == .incoming || amount < 0.5)
    }

    static func transition(direction: ScreenDirection, plan: ScreenMotion, width: Double) -> AnyTransition {
        .asymmetric(
            insertion: .modifier(
                active: ScreenSlide(role: .incoming, active: true, direction: direction, plan: plan, width: width),
                identity: ScreenSlide(role: .incoming, active: false, direction: direction, plan: plan, width: width)
            ),
            removal: .modifier(
                active: ScreenSlide(role: .outgoing, active: true, direction: direction, plan: plan, width: width),
                identity: ScreenSlide(role: .outgoing, active: false, direction: direction, plan: plan, width: width)
            )
        )
    }
}
