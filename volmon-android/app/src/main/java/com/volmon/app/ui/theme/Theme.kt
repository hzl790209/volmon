package com.volmon.app.ui.theme

import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Shapes
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp

/** 可选主题（设置里切换，存 DataStore themeMode）。 */
object ThemeModes {
    const val HACKER = 0    // 黑客绿
    const val PIP_BOY = 1   // 辐射4 PIP-Boy 终端（绿磷光 CRT / 像素方角 / 扫描线）
}

/** 黑客绿：近纯黑底 + 荧光绿。 */
private val HackerColorScheme = darkColorScheme(
    primary = Color(0xFF00E676),
    secondary = Color(0xFF00C853),
    background = Color(0xFF050805),
    surface = Color(0xFF0B120B),
    surfaceVariant = Color(0xFF122012),
    onBackground = Color(0xFFC8F5C8),
    onSurface = Color(0xFFC8F5C8),
    onSurfaceVariant = Color(0xFF7BA37B),
    outline = Color(0xFF3D5C3D),
    error = Color(0xFFFF5252),
)

/**
 * PIP-Boy 终端（辐射4）：黄绿磷光单色 CRT，暗绿底 + 方角（像素化），
 * 配合全局扫描线叠加层使用。
 */
private val PipBoyColorScheme = darkColorScheme(
    primary = Color(0xFF52FF33),          // 磷光黄绿
    secondary = Color(0xFF2EDB1E),
    background = Color(0xFF050A02),
    surface = Color(0xFF0A1405),
    surfaceVariant = Color(0xFF12240A),
    onBackground = Color(0xFFB9FFA7),
    onSurface = Color(0xFFB9FFA7),
    onSurfaceVariant = Color(0xFF86B36F),
    outline = Color(0xFF2E4D22),
    error = Color(0xFFFF5252),
)

private val RoundedShapes = Shapes(
    small = RoundedCornerShape(8.dp),
    medium = RoundedCornerShape(8.dp),
    large = RoundedCornerShape(12.dp),
)

/** 像素化：全部直角，无圆角（0dp 圆角即方角）。 */
private val PixelShapes = Shapes(
    small = RoundedCornerShape(0.dp),
    medium = RoundedCornerShape(0.dp),
    large = RoundedCornerShape(0.dp),
)

@Composable
fun VolmonTheme(themeMode: Int = ThemeModes.HACKER, content: @Composable () -> Unit) {
    val pipBoy = themeMode == ThemeModes.PIP_BOY
    MaterialTheme(
        colorScheme = if (pipBoy) PipBoyColorScheme else HackerColorScheme,
        shapes = if (pipBoy) PixelShapes else RoundedShapes,
        content = content,
    )
}
