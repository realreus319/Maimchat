package com.l2dchat.chat

import com.l2dchat.core.context.RoutingKey
import com.l2dchat.core.trigger.TriggerPriority
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ChatEnvironmentTriggerEmitterTest {
    @Test
    fun `model change emits routed environment trigger with reply metadata`() {
        var now = 1_000L
        val emitter = ChatEnvironmentTriggerEmitter(clockMillis = { now })
        val context = triggerContext()

        val submissions = emitter.onModelChanged(context)

        assertEquals(1, submissions.size)
        val submission = submissions.single()
        assertEquals(context.routingKey, submission.routingKey)
        assertEquals(TriggerPriority.NORMAL, submission.priority)
        assertEquals("android_app", submission.source)
        assertEquals("model_changed", submission.metadata["event_type"])
        assertEquals("alice", submission.metadata["receiver_user_id"])
        assertEquals("shizuku", submission.metadata["agent_user_id"])
        assertEquals(1.0, submission.timestampSeconds, 0.0)

        now += 2_000L
        assertTrue(emitter.onModelChanged(context).isEmpty())
    }

    @Test
    fun `environment update emits app interaction and visual snapshot triggers with dedupe`() {
        var now = 10_000L
        val emitter = ChatEnvironmentTriggerEmitter(clockMillis = { now })
        val context = triggerContext()

        val appVisible =
                emitter.onEnvironmentUpdate(
                        ChatEnvironmentUpdate(appVisible = true, lifecycleState = "resumed"),
                        context
                )
        assertEquals(1, appVisible.size)
        assertEquals("app_visible", appVisible.single().metadata["event_type"])
        assertTrue(
                emitter
                        .onEnvironmentUpdate(
                                ChatEnvironmentUpdate(
                                        appVisible = true,
                                        lifecycleState = "resumed"
                                ),
                                context
                        )
                        .isEmpty()
        )

        now += 1_000L
        val interaction =
                emitter.onEnvironmentUpdate(
                        ChatEnvironmentUpdate(
                                interaction =
                                        ChatEnvironmentInteraction(
                                                type = "tap",
                                                x = 12.25f,
                                                y = 8.75f,
                                                timestampMillis = 11_000L
                                        )
                        ),
                        context
                )
        assertEquals(1, interaction.size)
        assertEquals("android_live2d", interaction.single().source)
        assertEquals("live2d_interaction", interaction.single().metadata["event_type"])
        assertEquals("tap", interaction.single().metadata["interaction_type"])

        now += 1_000L
        val visual =
                emitter.onEnvironmentUpdate(
                        ChatEnvironmentUpdate(
                                visualSnapshot =
                                        ChatEnvironmentVisualSnapshot(
                                                reference = "android_wallpaper:abc",
                                                mimeType =
                                                        "application/vnd.l2dchat.environment-snapshot+json",
                                                width = 1080,
                                                height = 1920,
                                                capturedAtMillis = 12_000L
                                        )
                        ),
                        context
                )
        assertEquals(1, visual.size)
        assertEquals("android_wallpaper", visual.single().source)
        assertEquals("visual_snapshot_ready", visual.single().metadata["event_type"])
        assertEquals("android_wallpaper:abc", visual.single().metadata["visual_snapshot_reference"])

        now += 20_000L
        assertTrue(
                emitter
                        .onEnvironmentUpdate(
                                ChatEnvironmentUpdate(
                                        visualSnapshot =
                                                ChatEnvironmentVisualSnapshot(
                                                        reference = "android_wallpaper:abc",
                                                        mimeType =
                                                                "application/vnd.l2dchat.environment-snapshot+json",
                                                        width = 1080,
                                                        height = 1920,
                                                        capturedAtMillis = 32_000L
                                                )
                                ),
                                context
                        )
                        .isEmpty()
        )
    }

    @Test
    fun `wallpaper interaction uses wallpaper source`() {
        val emitter = ChatEnvironmentTriggerEmitter(clockMillis = { 20_000L })
        val submissions =
                emitter.onEnvironmentUpdate(
                        ChatEnvironmentUpdate(
                                interaction =
                                        ChatEnvironmentInteraction(
                                                type = "wallpaper_touch",
                                                x = 4f,
                                                y = 5f,
                                                timestampMillis = 20_000L
                                        )
                        ),
                        triggerContext()
                )

        assertEquals(1, submissions.size)
        assertEquals("android_wallpaper", submissions.single().source)
        assertEquals("live2d_interaction", submissions.single().metadata["event_type"])
    }

    @Test
    fun `motion finished emits live2d trigger metadata`() {
        var now = 30_000L
        val emitter = ChatEnvironmentTriggerEmitter(clockMillis = { now })
        val context = triggerContext()

        val submissions =
                emitter.onMotionFinished(
                        ChatEnvironmentMotionFinished(
                                group = "TapBody",
                                index = 1,
                                filePath = "mao/TapBody_01.motion3.json",
                                timestampMillis = 30_100L
                        ),
                        context
                )

        assertEquals(1, submissions.size)
        val submission = submissions.single()
        assertEquals("android_live2d", submission.source)
        assertEquals("motion_finished", submission.metadata["event_type"])
        assertEquals("TapBody", submission.metadata["motion_group"])
        assertEquals(1, submission.metadata["motion_index"])
        assertEquals("mao/TapBody_01.motion3.json", submission.metadata["motion_file_path"])
        assertEquals(30_100L, submission.metadata["motion_finished_at_millis"])

        now += 600L
        val repeat =
                emitter.onMotionFinished(
                        ChatEnvironmentMotionFinished(
                                group = "TapBody",
                                index = 1,
                                timestampMillis = 30_700L
                        ),
                        context
                )
        assertEquals(1, repeat.size)
    }

    private fun triggerContext(): ChatEnvironmentTriggerContext =
            ChatEnvironmentTriggerContext(
                    routingKey = RoutingKey(contextId = "room-shizuku", agentId = "shizuku"),
                    modelKey = "shizuku",
                    modelName = "Shizuku",
                    userId = "alice",
                    userName = "Alice",
                    agentUserId = "shizuku",
                    agentName = "Shizuku"
            )
}
