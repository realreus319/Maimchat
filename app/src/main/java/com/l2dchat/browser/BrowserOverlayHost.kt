package com.l2dchat.browser

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.graphics.Color
import android.graphics.PixelFormat
import android.graphics.Typeface
import android.hardware.display.DisplayManager
import android.os.Build
import android.provider.Settings
import android.util.Log
import android.view.Display
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.widget.Button
import android.widget.FrameLayout
import android.widget.HorizontalScrollView
import android.widget.LinearLayout
import android.widget.TextView
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * Process-wide host that renders the agent-driven multi-tab browser ([BrowserTabs]) inside a
 * **WindowManager overlay** (`TYPE_APPLICATION_OVERLAY`).
 *
 * Visibility model: the overlay window is created HIDDEN — full size so the active tab's WebView has a
 * real viewport and actually loads pages, but `alpha ≈ 0` + NOT_TOUCHABLE/NOT_FOCUSABLE so it's
 * invisible and touches pass through to the chat. The user opens it from the agent bubble's browser
 * icon ([BrowserComm.ACTION_BROWSER_EXPAND] → [expand]). Collapsing returns it out of sight WITHOUT
 * tearing it down, so the tabs stay viewable after the run ends.
 *
 * Multi-tab: a horizontal tab strip sits above the WebView; tapping a tab switches the visible WebView,
 * ✕ closes it. Only the active tab's WebView is attached at a time (the others' pages stay alive).
 */
object BrowserOverlayHost {

    private const val TAG = "BrowserOverlayHost"

    // Per-task browser contexts (Option C): each background worker (taskId) gets its OWN BrowserTabs
    // (isolated pages/cookies/state). Only the CURRENT task's active WebView is attached to the holder
    // at a time; the others stay alive detached. Callers serialize (make-current + execute) via
    // [browserMutex] so concurrent workers don't race the single holder.
    private val tabsByTask = linkedMapOf<String, BrowserTabs>()
    @Volatile
    private var currentTaskId: String? = null
    val tabs: BrowserTabs?
        get() = currentTaskId?.let { tabsByTask[it] } ?: tabsByTask.values.firstOrNull()
    val browserMutex = kotlinx.coroutines.sync.Mutex()

    @Volatile
    var isExpanded: Boolean = false
        private set

    private val scope = CoroutineScope(Dispatchers.Main + SupervisorJob())

    // Main-thread-only view refs.
    private var uiContext: Context? = null
    private var container: FrameLayout? = null
    private var windowManager: WindowManager? = null
    private var urlBar: android.widget.EditText? = null
    private var tabStrip: LinearLayout? = null
    private var webHolder: FrameLayout? = null
    private var params: WindowManager.LayoutParams? = null
    private var expandReceiver: BroadcastReceiver? = null
    private var receiverContext: Context? = null

    fun isOverlayGranted(context: Context): Boolean =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) Settings.canDrawOverlays(context) else true

    /**
     * Ensure the overlay (with a live [BrowserTabs]) exists and return it. Idempotent. The window comes
     * up HIDDEN — call [expand] (or send [BrowserComm.ACTION_BROWSER_EXPAND]) to show it.
     */
    suspend fun ensureShown(context: Context, taskId: String): BrowserTabs = withContext(Dispatchers.Main) {
        ensureWindow(context)
        val ui = uiContext ?: context.applicationContext
        val mgr = tabsByTask.getOrPut(taskId) { BrowserTabs(ui).also { it.onTabsChanged = { refreshTabsUi() } } }
        if (currentTaskId != taskId) {
            currentTaskId = taskId
            refreshTabsUi() // swap the holder to THIS task's active WebView
        }
        mgr.awaitActiveReady(timeoutMs = 5_000)
        mgr
    }

    /** Create the hidden overlay window + chrome ONCE. The holder starts empty; a task's tabs fill it. */
    private fun ensureWindow(context: Context) {
        if (container != null) return
        val appContext = context.applicationContext
        val ui: Context = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            val display = appContext.getSystemService(DisplayManager::class.java)
                .getDisplay(Display.DEFAULT_DISPLAY)
            appContext.createWindowContext(display, WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY, null)
        } else {
            appContext
        }
        uiContext = ui
        val wm = ui.getSystemService(Context.WINDOW_SERVICE) as WindowManager

        val root = FrameLayout(ui)
        val column = LinearLayout(ui).apply { orientation = LinearLayout.VERTICAL }

        // Title bar: editable address bar + go + collapse.
        val bar = LinearLayout(ui).apply {
            orientation = LinearLayout.HORIZONTAL
            setBackgroundColor(Color.parseColor("#222222"))
            gravity = Gravity.CENTER_VERTICAL
        }
        val address = android.widget.EditText(ui).apply {
            hint = "输入网址或搜索…"
            setText("")
            textSize = 12f
            setSingleLine()
            setTextColor(Color.WHITE)
            setHintTextColor(Color.parseColor("#999999"))
            setBackgroundColor(Color.parseColor("#333333"))
            setPadding(18, 10, 18, 10)
            inputType = android.text.InputType.TYPE_CLASS_TEXT or android.text.InputType.TYPE_TEXT_VARIATION_URI
            imeOptions = android.view.inputmethod.EditorInfo.IME_ACTION_GO
            setOnEditorActionListener { v, actionId, _ ->
                if (actionId == android.view.inputmethod.EditorInfo.IME_ACTION_GO) {
                    navigateTo(v.text.toString()); true
                } else false
            }
        }
        val goBtn = Button(ui).apply {
            text = "前往"
            textSize = 12f
            setOnClickListener { navigateTo(address.text.toString()) }
        }
        val collapseBtn = Button(ui).apply {
            text = "收起"
            textSize = 12f
            setOnClickListener { collapse() }
        }
        bar.addView(address, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        bar.addView(goBtn, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT))
        bar.addView(collapseBtn, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT))
        column.addView(bar, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))

        val strip = LinearLayout(ui).apply { orientation = LinearLayout.HORIZONTAL }
        val stripScroll = HorizontalScrollView(ui).apply {
            isHorizontalScrollBarEnabled = false
            setBackgroundColor(Color.parseColor("#1A1A1A"))
            addView(strip, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT))
        }
        column.addView(stripScroll, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))

        // WebView holder — empty until a task's active WebView is swapped in by refreshTabsUi().
        val holder = FrameLayout(ui)
        column.addView(holder, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))

        root.addView(column, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))

        val metrics = ui.resources.displayMetrics
        val width = metrics.widthPixels
        val height = (metrics.heightPixels * 0.7f).toInt()

        @Suppress("DEPRECATION")
        val type = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
        else
            WindowManager.LayoutParams.TYPE_PHONE

        val lp = WindowManager.LayoutParams(width, height, type, hiddenFlags(), PixelFormat.TRANSLUCENT).apply {
            gravity = Gravity.TOP or Gravity.CENTER_HORIZONTAL
            y = (metrics.density * 48).toInt()
            alpha = HIDDEN_ALPHA
        }

        wm.addView(root, lp)
        windowManager = wm
        container = root
        urlBar = address
        tabStrip = strip
        webHolder = holder
        params = lp
        isExpanded = false
        registerExpandReceiver(appContext)
        startTitleUpdates()
        Log.i(TAG, "overlay window created hidden (${width}x$height)")
    }

    /** Rebuild the tab chips + swap the holder to the active tab's WebView + update the title. */
    private fun refreshTabsUi() {
        val mgr = tabs ?: return
        val strip = tabStrip ?: return
        val holder = webHolder ?: return
        // Swap the visible WebView to the active tab.
        val active = mgr.activeView
        if (holder.childCount == 0 || holder.getChildAt(0) !== active) {
            (active.parent as? ViewGroup)?.removeView(active)
            holder.removeAllViews()
            holder.addView(active, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        }
        syncAddressBar()
        // Rebuild chips.
        strip.removeAllViews()
        val ctx = strip.context
        scope.launch {
            val infos = runCatching { mgr.listTabs() }.getOrDefault(emptyList())
            strip.removeAllViews()
            for (info in infos) {
                val chip = LinearLayout(ctx).apply {
                    orientation = LinearLayout.HORIZONTAL
                    gravity = Gravity.CENTER_VERTICAL
                    setBackgroundColor(if (info.active) Color.parseColor("#3A6EA5") else Color.parseColor("#2A2A2A"))
                    setPadding(20, 12, 12, 12)
                }
                val label = TextView(ctx).apply {
                    text = info.title.ifBlank { info.url.ifBlank { "新标签页" } }.take(18)
                    setTextColor(Color.WHITE)
                    textSize = 11f
                    maxLines = 1
                    if (info.active) setTypeface(typeface, Typeface.BOLD)
                    setOnClickListener { scope.launch { mgr.selectTab(info.index) } }
                }
                val close = TextView(ctx).apply {
                    text = "  ✕"
                    setTextColor(Color.parseColor("#FFBBBB"))
                    textSize = 11f
                    setOnClickListener {
                        scope.launch { mgr.execute("browser_tab_close", com.google.gson.JsonObject().apply { addProperty("index", info.index) }) }
                    }
                }
                chip.addView(label)
                chip.addView(close)
                val lpChip = LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT)
                lpChip.rightMargin = 4
                strip.addView(chip, lpChip)
            }
        }
    }

    fun expand() {
        val wm = windowManager ?: return
        val root = container ?: return
        val lp = params ?: return
        lp.flags = expandedFlags()
        lp.alpha = 1f
        runCatching { wm.updateViewLayout(root, lp) }
            .onFailure { Log.w(TAG, "expand failed: ${it.message}") }
        isExpanded = true
        Log.i(TAG, "overlay expanded")
    }

    fun collapse() {
        val wm = windowManager ?: return
        val root = container ?: return
        val lp = params ?: return
        lp.flags = hiddenFlags()
        lp.alpha = HIDDEN_ALPHA
        runCatching { wm.updateViewLayout(root, lp) }
            .onFailure { Log.w(TAG, "collapse failed: ${it.message}") }
        isExpanded = false
        Log.i(TAG, "overlay collapsed")
    }

    private fun hiddenFlags(): Int =
        WindowManager.LayoutParams.FLAG_HARDWARE_ACCELERATED or
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
            WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE

    private fun expandedFlags(): Int = WindowManager.LayoutParams.FLAG_HARDWARE_ACCELERATED

    private fun registerExpandReceiver(appContext: Context) {
        if (expandReceiver != null) return
        val receiver = object : BroadcastReceiver() {
            override fun onReceive(context: Context?, intent: Intent?) {
                when (intent?.action) {
                    BrowserComm.ACTION_BROWSER_EXPAND -> expand()
                    BrowserComm.ACTION_BROWSER_COLLAPSE -> collapse()
                }
            }
        }
        val filter = IntentFilter().apply {
            addAction(BrowserComm.ACTION_BROWSER_EXPAND)
            addAction(BrowserComm.ACTION_BROWSER_COLLAPSE)
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            appContext.registerReceiver(receiver, filter, Context.RECEIVER_NOT_EXPORTED)
        } else {
            @Suppress("UnspecifiedRegisterReceiverFlag")
            appContext.registerReceiver(receiver, filter)
        }
        expandReceiver = receiver
        receiverContext = appContext
    }

    fun dismiss() {
        expandReceiver?.let { r -> runCatching { receiverContext?.unregisterReceiver(r) } }
        expandReceiver = null
        receiverContext = null
        val wm = windowManager
        val root = container
        if (wm != null && root != null) {
            runCatching { wm.removeView(root) }.onFailure { Log.w(TAG, "removeView failed: ${it.message}") }
        }
        tabsByTask.values.forEach { runCatching { it.dismiss() } }
        tabsByTask.clear()
        currentTaskId = null
        uiContext = null
        container = null
        windowManager = null
        urlBar = null
        tabStrip = null
        webHolder = null
        params = null
        isExpanded = false
        Log.i(TAG, "overlay dismissed")
    }

    /** Reflect the active tab's URL in the address bar (unless the user is editing it). */
    private fun startTitleUpdates() {
        val holder = webHolder ?: return
        val tick = object : Runnable {
            override fun run() {
                syncAddressBar()
                holder.postDelayed(this, 1000)
            }
        }
        holder.postDelayed(tick, 1000)
    }

    private fun syncAddressBar() {
        val mgr = tabs ?: return
        val bar = urlBar ?: return
        if (bar.isFocused) return // don't clobber what the user is typing
        val u = mgr.activeView.url ?: ""
        if (bar.text.toString() != u) bar.setText(u)
    }

    /** Navigate the active tab to a typed address (or search it on Bing if it isn't a URL). */
    private fun navigateTo(raw: String) {
        val input = raw.trim()
        if (input.isEmpty()) return
        val url = when {
            input.contains("://") -> input
            input.contains(".") && !input.contains(" ") -> "https://$input"
            else -> "https://cn.bing.com/search?q=" +
                java.net.URLEncoder.encode(input, "UTF-8")
        }
        urlBar?.clearFocus()
        val mgr = tabs ?: return
        scope.launch {
            runCatching {
                mgr.execute(
                    "browser_navigate",
                    com.google.gson.JsonObject().apply { addProperty("url", url) },
                )
            }.onFailure { Log.w(TAG, "address-bar navigate failed: ${it.message}") }
            syncAddressBar()
        }
    }

    private const val HIDDEN_ALPHA = 0.004f
}
