"""命令行实时客户端：连接后端 WebSocket，在终端渲染**带坐标轴的实时音量图表**。

呈现方式对齐 Web 页面：
  - 纵轴：音量 dB（自适应刻度，含阈值）
  - 横轴：时间（左=起点、中=中间、右=现在）
  - 红色虚线行：预警阈值
  - 顶部状态条：报警/超量/正常 + 校准状态
字符全部使用 ASCII，保证在任意终端下对齐不错位。
"""
import asyncio
import json
import logging
import math
import os
import re
import sys
import time

import websockets
from io import StringIO
from rich.cells import cell_len
from rich.console import Console
from rich.live import Live
from rich.text import Text

logger = logging.getLogger("volmon.cli")

# 纵轴标签区宽度（如 "   90 |"）
LAB_W = 7

# 整屏重绘上限（次/秒）。Windows 传统控制台(conhost)在高频整屏重绘下会明显频闪，
# 尤其历史趋势这类"静态密集字符"区域；默认取较低值。Windows Terminal / iTerm 等
# 现代终端可用 `cli --fps 15` 调高让波形更顺滑。
DEFAULT_FPS = 6

# 历史趋势重新拉取的间隔（秒）
TREND_REFRESH_SEC = 60

# 除图表行之外的固定行开销（用于精确的纵向预算，保证内容不超出终端高度）
#   分屏模式：表头 2 + 波形轴/说明 3 + 分隔空行 1 + 趋势区块 6 + 静音/麦克风提示 2 = 14
#   单波形  ：表头 2 + 波形轴/说明 3 + 静音/麦克风提示 2 = 7
_OVERHEAD_TREND = 14
_OVERHEAD_WAVE = 7

# 终端行数低于此值就不做上下分屏：两段图表（各至少 5 行）挤不下会让内容超出屏幕，
# 而超出屏幕就会滚动 —— 底部的历史趋势会跟着"跳"，这正是要避免的闪烁来源。
MIN_SPLIT_LINES = 25


def _fmt_dur(sec):
    """把秒数格式化为 MM:SS（超过 1 小时为 H小时MM分）。"""
    sec = max(0, int(round(float(sec))))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}小时{m:02d}分" if h else f"{m:02d}:{s:02d}"


def _fit_lines(t, width):
    """把每一行按**显示宽度**裁到 width 以内（全角字符算 2 格），返回新的 Text。

    为什么不能只依赖 rich 的 ``no_wrap``：它在 ``console.print()`` 路径上并不生效
    （中文行会被折行），一旦折行，实际行数就比作者行数多一行，底部内容会被挤出屏幕
    ——而历史趋势正好在最底部，最先被挤掉，看上去就是"频闪"。
    这里主动裁一次，保证"作者写了多少行，终端就渲染多少行"这条不变量成立。
    """
    if width <= 0:
        return t
    out = Text(no_wrap=True, overflow="crop")
    for i, line in enumerate(t.split("\n", allow_blank=True)):
        if i:
            out.append("\n")
        if cell_len(line.plain) > width:
            line.truncate(width, overflow="crop")
        out.append(line)
    return out


def _range_for(vals, thr):
    """自适应 Y 轴范围：含阈值，取整到 10，最小跨度 30dB（与 Web 端一致）。"""
    mn, mx = min(vals), max(vals)
    mn = min(mn, thr)
    mx = max(mx, thr)
    lo = math.floor((mn - 5) / 10.0) * 10
    hi = math.ceil((mx + 5) / 10.0) * 10
    if hi - lo < 30:
        c = (hi + lo) / 2.0
        lo = math.floor((c - 15) / 5.0) * 5
        hi = math.ceil((c + 15) / 5.0) * 5
    return float(lo), float(hi)


def _range_for_robust(vals, thr, lo_q=0.01, hi_q=0.99):
    """稳健版 Y 轴范围：用分位数代替 min/max。

    历史区间长、点数多，个别坏点（如设备切换/校准异常产生的 100+ dB 假值）会把
    min/max 撑到极端值，导致正常数据全被压在底线、整张图不可读。这里按分位数取范围，
    超出范围的点在绘制时被 clamp 到边界（不丢数据，只是画在边上）。
    """
    s = sorted(vals)
    n = len(s)
    if not n:
        return _range_for([thr], thr)

    def quantile(p):
        i = min(n - 1, max(0, int(round(p * (n - 1)))))
        return s[i]

    mn, mx = quantile(lo_q), quantile(hi_q)
    mn, mx = min(mn, thr), max(mx, thr)
    lo = math.floor((mn - 5) / 10.0) * 10
    hi = math.ceil((mx + 5) / 10.0) * 10
    if hi - lo < 30:
        c = (hi + lo) / 2.0
        lo = math.floor((c - 15) / 5.0) * 5
        hi = math.ceil((c + 15) / 5.0) * 5
    return float(lo), float(hi)


def build_chart(hist, threshold, width, height, reserve=12):
    """把 [(时间戳ms, dB), ...] 渲染成字符图表。

    reserve: 右侧为阈值标注预留的字符宽度，避免终端换行错位。
    返回 (lines, plot_w, lo, hi, data)，lines 为 [(标签, 内容, 是否阈值行), ...]。
    """
    vals = [d for _, d in hist]
    lo, hi = _range_for(vals, threshold)
    plot_w = max(10, min(width - LAB_W - reserve, 200))
    rows = max(5, min(height, 24))
    data = hist[-plot_w:] if len(hist) > plot_w else hist

    canvas = [[" "] * plot_w for _ in range(rows)]

    def y_row(v):
        v = min(max(v, lo), hi)
        return min(max(int(round((hi - v) / (hi - lo) * (rows - 1))), 0), rows - 1)

    thr_row = y_row(threshold)

    # 描点
    n = len(data)
    for i, (_, v) in enumerate(data):
        c = int(round(i * (plot_w - 1) / max(n - 1, 1)))
        canvas[y_row(v)][c] = "*"
    # 竖直连线，让波形连续
    for c in range(plot_w):
        rs = [r for r in range(rows) if canvas[r][c] == "*"]
        if len(rs) >= 2:
            for rr in range(min(rs), max(rs)):
                if canvas[rr][c] == " ":
                    canvas[rr][c] = "|"

    lines = []
    for r in range(rows):
        val = hi - (hi - lo) * r / (rows - 1)
        label = f"{val:>5.0f} |"
        if r == thr_row:
            content = "".join("-" if canvas[r][c] == " " else canvas[r][c] for c in range(plot_w))
        else:
            content = "".join(canvas[r][c] for c in range(plot_w))
        lines.append((label, content, r == thr_row))
    return lines, plot_w, lo, hi, data


def _xlabels(data, plot_w):
    """横轴时间刻度：左=起点、中=中间、右=现在。"""
    lbl = [" "] * plot_w

    def put(s, pos):
        pos = max(0, min(plot_w - len(s), pos))
        for i, ch in enumerate(s):
            lbl[pos + i] = ch

    def f(ms):
        return time.strftime("%H:%M:%S", time.localtime(ms / 1000))

    t0, t1 = data[0][0], data[-1][0]
    put(f(t0), 0)
    put(f((t0 + t1) / 2), plot_w // 2 - 4)
    put(f(t1) + " 现在", plot_w - 14)
    return "".join(lbl)


def _bucketize(hist, plot_w):
    """把 [(ts_ms, db), ...] 按时间均匀聚合到 plot_w 个桶，每桶取均值。

    历史区间的点数远多于可绘制列数（24h 数千点 vs 终端约 100 列），必须按时间分桶，
    否则画出来的只是末尾一小段。实时波形（最近 600 点）不需要聚合，故单独实现。
    某桶无采样时返回 None。
    """
    t0, t1 = hist[0][0], hist[-1][0]
    span = max(1.0, float(t1 - t0))
    buckets = [[] for _ in range(plot_w)]
    for ts, db in hist:
        i = int((ts - t0) / span * (plot_w - 1))
        buckets[max(0, min(plot_w - 1, i))].append(db)
    return [sum(b) / len(b) if b else None for b in buckets]


def build_trend_chart(hist, threshold, width, height, reserve=12):
    """历史趋势图：整段采样按时间聚合后渲染（风格与实时波形一致）。

    返回 (lines, plot_w, lo, hi)，lines 为 [(标签, 内容, 是否阈值行), ...]。
    """
    vals = [d for _, d in hist]
    lo, hi = _range_for_robust(vals, threshold)   # 历史区间长，用分位数范围防坏点压扁图
    plot_w = max(10, min(width - LAB_W - reserve, 200))
    n_rows = max(5, min(height, 24))
    series = _bucketize(hist, plot_w)

    canvas = [[" "] * plot_w for _ in range(n_rows)]

    def y_row(v):
        v = min(max(v, lo), hi)
        return min(max(int(round((hi - v) / (hi - lo) * (n_rows - 1))), 0), n_rows - 1)

    thr_row = y_row(threshold)

    for c, v in enumerate(series):
        if v is None:
            continue
        canvas[y_row(v)][c] = "*"
    # 竖直连线，让相邻列的曲线连起来
    for c in range(plot_w):
        rs = [r for r in range(n_rows) if canvas[r][c] == "*"]
        if len(rs) >= 2:
            for rr in range(min(rs), max(rs)):
                if canvas[rr][c] == " ":
                    canvas[rr][c] = "|"

    lines = []
    for r in range(n_rows):
        val = hi - (hi - lo) * r / (n_rows - 1)
        label = f"{val:>5.0f} |"
        if r == thr_row:
            content = "".join("-" if canvas[r][c] == " " else canvas[r][c] for c in range(plot_w))
        else:
            content = "".join(canvas[r][c] for c in range(plot_w))
        lines.append((label, content, r == thr_row))
    return lines, plot_w, lo, hi


def _trend_xlabels(hist, plot_w):
    """历史趋势横轴刻度：左=起点、中=中间、右=终点（含月日）。"""
    lbl = [" "] * plot_w

    def put(s, pos):
        pos = max(0, min(plot_w - len(s), pos))
        for i, ch in enumerate(s):
            lbl[pos + i] = ch

    def f(ms):
        return time.strftime("%m-%d %H:%M", time.localtime(ms / 1000))

    t0, t1 = hist[0][0], hist[-1][0]
    put(f(t0), 0)
    put(f((t0 + t1) / 2), plot_w // 2 - 6)
    put(f(t1), plot_w - 11)
    return "".join(lbl)


def render_trend(hist, alert_count, threshold, hours, width=100, height=12, note=""):
    """历史趋势区块：区间标题 + 统计（含预警次数）+ 字符趋势图。返回 rich Text。

    **区块高度恒定**为 ``rows + 6`` 行（rows = 图表行数）：即使暂无数据 / 接口失败，
    也用空行补齐。高度恒定是"不闪"的关键——CLI 每帧整屏重绘，若这一块忽高忽低，
    整屏内容会跟着上下跳动，看起来就是历史趋势部分频闪。

    文本开启了 ``no_wrap`` + ``overflow="crop"``：超宽的行按终端宽度**裁掉**而不是
    折行，避免折行多出一行又把下面的内容顶出屏幕。
    """
    t = Text(no_wrap=True, overflow="crop")
    label = "近 7 天" if hours >= 168 else f"近 {hours:g} 小时"
    rows = max(5, min(height, 24))
    t.append(f"历史趋势（{label}）\n", style="bold cyan")
    if not hist:
        t.append("  该时段暂无采样数据\n", style="dim")
        t.append("\n" * (rows + 3))          # 补齐为空行，保持区块高度不变
        t.append((note + "\n") if note else "\n", style="dim")
        return _fit_lines(t, width)
    vals = [d for _, d in hist]
    avg = sum(vals) / len(vals)
    mx = max(vals)
    over = sum(1 for v in vals if v > threshold)
    t.append(
        f"  采样 {len(vals)} 点   均值 {avg:.1f} dB   峰值 {mx:.1f} dB   "
        f"超阈值占比 {over / len(vals) * 100:.1f}%   预警 {alert_count} 次\n",
        style="bold",
    )
    lines, plot_w, lo, hi = build_trend_chart(hist, threshold, width, height)
    for lab, content, is_thr in lines:
        t.append(lab, style="dim")
        if is_thr:
            t.append(content.rstrip(), style="bold red")
            t.append(f"  <=阈值{threshold:.1f}\n", style="bold red")
        else:
            t.append(content.rstrip() + "\n", style="bold cyan")
    t.append(" " * LAB_W + "-" * plot_w + "\n", style="dim")
    t.append(" " * LAB_W + _trend_xlabels(hist, plot_w).rstrip() + "\n", style="dim")
    t.append(f"  纵轴=音量 dB（{lo:.0f}~{hi:.0f}）  横轴=时间（左=起点 右=终点）\n", style="dim")
    clipped = sum(1 for v in vals if v > hi or v < lo)
    notes = []
    if clipped:
        notes.append(f"! {clipped} 个采样点超出显示范围（最高 {mx:.1f} dB），已按边界截断")
    if note:
        notes.append(note)
    # 备注行恒定占一行（无备注则留空），保证区块高度不抖
    t.append(("  " + "   ".join(notes) + "\n") if notes else "\n",
             style="bold yellow" if clipped else "dim")
    return _fit_lines(t, width)


def fetch_history(host, port, hours):
    """从后端 REST 拉历史采样与预警次数；服务未启动或异常时返回 (None, 0)。"""
    import urllib.request

    base = f"http://{host}:{port}"
    try:
        with urllib.request.urlopen(f"{base}/api/samples?hours={hours}", timeout=5) as r:
            rows = json.loads(r.read().decode("utf-8"))
        hist = [(x["t"], x["db"]) for x in rows]
    except Exception:
        return None, 0
    try:
        with urllib.request.urlopen(f"{base}/api/alerts/count?hours={hours}", timeout=5) as r:
            cnt = int(json.loads(r.read().decode("utf-8")).get("total", 0) or 0)
    except Exception:
        cnt = 0
    return hist, cnt


def render(db, raw_db, threshold, over, alert, calibrated, history, peak,
           alarm_phase="idle", mic_silent=False, width=100, height=14,
           version="", quiet_sec=0.0, period_label="", schedule_applied=False,
           trend_text=None, hint=""):
    # no_wrap + crop：每行按终端宽度裁切，绝不折行 —— 保证"实际行数 == 预算行数"，
    # 否则一行超宽折行就会把整屏内容顶下去一行（历史趋势在最底部，最先被顶掉）。
    t = Text(no_wrap=True, overflow="crop")
    status = "未校准(估算)" if not calibrated else "已校准"
    t.append("音量监控终端  ", style="bold cyan")
    if alarm_phase == "playing":
        t.append(" 报警播放中(静默) ", style="bold white on magenta")
    elif alarm_phase == "cooldown":
        t.append(" 冷却中(静默) ", style="bold magenta")
    elif alert:
        t.append(" 超量告警! ", style="bold white on red")
    elif over:
        t.append(" 超阈值 ", style="bold red")
    else:
        t.append(" 正常 ", style="bold green")
    t.append(f"[{status}]")
    if version:
        # 已经是完整产品版本（如 "volmon-mac 1.0"）就直接显示，否则补个 v 前缀
        label = version if " " in version else f"v{version}"
        t.append(f"  {label}", style="dim")
    if period_label:
        tag = f"{period_label}策略" if schedule_applied else f"{period_label}（全局）"
        t.append(f"  [{tag}]", style="bold blue")
    if quiet_sec > 0:
        t.append(f"  [静音中 剩 {_fmt_dur(quiet_sec)}]", style="bold yellow")
    if hint:
        t.append(f"  {hint}", style="bold yellow")
    t.append("\n")

    t.append(f"当前: {db:6.1f} dB", style="bold")
    t.append(f"  (原始 {raw_db:6.1f} dBFS)", style="dim")
    t.append(f"  阈值: {threshold:6.1f}  峰值: {peak:6.1f}\n")

    if not history:
        t.append("等待数据…\n", style="dim")
        return t

    lines, plot_w, lo, hi, data = build_chart(history, threshold, width, height)
    for label, content, is_thr in lines:
        t.append(label, style="dim")
        if is_thr:
            # 行是离散的，行标签可能与阈值有偏差，此处标注真实阈值
            t.append(content.rstrip(), style="bold red")
            t.append(f"  <=阈值{threshold:.1f}\n", style="bold red")
        else:
            t.append(content.rstrip() + "\n", style="bold cyan")

    t.append(" " * LAB_W + "-" * plot_w + "\n", style="dim")
    t.append(" " * LAB_W + _xlabels(data, plot_w).rstrip() + "\n", style="dim")
    t.append(
        f"纵轴=音量 dB ({lo:.0f}~{hi:.0f})   横轴=时间   红色行=预警阈值 {threshold:.1f} dB\n",
        style="dim",
    )

    if quiet_sec > 0:
        t.append(
            f"静音时段：剩余 {_fmt_dur(quiet_sec)}，期间超阈值不发声（监测与超量记录照常）\n",
            style="bold yellow",
        )
    if mic_silent:
        t.append("! 麦克风持续静音(-100 dBFS)：疑似未授权麦克风/输入被静音/设备选错\n", style="bold red")
    if trend_text is not None:
        t.append("\n")
        t.append(trend_text)
    return _fit_lines(t, width)


def layout(columns, lines, show_trend=True):
    """按终端尺寸算出 (波形行数, 趋势行数, 是否分屏, 顶部提示)。

    预算要**精确**：richtext 每帧整屏重绘，只要实际行数超过终端高度就会滚动，
    底部的历史趋势会跟着上下跳（肉眼看到的"频闪"）。所以这里按固定开销反推，
    并留 1 行余量；行数不足时宁可退回单波形模式也不挤。
    """
    use_trend = bool(show_trend and lines >= MIN_SPLIT_LINES)
    hint = ""
    if show_trend and not use_trend:
        hint = "终端行数不足，已隐藏历史趋势（放大窗口或加 --no-trend）"
    if use_trend:
        budget = max(10, lines - _OVERHEAD_TREND - 1)
        wave_h = max(5, int(budget * 0.55))
        trend_h = max(5, budget - wave_h)
    else:
        wave_h = max(6, min(lines - _OVERHEAD_WAVE - 1, 40))
        trend_h = 0
    return wave_h, trend_h, use_trend, hint


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _enable_vt():
    """在 Windows 控制台上启用 VT 序列（ENABLE_VIRTUAL_TERMINAL_PROCESSING）。

    返回 False 表示当前环境不适合裸写 ANSI（老系统且无法开启 VT）。
    注意：stdout 重定向到文件/管道时 GetConsoleMode 会失败，此时写 ANSI 无副作用，
    视为成功。
    """
    if os.name != "nt":
        return True
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(h, ctypes.byref(mode)):
            return True  # 不是控制台（重定向/管道），ANSI 直接随字节流走
        return bool(k32.SetConsoleMode(h, mode.value | 0x0004))
    except Exception:
        return False


def _capture_lines(frame, width, height):
    """把渲染结果捕获为 (ANSI 行列表, 纯文本行列表)。

    用 StringIO 上的假终端捕获 rich 的输出，得到带颜色的整行 ANSI 文本；
    行级 diff 用纯文本比较，重写时用 ANSI 版本（颜色不丢）。
    """
    buf = StringIO()
    cap = Console(
        file=buf, width=width, height=height,
        force_terminal=True, force_jupyter=False, legacy_windows=False,
        _environ={"TERM": "xterm-256color"},
    )
    cap.print(frame, end="", crop=True, no_wrap=True, overflow="crop")
    ansi = buf.getvalue().split("\n")
    plain = [_ANSI_RE.sub("", ln) for ln in ansi]
    return ansi, plain


class _DiffPainter:
    """行级 diff 渲染器：只重写内容真正变化的行，静态区（历史趋势）完全不碰。

    背景：rich 的 Live 每次 refresh 都是**整屏重写**。顶部时钟/当前分贝每秒都在
    变，导致从未变化的历史趋势区也跟着每秒被重写一遍；Windows 传统控制台
    (conhost) 重写中文行（双宽字符）时肉眼可见 = 用户看到的"频闪"。
    这里绕开 Live，用 VT 光标定位做行级更新：哪行没变就哪行一个字节都不写。
    """

    def __init__(self, console, fps):
        self.console = console
        self.fps = max(1, int(fps))
        self._last_plain = None
        self._last_paint = 0.0
        self._size = None
        self._full = True
        self._out = sys.stdout

    def __enter__(self):
        # 进备用屏、清屏、藏光标
        self._out.write("\x1b[?1049h\x1b[H\x1b[2J\x1b[?25l")
        self._out.flush()
        return self

    def __exit__(self, *exc):
        try:
            self._out.write("\x1b[?25h\x1b[?1049l")
            self._out.flush()
        except Exception:
            pass
        return False

    def paint(self, frame, now):
        size = self.console.size
        if (size.width, size.height) != self._size:
            self._size = (size.width, size.height)
            self._full = True           # 尺寸变化 → 全量重绘一次
            self._last_plain = None

        ansi, plain = _capture_lines(frame, size.width, size.height)

        if self._full:
            out = ["\x1b[H"]
            for i, ln in enumerate(ansi):
                out.append("\x1b[%d;1H" % (i + 1) + ln + "\x1b[m\x1b[K")
            for r in range(len(ansi), self._size[1]):
                out.append("\x1b[%d;1H\x1b[K" % (r + 1))
            self._out.write("".join(out))
            self._out.flush()
            self._last_plain, self._last_paint, self._full = plain, now, False
            return

        if plain == self._last_plain:
            return                      # 内容没变：一个字节都不写（零闪烁）
        if now - self._last_paint < 1.0 / self.fps:
            return                      # 节流：留给下一次消息再画

        last = self._last_plain or []
        out = []
        for i in range(max(len(plain), len(last))):
            new = plain[i] if i < len(plain) else None
            old = last[i] if i < len(last) else None
            if new == old:
                continue
            if new is None:
                out.append("\x1b[%d;1H\x1b[K" % (i + 1))
            else:
                out.append("\x1b[%d;1H" % (i + 1) + ansi[i] + "\x1b[m\x1b[K")
        self._out.write("".join(out))
        self._out.flush()
        self._last_plain, self._last_paint = plain, now


class _LivePainter:
    """兼容回退：rich Live 渲染（--no-screen 或 VT 不可用时使用）。"""

    def __init__(self, console, fps, screen):
        self.console = console
        self.fps = max(1, int(fps))
        self.screen = bool(screen)
        self._live = None
        self._last_plain = None
        self._last_paint = 0.0

    def __enter__(self):
        self._live = Live(console=self.console, refresh_per_second=self.fps,
                          screen=self.screen, auto_refresh=False)
        self._live.__enter__()
        return self

    def __exit__(self, *exc):
        return self._live.__exit__(*exc)

    def paint(self, frame, now):
        plain = frame.plain
        if plain == self._last_plain:
            return
        if now - self._last_paint < 1.0 / self.fps:
            return
        self._live.update(frame, refresh=True)
        self._last_plain, self._last_paint = plain, now


def _make_painter(console, fps, alt_screen):
    """选择渲染器：默认走行级 diff；用户要求兼容模式或 VT 不可用时回退 Live。"""
    if alt_screen and _enable_vt():
        return _DiffPainter(console, fps)
    return _LivePainter(console, fps, screen=bool(alt_screen))


async def run_cli(host="127.0.0.1", port=8000, trend_hours=1.0, show_trend=True,
                  fps=DEFAULT_FPS, alt_screen=True):
    """实时波形 +（可选）历史趋势。

    trend_hours: 历史趋势区间（小时）；show_trend=False 则只显示实时波形。
    fps:         重绘上限（次/秒）。越小越不闪，越大波形越顺滑。
    alt_screen:  用备用屏 + 行级 diff 渲染（默认，无闪烁）。
                 关掉则回退 rich Live 兼容模式（个别老控制台的逃生通道）。
    """
    url = f"ws://{host}:{port}/ws"
    console = Console()
    history = []           # [(ts_ms, db), ...]
    # 历史趋势状态（拉取与渲染解耦、失败不清空）
    th_hist = None         # 最近一次**成功**拉取的数据
    th_count = 0
    th_ver = 0             # 数据版本号：变化时才重建趋势文本
    th_last_try = 0.0      # 上次发起请求的时间（无论成败，严格节流）
    th_last_ok = 0.0       # 上次成功的时间
    th_failed = False
    th_task = None         # 进行中的拉取任务（asyncio.Task）
    th_key = None          # 已缓存趋势文本的键
    th_text = None
    loop = asyncio.get_running_loop()
    try:
        async with websockets.connect(url, ping_interval=10) as ws:
            console.print(f"[green]已连接 {url}[/green]")
            # 行级 diff 渲染（_DiffPainter）：内容没变的行一个字节都不写，
            # 静态的历史趋势区彻底静止；只有顶部状态行/波形列随数据更新。
            # 旧实现（rich Live 整屏重绘）即使在 v1.0.9 的"内容变化才刷新"下，
            # 仍会因顶部时钟每秒变化而把整屏（含趋势区）每秒重写一遍 —— conhost
            # 下中文行重写肉眼可见，即录屏里的"频闪"。
            with _make_painter(console, fps, alt_screen) as painter:
                async for message in ws:
                    data = json.loads(message)
                    db = data.get("db", 0.0)
                    raw = data.get("raw_db", 0.0)
                    thr = data.get("threshold", 70.0)
                    over = data.get("over", False)
                    alert = data.get("alert", False)
                    peak = data.get("peak", db)
                    cal = data.get("calibrated", False)
                    phase = data.get("alarm_phase", "idle")
                    mic_silent = data.get("mic_silent", False)
                    ver = data.get("version_display") or data.get("version", "")
                    quiet_sec = float(data.get("quiet_remaining_sec", 0.0) or 0.0)
                    period_label = data.get("period_label", "") or ""
                    sched_on = bool(data.get("schedule_applied", False))

                    history.append((data.get("t", time.time() * 1000), db))
                    if len(history) > 600:
                        history = history[-600:]

                    now = time.time()

                    # ---- 历史趋势数据：异步拉取（不阻塞实时渲染）+ 严格节流 ----
                    # 旧代码两个毛病：① `await run_in_executor(...)` 每 60 秒会把实时
                    # 渲染阻塞最多 5 秒（画面僵住后突然跳一下）；② 拉取失败返回 None 时
                    # `trend_hist is None` 会让节流失效 → **每帧都发起一次 HTTP**，
                    # 成功/失败交替时趋势区块高度在 14 行 / 2 行之间来回跳 = 肉眼频闪。
                    if (show_trend and th_task is None
                            and now - th_last_try >= TREND_REFRESH_SEC):
                        th_last_try = now
                        th_task = asyncio.ensure_future(
                            loop.run_in_executor(None, fetch_history, host, port, trend_hours)
                        )
                    if th_task is not None and th_task.done():
                        try:
                            hist, cnt = th_task.result()
                        except Exception:
                            hist, cnt = None, 0
                        th_task = None
                        if hist is not None:
                            th_hist, th_count = hist, cnt
                            th_ver += 1
                            th_last_ok, th_failed = now, False
                        else:
                            th_failed = True     # 失败：保留旧数据，只改提示行
                        th_key = None            # 数据/状态变了 → 失效渲染缓存

                    # ---- 尺寸与纵向预算（精确到行，确保不超出终端高度）----
                    size = console.size
                    columns, lines = size.width, size.height
                    wave_h, trend_h, use_trend, hint = layout(columns, lines, show_trend)

                    # ---- 趋势文本：仅在数据/尺寸/阈值变化时重建（每帧重算纯属浪费）----
                    if use_trend:
                        note = ""
                        if th_failed:
                            note = ("! 历史刷新失败，显示 "
                                    + time.strftime("%H:%M:%S", time.localtime(th_last_ok))
                                    + " 的数据") if th_last_ok else "! 历史数据暂不可用"
                        key = (th_ver, columns, trend_h, round(thr, 1), trend_hours, note)
                        if key != th_key:
                            th_text = render_trend(
                                th_hist or [], th_count, thr, trend_hours,
                                width=columns, height=trend_h, note=note,
                            )
                            th_key = key
                        trend_text = th_text
                    else:
                        trend_text = None

                    frame = render(
                        db, raw, thr, over, alert, cal, history, peak,
                        phase, mic_silent,
                        width=columns,
                        height=wave_h,
                        version=ver,
                        quiet_sec=quiet_sec,
                        period_label=period_label,
                        schedule_applied=sched_on,
                        trend_text=trend_text,
                        hint=hint,
                    )
                    # 兜底：终端尺寸极端时宁可不显示趋势，也绝不让内容超出屏幕（会滚动=闪）
                    if frame.plain.count("\n") > lines - 1:
                        frame = render(
                            db, raw, thr, over, alert, cal, history, peak,
                            phase, mic_silent,
                            width=columns,
                            height=max(6, lines - _OVERHEAD_WAVE - 1),
                            version=ver,
                            quiet_sec=quiet_sec,
                            period_label=period_label,
                            schedule_applied=sched_on,
                            trend_text=None,
                            hint="终端过小，已隐藏历史趋势",
                        )
                    # 去掉结尾换行：Text 末尾的 "\n" 会多渲染一个空行、正好把屏幕占满，
                    # 少一个余量。留 1 行空余，杜绝"最后一行触发滚动"这种闪烁源。
                    frame.rstrip()

                    # ---- 行级 diff 重绘：内容没变的行不写、不超过 fps 上限 ----
                    painter.paint(frame, now)
    except ConnectionRefusedError:
        console.print(
            f"[red]无法连接后端 {url}[/red]\n请先运行: [bold]python main.py serve[/bold]"
        )
    except KeyboardInterrupt:
        console.print("\n[yellow]已退出 CLI[/yellow]")
    finally:
        if th_task is not None:
            th_task.cancel()
