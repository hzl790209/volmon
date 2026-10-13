package com.volmon.app.audio

/**
 * 16-bit PCM 环形缓冲（预录用）。
 *
 * 监测服务持续把实时音频帧写进来；报警触发瞬间可"回拉"最近 N 秒的音频，
 * 从而保证不漏掉报警前的真实环境音（对应桌面版的 rec_pre_sec）。
 */
class RingPcm(capacityShorts: Int) {
    private val cap = capacityShorts.coerceAtLeast(1)
    private val buf = ShortArray(cap)
    private var writePos = 0
    private var filled = 0

    fun push(chunk: ShortArray) {
        for (s in chunk) {
            buf[writePos] = s
            writePos = (writePos + 1) % cap
            if (filled < cap) filled++
        }
    }

    /** 取最近 `seconds` 秒音频（按 sampleRate 换算），顺序为 旧→新。 */
    fun pull(seconds: Float, sampleRate: Int): ShortArray {
        val want = (seconds * sampleRate).toInt().coerceIn(0, filled)
        if (want == 0) return ShortArray(0)
        val out = ShortArray(want)
        val start = (writePos - filled + cap) % cap
        for (i in 0 until want) out[i] = buf[(start + i) % cap]
        return out
    }

    fun clear() {
        writePos = 0
        filled = 0
        buf.fill(0)
    }
}
