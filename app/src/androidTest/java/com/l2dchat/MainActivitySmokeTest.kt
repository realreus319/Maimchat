package com.l2dchat

import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithContentDescription
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextClearance
import androidx.compose.ui.test.performTextInput
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import org.junit.Assume.assumeTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@LargeTest
@RunWith(AndroidJUnit4::class)
class MainActivitySmokeTest {
    @get:Rule val composeRule = createAndroidComposeRule<MainActivity>()

    @Test
    fun mainActivityStartsAndComposesRoot() {
        composeRule.waitForIdle()
        composeRule.onRoot().assertExists("Compose root should exist")
        if (hasPackagedLive2DModels()) {
            waitForNodeWithContentDescription("更多操作")
            composeRule.onNodeWithContentDescription("更多操作").assertIsDisplayed()
        } else {
            waitForNodeWithText("没有找到可用的Live2D模型")
            composeRule.onNodeWithText("没有找到可用的Live2D模型").assertIsDisplayed()
            composeRule.onNodeWithText("选择模型").assertIsDisplayed()
        }
    }

    @Test
    fun agentProfileDialogOpensAndSavesForSelectedModel() {
        assumeTrue(
                "Requires packaged Live2D model assets",
                hasPackagedLive2DModels()
        )

        waitForNodeWithContentDescription("更多操作")
        composeRule.onNodeWithContentDescription("更多操作").performClick()
        waitForNodeWithText("角色配置")
        composeRule.onNodeWithText("角色配置").performClick()

        waitForNodeWithTag("agent-profile-dialog")
        composeRule.onNodeWithText("角色配置").assertIsDisplayed()
        composeRule.onNodeWithTag("agent-profile-agent-id").assertIsDisplayed()
        composeRule.onNodeWithTag("agent-profile-display-name").assertIsDisplayed()
        composeRule.onNodeWithTag("agent-profile-persona").assertIsDisplayed()
        composeRule.onNodeWithTag("agent-profile-provider").assertIsDisplayed()
        composeRule.onNodeWithTag("agent-profile-model").assertIsDisplayed()
        composeRule.onNodeWithTag("agent-profile-settings-json").assertIsDisplayed()
        waitUntilNodeIsEnabled("agent-profile-save")

        composeRule.onNodeWithTag("agent-profile-display-name").performTextClearance()
        composeRule.onNodeWithTag("agent-profile-display-name").performTextInput("Haru UI Test")
        composeRule.onNodeWithTag("agent-profile-save").performClick()

        waitUntilNodeIsEnabled("agent-profile-save")
        composeRule.onNodeWithText("关闭").performClick()
        composeRule.waitUntil(timeoutMillis = 10_000) {
            !hasNodeWithTag("agent-profile-dialog")
        }
    }

    private fun waitForNodeWithText(text: String) {
        composeRule.waitUntil(timeoutMillis = 10_000) { hasNodeWithText(text) }
    }

    private fun waitForNodeWithTag(tag: String) {
        composeRule.waitUntil(timeoutMillis = 10_000) { hasNodeWithTag(tag) }
    }

    private fun waitForNodeWithContentDescription(contentDescription: String) {
        composeRule.waitUntil(timeoutMillis = 15_000) {
            hasNodeWithContentDescription(contentDescription)
        }
    }

    private fun waitUntilNodeIsEnabled(tag: String) {
        composeRule.waitUntil(timeoutMillis = 10_000) {
            runCatching {
                        composeRule.onNodeWithTag(tag).assertIsEnabled()
                    }
                    .isSuccess
        }
    }

    private fun hasNodeWithText(text: String): Boolean =
            composeRule.onAllNodesWithText(text).fetchSemanticsNodes().isNotEmpty()

    private fun hasNodeWithTag(tag: String): Boolean =
            composeRule.onAllNodesWithTag(tag).fetchSemanticsNodes().isNotEmpty()

    private fun hasNodeWithContentDescription(contentDescription: String): Boolean =
            composeRule
                    .onAllNodesWithContentDescription(contentDescription)
                    .fetchSemanticsNodes()
                    .isNotEmpty()

    private fun hasPackagedLive2DModels(): Boolean {
        val assets = composeRule.activity.assets
        return runCatching {
                    assets.list("").orEmpty().any { folderName ->
                        assets.list(folderName).orEmpty().any { it.endsWith(".model3.json") }
                    }
                }
                .getOrDefault(false)
    }
}
