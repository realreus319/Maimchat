package com.l2dchat.test

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log
import com.l2dchat.wallpaper.WallpaperChatCoordinator
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch

/**
 * Debug-only, adb-driven test control. Gives E2E tests RELIABLE message input + a clean-slate clear
 * without the device's flaky IME / tap path. Declared ONLY in the debug manifest
 * (app/src/debug/AndroidManifest.xml), so it never ships in release.
 *
 * The app must be running (not force-stopped) to receive these; launch MainActivity first, or add
 * `--include-stopped-packages` to the broadcast.
 *
 *   adb shell am broadcast -p com.l2dchat -a com.l2dchat.test.SEND  --es text "<message>"
 *   adb shell am broadcast -p com.l2dchat -a com.l2dchat.test.CLEAR
 *
 * Record retrieval (DUMP) and freeze/kill are done from the host via adb directly (read the DB +
 * l2dchat.log; `am kill` / `am force-stop` / `input keyevent 3` / `am make-uid-idle`) — no app hook
 * needed — so the on-device surface stays minimal (just input + clear).
 */
class TestControlReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val app = context.applicationContext
        when (intent.action) {
            ACTION_SEND -> {
                val text = intent.getStringExtra(EXTRA_TEXT).orEmpty()
                Log.i(TAG, "SEND: ${text.take(80)}")
                val pending = goAsync()
                CoroutineScope(Dispatchers.Main).launch {
                    try {
                        val ok = WallpaperChatCoordinator.sendMessage(app, text)
                        Log.i(TAG, "SEND done ok=$ok")
                    } catch (t: Throwable) {
                        Log.e(TAG, "SEND failed", t)
                    } finally {
                        pending.finish()
                    }
                }
            }
            ACTION_CLEAR -> {
                Log.i(TAG, "CLEAR")
                val pending = goAsync()
                CoroutineScope(Dispatchers.Main).launch {
                    try {
                        val ok = WallpaperChatCoordinator.clearChat(app)
                        Log.i(TAG, "CLEAR done ok=$ok")
                    } catch (t: Throwable) {
                        Log.e(TAG, "CLEAR failed", t)
                    } finally {
                        pending.finish()
                    }
                }
            }
        }
    }

    companion object {
        private const val TAG = "L2DTestControl"
        const val ACTION_SEND = "com.l2dchat.test.SEND"
        const val ACTION_CLEAR = "com.l2dchat.test.CLEAR"
        const val EXTRA_TEXT = "text"
    }
}
