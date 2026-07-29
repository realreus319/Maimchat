package com.l2dchat.core.config

/** A selectable persona. [id] is the asset-bundle dir under assets/agents/<id>/ AND the routing
 *  agentId (so each persona gets its own history/memory/mood). [displayName] mirrors that bundle's
 *  agent.json `display_name` (the authoritative copy is read from the bundle at seed time). */
data class PersonaInfo(val id: String, val displayName: String)

/**
 * The canonical personas. Each maps to an asset bundle `assets/agents/<id>/` (agent.json + prompts).
 * The displayed character name and the routing agentId both come from the ACTIVE persona — decoupled
 * from the Live2D avatar (all personas currently share the Hiyori avatar).
 */
object PersonaRegistry {
    const val DEFAULT_PERSONA_ID = "gentle"

    val personas: List<PersonaInfo> =
            listOf(
                    PersonaInfo("gentle", "小莓"),
                    PersonaInfo("xiaoqian", "小千"),
            )

    fun availablePersonas(): List<PersonaInfo> = personas

    fun displayNameFor(id: String?): String? = personas.firstOrNull { it.id == id }?.displayName

    fun isKnown(id: String?): Boolean = id != null && personas.any { it.id == id }

    /** The active persona id, or the default if [id] isn't a known persona. */
    fun normalize(id: String?): String = if (isKnown(id)) id!! else DEFAULT_PERSONA_ID
}
