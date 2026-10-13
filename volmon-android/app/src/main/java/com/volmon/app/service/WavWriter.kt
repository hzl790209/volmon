package com.volmon.app.service

import java.io.File

/**
 * 把 16-bit PCM(单声道) 写成标准 WAV 文件（对应桌面版 recorder.py 的 WAV 落盘）。
 */
object WavWriter {
    fun write(file: File, samples: ShortArray, sampleRate: Int) {
        file.parentFile?.mkdirs()
        val dataLen = samples.size * 2
        val totalLen = 36 + dataLen
        file.outputStream().use { out ->
            fun writeInt(v: Int) {
                out.write(v and 0xFF)
                out.write((v ushr 8) and 0xFF)
                out.write((v ushr 16) and 0xFF)
                out.write((v ushr 24) and 0xFF)
            }
            fun writeShort(v: Int) {
                out.write(v and 0xFF)
                out.write((v ushr 8) and 0xFF)
            }
            fun writeStr(s: String) = s.forEach { out.write(it.code) }

            writeStr("RIFF"); writeInt(totalLen); writeStr("WAVE")
            writeStr("fmt "); writeInt(16)
            writeShort(1)            // PCM
            writeShort(1)            // 单声道
            writeInt(sampleRate)
            writeInt(sampleRate * 2) // 字节率 = sampleRate * blockAlign
            writeShort(2)            // blockAlign = channels * bits/8
            writeShort(16)           // 位深
            writeStr("data"); writeInt(dataLen)
            for (s in samples) writeShort(s.toInt() and 0xFFFF)
        }
    }
}
