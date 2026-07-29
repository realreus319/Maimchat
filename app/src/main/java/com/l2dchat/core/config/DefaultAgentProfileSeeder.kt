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

/**
 * Seeds a PERSONA's profile + prompts into the DB. The [agentId] doubles as the asset-bundle id: the
 * agent.json + prompts are read from `assets/agents/<agentId>/`, and BOTH the agent config and the
 * prompt templates are scoped to that agentId — so each persona has its own prompts (the
 * `queryPromptTemplate` query prefers an agent-scoped row over the null default). Re-seeds the untouched
 * default (updatedAtMillis == 0) but never clobbers a row the user has edited.
 */
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
        val profile = source.readProfile(normalizedAgentId)

        val existingConfig = stateDao.queryAgentConfig(normalizedAgentId)
        val desiredConfig =
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
        if (existingConfig == null ||
                        (existingConfig.updatedAtMillis == DEFAULT_UPDATED_AT &&
                                existingConfig != desiredConfig)
        ) {
            stateDao.upsertAgentConfig(desiredConfig)
        }

        PROMPT_NAMES.forEach { name ->
            val body =
                    source.readText("agents/$normalizedAgentId/prompts/$name.md")
                            ?.trim()
                            ?.takeIf { it.isNotBlank() }
                            ?: return@forEach
            val templateId = "${normalizedAgentId}_$name"
            val existing = stateDao.queryPromptTemplateById(templateId)
            if (existing != null &&
                            (existing.updatedAtMillis != DEFAULT_UPDATED_AT || existing.body == body)
            ) {
                // User-edited (non-zero) or already up to date — leave it alone.
                return@forEach
            }
            stateDao.upsertPromptTemplate(
                    PromptTemplateEntity(
                            templateId = templateId,
                            agentId = normalizedAgentId,
                            name = name,
                            body = body,
                            updatedAtMillis = DEFAULT_UPDATED_AT
                    )
            )
        }
    }

    private fun DefaultAgentProfileSource.readProfile(bundleId: String): DefaultAgentProfile {
        val json =
                readText("agents/$bundleId/agent.json")?.trim()?.takeIf { it.isNotBlank() }
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

    private val PROMPT_NAMES =
            listOf(
                    AgentPromptTemplateNames.PLANNER_SYSTEM,
                    AgentPromptTemplateNames.DECISION_SYSTEM,
                    AgentPromptTemplateNames.REPLIER_SYSTEM,
                    AgentPromptTemplateNames.REPLIER_USER
            )

    private const val DEFAULT_UPDATED_AT = 0L
    private val GSON = Gson()
}

private fun String?.trimmedOrNull(): String? = this?.trim()?.takeIf { it.isNotEmpty() }
