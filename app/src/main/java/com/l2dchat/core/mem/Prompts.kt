package com.l2dchat.core.mem

import java.time.Instant
import java.time.ZoneId
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter
import java.util.Locale

/**
 * System prompt assembly for the extraction agent — Kotlin port of
 * `mem/prompts.py` (design doc section 2).
 *
 * The prompt fixes four things: the agent's role, the registered node types
 * (rendered by [NodeTypeRegistry.promptSpec]), the `event_time` normalization
 * rules anchored at the batch's `time_anchor`, and the working protocol
 * (entity resolution first, mandatory linking, supersede on correction, no
 * invented hashes).
 */
object Prompts {

    /**
     * Day-of-week formatter producing the English weekday name, matching
     * Python's `%A` (e.g. `Thursday`).
     */
    private val WEEKDAY_FORMAT: DateTimeFormatter =
        DateTimeFormatter.ofPattern("EEEE", Locale.ENGLISH)

    /** ISO 8601 formatter with second precision, no offset. */
    private val ISO_SECONDS: DateTimeFormatter =
        DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss")

    /**
     * Build the extraction agent's system prompt.
     *
     * @param registry Node type registry; every registered type is described
     *   via its declarative prompt fragment.
     * @param timeAnchorEpoch The batch's anchor time as epoch seconds; all
     *   relative time expressions in the text are resolved against it.
     * @param zoneId Timezone used to render the anchor's wall-clock string.
     *   Defaults to [ZoneId.systemDefault] to mirror Python's
     *   `datetime.fromtimestamp`.
     * @param extra Optional host-supplied protocol section (e.g. how to use
     *   host-injected knowledge tools), appended verbatim.
     * @return The complete system prompt (English, markdown sections).
     */
    fun buildSystemPrompt(
        registry: NodeTypeRegistry,
        timeAnchorEpoch: Long,
        zoneId: ZoneId = ZoneId.systemDefault(),
        extra: String? = null,
    ): String {
        val anchor = formatAnchor(timeAnchorEpoch, zoneId)
        val lines = ArrayList<String>()
        lines.add("You are the memory extraction agent of a personal long-term memory system.")
        lines.add("Read one text batch and store what is worth remembering — events, life")
        lines.add("captures, and the entities (people, places, objects, concepts) they mention —")
        lines.add("using ONLY the provided tools. Every write goes through a tool; your closing")
        lines.add("plain-text reply is just a one-sentence summary of what you stored.")
        lines.add("")
        lines.add(registry.promptSpec())
        lines.add("")
        lines.add("# Time normalization")
        lines.add("This batch was written around $anchor. Resolve every relative or fuzzy time")
        lines.add("expression (\"yesterday\", \"next Saturday\", \"last month\") against this anchor.")
        lines.add("- Keep the original expression in event_time_raw (verbatim, any language).")
        lines.add("- Put the normalized value in event_time:")
        for (line in formatExampleTimes().split("\n")) {
            lines.add(line)
        }
        lines.add("- When the text says nothing about when something happened, leave event_time")
        lines.add("  empty. Periodic times (\"every Friday\") cannot be expressed: leave")
        lines.add("  event_time empty and keep the expression in event_time_raw.")
        lines.add("- mention_time (when the batch itself was said/recorded) is filled in by the")
        lines.add("  system, never by you.")
        lines.add("")
        lines.add("# Image attachments")
        lines.add("The user message may contain inline images; each one is preceded by an")
        lines.add("[IMAGE media_path=<path>] marker carrying the host-side file path.")
        lines.add("- Record every image as a life_capture node: content describes what the")
        lines.add("  image shows, and metadata carries media_path (the marker's path),")
        lines.add("  modality (\"photo\"), captured_at (when the image was taken/sent, if the")
        lines.add("  text says so) and source.")
        lines.add("- Entities recognizable in an image are treated like any mentioned entity:")
        lines.add("  search_entities / register_entity / link_event_entity as usual.")
        lines.add("")
        lines.add("# Working protocol")
        lines.add("1. Entity resolution comes first: call search_entities for every name you")
        lines.add("   encounter. When a returned candidate clearly is the same entity, reuse it")
        lines.add("   (register_entity returns its hash with reused=true); call register_entity")
        lines.add("   only for genuinely new entities, never twice for the same one, and always")
        lines.add("   with a one-line description stating who/what the entity is — factual, in")
        lines.add("   the batch's language (e.g. \"散爆网络出品的策略RPG手游\", not a repeat of")
        lines.add("   the entity_type).")
        lines.add("2. Every event/life_capture MUST link ALL entities it mentions: list them in")
        lines.add("   entity_mentions when calling register_content, then call link_event_entity")
        lines.add("   for each one. Unlinked mentions fail the closing validation and are sent")
        lines.add("   back to you for another round.")
        lines.add("3. When the batch contradicts or corrects something already stored (check")
        lines.add("   with entity_events), register the new content first, then call")
        lines.add("   supersede(old_hash, new_hash) to retire the outdated node.")
        lines.add("4. When you learn a new name for a known entity, record it with")
        lines.add("   update_entity_aliases so future mentions normalize for free.")
        lines.add("5. Never invent hashes: only use hash values returned by tool calls.")
        lines.add("6. Be selective: skip small talk with no lasting value. When you are done,")
        lines.add("   reply with one plain sentence summarizing what you stored.")
        val prompt = lines.joinToString("\n")
        return if (!extra.isNullOrBlank()) {
            "$prompt\n\n# Host tools\n$extra"
        } else {
            prompt
        }
    }

    /**
     * Build the search sub agent's system prompt — Kotlin port of
     * `mem/prompts.py::build_search_prompt` (design section 5.4).
     *
     * The prompt fixes four things: the agent's role, the current time anchor
     * (relative time expressions in questions are resolved against it), the
     * four retrieval tools' contracts, and the working protocol (rephrase →
     * progressive disclosure → ground every claim in hash citations → concise
     * answer in the question's language).
     *
     * @param nowEpoch Current time as epoch seconds; relative time expressions
     *   in the question ("last week", "recently") are resolved against it.
     * @param zoneId Timezone used to render the anchor's wall-clock string.
     *   Defaults to [ZoneId.systemDefault] to mirror Python's
     *   `datetime.fromtimestamp`.
     * @param extra Optional host-supplied protocol section (e.g. how to use
     *   host-injected knowledge tools), appended verbatim.
     * @return The complete system prompt (English, markdown sections).
     */
    fun buildSearchPrompt(
        nowEpoch: Long,
        zoneId: ZoneId = ZoneId.systemDefault(),
        extra: String? = null,
    ): String {
        val anchor = formatAnchor(nowEpoch, zoneId)
        val lines = ArrayList<String>()
        lines.add("You are the memory search agent of a personal long-term memory system.")
        lines.add("Answer the user's question from the memory store, using ONLY the provided")
        lines.add("tools. The current time is $anchor — resolve relative time expressions")
        lines.add("(\"last week\", \"recently\") against it.")
        lines.add("")
        lines.add("# Tools")
        lines.add("- search: semantic search over memory nodes. Returns summaries (hash prefix,")
        lines.add("  80-char preview, mention/event time, linked entity names) — never full")
        lines.add("  text. Filters: time_range (normalized expression like \"2026-07\" or")
        lines.add("  \"2026-07-01/2026-08-01\"), time_axis (\"event\" for when it happened,")
        lines.add("  \"mention\" for when it was recorded), entity, node_type.")
        lines.add("- temporal: pure time-axis scan when the question is about a period rather")
        lines.add("  than a topic (\"what happened in June\").")
        lines.add("- entity_events: everything linked to one entity, most recent first.")
        lines.add("- expand: full content + metadata + links of one node, by full hash or the")
        lines.add("  8-char prefix from a summary. Use it ONLY for the few summaries that look")
        lines.add("  directly relevant.")
        lines.add("")
        lines.add("# Working protocol")
        lines.add("1. Rephrase the question into one or more concrete searches. Try different")
        lines.add("   wordings (synonyms, entity names) when a search comes back empty — an")
        lines.add("   empty result means \"not found this way\", not \"not in memory\".")
        lines.add("2. Progressive disclosure: skim summaries first, then expand only what you")
        lines.add("   actually cite. Do not expand everything.")
        lines.add("3. Ground every claim in nodes you found: cite their hash prefixes inline,")
        lines.add("   like [gawL74cm]. If memory says nothing about the question, say so")
        lines.add("   plainly — never invent memories.")
        lines.add("4. Keep the final answer concise (a few sentences), in the question's")
        lines.add("   language, with hash citations.")
        val prompt = lines.joinToString("\n")
        return if (!extra.isNullOrBlank()) {
            "$prompt\n\n# Host tools\n$extra"
        } else {
            prompt
        }
    }

    /**
     * Format the anchor epoch seconds as `YYYY-MM-DDTHH:MM:SS (Weekday)`,
     * matching Python's `time_anchor.strftime("%Y-%m-%dT%H:%M:%S (%A)")`.
     */
    private fun formatAnchor(epochSeconds: Long, zoneId: ZoneId): String {
        val zoned = ZonedDateTime.ofInstant(Instant.ofEpochSecond(epochSeconds), zoneId)
        val iso = zoned.format(ISO_SECONDS)
        val weekday = zoned.format(WEEKDAY_FORMAT)
        return "$iso ($weekday)"
    }

    /**
     * Canonical `event_time` examples for the extraction prompt.
     *
     * Mirrors `format_example_times` in `timeparse.py` verbatim.
     */
    internal fun formatExampleTimes(): String =
        listOf(
            "event_time format (ISO 8601 + EDTF subset; leave empty when unknown):",
            "  2025                     a whole year",
            "  2026-03                  a whole month",
            "  2026-08-08               a single day",
            "  2026-08-08T15:00         a specific minute",
            "  2026-07-31/2026-08-02    interval, the end day included",
            "  2025-12/..               open interval (ongoing since then)",
            "  2026-03~                 approximate time",
        ).joinToString(separator = "\n")
}
