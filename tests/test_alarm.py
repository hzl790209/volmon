"""音量报警（volmon.alarm）单测 + 服务端静默抑制集成测试。

L1：AlarmManager 状态机（idle/playing/cooldown、触发、抑制、配置同步）。
L2：服务端 loop 在报警/冷却期间不落库（不记录报警声为噪音）。
不依赖真实音频设备与端口。
"""
import asyncio
import sys
import time

sys.path.insert(0, ".")

from volmon import alarm as alarm_mod
from volmon import alerting as alerting_mod
from volmon import storage as storage_mod
from volmon import config as config_mod
from volmon import server as server_mod
from volmon import sim as sim_mod


# ---------------- L1：状态机 ----------------
def test_disabled_never_triggers():
    mgr = alarm_mod.AlarmManager(enabled=False, player=lambda d: None)
    now = time.time() * 1000
    assert mgr.trigger(now) is False
    assert mgr.phase(now) == "idle"
    assert mgr.is_suppressed(now) is False


def test_trigger_returns_playing_then_cooldown_then_idle():
    played = []
    mgr = alarm_mod.AlarmManager(enabled=True, duration_sec=2.0, cooldown_sec=1.0, player=played.append)
    t0 = 1_000_000.0
    assert mgr.trigger(t0) is True
    assert played == [2.0]  # 播放器被调用，传入 duration_sec
    # 播放中
    assert mgr.phase(t0 + 500) == "playing"
    assert mgr.is_suppressed(t0 + 500) is True
    # 冷却中（播放结束，但仍在冷却）
    assert mgr.phase(t0 + 2500) == "cooldown"
    assert mgr.is_suppressed(t0 + 2500) is True
    # 冷却结束 -> 恢复 idle 且清理，可再次触发
    assert mgr.phase(t0 + 3500) == "idle"
    assert mgr.is_suppressed(t0 + 3500) is False
    assert mgr.trigger(t0 + 3600) is True  # 可再次触发


def test_no_retrigger_during_playing_or_cooldown():
    mgr = alarm_mod.AlarmManager(enabled=True, duration_sec=5.0, cooldown_sec=3.0, player=lambda d: None)
    t0 = 2_000_000.0
    assert mgr.trigger(t0) is True
    assert mgr.trigger(t0 + 1000) is False  # 播放中
    assert mgr.trigger(t0 + 6000) is False  # 冷却中
    assert mgr.trigger(t0 + 9000) is True   # 结束之后


def test_sync_from_config():
    mgr = alarm_mod.AlarmManager()
    assert mgr.enabled is False
    mgr.sync_from_config({"alarm_enabled": True, "alarm_duration_sec": 12, "alarm_cooldown_sec": 4})
    assert mgr.enabled is True
    assert mgr.duration_sec == 12.0
    assert mgr.cooldown_sec == 4.0


def test_test_play_only_sounds_without_suppression():
    played = []
    mgr = alarm_mod.AlarmManager(enabled=True, player=played.append)
    assert mgr.test_play(1.5) is True
    time.sleep(0.1)
    assert played == [1.5]
    # 试听只发声，不能进入静默窗口
    assert mgr._trigger_ts == 0
    assert mgr.is_suppressed(time.time() * 1000) is False


def test_alerts_pagination_and_local_csv():
    """超量日志：支持分页（不丢数据），且落地到本地 CSV 便于外部工具查看。"""
    import os
    import tempfile

    tmp = tempfile.mkdtemp()
    csv_path = os.path.join(tmp, "alerts.csv")
    store = storage_mod.Storage(os.path.join(tmp, "v.db"), alerts_csv=csv_path)
    now = int(time.time() * 1000)

    # 造 250 条告警（>2 页）
    for i in range(250):
        store.add_alert(now - (250 - i) * 1000, now - (250 - i) * 1000 + 500, 70.0 + i % 10, 70.0)

    # 分页：每页 100，应能取到不同且不重复的片段
    allrows = store.get_alerts(0)
    assert len(allrows) == 250
    p0 = store.get_alerts(0)[0:100]
    p1 = store.get_alerts(0)[100:200]
    p2 = store.get_alerts(0)[200:300]
    assert len(p0) == 100 and len(p1) == 100 and len(p2) == 50
    # 三页拼起来 == 全部（前端分页不会丢数据）
    assert [r["start_ts"] for r in p0 + p1 + p2] == [r["start_ts"] for r in allrows]

    # 本地 CSV 已自动写入，含表头 + 250 行
    assert os.path.exists(csv_path), "告警应自动写入本地 CSV"
    with open(csv_path, encoding="utf-8-sig") as f:
        lines = [l for l in f.read().splitlines() if l.strip()]
    assert len(lines) == 251, f"表头+250行，实际 {len(lines)}"
    assert lines[0].startswith("start_time")

    # 导出接口也可用
    out = os.path.join(tmp, "export_alerts.csv")
    n = store.export_alerts_csv(out, 0)
    assert n == 250 and os.path.exists(out)


def test_uncalibrated_display_is_realistic_positive_spl():
    """回归：未校准时不能把 dBFS（负数）直接当成 dB 显示。

    dB SPL ≈ dBFS + ref_spl_offset_db(默认90) + calib_offset_db，且钳制为非负。
    真实麦克风安静环境约 -46 dBFS -> 约 44 dB SPL（与其专业设备实测值相符）。
    """
    from volmon import config as C

    ref = C.DEFAULTS["ref_spl_offset_db"]
    assert ref == 90.0

    def to_spl(raw, ref_v=ref, off=0.0):
        return max(raw + ref_v + off, 0.0)

    # 典型笔记本内置麦克风读数
    assert to_spl(-46.0) == 44.0      # 安静房间
    assert to_spl(-30.0) == 60.0      # 正常交谈
    assert to_spl(-10.0) == 80.0      # 嘈杂
    assert to_spl(-100.0) == 0.0      # 静音地板：不能出现负数

    # 校准后：实测 44 dB 而估算为 44.5 时，修正 -0.5，读数应精确等于 44
    meas, base = 44.0, to_spl(-45.5)
    assert base == 44.5
    off = round(meas - base, 2)
    assert to_spl(-45.5, off=off) == 44.0


def test_audio_callback_yields_json_serializable_types():
    """回归：真实音频下 np.log10 会产出 np.float64，比较得到 np.bool_，
    而 json.dumps 无法序列化 np.bool_（曾导致 WebSocket 广播持续异常）。
    静音时 RMS=0 走 DBFS_FLOOR 分支是 Python float，反而掩盖了该问题。
    """
    import json

    import numpy as np
    import pytest

    pytest.importorskip("sounddevice")
    from volmon import audio

    eng = audio.AudioEngine()
    indata = (np.ones((2048, 1), dtype=np.float32) * 0.1)  # 非静音
    eng._callback(indata, 2048, None, None)
    v = eng.raw_db()
    assert type(v) is float, f"raw_db 必须是 Python float，实际 {type(v)}"
    over = v > 50.0
    assert type(over) is bool, f"比较结果必须是 Python bool，实际 {type(over)}"
    json.dumps({"db": v, "over": over})  # 不应抛异常


def test_audio_is_silent_detects_digital_silence():
    """静音地板自检：无数据/全为 DBFS_FLOOR 应判定为静音，有真实读数则不是。"""
    import pytest

    pytest.importorskip("sounddevice")
    from volmon import audio

    eng = audio.AudioEngine()
    assert eng.is_silent() is True, "无数据应视为静音（未授权/无输入）"
    eng._buf.extend([audio.DBFS_FLOOR] * 10)
    assert eng.is_silent() is True, "全为静音地板应判定静音"
    eng._buf.extend([-42.0, -38.5])
    assert eng.is_silent() is False, "出现真实读数不应判定静音"


def test_test_play_none_without_player():
    mgr = alarm_mod.AlarmManager(enabled=True, player=None)
    assert mgr.test_play(1.0) is False


# ---------------- L2：服务端静默抑制 ----------------
async def _run_loop_for(srv, seconds):
    task = asyncio.create_task(srv.loop())
    await asyncio.sleep(seconds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_server_suppresses_recording_during_alarm():
    import tempfile, os
    tmp = tempfile.mkdtemp()
    cfg = config_mod.Config(path=os.path.join(tmp, "config.json"))
    cfg.set("alarm_enabled", True)
    cfg.set("sample_interval_ms", 50)
    store = storage_mod.Storage(os.path.join(tmp, "v.db"))
    alm = alerting_mod.AlertManager(store, threshold=70, sustain_sec=3)
    alarm_mgr = alarm_mod.AlarmManager(
        enabled=True, duration_sec=10, cooldown_sec=3, player=lambda d: None
    )
    eng = sim_mod.SimEngine()
    srv = server_mod.MonitorServer(eng, store, cfg, alm, alarm=alarm_mgr)

    counter = {"n": 0}
    store.add_sample = lambda *a, **k: counter.__setitem__("n", counter["n"] + 1)

    # 1) 强制处于"播放中" -> 应完全不落库
    alarm_mgr._trigger_ts = time.time()
    asyncio.run(_run_loop_for(srv, 0.35))
    assert counter["n"] == 0, "报警/冷却期间不应记录采样"

    # 2) 恢复 idle -> 应正常落库
    alarm_mgr._trigger_ts = 0
    asyncio.run(_run_loop_for(srv, 0.35))
    assert counter["n"] > 0, "冷却结束后应恢复采样记录"


# ---------------- 报警静音时段（人要离开一段时间） ----------------
def test_quiet_window_blocks_sound_but_not_monitoring():
    """静音时段：不发声，但 phase 仍为 idle（监测与超量记录照常）。"""
    played = []
    mgr = alarm_mod.AlarmManager(enabled=True, duration_sec=1.0, cooldown_sec=0.5,
                                 player=played.append)
    t0 = time.time() * 1000
    mgr.sync_from_config({
        "alarm_enabled": True, "alarm_duration_sec": 1.0, "alarm_cooldown_sec": 0.5,
        "quiet_until_ms": t0 + 60_000,
    })
    assert mgr.is_quiet(t0) is True
    assert mgr.trigger(t0) is False, "静音时段不应发声"
    assert played == []
    # 与"报警自激静默"区分：静音并不抑制监测
    assert mgr.phase(t0) == "idle"
    assert mgr.is_suppressed(t0) is False

    # 静音到期后恢复发声
    assert mgr.is_quiet(t0 + 61_000) is False
    assert mgr.trigger(t0 + 61_000) is True
    time.sleep(0.05)
    assert len(played) == 1


def test_quiet_remaining_sec():
    mgr = alarm_mod.AlarmManager(enabled=True)
    t0 = time.time() * 1000
    mgr.quiet_until_ms = t0 + 90_000
    assert abs(mgr.quiet_remaining_sec(t0) - 90.0) < 0.5
    assert mgr.quiet_remaining_sec(t0 + 120_000) == 0.0
    mgr.quiet_until_ms = 0.0
    assert mgr.quiet_remaining_sec(t0) == 0.0


# ---------------- 报警声输出设备选择 ----------------
def test_alarm_output_device_comes_from_config():
    mgr = alarm_mod.AlarmManager(enabled=True)
    assert mgr.output_device == "", "默认应为系统默认输出（空串）"
    mgr.sync_from_config({"alarm_enabled": True, "alarm_output_device": "HECATE GM400"})
    assert mgr.output_device == "HECATE GM400"
    mgr.sync_from_config({"alarm_enabled": True, "alarm_output_device": None})
    assert mgr.output_device == ""
    assert config_mod.DEFAULTS.get("alarm_output_device") == ""


def test_player_plays_to_selected_output_device(monkeypatch):
    """报警声必须能从选定输出设备播放（3.5mm 耳机 / USB 耳机 / 本机扬声器）。

    选定设备会先被解析成唯一索引（避免 Windows 同名多设备的 Multiple devices found），
    再传给 sd.play；未选则回退系统默认（device=None）。
    """
    import pytest

    pytest.importorskip("sounddevice")
    import sounddevice as sd

    # 模拟设备名 -> 索引 的解析结果，隔离真实音频设备依赖
    monkeypatch.setattr(alarm_mod, "resolve_device_id",
                        lambda d, kind="output": 5 if d else None)
    monkeypatch.setattr(alarm_mod, "list_matching_indices", lambda d, kind="output": [])

    calls = []
    orig = sd.play
    sd.play = lambda *a, **k: calls.append((a, k))
    try:
        # 选了设备：应传 device=解析后的索引 5
        p = alarm_mod.make_sounddevice_player(device_getter=lambda: "USB 耳机")
        p(0.01)
        # 未选：应传 device=None（系统默认输出）
        p2 = alarm_mod.make_sounddevice_player()
        p2(0.01)
    finally:
        sd.play = orig

    assert len(calls) == 2
    assert calls[0][1].get("device") == 5, f"应把解析后的设备索引传给 sd.play，实际 {calls[0][1]}"
    assert calls[1][1].get("device") is None, "未选择时应使用系统默认输出"


def test_set_player_injection():
    played = []
    mgr = alarm_mod.AlarmManager(enabled=True, duration_sec=1.0, cooldown_sec=0.0)
    mgr.set_player(played.append)
    assert mgr.trigger(time.time() * 1000) is True
    import time as _t

    _t.sleep(0.05)
    assert played == [1.0]


def test_switch_device_rolls_back_on_failure():
    """热切换输入设备失败时应回滚到原设备并重启采集流，不能让采集停死。"""
    import pytest

    pytest.importorskip("sounddevice")
    from volmon import audio

    calls = {"n": 0}

    def fake_start():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("设备不可用")

    eng = audio.AudioEngine(device="原设备")
    eng.start = fake_start
    eng.stop = lambda: None
    eng._stream = object()  # 模拟"采集流正在运行"

    with pytest.raises(RuntimeError):
        eng.switch_device("HECATE GM400")
    assert eng.device == "原设备", "失败后应回滚到原设备"
    assert calls["n"] == 2, "回滚后应重新用原设备启动采集"


def test_active_device_info_when_not_running():
    import pytest

    pytest.importorskip("sounddevice")
    from volmon import audio

    eng = audio.AudioEngine(device="default")
    info = eng.active_device_info()
    assert info["running"] is False
    assert info["configured"] == "default"


def test_quiet_default_config_is_off():
    assert config_mod.DEFAULTS.get("quiet_until_ms") == 0.0
    mgr = alarm_mod.AlarmManager(enabled=True)
    mgr.sync_from_config(dict(config_mod.DEFAULTS))
    assert mgr.is_quiet(time.time() * 1000) is False


def test_server_quiet_keeps_recording_but_mutes_sound():
    """L2：静音时段采样落库照常、报警声不播；取消静音后超阈值应发声。"""
    import os
    import tempfile

    tmp = tempfile.mkdtemp()
    cfg = config_mod.Config(path=os.path.join(tmp, "config.json"))
    cfg.set("alarm_enabled", True)
    cfg.set("sample_interval_ms", 50)
    cfg.set("log_interval_ms", 50)
    cfg.set("quiet_until_ms", time.time() * 1000 + 3_600_000)
    store = storage_mod.Storage(os.path.join(tmp, "v.db"))
    # 阈值取极低值 + 持续 0 秒 -> 保证进入告警态，触发报警。
    # 注意必须写进 cfg：服务端循环会用**配置里的**（分时段）阈值同步给 AlertManager，
    # 直接给 AlertManager 传参会被覆盖掉。
    cfg.set("threshold_db", -100.0)
    cfg.set("sustain_sec", 0.0)
    alm = alerting_mod.AlertManager(store, threshold=-100.0, sustain_sec=0.0)
    played = []
    alarm_mgr = alarm_mod.AlarmManager(enabled=True, duration_sec=1.0, cooldown_sec=0.0,
                                       player=played.append)
    eng = sim_mod.SimEngine()
    srv = server_mod.MonitorServer(eng, store, cfg, alm, alarm=alarm_mgr)

    counter = {"n": 0}
    store.add_sample = lambda *a, **k: counter.__setitem__("n", counter["n"] + 1)

    asyncio.run(_run_loop_for(srv, 0.4))
    assert counter["n"] > 0, "静音时段采样记录应照常进行"
    assert played == [], "静音时段不应播放报警声"

    cfg.set("quiet_until_ms", 0.0)  # 取消静音
    asyncio.run(_run_loop_for(srv, 0.4))
    assert len(played) >= 1, "取消静音后超阈值应恢复发声"


# ---------------- 报警声规避（录音模块现已改为"触发瞬间截断"） ----------------
# 早期方案用"污染窗口"让录音模块跳过报警声那段音频；实测仍会把尾音漏进后置段，
# 故废弃。新方案见 recorder.cut_at_trigger：报警触发时把录音直接定格在触发点，
# 触发点之后的音频一律不录。相关验证已迁移到 tests/test_recorder.py 的
# test_alarm_trigger_cuts_recording_at_trigger_moment 等用例。
