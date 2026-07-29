package com.l2dchat.worker

import android.content.Context
import android.util.Log
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicLong
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit

/** Where a dispatched worker's completion reply should be routed (the delegating turn's origin). */
data class WorkerOrigin(val contextId: String, val agentId: String, val messageId: String)

/**
 * Runs `ask_ai_agent` tasks as DETACHED background workers instead of blocking the planner turn.
 * The planner dispatches (fire-and-forget), the turn ends; when a worker finishes, [onComplete] fires
 * a SYS trigger back into the planner so it delivers the result. Up to [MAX_CONCURRENT] run at once;
 * the rest queue. A live snapshot of goals+state feeds the planner's per-turn background-status block.
 */
class BackgroundWorkerManager(
    private val context: Context,
    private val scope: CoroutineScope,
    private val credsProvider: () -> WorkerCreds?,
    private val fileSink: (FileSubmission) -> Unit,
    /** (taskId, goal, kind, text) — live progress for the UI activity bubble + status snapshot. */
    private val onProgress: (String, String, String, String) -> Unit,
    /** (origin, goal, resultText, isError) — worker finished; caller fires the completion SYS trigger. */
    private val onComplete: (WorkerOrigin, String, String, Boolean) -> Unit,
    /** Compress the planner's recent context into a short background block for the worker (or null). */
    private val compactContext: suspend (WorkerOrigin) -> String? = { null },
) {
    private val TAG = "BgWorkerManager"

    enum class State { QUEUED, RUNNING, DONE, FAILED }

    data class WorkerInfo(
        val taskId: String,
        val goal: String,
        @Volatile var state: State,
        @Volatile var steps: Int,
        @Volatile var lastActivity: String,
        val startedAtMs: Long,
        val origin: WorkerOrigin,
        // FIFO of per-turn goals: the initial goal, then one per injected follow-up. Each turn's
        // completion pops its own goal so the planner sees distinct tasks (not duplicates of one goal).
        val goals: java.util.concurrent.ConcurrentLinkedQueue<String> =
            java.util.concurrent.ConcurrentLinkedQueue(),
    )

    companion object {
        const val MAX_CONCURRENT = 3
        // Backstop ceiling on a single session's lifetime (> the worker's own hard timeout) so a lost
        // session-ended signal can never leak a concurrency permit permanently.
        const val SESSION_HARD_CEILING_MS = 15 * 60 * 1000L
    }

    private val active = ConcurrentHashMap<String, WorkerInfo>()
    // Live session handles by taskId — targets for mid-run injection (#5).
    private val sessionHandles = ConcurrentHashMap<String, ShellEngineClient.SessionHandle>()
    // Origin messageIds with a live dispatch — one worker per user message (the planner sometimes
    // calls ask_ai_agent several times in one turn; without this a single request spawns a fleet).
    private val dispatchedMessages = ConcurrentHashMap.newKeySet<String>()
    private val gate = Semaphore(MAX_CONCURRENT)
    private val clock = AtomicLong(0)

    /** Register + start (or queue) a background worker. Returns a short confirmation for the planner. */
    fun dispatch(task: String, goal: String, origin: WorkerOrigin): String {
        if (!dispatchedMessages.add(origin.messageId)) {
            return "这条请求我已经在后台安排上了，不用重复派发；完成后我会主动把结果发给你。"
        }
        // Mid-run steering (#5): if a worker is ALREADY running for this conversation, inject this as a
        // follow-up message into its live session (the running worker adapts) instead of spawning a new
        // one. v1 heuristic — a planner "inject vs new" decision can later refine WHEN to inject.
        val liveInfo =
            active.values.firstOrNull {
                it.state == State.RUNNING &&
                    it.origin.contextId == origin.contextId &&
                    it.origin.agentId == origin.agentId
            }
        val liveHandle = liveInfo?.let { sessionHandles[it.taskId] }
        if (liveInfo != null && liveHandle != null && !liveHandle.isClosed) {
            liveInfo.goals.add(goal) // this follow-up turn delivers under its OWN goal
            liveHandle.inject(task)
            Log.i(TAG, "injected follow-up into live session ${liveHandle.taskId}")
            return "我正在后台跑的那个任务收到了你这条新指令，会一起处理，稍等。"
        }
        val creds = credsProvider() ?: run {
            dispatchedMessages.remove(origin.messageId)
            return "无法派发后台任务：未配置 worker 的 LLM。"
        }
        val taskId = UUID.randomUUID().toString()
        val info = WorkerInfo(taskId, goal, State.QUEUED, 0, "排队中", System.currentTimeMillis(), origin)
        info.goals.add(goal) // initial turn's goal
        active[taskId] = info
        val running = active.values.count { it.state == State.RUNNING }
        scope.launch { runOne(info, task, creds) }
        Log.i(TAG, "dispatched $taskId goal='${goal.take(40)}' (active=${active.size})")
        return if (running >= MAX_CONCURRENT)
            "已把任务排进后台队列（目标：$goal）。前面还有任务在跑，轮到它会自动开始，完成后我再来告诉你。"
        else
            "已在后台开始处理（目标：$goal）。它会独立跑，完成后我会主动把结果发给你。"
    }

    private suspend fun runOne(info: WorkerInfo, task: String, creds: WorkerCreds) {
        gate.withPermit {
            info.state = State.RUNNING
            info.lastActivity = "整理背景…"
            onProgress(info.taskId, info.goal, "agent_start", "后台处理：${info.goal.take(40)}…")
            // Compress the planner's recent context into a short background block so this otherwise
            // fresh worker knows what the user/it did before (e.g. a file path from an earlier task).
            val background =
                runCatching { compactContext(info.origin) }.getOrNull()?.takeIf { it.isNotBlank() }
            val fullTask =
                if (background != null)
                    "【背景信息（来自你与用户近期的对话，供参考，不一定与本次任务都相关）】\n$background\n\n【本次任务】\n$task"
                else task
            Log.i(TAG, "task ${info.taskId} background=${background?.length ?: 0} chars")
            info.lastActivity = "开始处理"
            // Run as a PERSISTENT session (#5): stays alive so mid-run injects run as follow-up turns.
            // Each turn's result is delivered via onComplete; the session closes after a grace-idle
            // window (engine side), which completes `done` and releases the concurrency permit.
            val done = kotlinx.coroutines.CompletableDeferred<Unit>()
            val handle =
                ShellEngineClient(context, onFileSubmit = fileSink).runSession(
                    fullTask, creds,
                    originContext = info.origin.contextId,
                    originAgent = info.origin.agentId,
                    originMessage = info.origin.messageId,
                    onProgress = { kind, text ->
                        if (kind == "tool") info.steps += 1
                        info.lastActivity = text.take(60)
                        onProgress(info.taskId, info.goal, kind, text)
                    },
                    onTurnResult = { outcome ->
                        // Each completed turn pops its own goal (initial, then injected follow-ups in
                        // order) so successive completions read as distinct tasks — not duplicates.
                        val turnGoal = info.goals.poll() ?: info.goal
                        onProgress(info.taskId, turnGoal, "agent_done",
                            WorkerActivity.done("后台任务完成", outcome.steps.size, info.steps, false).toJson())
                        val body = outcome.text.trim().ifBlank { "(无输出)" }
                        onComplete(info.origin, turnGoal, body, outcome.exitCode != 0)
                    },
                    onClosed = { done.complete(Unit) },
                )
            if (handle == null) {
                info.state = State.FAILED
                active.remove(info.taskId); dispatchedMessages.remove(info.origin.messageId)
                onProgress(info.taskId, info.goal, "agent_done",
                    WorkerActivity.done("后台任务失败", info.steps, info.steps, false).toJson())
                onComplete(info.origin, info.goal, "无法启动后台会话", true)
                return@withPermit
            }
            sessionHandles[info.taskId] = handle
            try {
                // Wait for the session to fully end (engine sends MSG_SESSION_ENDED -> onClosed). Backstop:
                // even if that signal is ever lost, never hold the concurrency permit forever — close the
                // session and move on after a hard ceiling (> the worker's own hard timeout).
                val ended =
                    kotlinx.coroutines.withTimeoutOrNull(SESSION_HARD_CEILING_MS) { done.await() }
                if (ended == null) {
                    Log.w(TAG, "session ${info.taskId} exceeded hard ceiling — force-closing to free permit")
                    runCatching { handle.close() }
                }
            } finally {
                info.state = State.DONE
                sessionHandles.remove(info.taskId)
                active.remove(info.taskId)
                dispatchedMessages.remove(info.origin.messageId)
            }
        }
    }

    /** Live snapshot for the planner's background-status context block. */
    fun snapshot(): List<WorkerInfo> = active.values.sortedBy { it.startedAtMs }

    fun hasActive(): Boolean = active.isNotEmpty()

    /**
     * Human-readable status of the active/queued workers for THIS conversation (contextId+agentId),
     * for the planner's per-turn context block. Null when none are active for it.
     */
    fun statusBlock(contextId: String, agentId: String): String? {
        val mine = snapshot().filter { it.origin.contextId == contextId && it.origin.agentId == agentId }
        if (mine.isEmpty()) return null
        return mine.joinToString("\n") { w ->
            val st = when (w.state) {
                State.QUEUED -> "排队中"
                State.RUNNING -> "进行中（已 ${w.steps} 步）"
                else -> w.state.name
            }
            "- 目标：${w.goal}｜状态：$st｜最近：${w.lastActivity}"
        }
    }
}
