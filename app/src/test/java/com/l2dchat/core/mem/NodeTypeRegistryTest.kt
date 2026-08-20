package com.l2dchat.core.mem

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

/**
 * Kotlin port of the Python `NodeTypeRegistry` contract — see `mem/registry.py`.
 *
 * Covers the three built-in types, lookup semantics, duplicate-registration
 * rejection, the `promptSpec()` rendering, and custom-type registration.
 */
class NodeTypeRegistryTest {
    @Test
    fun `default registry knows exactly event, entity, life_capture in that order`() {
        val registry = NodeTypeRegistry()
        assertEquals(
            listOf("event", "entity", "life_capture"),
            registry.knownTypes(),
        )
    }

    @Test
    fun `withBuiltins equals false starts empty`() {
        val registry = NodeTypeRegistry(withBuiltins = false)
        assertTrue(registry.knownTypes().isEmpty())
    }

    @Test
    fun `has returns true for builtins and false for unknown types`() {
        val registry = NodeTypeRegistry()
        assertTrue(registry.has("event"))
        assertTrue(registry.has("entity"))
        assertTrue(registry.has("life_capture"))
        assertFalse(registry.has("unknown"))
        assertFalse(registry.has(""))
    }

    @Test
    fun `get event returns the verbatim builtin spec`() {
        val registry = NodeTypeRegistry()
        val spec = registry.get("event")
        assertEquals("event", spec.nodeType)
        assertEquals("content", spec.baseType)
        assertEquals(
            "An event is something that happened or was mentioned in conversation. " +
                "content is a one-line summary; when the text says when it happened, fill " +
                "event_time with the normalized time expression.",
            spec.promptFragment,
        )
        assertEquals(
            linkedMapOf(
                "participants" to "list[str]: names of the people involved",
                "topics" to "list[str]: short topic keywords",
                "promises" to "list[str]: commitments or promises made (optional)",
            ),
            spec.metadataFields,
        )
    }

    @Test
    fun `get entity returns the verbatim builtin spec`() {
        val registry = NodeTypeRegistry()
        val spec = registry.get("entity")
        assertEquals("entity", spec.nodeType)
        assertEquals("entity", spec.baseType)
        assertEquals(
            "An entity is a registry card for a person, place, object or concept. " +
                "content is 'name: one-line description'; keep aliases up to date so " +
                "future mentions normalize for free.",
            spec.promptFragment,
        )
        assertEquals(
            linkedMapOf(
                "canonical_name" to "str: the canonical display name",
                "aliases" to "list[str]: alternative names this entity is known by",
                "entity_type" to "str: person | place | object | concept | ...",
                "appearance_count" to
                    "int: number of events linking to this entity " +
                    "(maintained by the library, +1 per link)",
            ),
            spec.metadataFields,
        )
    }

    @Test
    fun `get life_capture returns the verbatim builtin spec with modality photo or video or voice or text`() {
        val registry = NodeTypeRegistry()
        val spec = registry.get("life_capture")
        assertEquals("life_capture", spec.nodeType)
        assertEquals("content", spec.baseType)
        assertEquals(
            "A life_capture is a host-supplied text digest of a piece of media " +
                "(photo/video/voice/text). content summarizes what the media shows.",
            spec.promptFragment,
        )
        assertEquals(
            linkedMapOf(
                "modality" to "str: photo | video | voice | text",
                "media_path" to
                    "str: opaque host-side media path; the binary never " +
                    "enters the library",
                "captured_at" to "str: ISO timestamp of when the media was captured",
                "source" to "str: ingest source, e.g. life_capture",
            ),
            spec.metadataFields,
        )
    }

    @Test
    fun `get unknown type throws ToolValidationError listing registered types`() {
        val registry = NodeTypeRegistry()
        try {
            registry.get("nope")
            fail("Expected ToolValidationError for unknown node_type")
        } catch (e: ToolValidationError) {
            assertTrue(
                "Message must mention the bad type: ${e.message}",
                e.message?.contains("'nope'") == true,
            )
            assertTrue(
                "Message must list registered types: ${e.message}",
                e.message?.contains("event") == true &&
                    e.message?.contains("entity") == true &&
                    e.message?.contains("life_capture") == true,
            )
        }
    }

    @Test
    fun `register duplicate builtin throws ToolValidationError`() {
        val registry = NodeTypeRegistry()
        try {
            registry.register(
                nodeType = "event",
                baseType = "content",
                metadataFields = emptyMap(),
            )
            fail("Expected ToolValidationError for duplicate node_type")
        } catch (e: ToolValidationError) {
            assertTrue(
                "Message must mention duplicate node_type: ${e.message}",
                e.message?.contains("already registered") == true &&
                    e.message?.contains("'event'") == true,
            )
        }
    }

    @Test
    fun `register empty node_type throws ToolValidationError`() {
        val registry = NodeTypeRegistry(withBuiltins = false)
        try {
            registry.register(
                nodeType = "",
                baseType = "content",
                metadataFields = emptyMap(),
            )
            fail("Expected ToolValidationError for empty node_type")
        } catch (e: ToolValidationError) {
            assertTrue(
                "Message must mention node_type: ${e.message}",
                e.message?.contains("node_type") == true,
            )
        }
    }

    @Test
    fun `register invalid base_type throws ToolValidationError`() {
        val registry = NodeTypeRegistry(withBuiltins = false)
        try {
            registry.register(
                nodeType = "custom",
                baseType = "memory",
                metadataFields = emptyMap(),
            )
            fail("Expected ToolValidationError for invalid base_type")
        } catch (e: ToolValidationError) {
            assertTrue(
                "Message must mention base_type: ${e.message}",
                e.message?.contains("base_type") == true,
            )
            assertTrue(
                "Message must echo the bad value: ${e.message}",
                e.message?.contains("'memory'") == true,
            )
        }
    }

    @Test
    fun `custom registration appends to knownTypes and is retrievable`() {
        val registry = NodeTypeRegistry(withBuiltins = false)
        registry.register(
            nodeType = "custom",
            baseType = "entity",
            metadataFields = linkedMapOf("k" to "v"),
            promptFragment = "fragment",
        )
        assertEquals(listOf("custom"), registry.knownTypes())
        val spec = registry.get("custom")
        assertEquals("custom", spec.nodeType)
        assertEquals("entity", spec.baseType)
        assertEquals("fragment", spec.promptFragment)
        assertEquals(mapOf("k" to "v"), spec.metadataFields)
    }

    @Test
    fun `custom registration after builtins preserves builtin order then appends`() {
        val registry = NodeTypeRegistry()
        registry.register(
            nodeType = "reminder",
            baseType = "content",
            metadataFields = emptyMap(),
        )
        assertEquals(
            listOf("event", "entity", "life_capture", "reminder"),
            registry.knownTypes(),
        )
    }

    @Test
    fun `register copies metadataFields so later mutation of the source does not leak`() {
        val registry = NodeTypeRegistry(withBuiltins = false)
        val source = LinkedHashMap<String, String>()
        source["k"] = "v"
        registry.register(
            nodeType = "custom",
            baseType = "content",
            metadataFields = source,
        )
        source["k2"] = "v2"
        val spec = registry.get("custom")
        assertEquals(mapOf("k" to "v"), spec.metadataFields)
    }

    @Test
    fun `promptSpec renders the header and every builtin section`() {
        val registry = NodeTypeRegistry()
        val spec = registry.promptSpec()
        assertTrue(
            "promptSpec must start with the header: ${spec.take(20)}",
            spec.startsWith("# Node types"),
        )
        assertTrue("## event (base_type: content)" in spec)
        assertTrue("## entity (base_type: entity)" in spec)
        assertTrue("## life_capture (base_type: content)" in spec)
    }

    @Test
    fun `promptSpec contains the event prompt fragment and metadata field descriptions`() {
        val registry = NodeTypeRegistry()
        val spec = registry.promptSpec()
        assertTrue(
            "event prompt fragment must be present verbatim",
            "An event is something that happened or was mentioned in conversation." in spec,
        )
        assertTrue("metadata fields:" in spec)
        assertTrue("  - participants: list[str]: names of the people involved" in spec)
        assertTrue("  - topics: list[str]: short topic keywords" in spec)
        assertTrue("  - promises: list[str]: commitments or promises made (optional)" in spec)
    }

    @Test
    fun `promptSpec contains the entity metadata field descriptions`() {
        val registry = NodeTypeRegistry()
        val spec = registry.promptSpec()
        assertTrue("  - canonical_name: str: the canonical display name" in spec)
        assertTrue("  - aliases: list[str]: alternative names this entity is known by" in spec)
        assertTrue("  - entity_type: str: person | place | object | concept | ..." in spec)
        assertTrue(
            "  - appearance_count: int: number of events linking to this entity " in spec,
        )
    }

    @Test
    fun `promptSpec contains the life_capture metadata field descriptions`() {
        val registry = NodeTypeRegistry()
        val spec = registry.promptSpec()
        assertTrue("  - modality: str: photo | video | voice | text" in spec)
        assertTrue(
            "  - media_path: str: opaque host-side media path; the binary never " in spec,
        )
        assertTrue("  - captured_at: str: ISO timestamp of when the media was captured" in spec)
        assertTrue("  - source: str: ingest source, e.g. life_capture" in spec)
    }

    @Test
    fun `promptSpec emits types in registration order`() {
        val registry = NodeTypeRegistry()
        val spec = registry.promptSpec()
        val eventIdx = spec.indexOf("## event (base_type: content)")
        val entityIdx = spec.indexOf("## entity (base_type: entity)")
        val lifeCaptureIdx = spec.indexOf("## life_capture (base_type: content)")
        assertTrue("event must come before entity", eventIdx < entityIdx)
        assertTrue("entity must come before life_capture", entityIdx < lifeCaptureIdx)
    }

    @Test
    fun `promptSpec includes custom types after builtins`() {
        val registry = NodeTypeRegistry()
        registry.register(
            nodeType = "reminder",
            baseType = "content",
            metadataFields = linkedMapOf("due" to "str: when the reminder fires"),
            promptFragment = "A reminder is something the user wants to be reminded of.",
        )
        val spec = registry.promptSpec()
        assertTrue("## reminder (base_type: content)" in spec)
        assertTrue("A reminder is something the user wants to be reminded of." in spec)
        assertTrue("  - due: str: when the reminder fires" in spec)
        val reminderIdx = spec.indexOf("## reminder (base_type: content)")
        val lifeCaptureIdx = spec.indexOf("## life_capture (base_type: content)")
        assertTrue("custom type must come after builtins", lifeCaptureIdx < reminderIdx)
    }

    @Test
    fun `promptSpec is trimmed of leading and trailing whitespace`() {
        val registry = NodeTypeRegistry()
        val spec = registry.promptSpec()
        assertEquals(spec, spec.trim())
    }

    @Test
    fun `promptSpec on empty registry yields just the header`() {
        val registry = NodeTypeRegistry(withBuiltins = false)
        assertEquals("# Node types", registry.promptSpec())
    }

    @Test
    fun `BUILTIN_SPECS constant exposes the three builtins in order`() {
        assertEquals(3, NodeTypeRegistry.BUILTIN_SPECS.size)
        assertEquals(
            listOf("event", "entity", "life_capture"),
            NodeTypeRegistry.BUILTIN_SPECS.map { it.nodeType },
        )
    }
}
