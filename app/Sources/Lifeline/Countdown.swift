import LifelineKit
import SwiftUI

struct CountdownLine: View {
    let countdown: CountdownSummary?
    let fixed: Double?
    let palette: Palette
    @Environment(\.menuMotion) private var motion
    @State private var fraction: Double

    init(countdown: CountdownSummary?, fixed: Double?, palette: Palette) {
        self.countdown = countdown
        self.fixed = fixed
        self.palette = palette
        _fraction = State(initialValue: fixed ?? countdown?.remaining(at: Date()) ?? 1)
    }

    var body: some View {
        Group {
            if fixed == nil, let step = motion.plan.countdownStep, let countdown {
                TimelineView(.periodic(from: Date(), by: step)) { context in
                    line(countdown.remaining(at: context.date))
                }
            } else {
                line(fraction)
                    .onAppear(perform: drain)
                    .onChange(of: countdown?.endsAt) { _, _ in drain() }
            }
        }
        .padding(.horizontal, Metrics.sideInset)
    }

    private func line(_ value: Double) -> some View {
        let width = Metrics.contentWidth
        return ZStack(alignment: .leading) {
            Capsule().fill(palette.track).frame(width: width, height: 3)
            Capsule().fill(palette.calm.fill).frame(width: width * value, height: 3)
        }
        .frame(width: width, height: 3, alignment: .leading)
        .clipShape(Capsule())
    }

    private func drain() {
        guard fixed == nil, let countdown else { return }
        let now = Date()
        fraction = countdown.remaining(at: now)
        guard motion.plan.drains else { return }
        withAnimation(.linear(duration: countdown.secondsLeft(at: now))) {
            fraction = 0
        }
    }
}
