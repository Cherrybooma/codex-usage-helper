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
        check(local.widgetStyle == .glass, "未配置外观时使用系统玻璃材质")
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
        reloaded.setWidgetStyle(.pulse)
        check(UsageModel(root: temporaryRoot).widgetStyle == .pulse, "原生外观选择持久化，重启后保留 Pulse 暗色")
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
        brokenModel.setWidgetStyle(.white)
        check(brokenModel.widgetStyle == .glass && (try? Data(contentsOf: settingsFile)) == broken, "损坏设置不能因换主题被覆盖")

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
        func snapshot(_ taskJSON: String, other: String = "[]", quota: String = "null", legacyUpdates: Int = 0, budget: String = "null", todayTokens: Int? = nil) throws -> Snapshot {
            let today = todayTokens.map { String($0) } ?? "null"
            let json = """
            {"generated_at":"2026-10-07T06:00:00Z","total_tokens":1000,"today_tokens":\(today),
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
        check(items[2].facts.first(where: { $0.label == "用量排名" })?.value == "第 1 名", "附属用量排名按全部任务的用量计算")
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
        let incompleteToday = try snapshot("[]", quota: quota, budget: budget, todayTokens: 123)
        check(StripQuotaMetric.allCases.allSatisfy { metric in
            let entries = StripItem.items(incompleteToday, metric: metric)
            return entries[0].todayTokens == 123 && entries[1].todayTokens == 123
        }, "Codex 和账本均绑定今日真实 Token；切换指标及不完整周卡重建不丢失记录")
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
        check(items[2].avatar == .symbol("folder") && items[2].needsProjectIcon, "未绑定项目只用文件夹图标占位，不用文字首字")
        check(linkedItem.needsProjectIcon == false, "真实 Notion 图标不提示补充图标")
        check(["🧭", "🎙️", "👩🏽‍💻", "1️⃣"].allSatisfy(StripAvatar.isEmojiIcon), "允许完整用户 emoji 图标，包括变体、肤色和组合图标")
        check(["IP", "用", "1", "🧭🌱", ""].allSatisfy { !StripAvatar.isEmojiIcon($0) }, "错误头像字段不能把项目字母或名称显示在常态胶囊")
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
        check(StripItem.items(unsafe)[2].avatar == .symbol("folder") && StripItem.items(unsafe)[2].needsProjectIcon, "不读取任意本地头像文件，也不回退文字首字")
        let rejectedAvatarURLs = ["https://fixture-user:fixture-password@example.com/icon.png", "https://fixture-user@example.com/icon.png", "http://example.com/icon.png", "https://example.com:0/icon.png", "https://example.com:65536/icon.png", "https://example.com/icon name.png", "https://example.com/icon\n.png", "https://", String(repeating: "x", count: 4097)]
        check(rejectedAvatarURLs.allSatisfy { StripAvatar.safeRemoteURL($0) == nil }, "旧缓存头像拒绝凭据、HTTP、非法端口、空白控制字符和超长地址")
        let signedAvatarURL = "https://assets.example.com/icon.png?X-Amz-Signature=fixture&X-Amz-Expires=3600"
        check(StripAvatar.safeRemoteURL(signedAvatarURL)?.absoluteString == signedAvatarURL, "合法 Notion HTTPS 签名头像完整保留查询参数")
        let userInfoAvatar = try snapshot("""
        [{"name":"旧缓存头像","total_tokens":1000,"percent":100,"request_records":22,"icon_type":"external","icon_value":"https://fixture-user:fixture-password@example.com/icon.png"}]
        """)
        check(StripItem.items(userInfoAvatar)[2].avatar == .symbol("folder") && StripItem.items(userInfoAvatar)[2].needsProjectIcon, "旧缓存内带用户名密码的头像掉回文件夹，不进入网络加载")
        let redirectConfig = URLSessionConfiguration.ephemeral
        redirectConfig.urlCredentialStorage = nil
        let redirectSession = URLSession(configuration: redirectConfig)
        defer { redirectSession.invalidateAndCancel() }
        let redirectURL = URL(string: signedAvatarURL)!
        let redirectTask = redirectSession.dataTask(with: redirectURL) // Never resumed; no network request.
        let redirectResponse = HTTPURLResponse(url: redirectURL, statusCode: 302, httpVersion: "HTTP/1.1", headerFields: [:])!
        let redirectDelegate = ProjectAvatarRedirects()
        var redirected: URLRequest?
        redirectDelegate.urlSession(redirectSession, task: redirectTask, willPerformHTTPRedirection: redirectResponse,
                                    newRequest: URLRequest(url: URL(string: "http://assets.example.com/icon.png")!)) { redirected = $0 }
        check(redirected == nil, "头像实际重定向回调拒绝从 HTTPS 降级到 HTTP")
        redirectDelegate.urlSession(redirectSession, task: redirectTask, willPerformHTTPRedirection: redirectResponse,
                                    newRequest: URLRequest(url: URL(string: "https://fixture-user:fixture-password@assets.example.com/icon.png")!)) { redirected = $0 }
        check(redirected == nil, "头像实际重定向回调拒绝带凭据的新地址")
        redirectDelegate.urlSession(redirectSession, task: redirectTask, willPerformHTTPRedirection: redirectResponse,
                                    newRequest: URLRequest(url: redirectURL)) { redirected = $0 }
        check(redirected?.url?.absoluteString == signedAvatarURL, "正常 HTTPS 头像重定向保留签名参数")
        let loopingDelegate = ProjectAvatarRedirects()
        for _ in 0..<9 {
            loopingDelegate.urlSession(redirectSession, task: redirectTask, willPerformHTTPRedirection: redirectResponse,
                                      newRequest: URLRequest(url: redirectURL)) { redirected = $0 }
        }
        check(redirected == nil, "头像重定向循环有上限，不无限跳转")
        disabledSettings.createEmoji = "IP"
        check(disabledSettings.newProjectIcon == nil, "创建项目时不能以字母冒充 Notion 头像")
        disabledSettings.createEmoji = "👩🏽‍💻"
        check(disabledSettings.newProjectIcon?["emoji"] as? String == "👩🏽‍💻", "创建项目可明确选择组合 emoji 头像")
        disabledSettings.createIconMode = "external"
        disabledSettings.createLogoURL = "https://fixture-user:fixture-password@example.com/icon.png"
        check(disabledSettings.newProjectIcon == nil, "创建项目 Logo 地址也拒绝凭据")
        disabledSettings.createLogoURL = signedAvatarURL
        check(disabledSettings.newProjectIcon?["type"] as? String == "external", "创建项目支持有效 HTTPS Logo，无需联网即可校验")
        let managed = try snapshot("""
        [{"name":"管理项目","total_tokens":1000,"percent":100,"request_records":22,"sessions_count":6,
          "icon_type":"emoji","icon_value":"🧭","notion_url":"https://www.notion.so/abcdef0123456789abcdef0123456789",
          "project_status":"开发中","weekly_goal":"完成胶囊看板","next_step":"核验任务联动","project_goal":"让项目进度随时可见",
          "target_date":"2026-10-18","project_priority":"高","project_health":"需关注",
          "open_tasks":3,"blocked_tasks":1,"completed_tasks":5,"project_summary":"本周正在完成联动","last_reviewed":"2026-10-13T12:00:00Z"}]
        """)
        let managedItem = StripItem.items(managed)[2]
        check(managedItem.managementFacts.prefix(3).map(\.label) == ["本周目标", "下一步", "项目目标"], "项目详情优先目标与行动，再展示附属用量")
        check(managedItem.managementFacts.first(where: { $0.label == "受阻任务" })?.value == "1 项", "真实任务阻塞计数进入项目详情")
        check(managedItem.managementFacts.first(where: { $0.label == "交付日期" })?.value == "2026-10-18", "交付日期来自项目管理字段，不从日志日期推算")
        check(managedItem.managementFacts.first(where: { $0.label == "优先级" })?.value == "高" && managedItem.managementFacts.first(where: { $0.label == "健康度" })?.value == "需关注", "管理优先级和健康度明确区分")
        check(managedItem.managementFacts.first(where: { $0.label == "最近复盘" })?.value == "2026-10-13", "最近复盘保留项目实际日期")
        check(StripItem.projectDate("2026-10-12T16:00:00Z") == "2026-10-13" && StripItem.projectDate("2026-10-12T16:00:00.500Z") == "2026-10-13", "项目时间转为北京时间，不能截取 UTC 前十位导致少一天")
        check(StripItem.projectDate("2026-10-18") == "2026-10-18", "纯日期截止时间保留 Notion 原日期，不做时区偏移")
        check(items[2].managementFacts.isEmpty, "旧缓存缺少项目字段时不编造目标或任务数")
        let zeroTasks = try snapshot("""
        [{"name":"空任务清单","total_tokens":0,"percent":0,"request_records":0,"open_tasks":0,"blocked_tasks":0,"completed_tasks":0}]
        """)
        check(StripItem.items(zeroTasks)[2].managementFacts.map(\.value) == ["0 项", "0 项", "0 项"], "已读取且为空的任务清单保留真实零，不混淆未读取")
        let managementOnly = """
        {"generated_at":"2026-10-13T06:00:00Z","total_tokens":1000,"request_records":22,"legacy_updates":0,
         "tasks":[{"name":"耗费最多","total_tokens":1000,"percent":100,"request_records":22}],"other_tasks":[],
         "management_projects":[
          {"name":"重要但未关联","task_id":"notion-1","total_tokens":0,"percent":0,"request_records":0,"usage_linked":false,"project_status":"开发中","weekly_goal":"完成产品定义","icon_type":"emoji","icon_value":"🎯"},
          {"name":"已关联但本周未使用","task_id":"notion-2","total_tokens":0,"percent":0,"request_records":0,"usage_linked":true,"sessions_count":0,"project_status":"规划中","icon_type":"emoji","icon_value":"🌱"},
          {"name":"第三项目","total_tokens":0,"percent":0,"request_records":0,"usage_linked":false},
          {"name":"第四项目","total_tokens":0,"percent":0,"request_records":0,"usage_linked":false,"project_status":"暂停"}]}
        """
        let managementSnapshot = try JSONDecoder().decode(Snapshot.self, from: Data(managementOnly.utf8))
        let managementItems = StripItem.items(managementSnapshot)
        check(managementItems[2].title == "重要但未关联" && managementItems[3].title == "已关联但本周未使用", "Notion 项目顺序优先于 Token 排行，并展示本周零用量项目")
        check(!managementItems[2].usageLinked && managementItems[2].detailValue == "—" && managementItems[2].facts.isEmpty, "未关联记录不伪装成零 Token 或零占比")
        check(managementItems[3].usageLinked && managementItems[3].detailValue == "0" && managementItems[3].facts.first?.value == "0.0%", "已关联零用量与未关联明确区分")
        check(!managementItems[2].facts.contains(where: { $0.label == "用量排名" }), "管理顺序不冒充 Token 用量排名")
        check(managementItems.last?.managementList == true && managementItems.last?.unit == "项目" && managementItems.last?.members.first?.project_status == "暂停", "更多管理项目展示状态，不相加未关联 Token")
        check(managementItems[1].detailValue == "1.0K", "引入项目管理列表不改变本机总用量")
        let progressSnapshot = try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"generated_at":"2026-10-14T02:00:00Z","total_tokens":1000,"today_tokens":300,"request_records":22,"legacy_updates":0,
         "tasks":[
          {"name":"开发项目","total_tokens":800,"today_tokens":240,"percent":80,"request_records":20,"development_progress":35,"progress_source":"manual"},
          {"name":"任务清单","total_tokens":200,"today_tokens":60,"percent":20,"request_records":2,"development_progress":50,"progress_source":"tasks"}],"other_tasks":[]}
        """.utf8))
        let progressItems = StripItem.items(progressSnapshot)
        check(progressItems[2].primaryRing == 0.8 && progressItems[2].secondaryRing == 0.35, "项目外环 Token 占比、内环开发进度来自不同真实字段")
        check(progressItems[2].progressLabel == "开发进度" && progressItems[3].progressLabel == "行动完成率", "手动开发百分比与任务行动完成率明确区分")
        check(progressItems[1].todayTokens == 300 && progressItems[2].todayTokens == 240, "今日总用量和项目今日用量保留真实独立值")
        check(items[2].secondaryRing == nil && items[2].todayTokens == nil, "未知开发进度和今日记录保持未知，不画零进度")
        check(StripItem.projectProgress(0, source: "manual") == 0 && StripItem.fraction(0) == 0, "真实零开发进度与未知值不同")
        check(StripItem.projectProgress(50, source: nil) == nil && StripItem.projectProgress(101, source: "manual") == nil && StripItem.projectProgress(-1, source: "tasks") == nil, "缺来源或越界进度不能冒充有效开发数据")
        let freshQuota = quota.replacingOccurrences(of: "\"stale\":true", with: "\"stale\":false")
        let switching = try snapshot("""
        [{"name":"A","total_tokens":800,"percent":80,"request_records":20},
         {"name":"B","total_tokens":200,"percent":20,"request_records":2}]
        """, quota: freshQuota, budget: excessBudget)
        check(StripItem.items(switching, metric: .shortRemaining)[0].value == "50%", "滚轮指标读取实际五小时窗口")
        check(StripItem.items(switching, metric: .weeklyUsed)[0].value == "10%" && StripItem.items(switching, metric: .weeklyUsed)[0].primaryRing == 0.1, "默认周额度已用读取官方周窗口，不误用自然周累计")
        check(StripItem.items(switching, metric: .weeklyRemaining)[0].primaryRing == 0.9 && StripItem.items(switching, metric: .shortRemaining)[0].primaryRing == 0.5, "额度环随所选指标切换，与详情数字同源")
        check(StripItem.items(switching, metric: .weeklyObserved)[0].primaryRing == 1 && StripItem.items(switching, metric: .weeklyObserved)[0].value == "≈150.0%", "超百分百观测环封顶但实际累计数字不被截断")
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
        check(StripQuotaMetric.weeklyRemaining.advanced(by: -1) == .weeklyExcess && StripQuotaMetric.weeklyExcess.advanced(by: 1) == .weeklyRemaining, "全部额度指标双向循环")
        check(UsageStrip.naturalSize(itemCount: 5).height == UsageStrip.naturalSize(itemCount: 6).height, "项目增加只改变横条宽度，不移动顶部锚点")
        check(UsageStrip.detailHeight(for: linkedItem) <= UsageStrip.maxDetailHeight, "长名称详情使用固定最大预算内滚动")
        let dashboardCanvas = UsageStrip.canvasSize(itemCount: 5)
        let firstBoard = UsageStrip.detailFrame(for: managedItem, index: 0, itemCount: 5, vertical: false, canvas: dashboardCanvas)
        let lastBoard = UsageStrip.detailFrame(for: managedItem, index: 4, itemCount: 5, vertical: false, canvas: dashboardCanvas)
        check(firstBoard.width == 328 && lastBoard.width == 328, "悬停详情升级为 328pt 宽看板，不再挤在64pt胶囊")
        check(firstBoard.minX >= UsageStrip.inset && lastBoard.maxX <= dashboardCanvas.width - UsageStrip.inset, "第一个和最后一个项目看板均在屏幕画布内夹紧")
        let headerBefore = UsageStrip.headerFrame(itemCount: 5, vertical: false, canvas: dashboardCanvas)
        check(firstBoard.minY > headerBefore.maxY && lastBoard.minY == firstBoard.minY, "上方胶囊展开下方看板时头部坐标不变")
        let bottomOffset = dashboardCanvas.height - headerBefore.height
        let bottomHeader = UsageStrip.headerFrame(itemCount: 5, vertical: false, canvas: dashboardCanvas, headerOffset: bottomOffset)
        let flippedBoard = UsageStrip.detailFrame(for: managedItem, index: 4, itemCount: 5, vertical: false, canvas: dashboardCanvas, headerOffset: bottomOffset)
        check(flippedBoard.maxY < bottomHeader.minY && flippedBoard.minY >= UsageStrip.inset, "胶囊靠近底部时向上展开看板，保持按钮锚点")
        let bridge = UsageStrip.connectorFrame(index: 4, itemCount: 5, vertical: false, canvas: dashboardCanvas, detail: flippedBoard, headerOffset: bottomOffset)
        check(bridge.width == 18 && bridge.minY == flippedBoard.maxY && bridge.maxY >= bottomHeader.minY, "上方看板与按钮间有窄鼠标连接区，不把整片透明画布挡住")
        check(pointInRoundedRectangle(CGPoint(x: firstBoard.midX, y: firstBoard.minY + 1), rect: firstBoard, radius: 20) && !pointInRoundedRectangle(CGPoint(x: firstBoard.minX + 1, y: firstBoard.minY + 1), rect: firstBoard, radius: 20), "宽看板主体接收鼠标，透明圆角继续穿透")
        let verticalCanvas = UsageStrip.canvasSize(itemCount: 5, vertical: true)
        let verticalHeader = UsageStrip.headerFrame(itemCount: 5, vertical: true, canvas: verticalCanvas)
        let verticalBoard = UsageStrip.detailFrame(for: managedItem, index: 4, itemCount: 5, vertical: true, canvas: verticalCanvas)
        check(verticalBoard.maxX < verticalHeader.minX && verticalBoard.maxY <= verticalCanvas.height - UsageStrip.inset, "右侧竖条看板在左侧展开，末尾项目不越过底边")
        let monitor = CGRect(x: -1440, y: 0, width: 1440, height: 900)
        let bottomPlacement = UsageStrip.placement(anchor: CGPoint(x: -1500, y: 120), size: dashboardCanvas, headerHeight: 70, bounds: monitor)
        check(bottomPlacement.frame.maxY - bottomPlacement.headerOffset == 120 && monitor.contains(bottomPlacement.frame), "副屏边缘调整透明面板仍保留原胶囊屏幕锚点")
        let topPlacement = UsageStrip.placement(anchor: CGPoint(x: -300, y: 868), size: dashboardCanvas, headerHeight: 70, bounds: monitor)
        check(topPlacement.frame.maxY - topPlacement.headerOffset == 868 && topPlacement.frame.maxX <= monitor.maxX, "近顶部右边缘看板画布夹紧但不移动胶囊纵向位置")

        let interaction = StripInteraction()
        check(interaction.quotaMetric == .weeklyUsed, "原生 Codex 环默认显示官方周额度已用")
        interaction.quotaMetric = .weeklyRemaining
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
