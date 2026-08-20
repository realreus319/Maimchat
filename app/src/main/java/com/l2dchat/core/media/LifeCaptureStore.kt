package com.l2dchat.core.media

import android.content.Context
import android.net.Uri
import android.webkit.MimeTypeMap
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Durable store for phase-7 life-capture media (photo / video / voice). Files live under
 * filesDir/life_capture/ — never cacheDir — because mem_nodes.media_path references the path
 * permanently; a cache sweep must not break mem references.
 */
object LifeCaptureStore {
    private const val DIR_NAME = "life_capture"

    fun durableDir(context: Context): File =
            File(context.filesDir, DIR_NAME).apply { if (!exists()) mkdirs() }

    /** New timestamped capture target: life_capture/<yyyyMMdd_HHmmss>.<ext> (suffix on collision). */
    fun newCaptureFile(context: Context, extension: String): File {
        val suffix = extension.trimStart('.').ifEmpty { "bin" }
        val stamp = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(Date())
        val dir = durableDir(context)
        var candidate = File(dir, "$stamp.$suffix")
        var counter = 1
        while (candidate.exists()) {
            candidate = File(dir, "${stamp}_$counter.$suffix")
            counter++
        }
        return candidate
    }

    /**
     * Copy a content:// pick (album image etc.) into the durable store, resolving the extension
     * from the provider's MIME type when possible. Returns the durable absolute path, or null
     * when the source cannot be read (partial copy is cleaned up).
     */
    fun copyIntoStore(context: Context, source: Uri, fallbackExtension: String): String? {
        val mimeExtension =
                context.contentResolver.getType(source)?.let {
                    MimeTypeMap.getSingleton().getExtensionFromMimeType(it)
                }
        val dest = newCaptureFile(context, mimeExtension ?: fallbackExtension)
        val input =
                context.contentResolver.openInputStream(source)
                        ?: run {
                            dest.delete()
                            return null
                        }
        return try {
            input.use { src -> dest.outputStream().use { out -> src.copyTo(out) } }
            dest.absolutePath
        } catch (e: Exception) {
            dest.delete()
            null
        }
    }
}
