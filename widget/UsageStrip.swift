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

enum StripQuotaMetric: String, CaseIterable, Identifiable {
    case weeklyRemaining, shortRemaining, weeklyObserved, weeklyExcess
    var id: String { rawValue }
    var caption: String {
        switch self {
        case .weeklyRemaining: return "周剩余"
        case .shortRemaining: return "5h 剩余"
        case .weeklyObserved: return "本周已用"
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
    case openAI, symbol(String), initials(String), emoji(String), remote(String)

    static func project(_ row: TaskRow) -> Self {
        if row.icon_type == "emoji", let value = row.icon_value, !value.isEmpty { return .emoji(value) }
        if ["external", "file", "custom_emoji"].contains(row.icon_type ?? ""), let value = row.icon_value,
           let url = URL(string: value), url.scheme == "https", url.host != nil { return .remote(value) }
        return .initials(initials(for: row.display_name ?? row.name))
    }
    static func initials(for name: String) -> String {
        let clean = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let first = clean.first else { return "项" }
        if first.isASCII && first.isLetter {
            return String(clean.prefix(2)).uppercased()
        }
        return String(first)
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
    var avatar: StripAvatar = .symbol("ellipsis")
    var projectStatus: String?
    var notionURL: String?
    var notionStale = false
    var isProject: Bool { id.hasPrefix("folder:") }

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
            result[0] = StripItem(id: "quota", caption: metric.caption, value: quotaValue, title: "Codex 额度",
                detailValue: quotaValue, unit: "比例", footnote: quotaFoot + "\n周记录不完整",
                facts: result[0].facts, avatar: .openAI)
        }
        if let week {
            let formatter = DateFormatter()
            formatter.locale = Locale(identifier: "zh_CN")
            formatter.timeZone = TimeZone(identifier: "Asia/Shanghai")
            formatter.dateFormat = "EEE HH:mm"
            result[0].facts.append(StripFact(label: "下次重置", value: formatter.string(from: Date(timeIntervalSince1970: week.resets_at))))
        }
        if short != nil { result[0].facts.append(StripFact(label: "5小时剩余", value: remaining(short))) }
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
        let visible = Array(ranked.prefix(3))
        for (index, row) in visible.enumerated() {
            let title = row.display_name?.isEmpty == false ? row.display_name! : row.name
            var facts = [StripFact(label: "本周排名", value: "第 \(index + 1) 名"),
                         StripFact(label: "会话数量", value: row.sessions_count.map { "\($0) 个" } ?? "未记录"),
                         StripFact(label: "模型响应", value: "\(row.request_records) 次")]
            if snapshot.legacy_updates == 0 && row.request_records > 0 {
                let mean = Int((Double(row.total_tokens) / Double(row.request_records)).rounded())
                facts.append(StripFact(label: "平均/响应", value: compactTokens(mean) + " Token"))
            }
            result.append(StripItem(id: row.id, caption: title, value: String(format: "%.1f%%", row.percent),
                title: title, detailValue: compactTokens(row.total_tokens), unit: "Token",
                footnote: row.sessions_count.map { "\($0) 个会话" } ?? "会话数未记录",
                facts: facts, avatar: .project(row), projectStatus: row.project_status?.isEmpty == false ? row.project_status : row.notion_url == nil ? nil : "未设置",
                notionURL: row.notion_url, notionStale: row.notion_stale == true))
        }
        let remainder = Array(ranked.dropFirst(visible.count))
        if !remainder.isEmpty {
            let tokens = remainder.reduce(0) { $0 + $1.total_tokens }
            let percent = snapshot.total_tokens > 0 ? Double(tokens) * 100 / Double(snapshot.total_tokens) : 0
            result.append(StripItem(id: "remainder", caption: "其他", value: String(format: "%.1f%%", percent),
                title: "其他任务", detailValue: compactTokens(tokens), unit: "Token",
                footnote: "其余任务合计", members: remainder,
                facts: [StripFact(label: "合并任务", value: "\(remainder.count) 个")], avatar: .symbol("ellipsis")))
        }
        if ranked.isEmpty {
            result.append(StripItem(id: "empty", caption: "暂无任务", value: "—", title: "暂无任务用量",
                detailValue: "—", unit: "Token", footnote: "本周暂无可识别记录", avatar: .symbol("folder")))
        }
        return result
    }
}

@MainActor
final class StripInteraction: ObservableObject {
    @Published var expanded: String?
    @Published var locked = false
    @Published var quotaMetric: StripQuotaMetric = .weeklyRemaining
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
            self.expanded = self.hovered
            self.onResize?()
        }
        pending = work
        DispatchQueue.main.asyncAfter(deadline: .now() + (inside ? 0.18 : 0.24), execute: work)
    }

    func click(_ id: String) {
        pending?.cancel()
        if locked && expanded == id { dismiss() }
        else { expanded = id; locked = true; onResize?() }
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

    @ViewBuilder func body(content: Content) -> some View {
        let shape = RoundedRectangle(cornerRadius: radius, style: .continuous)
        // The strip deliberately remains a light surface, including on dark desktops.
        // Fixed dark type must never be placed on a dark accessibility fallback.
        if reduceTransparency || contrast == .increased {
            content.background(Color(red: 0.98, green: 0.98, blue: 0.96), in: shape)
                .overlay(shape.strokeBorder(StripInk.primary.opacity(active ? 0.22 : 0.12), lineWidth: 0.5))
        } else if #available(macOS 26.0, *) {
            content.glassEffect(.regular.tint(Color.white.opacity(active ? 0.78 : 0.70)), in: .rect(cornerRadius: radius))
        } else {
            content.background(Color.white.opacity(active ? 0.96 : 0.88), in: shape)
                .background(.thinMaterial, in: shape)
                .overlay(shape.strokeBorder(Color.white.opacity(active ? 0.70 : 0.44), lineWidth: 0.5))
        }
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
                guard let self, event.window === self.window,
                      self.bounds.contains(self.convert(event.locationInWindow, from: nil)),
                      abs(event.scrollingDeltaY) > abs(event.scrollingDeltaX),
                      event.momentumPhase.isEmpty else { return event }
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
    static let maxDetailHeight: CGFloat = 440
    static func naturalSize(itemCount: Int, vertical: Bool = false) -> CGSize {
        CGSize(width: vertical ? diameter + 2 * inset : CGFloat(itemCount) * moduleWidth + 2 * inset,
               height: vertical ? CGFloat(itemCount) * (diameter + 2 * rowInset) + 2 * inset : headerHeight + 2 * inset)
    }
    static func detailHeight(for item: StripItem) -> CGFloat {
        let titleAllowance = CGFloat(max(0, (item.title.count + 4) / 5 - 2)) * 14
        let projectAllowance: CGFloat = item.isProject ? 86 : item.id == "quota" ? 48 : 0
        return min(maxDetailHeight, 150 + CGFloat(item.facts.count) * 36 + titleAllowance
                   + projectAllowance + (item.members.isEmpty ? 0 : 100))
    }
    @ObservedObject var model: UsageModel
    @ObservedObject var interaction: StripInteraction
    @Environment(\.accessibilityReduceMotion) var reduceMotion
    let vertical: Bool
    let height: CGFloat
    var items: [StripItem] { StripItem.items(model.snapshot, failed: model.error != nil, metric: interaction.quotaMetric) }

    var body: some View {
        Group {
            if vertical { verticalStrip }
            else { horizontalStrip }
        }
        .frame(height: height, alignment: .top)
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .animation(reduceMotion ? nil : .smooth(duration: 0.24), value: interaction.expanded)
        .contextMenu {
            Button("项目与 Notion 设置…") { model.openSettings() }
            Divider()
            ForEach(WidgetKind.allCases, id: \.self) { kind in
                Toggle(kind.title, isOn: Binding(get: { model.enabledWidgets.contains(kind) },
                                               set: { _ in model.toggleWidget(kind) }))
            }
            Divider()
            Button("刷新") { model.refresh() }
        }
        .onChange(of: items.map(\.id)) { _, ids in
            if let selected = interaction.expanded, !ids.contains(selected) { interaction.dismiss() }
        }
        .onExitCommand { interaction.dismiss() }
    }

    private var horizontalStrip: some View {
        let width = Self.naturalSize(itemCount: items.count).width
        return ZStack(alignment: .topLeading) {
            // Both the common frame and foreground glass are decoration only.
            Color.clear.frame(width: width, height: Self.headerHeight + 2 * Self.inset)
                .modifier(SystemGlass(active: false, radius: Self.headerHeight / 2 + Self.inset))
                .shadow(color: .black.opacity(0.08), radius: 12, y: 5)
                .allowsHitTesting(false)
            if let item = items.first(where: { $0.id == interaction.expanded }),
               let index = items.firstIndex(where: { $0.id == item.id }) {
                Color.clear.frame(width: Self.diameter, height: Self.headerHeight + Self.detailHeight(for: item))
                    .modifier(SystemGlass(active: true, radius: Self.diameter / 2))
                    .shadow(color: .black.opacity(0.14), radius: 16, y: 8)
                    .offset(x: Self.inset + CGFloat(index) * Self.moduleWidth + Self.rowInset, y: Self.inset)
                    .allowsHitTesting(false)
                details(item)
                    .offset(x: Self.inset + CGFloat(index) * Self.moduleWidth + Self.rowInset,
                            y: Self.inset + Self.headerHeight)
                    .zIndex(10)
                    .onHover { interaction.hover(item.id, inside: $0, zone: "detail:\(item.id)") }
            }
            HStack(spacing: 0) {
                ForEach(items) { item in
                    circle(item, foreground: interaction.expanded == item.id)
                        .frame(width: Self.moduleWidth)
                        .overlay(alignment: .leading) {
                            if item.id != items.first?.id {
                                Rectangle().fill(StripInk.primary.opacity(0.10)).frame(width: 0.5, height: 24)
                                    .allowsHitTesting(false)
                            }
                        }
                        .onHover { interaction.hover(item.id, inside: $0, zone: "row:\(item.id)") }
                }
            }.padding(Self.inset).zIndex(20)
        }.frame(width: width, height: height, alignment: .topLeading)
    }

    private var verticalStrip: some View {
        ScrollView {
            VStack(spacing: 0) {
                ForEach(items) { item in
                    let active = interaction.expanded == item.id
                    VStack(spacing: 0) {
                        circle(item, foreground: active)
                        if active { details(item) }
                    }
                    .frame(width: Self.diameter)
                    .modifier(SystemGlass(active: active, radius: Self.diameter / 2))
                    .padding(.vertical, Self.rowInset)
                    .onHover { interaction.hover(item.id, inside: $0, zone: "column:\(item.id)") }
                }
            }.padding(Self.inset)
                .modifier(SystemGlass(active: false, radius: Self.diameter / 2 + Self.inset))
        }.scrollIndicators(.hidden)
    }

    @ViewBuilder private func avatar(_ item: StripItem) -> some View {
        switch item.avatar {
        case .openAI:
            Image(nsImage: StripBrandAsset.openAI).resizable().interpolation(.high).scaledToFit()
                .frame(width: 20, height: 20).accessibilityHidden(true)
        case .symbol(let name):
            Image(systemName: name).font(.system(size: 18, weight: .regular)).frame(width: 22, height: 22)
                .accessibilityHidden(true)
        case .initials(let letters):
            Text(letters).font(.system(size: 12, weight: .semibold, design: .rounded))
                .frame(width: 26, height: 26).background(StripInk.avatar, in: RoundedRectangle(cornerRadius: 8))
                .accessibilityHidden(true)
        case .emoji(let value):
            // This is the user's Notion page avatar, not a functional control icon.
            Text(value).font(.system(size: 22)).frame(width: 26, height: 26).accessibilityHidden(true)
        case .remote(let value):
            AsyncImage(url: URL(string: value)) { phase in
                if let image = phase.image { image.resizable().scaledToFill() }
                else {
                    Text(StripAvatar.initials(for: item.title)).font(.system(size: 12, weight: .semibold))
                        .frame(maxWidth: .infinity, maxHeight: .infinity).background(StripInk.avatar)
                }
            }.frame(width: 26, height: 26).clipShape(RoundedRectangle(cornerRadius: 8))
                .accessibilityHidden(true)
        }
    }

    func circle(_ item: StripItem, foreground: Bool) -> some View {
        Button { interaction.click(item.id) } label: {
            VStack(spacing: item.isProject ? 4 : 2) {
                avatar(item)
                Text(item.value).font(.system(size: 16, weight: .medium, design: .rounded))
                    .monospacedDigit().foregroundStyle(StripInk.primary).lineLimit(1)
                if !item.isProject {
                    Text(item.caption).font(.system(size: 10, weight: .medium)).foregroundStyle(StripInk.secondary)
                        .lineLimit(1)
                }
            }
            .frame(width: Self.diameter, height: Self.diameter)
            .background(Circle().fill(Color.white.opacity(foreground ? 0 : 0.035)))
            .contentShape(Circle())
        }.buttonStyle(.plain)
            .foregroundStyle(StripInk.primary)
            .background {
                if item.id == "quota" {
                    QuotaWheelArea { interaction.cycleQuota(steps: $0) }
                        .frame(width: Self.diameter, height: Self.diameter)
                }
            }
            .accessibilityLabel(item.isProject ? "\(item.title)，本周占比 \(item.value)，\(item.projectStatus ?? (item.notionURL == nil ? "本地任务" : "未设置状态"))" : "\(item.title)，\(item.caption) \(item.value)")
            .help(item.id == "quota" ? "滚轮切换额度指标；点击保持详情，右键选择指标" : "\(item.title) · 悬停查看详情，点击保持展开")
            .contextMenu {
                if item.id == "quota" {
                    ForEach(StripQuotaMetric.allCases) { metric in
                        Button(metric.caption) { interaction.quotaMetric = metric }
                    }
                } else if item.isProject {
                    if model.notionEnabled, let url = item.notionURL { Button("打开 Notion 项目") { model.openProject(url) } }
                    Button(model.notionEnabled ? "项目绑定设置…" : "项目设置…") { model.openSettings() }
                }
            }
    }

    func details(_ item: StripItem) -> some View {
        ScrollView {
            VStack(spacing: 10) {
                Rectangle().fill(StripInk.primary.opacity(0.12)).frame(width: 26, height: 0.5)
                Text(item.title).font(.system(size: 11, weight: .semibold))
                    .multilineTextAlignment(.center).fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity)
                if item.isProject {
                    VStack(spacing: 3) {
                        Text(item.projectStatus ?? (item.notionURL == nil ? "本地任务" : "未设置状态")).font(.system(size: 10, weight: .medium))
                        if item.notionStale {
                            Text("状态待更新").font(.system(size: 10)).foregroundStyle(StripInk.secondary)
                        }
                    }
                }
                if item.id != "quota" {
                    VStack(spacing: 4) {
                        Text(item.detailValue).font(.system(size: 16, weight: .medium, design: .rounded))
                            .monospacedDigit().lineLimit(1)
                        Text(item.unit).font(.system(size: 10)).foregroundStyle(StripInk.secondary)
                    }
                }
                ForEach(item.facts) { fact in
                    VStack(spacing: 4) {
                        Text(fact.label).font(.system(size: 10)).foregroundStyle(StripInk.secondary)
                        Text(fact.value.replacingOccurrences(of: " Token", with: "\nToken"))
                            .font(.system(size: 11, weight: .medium)).monospacedDigit()
                            .multilineTextAlignment(.center).fixedSize(horizontal: false, vertical: true)
                    }
                }
                ForEach(item.members) { member in
                    VStack(spacing: 3) {
                        Text(member.display_name ?? member.name)
                        Text(String(format: "%.1f%%", member.percent)).monospacedDigit().foregroundStyle(StripInk.secondary)
                    }.font(.system(size: 10)).multilineTextAlignment(.center)
                }
                if !item.isProject {
                    Text(item.footnote).font(.system(size: 10)).foregroundStyle(StripInk.secondary)
                        .multilineTextAlignment(.center).fixedSize(horizontal: false, vertical: true)
                }
                if item.id == "quota" {
                    Button { interaction.cycleQuota() } label: {
                        VStack(spacing: 3) {
                            Image(systemName: "arrow.triangle.2.circlepath").font(.system(size: 12))
                            Text("切换指标").font(.system(size: 10))
                        }.frame(width: 48, height: 44)
                    }.buttonStyle(.plain).help("也可在 Codex 模块上滚动切换")
                } else if item.isProject {
                    Button {
                        if model.notionEnabled, let url = item.notionURL { model.openProject(url) }
                        else { model.openSettings() }
                    } label: {
                        VStack(spacing: 3) {
                            Image(systemName: !model.notionEnabled ? "gearshape" : (item.notionURL == nil ? "link" : "arrow.up.right")).font(.system(size: 12))
                            Text(!model.notionEnabled ? "设置" : (item.notionURL == nil ? "绑定项目" : "Notion")).font(.system(size: 10))
                        }.frame(width: 48, height: 44)
                    }.buttonStyle(.plain).help(!model.notionEnabled ? "打开本机项目与显示设置" : (item.notionURL == nil ? "在设置中绑定本地任务与 Notion 项目" : "打开对应 Notion 项目"))
                }
            }.padding(.horizontal, 7).padding(.top, 8).padding(.bottom, 26)
                .frame(maxWidth: .infinity)
        }.scrollIndicators(.hidden)
            .foregroundStyle(StripInk.primary)
            .frame(width: Self.diameter, height: Self.detailHeight(for: item), alignment: .top)
            .contentShape(Rectangle()).onTapGesture { }
    }
}
