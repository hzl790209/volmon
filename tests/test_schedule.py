"""分时段策略（volmon/schedule.py）单测 + 服务端按时段生效集成测试。

覆盖：
  - 时间解析、跨零点窗口判定（如 22:00 → 07:00）
  - 未启用分时段时与旧行为完全一致（向后兼容）
  - 启用后阈值 / 持续 / 报警开关按时段覆盖
  - 两个时段未覆盖全天时给出提示
  - L2：服务端确实按当前时段下发有效阈值
"""
import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, ".")

import pytest  # noqa: E402

from volmon import schedule as S  # noqa: E402


def _hm(m):
    return f"{(m // 60) % 24:02d}:{m % 60:02d}"


def _window_around_now(delta=5):
    """返回一个必定包含"当前时刻"的窗口（用于稳定构造测试场景）。"""
    m = S.now_minutes()
    return _hm(m - delta), _hm(m + delta)


def _window_away_from_now():
    """返回一个必定**不包含**当前时刻的窗口。"""
    m = S.now_minutes()
    return _hm(m + 60), _hm(m + 120)


def _base_cfg(**over):
    from volmon import config as C

    c = dict(C.DEFAULTS)
    c.update(over)
    return c


# ---------------- L1：时间解析与窗口判定 ----------------
def test_parse_hm():
    assert S.parse_hm("07:30") == 450
    assert S.parse_hm("22:00") == 1320
    assert S.parse_hm("00:00") == 0
    assert S.parse_hm("bad") == 0, "解析失败应回退到 0，不应抛异常"


def test_in_window_supports_crossing_midnight():
    # 不跨零点
    assert S.in_window(480, 420, 1320) is True    # 08:00 in 07:00-22:00
    assert S.in_window(300, 420, 1320) is False   # 05:00 not in
    # 跨零点：22:00 -> 07:00
    assert S.in_window(1380, 1320, 420) is True   # 23:00
    assert S.in_window(180, 1320, 420) is True    # 03:00
    assert S.in_window(600, 1320, 420) is False   # 10:00
    # start == end 视为全天
    assert S.in_window(0, 0, 0) is True


def test_active_period_day_and_night():
    cfg = _base_cfg(day_start="07:00", day_end="22:00",
                    night_start="22:00", night_end="07:00")
    assert S.active_period(cfg, _epoch_at(12, 0)) == S.DAY
    assert S.active_period(cfg, _epoch_at(21, 59)) == S.DAY
    assert S.active_period(cfg, _epoch_at(22, 0)) == S.NIGHT
    assert S.active_period(cfg, _epoch_at(3, 0)) == S.NIGHT
    assert S.active_period(cfg, _epoch_at(6, 59)) == S.NIGHT


def _epoch_at(hour, minute):
    t = time.localtime()
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday,
                        hour, minute, 0, 0, 0, t.tm_isdst))


# ---------------- L1：有效配置解析 ----------------
def test_disabled_schedule_keeps_global_behavior():
    """未启用时必须与旧行为完全一致——回归保护，避免意外改变用户现有阈值。"""
    cfg = _base_cfg(schedule_enabled=False, threshold_db=70.0,
                    day_threshold_db=99.0, night_threshold_db=11.0)
    st = S.resolve(cfg)
    assert st["applied"] is False
    assert st["config"]["threshold_db"] == 70.0
    assert st["config"] == cfg


def test_enabled_schedule_applies_period_values():
    d0, d1 = _window_around_now()
    cfg = _base_cfg(schedule_enabled=True,
                    day_start=d0, day_end=d1,
                    night_start="22:00", night_end="07:00",
                    day_threshold_db=70.0, day_alarm_enabled=False,
                    night_threshold_db=55.0, night_alarm_enabled=True)
    st = S.resolve(cfg)
    assert st["applied"] is True
    assert st["period"] == S.DAY
    assert st["period_label"] == "白天"
    assert st["config"]["threshold_db"] == 70.0
    assert st["config"]["alarm_enabled"] is False

    # 把晚间窗口挪到"现在"，白天窗口挪开 -> 应立即按晚间策略
    n0, n1 = _window_around_now()
    a0, a1 = _window_away_from_now()
    cfg.update(day_start=a0, day_end=a1, night_start=n0, night_end=n1)
    st2 = S.resolve(cfg)
    assert st2["period"] == S.NIGHT
    assert st2["period_label"] == "晚间"
    assert st2["config"]["threshold_db"] == 55.0
    assert st2["config"]["alarm_enabled"] is True


def test_validate_detects_gap_and_overlap():
    ok = S.validate(_base_cfg(day_start="07:00", day_end="22:00",
                              night_start="22:00", night_end="07:00"))
    assert ok == [], f"互补的两个时段不应有提示，实际 {ok}"

    gap = S.validate(_base_cfg(day_start="07:00", day_end="20:00",
                               night_start="22:00", night_end="07:00"))
    assert any("空档" in w for w in gap), f"应提示空档，实际 {gap}"

    ov = S.validate(_base_cfg(day_start="07:00", day_end="23:00",
                              night_start="22:00", night_end="07:00"))
    assert any("重叠" in w for w in ov), f"应提示重叠，实际 {ov}"


def test_next_switch_minutes():
    cfg = _base_cfg(day_start="07:00", day_end="22:00",
                    night_start="22:00", night_end="07:00")
    # 12:00 处于白天，距离 22:00 还有 10 小时
    n = S.next_switch_minutes(cfg, _epoch_at(12, 0))
    assert n == 600, f"12:00 距 22:00 应为 600 分钟，实际 {n}"


# ---------------- L2：服务端按时段下发有效阈值 ----------------
def test_server_applies_period_threshold(monkeypatch):
    """服务端广播与 /api/schedule 必须用**当前时段**的阈值，而不是全局阈值。"""
    pytest.importorskip("aiohttp")
    from volmon import alerting as A
    from volmon import config as C
    from volmon import server as server_mod
    from volmon import sim as sim_mod

    tmp = tempfile.mkdtemp()
    monkeypatch.chdir(tmp)
    cfg = C.Config(path=os.path.join(tmp, "config.json"))
    cfg.set("sample_interval_ms", 50)
    cfg.set("threshold_db", 40.0)          # 全局阈值
    cfg.set("schedule_enabled", True)
    cfg.set("day_threshold_db", 70.0)
    cfg.set("night_threshold_db", 55.0)

    # 白天窗口覆盖"现在"
    d0, d1 = _window_around_now()
    cfg.set("day_start", d0); cfg.set("day_end", d1)
    a0, a1 = _window_away_from_now()
    cfg.set("night_start", a0); cfg.set("night_end", a1)

    from volmon import storage as ST

    store = ST.Storage(os.path.join(tmp, "v.db"))
    alm = A.AlertManager(store, threshold=cfg.get("threshold_db"), sustain_sec=3.0)
    srv = server_mod.MonitorServer(sim_mod.SimEngine(), store, cfg, alm)

    async def _run():
        from aiohttp.test_utils import TestClient, TestServer

        client = TestClient(TestServer(srv.app))
        await client.start_server()
        task = asyncio.create_task(srv.loop())
        try:
            r = await client.get("/api/schedule")
            j = await r.json()
            assert j["enabled"] is True
            assert j["period"] == S.DAY
            assert j["threshold_db"] == 70.0, "白天应生效 70 dB，而非全局 40 dB"

            ws = await client.ws_connect("/ws")
            d = (await asyncio.wait_for(ws.receive(), timeout=3.0)).json()
            assert d["period"] == S.DAY
            assert d["schedule_applied"] is True
            assert d["threshold"] == 70.0
            await ws.close()

            # 切到晚间：把晚间窗口挪到"现在"
            n0, n1 = _window_around_now()
            cfg.set("day_start", a0); cfg.set("day_end", a1)
            cfg.set("night_start", n0); cfg.set("night_end", n1)
            await asyncio.sleep(0.3)
            j2 = await (await client.get("/api/schedule")).json()
            assert j2["period"] == S.NIGHT
            assert j2["threshold_db"] == 55.0
            d2 = None
            ws2 = await client.ws_connect("/ws")
            d2 = (await asyncio.wait_for(ws2.receive(), timeout=3.0)).json()
            assert d2["threshold"] == 55.0
            await ws2.close()
        finally:
            task.cancel()
            await client.close()

    asyncio.run(_run())
