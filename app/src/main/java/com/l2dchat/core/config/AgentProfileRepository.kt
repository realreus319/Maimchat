package com.l2dchat.core.config

import com.google.gson.GsonBuilder
import com.google.gson.JsonElement
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import com.google.gson.JsonPrimitive
import com.l2dchat.core.message.AgentConfigEntity
import com.l2dchat.core.message.PromptTemplateEntity
import com.l2dchat.core.storage.RuntimeStateDao

object AgentPromptTemplateNames {
    const val PLANNER_SYSTEM = "planner_system"
    const val DECISION_SYSTEM = "decision_system"
    const val REPLIER_SYSTEM = "replier_system"
    const val REPLIER_USER = "replier_user"

    val ALL =
            listOf(
                    PLANNER_SYSTEM,
                    DECISION_SYSTEM,
                    REPLIER_SYSTEM,
                    REPLIER_USER
            )
}

data class EditableAgentProfile(
        val agentId: String,
        val displayName: String,
        val persona: String? = null,
        val provider: String? = null,
        val model: String? = null,
        val settingsJson: String? = null,
        val prompts: Map<String, String> = emptyMap()
)

class AgentProfileRepository(
        private val stateDao: RuntimeStateDao,
        private val clockMillis: () -> Long = { System.currentTimeMillis() }
) {
    suspend fun load(agentId: String, fallbackDisplayName: String? = null): EditableAgentProfile {
        val normalizedAgentId = requireAgentId(agentId)
        val config = stateDao.queryAgentConfig(normalizedAgentId)
        val prompts =
                AgentPromptTemplateNames.ALL.associateWith { name ->
                    stateDao.queryPromptTemplate(name, normalizedAgentId)?.body.orEmpty()
                }
        return EditableAgentProfile(
                agentId = normalizedAgentId,
                displayName =
                        config?.displayName.trimmedProfileStringOrNull()
                                ?: fallbackDisplayName.trimmedProfileStringOrNull()
                                ?: normalizedAgentId,
                persona = config?.persona.trimmedProfileStringOrNull(),
                provider = config?.provider.trimmedProfileStringOrNull(),
                model = config?.model.trimmedProfileStringOrNull(),
                settingsJson = config?.settingsJson.trimmedProfileStringOrNull(),
                prompts = prompts
        )
    }

    suspend fun save(profile: EditableAgentProfile): EditableAgentProfile {
        val normalized = profile.normalized()
        val updatedAtMillis = clockMillis()
        stateDao.upsertAgentConfig(
                AgentConfigEntity(
                        agentId = normalized.agentId,
                        displayName = normalized.displayName,
                        persona = normalized.persona,
                        provider = normalized.provider,
                        model = normalized.model,
                        settingsJson = normalized.settingsJson,
                        updatedAtMillis = updatedAtMillis
                )
        )
        normalized.prompts.forEach { (name, body) ->
            stateDao.upsertPromptTemplate(
                    PromptTemplateEntity(
                            templateId = agentPromptTemplateId(normalized.agentId, name),
                            agentId = normalized.agentId,
                            name = name,
                            body = body,
                            updatedAtMillis = updatedAtMillis
                    )
            )
        }
        return normalized
    }

    fun exportJson(profile: EditableAgentProfile): String {
        val normalized = profile.normalized()
        val root =
                JsonObject().apply {
                    addProperty("version", EXPORT_VERSION)
                    add(
                            "agent",
                            JsonObject().apply {
                                addProperty("agent_id", normalized.agentId)
                                addProperty("display_name", normalized.displayName)
                                addNullableString("persona", normalized.persona)
                                addNullableString("provider", normalized.provider)
                                addNullableString("model", normalized.model)
                                normalized.settingsJson?.let {
                                    add("settings_json", it.toJsonElementOrString())
                                }
                            }
                    )
                    add(
                            "prompts",
                            JsonObject().apply {
                                normalized.prompts.forEach { (name, body) ->
                                    addProperty(name, body)
                                }
                            }
                    )
                }
        return GSON.toJson(root)
    }

    fun importJson(
            json: String,
            targetAgentId: String? = null,
            fallbackDisplayName: String? = null
    ): EditableAgentProfile {
        val root = parseRootObject(json)
        val agent = root.objectOrNull("agent") ?: root
        val normalizedAgentId =
                targetAgentId.trimmedProfileStringOrNull()
                        ?: agent.stringOrNull("agent_id")
                        ?: root.stringOrNull("agent_id")
                        ?: throw IllegalArgumentException("agent_id must not be blank")
        val displayName =
                agent.stringOrNull("display_name")
                        ?: root.stringOrNull("display_name")
                        ?: fallbackDisplayName.trimmedProfileStringOrNull()
                        ?: normalizedAgentId
        val promptsObject = root.objectOrNull("prompts") ?: root.objectOrNull("prompt_templates")
        val prompts =
                AgentPromptTemplateNames.ALL.associateWith { name ->
                    promptsObject?.stringOrEmpty(name).orEmpty()
                }
        return EditableAgentProfile(
                        agentId = normalizedAgentId,
                        displayName = displayName,
                        persona = agent.stringOrNull("persona") ?: root.stringOrNull("persona"),
                        provider = agent.stringOrNull("provider") ?: root.stringOrNull("provider"),
                        model = agent.stringOrNull("model") ?: root.stringOrNull("model"),
                        settingsJson =
                                (agent.get("settings_json") ?: root.get("settings_json"))
                                        ?.toSettingsJsonString(),
                        prompts = prompts
                )
                .normalized()
    }

    private fun EditableAgentProfile.normalized(): EditableAgentProfile {
        val normalizedAgentId = requireAgentId(agentId)
        val normalizedPrompts =
                AgentPromptTemplateNames.ALL.associateWith { name ->
                    prompts[name].orEmpty()
                }
        return copy(
                agentId = normalizedAgentId,
                displayName =
                        displayName.trimmedProfileStringOrNull()
                                ?: normalizedAgentId,
                persona = persona.trimmedProfileStringOrNull(),
                provider = provider.trimmedProfileStringOrNull(),
                model = model.trimmedProfileStringOrNull(),
                settingsJson = settingsJson.trimmedProfileStringOrNull(),
                prompts = normalizedPrompts
        )
    }

    private companion object {
        const val EXPORT_VERSION = 1
        val GSON = GsonBuilder().setPrettyPrinting().create()
    }
}

private fun requireAgentId(agentId: String): String =
        agentId.trim().takeIf { it.isNotEmpty() }
                ?: throw IllegalArgumentException("agentId must not be blank")

private fun agentPromptTemplateId(agentId: String, name: String): String = "agent:$agentId:$name"

private fun parseRootObject(json: String): JsonObject =
        try {
            val element = JsonParser.parseString(json)
            require(element != null && element.isJsonObject)
            element.asJsonObject
        } catch (error: Exception) {
            throw IllegalArgumentException("Invalid agent profile JSON", error)
        }

private fun JsonObject.addNullableString(name: String, value: String?) {
    if (value == null) {
        add(name, null)
    } else {
        addProperty(name, value)
    }
}

private fun JsonObject.objectOrNull(name: String): JsonObject? {
    val element = get(name) ?: return null
    return if (!element.isJsonNull && element.isJsonObject) element.asJsonObject else null
}

private fun JsonObject.stringOrNull(name: String): String? {
    val element = get(name) ?: return null
    if (element.isJsonNull || !element.isJsonPrimitive) return null
    val primitive = element.asJsonPrimitive
    return if (primitive.isString) primitive.asString.trimmedProfileStringOrNull() else null
}

private fun JsonObject.stringOrEmpty(name: String): String? {
    val element = get(name) ?: return null
    if (element.isJsonNull || !element.isJsonPrimitive) return null
    val primitive = element.asJsonPrimitive
    return if (primitive.isString) primitive.asString else null
}

private fun JsonElement.toSettingsJsonString(): String? =
        when {
            isJsonNull -> null
            isJsonPrimitive && asJsonPrimitive.isString -> asString.trimmedProfileStringOrNull()
            else -> toString().trimmedProfileStringOrNull()
        }

private fun String.toJsonElementOrString(): JsonElement =
        runCatching { JsonParser.parseString(this) }
                .getOrNull()
                ?.takeIf { !it.isJsonNull }
                ?: JsonPrimitive(this)

private fun String?.trimmedProfileStringOrNull(): String? =
        this?.trim()?.takeIf { it.isNotEmpty() }
