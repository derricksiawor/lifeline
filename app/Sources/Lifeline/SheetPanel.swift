import AppKit
import LifelineKit
import SwiftUI

public struct SheetPanel<Content: View>: View {
    let size: SheetSize?
    let limit: CGFloat?
    let resize: (SheetSize) -> Void
    let commit: () -> Void
    let content: (CGFloat) -> Content
    @Environment(\.colorScheme) private var scheme
    @Environment(\.stillFields) private var still
    @Environment(\.sharedSurface) private var shared
    @State private var offset: CGFloat = 0
    @State private var contentHeight: CGFloat = 0
    @State private var viewport: CGFloat = 0
    static var space: String { "sheet" }

    public init(
        size: SheetSize?, limit: CGFloat?, resize: @escaping (SheetSize) -> Void = { _ in }, commit: @escaping () -> Void = {},
        @ViewBuilder content: @escaping (CGFloat) -> Content
    ) {
        self.size = size
        self.limit = limit
        self.resize = resize
        self.commit = commit
        self.content = content
    }

    var width: CGFloat {
        size.map { CGFloat($0.width) } ?? Metrics.panelWidth
    }

    public var body: some View {
        let palette = Palette.of(scheme)
        let fades = MenuLogic.listFades(offset: offset, content: contentHeight, limit: viewport)
        let shape = RoundedRectangle(cornerRadius: Metrics.panelRadius, style: .continuous)
        scroller
            .frame(width: width)
            .mask(EdgeFade(top: fades.top, bottom: fades.bottom))
            .clipShape(shape)
            .overlay(alignment: .bottom) {
                Capsule()
                    .fill(palette.track)
                    .frame(width: 40, height: 4)
                    .padding(.bottom, 6)
                    .allowsHitTesting(false)
            }
            .overlay(alignment: .bottomTrailing) {
                if !still {
                    handles
                }
            }
            .background {
                if !shared {
                    PanelSurface(palette: palette)
                }
            }
    }

    @ViewBuilder
    private var scroller: some View {
        let inner = ScrollView(.vertical) {
            content(width)
                .frame(width: width, alignment: .topLeading)
                .background(Measured(key: ListHeight.self))
                .background(GeometryReader { proxy in
                    Color.clear.preference(key: ListOffset.self, value: proxy.frame(in: .named(Self.space)).minY)
                })
        }
        .coordinateSpace(name: Self.space)
        .scrollIndicators(.automatic)
        .background(GeometryReader { box in
            Color.clear
                .onAppear { viewport = box.size.height }
                .onChange(of: box.size.height) { _, height in viewport = height }
        })
        .onPreferenceChange(ListHeight.self) { height in
            contentHeight = height
        }
        .onPreferenceChange(ListOffset.self) { value in
            offset = value
        }
        if let size {
            let tallest = limit ?? CGFloat(size.height)
            inner.frame(height: min(CGFloat(size.height), tallest, contentHeight > 0 ? contentHeight : .greatestFiniteMagnitude))
        } else if let limit {
            inner
                .frame(maxHeight: limit)
                .fixedSize(horizontal: false, vertical: true)
        } else {
            inner.fixedSize(horizontal: false, vertical: true)
        }
    }

    private var current: SheetSize {
        SheetSize(width: Double(width), height: Double(viewport > 0 ? viewport : (limit ?? 400)))
    }

    private var handles: some View {
        let reach = Metrics.gripInset + 4
        return ZStack(alignment: .bottomTrailing) {
            HStack(spacing: 0) {
                Spacer(minLength: 0)
                ResizeHandle(edges: (true, false), start: { current }, resize: resize, commit: commit)
                    .frame(width: reach)
            }
            VStack(spacing: 0) {
                Spacer(minLength: 0)
                ResizeHandle(edges: (false, true), start: { current }, resize: resize, commit: commit)
                    .frame(height: reach)
            }
            ResizeHandle(edges: (true, true), start: { current }, resize: resize, commit: commit)
                .frame(width: reach + 8, height: reach + 8)
        }
    }
}

struct ResizeHandle: NSViewRepresentable {
    let edges: (right: Bool, bottom: Bool)
    let start: () -> SheetSize
    let resize: (SheetSize) -> Void
    let commit: () -> Void

    func makeNSView(context: Context) -> Grip {
        let view = Grip()
        update(view)
        return view
    }

    func updateNSView(_ view: Grip, context: Context) {
        update(view)
    }

    private func update(_ view: Grip) {
        view.edges = edges
        view.start = start
        view.resize = resize
        view.commit = commit
        view.window?.invalidateCursorRects(for: view)
    }

    final class Grip: NSView {
        var edges: (right: Bool, bottom: Bool) = (false, false)
        var start: (() -> SheetSize)?
        var resize: ((SheetSize) -> Void)?
        var commit: (() -> Void)?
        private var origin: NSPoint?
        private var from: SheetSize?

        override func resetCursorRects() {
            var cursor: NSCursor
            switch (edges.right, edges.bottom) {
            case (true, false): cursor = .resizeLeftRight
            case (false, true): cursor = .resizeUpDown
            default: cursor = .resizeLeftRight
            }
            if edges.right && edges.bottom, #available(macOS 15.0, *) {
                cursor = NSCursor.frameResize(position: .bottomRight, directions: .all)
            }
            addCursorRect(bounds, cursor: cursor)
        }

        private func place(_ event: NSEvent) -> NSPoint {
            window?.convertPoint(toScreen: event.locationInWindow) ?? NSEvent.mouseLocation
        }

        override func mouseDown(with event: NSEvent) {
            origin = place(event)
            from = start?()
        }

        override func mouseDragged(with event: NSEvent) {
            guard let origin, let from else { return }
            let now = place(event)
            let next = SheetSizing.dragged(from: from, by: (Double(now.x - origin.x), Double(origin.y - now.y)), edges: edges)
            resize?(next)
        }

        override func mouseUp(with event: NSEvent) {
            if origin != nil {
                commit?()
            }
            origin = nil
            from = nil
        }
    }
}
