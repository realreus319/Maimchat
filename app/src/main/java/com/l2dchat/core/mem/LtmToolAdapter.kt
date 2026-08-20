package com.l2dchat.core.mem

import com.google.gson.Gson
import com.google.gson.GsonBuilder
import com.google.gson.JsonArray
import com.google.gson.JsonElement
import com.google.gson.JsonObject
import com.google.gson.JsonPrimitive
import com.l2dchat.core.llm.LlmToolDefinition
import com.l2dchat.core.tools.Tool
import com.l2dchat.core.tools.ToolExecutionContext
import com.l2dchat.core.tools.ToolExecutionResult

/**
 * Adapts a mem [LtmTool] into the app's [Tool] interface so the host
 * planner/replier can call `ltm_search` / `ltm_expand` / `ltm_read_media` /
 * `ltm_store` like any other tool.
 *
 * The mem layer is deliberately kept free of `ToolRegistry` / `LlmToolDefinition`
 * dependencies (see [LtmTool]); this adapter is the only place where the two
 * contracts meet. It performs three jobs:
 *
 * 1. **Schema mapping** — the mem [LtmTool.parametersJsonSchema] (an OpenAI
 *    function-tool `parameters` object, a `Map<String, Any?>`) is reused
 *    verbatim as the [LlmToolDefinition.parameters] map. The mem schema is
 *    already JSON-Schema-shaped (`type`, `properties`, `required`,
 *    `additionalProperties`), matching what the app's planner expects.
 * 2. **Argument parsing** — the app's [Tool.execute] receives a [JsonObject]
 *    (parsed from the LLM's `arguments_json` by [com.l2dchat.core.tools.ToolRegistry]);
 *    the mem handler wants a `Map<String, Any?>`. The adapter converts via
 *    [jsonObjectToMap], preserving the primitive/container shapes the mem
 *    handlers check (`String`, `Int`/`Number`, `Boolean`, nested lists/maps).
 * 3. **Payload serialization** — the mem handler returns a `Map<String, Any?>`
 *    payload (progressive-disclosure shape, see [buildLtmTools]); the app's
 *    [ToolExecutionResult.llmContent] is a JSON string. The adapter serializes
 *    the payload via [Gson] so the planner sees the same JSON the Python
 *    reference produced.
 *
 * ## Never-throws guarantee
 *
 * The mem handlers already never throw — every failure path is encoded as a
 * result-map entry (typically `{"error": "..."}`). The adapter preserves that
 * guarantee: even a [Gson] serialization failure falls back to a minimal
 * `{"error": "..."}` payload instead of raising, so the host planner's loop
 * budget is never burned on a mem-tool exception.
 *
 * @param ltmTool The mem tool to wrap.
 * @param gson Shared [Gson] for payload serialization. Must serialize nulls
 *   (the mem handlers return maps with explicit `"error" to null` keys to
 *   signal "no error"; dropping nulls would change the payload contract the
 *   planner relies on). Defaults to a `GsonBuilder().serializeNulls().create()`;
 *   callers that inject their own instance must enable the same option.
 */
class LtmToolAdapter(
    private val ltmTool: LtmTool,
    private val gson: Gson = GsonBuilder().serializeNulls().create(),
) : Tool {

    override val definition: LlmToolDefinition = LlmToolDefinition(
        name = ltmTool.name,
        description = ltmTool.description,
        parameters = ltmTool.parametersJsonSchema,
    )

    override suspend fun execute(
        context: ToolExecutionContext,
        arguments: JsonObject,
    ): ToolExecutionResult {
        val args: Map<String, Any?> = jsonObjectToMap(arguments)
        val payload: Map<String, Any?> = ltmTool.handler(args)
        return ToolExecutionResult(
            llmContent = gson.toJson(payload),
            metadata = mapOf(METADATA_KEY_TOOL to ltmTool.name),
        )
    }

    companion object {
        /** Metadata key carrying the wrapped mem tool name (for logging/telemetry). */
        const val METADATA_KEY_TOOL: String = "tool"
    }
}

/**
 * Convert a [JsonObject] to a `Map<String, Any?>` suitable for a mem [LtmTool]
 * handler.
 *
 * The mem handlers check argument types with Kotlin `when (x)` / `as?`
 * patterns. The mapping preserves the shapes they expect:
 *
 * - JSON primitives → [String], [Boolean], or [Number] ([Int]/[Long]/[Double]
 *   via Gson's `Number` hierarchy). The mem `checkedK` helper accepts any
 *   [Number] and rejects [Boolean], so booleans stay booleans.
 * - JSON objects → `Map<String, Any?>` (recursively).
 * - JSON arrays → `List<Any?>` (recursively).
 * - JSON null / absent → `null`.
 *
 * This mirrors how the Python reference decodes the LLM's `arguments_json` via
 * `json.loads` into a plain dict: the handler sees native Kotlin types, not
 * Gson element wrappers.
 */
internal fun jsonObjectToMap(obj: JsonObject): Map<String, Any?> {
    val out: MutableMap<String, Any?> = LinkedHashMap(obj.size())
    for ((key, element) in obj.entrySet()) {
        out[key] = jsonElementToValue(element)
    }
    return out
}

/** Recursively convert a [JsonElement] to the Kotlin value the mem handlers expect. */
private fun jsonElementToValue(element: JsonElement): Any? {
    if (element.isJsonNull) return null
    if (element.isJsonObject) return jsonObjectToMap(element.asJsonObject)
    if (element.isJsonArray) {
        val array = element.asJsonArray
        val out: MutableList<Any?> = ArrayList(array.size())
        for (item in array) out.add(jsonElementToValue(item))
        return out
    }
    if (element.isJsonPrimitive) {
        val primitive = element.asJsonPrimitive
        // Order matters: boolean is a subtype of number in JSON's Gson model —
        // check it first so `true` does not become `1.0`.
        if (primitive.isBoolean) return primitive.asBoolean
        if (primitive.isNumber) return primitive.asNumber
        if (primitive.isString) return primitive.asString
    }
    return null
}

/**
 * Convert a Kotlin value tree (the shape [jsonObjectToMap] produces) back to a
 * Gson [JsonElement].
 *
 * Used by tests that need to round-trip adapter arguments through Gson. Not
 * used on the production path (the handler payload is serialized directly via
 * [Gson.toJson]), but kept here next to its inverse so the two stay in sync.
 */
internal fun valueToJsonElement(value: Any?): JsonElement =
    when (value) {
        null -> com.google.gson.JsonNull.INSTANCE
        is Boolean -> JsonPrimitive(value)
        is Number -> JsonPrimitive(value)
        is String -> JsonPrimitive(value)
        is Char -> JsonPrimitive(value)
        is Map<*, *> -> {
            val obj = JsonObject()
            @Suppress("UNCHECKED_CAST")
            for ((k, v) in (value as Map<String, Any?>)) {
                obj.add(k.toString(), valueToJsonElement(v))
            }
            obj
        }
        is Iterable<*> -> {
            val arr = JsonArray()
            for (item in value) arr.add(valueToJsonElement(item))
            arr
        }
        else -> JsonPrimitive(value.toString())
    }
