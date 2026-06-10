package com.l2dchat

import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.assertIsDisplayed
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
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
        composeRule.onNodeWithText("没有找到可用的Live2D模型").assertIsDisplayed()
        composeRule.onNodeWithText("选择模型").assertIsDisplayed()
    }
}
