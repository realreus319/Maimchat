package com.l2dchat.core.mem

import java.time.LocalDateTime
import java.time.ZoneId

/**
 * Normalized event-time parsing — Kotlin port of `mem/timeparse.py`.
 *
 * The `event_time` string is an ISO 8601 + EDTF subset where precision is encoded
 * by string length:
 *
 * ```
 * 2025                      year precision
 * 2026-03                   month precision
 * 2026-08-08                day precision
 * 2026-08-08T15:00          minute precision
 * 2026-07-31/2026-08-02     interval (end day included, i.e. closed -> half-open)
 * 2025-12/..                open interval (ongoing), end = Long.MAX_VALUE
 * 2026-03~                  approximate (approx flag, interval still expanded)
 * ```
 *
 * Every value expands deterministically to a half-open interval `[startTs, endTs)`
 * so retrieval is always interval overlap.
 *
 * ## Timezone strategy
 *
 * The Python reference interprets naive date/time components as wall-clock time in
 * the process-local timezone and converts them to epoch seconds via `time.mktime`.
 * To keep this port byte-for-byte comparable with the Python output, the entry point
 * takes an explicit [ZoneId] (defaulting to [ZoneId.systemDefault]) and converts each
 * naive [LocalDateTime] via `atZone(zone).toEpochSecond()`. Callers that need to
 * reproduce Python `TZ=Asia/Shanghai` output should pass [ZoneId.of]`("Asia/Shanghai")`.
 *
 * ## `Long` vs Python `float`
 *
 * Epoch seconds are returned as [Long] throughout the Kotlin port (no `Double`/`Float`).
 * The Python sentinel `float("inf")` for open-ended intervals (`A/..`) is represented
 * as [Long.MAX_VALUE].
 *
 * @see parseEventTime
 */
object TimeParse {

    /**
     * A single time point: year, optionally -month, optionally -day, optionally `Thh:mm`.
     * Mirrors `_POINT_RE` in `timeparse.py`.
     */
    private val POINT_RE =
        Regex("^(\\d{4})(?:-(\\d{2})(?:-(\\d{2})(?:T(\\d{2}):(\\d{2}))?)?)?$")

    /** Precision names in increasing granularity, for error messages. */
    private val PRECISIONS = arrayOf("year", "month", "day", "minute")

    /**
     * Parse a normalized `event_time` string into a half-open epoch-second interval.
     *
     * Both interval endpoints expand at their own precision; the result start is the
     * left endpoint's start and the result end is the right endpoint's end (so a
     * day-precision right endpoint includes that whole day, turning the closed EDTF
     * interval into a half-open one). `A/..` means "ongoing" and yields
     * `endTs = Long.MAX_VALUE`. A trailing `~` only sets the approx flag; the
     * interval expands as usual.
     *
     * @param value  The normalized `event_time` string (see class doc).
     * @param zoneId Timezone used to interpret the naive wall-clock components.
     *               Defaults to [ZoneId.systemDefault] to mirror Python's `time.mktime`.
     * @return A [Triple] holding `(startTs, endTs, approx)`:
     *         - `first`  — inclusive start, epoch seconds ([Long]).
     *         - `second` — exclusive end, epoch seconds ([Long]); [Long.MAX_VALUE]
     *                      for open-ended (`A/..`).
     *         - `third`  — `true` when the input carried a trailing `~`.
     * @throws ToolValidationError on any malformed input, impossible calendar date,
     *         or a backwards/empty interval (`endTs <= startTs`).
     */
    fun parseEventTime(
        value: String,
        zoneId: ZoneId = ZoneId.systemDefault(),
    ): Triple<Long, Long, Boolean> {
        if (value.isBlank()) {
            throw ToolValidationError(
                "event_time: expected a non-empty string, got '$value'"
            )
        }
        val raw = value.trim()
        val approx = raw.endsWith("~")
        val text = if (approx) raw.dropLast(1) else raw

        if ("/" in text) {
            val slashIdx = text.indexOf('/')
            val left = text.substring(0, slashIdx)
            val right = text.substring(slashIdx + 1)
            val startPoint = parsePoint(left)
            val startTs = toEpoch(startPoint, zoneId)
            val endTs: Long =
                if (right == "..") {
                    Long.MAX_VALUE
                } else {
                    val (endPoint, endPrecision) = parsePointWithPrecision(right)
                    toEpoch(advance(endPoint, endPrecision), zoneId)
                }
            if (endTs != Long.MAX_VALUE && endTs <= startTs) {
                throw ToolValidationError(
                    "event_time: interval end is not after its start: '$value'"
                )
            }
            return Triple(startTs, endTs, approx)
        }

        val (point, precision) = parsePointWithPrecision(text)
        val startTs = toEpoch(point, zoneId)
        val endTs = toEpoch(advance(point, precision), zoneId)
        return Triple(startTs, endTs, approx)
    }

    /**
     * Parse one time point into a naive [LocalDateTime].
     *
     * Missing components are floored (month/day -> 1, hour/minute -> 0).
     */
    private fun parsePoint(text: String): LocalDateTime {
        val match =
            POINT_RE.matchEntire(text)
                ?: throw ToolValidationError(
                    "event_time: invalid time point '$text' " +
                        "(expected 2025 | 2026-03 | 2026-08-08 | 2026-08-08T15:00)"
                )
        return parseMatch(match).first
    }

    /** Parse a point and also return its precision name. */
    private fun parsePointWithPrecision(text: String): Pair<LocalDateTime, String> {
        val match =
            POINT_RE.matchEntire(text)
                ?: throw ToolValidationError(
                    "event_time: invalid time point '$text' " +
                        "(expected 2025 | 2026-03 | 2026-08-08 | 2026-08-08T15:00)"
                )
        return parseMatch(match)
    }

    /** Shared extraction logic for a successful [POINT_RE] match. */
    private fun parseMatch(match: MatchResult): Pair<LocalDateTime, String> {
        val (yearS, monthS, dayS, hourS, minuteS) = match.destructured
        val precisionIdx =
            when {
                hourS.isNotEmpty() -> 3
                dayS.isNotEmpty() -> 2
                monthS.isNotEmpty() -> 1
                else -> 0
            }
        val precision = PRECISIONS[precisionIdx]
        val year = yearS.toInt()
        val month = if (monthS.isEmpty()) 1 else monthS.toInt()
        val day = if (dayS.isEmpty()) 1 else dayS.toInt()
        val hour = if (hourS.isEmpty()) 0 else hourS.toInt()
        val minute = if (minuteS.isEmpty()) 0 else minuteS.toInt()
        try {
            return LocalDateTime.of(year, month, day, hour, minute) to precision
        } catch (e: Exception) {
            throw ToolValidationError(
                "event_time: invalid calendar date '${match.groupValues[0]}': ${e.message}"
            )
        }
    }

    /** Step a floored point one unit of its precision forward. Mirrors `_advance`. */
    private fun advance(point: LocalDateTime, precision: String): LocalDateTime =
        when (precision) {
            "year" -> point.withYear(point.year + 1)
            "month" ->
                if (point.monthValue == 12) {
                    point.withYear(point.year + 1).withMonth(1)
                } else {
                    point.withMonth(point.monthValue + 1)
                }
            "day" -> point.plusDays(1)
            else -> point.plusMinutes(1)
        }

    /**
     * Convert a naive local wall-clock [LocalDateTime] to epoch seconds.
     *
     * Equivalent to Python's `time.mktime(point.timetuple())` for the same timezone.
     */
    private fun toEpoch(point: LocalDateTime, zoneId: ZoneId): Long =
        point.atZone(zoneId).toEpochSecond()
}
