import AppKit
import CoreText
import SwiftUI

public enum Fonts {
    static let numbersFace = "ChivoMono-Medium"
    static let weightAxis = 0x7767_6874

    public static func register(folder: URL? = nil) {
        guard let folder = folder ?? Bundle.main.resourceURL?.appendingPathComponent("Fonts", isDirectory: true),
              let names = try? FileManager.default.contentsOfDirectory(atPath: folder.path) else {
            return
        }
        for name in names.sorted() where name.hasSuffix(".ttf") {
            var failure: Unmanaged<CFError>?
            CTFontManagerRegisterFontsForURL(folder.appendingPathComponent(name) as CFURL, .process, &failure)
        }
    }

    static func wordsFace(_ weight: Int) -> String {
        switch weight {
        case 300: return "Poppins-Light"
        case 500: return "Poppins-Medium"
        case 600: return "Poppins-SemiBold"
        default: return "Poppins-Regular"
        }
    }

    public static func wordsFont(_ size: CGFloat, weight: Int = 400) -> CTFont {
        CTFontCreateWithName(wordsFace(weight) as CFString, size, nil)
    }

    public static func numbersFont(_ size: CGFloat, weight: Int = 400) -> CTFont {
        let base = CTFontCreateWithName(numbersFace as CFString, size, nil)
        let attributes = [kCTFontVariationAttribute: [weightAxis: weight]] as CFDictionary
        let descriptor = CTFontDescriptorCreateWithAttributes(attributes)
        return CTFontCreateCopyWithAttributes(base, size, nil, descriptor)
    }

    public static func words(_ size: CGFloat, weight: Int = 400) -> Font {
        Font(wordsFont(size, weight: weight))
    }

    public static func numbers(_ size: CGFloat, weight: Int = 400) -> Font {
        Font(numbersFont(size, weight: weight))
    }
}
