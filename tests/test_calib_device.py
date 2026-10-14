"""校准相关回归测试：统计口径（能量域 RMS） + 校准值按设备隔离。

背景（用户反馈"校准完读数还是不准"）：
1) 旧实现把每个音频块的 dBFS 做**算术平均**（对数域平均 = 几何平均），起伏大时系统性
   低于真实能量 RMS——照它校准，校准完仍对不上专业设备读数。
2) 校准偏移只有一个全局值 ``calib_offset_db``，换麦克风（3.5mm 耳麦 -> USB 台式麦）
   会把别的设备十几 dB 的灵敏度差套过来，读数虚高到离谱。
"""
import asyncio
import os
import sys
import time

import pytest

pytest.importorskip("aiohttp")
pytest.importorskip("sounddevice")

from aiohttp.test_utils import TestServer, TestClient  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402

from volmon import alerting as A  # noqa: E402
from volmon import audio  # noqa: E402
from volmon import config as C  # noqa: E402
from volmon import server as server_mod  # noqa: E402
from volmon import storage as S  # noqa: E402


class FakeEngine:
    """返回固定 dBFS 的假引擎；device 可改，用于验证"换设备后套用不同偏移"。"""

    def __init__(self, raw=-40.0, device=None, hostapi="MME"):
        self._raw = raw
        self.device = device
        self.hostapi = hostapi
        self.rescanned = False

    def raw_db(self):
        return self._raw

    def stream_path(self):
        """正在使用的通路信息（真实引擎由 AudioEngine 提供）。"""
        return {"index": 3, "name": self.device, "hostapi": self.hostapi,
                "hostapi_slug": audio.hostapi_slug(self.hostapi),
                "group": audio.device_group_key(self.device or ""),
                "fallback": False, "fallback_reason": "", "running": True}

    def rms_db_over(self, seconds=5.0):
        self.last_window = seconds
        return self._raw

    def recent_raw_avg(self, n=100):
        return self._raw

    def list_input_devices(self):
        return [(0, "默认设备", 1), (1, "USB 麦克风", 1)]

    def list_output_devices(self):
        return [(1, "USB 耳机", 2)]

    def device_list(self, kind="input", aliases=None):
        names = ["默认设备", "USB 麦克风"] if kind == "input" else ["USB 耳机"]
        return [{"index": i, "name": n, "channels": 1, "hostapi": "MME",
                 "display": n, "alias": "", "is_dup": False} for i, n in enumerate(names)]

    def rescan(self):
        return self.list_input_devices()

    def active_device_info(self, aliases=None):
        name = self.device or "default"
        return {"configured": self.device, "index": 0, "name": name,
                "display": name, "samplerate": 44100, "running": True,
                "hostapi": self.hostapi, "hostapi_slug": audio.hostapi_slug(self.hostapi),
                "fallback": False, "fallback_reason": ""}

    def switch_device(self, device="default"):
        self.device = None if device in (None, "", "default") else device
        return self.active_device_info()


def _new_server(raw=-40.0, device="麦克风 (Realtek(R) Audio)"):
    cfg = C.Config("data/config.json")
    store = S.Storage("data/volmon.db")
    alm = A.AlertManager(store, threshold=70.0, sustain_sec=3.0)
    eng = FakeEngine(raw=raw, device=device)
    return server_mod.MonitorServer(eng, store, cfg, alm), cfg, eng


# ---------------------------------------------------------------------------
# 1. 统计口径：能量域 RMS 而不是对数域平均
# ---------------------------------------------------------------------------
def test_rms_db_over_is_energy_average_not_log_average():
    """回归：校准基准必须是能量的线性平均，不能是各块 dB 的算术平均。

    数据一半 -20 dBFS（幅度 0.1）、一半 -40 dBFS（幅度 0.01）：
      能量域 RMS = 20·log10(sqrt((0.1²+0.01²)/2)) ≈ -22.97 dB   <- 声级计口径
      对数域平均 = (-20 + -40)/2 = -30 dB                        <- 旧实现，差约 7 dB
    """
    eng = audio.AudioEngine()
    for amp in (0.1, 0.01):
        block = np.full((2048, 1), amp, dtype=np.float32)
        eng._callback(block, 2048, None, None)
        time.sleep(0.001)

    expected = 20.0 * np.log10(np.sqrt((0.1 ** 2 + 0.01 ** 2) / 2.0))
    got = eng.rms_db_over(5.0)
    assert abs(got - expected) < 0.1, f"应为能量域 RMS {expected:.2f}，实际 {got:.2f}"
    # 明确排除"对数域平均"这种错误算法（-30 dB 附近）
    assert got - (-30.0) > 5.0, f"疑似又退回对数域平均：{got:.2f}"


def test_device_group_key_merges_hostapi_variants():
    """同一物理设备跨 host API / 驱动全名应落到同一个校准键。"""
    assert audio.device_group_key("麦克风 (Realtek(R) Audio)") == "麦克风"
    assert audio.device_group_key("麦克风 (Realtek HD Audio Mic input)") == "麦克风"
    assert audio.device_group_key("麦克风阵列 (Realtek(R) Audio)") == "麦克风阵列"
    assert audio.device_group_key("耳机式麦克风 (HECATE GM400)") == "耳机式麦克风 (hecate gm400)"


# ---------------------------------------------------------------------------
# 2. 校准值按设备隔离
# ---------------------------------------------------------------------------
def test_calib_state_prefers_device_entry(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    srv, cfg, eng = _new_server()

    cfg.set("calib_offset_db", 44.7)          # 旧配置：3.5mm 耳麦校准出来的偏移
    off, key, dev, src = srv.calib_state()
    assert key.startswith("麦克风") and off == 44.7 and src == "global"

    # 整台设备（不分通路）的记录：可用，但标记为 device（非当前通路专属）
    cfg.set("device_calib", {"麦克风": 12.3})
    off, key, dev, src = srv.calib_state()
    assert (off, key, src) == (12.3, "麦克风@mme", "device")

    # 当前通路（MME）有专属记录时优先用它
    cfg.set("device_calib", {"麦克风": 12.3, "麦克风@mme": 30.0})
    off, key, dev, src = srv.calib_state()
    assert (off, key, src) == (30.0, "麦克风@mme", "path")

    # 换到另一台麦克风：不能沿用"麦克风"的偏移
    eng.device = "耳机式麦克风 (HECATE GM400)"
    off, key, dev, src = srv.calib_state()
    assert key.startswith("耳机式麦克风 (hecate gm400)")
    assert src == "global", "未校准的设备应标为'沿用旧全局值'，不能当作本设备已校准"

    # 把它校一次 -> 两台设备各自的偏移互不干扰
    table = dict(cfg.get("device_calib") or {})
    table[key] = -4.2
    cfg.set("device_calib", table)
    off, key, dev, src = srv.calib_state()
    assert (off, src) == (-4.2, "path")
    assert cfg.get("device_calib")["麦克风"] == 12.3


def test_calib_prefers_exact_path_over_device(monkeypatch, tmp_path):
    """回归：校准值按「设备 + 通路」区分；同设备换通路时不能直接套用（读数会差十几 dB）。"""
    monkeypatch.chdir(tmp_path)
    srv, cfg, eng = _new_server()
    cfg.set("device_calib", {"麦克风": 12.3, "麦克风@mme": 20.0})

    eng.hostapi = "MME"
    off, key, dev, src = srv.calib_state()
    assert (off, key, src) == (20.0, "麦克风@mme", "path")

    # 同一麦克风改走 DirectSound：没有该通路的校准 → 退到整机那个，并标成 device（需提示）
    eng.hostapi = "Windows DirectSound"
    off, key, dev, src = srv.calib_state()
    assert (off, key, src) == (12.3, "麦克风@directsound", "device")

    # 校准写入时默认绑定当前通路
    cfg.set("device_calib", {})
    eng.hostapi = "MME"
    table = dict(cfg.get("device_calib") or {})
    table["麦克风@mme"] = 8.0
    cfg.set("device_calib", table)
    off, key, dev, src = srv.calib_state()
    assert (off, src) == (8.0, "path")


def test_calib_state_estimate_when_nothing_configured(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    srv, cfg, eng = _new_server()
    off, key, dev, src = srv.calib_state()
    assert (off, src) == (0.0, "estimate")


# ---------------------------------------------------------------------------
# 3. REST：GET/POST/清除/异常
# ---------------------------------------------------------------------------
def test_api_calib_roundtrip_and_clear(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def _run():
        srv, cfg, eng = _new_server()
        cfg.set("calib_offset_db", 44.7)
        server, client = TestServer(srv.app), None
        client = TestClient(server)
        await client.start_server()
        try:
            # 未校准：base = 能量 RMS(-40) + ref(90) = 50 dB 估算
            r = await client.get("/api/calib?window=5")
            j = await r.json()
            assert j["ok"] and j["base_avg"] == 50.0
            assert j["source"] == "global" and j["calibrated"] is False
            assert j["device_key"].startswith("麦克风")
            assert j["path_exact"] is False and j["hostapi_slug"] == "mme"

            # 用专业设备实测 65 dB 校准 -> 偏移 15，写入**当前通路**
            r = await client.post("/api/calib", json={"real_db": 65.0, "window": 5})
            j = await r.json()
            assert j["ok"] and j["applied"] == 15.0
            assert cfg.get("device_calib")["麦克风@mme"] == 15.0
            assert cfg.get("calib_offset_db") == 44.7, "不应改动旧全局值"

            # 校准后：读数（能量 RMS + ref + 该通路偏移）应精确等于实测值
            j = await (await client.get("/api/calib?window=5")).json()
            assert j["displayed"] == 65.0 and j["calibrated"] is True
            assert j["source"] == "path" and j["path_exact"] is True

            # 换到没有校准记录的设备 -> 不能套用上面那 15 dB
            eng.device = "耳机式麦克风 (HECATE GM400)"
            j = await (await client.get("/api/calib?window=5")).json()
            assert j["calibrated"] is False and j["source"] == "global"

            # 清除当前（HECATE）设备的校准：不应动到"麦克风@mme"的记录
            j = await (await client.post("/api/calib", json={"clear": True})).json()
            assert j["ok"] and j["cleared"] is True
            assert cfg.get("device_calib")["麦克风@mme"] == 15.0, "只清除当前设备自己的记录"
        finally:
            await client.close()

    asyncio.run(_run())


def test_api_calib_rejects_invalid_input(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def _run():
        srv, cfg, eng = _new_server()
        client = TestClient(TestServer(srv.app))
        await client.start_server()
        try:
            # 缺少 real_db
            r = await client.post("/api/calib", json={})
            assert r.status == 400
            # 超出合理范围
            r = await client.post("/api/calib", json={"real_db": 999})
            assert r.status == 400
            # 静音状态下算出的偏移是假的：必须拒绝
            eng._raw = -100.0
            r = await client.post("/api/calib", json={"real_db": 65.0})
            assert r.status == 400
            j = await r.json()
            assert "无有效信号" in j["error"]
        finally:
            await client.close()

    asyncio.run(_run())


def test_ws_and_status_report_per_device_calib(tmp_path, monkeypatch):
    """WebSocket 心跳与 /api/status 都应体现"当前设备是否已校准"。"""
    monkeypatch.chdir(tmp_path)

    async def _run():
        srv, cfg, eng = _new_server()
        cfg.set("sample_interval_ms", 50)
        client = TestClient(TestServer(srv.app))
        await client.start_server()
        loop_task = asyncio.create_task(srv.loop())
        try:
            cfg.set("device_calib", {"麦克风": 15.0})
            ws = await client.ws_connect("/ws")
            msg = await asyncio.wait_for(ws.receive(), timeout=3.0)
            d = msg.json()
            assert d["calibrated"] is True and d["calib_source"] == "device"
            assert d["db"] == 65.0, "-40 dBFS + 90 参考 + 15 该设备偏移 = 65 dB"

            j = await (await client.get("/api/policy")).json()
            assert j["calibrated"] is True and j["calib_device_key"] == "麦克风@mme"
            # 记录是按"整机"存的（无 @通路 后缀）→ 可用但 calib_exact=False，面板会提示重校
            assert j["input_hostapi"] == "MME" and j["calib_source"] == "device"
            assert j["calib_exact"] is False
            assert j["global_calib_offset_db"] == 0.0
            await ws.close()
        finally:
            loop_task.cancel()
            await client.close()

    asyncio.run(_run())
