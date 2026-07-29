package com.l2dchat.core.perception

import com.l2dchat.chat.BaseMessageInfo
import com.l2dchat.chat.GroupInfo
import com.l2dchat.chat.MessageBase
import com.l2dchat.chat.ReceiverInfo
import com.l2dchat.chat.Seg
import com.l2dchat.chat.SenderInfo
import com.l2dchat.chat.UserInfo
import com.l2dchat.core.inbound.InboundBuilder
import com.l2dchat.core.trigger.Trigger
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.Assert.assertEquals
import org.junit.Test

class PerceptionDispatcherTest {
    @Test
    fun `submit creates one worker per routing key and emits triggers`() {
        val sink = CollectingTriggerSink(expectedCount = 2)

        runBlocking {
            val dispatcher = PerceptionDispatcher(scope = this, triggerSink = sink)
            dispatcher.submit(inbound("msg-1", "group-a", "agent-a", "hello"))
            dispatcher.submit(inbound("msg-2", "group-b", "agent-b", "world"))

            val triggers = withTimeout(1_000L) { sink.await() }

            assertEquals(2, dispatcher.workerCount)
            assertEquals(setOf("group-a:agent-a", "group-b:agent-b"), triggers.map { it.loopId() }.toSet())
            dispatcher.stopAndDrain()
        }
    }

    @Test
    fun `worker persists message before submitting trigger`() {
        val order = mutableListOf<String>()
        val store =
                object : PerceptionStore {
                    override suspend fun persist(parsedMessage: ParsedMessage) {
                        order.add("persist:${parsedMessage.messageId}")
                    }
                }
        val sink =
                object : TriggerSink {
                    val trigger = CompletableDeferred<Trigger>()

                    override suspend fun submit(trigger: Trigger) {
                        order.add("trigger:${trigger.messageId}")
                        this.trigger.complete(trigger)
                    }
                }

        runBlocking {
            val dispatcher =
                    PerceptionDispatcher(
                            scope = this,
                            triggerSink = sink,
                            storeFactory = { store }
                    )
            dispatcher.submit(inbound("msg-1", "group-a", "agent-a", "hello"))

            withTimeout(1_000L) { sink.trigger.await() }

            assertEquals(listOf("persist:msg-1", "trigger:msg-1"), order)
            dispatcher.stopAndDrain()
        }
    }

    private fun inbound(
            messageId: String,
            groupId: String,
            agentId: String,
            text: String
    ) = InboundBuilder().fromMessageBase(message(messageId, groupId, agentId, text))

    private fun message(
            messageId: String,
            groupId: String,
            agentId: String,
            text: String
    ): MessageBase =
            MessageBase(
                    messageInfo =
                            BaseMessageInfo(
                                    messageId = messageId,
                                    time = 1000.0,
                                    senderInfo =
                                            SenderInfo(
                                                    groupInfo =
                                                            GroupInfo(
                                                                    groupId = groupId
                                                            ),
                                                    userInfo =
                                                            UserInfo(
                                                                    userId = "user-id",
                                                                    userNickname = "Alice"
                                                            )
                                            ),
                                    receiverInfo =
                                            ReceiverInfo(
                                                    userInfo =
                                                            UserInfo(
                                                                    userId = agentId,
                                                                    userNickname = "Bot"
                                                            )
                                            )
                            ),
                    messageSegment = Seg("text", text),
                    rawMessage = text
            )

    private class CollectingTriggerSink(private val expectedCount: Int) : TriggerSink {
        private val triggers = mutableListOf<Trigger>()
        private val completed = CompletableDeferred<List<Trigger>>()

        override suspend fun submit(trigger: Trigger) {
            synchronized(triggers) {
                triggers.add(trigger)
                if (triggers.size == expectedCount && !completed.isCompleted) {
                    completed.complete(triggers.toList())
                }
            }
        }

        suspend fun await(): List<Trigger> = completed.await()
    }
}
