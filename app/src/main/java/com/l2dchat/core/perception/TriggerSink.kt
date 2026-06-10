package com.l2dchat.core.perception

import com.l2dchat.core.trigger.Trigger

interface TriggerSink {
    suspend fun submit(trigger: Trigger)
}
