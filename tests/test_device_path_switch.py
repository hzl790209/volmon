"""回归：切换输入设备后读数失准的三个根因。

本机实测（Windows / Realtek + HECATE）：
  1. 同一设备不同 host API 通路电平差极大：麦克风阵列 MME #3 正常有声，
     DirectSound #11 **完全静音**（-100 dBFS）；Terminate/Initialize 之后同一索引还会漂移 5~18 dB。
  2. 首选通路打开失败时**静默**改走别的通路 / 别的设备（3.5mm 麦 ← 阵列麦），
     而校准值是按原来那条通路做的 → 读数立刻失准。
  3. 因此：回退必须**显式暴露**，校准必须能按"设备 + 通路"区分。
"""
import os
import sys

import pytest

pytest.importorskip("sounddevice")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from volmon import audio  # noqa: E402

MME = 0
DS = 1


def _dev(name, hostapi, idx):
    return {"name": name, "max_input_channels": 2, "max_output_channels": 0,
            "hostapi": hostapi, "default_samplerate": 44100, "index": idx}


# 索引表：#3=阵列麦(MME)、#11=阵列麦(DirectSound)、#2=3.5mm 麦(MME)、#1=HECATE(MME)
FAKE_DEVICES = [
    _dev("Microsoft 声音映射器 - Input", MME, 0),
    _dev("耳机式麦克风 (HECATE GM400)", MME, 1),
    _dev("麦克风 (Realtek(R) Audio)", MME, 2),
    _dev("麦克风阵列 (Realtek(R) Audio)", MME, 3),
    _dev("Microsoft 声音映射器 - Output", MME, 4),
    {"name": "扬声器 (Realtek(R) Audio)", "max_input_channels": 0, "max_output_channels": 2,
     "hostapi": MME, "default_samplerate": 44100},
    {"name": "扬声器 (Realtek(R) Audio)", "max_input_channels": 0, "max_output_channels": 2,
     "hostapi": MME, "default_samplerate": 44100},
    {"name": "扬声器 (Realtek(R) Audio)", "max_input_channels": 0, "max_output_channels": 2,
     "hostapi": MME, "default_samplerate": 44100},
    {"name": "扬声器 (Realtek(R) Audio)", "max_input_channels": 0, "max_output_channels": 2,
     "hostapi": MME, "default_samplerate": 44100},
    {"name": "扬声器 (Realtek(R) Audio)", "max_input_channels": 0, "max_output_channels": 2,
     "hostapi": MME, "default_samplerate": 44100},
    {"name": "扬声器 (Realtek(R) Audio)", "max_input_channels": 0, "max_output_channels": 2,
     "hostapi": MME, "default_samplerate": 44100},
    _dev("麦克风阵列 (Realtek(R) Audio)", DS, 11),
]

# PortAudio 内���状态失效的典型报错（真实日志里的原文）
_STALE_MSG = ("Error opening InputStream: Unanticipated host error [PaErrorCode -9999]: "
              "'A device ID has been used that is out of range for your system.' [MME error 2]")


class FakeStream:
    def __init__(self, device):
        self.device = device
        self.samplerate = 44100

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        pass


class FakeSd:
    """按索引表应答的假 PortAudio；``fail`` 里的索引打开会抛指定异常。"""

    def __init__(self, fail=None, fail_msg=_STALE_MSG):
        self.fail = set(fail or ())
        self.fail_msg = fail_msg
        self.opened = None
        self.resets = 0

    def query_devices(self, i=None):
        if i is None:
            return list(FAKE_DEVICES)
        return FAKE_DEVICES[i]

    def query_hostapis(self):
        return [{"name": "MME"}, {"name": "Windows DirectSound"}]

    def check_input_settings(self, device=None, samplerate=None, channels=None):
        # 真实 PortAudio 在这里**不会**失败（只有真正 open 时才报 out of range），
        # 所以原型 simulated failures 必须只体现在 open 阶段，否则代表性失真。
        return True

    def check_output_settings(self, **kw):
        return True

    def InputStream(self, device=None, channels=None, samplerate=None,
                    blocksize=None, callback=None):
        if device in self.fail:
            raise Exception(self.fail_msg)
        self.opened = device
        return FakeStream(device)


def _engine(monkeypatch, fail=None, fail_msg=_STALE_MSG):
    fake = FakeSd(fail=fail, fail_msg=fail_msg)
    monkeypatch.setattr(audio, "sd", fake)
    monkeypatch.setattr(audio.AudioEngine, "rescan_devices",
                        classmethod(lambda cls: None))
    eng = audio.AudioEngine(device="麦克风阵列 (Realtek(R) Audio)")
    return eng, fake


def test_hostapi_slug_mapping():
    assert audio.hostapi_slug("MME") == "mme"
    assert audio.hostapi_slug("Windows DirectSound") == "directsound"
    assert audio.hostapi_slug("Windows WASAPI") == "wasapi"
    assert audio.hostapi_slug("Windows WDM-KS") == "wdm-ks"


def test_port_state_error_is_detected():
    assert audio.is_port_state_error(Exception(_STALE_MSG)) is True
    # 普通的"不支持采样率"不算内部状态失效，不该触发重初始化
    assert audio.is_port_state_error(Exception("Invalid sample rate")) is False


def test_sibling_excludes_other_physical_mic(monkeypatch):
    """回归：阵列麦打不开时，不能把同厂商的 3.5mm 麦当成"同一硬件的变体"。"""
    monkeypatch.setattr(audio, "sd", FakeSd())
    sibs = audio._sibling_input_indices("麦克风阵列 (Realtek(R) Audio)")
    # #2 是「麦克风 (Realtek(R) Audio)」另一台硬件，必须被排除；#11 是同设备同名另一通路，
    # 由 list_matching_indices 负责（不进 sibling）
    assert 2 not in sibs, f"不应把 3.5mm 麦当成阵列麦的变体: {sibs}"


def test_preferred_path_failure_is_marked_as_fallback(monkeypatch):
    """首选 MME#3 打不开 → 走到 DirectSound#11：必须显式标记 fallback 且说明原因。"""
    eng, fake = _engine(monkeypatch, fail={3})
    eng.start()
    meta = eng.stream_path()
    assert meta["index"] == 11, f"实际落在 #{meta['index']}"
    assert meta["fallback"] is True, "换通路必须显式标记，不能再静默发生"
    assert "首选通路" in meta["fallback_reason"]
    assert meta["hostapi_slug"] == "directsound"
    assert meta["name"] == "麦克风阵列 (Realtek(R) Audio)", "仍是用户选的那台设备"


def test_port_state_error_triggers_heal_and_retry(monkeypatch):
    """出现 'device ID out of range' 时应重初始化 PortAudio 一次并重��整条候选链。

    修复前：直接跳到下一个候选（DirectSound，实测静音），且再也不回到首选通路。
    """
    eng, fake = _engine(monkeypatch, fail={3})
    healed = {"n": 0}

    def fake_rescan(cls):
        healed["n"] += 1
        fake.fail.discard(3)      # 模拟重初始化后索引恢复正常

    monkeypatch.setattr(audio.AudioEngine, "rescan_devices", classmethod(fake_rescan))
    eng.start()
    assert healed["n"] == 1, "PortAudio 状态失效时应自愈重初始化一次"
    meta = eng.stream_path()
    assert meta["index"] == 3, f"自愈后应回到首选 MME 通路，实际 #{meta['index']}"
    assert meta["fallback"] is False
    assert meta["hostapi_slug"] == "mme"


def test_last_successful_path_is_preferred_next_time(monkeypatch):
    """打开过的通路下次优先复用，减少切换设备时的抖动。"""
    eng, fake = _engine(monkeypatch)
    eng.start()
    assert fake.opened == 3
    eng._last_index = 11
    eng.stop()
    eng.start()
    assert fake.opened == 11, "上次成功的通路应优先复用"


# ---------------------------------------------------------------------------
# 服务端：热插拔重扫不得再污染本进程的 PortAudio
# ---------------------------------------------------------------------------
def test_rescan_uses_subprocess_probe_not_terminate(monkeypatch, tmp_path):
    """回归：/api/devices/rescan 必须走子进程探针，不能在服务进程里 Terminate/Initialize。

    在进程内重初始化会打乱 host API 索引映射（实测之后 MME 设备报
    "device ID out of range"，被迫改走静音的 DirectSound 通路）→ 换设备后读数失准。
    """
    monkeypatch.chdir(tmp_path)

    pytest.importorskip("aiohttp")
    from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

    from volmon import alerting as A  # noqa: E402
    from volmon import config as C  # noqa: E402
    from volmon import server as server_mod  # noqa: E402
    from volmon import storage as S  # noqa: E402

    import asyncio  # noqa: E402

    class ProbeEngineStub:
        """只关心两件事：rescan() 有没有被调用、probe_subprocess() 返回什么。"""
        def __init__(self):
            self.raw_value = -40.0
            self.device = "麦克风阵列 (Realtek(R) Audio)"
            self.rescanned = False

        def raw_db(self):
            return self.raw_value

        def rms_db_over(self, seconds=5.0):
            return self.raw_value

        def recent_raw_avg(self, n=100):
            return self.raw_value

        def list_input_devices(self):
            return []

        def list_output_devices(self):
            return []

        def device_list(self, kind="input", aliases=None, include_all=False):
            return [{"index": 3, "name": "麦克风阵列 (Realtek(R) Audio)", "channels": 2,
                     "hostapi": "MME", "display": "笔记本自带麦", "alias": "", "is_dup": False,
                     "group": "麦克风阵列", "variants": [], "hidden": False}]

        def active_device_info(self, aliases=None):
            return {"configured": self.device, "index": 3, "name": self.device,
                    "display": self.device, "samplerate": 44100, "running": True,
                    "hostapi": "MME", "hostapi_slug": "mme", "fallback": False,
                    "fallback_reason": ""}

        def stream_path(self):
            return {"index": 3, "name": self.device, "hostapi": "MME",
                    "hostapi_slug": "mme", "group": "麦克风阵列",
                    "fallback": False, "fallback_reason": "", "running": True}

        def rescan(self):
            self.rescanned = True          # 老路径：进程内 Terminate/Initialize
            return self.list_input_devices()

        def probe_subprocess(self, timeout=10.0):
            return {"ok": True, "devices": self.device_list("input"),
                    "output_devices": [], "hidden_input": 2, "hidden_output": 3,
                    "groups": ["麦克风阵列"]}

        def switch_device(self, device="default"):
            self.device = device
            return self.active_device_info()

    cfg = C.Config("data/config.json")
    store = S.Storage("data/volmon.db")
    alm = A.AlertManager(store, threshold=70.0, sustain_sec=3.0)
    eng = ProbeEngineStub()
    srv = server_mod.MonitorServer(eng, store, cfg, alm)

    async def _run():
        client = TestClient(TestServer(srv.app))
        await client.start_server()
        try:
            j = await (await client.post("/api/devices/rescan")).json()
            assert j["ok"] is True
            assert j.get("need_restart") is False, j.get("msg")
            assert len(j["devices"]) == 1
            # devices 结构里 group 已存在 => 本进程已有该设备 => 无需重启
        finally:
            await client.close()

    asyncio.run(_run())
    assert eng.rescanned is False, "不能再走进程内 PortAudio 重初始化（会污染设备索引）"
