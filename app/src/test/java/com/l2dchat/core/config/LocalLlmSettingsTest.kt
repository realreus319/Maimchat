package com.l2dchat.core.config

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
}
