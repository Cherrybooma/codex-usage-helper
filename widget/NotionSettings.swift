import AppKit
import SwiftUI
import Security

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
}

@MainActor
final class ProjectSettingsModel: ObservableObject {
    @Published var taskRoot: String
    @Published var parentPage = ""
    @Published var token = ""
    @Published var busy = false
    @Published var message = ""
    @Published var choices: [NotionChoice] = []
    @Published var selectedTask = ""
    @Published var selectedPage = ""
    @Published var statusText = "尚未连接 Notion"
    @Published var projectsURL: String?
    @Published var usageURL: String?
    weak var usage: UsageModel?
    let root: URL
    let python: URL

    init(usage: UsageModel) {
        self.usage = usage; root = usage.root; python = usage.python
        let file = root.appendingPathComponent("widget/data/app-settings.json")
        let settings = (try? Data(contentsOf: file)).flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
        taskRoot = settings?["task_root"] as? String ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Documents").path
        let configFile = root.appendingPathComponent("widget/data/notion-config.json")
        if let data = try? Data(contentsOf: configFile),
           let config = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let parent = config["parent_page_id"] as? String { parentPage = "https://www.notion.so/" + parent.replacingOccurrences(of: "-", with: "") }
    }
    var tasks: [LocalProject] { usage?.snapshot?.available_projects ?? [] }

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
            let data = try JSONSerialization.data(withJSONObject: ["task_root": path], options: [.prettyPrinted])
            try data.write(to: folder.appendingPathComponent("app-settings.json"), options: .atomic)
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
        choices = (status["projects"] as? [[String: Any]] ?? [])
            .filter { $0["available"] as? Bool != false }
            .compactMap { item in
                guard let id = item["page_id"] as? String, let name = item["display_name"] as? String else { return nil }
                return NotionChoice(id: id, name: name)
            }.sorted { $0.name < $1.name }
        if selectedTask.isEmpty { selectedTask = tasks.first?.task_id ?? "" }
        if selectedPage.isEmpty { selectedPage = choices.first?.id ?? "" }
    }
    func perform(_ command: [String: Any]) {
        guard !busy else { return }
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
                    if json["ok"] as? Bool == true { message = "操作完成。" }
                    else { message = (json["error"] as? [String: Any])?["message"] as? String ?? "连接未完成，请检查授权和父页面权限。" }
                } else { message = "操作结果无法解析，保留原设置。" }
            case .failure(let error): message = error.localizedDescription
            }
            busy = false
            usage?.refresh()
        }
    }
    func connect() {
        do { try NotionCredential.save(token); token = "" }
        catch { message = error.localizedDescription; return }
        perform(["command": "setup", "parent_page": parentPage])
    }
    func create() {
        guard let task = tasks.first(where: { $0.task_id == selectedTask }) else { return }
        perform(["command": "create_project", "task_id": task.task_id, "name": task.name])
    }
    func bind() {
        guard !selectedTask.isEmpty, !selectedPage.isEmpty else { return }
        perform(["command": "bind", "task_id": selectedTask, "page_id": selectedPage])
    }
}

struct ProjectSettingsView: View {
    @ObservedObject var model: ProjectSettingsModel
    @ObservedObject var usage: UsageModel
    var body: some View {
        Form {
            Section("本地项目") {
                Text("每个直接子文件夹作为一个任务。未绑定、暂停和归档的项目仍计入用量。")
                    .font(.callout).foregroundStyle(.secondary)
                HStack { TextField("项目根目录", text: $model.taskRoot); Button("选择…") { model.chooseRoot() }; Button("保存") { model.saveRoot() } }
            }
            Section("连接 Notion") {
                Text(model.statusText).font(.callout)
                SecureField("Notion Integration Token（仅存系统钥匙串）", text: $model.token)
                TextField("已分享给 Integration 的父页面链接", text: $model.parentPage)
                HStack {
                    Button("连接并创建两个数据库") { model.connect() }
                    Button("刷新项目状态") { model.perform(["command": "projects", "force": true]) }
                    if let url = model.projectsURL { Button("打开项目库") { usage.openProject(url) } }
                    if let url = model.usageURL { Button("打开用量库") { usage.openProject(url) } }
                }
                Text("在 Notion 中创建内部 Integration，授予读取、插入和更新内容权限，再把父页面分享给它。首次同步从本周开始。")
                    .font(.callout).foregroundStyle(.secondary)
            }
            Section("绑定项目") {
                Picker("本地任务", selection: $model.selectedTask) {
                    Text("请选择").tag("")
                    ForEach(model.tasks) { task in Text(task.name + (task.notion_page_id == nil ? " · 未绑定" : " · 已绑定")).tag(task.task_id) }
                }
                Picker("Notion 项目", selection: $model.selectedPage) {
                    Text("请选择").tag("")
                    ForEach(model.choices) { page in Text(page.name).tag(page.id) }
                }
                HStack {
                    Button("绑定所选项目") { model.bind() }.disabled(model.selectedTask.isEmpty || model.selectedPage.isEmpty)
                    Button("按任务名新建并绑定") { model.create() }.disabled(model.selectedTask.isEmpty)
                    Button("解除绑定") { model.perform(["command": "unbind", "task_id": model.selectedTask]) }.disabled(model.selectedTask.isEmpty)
                }
                Text("改名后绑定仍然保留。文件夹移动后可以把新位置绑定到同一个 Notion 项目；不会自动创建项目。")
                    .font(.callout).foregroundStyle(.secondary)
            }
            Section("同步与显示") {
                HStack {
                    Button(usage.notionSyncing ? "同步中…" : "立即同步摘要") { usage.syncNotion(force: true) }.disabled(usage.notionSyncing)
                    Text(usage.notionSyncMessage ?? "每 5 分钟同步；离线仍可查看本地用量。").font(.callout).foregroundStyle(.secondary)
                }
                HStack {
                    ForEach(WidgetKind.allCases, id: \.self) { kind in
                        Toggle(kind.title, isOn: Binding(get: { usage.enabledWidgets.contains(kind) }, set: { _ in usage.toggleWidget(kind) }))
                    }
                }
                Text("同步仅发送项目关联、日期、计数与统计质量。对话、代码、工具输出、目录及会话 ID 留在本机。")
                    .font(.callout).foregroundStyle(.secondary)
            }
            if model.busy { ProgressView().controlSize(.small) }
            if !model.message.isEmpty { Text(model.message).font(.callout).textSelection(.enabled) }
        }.formStyle(.grouped).padding(12).frame(width: 640, height: 620)
            .disabled(model.busy)
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
