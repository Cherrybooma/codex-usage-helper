import AppKit
import SwiftUI
import CoreGraphics

struct TaskRow: Decodable, Identifiable {
    var task_ids: [String]?
    var id: String { is_other == true ? "__other_bucket__" : "folder:\(task_id ?? name)" }
    let name: String
    let total_tokens: Int
    let percent: Double
    let request_records: Int
    let is_other: Bool?
    let sessions_count: Int?
    let task_id: String?
    let display_name: String?
    let project_status: String?
    let icon_type: String?
    let icon_value: String?
    let project_id: String?
    let project_url: String?
    let management_stale: Bool?
    let icon_missing: Bool?
    let project_goal: String?
    let weekly_goal: String?
    let next_step: String?
    let current_goal: String?
    let human_open_tasks: Int?
    let human_blocked_tasks: Int?
    let human_completed_tasks: Int?
    let latest_output_title: String?
    let latest_output_url: String?
    let latest_progress_summary: String?
    let project_priority: String?
    let project_health: String?
    let target_date: String?
    let last_reviewed: String?
    let project_summary: String?
    let open_tasks: Int?
    let blocked_tasks: Int?
    let completed_tasks: Int?
    let usage_linked: Bool?
    let development_progress: Double?
    let progress_source: String?
    let today_tokens: Int?
    let estimated_cost_usd: Double?
    let estimated_cost_status: String?
    let estimated_cost_lower_bound: Bool?
}

struct LocalProject: Decodable, Identifiable {
    var id: String { task_id }
    let task_id: String
    let name: String
    let display_name: String?
    let project_id: String?
    let project_status: String?
}

struct SnapshotRange: Decodable {
    let since_inclusive: String?
    let until_exclusive: String?
}

struct ManagementConnectionStatus: Decodable {
    let connected: Bool?
    let state: String?
    let message: String?
    let projects_url: String?
    let usage_url: String?
    let last_synced_at: String?
}

struct QuotaWindow: Decodable, Identifiable {
    var id: String { key }
    let key: String
    let label: String
    let used_percent: Double
    let remaining_percent: Double
    let duration_mins: Int
    let resets_at: Double
}

struct Quota: Decodable {
    let observed_at: String
    let plan: String?
    let windows: [QuotaWindow]
    let stale: Bool?
    let age_seconds: Int?
}

struct WeeklyBudget: Decodable {
    let status: String
    let period_start: Double?
    let period_end: Double?
    let observed_total_percent: Double?
    let excess_percent: Double?
    let reset_count: Int
    let notes: [String]
    let usage_quality: String?
}

struct Snapshot: Decodable {
    let generated_at: String
    let total_tokens: Int
    let today_tokens: Int?
    let request_records: Int
    let legacy_updates: Int
    let tasks: [TaskRow]
    // Read legacy snapshots without losing small projects during an upgrade.
    let other_tasks: [TaskRow]?
    let all_usage_tasks: [TaskRow]?
    let task_count: Int?
    let estimated_cost_usd: Double?
    let estimated_cost_status: String?
    let estimated_cost_lower_bound: Bool?
    let management_projects: [TaskRow]?
    let quota: Quota?
    let quota_error: String?
    let weekly_budget: WeeklyBudget?
    let range: SnapshotRange?
    let warnings_count: Int?
    let dataset_id: String?
    let available_projects: [LocalProject]?
    let management: ManagementConnectionStatus?
}

enum SnapshotCache {
    static func decode(_ data: Data) -> Snapshot? {
        guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        // Presentation caches are disposable; source ledgers and project IDs are not.
        // An old Notion cache cannot masquerade as the newly selected local provider.
        if json["notion"] != nil && json["management_provider"] as? String != "markdown" { return nil }
        return try? JSONDecoder().decode(Snapshot.self, from: data)
    }
}

/// Cross-application navigation is read/open only: URI actions that append or
/// overwrite a note are deliberately rejected by this desktop viewer.
enum ProjectDestination {
    static func obsidianURL(_ value: String) -> URL? {
        guard value.count <= 8192,
              value.rangeOfCharacter(from: .controlCharacters) == nil,
              let parts = URLComponents(string: value), parts.scheme?.lowercased() == "obsidian",
              parts.host == "open", parts.path.isEmpty || parts.path == "/",
              parts.user == nil, parts.password == nil, parts.port == nil,
              parts.fragment == nil, let query = parts.queryItems else { return nil }
        let keys = query.map(\.name)
        guard Set(keys).count == keys.count, Set(keys).isSubset(of: ["vault", "file", "path", "paneType"]),
              query.allSatisfy({ $0.value?.isEmpty == false && $0.value?.rangeOfCharacter(from: .controlCharacters) == nil }) else { return nil }
        let values = Dictionary(uniqueKeysWithValues: query.map { ($0.name, $0.value!) })
        if let pane = values["paneType"], !["tab", "split", "window"].contains(pane) { return nil }
        if let path = values["path"] {
            guard values["vault"] == nil, values["file"] == nil, path.hasPrefix("/"),
                  !path.split(separator: "/").contains("..") else { return nil }
        } else {
            guard let vault = values["vault"], !vault.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return nil }
            if let file = values["file"] {
                guard !file.hasPrefix("/"), !file.split(separator: "/").contains("..") else { return nil }
            }
        }
        return parts.url
    }

    static func url(_ value: String) -> URL? {
        if let url = obsidianURL(value) { return url }
        // Existing published HTTPS evidence remains readable after migration.
        guard let parts = URLComponents(string: value), parts.scheme == "https",
              parts.host?.isEmpty == false, parts.user == nil, parts.password == nil,
              value.rangeOfCharacter(from: .whitespacesAndNewlines.union(.controlCharacters)) == nil else { return nil }
        return parts.url
    }
}

func compactTokens(_ value: Int) -> String {
    value >= 1_000_000 ? String(format: "%.2fM", Double(value) / 1_000_000) :
    value >= 1_000 ? String(format: "%.1fK", Double(value) / 1_000) : "\(value)"
}

func pointInCapsule(_ point: CGPoint, rect: CGRect) -> Bool {
    pointInRoundedRectangle(point, rect: rect, radius: min(rect.width, rect.height) / 2)
}

func pointInRoundedRectangle(_ point: CGPoint, rect: CGRect, radius requestedRadius: CGFloat) -> Bool {
    guard rect.contains(point) else { return false }
    let radius = max(0, min(requestedRadius, min(rect.width, rect.height) / 2))
    let nearest = CGPoint(x: min(rect.maxX - radius, max(rect.minX + radius, point.x)),
                          y: min(rect.maxY - radius, max(rect.minY + radius, point.y)))
    let dx = point.x - nearest.x, dy = point.y - nearest.y
    return dx * dx + dy * dy <= radius * radius
}

func localDate(_ seconds: Double) -> String {
    let format = DateFormatter()
    format.timeZone = TimeZone(identifier: "Asia/Shanghai")
    format.dateFormat = "M月d日"
    return format.string(from: Date(timeIntervalSince1970: seconds))
}

func timestampLabel(_ value: String) -> String {
    let iso = ISO8601DateFormatter()
    iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    var date = iso.date(from: value)
    if date == nil { iso.formatOptions = [.withInternetDateTime]; date = iso.date(from: value) }
    guard let date else { return "时间待核实" }
    let format = DateFormatter()
    format.timeZone = TimeZone(identifier: "Asia/Shanghai")
    format.dateFormat = "HH:mm"
    return format.string(from: date)
}

enum ManagementSyncResponse {
    static let fallback = "Markdown 更新未完成，本地记录已保留。"

    static func message(_ data: Data) -> String {
        guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return fallback }
        if json["ok"] as? Bool == true { return "Markdown 管理状态和账本摘要已更新。" }
        if let error = json["error"] as? [String: Any],
           let message = error["message"] as? String,
           !message.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return message
        }
        return fallback
    }
}

@MainActor
final class UsageModel: ObservableObject {
    @Published var snapshot: Snapshot?
    @Published var refreshing = false
    @Published var error: String?
    @Published var managementSyncing = false
    @Published var managementSyncMessage: String?
    @Published private(set) var managementEnabled = false
    @Published private(set) var widgetStyle: WidgetStyle = .glass
    @Published var enabledWidgets: Set<WidgetKind> = [.horizontal]
    var onVisibility: (() -> Void)?
    var onUpdated: (() -> Void)?
    let root: URL
    let python: URL
    var timer: Timer?
    var managementTimer: Timer?
    var settingsController: ProjectSettingsController?

    init(root: URL) {
        self.root = root
        python = root.appendingPathComponent(".venv/bin/python3")
        let saved = try? WidgetPreferences.read(root: root)
        let settings = saved ?? [:]
        managementEnabled = saved.map { WidgetPreferences.managementEnabled(in: $0) } ?? false
        widgetStyle = WidgetStyle(rawValue: settings["widget_style"] as? String ?? "") ?? .glass
        enabledWidgets = WidgetDisplayPreferences.restore(root: root)
    }

    func start() {
        let cacheURL = root.appendingPathComponent("widget/data/latest.json")
        Task { [weak self] in
            let cached: Snapshot? = await Task.detached(priority: .utility) {
                guard let data = try? Data(contentsOf: cacheURL) else { return nil }
                return SnapshotCache.decode(data)
            }.value
            // A delayed cache read must not replace a newer completed refresh.
            if self?.snapshot == nil, let cached {
                self?.snapshot = cached
                self?.onUpdated?()
            }
        }
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
        managementTimer = Timer.scheduledTimer(withTimeInterval: 300, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.syncManagement() }
        }
    }

    func openSettings() {
        if settingsController == nil { settingsController = ProjectSettingsController(usage: self) }
        NSApp.setActivationPolicy(.regular)
        settingsController?.showWindow(nil)
        NSApp.activate(ignoringOtherApps: true)
        settingsController?.window?.makeKeyAndOrderFront(nil)
    }

    func openProject(_ value: String) {
        guard let url = ProjectDestination.url(value) else { return }
        NSWorkspace.shared.open(url)
    }

    func syncManagement(force: Bool = false) {
        guard managementEnabled else {
            managementSyncMessage = "仅计量模式；Markdown 项目管理已关闭。"
            return
        }
        guard !managementSyncing else { return }
        if !force && snapshot?.management?.connected != true { return }
        managementSyncing = true
        managementSyncMessage = nil
        let executable = python, folder = root
        Task {
            let result: Result<Data, Error> = await Task.detached(priority: .utility) {
                do {
                    var args = [folder.appendingPathComponent("widget/widget_backend.py").path, "--sync-markdown"]
                    if force { args.append("--force-sync") }
                    return .success(try runWidgetProcess(python: executable, root: folder, arguments: args, timeout: 180))
                } catch { return .failure(error) }
            }.value
            switch result {
            case .success(let data):
                managementSyncMessage = ManagementSyncResponse.message(data)
            case .failure: managementSyncMessage = "Markdown 更新未完成，本地记录已保留。"
            }
            managementSyncing = false
            refresh()
        }
    }

    func setManagementEnabled(_ enabled: Bool) {
        // Finish an in-flight local publication before switching modes.
        guard !managementSyncing else { return }
        do {
            try WidgetPreferences.update(root: root, values: ["management_enabled": enabled])
            managementEnabled = enabled
            managementSyncMessage = enabled ? "已开启；每 5 分钟更新 Markdown 摘要。" : "仅计量模式；Markdown 项目管理已关闭。"
            onUpdated?()
        } catch { managementSyncMessage = "运行方式未保存，保留原设置。" }
    }

    func setWidgetStyle(_ style: WidgetStyle) {
        do {
            try WidgetPreferences.update(root: root, values: ["widget_style": style.rawValue])
            widgetStyle = style
            onUpdated?()
        } catch { self.error = "外观未保存，保留当前设置。" }
    }

    func toggleWidget(_ kind: WidgetKind) {
        if enabledWidgets.contains(kind) { enabledWidgets.remove(kind) }
        else { enabledWidgets.insert(kind) }
        try? WidgetDisplayPreferences.save(enabledWidgets, root: root)
        onVisibility?()
    }

    func refresh() {
        guard !refreshing else { return }
        refreshing = true
        error = nil
        let pythonURL = python
        let folder = root
        Task {
            let result: Result<Data, Error> = await Task.detached(priority: .utility) {
                do {
                    return .success(try runWidgetProcess(python: pythonURL, root: folder,
                        arguments: [folder.appendingPathComponent("widget/widget_backend.py").path]))
                } catch { return .failure(error) }
            }.value
            switch result {
            case .success(let data):
                do { snapshot = try JSONDecoder().decode(Snapshot.self, from: data) }
                catch { self.error = "数据暂时无法解析，保留上次显示。" }
            case .failure(let error): self.error = error.localizedDescription
            }
            refreshing = false
            onUpdated?()
        }
    }
}

class FloatingPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
    var backgroundDragRegion: ((CGPoint) -> Bool)?
    var onBackgroundDrag: ((Bool) -> Void)?

    override func sendEvent(_ event: NSEvent) {
        if event.type == .leftMouseDown, backgroundDragRegion?(event.locationInWindow) == true {
            // Intercept before NSHostingView can route this background press
            // to a rectangular SwiftUI tracking or button area.
            onBackgroundDrag?(true)
            performDrag(with: event)
            onBackgroundDrag?(false)
            return
        }
        super.sendEvent(event)
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    var strips: [WidgetKind: FloatingPanel] = [:]
    var interactions: [WidgetKind: StripInteraction] = [:]
    var model: UsageModel!
    var statusItem: NSStatusItem!
    var localEvents: Any?
    var globalEvents: Any?
    var pointerTimer: Timer?
    var pointerZones: [WidgetKind: String] = [:]
    var positions: [String: [String: Double]] = [:]
    var headerOffsets: [WidgetKind: CGFloat] = [:]
    var resizingStrips = false
    let root: URL
    let diagnosticsQueue = DispatchQueue(label: "local.codex.widget-diagnostics", qos: .utility)

    init(root: URL) { self.root = root }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        NSApp.mainMenu = WidgetApplicationMenu.make(settingsTarget: self)
        model = UsageModel(root: root)
        restorePositions()
        for kind in [WidgetKind.horizontal, .vertical] {
            let state = StripInteraction()
            let strip = makePanel(width: 108, height: 108)
            strip.title = "用量小助手 · \(kind.title)"
            // Start with the approved pale-glass capsule appearance.
            strip.appearance = NSAppearance(named: .aqua)
            strip.hasShadow = false
            strip.isMovableByWindowBackground = false
            strip.backgroundDragRegion = { [weak self, weak strip] location in
                guard let self, let strip else { return false }
                let point = CGPoint(x: location.x, y: strip.frame.height - location.y)
                return self.stripHitTarget(kind, at: point) == .background
            }
            strip.onBackgroundDrag = { [weak self, weak state] dragging in
                if dragging { state?.beginDragging() } else { state?.endDragging() }
                self?.pointerZones.removeValue(forKey: kind)
                if !dragging { self?.updatePointerPassthrough() }
            }
            interactions[kind] = state
            strips[kind] = strip
            state.onResize = { [weak self] in self?.resizeStrip(kind) }
            resizeStrip(kind, initial: true)
        }
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.image = NSImage(systemSymbolName: "chart.bar.xaxis", accessibilityDescription: "用量小助手")
        statusItem.button?.imagePosition = .imageLeading
        let menu = NSMenu()
        menu.autoenablesItems = false
        let label = NSMenuItem(title: "展示形式 · 可同时开启", action: nil, keyEquivalent: "")
        label.isEnabled = false
        menu.addItem(label)
        for kind in WidgetKind.allCases {
            let item = menu.addItem(withTitle: kind.title, action: #selector(toggleDisplay(_:)), keyEquivalent: "")
            item.representedObject = kind.rawValue
        }
        menu.addItem(.separator())
        let themeItem = NSMenuItem(title: "外观", action: nil, keyEquivalent: "")
        themeItem.submenu = NSMenu(title: "外观")
        for style in WidgetStyle.allCases {
            let item = themeItem.submenu!.addItem(withTitle: style.title, action: #selector(selectStyle(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = "theme:" + style.rawValue
        }
        menu.addItem(themeItem)
        menu.addItem(withTitle: "项目与 Markdown 设置…", action: #selector(openSettings), keyEquivalent: ",")
        menu.addItem(withTitle: "周期核算…", action: #selector(openSettings), keyEquivalent: "")
        menu.addItem(withTitle: "更新 Markdown 摘要", action: #selector(syncManagement), keyEquivalent: "")
        menu.addItem(withTitle: "刷新", action: #selector(refresh), keyEquivalent: "")
        menu.addItem(.separator())
        menu.addItem(withTitle: "退出用量小助手", action: #selector(quit), keyEquivalent: "q")
        for item in menu.items { item.target = self }
        statusItem.menu = menu
        model.onVisibility = { [weak self] in self?.updateVisibility() }
        model.onUpdated = { [weak self] in
            self?.updateStatus()
            for kind in [WidgetKind.horizontal, .vertical] { self?.resizeStrip(kind) }
            self?.recordWindowState()
        }
        localEvents = NSEvent.addLocalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown, .keyDown]) { [weak self] event in
            guard let self else { return event }
            if event.type == .keyDown && event.keyCode == 53 {
                self.dismissDetails()
                return nil
            }
            if event.type != .keyDown {
                for (kind, window) in self.strips where event.window !== window {
                    self.interactions[kind]?.dismiss()
                }
            }
            return event
        }
        globalEvents = NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) { [weak self] _ in
            Task { @MainActor in self?.dismissDetails() }
        }
        NotificationCenter.default.addObserver(self, selector: #selector(recordWindowState),
            name: NSWindow.didChangeOcclusionStateNotification, object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(windowMoved(_:)),
            name: NSWindow.didMoveNotification, object: nil)
        pointerTimer = Timer.scheduledTimer(withTimeInterval: 0.06, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.updatePointerPassthrough() }
        }
        updateStatus()
        updateVisibility()
        model.start()
        if CommandLine.arguments.contains("--settings") { model.openSettings() }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        model.openSettings()
        return true
    }

    func makePanel(width: CGFloat, height: CGFloat) -> FloatingPanel {
        let window = FloatingPanel(contentRect: NSRect(x: 0, y: 0, width: width, height: height),
            styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        window.level = desktopLevel()
        window.isOpaque = false
        window.backgroundColor = .clear
        window.hasShadow = true
        window.isMovableByWindowBackground = true
        window.hidesOnDeactivate = false
        window.becomesKeyOnlyIfNeeded = false
        window.acceptsMouseMovedEvents = true
        window.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle]
        return window
    }

    func dismissDetails() {
        for state in interactions.values { state.dismiss() }
    }

    func resizeStrip(_ kind: WidgetKind, initial: Bool = false) {
        guard let window = strips[kind], let state = interactions[kind] else { return }
        resizingStrips = true
        defer { resizingStrips = false }
        let items = StripItem.items(model.snapshot, failed: model.error != nil, metric: state.quotaMetric, managementEnabled: model.managementEnabled)
        let vertical = kind == .vertical
        let natural = UsageStrip.canvasSize(itemCount: items.count, vertical: vertical)
        // Reserve a fixed overlay budget so hover never moves its anchor.
        let savedScreen = positions[kind.rawValue]?["screen"]
        let screen = initial ? (NSScreen.screens.first { Double(($0.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value ?? 0) == savedScreen } ?? NSScreen.main) : (window.screen ?? NSScreen.main)
        let bounds = screen?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let width = min(natural.width, bounds.width - 16)
        let height = min(natural.height, max(0, bounds.height - 16))
        var x = window.frame.minX
        var anchorTop = window.frame.maxY - (headerOffsets[kind] ?? 0)
        if initial {
            x = vertical ? bounds.maxX - width - 24 : bounds.midX - width / 2
            anchorTop = bounds.maxY - (vertical ? 96 : 32)
            if let saved = positions[kind.rawValue] {
                x = bounds.minX + min(1, max(0, saved["x"] ?? 0.5)) * max(0, bounds.width - width)
                if let normalized = saved["anchor_top"] {
                    anchorTop = bounds.maxY - min(1, max(0, normalized)) * max(0, bounds.height - UsageStrip.naturalSize(itemCount: items.count, vertical: vertical).height)
                } else if let normalized = saved["top"] {
                    anchorTop = bounds.maxY - min(1, max(0, normalized)) * max(0, bounds.height - height)
                }
            }
        } else if window.frame.width != width { x = vertical ? window.frame.maxX - width : window.frame.midX - width / 2 }
        let headerHeight = min(height, UsageStrip.naturalSize(itemCount: items.count, vertical: vertical).height)
        let placement = UsageStrip.placement(anchor: CGPoint(x: x, y: anchorTop), size: CGSize(width: width, height: height), headerHeight: headerHeight, bounds: bounds)
        let offset = placement.headerOffset
        headerOffsets[kind] = offset
        window.appearance = model.widgetStyle.appearance
        let view = UsageStrip(model: model, interaction: state, vertical: vertical, height: height, canvasWidth: width, headerOffset: offset)
        if let host = window.contentView as? NSHostingView<UsageStrip> { host.rootView = view }
        else { window.contentView = NSHostingView(rootView: view) }
        window.setFrame(placement.frame, display: true)
        updatePointerPassthrough()
        recordWindowState()
    }

    func updatePointerPassthrough() {
        let mouse = NSEvent.mouseLocation
        for (kind, window) in strips {
            guard let state = interactions[kind], window.isVisible else { continue }
            let items = StripItem.items(model.snapshot, failed: model.error != nil, metric: state.quotaMetric, managementEnabled: model.managementEnabled)
            let x = mouse.x - window.frame.minX
            let y = window.frame.maxY - mouse.y
            let point = CGPoint(x: x, y: y)
            let target = stripHitTarget(kind, at: point)
            var hovered: String?
            switch target {
            case .module(let index): if items.indices.contains(index) { hovered = items[index].id }
            case .detail, .connector: hovered = state.expanded
            case .background, .outside: break
            }
            // The visible rail background receives dragging without selecting
            // a module. Only the unused transparent canvas passes through.
            window.ignoresMouseEvents = !target.acceptsMouse
            if pointerZones[kind] != hovered {
                if hovered == nil { state.leaveAllHoverZones() }
                else if let previous = pointerZones[kind] { state.hover(previous, inside: false, zone: "native-pointer") }
                if let hovered { state.hover(hovered, inside: true, zone: "native-pointer") }
                pointerZones[kind] = hovered
            }
        }
    }

    func stripHitTarget(_ kind: WidgetKind, at point: CGPoint) -> StripHitTarget {
        guard let window = strips[kind], let state = interactions[kind], model != nil else { return .outside }
        let items = StripItem.items(model.snapshot, failed: model.error != nil, metric: state.quotaMetric, managementEnabled: model.managementEnabled)
        let canvas = window.frame.size
        let vertical = kind == .vertical
        let offset = headerOffsets[kind] ?? 0
        let selectedIndex = items.firstIndex { $0.id == state.expanded }
        let detail = selectedIndex.map { index in
            UsageStrip.detailFrame(for: items[index], index: index, itemCount: items.count, vertical: vertical, canvas: canvas,
                headerOffset: offset, locked: state.locked, page: state.dashboardPage, sectionPage: state.sectionPage)
        }
        return UsageStrip.hitTarget(at: point, itemCount: items.count, vertical: vertical, canvas: canvas,
            headerOffset: offset, detail: detail, detailIndex: selectedIndex)
    }

    @objc func windowMoved(_ notification: Notification) {
        guard !resizingStrips else { return }
        guard let window = notification.object as? NSWindow, let screen = window.screen else { return }
        guard let kind = strips.first(where: { $0.value === window })?.key else { return }
        let bounds = screen.visibleFrame
        var saved = ["screen": Double((screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value ?? 0),
            "x": (window.frame.minX - bounds.minX) / max(1, bounds.width - window.frame.width),
            "top": (bounds.maxY - window.frame.maxY) / max(1, bounds.height - window.frame.height)]
        let items = StripItem.items(model.snapshot, failed: model.error != nil, metric: interactions[kind]?.quotaMetric ?? .weeklyUsed, managementEnabled: model.managementEnabled)
        let anchorTop = window.frame.maxY - (headerOffsets[kind] ?? 0)
        saved["anchor_top"] = (bounds.maxY - anchorTop) / max(1, bounds.height - UsageStrip.naturalSize(itemCount: items.count, vertical: kind == .vertical).height)
        positions[kind.rawValue] = saved
        savePositions()
    }

    func restorePositions() {
        if let data = try? Data(contentsOf: root.appendingPathComponent("widget/data/positions.json")),
           let saved = try? JSONDecoder().decode([String: [String: Double]].self, from: data) {
            positions = saved.filter { WidgetKind(rawValue: $0.key) != nil }
            if positions.count != saved.count { savePositions() }
        }
    }

    func savePositions() {
        if let data = try? JSONEncoder().encode(positions) {
            try? data.write(to: root.appendingPathComponent("widget/data/positions.json"), options: .atomic)
        }
    }

    func updateVisibility() {
        for kind in WidgetKind.allCases {
            guard let window = strips[kind] else { continue }
            if model.enabledWidgets.contains(kind) { window.orderFrontRegardless() }
            else { interactions[kind]?.dismiss(); window.orderOut(nil) }
        }
        for item in statusItem.menu?.items ?? [] {
            if let raw = item.representedObject as? String, let kind = WidgetKind(rawValue: raw) {
                item.state = model.enabledWidgets.contains(kind) ? .on : .off
            }
        }
        recordWindowState()
    }

    @objc func recordWindowState() {
        guard model != nil else { return }
        var windows: [[String: Any]] = []
        for kind in WidgetKind.allCases {
            guard let window = strips[kind] else { continue }
            windows.append(["kind": kind.rawValue, "level": window.level.rawValue,
                "is_visible": window.isVisible, "occlusion_visible": window.occlusionState.contains(.visible),
                "stationary": window.collectionBehavior.contains(.stationary),
                "x": window.frame.minX, "y": window.frame.minY, "width": window.frame.width, "height": window.frame.height,
                "expanded": interactions[kind]?.expanded ?? "", "locked": interactions[kind]?.locked ?? false])
        }
        let value: [String: Any] = ["recorded_at": ISO8601DateFormatter().string(from: Date()), "widget_style": model.widgetStyle.rawValue,
            "desktop_level": desktopLevel().rawValue,
            "enabled": model.enabledWidgets.map(\.rawValue).sorted(), "windows": windows]
        let destination = root.appendingPathComponent("widget/data/window-state.json")
        diagnosticsQueue.async {
            if let data = try? JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys]) {
                try? data.write(to: destination, options: .atomic)
            }
        }
    }

    func desktopLevel() -> NSWindow.Level {
        NSWindow.Level(rawValue: Int(CGWindowLevelForKey(.desktopIconWindow)) + 1)
    }

    func updateStatus() {
        for parent in statusItem.menu?.items ?? [] {
            for item in parent.submenu?.items ?? [] {
                if let raw = item.representedObject as? String, raw.hasPrefix("theme:") {
                    item.state = raw == "theme:" + model.widgetStyle.rawValue ? .on : .off
                }
            }
        }
        if let syncItem = statusItem.menu?.items.first(where: { $0.action == #selector(syncManagement) }) {
            syncItem.isEnabled = model.managementEnabled && !model.managementSyncing
        }
        if let snapshot = model.snapshot,
           let week = snapshot.quota?.windows.first(where: { $0.duration_mins == 10080 }) {
            let cached = snapshot.quota?.stale == true || snapshot.quota_error != nil
            let value = week.remaining_percent > 0 && week.remaining_percent < 1 ? "<1" : String(format: "%.0f", week.remaining_percent)
            statusItem.button?.title = " 剩余 \(cached ? "≈" : "")\(value)%"
            statusItem.button?.toolTip = "官方周额度剩余；\(compactTokens(snapshot.total_tokens)) 本机 tokens / 本周"
        } else { statusItem.button?.title = " 用量" }
    }

    @objc func toggleDisplay(_ item: NSMenuItem) {
        guard let raw = item.representedObject as? String, let kind = WidgetKind(rawValue: raw) else { return }
        model.toggleWidget(kind)
    }
    @objc func selectStyle(_ item: NSMenuItem) {
        guard let raw = item.representedObject as? String, let style = WidgetStyle(rawValue: String(raw.dropFirst(6))) else { return }
        model.setWidgetStyle(style)
    }
    @objc func refresh() { model.refresh() }
    @objc func openSettings() { model.openSettings() }
    @objc func syncManagement() { model.syncManagement(force: true) }
    @objc func quit() { NSApp.terminate(nil) }
}

#if !WIDGET_CHECKS
@main
struct WidgetLauncher {
    @MainActor static func main() {
        let arguments = CommandLine.arguments
        let bundledRoot = Bundle.main.bundleURL.deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        let rootURL = arguments.count > 1 && !arguments[1].hasPrefix("-") ?
            URL(fileURLWithPath: arguments[1]) : bundledRoot
        let app = NSApplication.shared
        let delegate = AppDelegate(root: rootURL)
        app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
    }
}

#endif
