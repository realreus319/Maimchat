package com.l2dchat.core.mem

/**
 * Unified node model for the mem library — Kotlin port of `mem/node.py`.
 *
 * Every memory item — an event, an entity registry card, a life capture — is a
 * single [Node] row in the `nodes` table. [baseType] splits the world into
 * `"content"` (things that happened or were captured) and `"entity"` (registry
 * cards for people/places/objects/concepts); [nodeType] is the declarative
 * extension point managed by [NodeTypeRegistry].
 *
 * ## `Long` vs Python `float`
 *
 * The Python reference stores epoch seconds as `float`. This port uses [Long]
 * throughout (per the mem-kotlin-integration plan) so the whole chain stays in
 * integer epoch seconds — no `Double`/`Float` mixing.
 *
 * @see BASE_TYPES
 */
data class Node(
    /** Content-addressed 12-char id (see [Hashing.shortHash]). */
    val hashId: String,
    /** `"content"` or `"entity"` — nothing else exists. */
    val baseType: String,
    /** Declarative type, e.g. `"event"` | `"entity"` | `"life_capture"`. */
    val nodeType: String,
    /** The only text that gets embedded (event: summary; entity: `"name: description"`). */
    val content: String,
    /** "Appearing when" — epoch seconds, filled by the orchestrator, never by the model. */
    val mentionTime: Long,
    /** Original "event when" expression, e.g. `"下周六"`. */
    val eventTimeRaw: String? = null,
    /** Normalized time expression (ISO 8601 + EDTF subset, see [TimeParse]). */
    val eventTime: String? = null,
    /** Derived half-open interval start (epoch seconds), or `null`. */
    val startTs: Long? = null,
    /** Derived half-open interval end (epoch seconds), or `null`. */
    val endTs: Long? = null,
    /** Optional model-assigned importance score. */
    val importance: Double? = null,
    /** Optional model-assigned sentiment score. */
    val sentiment: Double? = null,
    /** Declarative per-type extension fields. */
    val metadata: Map<String, Any?> = emptyMap(),
    /** Hash of the node that replaces this one, or `null`. */
    val supersededBy: String? = null,
    /** How often the node was expanded. */
    val accessCount: Int = 0,
    /** Epoch seconds of the last expand, or `null` (reserved for §5). */
    val lastAccessed: Long? = null,
    /** Creation epoch seconds. */
    val createdAt: Long = 0L,
    /** Last modification epoch seconds. */
    val updatedAt: Long = 0L,
) {
    init {
        require(hashId.isNotEmpty()) { "Node: hash_id must be non-empty" }
        require(baseType in BASE_TYPES) {
            "Node: base_type must be one of ${BASE_TYPES.toList()}, got '$baseType'"
        }
    }

    /**
     * Serialize to the `nodes` table row shape (DB column names).
     *
     * [metadata] becomes the `metadata_json` TEXT column. The JSON is encoded with
     * sorted keys (matching Python `json.dumps(..., ensure_ascii=False, sort_keys=True)`)
     * so identical metadata serializes byte-identically.
     *
     * @return A map keyed by the `nodes` column names, suitable for sqlite named-parameter
     *   binding.
     */
    fun toRow(): Map<String, Any?> =
        mapOf(
            "hash_id" to hashId,
            "base_type" to baseType,
            "node_type" to nodeType,
            "content" to content,
            "mention_time" to mentionTime,
            "event_time_raw" to eventTimeRaw,
            "event_time" to eventTime,
            "start_ts" to startTs,
            "end_ts" to endTs,
            "importance" to importance,
            "sentiment" to sentiment,
            "metadata_json" to metadataToJson(metadata),
            "superseded_by" to supersededBy,
            "access_count" to accessCount,
            "last_accessed" to lastAccessed,
            "created_at" to createdAt,
            "updated_at" to updatedAt,
        )

    companion object {
        /**
         * Build a node from a `nodes` table row.
         *
         * Accepts a mapping with the `nodes` column names. `metadata_json` is parsed
         * back into the [Node.metadata] map; a `null`/blank JSON value yields an empty
         * map, matching the Python `json.loads(row["metadata_json"] or "{}")` fallback.
         */
        fun fromRow(row: Map<String, Any?>): Node {
            val metadataJson = row["metadata_json"] as? String
            val parsedMetadata: Map<String, Any?> =
                if (metadataJson.isNullOrBlank()) {
                    emptyMap()
                } else {
                    parseMetadataJson(metadataJson)
                }
            return Node(
                hashId = row["hash_id"] as String,
                baseType = row["base_type"] as String,
                nodeType = row["node_type"] as String,
                content = row["content"] as String,
                mentionTime = (row["mention_time"] as Number).toLong(),
                eventTimeRaw = row["event_time_raw"] as? String,
                eventTime = row["event_time"] as? String,
                startTs = (row["start_ts"] as? Number)?.toLong(),
                endTs = (row["end_ts"] as? Number)?.toLong(),
                importance = (row["importance"] as? Number)?.toDouble(),
                sentiment = (row["sentiment"] as? Number)?.toDouble(),
                metadata = parsedMetadata,
                supersededBy = row["superseded_by"] as? String,
                accessCount = (row["access_count"] as? Number)?.toInt() ?: 0,
                lastAccessed = (row["last_accessed"] as? Number)?.toLong(),
                createdAt = (row["created_at"] as? Number)?.toLong() ?: 0L,
                updatedAt = (row["updated_at"] as? Number)?.toLong() ?: 0L,
            )
        }

        /**
         * Encode [metadata] as JSON with sorted keys.
         *
         * Mirrors Python `json.dumps(metadata, ensure_ascii=False, sort_keys=True)`:
         * keys are emitted in ascending lexicographic order, non-ASCII characters are
         * written verbatim (no `\uXXXX` escapes), and numbers use their minimal
         * representation. The output is deterministic so identical metadata produces
         * byte-identical JSON — a hard requirement for content-addressed rows.
         *
         * Supported value types mirror what the Python reference stores in metadata:
         * - [String], [Number] (`Int`/`Long`/`Double`/...), [Boolean], `null`
         * - [List] of any of the above (recursively)
         * - [Map] with [String] keys (recursively, sorted at every level)
         */
        @Suppress("CyclomaticComplexMethod", "NestedBlockDepth")
        internal fun metadataToJson(metadata: Map<String, Any?>): String {
            val sb = StringBuilder()
            appendJson(sb, metadata)
            return sb.toString()
        }

        /**
         * Parse a [metadata_json] string back into a map.
         *
         * Minimal JSON parser — enough for the metadata shapes the library itself
         * serializes. Kept dependency-free so the contract layer stays pure Kotlin.
         */
        @Suppress("CyclomaticComplexMethod", "NestedBlockDepth", "ReturnCount")
        internal fun parseMetadataJson(json: String): Map<String, Any?> {
            val parser = JsonReader(json)
            val value = parser.readValue()
            parser.skipWhitespace()
            if (!parser.atEnd()) {
                throw IllegalArgumentException(
                    "metadata_json: trailing characters at index ${parser.index}"
                )
            }
            @Suppress("UNCHECKED_CAST")
            return value as? Map<String, Any?>
                ?: throw IllegalArgumentException(
                    "metadata_json: expected a JSON object, got ${value?.javaClass?.simpleName}"
                )
        }

        /** Recursive JSON appender; objects have their keys sorted at every level. */
        private fun appendJson(sb: StringBuilder, value: Any?) {
            when (value) {
                null -> sb.append("null")
                is Boolean -> sb.append(if (value) "true" else "false")
                is Number -> appendNumber(sb, value)
                is String -> appendString(sb, value)
                is List<*> -> {
                    sb.append('[')
                    value.forEachIndexed { i, item ->
                        if (i > 0) sb.append(',')
                        appendJson(sb, item)
                    }
                    sb.append(']')
                }
                is Map<*, *> -> {
                    sb.append('{')
                    @Suppress("UNCHECKED_CAST")
                    val entries = value.entries.sortedBy { it.key.toString() }
                    entries.forEachIndexed { i, (k, v) ->
                        if (i > 0) sb.append(',')
                        appendString(sb, k.toString())
                        sb.append(':')
                        appendJson(sb, v)
                    }
                    sb.append('}')
                }
                else -> appendString(sb, value.toString())
            }
        }

        /**
         * Append a number using its minimal JSON representation.
         *
         * - [Int]/[Long] and other integer-valued numbers print without a decimal point.
         * - [Double]/[Float] print via Kotlin's default formatting, with the trailing
         *   `.0` preserved (matching Python `json.dumps(1.0)` → `"1.0"`).
         */
        private fun appendNumber(sb: StringBuilder, value: Number) {
            when (value) {
                is Double -> sb.append(value.toString())
                is Float -> sb.append(value.toString())
                else -> sb.append(value.toLong())
            }
        }

        /** Append a JSON string literal, escaping per RFC 8259. */
        @Suppress("ComplexCondition")
        private fun appendString(sb: StringBuilder, value: String) {
            sb.append('"')
            for (ch in value) {
                when (ch) {
                    '"' -> sb.append("\\\"")
                    '\\' -> sb.append("\\\\")
                    '\n' -> sb.append("\\n")
                    '\r' -> sb.append("\\r")
                    '\t' -> sb.append("\\t")
                    '\b' -> sb.append("\\b")
                    '\u000C' -> sb.append("\\f")
                    else ->
                        if (ch.code < 0x20) {
                            sb.append("\\u")
                            sb.append(ch.code.toString(16).padStart(4, '0'))
                        } else {
                            sb.append(ch)
                        }
                }
            }
            sb.append('"')
        }
    }
}

/** The only two allowed [Node.baseType] values (design section 3). */
val BASE_TYPES: Set<String> = setOf("content", "entity")

/**
 * Minimal JSON reader for parsing [Node.metadata] back from a `metadata_json` column.
 *
 * Implemented inline so the contract layer stays pure Kotlin (no Gson dependency).
 */
private class JsonReader(private val source: String) {
    var index: Int = 0
        private set

    fun atEnd(): Boolean = index >= source.length

    fun readValue(): Any? {
        skipWhitespace()
        if (atEnd()) throw IllegalArgumentException("metadata_json: unexpected end of input")
        val ch = source[index]
        return when {
            ch == '"' -> readString()
            ch == '{' -> readObject()
            ch == '[' -> readArray()
            ch == 't' || ch == 'f' -> readBoolean()
            ch == 'n' -> readNull()
            ch == '-' || ch in '0'..'9' -> readNumber()
            else -> throw IllegalArgumentException(
                "metadata_json: unexpected character '$ch' at index $index"
            )
        }
    }

    fun skipWhitespace() {
        while (!atEnd()) {
            val ch = source[index]
            if (ch != ' ' && ch != '\t' && ch != '\n' && ch != '\r') return
            index++
        }
    }

    private fun expect(ch: Char) {
        if (atEnd() || source[index] != ch) {
            throw IllegalArgumentException(
                "metadata_json: expected '$ch' at index $index"
            )
        }
        index++
    }

    private fun readString(): String {
        expect('"')
        val sb = StringBuilder()
        while (!atEnd()) {
            val ch = source[index++]
            if (ch == '"') return sb.toString()
            if (ch == '\\') {
                if (atEnd()) throw IllegalArgumentException("metadata_json: unterminated escape")
                val esc = source[index++]
                sb.append(
                    when (esc) {
                        '"' -> '"'
                        '\\' -> '\\'
                        '/' -> '/'
                        'n' -> '\n'
                        'r' -> '\r'
                        't' -> '\t'
                        'b' -> '\b'
                        'f' -> '\u000C'
                        'u' -> {
                            if (index + 4 > source.length) {
                                throw IllegalArgumentException("metadata_json: truncated \\u escape")
                            }
                            val hex = source.substring(index, index + 4)
                            index += 4
                            hex.toInt(16).toChar()
                        }
                        else -> throw IllegalArgumentException(
                            "metadata_json: invalid escape '\\$esc' at index ${index - 1}"
                        )
                    }
                )
            } else {
                sb.append(ch)
            }
        }
        throw IllegalArgumentException("metadata_json: unterminated string")
    }

    private fun readObject(): Map<String, Any?> {
        expect('{')
        val result = LinkedHashMap<String, Any?>()
        skipWhitespace()
        if (!atEnd() && source[index] == '}') {
            index++
            return result
        }
        while (true) {
            skipWhitespace()
            val key = readString()
            skipWhitespace()
            expect(':')
            val value = readValue()
            result[key] = value
            skipWhitespace()
            if (atEnd()) throw IllegalArgumentException("metadata_json: unterminated object")
            val ch = source[index++]
            if (ch == '}') return result
            if (ch != ',') {
                throw IllegalArgumentException(
                    "metadata_json: expected ',' or '}' at index ${index - 1}"
                )
            }
        }
    }

    private fun readArray(): List<Any?> {
        expect('[')
        val result = ArrayList<Any?>()
        skipWhitespace()
        if (!atEnd() && source[index] == ']') {
            index++
            return result
        }
        while (true) {
            result.add(readValue())
            skipWhitespace()
            if (atEnd()) throw IllegalArgumentException("metadata_json: unterminated array")
            val ch = source[index++]
            if (ch == ']') return result
            if (ch != ',') {
                throw IllegalArgumentException(
                    "metadata_json: expected ',' or ']' at index ${index - 1}"
                )
            }
        }
    }

    private fun readBoolean(): Boolean {
        if (source.startsWith("true", index)) {
            index += 4
            return true
        }
        if (source.startsWith("false", index)) {
            index += 5
            return false
        }
        throw IllegalArgumentException("metadata_json: invalid literal at index $index")
    }

    private fun readNull(): Nothing? {
        if (source.startsWith("null", index)) {
            index += 4
            return null
        }
        throw IllegalArgumentException("metadata_json: invalid literal at index $index")
    }

    private fun readNumber(): Number {
        val start = index
        if (!atEnd() && source[index] == '-') index++
        while (!atEnd() && source[index].isDigit()) index++
        if (!atEnd() && source[index] == '.') {
            index++
            while (!atEnd() && source[index].isDigit()) index++
            val text = source.substring(start, index)
            return text.toDouble()
        }
        if (!atEnd() && (source[index] == 'e' || source[index] == 'E')) {
            index++
            if (!atEnd() && (source[index] == '+' || source[index] == '-')) index++
            while (!atEnd() && source[index].isDigit()) index++
            val text = source.substring(start, index)
            return text.toDouble()
        }
        val text = source.substring(start, index)
        return text.toLong()
    }
}
