package com.volmon.app.audio

import android.media.AudioAttributes
import android.media.AudioDeviceInfo
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTrack
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlin.math.PI
import kotlin.math.sin

/**
 * 报警提示音：用 [AudioTrack] 实时合成一段正弦音（无需任何音频文件依赖）。
 * 对应桌面版 alarm.py 的蜂鸣器。
 *
 * 路由策略：
 *  - [device] = null → 走系统默认的 [AudioManager.STREAM_ALARM]（系统"提醒音"路由，
 *    多数厂商会强制从手机扬声器出声，即使连着蓝牙也听不到——正是用户反馈的问题）；
 *  - [device] != null → 改用 [AudioAttributes.USAGE_MEDIA] 并 [AudioTrack.setPreferredDevice]
 *    直接把提示音路由到所选设备（如蓝牙音响），绕过"报警强制手机扬声器"的音频策略。
 *
 * 重入保护：上一段没播完不叠加。
 */
class AlarmSound {
    private val scope = CoroutineScope(Dispatchers.Default + SupervisorJob())
    @Volatile
    private var playing = false

    /**
     * 播放 [durationSec] 秒、频率 [freqHz] 的正弦提示音。
     * @param device 指定输出设备；为 null 时走系统默认（提醒音路由）。
     */
    fun beep(durationSec: Float, device: AudioDeviceInfo? = null, freqHz: Int = 1000) {
        if (playing) return
        playing = true
        scope.launch {
            try {
                val sampleRate = 44100
                val numSamples = (durationSec * sampleRate).toInt().coerceAtLeast(sampleRate)
                val block = ShortArray(sampleRate / 10) // 100ms 分块
                val bufSize = AudioTrack.getMinBufferSize(
                    sampleRate, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT
                ).coerceAtLeast(2048)
                val attrs = AudioAttributes.Builder()
                    // 指定设备时改用媒体用途，才能把声音送到蓝牙/外接音响；
                    // 否则用闹钟用途，避免被媒体静音直接吞掉。
                    .setUsage(if (device != null) AudioAttributes.USAGE_MEDIA else AudioAttributes.USAGE_ALARM)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                    .build()
                val track = AudioTrack.Builder()
                    .setAudioAttributes(attrs)
                    .setAudioFormat(
                        AudioFormat.Builder()
                            .setSampleRate(sampleRate)
                            .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                            .build()
                    )
                    .setBufferSizeInBytes(bufSize)
                    .build()
                // 直接路由到用户选定的设备（绕过系统默认策略）
                if (device != null) track.setPreferredDevice(device)
                track.play()
                var pos = 0
                while (isActive && pos < numSamples) {
                    val n = if (block.size < numSamples - pos) block.size else numSamples - pos
                    for (i in 0 until n) {
                        val t = (pos + i).toDouble() / sampleRate
                        val s = (sin(2.0 * PI * freqHz * t) * 0.7 * 32767).toInt()
                        block[i] = s.toShort()
                    }
                    track.write(block, 0, n)
                    pos += n
                }
                track.stop()
                track.release()
            } finally {
                playing = false
            }
        }
    }

    fun release() {
        scope.cancel()
        playing = false
    }
}
