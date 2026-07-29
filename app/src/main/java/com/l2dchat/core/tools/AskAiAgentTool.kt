package com.l2dchat.core.tools

import android.content.Context
import com.google.gson.JsonObject
import com.l2dchat.core.llm.LlmToolDefinition
import com.l2dchat.worker.ShellEngineClient
import com.l2dchat.worker.WorkerActivity
import com.l2dchat.worker.WorkerCreds
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Planner tool, framed anthropomorphically: the persona (小千) "asks an AI agent" to do a concrete
 * task. Under the hood it delegates to the autonomous on-device worker sub-agent (Claude-Code python
 * port) running in a real Linux (proot/Alpine) environment in the headless engine package, and
 * returns its result. Creds + context resolve lazily so the tool can be registered before the app
 * context / settings are wired.
 */
class AskAiAgentTool(
        private val contextProvider: () -> Context?,
        // Dispatch a DETACHED background worker (task, goal, origin) → a short confirmation for the
        // planner. The worker runs to completion off-turn; its result comes back later as a SYS trigger.
        private val dispatch: (String, String, com.l2dchat.worker.WorkerOrigin) -> String,
) : Tool {

    override val definition: LlmToolDefinition =
            LlmToolDefinition(
                    name = NAME,
                    description =
                            "Delegate a task to an autonomous AI agent that drives a REAL Linux terminal WITH network " +
                                    "access. Its essence: it can accomplish almost ANYTHING a capable person with a shell " +
                                    "and the internet could — the examples below are just illustrations, not limits. E.g.: " +
                                    "search & research online, browse the web, run any shell command or code, install tools, " +
                                    "read/write/inspect files, look up local OR remote info, connect out to other machines " +
                                    "(SSH, curl, etc.), and carry out arbitrary multi-step technical work. Think of it as 小千 " +
                                    "handing the job to a skilled assistant and asking it to get it done. Use it whenever the " +
                                    "user wants something actually DONE or LOOKED UP that such an agent could handle, and TRUST " +
                                    "the real result it returns (never fabricate an answer). It runs DETACHED IN THE BACKGROUND: " +
                                    "this tool returns IMMEDIATELY with a dispatch confirmation (NOT the result); the finished " +
                                    "result arrives later as a system notification. Call it just ONCE per request — never call " +
                                    "it repeatedly in the same turn.",
                    parameters =
                            mapOf(
                                    "type" to "object",
                                    "additionalProperties" to false,
                                    "properties" to
                                            mapOf(
                                                    "task" to
                                                            mapOf(
                                                                    "type" to "string",
                                                                    "description" to
                                                                            "A clear, self-contained description of what you want the AI agent to do."
                                                            )
                                            ),
                                    "required" to listOf("task")
                            )
            )

    override suspend fun execute(
            context: ToolExecutionContext,
            arguments: JsonObject
    ): ToolExecutionResult {
        val task = arguments.get("task")?.takeIf { it.isJsonPrimitive }?.asString?.trim()
        if (task.isNullOrBlank()) {
            return ToolExecutionResult(llmContent = "ask_ai_agent error: missing 'task'.", isError = true)
        }
        contextProvider()
                ?: return ToolExecutionResult(llmContent = "ai agent unavailable: no app context.", isError = true)
        // The AI agent only runs for a DIRECT USER request. Environment/system triggers (idle/foreground
        // timers) fire the planner in the BACKGROUND; letting them invoke the worker re-does already-
        // completed tasks and spams the chat with duplicate deliverable files (one instruction → many
        // summary files) plus orphan activity bubbles. Skip the worker entirely on those triggers.
        if (context.trigger.triggerType != com.l2dchat.core.trigger.TriggerType.MSG) {
            return ToolExecutionResult(
                    llmContent =
                            "ai agent skipped: it only runs for a direct user request, not an ambient/background trigger.",
                    isError = false
            )
        }
        // DISPATCH a detached background worker and return immediately. The turn ends now; the worker
        // runs off-turn (up to 3 concurrent) and its result comes back later as a SYS trigger the
        // planner delivers. `goal` is a short handle shown in the per-turn background-status block.
        val goal = task.replace('\n', ' ').take(60)
        val origin =
                com.l2dchat.worker.WorkerOrigin(
                        contextId = context.trigger.contextId,
                        agentId = context.trigger.agentId,
                        messageId = context.trigger.messageId,
                )
        return try {
            val confirm = dispatch(withEnvironmentHint(task), goal, origin)
            ToolExecutionResult(llmContent = confirm, isError = false)
        } catch (t: Throwable) {
            ToolExecutionResult(
                    llmContent = "派发后台任务失败：${t.message ?: "unknown error"}",
                    isError = true
            )
        }
    }

    /**
     * Append deterministic environment guidance to every delegated task. The device sits behind the
     * GFW (Google / HuggingFace / *.github.io frequently time out or reset), and the worker otherwise
     * tends to exhaustively try many unreachable sites — which makes browser research crawl and can run
     * the task to its timeout. This nudges it toward reachable search engines and toward stopping once
     * it has enough to answer.
     */
    private fun withEnvironmentHint(task: String): String {
        val today = SimpleDateFormat("yyyy-MM-dd (EEEE)", Locale.SIMPLIFIED_CHINESE).format(Date())
        val dateHint =
                "[当前日期] 今天是 $today。凡涉及“最新/今年/现在/近期”等有时效性的问题，一律以这个日期为准，" +
                        "不要拿训练时的旧信息当“最新”。"
        return task + "\n\n" + dateHint + "\n\n" + ENV_HINT
    }

    companion object {
        const val NAME = "ask_ai_agent"

        private val ENV_HINT =
                """
                [执行能力 — 重要]
                - 你运行在一个【完整可用的 Alpine Linux 环境】（proot 容器，拥有 root 权限），不是受限沙箱。你可以、并且必须真正地执行命令来完成任务。
                - 已就绪的工具：`python3`（3.12）、`bash`/`sh`、常用 coreutils/busybox、`apk`（Alpine 包管理器，可联网安装任何软件）。
                - 缺什么就用 `apk add` 自己装，例如编译 C：先 `apk add gcc musl-dev make`（首次约需联网下载，装好后会持久保留），再 `gcc x.c -o x && ./x` 真实编译运行。
                - 涉及“编译/运行/执行/跑一下/验证输出”的任务：**必须真的把代码写进文件、真的编译、真的运行，并把程序的真实 stdout 原样返回**。严禁以“环境受限/无法执行/无编译器”为借口跳过执行或用心算、用公式、用“理论上的输出”糊弄——这些都是错误的，环境完全有能力执行。
                - 只有在你真的尝试执行并拿到了报错时，才如实报告该报错。

                [运行环境提示]
                - 本机位于中国大陆网络：Google、HuggingFace、*.github.io、Twitter/X 等站点经常无法访问（连接超时/重置），不要反复尝试它们。
                - 需要联网搜索时，优先使用可访问的引擎：必应 https://cn.bing.com 或百度 https://www.baidu.com ；也可直接访问已知可达的中文/官方站点。
                - 一旦从可达来源获得足够回答问题的信息，立刻停止检索并给出结论，不要逐个尝试更多网站或反复换源。
                - 单个网页若 5~10 秒内打不开就放弃它、换下一个可达来源，不要在同一个打不开的站点上反复重试。

                [交付物提示]
                - 判断这次的产物是不是一个【文件】：如果任务本身就是要产出一个文件——写代码/程序/脚本、做作业、生成文档/报告/表格/图片，或用户明确说“发给我/给我这个文件/导出/生成一个 xx 文件”——那就把成果写进一个文件（如 /root/xxx.c、/root/report.md），并在【完成后必须调用 `submit_file`（path 指向该文件）把它发给用户】。这是用户拿到文件的唯一方式：只在文字里报告“结果已保存在 /root/xxx”、或把整段文件内容贴进回复，都等于没交付、用户根本拿不到文件。资料不全也要把已有内容整理成文件提交并注明缺失部分。
                - 如果任务是“把某个已经存在的文件发给用户”并给了具体路径，直接对该路径调用 `submit_file` 即可，不必重新生成内容。
                - 如果用户只是普通提问或查信息（例如“查一下…”“…是什么”“告诉我…”“搜索…最新…”），直接用文字把答案返回即可，【绝不要】创建或 submit_file 任何文件——这是日常聊天，不是交付文件。
                - 一次请求最多提交一个文件；绝不要为同一个请求反复写多个版本、反复 submit_file。
                """.trimIndent()
    }
}
