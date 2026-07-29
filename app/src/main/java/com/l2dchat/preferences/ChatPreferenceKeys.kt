package com.l2dchat.preferences

object ChatPreferenceKeys {
    const val PREFS_NAME: String = "chat_prefs"
    const val SELECTED_MODEL_FOLDER: String = "selected_model_folder"
    // The active PERSONA id (== routing agentId). Decoupled from SELECTED_MODEL_FOLDER (the Live2D
    // avatar) so personas can share one avatar while each keeps its own history/memory/mood/prompts.
    const val SELECTED_PERSONA: String = "selected_persona"
}
