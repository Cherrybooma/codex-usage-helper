import AppKit
import SwiftUI
import Security
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

    static func notionEnabled(in settings: [String: Any]) -> Bool {
        // Reject strings and numeric values: syncing requires an explicit opt-in.
        guard let value = settings["notion_sync_enabled"] as? NSNumber,
              CFGetTypeID(value) == CFBooleanGetTypeID() else { return false }
        return value.boolValue
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
        let settings = application.submenu!.addItem(withTitle: "项目与 Notion 设置…",
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

// Only this application's dedicated credential is written. Nothing is read
// from Codex authentication files or another application's Keychain item.
enum NotionCredential {
    static let service = "local.codex.usage-widget.notion"
    static func save(_ value: String) throws {
        let token = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !token.isEmpty else { return }
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service, kSecAttrAccount as String: "integration"]
        let data = Data(token.utf8)
        var status = SecItemUpdate(query as CFDictionary, [kSecValueData as String: data] as CFDictionary)
        if status == errSecItemNotFound {
            var item = query
            item[kSecValueData as String] = data
            item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
            status = SecItemAdd(item as CFDictionary, nil)
        }
        guard status == errSecSuccess else {
            throw NSError(domain: "UsageWidget", code: Int(status),
                userInfo: [NSLocalizedDescriptionKey: "凭据未保存到钥匙串，请检查系统授权。"])
        }
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

struct NotionChoice: Identifiable {
    let id: String
    let name: String
    let iconMissing: Bool
}

enum ProjectImportSelection {
    static func command(manifestURL: URL, folder: URL, taskRoot: URL,
                        parentPage: String, existingHome: Bool) throws -> [String: Any] {
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
        if !parentPage.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            command[existingHome ? "management_home" : "parent_page"] = parentPage
        }
        command["codex_home"] = ProcessInfo.processInfo.environment["CODEX_HOME"]
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".codex").path
        return command
    }
}

@MainActor
final class ProjectSettingsModel: ObservableObject {
    @Published var taskRoot: String
    @Published var parentPage = ""
    @Published var token = ""
    @Published var tokenSourceFile = ""
    @Published var busy = false
    @Published var message = ""
    @Published var choices: [NotionChoice] = []
    @Published var selectedTask = ""
    @Published var selectedPage = ""
    @Published var statusText = "尚未连接 Notion"
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
    @Published var importParentIsHome = true
    weak var usage: UsageModel?
    let root: URL
    let python: URL

    init(usage: UsageModel) {
        self.usage = usage; root = usage.root; python = usage.python
        let settings = try? WidgetPreferences.read(root: root)
        taskRoot = settings?["task_root"] as? String ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Documents").path
        tokenSourceFile = settings?["notion_token_source_file"] as? String ?? ""
        if let anchor = BillingAnchorSettings.decode(settings?["billing_anchor"] as? String) {
            hasBillingAnchor = true
            billingAnchorDate = anchor
            billingAnchorPrecision = BillingAnchorPrecision(rawValue: settings?["billing_anchor_precision"] as? String ?? "") ?? .datetime
        }
        let configFile = root.appendingPathComponent("widget/data/notion-config.json")
        if let data = try? Data(contentsOf: configFile),
           let config = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            if let parent = config["parent_page_id"] as? String { parentPage = "https://www.notion.so/" + parent.replacingOccurrences(of: "-", with: "") }
            accountingURL = config["accounting_page_url"] as? String
        }
    }
    var tasks: [LocalProject] { usage?.snapshot?.available_projects ?? [] }
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
    var selectedPageNeedsIcon: Bool { choices.first(where: { $0.id == selectedPage })?.iconMissing == true }
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
        let configFile = root.appendingPathComponent("widget/data/notion-config.json")
        if let data = try? Data(contentsOf: configFile),
           let config = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let bindings = config["bindings"] as? [String: String], !bindings.isEmpty {
            let settingsFile = root.appendingPathComponent("widget/data/app-settings.json")
            let settings = (try? Data(contentsOf: settingsFile)).flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            let prior = settings?["task_root"] as? String ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Documents").path
            if URL(fileURLWithPath: path).standardizedFileURL != URL(fileURLWithPath: prior).standardizedFileURL {
                message = "已绑定 Notion 后更改根目录会改变历史归集。请先解除项目绑定，再选择新根目录并重新绑定；原用量记录保留审计。"
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
    func apply(_ status: [String: Any]) {
        statusText = status["message"] as? String ?? "尚未连接 Notion"
        projectsURL = status["projects_url"] as? String
        usageURL = status["usage_url"] as? String
        tasksURL = status["tasks_url"] as? String
        accountingURL = status["accounting_url"] as? String ?? accountingURL
        choices = (status["projects"] as? [[String: Any]] ?? [])
            .filter { $0["available"] as? Bool != false }
            .compactMap { item in
                guard let id = item["page_id"] as? String, let name = item["display_name"] as? String else { return nil }
                let missing = item["icon_missing"] as? Bool ?? ((item["icon_value"] as? String)?.isEmpty != false)
                return NotionChoice(id: id, name: name, iconMissing: missing)
            }.sorted { $0.name < $1.name }
        if selectedTask.isEmpty { selectedTask = tasks.first?.task_id ?? "" }
        if selectedPage.isEmpty { selectedPage = choices.first?.id ?? "" }
    }
    func perform(_ command: [String: Any]) {
        guard !busy else { return }
        guard command["command"] as? String == "status" || usage?.notionEnabled == true else {
            message = "当前为本机模式；Notion 连接可稍后开启。"; return
        }
        busy = true; message = ""
        let executable = python, folder = root
        Task {
            let result: Result<Data, Error> = await Task.detached(priority: .utility) {
                do {
                    let input = try JSONSerialization.data(withJSONObject: command)
                    return .success(try runWidgetProcess(python: executable, root: folder,
                        arguments: [folder.appendingPathComponent("widget/notion_sync.py").path,
                                    "--data-dir", folder.appendingPathComponent("widget/data").path], input: input, timeout: 120))
                } catch { return .failure(error) }
            }.value
            switch result {
            case .success(let data):
                if let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    if let status = json["status"] as? [String: Any] { apply(status) }
                    if json["ok"] as? Bool == true { message = command["command"] as? String == "status" ? "" : "操作完成。" }
                    else { message = (json["error"] as? [String: Any])?["message"] as? String ?? "连接未完成，请检查授权和父页面权限。" }
                } else { message = "操作结果无法解析，保留原设置。" }
            case .failure(let error): message = error.localizedDescription
            }
            busy = false
            usage?.refresh()
        }
    }
    func connect() {
        guard !busy, usage?.notionEnabled == true else { return }
        do {
            if tokenSourceFile.isEmpty { try NotionCredential.save(token) }
            token = ""
        }
        catch { message = error.localizedDescription; return }
        perform(["command": "setup", "parent_page": parentPage])
    }
    func chooseTokenSource() {
        let picker = NSOpenPanel()
        picker.canChooseDirectories = false; picker.canChooseFiles = true
        picker.allowsMultipleSelection = false
        if let type = UTType(filenameExtension: "pages") { picker.allowedContentTypes = [type] }
        if picker.runModal() == .OK, let url = picker.url {
            setTokenSource(url.path)
        }
    }
    func setTokenSource(_ path: String) {
        guard !busy, usage?.notionSyncing != true else { return }
        do {
            try WidgetPreferences.update(root: root, values: ["notion_token_source_file": path.isEmpty ? NSNull() : path as Any])
            tokenSourceFile = path; token = ""
            message = "密钥来源已保存；点击连接以验证权限。"
        } catch { message = "密钥来源未保存，保留原设置。" }
    }
    func create() {
        guard let task = tasks.first(where: { $0.task_id == selectedTask }) else { return }
        guard let icon = newProjectIcon else { message = "请先选择一个 emoji 头像或填写有效 HTTPS Logo 地址。"; return }
        perform(["command": "create_project", "task_id": task.task_id, "name": task.name, "icon": icon])
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
        guard !busy, usage?.notionEnabled == true, usage?.notionSyncing != true else { return }
        let command: [String: Any]
        do {
            let saved = try WidgetPreferences.read(root: root)
            let savedRoot = saved["task_root"] as? String
                ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Documents").path
            guard URL(fileURLWithPath: (taskRoot as NSString).expandingTildeInPath).resolvingSymlinksInPath().standardizedFileURL
                    == URL(fileURLWithPath: savedRoot).resolvingSymlinksInPath().standardizedFileURL else {
                message = "请先保存项目根目录，再导入项目。"; return
            }
            command = try ProjectImportSelection.command(manifestURL: URL(fileURLWithPath: importManifestPath),
                folder: URL(fileURLWithPath: importFolderPath),
                taskRoot: URL(fileURLWithPath: (taskRoot as NSString).expandingTildeInPath),
                parentPage: parentPage, existingHome: importParentIsHome)
            if tokenSourceFile.isEmpty { try NotionCredential.save(token) }
            token = ""
        } catch { message = error.localizedDescription; return }
        busy = true; message = "正在导入项目…"
        let executable = python, folder = root
        Task {
            let result: Result<Data, Error> = await Task.detached(priority: .utility) {
                do {
                    let input = try JSONSerialization.data(withJSONObject: command)
                    return .success(try runWidgetProcess(python: executable, root: folder,
                        arguments: [folder.appendingPathComponent("widget/project_import.py").path,
                                    "--data-dir", folder.appendingPathComponent("widget/data").path],
                        input: input, timeout: 240))
                } catch { return .failure(error) }
            }.value
            var imported = false
            switch result {
            case .success(let data):
                if let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    if let status = json["status"] as? [String: Any] { apply(status) }
                    if json["ok"] as? Bool == true {
                        imported = true
                        message = "项目已导入并绑定；胶囊按本周用量显示前四项，其余归入其他。"
                    } else {
                        message = (json["error"] as? [String: Any])?["message"] as? String ?? "导入尚未完成，可以用同一清单重试。"
                    }
                } else { message = "导入结果无法确认；保留原记录，使用同一清单重试。" }
            case .failure(let error): message = error.localizedDescription
            }
            busy = false
            if imported { usage?.syncNotion(force: true) }
            usage?.refresh()
        }
    }
    func bind() {
        guard !selectedTask.isEmpty, !selectedPage.isEmpty else { return }
        guard !selectedPageNeedsIcon else {
            message = "这个项目还没有头像。请先在 Notion 设置头像，再刷新并绑定；此处不会替你修改云端项目。"
            return
        }
        perform(["command": "bind", "task_id": selectedTask, "page_id": selectedPage])
    }
}

struct ProjectSettingsView: View {
    @ObservedObject var model: ProjectSettingsModel
    @ObservedObject var usage: UsageModel
    var body: some View {
        Form {
            Section("运行方式") {
                Toggle("启用 Notion 同步", isOn: Binding(get: { usage.notionEnabled },
                    set: { usage.setNotionEnabled($0) }))
                    .disabled(model.busy || usage.notionSyncing)
                Text(usage.notionEnabled ? "本机负责计量，Notion 保存已绑定项目的每日摘要。" : "本机模式：无需 Token。项目统计、历史账本和桌面组件照常运行。")
                    .font(.callout).foregroundStyle(.secondary)
                if let message = usage.notionSyncMessage { Text(message).font(.callout).foregroundStyle(.secondary) }
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
                Text("每个直接子文件夹作为一个任务。未绑定、暂停和归档的项目仍计入用量。")
                    .font(.callout).foregroundStyle(.secondary)
                HStack {
                    TextField("项目根目录", text: $model.taskRoot)
                    Button("选择…") { model.chooseRoot() }.disabled(model.busy)
                    Button("保存") { model.saveRoot() }.disabled(model.busy)
                }
            }
            if usage.notionEnabled {
                Section("连接 Notion") {
                    Text(model.statusText).font(.callout)
                    HStack {
                        Text(model.tokenSourceFile.isEmpty ? "密钥来源：系统钥匙串" : "密钥来源：" + URL(fileURLWithPath: model.tokenSourceFile).lastPathComponent)
                            .font(.callout).lineLimit(1)
                        Button("选择 Pages 文件…") { model.chooseTokenSource() }
                        if !model.tokenSourceFile.isEmpty { Button("改用钥匙串") { model.setTokenSource("") } }
                    }.disabled(model.busy || usage.notionSyncing)
                    if model.tokenSourceFile.isEmpty {
                        SecureField("Notion Integration Token（仅存系统钥匙串）", text: $model.token)
                    } else {
                        Text("从所选文件提取 Token 用于 Notion 认证；不会上传 Pages 文件。")
                            .font(.callout).foregroundStyle(.secondary)
                    }
                    TextField("已分享给 Integration 的父页面链接", text: $model.parentPage)
                    HStack {
                        Button("初始化项目工作台") { model.connect() }
                        Button("刷新项目状态") { model.perform(["command": "projects", "force": true]) }
                        if let url = model.projectsURL { Button("打开项目库") { usage.openProject(url) } }
                        if let url = model.tasksURL { Button("打开任务库") { usage.openProject(url) } }
                        if let url = model.usageURL { Button("打开用量库") { usage.openProject(url) } }
                    }.disabled(model.busy)
                    Text("在 Notion 中创建内部 Integration，授予读取、插入和更新内容权限，再把父页面分享给它。首次同步从本周开始。")
                        .font(.callout).foregroundStyle(.secondary)
                }
                Section("绑定项目") {
                    Picker("本地任务", selection: $model.selectedTask) {
                        Text("请选择").tag("")
                        ForEach(model.tasks) { task in Text(task.name + (task.notion_page_id == nil ? " · 未绑定" : " · 已绑定")).tag(task.task_id) }
                    }.disabled(model.busy)
                    Picker("Notion 项目", selection: $model.selectedPage) {
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
                                ForEach(["📦", "🎙️", "🛠️", "📝", "🌱", "🚀"], id: \.self) { value in
                                    Button(value) { model.createEmoji = value }
                                }
                            }
                        }.disabled(model.busy)
                    } else {
                        TextField("项目 Logo 的 HTTPS 地址", text: $model.createLogoURL).disabled(model.busy)
                    }
                    HStack {
                        Button("绑定所选项目") { model.bind() }.disabled(model.selectedTask.isEmpty || model.selectedPage.isEmpty || model.selectedPageNeedsIcon)
                        Button("按任务名新建并绑定") { model.create() }.disabled(model.selectedTask.isEmpty || model.newProjectIcon == nil)
                        Button("解除绑定") { model.perform(["command": "unbind", "task_id": model.selectedTask]) }.disabled(model.selectedTask.isEmpty)
                    }.disabled(model.busy)
                    Text("改名后绑定仍然保留。文件夹移动后可以把新位置绑定到同一个 Notion 项目；不会自动创建项目。")
                        .font(.callout).foregroundStyle(.secondary)
                    Text("项目头像会用于桌面胶囊。新建时必须选择头像；已有项目没有头像时，请在 Notion 设置后再绑定。")
                        .font(.callout).foregroundStyle(.secondary)
                }
                Section("一键导入已有项目") {
                    Text("先让 Codex 调用 notion-project-workbench 整理项目清单，再选择清单和本机文件夹。")
                        .font(.callout).foregroundStyle(.secondary)
                    HStack {
                        Text(model.importManifestPath.isEmpty ? "尚未选择项目清单" : URL(fileURLWithPath: model.importManifestPath).lastPathComponent)
                            .font(.callout).lineLimit(1)
                        Spacer()
                        Button("选择项目清单…") { model.chooseImportManifest() }
                    }
                    HStack {
                        Text(model.importFolderPath.isEmpty ? "尚未选择项目文件夹" : URL(fileURLWithPath: model.importFolderPath).lastPathComponent)
                            .font(.callout).lineLimit(1)
                        Spacer()
                        Button("选择项目文件夹…") { model.chooseImportFolder() }
                    }
                    Toggle("上面的父页面是已有项目管理主页面", isOn: $model.importParentIsHome)
                    Text("已有连接会复用原工作台。首次连接时，关闭此选项会在所选父页面下新建项目管理主页面。")
                        .font(.callout).foregroundStyle(.secondary)
                    Button("一键导入项目") { model.importProject() }
                        .disabled(model.importManifestPath.isEmpty || model.importFolderPath.isEmpty)
                }.disabled(model.busy || usage.notionSyncing)
                Section("摘要同步") {
                    HStack {
                        Button(usage.notionSyncing ? "同步中…" : "立即同步摘要") { usage.syncNotion(force: true) }.disabled(usage.notionSyncing)
                        Text(usage.notionSyncMessage ?? "每 5 分钟同步；离线仍可查看本地用量。").font(.callout).foregroundStyle(.secondary)
                    }
                    Text("同步仅发送项目关联、日期、计数与统计质量。对话、代码、工具输出、目录及会话 ID 留在本机。")
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
    }
}

@MainActor
final class ProjectSettingsController: NSWindowController, NSWindowDelegate {
    init(usage: UsageModel) {
        let model = ProjectSettingsModel(usage: usage)
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 640, height: 620),
            styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "用量小助手 · 项目、周期与 Notion"
        window.contentView = NSHostingView(rootView: ProjectSettingsView(model: model, usage: usage))
        window.isReleasedWhenClosed = false
        window.center()
        super.init(window: window)
        window.delegate = self
    }
    func windowWillClose(_ notification: Notification) { NSApp.setActivationPolicy(.accessory) }
    required init?(coder: NSCoder) { fatalError("init(coder:) is not supported") }
}
