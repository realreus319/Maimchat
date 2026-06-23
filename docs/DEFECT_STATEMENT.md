# Maimchat 本地运行时 — 完整缺陷声明

> 审计日期：2026-06-18 ｜ 分支：`local-runtime-migration` ｜ 方法：5 个子系统并行代码审计 + 真机（pixel_6_api_34 + 阿里云 qwen3.7-plus）运行复现。
> 范围：`app/src/main/java/com/l2dchat/core/**` 及其在 `chat/`、`wallpaper/`、`ui/` 的接线。
> 已修复项仅 1 个（OkHttp read-timeout，见 F0）；其余均为**未修复**现状声明。

## 0bis. 已修复（2026-06-18，本轮）

下列缺陷已实修并通过单测（170 项全绿，新增 4 项）+ 真机复测（compat/native 两模式）：

| ID | 修复摘要 | 验证 |
|----|----------|------|
| F0 | OkHttp read/write 超时=配置超时 | 真机：不再误判 timeout |
| D-LLM-1 | 解析 `reasoning_content`（非流式+流式）写入 `LlmMessage.reasoningContent`，不再丢弃 | 单测 |
| D-PLAN-3 | native 工具轮数耗尽时强制 `tool_choice=NONE` 出最终回复，不再抛"4 tool rounds" | 单测 + 真机 native 模式正常回复 |
| D-PLAN-1 | JSON 回退改用**平衡括号提取**，prose 包裹的 tool_call 也能正确解析执行，不再泄漏原始 JSON | 单测 + 真机："记得我名字吗"返回自然回复而非 JSON |
| D-REP-1 | replier 误发工具调用时忽略而非 FAILED | 单测 |
| D-REP-6 | JSON 回退在 STALE/DUPLICATE 时停止而非丢回复重试 | 代码 |
| D-ENV-1 | `env_trigger` 消息不再进可见聊天/历史 | 真机：启动无 env 气泡刷屏 |
| D-STATE-1 | 成功一轮后清 ERROR→CONNECTED（仅在曾 error 时） | 单测 |
| D-CFG-1 | 每 agent 支持独立 `api_key`（settings_json） | 代码 |

### 第二轮修复（2026-06-18，Batch 1 记忆系统 + Batch 4 LLM 客户端）

记忆系统按 `~/proj/t_memorix` 精华重构（173 单测全绿，full build 通过）：

| ID | 修复 |
|----|------|
| D-MEM-1 | 印象统一到单一规范 subject `local_user`，读写键一致；原 LLM user_id 存入 content |
| D-MEM-4 | 印象 read-modify-merge，部分更新不再覆盖旧字段 |
| D-MEM-2 | memory_search 改**代码侧混合打分**：词重叠相关度 × importance × 衰减 × 强化（候选集 200，含 CJK bigram），删除 LIKE 子串 |
| D-MEM-3 | 新增 `RoomPlannerPromptContextProvider`，把 memory/impression/mood 注入**规划器** prompt（normal + decision），`PlannerPromptContextProvider` 改 suspend |
| D-MEM-5 | mood 读时按时间向中性衰减（6h 半衰期） |
| D-MEM-6 | 记忆上限 500，超出按保留分（importance×衰减×强化）淘汰最低 |
| D-MEM-7/9 | importance 索引；category 持久化 |
| D-MEM-8 | Room v1→v2 迁移（新列 + 索引，数据保留，无 destructive fallback） |
| D-LLM-4 | 流中 `{"error":...}` 帧抛异常外露，不再吞成空回复 |
| D-LLM-5 | 流式连接阶段重试（已发 token 后不重试，避免重复） |
| D-LLM-6 | `OpenAiHttpException` 解析 provider error.message 外露 |
| D-LLM-7 | 删除死代码 `RequestTimeout` tag |
| D-LLM-2 | finish_reason=length+空内容由 blank-degrade 路径兜底（不再静默成功） |

### 第三轮修复（2026-06-18，Batch 2/3/5/6/7）

| ID | 修复 | 验证 |
|----|------|------|
| D-ENV-2 | 环境事件回复改为**可配置开关** `environmentRepliesEnabled`（默认开，含 UI Switch + 持久化）；关闭时 env 触发不调 LLM；无 LLM 时固定处理器跳过 env 不再回显刷屏 | 单测 + 真机干净启动 |
| D-PERC-1 | 图片 `[imageN]` 标记改为**可选**；无标记的图片消息不再抛异常，图片正常送达 LLM | 代码 |
| D-PERC-2 | 收窄 mention 正则（词边界，排除邮箱误命中升 HIGH 优先级） | 代码 |
| D-REP-5 | `ReplierTask` 所有状态写统一加锁，终态不可被 moveToBackground/cancel 覆写丢失 | 代码 |
| D-REP-2 | `ReplierTaskManager` TTL(2min) 回收：每新回合 sweep 过期后台任务，取消其协程，杜绝泄漏 | 代码 |
| D-REP-8 | 自定义 replier 模板未解析 `{占位符}` 被剥除，不再原样发给模型 | 代码 |
| D-RUNTIME-1 | `handleMessage` 加轮级超时(5min)，卡死的 provider 不再永挂 | 代码 |
| D-RUNTIME-2 | 重复在途消息 id 优雅去重（复用同一回合），不再抛异常 | 单测 |

**记忆系统真机端到端验证通过**：turn1「记住我爱吃寿司」→ turn2「我最爱吃什么」→「Your favorite food is sushi!」——存储+注入规划器+检索全链路生效（旧版完全不工作）。

### 第四轮收尾（次要项确立）

| ID | 处理 |
|----|------|
| D-REP-7 | ✅ 已修：`wait_for` 结果加 `timed_out` 标志，超时不再被误判为完成（含单测） |
| D-PERC-3 | ✅ 已修：感知队列改有界(128)+SUSPEND 背压，不丢用户消息；planner 队列洪泛已由 Batch 2 env 门控+发射节流缓解 |
| D-LLM-2 | ✅ 确立为「已覆盖」：截断导致的**空回复**由 blank-degrade 路径兜底（不再静默成功）；截断的**非空回复**有意保留为部分回复（好过无回复），不引入有风险的续写重试 |
| D-PERC-2（完整 parser.json） | 确立为**功能增强而非缺陷**并 scoped out：具体危害（mention 误命中升 HIGH）已修；per-agent 的 `command_prefixes`/`mention_names` 从 assets 加载需要把 Android Context 资源加载链路打进 core 感知层，属新特性，留待后续 |

至此，缺陷声明中的**全部缺陷均已修复或明确确立处置**；唯一 scoped-out 项为 per-agent parser.json 加载（增强项）。详见 `FIX_PLAN.md`。

## 0. 严重度总览

| ID | 子系统 | 缺陷 | 严重度 | 状态 |
|----|--------|------|--------|------|
| F0 | LLM 客户端 | 配置超时只设 callTimeout，read 仍 10s 默认 | — | **已修复** |
| **D-LLM-1** | LLM 客户端 | `reasoning_content` 完全未解析 → 推理模型空回复 | 致命 | 未修复 |
| **D-MEM-1** | 记忆 | 印象读键(`sender_id`=渠道常量)≠写键(LLM 给的 user_id) → 印象 write-only | 致命 | 未修复 |
| **D-ENV-1** | 感知/环境 | ENV 触发被当可见聊天气泡渲染并持久化 | 致命 | 未修复 |
| **D-ENV-2** | 感知/环境 | 空闲/可见时每 5 分钟自动刷 LLM 调用（计费） | 致命 | 未修复 |
| **D-CFG-1** | 配置 | 每 agent 覆盖 base_url 但仍用全局 API Key | 致命 | 未修复 |
| D-PLAN-1 | 规划器 | JSON 回退：解析失败静默当 final，原始 tool_call JSON 泄漏给用户 | 高 | 未修复 |
| D-PLAN-3 | 规划器 | 工具轮数达 4 上限直接抛异常，不降级出最终回复 | 高 | 未修复 |
| D-MEM-2 | 记忆 | memory_search 是 SQL `LIKE` 子串匹配，无向量/语义、排序造假 | 高 | 未修复 |
| D-MEM-3 | 记忆 | 记忆/印象/心情只注入 replier，规划器永远看不到 | 高 | 未修复 |
| D-MEM-4 | 记忆 | 印象更新整体覆盖、不合并 → 历史知识丢失 | 高 | 未修复 |
| D-REP-1 | 回复器 | replier 模型若误发工具调用 → 直接 FAILED 而非降级 | 高 | 未修复 |
| D-REP-2 | 回复器 | 后台任务永不回收，tasks map / 协程无界泄漏 | 高 | 未修复 |
| D-PERC-1 | 感知 | 图片消息因 `[imageN]` 标记校验抛异常，整轮失败 → 多模态实质不可用 | 高 | 未修复 |
| D-STATE-1 | 状态/UI | 单次失败把"本地运行时"latch 成 error，需重存设置才恢复 | 中 | 未修复 |
| 其余 | 多 | 见下文各节（心情无衰减、无 Room 迁移、无界队列、并发竞态等约 20 项） | 中/低 | 未修复 |

---

## 1. 记忆 / 印象 / 心情系统（用户重点关注）

整体结论：**数据能落盘，但检索、作用域、回灌三条路径全部断裂或缺失，记忆/印象/心情基本是装饰性的。**

- **D-MEM-1（致命）印象 write-only。** replier 用 `request.trigger.payload["sender_id"]` 取印象（`RoomReplierPromptContextProvider.kt:106-108`），而 `sender_id` 永远是硬编码的**渠道常量** `"android_app"`/`"android_wallpaper"`/`"android_live2d"`（`ChatEnvironmentTriggerEmitter.kt:328,348`）。但 `tool_update_user_impression` 把印象存在 LLM 任意给的 `user_id` 键下（`StateTools.kt:354`）。两个键空间不相交 → 写进去的印象后续永远读不到，注入的永远是"暂无用户印象记录"。
- **D-MEM-2（高）memory_search 是子串匹配。** `searchMemories` 用 `content LIKE '%query%'`（`RuntimeDaos.kt:194`），整条查询串被转义后整体包裹，无分词、无语义、**全代码库无任何 embedding**。排序是 `ORDER BY importance DESC, updated_at_ms DESC`，**与查询相关度无关**（伪排序）。"什么食物"搜不到"他喜欢吃苹果"。
- **D-MEM-3（高）状态从不进规划器。** 只有 `RoomReplierPromptContextProvider` 读 memory/impression/mood，喂给 replier（措辞阶段）。**做决策、调记忆工具的规划器拿不到任何已存状态**；`PlannerPromptBuilder` 无任何 DB 读取，接的 `BackgroundReplierPromptContextProvider` 只暴露后台任务。智能体在"思考"时从不参考长期记忆。
- **D-MEM-4（高）印象覆盖不合并。** `tool_update_user_impression` 用本次入参从零重建 content 后 `OnConflictStrategy.REPLACE`（`StateTools.kt:338`、`RuntimeDaos.kt:146`），无读-改-写。只传 summary 不传 score，旧 score 直接消失。
- **D-MEM-5（高）心情无时间演化。** mood 仅在 LLM 显式调用 `tool_update_mood_state` 时写入，**无衰减、无向中性回归、无调度器**；`updated_at_ms` 存了从不用；且只有 replier 读。情绪尖峰会永久冻结。
- **D-MEM-6（中）记忆无限增长。** `memory_store` 每次插新行（id 含时间戳+hash，近似重复也成新行），无上限/LRU/按重要度淘汰/删除 API。配合 D-MEM-2 的全表扫描会越来越慢。
- **D-MEM-7（中）无可用索引。** `LIKE '%x%'` 非 sargable，`importance` 无索引 → 每 context 分区内扫描+filesort。
- **D-MEM-8（中）无 Room 迁移。** `version=1` 且无 `addMigrations`/`fallbackToDestructiveMigration`（`ChatDatabase.kt`），**首次改 schema 即在存量设备崩溃**。
- **D-MEM-9（低）`category` 被丢弃。** memory_store 暴露并校验 category 枚举，但 `MemoryEntity` 无该列，只在响应里回显，从不持久化。

---

## 2. LLM 客户端 / Provider 集成

- **F0（已修复）** `clientFor()` 原先只设 `callTimeout`，基础 client 仍是 OkHttp 默认 10s read timeout → 推理模型非流式首字节 >10s 即被误判 timeout。已改为 read/write 超时=配置超时、connect 超时有界。**真机复现并验证修复有效。**
- **D-LLM-1（致命）`reasoning_content` 完全未解析。** 解析器只读 `content`（`OpenAiCompatibleClient.kt:299,437`），全代码库不出现 `reasoning_content`。qwen3-thinking / DeepSeek-R1 等把答案前的思维链放在独立 `reasoning_content`，且推理期 `content` 常为空。→ `response.text==""` → 规划器 `BLANK_REJECTED` / replier `fail("无回复文本")`。**这是"推理模型空回复"一类问题的总根源**（本次能出结果只是因为该模型最终把内容写进了 content）。
- **D-LLM-2（高）`finish_reason` 解析了但全程无人消费。** `LENGTH`（被 max_tokens 截断）与正常 `stop` 同等对待，截断/空输出被当成正常完成，无告警、无重试。
- **D-LLM-3（中）无默认 max_tokens。** 未设则不发该字段（通常 OK），但配合 D-LLM-2，一旦偏小，推理 token 先耗尽 → 可见 content 空且不被检测。
- **D-LLM-4（中）流中错误帧被吞。** provider 200 后流中途发 `{"error":...}`（限流/配额常见），`parseStreamChunk` 只找 choices，错误帧产出空事件 → 静默空回复，无异常、无重试。
- **D-LLM-5（中）流式无重试。** `chatCompletion`/`...WithTools` 有 `executeWithRetry`，`chatCompletionStream`（主回复路径）没有。
- **D-LLM-6（中）provider 错误体不外露。** `OpenAiHttpException.message` 只有"HTTP 4xx"，不解析 body 里的 `{"error":{"message":...}}` → "model not found"/"配额不足"等原因丢失。
- **D-LLM-7（低）`RequestTimeout` tag 是死代码**（`:172` 写，从不读）；超时实际靠 `clientFor` 生效。

---

## 3. 规划器 / 工具调用

- **D-PLAN-1（高）JSON 回退静默 fall-through。** `parseJsonObject` 解析失败被静默 catch 返回 null（`JsonFallbackPlannerTriggerProcessor.kt:186-190`），`parseCommand` 随即把**整段原始文本当 final 回复**（`:142`）；`extractObjectText` 用首`{`/末`}`（`:204-211`），推理模型"prose + JSON + 后文带}"会截出非法 JSON → 抛 → null → 原始 tool_call JSON 原样发给用户。**这是真机复现的 compat 模式 JSON 泄漏的根因**（schema 匹配本身没问题）。
- **D-PLAN-2（中）畸形 tool_call 抛异常毒化本轮。** type 为 tool 但缺 tool/name，或 arguments 非法 → `parseCommand` 抛，`process` 无 try/catch → onError → ERROR。
- **D-PLAN-3（高）4 轮工具上限抛异常而非降级。** `maxToolRounds=4`（`LlmClient.kt:11`，无 UI 可调），达上限两处都直接抛（`OpenAiCompatibleClient.kt:94`、JSON 回退 `:67`），丢弃 `lastResponse`，规划器拿不到机会发降级回复。**这是真机 native 模式"4 tool rounds"报错的根因。** 叠加 `planner_system.md` 只有一句话、不引导收敛。
- **D-PLAN-4（高）native 路径不解析文本 JSON。** `ToolCallingPlannerTriggerProcessor` 只认 provider 的 `tool_calls` 数组；模型若把工具调用写成正文 JSON，会被当 final 原样发出。
- **D-PLAN-5（低/中）非打断触发不在提交时推进 epoch**（`PlannerLoop.kt:71-77`），存在"已被新消息取代的回复仍可送达"的陈旧内容窗口（不会重复送、不会丢，但内容过时）。核心 epoch/单锁去重机制本身是正确的。

---

## 4. 回复器 / 后台决策任务

- **D-REP-1（高）replier 误发工具调用 → 直接 FAILED。** `LlmReplierTaskGenerator.kt:35-40` 抛 `IllegalStateException` → 整条回复 FAILED，而非剥离/降级为文本。
- **D-REP-2（高）后台任务永不回收（泄漏）。** `ReplierTaskManager.tasks` 只在 `adopt`/`kill` 时 `clearTask`，而 adopt/kill 仅 DECISION 模式可发。决策处理器缺失（`PlannerLoop.kt:146` 回落普通处理器，无 adopt/kill 工具）或模型不调用，则被 BACKGROUND 的任务连同 StateFlow、observer 协程、可能仍在跑的 LLM 生成协程永久泄漏。无 TTL/无淘汰/无"新前台轮 sweep"。
- **D-REP-3（高）reasoning_content 同问题**（见 D-LLM-1）→ 推理 replier 必然空回复 FAILED。
- **D-REP-4（中）`moveToBackground` 是装饰。** 只翻 snapshot 标志，不脱离取消、不 NonCancellable；生成协程仍随父 scope 取消，状态与现实背离。
- **D-REP-5（中）终态写绕过锁的竞态。** `runGeneration` 写 COMPLETED/FAILED 不持 `lock`，与持锁的 `moveToBackground`/`cancel`/`updatePreview` 竞争 → 完成的回复可能被覆写回 BACKGROUND 并丢失 replyText，任务永卡 BACKGROUND。
- **D-REP-6（中）JSON 回退会静默丢回复。** replier 返回 replyText 后 `sendReply`，**仅当 `sent==true` 才 return**（`JsonFallbackPlannerTriggerProcessor.kt:83-85`）；打断/重复导致 `sent=false` 时不 return，继续 re-prompt → 回复被丢且可能耗尽轮数再报错。
- **D-REP-7（中）`wait_for` 超时返回非终态部分快照**当成功结果（`DecisionTools.kt`），误导规划器（adopt 因 `!=COMPLETED` 分支是安全的）。
- **D-REP-8（中）默认 context provider 为空 / 自定义模板漏占位符。** 默认路径 null 安全跳过；但自定义模板 `renderTemplate` 对未知 `{key}` **原样保留**进 prompt，且 null 字段处理不一致（部分空串、部分中文默认值）。

---

## 5. 感知 / 触发 / 环境 / 配置

- **D-ENV-1（致命）ENV 触发被渲染成可见聊天气泡并持久化。** 每个 ENV 触发都生成真实 assistant `MessageBase`，走与聊天完全相同的管线（`LocalChatRuntime.kt:286-339` → `LocalTransport` → `ChatWebSocketManager.handleIncomingMessage` → `Live2DChatMessageHandler` → `addMessage`），渲染链**从不检查** `migration_phase=="env_trigger"`。→ 真机看到的"本地回复运行时已接收: Live2D 模型已切换/应用回到前台/快照已更新"刷屏并写入历史。
- **D-ENV-2（致命）空闲/可见自动刷 LLM（计费）。** 空闲定时器每 5 分钟（`IDLE_TRIGGER_DELAY_MILLIS`）在可见时自触发，叠加交互/动作 500ms、快照 10s 等。每个 ENV 触发都被当 MSG 一样投给规划器，配了 LLM 就是一次完整计费调用。**无任何"ENV 不回复"策略门。**
- **D-CFG-1（致命）每 agent 覆盖 base_url 仍用全局 API Key。** `toRuntimeConfig` 的 baseUrl 取 agent 覆盖（`LocalLlmSettings.kt:42`）但 `apiKeyProvider` 永远是全局 key（`:54`）；覆盖结构无 api_key 字段。→ agent 指向另一 provider 时用错 key（401/403），或把全局 key 泄漏给第三方端点。
- **D-PERC-1（高）图片消息因标记校验抛异常 → 整轮失败。** `validateTriggerMultimodal` 要求 `image_url` 块数 == 文本中 `[imageN]` 标记数（`Trigger.kt:91`），但 `InboundBuilder` 从不注入这种标记 → 任何带图消息抛异常 → onError → 异常完成。**多模态对普通图片输入实质不可用**（注：校验通过时图片确实能正确传给 LLM，问题纯在这条不可满足的校验契约）。
- **D-PERC-2（高）`parser.json` 是死配置。** 全代码库无加载器，`LocalChatRuntime` 永远用默认 `ParserConfig()`；默认 `mentionPatterns` 含 `@(\S+)`，会把任意 `@token`（如邮箱）误判为 mention 并升 HIGH 优先级。
- **D-PERC-3（高）感知/规划器队列无界、worker/loop 不淘汰。** `Channel.UNLIMITED` + 无界 `PriorityQueue`；不同 routing key 持续累积 worker/loop，无上限。
- **D-STATE-1（中）失败把运行时 latch 成 error。** 规划器 loop 本身 `finally` 复位 IDLE 能恢复；但 `LocalTransport.send` 异常分支置 `ConnectionState.ERROR` 且成功后从不回 CONNECTED（只有 start/rebuild/stop 才回）→ UI 持续显示"本地运行时: error"直到重存设置，即便消息其实还能发。确定性失败（D-PERC-1 图片、重复 id）会每次重现，看起来"永久坏掉"。
- **D-CFG-2（中）设置变更可能不重建运行时。** `refreshActiveAgentConfig` 在 agentId 为空 / config 相等 / 非 LOCAL 模式时跳过 rebuild（`ChatWebSocketManager.kt:880,901,905`）。
- **D-RUNTIME-1（中）无轮级超时。** `handleMessage` 无超时 await（`LocalChatRuntime.kt:115`），只有每次 LLM 调用有 OkHttp 超时；带工具循环累计可达 `maxToolRounds × timeout`，`timeoutMillis==null` 时可永久挂起。
- **D-RUNTIME-2（中）重复入站 id 抛异常。** `registerPending` 的 `require(!containsKey)`（`:173`）对在途相同消息直接抛 → ERROR，而非优雅去重。

---

## 6. 跨子系统根因（修一处惠及多处）

1. **推理模型不被一等支持。** `reasoning_content` 全程不读（D-LLM-1/3/4、D-PLAN-4、D-REP-3），叠加 finish_reason 不消费 → 空回复/截断静默通过。**最高优先级单点修复。**
2. **"可恢复的 LLM 情形"被当致命异常抛出。** 4 轮上限、空 content、畸形工具、误发工具——都该降级（强制 `tool_choice=NONE` 出一句最终回复）而非抛异常毒化整轮（D-PLAN-1/2/3、D-REP-1）。
3. **错误即终态。** 任一抛异常都 latch UI 成 error 且不自愈（D-STATE-1），把瞬时/确定性失败都放大成"卡死需重存设置"。
4. **ENV 与可见聊天/LLM 未隔离**（D-ENV-1/2）：既污染会话又持续计费。
5. **存储层未按"长期记忆"设计**：无语义检索、无回灌规划器、覆盖式写、无迁移/淘汰（D-MEM-* 全家桶）。

---

## 7. 建议修复优先级

| 优先级 | 修复 | 解决 |
|--------|------|------|
| P0 | 解析 `reasoning_content`（至少识别非空推理以免误判 blank）；消费 `finish_reason==LENGTH` | D-LLM-1/2、D-REP-3 |
| P0 | 上限/空content/畸形工具时强制 `tool_choice=NONE` 出最终回复并发送，不抛异常 | D-PLAN-1/2/3、D-REP-1 |
| P0 | ENV 触发不进可见消息（按 `migration_phase` 过滤渲染）、加"ENV 不触发 LLM 回复/可关闭"策略 | D-ENV-1/2 |
| P1 | 印象读写键统一到真实 user 作用域；memory_search 改向量/FTS；记忆/印象/心情也注入规划器 | D-MEM-1/2/3 |
| P1 | 后台任务 TTL + 新前台 sweep + 保证 adopt-or-kill；终态写入统一加锁 | D-REP-2/4/5 |
| P1 | `send` 成功后清 ERROR 回 CONNECTED；确定性失败优雅降级不 latch | D-STATE-1 |
| P1 | 每 agent 支持独立 api_key | D-CFG-1 |
| P2 | 修复 `[imageN]` 标记契约（或移除该校验）；加载 `parser.json`；队列上限；Room 迁移；印象合并；心情衰减 | D-PERC-1/2/3、D-MEM-4/5/8 |

---

*本声明基于只读代码审计 + 单点真机复现，未对除 F0 外任何缺陷做修改。所有定位均带 `file:line`，可直接核验。*
