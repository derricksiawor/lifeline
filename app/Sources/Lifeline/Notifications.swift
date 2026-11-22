import AppKit
import Combine
import LifelineKit
import UserNotifications

@MainActor
protocol NoticePosting: AnyObject {
    func register(_ titles: [String])
    func post(_ notice: Notice, done: @escaping @MainActor @Sendable (Bool) -> Void)
    func clearCancels(_ ids: [String])
}

@MainActor
final class SystemPoster: NoticePosting {
    private let center: UNUserNotificationCenter
    private var registered: [String] = []

    init(center: UNUserNotificationCenter) {
        self.center = center
    }

    func register(_ titles: [String]) {
        let wanted = registered + titles.filter { !registered.contains($0) }
        guard wanted != registered else { return }
        registered = wanted
        let categories = wanted.map { title in
            UNNotificationCategory(
                identifier: Notices.category(for: title),
                actions: [
                    UNNotificationAction(identifier: Notices.primaryAction, title: title, options: []),
                    UNNotificationAction(identifier: Notices.laterAction, title: Notices.later, options: [])
                ],
                intentIdentifiers: [],
                options: []
            )
        }
        center.setNotificationCategories(Set(categories))
    }

    func post(_ notice: Notice, done: @escaping @MainActor @Sendable (Bool) -> Void) {
        let content = UNMutableNotificationContent()
        content.title = notice.title
        content.body = notice.body
        content.sound = .default
        content.threadIdentifier = notice.account ?? "lifeline"
        if let action = notice.action {
            register([action.title])
            content.categoryIdentifier = Notices.category(for: action.title)
            content.userInfo = Notices.userInfo(for: action.command)
        }
        let request = UNNotificationRequest(identifier: notice.id, content: content, trigger: nil)
        center.add(request) { @Sendable error in
            let ok = error == nil
            Task { @MainActor in
                done(ok)
            }
        }
    }

    func clearCancels(_ ids: [String]) {
        if !ids.isEmpty {
            center.removeDeliveredNotifications(withIdentifiers: ids)
        }
        let cancel = Notices.category(for: Notices.cancelTitle)
        center.getDeliveredNotifications { @Sendable delivered in
            let stale = delivered.filter { $0.request.content.categoryIdentifier == cancel }.map(\.request.identifier)
            guard !stale.isEmpty else { return }
            Task { @MainActor in
                UNUserNotificationCenter.current().removeDeliveredNotifications(withIdentifiers: stale)
            }
        }
    }

    func remove(_ id: String) {
        center.removeDeliveredNotifications(withIdentifiers: [id])
    }
}

@MainActor
final class Notifier: NSObject, ObservableObject, UNUserNotificationCenterDelegate {
    static let seenKey = "shownNotices"

    @Published private(set) var permission: NoticePermission = .waiting
    private let store: StateStore
    private let shown: (ServiceState) -> ShownMenu?
    private let open: () -> Void
    private let defaults: UserDefaults?
    private var poster: NoticePosting?
    private var system: SystemPoster?
    private var watch: AnyCancellable?
    private var ledger: NoticeLedger
    private var cancels: [String] = []
    private var swept = false
    private var jobStates: [String: String]?

    init(
        store: StateStore, shown: @escaping (ServiceState) -> ShownMenu?, open: @escaping () -> Void, defaults: UserDefaults? = .standard
    ) {
        self.store = store
        self.shown = shown
        self.open = open
        self.defaults = defaults
        var loaded: [String: Date] = [:]
        if let stored = defaults?.dictionary(forKey: Self.seenKey) {
            for (id, value) in stored {
                if let seconds = value as? Double {
                    loaded[id] = Date(timeIntervalSince1970: seconds)
                }
            }
        }
        ledger = NoticeLedger(seen: loaded, now: Date())
        super.init()
    }

    static var bundled: Bool {
        Bundle.main.bundleIdentifier != nil && Bundle.main.bundleURL.pathExtension == "app"
    }

    func start() {
        guard Self.bundled else {
            permission = .unavailable
            return
        }
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        let system = SystemPoster(center: center)
        self.system = system
        begin(system)
        center.requestAuthorization(options: [.alert, .sound]) { @Sendable [weak self] _, _ in
            Task { @MainActor in
                self?.refreshPermission()
            }
        }
        NotificationCenter.default.addObserver(forName: NSWindow.didBecomeKeyNotification, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated {
                self?.refreshPermission()
            }
        }
    }

    func begin(_ poster: NoticePosting, permission known: NoticePermission? = nil) {
        self.poster = poster
        store.refusalPosted = { [weak self] sentence in
            self?.tell(sentence)
        }
        if let known {
            permission = known
        }
        watch = store.$state.sink { [weak self] fresh in
            guard let fresh else { return }
            MainActor.assumeIsolated {
                self?.noticeJobs(fresh)
                self?.consider(fresh)
            }
        }
    }

    func refreshPermission() {
        guard system != nil else { return }
        UNUserNotificationCenter.current().getNotificationSettings { @Sendable [weak self] settings in
            let status = settings.authorizationStatus
            let style: NoticeStyle
            switch settings.alertStyle {
            case .alert: style = .alert
            case .banner: style = .banner
            default: style = .none
            }
            Task { @MainActor in
                guard let self else { return }
                let authorized = status == .authorized || status == .provisional
                let next = NoticePermission.from(authorized: authorized, denied: status == .denied, style: style)
                if self.permission == .failed, next.posts {
                    return
                }
                self.permission = next
                if next.posts, let state = self.store.state {
                    self.consider(state)
                }
            }
        }
    }

    func consider(_ fresh: ServiceState) {
        guard let poster, permission.posts else { return }
        poster.register(Notices.titles(for: fresh))
        if Notices.countdownGone(fresh), !cancels.isEmpty || !swept {
            poster.clearCancels(cancels)
            cancels = []
            swept = true
        }
        let now = Date()
        let plan = ledger.take(state: fresh, shown: shown(fresh), now: now)
        save()
        for notice in plan.post {
            if notice.action?.command == .cancel {
                cancels.append(notice.id)
            }
            poster.post(notice) { [weak self] ok in
                self?.finished(notice.id, ok: ok)
            }
        }
    }

    func noticeJobs(_ fresh: ServiceState) {
        let before = jobStates
        jobStates = JobFlow.states(fresh)
        guard let before, let poster, permission.posts, shown(fresh) == nil else { return }
        let seen = store.seenJobs.union(store.cancelling)
        for job in JobFlow.ended(before: before, now: fresh) where !seen.contains(job.id) {
            guard !(job.result ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { continue }
            poster.post(JobFlow.notice(job, state: fresh)) { _ in }
        }
    }

    private func finished(_ id: String, ok: Bool) {
        if ok {
            ledger.sent(id, at: Date())
            save()
            if permission == .failed {
                permission = .waiting
                refreshPermission()
            }
        } else {
            ledger.failed(id)
            permission = .failed
        }
    }

    private func save() {
        defaults?.set(ledger.seen.mapValues { $0.timeIntervalSince1970 }, forKey: Self.seenKey)
    }

    private func tell(_ sentence: String) {
        guard let poster, permission.posts else { return }
        if let state = store.state, shown(state) != nil {
            return
        }
        poster.post(Notices.refusal(sentence, id: "refused-" + UUID().uuidString)) { _ in }
    }

    func respond(action: String, info: [String: String], id: String) {
        switch action {
        case Notices.primaryAction:
            if let command = Notices.command(from: info) {
                store.run(command, via: .notification, keepNote: true) { [weak self] outcome in
                    switch outcome {
                    case .refused(let sentence):
                        self?.tell(sentence)
                    case .done(let note?):
                        self?.tell(note)
                    default:
                        break
                    }
                }
            }
        case Notices.laterAction:
            system?.remove(id)
            store.later(id)
        case UNNotificationDefaultActionIdentifier:
            open()
        default:
            break
        }
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .list, .sound])
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        let action = response.actionIdentifier
        let id = response.notification.request.identifier
        var info: [String: String] = [:]
        for (key, value) in response.notification.request.content.userInfo {
            if let key = key as? String, let value = value as? String {
                info[key] = value
            }
        }
        let found = info
        Task { @MainActor [weak self] in
            self?.respond(action: action, info: found, id: id)
        }
        completionHandler()
    }
}
