import AppKit
import SwiftUI
import CoreGraphics

struct TaskRow: Decodable, Identifiable {
    var id: String { is_other == true ? "__other_bucket__" : "folder:\(task_id ?? name)" }
    let name: String
    let total_tokens: Int
    let percent: Double
    let request_records: Int
    let is_other: Bool?
    let members_count: Int?
    let sessions_count: Int?
    let task_id: String?
    let display_name: String?
    let project_status: String?
    let icon_type: String?
    let icon_value: String?
    let notion_url: String?
    let notion_stale: Bool?
    let notion_icon_missing: Bool?
    let project_goal: String?
    let weekly_goal: String?
    let next_step: String?
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
}

struct LocalProject: Decodable, Identifiable {
    var id: String { task_id }
    let task_id: String
    let name: String
    let display_name: String?
    let notion_page_id: String?
    let project_status: String?
}

struct SnapshotRange: Decodable {
    let since_inclusive: String?
    let until_exclusive: String?
}

struct NotionConnectionStatus: Decodable {
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
}

extension WeeklyBudget {
    var stripUsageLabel: String {
        observed_total_percent.map { String(format: "≈%.1f%%", $0) } ?? "待核实"
    }
    var stripExcessLabel: String {
        if let total = observed_total_percent, total <= 100 { return "未超额" }
        guard let excess = excess_percent else { return "待核实" }
        if excess > 0 { return String(format: status == "incomplete" ? "≥%.1f%%" : "+%.1f%%", excess) }
        return "未超额"
    }
}

struct Snapshot: Decodable {
    let generated_at: String
    let total_tokens: Int
    let today_tokens: Int?
    let request_records: Int
    let legacy_updates: Int
    let tasks: [TaskRow]
    let other_tasks: [TaskRow]
    let management_projects: [TaskRow]?
    let quota: Quota?
    let quota_error: String?
    let weekly_budget: WeeklyBudget?
    let range: SnapshotRange?
    let warnings_count: Int?
    let dataset_id: String?
    let available_projects: [LocalProject]?
    let notion: NotionConnectionStatus?
}

enum Palette {
    static let accent = Color(red: 0.78, green: 0.42, blue: 0.23)
    static let track = Color.primary.opacity(0.07)
    static func surface(_ scheme: ColorScheme) -> Color {
        scheme == .dark ? Color(red: 0.16, green: 0.16, blue: 0.14) :
                          Color(red: 0.98, green: 0.97, blue: 0.95)
    }
    static func warmAccent(_ scheme: ColorScheme) -> Color {
        scheme == .dark ? Color(red: 0.96, green: 0.65, blue: 0.43) : accent
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

@MainActor
final class UsageModel: ObservableObject {
    @Published var snapshot: Snapshot?
    @Published var refreshing = false
    @Published var error: String?
    @Published var compact = false
    @Published var pinned = false
    @Published var tab = 0
    @Published var showOther = false
    @Published var notionSyncing = false
    @Published var notionSyncMessage: String?
    @Published private(set) var notionEnabled = false
    @Published private(set) var widgetStyle: WidgetStyle = .glass
    @Published var enabledWidgets: Set<WidgetKind> = [.horizontal]
    var onVisibility: (() -> Void)?
    var onUpdated: (() -> Void)?
    var onResize: (() -> Void)?
    var onPin: ((Bool) -> Void)?
    let root: URL
    let python: URL
    var timer: Timer?
    var notionTimer: Timer?
    var settingsController: ProjectSettingsController?

    init(root: URL) {
        self.root = root
        python = root.appendingPathComponent(".venv/bin/python3")
        let settings = (try? WidgetPreferences.read(root: root)) ?? [:]
        notionEnabled = WidgetPreferences.notionEnabled(in: settings)
        widgetStyle = WidgetStyle(rawValue: settings["widget_style"] as? String ?? "") ?? .glass
    }

    func start() {
        let cacheURL = root.appendingPathComponent("widget/data/latest.json")
        Task { [weak self] in
            let cached: Snapshot? = await Task.detached(priority: .utility) {
                guard let data = try? Data(contentsOf: cacheURL) else { return nil }
                return try? JSONDecoder().decode(Snapshot.self, from: data)
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
        notionTimer = Timer.scheduledTimer(withTimeInterval: 300, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.syncNotion() }
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
        guard let url = URL(string: value), url.scheme == "https", let host = url.host,
              host == "notion.so" || host.hasSuffix(".notion.so") || host == "notion.site" || host.hasSuffix(".notion.site") || host == "notion.com" || host.hasSuffix(".notion.com") else { return }
        NSWorkspace.shared.open(url)
    }

    func syncNotion(force: Bool = false) {
        guard notionEnabled else {
            notionSyncMessage = "本机模式；Notion 同步已关闭。"
            return
        }
        guard !notionSyncing else { return }
        if !force && snapshot?.notion?.connected != true { return }
        notionSyncing = true
        notionSyncMessage = nil
        let executable = python, folder = root
        Task {
            let result: Result<Data, Error> = await Task.detached(priority: .utility) {
                do {
                    var args = [folder.appendingPathComponent("widget/widget_backend.py").path, "--sync-notion"]
                    if force { args.append("--force-sync") }
                    return .success(try runWidgetProcess(python: executable, root: folder, arguments: args, timeout: 180))
                } catch { return .failure(error) }
            }.value
            switch result {
            case .success(let data):
                if let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any], json["ok"] as? Bool == true {
                    notionSyncMessage = "摘要同步已检查；未绑定项目保留在本机。"
                } else { notionSyncMessage = "同步未完成，本地记录已保留。" }
            case .failure: notionSyncMessage = "同步未完成，本地记录已保留。"
            }
            notionSyncing = false
            refresh()
        }
    }

    func setNotionEnabled(_ enabled: Bool) {
        // An in-flight request may already have reached Notion; finish it first.
        guard !notionSyncing else { return }
        do {
            try WidgetPreferences.update(root: root, values: ["notion_sync_enabled": enabled])
            notionEnabled = enabled
            notionSyncMessage = enabled ? "已开启；连接完成后每 5 分钟同步摘要。" : "本机模式；Notion 同步已关闭。"
            onUpdated?()
        } catch { notionSyncMessage = "运行方式未保存，保留原设置。" }
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
        let folder = root.appendingPathComponent("widget/data")
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        if let data = try? JSONEncoder().encode(enabledWidgets) {
            try? data.write(to: folder.appendingPathComponent("display-settings.json"), options: .atomic)
        }
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

struct UsageBar: View {
    @Environment(\.colorScheme) var colorScheme
    let percent: Double
    var subtle = false
    var body: some View {
        ZStack(alignment: .leading) {
            Capsule().fill(Palette.track)
            Capsule().fill(Palette.warmAccent(colorScheme).opacity(subtle ? 0.5 : 0.85))
                .frame(width: max(0, min(100, percent)) * 3.04)
        }.frame(width: 304, height: subtle ? 3 : 5)
        .accessibilityLabel("\(String(format: "%.1f", percent))%")
    }
}

struct UsageView: View {
    @Environment(\.colorScheme) var colorScheme
    @ObservedObject var model: UsageModel
    var accent: Color { Palette.warmAccent(colorScheme) }
    var hide: () -> Void
    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                Image(systemName: "chart.bar.xaxis").foregroundStyle(accent)
                Text("用量小助手").font(.system(size: 15, weight: .semibold))
                Spacer()
                Button {
                    model.compact.toggle(); model.onResize?()
                } label: { Image(systemName: model.compact ? "arrow.up.left.and.arrow.down.right" : "arrow.down.right.and.arrow.up.left") }
                .help(model.compact ? "展开明细" : "紧凑速览")
                Button { model.pinned.toggle(); model.onPin?(model.pinned) } label: {
                    Image(systemName: model.pinned ? "pin.fill" : "pin")
                        .foregroundStyle(model.pinned ? accent : Color.secondary)
                }.help(model.pinned ? "恢复桌面显示" : "置顶显示")
                Button(action: hide) { Image(systemName: "xmark") }.help("隐藏到菜单栏")
            }.buttonStyle(.plain).padding(.bottom, 20)
            if let value = model.snapshot {
                if model.compact { compactBody(value) }
                else {
                    Picker("视图", selection: $model.tab) {
                        Text("项目用量").tag(0)
                        Text("本周超额").tag(1)
                    }.pickerStyle(.segmented).labelsHidden().padding(.bottom, 20)
                    ScrollView {
                        VStack(alignment: .leading, spacing: 16) {
                            if model.tab == 0 { projectBody(value) }
                            else { budgetBody(value) }
                        }.padding(.bottom, 4)
                    }.frame(maxHeight: .infinity)
                }
                footer(value)
            } else {
                VStack(alignment: .leading, spacing: 12) {
                    ProgressView().controlSize(.small)
                    Text("正在读取本机记录…").foregroundStyle(.secondary)
                    if let error = model.error { Text(error).font(.caption) }
                }.frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
            }
        }
        .padding(24)
        .frame(width: 368, height: model.compact ? 244 : 624)
        .background(Palette.surface(colorScheme).opacity(0.96), in: RoundedRectangle(cornerRadius: 16))
        .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.primary.opacity(0.08)))
        .tint(accent)
        .font(.system(size: 13))
        .onChange(of: model.compact) { _, _ in model.onResize?() }
    }

    func projectBody(_ snapshot: Snapshot) -> some View {
        VStack(alignment: .leading, spacing: 20) {
            VStack(alignment: .leading, spacing: 4) {
                Text("本周 · 本机记录").foregroundStyle(.secondary).font(.system(size: 12))
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(compactTokens(snapshot.total_tokens))
                        .font(.system(size: 34, weight: .medium, design: .rounded)).monospacedDigit()
                    Text("tokens").foregroundStyle(.secondary).font(.system(size: 12))
                }
                Text("\(snapshot.request_records.formatted()) 个响应 · 按文稿任务文件夹归集")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
            }
            VStack(alignment: .leading, spacing: 16) {
                if snapshot.tasks.isEmpty {
                    Text("窗口内暂无可识别用量").foregroundStyle(.secondary)
                }
                ForEach(snapshot.tasks) { row in
                    VStack(alignment: .leading, spacing: 8) {
                        HStack {
                            if row.is_other == true {
                                Button { model.showOther.toggle() } label: {
                                    HStack(spacing: 4) {
                                        Image(systemName: model.showOther ? "chevron.down" : "chevron.right").font(.system(size: 10))
                                        Text("其他 · \(row.members_count ?? 0) 项")
                                    }
                                }.buttonStyle(.plain)
                            } else { Text(row.name).lineLimit(1) }
                            Spacer(minLength: 8)
                            Text(String(format: "%.2f%%", row.percent)).monospacedDigit().fontWeight(.medium)
                        }
                        UsageBar(percent: row.percent, subtle: row.is_other == true)
                        if row.is_other == true && model.showOther {
                            ForEach(snapshot.other_tasks) { member in
                                HStack {
                                    Text(member.name).lineLimit(1)
                                    Spacer()
                                    Text(String(format: "%.2f%%", member.percent)).monospacedDigit()
                                }.foregroundStyle(.secondary).font(.system(size: 12))
                            }
                        }
                    }.help("\(row.total_tokens.formatted()) tokens · \(row.request_records) 个响应")
                }
            }
            Divider()
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Label("本周超额", systemImage: "chart.line.uptrend.xyaxis")
                    Spacer()
                    Text(budgetHeadline(snapshot.weekly_budget)).help("按已记录用量判断；记录不完整时，完整账户用量仍待核实。").foregroundStyle(accent)
                        .monospacedDigit().font(.system(size: 12, weight: .medium))
                }
                Text("低于 5% 合并到其他，可点击展开。")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
            }
            if snapshot.legacy_updates > 0 {
                Text("含 \(snapshot.legacy_updates) 条旧累计差分估算")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
            }
        }
    }

    func budgetHeadline(_ budget: WeeklyBudget?) -> String {
        guard let budget else { return "尚无记录" }
        if let total = budget.observed_total_percent, total <= 100 { return "未超额" }
        guard let excess = budget.excess_percent else { return "记录待核实" }
        if excess > 0 { return String(format: "超出约 %.1f%%", excess) }
        return "未超额"
    }

    func budgetBody(_ snapshot: Snapshot) -> some View {
        VStack(alignment: .leading, spacing: 20) {
            if let budget = snapshot.weekly_budget {
                VStack(alignment: .leading, spacing: 6) {
                    if let excess = budget.excess_percent, excess > 0 {
                        Text(budget.status == "incomplete" ? "至少超出约" : "已超出约")
                            .font(.system(size: 16, weight: .medium))
                        Text(String(format: "%.1f%%", excess))
                            .font(.system(size: 42, weight: .medium, design: .rounded))
                            .foregroundStyle(accent).monospacedDigit()
                    } else {
                        Text(budgetHeadline(budget))
                            .font(.system(size: 24, weight: .medium))
                        Text(budget.status == "incomplete" ? "按已记录用量统计 · 记录不完整" : "按已记录用量统计")
                            .font(.system(size: 12)).foregroundStyle(.secondary)
                    }
                }
                if let total = budget.observed_total_percent {
                    VStack(alignment: .leading, spacing: 10) {
                        HStack {
                            Text("累计使用")
                            Spacer()
                            Text(String(format: "约 %.1f%%", total)).fontWeight(.medium).monospacedDigit()
                        }
                        // Each complete rail represents one normal allowance.
                        // Keep the second rail visible after crossing 100%.
                        UsageBar(percent: total)
                        if total > 100 { UsageBar(percent: total - 100, subtle: true) }
                        Text("100% 以上为超额部分")
                            .font(.system(size: 11)).foregroundStyle(.secondary)
                    }
                }
                if let start = budget.period_start, let end = budget.period_end {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("统计时间").fontWeight(.medium)
                        Text("\(localDate(start)) — \(localDate(end - 1))")
                            .font(.system(size: 11)).foregroundStyle(.secondary)
                    }
                }
                Divider()
                if !budget.notes.isEmpty {
                    VStack(alignment: .leading, spacing: 8) {
                        Label("记录尚不完整", systemImage: "clock.badge.questionmark")
                            .fontWeight(.medium)
                        Text("累计只包含已记录的用量。")
                            .font(.system(size: 12)).foregroundStyle(.secondary)
                            .help(budget.notes.joined(separator: "\n"))
                    }
                }
                Text("每周一开始统计，周日结束。\n额度重置后继续累计；超额不代表额外收费。")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
            } else {
                Text("正在建立本周观测…").foregroundStyle(.secondary)
            }
        }
    }

    func compactBody(_ snapshot: Snapshot) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline) {
                Text(compactTokens(snapshot.total_tokens)).font(.system(size: 30, weight: .medium, design: .rounded))
                Text("tokens / 本周").font(.system(size: 12)).foregroundStyle(.secondary)
            }
            if let first = snapshot.tasks.first {
                HStack { Text(first.name); Spacer(); Text(String(format: "%.2f%%", first.percent)).monospacedDigit() }
                UsageBar(percent: first.percent)
            }
            Text("本周 · \(budgetHeadline(snapshot.weekly_budget))")
                .font(.system(size: 12)).foregroundStyle(.secondary)
        }
    }

    func footer(_ snapshot: Snapshot) -> some View {
        HStack {
            Circle().fill(model.error == nil ? Color.green.opacity(0.6) : Color.orange).frame(width: 5, height: 5)
            Text(model.error ?? "\(timestampLabel(snapshot.generated_at)) 更新 · 每分钟刷新")
                .lineLimit(1).font(.system(size: 11)).foregroundStyle(.secondary)
            Spacer()
            Button { model.refresh() } label: {
                Image(systemName: "arrow.clockwise").font(.system(size: 12))
            }.buttonStyle(.plain).disabled(model.refreshing).help("刷新本机记录与累计用量")
        }.padding(.top, 16)
    }
}

class FloatingPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    var panel: FloatingPanel!
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
        if let data = try? Data(contentsOf: root.appendingPathComponent("widget/data/positions.json")),
           let saved = try? JSONDecoder().decode([String: [String: Double]].self, from: data) { positions = saved }
        if let data = try? Data(contentsOf: root.appendingPathComponent("widget/data/display-settings.json")),
           let saved = try? JSONDecoder().decode(Set<WidgetKind>.self, from: data) {
            model.enabledWidgets = saved
        }
        panel = makePanel(width: 368, height: 624)
        panel.title = "用量小助手 · 桌面小框"
        panel.contentView = NSHostingView(rootView: UsageView(model: model, hide: { [weak self] in
            self?.model.toggleWidget(.box)
        }))
        if let frame = NSScreen.main?.visibleFrame {
            panel.setFrameOrigin(NSPoint(x: max(frame.minX, frame.maxX - 560), y: frame.maxY - 648))
        }
        restorePosition(panel, kind: .box)
        for kind in [WidgetKind.horizontal, .vertical] {
            let state = StripInteraction()
            let strip = makePanel(width: 108, height: 108)
            strip.title = "用量小助手 · \(kind.title)"
            // Match the approved pale-glass preview; the original box retains system appearance.
            strip.appearance = NSAppearance(named: .aqua)
            strip.hasShadow = false
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
        menu.addItem(withTitle: "项目与 Notion 设置…", action: #selector(openSettings), keyEquivalent: ",")
        menu.addItem(withTitle: "同步 Notion 摘要", action: #selector(syncNotion), keyEquivalent: "")
        menu.addItem(withTitle: "刷新", action: #selector(refresh), keyEquivalent: "")
        menu.addItem(.separator())
        menu.addItem(withTitle: "退出用量小助手", action: #selector(quit), keyEquivalent: "q")
        for item in menu.items { item.target = self }
        statusItem.menu = menu
        model.onResize = { [weak self] in self?.resize() }
        model.onPin = { [weak self] pinned in self?.setPinned(pinned) }
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
        let items = StripItem.items(model.snapshot, failed: model.error != nil, metric: state.quotaMetric)
        let vertical = kind == .vertical
        let natural = UsageStrip.canvasSize(itemCount: items.count, vertical: vertical)
        // Reserve a fixed overlay budget so hover never moves its anchor.
        let savedScreen = positions[kind.rawValue]?["screen"]
        let screen = initial ? (NSScreen.screens.first { Double(($0.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value ?? 0) == savedScreen } ?? NSScreen.main) : (window.screen ?? NSScreen.main)
        let bounds = screen?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let width = min(natural.width, bounds.width - 16)
        let height = min(natural.height, max(200, bounds.height - 64))
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
            let items = StripItem.items(model.snapshot, failed: model.error != nil, metric: state.quotaMetric)
            let x = mouse.x - window.frame.minX
            let y = window.frame.maxY - mouse.y
            let point = CGPoint(x: x, y: y)
            let canvas = window.frame.size
            let vertical = kind == .vertical
            let offset = headerOffsets[kind] ?? 0
            let header = UsageStrip.headerFrame(itemCount: items.count, vertical: vertical, canvas: canvas, headerOffset: offset).insetBy(dx: UsageStrip.inset, dy: UsageStrip.inset)
            var hovered: String?
            if pointInCapsule(point, rect: header) {
                if !vertical {
                    let index = Int((x - header.minX) / UsageStrip.moduleWidth)
                    if items.indices.contains(index) { hovered = items[index].id }
                } else { hovered = "__vertical_surface__" }
            }
            if hovered == nil, let selected = state.expanded, let index = items.firstIndex(where: { $0.id == selected }) {
                let card = UsageStrip.detailFrame(for: items[index], index: index, itemCount: items.count, vertical: vertical, canvas: canvas, headerOffset: offset)
                let bridge = UsageStrip.connectorFrame(index: index, itemCount: items.count, vertical: vertical, canvas: canvas, detail: card, headerOffset: offset)
                if pointInRoundedRectangle(point, rect: card, radius: UsageStrip.detailRadius) || bridge.contains(point) { hovered = selected }
            }
            window.ignoresMouseEvents = hovered == nil
            if pointerZones[kind] != hovered {
                if hovered == nil { state.leaveAllHoverZones() }
                else if let previous = pointerZones[kind] { state.hover(previous, inside: false, zone: "native-pointer") }
                if let hovered, hovered != "__vertical_surface__" { state.hover(hovered, inside: true, zone: "native-pointer") }
                pointerZones[kind] = hovered
            }
        }
    }

    func restorePosition(_ window: NSWindow, kind: WidgetKind) {
        guard let saved = positions[kind.rawValue] else { return }
        let screen = NSScreen.screens.first { Double(($0.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value ?? 0) == saved["screen"] } ?? NSScreen.main
        guard let bounds = screen?.visibleFrame else { return }
        let x = bounds.minX + min(1, max(0, saved["x"] ?? 0.5)) * max(0, bounds.width - window.frame.width)
        let top = bounds.maxY - min(1, max(0, saved["top"] ?? 0)) * max(0, bounds.height - window.frame.height)
        window.setFrameOrigin(NSPoint(x: x, y: top - window.frame.height))
    }

    @objc func windowMoved(_ notification: Notification) {
        guard !resizingStrips else { return }
        guard let window = notification.object as? NSWindow, let screen = window.screen else { return }
        let kind: WidgetKind?
        if window === panel { kind = .box }
        else { kind = strips.first(where: { $0.value === window })?.key }
        guard let kind else { return }
        let bounds = screen.visibleFrame
        var saved = ["screen": Double((screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value ?? 0),
            "x": (window.frame.minX - bounds.minX) / max(1, bounds.width - window.frame.width),
            "top": (bounds.maxY - window.frame.maxY) / max(1, bounds.height - window.frame.height)]
        if kind != .box {
            let items = StripItem.items(model.snapshot, failed: model.error != nil, metric: interactions[kind]?.quotaMetric ?? .weeklyUsed)
            let anchorTop = window.frame.maxY - (headerOffsets[kind] ?? 0)
            saved["anchor_top"] = (bounds.maxY - anchorTop) / max(1, bounds.height - UsageStrip.naturalSize(itemCount: items.count, vertical: kind == .vertical).height)
        }
        positions[kind.rawValue] = saved
        if let data = try? JSONEncoder().encode(positions) {
            try? data.write(to: root.appendingPathComponent("widget/data/positions.json"), options: .atomic)
        }
    }

    func updateVisibility() {
        for kind in WidgetKind.allCases {
            guard let window = kind == .box ? panel : strips[kind] else { continue }
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
        guard model != nil, panel != nil else { return }
        var windows: [[String: Any]] = []
        for kind in WidgetKind.allCases {
            guard let window = kind == .box ? panel : strips[kind] else { continue }
            windows.append(["kind": kind.rawValue, "level": window.level.rawValue,
                "is_visible": window.isVisible, "occlusion_visible": window.occlusionState.contains(.visible),
                "stationary": window.collectionBehavior.contains(.stationary),
                "x": window.frame.minX, "y": window.frame.minY, "width": window.frame.width, "height": window.frame.height,
                "expanded": interactions[kind]?.expanded ?? "", "locked": interactions[kind]?.locked ?? false])
        }
        let value: [String: Any] = ["recorded_at": ISO8601DateFormatter().string(from: Date()), "widget_style": model.widgetStyle.rawValue,
            "desktop_level": desktopLevel().rawValue, "pinned": model.pinned,
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

    func setPinned(_ pinned: Bool) {
        panel.level = pinned ? .floating : desktopLevel()
        panel.collectionBehavior = pinned ? [.canJoinAllSpaces, .fullScreenAuxiliary] :
                                            [.canJoinAllSpaces, .stationary, .ignoresCycle]
        if panel.isVisible { panel.orderFrontRegardless() }
        recordWindowState()
    }

    func resize() {
        let top = panel.frame.maxY
        let height: CGFloat = model.compact ? 244 : 624
        panel.setFrame(NSRect(x: panel.frame.minX, y: top - height, width: 368, height: height), display: true, animate: true)
    }

    func updateStatus() {
        for parent in statusItem.menu?.items ?? [] {
            for item in parent.submenu?.items ?? [] {
                if let raw = item.representedObject as? String, raw.hasPrefix("theme:") {
                    item.state = raw == "theme:" + model.widgetStyle.rawValue ? .on : .off
                }
            }
        }
        if let syncItem = statusItem.menu?.items.first(where: { $0.action == #selector(syncNotion) }) {
            syncItem.isEnabled = model.notionEnabled && !model.notionSyncing
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
    @objc func syncNotion() { model.syncNotion(force: true) }
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
