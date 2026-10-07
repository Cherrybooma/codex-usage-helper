import Foundation

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
        let items = StripItem.items(fixture)
        check(items[2].footnote == "6 个会话", "展示真实会话数，不误用响应数")
        check(items[2].caption == "用量小助手", "顶部直接显示任务名称")
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

        let interaction = StripInteraction()
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
        print("\(passed) UI checks passed")
    }
}
