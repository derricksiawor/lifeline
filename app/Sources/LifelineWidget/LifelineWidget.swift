import LifelineWidgetCore
import LifelineWidgetViews
import SwiftUI
import WidgetKit

/// The desktop widgets' extension. It reads the snapshot the app writes into the shared app group container and
/// draws it; it never reads the service or anything outside its container.
struct LifelineEntry: TimelineEntry {
    let date: Date
    let snapshot: WidgetSnapshot?
}

struct LifelineProvider: TimelineProvider {
    init() {
        WidgetFonts.register(folder: Bundle.main.resourceURL?.appendingPathComponent("Fonts", isDirectory: true))
    }

    func placeholder(in context: Context) -> LifelineEntry {
        LifelineEntry(date: Date(), snapshot: nil)
    }

    func getSnapshot(in context: Context, completion: @escaping (LifelineEntry) -> Void) {
        completion(LifelineEntry(date: Date(), snapshot: WidgetFeed.read()))
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<LifelineEntry>) -> Void) {
        let now = Date()
        let snapshot = WidgetFeed.read()
        let entries = WidgetFeed.entryDates(for: snapshot, now: now).map { LifelineEntry(date: $0, snapshot: snapshot) }
        completion(Timeline(entries: entries, policy: .after(now.addingTimeInterval(WidgetFeed.refresh))))
    }
}

struct LifelineWidgetFace: View {
    let entry: LifelineEntry
    @Environment(\.widgetFamily) private var family

    var body: some View {
        let size: WidgetSize = family == .systemSmall ? .small : (family == .systemLarge ? .large : .medium)
        WidgetFace(snapshot: entry.snapshot, size: size, now: entry.date)
            .containerBackground(for: .widget) {
                WidgetBackground()
            }
    }
}

struct LifelineDesktopWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: WidgetFeed.kind, provider: LifelineProvider()) { entry in
            LifelineWidgetFace(entry: entry)
        }
        .configurationDisplayName("Lifeline")
        .description("Your Claude accounts")
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge])
        .contentMarginsDisabled()
    }
}

@main
struct LifelineWidgets: WidgetBundle {
    var body: some Widget {
        LifelineDesktopWidget()
    }
}
