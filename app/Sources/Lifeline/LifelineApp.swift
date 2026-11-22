import AppKit
import Combine
import LifelineKit
import ServiceManagement

@MainActor
public final class AppStatus: ObservableObject {
    @Published public internal(set) var notices: NoticePermission = .waiting
    @Published public internal(set) var shortcut: ShortcutState = .waiting
    @Published public internal(set) var login: LoginState = LoginItem.state()
    private var watches: [AnyCancellable] = []

    var local: LocalStatus {
        LocalStatus(notices: notices, shortcut: shortcut, login: login)
    }

    func refreshLogin() {
        login = LoginItem.state()
    }

    func setLogin(_ on: Bool) {
        login = LoginItem.set(on)
    }

    func follow(_ notifier: Notifier, _ hotkey: Hotkey) {
        watches = [
            notifier.$permission.sink { [weak self] value in
                MainActor.assumeIsolated {
                    self?.notices = value
                }
            },
            hotkey.$state.sink { [weak self] value in
                MainActor.assumeIsolated {
                    self?.shortcut = value
                }
            }
        ]
    }
}

@MainActor
public final class AppDelegate: NSObject, NSApplicationDelegate {
    let api: LifelineAPI
    let store: StateStore
    let supervisor: ServiceSupervisor
    let presence: PresenceReporter
    public let status = AppStatus()
    private var panel: MenuPanel?
    private var hotkey: Hotkey?
    private var notifier: Notifier?
    private var signals: [DispatchSourceSignal] = []
    private var quitting = false
    private var widgets: WidgetPublisher?
    private var widgetWatch: AnyCancellable?

    public override init() {
        Fonts.register()
        let client = SocketClient(path: SocketClient.defaultPath(), limit: 2)
        api = LifelineAPI(client: client)
        store = StateStore(api: api)
        let path = ServicePath.resolve(
            Bundle.main.object(forInfoDictionaryKey: "LifelineServicePath") as? String,
            resources: Bundle.main.resourcePath
        )
        supervisor = ServiceSupervisor(
            api: api,
            servicePath: path,
            expectedBuild: Bundle.main.object(forInfoDictionaryKey: "LifelineServiceBuild") as? String
        )
        presence = PresenceReporter(api: api)
        super.init()
    }

    public func applicationDidFinishLaunching(_ notification: Notification) {
        signal(SIGPIPE, SIG_IGN)
        registerLoginItemIfInstalled()
        for number in [SIGTERM, SIGINT] {
            signal(number, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: number, queue: .main)
            source.setEventHandler {
                NSApplication.shared.terminate(nil)
            }
            source.resume()
            signals.append(source)
        }
        let panel = MenuPanel(store: store, status: status)
        self.panel = panel
        let hotkey = Hotkey { [weak panel] in
            panel?.toggle()
        }
        hotkey.register()
        self.hotkey = hotkey
        let notifier = Notifier(
            store: store,
            shown: { [weak panel] fresh in panel?.shownMenu(for: fresh) },
            open: { [weak panel] in panel?.open() }
        )
        self.notifier = notifier
        status.follow(notifier, hotkey)
        supervisor.start()
        presence.start()
        store.start()
        notifier.start()
        startWidgets()
    }

    /// The desktop widgets read what this app writes into their app group container. A copy not signed into the group
    /// writes nothing.
    private func startWidgets() {
        guard let widgets = WidgetPublisher.live() else { return }
        self.widgets = widgets
        widgetWatch = store.$state.sink { [weak widgets] fresh in
            MainActor.assumeIsolated {
                _ = widgets?.publish(fresh)
            }
        }
    }

    public func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if quitting {
            return .terminateLater
        }
        quitting = true
        hotkey?.unregister()
        let supervisor = self.supervisor
        DispatchQueue.global(qos: .userInitiated).async {
            supervisor.stop()
            RunLoop.main.perform(inModes: [.common, .modalPanel]) {
                MainActor.assumeIsolated {
                    NSApplication.shared.reply(toApplicationShouldTerminate: true)
                }
            }
            CFRunLoopWakeUp(CFRunLoopGetMain())
        }
        return .terminateLater
    }

    public func applicationWillTerminate(_ notification: Notification) {
        hotkey?.unregister()
        supervisor.stop()
    }

    private func registerLoginItemIfInstalled() {
        LoginItem.applyChoice()
        status.refreshLogin()
    }
}

@MainActor
public enum LifelineLauncher {
    private static var delegate: AppDelegate?

    public static func run() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        Self.delegate = delegate
        app.delegate = delegate
        app.setActivationPolicy(.accessory)
        app.run()
    }
}

@MainActor
enum LoginItem {
    static let choiceKey = "startAtLogin"

    static var installed: Bool {
        Bundle.main.bundlePath.hasPrefix(NSHomeDirectory() + "/Applications/") || Bundle.main.bundlePath.hasPrefix("/Applications/")
    }

    static func state() -> LoginState {
        guard installed else { return .notInstalled }
        switch SMAppService.mainApp.status {
        case .enabled:
            return .on
        case .requiresApproval:
            return .needsApproval
        default:
            return .off
        }
    }

    static func applyChoice() {
        guard installed else { return }
        let wanted = UserDefaults.standard.object(forKey: choiceKey) as? Bool ?? true
        if wanted, SMAppService.mainApp.status != .enabled {
            try? SMAppService.mainApp.register()
        }
    }

    static func set(_ on: Bool) -> LoginState {
        guard installed else { return .notInstalled }
        UserDefaults.standard.set(on, forKey: choiceKey)
        do {
            if on {
                try SMAppService.mainApp.register()
            } else {
                try SMAppService.mainApp.unregister()
            }
        } catch {
            return .failed
        }
        return state()
    }
}
