package com.l2dchat.core.config

import com.l2dchat.core.message.AgentConfigEntity
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class LocalLlmSettingsTest {
    @Test
    fun `disabled settings do not create runtime config`() {
        val settings =
                LocalLlmSettings(
                        enabled = false,
                        baseUrl = "https://api.example.com/v1",
                        plannerModel = "planner"
                )

        assertNull(settings.toRuntimeConfig())
    }

    @Test
    fun `enabled settings require base url and planner model`() {
        assertNull(LocalLlmSettings(enabled = true, plannerModel = "planner").toRuntimeConfig())
        assertNull(
                LocalLlmSettings(
                                enabled = true,
                                baseUrl = "https://api.example.com/v1"
                        )
                        .toRuntimeConfig()
        )
    }

    @Test
    fun `enabled settings build planner and replier generation configs`() {
        val config =
                LocalLlmSettings(
                                enabled = true,
                                baseUrl = " https://api.example.com/v1 ",
                                apiKey = " key ",
                                plannerModel = " planner-model ",
                                replierModel = " replier-model ",
                                nativeToolCalling = false,
                                temperature = 0.7,
                                maxTokens = 512,
                                timeoutMillis = 30_000L
                        )
                        .toRuntimeConfig()

        checkNotNull(config)
        assertEquals("planner-model", config.plannerConfig.model)
        assertEquals("replier-model", config.replierConfig.model)
        assertEquals(0.7, config.plannerConfig.temperature)
        assertEquals(512, config.plannerConfig.maxTokens)
        assertEquals(30_000L, config.plannerConfig.timeoutMillis)
        assertFalse(config.nativeToolCalling)
    }

    @Test
    fun `replier model defaults to planner model`() {
        val config =
                LocalLlmSettings(
                                enabled = true,
                                baseUrl = "https://api.example.com/v1",
                                plannerModel = "planner-model"
                        )
                        .toRuntimeConfig()

        checkNotNull(config)
        assertEquals("planner-model", config.replierConfig.model)
        assertTrue(config.nativeToolCalling)
    }

    @Test
    fun `string representation redacts api key`() {
        val text =
                LocalLlmSettings(
                                enabled = true,
                                baseUrl = "https://api.example.com/v1",
                                apiKey = "secret-key",
                                plannerModel = "planner-model"
                        )
                        .toString()

        assertFalse(text.contains("secret-key"))
        assertTrue(text.contains("apiKey=<redacted>"))
    }

    @Test
    fun `agent model overrides planner and replier models`() {
        val override =
                AgentLlmSettingsOverride.fromAgentConfig(agentConfig(model = " profile-model "))
        val config =
                LocalLlmSettings(
                                enabled = true,
                                baseUrl = "https://api.example.com/v1",
                                plannerModel = "global-planner",
                                replierModel = "global-replier"
                        )
                        .toRuntimeConfig(agentOverride = override)

        checkNotNull(config)
        assertEquals("profile-model", config.plannerConfig.model)
        assertEquals("profile-model", config.replierConfig.model)
    }

    @Test
    fun `agent settings json overrides runtime generation options`() {
        val override =
                agentConfig(
                                model = "profile-model",
                                settingsJson =
                                        """
                                        {
                                          "planner_model": "agent-planner",
                                          "replier_model": "agent-replier",
                                          "base_url": "https://agent.example.com/v1",
                                          "native_tool_calling": false,
                                          "temperature": 0.25,
                                          "max_tokens": 128,
                                          "timeout_millis": 15000
                                        }
                                        """
                        )
                        .let { AgentLlmSettingsOverride.fromAgentConfig(it) }
        val config =
                LocalLlmSettings(
                                enabled = true,
                                plannerModel = "global-planner",
                                replierModel = "global-replier",
                                temperature = 0.9,
                                maxTokens = 512,
                                timeoutMillis = 60_000L
                        )
                        .toRuntimeConfig(agentOverride = override)

        checkNotNull(config)
        assertEquals("agent-planner", config.plannerConfig.model)
        assertEquals("agent-replier", config.replierConfig.model)
        assertEquals(0.25, config.plannerConfig.temperature)
        assertEquals(128, config.plannerConfig.maxTokens)
        assertEquals(15_000L, config.plannerConfig.timeoutMillis)
        assertFalse(config.nativeToolCalling)
    }

    @Test
    fun `agent provider url can supply runtime base url`() {
        val override =
                AgentLlmSettingsOverride.fromAgentConfig(
                        agentConfig(provider = " https://provider.example.com/v1 ", model = "profile")
                )

        val config =
                LocalLlmSettings(enabled = true)
                        .toRuntimeConfig(agentOverride = override)

        checkNotNull(config)
        assertEquals("profile", config.plannerConfig.model)
    }

    @Test
    fun `invalid agent settings json and unsafe values are ignored`() {
        assertNull(
                AgentLlmSettingsOverride.fromAgentConfig(agentConfig(settingsJson = "{not-json"))
        )

        val override =
                agentConfig(
                                model = "profile-model",
                                settingsJson =
                                        """
                                        {
                                          "temperature": 9,
                                          "max_tokens": 0,
                                          "timeout_millis": -1
                                        }
                                        """
                        )
                        .let { AgentLlmSettingsOverride.fromAgentConfig(it) }
        val config =
                LocalLlmSettings(
                                enabled = true,
                                baseUrl = "https://api.example.com/v1",
                                plannerModel = "global-planner",
                                temperature = 0.7,
                                maxTokens = 512,
                                timeoutMillis = 60_000L
                        )
                        .toRuntimeConfig(agentOverride = override)

        checkNotNull(config)
        assertEquals("profile-model", config.plannerConfig.model)
        assertEquals(0.7, config.plannerConfig.temperature)
        assertEquals(512, config.plannerConfig.maxTokens)
        assertEquals(60_000L, config.plannerConfig.timeoutMillis)
    }

    private fun agentConfig(
            provider: String? = null,
            model: String? = null,
            settingsJson: String? = null
    ): AgentConfigEntity =
            AgentConfigEntity(
                    agentId = "agent",
                    displayName = "Agent",
                    persona = null,
                    provider = provider,
                    model = model,
                    settingsJson = settingsJson,
                    updatedAtMillis = 1L
            )
}
