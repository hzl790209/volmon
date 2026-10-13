package com.volmon.app.ui.components

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.PathEffect
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.nativeCanvas
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import kotlin.math.ceil
import kotlin.math.floor

internal data class YRange(val lo: Float, val hi: Float, val step: Float)

/**
 * 自适应 Y 轴（1:1 移植 Web 版 yRange）：
 * 含阈值一起取范围，整 10 取整；跨度不足 30dB 时居中扩到 ≥30；
 * 步长按跨度取 20/10/5。
 */
internal fun yRange(vals: List<Float>, thr: Float?): YRange {
    var mn = Float.POSITIVE_INFINITY
    var mx = Float.NEGATIVE_INFINITY
    for (v in vals) {
        if (v < mn) mn = v
        if (v > mx) mx = v
    }
    if (thr != null) {
        if (thr < mn) mn = thr
        if (thr > mx) mx = thr
    }
    if (mn.isInfinite() || mx.isInfinite()) return YRange(30f, 100f, 10f)
    var lo = floor((mn - 5f) / 10f) * 10f
    var hi = ceil((mx + 5f) / 10f) * 10f
    if (hi - lo < 30f) {
        val c = (hi + lo) / 2f
        lo = floor((c - 15f) / 5f) * 5f
        hi = ceil((c + 15f) / 5f) * 5f
    }
    val span = hi - lo
    val step = if (span > 120f) 20f else if (span > 60f) 10f else 5f
    return YRange(lo, hi, step)
}

/**
 * 实时趋势图（按 Web 版图表设计 1:1）：
 * 自适应 Y 轴 + 左侧 dB 刻度 + 横向网格 + "音量 dB" 轴标题 +
 * 红色虚线阈值 + X 轴时间标签（-Ns … 现在）+ 右对齐滚动曲线。
 * 颜色与圆角跟随主题（黑客绿 / PIP-Boy 均适配）。
 */
@Composable
fun LevelChart(
    history: List<Float>,
    threshold: Float? = null,
    modifier: Modifier = Modifier,
    height: Dp = 200.dp,
) {
    val n = history.size
    val nMax = 180 // 与 MonitorState 历史容量一致

    val line = MaterialTheme.colorScheme.primary
    val grid = MaterialTheme.colorScheme.outline.copy(alpha = 0.55f)
    val label = MaterialTheme.colorScheme.onSurfaceVariant
    val title = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.8f)
    val thrColor = MaterialTheme.colorScheme.error
    val bg = MaterialTheme.colorScheme.surface
    val border = MaterialTheme.colorScheme.outline
    val shape = MaterialTheme.shapes.small

    Box(
        modifier
            .height(height)
            .background(bg, shape)
            .border(1.dp, border, shape)
    ) {
        Canvas(Modifier.fillMaxSize()) {
            val c = drawContext.canvas.nativeCanvas
            val padL = 40.dp.toPx()
            val padR = 10.dp.toPx()
            val padT = 10.dp.toPx()
            val padB = 20.dp.toPx()
            val pw = size.width - padL - padR
            val ph = size.height - padT - padB
            if (pw <= 0f || ph <= 0f) return@Canvas

            val rng = yRange(history, threshold)
            fun y(v: Float): Float =
                padT + ph - ((v.coerceIn(rng.lo, rng.hi) - rng.lo) / (rng.hi - rng.lo)) * ph

            val yLabel = android.graphics.Paint().apply {
                isAntiAlias = true
                textSize = 10.dp.toPx()
                color = label.toArgb()
                textAlign = android.graphics.Paint.Align.RIGHT
            }
            val xLabel = android.graphics.Paint(yLabel).apply {
                textAlign = android.graphics.Paint.Align.CENTER
            }
            val titlePaint = android.graphics.Paint(yLabel).apply {
                color = title.toArgb()
                textAlign = android.graphics.Paint.Align.CENTER
            }

            // Y 轴网格 + dB 刻度
            var db = rng.lo
            while (db <= rng.hi + 0.1f) {
                val yy = y(db)
                drawLine(grid, Offset(padL, yy), Offset(padL + pw, yy), strokeWidth = 1f)
                c.drawText("%.0f".format(db), padL - 4.dp.toPx(), yy, yLabel)
                db += rng.step
            }

            // Y 轴标题（旋转 90°）
            val titleX = 12.dp.toPx()
            val titleY = padT + ph / 2f
            c.save()
            c.rotate(-90f, titleX, titleY)
            c.drawText("音量 dB", titleX, titleY, titlePaint)
            c.restore()

            // X 轴时间标签（滚动窗口：-Ns … 现在）
            if (n > 1) {
                val windowSec = n
                for (k in 0..4) {
                    val frac = k / 4f
                    val x = padL + pw * frac
                    val secAgo = Math.round((1 - frac) * windowSec)
                    val text = if (secAgo == 0) "现在" else "-${secAgo}s"
                    c.drawText(text, x, padT + ph + 13.dp.toPx(), xLabel)
                }
            }

            // 阈值线（虚线）
            if (threshold != null) {
                val ty = y(threshold)
                drawLine(
                    thrColor,
                    Offset(padL, ty),
                    Offset(padL + pw, ty),
                    strokeWidth = 1.5.dp.toPx(),
                    pathEffect = PathEffect.dashPathEffect(floatArrayOf(6.dp.toPx(), 4.dp.toPx()))
                )
            }

            // 曲线（右对齐：最新点在最右，向左滚动）
            if (n > 1) {
                val path = Path()
                for (i in 0 until n) {
                    val x = padL + pw * ((nMax - n + i).toFloat() / (nMax - 1))
                    val yy = y(history[i])
                    if (i == 0) path.moveTo(x, yy) else path.lineTo(x, yy)
                }
                drawPath(path, line, style = Stroke(width = 1.5.dp.toPx()))
            }
        }
    }
}
