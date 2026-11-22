// swift-tools-version:6.0
import PackageDescription

let package = Package(
    name: "Lifeline",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "Lifeline", targets: ["LifelineMain"]),
        .executable(name: "LifelineWidget", targets: ["LifelineWidget"])
    ],
    targets: [
        .target(name: "LifelineWidgetCore"),
        .target(name: "LifelineWidgetViews", dependencies: ["LifelineWidgetCore"]),
        .target(name: "LifelineKit", dependencies: ["LifelineWidgetCore"]),
        .target(name: "Lifeline", dependencies: ["LifelineKit", "LifelineWidgetCore"]),
        .executableTarget(name: "LifelineMain", dependencies: ["Lifeline"]),
        // The desktop widgets' extension. scripts/bundle.sh wraps it in Contents/PlugIns/LifelineWidget.appex;
        // these are the flags Xcode passes for a WidgetKit extension.
        .executableTarget(
            name: "LifelineWidget",
            dependencies: ["LifelineWidgetCore", "LifelineWidgetViews"],
            swiftSettings: [.unsafeFlags(["-application-extension"])],
            linkerSettings: [.unsafeFlags(["-Xlinker", "-e", "-Xlinker", "_NSExtensionMain"]), .linkedFramework("WidgetKit")]
        ),
        // The tests read the CoreStates fixtures from their own folder, so they are not bundled as resources.
        .testTarget(
            name: "LifelineKitTests",
            dependencies: ["LifelineKit", "Lifeline", "LifelineWidgetCore", "LifelineWidgetViews"],
            exclude: ["CoreStates"]
        )
    ]
)
