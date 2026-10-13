package com.volmon.app.ui.components

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.Box
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp

/**
 * CRT 扫描线叠加层（PIP-Boy 主题用）：整屏半透明黑色横条纹，
 * 纯绘制不拦截点击，营造老式终端质感。
 */
@Composable
fun ScanlineOverlay(modifier: Modifier = Modifier, gap: Dp = 3.dp) {
    Box(modifier) {
        Canvas(Modifier.matchParentSize()) {
            val lineH = gap.toPx()
            val barH = lineH / 2f
            var yy = 0f
            while (yy < size.height) {
                drawRect(
                    Color.Black.copy(alpha = 0.16f),
                    Offset(0f, yy),
                    Size(size.width, barH)
                )
                yy += lineH
            }
        }
    }
}
