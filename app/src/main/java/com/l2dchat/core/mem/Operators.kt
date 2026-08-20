package com.l2dchat.core.mem

import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import kotlin.math.ln1p
import kotlin.math.pow

/**
 * Pure-function operators of the atomic tool layer — Kotlin port of
 * `mem/operators.py` (design sections 5.3 / 8.3).
 *
 * Everything here is deterministic, stateless, and storage-free: interval
 * overlap, the ranking formula, and the progressive-disclosure summary format.
 * No Android or storage dependencies — only [Node] (the contract type) and
 * `java.time` for ISO formatting.
 *
 * ## `Long` vs Python `float`
 *
 * The Python reference carries epoch seconds as `float`. This port keeps the
 * whole chain in [Long] epoch seconds (per the mem-kotlin-integration plan).
 * [rank] takes [RankItem.mentionTime] / [RankItem.startTs] as [Long], and
 * [present] takes [Node.mentionTime] as [Long]. The rank math itself uses
 * [Double] intermediate values, matching Python's float arithmetic.
 *
 * @see overlap
 * @see rank
 * @see present
 */
object Operators {

    /**
     * Seconds per day, for the rank decay math. Matches `SECONDS_PER_DAY`
     * in `operators.py`. Kept as [Double] because the decay formula divides
     * by it in floating point.
     */
    const val SECONDS_PER_DAY: Double = 86400.0

    /** Preview truncation length for progressive disclosure (design section 6). */
    const val PREVIEW_LENGTH: Int = 80

    /** Hash prefix length shown in progressive-disclosure summaries. */
    const val HASH_PREFIX_LENGTH: Int = 8

    /** Appended to a preview when (and only when) the content was truncated. */
    private const val TRUNCATION_MARKER = "[+]"

    /**
     * Formatter producing the naive local-time ISO 8601 string with second
     * precision that Python's `datetime.fromtimestamp(t).isoformat(timespec="seconds")`
     * yields — no offset, no fractional seconds. Applied to the zoned
     * LocalDateTime so the output is timezone-independent once the zone is fixed.
     */
    private val ISO_SECONDS: DateTimeFormatter =
        DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss")

    /**
     * Return whether two half-open intervals `[aStart, aEnd)` and `[bStart, bEnd)`
     * overlap.
     *
     * Touching endpoints do not count as overlapping: `[1, 2)` and `[2, 3)` are
     * disjoint. Empty intervals (`start == end`) behave as points: a zero-length
     * interval at `x` overlaps `[s, e)` iff `s <= x < e`, and two zero-length
     * intervals overlap only if they share no point (so `overlap(x, x, x, x)`
     * is `false` — the half-open interval `[x, x)` is empty).
     *
     * Mirrors `overlap` in `operators.py` line 40: `a_start < b_end and b_start < a_end`.
     *
     * @param aStart First interval start (inclusive).
     * @param aEnd   First interval end (exclusive).
     * @param bStart Second interval start (inclusive).
     * @param bEnd   Second interval end (exclusive).
     * @return `true` iff the intervals share at least one point.
     */
    fun overlap(aStart: Long, aEnd: Long, bStart: Long, bEnd: Long): Boolean =
        aStart < bEnd && bStart < aEnd

    /**
     * Score and order retrieval candidates (design section 5.3).
     *
     * For each item:
     * ```
     * future event (startTs != null && startTs > now):
     *     final = score * 1 / (1 + daysUntil / 7)      // proximity boost
     * otherwise:
     *     final = score * 0.95 ^ daysSinceMention      // mention-axis decay
     * final *= 1 + 0.05 * ln(1 + entityWeight)         // entity weighting
     * ```
     * where `daysUntil = (startTs - now) / SECONDS_PER_DAY` and
     * `daysSinceMention = (now - mentionTime) / SECONDS_PER_DAY`.
     *
     * The input list is **not** mutated; each returned item is a copy of the
     * input with [RankItem.finalScore] set. Items are sorted by `finalScore`
     * descending; ties keep their original relative order (stable sort),
     * matching Python's `list.sort(key=...)`.
     *
     * @param items Candidate items. [RankItem.score] is the cosine similarity,
     *   [RankItem.mentionTime] the mention epoch, [RankItem.startTs] the event
     *   start epoch (or `null`), and [RankItem.entityWeight] the optional entity
     *   appearance count (defaults to `0`).
     * @param now Reference epoch seconds ("today"), injected for testability.
     * @return New list of items, sorted by `finalScore` descending.
     */
    fun rank(items: List<RankItem>, now: Long): List<RankItem> =
        items
            .map { it.copy(finalScore = computeFinal(it, now)) }
            .sortedByDescending { it.finalScore }

    /** Compute the ranking formula for a single item (see [rank]). */
    private fun computeFinal(item: RankItem, now: Long): Double {
        var finalScore = item.score
        val startTs = item.startTs
        if (startTs != null && startTs > now) {
            val daysUntil = (startTs - now).toDouble() / SECONDS_PER_DAY
            finalScore *= 1.0 / (1.0 + daysUntil / 7.0)
        } else {
            val daysSince = (now - item.mentionTime).toDouble() / SECONDS_PER_DAY
            finalScore *= 0.95.pow(daysSince)
        }
        finalScore *= 1.0 + 0.05 * ln1p(item.entityWeight.toDouble())
        return finalScore
    }

    /**
     * Render nodes as progressive-disclosure summaries (design section 6).
     *
     * A summary never contains full text or metadata; the model must call
     * `expand` for detail. The [NodeSummary.preview] is the content truncated
     * to [PREVIEW_LENGTH] characters with a `"[+]"` marker appended **only when
     * truncation happened** (nothing is hidden for short contents). Truncation
     * is by Unicode codepoint count, matching Python's `len(str)` / slicing.
     *
     * The [NodeSummary.mentionTime] is the naive local-time ISO 8601 string at
     * second precision (e.g. `"2026-08-06T15:06:40"`), matching Python's
     * `datetime.fromtimestamp(t).isoformat(timespec="seconds")`. The timezone
     * is controlled by [zoneId] (default [ZoneId.systemDefault]); pass an
     * explicit zone to make the output timezone-independent.
     *
     * @param nodes       Nodes to summarize.
     * @param entityNames Optional mapping `node hashId -> entity display names`
     *   to attach to each summary; missing hashes yield an empty list. `null`
     *   is treated as an empty map.
     * @param zoneId      Timezone used to render [NodeSummary.mentionTime].
     *   Defaults to [ZoneId.systemDefault] to mirror Python's
     *   `datetime.fromtimestamp` (which uses the process-local zone).
     * @return One [NodeSummary] per node, in input order.
     */
    fun present(
        nodes: List<Node>,
        entityNames: Map<String, List<String>>? = null,
        zoneId: ZoneId = ZoneId.systemDefault(),
    ): List<NodeSummary> {
        val names = entityNames ?: emptyMap()
        return nodes.map { node ->
            val content = node.content
            val preview =
                if (content.length > PREVIEW_LENGTH) {
                    content.take(PREVIEW_LENGTH) + TRUNCATION_MARKER
                } else {
                    content
                }
            NodeSummary(
                hash = node.hashId.take(HASH_PREFIX_LENGTH),
                preview = preview,
                mentionTime = formatIsoSeconds(node.mentionTime, zoneId),
                entities = names[node.hashId]?.toList() ?: emptyList(),
                nodeType = node.nodeType,
                eventTime = node.eventTime,
            )
        }
    }

    /**
     * Format epoch seconds as a naive local-time ISO 8601 string at second
     * precision, matching `datetime.fromtimestamp(epoch).isoformat(timespec="seconds")`.
     *
     * Sub-second fractions are truncated (Python `datetime.fromtimestamp`
     * rounds to microseconds then `timespec="seconds"` drops them).
     */
    private fun formatIsoSeconds(epochSeconds: Long, zoneId: ZoneId): String =
        Instant
            .ofEpochSecond(epochSeconds)
            .atZone(zoneId)
            .toLocalDateTime()
            .format(ISO_SECONDS)
}

/**
 * One retrieval candidate passed to [Operators.rank].
 *
 * Mirrors the Python `rank` input dict keys (`score`, `mention_time`,
 * `start_ts`, `entity_weight`, plus any opaque payload). [id] is an opaque
 * caller-supplied payload (e.g. a hash or a stable identifier) preserved
 * through ranking so callers can correlate input and output without mutation.
 *
 * @property id           Opaque caller payload (e.g. node hash). Not used by
 *   the ranking formula; carried through so callers can identify items.
 * @property score        Cosine similarity (the `base` term of the formula).
 * @property mentionTime  Mention epoch seconds ("appearing when").
 * @property startTs      Event start epoch seconds, or `null` when the node
 *   has no event-time interval. A value strictly greater than `now` triggers
 *   the future-event proximity boost; otherwise the mention-axis decay applies.
 * @property entityWeight Entity appearance count used for the entity-weighting
 *   multiplier. Defaults to `0` (no boost), matching Python's
 *   `item.get("entity_weight") or 0.0`.
 * @property finalScore   The computed ranking score. `0.0` on input (ignored
 *   by [Operators.rank]); set to the formula result on the output copies.
 */
data class RankItem(
    val id: String,
    val score: Double,
    val mentionTime: Long,
    val startTs: Long? = null,
    val entityWeight: Long = 0L,
    val finalScore: Double = 0.0,
)

/**
 * One progressive-disclosure summary produced by [Operators.present].
 *
 * Mirrors the Python `present` output dict. Never contains full content or
 * metadata — those require the `expand` hop.
 *
 * @property hash        8-char prefix of the node's `hash_id`.
 * @property preview     Content truncated to [Operators.PREVIEW_LENGTH]
 *   codepoints, with `"[+]"` appended iff truncation happened.
 * @property mentionTime Naive local-time ISO 8601 string at second precision.
 * @property entities    Entity display names attached to this node (empty
 *   when no mapping was supplied or the hash was missing).
 * @property nodeType    The node's declarative type (`"event"`, `"entity"`, ...).
 * @property eventTime   The normalized event-time string, or `null` when unset.
 */
data class NodeSummary(
    val hash: String,
    val preview: String,
    val mentionTime: String,
    val entities: List<String>,
    val nodeType: String,
    val eventTime: String?,
)
