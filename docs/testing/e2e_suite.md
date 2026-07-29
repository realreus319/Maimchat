# Maimchat E2E 测试套件

自动化端到端测试：通过 adb 注入消息、读取记录、模拟系统冻结/杀进程，覆盖普通回复、
多消息打断（fork/decision）、worker 任务、以及后台/进程存活等极端场景。

## 1. 测试用 API

### 设备内（debug-only，`app/src/debug/`）

只在 **debug** 构建中存在，release 不包含。`TestControlReceiver` 注册了两个广播：

| 动作 | 命令 | 说明 |
|------|------|------|
| **输入** | `am broadcast -p com.l2dchat -a com.l2dchat.test.SEND --es text "<消息>"` | 经 `WallpaperChatCoordinator.sendMessage` 注入用户消息，绕过不稳定的 IME/点击 |
| **清空** | `am broadcast -p com.l2dchat -a com.l2dchat.test.CLEAR` | 等价于界面「清空聊天记录」，9 张表全清，给每个用例干净初态 |

> 应用必须处于运行态（非 force-stop）才能收到广播；用例里先 `launch` 拉起 MainActivity。

### 主机侧（纯 adb，无需改 App）

| 能力 | 实现 | harness 函数 |
|------|------|------|
| **获取记录（DUMP）** | `run-as` 拉 `maimchat_runtime.db` + `files/logs/l2dchat.log` | `msgs` / `rounds` / `timing` / `asst_count` / `has_file` |
| **后台** | `input keyevent 3`（回桌面） | `home` |
| **杀主进程** | `am kill`（仅杀缓存态主进程，engine 独立进程不受影响） | `kill_main` |
| **杀全部** | `am force-stop`（engine 是独立进程，需单独处理） | `force_stop` |
| **模拟冻结** | `am make-uid-idle` + `cmd deviceidle force-idle` | `freeze` / `unfreeze` |
| **worker 存活探测** | `ps -A | grep proot|python3` | `worker_n` |

## 2. 运行

```bash
cd scripts/e2e
DEV=emu ./tests.sh            # 默认快测：t1 t2 t3
DEV=emu ./tests.sh t1 t6      # 指定用例
DEV=plj ./tests.sh t4 t7 t8   # 在真机 PLJ110 上跑 worker 用例
```

`harness.sh` 按 `DEV`（emu=emulator-5554，plj=IPv6 隧道真机）选择 adb 目标。

## 3. 用例清单

| 用例 | 场景 | 关键断言 |
|------|------|---------|
| **T1** | 基线单轮回复 | 有助手回复 + `replySent=true` + 无错误 |
| **T2** | 两条消息打断（A 长回复被 B 取消） | 发生 DECISION 轮 + B 得到正确答案 + A 被 kill |
| **T3** | 三条消息递归打断 | 最终只回最后一条 + 无崩溃 |
| **T4** | C 语言作业（worker 编译运行） | 回复含斐波那契/结果 + 无 max-tokens 截断 |
| **T5** | 网页抓取 → submit_file（worker+browser） | 交付文件或抓取文本 |
| **T6** | 回复一半回桌面（前台轮后台完成） | 后台仍 `replySent=true` |
| **T7** | worker 工作中回桌面 | worker 后台继续 + 结果交付（5050） |
| **T8** | worker 工作中杀主进程 | engine 存活 + 重启后结果交付（3628800） |

## 4. 设备差异

引擎 **同时打包 x86_64 和 arm64**（`engine/src/main/assets/` 下两套 `proot/<abi>/` +
`rootfs-<abi>.tar.gz`），`WorkerRuntime.kt:31-34` 运行时按 `Build.SUPPORTED_ABIS` 主 ABI
选取，`engine/build.gradle.kts` 无任何 `abiFilters`。所以 **x86 模拟器一样能跑 worker**：

- PocActivity 自检（`am start -n com.l2dchat.shell/.PocActivity`）在模拟器上：
  `selected runtime abi=x86_64` → proot `exit=0 :: hi-from-x86_64` → `python 3.12.13`。
- 真实 worker 任务在模拟器上验证通过：`print(2**10)` → 回复「代码运行结果是 1024」。

> 早期 T4/T7 出现「受环境限制没能直接运行脚本」是 **worker LLM(kimi) 偷懒/找借口**，
> 不是平台不支持——换清晰指令即可正常执行。worker 类用例在 `DEV=emu` 也可跑；
> 真机 PLJ110 仅用于贴近线上环境的最终回归。
