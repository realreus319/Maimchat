package com.l2dchat.browser

import android.app.Activity
import android.content.Context
import android.graphics.Bitmap
import android.graphics.Rect
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import android.view.InputDevice
import android.view.MotionEvent
import android.view.PixelCopy
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.webkit.WebViewClient
import com.google.gson.Gson
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import java.io.ByteArrayOutputStream
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

/** One interactive element from a [BrowserSnapshot]. `rect` is in CSS px, relative to the viewport. */
data class ElementRect(val x: Float, val y: Float, val w: Float, val h: Float)

data class BrowserElement(
    val ref: String,
    val tag: String,
    val role: String,
    val name: String,
    val value: String,
    val rect: ElementRect,
)

data class BrowserSnapshot(
    val url: String,
    val title: String,
    val elements: List<BrowserElement>,
) {
    /** Human-readable rendering, one line per interactive element (for debug logs / model prompts). */
    fun toReadableText(): String {
        val sb = StringBuilder()
        sb.append("url: ").append(url).append('\n')
        sb.append("title: ").append(title).append('\n')
        sb.append("elements (").append(elements.size).append("):\n")
        for (e in elements) {
            sb.append("  [ref=").append(e.ref).append("] ").append(e.tag)
            if (e.role.isNotEmpty()) sb.append(" role=").append(e.role)
            if (e.name.isNotEmpty()) sb.append(" \"").append(e.name).append('"')
            if (e.value.isNotEmpty()) sb.append(" value=\"").append(e.value).append('"')
            sb.append(" @(")
                .append(e.rect.x.toInt()).append(',').append(e.rect.y.toInt())
                .append(' ').append(e.rect.w.toInt()).append('x').append(e.rect.h.toInt())
                .append(")\n")
        }
        return sb.toString()
    }
}

/**
 * Action layer on top of an in-process [WebView] + an in-process [CdpConnection].
 *
 * Threading contract: every WebView touch (create/loadUrl/evaluateJavascript/dispatchTouchEvent/
 * PixelCopy) runs on the main thread via [withContext]; CDP/HTTP networking runs on IO inside
 * [CdpConnection]. Construct this on the main thread (the WebView is created in the constructor).
 */
class BrowserController(private val context: Context) {

    private val tag = "BrowserController"
    private val gson = Gson()
    private val mainHandler = Handler(Looper.getMainLooper())

    /** ref -> rect (CSS px, viewport-relative) captured by the most recent [snapshot]. */
    private val refRects = HashMap<String, ElementRect>()

    private val cdpMutex = Mutex()
    private var cdpConn: CdpConnection? = null

    @Volatile
    private var pageLoad: CompletableDeferred<Unit>? = null

    @Volatile
    var progress: Int = 0

    // Multi-tab hooks (set by BrowserTabs). `claimedPathsProvider` returns CDP page paths already bound
    // to OTHER tabs so this tab's cdp() binds to a distinct WebView page; `onCreateWindowRequest` is
    // invoked when a page opens a new window (target=_blank / window.open) to mint a new tab's WebView.
    var claimedPathsProvider: () -> Set<String> = { emptySet() }
    var onCreateWindowRequest: (() -> WebView?)? = null

    /** The CDP page path this tab is bound to (null until [cdp] first connects). */
    val cdpPagePath: String? get() = cdpConn?.pagePath

    val view: WebView

    init {
        // setWebContentsDebuggingEnabled is process-global; set it once before any CDP attach.
        WebView.setWebContentsDebuggingEnabled(true)
        view = WebView(context).apply {
            @Suppress("SetJavaScriptEnabled")
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            // Let target=_blank / window.open create real new tabs (handled in onCreateWindow below).
            settings.setSupportMultipleWindows(true)
            settings.javaScriptCanOpenWindowsAutomatically = true
            webViewClient = object : WebViewClient() {
                override fun onPageStarted(view: WebView?, url: String?, favicon: android.graphics.Bitmap?) {
                    // Logged the instant a navigation begins — robust even if the process is killed
                    // shortly after (e.g. backgrounded under memory pressure on a busy device).
                    Log.i(this@BrowserController.tag, "onPageStarted: $url")
                }
                override fun onPageFinished(view: WebView?, url: String?) {
                    Log.i(this@BrowserController.tag, "onPageFinished: $url")
                    pageLoad?.complete(Unit)
                }
                override fun onReceivedError(
                    view: WebView?,
                    request: android.webkit.WebResourceRequest?,
                    error: android.webkit.WebResourceError?,
                ) {
                    Log.w(
                        this@BrowserController.tag,
                        "onReceivedError: ${request?.url} code=${error?.errorCode} desc=${error?.description}",
                    )
                }
                override fun onReceivedHttpError(
                    view: WebView?,
                    request: android.webkit.WebResourceRequest?,
                    errorResponse: android.webkit.WebResourceResponse?,
                ) {
                    Log.w(
                        this@BrowserController.tag,
                        "onReceivedHttpError: ${request?.url} status=${errorResponse?.statusCode}",
                    )
                }
            }
            webChromeClient = object : WebChromeClient() {
                override fun onProgressChanged(view: WebView?, newProgress: Int) {
                    this@BrowserController.progress = newProgress
                }
                override fun onCreateWindow(
                    view: WebView?,
                    isDialog: Boolean,
                    isUserGesture: Boolean,
                    resultMsg: android.os.Message?,
                ): Boolean {
                    // A page opened a new window (target=_blank / window.open): mint a new tab and hand
                    // its WebView to the transport so the new page loads there.
                    val newView = onCreateWindowRequest?.invoke()
                    val transport = resultMsg?.obj as? WebView.WebViewTransport
                    if (newView == null || transport == null) return false
                    transport.webView = newView
                    // Defer the load until the new tab's WebView is attached + laid out (the overlay
                    // swap in onCreateWindowRequest attached it); a real-device WebView drops a load
                    // issued before the view is sized.
                    newView.post { resultMsg.sendToTarget() }
                    return true
                }
            }
        }
    }

    /** Lazily attach a CDP connection to THIS process's WebView and enable the P1 domains. */
    private suspend fun cdp(): CdpConnection = cdpMutex.withLock {
        cdpConn ?: run {
            val conn = CdpConnection.connect(claimedPathsProvider())
            conn.enableDomain("Page")
            conn.enableDomain("Runtime")
            conn.enableDomain("DOM")
            cdpConn = conn
            conn
        }
    }

    /** Suspend until this tab's WebView is attached to its window and laid out (or [timeoutMs]). New
     *  tabs must be ready before loadUrl — a real-device WebView drops a load issued while unsized. */
    suspend fun awaitReady(timeoutMs: Long = 5_000) {
        val v = view
        if (v.isAttachedToWindow && v.width > 0) return
        withContext(Dispatchers.Main) {
            kotlinx.coroutines.withTimeoutOrNull(timeoutMs) {
                suspendCancellableCoroutine<Unit> { cont ->
                    val listener = object : android.view.View.OnLayoutChangeListener {
                        override fun onLayoutChange(
                            vv: android.view.View, l: Int, t: Int, r: Int, b: Int,
                            ol: Int, ot: Int, orr: Int, ob: Int,
                        ) {
                            if (vv.isAttachedToWindow && vv.width > 0) {
                                vv.removeOnLayoutChangeListener(this)
                                if (cont.isActive) cont.resume(Unit)
                            }
                        }
                    }
                    v.addOnLayoutChangeListener(listener)
                    cont.invokeOnCancellation { v.removeOnLayoutChangeListener(listener) }
                    if (v.isAttachedToWindow && v.width > 0) {
                        v.removeOnLayoutChangeListener(listener)
                        if (cont.isActive) cont.resume(Unit)
                    }
                }
            }
        }
    }

    /** Navigate and suspend until onPageFinished (or [timeoutMs]); returns current url + title. */
    suspend fun navigate(url: String, timeoutMs: Long = 30_000): Pair<String, String> {
        val deferred = CompletableDeferred<Unit>()
        withContext(Dispatchers.Main) {
            pageLoad = deferred
            Log.i(tag, "navigate -> loadUrl(\"$url\") view=${view.width}x${view.height} attached=${view.isAttachedToWindow}")
            view.loadUrl(url)
        }
        withTimeout(timeoutMs) { deferred.await() }
        return withContext(Dispatchers.Main) { (view.url ?: url) to (view.title ?: "") }
    }

    /**
     * Walk the same-origin DOM, tag each interactive element with `data-l2d-ref`, and return a
     * structured snapshot. (Cross-origin iframes via CDP's full tree are deferred to P2.)
     */
    suspend fun snapshot(): BrowserSnapshot {
        val raw = evalJs(SNAPSHOT_JS)
        // evaluateJavascript serializes the returned object to JSON directly (not double-quoted).
        val obj = JsonParser.parseString(raw).asJsonObject
        val url = obj.get("url").asString
        val title = obj.get("title").asString
        val elements = ArrayList<BrowserElement>()
        refRects.clear()
        for (el in obj.getAsJsonArray("elements")) {
            val e = el.asJsonObject
            val r = e.getAsJsonObject("rect")
            val rect = ElementRect(
                r.get("x").asFloat, r.get("y").asFloat, r.get("w").asFloat, r.get("h").asFloat,
            )
            val element = BrowserElement(
                ref = e.get("ref").asString,
                tag = e.get("tag").asString,
                role = e.get("role").asString,
                name = e.get("name").asString,
                value = e.get("value").asString,
                rect = rect,
            )
            elements.add(element)
            refRects[element.ref] = rect
        }
        return BrowserSnapshot(url, title, elements)
    }

    /**
     * Synthetic-touch click on the element identified by [ref] (must come from the latest snapshot).
     * Coordinate conversion: viewPx = cssPx * (WebView pixel width / CSS viewport width). This
     * pixels-per-CSS-px ratio is exact across density and page zoom — unlike [WebView.getScale],
     * which is deprecated and returns 1.0 on modern WebView (which mislands the touch near the top
     * of the page). getBoundingClientRect is already viewport-relative, so no scroll offset is added.
     */
    suspend fun click(ref: String): Boolean {
        val rect = refRects[ref]
            ?: throw IllegalStateException("Unknown ref '$ref' — run snapshot() first")
        // CDP Input.dispatchMouseEvent: coordinates are CSS px viewport-relative (same space as
        // getBoundingClientRect — no scale conversion), and the renderer treats it as a trusted
        // (isTrusted=true) click that actually fires navigation/handlers. This is the path
        // Playwright/Puppeteer use. Synthetic dispatchTouchEvent (see [clickSynthetic]) is kept for
        // the future dual-path but empirically does NOT activate WebView links on this build.
        val cx = (rect.x + rect.w / 2f).toDouble()
        val cy = (rect.y + rect.h / 2f).toDouble()
        val conn = cdp()
        // Full move -> press -> release sequence (what Puppeteer/Playwright do). A prior mouseMoved
        // sets the renderer's hover target; without it Chromium often drops the synthesized click.
        conn.send("Input.dispatchMouseEvent", mouseEvent("mouseMoved", cx, cy))
        conn.send("Input.dispatchMouseEvent", mouseEvent("mousePressed", cx, cy))
        conn.send("Input.dispatchMouseEvent", mouseEvent("mouseReleased", cx, cy))
        Log.i(tag, "click($ref) via CDP at css($cx,$cy)")
        return true
    }

    private fun mouseEvent(type: String, x: Double, y: Double): JsonObject = JsonObject().apply {
        addProperty("type", type)
        addProperty("x", x)
        addProperty("y", y)
        when (type) {
            "mousePressed" -> {
                addProperty("button", "left"); addProperty("buttons", 1); addProperty("clickCount", 1)
            }
            "mouseReleased" -> {
                addProperty("button", "left"); addProperty("buttons", 0); addProperty("clickCount", 1)
            }
            else -> { // mouseMoved
                addProperty("button", "none"); addProperty("buttons", 0)
            }
        }
    }

    /**
     * Synthetic-touch click (no CDP). Kept for the P2 input dual-path. NOTE: on this WebView build
     * it dispatches but does NOT activate links/handlers — use [click] (CDP) for real clicks.
     * Coordinate conversion: viewPx = cssPx * (WebView pixel width / CSS viewport width).
     */
    suspend fun clickSynthetic(ref: String): Boolean {
        val rect = refRects[ref]
            ?: throw IllegalStateException("Unknown ref '$ref' — run snapshot() first")
        val innerWidth = evalJs("window.innerWidth").trim().toFloatOrNull()
        val viewWidth = withContext(Dispatchers.Main) { view.width.toFloat() }
        val scale =
            if (innerWidth != null && innerWidth > 0f && viewWidth > 0f) viewWidth / innerWidth
            else context.resources.displayMetrics.density
        val cx = (rect.x + rect.w / 2f) * scale
        val cy = (rect.y + rect.h / 2f) * scale
        val downTime = SystemClock.uptimeMillis()
        withContext(Dispatchers.Main) {
            dispatchTouch(downTime, downTime, MotionEvent.ACTION_DOWN, cx, cy)
        }
        delay(60)
        withContext(Dispatchers.Main) {
            dispatchTouch(downTime, SystemClock.uptimeMillis(), MotionEvent.ACTION_UP, cx, cy)
        }
        return true
    }

    private fun dispatchTouch(downTime: Long, eventTime: Long, action: Int, x: Float, y: Float) {
        val ev = MotionEvent.obtain(downTime, eventTime, action, x, y, 0)
        // WebView's gesture detector ignores events with SOURCE_UNKNOWN (the obtain() default);
        // mark them as coming from the touchscreen so taps actually activate links/buttons.
        ev.source = InputDevice.SOURCE_TOUCHSCREEN
        view.dispatchTouchEvent(ev)
        ev.recycle()
    }

    /**
     * Focus the [ref] element, set its value, and fire input/change. With [submit], also dispatch
     * Enter and submit the owning form. (Human-like per-key synthesis is deferred to a later phase.)
     */
    suspend fun type(ref: String, text: String, submit: Boolean = false): Boolean {
        val refJson = gson.toJson(ref)
        val textJson = gson.toJson(text)
        val js = buildString {
            append("(function(){")
            append("var ref=").append(refJson).append(";")
            append("var el=document.querySelector('[data-l2d-ref=\"'+ref+'\"]');")
            append("if(!el){return 'notfound';}")
            append("el.focus();")
            append("if('value' in el){el.value=").append(textJson).append(";}")
            append("else{el.textContent=").append(textJson).append(";}")
            append("el.dispatchEvent(new Event('input',{bubbles:true}));")
            append("el.dispatchEvent(new Event('change',{bubbles:true}));")
            if (submit) {
                append("el.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',keyCode:13,which:13,bubbles:true}));")
                append("el.dispatchEvent(new KeyboardEvent('keyup',{key:'Enter',keyCode:13,which:13,bubbles:true}));")
                append("if(el.form){el.form.requestSubmit?el.form.requestSubmit():el.form.submit();}")
            }
            append("return 'ok';")
            append("})()")
        }
        val result = evalJs(js)
        if (result.contains("notfound")) {
            throw IllegalStateException("type: element with ref '$ref' not found in DOM")
        }
        return true
    }

    /** PNG screenshot of the WebView via PixelCopy (captures hardware-accelerated web content). */
    suspend fun screenshot(): ByteArray = withContext(Dispatchers.Main) {
        val w = view.width
        val h = view.height
        if (w <= 0 || h <= 0) {
            throw IllegalStateException("WebView is not laid out (size ${w}x$h)")
        }
        val window = (context as? Activity)?.window
            ?: throw IllegalStateException("PixelCopy needs an Activity context (window)")
        val bitmap = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888)
        val loc = IntArray(2)
        view.getLocationInWindow(loc)
        val src = Rect(loc[0], loc[1], loc[0] + w, loc[1] + h)
        suspendCancellableCoroutine<Unit> { cont ->
            PixelCopy.request(window, src, bitmap, { result ->
                if (result == PixelCopy.SUCCESS) {
                    cont.resume(Unit)
                } else {
                    cont.resumeWithException(RuntimeException("PixelCopy failed: code=$result"))
                }
            }, mainHandler)
        }
        ByteArrayOutputStream().use { baos ->
            bitmap.compress(Bitmap.CompressFormat.PNG, 100, baos)
            baos.toByteArray()
        }
    }

    /** Result of a Playwright-MCP-style tool dispatch. [text] is what the model sees. */
    data class ExecResult(val text: String, val isError: Boolean = false)

    /**
     * Dispatch a Playwright-MCP-compatible tool by [name] with [args] (the tool's `arguments` object).
     * Mirrors Playwright MCP semantics: action tools (navigate/click/type/back) return the *new* page
     * snapshot as their result text; browser_snapshot returns the snapshot directly. Unknown tools
     * yield an error result (never throws for routing — the model gets a readable message).
     */
    suspend fun execute(name: String, args: JsonObject): ExecResult {
        fun str(key: String): String? = args.get(key)?.takeIf { !it.isJsonNull }?.asString
        when (name) {
            "browser_navigate" -> {
                val url = str("url") ?: return ExecResult("browser_navigate: missing 'url'", isError = true)
                navigate(url)
                return ExecResult(snapshot().toReadableText())
            }
            "browser_snapshot" -> return ExecResult(snapshot().toReadableText())
            "browser_click" -> {
                val ref = str("ref") ?: return ExecResult("browser_click: missing 'ref'", isError = true)
                click(ref)
                return ExecResult(snapshot().toReadableText())
            }
            "browser_type" -> {
                val ref = str("ref") ?: return ExecResult("browser_type: missing 'ref'", isError = true)
                val text = str("text") ?: return ExecResult("browser_type: missing 'text'", isError = true)
                val submit = args.get("submit")?.takeIf { !it.isJsonNull }?.asBoolean ?: false
                type(ref, text, submit)
                return ExecResult(snapshot().toReadableText())
            }
            "browser_navigate_back" -> {
                navigateBack()
                return ExecResult(snapshot().toReadableText())
            }
            "browser_take_screenshot" -> {
                val png = screenshot()
                return ExecResult("screenshot: ${png.size} bytes (visual host pending P4)")
            }
            else -> return ExecResult("unknown tool: $name", isError = true)
        }
    }

    /** Navigate back in history and suspend until the page finishes loading (or [timeoutMs]). */
    suspend fun navigateBack(timeoutMs: Long = 30_000): Pair<String, String> {
        val deferred = CompletableDeferred<Unit>()
        withContext(Dispatchers.Main) {
            pageLoad = deferred
            view.goBack()
        }
        withTimeout(timeoutMs) { deferred.await() }
        return withContext(Dispatchers.Main) { (view.url ?: "") to (view.title ?: "") }
    }

    /**
     * Evaluate [expr] through CDP `Runtime.evaluate` and return its value as a string. Used to prove
     * the in-process CDP link is actually live (independent of evaluateJavascript).
     */
    suspend fun evaluateCdp(expr: String): String {
        val conn = cdp()
        val params = JsonObject().apply {
            addProperty("expression", expr)
            addProperty("returnByValue", true)
        }
        val res = conn.send("Runtime.evaluate", params)
        if (res.has("exceptionDetails")) {
            throw CdpConnection.CdpException("Runtime.evaluate threw: ${res.get("exceptionDetails")}")
        }
        val result = res.getAsJsonObject("result")
        val value = result?.get("value")
        return when {
            value != null && !value.isJsonNull -> if (value.isJsonPrimitive) value.asString else value.toString()
            result != null && result.has("description") -> result.get("description").asString
            else -> result?.toString() ?: "null"
        }
    }

    private suspend fun evalJs(js: String): String = withContext(Dispatchers.Main) {
        suspendCancellableCoroutine { cont ->
            view.evaluateJavascript(js) { value -> cont.resume(value ?: "null") }
        }
    }

    fun close() {
        cdpConn?.close()
        cdpConn = null
        mainHandler.post {
            view.stopLoading()
            view.destroy()
        }
    }

    companion object {
        private val SNAPSHOT_JS = """
            (function(){
              var sel='a,button,input,textarea,select,[role=button],[onclick],[contenteditable]';
              var nodes=document.querySelectorAll(sel);
              var out=[];var i=0;
              for(var k=0;k<nodes.length;k++){
                var el=nodes[k];
                var r=el.getBoundingClientRect();
                if(r.width<=0||r.height<=0){continue;}
                i++;var ref='e'+i;
                el.setAttribute('data-l2d-ref',ref);
                var name='';
                if(el.getAttribute('aria-label')){name=el.getAttribute('aria-label');}
                else if(el.tagName.toLowerCase()==='input'&&el.getAttribute('placeholder')){name=el.getAttribute('placeholder');}
                else if(el.innerText){name=el.innerText;}
                else if(el.value){name=el.value;}
                name=(name||'').replace(/\s+/g,' ').trim().slice(0,120);
                out.push({
                  ref:ref,
                  tag:el.tagName.toLowerCase(),
                  role:el.getAttribute('role')||'',
                  name:name,
                  value:(('value' in el)&&el.value)?String(el.value):'',
                  rect:{x:r.x,y:r.y,w:r.width,h:r.height}
                });
              }
              return {url:location.href,title:document.title,elements:out};
            })()
        """.trimIndent()
    }
}
