package com.l2dchat.chat

/**
 * One-to-many mapping from the character's MOOD (valence/arousal) to Live2D motions, played when the
 * character REPLIES. Each mood CATEGORY maps to SEVERAL candidate motions; one is picked at random.
 *
 * Rules:
 *  - Mood reactions do NOT use Idle motions (those are the passive breathing loop). When the character
 *    has a real mood, it plays a deliberate non-idle motion.
 *  - When there is no (meaningful) mood, it plays from a dedicated CALM group instead.
 *
 * Per-model; "hiyori" provided to start. NOTE Hiyori only ships Idle[0..8] + a single TapBody[0] and no
 * expressions, so its only non-idle motion is TapBody — every active mood maps to it (no per-mood
 * variety is possible until the model has more non-idle/emotion motions), while CALM draws from gentle
 * Idle motions. Index N = the Nth file of that group in Hiyori.model3.json.
 */
object MoodMotionMap {

    data class MoodMotion(val group: String, val index: Int)

    enum class MoodCategory { HAPPY, CONTENT, SAD, UPSET, CALM }

    /** Coarse category from a (decayed) valence∈[-1,1] / arousal∈[0,1]. A weak/absent mood → CALM. */
    fun categoryOf(valence: Double, arousal: Double): MoodCategory = when {
        valence > 0.30 && arousal > 0.45 -> MoodCategory.HAPPY  // 开心 / 兴奋
        valence < -0.20 && arousal > 0.45 -> MoodCategory.UPSET // 烦躁 / 生气
        valence > 0.20 -> MoodCategory.CONTENT                   // 满足 / 愉悦
        valence < -0.20 -> MoodCategory.SAD                      // 低落 / 沮丧
        else -> MoodCategory.CALM                                // 中性 / 微弱 / 无心情
    }

    /** Category to use when there is no mood row at all. */
    val NO_MOOD: MoodCategory = MoodCategory.CALM

    private fun tap() = MoodMotion("TapBody", 0)
    private fun idle(i: Int) = MoodMotion("Idle", i)

    // Hiyori: active moods use the only non-idle motion (TapBody); CALM uses gentle idle motions.
    private val HIYORI: Map<MoodCategory, List<MoodMotion>> =
        mapOf(
            MoodCategory.HAPPY to listOf(tap()),
            MoodCategory.CONTENT to listOf(tap()),
            MoodCategory.SAD to listOf(tap()),
            MoodCategory.UPSET to listOf(tap()),
            MoodCategory.CALM to listOf(idle(0), idle(2), idle(6), idle(8)),
        )

    private val BY_MODEL: Map<String, Map<MoodCategory, List<MoodMotion>>> =
        mapOf("hiyori" to HIYORI)

    fun supports(modelKey: String?): Boolean = BY_MODEL.containsKey(modelKey?.lowercase())

    /** Pick one motion for [modelKey]'s [category] (random among the candidates), or null if unmapped. */
    fun pick(modelKey: String?, category: MoodCategory): MoodMotion? {
        val map = BY_MODEL[modelKey?.lowercase()] ?: return null
        val list = map[category]?.takeIf { it.isNotEmpty() }
            ?: map[MoodCategory.CALM]?.takeIf { it.isNotEmpty() }
            ?: return null
        return list.random()
    }
}
