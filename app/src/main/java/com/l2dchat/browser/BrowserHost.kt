package com.l2dchat.browser

import android.app.Activity
import android.os.Bundle
import android.util.Log
import android.view.ViewGroup
import android.widget.LinearLayout
import kotlinx.coroutines.CompletableDeferred

/**
 * Process-wide rendezvous for the single visible [BrowserController] hosted by [BrowserHostActivity].
 *
 * The reverse browser bridge (see [com.l2dchat.worker.ShellEngineClient]) needs a live, laid-out
 * WebView to drive. The controller can only be created on the main thread inside an Activity (it
 * builds a WebView and PixelCopy needs a window), so the Activity publishes it here and the bridge
 * awaits readiness.
 */
object BrowserHost {

    private const val TAG = "BrowserHost"

    @Volatile
    var controller: BrowserController? = null
        private set

    @Volatile
    private var readySignal = CompletableDeferred<BrowserController>()

    /** Called by the host Activity once its controller (WebView) is created and attached. */
    fun publish(c: BrowserController) {
        controller = c
        val signal = readySignal
        if (!signal.isCompleted) signal.complete(c)
        Log.i(TAG, "controller published")
    }

    /** Called by the host Activity on teardown. Resets the ready gate for the next host. */
    fun clear(c: BrowserController) {
        if (controller === c) {
            controller = null
            readySignal = CompletableDeferred()
            Log.i(TAG, "controller cleared")
        }
    }

    /** Suspend until a controller is available (returns immediately if one already is). */
    suspend fun awaitReady(): BrowserController =
        controller ?: readySignal.await()
}

/**
 * Visible host for the agent-driven browser. Created on demand (the reverse bridge launches it when
 * no controller is yet published). The WebView fills the screen so the user can watch the agent
 * operate (P4 will dress this up with chrome / takeover; this is the P3 skeleton).
 *
 * Start manually: adb shell am start -n com.l2dchat/com.l2dchat.browser.BrowserHostActivity
 */
class BrowserHostActivity : Activity() {

    private lateinit var controller: BrowserController

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        controller = BrowserController(this)
        val root = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        root.addView(
            controller.view,
            LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT,
            ),
        )
        setContentView(root)
        BrowserHost.publish(controller)
    }

    override fun onDestroy() {
        super.onDestroy()
        BrowserHost.clear(controller)
        controller.close()
    }
}
