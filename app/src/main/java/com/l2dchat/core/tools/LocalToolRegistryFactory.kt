package com.l2dchat.core.tools

import com.google.gson.Gson
import com.l2dchat.core.environment.EmptyEnvironmentStateProvider
import com.l2dchat.core.environment.EnvironmentStateProvider
import com.l2dchat.core.environment.MotionController
import com.l2dchat.core.environment.NoopMotionController
import com.l2dchat.core.storage.RuntimeStateDao

object LocalToolRegistryFactory {
    fun normalTools(
            gson: Gson = Gson(),
            environmentStateProvider: EnvironmentStateProvider = EmptyEnvironmentStateProvider,
            motionController: MotionController = NoopMotionController,
            runtimeStateDao: RuntimeStateDao? = null,
            replierTaskManager: ReplierTaskManager? = null,
            replierTaskIdFactory: ((ToolExecutionContext) -> String)? = null,
            extraTools: List<Tool> = emptyList()
    ): List<Tool> {
        val tools =
                mutableListOf<Tool>(
                        ReplierTool(
                                gson = gson,
                                taskManager = replierTaskManager,
                                taskIdFactory = replierTaskIdFactory
                        )
                )
        if (replierTaskManager != null) {
            tools += WaitForTool(taskManager = replierTaskManager, gson = gson)
        }
        tools += GetWorldStateTool(stateProvider = environmentStateProvider, gson = gson)
        tools += LookAtTool(stateProvider = environmentStateProvider, gson = gson)
        tools += TriggerMotionTool(motionController = motionController, gson = gson)
        if (runtimeStateDao != null) {
            tools += MemoryStoreTool(stateDao = runtimeStateDao, gson = gson)
            tools += MemorySearchTool(stateDao = runtimeStateDao, gson = gson)
            tools += GetUserImpressionsTool(stateDao = runtimeStateDao, gson = gson)
            tools += UpdateUserImpressionTool(stateDao = runtimeStateDao, gson = gson)
            tools += QueryImpressionTool(stateDao = runtimeStateDao, gson = gson)
            tools += UpdateMoodStateTool(stateDao = runtimeStateDao, gson = gson)
        }
        tools += extraTools
        return tools
    }

    fun normalRegistry(
            gson: Gson = Gson(),
            environmentStateProvider: EnvironmentStateProvider = EmptyEnvironmentStateProvider,
            motionController: MotionController = NoopMotionController,
            runtimeStateDao: RuntimeStateDao? = null,
            replierTaskManager: ReplierTaskManager? = null,
            replierTaskIdFactory: ((ToolExecutionContext) -> String)? = null,
            extraTools: List<Tool> = emptyList()
    ): ToolRegistry =
            ToolRegistry(
                    normalTools(
                            gson = gson,
                            environmentStateProvider = environmentStateProvider,
                            motionController = motionController,
                            runtimeStateDao = runtimeStateDao,
                            replierTaskManager = replierTaskManager,
                            replierTaskIdFactory = replierTaskIdFactory,
                            extraTools = extraTools
                    )
            )

    fun decisionTools(
            taskManager: ReplierTaskManager,
            gson: Gson = Gson()
    ): List<Tool> =
            DecisionTools.defaultTools(taskManager = taskManager, gson = gson)
                    .filter { ToolExecutionMode.DECISION in it.allowedModes }

    fun decisionRegistry(
            taskManager: ReplierTaskManager,
            gson: Gson = Gson()
    ): ToolRegistry = ToolRegistry(decisionTools(taskManager = taskManager, gson = gson))
}
