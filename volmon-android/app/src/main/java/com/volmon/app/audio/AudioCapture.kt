package com.volmon.app.audio

import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import com.volmon.app.data.AudioLevel
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlin.math.sqrt

/**
 * 麦克风实时采集（对应桌面版 audio.py 的 AudioEngine）。
 *
 * 用 AudioRecord 读取 16-bit PCM，每帧算 RMS→dBFS，通过 [level] 流给上层；
 * 同时把原始帧喂给 [ring]（预录缓冲）和 [onChunk] 回调（录音用）。
 */
class AudioCapture(
    private val sampleRate: Int = 44100,
    ringSeconds: Float = 10f,
) {
    private val scope = CoroutineScope(Dispatchers.Default + SupervisorJob())
    private val ring = RingPcm((sampleRate * ringSeconds).toInt().coerceAtLeast(sampleRate))
    private var record: AudioRecord? = null

    private val _level = MutableStateFlow(AudioLevel.DBFS_FLOOR)
    val level = _level.asStateFlow()

    /** 每读到一个 PCM 块回调（录音功能用，注意回调在采集线程）。 */
    var onChunk: ((ShortArray) -> Unit)? = null

    /**
     * 污染判定：返回 true 时该帧视为被污染（如报警声自收），
     * 不入预录环（避免下次录音的 pre-roll 开头残留报警声）。
     * 由服务在报警期间置真（检查 pollutionUntil）。
     */
    var isContaminated: (() -> Boolean)? = null

    @Volatile
    private var running = false

    fun start() {
        if (running) return
        running = true
        val minBuf = AudioRecord.getMinBufferSize(
            sampleRate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT
        )
        val bufSize = (minBuf.coerceAtLeast(1) * 2).coerceAtLeast(sampleRate) // 至少 ~1s
        record = AudioRecord(
            MediaRecorder.AudioSource.MIC, sampleRate,
            AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, bufSize
        )
        record?.startRecording()
        scope.launch {
            val shortBuf = ShortArray(bufSize / 2)
            while (isActive && running) {
                val read = record?.read(shortBuf, 0, shortBuf.size) ?: 0
                if (read <= 0) {
                    delay(10)
                    continue
                }
                val chunk = if (read == shortBuf.size) shortBuf else shortBuf.copyOf(read)
                val rms = computeRms(chunk)
                _level.value = AudioLevel.rmsToDbfs(rms)
                // 报警声自收期间不入预录环：否则下次录音的 pre-roll 会带上报警残留
                if (isContaminated?.invoke() != true) ring.push(chunk)
                onChunk?.invoke(chunk)
            }
        }
    }

    /** 回拉最近 `seconds` 秒的预录音频。 */
    fun pullRing(seconds: Float): ShortArray = ring.pull(seconds, sampleRate)

    fun stop() {
        running = false
        try {
            record?.stop()
        } catch (_: Exception) {
        }
        try {
            record?.release()
        } catch (_: Exception) {
        }
        record = null
        scope.cancel()
    }

    private fun computeRms(chunk: ShortArray): Float {
        if (chunk.isEmpty()) return 0f
        var sum = 0.0
        for (s in chunk) {
            val v = s / 32768.0
            sum += v * v
        }
        return sqrt(sum / chunk.size).toFloat()
    }
}
