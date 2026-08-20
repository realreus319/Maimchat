package com.l2dchat.core.mem

import com.l2dchat.core.llm.LlmImageUrlPart
import com.l2dchat.core.llm.LlmTextPart
import java.nio.file.Files
import java.nio.file.Path
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test

/**
 * Tests for [Multimodal.buildUserContent] — Kotlin port of the behavior in
 * `mem/multimodal.py`.
 *
 * Covers: no-images passthrough, placeholder in-place insertion, append-when-
 * no-placeholder, marker format, mime inference by extension, and the 10MB
 * enforcement. Image bytes are written to temp files so the file-reading path
 * is exercised for real.
 */
class MultimodalTest {

    private lateinit var tmpDir: Path

    @Before
    fun setUp() {
        tmpDir = Files.createTempDirectory("multimodal-test")
    }

    @After
    fun tearDown() {
        tmpDir.toFile().deleteRecursively()
    }

    @Test
    fun `no images returns a single text part with the batch text`() {
        val batch = batch(text = "hello world", images = emptyList())
        val parts = Multimodal.buildUserContent(batch)
        assertEquals(1, parts.size)
        val text = parts[0] as LlmTextPart
        assertEquals("hello world", text.text)
    }

    @Test
    fun `image with placeholder is inserted in place, replacing the placeholder`() {
        val img = writeImage("photo.png", byteArrayOf(1, 2, 3, 4))
        val batch =
            batch(
                text = "before[图片]after",
                images = listOf(ImageAttachment(path = img.toString(), placeholder = "[图片]")),
            )
        val parts = Multimodal.buildUserContent(batch)
        // Expected: text("before"), text("[IMAGE ...]"), image_url, text("after")
        assertEquals(4, parts.size)
        assertEquals("before", (parts[0] as LlmTextPart).text)
        val marker = (parts[1] as LlmTextPart).text
        assertEquals("[IMAGE media_path=$img]", marker)
        assertTrue(parts[2] is LlmImageUrlPart)
        assertEquals("after", (parts[3] as LlmTextPart).text)
    }

    @Test
    fun `image without placeholder hit is appended after the text`() {
        val img = writeImage("photo.jpg", byteArrayOf(1, 2, 3))
        val batch =
            batch(
                text = "no placeholder here",
                images = listOf(ImageAttachment(path = img.toString(), placeholder = "[图片]")),
            )
        val parts = Multimodal.buildUserContent(batch)
        // Expected: text("no placeholder here"), text("[IMAGE ...]"), image_url
        assertEquals(3, parts.size)
        assertEquals("no placeholder here", (parts[0] as LlmTextPart).text)
        assertEquals("[IMAGE media_path=$img]", (parts[1] as LlmTextPart).text)
        assertTrue(parts[2] is LlmImageUrlPart)
    }

    @Test
    fun `image with null placeholder is appended after the text`() {
        val img = writeImage("photo.webp", byteArrayOf(9, 8, 7))
        val batch =
            batch(
                text = "some text",
                images = listOf(ImageAttachment(path = img.toString(), placeholder = null)),
            )
        val parts = Multimodal.buildUserContent(batch)
        assertEquals(3, parts.size)
        assertEquals("some text", (parts[0] as LlmTextPart).text)
        assertTrue(parts[2] is LlmImageUrlPart)
    }

    @Test
    fun `multiple images each consume their own placeholder occurrence`() {
        val img1 = writeImage("a.png", byteArrayOf(1))
        val img2 = writeImage("b.png", byteArrayOf(2))
        val batch =
            batch(
                text = "head[图1]mid[图2]tail",
                images =
                    listOf(
                        ImageAttachment(path = img1.toString(), placeholder = "[图1]"),
                        ImageAttachment(path = img2.toString(), placeholder = "[图2]"),
                    ),
            )
        val parts = Multimodal.buildUserContent(batch)
        // head, marker1, img1, mid, marker2, img2, tail
        assertEquals(7, parts.size)
        assertEquals("head", (parts[0] as LlmTextPart).text)
        assertEquals("[IMAGE media_path=$img1]", (parts[1] as LlmTextPart).text)
        assertEquals("mid", (parts[3] as LlmTextPart).text)
        assertEquals("[IMAGE media_path=$img2]", (parts[4] as LlmTextPart).text)
        assertEquals("tail", (parts[6] as LlmTextPart).text)
    }

    @Test
    fun `image_url part carries a data URL with the inferred mime subtype`() {
        val img = writeImage("photo.jpeg", byteArrayOf(0xFF.toByte(), 0xD8.toByte(), 0xFF.toByte()))
        val batch =
            batch(
                text = "x",
                images = listOf(ImageAttachment(path = img.toString(), placeholder = null)),
            )
        val parts = Multimodal.buildUserContent(batch)
        val urlPart = parts[2] as LlmImageUrlPart
        assertTrue("url should start with data:image/jpeg;base64,", urlPart.url.startsWith("data:image/jpeg;base64,"))
    }

    @Test
    fun `mime inference maps extensions per the Python table`() {
        assertEquals("png", Multimodal.mimeForExtension("a.png"))
        assertEquals("jpeg", Multimodal.mimeForExtension("a.jpg"))
        assertEquals("jpeg", Multimodal.mimeForExtension("a.jpeg"))
        assertEquals("webp", Multimodal.mimeForExtension("a.webp"))
        assertEquals("gif", Multimodal.mimeForExtension("a.gif"))
        // Unknown extension → png fallback.
        assertEquals("png", Multimodal.mimeForExtension("a.bmp"))
        assertEquals("png", Multimodal.mimeForExtension("noext"))
    }

    @Test
    fun `mime inference is case-insensitive on the extension`() {
        assertEquals("png", Multimodal.mimeForExtension("A.PNG"))
        assertEquals("jpeg", Multimodal.mimeForExtension("A.JPEG"))
    }

    @Test
    fun `image larger than MAX_IMAGE_BYTES raises ConfigError`() {
        // Write a file just over the limit (sparse is fine — we only need
        // Files.size to report the length; the content never needs to be read
        // because the size check fires first).
        val tooBig = tmpDir.resolve("huge.png")
        Files.write(tooBig, ByteArray(0))
        // Pad to MAX_IMAGE_BYTES + 1 using a sparse-ish approach: write a small
        // payload then truncate the file to the target length.
        val targetSize = Multimodal.MAX_IMAGE_BYTES + 1
        val raf = java.io.RandomAccessFile(tooBig.toFile(), "rw")
        raf.setLength(targetSize)
        raf.close()
        assertEquals(targetSize, Files.size(tooBig))

        val batch =
            batch(
                text = "x",
                images = listOf(ImageAttachment(path = tooBig.toString(), placeholder = null)),
            )
        try {
            Multimodal.buildUserContent(batch)
            fail("expected ConfigError for oversized image")
        } catch (e: ConfigError) {
            assertTrue(e.message!!.contains("too large"))
        }
    }

    @Test
    fun `unreadable image file raises ConfigError`() {
        val batch =
            batch(
                text = "x",
                images = listOf(ImageAttachment(path = tmpDir.resolve("does-not-exist.png").toString(), placeholder = null)),
            )
        try {
            Multimodal.buildUserContent(batch)
            fail("expected ConfigError for missing image")
        } catch (e: ConfigError) {
            assertTrue(e.message!!.contains("not readable"))
        }
    }

    @Test
    fun `image at exactly MAX_IMAGE_BYTES is accepted`() {
        val exact = tmpDir.resolve("exact.png")
        val raf = java.io.RandomAccessFile(exact.toFile(), "rw")
        raf.setLength(Multimodal.MAX_IMAGE_BYTES)
        raf.close()
        val batch =
            batch(
                text = "x",
                images = listOf(ImageAttachment(path = exact.toString(), placeholder = null)),
            )
        val parts = Multimodal.buildUserContent(batch)
        assertTrue(parts[2] is LlmImageUrlPart)
    }

    @Test
    fun `empty text segment between placeholder and image is not emitted as a part`() {
        val img = writeImage("photo.png", byteArrayOf(1))
        // Placeholder at the very start → before-segment is empty.
        val batch =
            batch(
                text = "[图]tail",
                images = listOf(ImageAttachment(path = img.toString(), placeholder = "[图]")),
            )
        val parts = Multimodal.buildUserContent(batch)
        // marker, image, tail — no leading empty text part.
        assertEquals(3, parts.size)
        assertEquals("[IMAGE media_path=$img]", (parts[0] as LlmTextPart).text)
        assertTrue(parts[1] is LlmImageUrlPart)
        assertEquals("tail", (parts[2] as LlmTextPart).text)
    }

    private fun batch(text: String, images: List<ImageAttachment>): IngestBatch =
        IngestBatch(
            batchId = "test-batch",
            source = BatchSource.MANUAL,
            text = text,
            timeAnchor = 1_700_000_000L,
            mentionTime = 1_700_000_000L,
            images = images,
        )

    private fun writeImage(name: String, bytes: ByteArray): Path {
        val p = tmpDir.resolve(name)
        Files.write(p, bytes)
        return p
    }
}
