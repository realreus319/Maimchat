package com.l2dchat.core.tools

interface ReplierTaskStore {
    suspend fun upsertTaskSnapshot(
            request: ReplierTaskRequest,
            snapshot: ReplierTaskSnapshot,
            createdAtMillis: Long,
            updatedAtMillis: Long
    )
}

object NoopReplierTaskStore : ReplierTaskStore {
    override suspend fun upsertTaskSnapshot(
            request: ReplierTaskRequest,
            snapshot: ReplierTaskSnapshot,
            createdAtMillis: Long,
            updatedAtMillis: Long
    ) = Unit
}
