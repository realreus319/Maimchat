# 异步后台 worker（脱手 + 完成回调 + 后台态势感知）

## 目标模型

worker 一律**脱手后台**执行，不阻塞对话回合：

1. **派发**：`ask_ai_agent` 登记一个后台 worker（记录其**目标**）后立刻返回"已派发"，planner 正常收尾（回一句"我去后台做，稍等"→ `wait_for` 结束回合）。
2. **独立执行**：worker 在引擎（started 前台服务，T8）里独立跑；**最多 3 个并发**，超出排队。
3. **完成回调**：worker 结束 → 引擎把结果推给活着的 `:chat` → app 给 planner 发一条**完成 `SYS` trigger**（携带结果+目标）→ 新回合把结果交付用户。这就是"触发 wait_for 的等待"。
4. **后台态势**：只要有活跃/排队 worker，planner 处理的**每条 trigger 都自动附带一段"当前所有后台 worker 的目标+状态"**，planner 据此可回答、可掐、可追加。

消解了"worker 运行中来新消息"的竟态：无阻塞回合可打断，新消息即普通回合 + 后台态势。

## 决策（已定）

1. 并发上限 **3**；超出排队，状态块显示"排队中"。
2. 反向浏览器桥**改多实例**：每个 worker 独立 BrowserMcpServer + 独立浏览器上下文，按 taskId/port 路由（不再共用单一 `activeClient`）。
3. `wait_for` 也用于等待 worker 完成——派发后 `wait_for` 是常规操作，在 planner prompt 增加提醒。

## 组件与改动（映射代码）

### A. 派发式 ask_ai_agent（app）
- `AskAiAgentTool.execute`：不再 `await runTask`。改为向 `BackgroundWorkerManager` 登记任务（taskId、goal=task 摘要、originContext/agent/message）+ 异步启动，立即返回"已在后台启动任务（目标：…），完成后会通知"。
- 保留 MSG 触发限制（仅用户消息派发 worker）。

### B. BackgroundWorkerManager（app :chat，新）
- 活跃 worker 注册表：taskId→{goal, state(queued/running/done/failed), lastProgress, startedAt, origin}。
- 并发闸门（≤3，超出 queued）。
- 每个 worker 用独立 `ShellEngineClient.runTask`（跑在 manager 的 scope）；progress 回调更新 lastProgress；完成/失败 → 触发完成回调。
- 结果落盘走 T8 缓冲（跨重启）。

### C. 完成 → SYS trigger（app）
- worker 完成 → manager 调 `plannerLoop.submitTrigger(SYS)`，payload.text = "【后台任务完成】目标：<goal>；结果：<worker text>。请把结果交付给用户。"（失败则相应文案）。复用现有 SYS 续回合链路。
- 常驻监听：把 T8 的"仅重启补投"升级为常驻 `WorkerResultRecovery`（:chat 存活期间也即时收），孤儿结果（进程被杀后完成）同样走 SYS trigger。

### D. 后台态势上下文块（app）
- 新增 `BackgroundWorkerContextProvider : PlannerPromptContextProvider`，`blocksFor` 返回活跃/排队 worker 列表（目标+状态+已跑步数）。经 `CompositePlannerPromptContextProvider` 挂上，和 memory/mood 并列 → 每回合自动带。
- 无活跃 worker 时返回空（不污染上下文）。

### E. 多实例浏览器桥（engine）
- 现状：单 `BrowserMcpServer`（单 mcpPort）+ `ShellService.activeClient` 单值 → 多 worker 串台。
- 改：每个 run-task 起一个**独立 BrowserMcpServer 实例**（独立端口），注入该 worker 的 settings.json；反向桥按"发起该桥调用的 server/port"路由回**该 worker 对应的 client**（`jobs`/client 按 taskId 存）。
- app 侧 `BrowserOverlayHost`/`BrowserController` 需支持**多套浏览器上下文/标签组**（每 worker 一组），避免共用 WebView 串台。

### F. wait_for 提醒（prompt）
- planner_system.md（gentle/default/xiaoqian）：增加"把任务派发给 ask_ai_agent 后，它在后台跑、完成时会自动回来通知你；此时正常收尾（`wait_for`），不要干等也不要反复追问；结果回来时你会收到一条系统提示再交付给用户。后台可同时有多个任务，你随时能看到它们的目标与状态。"

## 分阶段实现

- **P1 核心异步**（不含多实例浏览器）：B 注册表(并发≤3, 暂不支持并发浏览器) + A 派发 + C 完成 SYS trigger + 常驻监听 + D 态势块 + F 提醒。先让"派发→后台→完成交付"单 worker 跑通，再放开到 3 并发（无浏览器并发）。
- **P2 多实例浏览器**：E 改造 BrowserMcpServer 多实例 + 反向桥按 taskId 路由 + app 多浏览器上下文。
- 每阶段真机/模拟器 E2E 验证。

## 风险
- 多 worker 并发资源（proot×3）在低端机内存压力；靠并发闸门 + T8 前台服务缓解。
- 多实例浏览器上下文是 P2 主要复杂点（WebView 多标签隔离）。
- 派发式改动会让**短任务也变两段式**（先"稍等"后结果）；本期按"一律脱手"，后续可加"短任务同步"启发式。
