package com.l2dchat.core.storage

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import com.l2dchat.core.message.AgentConfigEntity
import com.l2dchat.core.message.ImpressionEntity
import com.l2dchat.core.message.MediaBlockEntity
import com.l2dchat.core.message.MemoryEntity
import com.l2dchat.core.message.MoodStateEntity
import com.l2dchat.core.message.PlannerMessageEntity
import com.l2dchat.core.message.PlannerRoundEntity
import com.l2dchat.core.message.PromptTemplateEntity
import com.l2dchat.core.message.StandardMessageEntity
import com.l2dchat.core.message.ToolTaskEntity
import com.l2dchat.core.message.VisibleMessageEntity

@Database(
        entities =
                [
                        VisibleMessageEntity::class,
                        StandardMessageEntity::class,
                        PlannerRoundEntity::class,
                        PlannerMessageEntity::class,
                        AgentConfigEntity::class,
                        PromptTemplateEntity::class,
                        ToolTaskEntity::class,
                        MemoryEntity::class,
                        ImpressionEntity::class,
                        MoodStateEntity::class,
                        MediaBlockEntity::class
                ],
        version = 1,
        exportSchema = true
)
abstract class ChatDatabase : RoomDatabase() {
    abstract fun runtimeMessageDao(): RuntimeMessageDao

    abstract fun plannerStateDao(): PlannerStateDao

    abstract fun runtimeStateDao(): RuntimeStateDao

    companion object {
        private const val DATABASE_NAME = "maimchat_runtime.db"

        @Volatile private var instance: ChatDatabase? = null

        fun getInstance(context: Context): ChatDatabase =
                instance
                        ?: synchronized(this) {
                            instance
                                    ?: Room.databaseBuilder(
                                                    context.applicationContext,
                                                    ChatDatabase::class.java,
                                                    DATABASE_NAME
                                            )
                                            .build()
                                            .also { instance = it }
                        }
    }
}
