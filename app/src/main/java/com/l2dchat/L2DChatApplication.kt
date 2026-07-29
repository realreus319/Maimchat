package com.l2dchat

import android.app.Application
import android.os.Build
import android.webkit.WebView
import com.l2dchat.logging.L2DLogger
import com.l2dchat.logging.LogModule

class L2DChatApplication : Application() {
    private val logger by lazy { L2DLogger.module(LogModule.MAIN_VIEW) }

    override fun onCreate() {
        super.onCreate()
        // A given WebView data directory may only be used by ONE process at a time (crbug.com/558377).
        // The app runs WebView in multiple processes — the agent browser overlay is created from the
        // :chat process (where ChatConnectionService / the planner's ShellEngineClient live), while the
        // debug activities create it in the main process. Give each process its own WebView data dir so
        // they never collide. Must run before any WebView is instantiated in this process, which
        // Application.onCreate guarantees.
        applyWebViewDataDirectorySuffix()
        L2DLogger.init(this)
        logger.info("L2DChatApplication initialized")
    }

    private fun applyWebViewDataDirectorySuffix() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.P) return
        val processName = currentProcessName()
        val suffix = if (processName.contains(':')) processName.substringAfter(':') else "main"
        try {
            WebView.setDataDirectorySuffix(suffix)
        } catch (_: IllegalStateException) {
            // Already set, or a WebView was already used in this process — nothing to do.
        }
    }

    private fun currentProcessName(): String {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            return android.os.Process.myProcessName()
        }
        return try {
            java.io.File("/proc/self/cmdline")
                .readText()
                .trim { it <= ' ' || it == '\u0000' }
        } catch (_: Throwable) {
            packageName
        }
    }
}
