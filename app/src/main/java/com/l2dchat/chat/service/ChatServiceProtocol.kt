package com.l2dchat.chat.service

object ChatServiceProtocol {
    // Client -> Service commands
    const val MSG_REGISTER_CLIENT = 1
    const val MSG_UNREGISTER_CLIENT = 2
    const val MSG_CONNECT = 3
    const val MSG_DISCONNECT = 4
    const val MSG_SEND_MESSAGE = 5
    const val MSG_UPDATE_CONFIG = 6
    const val MSG_REQUEST_SNAPSHOT = 7
    const val MSG_CLEAR_MESSAGES = 8
    const val MSG_SET_ACTIVE_MODEL = 9
    const val MSG_CLEAR_MESSAGES_EPHEMERAL = 10
    const val MSG_START_LOCAL_RUNTIME = 11

    // Service -> Client events
    const val MSG_EVENT_CONNECTION_STATE = 101
    const val MSG_EVENT_NEW_MESSAGE = 102
    const val MSG_EVENT_SNAPSHOT = 103
    const val MSG_EVENT_ERROR = 104
    const val MSG_EVENT_STANDARD_MESSAGE = 105

    // Common extras
    const val EXTRA_URL = "extra_url"
    const val EXTRA_PLATFORM = "extra_platform"
    const val EXTRA_AUTH_TOKEN = "extra_auth_token"
    const val EXTRA_MESSAGE_TEXT = "extra_message_text"
    const val EXTRA_NICKNAME = "extra_nickname"
    const val EXTRA_RECEIVER_ID = "extra_receiver_id"
    const val EXTRA_RECEIVER_NICKNAME = "extra_receiver_nickname"
    const val EXTRA_MODEL_NAME = "extra_model_name"
    const val EXTRA_LOCAL_LLM_ENABLED = "extra_local_llm_enabled"
    const val EXTRA_LOCAL_LLM_BASE_URL = "extra_local_llm_base_url"
    const val EXTRA_LOCAL_LLM_API_KEY = "extra_local_llm_api_key"
    const val EXTRA_LOCAL_LLM_PLANNER_MODEL = "extra_local_llm_planner_model"
    const val EXTRA_LOCAL_LLM_REPLIER_MODEL = "extra_local_llm_replier_model"
    const val EXTRA_LOCAL_LLM_NATIVE_TOOL_CALLING = "extra_local_llm_native_tool_calling"
    const val EXTRA_LOCAL_LLM_TEMPERATURE = "extra_local_llm_temperature"
    const val EXTRA_LOCAL_LLM_MAX_TOKENS = "extra_local_llm_max_tokens"
    const val EXTRA_LOCAL_LLM_TIMEOUT_MILLIS = "extra_local_llm_timeout_millis"

    // Event extras
    const val EXTRA_CONNECTION_STATE = "extra_connection_state"
    const val EXTRA_CONNECTION_LABEL = "extra_connection_label"
    const val EXTRA_MESSAGE_ID = "extra_message_id"
    const val EXTRA_MESSAGE_CONTENT = "extra_message_content"
    const val EXTRA_MESSAGE_FROM_USER = "extra_message_from_user"
    const val EXTRA_MESSAGE_TIMESTAMP = "extra_message_timestamp"
    const val EXTRA_MESSAGE_BUNDLE_LIST = "extra_message_bundle_list"
    const val EXTRA_STANDARD_MESSAGE_LIST = "extra_standard_message_list"
    const val EXTRA_STANDARD_MESSAGE_JSON = "extra_standard_message_json"
    const val EXTRA_ERROR_MESSAGE = "extra_error_message"
}
