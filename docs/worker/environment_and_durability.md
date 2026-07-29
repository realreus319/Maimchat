# Worker 环境、工具链与结果持久性

记录 worker（on-device proot/Alpine + cc_research python 端口）的能力边界，以及一批健壮性修复。

## 1. 跨架构：本就同时支持 x86_64 和 arm64

`engine/src/main/assets/` 同时打包 `proot/<abi>/` + `rootfs-<abi>.tar.gz` 两套；
`WorkerRuntime.kt` 按 `Build.SUPPORTED_ABIS` 主 ABI 运行时选取，`build.gradle.kts` 无 `abiFilters`。
x86 模拟器与 arm64 真机用同一个 APK。PocActivity 自检 + 真实任务（`print(2**10)`→1024）在模拟器均通过。

## 2. 工具链：可运行时安装、且持久

- proot 现在带 **`--link2symlink`**（`WorkerRuntime.runTask`/`runProbe`）。Android 应用私有存储不允许硬链接，
  没有这个开关时 `apk add gcc` 会在 `as`/`ld`/`x86_64-alpine-linux-musl-gcc` 等硬链接文件上报
  `Permission denied`，导致 gcc 装了却不能汇编/链接。加上后：`apk add gcc musl-dev` → gcc 真实编译运行（实测退出码 42）。
- **网络 + apk 正常**：rootfs 内含 `/sbin/apk`，repos = Alpine v3.21，resolv.conf 运行时刷新。`apk update`/`add` 可联网。
- **持久性**（见 §4）：运行时装的包会在 App 更新后保留，无需每次重装。
- **worker 不再找借口**：`AskAiAgentTool.ENV_HINT` 明确告知 worker 它在完整 Alpine（有 root、gcc/python3/apk），
  必须真正编译/运行，禁止以“环境受限”糊弄。此前 kimi 会假称“无法执行 gcc”。

> 为什么不把 gcc 预装进镜像？完整 gcc+binutils ≈ 200MB/arch，会让 APK 从 ~88MB 膨胀到 ~300MB+；
> 且重烤 base 镜像需要每种架构各自的运行环境（arm64 真机）。既然运行时安装可用且持久，默认走运行时安装。
> 需要离线/预装的团队可用 `worker/bake-toolchain.sh`（可选，按架构在设备上烤）。

## 3. 结果持久性：worker 工作中 App 被杀，结果不再丢失（原 T8）

**问题**：`ask_ai_agent` 委派的 worker 任务运行在 `:chat` 进程；App 退到后台被系统回收（`am kill`）时
`:chat` 一并被杀，`ShellService` 仅是 bound 服务 → 解绑即销毁 → `pool.shutdownNow()` 中断在途任务 →
结果丢失，且重启时孤儿轮次被直接判失败，不续接。

**修复**（engine + app 双侧）：
- **engine 存活**：客户端 `startForegroundService` 拉起，`ShellService` 成为 **started 前台服务**，
  解绑不再销毁（`onUnbind` 不取消任务，`stopIfIdle` 仅在空闲时停）。任务跑到完成。
- **结果落盘缓冲**：任务完成（成功/失败）总是把结果写到 `engine files/pending/<taskId>.json`（含来源
  contextId/agentId/messageId），投递给当前客户端；客户端 `MSG_RESULT_ACK` 后删除；无客户端则留存。
- **重连补投**：任何客户端 `MSG_REGISTER` 时 engine flush 所有未 ack 的缓冲结果。
- **App 侧恢复**：`:chat` 启动时 `WorkerResultRecovery` 绑定 engine + register，收到 flush 的结果后，
  用 `buildReconciledReply` **直接作为一条 agent 回复投进聊天**（不走 planner/环境触发——原始轮次已亡，
  且环境回复在本构建被关闭会被丢弃），随后 ack。
- 每任务客户端按 taskId 过滤，只认自己的结果（避免把补投的旧结果错当自己的）。

**验证**：
- 模拟器 x86_64：发 worker 阶乘任务 → 后台 → `am kill` → engine 存活跑完 → 落盘结果 → 重启 →
  聊天出现「（我刚在后台帮你把这个任务跑完了）3628800」→ 缓冲 ack 清空。
- **真机 PLJ110 arm64（ColorOS/Android14）已验证**：发一个 `time.sleep(40)` 的长任务 → 后台 →
  `am kill`（main+:chat 死）→ **engine + proot(worker) 存活**（`engine=alive worker=3` 持续到睡眠结束）
  → 落盘 1 个结果 → 重启 → 聊天出现「…3628800」→ 缓冲清空。日志见 `Background started FGS: Allowed`
  + engine 反复 `client gone` 但任务照跑完。回归 T1/T2/T7 全绿。

**已知残留（窄窗竞态，未修）**：若 worker 任务**很快完成**（秒级）后结果已投递并 ack，而 App 恰在随后
planner 生成回复的 ~2–5s 窗口内被杀，则该结果会丢失（缓冲已被 ack 删除、回复又没落库）。原因是
ShellEngineClient 收到结果即 ack。彻底修复需改为“回复落库后再 ack”或“恢复时按轮次状态去重后再 ack”，
会带来缓冲堆积/重复投递风险，故暂缓。**主场景（长任务运行中被系统回收）不受影响，已在真机验证。**

## 4. rootfs 持久性（版本化解包）

`WorkerRuntime.RUNTIME_VERSION` 写入 `rootfs/.extracted`。`ensureInstalled` 仅当版本不符才解包，
且是**覆盖式**（`extractTar` 就地覆盖、不清空目录）：
- 同版本 App 更新 → 不重解包 → **运行时装的包（如 gcc）保留**。
- 版本升级 → 覆盖刷新 base、保留用户新增文件。
- 仅卸载会清空（不可避免）。改了 proot 二进制或 rootfs 资产时务必 bump `RUNTIME_VERSION`。

## 5. 其他环境限制

proot 基于 ptrace 逐 syscall 拦截（需 seccomp 模式），较慢；无真 TTY（ioctl TIOCGWINSZ 被 SELinux 拒，
curses/交互程序不可用）；`-0` 是假 root，不能 mount/namespace/cgroup/加载内核模块/跑 Docker；
网络随宿主；类 chroot 无 init/常驻守护。
