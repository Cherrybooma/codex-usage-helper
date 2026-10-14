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

@MainActor
enum WidgetApplicationMenu {
    static func make(settingsTarget: NSObject? = nil) -> NSMenu {
        let menu = NSMenu()
        let application = NSMenuItem()
        application.submenu = NSMenu(title: "用量小助手")
        let settings = application.submenu!.addItem(withTitle: "项目与 Notion 设置…",
            action: #selector(AppDelegate.openSettings), keyEquivalent: ",")
        settings.target = settingsTarget
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
    @Published var createIconMode = "emoji"
    @Published var createEmoji = ""
    @Published var createLogoURL = ""
    weak var usage: UsageModel?
    let root: URL
    let python: URL

    init(usage: UsageModel) {
        self.usage = usage; root = usage.root; python = usage.python
        let settings = try? WidgetPreferences.read(root: root)
        taskRoot = settings?["task_root"] as? String ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Documents").path
        tokenSourceFile = settings?["notion_token_source_file"] as? String ?? ""
        let configFile = root.appendingPathComponent("widget/data/notion-config.json")
        if let data = try? Data(contentsOf: configFile),
           let config = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let parent = config["parent_page_id"] as? String { parentPage = "https://www.notion.so/" + parent.replacingOccurrences(of: "-", with: "") }
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
                Text("横条和竖条保持桌面层；普通窗口可以盖住它们。显示选择和位置会自动保存。")
                    .font(.callout).foregroundStyle(.secondary)
                Text("液态玻璃使用系统材质；macOS 26 启用系统玻璃效果，较早系统使用原生透明材质。悬停展开项目看板。")
                    .font(.callout).foregroundStyle(.secondary)
            }
            if model.busy { ProgressView().controlSize(.small) }
            if !model.message.isEmpty { Text(model.message).font(.callout).textSelection(.enabled) }
        }.formStyle(.grouped).padding(12).frame(width: 640, height: 620)
            .onAppear { model.perform(["command": "status"]) }
    }
}

@MainActor
final class ProjectSettingsController: NSWindowController, NSWindowDelegate {
    init(usage: UsageModel) {
        let model = ProjectSettingsModel(usage: usage)
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 640, height: 620),
            styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "用量小助手 · 项目与 Notion"
        window.contentView = NSHostingView(rootView: ProjectSettingsView(model: model, usage: usage))
        window.isReleasedWhenClosed = false
        window.center()
        super.init(window: window)
        window.delegate = self
    }
    func windowWillClose(_ notification: Notification) { NSApp.setActivationPolicy(.accessory) }
    required init?(coder: NSCoder) { fatalError("init(coder:) is not supported") }
}
