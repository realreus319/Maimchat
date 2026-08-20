package com.l2dchat.core.mem

import java.text.Normalizer
import kotlin.math.ln

/**
 * Lexical (BM25) retrieval over short texts — the string-match half of the
 * hybrid entity recall. Mirrors `mem/lexical.py` from the Python reference.
 *
 * Why char n-grams instead of a word segmenter: entity names are short, mostly
 * CJK, and the failure mode this module exists for is *surface variants* —
 * abbreviations ("罗联" ⊂ "罗联政府"), punctuation variants, full/half-width
 * drift. CJK unigram+bigram tokenization catches containment for free, needs
 * no jieba-style dependency, and is language-agnostic. Latin/digit runs stay
 * whole-word tokens ("kfc", "qwen3").
 *
 * Everything is deterministic and dependency-free; the index lives in memory
 * next to the vector index and is rebuilt from the database on open.
 *
 * Thread-safety: all public methods that read or mutate index state are
 * `@Synchronized` on the index instance. [MemStore] reaches this index from
 * multiple coroutines (load, apply() index-sync, deleteNode, entityLexTopK
 * readers); the lock serializes them. Contention is low.
 */

/**
 * True if [ch] is a CJK ideograph in the ranges covered by the Python
 * reference (`U+4E00..U+9FFF`, `U+3400..U+4DBF`, `U+F900..U+FAFF`).
 *
 * Mirrors `mem/lexical._is_cjk`.
 */
fun isCjk(ch: Char): Boolean {
    val cp = ch.code
    return cp in 0x4E00..0x9FFF || cp in 0x3400..0x4DBF || cp in 0xF900..0xFAFF
}

/**
 * Tokenize [text] into CJK uni/bigrams and latin/digit word tokens.
 *
 * NFKC normalization first: folds full-width variants ("ＫＦＣ" → "KFC") and
 * compatibility characters, then everything lowercases. Consecutive CJK
 * characters emit their unigrams plus the sliding bigrams; other alphanumeric
 * runs emit one whole token. All other characters are separators.
 *
 * The emission order matches the Python reference exactly: as each CJK
 * character is scanned its unigram is appended first, then the bigram with
 * the immediately preceding CJK character (if any). Callers must not assume
 * all unigrams precede all bigrams.
 *
 * Mirrors `mem/lexical.tokenize`. Output is byte-for-byte equivalent to the
 * Python implementation for any input where JDK `Normalizer.Form.NFKC` agrees
 * with `unicodedata.normalize("NFKC")` (the Unicode normalization algorithm
 * is versioned identically for both).
 */
fun tokenize(text: String): List<String> {
    val normalized = Normalizer.normalize(text, Normalizer.Form.NFKC).lowercase()
    val tokens = mutableListOf<String>()
    val word = StringBuilder()
    var prevCjk: Char? = null

    fun flushWord() {
        if (word.isNotEmpty()) {
            tokens.add(word.toString())
            word.setLength(0)
        }
    }

    for (ch in normalized) {
        if (isCjk(ch)) {
            flushWord()
            tokens.add(ch.toString())
            prevCjk?.let { prev -> tokens.add(prev.toString() + ch.toString()) }
            prevCjk = ch
        } else if (ch.isLetterOrDigit()) {
            // Python uses str.isalnum() which is True for Unicode letters and
            // digits; Char.isLetterOrDigit() matches that coverage for the
            // post-NFKC, post-lowercase input we operate on.
            prevCjk = null
            word.append(ch)
        } else {
            flushWord()
            prevCjk = null
        }
    }
    flushWord()
    return tokens
}

/**
 * A minimal in-memory Okapi-BM25 index over short documents.
 *
 * Documents are addressed by an external id (the node hash). Re-adding an
 * existing id replaces its posting; [discard] removes it. Parameters follow
 * the usual defaults (k1=1.5, b=0.75); with document lengths in the tens of
 * tokens the length normalization matters little but costs nothing.
 *
 * Mirrors `mem/lexical.BM25Index`.
 *
 * @param k1 Okapi BM25 term-frequency saturation parameter.
 * @param b Okapi BM25 length-normalization parameter.
 */
class BM25Index(
    private val k1: Double = 1.5,
    private val b: Double = 0.75,
) {
    private val docs: MutableMap<String, MutableMap<String, Int>> = LinkedHashMap()
    private val df: MutableMap<String, Int> = mutableMapOf()
    private val docLen: MutableMap<String, Int> = mutableMapOf()
    private var totalLen: Int = 0

    /** Number of indexed documents. Mirrors `BM25Index.__len__`. */
    @Synchronized
    fun size(): Int = docs.size

    /**
     * Add or replace a document under [docId]. Re-adding an existing id first
     * discards the old posting so term frequencies and document lengths are
     * fully replaced. Documents that tokenize to nothing (empty or
     * separator-only text) are not stored.
     *
     * Mirrors `BM25Index.add`.
     */
    @Synchronized
    fun add(docId: String, text: String) {
        discard(docId)
        val terms = countTerms(tokenize(text))
        if (terms.isEmpty()) return
        docs[docId] = terms
        val len = terms.values.sum()
        docLen[docId] = len
        totalLen += len
        for (term in terms.keys) {
            df.merge(term, 1) { a, c -> a + c }
        }
    }

    /**
     * Remove a document if present. No-op when [docId] is unknown.
     *
     * Mirrors `BM25Index.discard`.
     */
    @Synchronized
    fun discard(docId: String) {
        val terms = docs.remove(docId) ?: return
        totalLen -= docLen.remove(docId) ?: 0
        for (term in terms.keys) {
            val newDf = (df[term] ?: 0) - 1
            if (newDf <= 0) {
                df.remove(term)
            } else {
                df[term] = newDf
            }
        }
    }

    /**
     * Return `(docId, score)` pairs with score > 0, best first.
     *
     * Ties break on doc id ascending for determinism. An empty query, empty
     * index, or non-positive [k] yields no results.
     *
     * Mirrors `BM25Index.search`.
     */
    @Synchronized
    fun search(query: String, k: Int): List<Pair<String, Double>> {
        val queryTerms = tokenize(query)
        if (queryTerms.isEmpty() || docs.isEmpty() || k <= 0) return emptyList()
        val nDocs = docs.size
        val avgdl = if (nDocs > 0) totalLen.toDouble() / nDocs else 0.0
        val scores = mutableMapOf<String, Double>()
        // Dedupe query terms preserving first-seen order, matching
        // dict.fromkeys(query_terms) in the Python reference.
        val seen = LinkedHashSet<String>()
        for (term in queryTerms) seen.add(term)
        for (term in seen) {
            val df = df[term] ?: 0
            if (df == 0) continue
            val idf = ln(1.0 + (nDocs - df + 0.5) / (df + 0.5))
            for ((docId, terms) in docs) {
                val tf = terms[term] ?: 0
                if (tf == 0) continue
                val dl = docLen[docId] ?: 0
                val norm = 1.0 - b + b * (if (avgdl > 0.0) dl / avgdl else 0.0)
                val delta = idf * (tf * (k1 + 1.0)) / (tf + k1 * norm)
                scores.merge(docId, delta) { a, c -> a + c }
            }
        }
        return scores.entries
            .sortedWith(
                compareByDescending<Map.Entry<String, Double>> { it.value }
                    .thenBy { it.key }
            )
            .take(k)
            .map { it.key to it.value }
    }

    private fun countTerms(tokens: List<String>): MutableMap<String, Int> {
        val counts: MutableMap<String, Int> = LinkedHashMap()
        for (t in tokens) counts.merge(t, 1) { a, c -> a + c }
        return counts
    }
}
