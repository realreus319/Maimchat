# 拆包式本地 Worker Agent 引擎 — 实施计划（已锁定）

## 目标
给 Maimchat 的对话 planner 一个**设备上的自主编码/执行子代理（worker agent）**：planner 把任务委派给
worker，worker 在一个真实 Linux 环境里自主调用 shell/文件/git 等工具完成，再把结果流式回灌。
要求：**不要求用户主动安装第三方 App**，由主 App 自动安装一个**无启动入口的引擎包**承载该环境。

## 已锁定决策
1. **rootfs 来源**：随引擎 APK 内嵌（真自包含、无需联网）。
2. **发行版**：**Alpine**（musl，小；可用 toybox 替 busybox 让许可证更干净）。
3. **ABI**：**arm64-v8a + x86_64**（真机 + 模拟器开发/CI）。
4. **worker**：**Claude Code 的 Python 移植版**（本机 `/home/tcmofashi/proj/cc_research/python_src`）。
   - planner **不**直接执行命令；只通过 `delegate_to_worker` 工具委派给该 worker。
   - 选它而非 opencode：**已内置 OpenAI 兼容 provider**、**纯 Python(无原生模块, Android 友好)**、**有全部源码**
     → LLM 配置/请求格式/agent 循环/工具集全部可改 = 硬要求“全部把握在我们手上”彻底满足。

### ✅ 桌面 PoC 已验证（2026-06-25）
用 `python3.12` + Maimchat 的 qwen 凭据，headless 跑通：
- 纯问答：`--print "2+2"` → `2+2等于4。`（rc=0）。
- **自主工具任务**：`--print --output-format stream-json --permission-mode bypassPermissions "建文件→cat"`
  → qwen 自主调用 **Write + Bash**，落了文件，`terminal: completed`。事件流含
  `assistant/tool_use/tool_result/thinking/content_block_*/message_*/terminal`（即主 App 要消费的协议）。
- **运行方式**（关键，照抄到 Android）：
  - 入口：`python -m python_src.entrypoints.cli`（不是 main.py，需 `PYTHONPATH=<cc_research>` 或在该目录）。
  - provider：`MODEL_PROVIDER=openai OPENAI_API_KEY=<qwen key> OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1 OPENAI_MODEL=qwen3.7-plus`。
  - **配置隔离**：`CLAUDE_PY_CONFIG_DIR=<私有目录>`。⚠️ 否则 `~/.claude_py/settings.json` 的 `model`/`env` 块
    会**压过**环境变量（PoC 中一度强制走 deepseek-v4-pro）。Android 上我们自建该目录+settings.json → 反而是干净的控制点。
  - 工具放行：`--permission-mode bypassPermissions`（非交互）。
- **运行时硬要求**：**Python ≥ 3.11**（用到 `asyncio.Runner`，3.10 会报 `module 'asyncio' has no attribute 'Runner'`）。Alpine 自带 3.11/3.12，OK。

## 架构（拆包 + 子代理）
```
com.l2dchat (主App, targetSdk 36, 闭源)                 com.l2dchat.shell (引擎包, targetSdk 28, 无入口, GPLv3)
 ├─ planner (现有对话/人设层)                             ├─ ShellService (Messenger: 控制/握手/生命周期)
 ├─ delegate_to_worker : Tool ──委派任务──►              ├─ Alpine rootfs + proot (filesDir, targetSdk28 可执行)
 │     (接入现有 ToolRegistry)                            ├─ Node + opencode (headless server, 跑在 proot 内)
 ├─ WorkerClient: Messenger握手 + loopback HTTP ◄─事件流─ │     └─ 自主调用 shell/文件/git 完成任务
 └─ ShellEngineInstaller (PackageInstaller 引导安装)      └─ 前台服务(运行时通知)
        同一签名密钥 → signature 级权限只允许同 key 应用绑定
```
**通信分层**：
- **Messenger（安全控制面）**：跨包绑定，签名权限保护。用于握手（下发 worker 监听端口 + 一次性 token +
  **运行时注入 LLM API key**）、启动/停止/健康检查。
- **Loopback HTTP（数据面）**：opencode 在引擎内监听 `127.0.0.1:<随机端口>`；主 App 经回环连它，按
  opencode 的 headless 协议提交任务、流式收 agent 事件。端口随机 + token 鉴权，防同机其他应用蹭。

**为什么拆包**：W^X 执行豁免按各 App 自己的 `targetSdkVersion` 决定 SELinux 域。引擎包钉 targetSdk=28 →
可 execve 可写目录里的 guest 二进制（proot/Node/python）；主 App 保持 36 不受影响。GPL 隔离在引擎包，
主 App 仅经 IPC 通信（arm's length，非衍生作品）可闭源。opencode 宽松许可，worker 层不污染。

## 关键约束（已确认接受）
- 安装非静默：`PackageInstaller` 需用户授“安装未知应用”+ 每次装/更新弹一次系统框。
- 引擎包在「设置→应用」可见（无图标但非系统级隐身）。
- 长任务需前台服务 + 常驻通知。
- 引擎包 GPLv3、独立开源仓；仅侧载分发。
- **LLM API key 运行时经安全 IPC 下发给 worker，绝不烤进引擎包/不入库。**

---

## 分阶段计划

### Phase 0 — 脚手架
- 新 Gradle 模块 `:engine`（`com.android.application`，applicationId `com.l2dchat.shell`，targetSdk 28/minSdk 24）。
- 签名：release 共用 `signing.properties` 同 key；debug 共用机器 `~/.android/debug.keystore`（同机即同 key）。
- 自定义权限 `com.l2dchat.permission.SHELL_ENGINE`（`signature`）：引擎声明，主 App `uses-permission`。

### Phase 1 — W^X / proot PoC（**最先做，验证地基**）
- 引擎包内打包 `proot`（`jniLibs/<abi>/libproot.so`）+ 最小 Alpine rootfs（assets，白名单）。
- 首启解压 rootfs → `filesDir/rootfs`；`ProcessBuilder(nativeLibDir/libproot.so, -r rootfs, /bin/sh -lc "uname -a")`。
- **在 emulator(x86_64) 跑通 `uname -a`，确认 targetSdk 28 在目标 Android 版本上确实能执行可写目录二进制、无 SELinux denial。**
- 失败回退：改“jniLibs 静态工具集（toybox/musl/python）”——无 apt、但 Play 兼容、无需拆包。

### Phase 2 — Linux 运行时 + Node/opencode 入镜像
- rootfs 预烤：apk 装 `nodejs`、`git`、`python3`、常用工具；npm/预装 `opencode`（裁剪 node_modules）。
- **体积实测**：内嵌 Node+opencode 可能 100MB+。若过大，仅“Node/opencode 层”破例改首启拉取（rootfs 基底仍内嵌）。
- 验证：proot 内 `node -v && opencode --version`，并能以 headless/server 模式起服务监听 loopback。

### Phase 3 — 引擎包骨架（headless）+ Termux 基础设施复用
- 无 launcher Activity；`ShellService`(`exported=true`，签名权限保护) + `WorkerProtocol`（Messenger）：
  - C→S：`MSG_REGISTER` / `MSG_START_WORKER(apiKey,baseUrl,model)` / `MSG_STOP` / `MSG_HEALTH`。
  - S→C：`MSG_EVENT_READY(port,token)` / `MSG_EVENT_WORKER_EXIT` / `MSG_EVENT_ERROR`。
- 复用 Termux：bootstrap 安装逻辑、`termux-exec`(LD_PRELOAD 修 exec 路径)、（交互式 PTY 用 terminal-emulator，二期）。砍 UI、重签名、改包名。
- 前台服务 + 通知；空闲超时自停 worker/进程。

### Phase 4 — 主 App：安装/引导
- 加 `REQUEST_INSTALL_PACKAGES`；`engine-release.apk` 作主 App 资产（`.gitignore` 白名单）。
- `ShellEngineInstaller`：查已装/版本/签名一致 → 缺失或过旧经 FileProvider + `PackageInstaller.Session` 安装（用户确认一次）；引导“安装未知应用”授权。

### Phase 5 — 主 App：WorkerClient（IPC）
- 跨包绑定 `ShellService`（显式 ComponentName + setPackage + 签名权限 + `BIND_AUTO_CREATE`），镜像 `ChatServiceClient`。
- 握手：`MSG_START_WORKER` 下发 key/baseUrl/model → 收 `MSG_EVENT_READY(port,token)`。
- 数据面：连 `127.0.0.1:port`，按 opencode headless 协议提交任务、流式收事件 → `SharedFlow<WorkerEvent>`。

### Phase 6 — Agent 工具接入（零侵入）
- `DelegateToWorkerTool : Tool`（`core/tools/`）：
  - `definition`：name=`delegate_to_worker`，schema `{task:string, context?:string, timeout_ms?:int}`。
  - `execute()`：`withContext(IO)` 经 WorkerClient 提交任务、聚合/截断事件流 → `ToolExecutionResult`。
- 注册进 `LocalToolRegistryFactory.normalTools()`，**置于配置开关后**（默认关）。
- 提示词：`PlannerSystemPromptProvider`/`PlannerPromptContextProvider` 注入“你可把编码/执行类任务委派给 worker 子代理”的说明与边界。
- 安全：任务超时、事件/输出截断、worker 工作目录限 rootfs、危险操作策略（可选确认）。

### Phase 7 — 测试与验证
- 引擎：instrumented（emulator x86_64）跑 proot+node+opencode 起服务并完成一个最小任务；条件允许 arm64 真机复测。
- IPC：握手 + loopback 往返测试（仿 `LocalTransportTest`）。
- 端到端：聊天里给 planner 一个需要执行的任务 → `delegate_to_worker` → opencode 在环境内完成 → 结果回灌续答（debug + 真 provider 凭据）。
- 全程 emulator-5554 + logcat，无崩溃/SELinux denial。

### Phase 8 — 构建打包
- Gradle：`:engine:assembleRelease` 产物 APK 任务化拷入 `:app` 资产；engine `versionCode` 升级触发重装。
- `.gitignore` 白名单：engine apk + rootfs（+ 可选 Node 层）。GPL 源码可得性说明（引擎独立仓 + written offer）。

---

## 风险与回退点
- **W^X**（最高优先，Phase 1 先验证）：若目标 Android 收紧 → 回退“jniLibs 静态工具集”。
- **体积**：Node+opencode 内嵌过大 → 仅该层首启拉取。
- **opencode headless 协议细节**：以 PoC 实测为准（server 模式/事件格式/鉴权），文档据实更新。
- **性能**：proot ptrace 有开销；worker 多为 IO/网络绑定，可接受；不适合大型编译。
- **安全**：loopback 端口须随机 + token；API key 仅运行时内存态。

## ✅ Android on-device PoC 已验证（2026-06-25, emulator x86_64 / Android 14 / SDK34）
`:engine` 模块（applicationId `com.l2dchat.shell`, **targetSdk 28**, 无依赖第三方 App）打包 proot+Alpine+Python worker，
真机跑通：
- **Phase A（离线）**: proot → `python 3.12.13` + `worker import OK`（exit 0）。
- **Phase B（在线）**: worker 由 **qwen3.7-plus** 驱动，在 proot/Alpine 内**自主用 Write+Bash 工具**创建
  `android_poc.txt`、写入 `hi-from-android-proot`、`cat` 验证（exit 0，文件落盘内容正确）。
→ **W^X 执行豁免（targetSdk 28 从 filesDir execve）+ proot + Alpine + Python + Claude-Code worker + qwen + 工具** 全链成立。

**备料（已固化在 scratchpad，可直接复用）**：
- proot 套件来自 Termux deb：`proot` + `loader`/`loader32` + `libtalloc.so.2` + `libandroid-shmem.so`（x86_64，~330KB）。
- rootfs：Alpine minirootfs + `apk add python3 bash py3-urllib3` + 注入 `python_src`（worker）。**worker 仅需 urllib3 一个第三方库**，其余纯 stdlib。打包后 ~23MB(gz)/57MB(解压)。

**三个 Android 专属坑（务必照搬到真正实现）**：
1. **proot 必须用 seccomp 模式（不要设 `PROOT_NO_SECCOMP=1`）**：纯 ptrace 模式在此内核加载次级共享库（`libpython*.so`）会 ENOSYS（“Function not implemented”）；seccomp 模式原生放行即正常。这是整个 PoC 最关键的一处。
2. **guest 环境要自己给**：proot 继承的是宿主(Android)的 PATH/env，需显式传 `PATH=/usr/bin:/bin:…`、`HOME=/root`、`PYTHONPATH=/opt/worker`、`CLAUDE_PY_CONFIG_DIR=/opt/cfg`，并用绝对路径 `/usr/bin/python3`。
3. **DNS**：烤进 rootfs 的 `/etc/resolv.conf` 是构建机的，设备上要运行时改写（emulator 用 `nameserver 10.0.2.3` + 公共 DNS）。
4. （打包）AAPT2 会自动解压 `.gz` 资源并去掉后缀 → 打进 APK 的是 `assets/rootfs.tar`（用 `tar xpf` 解，不要 `z`）。

## ✅ 工程化成品（x86_64 模拟器，2026-06-25）
全链作为产品在 emulator 上端到端跑通（不止 PoC）：
1. 主 App 启动委派时检测引擎包未装 → **`PackageInstaller` 自动安装内嵌的 `assets/engine/engine.apk`**（系统确认框，需 `REQUEST_INSTALL_PACKAGES`）→ 安装成功。
2. 主 App **跨包绑定** `com.l2dchat.shell/.ShellService`（signature 权限，两包同 debug key）→ Messenger 发任务 → 引擎前台服务里 `WorkerRuntime` 解压 rootfs + proot 跑 worker → worker(qwen) 自主用工具完成 → 结果经 IPC 回传，`exit=0`。

落地组件：
- 引擎包 `:engine`（targetSdk 28，headless）：`WorkerRuntime`（封装 proot/seccomp/DNS/guestenv）、`ShellService`（Messenger 协议 `ShellProtocol`，前台服务）、`<permission signature>`，调试自测 `PocActivity`。
- 主 App：`worker/ShellEngineProtocol`（协议镜像）、`ShellEngineClient`（跨包绑定 + `suspend runTask` 流式）、`ShellEngineInstaller`（PackageInstaller 自动装）、`WorkerDebugActivity`（自测入口）、`core/tools/DelegateToWorkerTool`（接入现有 `ToolRegistry`，经 `extraNormalTools` 一路传到 `LocalToolRegistryFactory`；creds 用 planner 同款 qwen，运行时经 IPC 下发）。
- 打包：`:app` 经 Gradle `copyEngineApk` 依赖 `:engine:assembleDebug`，把引擎 APK 作为生成资源打入主 App（app-debug ~65MB，含 27MB 引擎）。

### ✅ (a) 聊天自主委派 + (c) stream-json 进度（2026-06-25 验证）
- **(a)**：在聊天里发一句英文任务（"Use your local worker to create a file …"），**qwen planner 自主调用了 `delegate_to_worker`** → 引擎 worker 运行 → 最终回复正确说出文件内容。logcat 实证 `WorkerRuntime: run task` + `ShellService` 被 `com.l2dchat` 绑定；`run-as cat files/rootfs/root/chatdemo.txt` = `hello-from-planner`。
- **(c)**：worker 改用 `--output-format stream-json`，`WorkerRuntime.parseEvent` 解析事件 → `tool_use_summary` 作步骤、最后一个 `assistant` 事件的 text 作最终结果；结构化进度经 IPC 回传，主 App 把 **⚙️ 工具步骤实时显示为聊天气泡**（截图可见 ⚙️ Write/Read completed 气泡 + 最终回复），tool 结果含步骤摘要回灌 planner。
- 小观察（无害）：worker 是 Claude-Code 移植，自带“先 Read 再 Write”策略，偶发一次 `Write failed: file not read` 后改用 Bash/Read 完成。

### ✅ 进度气泡改为 ephemeral（不入库、不进上下文，2026-06-25 验证）
`ChatMessage` 加 `ephemeral` 标记。⚙️ 进度气泡走 `addEphemeralMessage`：只进 UI 的 `_messages`，**不调** `appendVisibleHistory/saveHistory`（不入库）、**不进** `syncEnvironmentRecentMessages`（不进模型上下文）；`saveHistory`/`syncEnvironmentRecentMessages` 也都 `filterNot { ephemeral }` 兜底。真消息到达或 `processing→false` 时 `clearEphemeralProgress()` 移除气泡并发 `resyncSignal` → 服务 `sendSnapshot()`（增量广播表达不了“删除”，必须全量快照；client `handleSnapshot` 用 `_messages.value = …` 整列替换）→ UI 丢弃气泡。最终“气泡区域 == 模型上下文区域”（仅 user+reply）。真机验证：运行中 ⚙️ 气泡可见；回复到达后消失；冷启动重开历史无残留。

### ✅ arm64 真机验证（2026-06-25, MD-PH-001 / Android 14 / kernel 4.19.191 aarch64，经 Windows→Linux 的 SSH adb 隧道）
引擎做成**多 ABI**：assets 改为 `proot/<abi>/*` + `rootfs-<abi>.tar.gz`（x86_64 + arm64 各一份），`WorkerRuntime` 按 `Build.SUPPORTED_ABIS[0]`（primary/native ABI）选择。aarch64 rootfs 用 **qemu-user-static + chroot** 在 dev 机上按同样配方烤（apk add python3/bash/py3-urllib3 + 注入 worker）；aarch64 proot 套件取自 Termux aarch64 deb。
真机离线自测全绿（无需联网）：proot+busybox `exit=0`（**W^X execve from filesDir 在真 arm64/4.19 内核成立**）、`python 3.12.13 + OpenSSL 3.3.7 + worker import OK`、fs/shell `hi-from-arm64`。x86_64 模拟器同一多-ABI 包亦全绿。
**两个本轮修的真 bug**：
1. **不能用设备 `tar` 解包**：Android toybox tar 拒绝 Alpine rootfs 的绝对 applet 符号链接（`/bin/busybox`→`/system/bin/busybox` 被判定逃出 dest）。改为 `WorkerRuntime.extractTar` **进程内 Kotlin 解包**（正则文件/目录/符号链接 via `Os.symlink` 原样创建 + GNU long-name），跨设备稳定。
2. **ABI 选择**：x86_64 模拟器的 `SUPPORTED_ABIS` 同时含 `arm64-v8a`（arm 翻译），早期“先匹配 arm64”会在 x86_64 上误选 arm64 proot → 链接失败。改用 primary ABI（`SUPPORTED_ABIS[0]` 含 "x86" 则 x86_64，否则 arm64）。
### ✅ arm64 真机在线全链验证（2026-06-26，设备联网后）
设备恢复网络（`dashscope.aliyuncs.com` 可解析、~38ms）后，在真 arm64 手机上把**完整产品链**跑通：
- **跨包在线委派**（WorkerDebugActivity）：worker 由 **qwen 联网驱动**，自主 Write+Bash 建文件并验证，`RESULT exit=0`。
- **完整聊天 UI 自主委派**：聊天里发任务 → qwen planner **自主调用 `delegate_to_worker`** → worker 在手机上跑 → ⚙️ 临时进度气泡出现后消失（ephemeral）→ Haru 回复“文件内容是 hello-from-arm。”；`run-as cat …/armchat.txt`=`hello-from-arm`。
- **SELinux 实证**：logcat `avc: granted { execute_no_trans } path=".../files/bin/proot" app=com.l2dchat.shell` —— targetSdk 28 让 W^X 从 app filesDir execve 在真机内核被放行（审计级证据）。
至此 arm64 与 x86_64 均端到端验证（含 planner 自主调用 + 在线 qwen + 工具 + ephemeral 进度）。
（真机输入小坑：发送前 App 要求填“我的昵称(必填)”；中文拼音 IME 会把 ASCII 输乱，切到 AOSP LatinIME 即可——均非代码问题。）

## 推荐开工顺序
**Phase 1（W^X/proot PoC）→ Phase 2（Node/opencode 入镜像）** 先把地基和 worker 跑通，再回头补
拆包/安装/IPC/工具接入。地基若不通（W^X 或体积），尽早切回退方案，避免在上层白做。
