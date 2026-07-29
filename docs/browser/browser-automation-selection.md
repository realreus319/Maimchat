# 浏览器自动化能力 — 选型文档

> 状态：选型中（待用户拍板关键岔路）。日期：2026-06。
> 背景：为设备端 AI agent **worker**（proot/Alpine + Python Claude-Code 端口，跑在 engine 包 `com.l2dchat.shell`）增加"浏览器自动化"工具。

## 1. 需求与张力

| # | 需求 | 倾向的技术方向 |
|---|---|---|
| 1 | 对模型类似 Playwright MCP 的体验（或直接 Playwright） | CDP / Playwright |
| 2 | 尽可能完善伪装原生浏览器，防止被识别为机器人 | 设备上**真实**浏览器引擎 |
| 3 | 用户能交互：模型每开一页，用户能点进去查看/操作真实页面 | 原生 Android **WebView**（有 UI 的进程） |

**核心约束(用户确认)**：浏览器工具属于 **worker** 的工具集，不是 planner 的 app 侧工具。
→ worker 在 proot(headless,无 UI)里,而要求3 要求浏览器是原生 WebView(在某个有 UI 的 Android 进程里)。
→ **浏览器不能放 proot 内**(不可见 + headless 反检测弱 + Alpine musl 装不了官方浏览器),必须是原生 WebView,由 worker **跨 proot 边界**驱动。

**要求1 vs 要求2 的张力**：Playwright/Playwright-MCP 体验最自然走 CDP；但 CDP/remote-debugging 会重新引入自动化协议痕迹（`Runtime.enable` 探测等）。反检测最干净是"不开 CDP、纯进程内 JS 注入 + 合成触摸"。

## 2. 调研要点（四支：Playwright可行性 / 原生引擎 / 反指纹 / 代码库桥接）

### 2.1 Playwright 在设备端
- **纯 proot/Alpine headless 路线否决**：官方浏览器二进制依赖 glibc，Alpine 用 musl，**装不上**；且 headless 反检测最弱、用户不可见。
- `playwright.android` 经 ADB+CDP 驱动设备真实 Chrome/WebView 可行，但实验性、需 ADB、仅 Chromium 系。
- **Playwright(python) `connect_over_cdp` 只连不启浏览器**：可连一个已存在的 WebView CDP 端点而**不需要自带浏览器二进制**——绕开 musl 死结（若要在 proot 内用 Playwright 体验，这是唯一现实形态；但仍需 Node 驱动，musl 兼容存疑）。
- Playwright MCP 用 accessibility snapshot(带 ref)驱动模型,可经 `PLAYWRIGHT_MCP_CDP_ENDPOINT` 指向自定义 CDP。

### 2.2 原生可自动化引擎对比
| 引擎 | 用户可见可嵌入 | 可自动化 | 反检测 | Playwright 衔接 | 结论 |
|---|---|---|---|---|---|
| **Android System WebView** | ✅ 就是个 View,嵌进 UI | ✅ 进程内 `evaluateJavascript`+合成输入+无障碍树;或开 `setWebContentsDebuggingEnabled` 走 CDP(进程内 `LocalSocket` 连 `webview_devtools_remote_<pid>` 抽象 socket,**无需 adb**) | 真 Chromium 引擎;`navigator.webdriver` 默认 false 且 **CDP attach 不置 true**;唯一明显破绽 UA 的 `wv`(可覆盖) | 可包成 Playwright 风味 facade | **首选** |
| GeckoView | ✅ | Marionette/WebExtension(会暴露 webdriver=true) | 指纹差异化(无 wv)但移动端更少数派更显眼;依赖重(数十 MB) | 弱 | 备胎 |
| 真 Chrome via CDP | ❌ 不能嵌进自家 UI | CDP(需开发者模式) | 最真 | 中 | 出局(不可嵌入) |
| Custom Tabs | ❌ 部分 | ❌ 禁 JS 注入/无 DOM/无 CDP | — | — | 出局(不可驱动) |

### 2.3 反机器人指纹（要求2 的能力边界）
- 2026 决定性检测在**网络层(TLS JA3/JA4 + HTTP/2 Akamai + TCP)**与**行为层 + ML**;UA/JS stealth 只够指纹层,**碰不到 TLS/IP/行为**。
- **WebView 与 Chrome 共用 Chromium 网络栈(Cronet/BoringSSL)** → 设备真实 WebView 的 TLS/HTTP2/TCP 指纹**天生是真 Chrome**。
- **设备真实引擎天然免疫(自洽,无需伪造)**:TLS/HTTP2/TCP、GPU/WebGL/Canvas、字体/Audio/屏幕/传感器、UA-CH 高熵、真实蜂窝/住宅 IP。
- **真实引擎仍须补**:① 自动化协议痕迹(用 CDP 则须 CDP-minimal;用 WebDriver/chromedriver 则 webdriver=true,应避免);② 人类化行为(触摸坐标相对主框架 + 自然时序 + 导航预热防 intent 检测);③ 避免数据中心 IP;④ 自定义 UA **须保留默认 UA 子串**否则破坏 UA-CH;⑤ 覆盖 WebView 的 `wv`/`Version/4.0` token。
- vs proot headless:容器要在 7~8 层同时撒谎且不矛盾(SwiftShader 软渲染、JA4T 暴露 Linux、云 IP),工程上几乎不可行。**真机真实引擎是反检测上的代差优势。**

### 2.4 代码库桥接现状（接入点）
- **planner 跑在主 app 进程**(`LocalChatRuntime`/`ToolRegistry`,Kotlin `Tool` 进程内执行);`AskAiAgentTool` 经 Messenger IPC(签名级权限)把子任务委派给 engine 进程,engine 用 proot 跑 Python Claude-Code worker,stream-json 回传进度/结果。
- **worker 自带工具(Bash/Write/Read/SSH)在 proot 内(Python)定义执行**;worker 与外界经 stdout stream-json(出) + 任务输入(入)。
- 现有跨界通道:Messenger IPC(app↔engine 控制面)、stream-json(proot→engine→app 数据面)、`extraNormalTools` 注入点(app 侧工具)。
- **缺口**:目前没有"worker(proot)运行中反向调用 native 驱动 WebView 并取回结果"的通道——这是浏览器工具要新建的桥。

## 3. 推荐架构（worker 驱动、用户可见、强反检测）

```
planner(app) --ask_ai_agent--> engine(ShellService) --proot--> worker(Python Claude-Code)
                                                                     |
                                              worker 的 browser 工具(薄 RPC, proot 内)
                                                                     | 本地 socket(共享命名空间)
                                              engine-native(Kotlin) BrowserController
                                                                     | 驱动
                                              engine 进程内 用户可见 WebView(Activity)
                                                                     ^ 用户可点进去查看/接管
```

1. **引擎选 Android System WebView**,放在 **engine 进程**的一个用户可见 Activity 里(主 app 可拉起给用户看;agent 与用户共用同一 WebView 实例,可接管)。真 Chromium → 网络/硬件指纹天然真实(要求2 地基)。
2. **engine-native(Kotlin)实现 Playwright-MCP 风味的 BrowserController**:navigate、snapshot(无障碍树/DOM,带 ref)、click(ref)、type(ref,text)、screenshot、waitFor。
   - **驱动方式(关键岔路,见 §4)**:推荐 **无 CDP**——读/快照用进程内 `evaluateJavascript` + 无障碍树,点击/输入用合成 `dispatchTouchEvent`(真实坐标、人类化时序)。零自动化协议足迹(不开 remote-debugging、webdriver 保持 false、无 Runtime.enable),反检测最干净。CDP 留作后续高级特性(网络拦截)按需开,并配 CDP-minimal 缓解。
3. **worker 的 `browser` 工具(proot Python)** = 薄 RPC 客户端:连 engine 暴露的本地 socket(proot 与 engine 共享网络命名空间,可达),下发高级动作、收回 snapshot/截图/文本。worker(Claude-Code)看到的就是 Playwright-MCP 式工具。
4. **反检测补强**:保留默认 UA 子串(护住 UA-CH)或谨慎覆盖 `wv`;人类化输入时序/坐标;依赖设备真实 IP,杜绝数据中心代理。

**为何不把 WebView 放主 app**:worker→主app-WebView 要多跨一层(proot→engine IPC→主app IPC→WebView),并要新建 engine↔app 反向工具通道;放 engine 进程跨界最少。代价:浏览器 UI 在 engine 包(需一个可见 Activity,主 app 拉起)。

**为何不在 proot 内跑浏览器**:不可见(违反要求3)、Alpine musl 装不了官方浏览器、headless 反检测弱(违反要求2)。

## 4. 决策定型（用户已拍板）

- **引擎**：Android System WebView（真 Chromium，反检测地基）。
- **驱动方式 = 走 CDP（混合为主）**：在 app 内 WebView 上 `setWebContentsDebuggingEnabled(true)`，主 app 进程内用**原始 CDP 客户端**(非 chromedriver,故 `navigator.webdriver` 仍 false)驱动导航/读取/快照/等待/跨域 iframe/网络。能力对齐 Playwright(Playwright 本就建在 CDP 上),复杂度低、不用自搓。
  - **理由**：反检测决定性红利(TLS/HTTP2/TCP + 硬件指纹)来自"用真实 WebView",与是否用 CDP 无关;CDP 仅多一点可缓解的协议痕迹(`Runtime.enable`,V8 2025 补丁 + CDP-minimal 可压)。自搓无 CDP(原 A1)复杂且复刻不了 Playwright(跨域 iframe/网络拦截/可靠等待做不全)。
  - **输入双路**：点击/输入**同时提供「合成触摸(`dispatchTouchEvent`,真实 OS 事件、最人类化)」与「CDP 输入(`Input.*`)」两条实现,默认先试合成触摸,达不到目标(如元素遮挡/坐标解析失败)再回退 CDP 输入**。
- **WebView 位置 = B2 主 app 进程**：内联在主 app(聊天界面可切入),UX 最连贯;代价是 worker→WebView 需经 proot→engine→主app 的反向工具调用通道(新建)。CDP 客户端在主 app 进程内连本进程 WebView 的 devtools 抽象 socket(同进程,无 SELinux 跨 app 问题)。
- **模型接口 = 足够封装即可(偏 C2)**：给 worker 一组高层、好用的浏览器工具(自动等待加载、ref/文本解析、重试等复杂度内置),不追求 Playwright-MCP 完整 ref 体系。
- **优先级**：**用户界面友好优先**(可观看 agent 浏览、可一键接管);模型侧调用够封装。

实现细节见 `browser-automation-design.md`。

## 5. (历史)曾考虑的岔路

### 岔路 A：驱动方式（要求1 Playwright 体验 ↔ 要求2 反检测 的权衡）
- **A1 无 CDP（进程内 JS 注入 + 合成触摸）**：反检测最干净(无自动化协议足迹);但要手搓 Playwright 风味 API,网络拦截等高级特性弱。**推荐**。
- **A2 CDP（开 remote-debugging + 进程内 CDP 客户端 / Playwright connect_over_cdp / Playwright MCP）**：Playwright 体验最原生、特性最全;但有 CDP 痕迹(可用 patchright/nodriver 式 CDP-minimal 缓解,webdriver 仍 false)。
- **A3 混合**：读/快照走 CDP,点击/输入走合成触摸(人类化、相对主框架坐标)。

### 岔路 B：用户可见 WebView 放哪个进程
- **B1 engine 进程**(跨界最少,浏览器 UI 在 engine 包,主 app 拉起其 Activity)。**推荐**。
- **B2 主 app 进程**(UX 内联最好,但 worker→WebView 多一层 IPC + 新反向通道)。

### 岔路 C：模型接口丰富度
- **C1 完整 Playwright-MCP 式**(accessibility snapshot + ref 定位 + 全套动作)。
- **C2 精简自定义工具集**(navigate/click/type/read/screenshot 几个)。

## 5. 备选/降级
- 特定站点专杀 Chromium WebView → GeckoView 作差异化指纹备用引擎(接受重依赖、弱 Playwright 衔接)。
- 对抗 Cloudflare Turnstile 等最硬目标 → 真实引擎 + 住宅/移动 IP + 行为预热;极致可参考 nodriver 式(直连 CDP、去 Playwright 协议握手)但牺牲 Playwright 体验。

## 6. 主要来源（节选）
- Playwright Browsers/Android、GLIBC#9194、playwright-termux、Playwright MCP CDP endpoint
- WebView 远程调试(抽象 socket)、UA Reduction 2024-12、navigator.webdriver(MDN/ZenRows)、GeckoView automation
- 反指纹：FoxIO JA4+、Akamai HTTP/2 白皮书、Cloudflare/DataDome/HUMAN、Cronet(WebView=Chrome 网络栈)、2026 nodriver 基准
