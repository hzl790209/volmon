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
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.PathEffect
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.nativeCanvas
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

private val histTimeFmt = SimpleDateFormat("MM-dd HH:mm", Locale.getDefault())

/**
 * 历史趋势图（Web 版 trend 图 1:1 设计）：传入按时间分桶后的均值序列（均匀分布），
 * X 轴显示 起 / 中 / 止 时间标签，其余（自适应 Y 轴/网格/阈值虚线/主题配色）与实时图一致。
 */
@Composable
fun HistoryTrendChart(
    values: List<Float>,
    threshold: Float? = null,
    startTimeMs: Long,
    endTimeMs: Long,
    modifier: Modifier = Modifier,
    height: Dp = 200.dp,
) {
    val line = MaterialTheme.colorScheme.primary
    val grid = MaterialTheme.colorScheme.outline.copy(alpha = 0.55f)
    val label = MaterialTheme.colorScheme.onSurfaceVariant
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

            val rng = yRange(values, threshold)
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

            // Y 轴网格 + dB 刻度
            var db = rng.lo
            while (db <= rng.hi + 0.1f) {
                val yy = y(db)
                drawLine(grid, Offset(padL, yy), Offset(padL + pw, yy), strokeWidth = 1f)
                c.drawText("%.0f".format(db), padL - 4.dp.toPx(), yy, yLabel)
                db += rng.step
            }

            // X 轴时间标签（起 / 中 / 止，同 Web 版 trend）
            val baseY = padT + ph + 13.dp.toPx()
            c.drawText(histTimeFmt.format(Date(startTimeMs)), padL, baseY, xLabel)
            c.drawText(
                histTimeFmt.format(Date((startTimeMs + endTimeMs) / 2)),
                padL + pw / 2f, baseY, xLabel
            )
            c.drawText(histTimeFmt.format(Date(endTimeMs)), padL + pw, baseY, xLabel)

            // 阈值虚线
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

            // 曲线（均匀分桶，左→右即时间正序）
            if (values.size > 1) {
                val path = Path()
                val step = pw / (values.size - 1)
                values.forEachIndexed { i, v ->
                    val x = padL + i * step
                    val yy = y(v)
                    if (i == 0) path.moveTo(x, yy) else path.lineTo(x, yy)
                }
                drawPath(path, line, style = Stroke(width = 1.5.dp.toPx()))
            }
        }
    }
}
