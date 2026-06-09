package com.l2dchat.core.inbound

data class ContentBlock(
        val type: String,
        val text: String? = null,
        val imageUrl: String? = null,
        val data: String? = null
) {
    fun toPayloadMap(): Map<String, Any> {
        val payload = linkedMapOf<String, Any>("type" to type)
        text?.let { payload["text"] = it }
        imageUrl?.let { payload["image_url"] = mapOf("url" to it) }
        data?.let { payload["data"] = it }
        return payload
    }

    companion object {
        fun text(value: String): ContentBlock = ContentBlock(type = "text", text = value)

        fun imageUrl(value: String): ContentBlock =
                ContentBlock(type = "image_url", imageUrl = value)

        fun opaque(type: String, data: String): ContentBlock =
                ContentBlock(type = type, data = data)
    }
}
