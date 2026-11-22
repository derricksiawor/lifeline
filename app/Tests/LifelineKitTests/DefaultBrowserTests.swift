import AppKit
import Foundation
import SwiftUI
import Testing
import Vision
@testable import Lifeline
@testable import LifelineKit

private func sourceText() throws -> String {
    let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    var text = ""
    for folder in ["Sources/Lifeline", "Sources/LifelineKit"] {
        let url = root.appendingPathComponent(folder)
        for file in try FileManager.default.contentsOfDirectory(at: url, includingPropertiesForKeys: nil) where file.pathExtension == "swift" {
            text += try String(contentsOf: file, encoding: .utf8) + "\n"
        }
    }
    return text
}

private func stutters(_ sentence: String) -> Bool {
    let words = sentence.lowercased().split(whereSeparator: { $0 == " " || $0 == "\n" }).map { $0.trimmingCharacters(in: .punctuationCharacters) }
    return zip(words, words.dropFirst()).contains { $0 == $1 && $0 == "in" }
}

struct DefaultBrowserTests {
    @Test func theSignInNamesTheDefaultBrowserAndNeverAPrivateWindow() throws {
        let state = try Fixture.state("devbox-work-unadded")
        let browser = try #require(state.signinBrowser)
        #expect(AddFlow.explain(state).hasPrefix("Lifeline opens Claude's sign-in in \(browser), your default browser."), "\(AddFlow.explain(state))")
        for job in try Fixture.state("job-cards").jobs ?? [] where JobFlow.signInKinds.contains(job.kind ?? "") {
            #expect(JobFlow.waiting(job) == "Waiting for you to finish the sign-in in \(job.browser ?? "").", "\(job.id)")
        }
    }

    @Test func noAppSentenceSaysPrivateOrChromeOrStutters() throws {
        let text = try sourceText()
        #expect(!text.contains("private window"))
        #expect(!text.contains("privately"))
        #expect(!text.contains("a private \\("))
        #expect(!text.contains("Chrome"))
        let literals = text.split(separator: "\"").enumerated().filter { $0.offset % 2 == 1 }.map { String($0.element) }
        for literal in literals where stutters(literal) {
            Issue.record("stutters: \(literal)")
        }
        var sentences: [String] = []
        for name in ["devbox-work-unadded", "job-cards", "failed-jobs", "after-mac-add-org"] {
            let state = try Fixture.state(name)
            sentences.append(AddFlow.explain(state))
            sentences += (state.knownAccounts ?? []).map { AddFlow.explain(state, pick: $0) }
            for job in state.jobs ?? [] {
                sentences += [JobFlow.waiting(job), job.compact ?? ""] + JobFlow.guidance(job, state: state) + JobFlow.cardLines(job, state: state)
            }
        }
        #expect(sentences.filter { !$0.isEmpty }.count > 10)
        for sentence in sentences {
            #expect(!stutters(sentence), "\(sentence)")
            #expect(!sentence.contains("private"), "\(sentence)")
        }
        #expect(stutters("Finish signing in in Safari."))
        #expect(!stutters("the sign-in in Safari"))
    }
}

@MainActor
struct StableStartTests {
    private func openButtonTop(_ target: AddTarget) throws -> CGFloat? {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let state = try ServiceState.decode(Data(#"{"version": 1, "signin_browser": "Safari", "devbox": {"name": "Studio", "reachable": true, "answers": true, "can_sign_in": true}, "accounts": [{"uuid": "j", "name": "Alex's Account", "number": 1}]}"#.utf8))
        _ = testFonts
        let renderer = ImageRenderer(content: AddAccountView(state: state, step: .start, target: target, problem: nil, zone: .current).background(Color.white).environment(\.colorScheme, .light))
        renderer.scale = 2
        guard let image = renderer.cgImage else { return nil }
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        onCPU(request)
        try performText(request, on: image)
        guard let open = (request.results ?? []).first(where: { isOpenButton($0.topCandidates(1).first?.string) }) else { return nil }
        return CGFloat(image.height) * (1 - open.boundingBox.maxY) / renderer.scale
    }

    @Test func choosingBothNeverMovesTheOpenButton() throws {
        let mac = try #require(try openButtonTop(.mac))
        let both = try #require(try openButtonTop(.both))
        let devbox = try #require(try openButtonTop(.devbox))
        #expect(abs(mac - both) < 1.5, "\(mac) \(both)")
        #expect(abs(mac - devbox) < 1.5, "\(mac) \(devbox)")
    }
}
