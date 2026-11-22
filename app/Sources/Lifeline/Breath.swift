import AppKit
import QuartzCore

@MainActor
enum Breath {
    static let key = "breath"
    static let low: Float = 0.35
    static let still: CGFloat = 0.6
    static let period: CFTimeInterval = 2.4

    static func apply(_ view: NSView, on: Bool, reduceMotion: Bool) {
        view.wantsLayer = true
        guard let layer = view.layer else { return }
        let running = layer.animation(forKey: key) != nil
        guard on else {
            if running {
                layer.removeAnimation(forKey: key)
            }
            view.alphaValue = 1
            return
        }
        if reduceMotion {
            if running {
                layer.removeAnimation(forKey: key)
            }
            view.alphaValue = still
            return
        }
        view.alphaValue = 1
        guard !running else { return }
        let animation = CABasicAnimation(keyPath: "opacity")
        animation.fromValue = 1
        animation.toValue = low
        animation.duration = period / 2
        animation.autoreverses = true
        animation.repeatCount = .infinity
        animation.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        animation.isRemovedOnCompletion = false
        layer.add(animation, forKey: key)
    }
}
