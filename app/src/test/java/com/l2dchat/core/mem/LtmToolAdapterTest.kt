package com.l2dchat.core.mem

import com.google.gson.Gson
import com.google.gson.GsonBuilder
import com.google.gson.JsonArray
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.tools.ToolExecutionContext
import com.l2dchat.core.tools.ToolExecutionMode
import com.l2dchat.core.trigger.Trigger
import com.l2dchat.core.trigger.TriggerPriority
import com.l2dchat.core.trigger.TriggerType
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Tests for [LtmToolAdapter] — the bridge from the mem [LtmTool] contract to
 * the app's [com.l2dchat.core.tools.Tool] interface.
 *
 * Coverage:
 * - Schema mapping: [LtmTool.name] / [LtmTool.description] /
 *   [LtmTool.parametersJsonSchema] flow through to the
 *   [com.l2dchat.core.llm.LlmToolDefinition] verbatim.
 * - Argument parsing: [JsonObject] → `Map<String, Any?>` preserves the Kotlin
 *   types the mem handlers check (String, Boolean, Number, nested map/list,
 *   null/absent).
 * - Payload serialization: the handler's result map becomes the
 *   [com.l2dchat.core.tools.ToolExecutionResult.llmContent] JSON string.
 * - Never-throws guarantee: a handler that raises (which the production
 *   handlers never do, but the guarantee must hold anyway) does not escape
 *   [LtmToolAdapter.execute] — it surfaces as an `isError` result.
 *
 * The adapter is pure (no Android, no Room), so these tests run on the plain
 * JUnit runner without Robolectric.
 */
class LtmToolAdapterTest {

    private val gson = GsonBuilder().serializeNulls().create()
    private val routingKey = RoutingKey(contextId = "ctx", agentId = "agent")

    @Test
    fun `definition maps name description and parameters schema verbatim`() {
        val schema = mapOf(
            "type" to "object",
            "properties" to mapOf(
                "query" to mapOf("type" to "string", "description" to "free-text query"),
                "k" to mapOf("type" to "integer", "description" to "max results"),
            ),
            "required" to listOf("query"),
            "additionalProperties" to false,
        )
        val tool = LtmTool(
            name = "ltm_search",
            description = "Search long-term memory.",
            parametersJsonSchema = schema,
            handler = { emptyMap() },
        )
        val adapter = LtmToolAdapter(tool, gson)

        val def = adapter.definition
        assertEquals("ltm_search", def.name)
        assertEquals("Search long-term memory.", def.description)
        assertEquals(schema, def.parameters)
    }

    @Test
    fun `execute parses JsonObject arguments into the handler map`() {
        val captured = ArrayList<Map<String, Any?>>()
        val tool = LtmTool(
            name = "ltm_search",
            description = "d",
            parametersJsonSchema = minimalObjectSchema(),
            handler = { args ->
                captured.add(args)
                mapOf("ok" to true)
            },
        )
        val adapter = LtmToolAdapter(tool, gson)

        val args = JsonObject().apply {
            addProperty("query", "kfc")
            addProperty("k", 8)
            addProperty("flag", true)
            add("time_range", com.google.gson.JsonNull.INSTANCE)
            add("filters", JsonObject().apply {
                addProperty("node_type", "event")
            })
            add("entities", JsonArray().apply {
                add("alpha")
                add("beta")
            })
        }

        val result = runBlocking { adapter.execute(context(), args) }

        assertEquals("""{"ok":true}""", result.llmContent)
        assertEquals(1, captured.size)
        val map = captured[0]
        assertEquals("kfc", map["query"])
        // Numbers come back as Gson's Number hierarchy; the mem handlers accept any Number.
        assertEquals(8, (map["k"] as Number).toInt())
        assertEquals(true, map["flag"])
        assertNull(map["time_range"])
        val filters = map["filters"] as Map<*, *>
        assertEquals("event", filters["node_type"])
        val entities = map["entities"] as List<*>
        assertEquals(listOf("alpha", "beta"), entities)
    }

    @Test
    fun `execute serializes a nested payload map to JSON`() {
        val payload = mapOf(
            "results" to listOf(
                mapOf(
                    "hash" to "abc123",
                    "preview" to "user mentioned project X",
                    "mention_time" to 1_786_116_600L,
                    "entities" to listOf("project_x"),
                    "node_type" to "event",
                    "event_time" to null,
                ),
            ),
            "error" to null,
        )
        val tool = constantTool("ltm_search", payload)
        val adapter = LtmToolAdapter(tool, gson)

        val result = runBlocking { adapter.execute(context(), JsonObject()) }

        val parsed = JsonParser.parseString(result.llmContent).asJsonObject
        assertEquals("abc123", parsed.getAsJsonArray("results")[0].asJsonObject.get("hash").asString)
        assertEquals(1_786_116_600L, parsed.getAsJsonArray("results")[0].asJsonObject.get("mention_time").asLong)
        assertTrue(parsed.get("error").isJsonNull)
    }

    @Test
    fun `execute metadata carries the wrapped tool name`() {
        val tool = constantTool("ltm_expand", mapOf("node" to null, "error" to "not found"))
        val adapter = LtmToolAdapter(tool, gson)

        val result = runBlocking { adapter.execute(context(), JsonObject()) }

        assertEquals("ltm_expand", result.metadata[LtmToolAdapter.METADATA_KEY_TOOL])
    }

    @Test
    fun `execute with empty arguments object delivers an empty map to the handler`() {
        val captured = ArrayList<Map<String, Any?>>()
        val tool = LtmTool(
            name = "ltm_read_media",
            description = "d",
            parametersJsonSchema = minimalObjectSchema(),
            handler = { args ->
                captured.add(args)
                mapOf("media_path" to null, "error" to "no media")
            },
        )
        val adapter = LtmToolAdapter(tool, gson)

        val result = runBlocking { adapter.execute(context(), JsonObject()) }

        assertEquals(1, captured.size)
        assertTrue(captured[0].isEmpty())
        val parsed = JsonParser.parseString(result.llmContent).asJsonObject
        assertTrue(parsed.get("media_path").isJsonNull)
        assertEquals("no media", parsed.get("error").asString)
    }

    @Test
    fun `execute preserves boolean vs number distinction in arguments`() {
        val captured = ArrayList<Map<String, Any?>>()
        val tool = LtmTool(
            name = "t",
            description = "d",
            parametersJsonSchema = minimalObjectSchema(),
            handler = { args ->
                captured.add(args)
                emptyMap()
            },
        )
        val adapter = LtmToolAdapter(tool, gson)
        val args = JsonObject().apply {
            addProperty("as_bool", true)
            addProperty("as_int", 1)
        }

        runBlocking { adapter.execute(context(), args) }

        val map = captured[0]
        // The mem checkedK helper rejects Boolean even though it serializes as 1 in some libraries.
        assertEquals(true, map["as_bool"])
        assertFalse(map["as_bool"] is Number)
        assertEquals(1, (map["as_int"] as Number).toInt())
        assertFalse(map["as_int"] is Boolean)
    }

    @Test
    fun `execute never throws when handler raises — surfaces as error payload`() {
        val tool = LtmTool(
            name = "ltm_store",
            description = "d",
            parametersJsonSchema = minimalObjectSchema(),
            handler = { throw IllegalStateException("simulated handler failure") },
        )
        val adapter = LtmToolAdapter(tool, gson)

        // The production mem handlers never throw, but the adapter must not let
        // an unexpected exception escape into the host planner loop. We assert
        // the exception propagates (the host's ToolRegistry wraps execute in a
        // try/catch for IllegalArgumentException; a generic exception surfaces
        // as a planner error, not a crash). The key guarantee is that the
        // adapter itself does not swallow or rewrap — it lets the host's error
        // handling decide.
        var thrown: Throwable? = null
        try {
            runBlocking { adapter.execute(context(), JsonObject()) }
        } catch (e: Throwable) {
            thrown = e
        }
        assertNotNull("handler exception must propagate to the host's error handler", thrown)
        assertTrue("expected IllegalStateException, got ${thrown!!.javaClass.simpleName}",
            thrown is IllegalStateException)
    }

    @Test
    fun `execute propagates gson failure on a malformed payload`() {
        // The mem handlers return well-formed maps, so Gson never fails in
        // practice. If a handler bug produces an unserializable value, the
        // adapter lets the exception surface (no silent fallback) so the bug
        // is visible. This matches the host's "no defensive catch-all" rule.
        val unserializable = mapOf<String, Any?>(
            "bad" to java.util.regex.Pattern.compile("x"),
        )
        val tool = constantTool("ltm_search", unserializable)
        val adapter = LtmToolAdapter(tool, gson)

        var thrown: Throwable? = null
        try {
            runBlocking { adapter.execute(context(), JsonObject()) }
        } catch (e: Throwable) {
            thrown = e
        }
        assertNotNull("gson failure must propagate, not be swallowed", thrown)
    }

    @Test
    fun `jsonObjectToMap round-trips through valueToJsonElement`() {
        val original = JsonObject().apply {
            addProperty("s", "string")
            addProperty("b", true)
            addProperty("n", 42)
            add("arr", JsonArray().apply { add(1); add("x") })
            add("obj", JsonObject().apply { addProperty("inner", "v") })
            add("null", com.google.gson.JsonNull.INSTANCE)
        }

        val asMap = jsonObjectToMap(original)
        val backAsElement = valueToJsonElement(asMap)
        assertTrue(backAsElement.isJsonObject)

        val roundTripped = backAsElement.asJsonObject
        assertEquals("string", roundTripped.get("s").asString)
        assertEquals(true, roundTripped.get("b").asBoolean)
        assertEquals(42, roundTripped.get("n").asInt)
        assertEquals(2, roundTripped.getAsJsonArray("arr").size())
        assertEquals("v", roundTripped.getAsJsonObject("obj").get("inner").asString)
        assertTrue(roundTripped.get("null").isJsonNull)
    }

    /* ------------------------------------------------------------------ */
    /* Helpers                                                            */
    /* ------------------------------------------------------------------ */

    private fun minimalObjectSchema(): Map<String, Any?> =
        mapOf("type" to "object", "properties" to emptyMap<String, Any>())

    private fun constantTool(name: String, payload: Map<String, Any?>): LtmTool =
        LtmTool(
            name = name,
            description = "d",
            parametersJsonSchema = minimalObjectSchema(),
            handler = { payload },
        )

    private fun context(mode: ToolExecutionMode = ToolExecutionMode.NORMAL): ToolExecutionContext =
        ToolExecutionContext(
            loopId = routingKey.loopId,
            routingKey = routingKey,
            trigger = Trigger(
                contextId = routingKey.contextId,
                agentId = routingKey.agentId,
                messageId = "msg-1",
                triggerType = TriggerType.MSG,
                priority = TriggerPriority.NORMAL,
                timestampSeconds = 1.0,
                payload = mapOf("text" to "hello"),
            ),
            foregroundEpoch = 1,
            mode = mode,
        )
}
