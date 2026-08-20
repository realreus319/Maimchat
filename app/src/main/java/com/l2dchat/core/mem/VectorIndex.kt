package com.l2dchat.core.mem

import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.math.sqrt

/**
 * In-memory brute-force cosine top-k index over dense float vectors.
 *
 * Mirrors `MemStore.vec_topk` from the Python reference
 * (`/home/tcmofashi/proj/t_memorix/src/mem/store.py`, `vec_topk` around L458-501).
 *
 * The index is loaded fully into memory at [MemStore] startup and mutated
 * incrementally as vectors are put or removed. Lookups are O(n·d) brute-force
 * cosine — no approximation, no external dependency. Pure Kotlin, zero Android
 * dependencies, so it is unit-testable on the JVM.
 *
 * Dimension contract:
 * - If [expectedDim] is supplied (or inferred from the first [put]/[loadFromBlob]),
 *   every subsequent vector and every query must match it or [DimMismatchError]
 *   is thrown. This mirrors the Python `self._dim` guard.
 *
 * Zero-norm handling:
 * - A zero-norm stored vector scores `0.0` against any query (its dot product
 *   is zero, and we avoid divide-by-zero by short-circuiting).
 * - A zero-norm query scores `0.0` against every stored vector, matching the
 *   Python branch `if query_norm == 0.0: scores = np.zeros(...)`.
 *
 * Tie-break:
 * - Results are sorted by score descending, then by `hashId` ascending for
 *   determinism, exactly as the Python key `lambda i: (-float(scores[i]), hashes[i])`.
 *
 * Concurrency:
 * - Thread-safe. All public methods that read or mutate index state are
 *   `@Synchronized` on the index instance. [MemStore] calls into this index
 *   from multiple coroutines (load on Dispatchers.IO, apply() index-sync,
 *   deleteNode, ltm_search vecTopK readers), so the lock serializes those
 *   accesses. Contention is low (in-memory hot path, small critical sections).
 */
class VectorIndex(expectedDim: Int? = null) {

    init {
        if (expectedDim != null) {
            require(expectedDim > 0) {
                "VectorIndex: expectedDim must be positive, got $expectedDim"
            }
        }
    }

    private var dim: Int? = expectedDim
    private val vectors: HashMap<String, FloatArray> = HashMap()

    /** Configured vector dimension, or null until the first vector is inserted. */
    @Synchronized
    fun dimension(): Int? = dim

    /** Number of stored vectors. */
    @Synchronized
    fun size(): Int = vectors.size

    /**
     * Inserts (or replaces) the vector for [hashId].
     *
     * The first insertion when [dimension] is null fixes the dimension; later
     * insertions must match it. Throws [DimMismatchError] on violation.
     */
    @Synchronized
    fun put(hashId: String, vector: FloatArray) {
        require(hashId.isNotEmpty()) { "VectorIndex.put: hashId must be non-empty" }
        val incoming = vector.size
        val current = dim
        if (current == null) {
            require(incoming > 0) {
                "VectorIndex.put: vector dimension must be positive, got $incoming"
            }
            dim = incoming
        } else if (incoming != current) {
            throw DimMismatchError(
                "VectorIndex.put: dim mismatch: expected $current, got $incoming for hashId '$hashId'"
            )
        }
        // Defensive copy so external mutation cannot corrupt the index.
        vectors[hashId] = vector.copyOf()
    }

    /** Removes the vector for [hashId]; no-op if absent. Returns true if removed. */
    @Synchronized
    fun remove(hashId: String): Boolean {
        return vectors.remove(hashId) != null
    }

    /**
     * Loads a vector from a little-endian float blob (the on-disk format used
     * by the `mem_vectors.embedding` column).
     *
     * [dim] is the declared dimension stored alongside the blob; it must equal
     * `bytes.size / 4` or [DimMismatchError] is thrown. The blob must be empty
     * of trailing bytes.
     *
     * This is the raw-parameter equivalent of `loadFromEntities` — callers that
     * already have [MemVectorEntity] rows should unpack each row into
     * `(hashId, dim, bytes)` and call this per row.
     */
    @Synchronized
    fun loadFromBlob(hashId: String, dim: Int, bytes: ByteArray) {
        require(hashId.isNotEmpty()) { "VectorIndex.loadFromBlob: hashId must be non-empty" }
        require(dim > 0) { "VectorIndex.loadFromBlob: dim must be positive, got $dim" }
        if (bytes.size % Float.SIZE_BYTES != 0) {
            throw DimMismatchError(
                "VectorIndex.loadFromBlob: blob length ${bytes.size} is not a multiple of " +
                    "${Float.SIZE_BYTES} bytes for hashId '$hashId'"
            )
        }
        val declared = bytes.size / Float.SIZE_BYTES
        if (declared != dim) {
            throw DimMismatchError(
                "VectorIndex.loadFromBlob: dim mismatch: declared $dim, but blob holds " +
                    "$declared floats for hashId '$hashId'"
            )
        }
        val floats = floatsFromBytes(bytes)
        // floats.size == dim is guaranteed by the checks above; put() will also
        // reconcile against any pre-existing configured dimension.
        put(hashId, floats)
    }

    /**
     * Brute-force cosine top-k.
     *
     * @param query Query embedding. Must match [dimension] if it is set.
     * @param k Maximum results. Non-positive returns an empty list, matching
     *   the Python `if k <= 0: return []` guard.
     * @param candidates Optional pre-filter; only these hashIds compete. Hashes
     *   not present in the index are silently ignored, matching the Python
     *   `[h for h in candidate_hashes if h in self._vec_index]`.
     * @return `(hashId, cosineScore)` pairs sorted by score descending, ties
     *   broken by hashId ascending. Zero-norm vectors score `0.0`.
     */
    @Synchronized
    fun topK(
        query: FloatArray,
        k: Int,
        candidates: Set<String>? = null,
    ): List<Pair<String, Double>> {
        val current = dim
        if (current != null && query.size != current) {
            throw DimMismatchError(
                "VectorIndex.topK: dim mismatch: expected $current, got ${query.size}"
            )
        }
        if (k <= 0) return emptyList()

        val hashes: List<String> = if (candidates == null) {
            ArrayList(vectors.keys)
        } else {
            ArrayList<String>(candidates.size).apply {
                for (h in candidates) {
                    if (h in vectors) add(h)
                }
            }
        }
        if (hashes.isEmpty()) return emptyList()

        val queryNorm = doubleNorm(query)
        // Python: if query_norm == 0.0: scores = np.zeros(len(hashes))
        if (queryNorm == 0.0) {
            // Every score is 0.0; still apply tie-break by hashId ascending.
            val ordered = hashes.sorted()
            return ordered.take(k).map { it to 0.0 }
        }

        // Score every candidate. Zero-norm stored vectors yield dot=0 → score 0.
        val scored = ArrayList<Pair<String, Double>>(hashes.size)
        for (h in hashes) {
            val v = vectors.getValue(h)
            val rowNorm = doubleNorm(v)
            val score = if (rowNorm == 0.0) {
                0.0
            } else {
                dot(query, v) / (queryNorm * rowNorm)
            }
            scored.add(h to score)
        }
        scored.sortWith(
            compareByDescending<Pair<String, Double>> { it.second }
                .thenBy { it.first }
        )
        return scored.take(k)
    }

    private fun dot(a: FloatArray, b: FloatArray): Double {
        var sum = 0.0
        for (i in a.indices) {
            sum += a[i].toDouble() * b[i].toDouble()
        }
        return sum
    }

    private fun doubleNorm(a: FloatArray): Double {
        var sum = 0.0
        for (v in a) {
            val d = v.toDouble()
            sum += d * d
        }
        return sqrt(sum)
    }
}

/**
 * Decodes a little-endian float blob into a [FloatArray].
 *
 * The `mem_vectors.embedding` column stores float32 values in little-endian
 * byte order; this helper performs the inverse of [floatsToBytes].
 */
internal fun floatsFromBytes(bytes: ByteArray): FloatArray {
    val buffer = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN)
    val out = FloatArray(bytes.size / Float.SIZE_BYTES)
    buffer.asFloatBuffer().get(out)
    return out
}

/**
 * Encodes a [FloatArray] into a little-endian byte blob, the on-disk format
 * for the `mem_vectors.embedding` column. Inverse of [floatsFromBytes].
 */
internal fun floatsToBytes(floats: FloatArray): ByteArray {
    val buffer = ByteBuffer.allocate(floats.size * Float.SIZE_BYTES)
        .order(ByteOrder.LITTLE_ENDIAN)
    buffer.asFloatBuffer().put(floats)
    return buffer.array()
}
