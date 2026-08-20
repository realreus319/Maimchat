package com.l2dchat.core.mem

/**
 * Declarative node type registry — Kotlin port of `mem/registry.py`.
 *
 * Adding a node type means registering it here — a [baseType], the [metadataFields]
 * schema, and a [promptFragment] for the extraction agent — without touching the
 * architecture. [NodeTypeRegistry.promptSpec] renders every registered type into
 * the prompt text consumed by the extraction flow (design section 2).
 */

/**
 * Declarative description of one node type.
 *
 * Mirrors `NodeTypeSpec` in `registry.py`.
 *
 * @property nodeType The registered type name, e.g. `"event"`.
 * @property baseType `"content"` or `"entity"` (see [BASE_TYPES]).
 * @property metadataFields Field name -> human/LLM-readable description, in
 *   declaration order. Modeled as a [LinkedHashMap] so registration order is
 *   preserved when rendered by [NodeTypeRegistry.promptSpec].
 * @property promptFragment Free-text extraction guidance for this type.
 */
data class NodeTypeSpec(
    val nodeType: String,
    val baseType: String,
    val metadataFields: Map<String, String>,
    val promptFragment: String,
)

/**
 * Registry mapping [NodeTypeSpec.nodeType] names to their declarative specs.
 *
 * @param withBuiltins When `true` (default), the three built-in types
 *   (`event`, `entity`, `life_capture`) are pre-registered in that exact order.
 */
class NodeTypeRegistry(
    withBuiltins: Boolean = true,
) {
    private val specs: LinkedHashMap<String, NodeTypeSpec> = LinkedHashMap()

    init {
        if (withBuiltins) {
            for (spec in BUILTIN_SPECS) {
                register(
                    nodeType = spec.nodeType,
                    baseType = spec.baseType,
                    metadataFields = spec.metadataFields,
                    promptFragment = spec.promptFragment,
                )
            }
        }
    }

    /**
     * Register a node type (declarative extension point).
     *
     * @param nodeType Type name; must be non-empty and not yet registered.
     * @param baseType `"content"` or `"entity"`.
     * @param metadataFields Field name -> description, in declaration order.
     * @param promptFragment Extraction guidance text for this type.
     * @throws ToolValidationError on empty [nodeType], invalid [baseType], or a
     *   duplicate [nodeType].
     */
    fun register(
        nodeType: String,
        baseType: String,
        metadataFields: Map<String, String>,
        promptFragment: String = "",
    ) {
        if (nodeType.isEmpty()) {
            throw ToolValidationError("register: node_type must be non-empty")
        }
        if (baseType !in BASE_TYPES) {
            throw ToolValidationError(
                "register: base_type must be one of ${BASE_TYPES.toList()}, got '$baseType'"
            )
        }
        if (nodeType in specs) {
            throw ToolValidationError("register: node_type already registered: '$nodeType'")
        }
        specs[nodeType] =
            NodeTypeSpec(
                nodeType = nodeType,
                baseType = baseType,
                metadataFields = LinkedHashMap(metadataFields),
                promptFragment = promptFragment,
            )
    }

    /**
     * Look up a registered type.
     *
     * @throws ToolValidationError if [nodeType] is not registered.
     */
    fun get(nodeType: String): NodeTypeSpec =
        specs[nodeType]
            ?: throw ToolValidationError(
                "unknown node_type: '$nodeType' (registered: ${specs.keys.sorted()})"
            )

    /** Return whether [nodeType] is registered. */
    fun has(nodeType: String): Boolean = nodeType in specs

    /** Return the registered type names in registration order. */
    fun knownTypes(): List<String> = specs.keys.toList()

    /**
     * Render all registered types as extraction prompt text.
     *
     * Output format (mirrors `NodeTypeRegistry.prompt_spec` in `registry.py`):
     *
     * ```
     * # Node types
     *
     * ## {type} (base_type: {base})
     * {prompt fragment, if any}
     * metadata fields:
     *   - {name}: {description}
     * ...
     * ```
     *
     * Types are emitted in registration order; metadata fields are emitted in
     * their declaration order. Leading/trailing whitespace is stripped.
     */
    fun promptSpec(): String {
        val lines = ArrayList<String>()
        lines.add("# Node types")
        lines.add("")
        for (spec in specs.values) {
            lines.add("## ${spec.nodeType} (base_type: ${spec.baseType})")
            if (spec.promptFragment.isNotEmpty()) {
                lines.add(spec.promptFragment)
            }
            if (spec.metadataFields.isNotEmpty()) {
                lines.add("metadata fields:")
                for ((name, desc) in spec.metadataFields) {
                    lines.add("  - $name: $desc")
                }
            }
            lines.add("")
        }
        return lines.joinToString("\n").trim()
    }

    companion object {
        /**
         * The three built-in types of design section 3.
         *
         * `promptFragment` and `metadataFields` values are copied verbatim from
         * `_BUILTIN_SPECS` in `mem/registry.py` so the extraction prompt stays
         * identical to the Python reference.
         */
        val BUILTIN_SPECS: List<NodeTypeSpec> =
            listOf(
                NodeTypeSpec(
                    nodeType = "event",
                    baseType = "content",
                    metadataFields =
                        linkedMapOf(
                            "participants" to "list[str]: names of the people involved",
                            "topics" to "list[str]: short topic keywords",
                            "promises" to "list[str]: commitments or promises made (optional)",
                        ),
                    promptFragment =
                        "An event is something that happened or was mentioned in conversation. " +
                            "content is a one-line summary; when the text says when it happened, fill " +
                            "event_time with the normalized time expression.",
                ),
                NodeTypeSpec(
                    nodeType = "entity",
                    baseType = "entity",
                    metadataFields =
                        linkedMapOf(
                            "canonical_name" to "str: the canonical display name",
                            "aliases" to "list[str]: alternative names this entity is known by",
                            "entity_type" to "str: person | place | object | concept | ...",
                            "appearance_count" to
                                "int: number of events linking to this entity " +
                                "(maintained by the library, +1 per link)",
                        ),
                    promptFragment =
                        "An entity is a registry card for a person, place, object or concept. " +
                            "content is 'name: one-line description'; keep aliases up to date so " +
                            "future mentions normalize for free.",
                ),
                NodeTypeSpec(
                    nodeType = "life_capture",
                    baseType = "content",
                    metadataFields =
                        linkedMapOf(
                            "modality" to "str: photo | video | voice | text",
                            "media_path" to
                                "str: opaque host-side media path; the binary never " +
                                "enters the library",
                            "captured_at" to "str: ISO timestamp of when the media was captured",
                            "source" to "str: ingest source, e.g. life_capture",
                        ),
                    promptFragment =
                        "A life_capture is a host-supplied text digest of a piece of media " +
                            "(photo/video/voice/text). content summarizes what the media shows.",
                ),
            )
    }
}
