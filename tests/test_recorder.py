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
    assert rel.endswith(".wav") and "/" in rel   # "YYYY-MM-DD/rec_YYYYMMDD_HHMMSS_ms.wav"
    # 文件名必须自带年月日：单独把 WAV 拷出来、或在播放器列表里，也能看出是哪天录的
    fname = os.path.basename(rel)
    date_part = fname.replace("rec_", "").split("_")[0]
    assert len(date_part) == 8 and date_part.isdigit(), f"文件名应含年月日(YYYYMMDD): {fname}"

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


def _record_once(tmp, **cfg_over):
    """喂一段很轻的声音并触发一次录音，返回 (recorder, wav绝对路径)。"""
    rec = rec_mod.Recorder(_cfg(tmp, rec_pre_sec=1.0, rec_post_sec=1.0, **cfg_over))
    rec.attach(SR, 1)
    np.random.seed(7)                   # 固定随机序列，保证不同模式间可比
    _feed(rec, 1.0, amp=0.01)           # 很轻的声音（高斯噪声峰值约 -30 dBFS）
    assert rec.trigger(peak_db=60.0, reason="over") is not None
    _feed(rec, 1.2, amp=0.01)
    done = rec.tick()
    assert len(done) == 1, "应完成写盘"
    return rec, done[0][1]


def test_saved_wav_keeps_original_level_when_off():
    """off 模式必须**原样保存**：不放大、不归一化。

    用户反馈自动归一化后"听不清内容"——底噪与混响被同比例放大。
    故保留 off 作为页面可选项，off 时严格保持麦克风原始电平。
    """
    tmp = tempfile.mkdtemp()
    _rec, path = _record_once(tmp, rec_gain_mode="off")
    audio = _read_wav(path)
    peak = float(np.max(np.abs(audio)))
    assert 0.02 < peak < 0.2, f"off 应保持麦克风原始音量（约 0.03~0.05），实际 {peak:.3f}"


def test_saved_wav_uses_auto_gain_by_default():
    """默认（auto）应把录音峰值归一化到接近满刻度，回放最响亮。

    修复"录音播放几乎听不到"：之前默认 off（fixed 0dB 等同），弱信号原样存盘，
    回放极小声。auto 峰值归一化到 TARGET_PEAK，既响亮又不会削波失真。
    """
    tmp = tempfile.mkdtemp()
    _rec, path = _record_once(tmp)  # 不指定 -> 走配置默认 auto
    audio = _read_wav(path)
    peak = float(np.max(np.abs(audio)))
    assert peak > 0.95 and peak <= 1.0, f"默认应归一化到接近满刻度，实际 {peak:.3f}"


def test_saved_wav_applies_configured_gain():
    """按配置施加增益：fixed 按指定 dB 放大，auto 归一化到满刻度。"""
    # fixed +20 dB ≈ 10 倍
    tmp = tempfile.mkdtemp()
    _r1, p_fixed = _record_once(tmp, rec_gain_mode="fixed", rec_gain_db=20.0)
    a_fixed = float(np.max(np.abs(_read_wav(p_fixed))))

    # auto：顶到满刻度
    tmp2 = tempfile.mkdtemp()
    _r2, p_auto = _record_once(tmp2, rec_gain_mode="auto")
    a_auto = float(np.max(np.abs(_read_wav(p_auto))))

    # 原始
    tmp3 = tempfile.mkdtemp()
    _r3, p_off = _record_once(tmp3, rec_gain_mode="off")
    a_off = float(np.max(np.abs(_read_wav(p_off))))

    assert a_fixed == pytest.approx(a_off * 10.0, rel=0.02), (
        f"+20 dB 应放大约 10 倍，实际 {a_fixed / a_off:.2f} 倍")
    assert a_auto > 0.95 and a_auto <= 1.0, f"auto 应归一化到接近满刻度，实际 {a_auto:.3f}"


def test_gain_auto_normalizes_every_non_silent_recording():
    """auto 模式：所有**非静音**录音都要被归一化到接近满刻度（回放音量最大）。"""
    # 1) 数字静音：不放大（否则量化噪声会被放大成一片刺耳白噪）
    silence = np.full((1000, 1), 1e-6, dtype=np.float32)
    out, g = rec_mod.Recorder._apply_gain(silence, mode="auto")
    assert g == 1.0, "静音段不应施加增益"
    assert float(np.max(np.abs(out))) <= rec_mod.SILENCE_PEAK

    # 2) 从"刚过静音线"到"已经很响"，各种响度都应被顶到目标峰值
    for amp in (2e-5, 1e-3, 0.01, 0.2, 0.9):
        _out, _g = rec_mod.Recorder._apply_gain(
            np.full((1000, 1), amp, dtype=np.float32), mode="auto")
        peak = float(np.max(np.abs(_out)))
        assert abs(peak - rec_mod.TARGET_PEAK) < 1e-3, (
            f"幅值 {amp} 的录音应归一化到 {rec_mod.TARGET_PEAK}，实际 {peak:.4f}")

    # 3) 自洽性：增益上限必须足以覆盖静音阈值。否则极安静的录音会被上限截断，
    #    达不到满刻度——那就违背了"auto 模式下所有录音音量最大"。
    max_gain = 10 ** (rec_mod.MAX_GAIN_DB / 20.0)
    assert max_gain >= rec_mod.TARGET_PEAK / rec_mod.SILENCE_PEAK, (
        "MAX_GAIN_DB 不足以覆盖 SILENCE_PEAK，极安静录音将无法归一化到满刻度")


def test_gain_off_and_fixed_modes():
    """off 原样返回；fixed 按 dB 精确缩放，且被限制在 ±60 dB 内（防极端值把音频毁掉）。"""
    data = np.full((1000, 1), 0.01, dtype=np.float32)

    out_off, g_off = rec_mod.Recorder._apply_gain(data, mode="off", gain_db=40.0)
    assert g_off == 1.0 and float(np.max(np.abs(out_off))) == pytest.approx(0.01)

    out_pos, g_pos = rec_mod.Recorder._apply_gain(data, mode="fixed", gain_db=20.0)
    assert g_pos == pytest.approx(10.0, rel=1e-6)
    assert float(np.max(np.abs(out_pos))) == pytest.approx(0.1, rel=1e-4)

    out_neg, g_neg = rec_mod.Recorder._apply_gain(data, mode="fixed", gain_db=-20.0)
    assert float(np.max(np.abs(out_neg))) == pytest.approx(0.001, rel=1e-4)
    assert g_neg == pytest.approx(0.1, rel=1e-6)

    # 极端值夹紧到 ±60 dB（10^(60/20)=1000 倍），不会因手滑填 999 而彻底失真
    _o, g_max = rec_mod.Recorder._apply_gain(data, mode="fixed", gain_db=999.0)
    assert g_max == pytest.approx(10 ** (rec_mod.GAIN_DB_MAX / 20.0), rel=1e-6)
    _o2, g_min = rec_mod.Recorder._apply_gain(data, mode="fixed", gain_db=-999.0)
    assert g_min == pytest.approx(10 ** (rec_mod.GAIN_DB_MIN / 20.0), rel=1e-6)

    # 未知模式按 off 处理（配置被手改坏时不至于把音频搞烂）
    _o3, g_bad = rec_mod.Recorder._apply_gain(data, mode="???", gain_db=30.0)
    assert g_bad == 1.0


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
                                rec_cut_on_alarm=True, rec_gain_mode="off"))
    rec.attach(SR, 1)

    _feed_const(rec, 1.5, 0.01)     # 触发前的安静环境
    rec.trigger()                   # 这一刻录音开始（前置段已取好）
    rec.cut_at_trigger(reason="报警触发")   # 紧接着报警触发 -> 截断在触发点
    # 触发点之后：报警声被麦克风拾回（幅值很大），但任务已移出 pending，不再接收
    _feed_const(rec, 1.0, 0.9)
    done = rec.tick()
    assert len(done) == 1, "截断任务应被立即写盘"
    audio = _read_wav(done[0][1])
    assert done[0][2]["cut"] is True
    # 判据说明：本测试**显式 off 增益**，环境音幅值 0.01、报警声 0.9，
    # 因此**绝对峰值**就能直接区分报警声是否混入：
    #   1) 峰值——真混进来那 0.9 幅值的报警段，峰值必然远大于环境音的 0.01；
    #   2) 时长——报警声长 1 秒，混进来的话文件必然超出前置段长度。
    # （增益与截断逻辑无关：auto 增益在写盘阶段才施加，不会改变"报警声是否被截断"的结论。）
    secs = len(audio) / SR
    assert secs <= 1.3, f"应只含触发前音频，实际 {secs:.2f}s"
    peak = float(np.max(np.abs(audio)))
    assert peak < 0.2, f"报警声不应入 WAV（环境音仅 0.01），实际峰值 {peak:.3f}"


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
