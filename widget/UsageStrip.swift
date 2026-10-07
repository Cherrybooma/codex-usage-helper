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

struct StripFact: Identifiable {
    var id: String { label }
    let label: String
    let value: String
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

    static func items(_ snapshot: Snapshot?, failed: Bool = false) -> [StripItem] {
        guard let snapshot else {
            return [StripItem(id: "quota", caption: "周额度剩余", value: "—", title: "等待额度数据", detailValue: "—", unit: "剩余比例", footnote: failed ? "读取失败，稍后重试" : "正在读取"),
                    StripItem(id: "total", caption: "本周记录", value: "—", title: "本机 Token", detailValue: "—", unit: "Token", footnote: "正在读取本机记录")]
        }
        let week = snapshot.quota?.windows.first { $0.duration_mins == 10080 }
        let stale = snapshot.quota?.stale == true || snapshot.quota_error != nil || failed
        let remaining = week.map { String(format: "%.0f%%", $0.remaining_percent) } ?? "—"
        var result = [
            StripItem(id: "quota", caption: "周额度剩余", value: (stale && week != nil ? "≈" : "") + remaining,
                      title: "额度详情", detailValue: remaining, unit: "剩余比例",
                      footnote: week == nil ? "尚未取得额度" : stale ? "上次记录 · 等待更新" : "账号当前额度窗口"),
            StripItem(id: "total", caption: "本周记录", value: compactTokens(snapshot.total_tokens),
                      title: "本机 Token", detailValue: compactTokens(snapshot.total_tokens), unit: "Token",
                      footnote: failed ? "上次记录 · 等待更新" : snapshot.legacy_updates > 0 ? "含旧记录估算" : "\(snapshot.request_records) 个响应")
        ]
        let major = snapshot.tasks.filter { $0.is_other != true }
        let ranked = (major + snapshot.other_tasks).sorted {
            $0.total_tokens == $1.total_tokens ? $0.name < $1.name : $0.total_tokens > $1.total_tokens
        }
        if let week {
            result[0].facts.append(StripFact(label: "当前已用", value: (stale ? "≈" : "") + String(format: "%.0f%%", week.used_percent)))
        }
        let budget = snapshot.weekly_budget
        result[0].facts.append(StripFact(label: "本周已用", value: budget?.stripUsageLabel ?? "尚无记录"))
        result[0].facts.append(StripFact(label: "本周超额", value: budget?.stripExcessLabel ?? "尚无记录"))
        if budget?.status == "incomplete" {
            result[0] = StripItem(id: result[0].id, caption: result[0].caption, value: result[0].value,
                title: result[0].title, detailValue: result[0].detailValue, unit: result[0].unit,
                footnote: stale ? "额度待更新\n周记录不完整" : "周记录不完整", facts: result[0].facts)
        }
        if let week {
            let formatter = DateFormatter()
            formatter.locale = Locale(identifier: "zh_CN")
            formatter.timeZone = TimeZone(identifier: "Asia/Shanghai")
            formatter.dateFormat = "EEE HH:mm"
            result[0].facts.append(StripFact(label: "下次重置", value: formatter.string(from: Date(timeIntervalSince1970: week.resets_at))))
        }
        if let short = snapshot.quota?.windows.first(where: { $0.duration_mins == 300 }) {
            result[0].facts.append(StripFact(label: "5小时剩余", value: (stale ? "≈" : "") + String(format: "%.0f%%", short.remaining_percent)))
        }
        result[1].facts = [StripFact(label: "任务数量", value: "\(ranked.count) 个"),
                           StripFact(label: "模型响应", value: "\(snapshot.request_records) 次")]
        let visible = Array(ranked.prefix(3))
        for row in visible {
            result.append(StripItem(id: row.id, caption: row.name, value: String(format: "%.1f%%", row.percent),
                title: row.name, detailValue: compactTokens(row.total_tokens), unit: "Token",
                footnote: row.sessions_count.map { "\($0) 个会话" } ?? "会话数未记录"))
            var facts = [StripFact(label: "本周排名", value: "第 \((ranked.firstIndex(where: { $0.id == row.id }) ?? 0) + 1) 名"),
                         StripFact(label: "会话数量", value: row.sessions_count.map { "\($0) 个" } ?? "未记录"),
                         StripFact(label: "模型响应", value: "\(row.request_records) 次")]
            if snapshot.legacy_updates == 0 && row.request_records > 0 {
                let mean = Int((Double(row.total_tokens) / Double(row.request_records)).rounded())
                facts.append(StripFact(label: "平均/响应", value: compactTokens(mean) + " Token"))
            }
            result[result.count - 1].facts = facts
        }
        let remainder = Array(ranked.dropFirst(visible.count))
        if !remainder.isEmpty {
            let tokens = remainder.reduce(0) { $0 + $1.total_tokens }
            let percent = snapshot.total_tokens > 0 ? Double(tokens) * 100 / Double(snapshot.total_tokens) : 0
            result.append(StripItem(id: "remainder", caption: "其他", value: String(format: "%.1f%%", percent),
                title: "其他任务", detailValue: compactTokens(tokens), unit: "Token",
                footnote: "其余任务合计", members: remainder,
                facts: [StripFact(label: "合并任务", value: "\(remainder.count) 个")]))
        }
        if ranked.isEmpty {
            result.append(StripItem(id: "empty", caption: "暂无任务", value: "—", title: "暂无任务用量",
                detailValue: "—", unit: "Token", footnote: "本周暂无可识别记录"))
        }
        return result
    }
}

@MainActor
final class StripInteraction: ObservableObject {
    @Published var expanded: String?
    @Published var locked = false
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

    func dismiss() {
        pending?.cancel()
        hovered = nil
        zones.removeAll()
        locked = false
        expanded = nil
        onResize?()
    }
}

enum StripInk {
    static let primary = Color(red: 0.12, green: 0.16, blue: 0.14)
    static let secondary = Color(red: 0.30, green: 0.36, blue: 0.32)
}

struct SystemGlass: ViewModifier {
    @Environment(\.accessibilityReduceTransparency) var reduceTransparency
    @Environment(\.colorSchemeContrast) var contrast
    @Environment(\.colorScheme) var scheme
    let active: Bool
    let radius: CGFloat

    @ViewBuilder func body(content: Content) -> some View {
        let shape = RoundedRectangle(cornerRadius: radius, style: .continuous)
        if reduceTransparency || contrast == .increased {
            content.background(Palette.surface(scheme), in: shape)
                .overlay(shape.strokeBorder(Color.primary.opacity(active ? 0.2 : 0.1), lineWidth: 0.5))
        } else if #available(macOS 26.0, *) {
            content.glassEffect(.regular.tint(Color.white.opacity(active ? 0.72 : 0.60)), in: .rect(cornerRadius: radius))
        } else {
            content.background(Color.white.opacity(active ? 0.94 : 0.82), in: shape)
                .background(.thinMaterial, in: shape)
                .overlay(shape.strokeBorder(Color.white.opacity(active ? 0.5 : 0.28), lineWidth: 0.5))
        }
    }
}

struct UsageStrip: View {
    static let moduleWidth: CGFloat = 68
    static let headerHeight: CGFloat = 64
    static let diameter: CGFloat = 64
    static let gap: CGFloat = 0
    static let inset: CGFloat = 3
    static let rowInset: CGFloat = 2
    static func detailHeight(for item: StripItem) -> CGFloat {
        150 + CGFloat(item.facts.count) * 36 + (item.members.isEmpty ? 0 : 100)
    }
    @ObservedObject var model: UsageModel
    @ObservedObject var interaction: StripInteraction
    @Environment(\.accessibilityReduceMotion) var reduceMotion
    @Environment(\.colorScheme) var scheme
    let vertical: Bool
    let height: CGFloat
    var items: [StripItem] { StripItem.items(model.snapshot, failed: model.error != nil) }

    var body: some View {
        Group {
            if vertical {
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
                            .onHover { interaction.hover(item.id, inside: $0, zone: "column") }
                        }
                    }.padding(Self.inset)
                        .modifier(SystemGlass(active: false, radius: Self.diameter / 2 + Self.inset))
                }.scrollIndicators(.hidden)
            } else {
                ZStack(alignment: .topLeading) {
                    // Decorative glass never intercepts clicks on the module buttons.
                    Color.clear.frame(height: Self.headerHeight + 2 * Self.inset)
                        .modifier(SystemGlass(active: false, radius: Self.headerHeight / 2 + Self.inset))
                        .shadow(color: .black.opacity(0.08), radius: 12, y: 5)
                        .allowsHitTesting(false)
                    HStack(alignment: .top, spacing: 0) {
                        ForEach(items) { item in
                            let active = interaction.expanded == item.id
                            // One persistent button per column; expanding only adds its details.
                            VStack(spacing: 0) {
                                circle(item, foreground: active)
                                if active { details(item) }
                            }
                            .frame(width: Self.diameter)
                            .background {
                                if active {
                                    Color.clear.modifier(SystemGlass(active: true, radius: Self.diameter / 2))
                                        .allowsHitTesting(false)
                                }
                            }
                            .clipShape(RoundedRectangle(cornerRadius: Self.diameter / 2, style: .continuous))
                            .shadow(color: .black.opacity(active ? 0.16 : 0), radius: 16, y: 8)
                            .frame(width: Self.moduleWidth, alignment: .top)
                            .overlay(alignment: .topLeading) {
                                if item.id != items.first?.id {
                                    Rectangle().fill(StripInk.primary.opacity(0.10))
                                        .frame(width: 0.5, height: 24).offset(y: 20)
                                        .allowsHitTesting(false)
                                }
                            }
                            .zIndex(active ? 10 : 0)
                            .onHover { interaction.hover(item.id, inside: $0, zone: "column:\(item.id)") }
                        }
                    }.padding(Self.inset)
                }
            }
        }
        .frame(height: height, alignment: .top)
        .frame(maxWidth: .infinity, alignment: .topLeading)
        .background(Color.clear.contentShape(Rectangle()).onTapGesture { interaction.dismiss() })
        .animation(reduceMotion ? nil : .smooth(duration: 0.24), value: interaction.expanded)
        .contextMenu {
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

    func circle(_ item: StripItem, foreground: Bool) -> some View {
        Button { interaction.click(item.id) } label: {
            VStack(spacing: 4) {
                Text(item.caption).font(.system(size: 10, weight: .medium)).foregroundStyle(StripInk.secondary)
                    .lineLimit(1).truncationMode(.middle).frame(width: Self.diameter - 8)
                Text(item.value).font(.system(size: 16, weight: .medium, design: .rounded))
                    .monospacedDigit().foregroundStyle(StripInk.primary).minimumScaleFactor(0.8).lineLimit(1)
            }
            .frame(width: Self.diameter, height: Self.diameter)
            .background(Circle().fill(Color.white.opacity(foreground ? 0 : 0.035)))
            .overlay(Circle().strokeBorder(Color.white.opacity(foreground ? 0 : 0.18), lineWidth: 0.5))
            .contentShape(Circle())
        }.buttonStyle(.plain)
            .accessibilityLabel(item.id.hasPrefix("folder:") ? "\(item.title)，本周占比 \(item.value)" : "\(item.title)，\(item.caption) \(item.value)")
            .help("悬停查看详情，点击保持展开；右键选择展示形式")
    }

    func details(_ item: StripItem) -> some View {
        VStack(spacing: 10) {
            Rectangle().fill(StripInk.primary.opacity(0.12)).frame(width: 26, height: 0.5)
            Text(item.title).font(.system(size: 11, weight: .semibold))
                .lineLimit(3).multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true).frame(maxWidth: .infinity)
            if item.id != "quota" {
                VStack(spacing: 4) {
                    Text(item.detailValue).font(.system(size: 16, weight: .medium, design: .rounded))
                        .monospacedDigit().lineLimit(1).minimumScaleFactor(0.8)
                    Text(item.unit).font(.system(size: 10)).foregroundStyle(StripInk.secondary)
                }
            }
            if !item.facts.isEmpty {
                VStack(spacing: 10) {
                    ForEach(item.facts) { fact in
                        VStack(spacing: 4) {
                            Text(fact.label).font(.system(size: 10)).foregroundStyle(StripInk.secondary)
                            Text(fact.value.replacingOccurrences(of: " Token", with: "\nToken"))
                                .font(.system(size: 11, weight: .medium)).monospacedDigit()
                                .multilineTextAlignment(.center).lineLimit(2)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
            }
            if !item.members.isEmpty {
                ScrollView {
                    VStack(spacing: 10) {
                        ForEach(item.members) { member in
                            VStack(spacing: 3) {
                                Text(member.name).lineLimit(3)
                                Text(String(format: "%.1f%%", member.percent)).monospacedDigit().foregroundStyle(StripInk.secondary)
                            }.font(.system(size: 10)).multilineTextAlignment(.center)
                        }
                    }.frame(maxWidth: .infinity)
                }.frame(height: 100)
            }
            if !item.id.hasPrefix("folder:") {
                Text(item.footnote).font(.system(size: 10)).foregroundStyle(StripInk.secondary)
                    .multilineTextAlignment(.center).lineLimit(3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 7).padding(.top, 8).padding(.bottom, 26)
        .foregroundStyle(StripInk.primary)
        .frame(width: Self.diameter, height: Self.detailHeight(for: item), alignment: .top)
        .contentShape(Rectangle()).onTapGesture { }
    }
}
