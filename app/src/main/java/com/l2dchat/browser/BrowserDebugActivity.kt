package com.l2dchat.browser

import android.app.Activity
import android.os.Bundle
import android.util.Log
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/**
 * DEBUG: drives the in-process WebView + CDP browser stack by hand to validate the P1 actions
 * (navigate / snapshot-with-ref / synthetic-touch click / type / screenshot / CDP eval).
 * Start with: adb shell am start -n com.l2dchat/com.l2dchat.browser.BrowserDebugActivity
 */
class BrowserDebugActivity : Activity() {

    private val tag = "BrowserDebug"
    private val scope = CoroutineScope(Dispatchers.Main)
    private lateinit var controller: BrowserController
    private lateinit var out: TextView

    private lateinit var urlField: EditText
    private lateinit var refField: EditText
    private lateinit var textField: EditText

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        controller = BrowserController(this)

        val root = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }

        // Row 1: url + Go
        urlField = EditText(this).apply { setText("https://example.com"); textSize = 12f }
        val goBtn = Button(this).apply { text = "Go" }
        root.addView(row(urlField, goBtn))

        // Row 2: ref + Click
        refField = EditText(this).apply { hint = "ref (e.g. e1)"; textSize = 12f }
        val clickBtn = Button(this).apply { text = "Click" }
        root.addView(row(refField, clickBtn))

        // Row 3: text + Type
        textField = EditText(this).apply { hint = "text to type"; textSize = 12f }
        val typeBtn = Button(this).apply { text = "Type" }
        root.addView(row(textField, typeBtn))

        // Row 4: snapshot / screenshot / eval
        val snapBtn = Button(this).apply { text = "Snapshot" }
        val shotBtn = Button(this).apply { text = "Screenshot(CDP-check)" }
        val evalBtn = Button(this).apply { text = "Eval CDP(document.title)" }
        root.addView(LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            addView(snapBtn, eqWeight())
            addView(shotBtn, eqWeight())
            addView(evalBtn, eqWeight())
        })

        // Log output (fixed-ish height, scrollable)
        out = TextView(this).apply { textSize = 10f; setPadding(12, 12, 12, 12) }
        val logScroll = ScrollView(this).apply {
            addView(out)
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0,
            ).apply { weight = 1f }
        }
        root.addView(logScroll)

        // WebView occupies the main area
        root.addView(controller.view, LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, 0,
        ).apply { weight = 3f })

        setContentView(root)
        log("== browser P1 debug ==")

        goBtn.setOnClickListener {
            val url = urlField.text.toString().trim()
            launch("navigate") {
                val (u, t) = controller.navigate(url)
                log("navigated -> url=$u title=\"$t\"")
            }
        }
        snapBtn.setOnClickListener {
            launch("snapshot") {
                val snap = controller.snapshot()
                log(snap.toReadableText())
            }
        }
        clickBtn.setOnClickListener {
            val ref = refField.text.toString().trim()
            launch("click") {
                val ok = controller.click(ref)
                log("click($ref) -> $ok")
            }
        }
        typeBtn.setOnClickListener {
            val ref = refField.text.toString().trim()
            val text = textField.text.toString()
            launch("type") {
                val ok = controller.type(ref, text, submit = false)
                log("type($ref, \"$text\") -> $ok")
            }
        }
        shotBtn.setOnClickListener {
            launch("screenshot") {
                val png = controller.screenshot()
                log("screenshot -> ${png.size} bytes PNG")
            }
        }
        evalBtn.setOnClickListener {
            launch("evalCdp") {
                val r = controller.evaluateCdp("document.title")
                log("evaluateCdp(document.title) -> \"$r\"")
            }
        }

        // Auto self-test (no taps — survives other apps stealing the foreground):
        //   adb shell am start -n com.l2dchat/com.l2dchat.browser.BrowserDebugActivity --es auto click
        if (intent.getStringExtra("auto") == "click") {
            launch("auto-click") {
                val url = urlField.text.toString().trim()
                val (u1, t1) = controller.navigate(url)
                log("auto: navigated -> $u1 \"$t1\"")
                val snap = controller.snapshot()
                log("auto:\n" + snap.toReadableText())
                val ref = snap.elements.firstOrNull()?.ref
                if (ref == null) { log("auto: no clickable element"); return@launch }
                controller.click(ref)
                log("auto: click($ref) dispatched, waiting for navigation…")
                delay(3500)
                log("auto: AFTER click -> url=${controller.view.url} title=\"${controller.view.title}\"")
            }
        }
    }

    private fun row(field: EditText, button: Button): LinearLayout =
        LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            addView(field, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
            addView(button)
        }

    private fun eqWeight() = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)

    private fun launch(label: String, block: suspend () -> Unit) {
        scope.launch {
            try {
                block()
            } catch (t: Throwable) {
                Log.e(tag, "$label failed", t)
                log("ERROR [$label]: ${t.message}")
            }
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        controller.close()
    }

    private fun log(line: String) {
        Log.i(tag, line)
        runOnUiThread { out.append(line + "\n") }
    }
}
