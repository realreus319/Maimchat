package com.l2dchat.shell

import android.app.Activity
import android.os.Bundle
import android.util.Log
import android.widget.ScrollView
import android.widget.TextView
import kotlin.concurrent.thread

/**
 * DEBUG offline self-test: extracts the ABI-matched proot + Alpine rootfs and runs python + the
 * worker import under proot — NO network/LLM. Validates the load-bearing platform bits on whatever
 * device it runs (used to confirm the arm64 build on a real phone).
 */
class PocActivity : Activity() {

    private val TAG = "L2DShellPoC"
    private lateinit var out: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        out = TextView(this).apply { textSize = 11f; setPadding(16, 16, 16, 16) }
        setContentView(ScrollView(this).apply { addView(out) })
        thread { runOffline() }
    }

    private fun runOffline() {
        try {
            log("== L2D Shell offline self-test ==")
            log("abi=${android.os.Build.SUPPORTED_ABIS.firstOrNull()} sdk=${android.os.Build.VERSION.SDK_INT} model=${android.os.Build.MODEL}")
            val rt = WorkerRuntime(this)
            rt.ensureInstalled { log(it) }
            log("selected runtime abi=${rt.deviceAbi}")

            log("\n--- proot + busybox (W^X exec from filesDir) ---")
            val a = rt.runProbe(listOf("/bin/busybox", "uname", "-a"), 30_000)
            log("exit=${a.first} :: ${a.second.trim().take(160)}")

            log("\n--- python + ssl + worker import (offline) ---")
            val b = rt.runProbe(
                listOf(
                    "/usr/bin/env", "PATH=/usr/bin:/bin", "HOME=/root", "PYTHONPATH=/opt/worker",
                    "/usr/bin/python3", "-c",
                    "import sys, ssl, python_src.entrypoints.cli; print('python', sys.version.split()[0]); print(ssl.OPENSSL_VERSION); print('worker import OK')"
                ),
                90_000
            )
            log("exit=${b.first}\n${b.second.trim()}")

            log("\n--- fs + shell (write/read a file in rootfs) ---")
            val c = rt.runProbe(
                listOf("/usr/bin/env", "PATH=/usr/bin:/bin", "/bin/sh", "-c",
                    "echo hi-from-${rt.deviceAbi} > /root/probe.txt && cat /root/probe.txt"),
                30_000
            )
            log("exit=${c.first} :: ${c.second.trim()}")

            log("\n--- timeout resilience (force destroy mid-read; engine must NOT crash) ---")
            val d = rt.runProbe(listOf("/bin/busybox", "sleep", "30"), 2_000)
            log("exit=${d.first} :: ${d.second.trim().take(80)} (expected timeout, process alive)")

            log("\n== self-test done ==")
        } catch (t: Throwable) {
            Log.e(TAG, "self-test failed", t)
            log("ERROR: ${t.message}\n${Log.getStackTraceString(t)}")
        }
    }

    private fun log(line: String) {
        Log.i(TAG, line)
        runOnUiThread { out.append(line + "\n") }
    }
}
