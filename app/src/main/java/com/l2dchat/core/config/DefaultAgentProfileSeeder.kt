package com.l2dchat.core.config

import android.content.Context
import com.google.gson.Gson
import com.google.gson.JsonObject
import com.l2dchat.core.message.AgentConfigEntity
import com.l2dchat.core.message.PromptTemplateEntity
import com.l2dchat.core.storage.RuntimeStateDao
import java.io.IOException

interface DefaultAgentProfileSource {
    fun readText(path: String): String?
}

class AssetDefaultAgentProfileSource(context: Context) : DefaultAgentProfileSource {
    private val assets = context.applicationContext.assets

    override fun readText(path: String): String? =
            try {
                assets.open(path).bufferedReader().use { it.readText() }
            } catch (_: IOException) {
                null
            }
}

object DefaultAgentProfileSeeder {
    suspend fun seed(
            context: Context,
            stateDao: RuntimeStateDao,
            agentId: String,
            displayName: String?
    ) {
        seed(
                source = AssetDefaultAgentProfileSource(context.applicationContext),
                stateDao = stateDao,
                agentId = agentId,
                displayName = displayName
        )
    }

    suspend fun seed(
            source: DefaultAgentProfileSource,
            stateDao: RuntimeStateDao,
            agentId: String,
            displayName: String?
    ) {
        val normalizedAgentId = agentId.trim().takeIf { it.isNotBlank() } ?: return
        val profile = source.readProfile()
        if (stateDao.queryAgentConfig(normalizedAgentId) == null) {
            stateDao.upsertAgentConfig(
                    AgentConfigEntity(
                            agentId = normalizedAgentId,
                            displayName =
                                    displayName.trimmedOrNull()
                                            ?: profile.displayName
                                            ?: normalizedAgentId,
                            persona = profile.persona,
                            provider = profile.provider,
                            model = profile.model,
                            settingsJson = profile.settingsJson,
                            updatedAtMillis = DEFAULT_UPDATED_AT
                    )
            )
        }

        DEFAULT_PROMPTS.forEach { prompt ->
            val body = source.readText(prompt.path)?.trim()?.takeIf { it.isNotBlank() }
                    ?: return@forEach
            stateDao.upsertPromptTemplate(
                    PromptTemplateEntity(
                            templateId = "default_${prompt.name}",
                            agentId = null,
                            name = prompt.name,
                            body = body,
                            updatedAtMillis = DEFAULT_UPDATED_AT
                    )
            )
        }
    }

    private fun DefaultAgentProfileSource.readProfile(): DefaultAgentProfile {
        val json = readText(AGENT_JSON_PATH)?.trim()?.takeIf { it.isNotBlank() }
                ?: return DefaultAgentProfile()
        val root = runCatching { GSON.fromJson(json, JsonObject::class.java) }.getOrNull()
                ?: return DefaultAgentProfile()
        return DefaultAgentProfile(
                displayName = root.stringProperty("display_name"),
                persona = root.stringProperty("persona"),
                provider = root.stringProperty("provider"),
                model = root.stringProperty("model"),
                settingsJson =
                        root.get("settings_json")
                                ?.takeIf { !it.isJsonNull }
                                ?.toString()
                                ?.takeIf { it != "null" }
        )
    }

    private fun JsonObject.stringProperty(name: String): String? {
        val element = get(name) ?: return null
        if (element.isJsonNull || !element.isJsonPrimitive) return null
        val primitive = element.asJsonPrimitive
        if (!primitive.isString) return null
        return primitive.asString.trimmedOrNull()
    }

    private data class DefaultAgentProfile(
            val displayName: String? = null,
            val persona: String? = null,
            val provider: String? = null,
            val model: String? = null,
            val settingsJson: String? = null
    )

    private data class DefaultPrompt(val name: String, val path: String)

    private val DEFAULT_PROMPTS =
            listOf(
                    DefaultPrompt(
                            AgentPromptTemplateNames.PLANNER_SYSTEM,
                            "agents/default/prompts/planner_system.md"
                    ),
                    DefaultPrompt(
                            AgentPromptTemplateNames.DECISION_SYSTEM,
                            "agents/default/prompts/decision_system.md"
                    ),
                    DefaultPrompt(
                            AgentPromptTemplateNames.REPLIER_SYSTEM,
                            "agents/default/prompts/replier_system.md"
                    ),
                    DefaultPrompt(
                            AgentPromptTemplateNames.REPLIER_USER,
                            "agents/default/prompts/replier_user.md"
                    )
            )

    private const val AGENT_JSON_PATH = "agents/default/agent.json"
    private const val DEFAULT_UPDATED_AT = 0L
    private val GSON = Gson()
}

private fun String?.trimmedOrNull(): String? = this?.trim()?.takeIf { it.isNotEmpty() }
