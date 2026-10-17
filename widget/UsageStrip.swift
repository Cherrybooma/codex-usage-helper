import AppKit
import SwiftUI

enum WidgetKind: String, Codable, CaseIterable {
    case horizontal, vertical, box
    var title: String {
        switch self {
        case .horizontal: return "顶部横条"
        case .vertical: return "右侧竖条"
        case .box: return "桌面小框"
        }
    }
}

enum WidgetStyle: String, Codable, CaseIterable, Identifiable {
    case glass, pulse, white
    var id: String { rawValue }
    var title: String {
        switch self { case .glass: return "液态玻璃"; case .pulse: return "Pulse 暗色"; case .white: return "经典白色" }
    }
    var primary: Color { self == .pulse ? Color(white: 0.95) : StripInk.primary }
    var secondary: Color { self == .pulse ? Color(red: 0.72, green: 0.73, blue: 0.74) : StripInk.secondary }
    var accent: Color { self == .pulse ? Color(red: 0.70, green: 0.91, blue: 0.76) : Color(red: 0.078, green: 0.424, blue: 0.227) }
    var progress: Color { self == .pulse ? Color(red: 0.88, green: 0.68, blue: 0.45) : Color(red: 0.46, green: 0.59, blue: 0.69) }
    var track: Color { primary.opacity(self == .pulse ? 0.14 : 0.10) }
    var appearance: NSAppearance? { NSAppearance(named: self == .pulse ? .darkAqua : .aqua) }
}

enum StripQuotaMetric: String, CaseIterable, Identifiable {
    case weeklyRemaining, shortRemaining, weeklyUsed, weeklyObserved, weeklyExcess
    var id: String { rawValue }
    var caption: String {
        switch self {
        case .weeklyRemaining: return "周剩余"
        case .shortRemaining: return "5h 剩余"
        case .weeklyUsed: return "周额度已用"
        case .weeklyObserved: return "本周累计"
        case .weeklyExcess: return "本周超额"
        }
    }
    func advanced(by steps: Int) -> Self {
        let values = Self.allCases
        let index = values.firstIndex(of: self) ?? 0
        return values[((index + steps) % values.count + values.count) % values.count]
    }
}

struct StripFact: Identifiable {
    var id: String { label }
    let label: String
    let value: String
}

enum StripAvatar: Equatable {
    case openAI, symbol(String), emoji(String), remote(String)

    static func project(_ row: TaskRow) -> Self {
        if row.icon_type == "emoji", let value = row.icon_value, isEmojiIcon(value) { return .emoji(value) }
        if ["external", "file", "custom_emoji"].contains(row.icon_type ?? ""), let value = row.icon_value,
           safeRemoteURL(value) != nil { return .remote(value) }
        return .symbol("folder")
    }
    static func isEmojiIcon(_ value: String) -> Bool {
        guard value.count == 1 else { return false }
        return value.unicodeScalars.contains { $0.properties.isEmojiPresentation || ($0.value > 0x7F && $0.properties.isEmoji) || $0.value == 0x20E3 }
    }
    static func safeRemoteURL(_ value: String) -> URL? {
        guard !value.isEmpty, value.count <= 4096,
              value.rangeOfCharacter(from: .whitespacesAndNewlines.union(.controlCharacters)) == nil,
              let components = URLComponents(string: value), components.scheme == "https",
              components.host?.isEmpty == false, components.user == nil, components.password == nil,
              components.port.map({ 1...65535 ~= $0 }) ?? true else { return nil }
        return components.url
    }
}

// Validate every redirect as well as the original cached URL. This session has
// no account credentials or cookies and keeps normal HTTPS signed asset URLs.
final class ProjectAvatarRedirects: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    private var redirectCount = 0
    static func permitted(_ request: URLRequest) -> Bool {
        request.url.map { StripAvatar.safeRemoteURL($0.absoluteString) != nil } ?? false
    }
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        redirectCount += 1
        completionHandler(redirectCount <= 8 && Self.permitted(request) ? request : nil)
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        completionHandler(challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust ? .performDefaultHandling : .cancelAuthenticationChallenge, nil)
    }
}

@MainActor
final class ProjectAvatarLoader: ObservableObject {
    @Published var image: NSImage?
    @Published var failed = false
    private var session: URLSession?
    private var task: URLSessionDataTask?
    private var value: String?
    private var generation = UUID()

    func load(_ value: String) {
        if self.value == value && image != nil { return }
        cancel()
        self.value = value; image = nil; failed = false
        guard let url = StripAvatar.safeRemoteURL(value) else { failed = true; return }
        let config = URLSessionConfiguration.ephemeral
        config.httpCookieStorage = nil; config.httpShouldSetCookies = false
        config.urlCredentialStorage = nil; config.urlCache = nil
        config.timeoutIntervalForRequest = 15; config.timeoutIntervalForResource = 20
        let session = URLSession(configuration: config, delegate: ProjectAvatarRedirects(), delegateQueue: nil)
        self.session = session
        let generation = self.generation
        task = session.dataTask(with: url) { [weak self] data, response, error in
            session.finishTasksAndInvalidate()
            Task { @MainActor [weak self] in
                guard let self, self.value == value, self.generation == generation else { return }
                let valid = error == nil && (response as? HTTPURLResponse)?.statusCode == 200
                    && response?.url.map { StripAvatar.safeRemoteURL($0.absoluteString) != nil } == true
                self.image = valid ? data.flatMap(NSImage.init(data:)) : nil
                self.failed = self.image == nil
                self.task = nil
            }
        }
        task?.resume()
    }
    func cancel() { generation = UUID(); task?.cancel(); task = nil; session?.invalidateAndCancel(); session = nil }
    deinit { task?.cancel(); session?.invalidateAndCancel() }
}

private struct ProjectAvatarImage: View {
    let value: String
    @StateObject private var loader = ProjectAvatarLoader()
    var body: some View {
        Group {
            if let image = loader.image { Image(nsImage: image).resizable().scaledToFill() }
            else {
                Image(systemName: "folder").font(.system(size: 18, weight: .regular))
                    .frame(maxWidth: .infinity, maxHeight: .infinity).background(Color.primary.opacity(0.08))
            }
        }.task(id: value) { loader.load(value) }.onDisappear { loader.cancel() }
            .help(loader.failed ? "图标加载失败，请在 Notion 更新头像" : "Notion 项目图标")
    }
}

struct StripItem: Identifiable {
    let id: String
    let caption: String
    let value: String
    let title: String
    let detailValue: String
    let unit: String
    let footnote: String
    var members: [TaskRow] = []
    var facts: [StripFact] = []
    var managementFacts: [StripFact] = []
    var avatar: StripAvatar = .symbol("ellipsis")
    var projectStatus: String?
    var notionURL: String?
    var notionStale = false
    var needsProjectIcon = false
    var usageLinked = true
    var managementList = false
    var primaryRing: Double?
    var secondaryRing: Double?
    var developmentProgress: Double?
    var progressLabel = "完成进度"
    var todayTokens: Int?
    var ringStale = false
    var estimatedCost = "—"
    var latestOutputTitle: String?
    var latestOutputURL: String?
    var managedMembers: [TaskRow] = []
    var isProject: Bool { id.hasPrefix("folder:") }

    static func projectDate(_ value: String) -> String {
        guard value.contains("T") else { return value }
        let iso = ISO8601DateFormatter()
        iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        var parsed = iso.date(from: value)
        if parsed == nil {
            iso.formatOptions = [.withInternetDateTime]
            parsed = iso.date(from: value)
        }
        guard let parsed else { return value }
        let calendar = DateFormatter()
        calendar.locale = Locale(identifier: "en_US_POSIX")
        calendar.calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "Asia/Shanghai")
        calendar.dateFormat = "yyyy-MM-dd"
        return calendar.string(from: parsed)
    }

    static func fraction(_ value: Double?) -> Double? {
        guard let value, value.isFinite, value >= 0 else { return nil }
        return min(1, value / 100)
    }

    static func projectProgress(_ value: Double?, source: String?) -> Double? {
        guard ["manual", "ai"].contains(source ?? ""), let value, value.isFinite, 0...100 ~= value else { return nil }
        return value
    }

    static func statusLabel(_ value: String?) -> String? {
        switch value {
        case "正常": return "顺利"
        case "阻塞", "受阻": return "遇到卡点"
        default: return value
        }
    }

    static func costLabel(_ amount: Double?, status: String?, lowerBound: Bool? = nil) -> String {
        guard let amount, amount.isFinite, amount >= 0,
              ["estimated", "lower_bound"].contains(status ?? "") else { return "—" }
        return String(format: (status == "lower_bound" || lowerBound == true) ? "≥$%.2f" : "≈$%.2f", amount)
    }

    private static func notionIdentity(_ row: TaskRow) -> String? {
        guard let value = row.notion_url, let url = URL(string: value) else { return nil }
        let component = url.lastPathComponent.replacingOccurrences(of: "-", with: "")
        let suffix = String(component.suffix(32)).lowercased()
        if suffix.count == 32 && suffix.allSatisfy({ $0.isHexDigit }) { return "page:" + suffix }
        return value.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
    }

    static func availableManagedProjects(_ snapshot: Snapshot?) -> [TaskRow] {
        var projects: [TaskRow] = [], indices: [String: Int] = [:]
        // A Notion page is one project even when several local folders bind to it.
        // Keep every binding before deduplication so aggregation cannot lose a folder.
        for row in (snapshot?.management_projects ?? []).sorted(by: { $0.id < $1.id }) {
            guard row.is_other != true, !["归档", "已归档"].contains(row.project_status ?? ""),
                  let id = row.task_id, !id.isEmpty else { continue }
            let key = notionIdentity(row) ?? row.id
            if let index = indices[key] {
                projects[index].task_ids = Array(Set((projects[index].task_ids ?? []) + (row.task_ids ?? []) + [id])).sorted()
            } else {
                var project = row
                project.task_ids = Array(Set((row.task_ids ?? []) + [id])).sorted()
                indices[key] = projects.count
                projects.append(project)
            }
        }
        return projects
    }

    private static func matches(_ project: TaskRow, usage: TaskRow) -> Bool {
        if let id = usage.task_id, project.task_id == id || project.task_ids?.contains(id) == true { return true }
        if let page = notionIdentity(project), page == notionIdentity(usage) { return true }
        return false
    }

    static func items(_ snapshot: Snapshot?, failed: Bool = false,
                      metric: StripQuotaMetric = .weeklyRemaining, now: Date = Date(),
                      managementEnabled: Bool = true) -> [StripItem] {
        // The approved Codex entry has one fixed meaning: current weekly quota remaining.
        // Retain the parameter only for old callers; it cannot change the capsule's meaning.
        guard let snapshot else {
            return [StripItem(id: "quota", caption: "周额度剩余", value: "—", title: "Codex",
                             detailValue: "—", unit: "比例", footnote: failed ? "读取失败" : "正在读取",
                             facts: [StripFact(label: "本周已用额度", value: "—"), StripFact(label: "本周 Token", value: "—")], avatar: .openAI),
                    StripItem(id: "total", caption: "本期 Token", value: "—", title: "本期 Token",
                             detailValue: "—", unit: "Token", footnote: "正在读取",
                             facts: [StripFact(label: "任务数量", value: "—"), StripFact(label: "统计时间", value: "—"),
                                     StripFact(label: "模型响应次数", value: "—")], avatar: .symbol("book.closed"))]
        }
        let week = snapshot.quota?.windows.first { $0.duration_mins == 10080 }
        let stale = snapshot.quota?.stale == true || snapshot.quota_error != nil || failed
            || week.map { $0.resets_at <= now.timeIntervalSince1970 } == true
        let remainingPercent = week.flatMap { window -> Double? in
            guard window.used_percent.isFinite, 0...100 ~= window.used_percent else { return nil }
            return 100 - window.used_percent
        }
        let remaining = remainingPercent.map { value in
            (stale ? "≈" : "") + (value > 0 && value < 1 ? "<1%" : String(format: "%.0f%%", value))
        } ?? "—"
        let budget = snapshot.weekly_budget
        let weeklyUsed: String
        if let value = budget?.observed_total_percent, value.isFinite, value >= 0 {
            let quality = budget?.usage_quality ?? (budget?.status == "estimated" ? "estimated" : "unavailable")
            weeklyUsed = ["estimated", "lower_bound"].contains(quality)
                ? String(format: quality == "lower_bound" ? "≥%.1f%%" : "≈%.1f%%", value) : "—"
        } else { weeklyUsed = "—" }
        var globalAmount = snapshot.estimated_cost_usd
        var globalStatus = snapshot.estimated_cost_status
        if globalAmount == nil, globalStatus == nil, budget?.status == "estimated",
           budget?.usage_quality == nil || budget?.usage_quality == "estimated",
           let used = budget?.observed_total_percent, used.isFinite, used >= 0 {
            globalAmount = 5 * used / 100; globalStatus = "estimated"
        }
        let globalCost = costLabel(globalAmount, status: globalStatus, lowerBound: snapshot.estimated_cost_lower_bound)
        let sourceRows = snapshot.all_usage_tasks ?? (snapshot.tasks.filter { $0.is_other != true } + snapshot.other_tasks)
        var seen = Set<String>()
        let ranked = sourceRows.filter { $0.is_other != true && $0.total_tokens > 0 && seen.insert($0.id).inserted }.sorted {
            $0.total_tokens == $1.total_tokens ? $0.id < $1.id : $0.total_tokens > $1.total_tokens
        }
        var result = [
            StripItem(id: "quota", caption: "周额度剩余", value: remaining, title: "Codex",
                      detailValue: remaining, unit: "比例", footnote: stale ? "等待更新" : remainingPercent == 0 ? "当前窗口已用尽" : "",
                      facts: [StripFact(label: "本周已用额度", value: weeklyUsed),
                              StripFact(label: "本周 Token", value: compactTokens(snapshot.total_tokens))],
                      avatar: .openAI, primaryRing: fraction(remainingPercent), todayTokens: snapshot.today_tokens,
                      ringStale: stale, estimatedCost: globalCost),
            StripItem(id: "total", caption: "本期 Token", value: compactTokens(snapshot.total_tokens),
                      title: "本期 Token", detailValue: compactTokens(snapshot.total_tokens), unit: "Token",
                      footnote: snapshot.legacy_updates > 0 ? "含旧记录估算" : "",
                      facts: [StripFact(label: "任务数量", value: "\(snapshot.task_count ?? ranked.count) 个"),
                              StripFact(label: "模型响应次数", value: "\(snapshot.request_records) 次")],
                      avatar: .symbol("book.closed"), todayTokens: snapshot.today_tokens)
        ]
        if let start = budget?.period_start, let end = budget?.period_end {
            result[1].facts.append(StripFact(label: "统计时间", value: localDate(start) + " — " + localDate(end - 1)))
        } else if let start = snapshot.range?.since_inclusive, let end = snapshot.range?.until_exclusive {
            result[1].facts.append(StripFact(label: "统计时间", value: projectDate(start) + " — " + projectDate(end)))
        } else { result[1].facts.append(StripFact(label: "统计时间", value: "—")) }
        let managed = managementEnabled ? availableManagedProjects(snapshot) : []
        var projects: [(usage: [TaskRow], metadata: TaskRow)] = []
        var claimedUsageIDs = Set<String>()
        for project in managed {
            let usage = ranked.filter { !claimedUsageIDs.contains($0.id) && matches(project, usage: $0) }
            claimedUsageIDs.formUnion(usage.map(\.id))
            projects.append((usage, project))
        }
        if managed.isEmpty {
            // Standalone mode and old offline snapshots retain the local folder ranking.
            projects = ranked.map { ([$0], $0) }
        }
        projects.sort {
            let left = $0.usage.reduce(0) { $0 + $1.total_tokens }
            let right = $1.usage.reduce(0) { $0 + $1.total_tokens }
            return left == right ? $0.metadata.id < $1.metadata.id : left > right
        }
        let displayed = Array(projects.prefix(4))
        var displayedUsageIDs = Set<String>(), displayedManagedIDs = Set<String>()
        for (usage, metadata) in displayed {
            displayedUsageIDs.formUnion(usage.map(\.id))
            displayedManagedIDs.insert(metadata.task_id ?? "")
            let row = usage.first ?? metadata
            // Notion enriches a task's identity and management; it never replaces its usage.
            let hasUsage = !usage.isEmpty
            let tokens = usage.reduce(0) { $0 + $1.total_tokens }
            let title = metadata.display_name?.isEmpty == false ? metadata.display_name! : metadata.name
            let percent = snapshot.total_tokens > 0 ? Double(tokens) * 100 / Double(snapshot.total_tokens) : 0
            var management: [StripFact] = []
            func add(_ label: String, _ value: String?) {
                if let value = value?.trimmingCharacters(in: .whitespacesAndNewlines), !value.isEmpty {
                    management.append(StripFact(label: label, value: value))
                }
            }
            add("当前目标", metadata.current_goal ?? row.current_goal ?? metadata.weekly_goal ?? row.weekly_goal)
            add("下一步", metadata.next_step ?? row.next_step)
            add("项目愿景", metadata.project_goal ?? row.project_goal)
            add("健康度", metadata.project_health ?? row.project_health)
            if let count = metadata.human_open_tasks ?? row.human_open_tasks { add("人工待办", "\(count) 项") }
            if let count = metadata.human_blocked_tasks ?? row.human_blocked_tasks { add("人工受阻", "\(count) 项") }
            if let count = metadata.human_completed_tasks ?? row.human_completed_tasks { add("人工已完成", "\(count) 项") }
            add("最新产出与进度记录", metadata.latest_progress_summary ?? row.latest_progress_summary)
            let development = projectProgress(metadata.development_progress ?? row.development_progress,
                                             source: metadata.progress_source ?? row.progress_source)
            let projectAvatar = StripAvatar.project(metadata)
            let ownCost: Double?
            let costStatus: String?
            if let globalAmount, ["estimated", "lower_bound"].contains(globalStatus ?? "") {
                ownCost = globalAmount * percent / 100; costStatus = globalStatus
            } else if hasUsage && usage.allSatisfy({ $0.estimated_cost_usd != nil && ["estimated", "lower_bound"].contains($0.estimated_cost_status ?? "") }) {
                ownCost = usage.reduce(0) { $0 + $1.estimated_cost_usd! }
                costStatus = usage.contains { $0.estimated_cost_status == "lower_bound" || $0.estimated_cost_lower_bound == true } ? "lower_bound" : "estimated"
            } else { ownCost = nil; costStatus = nil }
            let today = usage.isEmpty ? 0 : usage.allSatisfy({ ($0.today_tokens ?? -1) >= 0 })
                ? usage.reduce(0) { $0 + $1.today_tokens! } : nil
            result.append(StripItem(id: metadata.id, caption: title, value: String(format: "%.1f%%", percent),
                title: title, detailValue: compactTokens(tokens), unit: "Token", footnote: "",
                facts: [StripFact(label: "本周占比", value: String(format: "%.1f%%", percent))],
                managementFacts: management, avatar: projectAvatar,
                projectStatus: statusLabel(metadata.project_health?.isEmpty == false ? metadata.project_health : metadata.project_status ?? row.project_status),
                notionURL: metadata.notion_url ?? row.notion_url, notionStale: metadata.notion_stale == true || row.notion_stale == true,
                needsProjectIcon: projectAvatar == .symbol("folder"),
                usageLinked: hasUsage,
                primaryRing: fraction(percent), secondaryRing: fraction(development), developmentProgress: development,
                todayTokens: today,
                estimatedCost: costLabel(ownCost, status: costStatus, lowerBound: snapshot.estimated_cost_lower_bound),
                latestOutputTitle: metadata.latest_output_title ?? row.latest_output_title,
                latestOutputURL: metadata.latest_output_url ?? row.latest_output_url))
        }
        let remainder = ranked.filter { !displayedUsageIDs.contains($0.id) }
        let managedRemainder = managed.filter { project in
            !displayedManagedIDs.contains(project.task_id ?? "") && !ranked.contains { matches(project, usage: $0) }
        }
        if !remainder.isEmpty || !managedRemainder.isEmpty {
            let tokens = remainder.reduce(0) { $0 + $1.total_tokens }
            let percent = snapshot.total_tokens > 0 ? Double(tokens) * 100 / Double(snapshot.total_tokens) : 0
            result.append(StripItem(id: "remainder", caption: "其他", value: String(format: "%.1f%%", percent),
                title: "其他任务", detailValue: compactTokens(tokens), unit: "Token", footnote: "",
                members: remainder, avatar: .symbol("ellipsis"), primaryRing: fraction(percent), managedMembers: managedRemainder))
        }
        if displayed.isEmpty && ranked.isEmpty && managedRemainder.isEmpty {
            result.append(StripItem(id: "empty", caption: "暂无项目", value: "—", title: "暂无项目",
                detailValue: "—", unit: "项目", footnote: "本周暂无有用量任务", avatar: .symbol("folder")))
        }
        return result
    }

}

// Detail content is data first, so each entry's approved fields can be verified
// independently of rendering and paginated without a card-internal scroll view.
struct StripDetailSection: Identifiable {
    enum Kind { case row, status, statement, progress, manual, artifact }
    let id: String
    let kind: Kind
    let label: String
    let value: String
    var percent: Double?
    var url: String?

    static func content(for item: StripItem, locked: Bool, page: StripDashboardPage) -> [Self] {
        func management(_ label: String) -> String { item.managementFacts.first { $0.label == label }?.value ?? "—" }
        func record(_ label: String) -> String { item.facts.first { $0.label == label }?.value ?? "—" }
        func row(_ id: String, _ label: String, _ value: String) -> Self { Self(id: id, kind: .row, label: label, value: value) }
        func statement(_ id: String, _ label: String, _ value: String) -> Self { Self(id: id, kind: .statement, label: label, value: value) }
        let status = Self(id: "status", kind: .status, label: "项目状态", value: item.projectStatus ?? "未设置")
        let progress = Self(id: "progress", kind: .progress, label: "完成进度",
                            value: item.developmentProgress.map { String(format: "%.0f%%", $0) } ?? "—", percent: item.developmentProgress)
        let share = row("share", "本周占比", item.value)
        if item.isProject {
            if !locked { return [status, statement("next", "下一步行动", management("下一步")), progress, share] }
            switch page {
            case .overview: return [status, statement("vision", "项目愿景", management("项目愿景")), progress, share]
            case .actions:
                var result = [statement("goal", "当前目标", management("当前目标")),
                    Self(id: "manual", kind: .manual, label: "人工待办",
                         value: [management("人工待办"), management("人工受阻"), management("人工已完成")].joined(separator: "|")),
                    statement("latest", "进度记录", management("最新产出与进度记录"))]
                if let title = item.latestOutputTitle, !title.isEmpty, let url = item.latestOutputURL,
                   safeOutputURL(url) != nil {
                    result.append(Self(id: "output", kind: .artifact, label: "最新产出", value: title, url: url))
                }
                return result
            case .investment:
                return [row("week-tokens", "本周 Token", item.detailValue),
                        row("today-tokens", "今日 Token", item.todayTokens.map(compactTokens) ?? "—"), share,
                        row("cost", "估算投入", item.estimatedCost)]
            }
        }
        if item.id == "quota" {
            return [row("remaining", "周额度剩余", item.detailValue), row("used", "本周已用额度", record("本周已用额度")),
                    row("week-tokens", "本周 Token", record("本周 Token")), row("cost", "估算投入", item.estimatedCost)]
        }
        if item.id == "total" {
            return [row("week-tokens", "本周 Token", item.detailValue), row("tasks", "任务数量", record("任务数量")),
                    row("time", "统计时间", record("统计时间")), row("responses", "模型响应次数", record("模型响应次数"))]
        }
        if item.id == "remainder" {
            return [row("share", "本周占比", item.value), row("week-tokens", "本周 Token", item.detailValue)] + item.members.map { member in
                if let url = member.notion_url, safeOutputURL(url) != nil {
                    return Self(id: member.id, kind: .artifact, label: "项目", value: (member.display_name ?? member.name) + " · " + String(format: "%.1f%%", member.percent), url: url)
                }
                return row(member.id, member.display_name ?? member.name, String(format: "%.1f%%", member.percent))
            } + item.managedMembers.map { member in
                if let url = member.notion_url, safeOutputURL(url) != nil {
                    return Self(id: member.id, kind: .artifact, label: "项目", value: member.display_name ?? member.name, url: url)
                }
                return row(member.id, member.display_name ?? member.name, member.project_health ?? member.project_status ?? "未设置")
            }
        }
        return [statement("empty", "暂无项目", item.footnote)]
    }

    static func safeOutputURL(_ value: String) -> URL? {
        if let url = StripAvatar.safeRemoteURL(value) { return url }
        // Formal local outputs are documents, not executables or arbitrary URL schemes.
        let url = value.hasPrefix("/") ? URL(fileURLWithPath: value) : URL(string: value)
        guard let url, url.isFileURL, url.host == nil || url.host == "" || url.host == "localhost",
              ["md", "txt", "pdf", "html", "docx", "xlsx", "pptx", "png", "jpg", "jpeg", "svg"].contains(url.pathExtension.lowercased()) else { return nil }
        return url
    }

    var textValue: Bool {
        id == "time" || value.rangeOfCharacter(from: .decimalDigits) == nil
    }
    var valueSize: CGFloat { textValue ? StripDetailMetrics.bodySize : StripDetailMetrics.titleSize }
    var valueWeight: NSFont.Weight { textValue ? .regular : .semibold }
    var countUnit: String? {
        ["个", "次", "项"].first { value.hasSuffix(" " + $0) }
    }
    func labelWidth(width: CGFloat) -> CGFloat {
        min(width * 0.5, ceil((label as NSString).size(withAttributes: [.font: NSFont.systemFont(ofSize: StripDetailMetrics.labelSize)]).width))
    }
    static func textHeight(_ text: String, width: CGFloat, size: CGFloat = StripDetailMetrics.bodySize,
                           weight: NSFont.Weight = .regular, lineHeight: CGFloat = StripDetailMetrics.bodyLine) -> CGFloat {
        let paragraph = NSMutableParagraphStyle()
        paragraph.minimumLineHeight = lineHeight
        paragraph.maximumLineHeight = lineHeight
        paragraph.lineBreakMode = .byWordWrapping
        let bounds = (text as NSString).boundingRect(with: CGSize(width: max(20, width), height: .greatestFiniteMagnitude),
            options: [.usesLineFragmentOrigin, .usesFontLeading], attributes: [
                .font: NSFont.systemFont(ofSize: size, weight: weight), .paragraphStyle: paragraph])
        return max(lineHeight, ceil(bounds.height))
    }
    func height(width: CGFloat) -> CGFloat {
        switch kind {
        case .row: return max(StripDetailMetrics.bodyLine,
                              Self.textHeight(label, width: labelWidth(width: width), size: StripDetailMetrics.labelSize, lineHeight: StripDetailMetrics.labelLine),
                              Self.textHeight(value, width: width - labelWidth(width: width) - StripDetailMetrics.sectionGap,
                                              size: valueSize, weight: valueWeight))
        case .status: return max(24, Self.textHeight(value, width: width * 0.60 - 22, size: StripDetailMetrics.labelSize,
                                                   weight: .medium, lineHeight: StripDetailMetrics.labelLine) + 6)
        case .progress: return 32
        case .manual: return 46
        case .artifact: return StripDetailMetrics.labelLine + StripDetailMetrics.fieldGap + Self.textHeight(value, width: width - 26)
        case .statement: return StripDetailMetrics.labelLine + StripDetailMetrics.fieldGap + Self.textHeight(value, width: width)
        }
    }

    static func pages(_ sections: [Self], width: CGFloat, height: CGFloat, metrics: Bool = false) -> [[Self]] {
        let budget = max(1, height)
        let fragmentBudget = max(1, budget - (metrics ? 2 : 0))
        var expanded: [Self] = []
        for original in sections {
            var section = original
            if section.kind == .manual && section.height(width: width) > budget {
                let values = section.value.components(separatedBy: "|")
                for (index, label) in ["人工待办", "人工受阻", "人工已完成"].enumerated() {
                    expanded.append(Self(id: section.id + ":\(index)", kind: .row, label: label,
                                         value: values.indices.contains(index) ? values[index] : "—"))
                }
                continue
            }
            if [.row, .status].contains(section.kind) && section.height(width: width) > budget {
                section = Self(id: section.id, kind: .statement, label: section.kind == .status ? section.label : "任务用量",
                               value: section.kind == .status ? section.value : section.label + " · " + section.value)
            }
            // Every variable-length text is split, including output buttons and
            // long names in Other; no page relies on clipped text or a tooltip.
            if [.statement, .artifact].contains(section.kind) && section.height(width: width) > fragmentBudget {
                let characters = Array(section.value)
                var start = 0
                var part = 0
                while start < characters.count {
                    var low = start + 1, high = characters.count, end = start + 1
                    while low <= high {
                        let middle = (low + high) / 2
                        let probe = Self(id: section.id, kind: section.kind, label: section.label,
                                         value: String(characters[start..<middle]), url: section.url)
                        if probe.height(width: width) <= fragmentBudget { end = middle; low = middle + 1 }
                        else { high = middle - 1 }
                    }
                    expanded.append(Self(id: section.id + ":\(part)", kind: section.kind,
                        label: section.label + (part == 0 ? "" : "（续）"), value: String(characters[start..<end]), url: section.url))
                    start = end; part += 1
                }
            } else { expanded.append(section) }
        }
        var pages: [[Self]] = [], current: [Self] = []
        for section in expanded {
            let candidate = current + [section]
            if !current.isEmpty && StripDetailLayout.contentHeight(candidate, width: width, metrics: metrics) > budget {
                pages.append(current); current = []
            }
            current.append(section)
        }
        if !current.isEmpty { pages.append(current) }
        return pages.isEmpty ? [[]] : pages
    }
}

// Approved HTML review: the same measurements drive drawing, pagination and
// the desktop panel's pointer hit regions. Short screens never change fonts.
enum StripDetailMetrics {
    static let labelSize: CGFloat = 12
    static let bodySize: CGFloat = 14
    static let titleSize: CGFloat = 16
    static let labelLine: CGFloat = 18
    static let bodyLine: CGFloat = 22
    static let padding: CGFloat = 20
    static let sectionGap: CGFloat = 16
    static let fieldGap: CGFloat = 6
    static let maxHeight: CGFloat = 520
    static let tabsHeight: CGFloat = 42
    static let navigationHeight: CGFloat = 57
    static let footerHeight: CGFloat = 51
}

struct StripDetailLayout {
    let pages: [[StripDetailSection]]
    let selectedPage: Int
    let height: CGFloat
    let maximumPageHeight: CGFloat
    let contentWidth: CGFloat
    let headerHeight: CGFloat
    let hasTabs: Bool
    let showsFooter: Bool
    let metrics: Bool
    var visibleSections: [StripDetailSection] { pages[selectedPage] }

    static func contentHeight(_ sections: [StripDetailSection], width: CGFloat, metrics: Bool) -> CGFloat {
        guard !sections.isEmpty else { return 0 }
        let total = sections.reduce(CGFloat(0)) { $0 + $1.height(width: width) }
        return total + (metrics ? 2 + CGFloat(sections.count - 1) * 19 : CGFloat(sections.count - 1) * StripDetailMetrics.sectionGap)
    }

    static func make(item: StripItem, locked: Bool, page: StripDashboardPage, sectionPage: Int = 0,
                     width: CGFloat = 360, maxHeight: CGFloat = StripDetailMetrics.maxHeight) -> Self {
        let contentWidth = max(20, width - 2 * StripDetailMetrics.padding)
        let hasTabs = item.isProject && locked
        let showsFooter = item.isProject && locked
        let headerHeight = max(30, StripDetailSection.textHeight(item.title, width: contentWidth - 80,
            size: StripDetailMetrics.titleSize, weight: .semibold))
        let base = 2 * StripDetailMetrics.padding + headerHeight + StripDetailMetrics.sectionGap
            + (hasTabs ? StripDetailMetrics.tabsHeight + StripDetailMetrics.sectionGap : 0)
            + (showsFooter ? StripDetailMetrics.footerHeight : 0)
        let sections = StripDetailSection.content(for: item, locked: locked, page: page)
        let metrics = !sections.isEmpty && sections.allSatisfy { $0.kind == .row }
        let completeHeight = contentHeight(sections, width: contentWidth, metrics: metrics)
        let pages = base + completeHeight <= maxHeight ? [sections] : StripDetailSection.pages(sections, width: contentWidth,
            height: maxHeight - base - StripDetailMetrics.navigationHeight, metrics: metrics)
        let selected = min(max(0, sectionPage), pages.count - 1)
        return Self(pages: pages, selectedPage: selected,
            height: base + contentHeight(pages[selected], width: contentWidth, metrics: metrics)
                + (pages.count > 1 ? StripDetailMetrics.navigationHeight : 0),
            maximumPageHeight: base + (pages.map { contentHeight($0, width: contentWidth, metrics: metrics) }.max() ?? 0)
                + (pages.count > 1 ? StripDetailMetrics.navigationHeight : 0),
            contentWidth: contentWidth, headerHeight: headerHeight, hasTabs: hasTabs, showsFooter: showsFooter, metrics: metrics)
    }
}

enum StripDashboardPage: String, CaseIterable, Identifiable {
    case overview, actions, investment
    var id: String { rawValue }
    func title(for item: StripItem) -> String {
        switch self {
        case .overview: return "概览"
        case .actions: return item.id == "quota" ? "额度" : item.isProject ? "行动" : "记录"
        case .investment: return "投入"
        }
    }
}

@MainActor
final class StripInteraction: ObservableObject {
    @Published var expanded: String?
    @Published var locked = false
    @Published var dashboardPage: StripDashboardPage = .overview
    @Published var quotaMetric: StripQuotaMetric = .weeklyRemaining
    @Published var sectionPage = 0
    private(set) var isDragging = false
    private var hovered: String?
    private var zones: [String: (id: String, sequence: Int)] = [:]
    private var sequence = 0
    private var pending: DispatchWorkItem?
    var onResize: (() -> Void)?

    func hover(_ id: String, inside: Bool, zone: String = "pointer") {
        guard !isDragging else { return }
        if inside {
            sequence += 1
            zones[zone] = (id, sequence)
        } else if zones[zone]?.id == id { zones.removeValue(forKey: zone) }
        hovered = zones.values.max(by: { $0.sequence < $1.sequence })?.id
        guard !locked else { return }
        pending?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            if self.expanded != self.hovered { self.dashboardPage = .overview; self.sectionPage = 0 }
            self.expanded = self.hovered
            self.onResize?()
        }
        pending = work
        DispatchQueue.main.asyncAfter(deadline: .now() + (inside ? 0.18 : 0.24), execute: work)
    }

    func click(_ id: String) {
        guard !isDragging else { return }
        pending?.cancel()
        if locked && expanded == id { dismiss() }
        else {
            if expanded != id || !locked { dashboardPage = .overview; sectionPage = 0 }
            expanded = id; locked = true; onResize?()
        }
    }

    func selectPage(_ page: StripDashboardPage) {
        guard expanded != nil, locked else { return }
        dashboardPage = page
        sectionPage = 0
        onResize?()
    }

    func beginDragging() {
        isDragging = true
        dismiss()
    }

    func endDragging() { isDragging = false }

    func selectSectionPage(_ page: Int) {
        sectionPage = max(0, page)
        onResize?()
    }

    func cycleQuota(steps: Int = 1) {
        quotaMetric = .weeklyRemaining
        onResize?()
    }

    func leaveAllHoverZones() {
        // A desktop panel stops receiving mouse events in its transparent
        // overlay area. Native pointer sampling must clear stale SwiftUI zones.
        hovered = nil
        zones.removeAll()
        guard !locked else { return }
        pending?.cancel()
        let work = DispatchWorkItem { [weak self] in
            self?.expanded = nil
            self?.onResize?()
        }
        pending = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.24, execute: work)
    }

    func dismiss() {
        pending?.cancel()
        hovered = nil
        zones.removeAll()
        locked = false
        dashboardPage = .overview
        sectionPage = 0
        expanded = nil
        onResize?()
    }
}


// Real OpenAI Blossom, unchanged shape. Source and ownership: THIRD_PARTY_NOTICES.md.
private enum StripBrandAsset {
    static let openAI = NSImage(data: Data(base64Encoded: "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")!)!
}

enum StripInk {
    static let primary = Color(red: 0.067, green: 0.090, blue: 0.071)
    static let secondary = Color(red: 0.282, green: 0.341, blue: 0.306)
    static let avatar = Color(red: 0.86, green: 0.89, blue: 0.85)
}

struct SystemGlass: ViewModifier {
    @Environment(\.accessibilityReduceTransparency) var reduceTransparency
    @Environment(\.colorSchemeContrast) var contrast
    let active: Bool
    let radius: CGFloat
    var style: WidgetStyle = .glass

    @ViewBuilder func body(content: Content) -> some View {
        let shape = RoundedRectangle(cornerRadius: radius, style: .continuous)
        if style == .pulse {
            content.background((active ? Color(red: 0.098, green: 0.106, blue: 0.125) : Color(red: 0.085, green: 0.095, blue: 0.11))
                .opacity(reduceTransparency ? 1 : 0.98), in: shape)
                .overlay(shape.strokeBorder(Color.white.opacity(active ? 0.17 : 0.10), lineWidth: 0.7).allowsHitTesting(false))
        } else if style == .white || reduceTransparency || contrast == .increased {
            content.background(style == .white ? Color.white : Color(red: 0.98, green: 0.99, blue: 0.97), in: shape)
                .overlay(shape.strokeBorder(StripInk.primary.opacity(active ? 0.14 : 0.09), lineWidth: 0.7).allowsHitTesting(false))
        } else if #available(macOS 26.0, *) {
            content.background {
                shape.fill(Color.white.opacity(active ? 0.88 : 0.80))
                    .glassEffect(.regular.tint(Color.white.opacity(0.08)), in: .rect(cornerRadius: radius))
            }.overlay(shape.strokeBorder(Color.white.opacity(0.55), lineWidth: 0.7).allowsHitTesting(false))
        } else {
            content.background {
                shape.fill(.ultraThinMaterial)
                    .overlay(shape.fill(Color.white.opacity(active ? 0.80 : 0.72)).allowsHitTesting(false))
            }.overlay(shape.strokeBorder(Color.white.opacity(0.65), lineWidth: 0.7).allowsHitTesting(false))
        }
    }
}

private struct DashboardRing: View {
    let fraction: Double?
    let tint: Color
    let track: Color
    let size: CGFloat
    let line: CGFloat
    var stale = false
    var decorative = false
    var body: some View {
        ZStack {
            Circle().stroke(track, style: StrokeStyle(lineWidth: line, lineCap: .round,
                dash: fraction == nil && !decorative ? [2, 4] : []))
            if let fraction, fraction > 0 {
                Circle().trim(from: 0, to: min(1, max(0, fraction)))
                    .stroke(tint.opacity(stale ? 0.55 : 1), style: StrokeStyle(lineWidth: line, lineCap: .round))
                    .rotationEffect(.degrees(-90))
            }
        }.frame(width: size, height: size).accessibilityHidden(true)
    }
}

enum StripHitTarget: Equatable {
    case module(Int), background, detail, connector, outside
    var acceptsMouse: Bool { self != .outside }
}

struct UsageStrip: View {
    static let moduleWidth: CGFloat = 68
    static let headerHeight: CGFloat = 64
    static let diameter: CGFloat = 64
    // The outer progress ring is 51pt with a 3pt stroke. Its visible outside
    // edge, rather than the surrounding 64/68pt layout slot, is interactive.
    static let interactionDiameter: CGFloat = 54
    static let gap: CGFloat = 0
    static let inset: CGFloat = 3
    static let rowInset: CGFloat = 2
    static let detailWidth: CGFloat = 360
    static let detailGap: CGFloat = 10
    static let detailRadius: CGFloat = 20
    static let maxDetailHeight: CGFloat = StripDetailMetrics.maxHeight
    static func naturalSize(itemCount: Int, vertical: Bool = false) -> CGSize {
        CGSize(width: vertical ? diameter + 2 * inset : CGFloat(itemCount) * moduleWidth + 2 * inset,
               height: vertical ? CGFloat(itemCount) * (diameter + 2 * rowInset) + 2 * inset : headerHeight + 2 * inset)
    }
    static func canvasSize(itemCount: Int, vertical: Bool = false) -> CGSize {
        let natural = naturalSize(itemCount: itemCount, vertical: vertical)
        return CGSize(width: vertical ? natural.width + detailWidth + detailGap : max(natural.width, detailWidth + 2 * inset),
                      height: vertical ? max(natural.height, maxDetailHeight + 2 * inset) : natural.height + detailGap + maxDetailHeight)
    }
    static func placement(anchor: CGPoint, size: CGSize, headerHeight: CGFloat, bounds: CGRect) -> (frame: CGRect, headerOffset: CGFloat) {
        let anchorTop = min(bounds.maxY, max(anchor.y, bounds.minY + headerHeight))
        let top = min(bounds.maxY, max(anchorTop, bounds.minY + size.height))
        let x = max(bounds.minX, min(anchor.x, bounds.maxX - size.width))
        return (CGRect(x: x, y: top - size.height, width: size.width, height: size.height), top - anchorTop)
    }
    static func headerFrame(itemCount: Int, vertical: Bool, canvas: CGSize, headerOffset: CGFloat = 0) -> CGRect {
        let natural = naturalSize(itemCount: itemCount, vertical: vertical)
        return CGRect(x: vertical ? canvas.width - natural.width : (canvas.width - natural.width) / 2,
                      y: headerOffset, width: natural.width, height: min(natural.height, max(0, canvas.height - headerOffset)))
    }
    static func moduleCircleFrame(index: Int, itemCount: Int, vertical: Bool, canvas: CGSize, headerOffset: CGFloat = 0) -> CGRect {
        let header = headerFrame(itemCount: itemCount, vertical: vertical, canvas: canvas, headerOffset: headerOffset)
        let center = CGPoint(
            x: header.minX + inset + (vertical ? diameter / 2 : CGFloat(index) * moduleWidth + moduleWidth / 2),
            y: header.minY + inset + (vertical ? CGFloat(index) * (diameter + 2 * rowInset) : 0) + diameter / 2)
        return CGRect(x: center.x - interactionDiameter / 2, y: center.y - interactionDiameter / 2,
                      width: interactionDiameter, height: interactionDiameter)
    }
    static func hitTarget(at point: CGPoint, itemCount: Int, vertical: Bool, canvas: CGSize, headerOffset: CGFloat = 0,
                          detail: CGRect? = nil, detailIndex: Int? = nil) -> StripHitTarget {
        if let detail, pointInRoundedRectangle(point, rect: detail, radius: detailRadius) { return .detail }
        let header = headerFrame(itemCount: itemCount, vertical: vertical, canvas: canvas, headerOffset: headerOffset)
        if pointInCapsule(point, rect: header) {
            for index in 0..<itemCount {
                let circle = moduleCircleFrame(index: index, itemCount: itemCount, vertical: vertical, canvas: canvas, headerOffset: headerOffset)
                if pointInCapsule(point, rect: circle) { return .module(index) }
            }
            return .background
        }
        if let detail, let detailIndex {
            let bridge = connectorFrame(index: detailIndex, itemCount: itemCount, vertical: vertical, canvas: canvas,
                                        detail: detail, headerOffset: headerOffset)
            if bridge.contains(point) { return .connector }
        }
        return .outside
    }
    static func detailHeight(for item: StripItem) -> CGFloat {
        StripDetailLayout.make(item: item, locked: true, page: .overview).height
    }
    static func needsPagination(availableHeight: CGFloat, locked: Bool) -> Bool {
        availableHeight < (locked ? 405 : 305)
    }
    static func detailFrame(for item: StripItem, index: Int, itemCount: Int, vertical: Bool, canvas: CGSize,
                            headerOffset: CGFloat = 0, locked: Bool = true, page: StripDashboardPage = .overview, sectionPage: Int = 0) -> CGRect {
        detailPlacement(for: item, index: index, itemCount: itemCount, vertical: vertical, canvas: canvas,
                        headerOffset: headerOffset, locked: locked, page: page, sectionPage: sectionPage).frame
    }
    static func detailPlacement(for item: StripItem, index: Int, itemCount: Int, vertical: Bool, canvas: CGSize,
                                headerOffset: CGFloat = 0, locked: Bool = true, page: StripDashboardPage = .overview,
                                sectionPage: Int = 0) -> (frame: CGRect, layout: StripDetailLayout) {
        let header = headerFrame(itemCount: itemCount, vertical: vertical, canvas: canvas, headerOffset: headerOffset)
        let width = min(detailWidth, max(0, canvas.width - 2 * inset))
        let natural = StripDetailLayout.make(item: item, locked: locked, page: page, width: width)
        // Keep one direction and one pagination budget for the whole page set.
        // A shorter last page must not flip sides and re-split the same text.
        let desiredHeight = natural.maximumPageHeight
        let below = max(0, canvas.height - header.maxY - detailGap - inset)
        let above = max(0, header.minY - detailGap - inset)
        let opensBelow = below >= desiredHeight || below >= above
        // If the rail leaves too little room for even one readable text page,
        // let the card cover the rail, as in the short-window HTML review.
        let minimum = 2 * StripDetailMetrics.padding + natural.headerHeight + StripDetailMetrics.sectionGap
            + (natural.hasTabs ? StripDetailMetrics.tabsHeight + StripDetailMetrics.sectionGap : 0)
            + (natural.showsFooter ? StripDetailMetrics.footerHeight : 0)
            + StripDetailMetrics.navigationHeight + 46
        let overlapsRail = !vertical && max(below, above) < min(desiredHeight, minimum)
        let available = vertical || overlapsRail ? canvas.height - 2 * inset : (opensBelow ? below : above)
        let layout = StripDetailLayout.make(item: item, locked: locked, page: page, sectionPage: sectionPage,
                                           width: width, maxHeight: min(maxDetailHeight, max(0, available)))
        let height = layout.height
        let desiredX = header.minX + inset + CGFloat(index) * moduleWidth + moduleWidth / 2 - width / 2
        let x = vertical ? inset : min(max(inset, desiredX), max(inset, canvas.width - width - inset))
        let desiredY = vertical ? header.minY + inset + CGFloat(index) * (diameter + 2 * rowInset) :
            overlapsRail ? inset : (opensBelow ? header.maxY + detailGap : header.minY - detailGap - height)
        let y = min(max(inset, desiredY), max(inset, canvas.height - height - inset))
        return (CGRect(x: x, y: y, width: width, height: height), layout)
    }
    static func connectorFrame(index: Int, itemCount: Int, vertical: Bool, canvas: CGSize, detail: CGRect, headerOffset: CGFloat = 0) -> CGRect {
        let header = headerFrame(itemCount: itemCount, vertical: vertical, canvas: canvas, headerOffset: headerOffset)
        if vertical {
            let y = header.minY + inset + CGFloat(index) * (diameter + 2 * rowInset) + diameter / 2
            return CGRect(x: detail.maxX, y: y - 9, width: max(0, header.minX + inset - detail.maxX), height: 18)
        }
        let x = header.minX + inset + CGFloat(index) * moduleWidth + moduleWidth / 2
        if detail.maxY <= header.minY {
            return CGRect(x: x - 9, y: detail.maxY, width: 18, height: header.minY + inset - detail.maxY)
        }
        return CGRect(x: x - 9, y: header.maxY - inset, width: 18, height: max(0, detail.minY - header.maxY + inset))
    }
    @ObservedObject var model: UsageModel
    @ObservedObject var interaction: StripInteraction
    @Environment(\.accessibilityReduceMotion) var reduceMotion
    let vertical: Bool
    let height: CGFloat
    var canvasWidth: CGFloat = 0
    var headerOffset: CGFloat = 0
    var items: [StripItem] { StripItem.items(model.snapshot, failed: model.error != nil, metric: interaction.quotaMetric, managementEnabled: model.notionEnabled) }
    var width: CGFloat { canvasWidth > 0 ? canvasWidth : Self.canvasSize(itemCount: items.count, vertical: vertical).width }
    var canvas: CGSize { CGSize(width: width, height: height) }
    var style: WidgetStyle { model.widgetStyle }

    var body: some View {
        let header = Self.headerFrame(itemCount: items.count, vertical: vertical, canvas: canvas, headerOffset: headerOffset)
        ZStack(alignment: .topLeading) {
            Color.clear.frame(width: header.width, height: header.height)
                .modifier(SystemGlass(active: false, radius: Self.headerHeight / 2 + Self.inset, style: style))
                .shadow(color: .black.opacity(style == .pulse ? 0.24 : 0.10), radius: 14, y: 6)
                .offset(x: header.minX, y: header.minY).allowsHitTesting(false)
            if let item = items.first(where: { $0.id == interaction.expanded }),
               let index = items.firstIndex(where: { $0.id == item.id }) {
                let placement = Self.detailPlacement(for: item, index: index, itemCount: items.count, vertical: vertical, canvas: canvas,
                    headerOffset: headerOffset, locked: interaction.locked, page: interaction.dashboardPage, sectionPage: interaction.sectionPage)
                let card = placement.frame
                let bridge = Self.connectorFrame(index: index, itemCount: items.count, vertical: vertical, canvas: canvas, detail: card, headerOffset: headerOffset)
                Color.clear.frame(width: bridge.width, height: bridge.height).contentShape(Rectangle())
                    .offset(x: bridge.minX, y: bridge.minY)
                details(item, layout: placement.layout).frame(width: card.width, height: card.height, alignment: .top)
                    .modifier(SystemGlass(active: true, radius: Self.detailRadius, style: style))
                    .clipShape(RoundedRectangle(cornerRadius: Self.detailRadius, style: .continuous))
                    .shadow(color: .black.opacity(style == .pulse ? 0.28 : 0.14), radius: 20, y: 9)
                    .offset(x: card.minX, y: card.minY).zIndex(card.intersects(header) ? 30 : 10)
                    .transition(.opacity.combined(with: .scale(scale: 0.97, anchor: vertical ? .trailing : .top)))
            }
            rail.frame(width: header.width, height: header.height, alignment: .top)
                .offset(x: header.minX, y: header.minY).zIndex(20)
        }.frame(width: width, height: height, alignment: .topLeading)
            .foregroundStyle(style.primary)
            .animation(reduceMotion ? nil : .smooth(duration: 0.22), value: interaction.expanded)
            .contextMenu {
                Button("项目与 Notion 设置…") { model.openSettings() }
                Menu("外观") {
                    ForEach(WidgetStyle.allCases) { theme in
                        Button { model.setWidgetStyle(theme) } label: {
                            if theme == style { Label(theme.title, systemImage: "checkmark") } else { Text(theme.title) }
                        }
                    }
                }
                Divider()
                ForEach(WidgetKind.allCases, id: \.self) { kind in
                    Toggle(kind.title, isOn: Binding(get: { model.enabledWidgets.contains(kind) }, set: { _ in model.toggleWidget(kind) }))
                }
                Button("刷新") { model.refresh() }
            }
            .onChange(of: items.map(\.id)) { _, ids in
                if let selected = interaction.expanded, !ids.contains(selected) { interaction.dismiss() }
            }.onExitCommand { interaction.dismiss() }
    }

    @ViewBuilder private var rail: some View {
        if vertical {
            VStack(spacing: 2 * Self.rowInset) {
                    ForEach(items) { item in circle(item, foreground: interaction.expanded == item.id) }
            }.padding(Self.inset)
        } else {
            HStack(spacing: 0) {
                ForEach(items) { item in circle(item, foreground: interaction.expanded == item.id)
                    .frame(width: Self.moduleWidth) }
            }.padding(Self.inset)
        }
    }

    @ViewBuilder private func avatar(_ item: StripItem) -> some View {
        switch item.avatar {
        case .openAI:
            Image(nsImage: StripBrandAsset.openAI).resizable().interpolation(.high).scaledToFit()
                .colorInvertIf(style == .pulse).frame(width: 24, height: 24).accessibilityHidden(true)
        case .symbol(let name):
            Image(systemName: name).font(.system(size: 18, weight: .regular)).frame(width: 22, height: 22).accessibilityHidden(true)
        case .emoji(let value):
            Text(value).font(.system(size: 22)).frame(width: 26, height: 26).accessibilityHidden(true)
        case .remote(let value):
            ProjectAvatarImage(value: value).frame(width: 26, height: 26).clipShape(RoundedRectangle(cornerRadius: 8)).accessibilityHidden(true)
        }
    }

    func circle(_ item: StripItem, foreground: Bool) -> some View {
        Button { interaction.click(item.id) } label: {
            ZStack {
                DashboardRing(fraction: item.primaryRing, tint: style.accent, track: style.track, size: 51, line: 3,
                              stale: item.ringStale, decorative: item.id != "quota" && !item.isProject)
                if item.isProject {
                    DashboardRing(fraction: item.secondaryRing, tint: style.progress, track: style.track, size: 41, line: 2,
                                  stale: item.notionStale)
                }
                avatar(item)
            }.frame(width: Self.interactionDiameter, height: Self.interactionDiameter)
                .background(Circle().fill(style.primary.opacity(foreground ? 0.045 : 0)))
                .shadow(color: style.accent.opacity(foreground ? 0.14 : 0), radius: 8)
                .contentShape(Circle())
        }.buttonStyle(.plain).foregroundStyle(style.primary)
            .contentShape(.interaction, Circle())
            .accessibilityLabel(item.isProject ? "\(item.title)，\(item.projectStatus ?? "未设置状态")，外环本周 Token 占比，内环\(item.progressLabel)，点击查看项目看板" : "\(item.title)，\(item.caption) \(item.value)")
            .help("\(item.title) · 悬停查看，点击保持")
            .contextMenu {
                if item.isProject {
                    if let url = item.notionURL { Button("打开 Notion 项目") { model.openProject(url) } }
                    Button("项目设置…") { model.openSettings() }
                }
            }
            .frame(width: Self.diameter, height: Self.diameter)
    }

    @ViewBuilder func details(_ item: StripItem, layout: StripDetailLayout) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .center, spacing: 10) {
                avatar(item).frame(width: 30, height: 30)
                detailText(item.title, size: StripDetailMetrics.titleSize, weight: .semibold)
                    .frame(maxWidth: .infinity, alignment: .leading)
                Button { interaction.dismiss() } label: {
                    Image(systemName: "xmark").font(.system(size: 14, weight: .regular))
                        .frame(width: 28, height: 28).contentShape(Rectangle())
                }.buttonStyle(.plain).foregroundStyle(style.secondary).accessibilityLabel("收起详情")
            }.frame(height: layout.headerHeight).padding(.bottom, StripDetailMetrics.sectionGap)
            if layout.hasTabs {
                HStack(spacing: 3) {
                    ForEach(StripDashboardPage.allCases) { page in
                        Button { interaction.selectPage(page) } label: {
                            Text(page.title(for: item)).font(.system(size: StripDetailMetrics.bodySize,
                                weight: interaction.dashboardPage == page ? .medium : .regular))
                                .frame(maxWidth: .infinity, minHeight: 36)
                                .background(interaction.dashboardPage == page ? (style == .pulse ? Color.white.opacity(0.10) : Color.white.opacity(0.88)) : Color.clear,
                                            in: RoundedRectangle(cornerRadius: 8))
                                .contentShape(Rectangle())
                        }.buttonStyle(.plain)
                            .foregroundStyle(interaction.dashboardPage == page ? style.primary : style.secondary)
                            .accessibilityAddTraits(interaction.dashboardPage == page ? .isSelected : [])
                    }
                }.padding(3).frame(height: StripDetailMetrics.tabsHeight)
                    .background(style.track, in: RoundedRectangle(cornerRadius: 10))
                    .padding(.bottom, StripDetailMetrics.sectionGap)
            }
            VStack(alignment: .leading, spacing: layout.metrics ? 0 : StripDetailMetrics.sectionGap) {
                ForEach(Array(layout.visibleSections.enumerated()), id: \.element.id) { index, section in
                    if layout.metrics && index > 0 { Rectangle().fill(style.track).frame(height: 1) }
                    sectionView(section, width: layout.contentWidth)
                        .frame(height: section.height(width: layout.contentWidth), alignment: .top)
                        .padding(.top, layout.metrics ? (index == 0 ? 1 : 9) : 0)
                        .padding(.bottom, layout.metrics ? (index == layout.visibleSections.count - 1 ? 1 : 9) : 0)
                }
            }
            if layout.pages.count > 1 {
                VStack(spacing: 12) {
                    Rectangle().fill(style.track).frame(height: 1)
                    HStack {
                        Text("第 \(layout.selectedPage + 1) / \(layout.pages.count) 页")
                            .font(.system(size: StripDetailMetrics.labelSize)).foregroundStyle(style.secondary)
                        Spacer()
                        HStack(spacing: 6) {
                            Button { interaction.selectSectionPage(layout.selectedPage - 1) } label: {
                                Image(systemName: "chevron.left").font(.system(size: 12))
                                    .frame(width: 30, height: 28).contentShape(Rectangle())
                            }.buttonStyle(.plain).disabled(layout.selectedPage == 0).accessibilityLabel("上一页")
                            Button { interaction.selectSectionPage(layout.selectedPage + 1) } label: {
                                Image(systemName: "chevron.right").font(.system(size: 12))
                                    .frame(width: 30, height: 28).contentShape(Rectangle())
                            }.buttonStyle(.plain).disabled(layout.selectedPage == layout.pages.count - 1).accessibilityLabel("下一页")
                        }.background(style.track, in: RoundedRectangle(cornerRadius: 7))
                    }.frame(height: 28)
                }.padding(.top, StripDetailMetrics.sectionGap)
            }
            if layout.showsFooter {
                VStack(spacing: 12) {
                    Rectangle().fill(style.track).frame(height: 1)
                    Button { if let url = item.notionURL { model.openProject(url) } else { model.openSettings() } } label: {
                        HStack {
                            Text(item.notionURL == nil ? "关联 Notion 项目" : "在 Notion 查看项目")
                            Spacer()
                            Image(systemName: "arrow.up.right")
                        }.font(.system(size: StripDetailMetrics.bodySize))
                            .frame(maxWidth: .infinity, minHeight: StripDetailMetrics.bodyLine, alignment: .leading)
                            .contentShape(Rectangle())
                    }.buttonStyle(.plain).foregroundStyle(style.secondary)
                }.padding(.top, StripDetailMetrics.sectionGap)
            }
        }.padding(StripDetailMetrics.padding).frame(maxWidth: .infinity, alignment: .topLeading)
            .foregroundStyle(style.primary)
    }

    private func detailText(_ value: String, size: CGFloat = StripDetailMetrics.bodySize,
                            weight: Font.Weight = .regular, lineHeight: CGFloat = StripDetailMetrics.bodyLine) -> some View {
        Text(value).font(.system(size: size, weight: weight))
            .lineSpacing(max(0, lineHeight - ceil(NSLayoutManager().defaultLineHeight(for: NSFont.systemFont(ofSize: size)))))
            .fixedSize(horizontal: false, vertical: true)
    }

    @ViewBuilder private func sectionView(_ section: StripDetailSection, width: CGFloat) -> some View {
        switch section.kind {
        case .row:
            HStack(alignment: .center, spacing: StripDetailMetrics.sectionGap) {
                detailText(section.label, size: StripDetailMetrics.labelSize, lineHeight: StripDetailMetrics.labelLine)
                    .foregroundStyle(style.secondary).frame(width: section.labelWidth(width: width), alignment: .leading)
                HStack(alignment: .firstTextBaseline, spacing: 4) {
                    detailText(section.countUnit == nil ? section.value : String(section.value.dropLast(2)),
                               size: section.valueSize, weight: section.textValue ? .regular : .semibold)
                        .monospacedDigit().multilineTextAlignment(.trailing)
                    if let unit = section.countUnit {
                        Text(unit).font(.system(size: StripDetailMetrics.labelSize)).foregroundStyle(style.secondary).fixedSize()
                    }
                }.frame(maxWidth: .infinity, alignment: .trailing)
                    .foregroundStyle(section.id == "remaining" ? style.accent : style.primary)
            }.frame(maxWidth: .infinity, minHeight: StripDetailMetrics.bodyLine)
        case .status:
            HStack {
                Text(section.label).font(.system(size: StripDetailMetrics.labelSize)).foregroundStyle(style.secondary)
                Spacer()
                HStack(spacing: 6) {
                    Circle().fill(statusColor(section.value)).frame(width: 5, height: 5)
                    detailText(section.value, size: StripDetailMetrics.labelSize, weight: .medium, lineHeight: StripDetailMetrics.labelLine)
                }.foregroundStyle(statusColor(section.value))
                    .padding(.horizontal, 8).padding(.vertical, 3)
                    .background(statusColor(section.value).opacity(style == .pulse ? 0.14 : 0.10), in: RoundedRectangle(cornerRadius: 6))
            }.frame(minHeight: 24)
        case .statement:
            VStack(alignment: .leading, spacing: StripDetailMetrics.fieldGap) {
                Text(section.label).font(.system(size: StripDetailMetrics.labelSize)).foregroundStyle(style.secondary)
                    .frame(height: StripDetailMetrics.labelLine, alignment: .leading)
                detailText(section.value).frame(height: StripDetailSection.textHeight(section.value, width: width), alignment: .topLeading)
            }.frame(maxWidth: .infinity, alignment: .leading)
        case .progress:
            VStack(alignment: .leading, spacing: StripDetailMetrics.fieldGap) {
                HStack {
                    Text("完成进度").font(.system(size: StripDetailMetrics.labelSize)).foregroundStyle(style.secondary)
                    Spacer()
                    Text(section.percent == nil ? "待评估" : section.value)
                        .font(.system(size: section.percent == nil ? StripDetailMetrics.bodySize : StripDetailMetrics.titleSize,
                                      weight: section.percent == nil ? .regular : .semibold)).monospacedDigit()
                        .foregroundStyle(section.percent == nil ? style.secondary : style.primary)
                }.frame(height: StripDetailMetrics.bodyLine)
                Capsule().fill(style.track).frame(height: 4).overlay(alignment: .leading) {
                    if let percent = section.percent {
                        Capsule().fill(style.progress).frame(width: width * CGFloat(percent / 100), height: 4)
                    }
                }.accessibilityLabel("完成进度").accessibilityValue(section.value)
            }
        case .manual:
            VStack(alignment: .leading, spacing: StripDetailMetrics.fieldGap) {
                Text("人工待办").font(.system(size: StripDetailMetrics.labelSize)).foregroundStyle(style.secondary)
                    .frame(height: StripDetailMetrics.labelLine, alignment: .leading)
                let values = section.value.components(separatedBy: "|")
                HStack(spacing: 8) {
                    ForEach(Array(["待办", "受阻", "已完成"].enumerated()), id: \.offset) { index, label in
                        if index > 0 { Spacer(minLength: 0) }
                        HStack(alignment: .firstTextBaseline, spacing: 4) {
                            Text(label).font(.system(size: StripDetailMetrics.labelSize)).foregroundStyle(style.secondary)
                            let value = values.indices.contains(index) ? values[index].replacingOccurrences(of: "项", with: "").trimmingCharacters(in: .whitespaces) : "—"
                            Text(value).font(.system(size: StripDetailMetrics.titleSize, weight: .semibold)).monospacedDigit()
                            Text("项").font(.system(size: StripDetailMetrics.labelSize)).foregroundStyle(style.secondary)
                        }.fixedSize()
                    }
                }.frame(height: StripDetailMetrics.bodyLine)
            }
        case .artifact:
            VStack(alignment: .leading, spacing: StripDetailMetrics.fieldGap) {
                Text(section.label).font(.system(size: StripDetailMetrics.labelSize)).foregroundStyle(style.secondary)
                    .frame(height: StripDetailMetrics.labelLine, alignment: .leading)
                Button {
                    if let value = section.url, let url = StripDetailSection.safeOutputURL(value) { NSWorkspace.shared.open(url) }
                } label: {
                    HStack(alignment: .top, spacing: 12) {
                        detailText(section.value).frame(maxWidth: .infinity, alignment: .leading)
                        Image(systemName: "arrow.up.right").font(.system(size: 14)).frame(width: 14, height: StripDetailMetrics.bodyLine)
                            .foregroundStyle(style.secondary)
                    }.frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
                }.buttonStyle(.plain).accessibilityLabel("查看正式产出：" + section.value)
            }
        }
    }

    private func statusColor(_ value: String) -> Color {
        if ["遇到卡点", "阻塞", "受阻", "有风险", "需关注"].contains(value) {
            return style == .pulse ? Color(red: 0.97, green: 0.77, blue: 0.47) : Color(red: 0.55, green: 0.30, blue: 0.08)
        }
        if ["顺利", "健康", "完成", "已完成", "开发中"].contains(value) { return style.accent }
        return style.secondary
    }


}

private extension View {
    @ViewBuilder func colorInvertIf(_ value: Bool) -> some View { if value { self.colorInvert() } else { self } }
}
