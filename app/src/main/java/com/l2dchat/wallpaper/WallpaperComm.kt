package com.l2dchat.wallpaper

object WallpaperComm {
    const val ACTION_SEND_MESSAGE = "com.l2dchat.wallpaper.ACTION_SEND_MESSAGE"
    const val EXTRA_MESSAGE_TEXT = "extra_message_text"
    const val ACTION_REFRESH_BACKGROUND = "com.l2dchat.wallpaper.ACTION_REFRESH_BACKGROUND"
    const val EXTRA_BACKGROUND_PATH = "extra_background_path"
    const val ACTION_REFRESH_MODEL = "com.l2dchat.wallpaper.ACTION_REFRESH_MODEL"
    const val EXTRA_MODEL_FOLDER = "extra_model_folder"
    const val ACTION_REFRESH_BUBBLE_COUNT = "com.l2dchat.wallpaper.ACTION_REFRESH_BUBBLE_COUNT"
    const val EXTRA_BUBBLE_COUNT = "extra_bubble_count"

    // How many recent chat bubbles the live wallpaper keeps pinned at the bottom (configurable).
    const val PREF_WALLPAPER_BUBBLE_COUNT = "wallpaper_bubble_count"
    const val DEFAULT_BUBBLE_COUNT = 5
    const val MIN_BUBBLE_COUNT = 1
    const val MAX_BUBBLE_COUNT = 12

    const val PREF_WIDGET_INPUT = "widget_input"
    const val PREF_WIDGET_LAST_INPUT_KEY = "last_input"
    const val PREF_WALLPAPER = "wallpaper_prefs"
    const val PREF_WALLPAPER_BG_PATH = "bg_path"
    const val PREF_WALLPAPER_MODEL_FOLDER = "model_folder"
    const val PREF_WALLPAPER_VISIBLE = "wallpaper_visible"
    const val PREF_WALLPAPER_VISIBLE_UPDATED_AT = "wallpaper_visible_updated_at"
    const val PREF_WALLPAPER_INTERACTION_TYPE = "wallpaper_interaction_type"
    const val PREF_WALLPAPER_INTERACTION_X = "wallpaper_interaction_x"
    const val PREF_WALLPAPER_INTERACTION_Y = "wallpaper_interaction_y"
    const val PREF_WALLPAPER_INTERACTION_TIMESTAMP = "wallpaper_interaction_timestamp"
    const val PREF_WALLPAPER_SURFACE_WIDTH = "wallpaper_surface_width"
    const val PREF_WALLPAPER_SURFACE_HEIGHT = "wallpaper_surface_height"
    const val PREF_WALLPAPER_SURFACE_UPDATED_AT = "wallpaper_surface_updated_at"
}
