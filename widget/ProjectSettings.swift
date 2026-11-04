import AppKit
import SwiftUI
import UniformTypeIdentifiers

enum WidgetPreferences {
    static func read(root: URL) throws -> [String: Any] {
        let file = root.appendingPathComponent("widget/data/app-settings.json")
        guard FileManager.default.fileExists(atPath: file.path) else { return [:] }
        guard let settings = try JSONSerialization.jsonObject(with: Data(contentsOf: file)) as? [String: Any] else {
            throw NSError(domain: "UsageWidget", code: 2)
        }
        return settings
    }

    static func managementEnabled(in settings: [String: Any]) -> Bool {
        guard let raw = settings["management_enabled"] else { return true }
        guard let value = raw as? NSNumber, CFGetTypeID(value) == CFBooleanGetTypeID() else { return false }
        return value.boolValue
    }

    static func defaultVault(root: URL) -> URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Documents/项目管理obsidian", isDirectory: true)
    }

    static func update(root: URL, values: [String: Any]) throws {
        var settings = try read(root: root)
        settings.merge(values) { _, new in new }
        let folder = root.appendingPathComponent("widget/data")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let file = folder.appendingPathComponent("app-settings.json")
        try JSONSerialization.data(withJSONObject: settings, options: [.prettyPrinted]).write(to: file, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
    }
}

enum BillingAnchorPrecision: String, CaseIterable, Identifiable {
    case date, datetime
    var id: String { rawValue }
    var title: String { self == .date ? "只知道日期" : "知道具体时间" }
}

enum BillingAnchorSettings {
    static let timezone = TimeZone(identifier: "Asia/Shanghai")!

    static func decode(_ value: String?) -> Date? {
        guard let value else { return nil }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = formatter.date(from: value) { return date }
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.date(from: value)
    }

    static func values(anchor: Date?, precision: BillingAnchorPrecision,
                       now: Date = Date()) throws -> [String: Any] {
        guard let anchor else {
            return ["billing_anchor": NSNull(), "billing_anchor_precision": NSNull(),
                    "billing_timezone": "Asia/Shanghai"]
        }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timezone
        let normalized = precision == .date ? calendar.startOfDay(for: anchor) : anchor
        guard normalized <= now else {
            throw NSError(domain: "UsageWidget", code: 3,
                userInfo: [NSLocalizedDescriptionKey: "请填写最近一次已经发生的续费时间。"])
        }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return ["billing_anchor": formatter.string(from: normalized),
                "billing_anchor_precision": precision.rawValue,
                "billing_timezone": "Asia/Shanghai"]
    }
}

@MainActor
enum WidgetApplicationMenu {
    static func make(settingsTarget: NSObject? = nil) -> NSMenu {
        let menu = NSMenu()
        let application = NSMenuItem()
        application.submenu = NSMenu(title: "用量小助手")
        let settings = application.submenu!.addItem(withTitle: "项目与 Markdown 设置…",
            action: #selector(AppDelegate.openSettings), keyEquivalent: ",")
        settings.target = settingsTarget
        let accounting = application.submenu!.addItem(withTitle: "周期核算…",
            action: #selector(AppDelegate.openSettings), keyEquivalent: "")
        accounting.target = settingsTarget
        application.submenu!.addItem(.separator())
        application.submenu!.addItem(withTitle: "退出用量小助手",
            action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        menu.addItem(application)

        let edit = NSMenuItem(title: "编辑", action: nil, keyEquivalent: "")
        edit.submenu = NSMenu(title: "编辑")
        for (title, action, key) in [("剪切", #selector(NSText.cut(_:)), "x"),
                                    ("复制", #selector(NSText.copy(_:)), "c"),
                                    ("粘贴", #selector(NSText.paste(_:)), "v"),
                                    ("全选", #selector(NSText.selectAll(_:)), "a")] {
            // A nil target routes the command to the active field editor.
            let item = edit.submenu!.addItem(withTitle: title, action: action, keyEquivalent: key)
            item.keyEquivalentModifierMask = [.command]
        }
        menu.addItem(edit)
        return menu
    }
}

func runWidgetProcess(python: URL, root: URL, arguments: [String], input: Data? = nil,
                      timeout: TimeInterval = 90) throws -> Data {
    let process = Process()
    process.executableURL = python
    process.arguments = arguments
    process.currentDirectoryURL = root
    let output = Pipe()
    process.standardOutput = output
    process.standardError = FileHandle.nullDevice
    let incoming = Pipe()
    process.standardInput = input == nil ? FileHandle.nullDevice : incoming
    try process.run()
    let watchdog = DispatchSource.makeTimerSource(queue: .global(qos: .utility))
    watchdog.schedule(deadline: .now() + timeout)
    watchdog.setEventHandler { if process.isRunning { process.terminate() } }
    watchdog.resume()
    defer { watchdog.cancel() }
    if let input {
        try incoming.fileHandleForWriting.write(contentsOf: input)
        try incoming.fileHandleForWriting.close()
    }
    let data = output.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
    guard !data.isEmpty else {
        throw NSError(domain: "UsageWidget", code: 1,
            userInfo: [NSLocalizedDescriptionKey: "操作未完成，保留本地记录；可以稍后重试。"])
    }
    return data
}

struct ProjectChoice: Identifiable {
    let id: String
    let name: String
    let iconMissing: Bool
}

enum ProjectImportSelection {
    static func command(manifestURL: URL, folder: URL, taskRoot: URL) throws -> [String: Any] {
        let root = taskRoot.resolvingSymlinksInPath().standardizedFileURL
        let selected = folder.resolvingSymlinksInPath().standardizedFileURL
        var directory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: selected.path, isDirectory: &directory), directory.boolValue,
              selected.deletingLastPathComponent() == root else {
            throw NSError(domain: "UsageWidget", code: 4,
                userInfo: [NSLocalizedDescriptionKey: "请选择项目根目录下的直接子文件夹。"])
        }
        let data = try Data(contentsOf: manifestURL)
        guard data.count <= 256 * 1024,
              let manifest = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              manifest["version"] as? Int == 1, manifest["project"] is [String: Any] else {
            throw NSError(domain: "UsageWidget", code: 5,
                userInfo: [NSLocalizedDescriptionKey: "请选择 Skill 生成的项目清单 JSON，文件不能超过 256 KB。"])
        }
        var command: [String: Any] = ["manifest": manifest, "folder": selected.path, "task_root": root.path]
        command["codex_home"] = ProcessInfo.processInfo.environment["CODEX_HOME"]
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".codex").path
        return command
    }
}

@MainActor
final class ProjectSettingsModel: ObservableObject {
    @Published var taskRoot: String
    @Published var vaultPath: String
    @Published var createGoal = ""
    @Published var busy = false
    @Published var message = ""
    @Published var choices: [ProjectChoice] = []
    @Published var selectedTask = "" {
        didSet {
            if oldValue != selectedTask {
                projectSelectionEdited = false
                followSelectedBinding()
            }
        }
    }
    @Published var selectedProject = "" {
        didSet { if !updatingProjectSelection && oldValue != selectedProject { projectSelectionEdited = true } }
    }
    private var bindings: [String: String]?
    private var projectSelectionEdited = false
    private var updatingProjectSelection = false
    @Published var statusText = "正在读取 Markdown 管理库"
    @Published var projectsURL: String?
    @Published var usageURL: String?
    @Published var tasksURL: String?
    @Published var accountingURL: String?
    @Published var hasBillingAnchor = false
    @Published var billingAnchorDate = Date(timeIntervalSince1970: floor(Date().timeIntervalSince1970 / 60) * 60)
    @Published var billingAnchorPrecision: BillingAnchorPrecision = .date
    @Published var accountingBusy = false
    @Published var accountingMessage = ""
    @Published var accountingReport: [String: Any]?
    @Published var createIconMode = "emoji"
    @Published var createEmoji = ""
    @Published var createLogoURL = ""
    @Published var importManifestPath = ""
    @Published var importFolderPath = ""
    weak var usage: UsageModel?
    let root: URL
    let python: URL

    init(usage: UsageModel) {
        self.usage = usage; root = usage.root; python = usage.python
        let settings = try? WidgetPreferences.read(root: root)
        taskRoot = settings?["task_root"] as? String ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Documents").path
        vaultPath = settings?["vault_path"] as? String ?? WidgetPreferences.defaultVault(root: root).path
        if let anchor = BillingAnchorSettings.decode(settings?["billing_anchor"] as? String) {
            hasBillingAnchor = true
            billingAnchorDate = anchor
            billingAnchorPrecision = BillingAnchorPrecision(rawValue: settings?["billing_anchor_precision"] as? String ?? "") ?? .datetime
        }

    }
    var tasks: [LocalProject] { usage?.snapshot?.available_projects ?? [] }

    func boundProject(for taskID: String) -> String? {
        // A returned bindings map is authoritative, including explicit unbinding.
        if let bindings { return bindings[taskID] }
        return tasks.first(where: { $0.task_id == taskID })?.project_id
    }

    private func followSelectedBinding() {
        let actual = boundProject(for: selectedTask) ?? ""
        let validSelection = selectedProject.isEmpty || choices.contains { $0.id == selectedProject }
        if projectSelectionEdited && validSelection && selectedProject != actual { return }
        updatingProjectSelection = true
        selectedProject = choices.contains { $0.id == actual } ? actual : ""
        updatingProjectSelection = false
        projectSelectionEdited = false
    }

    func refreshTaskSelection() {
        if selectedTask.isEmpty || !tasks.contains(where: { $0.task_id == selectedTask }) {
            selectedTask = tasks.first?.task_id ?? ""
        }
        followSelectedBinding()
    }

    var newProjectIcon: [String: Any]? {
        if createIconMode == "emoji" {
            let value = createEmoji.trimmingCharacters(in: .whitespacesAndNewlines)
            guard value.count == 1, value.utf16.count <= 32,
                  value.unicodeScalars.contains(where: {
                    $0.properties.isEmojiPresentation || ($0.properties.isEmoji && $0.value > 0x7F) || $0.value == 0x20E3
                  }) else { return nil }
            return ["type": "emoji", "emoji": value]
        }
        let value = createLogoURL.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty, value.count <= 4096,
              value.rangeOfCharacter(from: .whitespacesAndNewlines.union(.controlCharacters)) == nil,
              let components = URLComponents(string: value), components.scheme == "https",
              components.host?.isEmpty == false, components.user == nil, components.password == nil,
              components.port.map({ 1...65535 ~= $0 }) ?? true else { return nil }
        return ["type": "external", "external": ["url": value]]
    }
    var selectedProjectNeedsIcon: Bool { choices.first(where: { $0.id == selectedProject })?.iconMissing == true }
    var accountingWeek: [String: Any]? { (accountingReport?["weekly"] as? [String: Any])?["current"] as? [String: Any] }
    var accountingMonth: [String: Any]? { (accountingReport?["monthly"] as? [String: Any])?["current"] as? [String: Any] }
    var monthlyNeedsAnchor: Bool { (accountingReport?["monthly"] as? [String: Any])?["status"] as? String == "needs_anchor" }

    func periodTokens(_ period: [String: Any]?) -> String {
        guard let totals = period?["totals"] as? [String: Any],
              let total = totals["total_tokens"] as? NSNumber,
              CFGetTypeID(total) != CFBooleanGetTypeID() else { return "尚未核算" }
        return compactTokens(total.intValue) + " Token"
    }

    func periodDates(_ period: [String: Any]?) -> String? {
        guard let start = BillingAnchorSettings.decode(period?["start"] as? String),
              let end = BillingAnchorSettings.decode(period?["end"] as? String) else { return nil }
        let formatter = DateFormatter()
        formatter.timeZone = BillingAnchorSettings.timezone
        formatter.dateFormat = "M月d日 HH:mm"
        return formatter.string(from: start) + " — " + formatter.string(from: end) + "（不含结束时刻）"
    }

    func periodCoverage(_ period: [String: Any]?) -> String? {
        guard let coverage = period?["coverage"] as? [String: Any],
              let quality = coverage["quality"] as? [String] else { return nil }
        var labels: [String] = []
        if quality.contains("partial_unreadable") { labels.append("有记录未能识别或读取") }
        if quality.contains("legacy_estimated") { labels.append("含旧格式估算") }
        if quality.contains("no_observed_usage") { labels.append("窗口内未记录可用用量") }
        if labels.isEmpty && quality.contains("response_records_verified") { labels.append("按响应记录统计") }
        return labels.isEmpty ? nil : labels.joined(separator: " · ")
    }

    func saveBillingAnchor() {
        guard !accountingBusy else { return }
        do {
            let values = try BillingAnchorSettings.values(anchor: hasBillingAnchor ? billingAnchorDate : nil,
                precision: billingAnchorPrecision)
            try WidgetPreferences.update(root: root, values: values)
            if let value = values["billing_anchor"] as? String,
               let saved = BillingAnchorSettings.decode(value) { billingAnchorDate = saved }
            accountingMessage = hasBillingAnchor
                ? (billingAnchorPrecision == .date ? "续费日期已保存；按上海时间当天零点划分，账期边界为日粒度估算。" : "续费时间已保存；按会员账期核算。")
                : "续费时间未提供；月账期保持待配置，不会自动猜测。"
            refreshAccounting()
        } catch { accountingMessage = error.localizedDescription }
    }

    func refreshAccounting() {
        guard !accountingBusy else { return }
        accountingBusy = true
        accountingMessage = ""
        let executable = python, folder = root
        Task {
            let result: Result<Data, Error> = await Task.detached(priority: .utility) {
                do {
                    return .success(try runWidgetProcess(python: executable, root: folder,
                        arguments: [folder.appendingPathComponent("widget/widget_backend.py").path,
                                    "--accounting-summary"], timeout: 180))
                } catch { return .failure(error) }
            }.value
            switch result {
            case .success(let data):
                if let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                   json["ok"] as? Bool == true,
                   let report = json["accounting"] as? [String: Any] {
                    accountingReport = report
                    accountingMessage = monthlyNeedsAnchor ? "核算已刷新；会员月账期待配置续费时间。" : "核算已刷新；统计截至本次扫描。"
                } else {
                    accountingMessage = "周期核算未完成，保留已有记录。"
                }
            case .failure(let error): accountingMessage = error.localizedDescription
            }
            accountingBusy = false
            usage?.refresh()
        }
    }

    func saveRoot() {
        let path = (taskRoot as NSString).expandingTildeInPath
        var directory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: path, isDirectory: &directory), directory.boolValue else {
            message = "请选择已存在的项目根目录。"; return
        }
        let configFile = root.appendingPathComponent("widget/data/markdown-config.json")
        if let data = try? Data(contentsOf: configFile),
           let config = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let bindings = config["bindings"] as? [String: String], !bindings.isEmpty {
            let settingsFile = root.appendingPathComponent("widget/data/app-settings.json")
            let settings = (try? Data(contentsOf: settingsFile)).flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            let prior = settings?["task_root"] as? String ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Documents").path
            if URL(fileURLWithPath: path).standardizedFileURL != URL(fileURLWithPath: prior).standardizedFileURL {
                message = "已绑定项目后更改工程根目录会改变历史归集。请先解除绑定，再选择新根目录；历史用量保留。"
                return
            }
        }
        do {
            let folder = root.appendingPathComponent("widget/data")
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            try WidgetPreferences.update(root: root, values: ["task_root": path])
            taskRoot = path; message = "已保存；每个直接子文件夹作为一个任务。"
            usage?.refresh()
        } catch { message = "设置未保存，请检查目录权限。" }
    }
    func chooseRoot() {
        let picker = NSOpenPanel()
        picker.canChooseDirectories = true; picker.canChooseFiles = false
        picker.allowsMultipleSelection = false
        if picker.runModal() == .OK, let url = picker.url { taskRoot = url.path; saveRoot() }
    }
    func apply(_ status: [String: Any], resetProjectSelection: Bool = false) {
        if resetProjectSelection { projectSelectionEdited = false }
        if let currentBindings = status["bindings"] as? [String: String] { bindings = currentBindings }
        statusText = status["message"] as? String ?? statusText
        projectsURL = status["projects_url"] as? String ?? projectsURL
        usageURL = status["usage_url"] as? String ?? usageURL
        tasksURL = status["tasks_url"] as? String ?? tasksURL
        accountingURL = status["accounting_url"] as? String ?? accountingURL
        if let path = status["vault_path"] as? String { vaultPath = path }
        if let rows = status["projects"] as? [[String: Any]] {
            choices = rows.filter { $0["available"] as? Bool != false }.compactMap { item in
                guard let id = (item["project_id"] ?? item["id"]) as? String,
                      let name = (item["display_name"] ?? item["name"]) as? String else { return nil }
                let missing = item["icon_missing"] as? Bool ?? ((item["icon_value"] as? String)?.isEmpty != false)
                return ProjectChoice(id: id, name: name, iconMissing: missing)
            }.sorted { $0.name < $1.name }
        }
        refreshTaskSelection()
    }

    func perform(_ command: [String: Any]) {
        guard !busy else { return }
        guard command["command"] as? String == "status" || usage?.managementEnabled == true else {
            message = "仅计量模式；请先开启 Markdown 项目管理。"; return
        }
        busy = true; message = ""
        let executable = python, folder = root
        Task {
            let result: Result<Data, Error> = await Task.detached(priority: .utility) {
                do {
                    func call(_ payload: [String: Any]) throws -> [String: Any] {
                        let input = try JSONSerialization.data(withJSONObject: payload)
                        let data = try runWidgetProcess(python: executable, root: folder,
                            arguments: [folder.appendingPathComponent("widget/markdown_store.py").path,
                                        "--data-dir", folder.appendingPathComponent("widget/data").path], input: input, timeout: 120)
                        guard let result = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                            throw NSError(domain: "UsageWidget", code: 6,
                                userInfo: [NSLocalizedDescriptionKey: "管理数据无法解析，保留原文件。"])
                        }
                        return result
                    }
                    var response = try call(command)
                    if response["ok"] as? Bool == true {
                        let state = try call(["command": "status"])
                        let projects = try call(["command": "projects"])
                        var status = state["status"] as? [String: Any] ?? [:]
                        if projects["ok"] as? Bool == true { status["projects"] = projects["projects"] }
                        response["status"] = status
                    }
                    return .success(try JSONSerialization.data(withJSONObject: response))
                } catch { return .failure(error) }
            }.value
            switch result {
            case .success(let data):
                if let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    if let status = json["status"] as? [String: Any] {
                        let mutation = ["bind", "unbind", "create", "import"].contains(command["command"] as? String ?? "")
                        apply(status, resetProjectSelection: json["ok"] as? Bool == true && mutation)
                    }
                    if json["ok"] as? Bool == true {
                        message = command["command"] as? String == "status" ? "" : "Markdown 项目资料已更新。"
                    } else {
                        message = (json["error"] as? [String: Any])?["message"] as? String ?? "操作未完成，请检查管理库路径和笔记格式。"
                    }
                } else { message = "操作结果无法解析，保留原设置。" }
            case .failure(let error): message = error.localizedDescription
            }
            busy = false
            usage?.refresh()
        }
    }

    func connect() {
        guard !busy, usage?.managementEnabled == true else { return }
        perform(["command": "setup", "vault_path": (vaultPath as NSString).expandingTildeInPath,
                 "task_root": (taskRoot as NSString).expandingTildeInPath])
    }

    func chooseVault() {
        let picker = NSOpenPanel()
        picker.canChooseDirectories = true; picker.canChooseFiles = false
        picker.allowsMultipleSelection = false
        picker.directoryURL = URL(fileURLWithPath: (vaultPath as NSString).expandingTildeInPath)
        if picker.runModal() == .OK, let url = picker.url { vaultPath = url.path; connect() }
    }

    func create() {
        guard let task = tasks.first(where: { $0.task_id == selectedTask }) else { return }
        guard let icon = newProjectIcon else { message = "请先选择一个 emoji 头像或填写有效 HTTPS Logo 地址。"; return }
        perform(["command": "create", "task_id": task.task_id, "name": task.name, "icon": icon, "goal": createGoal])
    }
    func chooseImportManifest() {
        let picker = NSOpenPanel()
        picker.canChooseDirectories = false; picker.canChooseFiles = true
        picker.allowsMultipleSelection = false; picker.allowedContentTypes = [.json]
        if picker.runModal() == .OK, let url = picker.url { importManifestPath = url.path }
    }
    func chooseImportFolder() {
        let picker = NSOpenPanel()
        picker.canChooseDirectories = true; picker.canChooseFiles = false
        picker.allowsMultipleSelection = false
        picker.directoryURL = URL(fileURLWithPath: (taskRoot as NSString).expandingTildeInPath)
        if picker.runModal() == .OK, let url = picker.url { importFolderPath = url.path }
    }
    func importProject() {
        guard !busy, usage?.managementEnabled == true, usage?.managementSyncing != true else { return }
        do {
            let saved = try WidgetPreferences.read(root: root)
            let savedRoot = saved["task_root"] as? String
                ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Documents").path
            guard URL(fileURLWithPath: (taskRoot as NSString).expandingTildeInPath).resolvingSymlinksInPath().standardizedFileURL
                    == URL(fileURLWithPath: savedRoot).resolvingSymlinksInPath().standardizedFileURL else {
                message = "请先保存工程根目录，再导入项目。"; return
            }
            var command = try ProjectImportSelection.command(manifestURL: URL(fileURLWithPath: importManifestPath),
                folder: URL(fileURLWithPath: importFolderPath),
                taskRoot: URL(fileURLWithPath: (taskRoot as NSString).expandingTildeInPath))
            command["command"] = "import"
            perform(command)
        } catch { message = error.localizedDescription }
    }

    func bind() {
        guard !selectedTask.isEmpty, !selectedProject.isEmpty else { return }
        guard !selectedProjectNeedsIcon else {
            message = "这个项目还没有头像。请先在项目笔记的 icon 属性设置 emoji 或 HTTPS 图片，再刷新绑定。"
            return
        }
        perform(["command": "bind", "task_id": selectedTask, "project_id": selectedProject])
    }
}

struct ProjectSettingsView: View {
    @ObservedObject var model: ProjectSettingsModel
    @ObservedObject var usage: UsageModel
    var body: some View {
        Form {
            Section("项目管理") {
                Toggle("启用 Markdown 项目管理", isOn: Binding(get: { usage.managementEnabled },
                    set: { usage.setManagementEnabled($0) }))
                    .disabled(model.busy || usage.managementSyncing)
                Text(usage.managementEnabled ? "项目资料保存在 Obsidian 管理库；工程和代码仍放在文稿/项目名。" : "仅计量模式：本机 Token 和额度继续运行。")
                    .font(.callout).foregroundStyle(.secondary)
                if let message = usage.managementSyncMessage { Text(message).font(.callout).foregroundStyle(.secondary) }
            }
            Section("周期核算") {
                LabeledContent("项目周报", value: "自然周 · 周一至周日")
                LabeledContent("额度窗口", value: "按 Codex 实际重置时间")
                LabeledContent("会员月核算", value: "按会员续费账期")
                if let report = model.accountingReport {
                    LabeledContent("本周记录", value: model.periodTokens(model.accountingWeek))
                    if let dates = model.periodDates(model.accountingWeek) {
                        Text(dates).font(.caption).foregroundStyle(.secondary)
                    }
                    if let coverage = model.periodCoverage(model.accountingWeek) {
                        Text(coverage).font(.caption).foregroundStyle(.secondary)
                    }
                    LabeledContent("本期会员月", value: model.monthlyNeedsAnchor ? "待配置续费时间" : model.periodTokens(model.accountingMonth))
                    if let dates = model.periodDates(model.accountingMonth) {
                        Text(dates).font(.caption).foregroundStyle(.secondary)
                    }
                    if let coverage = model.periodCoverage(model.accountingMonth) {
                        Text(coverage).font(.caption).foregroundStyle(.secondary)
                    }
                    if let generated = report["generated_at"] as? String {
                        Text("更新于 " + timestampLabel(generated) + " · 上海时间").font(.caption).foregroundStyle(.secondary)
                    }
                }
                Toggle("已确认最近一次会员续费时间", isOn: $model.hasBillingAnchor)
                    .disabled(model.accountingBusy)
                if model.hasBillingAnchor {
                    Picker("已知精度", selection: $model.billingAnchorPrecision) {
                        ForEach(BillingAnchorPrecision.allCases) { precision in Text(precision.title).tag(precision) }
                    }.pickerStyle(.segmented).disabled(model.accountingBusy)
                    DatePicker("确认的续费锚点", selection: $model.billingAnchorDate, in: ...Date(),
                        displayedComponents: model.billingAnchorPrecision == .date ? [.date] : [.date, .hourAndMinute])
                        .environment(\.timeZone, BillingAnchorSettings.timezone)
                        .environment(\.locale, Locale(identifier: "zh_CN"))
                        .disabled(model.accountingBusy)
                    Text(model.billingAnchorPrecision == .date ? "只知道日期时，以上海时间当天零点为边界，月账期标记为日粒度估算。" : "按这次续费的日期和时间划分会员月账期。")
                        .font(.callout).foregroundStyle(.secondary)
                    Text("月底续费请使用原续费日的锚点，例如31日。短月份的一次实际扣费日期不能确定原来是30日还是31日。")
                        .font(.callout).foregroundStyle(.secondary)
                } else {
                    Text("续费时间未知时，月账期保持待配置。请从会员付款或续费记录确认；这里不会自动填入今天。")
                        .font(.callout).foregroundStyle(.secondary)
                }
                HStack {
                    Button("保存账期") { model.saveBillingAnchor() }.disabled(model.accountingBusy)
                    Button(model.accountingBusy ? "核算中…" : "刷新核算") { model.refreshAccounting() }.disabled(model.accountingBusy)
                    if let url = model.accountingURL { Button("打开周期与额度") { usage.openProject(url) } }
                }
                Text("周期账本独立于项目管理。Token 来自本机保留日志；额度按实测窗口记录，不把一个月换算成四周。")
                    .font(.callout).foregroundStyle(.secondary)
                if !model.accountingMessage.isEmpty { Text(model.accountingMessage).font(.callout).textSelection(.enabled) }
            }
            Section("本地项目") {
                Text("按工程文件夹归集本机投入，项目资料通过下方绑定关联。")
                    .font(.callout).foregroundStyle(.secondary)
                HStack {
                    TextField("项目根目录", text: $model.taskRoot)
                    Button("选择…") { model.chooseRoot() }.disabled(model.busy)
                    Button("保存") { model.saveRoot() }.disabled(model.busy)
                }
            }
            if usage.managementEnabled {
                Section("Obsidian 管理库") {
                    Text(model.statusText).font(.callout).textSelection(.enabled)
                    HStack {
                        TextField("管理库文件夹", text: $model.vaultPath)
                        Button("选择…") { model.chooseVault() }
                    }
                    HStack {
                        Button("保存并连接") { model.connect() }
                        Button("刷新项目") { model.perform(["command": "projects"]) }
                        if let url = model.projectsURL { Button("打开项目总览") { usage.openProject(url) } }
                        if let url = model.tasksURL { Button("打开人工事项") { usage.openProject(url) } }
                        if let url = model.usageURL { Button("打开用量账本") { usage.openProject(url) } }
                    }.disabled(model.busy)
                    Text("直接读取本地 Markdown。移动端连接尚未配置。")
                        .font(.callout).foregroundStyle(.secondary)
                }
                Section("绑定工程与项目资料") {
                    Picker("本地工程", selection: $model.selectedTask) {
                        Text("请选择").tag("")
                        ForEach(model.tasks) { task in Text(task.name + (model.boundProject(for: task.task_id) == nil ? " · 未绑定" : " · 已绑定")).tag(task.task_id) }
                    }.disabled(model.busy)
                    Picker("Markdown 项目", selection: $model.selectedProject) {
                        Text("请选择").tag("")
                        ForEach(model.choices) { page in Text(page.name + (page.iconMissing ? " · 需设置头像" : "")).tag(page.id) }
                    }.disabled(model.busy)
                    Picker("新项目头像", selection: $model.createIconMode) {
                        Text("emoji").tag("emoji")
                        Text("HTTPS Logo").tag("external")
                    }.pickerStyle(.segmented).disabled(model.busy)
                    if model.createIconMode == "emoji" {
                        HStack {
                            TextField("输入一个项目 emoji", text: $model.createEmoji)
                            Menu("选择头像") {
                                ForEach(["⏱️", "📦", "🎙️", "🛠️", "📝", "🌱", "🚀"], id: \.self) { value in
                                    Button(value) { model.createEmoji = value }
                                }
                            }
                        }.disabled(model.busy)
                    } else {
                        TextField("项目 Logo 的 HTTPS 地址", text: $model.createLogoURL).disabled(model.busy)
                    }
                    TextField("新项目总目标（可在项目笔记中继续完善）", text: $model.createGoal)
                    HStack {
                        Button("绑定所选项目") { model.bind() }.disabled(model.selectedTask.isEmpty || model.selectedProject.isEmpty || model.selectedProjectNeedsIcon)
                        Button("按工程名新建并绑定") { model.create() }.disabled(model.selectedTask.isEmpty || model.newProjectIcon == nil)
                        Button("解除绑定") { model.perform(["command": "unbind", "task_id": model.selectedTask]) }.disabled(model.selectedTask.isEmpty)
                    }.disabled(model.busy)
                    Text("工程在文稿目录的独立文件夹中，管理资料统一保存在此 Obsidian 库。改名保留稳定身份；不会自动导入所有文件夹。")
                        .font(.callout).foregroundStyle(.secondary)
                }
                Section("导入已有项目清单") {
                    Text("让 AI 按管理规则核对真实材料并生成项目清单，再选择清单和对应工程文件夹。")
                        .font(.callout).foregroundStyle(.secondary)
                    HStack {
                        Text(model.importManifestPath.isEmpty ? "尚未选择项目清单" : URL(fileURLWithPath: model.importManifestPath).lastPathComponent)
                            .font(.callout).lineLimit(1)
                        Spacer()
                        Button("选择项目清单…") { model.chooseImportManifest() }
                    }
                    HStack {
                        Text(model.importFolderPath.isEmpty ? "尚未选择工程文件夹" : URL(fileURLWithPath: model.importFolderPath).lastPathComponent)
                            .font(.callout).lineLimit(1)
                        Spacer()
                        Button("选择工程文件夹…") { model.chooseImportFolder() }
                    }
                    Button("导入并绑定") { model.importProject() }
                        .disabled(model.importManifestPath.isEmpty || model.importFolderPath.isEmpty)
                }.disabled(model.busy || usage.managementSyncing)
                Section("本地摘要发布") {
                    HStack {
                        Button(usage.managementSyncing ? "更新中…" : "立即更新 Markdown 摘要") { usage.syncManagement(force: true) }.disabled(usage.managementSyncing)
                        Text(usage.managementSyncMessage ?? "管理状态每分钟读取，账本摘要每 5 分钟检查。")
                            .font(.callout).foregroundStyle(.secondary)
                    }
                    Text("只发布核算摘要，不把每次刷新写成开发历史。日账本和额度观测继续保存在本机。")
                        .font(.callout).foregroundStyle(.secondary)
                }
            }
            Section("桌面显示") {
                Picker("外观", selection: Binding(get: { usage.widgetStyle }, set: { usage.setWidgetStyle($0) })) {
                    ForEach(WidgetStyle.allCases) { style in Text(style.title).tag(style) }
                }.pickerStyle(.segmented)
                HStack {
                    ForEach(WidgetKind.allCases, id: \.self) { kind in
                        Toggle(kind.title, isOn: Binding(get: { usage.enabledWidgets.contains(kind) }, set: { _ in usage.toggleWidget(kind) }))
                    }
                }
                Text("前两个入口为 Codex 和本期 Token，后面显示本周用量最高的四个管理项目；其余任务放入其他。")
                    .font(.callout).foregroundStyle(.secondary)
                Text("横条和竖条保持桌面层；普通窗口可以盖住它们。显示选择和位置会自动保存。")
                    .font(.callout).foregroundStyle(.secondary)
                Text("液态玻璃使用系统材质；macOS 26 启用系统玻璃效果，较早系统使用原生透明材质。悬停展开项目看板。")
                    .font(.callout).foregroundStyle(.secondary)
            }
            if model.busy { ProgressView().controlSize(.small) }
            if !model.message.isEmpty { Text(model.message).font(.callout).textSelection(.enabled) }
        }.formStyle(.grouped).padding(12).frame(width: 640, height: 620)
            .onAppear { model.perform(["command": "status"]); model.refreshAccounting() }
            .onChange(of: usage.snapshot?.generated_at) { _, _ in model.refreshTaskSelection() }
    }
}

@MainActor
final class ProjectSettingsController: NSWindowController, NSWindowDelegate {
    init(usage: UsageModel) {
        let model = ProjectSettingsModel(usage: usage)
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 640, height: 620),
            styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "用量小助手 · Markdown、工程与周期"
        window.contentView = NSHostingView(rootView: ProjectSettingsView(model: model, usage: usage))
        window.isReleasedWhenClosed = false
        window.center()
        super.init(window: window)
        window.delegate = self
    }
    func windowWillClose(_ notification: Notification) { NSApp.setActivationPolicy(.accessory) }
    required init?(coder: NSCoder) { fatalError("init(coder:) is not supported") }
}
