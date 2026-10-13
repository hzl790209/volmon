"""触发录音：环形缓冲 + 前置录音，保证短促声音不被漏掉。

重点验证的正是需求里最担心的那件事——**程序反应再慢，开头也不能缺**：
  1. 先喂 5 秒"正常的低音量"
  2. 中间插入一段**极短（0.2 秒）的高音量**（模拟敲门/喊叫）
  3. 再喂 3 秒
  4. **等到第 8 秒才触发**（模拟判定链路的延迟）
  → 断言：写出的 WAV 里**包含那段 0.2 秒的高音量**，且位置在文件中段（前置段里）
"""
import os
import sys
import tempfile
import time
import wave

sys.path.insert(0, ".")

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from volmon import config as config_mod  # noqa: E402
from volmon import recorder as rec_mod  # noqa: E402
from volmon import storage as storage_mod  # noqa: E402

SR = 8000          # 测试用低采样率，快
BLOCK = 256


def _cfg(tmp, **over):
    cfg = config_mod.Config(path=os.path.join(tmp, "c.json"))
    cfg.set("rec_enabled", True)
    cfg.set("rec_dir", os.path.join(tmp, "recordings"))
    cfg.set("rec_pre_sec", 5.0)
    cfg.set("rec_post_sec", 2.0)
    cfg.set("rec_keep_days", 30)
    cfg.set("rec_max_files", 0)
    cfg.set("rec_max_mb", 0)
    for k, v in over.items():
        cfg.set(k, v)
    return cfg


def _feed(rec, seconds, amp=0.01):
    """喂 seconds 秒的音频（amp=线性幅度）。"""
    n = int(seconds * SR / BLOCK)
    for _ in range(n):
        rec.feed((np.random.randn(BLOCK, 1).astype(np.float32) * amp))


def _read_wav(path):
    with wave.open(path, "rb") as w:
        n = w.getnframes()
        raw = w.readframes(n)
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32767.0


# ---------------- 环形缓冲 ----------------
def test_ring_buffer_tail_and_wrap():
    rb = rec_mod.RingBuffer(SR, 2.0, 1)          # 容量 2 秒
    assert rb.capacity == 2 * SR
    rb.write(np.zeros((SR, 1), np.float32))      # 写 1 秒
    rb.write(np.ones((SR * 2, 1), np.float32))   # 再写 2 秒（超过容量，触发回绕）
    tail = rb.tail(1.0)
    assert tail.shape[0] == SR
    assert float(np.mean(tail)) == pytest.approx(1.0, abs=1e-6), "回绕后取最近数据应为后写的"
    # 写入超过容量的一整块
    rb.write(np.full((SR * 3, 1), 0.5, np.float32))
    assert float(np.mean(rb.tail(0.5))) == pytest.approx(0.5, abs=1e-6)


# ---------------- 核心：短促声音不丢 ----------------
def test_short_burst_is_captured_even_if_trigger_is_late():
    """延迟 3 秒才触发，0.2 秒的突发声仍完整在录音里。"""
    tmp = tempfile.mkdtemp()
    rec = rec_mod.Recorder(_cfg(tmp))
    rec.attach(SR, 1)

    _feed(rec, 2.0, amp=0.01)                    # 安静
    _feed(rec, 0.2, amp=0.9)                     # ← 短促突发（要录到的目标）
    _feed(rec, 4.0, amp=0.01)                    # 又安静（模拟判定链路**延迟 4 秒**才反应）

    rel = rec.trigger(peak_db=80.0, reason="报警")
    assert rel is not None, "开启后应能触发录音"
    assert rel.endswith(".wav") and "/" in rel   # "YYYY-MM-DD/rec_HHMMSS_ms.wav"

    _feed(rec, 2.5, amp=0.02)                    # 触发后的音频
    done = rec.tick()
    assert len(done) == 1, "录够时长后应完成写盘"
    rel2, path, meta = done[0]
    assert os.path.exists(path)

    audio = _read_wav(path)
    total_sec = len(audio) / SR
    assert total_sec >= 7.0, f"应含前 5s + 后 ~2.5s，实际 {total_sec:.1f}s"

    # 突发声在文件里：找最大幅值窗口
    win = int(0.2 * SR)
    peaks = [float(np.max(np.abs(audio[i:i + win]))) for i in range(0, len(audio) - win, win // 2)]
    assert max(peaks) > 0.5, f"录音里应能找到那段 0.9 幅值的突发声，实际最大 {max(peaks):.3f}"

    # 突发声应位于**前置段**（即文件中点之前）——证明是"补录"来的，不是触发后才录到的
    scores = [float(np.max(np.abs(audio[i:i + win]))) for i in range(0, len(audio) - win, win // 2)]
    burst_pos = int(np.argmax(scores)) * (win // 2)
    assert burst_pos < len(audio) / 2, "突发声必须在前置缓冲段（触发之前）"
    assert meta["reason"] == "报警" and meta["peak_db"] == 80.0


def test_trigger_disabled_returns_none():
    tmp = tempfile.mkdtemp()
    rec = rec_mod.Recorder(_cfg(tmp, rec_enabled=False))
    rec.attach(SR, 1)
    _feed(rec, 1.0)
    assert rec.trigger() is None
    assert rec.tick() == []


def test_pending_keeps_recording_until_post_elapsed():
    """没录够 post 秒之前不该写盘。"""
    tmp = tempfile.mkdtemp()
    rec = rec_mod.Recorder(_cfg(tmp, rec_post_sec=2.0))
    rec.attach(SR, 1)
    _feed(rec, 1.0)
    rec.trigger()
    assert len(rec.pending) == 1
    assert rec.tick() == [], "时长不够不应完成"
    _feed(rec, 2.5)
    out = rec.tick()
    assert len(out) == 1 and os.path.exists(out[0][1])


def test_concurrent_recordings_are_capped():
    tmp = tempfile.mkdtemp()
    rec = rec_mod.Recorder(_cfg(tmp, rec_post_sec=1.0))
    rec.attach(SR, 1)
    _feed(rec, 1.0)
    ok = [rec.trigger() for _ in range(6)]
    assert sum(1 for x in ok if x) == rec_mod.MAX_CONCURRENT, "并发任务数应被限制"


# ---------------- 清理策略 ----------------
def test_prune_by_keep_days_and_quota(tmp=None):
    tmp = tempfile.mkdtemp()
    rec = rec_mod.Recorder(_cfg(tmp, rec_keep_days=30, rec_max_files=3, rec_max_mb=0))
    base = rec.base_dir()
    import datetime
    for d in (0, 1, 40, 45):
        day = (datetime.date.today() - datetime.timedelta(days=d)).isoformat()
        os.makedirs(os.path.join(base, day), exist_ok=True)
        for i in range(2):
            open(os.path.join(base, day, f"rec_{i}.wav"), "wb").write(b"x" * 1024)
    assert rec.stats()["count"] == 8
    removed = rec.prune()
    assert len(removed) > 0
    left = rec.stats()["count"]
    assert left <= 3, f"文件数配额应生效，剩余 {left}"
    # 过期目录被删
    import datetime as dt
    old = (dt.date.today() - dt.timedelta(days=40)).isoformat()
    assert not os.path.isdir(os.path.join(base, old)), "超过保留天数的目录应被清理"


def test_prune_by_size_quota():
    tmp = tempfile.mkdtemp()
    rec = rec_mod.Recorder(_cfg(tmp, rec_keep_days=0, rec_max_files=0, rec_max_mb=0.002))  # ~2KB
    base = rec.base_dir()
    import datetime
    day = datetime.date.today().isoformat()
    os.makedirs(os.path.join(base, day), exist_ok=True)
    for i in range(5):
        with open(os.path.join(base, day, f"r{i}.wav"), "wb") as f:
            f.write(b"x" * 1024)
        time.sleep(0.01)
    rec.prune()
    assert rec.stats()["total_mb"] * 1024 * 1024 <= 4096, "大小配额应生效"


def test_clear_all_removes_everything():
    tmp = tempfile.mkdtemp()
    cfg = config_mod.Config(path=os.path.join(tmp, "c.json"))
    cfg.set("rec_enabled", True)
    cfg.set("rec_dir", os.path.join(tmp, "recordings"))
    rec = rec_mod.Recorder(cfg)
    day = os.path.join(tmp, "recordings", "2026-09-27")
    os.makedirs(day, exist_ok=True)
    for i in range(3):
        with open(os.path.join(day, f"rec_{i}.wav"), "w") as f:
            f.write("x")
    assert rec.stats()["count"] == 3
    # 清空所有
    assert rec.clear_all() == 3
    assert not os.path.isdir(day), "清空后应无录音目录"
    assert rec.stats()["count"] == 0
    # 幂等：再清一次不再删任何东西、不报错
    assert rec.clear_all() == 0


# ---------------- 与告警记录关联 ----------------
def test_link_audio_matches_the_right_alert():
    tmp = tempfile.mkdtemp()
    store = storage_mod.Storage(os.path.join(tmp, "v.db"))
    t0 = int(time.time() * 1000) - 5000
    store.add_alert(t0, t0 + 2000, 60.0, 50.0)
    store.add_alert(t0 - 60000, t0 - 58000, 55.0, 50.0)
    # 触发时刻落在第一条超标区间内
    assert store.link_audio(t0 + 1000, "2026-09-27/rec_test.wav") is True
    rows = store.get_alerts(0)
    assert rows[0]["audio_file"] == "2026-09-27/rec_test.wav"
    assert rows[1]["audio_file"] == ""


def test_alerts_csv_has_audio_column():
    tmp = tempfile.mkdtemp()
    csv_path = os.path.join(tmp, "alerts.csv")
    store = storage_mod.Storage(os.path.join(tmp, "v.db"), alerts_csv=csv_path)
    now = int(time.time() * 1000)
    store.add_alert(now, now + 1000, 60.0, 55.0,
                    context={"period": "白天", "audio_file": "2026-09-27/rec_a.wav"})
    import csv as csvmod
    with open(csv_path, encoding="utf-8-sig") as f:
        rows = list(csvmod.reader(f))
    assert "audio_file" in rows[0]
    assert rows[1][-1] == "2026-09-27/rec_a.wav"


def _feed_const(rec, seconds, value):
    """喂 seconds 秒的**恒定幅值**音频（便于确定性断言，不用随机噪声）。"""
    n = int(seconds * SR / BLOCK)
    for _ in range(n):
        rec.feed(np.full((BLOCK, 1), value, dtype=np.float32))


# ---------------- 报警声自激：报警触发即截断（仅保留触发前） ----------------
def test_alarm_trigger_cuts_recording_at_trigger_moment():
    """报警一触发，正在录的这段立即停在触发瞬间，触发点之后的报警声不进 WAV。"""
    tmp = tempfile.mkdtemp()
    rec = rec_mod.Recorder(_cfg(tmp, rec_pre_sec=1.0, rec_post_sec=2.0,
                                rec_cut_on_alarm=True))
    rec.attach(SR, 1)

    _feed_const(rec, 1.5, 0.01)     # 触发前的安静环境
    rec.trigger()                   # 这一刻录音开始（前置段已取好）
    rec.cut_at_trigger(reason="报警触发")   # 紧接着报警触发 -> 截断在触发点
    # 触发点之后：报警声被麦克风拾回（幅值很大），但任务已移出 pending，不再接收
    _feed_const(rec, 1.0, 0.9)
    done = rec.tick()
    assert len(done) == 1, "截断任务应被立即写盘"
    audio = _read_wav(done[0][1])
    peak = float(np.max(np.abs(audio)))
    assert peak < 0.2, f"触发点之后的报警声不应入 WAV，实际最大幅值 {peak:.3f}"
    assert done[0][2]["cut"] is True
    # 文件长度应≈前置段 1 秒（不该含触发点后的 2 秒报警）
    assert len(audio) / SR <= 1.3, f"应只含触发前音频，实际 {len(audio)/SR:.2f}s"


def test_recording_includes_alarm_when_cut_disabled():
    """对照组：关掉"截断"后，同样的报警声**应该**被录进去。

    没有这条对照，上一条测试即使屏蔽逻辑根本没生效也会通过（噪声幅值本来就低）。
    """
    tmp = tempfile.mkdtemp()
    rec = rec_mod.Recorder(_cfg(tmp, rec_pre_sec=1.0, rec_post_sec=1.0,
                                rec_cut_on_alarm=False))
    rec.attach(SR, 1)
    _feed_const(rec, 1.5, 0.01)
    rec.trigger()
    # 没截断：继续喂报警声 + 后续音频，凑满后置段
    _feed_const(rec, 1.0, 0.9)
    _feed_const(rec, 1.5, 0.02)
    done = rec.tick()
    assert len(done) == 1
    audio = _read_wav(done[0][1])
    assert float(np.max(np.abs(audio))) > 0.5, "未开启截断时报警声应照常录进去"
    assert done[0][2]["cut"] is False


def test_cut_keeps_pre_trigger_audio():
    """截断后 WAV 仍包含"触发前"那段真实声音（开头不丢）。"""
    tmp = tempfile.mkdtemp()
    rec = rec_mod.Recorder(_cfg(tmp, rec_pre_sec=1.0, rec_post_sec=1.0,
                                rec_cut_on_alarm=True))
    rec.attach(SR, 1)
    _feed_const(rec, 0.5, 0.01)
    _feed_const(rec, 0.5, 0.9)      # ← 触发前的真实突发声（要保留）
    rec.trigger()
    rec.cut_at_trigger(reason="报警触发")
    done = rec.tick()
    audio = _read_wav(done[0][1])
    win = int(0.3 * SR)
    peaks = [float(np.max(np.abs(audio[i:i + win]))) for i in range(0, len(audio) - win, win // 2)]
    assert max(peaks) > 0.5, f"触发前的真实声音应被保留，实际最大 {max(peaks):.3f}"


def test_cut_does_nothing_without_pending():
    """没有进行中的录音时，cut_at_trigger 是安全的空操作。"""
    tmp = tempfile.mkdtemp()
    rec = rec_mod.Recorder(_cfg(tmp, rec_cut_on_alarm=True))
    rec.attach(SR, 1)
    assert rec.cut_at_trigger() == 0
    assert rec.tick() == []


def test_stats_expose_cut_on_alarm_state():
    tmp = tempfile.mkdtemp()
    rec = rec_mod.Recorder(_cfg(tmp, rec_cut_on_alarm=True))
    rec.attach(SR, 1)
    assert rec.stats()["cut_on_alarm"] is True
    rec.set_cut_on_alarm(False)
    assert rec.stats()["cut_on_alarm"] is False
