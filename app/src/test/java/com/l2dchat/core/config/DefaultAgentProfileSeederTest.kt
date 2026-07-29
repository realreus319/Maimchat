package com.l2dchat.core.config

import com.l2dchat.core.message.AgentConfigEntity
import com.l2dchat.core.message.ImpressionEntity
import com.l2dchat.core.message.MediaBlockEntity
import com.l2dchat.core.message.MemoryEntity
import com.l2dchat.core.message.MoodStateEntity
import com.l2dchat.core.message.PromptTemplateEntity
import com.l2dchat.core.storage.RuntimeStateDao
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Test

class DefaultAgentProfileSeederTest {
    @Test
    fun `seeder inserts persona config and scoped prompts`() {
        val dao = FakeRuntimeStateDao()

        runBlocking {
            DefaultAgentProfileSeeder.seed(
                    source =
                            MapDefaultAgentProfileSource(
                                    mapOf(
                                            "agents/agent-a/agent.json" to
                                                    """
                                                    {
                                                      "display_name": "Default",
                                                      "persona": "default persona",
                                                      "provider": "openai",
                                                      "model": "model-a",
                                                      "settings_json": {"temperature": 0.7}
                                                    }
                                                    """
                                                            .trimIndent(),
                                            "agents/agent-a/prompts/planner_system.md" to
                                                    "planner prompt",
                                            "agents/agent-a/prompts/decision_system.md" to
                                                    "decision prompt",
                                            "agents/agent-a/prompts/replier_system.md" to
                                                    "replier system prompt",
                                            "agents/agent-a/prompts/replier_user.md" to
                                                    "replier user prompt"
                                    )
                            ),
                    stateDao = dao,
                    agentId = "agent-a",
                    displayName = "Live2D Name"
            )
        }

        val config = dao.agentConfigs.getValue("agent-a")
        assertEquals("Live2D Name", config.displayName)
        assertEquals("default persona", config.persona)
        assertEquals("openai", config.provider)
        assertEquals("model-a", config.model)
        assertEquals("{\"temperature\":0.7}", config.settingsJson)
        assertEquals("planner prompt", dao.promptTemplates.getValue("agent-a_planner_system").body)
        assertEquals("decision prompt", dao.promptTemplates.getValue("agent-a_decision_system").body)
        assertEquals(
                "replier system prompt",
                dao.promptTemplates.getValue("agent-a_replier_system").body
        )
        assertEquals("replier user prompt", dao.promptTemplates.getValue("agent-a_replier_user").body)
        assertEquals("agent-a", dao.promptTemplates.getValue("agent-a_replier_user").agentId)
    }

    @Test
    fun `seeder does not overwrite existing agent config`() {
        val existing =
                AgentConfigEntity(
                        agentId = "agent-a",
                        displayName = "Existing",
                        persona = "existing persona",
                        provider = null,
                        model = null,
                        settingsJson = null,
                        updatedAtMillis = 99L
                )
        val dao = FakeRuntimeStateDao(agentConfigs = mutableMapOf("agent-a" to existing))

        runBlocking {
            DefaultAgentProfileSeeder.seed(
                    source =
                            MapDefaultAgentProfileSource(
                                    mapOf(
                                            "agents/agent-a/agent.json" to
                                                    """{"display_name":"Default","persona":"default"}"""
                                    )
                            ),
                    stateDao = dao,
                    agentId = "agent-a",
                    displayName = "Live2D Name"
            )
        }

        assertEquals(existing, dao.agentConfigs.getValue("agent-a"))
    }

    private class MapDefaultAgentProfileSource(
            private val files: Map<String, String>
    ) : DefaultAgentProfileSource {
        override fun readText(path: String): String? = files[path]
    }

    private class FakeRuntimeStateDao(
            val agentConfigs: MutableMap<String, AgentConfigEntity> = mutableMapOf(),
            val promptTemplates: MutableMap<String, PromptTemplateEntity> = mutableMapOf()
    ) : RuntimeStateDao {
        override suspend fun upsertAgentConfig(config: AgentConfigEntity) {
            agentConfigs[config.agentId] = config
        }

        override suspend fun upsertPromptTemplate(template: PromptTemplateEntity) {
            promptTemplates[template.templateId] = template
        }

        override suspend fun upsertMemory(memory: MemoryEntity) = Unit

        override suspend fun upsertImpression(impression: ImpressionEntity) = Unit

        override suspend fun upsertMoodState(moodState: MoodStateEntity) = Unit

        override suspend fun upsertMediaBlock(mediaBlock: MediaBlockEntity) = Unit

        override suspend fun queryMediaBlocksForMessage(messageId: String): List<MediaBlockEntity> =
                emptyList()

        override suspend fun queryAgentConfig(agentId: String): AgentConfigEntity? =
                agentConfigs[agentId]

        override suspend fun queryPromptTemplate(
                name: String,
                agentId: String
        ): PromptTemplateEntity? =
                promptTemplates.values.firstOrNull {
                    it.name == name && (it.agentId == agentId || it.agentId == null)
                }

        override suspend fun queryPromptTemplateById(templateId: String): PromptTemplateEntity? =
                promptTemplates[templateId]

        override suspend fun queryMemories(
                contextId: String,
                agentId: String,
                limit: Int
        ): List<MemoryEntity> = emptyList()

        override suspend fun countMemories(contextId: String, agentId: String): Int = 0

        override suspend fun queryMemoryByContent(
                contextId: String,
                agentId: String,
                content: String
        ): MemoryEntity? = null

        override suspend fun reinforceMemory(
                memoryId: String,
                accessCount: Int,
                lastAccessMillis: Long
        ) = Unit

        override suspend fun deleteMemory(memoryId: String) = Unit

        override suspend fun deleteAllMemories() = Unit

        override suspend fun deleteAllImpressions() = Unit

        override suspend fun deleteAllMoodState() = Unit

        override suspend fun deleteAllMediaBlocks() = Unit

        override suspend fun queryImpression(
                contextId: String,
                agentId: String,
                subjectId: String
        ): ImpressionEntity? = null

        override suspend fun queryImpressions(
                contextId: String,
                agentId: String,
                limit: Int
        ): List<ImpressionEntity> = emptyList()

        override suspend fun queryMoodState(contextId: String, agentId: String): MoodStateEntity? =
                null

        override fun observeMoodState(
                contextId: String,
                agentId: String
        ): kotlinx.coroutines.flow.Flow<MoodStateEntity?> = kotlinx.coroutines.flow.flowOf(null)
    }
}
