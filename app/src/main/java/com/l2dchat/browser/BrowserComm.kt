package com.l2dchat.browser

import android.content.Context
import android.content.Intent

/**
 * Cross-process control channel for the agent browser overlay.
 *
 * The overlay [BrowserOverlayHost] lives in the process that drives the worker bridge (the `:chat`
 * process for the real chat flow), while the chat UI (and its per-bubble "open browser" icon) renders
 * in the main process. A signature-restricted broadcast lets the UI ask the overlay to expand/collapse
 * without coupling the two processes.
 */
object BrowserComm {

    const val ACTION_BROWSER_EXPAND = "com.l2dchat.browser.action.EXPAND"
    const val ACTION_BROWSER_COLLAPSE = "com.l2dchat.browser.action.COLLAPSE"

    /** Ask the (hidden) agent browser overlay to expand to full screen so the user can view it. */
    fun requestExpand(context: Context) = send(context, ACTION_BROWSER_EXPAND)

    /** Ask the expanded agent browser overlay to collapse back out of sight. */
    fun requestCollapse(context: Context) = send(context, ACTION_BROWSER_COLLAPSE)

    private fun send(context: Context, action: String) {
        // setPackage keeps the broadcast in-app (also required for delivery on Android 8+ implicit
        // broadcast restrictions) so it reaches the :chat-process receiver.
        context.sendBroadcast(Intent(action).setPackage(context.packageName))
    }
}
