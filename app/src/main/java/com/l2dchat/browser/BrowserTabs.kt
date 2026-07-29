package com.l2dchat.browser

import android.content.Context
import android.webkit.WebView
import com.google.gson.JsonObject
import java.util.concurrent.CopyOnWriteArrayList
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/** One tab's metadata for the overlay tab strip. */
data class TabInfo(val index: Int, val url: String, val title: String, val active: Boolean)

/**
 * Multi-tab coordinator over per-tab [BrowserController]s. The MCP browser tools route through
 * [execute]: the tab tools (browser_tab_list / _new / _select / _close, matching Playwright MCP) are
 * handled here; every other tool delegates to the ACTIVE tab. New tabs are created explicitly
 * (browser_tab_new) or implicitly when a page opens a new window (target=_blank / window.open). Each
 * tab's CDP binds to a distinct WebView page — the manager supplies the page-paths already claimed by
 * other tabs. Construct + mutate on the main thread (the bridge dispatches browser actions there).
 */
class BrowserTabs(private val context: Context) {

    private val tabs = CopyOnWriteArrayList<BrowserController>()

    @Volatile
    var activeIndex: Int = 0
        private set

    /** Fired whenever the tab set / active tab changes, so the overlay can refresh + swap the WebView. */
    @Volatile
    var onTabsChanged: (() -> Unit)? = null

    init {
        tabs.add(newController())
        activeIndex = 0
    }

    val activeView: WebView get() = tabs[activeIndex.coerceIn(0, tabs.size - 1)].view
    val tabCount: Int get() = tabs.size

    /** Wait until the active tab's WebView is attached + laid out (so its first load isn't dropped). */
    suspend fun awaitActiveReady(timeoutMs: Long = 5_000) {
        tabs.getOrNull(activeIndex.coerceIn(0, tabs.size - 1))?.awaitReady(timeoutMs)
    }

    private fun newController(): BrowserController {
        val c = BrowserController(context)
        c.claimedPathsProvider = {
            tabs.filter { it !== c }.mapNotNull { it.cdpPagePath }.filter { it.isNotEmpty() }.toSet()
        }
        c.onCreateWindowRequest = {
            // window.open / target=_blank → mint a new tab, make it active, attach + return its WebView.
            val nc = newController()
            tabs.add(nc)
            activeIndex = tabs.size - 1
            onTabsChanged?.invoke() // overlay attaches nc.view synchronously (we are on the main thread)
            nc.view
        }
        return c
    }

    /** Snapshot of tabs for the UI. Reads WebView url/title on the main thread. */
    suspend fun listTabs(): List<TabInfo> = withContext(Dispatchers.Main) {
        tabs.mapIndexed { i, c ->
            TabInfo(i, c.view.url ?: "", c.view.title ?: "", i == activeIndex)
        }
    }

    suspend fun selectTab(index: Int) {
        if (index in tabs.indices) {
            activeIndex = index
            withContext(Dispatchers.Main) { onTabsChanged?.invoke() }
        }
    }

    /** Dispatch a Playwright-MCP tool: tab tools here, everything else on the active tab. */
    suspend fun execute(name: String, args: JsonObject): BrowserController.ExecResult {
        return when (name) {
            "browser_tab_list" -> BrowserController.ExecResult(renderTabs(listTabs()))
            "browser_tab_new" -> {
                val url = args.get("url")?.takeIf { !it.isJsonNull }?.asString
                val nc = newController()
                tabs.add(nc)
                activeIndex = tabs.size - 1
                withContext(Dispatchers.Main) { onTabsChanged?.invoke() }
                // Attach happens in onTabsChanged (overlay swap); wait for layout before loading so the
                // real-device WebView doesn't drop the loadUrl issued before the view is sized.
                nc.awaitReady()
                if (!url.isNullOrBlank()) nc.navigate(url)
                BrowserController.ExecResult("opened tab $activeIndex\n" + renderTabs(listTabs()))
            }
            "browser_tab_select" -> {
                val idx = args.get("index")?.takeIf { !it.isJsonNull }?.asInt
                    ?: return BrowserController.ExecResult("browser_tab_select: missing 'index'", isError = true)
                if (idx !in tabs.indices) return BrowserController.ExecResult("no tab $idx", isError = true)
                selectTab(idx)
                tabs[activeIndex].execute("browser_snapshot", JsonObject())
            }
            "browser_tab_close" -> {
                val idx = args.get("index")?.takeIf { !it.isJsonNull }?.asInt ?: activeIndex
                closeTab(idx)
                BrowserController.ExecResult("closed tab $idx\n" + renderTabs(listTabs()))
            }
            else -> tabs[activeIndex.coerceIn(0, tabs.size - 1)].execute(name, args)
        }
    }

    private suspend fun closeTab(index: Int) {
        if (index !in tabs.indices) return
        if (tabs.size <= 1) {
            // Keep at least one tab — just blank it instead of removing the last one.
            tabs[0].navigate("about:blank")
            withContext(Dispatchers.Main) { onTabsChanged?.invoke() }
            return
        }
        val c = tabs.removeAt(index)
        c.close()
        if (activeIndex >= tabs.size) activeIndex = tabs.size - 1
        withContext(Dispatchers.Main) { onTabsChanged?.invoke() }
    }

    private fun renderTabs(list: List<TabInfo>): String = buildString {
        append("tabs (").append(list.size).append("):\n")
        for (t in list) {
            append(if (t.active) "* " else "  ")
            append("[").append(t.index).append("] ")
            append(t.title.ifBlank { "(untitled)" })
            append(" — ").append(t.url.ifBlank { "about:blank" }).append('\n')
        }
    }

    fun dismiss() {
        tabs.forEach { it.close() }
        tabs.clear()
    }
}
