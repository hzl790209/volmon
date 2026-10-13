"""L1：终端图表渲染（volmon/cli.py）单测。

不依赖网络与端口，直接构造 (时间戳, dB) 序列验证：
  - 坐标轴行数、每行宽度一致（保证终端对齐）
  - 自适应 Y 轴范围含阈值、最小跨度 30dB
  - 阈值行存在且唯一
rich 未安装时整模块跳过。
"""
import sys

import pytest

sys.path.insert(0, ".")

pytest.importorskip("rich")

from volmon import cli  # noqa: E402


def _hist(n=60, base=45.0, amp=8.0):
    import math
    import time

    t0 = int(time.time() * 1000) - n * 100
    return [(t0 + i * 100, base + amp * math.sin(i / 5.0)) for i in range(n)]


def test_build_chart_axes_are_aligned():
    hist = _hist()
    lines, plot_w, lo, hi, data = cli.build_chart(hist, 55.0, 100, 14)
    assert len(lines) == 14, "行数应等于请求高度"
    # 每行内容宽度必须完全一致，否则终端会错位
    widths = {len(content) for _, content, _ in lines}
    assert len(widths) == 1, f"各行宽度不一致: {widths}"
    assert plot_w == 100 - cli.LAB_W - 12, "应为阈值标注预留右侧空间"


def test_y_range_includes_threshold_and_min_span():
    hist = _hist(base=45.0, amp=2.0)  # 数据跨度很小
    _, _, lo, hi, _ = cli.build_chart(hist, 55.0, 100, 14)
    assert lo <= 45.0 <= hi
    assert lo <= 55.0 <= hi, "阈值必须落在 Y 轴范围内（否则阈值线画不出来）"
    assert hi - lo >= 30, "最小跨度应不小于 30dB"


def test_threshold_row_exists_and_unique():
    hist = _hist()
    lines, _, _, _, _ = cli.build_chart(hist, 55.0, 100, 14)
    thr_rows = [i for i, (_, _, is_thr) in enumerate(lines) if is_thr]
    assert len(thr_rows) == 1, "应恰好有一条阈值行"


def test_render_contains_axis_legend():
    hist = _hist()
    t = cli.render(45.0, -45.0, 55.0, False, False, False, hist, 50.0,
                   alarm_phase="idle", mic_silent=False, width=100, height=14)
    plain = t.plain
    assert "纵轴" in plain and "横轴" in plain
    assert "阈值" in plain


def test_render_shows_version():
    """顶部状态条显示后端版本号，便于确认「改代码后是否已重启生效」。"""
    hist = _hist()
    t = cli.render(45.0, -45.0, 55.0, False, False, False, hist, 50.0,
                   width=100, height=14, version="2.4.0")
    assert "v2.4.0" in t.plain


def test_render_shows_quiet_countdown():
    """报警静音时段应在终端显示剩余时间，并说明「记录照常」。"""
    hist = _hist()
    t = cli.render(45.0, -45.0, 55.0, False, False, False, hist, 50.0,
                   width=100, height=14, quiet_sec=1800)
    plain = t.plain
    assert "静音" in plain
    assert "30:00" in plain, "剩余 1800s 应格式化为 30:00"
    assert "照常" in plain, "需说明监测与记录照常，仅不发声"


def test_fmt_dur():
    assert cli._fmt_dur(0) == "00:00"
    assert cli._fmt_dur(65) == "01:05"
    assert cli._fmt_dur(3600) == "1小时00分"
    assert cli._fmt_dur(5400) == "1小时30分"
