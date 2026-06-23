# Maimchat 缺陷全量修复计划（一次性完成）

> 目标：把 `docs/DEFECT_STATEMENT.md` 中**所有仍未修复**的缺陷一次性修完。
> 已修复 9 项（见 DEFECT_STATEMENT.md 0bis 节）不在本计划内。
> 执行协议：每个 Batch 完成后跑 `./gradlew :app:testDebugUnitTest`；全部完成后跑 `./scripts/check_build.sh` + 真机 qwen3.7-plus 复测；遇到回退/绕过必须在总结中说明。
> 原则（遵循全局约束）：干净实现、不保留向后兼容分支、少异常处理、不 mock/跳过、测试放 `app/src/test`。

剩余缺陷共约 24 项，分 7 个 Batch。Batch 1（记忆系统）是核心与最大工作量。

---

## Batch 1 — 记忆 / 印象 / 心情系统（核心重构）

修复 D-MEM-1/2/3/4/5/6/7/8/9。这是用户重点关注项。

### 现状关键事实（已核实）
- `RuntimeStateDao`（`storage/RuntimeDaos.kt`）已有 `queryImpression(subjectId)` 和 `queryImpressions(limit)` 两种读法。
- 印象写在 `StateTools.kt` 用 LLM 给的 `user_id` 作 `subject_id`；replier 读用 `sender_id`(渠道常量)。键不匹配 → write-only。
- `PlannerPromptBuilder` **已支持** `PlannerPromptContextProvider`（注入 `[planner_context]` 块），但 `blocksFor` 是同步签名，且当前只接 `BackgroundReplierPromptContextProvider`。
- `ChatDatabase` version=1，无 migration；`MemoryEntity` 无 category 列。

### 修复步骤
1. **D-MEM-1 印象键统一**（致命）。本应用是单用户/agent 场景：印象作用域定为 `(contextId, agentId)` + 单一规范 subject。
   - `StateTools` 写印象时 `subjectId` 不再用 LLM 任意 `user_id`，改用从 routing 派生的稳定 subject（取 trigger 的 receiver/local user id，常量化为 `local_user`，或 routingKey 派生）。
   - `RoomReplierPromptContextProvider` 读印象改用同一 subject（或直接 `queryImpressions(limit=1)` 取最近），不再用 `sender_id`。
   - 文件：`core/tools/StateTools.kt`、`storage/RoomReplierPromptContextProvider.kt`。
2. **D-MEM-4 印象合并**（高）。`tool_update_user_impression` 改 read-modify-write：先 `queryImpression`，把本次未提供的字段保留旧值再 upsert。
   - 文件：`StateTools.kt`。
3. **D-MEM-2 语义检索**（高）。引入 SQLite FTS4 虚拟表镜像 memory.content（Room `@Fts4`），`searchMemories` 改 `MATCH`（分词匹配），并按 FTS rank + importance 排序。无需 embedding 模型，端上可用。
   - 新增 `MemoryFtsEntity`(@Fts4, contentEntity=MemoryEntity) 或独立 FTS 表 + 触发器同步；DAO 加 `searchMemoriesFts`。
   - 文件：`core/message/RuntimeEntities.kt`、`storage/RuntimeDaos.kt`、`storage/ChatDatabase.kt`、`StateTools.kt`。
4. **D-MEM-3 记忆进规划器**（高）。
   - 把 `PlannerPromptContextProvider.blocksFor` 改为 `suspend`（仅一个生产实现 + Background 实现需同步改）。
   - 新增 `RoomPlannerPromptContextProvider`：注入精简的 memory（top-N 重要）/impression/mood 摘要为 `[memory]/[impression]/[mood]` 块。
   - 用组合 provider（memory + background tasks）接入 `LocalRuntimeFactory` 的 planner/decision 处理器。
   - 文件：`reply/PlannerPromptBuilder.kt`、新增 `storage/RoomPlannerPromptContextProvider.kt`、`reply/BackgroundReplierPromptContextProvider.kt`、`core/LocalRuntimeFactory.kt`。
5. **D-MEM-5 心情衰减**（高）。读取时按 `now - updated_at_ms` 把 valence/arousal 朝中性线性衰减（半衰期常量，纯读时计算，无需调度器）。
   - 文件：`storage/RoomReplierPromptContextProvider.kt`（或 MoodState 读路径）、`StateTools.kt`（toPromptText 处）。
6. **D-MEM-6 记忆上限**（中）。`memory_store` 后按 `(context,agent)` 保留上限（如 500），超出删最低 importance/最旧。DAO 加 `deleteLowestPriorityMemories` / `countMemories`。
   - 文件：`RuntimeDaos.kt`、`StateTools.kt`。
7. **D-MEM-9 category 持久化**（低）。`MemoryEntity` 加 `category` 列，写入并可按 category 过滤检索。
8. **D-MEM-7 索引**（中）。FTS 解决检索；给 `importance` 加索引用于 `queryMemories` 排序。
9. **D-MEM-8 Room 迁移**（中）。`ChatDatabase` version → 2，加 `addMigrations(MIGRATION_1_2)`（新增 category 列、FTS 表、索引）；保留 `exportSchema`。**不使用** destructive fallback（保数据）。
   - 文件：`ChatDatabase.kt`。

### 测试
- 印象写后能读回（同 subject）；只更新 summary 不丢 score（合并）。
- FTS：「喜欢苹果」可被「水果/苹果」查询命中，子串查询仍可。
- 规划器 prompt 含 memory/impression/mood 块。
- mood 随时间衰减；memory 超上限淘汰；migration 1→2 不丢数据（Room MigrationTestHelper）。

---

## Batch 2 — 环境触发成本控制（D-ENV-2）

ENV 触发（空闲 5min/交互/快照…）当前无条件投给规划器 → 计费。

### 修复
- `LocalLlmSettings` 加 `environmentRepliesEnabled: Boolean = false`（默认关）。
- ENV 触发仍可驱动工具/状态，但**默认不生成 LLM 回复**：在 ENV→planner 派发处按设置门控（`ChatWebSocketManager` 的环境触发发射 + `LocalChatRuntime.submitEnvironmentTrigger` 路径）。关闭时不调用 LLM。
- 空闲定时器仅在设置开启时排程。
- UI：运行设置对话框加一个开关「响应环境事件（实验/计费）」。
- 文件：`core/config/LocalLlmSettings.kt`、`chat/ChatWebSocketManager.kt`、`core/LocalChatRuntime.kt`、`ui/screens/ChatWithModelScreen.kt`（设置对话框）。

### 测试
- 默认关：ENV 触发不产生 LLM 调用（fake client 0 次）。
- 开启：ENV 触发产生一次规划器回合。

---

## Batch 3 — 多模态与解析配置（D-PERC-1、D-PERC-2）

### 修复
- **D-PERC-1 图片标记契约**：`InboundBuilder.normalizeContentBlocks` 在文本中为每个 image 块注入 `[imageN]` 标记（与 `validateTriggerMultimodal` 契约一致），使图片消息既能通过校验又能正确送达 LLM。同步处理 emoji/voice 块。
  - 文件：`core/inbound/InboundBuilder.kt`，校验侧 `core/trigger/Trigger.kt` 复核。
- **D-PERC-2 parser.json 生效**：从 agent 资源/配置加载 `ParserConfig`（command_prefixes / mention_names / preserve_multimodal_blocks），注入 `PerceptionProcessor`；缩窄默认 `mentionPatterns`（去掉会误命中邮箱的 `@(\S+)`，改为基于配置的 mention 名）。
  - 文件：`core/perception/ParserConfig.kt`、`PerceptionDispatcher.kt`、`PerceptionProcessor.kt`、`DefaultMessageParser.kt`、`config/DefaultAgentProfileSeeder.kt` 或资源加载处。

### 测试
- 带图入站 → trigger 校验通过 + prompt 含 image_url，不再抛异常。
- mention 仅命中配置的名字；邮箱 `a@b.com` 不再升 HIGH。

---

## Batch 4 — LLM 客户端硬化（D-LLM-2/4/5/6/7）

文件主要在 `core/llm/OpenAiCompatibleClient.kt`。

- **D-LLM-2 finish_reason**：消费 `LENGTH`。若 content 非空但被截断 → 记录/标注；若 content 空且 `LENGTH` → 走与空回复一致的降级（不静默成功）。
- **D-LLM-4 流中错误帧**：`parseStreamChunk` 检测 `{"error":...}` 帧 → 抛 `OpenAiHttpException`（触发重试/外露），不再吞成空 Completed。
- **D-LLM-5 流式重试**：`chatCompletionStream` 的连接建立阶段纳入 `executeWithRetry`（仅连接前，已产出 token 后不重试，避免重复）。
- **D-LLM-6 错误体外露**：`OpenAiHttpException` 解析 body 的 `error.message` 并并入异常 message，便于诊断（model not found/配额等）。
- **D-LLM-7 死代码**：移除未消费的 `RequestTimeout` tag 及其 data class。

### 测试
- 流中错误帧 → 抛异常（MockWebServer/fake interceptor）。
- 非 2xx body 的 error.message 出现在异常里。
- finish_reason=length+空 content → 不被当正常完成。

---

## Batch 5 — 回复器 / 后台任务健壮性（D-REP-2/4/5/7/8）

文件：`core/tools/ReplierTask.kt`、`ReplierTaskManager.kt`、`ReplierTool.kt`、`DecisionTools.kt`、`ReplierPromptBuilder.kt`。

- **D-REP-2 后台任务回收**（高）：`ReplierTaskManager` 加 TTL + 「新前台回合 sweep」：进入新前台 MSG 时清理过期/孤儿 BACKGROUND 任务（取消协程 + clearTask）。保证 adopt-or-kill 不会无限泄漏。
- **D-REP-4/5 生命周期竞态**：`ReplierTask` 所有状态写（含 `runGeneration` 的 COMPLETED/FAILED 终态）统一进 `synchronized(lock)`；终态与 `moveToBackground`/`cancel` 互斥，避免「完成被覆写回 BACKGROUND 丢失 replyText」。`moveToBackground` 配合 `NonCancellable` 让后台生成真正脱离取消（语义与名字一致）。
- **D-REP-7 wait_for 超时语义**：`WaitForTool` 区分「完成」与「超时部分」，超时返回带 `timed_out=true` 的结构化结果，不冒充成功。
- **D-REP-8 自定义模板占位符**：`ReplierPromptBuilder.renderTemplate` 对未知 `{key}` 不原样保留（替换为空或剥除），null 字段处理一致化。

### 测试
- 打断后完成的回复不丢、可 adopt；后台任务在新前台回合后被回收（计数归零）。
- 终态竞态：并发 complete + moveToBackground 不产生 BACKGROUND-终态丢失（重复跑/压力）。
- wait_for 超时结果带 timed_out。
- 自定义模板未知占位符不外泄。

---

## Batch 6 — 运行时杂项健壮性（D-PERC-3、D-RUNTIME-1/2）

- **D-RUNTIME-1 轮级超时**：`LocalChatRuntime.handleMessage` 的 `await()` 加整轮超时（如 `maxToolRounds × perCall` 上界或独立配置），超时优雅失败而非永挂。
- **D-RUNTIME-2 重复消息去重**：`registerPending` 对相同 `(routingKey, messageId)` 优雅去重（忽略/合并），不再抛异常。
- **D-PERC-3 队列/worker 上界**：感知 `Channel` 与 planner `PriorityQueue` 加合理上限或丢弃策略并 `log`；不同 routing key 的 worker/loop 加 LRU 上限或闲置淘汰（或显式 log 不淘汰的取舍）。
- 文件：`core/LocalChatRuntime.kt`、`core/perception/PerceptionWorker.kt`/`PerceptionDispatcher.kt`、`core/reply/PlannerLoop.kt`/`ReplyLayerFactory.kt`。

### 测试
- 重复 id 不抛异常、第二次被忽略。
- 轮级超时触发优雅失败（fake 永挂 client）。

---

## Batch 7 — UI 接线 + 全量校验

- 设置对话框补：Batch 2 的环境回复开关；（Batch 1）记忆相关无 UI。
- 跑 `./gradlew :app:testDebugUnitTest`（期望全绿，新增约 20+ 测试）。
- 跑 `./scripts/check_build.sh`（unit + androidTest 编译 + assemble）。
- 真机 qwen3.7-plus 复测：多轮对话（含「记住我名字→后续回忆」验证 D-MEM-1/2/3 真正生效）、图片输入（D-PERC-1）、native+compat 双模式、空闲不刷 LLM（D-ENV-2）。截图存 `docs/manual_verification/screenshots/`。
- 更新 `DEFECT_STATEMENT.md` 标记全部已修；更新记忆。

---

## 执行顺序与依赖
1 → 2 → 3 → 4 → 5 → 6 → 7。Batch 1 最大、风险最高（Room 迁移、prompt 接口改 suspend），先做并单测稳固再继续。各 Batch 之间编译+单测为门。

## 风险与取舍
- **Room 迁移**：必须写正确的 1→2 migration（新列/FTS/索引），用 MigrationTestHelper 验证；失败会导致升级崩溃。
- **PlannerPromptContextProvider 改 suspend**：是 API 破坏性改动，但仅内部使用、单实现，符合「不留兼容」原则。
- **FTS**：选 FTS4（兼容性稳）；中文分词用默认 unicode61/simple tokenizer，子串场景保留 LIKE 兜底。
- **prompt 变长**：注入记忆/印象会增加 token；用 top-N + 截断控制。
- **工作量**：约 24 项、~25 文件、~20+ 新测试，是一次大改；将分 Batch 提交式推进，全程不 mock/绕过，遇阻明确上报。

## 待确认（如有偏好）
- 是否需要把环境回复开关默认设为「关」（计划默认关，省 token）。
- FTS 中文分词是否够用，或接受「FTS + LIKE 兜底」的混合检索。
- 是否在本仓库直接提交（当前在 `local-runtime-migration` 分支，工作树有未提交的修复）。
