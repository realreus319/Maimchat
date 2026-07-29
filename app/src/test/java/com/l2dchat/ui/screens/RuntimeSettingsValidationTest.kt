package com.l2dchat.ui.screens

import com.l2dchat.core.config.LocalLlmSettings
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class RuntimeSettingsValidationTest {
    @Test
    fun `local mode allows empty provider config for fixed reply runtime`() {
        val errors =
                RuntimeSettingsValidation.validateConfig(
                        localLlmSettings = LocalLlmSettings(enabled = true)
                )

        assertTrue(errors.isEmpty())
    }

    @Test
    fun `local mode requires complete provider config when partially configured`() {
        val plannerOnlyErrors =
                RuntimeSettingsValidation.validateConfig(
                        localLlmSettings =
                                LocalLlmSettings(
                                        enabled = true,
                                        plannerModel = "planner-model"
                                )
                )
        val endpointOnlyErrors =
                RuntimeSettingsValidation.validateConfig(
                        localLlmSettings =
                                LocalLlmSettings(
                                        enabled = true,
                                        baseUrl = "https://api.example.com/v1"
                                )
                )

        assertEquals(listOf("LLM Endpoint 不能为空"), plannerOnlyErrors)
        assertEquals(listOf("Planner 模型不能为空"), endpointOnlyErrors)
    }

    @Test
    fun `local mode treats api key as provider input`() {
        val errors =
                RuntimeSettingsValidation.validateConfig(
                        localLlmSettings =
                                LocalLlmSettings(enabled = true, apiKey = "secret")
                )

        assertEquals(listOf("LLM Endpoint 不能为空", "Planner 模型不能为空"), errors)
    }

    @Test
    fun `local mode validates endpoint scheme when provider is configured`() {
        val errors =
                RuntimeSettingsValidation.validateConfig(
                        localLlmSettings =
                                LocalLlmSettings(
                                        enabled = true,
                                        baseUrl = "api.example.com/v1",
                                        plannerModel = "planner-model"
                                )
                )

        assertEquals(listOf("LLM Endpoint 必须以 http:// 或 https:// 开头"), errors)
    }

    @Test
    fun `disabled provider stays in local runtime and does not require websocket endpoint`() {
        val errors =
                RuntimeSettingsValidation.validateConfig(
                        localLlmSettings = LocalLlmSettings(enabled = false)
                )

        assertTrue(errors.isEmpty())
    }
}
