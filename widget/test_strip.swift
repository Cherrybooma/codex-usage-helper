import Foundation
import AppKit

final class EditCommandRecorder: NSObject {
    var pasteCount = 0
    @objc func paste(_ sender: Any?) { pasteCount += 1 }
}

final class RecordingDragPanel: FloatingPanel {
    var dragLocations: [CGPoint] = []
    override func performDrag(with event: NSEvent) { dragLocations.append(event.locationInWindow) }
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
        let items = StripItem.items(fixture)
        let hitRect = CGRect(x: 10, y: 10, width: 64, height: 400)
        check(pointInCapsule(CGPoint(x: 42, y: 11), rect: hitRect), "竖胶囊顶部中心接收鼠标")
        check(!pointInCapsule(CGPoint(x: 11, y: 11), rect: hitRect), "胶囊圆角的透明部分不接收鼠标")
        check(pointInCapsule(CGPoint(x: 11, y: 200), rect: hitRect), "胶囊主体仍可交互")
        check(!pointInCapsule(CGPoint(x: 42, y: 420), rect: hitRect), "预留详情外的透明区域穿透鼠标")
        check(UsageStrip.interactionDiameter == 54, "模块交互圆沿54pt可见外环边缘，不扩大成64pt方形热区")
        for vertical in [false, true] {
            let orientation = vertical ? "竖条" : "横条"
            let count = 5
            let canvas = UsageStrip.canvasSize(itemCount: count, vertical: vertical)
            let header = UsageStrip.headerFrame(itemCount: count, vertical: vertical, canvas: canvas)
            let circle = UsageStrip.moduleCircleFrame(index: 1, itemCount: count, vertical: vertical, canvas: canvas)
            let radius = UsageStrip.interactionDiameter / 2
            let center = CGPoint(x: circle.midX, y: circle.midY)
            let expectedCenter = CGPoint(
                x: header.minX + UsageStrip.inset + (vertical ? UsageStrip.diameter / 2 : UsageStrip.moduleWidth * 1.5),
                y: header.minY + UsageStrip.inset + (vertical ? UsageStrip.diameter * 1.5 + 2 * UsageStrip.rowInset : UsageStrip.diameter / 2))
            func target(_ point: CGPoint, offset: CGFloat = 0, detail: CGRect? = nil, detailIndex: Int? = nil) -> StripHitTarget {
                UsageStrip.hitTarget(at: point, itemCount: count, vertical: vertical, canvas: canvas,
                                     headerOffset: offset, detail: detail, detailIndex: detailIndex)
            }
            check(circle.size == CGSize(width: 54, height: 54) && center == expectedCenter,
                  "\(orientation)交互圆与实际图标中心对齐")
            check(target(center) == .module(1), "\(orientation)圆心展开对应模块")
            for delta in [CGPoint(x: radius - 0.25, y: 0), CGPoint(x: -radius + 0.25, y: 0),
                          CGPoint(x: 0, y: radius - 0.25), CGPoint(x: 0, y: -radius + 0.25)] {
                check(target(CGPoint(x: center.x + delta.x, y: center.y + delta.y)) == .module(1),
                      "\(orientation)圆内近边仍可交互")
            }
            for corner in [CGPoint(x: circle.minX + 0.5, y: circle.minY + 0.5),
                           CGPoint(x: circle.maxX - 0.5, y: circle.minY + 0.5),
                           CGPoint(x: circle.minX + 0.5, y: circle.maxY - 0.5),
                           CGPoint(x: circle.maxX - 0.5, y: circle.maxY - 0.5)] {
                check(target(corner) == .background, "\(orientation)圆包围盒角落属于拖动背景，不触发模块")
            }
            check(target(CGPoint(x: center.x + radius + 0.25, y: center.y)) == .background,
                  "\(orientation)可见外环之外立即回到拖动背景")
            let next = UsageStrip.moduleCircleFrame(index: 2, itemCount: count, vertical: vertical, canvas: canvas)
            let gap = vertical ? CGPoint(x: center.x, y: (circle.maxY + next.minY) / 2)
                               : CGPoint(x: (circle.maxX + next.minX) / 2, y: center.y)
            check(target(gap) == .background, "\(orientation)模块间隙只拖动胶囊")
            check(target(CGPoint(x: header.midX, y: header.minY + 1)) == .background,
                  "\(orientation)玻璃外框边缘仍可按住拖动")
            check(target(CGPoint(x: header.minX + 1, y: header.minY + 1)) == .outside,
                  "\(orientation)胶囊透明圆角穿透鼠标")
            check(target(CGPoint(x: canvas.width - 1, y: canvas.height - 1)) == .outside,
                  "\(orientation)预留详情画布穿透鼠标")
            let movedCircle = UsageStrip.moduleCircleFrame(index: 1, itemCount: count, vertical: vertical,
                                                          canvas: canvas, headerOffset: 47)
            check(movedCircle.midY == circle.midY + 47
                  && target(CGPoint(x: movedCircle.midX, y: movedCircle.midY), offset: 47) == .module(1),
                  "\(orientation)底部或偏移锚点仍按实际圆形命中")
            let overlappingDetail = CGRect(x: center.x - 40, y: center.y - 40, width: 80, height: 80)
            check(target(center, detail: overlappingDetail, detailIndex: 1) == .detail,
                  "\(orientation)详情覆盖圆形时优先操作详情，不误点背后模块")
            let detail = UsageStrip.detailFrame(for: items[0], index: 1, itemCount: count, vertical: vertical, canvas: canvas)
            let connector = UsageStrip.connectorFrame(index: 1, itemCount: count, vertical: vertical,
                                                       canvas: canvas, detail: detail)
            check(connector.width > 0 && connector.height > 0
                  && target(CGPoint(x: connector.midX, y: connector.midY), detail: detail, detailIndex: 1) == .connector,
                  "\(orientation)离开胶囊后经桥接进入详情仍可接收鼠标")
        }
        check([StripHitTarget.module(0), .background, .detail, .connector].allSatisfy(\.acceptsMouse)
              && !StripHitTarget.outside.acceptsMouse,
              "背景拖动与模块悬停分开，只有可见交互区域接收鼠标")
        let dragCanvas = UsageStrip.canvasSize(itemCount: 5)
        let dragPanel = RecordingDragPanel(contentRect: CGRect(origin: .zero, size: dragCanvas),
            styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        dragPanel.isMovableByWindowBackground = false
        var dragCallbacks: [Bool] = []
        dragPanel.backgroundDragRegion = { local in
            UsageStrip.hitTarget(at: CGPoint(x: local.x, y: dragCanvas.height - local.y), itemCount: 5,
                vertical: false, canvas: dragCanvas) == .background
        }
        dragPanel.onBackgroundDrag = { dragCallbacks.append($0) }
        func sendPanelPress(at topDownPoint: CGPoint, type: NSEvent.EventType = .leftMouseDown) {
            let event = NSEvent.mouseEvent(with: type,
                location: CGPoint(x: topDownPoint.x, y: dragCanvas.height - topDownPoint.y), modifierFlags: [],
                timestamp: 0, windowNumber: dragPanel.windowNumber, context: nil, eventNumber: 1,
                clickCount: 1, pressure: 1)!
            dragPanel.sendEvent(event)
        }
        let dragCircle = UsageStrip.moduleCircleFrame(index: 1, itemCount: 5, vertical: false, canvas: dragCanvas)
        let dragBackground = CGPoint(x: dragCircle.minX + 0.5, y: dragCircle.minY + 0.5)
        sendPanelPress(at: dragBackground)
        check(dragPanel.dragLocations == [CGPoint(x: dragBackground.x, y: dragCanvas.height - dragBackground.y)]
              && dragCallbacks == [true, false],
              "原生窗口背景左键实际分派到performDrag，开始结束回调成对且坐标正确")
        sendPanelPress(at: CGPoint(x: dragCircle.midX, y: dragCircle.midY))
        check(dragPanel.dragLocations.count == 1 && dragCallbacks == [true, false],
              "原生窗口圆内左键不拦截为背景拖动")
        sendPanelPress(at: CGPoint(x: dragCanvas.width - 1, y: dragCanvas.height - 1))
        check(dragPanel.dragLocations.count == 1 && dragCallbacks == [true, false],
              "原生窗口透明区域事件不启动拖动")
        sendPanelPress(at: dragBackground, type: .rightMouseDown)
        check(dragPanel.dragLocations.count == 1 && dragCallbacks == [true, false],
              "原生窗口背景右键保留菜单事件，不启动左键拖动")
        dragPanel.close()
        check(items[2].caption == "用量小助手" && items[2].detailValue == "800", "真实任务名与本周 Token 保留")
        check(items[0].value == "—", "缺失额度不能补剩余100%")
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
        check(crowded.count == 7, "Codex、本期Token、前四任务与其他共七个纯图标")
        check(crowded.filter(\.isProject).map(\.title) == ["A", "B", "C", "D"], "四项任务按全量周Token排名")
        check(crowded.last!.members.map(\.name) == ["E", "F"], "其他保留前四之外的全部任务")
        check(crowded.last!.value == "10.0%" && crowded.last!.detailValue == "100", "其他合计沿用完整用量分母")
        check(crowded.dropFirst(2).reduce(0) { $0 + Int($1.detailValue)! } == lots.total_tokens, "前四加其他Token不重复不遗漏")
        let smallLeaders = try snapshot("""
        [{"name":"主项目","total_tokens":900,"percent":90,"request_records":10}]
        """, other: """
        [{"name":"第二项目","total_tokens":45,"percent":4.5,"request_records":1},
         {"name":"第三项目","total_tokens":35,"percent":3.5,"request_records":1},
         {"name":"第四项目","total_tokens":20,"percent":2,"request_records":1}]
        """)
        check(StripItem.items(smallLeaders).filter(\.isProject).count == 4 && !StripItem.items(smallLeaders).contains { $0.id == "remainder" }, "低于5%的任务仍可进入前四，四任务无需其他")
        let bumped = try snapshot("""
        [{"name":"A","total_tokens":500,"percent":50,"request_records":10},
         {"name":"B","total_tokens":200,"percent":20,"request_records":4},
         {"name":"C","total_tokens":120,"percent":12,"request_records":3},
         {"name":"D","total_tokens":80,"percent":8,"request_records":2}]
        """, other: """
        [{"name":"E","total_tokens":100,"percent":10,"request_records":2}]
        """)
        check(StripItem.items(bumped).filter(\.isProject).map(\.title) == ["A", "B", "C", "E"] && StripItem.items(bumped).last!.members.map(\.name) == ["D"], "后排任务增长后自动替换第四项，重新归并其他")
        let quota = """
        {"observed_at":"2026-10-07T06:00:00Z","stale":true,"windows":[
         {"key":"primary","label":"5小时","used_percent":50,"remaining_percent":50,"duration_mins":300,"resets_at":9999999999},
         {"key":"secondary","label":"周","used_percent":69,"remaining_percent":69,"duration_mins":10080,"resets_at":9999999999}]}
        """
        let cached = try snapshot("[]", quota: quota)
        check(StripItem.items(cached)[0].value == "≈31%" && StripItem.items(cached)[0].primaryRing == 0.31, "周剩余额度取100减真实已用，旧缓存69不会错误显示剩余69")
        let budget = """
        {"status":"estimated","usage_quality":"estimated","period_start":1791734400,"period_end":1792339200,"observed_total_percent":140,"excess_percent":40,"reset_count":1,"notes":[]}
        """
        let calendarSnapshot = try snapshot("""
        [{"name":"用量小助手","total_tokens":882,"percent":88.2,"request_records":20},
         {"name":"余下","total_tokens":118,"percent":11.8,"request_records":2}]
        """, quota: quota, budget: budget)
        let calendarItems = StripItem.items(calendarSnapshot)
        check(calendarItems[0].value == "≈31%" && calendarItems[0].facts.first { $0.label == "本周已用额度" }?.value == "≈140.0%", "当前窗口剩余31%与自然周累计140%独立展示")
        check(calendarItems[0].estimatedCost == "≈$7.00" && calendarItems[2].estimatedCost == "≈$6.17", "周100%=$5，全局140%=$7，88.2%项目分摊$6.17")
        check(StripQuotaMetric.allCases.allSatisfy { StripItem.items(calendarSnapshot, metric: $0)[0].value == "≈31%" }, "旧指标参数和滚轮不能改变Codex剩余定位")
        let incomplete = try snapshot("[]", budget: """
        {"status":"incomplete","observed_total_percent":150,"excess_percent":null,"reset_count":1,"notes":["原因未确认"]}
        """)
        check(StripItem.items(incomplete)[0].estimatedCost == "—" && StripItem.items(incomplete)[0].facts.first { $0.label == "本周已用额度" }?.value == "—", "缺质量的旧不完整预算不强行估算额度和金额")
        check(StripItem.costLabel(7, status: "lower_bound", lowerBound: true) == "≥$7.00" && StripItem.costLabel(7, status: "unavailable") == "—", "已确认下限与未知金额明确区分")
        check(StripItem.costLabel(-1, status: "estimated") == "—" && StripItem.costLabel(.infinity, status: "estimated") == "—", "非法金额不进入展示")
        let freshQuota = quota.replacingOccurrences(of: "\"stale\":true", with: "\"stale\":false")
        let tinyQuota = freshQuota.replacingOccurrences(of: "\"used_percent\":69,\"remaining_percent\":69", with: "\"used_percent\":99.6,\"remaining_percent\":0.4")
        let tinySnapshot = try snapshot("[]", quota: tinyQuota)
        check(StripItem.items(tinySnapshot)[0].value == "<1%", "小于1%的正剩余不四舍五入成耗尽")
        let depleted = freshQuota.replacingOccurrences(of: "\"used_percent\":69,\"remaining_percent\":69", with: "\"used_percent\":100,\"remaining_percent\":0")
        let depletedSnapshot = try snapshot("[]", quota: depleted)
        check(StripItem.items(depletedSnapshot)[0].value == "0%", "合法零剩余与未知额度不同")
        let freshSnapshot = try snapshot("[]", quota: freshQuota)
        check(StripItem.items(freshSnapshot, now: Date(timeIntervalSince1970: 9999999999))[0].value == "≈31%", "过重置时间的旧剩余额度保留近似标记")
        let empty = try snapshot("[]")
        check(StripItem.items(empty).last!.id == "empty", "无有用量任务不编造示例")
        let quotaSections = StripDetailSection.content(for: calendarItems[0], locked: true, page: .actions)
        check(quotaSections.map(\.label) == ["周额度剩余", "本周已用额度", "本周 Token", "估算投入"] && quotaSections.allSatisfy { $0.kind == .row }, "Codex固定单页四行，无解释和分页tabs")
        let ledgerSections = StripDetailSection.content(for: calendarItems[1], locked: true, page: .investment)
        check(ledgerSections.map(\.label) == ["本周 Token", "任务数量", "统计时间", "模型响应次数"], "本期Token固定单页四项")
        check(StripDetailSection.content(for: calendarItems[0], locked: false, page: .overview).map(\.value) == quotaSections.map(\.value), "Codex悬停和点击内容一致")
        check(ledgerSections[1].value == "2 个" && ledgerSections[3].value == "22 次", "任务计数来自全部任务，响应不冒充会话")

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
          "project_status":"开发中","project_health":"顺利","weekly_goal":"旧目标","current_goal":"完成胶囊字段审阅，并核验正式产出链接。",
          "next_step":"确认界面后开发原生组件。","project_goal":"项目进度和投入随时可见。",
          "open_tasks":33,"blocked_tasks":11,"completed_tasks":55,"human_open_tasks":2,"human_blocked_tasks":0,"human_completed_tasks":15,
          "latest_progress_summary":"账期核算完成，交互待审阅。","latest_output_title":"周期核算验证报告","latest_output_url":"https://example.com/report",
          "development_progress":65,"progress_source":"ai","today_tokens":200}]
        """, budget: budget)
        let managedItem = StripItem.items(managed)[2]
        check(managedItem.projectStatus == "顺利" && managedItem.developmentProgress == 65 && managedItem.secondaryRing == 0.65, "项目健康状态与AI估算进度进入原生卡片和内环")
        check(managedItem.progressLabel == "完成进度" && StripItem.projectProgress(50, source: "tasks") == nil, "任务完成率不能冒充项目完成进度")
        check(StripItem.projectProgress(0, source: "ai") == 0 && StripItem.projectProgress(65, source: "manual") == 65 && StripItem.projectProgress(101, source: "ai") == nil, "真实零进度有效，缺来源和越界值未知")
        check(StripItem.projectDate("2026-10-12T16:00:00Z") == "2026-10-13" && StripItem.projectDate("2026-10-18") == "2026-10-18", "时间采用上海日期，纯日期原样保留")
        let hoverSections = StripDetailSection.content(for: managedItem, locked: false, page: .overview)
        check(hoverSections.map(\.id) == ["status", "next", "progress", "share"] && hoverSections[2].value == "65%", "悬停仅状态、下一步、完成进度、周占比，无约字和Token量")
        let overviewSections = StripDetailSection.content(for: managedItem, locked: true, page: .overview)
        check(overviewSections.map(\.id) == ["status", "vision", "progress", "share"] && !overviewSections.contains { $0.label.contains("下一步") }, "点击概览用愿景替换下一步，保持四项")
        let actionSections = StripDetailSection.content(for: managedItem, locked: true, page: .actions)
        check(actionSections.map(\.id) == ["goal", "manual", "latest", "output"] && actionSections[0].value == "完成胶囊字段审阅，并核验正式产出链接。", "行动展示完整当前目标、人工待办、精简进度与正式产出")
        check(actionSections[1].value == "2 项|0 项|15 项", "人工待办不误用AI总任务计数33/11/55")
        let investmentSections = StripDetailSection.content(for: managedItem, locked: true, page: .investment)
        check(investmentSections.map(\.label) == ["本周 Token", "今日 Token", "本周占比", "估算投入"] && investmentSections.allSatisfy { $0.kind == .row }, "项目投入统一四行，去掉格子和冗余解释")
        check(investmentSections[1].value == "200" && investmentSections[3].value == "≈$7.00", "今日真实Token与订阅分摊估算金额分别展示")
        let sparse = StripDetailSection.content(for: items[2], locked: true, page: .actions)
        check(sparse[1].value == "—|—|—" && sparse[2].value == "—", "未取得人工任务和正式产出保持未知")
        let managementOnly = try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"generated_at":"2026-10-13T06:00:00Z","total_tokens":1000,"request_records":22,"legacy_updates":0,
         "tasks":[{"name":"真实耗费","total_tokens":1000,"percent":100,"request_records":22}],"other_tasks":[],
         "management_projects":[{"name":"未关联Notion项目","task_id":"notion-1","total_tokens":0,"percent":0,"request_records":0,"usage_linked":false,"notion_url":"https://www.notion.so/zero-project"}]}
        """.utf8))
        let managedZero = StripItem.items(managementOnly)[2]
        check(managedZero.title == "未关联Notion项目" && managedZero.detailValue == "0" && !managedZero.usageLinked,
              "零用量管理项目填入项目位，不采用管理缓存伪用量")
        check(StripItem.items(managementOnly)[1].facts.first { $0.label == "任务数量" }?.value == "1 个",
              "展示零用量管理项目不增加真实文件夹任务数")
        let zeroOther = StripItem.items(managementOnly).last!
        check(zeroOther.id == "remainder" && zeroOther.members.map(\.name) == ["真实耗费"] && zeroOther.detailValue == "1.0K",
              "未纳入管理的正用量目录进入其他，用量不会丢失")
        check(managedZero.notionURL == "https://www.notion.so/zero-project",
              "零用量项目仍可打开Notion工作台")
        let allUsage = try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"generated_at":"2026-10-13T06:00:00Z","total_tokens":1000,"request_records":22,"legacy_updates":0,"task_count":6,
         "tasks":[{"name":"旧缓存主项目","total_tokens":1000,"percent":100,"request_records":22}],"other_tasks":[],
         "all_usage_tasks":[{"name":"真实全量A","total_tokens":600,"percent":60,"request_records":10},{"name":"真实全量B","total_tokens":400,"percent":40,"request_records":12}]}
        """.utf8))
        check(StripItem.items(allUsage).filter(\.isProject).map(\.title) == ["真实全量A", "真实全量B"] && StripItem.items(allUsage)[1].facts.first { $0.label == "任务数量" }?.value == "6 个", "全量合同优先旧5%聚合缓存，任务数量不取可见图标数")
        let lowerBoundSnapshot = try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"generated_at":"2026-10-13T06:00:00Z","total_tokens":1000,"request_records":22,"legacy_updates":0,
         "estimated_cost_usd":7,"estimated_cost_status":"lower_bound","estimated_cost_lower_bound":true,
         "tasks":[{"name":"真实A","total_tokens":800,"percent":80,"request_records":20}],"other_tasks":[],
         "weekly_budget":{"status":"incomplete","usage_quality":"lower_bound","observed_total_percent":140,"reset_count":1,"notes":[]}}
        """.utf8))
        let lowerItems = StripItem.items(lowerBoundSnapshot)
        check(lowerItems[0].estimatedCost == "≥$7.00" && lowerItems[2].estimatedCost == "≥$5.60", "已确认额度金额下限分摊后仍保持下限标记")
        check(lowerItems[0].facts.first { $0.label == "本周已用额度" }?.value == "≥140.0%", "不完整观测的累计额度标记下限")
        check(StripDetailSection.safeOutputURL("https://example.com/report") != nil && StripDetailSection.safeOutputURL("/tmp/report.md")?.isFileURL == true, "正式产出支持HTTPS与本地文档")
        check(StripDetailSection.safeOutputURL("javascript:alert(1)") == nil && StripDetailSection.safeOutputURL("https://user:password@example.com/report") == nil && StripDetailSection.safeOutputURL("file:///tmp/unsafe.app") == nil, "正式产出拒绝任意执行方案和凭据URL")
        let longGoal = String(repeating: "完成当前目标，并核验每一个阶段的正式产出和关联记录。", count: 20)
        let longSection = StripDetailSection(id: "goal", kind: .statement, label: "当前目标", value: longGoal)
        let reviewContentWidth: CGFloat = 320
        let shortPages = StripDetailSection.pages([longSection], width: reviewContentWidth, height: 90)
        check(shortPages.count > 1 && shortPages.flatMap { $0 }.map(\.value).joined() == longGoal, "短屏对长目标分页，逐字完整可达无省略")
        check(shortPages.allSatisfy { StripDetailLayout.contentHeight($0, width: reviewContentWidth, metrics: false) <= 90 }, "每页长目标含组间距仍在可用高度内，不靠滚动和裁切")
        let normalPages = StripDetailSection.pages(investmentSections, width: reviewContentWidth, height: 245, metrics: true)
        check(normalPages.count == 1 && normalPages[0].count == 4, "正常屏项目投入四行在同一页完整显示")
        let shortInvestment = StripDetailSection.pages(investmentSections, width: reviewContentWidth, height: 80, metrics: true)
        check(shortInvestment.count == 2 && shortInvestment.flatMap { $0 }.map(\.id) == investmentSections.map(\.id)
              && shortInvestment.allSatisfy { StripDetailLayout.contentHeight($0, width: reviewContentWidth, metrics: true) <= 80 }, "短屏四行分页保留字段顺序与分隔留白，逐页不溢出")

        let longOutput = StripDetailSection(id: "output", kind: .artifact, label: "正式产出", value: String(repeating: "完整正式产出标题", count: 30), url: "https://example.com/report")
        let outputPages = StripDetailSection.pages([longOutput], width: reviewContentWidth, height: 50)
        check(outputPages.flatMap { $0 }.map(\.value).joined() == longOutput.value && outputPages.flatMap { $0 }.allSatisfy { $0.url == longOutput.url && $0.height(width: reviewContentWidth) <= 50 }, "长产出标题完整分页，每片保留同一正式文档链接")
        let longName = StripDetailSection(id: "long-other", kind: .row, label: String(repeating: "很长的项目名称", count: 20), value: "1.0%")
        let namePages = StripDetailSection.pages([longName], width: reviewContentWidth, height: 50)
        check(namePages.flatMap { $0 }.map(\.value).joined() == longName.label + " · 1.0%" && namePages.flatMap { $0 }.allSatisfy { $0.height(width: reviewContentWidth) <= 50 }, "其他内的长项目名同样完整可达，不依赖滚动与省略")
        let tinyPages = StripDetailSection.pages(actionSections, width: reviewContentWidth, height: 50)
        check(tinyPages.flatMap { $0 }.contains { $0.id == "manual" && $0.value == actionSections[1].value }
              && tinyPages.allSatisfy { StripDetailLayout.contentHeight($0, width: reviewContentWidth, metrics: false) <= 50 }, "极短内容页保留完整人工事项与全部文字，含留白不溢出")

        check(StripDetailMetrics.labelSize == 12 && StripDetailMetrics.bodySize == 14 && StripDetailMetrics.titleSize == 16
              && StripDetailMetrics.bodyLine == 22 && StripDetailMetrics.labelLine == 18, "原生共享批准稿12／14／16字号与22正文行高")
        check(StripDetailMetrics.padding == 20 && StripDetailMetrics.sectionGap == 16 && StripDetailMetrics.fieldGap == 6,
              "卡片统一20内边距、16组间距与6标签正文距")
        check(ledgerSections.first { $0.id == "time" }?.valueSize == 14
              && ledgerSections.first { $0.id == "week-tokens" }?.valueSize == 16,
              "统计日期使用14正文，Token数字保持16，不因日期内含数字而放大")
        let quotaLayout = StripDetailLayout.make(item: calendarItems[0], locked: true, page: .overview)
        let ledgerLayout = StripDetailLayout.make(item: calendarItems[1], locked: true, page: .overview)
        let overviewLayout = StripDetailLayout.make(item: managedItem, locked: true, page: .overview)
        let actionsLayout = StripDetailLayout.make(item: managedItem, locked: true, page: .actions)
        let investmentLayout = StripDetailLayout.make(item: managedItem, locked: true, page: .investment)
        let reviewLayouts = [quotaLayout, ledgerLayout, overviewLayout, actionsLayout, investmentLayout]
        check(reviewLayouts.allSatisfy { $0.contentWidth == reviewContentWidth && $0.pages.count == 1 && $0.height <= 520 },
              "五种普通详情在360宽卡片内自然排布，字段完整单页可达")
        check(!quotaLayout.hasTabs && !quotaLayout.showsFooter && !ledgerLayout.hasTabs && !ledgerLayout.showsFooter
              && quotaLayout.visibleSections.count == 4 && ledgerLayout.visibleSections.count == 4,
              "Codex与账本不保留项目标签或底部项目入口，正常四行一页")
        check(quotaLayout.height < 300 && ledgerLayout.height < 300
              && overviewLayout.hasTabs && overviewLayout.showsFooter && investmentLayout.metrics,
              "少内容卡片自然收紧，项目保留完整标签和链接，投入采用同行指标")
        var longerActionItem = managedItem
        longerActionItem.managementFacts = managedItem.managementFacts.map {
            $0.label == "当前目标" ? StripFact(label: $0.label, value: longGoal) : $0
        }
        let longActionLayout = StripDetailLayout.make(item: longerActionItem, locked: true, page: .actions)
        check(longActionLayout.pages.count > 1
              && longActionLayout.pages.flatMap { $0 }.filter { $0.id.hasPrefix("goal") }.map(\.value).joined() == longGoal,
              "长行动目标按原文字顺序分到多页，不裁字或缩字")
        let selectedLast = StripDetailLayout.make(item: longerActionItem, locked: true, page: .actions, sectionPage: 999)
        let selectedFirst = StripDetailLayout.make(item: longerActionItem, locked: true, page: .actions, sectionPage: -1)
        check(selectedFirst.selectedPage == 0 && selectedLast.selectedPage == selectedLast.pages.count - 1
              && selectedLast.visibleSections.map(\.id) == selectedLast.pages.last?.map(\.id),
              "缓存页码越界安全夹紧，末页只显示该页内容")
        check(longActionLayout.pages.indices.allSatisfy { page in
            let layout = StripDetailLayout.make(item: longerActionItem, locked: true, page: .actions, sectionPage: page)
            return layout.height <= 520 && layout.contentWidth == reviewContentWidth
        }, "长内容每页把标题标签、间距、分页和项目入口都计入520高度预算")
        check(StripItem.statusLabel("正常") == "顺利" && StripItem.statusLabel("阻塞") == "遇到卡点" && StripItem.statusLabel("有风险") == "有风险" && StripItem.statusLabel("开发中") == "开发中", "Notion健康词归一而开发中不冒充顺利")
        let explicitUnknown = try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"generated_at":"2026-10-13T06:00:00Z","total_tokens":1000,"request_records":22,"legacy_updates":0,
         "estimated_cost_status":"unavailable","tasks":[],"other_tasks":[],
         "weekly_budget":{"status":"estimated","usage_quality":"unavailable","observed_total_percent":130,"reset_count":1,"notes":[]}}
        """.utf8))
        check(StripItem.items(explicitUnknown)[0].estimatedCost == "—" && StripItem.items(explicitUnknown)[0].facts.first { $0.label == "本周已用额度" }?.value == "—", "明确unavailable预算不可被旧estimated状态fallback复活")
        let dedupManaged = try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"generated_at":"2026-10-13T06:00:00Z","total_tokens":1000,"request_records":22,"legacy_updates":0,
         "tasks":[{"task_id":"localA","name":"本地项目","total_tokens":1000,"percent":100,"request_records":22}],"other_tasks":[],
         "management_projects":[{"task_id":"notion:page1","task_ids":["localA"],"name":"已关联项目","total_tokens":0,"percent":0,"request_records":0,"notion_url":"https://www.notion.so/page1"}]}
        """.utf8))
        check(!StripItem.items(dedupManaged).contains { $0.id == "remainder" }, "已绑定正用量folder的Notion管理页不在其他重复出现")

        let pinSnapshot = try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"generated_at":"2026-10-15T06:00:00Z","total_tokens":1000,"request_records":25,"legacy_updates":0,"task_count":5,
         "tasks":[],"other_tasks":[],"all_usage_tasks":[
          {"task_id":"liveA","name":"用量A","total_tokens":600,"percent":60,"request_records":10,"today_tokens":150,"notion_url":"https://app.notion.com/p/0123456789abcdef0123456789abcdef"},
          {"task_id":"liveB","name":"用量B","total_tokens":200,"percent":20,"request_records":6},
          {"task_id":"liveC","name":"用量C","total_tokens":100,"percent":10,"request_records":4},
          {"task_id":"liveD","name":"用量D","total_tokens":60,"percent":6,"request_records":3},
          {"task_id":"liveE","name":"用量E","total_tokens":40,"percent":4,"request_records":2}],
         "management_projects":[
          {"task_id":"notion:cold1","name":"播客","total_tokens":999,"percent":99,"request_records":99,"usage_linked":false,"icon_type":"emoji","icon_value":"🎧","project_goal":"完成播客收藏工具","current_goal":"核验导入","next_step":"跑通收藏","development_progress":35,"progress_source":"ai"},
          {"task_id":"notion:cold2","name":"问题单","total_tokens":0,"percent":0,"request_records":0},
          {"task_id":"notion:cold3","name":"时间管理","total_tokens":0,"percent":0,"request_records":0},
          {"task_id":"notion:liveA","task_ids":["liveA"],"name":"已绑定A","total_tokens":0,"percent":0,"request_records":0,"notion_url":"https://www.notion.so/0123456789abcdef0123456789abcdef"},
          {"task_id":"notion:archived","name":"已归档","project_status":"归档","total_tokens":0,"percent":0,"request_records":0},
          {"task_id":"notion:archived-old","name":"旧归档","project_status":"已归档","total_tokens":0,"percent":0,"request_records":0}]}
        """.utf8))
        let dynamicItems = StripItem.items(pinSnapshot)
        check(dynamicItems.prefix(2).map(\.id) == ["quota", "total"], "前两个入口固定为Codex账号和本期Token")
        check(dynamicItems.filter(\.isProject).map(\.title) == ["已绑定A", "播客", "问题单", "时间管理"],
              "第三入口为用量最多管理项目，零用量管理项目补满四个位置")
        check(dynamicItems.filter(\.isProject).dropFirst().allSatisfy { $0.detailValue == "0" && $0.value == "0.0%" && $0.todayTokens == 0 && !$0.usageLinked },
              "管理项目零用量不采用管理缓存的伪计数")
        check(dynamicItems[1].facts.first { $0.label == "任务数量" }?.value == "5 个", "四个管理图标不改变本周真实文件夹任务数量")
        let dynamicRemainder = dynamicItems.first { $0.id == "remainder" }!
        check(dynamicRemainder.members.map(\.task_id) == ["liveB", "liveC", "liveD", "liveE"] && dynamicRemainder.detailValue == "400" && dynamicRemainder.managedMembers.isEmpty,
              "未纳入管理的原始目录在其他，绑定管理行不重复出现")
        check(dynamicRemainder.members.reduce(600) { $0 + $1.total_tokens } == pinSnapshot.total_tokens,
              "管理项目与其他之和仍等于全部真实用量")
        let zeroProject = dynamicItems.first { $0.title == "播客" }!
        check(StripDetailSection.content(for: zeroProject, locked: false, page: .overview).count == 4 && StripDetailSection.content(for: zeroProject, locked: true, page: .overview).first { $0.id == "vision" }?.value == "完成播客收藏工具",
              "零用量管理项目拥有独立悬停摘要与完整项目详情")
        check(StripDetailSection.content(for: zeroProject, locked: true, page: .actions).first { $0.id == "goal" }?.value == "核验导入" && StripDetailSection.content(for: zeroProject, locked: true, page: .investment).first?.value == "0",
              "零用量项目的行动与投入页保持真实可用")
        check(dynamicItems.filter(\.isProject).first?.id == "folder:notion:liveA" && dynamicItems.filter(\.isProject).first?.detailValue == "600" && dynamicItems.filter(\.isProject).first?.todayTokens == 150,
              "已绑定项目使用真实folder计数与稳定管理项目身份")
        let byURLJSON = """
        {"generated_at":"2026-10-15T06:00:00Z","total_tokens":600,"request_records":10,"legacy_updates":0,
         "tasks":[{"task_id":"liveA","name":"A","total_tokens":600,"percent":100,"request_records":10,"notion_url":"https://app.notion.com/p/0123456789abcdef0123456789abcdef"}],"other_tasks":[],
         "management_projects":[{"task_id":"notion:pageA","name":"绑定A","total_tokens":0,"percent":0,"request_records":0,"notion_url":"https://www.notion.so/0123456789abcdef0123456789abcdef"}]}
        """
        let byURL = try JSONDecoder().decode(Snapshot.self, from: Data(byURLJSON.utf8))
        check(StripItem.items(byURL).filter(\.isProject).map(\.detailValue) == ["600"] && !StripItem.items(byURL).contains { $0.id == "remainder" },
              "不同Notion域名的同页可匹配真实用量且不重影")
        check(StripItem.items(pinSnapshot, managementEnabled: false).filter(\.isProject).map(\.title) == ["用量A", "用量B", "用量C", "用量D"],
              "本机模式忽略管理项目缓存，回退原有前四正用量目录")
        let grouped = try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"generated_at":"2026-10-15T06:00:00Z","total_tokens":1000,"request_records":20,"legacy_updates":0,"task_count":4,
         "estimated_cost_usd":10,"estimated_cost_status":"estimated", "tasks":[],"other_tasks":[],
         "all_usage_tasks":[
          {"task_id":"a1","name":"A前端","total_tokens":250,"percent":25,"request_records":5,"today_tokens":50},
          {"task_id":"a2","name":"A后端","total_tokens":350,"percent":35,"request_records":5,"today_tokens":70},
          {"task_id":"b","name":"B","total_tokens":300,"percent":30,"request_records":7,"today_tokens":90},
          {"task_id":"unmanaged","name":"Codex目录","total_tokens":100,"percent":10,"request_records":3,"today_tokens":10}],
         "management_projects":[
          {"task_id":"project-a","task_ids":["a1"],"name":"合并A","total_tokens":999,"percent":99,"request_records":99,"notion_url":"https://app.notion.com/p/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
          {"task_id":"project-a-copy","task_ids":["a2","a1"],"name":"合并A副本","total_tokens":999,"percent":99,"request_records":99,"notion_url":"https://www.notion.so/A-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
          {"task_id":"project-b","task_ids":["b"],"name":"B","total_tokens":0,"percent":0,"request_records":0},
          {"task_id":"project-c","name":"C","total_tokens":0,"percent":0,"request_records":0},
          {"task_id":"project-d","name":"D","total_tokens":0,"percent":0,"request_records":0},
          {"task_id":"project-e","name":"E","total_tokens":0,"percent":0,"request_records":0}]}
        """.utf8))
        let groupedItems = StripItem.items(grouped)
        check(groupedItems.filter(\.isProject).map(\.title) == ["合并A", "B", "C", "D"],
              "按Notion页合并多folder后排名，正用量优先，零用量同值按稳定ID排序")
        check(groupedItems[2].detailValue == "600" && groupedItems[2].value == "60.0%" && groupedItems[2].todayTokens == 120 && groupedItems[2].estimatedCost == "≈$6.00",
              "同页多个folder的本周、今日与金额均汇总，不取第一folder冒充项目总量")
        check(groupedItems.last?.members.map(\.task_id) == ["unmanaged"] && groupedItems.last?.managedMembers.map(\.name) == ["E"],
              "前四之外的管理页和未管理目录均可从其他访问")
        check(groupedItems.dropFirst(2).reduce(0) { $0 + (Int($1.detailValue) ?? 0) } == grouped.total_tokens,
              "多folder同页去重与管理页去重之后总Token守恒")
        let overlapping = try JSONDecoder().decode(Snapshot.self, from: Data("""
        {"generated_at":"2026-10-15T06:00:00Z","total_tokens":1000,"request_records":20,"legacy_updates":0,"tasks":[],"other_tasks":[],
         "all_usage_tasks":[{"task_id":"shared","name":"唯一目录","total_tokens":1000,"percent":100,"request_records":20}],
         "management_projects":[
          {"task_id":"first","task_ids":["shared"],"name":"第一项目","total_tokens":0,"percent":0,"request_records":0},
          {"task_id":"second","task_ids":["shared"],"name":"旧关联副本","total_tokens":0,"percent":0,"request_records":0}]}
        """.utf8))
        let overlappingItems = StripItem.items(overlapping)
        check(overlappingItems.filter(\.isProject).map(\.value) == ["100.0%", "0.0%"] && !overlappingItems.contains { $0.id == "remainder" },
              "旧绑定意外重叠时同一folder仅分配一次，不重复累计Token")
        var grownJSON = try JSONSerialization.jsonObject(with: Data("""
        {"generated_at":"2026-10-15T06:00:00Z","total_tokens":1000,"request_records":20,"legacy_updates":0,"tasks":[],"other_tasks":[],
         "all_usage_tasks":[{"task_id":"new-leader","name":"新用量","total_tokens":1000,"percent":100,"request_records":20}],
         "management_projects":[
          {"task_id":"a","name":"A","total_tokens":0,"percent":0,"request_records":0},
          {"task_id":"b","name":"B","total_tokens":0,"percent":0,"request_records":0},
          {"task_id":"c","name":"C","total_tokens":0,"percent":0,"request_records":0},
          {"task_id":"d","name":"D","total_tokens":0,"percent":0,"request_records":0},
          {"task_id":"e","task_ids":["new-leader"],"name":"新领先项目","total_tokens":0,"percent":0,"request_records":0}]}
        """.utf8)) as! [String: Any]
        let grown = try JSONDecoder().decode(Snapshot.self, from: JSONSerialization.data(withJSONObject: grownJSON))
        check(StripItem.items(grown).filter(\.isProject).map(\.title) == ["新领先项目", "A", "B", "C"],
              "后排管理项目用量增长后进入第三入口，动态挤出第四位")
        grownJSON["management_projects"] = (grownJSON["management_projects"] as! [[String: Any]]).reversed().map { $0 }
        let shuffled = try JSONDecoder().decode(Snapshot.self, from: JSONSerialization.data(withJSONObject: grownJSON))
        check(StripItem.items(shuffled).filter(\.isProject).map(\.id) == StripItem.items(grown).filter(\.isProject).map(\.id),
              "管理缓存返回顺序改变时同用量项目位置稳定")
        check(UsageStrip.naturalSize(itemCount: 5).height == UsageStrip.naturalSize(itemCount: 6).height, "项目增加只改变横条宽度，不移动顶部锚点")
        check(UsageStrip.detailHeight(for: linkedItem) <= UsageStrip.maxDetailHeight, "长名称使用有界自然卡片，不无限拉长看板")
        let dashboardCanvas = UsageStrip.canvasSize(itemCount: 5)
        let firstBoard = UsageStrip.detailFrame(for: managedItem, index: 0, itemCount: 5, vertical: false, canvas: dashboardCanvas)
        let lastBoard = UsageStrip.detailFrame(for: managedItem, index: 4, itemCount: 5, vertical: false, canvas: dashboardCanvas)
        check(firstBoard.width == 360 && lastBoard.width == 360, "首尾详情使用批准稿360pt卡片宽度")
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
        check(interaction.quotaMetric == .weeklyRemaining, "原生Codex环固定显示周剩余额度")
        interaction.quotaMetric = .weeklyRemaining
        interaction.click("A")
        interaction.cycleQuota(steps: 1)
        check(interaction.quotaMetric == .weeklyRemaining && interaction.expanded == "A" && interaction.locked, "旧切换调用不改变Codex剩余定位或关闭项目卡片")
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
        interaction.dismiss()
        interaction.hover("A", inside: true)
        drain(0.22)
        interaction.selectPage(.investment)
        check(interaction.dashboardPage == .overview && !interaction.locked, "悬停概览不能被未锁定的详情页切换")
        interaction.click("A")
        interaction.selectPage(.actions)
        check(interaction.locked && interaction.dashboardPage == .actions, "点击保持后可切换行动页")
        interaction.cycleQuota()
        check(interaction.dashboardPage == .actions && interaction.expanded == "A", "额度指标变化不重置锁定卡片的详情页")
        interaction.click("quota")
        check(interaction.dashboardPage == .overview && interaction.expanded == "quota", "切换项目到额度时回到概览，不遗留项目行动页")
        interaction.selectPage(.investment)
        interaction.dismiss()
        check(interaction.dashboardPage == .overview && interaction.expanded == nil, "Esc 或外部关闭清除页状态")
        interaction.click("A")
        interaction.selectPage(.actions)
        interaction.beginDragging()
        check(interaction.isDragging && interaction.expanded == nil && !interaction.locked
              && interaction.dashboardPage == .overview, "背景开始拖动立即收起详情并清除锁定")
        interaction.hover("B", inside: true, zone: "native-pointer")
        interaction.click("B")
        drain(0.28)
        check(interaction.isDragging && interaction.expanded == nil && !interaction.locked,
              "拖动过程中经过圆形或收到点击也不展开详情")
        interaction.endDragging()
        check(!interaction.isDragging && interaction.expanded == nil, "拖动结束不自动展开路过的项目")
        interaction.hover("B", inside: true, zone: "native-pointer")
        drain(0.22)
        check(interaction.expanded == "B" && !interaction.locked, "拖动结束后重新进入圆形恢复悬停")
        interaction.dismiss()
        interaction.hover("A", inside: true, zone: "native-pointer")
        interaction.beginDragging()
        drain(0.28)
        check(interaction.isDragging && interaction.expanded == nil,
              "开始拖动取消尚未执行的悬停展开，延迟回调不会弹卡片")
        interaction.endDragging()
        check(UsageStrip.needsPagination(availableHeight: 120, locked: true) && !UsageStrip.needsPagination(availableHeight: 420, locked: true), "极小屏用分页完整到达，任何详情都没有卡内滚动")
        let hoverBoard = UsageStrip.detailFrame(for: managedItem, index: 0, itemCount: 5, vertical: false, canvas: UsageStrip.canvasSize(itemCount: 5), locked: false)
        let pinnedBoard = UsageStrip.detailFrame(for: managedItem, index: 0, itemCount: 5, vertical: false, canvas: UsageStrip.canvasSize(itemCount: 5), locked: true)
        let hoverLayout = StripDetailLayout.make(item: managedItem, locked: false, page: .overview)
        check(hoverBoard.height == hoverLayout.height && !hoverLayout.hasTabs && !hoverLayout.showsFooter
              && pinnedBoard.height > hoverBoard.height && pinnedBoard.height <= UsageStrip.maxDetailHeight && hoverBoard.minY == pinnedBoard.minY,
              "悬停摘要自然收紧，点击才出现标签和项目入口，保留同一按钮锚点")
        let smallCanvas = CGSize(width: 390, height: 400)
        let smallFirst = UsageStrip.detailPlacement(for: longerActionItem, index: 2, itemCount: 5, vertical: false,
                                                    canvas: smallCanvas, page: .actions)
        check(smallFirst.layout.pages.count > 1 && smallFirst.frame.width == 360
              && smallFirst.layout.contentWidth == 320 && smallFirst.frame.height == smallFirst.layout.height,
              "400pt短窗口仍用360宽与原字号，通过内容分页适应可见高度")
        let shortScreenPages = smallFirst.layout.pages.indices.map { page in
            UsageStrip.detailPlacement(for: longerActionItem, index: 2, itemCount: 5, vertical: false,
                                       canvas: smallCanvas, page: .actions, sectionPage: page)
        }
        check(shortScreenPages.allSatisfy { placement in
            let hitFrame = UsageStrip.detailFrame(for: longerActionItem, index: 2, itemCount: 5, vertical: false,
                                                 canvas: smallCanvas, page: .actions, sectionPage: placement.layout.selectedPage)
            return placement.frame == hitFrame && placement.frame.height == placement.layout.height
                && placement.frame.minX >= UsageStrip.inset && placement.frame.maxX <= smallCanvas.width - UsageStrip.inset
                && placement.frame.minY >= UsageStrip.inset && placement.frame.maxY <= smallCanvas.height - UsageStrip.inset
                && pointInRoundedRectangle(CGPoint(x: hitFrame.midX, y: hitFrame.maxY - 8), rect: hitFrame, radius: UsageStrip.detailRadius)
        }, "短屏逐页渲染与鼠标命中共享几何，底部链接可点击，卡片不越界")
        check(shortScreenPages.flatMap { $0.layout.visibleSections }.filter { $0.id.hasPrefix("goal") }.map(\.value).joined() == longGoal,
              "400pt短屏跨页能读完完整目标，分页后无丢字")
        var middleAnchorItem = managedItem
        let middleAnchorGoal = String(repeating: "验证长目标的分页位置与文本内容，不应因翻页而改变页数。", count: 8)
        middleAnchorItem.managementFacts = [StripFact(label: "当前目标", value: middleAnchorGoal),
            StripFact(label: "人工待办", value: "2 项"), StripFact(label: "人工受阻", value: "0 项"),
            StripFact(label: "人工已完成", value: "15 项"), StripFact(label: "最新产出与进度记录", value: "已完成一轮验证。")]
        middleAnchorItem.latestOutputTitle = nil
        middleAnchorItem.latestOutputURL = nil
        let middleCanvas = CGSize(width: 500, height: 900)
        let middleFirst = UsageStrip.detailPlacement(for: middleAnchorItem, index: 2, itemCount: 5, vertical: false,
            canvas: middleCanvas, headerOffset: 450, page: .actions)
        let middlePages = middleFirst.layout.pages.indices.map { page in
            UsageStrip.detailPlacement(for: middleAnchorItem, index: 2, itemCount: 5, vertical: false,
                canvas: middleCanvas, headerOffset: 450, page: .actions, sectionPage: page)
        }
        check(middlePages.count > 1 && middlePages.allSatisfy {
            $0.layout.pages.count == middleFirst.layout.pages.count && $0.frame.maxY < 450
        }, "中部锚点翻到短末页仍向同侧展开，页数和分页预算不跳变")
        check(middlePages.flatMap { $0.layout.visibleSections }.filter { $0.id.hasPrefix("goal") }.map(\.value).joined() == middleAnchorGoal
              && middlePages.flatMap { $0.layout.visibleSections }.filter { $0.id == "manual" }.count == 1
              && middlePages.flatMap { $0.layout.visibleSections }.filter { $0.id.hasPrefix("latest") }.map(\.value).joined() == "已完成一轮验证。",
              "展开方向固定后跨页全文无重复无遗漏，人工事项和进展各出现一次")
        check(StripDashboardPage.actions.title(for: managedItem) == "行动", "项目行动页保留已审阅名称")
        print("\(passed) UI checks passed")
    }
}
