# 浏览器自动化 — 实现设计

> 选型见 `browser-automation-selection.md`。本文是落地设计与分阶段计划。
> 定稿决策：app 内嵌真实 WebView（主 app 进程）+ 走 CDP（混合为主）+ 输入双路（合成触摸优先、CDP 输入回退）+ worker 经反向桥驱动 + 模型侧高层封装 + UI 友好优先。

## 1. 架构总览

```
 planner(主app) ──ask_ai_agent──▶ engine(ShellService) ──proot──▶ worker(Claude-Code)
                                                                       │ 作为 MCP 客户端
                                                                       ▼ MCP over local HTTP(127.0.0.1, 共享netns)
                          engine 进程: Playwright-MCP-兼容 MCP server  ◀┘
                          (1:1 复刻 Playwright MCP 工具面 + snapshot/ref 语义)
                                        │ 反向桥
                                        │ Messenger: MSG_BROWSER_ACTION/RESULT
                                        ▼
  主app: BrowserController(Kotlin)
        ├─ CDP 客户端  ── LocalSocket ─▶ 本进程 WebView 的 webview_devtools_remote_<pid>(开 setWebContentsDebuggingEnabled)
        │     用于: 导航/读取/快照(a11y/DOM 全树)/等待/跨域 iframe/网络拦截/CDP 输入(回退)
        └─ 进程内 WebView API: loadUrl、dispatchTouchEvent(合成触摸·默认)、PixelCopy 截图、scroll
        ▼
  主app: 用户可见 BrowserSurface(Compose/Activity，内联聊天) ── 用户随时切入查看/接管，操作同一页面
```

要点：
- **模型接口 = Playwright MCP，1:1 复刻**：在 engine 里自实现一个 MCP server,工具签名与 snapshot/ref 语义照搬 Playwright MCP 规范(不跑真正的 `@playwright/mcp` Node 包——on-device Node/musl 与跨 app CDP 不现实)。对模型等价于标准 Playwright MCP。worker(Claude-Code 端口)作为 MCP 客户端连它(本地 HTTP)。
- **engine MCP server 不直接碰 WebView**：它把 MCP 工具调用经反向桥下发到主 app 的 `BrowserController` 执行,自己只负责 MCP 协议 + 工具面 + 把 BrowserController 的快照转成 Playwright-MCP 的 ref 格式。
- **CDP 客户端在主 app 进程内,连本进程 WebView 的 devtools socket**(同 app/同进程,无 SELinux 跨 app 限制;不走 chromedriver → `navigator.webdriver` 保持 false)。
- **混合驱动**：能力面(iframe/网络/快照/等待)走 CDP;输入默认走进程内合成触摸,失败回退 CDP 输入;截图走 PixelCopy。
- **依赖/前提(已验证)**：worker 的 Claude-Code 端口**已内置 MCP 客户端**(`services/mcp/`、`--mcp-config`/`--strict-mcp-config`、`claude mcp` 子命令)。集成方式:`WorkerRuntime` 拉起 worker 时,除现有 task/creds 外,额外传 `--mcp-config '{"mcpServers":{"browser":{"type":"http","url":"http://127.0.0.1:<port>/mcp"}}}'` 指向 engine 的 MCP server。无需给 worker 补 MCP 能力。snapshot 必须产出 Playwright MCP 同款 ref(每个可交互节点带稳定 ref,动作按 ref 定位)。

## 2. 跨边界反向桥（新建的关键管道）

现状：主app→engine(`ShellEngineClient`→`ShellService`,Messenger)下发任务;proot→engine→主app 回传 stream-json 进度。**缺**:worker 运行中**同步**调主app WebView 并取回结果。

设计（worker 经 MCP 连 engine 内 MCP server + 复用现有 Messenger 通道作反向桥）：
1. **proot worker → engine MCP server**：engine 进程内起一个 **Playwright-MCP-兼容 MCP server,本地 HTTP 暴露**(127.0.0.1:随机端口,经 env/MCP 配置传给 worker;proot 与 engine 共享网络命名空间,127.0.0.1 可达)。worker(Claude-Code)作为 MCP 客户端调用工具。
2. **engine MCP server → 主app**：MCP server 收到工具调用后,经现有 Messenger 通道向已绑定的客户端(主app `ShellEngineClient`)发 `MSG_BROWSER_ACTION`(action+args+token)。
3. **主app 执行**：`ShellEngineClient` 把请求交给 `BrowserController` 执行,完成后回 `MSG_BROWSER_RESULT`(token + payload,含 snapshot/screenshot)。
4. **回程**：MCP server 把结果转成 MCP 工具响应回 worker。
- 超时/取消复用 ask_ai_agent 的整体超时;单次 browser 动作设较短子超时(如 30s,自动等待加载在内)。
- 协议常量加在 `ShellProtocol`/`ShellEngineProtocol`:`MSG_BROWSER_ACTION=5`、`MSG_BROWSER_RESULT=105`。

## 3. 模型侧工具面 = Playwright MCP（1:1 复刻）

engine 内的 MCP server 暴露与官方 Playwright MCP **同名同签名**的工具集,模型体验与标准 Playwright MCP 完全一致。以官方工具清单为准对齐(随其版本跟进),核心包括：

- 导航/页面：`browser_navigate(url)`、`browser_navigate_back`、`browser_close`、`browser_resize(width,height)`
- **快照(核心)**：`browser_snapshot` → 返回页面 accessibility snapshot,每个可交互节点带稳定 `ref`(动作据 ref 定位);`browser_take_screenshot`
- 交互：`browser_click(element,ref[,doubleClick,button])`、`browser_type(element,ref,text[,submit,slowly])`、`browser_fill_form(fields[])`、`browser_select_option(element,ref,values)`、`browser_hover(element,ref)`、`browser_drag(startRef,endRef)`、`browser_press_key(key)`、`browser_file_upload(paths)`、`browser_handle_dialog(accept[,promptText])`
- 标签页：`browser_tabs(action: list|new|select|close[,index])`
- 等待/求值：`browser_wait_for(text|textGone|time)`、`browser_evaluate(function[,ref])`
- 观测：`browser_console_messages`、`browser_network_requests`

实现策略：
- **签名与语义 1:1 对齐 Playwright MCP**(尤其 `browser_snapshot` 的 ref 体系、`element`+`ref` 双参约定)。这样任何"懂 Playwright MCP"的模型/Claude-Code 提示无需改动即可用。
- 工具调用 → engine MCP server → 反向桥 → 主 app `BrowserController` 执行 → 结果转回 MCP 响应。`ref` 的分配与解析在 `BrowserController`(基于 CDP 的 a11y/DOM 全树,含跨域 iframe),与 Playwright MCP 输出格式对齐。
- 复杂度(自动等待加载、ref 解析、合成触摸→CDP 回退、重试)封装在 BrowserController/MCP server,模型只按 Playwright MCP 心智调用。
- 工具清单随官方 Playwright MCP 版本对齐,文档记录所对齐的版本号。

## 4. 主 app 侧组件

- **`BrowserController`(Kotlin)**：管理 WebView 实例(可多页/多 WebView)、CDP 客户端、动作执行、快照生成、输入双路、截图。对外暴露 `suspend fun execute(action): BrowserActionResult`。
- **CDP 客户端**：连 `localabstract:webview_devtools_remote_<pid>`,实现需要的 CDP 域(`Page`/`DOM`/`Runtime`/`Accessibility`/`Target`(iframe)/`Network`/`Fetch`/`Input`)。Kotlin WebSocket + JSON;按需逐步实现域,不一次铺满。
- **输入双路**：`SyntheticTouchInput`(`dispatchTouchEvent`,默认)+ `CdpInput`(`Input.dispatchTouchEvent/dispatchKeyEvent`,回退);统一 `InputDriver` 接口,先试合成、判定失败(元素不可命中/无变化)再回退。
- **`BrowserSurface`(Compose/Activity)**：UI,见 §5。
- **`BrowserBridgeHandler`**：接 engine 的 `MSG_BROWSER_ACTION`,调 `BrowserController`,回 `MSG_BROWSER_RESULT`。

## 5. UI 设计（友好优先）

- **聊天内提示**：worker 浏览时,聊天区出现 `🌐 小千正在浏览 example.com · 点击查看`(类比现有 worker 工作气泡),点击展开浏览器面板。
- **浏览器面板**(底部 sheet 或全屏可切换):
  - 顶部:URL/标题、加载进度、返回/关闭、**「接管」开关**。
  - 中部:实时 WebView(用户能看 agent 实时操作)。agent 点击时**高亮目标元素**,让用户看清在做什么。
  - **接管模式**:开启后用户手指操作直达页面(与 agent 共用同一实例);关闭则回到 agent 驱动。
- **多页**:agent 开多页时顶部 tab/列表,用户可点进任一页查看。
- 与现有 `workerStatus` 通道一致地把"浏览中/当前 URL"状态推给 UI。

## 6. 反检测补强清单（要求2 落地）

- 真实 WebView 引擎 → 网络层(TLS/HTTP2/TCP)+ 硬件指纹天然真实(地基,免做)。
- 原始 CDP attach(非 chromedriver)→ `navigator.webdriver` 保持 false。
- CDP 痕迹缓解:`Runtime.enable` 用完即 `disable`/隔离世界执行(参考 patchright/nodriver 思路),按需才 enable 对应域。
- **UA/UA-CH**:优先保留默认 UA(护住 `Sec-CH-UA` 高熵一致性);如必须去 `wv`,覆盖 UA 时务必保留默认 UA 子串,避免 UA-CH 缺失矛盾。
- **人类化输入**:合成触摸用真实坐标(相对主框架,规避 Turnstile 的 iframe 坐标检测)+ 自然时序/轨迹 + 导航预热,防 intent-based 检测。
- **IP**:依赖设备真实蜂窝/Wi-Fi IP,杜绝数据中心代理。

## 7. 分阶段实现计划

- **P1 主app BrowserController + CDP 客户端 + WebView(独立可测)**：能 `open/snapshot/click(合成触摸)/type/screenshot`;一个 debug 界面直接驱动验证。先打通最小 CDP 域(Page/DOM/Runtime/Accessibility/Input)。
- **P2 输入双路 + 跨域 iframe/等待**：补 Target 域(iframe 快照)、可靠加载等待、CDP 输入回退。
- **P3 engine MCP server + 反向桥**：engine 内 Playwright-MCP-兼容 MCP server(本地 HTTP)+ Messenger `MSG_BROWSER_*` 反向桥 + worker(Claude-Code)配置该 MCP;`browser_snapshot` 的 ref 格式与 Playwright MCP 对齐;端到端"worker 经 MCP 调浏览器"打通。
- **P4 UI 友好**：BrowserSurface、聊天提示、接管开关、元素高亮、多页。
- **P5 反检测补强 + 真机验证**：UA/UA-CH、人类化时序、CDP 痕迹缓解;真机端到端(含一个有反 bot 的站点)冒烟。

## 7.1 P1 真机验证结果（2026-06，arm 真机）

`BrowserController` + `CdpConnection` + `BrowserDebugActivity` 已实现并真机验证通过：
- ✅ WebView 内嵌渲染、navigate、screenshot(PixelCopy)。
- ✅ **进程内 CDP 客户端**：app 进程内经"本地 TCP↔抽象 socket 中继"连本进程 WebView 的 `webview_devtools_remote_<pid>`,`Runtime.evaluate` 成功返回 `document.title`。(踩坑:relay 的 `ServerSocket` 必须显式绑 `127.0.0.1`,`getLoopbackAddress()` 在 IPv6 设备返回 `::1` 导致 OkHttp 连 127.0.0.1 被拒。)
- ✅ snapshot(带 ref+rect)。
- ✅ **CDP 输入点击**:`Input.dispatchMouseEvent` 全序列 **move→press→release**(CSS px 坐标,无需 scale),成功点中链接并导航(example.com→iana.org)。CDP 输入产生 `isTrusted=true` 的真实点击。
- ❌ **合成触摸 `dispatchTouchEvent`(含 `SOURCE_TOUCHSCREEN`)派发成功但点不动 WebView 链接**——坐标/缩放已校正(`scale=viewWidth/window.innerWidth`)仍无效。

**对设计的影响(重要)**:原"输入合成触摸优先、CDP 回退"的前提不成立——合成触摸在本 WebView 上无法激活链接。结论:**点击/输入改用 CDP 输入为主**。注意这不损失反检测:CDP 输入在页面侧同样是 `isTrusted=true`(Playwright 即依赖此),其唯一代价是"CDP 协议存在"——而该代价在选择走 CDP 时已接受;人类化(坐标相对主框架、自然时序)改为在 CDP 输入参数上施加,而非依赖 OS 触摸。`clickSynthetic` 保留备查。

## 7.2 P3 真机验证结果（2026-06）

engine 内 Playwright-MCP 兼容 HTTP server + 反向桥 + BrowserController 派发 + 可见 BrowserHostActivity 已实现,app+engine 双 APK 部署真机,经 `BrowserMcpDebugActivity`(不经 worker/LLM,独立验证)**端到端验证通过**:
- ✅ engine MCP server 启动监听 `127.0.0.1:<port>`,`ShellService` 取得 port。
- ✅ MCP `initialize`(protocolVersion 2025-06-18 / serverInfo l2d-browser)、`tools/list`(6 个 Playwright-MCP 兼容工具,schema 1:1)。
- ✅ `tools/call browser_navigate {url}` → **反向桥(Messenger token 往返)→ 主 app BrowserController.navigate → snapshot → 结果原路返回** `{content:[{type:text,text:"…[ref=e1] a \"Learn more\"…"}]}`。`browser_snapshot` 同样通过。
- ✅ BrowserHostActivity 拉起、controller 发布、真实 example.com 页面可见渲染。

**未验证(诚实记录)**:
- 真实 **worker(Claude-Code)经 `--mcp-config` 连接并调用**这些工具——需完整 聊天→ask_ai_agent→worker 链路(依赖 `:chat` 运行时,当晚受 AMS bad-process 节流 + 不可重启约束;+ LLM)。MCP server/桥/浏览器已证,worker 接入(`--mcp-config` 注入)已实现但未与真实 worker 跑通。
- **后台启动 Activity 限制(g3,最大风险)**:bridge 在主 app 非前台时 `startActivity(BrowserHostActivity)` 可能被 Android 10+ 拦截 → `awaitReady()` 挂到 60s 超时。正常流程(用户聊天→app 前台)应 OK;worker 在后台触发浏览时需在 P4 处理(如前台服务/overlay/复用聊天前台)。
- `browser_click/type/back` 经 worker 未单独跑(click 的 CDP 输入已在 P1 直接验证)。
- `browser_take_screenshot` P3 仅回字节数,图片内容块留 P4。

## 8. 风险与待解
- CDP 域逐步实现的工作量(尤其 Target/iframe、Network 拦截);先做 P1 最小集验证可行性。
- 反向桥的同步语义与超时/取消、并发(同一时刻一个 browser 动作)。
- 合成触摸的元素命中(坐标解析、滚动到可视、shadow DOM)——故保留 CDP 输入回退。
- engine↔app Messenger 反向消息要求任务期间通道保持绑定(ask_ai_agent 期间本就绑定)。
