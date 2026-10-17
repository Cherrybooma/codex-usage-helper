import Foundation

@main
struct PeriodSettingsChecks {
    @MainActor static func main() throws {
        var passed = 0
        func check(_ condition: @autoclosure () -> Bool, _ name: String) {
            guard condition() else { fatalError("FAIL: \(name)") }
            passed += 1
            print("PASS: \(name)")
        }
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let usage = UsageModel(root: root)
        let initial = ProjectSettingsModel(usage: usage)
        check(!initial.hasBillingAnchor, "未知续费时间默认未确认，不将今天当作账期起点")
        check(!FileManager.default.fileExists(atPath: root.appendingPathComponent("widget/data/app-settings.json").path), "打开设置不生成续费配置")
        check(BillingAnchorSettings.decode("2026-10-01T12:00:00") == nil, "没有时区的时间不能作为已确认续费锚点")

        let exact = BillingAnchorSettings.decode("2026-09-30T20:15:00+08:00")!
        let now = BillingAnchorSettings.decode("2026-10-14T00:00:00Z")!
        let dateValues = try BillingAnchorSettings.values(anchor: exact, precision: .date, now: now)
        check(dateValues["billing_anchor"] as? String == "2026-09-29T16:00:00Z", "只知道日期时按上海时间当天零点归一化")
        check(dateValues["billing_anchor_precision"] as? String == "date", "日期边界显式保留估算精度")
        check(dateValues["billing_timezone"] as? String == "Asia/Shanghai", "账期时区明确保存为上海")
        let exactValues = try BillingAnchorSettings.values(anchor: exact, precision: .datetime, now: now)
        check(exactValues["billing_anchor"] as? String == "2026-09-30T12:15:00Z", "已知具体时间转换为等价UTC时刻而不改边界")
        check(exactValues["billing_anchor_precision"] as? String == "datetime", "具体时间保留datetime精度")
        do {
            _ = try BillingAnchorSettings.values(anchor: now.addingTimeInterval(3600), precision: .datetime, now: now)
            check(false, "未来时间不能作为最近一次已发生续费")
        } catch { check(true, "未来时间不能作为最近一次已发生续费") }

        try WidgetPreferences.update(root: root, values: ["notion_sync_enabled": true, "future_setting": "keep"])
        try WidgetPreferences.update(root: root, values: dateValues)
        let restored = ProjectSettingsModel(usage: usage)
        check(restored.hasBillingAnchor && restored.billingAnchorPrecision == .date, "已保存日期与精度能恢复到设置")
        check(restored.billingAnchorDate == BillingAnchorSettings.decode("2026-09-29T16:00:00Z")!, "恢复不会使用电脑当前时区改变续费日期")
        try WidgetPreferences.update(root: root, values: BillingAnchorSettings.values(anchor: nil, precision: .date, now: now))
        let cleared = try WidgetPreferences.read(root: root)
        check(cleared["billing_anchor"] is NSNull && cleared["billing_anchor_precision"] is NSNull, "清除锚点同时清除精度，不保留虚假账期")
        check(cleared["future_setting"] as? String == "keep" && WidgetPreferences.notionEnabled(in: cleared), "保存或清除账期不修改其他设置及Notion授权")
        check(!ProjectSettingsModel(usage: usage).hasBillingAnchor, "清除后回到未确认状态")

        restored.accountingReport = ["weekly": ["current": ["start": "2026-10-11T16:00:00Z", "end": "2026-10-18T16:00:00Z", "totals": ["total_tokens": 12500]]], "monthly": ["status": "needs_anchor"]]
        check(restored.periodTokens(restored.accountingWeek) == "12.5K Token", "周期展示读取同一账本的真实Token总数")
        check(restored.monthlyNeedsAnchor && restored.periodTokens(restored.accountingMonth) == "尚未核算", "没有月锚点显示待配置，不用零Token替代缺失数据")
        check(restored.periodDates(restored.accountingWeek) == "10月12日 00:00 — 10月19日 00:00（不含结束时刻）", "日期显示上海时间及半开区间边界")
        check(restored.periodTokens(["totals": ["total_tokens": true]]) == "尚未核算", "布尔值不能被误显示为Token计数")
        check(restored.periodCoverage(["coverage": ["quality": ["response_records_verified", "legacy_estimated"]]]) == "含旧格式估算", "混合格式不把所有Token标为已核实响应")
        check(restored.periodCoverage(["coverage": ["quality": ["partial_unreadable", "no_observed_usage"]]]) == "有记录未能识别或读取 · 窗口内未记录可用用量", "无法识别数据和窗口内无用量分别展示")
        check(restored.periodCoverage(["coverage": ["quality": ["response_records_verified"]]]) == "按响应记录统计", "已识别响应展示明确统计口径")
        let oldSnapshot = """
        {"generated_at":"2026-10-07T06:00:00Z","total_tokens":1000,"request_records":22,
         "legacy_updates":0,"tasks":[],"other_tasks":[]}
        """
        let decoded = try JSONDecoder().decode(Snapshot.self, from: Data(oldSnapshot.utf8))
        check(decoded.total_tokens == 1000, "历史缓存没有周期字段也能继续显示项目用量")
        let importRoot = root.appendingPathComponent("Projects")
        let importFolder = importRoot.appendingPathComponent("素材目录")
        try FileManager.default.createDirectory(at: importFolder, withIntermediateDirectories: true)
        let manifestURL = root.appendingPathComponent("project-import.json")
        try JSONSerialization.data(withJSONObject: ["version": 1, "project": ["name": "素材目录"]]).write(to: manifestURL)
        let importCommand = try ProjectImportSelection.command(manifestURL: manifestURL, folder: importFolder,
            taskRoot: importRoot, parentPage: "existing-home", existingHome: true)
        check(importCommand["management_home"] as? String == "existing-home" && importCommand["parent_page"] == nil,
            "已有管理主页直接复用，不创建第二个入口")
        check(importCommand["folder"] as? String == importFolder.resolvingSymlinksInPath().path,
            "一键导入保留独立本机文件夹身份")
        check(importCommand["manifest"] is [String: Any] && importCommand["token"] == nil,
            "导入stdin携带管理清单，不携带认证Token")
        let createHomeCommand = try ProjectImportSelection.command(manifestURL: manifestURL, folder: importFolder,
            taskRoot: importRoot, parentPage: "shared-container", existingHome: false)
        check(createHomeCommand["parent_page"] as? String == "shared-container" && createHomeCommand["management_home"] == nil,
            "首次没有管理主页时使用已共享容器建立入口")
        let nested = importFolder.appendingPathComponent("repo")
        try FileManager.default.createDirectory(at: nested, withIntermediateDirectories: true)
        do {
            _ = try ProjectImportSelection.command(manifestURL: manifestURL, folder: nested,
                taskRoot: importRoot, parentPage: "", existingHome: true)
            check(false, "内层仓库不能无意绑定整个上级统计任务")
        } catch { check(true, "内层仓库不能无意绑定整个上级统计任务") }
        try Data(repeating: 32, count: 256 * 1024 + 1).write(to: manifestURL)
        do {
            _ = try ProjectImportSelection.command(manifestURL: manifestURL, folder: importFolder,
                taskRoot: importRoot, parentPage: "", existingHome: true)
            check(false, "超大清单在原生入口拒绝")
        } catch { check(true, "超大清单在原生入口拒绝") }
        try WidgetPreferences.update(root: root, values: ["capsule_project_ids": ["old-pin", "missing"]])
        let oldPinSettings = UsageModel(root: root)
        oldPinSettings.setWidgetStyle(.white)
        let afterOldPins = try WidgetPreferences.read(root: root)
        check(afterOldPins["future_setting"] as? String == "keep" && WidgetPreferences.notionEnabled(in: afterOldPins),
            "旧固定项目配置不会破坏外观、账期或同步授权")
        check(oldPinSettings.widgetStyle == .white, "保留旧配置时外观仍可保存，不再读取项目固定顺序")
        let rankingData = Data("""
        {"generated_at":"2026-10-15T06:00:00Z","total_tokens":100,"request_records":1,"legacy_updates":0,
         "tasks":[{"task_id":"leading","name":"真实用量","total_tokens":100,"percent":100,"request_records":1}],"other_tasks":[],
         "management_projects":[
          {"task_id":"old-pin","name":"旧固定项","total_tokens":0,"percent":0,"request_records":0},
          {"task_id":"leader","task_ids":["leading"],"name":"真实领先项目","total_tokens":0,"percent":0,"request_records":0}]}
        """.utf8)
        oldPinSettings.snapshot = try JSONDecoder().decode(Snapshot.self, from: rankingData)
        check(StripItem.items(oldPinSettings.snapshot, managementEnabled: oldPinSettings.notionEnabled)[2].title == "真实领先项目",
            "应用保留capsule_project_ids旧值时第三入口仍为真实用量最多项目")
        print("\(passed) period settings checks passed")
    }
}
