package com.l2dchat.chat

import com.l2dchat.core.mem.ReceiptStatus
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class CompactIngestGateTest {
    @Test
    fun `only one compact ingest can be in flight`() {
        val gate = CompactIngestGate()

        assertNotNull(gate.tryStart())
        assertNull(gate.tryStart())
    }

    @Test
    fun `failed receipt releases gate without allowing watermark advance`() {
        val gate = CompactIngestGate()
        val token = gate.tryStart()!!

        assertFalse(gate.finish(token, ReceiptStatus.FAILED))
        assertNotNull(gate.tryStart())
    }

    @Test
    fun `done receipt releases gate and allows watermark advance`() {
        val gate = CompactIngestGate()
        val token = gate.tryStart()!!

        assertTrue(gate.finish(token, ReceiptStatus.DONE))
        assertNotNull(gate.tryStart())
    }

    @Test
    fun `aborted ingest releases gate without a completion decision`() {
        val gate = CompactIngestGate()
        val token = gate.tryStart()!!

        gate.abort(token)

        assertNotNull(gate.tryStart())
    }

    @Test
    fun `stale abort cannot release a newer ingest`() {
        val gate = CompactIngestGate()
        val firstToken = gate.tryStart()!!
        assertTrue(gate.finish(firstToken, ReceiptStatus.DONE))
        val secondToken = gate.tryStart()!!

        gate.abort(firstToken)

        assertNull(gate.tryStart())
        assertTrue(gate.finish(secondToken, ReceiptStatus.DONE))
    }
}
