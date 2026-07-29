# Maimchat 待办与计划

滚动记录当前未完成项 + 正在做的方案。完成的划掉/移除。

## 待办清单（滚动，标注最终状态）

1. **#21 应用侧 WebView per-task 隔离 —— 方案 C ✅ 已实现（代码），并发渲染待视觉验证**
   - `BrowserOverlayHost` 重构为 `tabsByTask: Map<taskId, BrowserTabs>` + `currentTaskId`；`tabs` 变
     computed getter；`ensureShown(ctx, taskId)` 只建一次 window(`ensureWindow`)、按 taskId 取/建
     `BrowserTabs`、换进 holder。`ShellEngineClient.handleBrowserAction(svc, taskId, …)` 用
     `BrowserOverlayHost.browserMutex.withLock { ensureShown(ctx, taskId) → execute }` 串行。
     引擎侧 per-task server + `KEY_TASK_ID` 路由已就绪。回归 A1/A3（非浏览器）4/4 通过、编译通过。
   - **仍需真机 + 视觉 agent 截图验证并发浏览渲染**（多 WebView detach/attach 行为，规则10）。

2. **异步模型 × fork/decision 打断对齐 ✅ 安全部分已修**
   - `PlannerLoop` 新增 `activeTriggerType`，`shouldInterrupt` 加 `&& activeTriggerType != SYS`——
     **后台完成(SYS)回合不再被打断**（避免把结果交付 fork 进 decision）。T2/T3 的 MSG 长回复打断不受影响。
   - 残留（无害）：派发回合（MSG）被新消息打断时仍会走一次 decision；异步下回合很短、A3 去重后不影响交付，
     暂不深改（彻底修需按"是否已派发/是否长回复"分流，风险高）。

3. **【✅ 已完成】跨回合 worker 上下文缺失**（"把刚才那个文件发给我"失败的根因）
   - 三处一起修，模拟器验证通过（`scripts/e2e/cross_turn_file.sh` 通过）：
   - **3.1 compact 背景注入 ✅**：`ChatWebSocketManager.compactPlannerContext` 用 compact 模型
     （复用 replier=qwen）把近期 `standard_messages`（注意查询用 `activeModelKey` 做 context_id，
     不是 trigger 的 user-scoped contextId）压成背景，`BackgroundWorkerManager.runOne` 在 runTask 前
     拼进任务开头。日志验证："用户项目代号为蓝鲸行动…创建/root/note.txt"。compact prompt 要写"宁多勿漏"，
     否则会过度压成"无"。
   - **3.2 worker 固定 session + 自查 ✅**：`WorkerRuntime` 给每次 run 加 `--append-system-prompt`
     （`SESSION_HINT`）告诉 worker 它历史会话存在 `/opt/cfg/sessions/`、可 `ls -t` + `cat` 自查。
   - **附带修**：planner_system.md(三persona)【文件交付】改为——用户用"刚才/那个/之前那个"指代时，
     直接交给 ask_ai_agent（它有背景/会话可自查），**别说"找不到"**、回"稍等我去找"。这是关键：
     否则 planner 因无对话历史而直接拒绝。

4. **T8 快任务窄竞态 —— 暂保持现状（有意）**：秒级任务完成+ack 后 app 恰在 2–5s 回复窗口被杀→结果丢。
   彻底修需"回复落库后再 ack + 恢复时按 taskId 去重"，但 taskId 未与完成回复关联（回复 id 是
   `worker_done_<uuid>`），要引入"已交付 taskId 持久集"且在回复落库处记录——改动深、且做不好会**重复投递**
   （比偶发丢失更糟）。故保持 ack-on-receipt，把窄竞态记为已知限制。主场景（运行中被杀）由 T8 恢复覆盖。

5. **worker 真·mid-run 注入 —— 【worker 基座✅ / 引擎·应用集成待做】**：
   - **✅ worker python 基座已实现并验证**（`worker/python_src/main.py`）：`--print --input-format stream-json`
     时进入**常驻多轮循环**——守护线程逐行读 stdin(NDJSON) 入队，主循环每条 `{"type":"user",...}` 跑一轮
     并输出该轮 `terminal`；`{"type":"control","subtype":"interrupt"}` 通过 `loop.call_soon_threadsafe`
     置 `QueryStreamConfig.signal` **中断当前轮**（每轮开头 clear，故 turn 间的过期 interrupt 被忽略）；
     `end`/EOF 干净退出。一次性/文本模式行为**逐字节不变**（回归 a1 通过，5050 仍正常）。协议：
     stdin=user/interrupt/end 三种；stdout 每轮一个 `terminal` 作边界。
   - **✅ 引擎会话原语已实现（编译通过）**：`WorkerRuntime.startSession(...)` + `LiveSession`
     （`inject`/`interrupt`/`close`/`kill`）——起常驻 worker 进程（`--input-format stream-json`）、
     首条即 initialTask、`parseEvent` 的 `terminal` 事件切每轮 `Result` 回调 `onTurnDone`。
   - **✅ 引擎+应用接线全部实现，端到端跑通（但有间歇 flaky，待硬化）**：
     (a) `ShellProtocol`/`ShellEngineProtocol` 加 `MSG_RUN_SESSION`/`MSG_INJECT`/`MSG_CLOSE_SESSION`；
     (b) `ShellService.startSessionTask`（新，不动一次性 startTask）：`runtime.startSession` 起常驻会话、
         `sessions[taskId]` 存活、`MSG_INJECT`→`session.inject`、每轮 `onTurnDone`→saveResult+MSG_RESULT
         （复用 T8 缓冲）、空闲 60s 宽限后 close；
     (c) `ShellEngineClient.runSession`（新，保持绑定、每轮 `onTurnResult` 回调、`SessionHandle` 暴露
         inject/close）；
     (d) `BackgroundWorkerManager`：runOne 改用 runSession + `CompletableDeferred` 等 close 释放并发闸门；
         `sessionHandles` 持有句柄；**dispatch 加"同会话已有 running worker→注入而非派新"启发式（v1）**；
     (e) planner decision（inject vs 派新）——**未做**，当前是 (d) 的启发式默认注入。
   - **✅ 已修的关键 bug（会话中途死 = 主要 flaky 源）**：`session reader stopped InterruptedIOException`
     根因**不是 worker**，而是引擎 idle-grace-close 把**首轮时长**算进空闲窗：一轮流式输出时 `parseEvent`
     忽略 stream/thinking 事件→无 onProgress→`lastActivity` 不更新→turn 超 60s 被误判空闲 kill。已修：
     `LiveSession.lastActivityMs` 在**每行 stdout** 更新、grace-close 用它、且**首轮完成前绝不 idle-close**
     (`firstTurnDone`)；加 worker 侧兜底 try/except（单轮崩不拖死会话）+ 每轮独立 goal(`WorkerInfo.goals` FIFO)。
     修完后**注入的常驻会话不再中途死**、注入轮结果稳定产出。
   - **⚠️ 端到端仍 flaky，但根因转移到 planner 层 + 测试本身（非注入机制）**。用持久 `l2dchat.log` 抓到真相：
     (i) **测试假阳性**：任务里含 `print(999888777)`，planner 在**派发回合**就把 999888777 从代码里念出来了
     （"代码正在后台运行中，输出显示为 999888777"），`grep 999888777` 命中的是这条**预回显**、不是 worker 真
     完成——A 断言不可靠，应换成 prompt 里不出现的答案。
     (ii) **planner 偶尔忽略追问**：某些run planner 收到 B("算2^100") 直接 `STOP`、**根本没调 ask_ai_agent**
     （`planner ended WITHOUT sending a reply`）→ 没派发/没注入。LLM 非确定性。
     (iii) **worker 完成的 SYS 回合偶尔没触发**：抓到的窗口里 A 的真完成之后**没有任何 planner 回合**，SYS_A
     没落地。需核查 onComplete→submitBackgroundTrigger→PlannerLoop 在"会话还活着(grace 中)"时的投递。
   - **✅ 已定位并修 (iii) 的真根因（决定性日志）**：给 `PlannerLoop.submitTrigger/processQueuedTrigger`
     加 `[trig]` 日志、`onComplete` 加 `[worker]` 日志后抓到：两条 worker 完成 SYS **都 submit 且 process 了**，
     但**两个 SYS 回合都 `replySent=false`**——planner LLM 在完成触发上偶尔直接 STOP 不调 replier，而
     `ToolCallingPlannerTriggerProcessor` 早前"forceReplier removed by design"→结果被静默丢弃。**修复**：
     对 **SYS 触发**（且仅 SYS）加回 forceReplier 兜底（复用 `ReplierTool` + 2 次重试，用 trigger payload 的
     完成文本当 `thinking`），保证后台完成一定交付；前台 MSG 回合仍保持不兜底（暴露真缺陷）。已编译+部署。
   - **✅ 真正的元凶 = 并发闸门 permit 泄漏（这才是"连跑几次后 worker 完全不启动"的根因）**：`runOne`
     `gate.withPermit{ … done.await() }`，`done` 只在 `onClosed` 触发时完成，而 `onClosed` 只在 client
     unbind 时触发——但**引擎从不告诉 client 会话结束了**，client 的 runSession 永远保持绑定→`onClosed`
     不触发→`done.await()` 永久挂起→**permit 永久泄漏**；泄漏满 3 个后所有派发卡在 `gate.withPermit`，
     worker 再也不启动（表现为"引擎无 start session 日志、proot 进程 0、a1 也超时"，冷启动 app 立刻恢复）。
     **修**：新增 `MSG_SESSION_ENDED`(引擎→app)，`startSessionTask` finally 里发；`ShellEngineClient.runSession`
     收到即 `cleanup()`→onClosed→done 完成→放闸；再加 `SESSION_HARD_CEILING_MS=15min` 兜底
     `withTimeoutOrNull(done.await())`，信号万一丢也不会永久泄漏。**修后连跑 6 次不再退化**（之前 3-4 次必死）。
   - **✅ forceReplier(SYS-only) 验证生效**：日志实锤 `SYS completion had no reply — forced replier (sent=true)`。
   - **✅ 最后一格 = SYS 完成不再打断前台**：残留的 A 偶发丢，是 SYS 完成到达时**打断**了前台回合→走
     decision 的 `adopt_background_reply`→约 1/3 不回。修：`PlannerLoop.submitTrigger` 的 shouldInterrupt 加
     `trigger.triggerType != SYS`——后台完成**永不打断**在跑的回合，改为排队、回合结束后按正常 SYS 路径
     （→forceReplier 保底）投递。
   - **✅ 端到端验证**：`scripts/e2e/inject_test.sh`（A=`print(7**11)` 去假阳性、B=2^100，运行中注入）——
     修全后**连跑 4/4 全绿**（A、B 每次都交付）；leak 修复前连跑 6 次 B 恒交付、A 4/6。**#5 至此可靠可用。**
   - **诊断日志**：`[trig] submit/process`、`[worker] onComplete`、`WorkerRuntime session ended/turn done`
     暂留，稳定后可降级/删除。

6. **文件输入（对话→worker）**：用户明确暂缓。

## 问题 3 实现方案（本轮进行）

### 3.1 compact 背景注入
- 位置：`BackgroundWorkerManager.runOne` 在 `runTask` 前，调用 `compactContext(origin)` 得到背景摘要，
  拼成 `【背景信息】<摘要>\n\n【本次任务】<task>`。发生在 worker 起跑前（异步、不阻塞派发返回）。
- `compactContext: suspend (WorkerOrigin) -> String?` 由 `ChatWebSocketManager` 实现：
  取该会话近期消息（含上次 worker 交付/文件气泡）→ 调 **compact 模型** 压成简短背景 → 返回。
- compact 模型：可配置，默认复用 planner 的 LLM（`OpenAiCompatibleClient` + plannerModel）；
  prompt 让它只输出"用户近期意图 + 已产出的东西（文件路径等）+ 与本次任务相关的事实"，简洁。

### 3.2 worker 固定 session + 开头提示
- 引擎已设 `CLAUDE_PY_CONFIG_DIR=/opt/cfg` → session 存 `/opt/cfg/sessions/{id}.json`（worker 每轮落盘）。
- 在 worker 任务 prompt 的环境提示（`AskAiAgentTool.ENV_HINT` 或引擎 append-system-prompt）里加一段：
  "你过往每次任务的完整会话记录都存在 `/opt/cfg/sessions/` 下（每个 .json 一次）；当你需要回忆自己
  之前做过什么、生成过哪些文件时，可以 `ls -t /opt/cfg/sessions/` 并读取最近的 json。"
- 让 worker 能自查历史，与 3.1 的"planner 视角背景"互补。
