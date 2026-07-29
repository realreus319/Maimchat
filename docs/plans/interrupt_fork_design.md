# 打断 / Fork / Decision 设计(固定版)

本文固定 Maimchat 的打断闭环设计,对齐参考实现 `l2d_backend`
(`docs/planner_interrupt_runtime.md`)。Maimchat 当前实现缺了"decision 后回到 NORMAL"
这条边(见 §6 差距),本文是目标形态。

## 0. 记号(msg 列表 / fork 视角)
所有 trigger 都封装成 msg、各占一槽。planner session 是一条 msg 列表:
- `U_old` / `U_new`：用户消息(`MSG` trigger),各占一槽。
- `⟨A_old*⟩`：未完成的在飞 replier 生成(被打断后转 `background_replier`)。
- `SYS(...)`：decision 的系统触发(`SYS` trigger),**单占一槽**,写入同一 planner turn。
- `A_old` / `A_new`：已发送的助手回复。

## 1. 三个 loop 状态
`IDLE`(空闲)/ `GENERATING`(跑前台回合)/ `DECIDING`(对后台回复做 adopt/kill 决策)。
`BACKGROUND` 不是 loop 状态,是 `ReplierTask` 的状态。

## 2. NORMAL(前台正常路径)
`replier` 当轮**同步**生成并发送回复;之后必须接 `wait_for` 作**停止锚**(没有它模型会反复调
replier 停不下来),然后 STOP。
```
U │ replier(生成+发送 A) │ wait_for │ STOP
```
DECISION 模式**不含** `wait_for`;`wait_for`、更新心情、触发动作、记忆等只在 NORMAL 出现。

## 3. 打断 → fork → decision
仅当 `trigger.canInterrupt()`(只有 MSG 为真)且 `state ∈ {GENERATING, DECIDING}`,且前台**确有可接管
的 `current_replier`** 时,才发生真 fork:
- `current_replier ⇒ background_replier`(在飞 `⟨A_old*⟩` 转后台,继续生成)
- `foreground_epoch += 1`(旧前台 sendReply 变 `STALE`,被丢弃)
- `state = DECIDING`,为这次打断补一轮 decision
- 把 `U_new` 与 `SYS(decision)` 都写入 planner session,再回读作 decision prompt

DECISION 工具白名单**只有两个**:`adopt_background_reply`、`kill_background_reply`(其余硬拒
`tool_not_allowed`)。

## 4. 两个决策的 msg 队列(核心)
**decision 链路只做"处置在飞回复 + 给出 SYS 返回 NORMAL";任何新生成都不在 decision 链路里。**

### ADOPT —— 复用在飞旧回复,零新生成
```
decision:  U_old │ U_new │ SYS(adopt) │ A_old
                                         └ adopt 等完在飞任务 → 发出已有的 A_old
                                            ↓ 返回 NORMAL
NORMAL:                                    (wait_for / 更新心情 / 动作 / 记忆 …) → STOP
```
- `SYS(adopt)` 告诉续跑的 NORMAL:**回复已发出,不要再 replier,只做事后操作**。

### KILL —— 丢弃在飞回复,交回 NORMAL 生成新回复
```
decision:  U_old │ U_new │ SYS(kill)
                              └ kill 终止后台 replier,记录 followup=U_new,自身不发
                                 ↓ 返回 NORMAL(state 显式设回 GENERATING)
NORMAL:                         A_new (replier 新生成) → (wait_for / 更新心情 …) → STOP
```
- `SYS(kill)` 告诉续跑的 NORMAL:**旧回复已弃,请对最新状态重新回复**。

| | decision 轮只做 | 终点 | 回复来源 |
|---|---|---|---|
| ADOPT | 等完并发出**已有的** `A_old` | 返回 NORMAL 做事后操作 → STOP | 复用在飞(零新生成) |
| KILL | 终止后台 + 给出 `SYS(kill)` | **止于 SYS,交回 NORMAL** | NORMAL 新生成 `A_new` |

## 5. 递归打断:decision 进行中又来新消息(对齐 l2d_backend §7.4)
若新消息到来时 `state == DECIDING` 且 `background_replier` 仍在:
- **不丢后台 replier**(始终只有一个在飞回复)
- **丢弃当前 decision 上下文**(取消正在跑的那轮 decision)
- **以新 trigger 重新进入一轮 decision**(仍对同一个 `⟨A_old*⟩` 做 adopt/kill)

msg 列表:
```
U_old
[⟨A_old*⟩ ⇒ background_replier]      ← 唯一在飞回复,全程保留
U_new1 │ SYS(decision1)               ← 被 U_new2 取消
U_new2 │ SYS(decision2)               ← 重新对同一个 A_old* 决策(latest 赢)
→ (无新打断时) adopt A_old* / kill→NORMAL(对 U_old+U_new1+U_new2 合并状态重答)
```
递归被**预算限制**:首次发送前最多连续打断 `_MAX_PRE_REPLY_INTERRUPTS = 3` 次,超过则新 trigger
入队不再 fork,避免失控链路。

## 6. 不变量(复刻必须保留)
- 打断判断在**消息进入时快照** `current_replier`,不是延迟看 live 状态。
- foreground 与 decision 是**同一个 loop 的状态机**,不是两个执行器。
- **真 decision 只在存在 `background_replier` 时成立**;无后台 replier 不进伪 decision。
- decision 工具**强白名单**(adopt/kill),DECISION 无 `wait_for`。
- **每个 trigger 先写 planner session,再回读 prompt**;`SYS` 也写入。
- 旧请求晚到的 assistant/tool 结果写回**原 turn**;`owner_epoch` 落后则 `stale_foreground`,不再对用户输出。
- **decision 结束必须显式把 `state` 设回 `GENERATING`**,否则后续消息被误判为"decision 中再打断"。
- 用户可见输出以 `replier` 的 `sent=true` 为准。

## 7. Maimchat 当前差距(待实现)
1. `decisionRegistry` 去掉 `wait_for`,只留 `adopt` + `kill`(`LocalToolRegistryFactory.kt`)。
2. decision 轮结束发 `SYS(adopt|kill)` 触发,**路由回 NORMAL**(当前 `PlannerLoop` 缺这条边,
   新消息被 decision 轮吞掉就没了)。
3. `adopt` 当轮发完即返回 NORMAL 做事后操作;`kill` 记录 followup → NORMAL 用 `replier` 新生成。
4. decision 后显式 `state = GENERATING`。
5. forceReplier 已删除(触发即异常,不再掩盖)。
6. 递归打断:DECIDING 中再来消息 → 保留 `background_replier`、丢弃当前 decision、以新 trigger 重决策。
