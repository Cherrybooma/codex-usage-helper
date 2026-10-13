import Foundation
import AppKit

final class EditCommandRecorder: NSObject {
    var pasteCount = 0
    @objc func paste(_ sender: Any?) { pasteCount += 1 }
}

@main
struct StripChecks {
    @MainActor static func main() throws {
        var passed = 0
        func check(_ condition: @autoclosure () -> Bool, _ name: String) {
            guard condition() else { fatalError("FAIL: \(name)") }
            passed += 1
            print("PASS: \(name)")
        }
        func drain(_ seconds: Double) {
            let end = Date().addingTimeInterval(seconds)
            while Date() < end { RunLoop.main.run(until: Date().addingTimeInterval(0.01)) }
        }
        let temporaryRoot = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: temporaryRoot) }
        let local = UsageModel(root: temporaryRoot)
        check(!local.notionEnabled, "未配置时默认本机模式")
        local.syncNotion(force: true)
        check(!local.notionSyncing && local.notionSyncMessage?.contains("本机模式") == true, "手动强制同步在本机模式也不会启动进程")
        let disabledSettings = ProjectSettingsModel(usage: local)
        disabledSettings.perform(["command": "setup", "parent_page": "fixture"])
        check(!disabledSettings.busy && disabledSettings.message.contains("本机模式"), "本机模式不能从设置绕过连接开关")
        local.setNotionEnabled(true)
        try WidgetPreferences.update(root: temporaryRoot, values: ["task_root": "/tmp/example", "future_setting": "keep"])
        let reloaded = UsageModel(root: temporaryRoot)
        check(reloaded.notionEnabled, "启用同步后设置持久化，保存项目根目录不会抹掉开关")
        reloaded.setNotionEnabled(false)
        let preferences = try WidgetPreferences.read(root: temporaryRoot)
        check(preferences["task_root"] as? String == "/tmp/example" && preferences["future_setting"] as? String == "keep", "关闭同步保留目录及其他本地设置")
        check(!UsageModel(root: temporaryRoot).notionEnabled, "重启后保留本机模式")
        let invalidValues: [Any] = [1, "true", NSNull()]
        check(invalidValues.allSatisfy { !WidgetPreferences.notionEnabled(in: ["notion_sync_enabled": $0]) }, "数值或字符串不能被误当成同步授权")
        let settingsFile = temporaryRoot.appendingPathComponent("widget/data/app-settings.json")
        let broken = Data("[]".utf8)
        try broken.write(to: settingsFile)
        let brokenModel = UsageModel(root: temporaryRoot)
        brokenModel.setNotionEnabled(true)
        check(!brokenModel.notionEnabled && (try? Data(contentsOf: settingsFile)) == broken, "设置损坏时不覆盖原文件或启用同步")

        _ = NSApplication.shared
        let mainMenu = WidgetApplicationMenu.make()
        let edit = mainMenu.items.first(where: { $0.title == "编辑" })!.submenu!
        check(edit.items.allSatisfy { $0.target == nil }, "编辑命令交给当前输入框响应链")
        let keys = ["x": "cut:", "c": "copy:", "v": "paste:", "a": "selectAll:"]
        check(keys.allSatisfy { key, action in edit.items.contains { $0.keyEquivalent == key && $0.action == Selector(action) && $0.keyEquivalentModifierMask == [.command] } }, "原生编辑菜单包含正确的剪切复制粘贴全选快捷键")
        let recorder = EditCommandRecorder()
        let pasteItem = edit.items.first(where: { $0.keyEquivalent == "v" })!
        pasteItem.target = recorder
        let pasteEvent = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [.command],
            timestamp: 0, windowNumber: 0, context: nil, characters: "v", charactersIgnoringModifiers: "v", isARepeat: false, keyCode: 9)!
        check(edit.performKeyEquivalent(with: pasteEvent) && recorder.pasteCount == 1, "真实菜单快捷键分派 Cmd+V；测试不读取剪贴板或凭据")
        pasteItem.target = nil
        func snapshot(_ taskJSON: String, other: String = "[]", quota: String = "null", legacyUpdates: Int = 0, budget: String = "null") throws -> Snapshot {
            let json = """
            {"generated_at":"2026-10-07T06:00:00Z","total_tokens":1000,
             "request_records":22,"legacy_updates":\(legacyUpdates),"tasks":\(taskJSON),
             "other_tasks":\(other),"quota":\(quota),"weekly_budget":\(budget)}
            """
            return try JSONDecoder().decode(Snapshot.self, from: Data(json.utf8))
        }
        let fixture = try snapshot("""
        [{"name":"用量小助手","total_tokens":800,"percent":80,"request_records":20,"sessions_count":6},
         {"name":"Codex","total_tokens":200,"percent":20,"request_records":2,"sessions_count":1}]
        """)
        let hitRect = CGRect(x: 10, y: 10, width: 64, height: 400)
        check(pointInCapsule(CGPoint(x: 42, y: 11), rect: hitRect), "竖胶囊顶部中心接收鼠标")
        check(!pointInCapsule(CGPoint(x: 11, y: 11), rect: hitRect), "胶囊圆角的透明部分不接收鼠标")
        check(pointInCapsule(CGPoint(x: 11, y: 200), rect: hitRect), "胶囊主体仍可交互")
        check(!pointInCapsule(CGPoint(x: 42, y: 420), rect: hitRect), "预留详情外的透明区域穿透鼠标")
        let items = StripItem.items(fixture)
        check(items[2].footnote == "6 个会话", "展示真实会话数，不误用响应数")
        check(items[2].caption == "用量小助手", "完整任务名保留在详情和可访问性语义")
        check(items[2].detailValue == "800" && items[2].unit == "Token", "任务详情显示 Token")
        check(items[2].facts.first(where: { $0.label == "本周排名" })?.value == "第 1 名", "详情排名按全部任务的用量计算")
        check(items[2].facts.first(where: { $0.label == "模型响应" })?.value == "20 次", "详情区分响应次数与会话数")
        check(items[2].facts.first(where: { $0.label == "平均/响应" })?.value == "40 Token", "平均用量使用真实响应数作分母")
        let mixed = try snapshot("""
        [{"name":"混合历史","total_tokens":1000,"percent":100,"request_records":2}]
        """, legacyUpdates: 3)
        check(!StripItem.items(mixed)[2].facts.contains(where: { $0.label == "平均/响应" }), "含旧累计估算时不误算每次响应平均")
        check(items[0].value == "—", "缺失额度不显示剩余 100%")
        let legacy = try snapshot("""
        [{"name":"旧缓存","total_tokens":1000,"percent":100,"request_records":22}]
        """)
        check(StripItem.items(legacy)[2].footnote == "会话数未记录", "旧缓存兼容且不猜测会话数")
        let lots = try snapshot("""
        [{"name":"A","total_tokens":500,"percent":50,"request_records":10},
         {"name":"B","total_tokens":200,"percent":20,"request_records":4},
         {"name":"C","total_tokens":120,"percent":12,"request_records":3},
         {"name":"D","total_tokens":80,"percent":8,"request_records":2},
         {"name":"E","total_tokens":60,"percent":6,"request_records":2}]
        """, other: """
        [{"name":"F","total_tokens":40,"percent":4,"request_records":1}]
        """)
        let crowded = StripItem.items(lots)
        check(crowded.count == 6, "多任务时横条最多六模块")
        check(crowded.last!.members.map(\.name) == ["D", "E", "F"], "更多任务保留全部尾部任务")
        check(crowded.last!.value == "18.0%" && crowded.last!.detailValue == "180", "尾部合计占比和 Token 使用同一分母")
        let smallLeaders = try snapshot("""
        [{"name":"主项目","total_tokens":900,"percent":90,"request_records":10}]
        """, other: """
        [{"name":"第二项目","total_tokens":45,"percent":4.5,"request_records":1},
         {"name":"第三项目","total_tokens":35,"percent":3.5,"request_records":1},
         {"name":"第四项目","total_tokens":20,"percent":2,"request_records":1}]
        """)
        let topThree = StripItem.items(smallLeaders)
        check(topThree.filter { $0.id.hasPrefix("folder:") }.map(\.title) == ["主项目", "第二项目", "第三项目"], "低于5%的前三任务仍显示在顶部")
        check(topThree.last!.members.map(\.name) == ["第四项目"] && topThree.last!.detailValue == "20", "其他只包含前三任务之外的用量")
        check(topThree.dropFirst(2).reduce(0) { $0 + Int($1.detailValue)! } == smallLeaders.total_tokens, "顶部任务与其他合计不重复不遗漏")
        let fourMajor = try snapshot("""
        [{"name":"A","total_tokens":500,"percent":50,"request_records":10},
         {"name":"B","total_tokens":250,"percent":25,"request_records":4},
         {"name":"C","total_tokens":150,"percent":15,"request_records":3},
         {"name":"D","total_tokens":100,"percent":10,"request_records":2}]
        """)
        check(StripItem.items(fourMajor).filter { $0.id.hasPrefix("folder:") }.count == 3 && StripItem.items(fourMajor).last!.members.first?.name == "D", "无小任务时也只展示前三个任务")
        let quota = """
        {"observed_at":"2026-10-07T06:00:00Z","stale":true,"windows":[
         {"key":"primary","label":"5小时","used_percent":50,"remaining_percent":50,"duration_mins":300,"resets_at":9999999999},
         {"key":"secondary","label":"周","used_percent":10,"remaining_percent":90,"duration_mins":10080,"resets_at":9999999999}]}
        """
        let cached = try snapshot("[]", quota: quota)
        check(StripItem.items(cached)[0].value == "≈90%", "显示周窗口且标记过期缓存")
        let budget = """
        {"status":"incomplete","observed_total_percent":36,"excess_percent":null,"reset_count":1,"notes":["记录不完整"]}
        """
        let calendar = StripItem.items(try snapshot("[]", quota: quota, budget: budget))[0]
        check(calendar.facts.first(where: { $0.label == "当前已用" })?.value == "≈10%" && calendar.facts.first(where: { $0.label == "本周已用" })?.value == "≈36.0%", "当前窗口已用与本周累计分别显示")
        check(calendar.facts.first(where: { $0.label == "本周超额" })?.value == "未超额" && calendar.footnote.contains("不完整"), "已记录未超额同时保留记录不完整说明")
        let excessBudget = """
        {"status":"incomplete","observed_total_percent":150,"excess_percent":50,"reset_count":1,"notes":["记录不完整"]}
        """
        let excess = StripItem.items(try snapshot("[]", budget: excessBudget))[0]
        check(excess.facts.first(where: { $0.label == "本周超额" })?.value == "≥50.0%", "已确认超额显示观测下限")
        let ambiguousBudget = """
        {"status":"incomplete","observed_total_percent":150,"excess_percent":null,"reset_count":1,"notes":["原因未确认"]}
        """
        let ambiguous = StripItem.items(try snapshot("[]", budget: ambiguousBudget))[0]
        check(ambiguous.facts.first(where: { $0.label == "本周超额" })?.value == "待核实", "不从有歧义的累计强推超额")
        check(items[0].facts.first(where: { $0.label == "本周已用" })?.value == "尚无记录", "缺少周记录时不误用当前窗口比例")
        let empty = try snapshot("[]")
        check(StripItem.items(empty).last!.id == "empty", "空用量状态可解释")

        let linked = try snapshot("""
        [{"name":"本地文件夹","task_id":"task-stable-001","display_name":"用量小助手：这是一个改名后依然应完整显示的长项目名称",
          "total_tokens":1000,"percent":100,"request_records":22,"sessions_count":6,
          "project_status":"开发中","icon_type":"emoji","icon_value":"📘",
          "notion_url":"https://www.notion.so/abcdef0123456789abcdef0123456789","notion_stale":true}]
        """)
        let linkedItem = StripItem.items(linked)[2]
        check(linkedItem.id == "folder:task-stable-001", "项目改名使用稳定任务 ID")
        check(linkedItem.title == linked.tasks[0].display_name && linkedItem.title.count > 20, "长项目名称完整保留，不截断计量身份")
        check(linkedItem.projectStatus == "开发中" && linkedItem.notionStale, "Notion 状态与离线旧缓存明确区分")
        check(linkedItem.avatar == .emoji("📘") && linkedItem.notionURL?.contains("notion.so") == true, "使用用户 Notion 头像与项目链接")
        check(StripAvatar.initials(for: " 用量小助手 ") == "用" && StripAvatar.initials(for: "IP 项目") == "IP", "缺少头像时按名称生成首字")
        check(StripAvatar.initials(for: " ") == "项", "空名称仍有可解释默认头像")
        let remote = try snapshot("""
        [{"name":"远程头像","total_tokens":1000,"percent":100,"request_records":22,"icon_type":"external","icon_value":"https://example.com/avatar.png"}]
        """)
        check(StripItem.items(remote)[2].avatar == .remote("https://example.com/avatar.png"), "支持 Notion HTTPS 项目 Logo")
        let customAvatar = try snapshot("""
        [{"name":"自定义头像","total_tokens":1000,"percent":100,"request_records":22,"icon_type":"custom_emoji","icon_value":"https://example.com/custom.png"}]
        """)
        check(StripItem.items(customAvatar)[2].avatar == .remote("https://example.com/custom.png"), "支持 Notion custom_emoji 的 HTTPS 头像")
        let unsafe = try snapshot("""
        [{"name":"安全头像","total_tokens":1000,"percent":100,"request_records":22,"icon_type":"external","icon_value":"file:///Users/user/private.png"}]
        """)
        check(StripItem.items(unsafe)[2].avatar == .initials("安"), "不读取头像字段中的任意本地文件")
        let freshQuota = quota.replacingOccurrences(of: "\"stale\":true", with: "\"stale\":false")
        let switching = try snapshot("""
        [{"name":"A","total_tokens":800,"percent":80,"request_records":20},
         {"name":"B","total_tokens":200,"percent":20,"request_records":2}]
        """, quota: freshQuota, budget: excessBudget)
        check(StripItem.items(switching, metric: .shortRemaining)[0].value == "50%", "滚轮指标读取实际五小时窗口")
        check(StripItem.items(switching, metric: .weeklyObserved)[0].value == "≈150.0%", "周累计与当前窗口分别显示")
        check(StripItem.items(switching, metric: .weeklyExcess)[0].value == "≥50.0%", "超额显示观测下限而非新造额度")
        check(StripItem.items(fixture, metric: .shortRemaining)[0].value == "—", "缺少短周期额度不补零")
        check(StripItem.items(fixture, metric: .weeklyObserved)[0].value == "—", "缺少累计观测不补零")
        let baselineProjects = StripItem.items(switching).dropFirst(2).map { "\($0.id):\($0.value)" }
        check(StripQuotaMetric.allCases.allSatisfy {
            StripItem.items(switching, metric: $0).dropFirst(2).map { "\($0.id):\($0.value)" } == baselineProjects
        }, "切换额度指标不改变项目排行和占比分母")
        let tinyQuota = freshQuota.replacingOccurrences(of: "\"used_percent\":10,\"remaining_percent\":90", with: "\"used_percent\":99.6,\"remaining_percent\":0.4")
        let tinyItem = StripItem.items(try snapshot("[]", quota: tinyQuota))[0]
        check(tinyItem.value == "<1%" && tinyItem.footnote != "当前窗口已用尽", "小于1%的正剩余额度不四舍五入成耗尽")
        let depleted = freshQuota.replacingOccurrences(of: "\"used_percent\":10,\"remaining_percent\":90", with: "\"used_percent\":100,\"remaining_percent\":0")
        let depletedItem = StripItem.items(try snapshot("[]", quota: depleted))[0]
        check(depletedItem.footnote == "当前窗口已用尽", "合法零剩余标记耗尽，区别读取失败")
        let expired = StripItem.items(try snapshot("[]", quota: freshQuota), now: Date(timeIntervalSince1970: 9999999999))
        check(expired[0].value == "≈90%" && expired[0].footnote.contains("等待更新"), "超过重置时间的读数不能冒充当前额度")
        check(StripQuotaMetric.weeklyRemaining.advanced(by: -1) == .weeklyExcess && StripQuotaMetric.weeklyExcess.advanced(by: 1) == .weeklyRemaining, "四个额度指标双向循环")
        check(UsageStrip.naturalSize(itemCount: 5).height == UsageStrip.naturalSize(itemCount: 6).height, "项目增加只改变横条宽度，不移动顶部锚点")
        check(UsageStrip.detailHeight(for: linkedItem) <= UsageStrip.maxDetailHeight, "长名称详情使用固定最大预算内滚动")

        let interaction = StripInteraction()
        interaction.click("A")
        interaction.cycleQuota(steps: 1)
        check(interaction.quotaMetric == .shortRemaining && interaction.expanded == "A" && interaction.locked, "切换额度不关闭或切换锁定的项目详情")
        interaction.dismiss()
        interaction.hover("A", inside: true)
        check(interaction.expanded == nil, "快速经过不立刻展开")
        drain(0.22)
        check(interaction.expanded == "A" && !interaction.locked, "停留后悬停展开")
        interaction.hover("A", inside: false)
        interaction.hover("A", inside: true)
        drain(0.28)
        check(interaction.expanded == "A", "进入同一卡片取消延迟收起")
        interaction.hover("B", inside: true)
        drain(0.22)
        check(interaction.expanded == "B", "切换模块只展开当前详情")
        interaction.hover("B", inside: false)
        drain(0.28)
        check(interaction.expanded == nil, "离开后自动收起")
        interaction.click("A")
        interaction.hover("B", inside: true)
        drain(0.22)
        check(interaction.expanded == "A" && interaction.locked, "点击保持展开，不被路过鼠标切换")
        interaction.click("B")
        check(interaction.expanded == "B" && interaction.locked, "点击另一模块切换保持的详情")
        interaction.click("B")
        check(interaction.expanded == nil && !interaction.locked, "再次点击收起")
        interaction.hover("A", inside: true)
        interaction.dismiss()
        drain(0.28)
        check(interaction.expanded == nil && !interaction.locked, "关闭取消尚未执行的悬停任务")
        for source in ["total", "folder:用量小助手", "folder:Codex", "folder:示例项目", "remainder"] {
            interaction.click(source)
            interaction.hover(source, inside: true, zone: "column:\(source)")
            interaction.click("quota")
            drain(0.28)
            check(interaction.expanded == "quota" && interaction.locked, "从\(source)切回周额度仍保持展开")
            interaction.dismiss()
        }
        let enabled: Set<WidgetKind> = [.horizontal, .vertical, .box]
        let decoded = try JSONDecoder().decode(Set<WidgetKind>.self, from: JSONEncoder().encode(enabled))
        check(decoded == enabled, "三个独立开关可同时开启并保存")
        interaction.hover("A", inside: true, zone: "row")
        drain(0.22)
        interaction.hover("A", inside: true, zone: "capsule")
        interaction.hover("A", inside: false, zone: "row")
        drain(0.28)
        check(interaction.expanded == "A", "前景胶囊覆盖原圆形后仍保持悬停")
        interaction.hover("A", inside: false, zone: "capsule")
        drain(0.28)
        check(interaction.expanded == nil, "离开前景胶囊后正常收起")
        interaction.hover("A", inside: true, zone: "row")
        interaction.hover("B", inside: true, zone: "row")
        interaction.hover("A", inside: false, zone: "row")
        drain(0.28)
        check(interaction.expanded == "B", "旧圆形退出事件不会清除新圆形悬停")
        interaction.leaveAllHoverZones()
        drain(0.28)
        check(interaction.expanded == nil, "透明区域停止鼠标事件后，原生指针清除旧悬停区")
        interaction.click("A")
        interaction.leaveAllHoverZones()
        drain(0.28)
        check(interaction.expanded == "A" && interaction.locked, "离开透明区域保留点击锁定的详情")
        interaction.dismiss()
        interaction.hover("A", inside: true, zone: "row")
        interaction.leaveAllHoverZones()
        interaction.hover("B", inside: true, zone: "native-pointer")
        drain(0.28)
        check(interaction.expanded == "B", "重新进入真实模块取消透明区域延迟收起")
        print("\(passed) UI checks passed")
    }
}
