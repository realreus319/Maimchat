package com.l2dchat.core.mem

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

class MemErrorsTest {
    @Test
    fun `MemError is an Exception subclass`() {
        val error = ConfigError("bad config")
        assertTrue("MemError must extend Exception", error is Exception)
        assertTrue("ConfigError must extend MemError", error is MemError)
    }

    @Test
    fun `ConfigError propagates message and inherits MemError`() {
        val error = ConfigError("missing dim")
        assertEquals("missing dim", error.message)
        assertTrue(error is MemError)
    }

    @Test
    fun `DimMismatchError propagates message and inherits MemError`() {
        val error = DimMismatchError("expected 384, got 512")
        assertEquals("expected 384, got 512", error.message)
        assertTrue(error is MemError)
    }

    @Test
    fun `ToolValidationError propagates message and inherits MemError`() {
        val error = ToolValidationError("tool 'foo' unknown")
        assertEquals("tool 'foo' unknown", error.message)
        assertTrue(error is MemError)
    }

    @Test
    fun `ProviderError propagates message and inherits MemError`() {
        val error = ProviderError("upstream 503")
        assertEquals("upstream 503", error.message)
        assertTrue(error is MemError)
    }

    @Test
    fun `ReactLoopAbort inherits MemError and propagates message`() {
        val error = ReactLoopAbort("max turns reached")
        assertTrue("ReactLoopAbort must extend MemError", error is MemError)
        assertTrue("ReactLoopAbort must extend Exception", error is Exception)
        assertEquals("max turns reached", error.message)
    }

    @Test
    fun `ReactLoopAbort calls defaults to empty list`() {
        val error = ReactLoopAbort("aborted")
        assertNotNull(error.calls)
        assertTrue("calls must default to an empty list", error.calls.isEmpty())
    }

    @Test
    fun `ReactLoopAbort preserves passed calls list reference`() {
        val records: List<Any> = listOf("call-a", "call-b", 42)
        val error = ReactLoopAbort("aborted", records)
        assertEquals(3, error.calls.size)
        assertEquals("call-a", error.calls[0])
        assertEquals("call-b", error.calls[1])
        assertEquals(42, error.calls[2])
    }

    @Test
    fun `ReactLoopAbort defensive-copies the calls argument so later mutations do not leak`() {
        val source: MutableList<Any> = mutableListOf("call-a")
        val error = ReactLoopAbort("aborted", source)
        source.add("call-b")
        assertEquals(
                "ReactLoopAbort should snapshot calls at construction time",
                1,
                error.calls.size,
        )
        assertEquals("call-a", error.calls[0])
    }

    @Test
    fun `every concrete subclass is catchable as MemError`() {
        val samples: List<MemError> =
                listOf(
                        ConfigError("c"),
                        DimMismatchError("d"),
                        ToolValidationError("t"),
                        ReactLoopAbort("r"),
                        ProviderError("p"),
                )
        assertEquals(5, samples.size)
        samples.forEach { err ->
            try {
                throw err
            } catch (caught: MemError) {
                assertSame(err, caught)
            } catch (caught: Exception) {
                fail("Subclass should be caught as MemError, got ${caught::class.java.name}")
            }
        }
    }
}