import CoreGraphics
import CoreML
import Foundation
import Testing
import Vision
import AppKit
import SwiftUI
@testable import Lifeline
@testable import LifelineKit

private let readingQueue = DispatchQueue(label: "lifeline.tests.text-reading")

/// The app's own fonts, which the app registers at launch. Every test that reads text from pixels registers them first,
/// so what it reads does not depend on which test happened to run before it.
let testFonts: Void = Fonts.register(
    folder: URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        .appendingPathComponent("Resources/Fonts", isDirectory: true)
)

private final class Reading: @unchecked Sendable {
    let request: VNRequest
    let image: CGImage
    var failure: Error?

    init(request: VNRequest, image: CGImage) {
        self.request = request
        self.image = image
    }
}

func onCPU(_ request: VNRequest) {
    guard let stages = try? request.supportedComputeStageDevices else { return }
    for (stage, devices) in stages {
        for device in devices {
            if case .cpu = device {
                try? request.setComputeDevice(device, for: stage)
                break
            }
        }
    }
}

private func enlarged(_ image: CGImage, by factor: Int) -> CGImage {
    let width = image.width * factor
    let height = image.height * factor
    guard let context = CGContext(
        data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ) else { return image }
    context.interpolationQuality = .high
    context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
    return context.makeImage() ?? image
}

func performText(_ request: VNRequest, on image: CGImage) throws {
    let reading = Reading(request: request, image: image)
    let done = DispatchSemaphore(value: 0)
    readingQueue.async {
        var picture = reading.image
        for attempt in 0..<9 {
            if attempt == 6, let text = reading.request as? VNRecognizeTextRequest {
                text.recognitionLevel = .fast
                text.usesLanguageCorrection = true
                picture = enlarged(reading.image, by: reading.image.width < 1200 ? 3 : 2)
            }
            do {
                try VNImageRequestHandler(cgImage: picture).perform([reading.request])
                reading.failure = nil
                break
            } catch {
                reading.failure = error
                Thread.sleep(forTimeInterval: 0.15 * Double(attempt + 1))
            }
        }
        done.signal()
    }
    done.wait()
    if let failure = reading.failure {
        throw failure
    }
}

// Typeset reading. DrawnText reports every line SwiftUI laid out, in drawing order, so a test reads exactly what the view
// typeset. Machine load and Vision misreads cannot change it. Tests that need where a word sits on screen still use Vision.

/// What a view reported, kept behind a lock. SwiftUI's preference callback is not main-actor isolated, so the box never
/// assumes it runs there (an assumeIsolated that runs off the main executor traps with signal 5).
final class DrawnBox: @unchecked Sendable {
    private let lock = NSLock()
    private var stored: [DrawnLine] = []

    var lines: [DrawnLine] {
        get { lock.withLock { stored } }
        set { lock.withLock { stored = newValue } }
    }
}

@MainActor
func drawnLines(_ view: some View, until ready: ([String]) -> Bool = { !$0.isEmpty }) -> [String] {
    drawnRuns(view, until: ready).map(\.text)
}

/// The lines a view set in the mono face.
@MainActor
func drawnMono(_ view: some View) -> [String] {
    drawnRuns(view, until: { !$0.isEmpty }).filter(\.mono).map(\.text)
}

@MainActor
func drawnRuns(_ view: some View, until ready: ([String]) -> Bool) -> [DrawnLine] {
    NSApplication.shared.setActivationPolicy(.prohibited)
    let box = DrawnBox()
    let root = view
        .environment(\.menuMotion, Motion(MotionPlan(reduceMotion: true)))
        .environment(\.stillFields, true)
        .environment(\.reportsText, true)
        .onPreferenceChange(DrawnText.self) { lines in box.lines = lines }
    let host = NSHostingView(rootView: root)
    host.frame = NSRect(origin: .zero, size: host.fittingSize)
    let window = NSWindow(contentRect: NSRect(x: -30000, y: -30000, width: host.frame.width, height: host.frame.height), styleMask: [.borderless], backing: .buffered, defer: false)
    window.isReleasedWhenClosed = false
    window.contentView = host
    defer { window.contentView = nil }
    func clean(_ lines: [DrawnLine]) -> [DrawnLine] {
        lines.map { DrawnLine(text: $0.text.replacingOccurrences(of: "\u{00A0}", with: " ").replacingOccurrences(of: "\u{2060}", with: ""), mono: $0.mono) }
    }
    for _ in 0..<1000 where !ready(clean(box.lines).map(\.text)) {
        host.layoutSubtreeIfNeeded()
        RunLoop.main.run(until: Date().addingTimeInterval(0.01))
    }
    if !ready(clean(box.lines).map(\.text)) {
        Issue.record("the view never drew what the test waited for, so this is a stall, not a wrong text: \(clean(box.lines).map(\.text))")
    }
    return clean(box.lines)
}

@MainActor
func typeset(_ view: some View, dark: Bool = false, until ready: ([String]) -> Bool = { !$0.isEmpty }) -> [String] {
    let lines = drawnLines(view.environment(\.colorScheme, dark ? .dark : .light), until: ready)
    return [lines.joined(separator: " ").replacingOccurrences(of: "  ", with: " ")] + lines
}

/// Text recognition sometimes reads the start button's label with a capital S or drops its last letters, so it is found
/// by how it begins. "Open the sign-in page again" is a different button and never matches.
func isOpenButton(_ text: String?) -> Bool {
    guard let text = text?.lowercased(), !text.contains("again") else { return false }
    return text.hasPrefix("open the sign-in pa")
}
