package com.l2dchat.core.mem

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.math.sqrt

/**
 * Unit tests for [VectorIndex], the in-memory brute-force cosine top-k index.
 *
 * Cross-references the Python `MemStore.vec_topk` in
 * `/home/tcmofashi/proj/t_memorix/src/mem/store.py` (around L458-501):
 * - dim validation on put and query,
 * - candidate pre-filtering,
 * - zero-norm vectors scoring 0.0,
 * - tie-break by hashId ascending for determinism.
 */
class VectorIndexTest {

    // ------------------------------------------------------------------
    // topK ordering on 5 known vectors
    // ------------------------------------------------------------------

    @Test
    fun `topK returns vectors sorted by cosine similarity descending`() {
        // Query along the x-axis; cosine values are exact and easy to verify.
        //   v1 = [1, 0]   -> 1.0
        //   v2 = [1, 1]   -> 1/sqrt(2) ~= 0.7071
        //   v3 = [0, 1]   -> 0.0   (orthogonal)
        //   v4 = [-1, 0]  -> -1.0  (opposite)
        //   v5 = [0, 0]   -> 0.0   (zero-norm, must score 0.0)
        val index = VectorIndex()
        index.put("v1", floatArrayOf(1f, 0f))
        index.put("v2", floatArrayOf(1f, 1f))
        index.put("v3", floatArrayOf(0f, 1f))
        index.put("v4", floatArrayOf(-1f, 0f))
        index.put("v5", floatArrayOf(0f, 0f))

        val query = floatArrayOf(1f, 0f)
        val results = index.topK(query, k = 5)

        assertEquals(5, results.size)
        assertEquals("v1", results[0].first)
        assertEquals(1.0, results[0].second, 1e-9)
        assertEquals("v2", results[1].first)
        assertEquals(1.0 / sqrt(2.0), results[1].second, 1e-6)
        // v3 and v5 both score 0.0; tie-break by hashId ascending → v3 before v5.
        assertEquals("v3", results[2].first)
        assertEquals(0.0, results[2].second, 0.0)
        assertEquals("v5", results[3].first)
        assertEquals(0.0, results[3].second, 0.0)
        assertEquals("v4", results[4].first)
        assertEquals(-1.0, results[4].second, 1e-9)
    }

    @Test
    fun `topK respects k limit and returns only top results`() {
        val index = VectorIndex()
        index.put("v1", floatArrayOf(1f, 0f))
        index.put("v2", floatArrayOf(1f, 1f))
        index.put("v3", floatArrayOf(0f, 1f))

        val results = index.topK(floatArrayOf(1f, 0f), k = 2)
        assertEquals(2, results.size)
        assertEquals("v1", results[0].first)
        assertEquals("v2", results[1].first)
    }

    @Test
    fun `topK with k larger than index size returns all vectors`() {
        val index = VectorIndex()
        index.put("only", floatArrayOf(1f, 0f))
        val results = index.topK(floatArrayOf(1f, 0f), k = 100)
        assertEquals(1, results.size)
        assertEquals("only", results[0].first)
    }

    @Test
    fun `topK with non-positive k returns empty list`() {
        val index = VectorIndex()
        index.put("v1", floatArrayOf(1f, 0f))
        assertTrue(index.topK(floatArrayOf(1f, 0f), k = 0).isEmpty())
        assertTrue(index.topK(floatArrayOf(1f, 0f), k = -1).isEmpty())
    }

    // ------------------------------------------------------------------
    // dim mismatch
    // ------------------------------------------------------------------

    @Test
    fun `put throws DimMismatchError when vector dim differs from configured dim`() {
        val index = VectorIndex(expectedDim = 4)
        try {
            index.put("bad", floatArrayOf(1f, 2f, 3f))
            fail("Expected DimMismatchError for 3-dim vector in 4-dim index")
        } catch (e: DimMismatchError) {
            assertTrue(
                "Message must mention expected and actual dims: ${e.message}",
                e.message?.contains("4") == true && e.message?.contains("3") == true,
            )
        }
    }

    @Test
    fun `put fixes dimension on first insert when expectedDim is null`() {
        val index = VectorIndex()
        assertNull(index.dimension())
        index.put("first", floatArrayOf(1f, 2f, 3f))
        assertEquals(3, index.dimension())
        // Subsequent put with a different dim must fail.
        try {
            index.put("bad", floatArrayOf(1f, 2f))
            fail("Expected DimMismatchError after dim was fixed by first put")
        } catch (e: DimMismatchError) {
            assertTrue(e.message?.contains("3") == true && e.message?.contains("2") == true)
        }
    }

    @Test
    fun `topK throws DimMismatchError when query dim differs from index dim`() {
        val index = VectorIndex(expectedDim = 4)
        index.put("v1", floatArrayOf(1f, 0f, 0f, 0f))
        try {
            index.topK(floatArrayOf(1f, 0f, 0f), k = 1)
            fail("Expected DimMismatchError for 3-dim query on 4-dim index")
        } catch (e: DimMismatchError) {
            assertTrue(
                "Message must mention expected and actual dims: ${e.message}",
                e.message?.contains("4") == true && e.message?.contains("3") == true,
            )
        }
    }

    @Test
    fun `topK throws DimMismatchError when query dim differs from inferred dim`() {
        val index = VectorIndex()
        index.put("v1", floatArrayOf(1f, 2f, 3f)) // fixes dim=3
        try {
            index.topK(floatArrayOf(1f, 2f), k = 1)
            fail("Expected DimMismatchError for 2-dim query on inferred 3-dim index")
        } catch (e: DimMismatchError) {
            assertTrue(e.message?.contains("3") == true && e.message?.contains("2") == true)
        }
    }

    // ------------------------------------------------------------------
    // candidates filtering
    // ------------------------------------------------------------------

    @Test
    fun `topK with candidates only considers the specified hashIds`() {
        val index = VectorIndex()
        index.put("v1", floatArrayOf(1f, 0f))
        index.put("v2", floatArrayOf(1f, 1f))
        index.put("v3", floatArrayOf(0f, 1f))

        val results = index.topK(
            floatArrayOf(1f, 0f),
            k = 5,
            candidates = setOf("v2", "v3"),
        )
        assertEquals(2, results.size)
        val returned = results.map { it.first }.toSet()
        assertEquals(setOf("v2", "v3"), returned)
        assertFalse("v1 must be excluded by candidates filter", "v1" in returned)
    }

    @Test
    fun `topK with candidates silently ignores hashes not in the index`() {
        val index = VectorIndex()
        index.put("present", floatArrayOf(1f, 0f))
        val results = index.topK(
            floatArrayOf(1f, 0f),
            k = 5,
            candidates = setOf("present", "absent-a", "absent-b"),
        )
        assertEquals(1, results.size)
        assertEquals("present", results[0].first)
    }

    @Test
    fun `topK with empty candidates returns empty list`() {
        val index = VectorIndex()
        index.put("v1", floatArrayOf(1f, 0f))
        val results = index.topK(floatArrayOf(1f, 0f), k = 5, candidates = emptySet())
        assertTrue(results.isEmpty())
    }

    @Test
    fun `topK with candidates all absent returns empty list`() {
        val index = VectorIndex()
        index.put("v1", floatArrayOf(1f, 0f))
        val results = index.topK(
            floatArrayOf(1f, 0f),
            k = 5,
            candidates = setOf("ghost-a", "ghost-b"),
        )
        assertTrue(results.isEmpty())
    }

    // ------------------------------------------------------------------
    // empty index
    // ------------------------------------------------------------------

    @Test
    fun `topK on empty index returns empty list`() {
        val index = VectorIndex()
        assertTrue(index.topK(floatArrayOf(1f, 0f), k = 5).isEmpty())
    }

    @Test
    fun `topK on empty index with candidates returns empty list`() {
        val index = VectorIndex()
        assertTrue(index.topK(floatArrayOf(1f, 0f), k = 5, candidates = setOf("a")).isEmpty())
    }

    @Test
    fun `dimension is null on a fresh index with no expectedDim`() {
        val index = VectorIndex()
        assertNull(index.dimension())
    }

    @Test
    fun `dimension reflects expectedDim when provided`() {
        val index = VectorIndex(expectedDim = 384)
        assertEquals(384, index.dimension())
    }

    // ------------------------------------------------------------------
    // zero-norm handling
    // ------------------------------------------------------------------

    @Test
    fun `zero-norm stored vector scores 0`() {
        val index = VectorIndex()
        index.put("zero", floatArrayOf(0f, 0f))
        index.put("unit", floatArrayOf(1f, 0f))
        val results = index.topK(floatArrayOf(1f, 0f), k = 5)
        // unit scores 1.0, zero scores 0.0
        assertEquals("unit", results[0].first)
        assertEquals(1.0, results[0].second, 1e-9)
        assertEquals("zero", results[1].first)
        assertEquals(0.0, results[1].second, 0.0)
    }

    @Test
    fun `zero-norm query scores 0 against every stored vector`() {
        val index = VectorIndex()
        index.put("v1", floatArrayOf(1f, 0f))
        index.put("v2", floatArrayOf(0f, 1f))
        index.put("v3", floatArrayOf(1f, 1f))
        val results = index.topK(floatArrayOf(0f, 0f), k = 5)
        assertEquals(3, results.size)
        for ((hashId, score) in results) {
            assertEquals(
                "Zero-norm query must score 0.0 against '$hashId'",
                0.0,
                score,
                0.0,
            )
        }
        // Tie-break by hashId ascending.
        assertEquals(listOf("v1", "v2", "v3"), results.map { it.first })
    }

    @Test
    fun `all zero-norm stored vectors score 0 and tie-break by hashId`() {
        val index = VectorIndex()
        index.put("z3", floatArrayOf(0f, 0f))
        index.put("z1", floatArrayOf(0f, 0f))
        index.put("z2", floatArrayOf(0f, 0f))
        val results = index.topK(floatArrayOf(1f, 0f), k = 5)
        assertEquals(listOf("z1", "z2", "z3"), results.map { it.first })
        for ((_, score) in results) {
            assertEquals(0.0, score, 0.0)
        }
    }

    // ------------------------------------------------------------------
    // tie-break determinism
    // ------------------------------------------------------------------

    @Test
    fun `equal scores are tie-broken by hashId ascending`() {
        // Two identical vectors → identical cosine scores; tie-break by hashId.
        val index = VectorIndex()
        index.put("charlie", floatArrayOf(1f, 1f))
        index.put("alpha", floatArrayOf(1f, 1f))
        index.put("bravo", floatArrayOf(1f, 1f))
        val results = index.topK(floatArrayOf(1f, 0f), k = 3)
        assertEquals(listOf("alpha", "bravo", "charlie"), results.map { it.first })
        val scores = results.map { it.second }
        // All three scores equal.
        assertEquals(scores[0], scores[1], 1e-12)
        assertEquals(scores[1], scores[2], 1e-12)
    }

    @Test
    fun `topK is deterministic across repeated calls`() {
        val index = VectorIndex()
        index.put("v1", floatArrayOf(1f, 0f))
        index.put("v2", floatArrayOf(1f, 1f))
        index.put("v3", floatArrayOf(0f, 1f))
        index.put("v4", floatArrayOf(0f, 0f))
        val first = index.topK(floatArrayOf(1f, 0f), k = 4)
        val second = index.topK(floatArrayOf(1f, 0f), k = 4)
        assertEquals(first.map { it.first }, second.map { it.first })
        for (i in first.indices) {
            assertEquals(first[i].second, second[i].second, 1e-12)
        }
    }

    // ------------------------------------------------------------------
    // put / remove semantics
    // ------------------------------------------------------------------

    @Test
    fun `put replaces existing vector for the same hashId`() {
        val index = VectorIndex()
        index.put("v1", floatArrayOf(1f, 0f))
        index.put("v1", floatArrayOf(0f, 1f)) // replace
        assertEquals(1, index.size())
        val results = index.topK(floatArrayOf(1f, 0f), k = 5)
        assertEquals(1, results.size)
        // After replacement, v1 is orthogonal to the query → score 0.0.
        assertEquals("v1", results[0].first)
        assertEquals(0.0, results[0].second, 0.0)
    }

    @Test
    fun `put does not mutate caller array after insertion`() {
        val index = VectorIndex()
        val caller = floatArrayOf(1f, 2f, 3f)
        index.put("v1", caller)
        // Mutate the caller array; the index must be unaffected.
        caller[0] = 999f
        val results = index.topK(floatArrayOf(1f, 0f, 0f), k = 1)
        assertEquals(1, results.size)
        // cosine([1,0,0], [1,2,3]) = 1 / sqrt(14)
        assertEquals(1.0 / sqrt(14.0), results[0].second, 1e-6)
    }

    @Test
    fun `remove deletes a vector and returns true`() {
        val index = VectorIndex()
        index.put("v1", floatArrayOf(1f, 0f))
        assertTrue(index.remove("v1"))
        assertEquals(0, index.size())
        assertTrue(index.topK(floatArrayOf(1f, 0f), k = 5).isEmpty())
    }

    @Test
    fun `remove on absent hashId returns false and is a no-op`() {
        val index = VectorIndex()
        index.put("v1", floatArrayOf(1f, 0f))
        assertFalse(index.remove("ghost"))
        assertEquals(1, index.size())
    }

    @Test
    fun `put with empty hashId throws IllegalArgumentException`() {
        val index = VectorIndex()
        try {
            index.put("", floatArrayOf(1f, 0f))
            fail("Expected IllegalArgumentException for empty hashId")
        } catch (e: IllegalArgumentException) {
            assertTrue(e.message?.contains("hashId") == true)
        }
    }

    @Test
    fun `VectorIndex with expectedDim zero throws IllegalArgumentException`() {
        try {
            VectorIndex(expectedDim = 0)
            fail("Expected IllegalArgumentException for expectedDim = 0")
        } catch (e: IllegalArgumentException) {
            assertTrue(e.message?.contains("expectedDim") == true)
        }
    }

    @Test
    fun `put with zero-length vector when dim is null throws IllegalArgumentException`() {
        val index = VectorIndex()
        try {
            index.put("bad", FloatArray(0))
            fail("Expected IllegalArgumentException for zero-length vector")
        } catch (e: IllegalArgumentException) {
            assertTrue(e.message?.contains("dimension") == true)
        }
    }

    // ------------------------------------------------------------------
    // loadFromBlob
    // ------------------------------------------------------------------

    @Test
    fun `loadFromBlob decodes little-endian floats and inserts into the index`() {
        val index = VectorIndex()
        val floats = floatArrayOf(1f, 0f, 0f, 1f)
        val bytes = floatsToBytes(floats)
        index.loadFromBlob("v1", dim = 4, bytes = bytes)
        assertEquals(4, index.dimension())
        val results = index.topK(floatArrayOf(1f, 0f, 0f, 0f), k = 1)
        assertEquals("v1", results[0].first)
        // cosine([1,0,0,0], [1,0,0,1]) = 1 / sqrt(2)
        assertEquals(1.0 / sqrt(2.0), results[0].second, 1e-6)
    }

    @Test
    fun `loadFromBlob throws DimMismatchError when declared dim does not match blob length`() {
        val index = VectorIndex()
        val bytes = floatsToBytes(floatArrayOf(1f, 2f, 3f, 4f)) // 4 floats
        try {
            index.loadFromBlob("v1", dim = 3, bytes = bytes)
            fail("Expected DimMismatchError for dim=3 on a 4-float blob")
        } catch (e: DimMismatchError) {
            assertTrue(e.message?.contains("3") == true && e.message?.contains("4") == true)
        }
    }

    @Test
    fun `loadFromBlob throws DimMismatchError when blob length is not a multiple of 4 bytes`() {
        val index = VectorIndex()
        val bytes = ByteArray(5) // not a multiple of Float.SIZE_BYTES
        try {
            index.loadFromBlob("v1", dim = 1, bytes = bytes)
            fail("Expected DimMismatchError for non-multiple-of-4 blob length")
        } catch (e: DimMismatchError) {
            assertTrue(e.message?.contains("5") == true)
        }
    }

    @Test
    fun `loadFromBlob throws DimMismatchError when blob dim conflicts with configured dim`() {
        val index = VectorIndex(expectedDim = 8)
        val bytes = floatsToBytes(floatArrayOf(1f, 2f, 3f, 4f)) // 4 floats
        try {
            index.loadFromBlob("v1", dim = 4, bytes = bytes)
            fail("Expected DimMismatchError: configured dim=8 but blob holds 4 floats")
        } catch (e: DimMismatchError) {
            assertTrue(e.message?.contains("8") == true && e.message?.contains("4") == true)
        }
    }

    // ------------------------------------------------------------------
    // ByteArray ↔ FloatArray little-endian round-trip
    // ------------------------------------------------------------------

    @Test
    fun `floatsToBytes then floatsFromBytes round-trips exactly`() {
        val original = floatArrayOf(1.0f, -1.0f, 0.5f, -0.25f, 123.456f, Float.MAX_VALUE, Float.MIN_VALUE)
        val bytes = floatsToBytes(original)
        val recovered = floatsFromBytes(bytes)
        assertEquals(original.size, recovered.size)
        for (i in original.indices) {
            assertEquals(
                "Mismatch at index $i",
                original[i],
                recovered[i],
                0.0f,
            )
        }
    }

    @Test
    fun `floatsToBytes produces little-endian byte order`() {
        // 1.0f in IEEE-754 is 0x3F800000.
        // Little-endian bytes: [0x00, 0x00, 0x80, 0x3F].
        val bytes = floatsToBytes(floatArrayOf(1.0f))
        assertEquals(4, bytes.size)
        assertEquals(0x00.toByte(), bytes[0])
        assertEquals(0x00.toByte(), bytes[1])
        assertEquals(0x80.toByte(), bytes[2])
        assertEquals(0x3F.toByte(), bytes[3])
    }

    @Test
    fun `floatsFromBytes on empty byte array returns empty float array`() {
        val recovered = floatsFromBytes(ByteArray(0))
        assertEquals(0, recovered.size)
    }

    @Test
    fun `floatsToBytes then floatsFromBytes round-trips an empty float array`() {
        val bytes = floatsToBytes(FloatArray(0))
        assertEquals(0, bytes.size)
        val recovered = floatsFromBytes(bytes)
        assertEquals(0, recovered.size)
    }

    @Test
    fun `floatsFromBytes matches ByteBuffer little-endian FloatBuffer directly`() {
        val original = floatArrayOf(0.1f, 0.2f, 0.3f, 0.4f, 0.5f)
        val bytes = floatsToBytes(original)
        // Independent decode path using ByteBuffer directly.
        val direct = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN)
            .asFloatBuffer()
        val expected = FloatArray(original.size)
        direct.get(expected)
        val recovered = floatsFromBytes(bytes)
        for (i in original.indices) {
            assertEquals(expected[i], recovered[i], 0.0f)
        }
    }

    // ------------------------------------------------------------------
    // performance smoke
    // ------------------------------------------------------------------

    @Test
    fun `topK on 10_000 vectors of 1024 dims completes under 2 seconds`() {
        // Aspirational target is < 50ms; the 2s ceiling is a generous CI guard
        // to avoid flakiness. Actual elapsed time is asserted and logged.
        val n = 10_000
        val d = 1024
        val random = java.util.Random(0xC0FFEEL)
        val index = VectorIndex(expectedDim = d)
        for (i in 0 until n) {
            val vec = FloatArray(d) { random.nextFloat() * 2f - 1f }
            index.put("v$i", vec)
        }
        assertEquals(n, index.size())

        val query = FloatArray(d) { random.nextFloat() * 2f - 1f }
        val start = System.nanoTime()
        val results = index.topK(query, k = 8)
        val elapsedNanos = System.nanoTime() - start
        val elapsedMillis = elapsedNanos / 1_000_000

        println(
            "VectorIndex perf smoke: topK(8) over $n x $d vectors took ${elapsedMillis}ms"
        )

        assertEquals(8, results.size)
        // Scores must be non-increasing (sorted descending).
        for (i in 1 until results.size) {
            assertTrue(
                "Scores must be non-increasing: ${results[i - 1].second} < ${results[i].second}",
                results[i - 1].second >= results[i].second,
            )
        }
        // Generous ceiling to avoid CI flakiness; the 50ms target is aspirational.
        assertTrue(
            "topK over $n x $d vectors took ${elapsedMillis}ms, expected < 2000ms",
            elapsedMillis < 2_000,
        )
    }

    // ------------------------------------------------------------------
    // misc: defensive coverage
    // ------------------------------------------------------------------

    @Test
    fun `size reports the number of stored vectors`() {
        val index = VectorIndex()
        assertEquals(0, index.size())
        index.put("a", floatArrayOf(1f, 0f))
        assertEquals(1, index.size())
        index.put("b", floatArrayOf(0f, 1f))
        assertEquals(2, index.size())
        index.put("a", floatArrayOf(1f, 1f)) // replace, no size change
        assertEquals(2, index.size())
        index.remove("b")
        assertEquals(1, index.size())
    }

    @Test
    fun `topK result is a fresh list and does not leak internal state`() {
        val index = VectorIndex()
        index.put("v1", floatArrayOf(1f, 0f))
        val results = index.topK(floatArrayOf(1f, 0f), k = 5)
        assertEquals(1, results.size)
        // Mutating the returned list must not affect subsequent queries.
        mutableListOf(*results.toTypedArray()).clear()
        val again = index.topK(floatArrayOf(1f, 0f), k = 5)
        assertEquals(1, again.size)
        assertEquals("v1", again[0].first)
    }
}
