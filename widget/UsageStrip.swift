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
    var secondary: Color { self == .pulse ? Color(white: 0.67) : StripInk.secondary }
    var accent: Color { self == .pulse ? Color(red: 0.68, green: 0.88, blue: 0.71) : Color(red: 0.29, green: 0.47, blue: 0.37) }
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
    var progressLabel = "开发进度"
    var todayTokens: Int?
    var ringStale = false
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
        guard ["manual", "tasks"].contains(source ?? ""), let value, value.isFinite, 0...100 ~= value else { return nil }
        return value
    }

    static func items(_ snapshot: Snapshot?, failed: Bool = false,
                      metric: StripQuotaMetric = .weeklyRemaining, now: Date = Date()) -> [StripItem] {
        guard let snapshot else {
            return [StripItem(id: "quota", caption: metric.caption, value: "—", title: "Codex 额度",
                             detailValue: "—", unit: "剩余比例", footnote: failed ? "读取失败，稍后重试" : "正在读取", avatar: .openAI),
                    StripItem(id: "total", caption: "本周记录", value: "—", title: "本机 Token",
                             detailValue: "—", unit: "Token", footnote: "正在读取本机记录", avatar: .symbol("book.closed"))]
        }
        // Window identity comes from the reported duration, never the primary/secondary key.
        let week = snapshot.quota?.windows.first { $0.duration_mins == 10080 }
        let short = snapshot.quota?.windows.first { $0.duration_mins == 300 }
        let stale = snapshot.quota?.stale == true || snapshot.quota_error != nil || failed
        func staleWindow(_ window: QuotaWindow?) -> Bool {
            stale || window.map { $0.resets_at <= now.timeIntervalSince1970 } == true
        }
        func remaining(_ window: QuotaWindow?) -> String {
            guard let window else { return "—" }
            let value = window.remaining_percent > 0 && window.remaining_percent < 1 ? "<1%" : String(format: "%.0f%%", window.remaining_percent)
            return (staleWindow(window) ? "≈" : "") + value
        }
        let budget = snapshot.weekly_budget
        let quotaValue: String
        switch metric {
        case .weeklyRemaining: quotaValue = remaining(week)
        case .shortRemaining: quotaValue = remaining(short)
        case .weeklyUsed:
            quotaValue = week.map { (staleWindow($0) ? "≈" : "") + String(format: "%.0f%%", $0.used_percent) } ?? "—"
        case .weeklyObserved: quotaValue = budget?.stripUsageLabel ?? "—"
        case .weeklyExcess: quotaValue = budget?.stripExcessLabel ?? "—"
        }
        let selected = metric == .shortRemaining ? short : week
        let quotaFoot: String
        if [.weeklyObserved, .weeklyExcess].contains(metric) {
            quotaFoot = budget == nil ? "尚无周观测" : budget?.status == "incomplete" ? "已记录累计\n周记录不完整" : "已记录累计\n不是 Token 比例"
        } else if selected == nil { quotaFoot = "尚未取得额度" }
        else if staleWindow(selected) { quotaFoot = "上次记录\n等待更新" }
        else if selected!.remaining_percent <= 0 { quotaFoot = "当前窗口已用尽" }
        else { quotaFoot = "账号当前额度窗口" }
        var result = [
            StripItem(id: "quota", caption: metric.caption, value: quotaValue, title: "Codex 额度",
                      detailValue: quotaValue, unit: "比例", footnote: quotaFoot, avatar: .openAI),
            StripItem(id: "total", caption: "本周记录", value: compactTokens(snapshot.total_tokens),
                      title: "本机 Token", detailValue: compactTokens(snapshot.total_tokens), unit: "Token",
                      footnote: failed ? "上次记录 · 等待更新" : snapshot.legacy_updates > 0 ? "含旧记录估算" : "本机保留记录", avatar: .symbol("book.closed"))
        ]
        switch metric {
        case .weeklyRemaining: result[0].primaryRing = fraction(week?.remaining_percent)
        case .shortRemaining: result[0].primaryRing = fraction(short?.remaining_percent)
        case .weeklyUsed: result[0].primaryRing = fraction(week?.used_percent)
        case .weeklyObserved: result[0].primaryRing = fraction(budget?.observed_total_percent)
        case .weeklyExcess:
            result[0].primaryRing = fraction(budget?.excess_percent ?? (budget?.observed_total_percent.map { $0 <= 100 ? 0 : nil } ?? nil))
        }
        result[0].ringStale = staleWindow(selected)
        result[1].todayTokens = snapshot.today_tokens.flatMap { $0 >= 0 ? $0 : nil }
        let major = snapshot.tasks.filter { $0.is_other != true }
        let ranked = (major + snapshot.other_tasks).sorted {
            $0.total_tokens == $1.total_tokens ? $0.id < $1.id : $0.total_tokens > $1.total_tokens
        }
        if let week {
            result[0].facts.append(StripFact(label: "当前已用", value: (staleWindow(week) ? "≈" : "") + String(format: "%.0f%%", week.used_percent)))
        }
        result[0].facts.append(StripFact(label: "本周已用", value: budget?.stripUsageLabel ?? "尚无记录"))
        result[0].facts.append(StripFact(label: "本周超额", value: budget?.stripExcessLabel ?? "尚无记录"))
        if budget?.status == "incomplete", !result[0].footnote.contains("不完整") {
            let ring = result[0].primaryRing
            result[0] = StripItem(id: "quota", caption: metric.caption, value: quotaValue, title: "Codex 额度",
                detailValue: quotaValue, unit: "比例", footnote: quotaFoot + "\n周记录不完整",
                facts: result[0].facts, avatar: .openAI)
            result[0].primaryRing = ring
            result[0].ringStale = staleWindow(selected)
        }
        result[0].todayTokens = snapshot.today_tokens.flatMap { $0 >= 0 ? $0 : nil }
        if let week {
            let formatter = DateFormatter()
            formatter.locale = Locale(identifier: "zh_CN")
            formatter.timeZone = TimeZone(identifier: "Asia/Shanghai")
            formatter.dateFormat = "EEE HH:mm"
            result[0].facts.append(StripFact(label: "下次重置", value: formatter.string(from: Date(timeIntervalSince1970: week.resets_at))))
        }
        if let short {
            result[0].facts.append(StripFact(label: "5小时剩余", value: remaining(short)))
            let formatter = DateFormatter()
            formatter.locale = Locale(identifier: "zh_CN")
            formatter.timeZone = TimeZone(identifier: "Asia/Shanghai")
            formatter.dateFormat = "EEE HH:mm"
            result[0].facts.append(StripFact(label: "5小时重置", value: formatter.string(from: Date(timeIntervalSince1970: short.resets_at))))
        }
        if let observed = snapshot.quota?.observed_at {
            result[0].facts.append(StripFact(label: "最近观测", value: timestampLabel(observed)))
        }
        result[1].facts = [StripFact(label: "任务数量", value: "\(ranked.count) 个"),
                           StripFact(label: "模型响应", value: "\(snapshot.request_records) 次")]
        if snapshot.legacy_updates > 0 {
            result[1].facts.append(StripFact(label: "记录质量", value: "含旧版估算"))
        }
        if let count = snapshot.warnings_count, count > 0 {
            result[1].facts.append(StripFact(label: "扫描提示", value: "\(count) 项"))
        }
        if let start = budget?.period_start, let end = budget?.period_end {
            result[1].facts.append(StripFact(label: "统计时间", value: localDate(start) + "\n—\n" + localDate(end - 1)))
        }
        let projectRows = snapshot.management_projects ?? ranked
        let visible = Array(projectRows.prefix(3))
        for row in visible {
            let title = row.display_name?.isEmpty == false ? row.display_name! : row.name
            var management: [StripFact] = []
            func add(_ label: String, _ value: String?) {
                if let value = value?.trimmingCharacters(in: .whitespacesAndNewlines), !value.isEmpty {
                    management.append(StripFact(label: label, value: value))
                }
            }
            add("本周目标", row.weekly_goal)
            add("下一步", row.next_step)
            add("项目目标", row.project_goal)
            add("交付日期", row.target_date.map(projectDate))
            add("优先级", row.project_priority)
            add("健康度", row.project_health)
            if let count = row.open_tasks { add("待办任务", "\(count) 项") }
            if let count = row.blocked_tasks { add("受阻任务", "\(count) 项") }
            if let count = row.completed_tasks { add("已完成", "\(count) 项") }
            add("项目摘要", row.project_summary)
            add("最近复盘", row.last_reviewed.map(projectDate))
            let projectAvatar = StripAvatar.project(row)
            let usageLinked = row.usage_linked != false
            let development = projectProgress(row.development_progress, source: row.progress_source)
            var facts: [StripFact] = []
            if usageLinked {
                facts = [StripFact(label: "本周占比", value: String(format: "%.1f%%", row.percent)),
                         StripFact(label: "会话数量", value: row.sessions_count.map { "\($0) 个" } ?? "未记录"),
                         StripFact(label: "模型响应", value: "\(row.request_records) 次")]
                if let rank = ranked.firstIndex(where: { $0.id == row.id }) {
                    facts.insert(StripFact(label: "用量排名", value: "第 \(rank + 1) 名"), at: 1)
                }
            }
            if usageLinked && snapshot.legacy_updates == 0 && row.request_records > 0 {
                let mean = Int((Double(row.total_tokens) / Double(row.request_records)).rounded())
                facts.append(StripFact(label: "平均/响应", value: compactTokens(mean) + " Token"))
            }
            result.append(StripItem(id: row.id, caption: title, value: usageLinked ? String(format: "%.1f%%", row.percent) : "—",
                title: title, detailValue: usageLinked ? compactTokens(row.total_tokens) : "—", unit: usageLinked ? "Token" : "未关联本地记录",
                footnote: usageLinked ? (row.sessions_count.map { "\($0) 个会话" } ?? "会话数未记录") : "未关联本地记录",
                facts: facts, managementFacts: management, avatar: projectAvatar,
                projectStatus: row.project_status?.isEmpty == false ? row.project_status : row.notion_url == nil ? nil : "未设置",
                notionURL: row.notion_url, notionStale: row.notion_stale == true,
                needsProjectIcon: projectAvatar == .symbol("folder"), usageLinked: usageLinked,
                managementList: snapshot.management_projects != nil,
                primaryRing: usageLinked ? fraction(row.percent) : nil,
                secondaryRing: fraction(development), developmentProgress: development,
                progressLabel: row.progress_source == "tasks" ? "行动完成率" : "开发进度",
                todayTokens: usageLinked ? row.today_tokens.flatMap { $0 >= 0 ? $0 : nil } : nil))
        }
        let remainder = Array(projectRows.dropFirst(visible.count))
        if !remainder.isEmpty {
            if snapshot.management_projects != nil {
                result.append(StripItem(id: "remainder", caption: "更多项目", value: "\(remainder.count)",
                    title: "更多项目", detailValue: "\(remainder.count)", unit: "项目", footnote: "Notion 项目列表",
                    members: remainder, facts: [], avatar: .symbol("ellipsis"), managementList: true))
            } else {
                let tokens = remainder.reduce(0) { $0 + $1.total_tokens }
                let percent = snapshot.total_tokens > 0 ? Double(tokens) * 100 / Double(snapshot.total_tokens) : 0
                result.append(StripItem(id: "remainder", caption: "其他", value: String(format: "%.1f%%", percent),
                    title: "其他任务", detailValue: compactTokens(tokens), unit: "Token",
                    footnote: "其余任务合计", members: remainder,
                    facts: [StripFact(label: "合并任务", value: "\(remainder.count) 个")], avatar: .symbol("ellipsis")))
            }
        }
        if projectRows.isEmpty {
            result.append(StripItem(id: "empty", caption: "暂无项目", value: "—", title: "暂无项目",
                detailValue: "—", unit: "项目", footnote: snapshot.management_projects == nil ? "本周暂无可识别记录" : "请在 Notion\n创建项目并分配图标", avatar: .symbol("folder")))
        }
        return result
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

// Quota switching belongs only to the icon, never to the dashboard scroll area.
enum QuotaWheelRouting {
    static func consumes(sameWindow: Bool, point: CGPoint, iconBounds: CGRect,
                         deltaX: CGFloat, deltaY: CGFloat, momentum: Bool) -> Bool {
        guard sameWindow, !momentum, abs(deltaY) > abs(deltaX), iconBounds.width > 0, iconBounds.height > 0 else { return false }
        let dx = (point.x - iconBounds.midX) / (iconBounds.width / 2)
        let dy = (point.y - iconBounds.midY) / (iconBounds.height / 2)
        return dx * dx + dy * dy <= 1
    }
}

@MainActor
final class StripInteraction: ObservableObject {
    @Published var expanded: String?
    @Published var locked = false
    @Published var dashboardPage: StripDashboardPage = .overview
    @Published var quotaMetric: StripQuotaMetric = .weeklyUsed
    private var hovered: String?
    private var zones: [String: (id: String, sequence: Int)] = [:]
    private var sequence = 0
    private var pending: DispatchWorkItem?
    var onResize: (() -> Void)?

    func hover(_ id: String, inside: Bool, zone: String = "pointer") {
        if inside {
            sequence += 1
            zones[zone] = (id, sequence)
        } else if zones[zone]?.id == id { zones.removeValue(forKey: zone) }
        hovered = zones.values.max(by: { $0.sequence < $1.sequence })?.id
        guard !locked else { return }
        pending?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            if self.expanded != self.hovered { self.dashboardPage = .overview }
            self.expanded = self.hovered
            self.onResize?()
        }
        pending = work
        DispatchQueue.main.asyncAfter(deadline: .now() + (inside ? 0.18 : 0.24), execute: work)
    }

    func click(_ id: String) {
        pending?.cancel()
        if locked && expanded == id { dismiss() }
        else {
            if expanded != id { dashboardPage = .overview }
            expanded = id; locked = true; onResize?()
        }
    }

    func selectPage(_ page: StripDashboardPage) {
        guard expanded != nil, locked else { return }
        dashboardPage = page
    }

    func cycleQuota(steps: Int = 1) {
        quotaMetric = quotaMetric.advanced(by: steps)
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
        expanded = nil
        onResize?()
    }
}


// Real OpenAI Blossom, unchanged shape. Source and ownership: THIRD_PARTY_NOTICES.md.
private enum StripBrandAsset {
    static let openAI = NSImage(data: Data(base64Encoded: "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")!)!
}

enum StripInk {
    static let primary = Color(red: 0.12, green: 0.16, blue: 0.14)
    static let secondary = Color(red: 0.30, green: 0.36, blue: 0.32)
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
            content.background(Color(red: 0.085, green: 0.095, blue: 0.11).opacity(reduceTransparency ? 1 : 0.98), in: shape)
                .overlay(shape.strokeBorder(Color.white.opacity(active ? 0.17 : 0.10), lineWidth: 0.7).allowsHitTesting(false))
        } else if style == .white || reduceTransparency || contrast == .increased {
            content.background(Color(red: 0.98, green: 0.98, blue: 0.96), in: shape)
                .overlay(shape.strokeBorder(StripInk.primary.opacity(active ? 0.14 : 0.09), lineWidth: 0.7).allowsHitTesting(false))
        } else if #available(macOS 26.0, *) {
            content.glassEffect(.regular.tint(Color.white.opacity(active ? 0.14 : 0.07)), in: .rect(cornerRadius: radius))
                .overlay(shape.strokeBorder(Color.white.opacity(0.55), lineWidth: 0.7).allowsHitTesting(false))
        } else {
            content.background(.ultraThinMaterial, in: shape)
                .overlay(shape.fill(Color.white.opacity(active ? 0.13 : 0.06)).allowsHitTesting(false))
                .overlay(shape.strokeBorder(Color.white.opacity(0.65), lineWidth: 0.7).allowsHitTesting(false))
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

// A local event monitor receives only scrolls inside this transparent view's bounds.
// It does not take mouse clicks away from the persistent SwiftUI module button.
private struct QuotaWheelArea: NSViewRepresentable {
    let cycle: (Int) -> Void
    class MonitorView: NSView {
        var cycle: ((Int) -> Void)?
        var monitor: Any?
        var accumulated: CGFloat = 0
        var lastCycle: TimeInterval = 0
        override func hitTest(_ point: NSPoint) -> NSView? { nil }
        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            if let monitor { NSEvent.removeMonitor(monitor); self.monitor = nil }
            guard window != nil else { return }
            monitor = NSEvent.addLocalMonitorForEvents(matching: .scrollWheel) { [weak self] event in
                guard let self, QuotaWheelRouting.consumes(sameWindow: event.window === self.window,
                    point: self.convert(event.locationInWindow, from: nil), iconBounds: self.bounds,
                    deltaX: event.scrollingDeltaX, deltaY: event.scrollingDeltaY,
                    momentum: !event.momentumPhase.isEmpty) else { return event }
                let stamp = ProcessInfo.processInfo.systemUptime
                self.accumulated += event.scrollingDeltaY
                let threshold: CGFloat = event.hasPreciseScrollingDeltas ? 22 : 1
                if abs(self.accumulated) >= threshold && stamp - self.lastCycle >= 0.22 {
                    self.cycle?(self.accumulated < 0 ? 1 : -1)
                    self.accumulated = 0
                    self.lastCycle = stamp
                }
                return nil
            }
        }
        deinit { if let monitor { NSEvent.removeMonitor(monitor) } }
    }
    func makeNSView(context: Context) -> MonitorView { let view = MonitorView(); view.cycle = cycle; return view }
    func updateNSView(_ view: MonitorView, context: Context) { view.cycle = cycle }
}

struct UsageStrip: View {
    static let moduleWidth: CGFloat = 68
    static let headerHeight: CGFloat = 64
    static let diameter: CGFloat = 64
    static let gap: CGFloat = 0
    static let inset: CGFloat = 3
    static let rowInset: CGFloat = 2
    static let detailWidth: CGFloat = 328
    static let detailGap: CGFloat = 10
    static let detailRadius: CGFloat = 20
    static let maxDetailHeight: CGFloat = 420
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
    static func detailHeight(for item: StripItem) -> CGFloat { maxDetailHeight }
    static func needsCompactScroll(availableHeight: CGFloat, locked: Bool) -> Bool {
        availableHeight < (locked ? 405 : 305)
    }
    static func detailFrame(for item: StripItem, index: Int, itemCount: Int, vertical: Bool, canvas: CGSize, headerOffset: CGFloat = 0, locked: Bool = true) -> CGRect {
        let header = headerFrame(itemCount: itemCount, vertical: vertical, canvas: canvas, headerOffset: headerOffset)
        let desiredHeight: CGFloat = locked ? detailHeight(for: item) : 318
        let width = min(detailWidth, max(0, canvas.width - 2 * inset))
        let below = max(0, canvas.height - header.maxY - detailGap - inset)
        let above = max(0, header.minY - detailGap - inset)
        let opensBelow = below >= desiredHeight || below >= above
        let available = vertical ? canvas.height - 2 * inset : (opensBelow ? below : above)
        let height = min(desiredHeight, max(0, available))
        let desiredX = header.minX + inset + CGFloat(index) * moduleWidth + moduleWidth / 2 - width / 2
        let x = vertical ? inset : min(max(inset, desiredX), max(inset, canvas.width - width - inset))
        let desiredY = vertical ? header.minY + inset + CGFloat(index) * (diameter + 2 * rowInset) :
            (opensBelow ? header.maxY + detailGap : header.minY - detailGap - height)
        let y = min(max(inset, desiredY), max(inset, canvas.height - height - inset))
        return CGRect(x: x, y: y, width: width, height: height)
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
        return CGRect(x: x - 9, y: header.maxY - inset, width: 18, height: detail.minY - header.maxY + inset)
    }
    @ObservedObject var model: UsageModel
    @ObservedObject var interaction: StripInteraction
    @Environment(\.accessibilityReduceMotion) var reduceMotion
    let vertical: Bool
    let height: CGFloat
    var canvasWidth: CGFloat = 0
    var headerOffset: CGFloat = 0
    var items: [StripItem] { StripItem.items(model.snapshot, failed: model.error != nil, metric: interaction.quotaMetric) }
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
                let card = Self.detailFrame(for: item, index: index, itemCount: items.count, vertical: vertical, canvas: canvas, headerOffset: headerOffset, locked: interaction.locked)
                let bridge = Self.connectorFrame(index: index, itemCount: items.count, vertical: vertical, canvas: canvas, detail: card, headerOffset: headerOffset)
                Color.clear.frame(width: bridge.width, height: bridge.height).contentShape(Rectangle())
                    .offset(x: bridge.minX, y: bridge.minY)
                    .onHover { interaction.hover(item.id, inside: $0, zone: "bridge:\(item.id)") }
                details(item, availableHeight: card.height).frame(width: card.width, height: card.height, alignment: .top)
                    .modifier(SystemGlass(active: true, radius: Self.detailRadius, style: style))
                    .clipShape(RoundedRectangle(cornerRadius: Self.detailRadius, style: .continuous))
                    .shadow(color: .black.opacity(style == .pulse ? 0.28 : 0.14), radius: 20, y: 9)
                    .offset(x: card.minX, y: card.minY).zIndex(10)
                    .transition(.opacity.combined(with: .scale(scale: 0.97, anchor: vertical ? .trailing : .top)))
                    .onHover { interaction.hover(item.id, inside: $0, zone: "detail:\(item.id)") }
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
            ScrollView {
                VStack(spacing: 2 * Self.rowInset) {
                    ForEach(items) { item in circle(item, foreground: interaction.expanded == item.id)
                        .onHover { interaction.hover(item.id, inside: $0, zone: "row:\(item.id)") } }
                }.padding(Self.inset)
            }.scrollIndicators(.hidden)
        } else {
            HStack(spacing: 0) {
                ForEach(items) { item in circle(item, foreground: interaction.expanded == item.id)
                    .frame(width: Self.moduleWidth)
                    .onHover { interaction.hover(item.id, inside: $0, zone: "row:\(item.id)") } }
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
            }.frame(width: Self.diameter, height: Self.diameter)
                .background(Circle().fill(style.primary.opacity(foreground ? 0.045 : 0)))
                .shadow(color: style.accent.opacity(foreground ? 0.14 : 0), radius: 8)
                .contentShape(Circle())
        }.buttonStyle(.plain).foregroundStyle(style.primary)
            .background {
                if item.id == "quota" { QuotaWheelArea { interaction.cycleQuota(steps: $0) }.frame(width: Self.diameter, height: Self.diameter) }
            }
            .accessibilityLabel(item.isProject ? "\(item.title)，\(item.projectStatus ?? "未设置状态")，外环本周 Token 占比，内环\(item.progressLabel)，点击查看项目看板" : "\(item.title)，\(item.caption) \(item.value)")
            .help(item.id == "quota" ? "滚轮切换额度指标；点击保持看板" : "\(item.title) · 悬停查看项目看板，点击保持")
            .contextMenu {
                if item.id == "quota" { ForEach(StripQuotaMetric.allCases) { metric in Button(metric.caption) { interaction.quotaMetric = metric } } }
                else if item.isProject {
                    if let url = item.notionURL { Button("打开 Notion 项目") { model.openProject(url) } }
                    Button("项目设置…") { model.openSettings() }
                }
            }
    }

    private func management(_ label: String, in item: StripItem) -> String? { item.managementFacts.first(where: { $0.label == label })?.value }
    private func record(_ label: String, in item: StripItem) -> String? { item.facts.first(where: { $0.label == label })?.value }
    private func badge(_ value: String, tint: Color? = nil) -> some View {
        Text(value).font(.system(size: 11, weight: .medium)).foregroundStyle(tint ?? style.secondary)
            .padding(.horizontal, 8).padding(.vertical, 4).background((tint ?? style.primary).opacity(0.08), in: Capsule())
    }
    private func statement(_ label: String, _ value: String, lines: Int = 2) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(label).font(.system(size: 11)).foregroundStyle(style.secondary)
            Text(value).font(.system(size: 13, weight: .medium)).lineLimit(lines).fixedSize(horizontal: false, vertical: true).help(value)
        }.frame(maxWidth: .infinity, alignment: .leading)
    }
    private func statistic(_ label: String, _ value: String, tint: Color? = nil) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(value).font(.system(size: 16, weight: .semibold, design: .rounded)).monospacedDigit().foregroundStyle(tint ?? style.primary)
            Text(label).font(.system(size: 11)).foregroundStyle(style.secondary)
        }.frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder func details(_ item: StripItem, availableHeight: CGFloat) -> some View {
        // Ordinary cards are bounded summaries. Small screens retain a real,
        // reachable scroll fallback instead of clipping the last actions.
        if Self.needsCompactScroll(availableHeight: availableHeight, locked: interaction.locked) {
            ScrollView { dashboard(item).fixedSize(horizontal: false, vertical: true) }
                .scrollIndicators(.hidden).foregroundStyle(style.primary)
        } else {
            ViewThatFits(in: .vertical) {
                dashboard(item).fixedSize(horizontal: false, vertical: true)
                ScrollView { dashboard(item).fixedSize(horizontal: false, vertical: true) }.scrollIndicators(.hidden)
            }.foregroundStyle(style.primary)
        }
    }

    private func dashboard(_ item: StripItem) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .top, spacing: 10) {
                avatar(item).frame(width: 30, height: 30)
                VStack(alignment: .leading, spacing: 3) {
                    Text(item.title).font(.system(size: 16, weight: .semibold)).lineLimit(2).help(item.title)
                    Text(item.isProject ? (item.projectStatus ?? (item.notionURL == nil ? "未绑定项目" : "未设置状态")) : item.caption)
                        .font(.system(size: 11)).foregroundStyle(style.secondary)
                }
                Spacer(minLength: 0)
                if item.isProject { projectAction(item) }
            }
            if interaction.locked {
                HStack(spacing: 4) {
                    ForEach(StripDashboardPage.allCases) { page in
                        Button { interaction.selectPage(page) } label: {
                            Text(page.title(for: item)).font(.system(size: 12, weight: .medium))
                                .frame(maxWidth: .infinity).padding(.vertical, 7)
                                .background(interaction.dashboardPage == page ? style.primary.opacity(0.09) : Color.clear,
                                            in: RoundedRectangle(cornerRadius: 8))
                        }.buttonStyle(.plain).accessibilityAddTraits(interaction.dashboardPage == page ? .isSelected : [])
                    }
                }.padding(3).background(style.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 10))
            }
            dashboardContent(item, page: interaction.locked ? interaction.dashboardPage : .overview)
            Spacer(minLength: 0)
            if item.isProject {
                Button { if let url = item.notionURL { model.openProject(url) } else { model.openSettings() } } label: {
                    Label(item.notionURL == nil ? "关联 Notion 项目" : "查看完整项目", systemImage: "arrow.up.right")
                        .font(.system(size: 12)).frame(maxWidth: .infinity, alignment: .leading)
                }.buttonStyle(.plain).foregroundStyle(style.accent)
            } else if !interaction.locked {
                Text("点击查看完整记录").font(.system(size: 10)).foregroundStyle(style.secondary)
            }
        }.padding(18).frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    @ViewBuilder private func dashboardContent(_ item: StripItem, page: StripDashboardPage) -> some View {
        if item.isProject { projectContent(item, page: page) }
        else if item.id == "quota" { quotaContent(item, page: page) }
        else { ledgerContent(item, page: page) }
    }

    private func progressSummary(_ item: StripItem) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                Text(item.progressLabel).font(.system(size: 11)).foregroundStyle(style.secondary)
                Spacer()
                Text(item.developmentProgress.map { String(format: "%.1f%%", $0) } ?? "未取得")
                    .font(.system(size: 13, weight: .semibold, design: .rounded)).monospacedDigit()
            }
            if let fraction = item.secondaryRing {
                GeometryReader { proxy in
                    Capsule().fill(style.track).overlay(alignment: .leading) {
                        Capsule().fill(style.progress).frame(width: proxy.size.width * fraction)
                    }
                }.frame(height: 4)
            }
        }
    }

    @ViewBuilder private func projectContent(_ item: StripItem, page: StripDashboardPage) -> some View {
        switch page {
        case .overview:
            HStack(spacing: 6) {
                if let health = management("健康度", in: item) { badge(health) }
                if item.notionStale { badge("缓存 · 待更新") }
            }
            statement("下一步", management("下一步", in: item) ?? "在 Notion 中补充下一步行动。")
            progressSummary(item)
            HStack {
                statistic("本周 Token", item.usageLinked ? item.detailValue : "未关联")
                statistic("本周占比", item.usageLinked ? item.value : "—")
            }
        case .actions:
            statement("当前目标", management("本周目标", in: item) ?? management("项目目标", in: item) ?? "未填写目标", lines: 1)
            statement("下一步", management("下一步", in: item) ?? "未填写下一步")
            HStack {
                statistic("待办", management("待办任务", in: item) ?? "—")
                statistic("受阻", management("受阻任务", in: item) ?? "—", tint: style.progress)
                statistic("已完成", management("已完成", in: item) ?? "—")
            }
            if let summary = management("项目摘要", in: item) { statement("最新产出与进展", summary) }
            HStack {
                if let priority = management("优先级", in: item) { Text("优先级 " + priority) }
                if let date = management("交付日期", in: item) { Text(date) }
            }.font(.system(size: 10)).foregroundStyle(style.secondary)
        case .investment:
            HStack {
                statistic("本周 Token", item.usageLinked ? item.detailValue : "未关联")
                statistic("今日 Token", item.todayTokens.map(compactTokens) ?? "—")
            }
            factRows(item, labels: ["本周占比", "会话数量", "模型响应", "平均/响应"])
            Text(item.usageLinked ? "外环：本周 Token 占比 · 内环：" + item.progressLabel : "未关联本地记录，不代表零用量。")
                .font(.system(size: 10)).foregroundStyle(style.secondary).lineLimit(2)
            if item.progressLabel == "行动完成率" {
                Text("行动完成率仅代表任务清单，不能代替项目整体完成度。")
                    .font(.system(size: 10)).foregroundStyle(style.secondary).lineLimit(2)
            }
        }
    }

    @ViewBuilder private func quotaContent(_ item: StripItem, page: StripDashboardPage) -> some View {
        switch page {
        case .overview:
            HStack {
                statistic(item.caption, item.detailValue)
                statistic("今日 Token · 本机", item.todayTokens.map(compactTokens) ?? "—")
            }
            factRows(item, labels: ["下次重置", "5小时剩余", "本周超额"])
            Text(item.footnote.replacingOccurrences(of: "\n", with: " · ")).font(.system(size: 11))
                .foregroundStyle(style.secondary).lineLimit(2)
        case .actions:
            HStack { statistic(item.caption, item.detailValue) }
            factRows(item, labels: ["当前已用", "5小时剩余", "下次重置", "5小时重置"])
            Button { interaction.cycleQuota() } label: { Label("切换额度指标", systemImage: "arrow.triangle.2.circlepath") }
                .buttonStyle(.plain).font(.system(size: 12)).foregroundStyle(style.accent)
            Text("滚轮只切换 Codex 图标指标，卡片内不切换额度。")
                .font(.system(size: 10)).foregroundStyle(style.secondary).lineLimit(2)
        case .investment:
            factRows(item, labels: ["本周已用", "本周超额"])
            statistic("今日 Token · 本机", item.todayTokens.map(compactTokens) ?? "—")
            Text("累计与超额来自额度观测；今日 Token 来自本机日志，两者不能互相换算。环最多显示 100%，实际累计保留原值。")
                .font(.system(size: 11)).foregroundStyle(style.secondary).lineLimit(4)
        }
    }

    @ViewBuilder private func ledgerContent(_ item: StripItem, page: StripDashboardPage) -> some View {
        if !item.members.isEmpty {
            Text("其余 \(item.members.count) 个项目").font(.system(size: 13, weight: .medium))
            ForEach(Array(item.members.prefix(4).enumerated()), id: \.offset) { _, member in
                HStack {
                    Text(member.display_name ?? member.name).lineLimit(1).help(member.display_name ?? member.name)
                    Spacer()
                    Text(item.managementList ? (member.project_status ?? "未设置状态") : String(format: "%.1f%%", member.percent))
                        .foregroundStyle(style.secondary)
                }.font(.system(size: 12))
            }
            Button("查看全部项目…") { model.openSettings() }.buttonStyle(.plain).font(.system(size: 12)).foregroundStyle(style.accent)
        } else {
            HStack {
                statistic("本周 Token", item.detailValue)
                statistic("今日 Token", item.todayTokens.map(compactTokens) ?? "—")
            }
            factRows(item, labels: page == .overview ? ["任务数量", "统计时间"] : ["任务数量", "模型响应", "记录质量", "扫描提示", "统计时间"])
            Text(item.footnote.replacingOccurrences(of: "\n", with: " · ")).font(.system(size: 11)).foregroundStyle(style.secondary).lineLimit(2)
            if page == .investment {
                Text("本机保留记录中的投入，不代表订阅剩余额度或最终账单。")
                    .font(.system(size: 10)).foregroundStyle(style.secondary).lineLimit(3)
            }
            if item.id == "empty" { Button("项目设置…") { model.openSettings() }.buttonStyle(.plain).font(.system(size: 12)) }
        }
    }

    private func factRows(_ item: StripItem, labels: [String]) -> some View {
        VStack(spacing: 10) {
            ForEach(labels, id: \.self) { label in
                if let value = record(label, in: item) {
                    HStack(alignment: .top) {
                        Text(label).foregroundStyle(style.secondary)
                        Spacer(minLength: 10)
                        Text(value.replacingOccurrences(of: "\n", with: " ")).fontWeight(.medium)
                            .multilineTextAlignment(.trailing).lineLimit(2)
                    }.font(.system(size: 12)).monospacedDigit()
                }
            }
        }
    }

    private func projectAction(_ item: StripItem) -> some View {
        Button {
            if let url = item.notionURL { model.openProject(url) } else { model.openSettings() }
        } label: {
            Image(systemName: item.notionURL == nil ? "link" : "arrow.up.right").font(.system(size: 13))
                .frame(width: 30, height: 30).background(style.primary.opacity(0.05), in: Circle())
        }.buttonStyle(.plain).help(item.notionURL == nil ? "绑定 Notion 项目" : "打开 Notion 项目")
    }
}

private extension View {
    @ViewBuilder func colorInvertIf(_ value: Bool) -> some View { if value { self.colorInvert() } else { self } }
}
