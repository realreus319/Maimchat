package com.l2dchat.core.mem

import com.l2dchat.core.llm.LlmContentPart
import com.l2dchat.core.llm.LlmImageUrlPart
import com.l2dchat.core.llm.LlmTextPart
import java.io.File
import java.io.IOException
import java.util.Base64

/**
 * Multimodal user-message assembly for the extraction pipeline — Kotlin port
 * of `mem/multimodal.py`.
 *
 * [buildUserContent] renders an [IngestBatch] into the user message sent to the
 * extraction model. A text-only batch stays a plain string (returned as a
 * single-element list of one [LlmTextPart] for uniform caller handling); a
 * batch with image attachments becomes an OpenAI content-part list in which
 * every image is preceded by an `[IMAGE media_path=<path>]` marker, so the
 * model both sees the picture and has the path string at hand to fill
 * `life_capture` `media_path` metadata.
 *
 * ## Image inlining
 *
 * The Python reference reads image files and inlines base64 data URLs into the
 * content parts. This port replicates that exactly: file bytes are read via
 * [Files.readAllBytes], base64-encoded, and wrapped as
 * `data:image/<mime>;base64,<...>`. The mime subtype is inferred from the file
 * extension (unknown extensions fall back to `png`), matching the Python
 * `_MIME_BY_EXT` table.
 */
object Multimodal {

    /**
     * Maximum inline image size in bytes (provider gateways reject larger ones).
     *
     * Mirrors `MAX_IMAGE_BYTES = 10 * 1024 * 1024` in `multimodal.py`.
     */
    const val MAX_IMAGE_BYTES: Long = 10L * 1024L * 1024L

    /**
     * Image subtype by file extension; unknown extensions fall back to `png`.
     *
     * Mirrors `_MIME_BY_EXT` in `multimodal.py`. Keys are lowercased including
     * the leading dot.
     */
    private val MIME_BY_EXT: Map<String, String> =
        mapOf(
            ".png" to "png",
            ".jpg" to "jpeg",
            ".jpeg" to "jpeg",
            ".webp" to "webp",
            ".gif" to "gif",
        )

    /**
     * Build the extraction user message content for one batch.
     *
     * - When the batch carries no images, returns a single-element list with
     *   one [LlmTextPart] carrying [IngestBatch.text] (the caller may treat it
     *   as plain text by reading the single part).
     * - When the batch carries images, returns an OpenAI content-part list
     *   alternating text segments and `image_url` parts. An image whose
     *   [ImageAttachment.placeholder] occurs in the (remaining) text replaces
     *   that occurrence in place — the placeholder itself is swapped for the
     *   `[IMAGE media_path=<path>]` marker text — while images without a hit
     *   are appended after the text.
     *
     * @param batch The batch to render.
     * @return Content parts suitable for an [com.l2dchat.core.llm.LlmMessage].
     * @throws ConfigError On an unreadable image file or one larger than
     *   [MAX_IMAGE_BYTES].
     */
    fun buildUserContent(batch: IngestBatch): List<LlmContentPart> {
        if (batch.images.isEmpty()) {
            return listOf(LlmTextPart(batch.text))
        }

        val parts: MutableList<LlmContentPart> = ArrayList()
        var text: String = batch.text
        val unplaced: MutableList<ImageAttachment> = ArrayList()
        for (image in batch.images) {
            val placeholder = image.placeholder
            if (placeholder != null && placeholder.isNotEmpty() && text.contains(placeholder)) {
                val idx = text.indexOf(placeholder)
                val before = text.substring(0, idx)
                text = text.substring(idx + placeholder.length)
                pushText(parts, before)
                pushImage(parts, image)
            } else {
                unplaced.add(image)
            }
        }
        pushText(parts, text)
        for (image in unplaced) {
            pushImage(parts, image)
        }
        return parts
    }

    /**
     * Append one text part, skipping empty segments.
     *
     * Mirrors `_push_text` in `multimodal.py`.
     */
    private fun pushText(parts: MutableList<LlmContentPart>, text: String) {
        if (text.isNotEmpty()) {
            parts.add(LlmTextPart(text))
        }
    }

    /**
     * Append the `[IMAGE ...]` marker text part followed by the image part.
     *
     * Mirrors `_push_image` in `multimodal.py`.
     */
    private fun pushImage(parts: MutableList<LlmContentPart>, image: ImageAttachment) {
        pushText(parts, "[IMAGE media_path=${image.path}]")
        parts.add(LlmImageUrlPart(url = dataUrl(image.path)))
    }
    /**
     * Read one image file and return its `data:image/<mime>;base64` URL.
     *
     * The mime subtype comes from the file extension (unknown extensions fall
     * back to `png`), matching the Python `_data_url` helper.
     *
     * Exposed as the host-side media-injection entry point (T5): after an
     * `ltm_read_media` tool result returns a `media_path`, the host planner
     * reads the bytes via this helper and injects an `image_url` content block
     * into the next LLM request — without going through the extraction
     * pipeline. The [MAX_IMAGE_BYTES] guard applies identically.
     *
     * @param path The image file path.
     * @return The data URL.
     * @throws ConfigError If the file cannot be read or exceeds [MAX_IMAGE_BYTES].
     */
    fun dataUrl(path: String): String {
        val file = File(path)
        val size: Long =
            try {
                file.length()
            } catch (e: SecurityException) {
                throw ConfigError("image attachment not readable: '$path' ($e)")
            }
        if (size <= 0L) {
            throw ConfigError("image attachment not readable: '$path'")
        }
        if (size > MAX_IMAGE_BYTES) {
            throw ConfigError(
                "image attachment too large: '$path' is $size bytes (limit $MAX_IMAGE_BYTES bytes)"
            )
        }
        val raw: ByteArray =
            try {
                file.readBytes()
            } catch (e: IOException) {
                throw ConfigError("image attachment not readable: '$path' ($e)")
            } catch (e: SecurityException) {
                throw ConfigError("image attachment not readable: '$path' ($e)")
            }
        val mime = mimeForExtension(path)
        val encoded = Base64.getEncoder().encodeToString(raw)
        return "data:image/$mime;base64,$encoded"
    }

    /**
     * Infer the image mime subtype from the file extension.
     *
     * Unknown extensions fall back to `png`, matching `_MIME_BY_EXT.get(..., "png")`.
     */
    internal fun mimeForExtension(path: String): String {
        val dotIdx = path.lastIndexOf('.')
        if (dotIdx < 0) return "png"
        val ext = path.substring(dotIdx).lowercase()
        return MIME_BY_EXT[ext] ?: "png"
    }
}
