package com.l2dchat.chat

import com.l2dchat.core.mem.ReceiptStatus

/**
 * Serializes compact-memory ingests and decides whether their watermark may be committed.
 *
 * A failed receipt must release the gate for a later retry without advancing the watermark.
 */
internal class CompactIngestGate {
    private var nextToken: Long = 0L
    private var activeToken: Long? = null

    @Synchronized
    fun tryStart(): Long? {
        if (activeToken != null) return null
        nextToken += 1L
        activeToken = nextToken
        return nextToken
    }

    @Synchronized
    fun finish(token: Long, status: ReceiptStatus): Boolean {
        if (activeToken != token) return false
        activeToken = null
        return status == ReceiptStatus.DONE
    }

    /** Release an interrupted ingest without allowing its watermark to advance. */
    @Synchronized
    fun abort(token: Long) {
        if (activeToken == token) activeToken = null
    }
}
