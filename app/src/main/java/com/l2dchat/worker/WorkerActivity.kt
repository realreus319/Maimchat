package com.l2dchat.worker

import org.json.JSONObject

/**
 * Structured on-device-agent activity surfaced to the chat UI. It travels over the existing
 * worker-status string channel (JSON-encoded) so no new IPC message is needed. While the agent runs it
 * is [PHASE_RUNNING] (drives the live "working" bubble); when the run finishes it becomes [PHASE_DONE]
 * carrying the step / tool counts and a browser flag (drives the persistent call-summary bubble whose
 * browser icon reopens the agent's browser tab).
 */
data class WorkerActivity(
    val phase: String,
    val text: String,
    val steps: Int = 0,
    val tools: Int = 0,
    val browser: Boolean = false,
) {
    val isDone: Boolean get() = phase == PHASE_DONE

    fun toJson(): String =
        JSONObject()
            .put("phase", phase)
            .put("text", text)
            .put("steps", steps)
            .put("tools", tools)
            .put("browser", browser)
            .toString()

    companion object {
        const val PHASE_RUNNING = "running"
        const val PHASE_DONE = "done"

        fun running(text: String, browser: Boolean = false): WorkerActivity =
            WorkerActivity(PHASE_RUNNING, text, browser = browser)

        fun done(text: String, steps: Int, tools: Int, browser: Boolean): WorkerActivity =
            WorkerActivity(PHASE_DONE, text, steps = steps, tools = tools, browser = browser)

        /** Parse a worker-status payload. Back-compat: a non-JSON string is treated as running text. */
        fun parse(raw: String?): WorkerActivity? {
            if (raw.isNullOrBlank()) return null
            return try {
                val o = JSONObject(raw)
                if (!o.has("phase")) return running(raw)
                WorkerActivity(
                    phase = o.optString("phase", PHASE_RUNNING),
                    text = o.optString("text", ""),
                    steps = o.optInt("steps", 0),
                    tools = o.optInt("tools", 0),
                    browser = o.optBoolean("browser", false),
                )
            } catch (_: Throwable) {
                running(raw)
            }
        }
    }
}
