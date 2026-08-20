package com.l2dchat.core.mem

import java.security.MessageDigest
import java.util.Base64

/**
 * Content hashing utilities for the mem library.
 *
 * Mirrors `mem/hashing.py` from the Python reference. Hashes are byte-identical
 * with the Python implementation (SHA-256 digest, URL-safe Base64 without
 * padding) so that content addressing is stable across the port.
 */
object Hashing {
    private const val ALGORITHM = "SHA-256"

    /**
     * Compute a short, URL-safe content hash of [text].
     *
     * The digest is SHA-256, encoded as URL-safe Base64 without padding, then
     * truncated to [length] characters. The same input always yields the same
     * output (content addressing).
     *
     * @param text The string to hash.
     * @param length Number of characters to keep. Must be non-negative.
     * @return A URL-safe Base64 digest prefix of [length] characters.
     * @throws IllegalArgumentException if [length] is negative.
     */
    fun shortHash(
        text: String,
        length: Int = 12,
    ): String {
        require(length >= 0) { "shortHash: length must be non-negative, got $length" }
        val digest = MessageDigest.getInstance(ALGORITHM).digest(text.toByteArray(Charsets.UTF_8))
        return Base64.getUrlEncoder().withoutPadding().encodeToString(digest).take(length)
    }
}
