package com.l2dchat.core.config

import com.google.gson.JsonParser
import com.l2dchat.core.message.AgentConfigEntity
import com.l2dchat.core.message.ImpressionEntity
import com.l2dchat.core.message.MediaBlockEntity
import com.l2dchat.core.message.MemoryEntity
import com.l2dchat.core.message.MoodStateEntity
import com.l2dchat.core.message.PromptTemplateEntity
import com.l2dchat.core.storage.RuntimeStateDao
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Test

class AgentProfileRepositoryTest {
    @Test
    fun `load reads agent config and effective prompts`() {
        val dao =
                FakeRuntimeStateDao(
                        agentConfigs =
                                mutableMapOf(
                                        "agent-a" to
                                                AgentConfigEntity(
                                                        agentId = "agent-a",
                                                        displayName = "Agent A",
                                                        persona = "persona",
                                                        provider = "openai",
                                                        model = "model-a",
                                                        settingsJson = """{"temperature":0.7}""",
                                                        updatedAtMillis = 10L
                                                )
                                ),
                        promptTemplates =
                                mutableMapOf(
                                        "default_planner_system" to
                                                prompt(
                                                        templateId = "default_planner_system",
                                                        agentId = null,
                                                        name =
                                                                AgentPromptTemplateNames
                                                                        .PLANNER_SYSTEM,
                                                        body = "global planner",
                                                        updatedAtMillis = 1L
                                                ),
                                        "agent_planner_system" to
                                                prompt(
                                                        templateId = "agent_planner_system",
                                                        agentId = "agent-a",
                                                        name =
                                                                AgentPromptTemplateNames
                                                                        .PLANNER_SYSTEM,
                                                        body = "agent planner",
                                                        updatedAtMillis = 2L
                                                ),
                                        "default_replier_user" to
                                                prompt(
                                                        templateId = "default_replier_user",
                                                        agentId = null,
                                                        name = AgentPromptTemplateNames.REPLIER_USER,
                                                        body = "global user",
                                                        updatedAtMillis = 1L
                                                )
                                )
                )
        val repository = AgentProfileRepository(dao)

        val profile = runBlocking { repository.load(" agent-a ") }

        assertEquals("agent-a", profile.agentId)
        assertEquals("Agent A", profile.displayName)
        assertEquals("persona", profile.persona)
        assertEquals("openai", profile.provider)
        assertEquals("model-a", profile.model)
        assertEquals("""{"temperature":0.7}""", profile.settingsJson)
        assertEquals("agent planner", profile.prompts[AgentPromptTemplateNames.PLANNER_SYSTEM])
        assertEquals("global user", profile.prompts[AgentPromptTemplateNames.REPLIER_USER])
        assertEquals("", profile.prompts[AgentPromptTemplateNames.DECISION_SYSTEM])
    }

    @Test
    fun `save upserts agent config and agent scoped prompts`() {
        val dao = FakeRuntimeStateDao()
        val repository = AgentProfileRepository(dao, clockMillis = { 123L })

        val saved =
                runBlocking {
                    repository.save(
                            EditableAgentProfile(
                                    agentId = " agent-a ",
                                    displayName = " Agent A ",
                                    persona = " persona ",
                                    provider = " openai ",
                                    model = " model-a ",
                                    settingsJson = """ {"temperature":0.7} """,
                                    prompts =
                                            mapOf(
                                                    AgentPromptTemplateNames.PLANNER_SYSTEM to
                                                            "planner",
                                                    AgentPromptTemplateNames.REPLIER_SYSTEM to
                                                            "system",
                                                    AgentPromptTemplateNames.REPLIER_USER to "user"
                                            )
                            )
                    )
                }

        assertEquals("agent-a", saved.agentId)
        assertEquals("Agent A", saved.displayName)
        assertEquals("persona", saved.persona)
        assertEquals("""{"temperature":0.7}""", saved.settingsJson)
        val config = dao.agentConfigs.getValue("agent-a")
        assertEquals(123L, config.updatedAtMillis)
        assertEquals("Agent A", config.displayName)
        assertEquals(AgentPromptTemplateNames.ALL.size, dao.promptTemplates.size)
        AgentPromptTemplateNames.ALL.forEach { name ->
            val template = dao.promptTemplates.getValue("agent:agent-a:$name")
            assertEquals("agent-a", template.agentId)
            assertEquals(name, template.name)
            assertEquals(123L, template.updatedAtMillis)
        }
        assertEquals("planner", dao.promptTemplates.getValue("agent:agent-a:planner_system").body)
        assertEquals("", dao.promptTemplates.getValue("agent:agent-a:decision_system").body)
    }

    @Test
    fun `export and import profile json round trips known fields`() {
        val repository = AgentProfileRepository(FakeRuntimeStateDao())
        val exported =
                repository.exportJson(
                        EditableAgentProfile(
                                agentId = "agent-a",
                                displayName = "Agent A",
                                persona = "persona",
                                provider = "openai",
                                model = "model-a",
                                settingsJson = """{"temperature":0.7}""",
                                prompts =
                                        mapOf(
                                                AgentPromptTemplateNames.PLANNER_SYSTEM to
                                                        "planner",
                                                AgentPromptTemplateNames.REPLIER_USER to "user"
                                        )
                        )
                )

        val root = JsonParser.parseString(exported).asJsonObject
        assertEquals(1, root["version"].asInt)
        assertEquals(
                0.7,
                root["agent"].asJsonObject["settings_json"]
                        .asJsonObject["temperature"]
                        .asDouble,
                0.0
        )

        val imported = repository.importJson(exported, targetAgentId = "agent-b")

        assertEquals("agent-b", imported.agentId)
        assertEquals("Agent A", imported.displayName)
        assertEquals("persona", imported.persona)
        assertEquals("openai", imported.provider)
        assertEquals("model-a", imported.model)
        assertEquals("""{"temperature":0.7}""", imported.settingsJson)
        assertEquals("planner", imported.prompts[AgentPromptTemplateNames.PLANNER_SYSTEM])
        assertEquals("user", imported.prompts[AgentPromptTemplateNames.REPLIER_USER])
        assertEquals("", imported.prompts[AgentPromptTemplateNames.DECISION_SYSTEM])
    }

    @Test
    fun `import rejects invalid profile json`() {
        val repository = AgentProfileRepository(FakeRuntimeStateDao())

        val error =
                runCatching { repository.importJson("""{"agent":{"display_name":"Agent"}}""") }
                        .exceptionOrNull()

        assertNotNull(error)
        assertEquals(IllegalArgumentException::class.java, error!!::class.java)
    }

    private fun prompt(
            templateId: String,
            agentId: String?,
            name: String,
            body: String,
            updatedAtMillis: Long
    ): PromptTemplateEntity =
            PromptTemplateEntity(
                    templateId = templateId,
                    agentId = agentId,
                    name = name,
                    body = body,
                    updatedAtMillis = updatedAtMillis
            )

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
                promptTemplates.values
                        .filter { it.name == name && (it.agentId == agentId || it.agentId == null) }
                        .sortedWith(
                                compareBy<PromptTemplateEntity> {
                                            if (it.agentId == agentId) 0 else 1
                                        }
                                        .thenByDescending { it.updatedAtMillis }
                        )
                        .firstOrNull()

        override suspend fun queryMemories(
                contextId: String,
                agentId: String,
                limit: Int
        ): List<MemoryEntity> = emptyList()

        override suspend fun searchMemories(
                contextId: String,
                agentId: String,
                query: String,
                limit: Int
        ): List<MemoryEntity> = emptyList()

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
    }
}
