package com.l2dchat.core.tools

import com.google.gson.JsonParser
import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.message.AgentConfigEntity
import com.l2dchat.core.message.ImpressionEntity
import com.l2dchat.core.message.MediaBlockEntity
import com.l2dchat.core.message.MemoryEntity
import com.l2dchat.core.message.MoodStateEntity
import com.l2dchat.core.message.PromptTemplateEntity
import com.l2dchat.core.storage.RuntimeStateDao
import com.l2dchat.core.trigger.Trigger
import com.l2dchat.core.trigger.TriggerPriority
import com.l2dchat.core.trigger.TriggerType
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class StateToolsTest {
    private val routingKey = RoutingKey(contextId = "room-a", agentId = "agent-a")

    @Test
    fun `memory tools store and search local memories`() = runBlocking {
        val dao = FakeRuntimeStateDao()
        val context = context()
        val storeTool =
                MemoryStoreTool(
                        stateDao = dao,
                        clockMillis = { 1_000L },
                        memoryIdFactory = { _, _, _ -> "memory-1" }
                )

        val storeResult =
                storeTool.execute(
                        context,
                        jsonObject(
                                """{"content":"Alice likes oolong tea","category":"preference","importance":0.8}"""
                        )
                )

        assertFalse(storeResult.isError)
        assertEquals("Alice likes oolong tea", dao.memories.single().content)
        assertEquals(0.8, dao.memories.single().importance, 0.0)
        assertEquals("preference", jsonObject(storeResult.llmContent)["category"].asString)

        val searchResult =
                MemorySearchTool(dao)
                        .execute(context, jsonObject("""{"query":"oolong","top_k":3}"""))
        val searchJson = jsonObject(searchResult.llmContent)

        assertFalse(searchResult.isError)
        assertEquals(1, searchJson["total"].asInt)
        assertEquals(
                "memory-1",
                searchJson["results"].asJsonArray[0].asJsonObject["memoryId"].asString
        )
    }

    @Test
    fun `memory_store dedups identical content and merges importance`() = runBlocking {
        val dao = FakeRuntimeStateDao()
        val context = context()
        val store = MemoryStoreTool(stateDao = dao, clockMillis = { 1_000L })

        store.execute(context, jsonObject("""{"content":"likes tea","importance":0.3}"""))
        val second =
                store.execute(context, jsonObject("""{"content":"likes tea","importance":0.9}"""))

        // Identical content reinforces the existing row instead of inserting a duplicate.
        assertEquals(1, dao.memories.size)
        assertEquals(0.9, dao.memories.single().importance, 0.0)
        assertEquals(1, dao.memories.single().accessCount)
        assertEquals(0.9, jsonObject(second.llmContent)["importance"].asDouble, 0.0)
    }

    @Test
    fun `memory_search reinforces recalled memories`() = runBlocking {
        val dao = FakeRuntimeStateDao()
        val context = context()
        MemoryStoreTool(stateDao = dao, clockMillis = { 1_000L }, memoryIdFactory = { _, _, _ -> "m1" })
                .execute(context, jsonObject("""{"content":"Alice likes oolong tea"}"""))

        MemorySearchTool(dao, clockMillis = { 2_000L })
                .execute(context, jsonObject("""{"query":"oolong tea"}"""))

        val reinforced = dao.memories.single { it.memoryId == "m1" }
        assertEquals(1, reinforced.accessCount)
        assertEquals(2_000L, reinforced.lastAccessMillis)
    }

    @Test
    fun `memory_store persists category`() = runBlocking {
        val dao = FakeRuntimeStateDao()
        MemoryStoreTool(stateDao = dao, clockMillis = { 1_000L })
                .execute(context(), jsonObject("""{"content":"x","category":"fact"}"""))
        assertEquals("fact", dao.memories.single().category)
    }

    @Test
    fun `impression tools update query and list local impressions`() = runBlocking {
        val dao = FakeRuntimeStateDao()
        val context = context()
        val updateTool =
                UpdateUserImpressionTool(
                        stateDao = dao,
                        clockMillis = { 2_000L },
                        impressionIdFactory = { _, _ -> "impression-1" }
                )

        val updateResult =
                updateTool.execute(
                        context,
                        jsonObject(
                                """
                                {
                                  "user_id":"alice",
                                  "impression_summary":"喜欢安静聊天",
                                  "relationship_score":0.7,
                                  "relationship_stage":"friend",
                                  "relationship_summary":"互动稳定"
                                }
                                """
                        )
                )

        assertFalse(updateResult.isError)
        // Impressions are keyed on the single canonical local-user subject, not the
        // LLM-supplied user_id (which is preserved inside the content).
        assertEquals(CANONICAL_SUBJECT_ID, dao.impressions.single().subjectId)
        assertTrue(dao.impressions.single().content.contains("user_id: alice"))
        assertTrue(dao.impressions.single().content.contains("relationship_stage: friend"))

        // A partial follow-up update must MERGE, not overwrite: relationship_stage survives.
        updateTool.execute(
                context,
                jsonObject(
                        """{"user_id":"alice","impression_summary":"更熟悉了"}"""
                )
        )
        assertEquals(1, dao.impressions.size)
        val merged = dao.impressions.single().content
        assertTrue(merged.contains("impression_summary: 更熟悉了"))
        assertTrue(merged.contains("relationship_stage: friend"))
        assertTrue(merged.contains("relationship_score: 0.7"))

        val queryResult =
                QueryImpressionTool(dao)
                        .execute(context, jsonObject("""{"user_id":"alice"}"""))
        val queryJson = jsonObject(queryResult.llmContent)

        assertFalse(queryResult.isError)
        assertTrue(queryJson["found"].asBoolean)

        val listResult = GetUserImpressionsTool(dao).execute(context, jsonObject("{}"))
        assertEquals(1, jsonObject(listResult.llmContent)["total"].asInt)
    }

    @Test
    fun `mood tool updates local mood state`() = runBlocking {
        val dao = FakeRuntimeStateDao()

        val result =
                UpdateMoodStateTool(stateDao = dao, clockMillis = { 3_000L })
                        .execute(
                                context(),
                                jsonObject(
                                        """{"valence":0.25,"arousal":0.4,"state_json":{"label":"calm"}}"""
                                )
                        )

        assertFalse(result.isError)
        assertEquals(0.25, dao.moodState?.valence ?: 0.0, 0.0)
        assertEquals(0.4, dao.moodState?.arousal ?: 0.0, 0.0)
        assertEquals("""{"label":"calm"}""", dao.moodState?.stateJson)
        assertTrue(jsonObject(result.llmContent)["moodUpdated"].asBoolean)
    }

    @Test
    fun `normal registry exposes state tools when runtime state dao is provided`() {
        val registry =
                LocalToolRegistryFactory.normalRegistry(runtimeStateDao = FakeRuntimeStateDao())

        assertEquals(
                listOf(
                        ReplierTool.NAME,
                        GetWorldStateTool.NAME,
                        LookAtTool.NAME,
                        TriggerMotionTool.NAME,
                        MemoryStoreTool.NAME,
                        MemorySearchTool.NAME,
                        GetUserImpressionsTool.NAME,
                        UpdateUserImpressionTool.NAME,
                        QueryImpressionTool.NAME,
                        UpdateMoodStateTool.NAME
                ),
                registry.definitions.map { it.name }
        )
    }

    private fun context(mode: ToolExecutionMode = ToolExecutionMode.NORMAL): ToolExecutionContext =
            ToolExecutionContext(
                    loopId = routingKey.loopId,
                    routingKey = routingKey,
                    trigger =
                            Trigger(
                                    contextId = routingKey.contextId,
                                    agentId = routingKey.agentId,
                                    messageId = "msg-1",
                                    triggerType = TriggerType.MSG,
                                    priority = TriggerPriority.NORMAL,
                                    timestampSeconds = 1.0,
                                    payload = mapOf("sender_id" to "alice", "text" to "hello")
                            ),
                    foregroundEpoch = 1,
                    mode = mode
            )

    private class FakeRuntimeStateDao : RuntimeStateDao {
        val memories = mutableListOf<MemoryEntity>()
        val impressions = mutableListOf<ImpressionEntity>()
        var moodState: MoodStateEntity? = null

        override suspend fun upsertAgentConfig(config: AgentConfigEntity) = Unit

        override suspend fun upsertPromptTemplate(template: PromptTemplateEntity) = Unit

        override suspend fun upsertMemory(memory: MemoryEntity) {
            memories.removeAll { it.memoryId == memory.memoryId }
            memories += memory
        }

        override suspend fun upsertImpression(impression: ImpressionEntity) {
            impressions.removeAll {
                it.contextId == impression.contextId &&
                        it.agentId == impression.agentId &&
                        it.subjectId == impression.subjectId
            }
            impressions += impression
        }

        override suspend fun upsertMoodState(moodState: MoodStateEntity) {
            this.moodState = moodState
        }

        override suspend fun upsertMediaBlock(mediaBlock: MediaBlockEntity) = Unit

        override suspend fun queryMediaBlocksForMessage(messageId: String): List<MediaBlockEntity> =
                emptyList()

        override suspend fun queryAgentConfig(agentId: String): AgentConfigEntity? = null

        override suspend fun queryPromptTemplate(
                name: String,
                agentId: String
        ): PromptTemplateEntity? = null

        override suspend fun queryPromptTemplateById(templateId: String): PromptTemplateEntity? =
                null

        override suspend fun queryMemories(
                contextId: String,
                agentId: String,
                limit: Int
        ): List<MemoryEntity> =
                memories
                        .matchingMemoryScope(contextId, agentId)
                        .sortedByMemoryRelevance()
                        .take(limit)

        override suspend fun countMemories(contextId: String, agentId: String): Int =
                memories.matchingMemoryScope(contextId, agentId).size

        override suspend fun queryMemoryByContent(
                contextId: String,
                agentId: String,
                content: String
        ): MemoryEntity? =
                memories.matchingMemoryScope(contextId, agentId).firstOrNull {
                    it.content == content
                }

        override suspend fun reinforceMemory(
                memoryId: String,
                accessCount: Int,
                lastAccessMillis: Long
        ) {
            val index = memories.indexOfFirst { it.memoryId == memoryId }
            if (index >= 0) {
                memories[index] =
                        memories[index].copy(
                                accessCount = accessCount,
                                lastAccessMillis = lastAccessMillis
                        )
            }
        }

        override suspend fun deleteMemory(memoryId: String) {
            memories.removeAll { it.memoryId == memoryId }
        }

        override suspend fun queryImpression(
                contextId: String,
                agentId: String,
                subjectId: String
        ): ImpressionEntity? =
                impressions.firstOrNull {
                    it.contextId == contextId && it.agentId == agentId && it.subjectId == subjectId
                }

        override suspend fun queryImpressions(
                contextId: String,
                agentId: String,
                limit: Int
        ): List<ImpressionEntity> =
                impressions
                        .filter { it.contextId == contextId && it.agentId == agentId }
                        .sortedByDescending { it.updatedAtMillis }
                        .take(limit)

        override suspend fun queryMoodState(contextId: String, agentId: String): MoodStateEntity? =
                moodState?.takeIf { it.contextId == contextId && it.agentId == agentId }
    }
}

private fun jsonObject(json: String) = JsonParser.parseString(json.trimIndent()).asJsonObject

private fun List<MemoryEntity>.matchingMemoryScope(
        contextId: String,
        agentId: String
): List<MemoryEntity> = filter { it.contextId == contextId && it.agentId == agentId }

private fun List<MemoryEntity>.sortedByMemoryRelevance(): List<MemoryEntity> =
        sortedWith(
                compareByDescending<MemoryEntity> { it.importance }
                        .thenByDescending { it.updatedAtMillis }
        )
