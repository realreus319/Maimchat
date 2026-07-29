package com.l2dchat.worker

import android.app.Activity
import android.os.Bundle
import android.util.Log
import android.widget.ScrollView
import android.widget.TextView
import com.l2dchat.BuildConfig
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch

/**
 * DEBUG: verifies the full cross-package delegation path from the MAIN app — bind the engine
 * package's ShellService (signature permission), run a task, stream progress, show the result.
 * Start with: adb shell am start -n com.l2dchat/com.l2dchat.worker.WorkerDebugActivity
 */
class WorkerDebugActivity : Activity() {

    private val TAG = "WorkerDebug"
    private lateinit var out: TextView
    private val scope = CoroutineScope(Dispatchers.Main)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        out = TextView(this).apply { textSize = 11f; setPadding(16, 16, 16, 16) }
        setContentView(ScrollView(this).apply { addView(out) })
        log("== worker auto-install + delegation test ==")

        val installer = ShellEngineInstaller(applicationContext)
        if (installer.isEngineInstalled()) {
            log("engine package present")
            delegate()
        } else {
            log("engine not installed — installing bundled APK (confirm the system dialog)…")
            installer.install(this) { ok, msg ->
                runOnUiThread {
                    if (ok) { log("engine installed ✓"); delegate() }
                    else log("install failed: $msg")
                }
            }
        }
    }

    private fun delegate() {
        val client = ShellEngineClient(applicationContext)
        val creds = WorkerCreds(
            apiKey = BuildConfig.LLM_DEFAULT_API_KEY,
            baseUrl = BuildConfig.LLM_DEFAULT_BASE_URL,
            model = BuildConfig.LLM_DEFAULT_PLANNER_MODEL
        )
        // Task can be passed via intent extra for ad-hoc testing:
        //   adb shell am start -n com.l2dchat/com.l2dchat.worker.WorkerDebugActivity --es task "..."
        val task = intent.getStringExtra("task")
                ?: "在当前目录创建文件 xpkg_poc.txt，写入一行 hi-cross-package，然后运行 cat xpkg_poc.txt"
        log("task: ${task.take(120)}")
        scope.launch {
            try {
                log("delegating to engine package…")
                val r = client.runTask(task, creds, timeoutMs = 240_000) { kind, text ->
                    log("[$kind] ${text.take(160)}")
                }
                log("\nsteps: ${r.steps.size}")
                log("== RESULT exit=${r.exitCode} ==\n${r.text.trim()}")
            } catch (t: Throwable) {
                Log.e(TAG, "delegation failed", t)
                log("ERROR: ${t.message}")
            }
        }
    }

    private fun log(line: String) {
        Log.i(TAG, line)
        runOnUiThread { out.append(line + "\n") }
    }
}
