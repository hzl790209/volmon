"""音频设备清单整理：同一物理设备的多 host API 通路合并、噪声项剔除。

Windows 下 PortAudio 会把同一硬件在 MME / DirectSound / WASAPI / WDM-KS 各列一份，
还夹着虚拟映射器、回环混音、WDM-KS 底层端点。这里用**假设备矩阵**验证整理逻辑，
不依赖真实声卡（CI / 无音频设备的环境也能跑）。
"""
import pytest

sounddevice = pytest.importorskip("sounddevice")

from volmon import audio as audio_mod  # noqa: E402

# （设备名, host API, 输入通道, 输出通道, 默认采样率）
FAKE_DEVICES = [
    ("Microsoft 声音映射器 - Input", "MME", 2, 0, 44100.0),
    ("麦克风阵列 (Realtek(R) Audio)", "MME", 2, 0, 44100.0),
    ("麦克风阵列 (Realtek(R) Audio)", "Windows DirectSound", 2, 0, 44100.0),
    ("麦克风阵列 (Realtek(R) Audio)", "Windows WASAPI", 2, 0, 48000.0),
    ("麦克风阵列 (Realtek HD Audio Mic Array input)", "Windows WDM-KS", 2, 0, 44100.0),
    ("主声音捕获驱动程序", "Windows DirectSound", 2, 0, 44100.0),
    ("麦克风 (Realtek HD Audio Mic input)", "Windows WDM-KS", 2, 0, 44100.0),
    ("立体声混音 (Realtek HD Audio Stereo input)", "Windows WDM-KS", 2, 0, 48000.0),
    ("耳机 (HECATE GM400)", "MME", 2, 0, 44100.0),
    ("耳机 (HECATE GM400)", "Windows WASAPI", 2, 0, 48000.0),
    ("扬声器 (Realtek(R) Audio)", "MME", 0, 2, 44100.0),
    ("扬声器 (Realtek(R) Audio)", "Windows WASAPI", 0, 2, 48000.0),
    ("Microsoft 声音映射器 - Output", "MME", 0, 2, 44100.0),
    ("主声音驱动程序", "Windows DirectSound", 0, 2, 44100.0),
    ("Speakers 1 (Realtek HD Audio output with SST)", "Windows WDM-KS", 0, 2, 44100.0),
    ("Speakers (Nahimic Wave Speaker)", "Windows WDM-KS", 0, 8, 48000.0),
]

HOSTAPIS = ["MME", "Windows DirectSound", "Windows WASAPI", "Windows WDM-KS"]


class FakeSd:
    """模拟 PortAudio 枚举；``check_*`` 按默认采样率决定"能不能以该采样率打开"。"""

    def query_devices(self, device=None, kind=None):
        out = [{
            "name": n,
            "hostapi": HOSTAPIS.index(ha),
            "max_input_channels": mi,
            "max_output_channels": mo,
            "default_samplerate": sr,
        } for (n, ha, mi, mo, sr) in FAKE_DEVICES]
        if device is None:
            return out
        return out[device] if isinstance(device, int) else out

    def query_hostapis(self):
        return [{"name": x} for x in HOSTAPIS]

    def check_input_settings(self, device=None, samplerate=None, channels=None):
        d = self.query_devices(device)
        if d["max_input_channels"] < (channels or 1):
            raise ValueError("invalid channel count")
        if samplerate != d["default_samplerate"]:
            raise ValueError("invalid sample rate")

    def check_output_settings(self, device=None, samplerate=None, channels=None):
        d = self.query_devices(device)
        if d["max_output_channels"] < (channels or 1):
            raise ValueError("invalid channel count")
        if samplerate != d["default_samplerate"]:
            raise ValueError("invalid sample rate")


@pytest.fixture
def fake_sd(monkeypatch):
    fake = FakeSd()
    monkeypatch.setattr(audio_mod, "sd", fake)
    return fake


def _names(items):
    return [x["name"] for x in items]


def test_group_key_merges_cross_hostapi_full_names():
    """同一硬件的不同全名（含**嵌套括号**）必须归到同一个键。

    ``Realtek(R) Audio`` 内含括号，若限定"括号内不能有括号"就会匹配失败、
    导致跨名归并失效——这里正是防该回归。
    """
    assert audio_mod._device_group_key("麦克风阵列 (Realtek(R) Audio)") == "麦克风阵列"
    assert audio_mod._device_group_key(
        "麦克风阵列 (Realtek HD Audio Mic Array input)") == "麦克风阵列"
    # 不同硬件不得被合并
    assert audio_mod._device_group_key("麦克风 (Realtek HD Audio Mic input)") == "麦克风"
    # 括号内不是驱动/厂商后缀时不合并（避免把两台耳机误当成一台）
    assert audio_mod._device_group_key("耳机 (HECATE GM400)") == "耳机 (hecate gm400)"


def test_duplicate_hostapi_variants_collapse_into_one(fake_sd):
    """同一物理设备的 4 个通路合并成一项，且自动挑**能开**的那个作代表。"""
    items = audio_mod.enriched_device_list("input")
    mic = next(x for x in items if x["group"] == "麦克风阵列")
    assert mic["variant_count"] == 4, "麦克风阵列 有 MME/DS/WASAPI/WDM-KS 四个通路"
    # WASAPI(#2) 只支持 48000，服务用 44100 -> 不能选它，否则每次开流都先失败再回退
    assert mic["hostapi"] == "MME"
    assert mic["index"] == 1
    assert {"Windows DirectSound", "Windows WASAPI"}.issubset(set(mic["alt_hostapis"]))


def test_output_duplicates_collapse(fake_sd):
    items = audio_mod.enriched_device_list("output")
    spk = next(x for x in items if x["group"] == "扬声器")
    assert spk["variant_count"] == 2
    assert spk["hostapi"] == "MME"          # WASAPI(#11) 不支持 44100


def test_noise_items_hidden_by_default(fake_sd):
    """虚拟映射器 / 回环混音 / WDM-KS 底层端点默认不出现。"""
    ins = _names(audio_mod.enriched_device_list("input"))
    outs = _names(audio_mod.enriched_device_list("output"))

    assert "Microsoft 声音映射器 - Input" not in ins, "等于「默认设备」，无需单列"
    assert "主声音捕获驱动程序" not in ins
    assert "立体声混音 (Realtek HD Audio Stereo input)" not in ins, "回环设备不是麦克风"
    assert "麦克风 (Realtek HD Audio Mic input)" not in ins, "WDM-KS 底层端点"
    assert "Microsoft 声音映射器 - Output" not in outs
    assert "主声音驱动程序" not in outs
    assert not any("Nahimic" in n for n in outs), "Nahimic 虚拟音效设备不应出现在精简列表"

    # 真正能用的设备必须还在
    assert any("麦克风阵列" in n for n in ins)
    assert "耳机 (HECATE GM400)" in ins
    assert any("扬声器" in n for n in outs)


def test_include_all_reveals_hidden_items_with_reason(fake_sd):
    """「显示全部设备」要把底层端点带原因一并返回，且默认列表不超过全量。"""
    simple = audio_mod.enriched_device_list("input")
    full = audio_mod.enriched_device_list("input", include_all=True)
    assert len(full) > len(simple)

    mapper = next(x for x in full if x["name"] == "Microsoft 声音映射器 - Input")
    assert mapper["hidden"] is True
    assert "虚拟" in mapper["hidden_reason"]
    loopback = next(x for x in full if "立体声混音" in x["name"])
    assert loopback["hidden"] is True and "回环" in loopback["hidden_reason"]


def test_alias_used_as_display_name(fake_sd):
    """友好别名优先作为展示名（例如把系统名显示成"3.5mm耳麦"）。"""
    items = audio_mod.enriched_device_list(
        "input", aliases={"麦克风阵列": "内置阵列麦"})
    mic = next(x for x in items if x["group"] == "麦克风阵列")
    assert mic["display"] == "内置阵列麦"
    assert mic["alias"] == "内置阵列麦"


def test_resolve_device_id_prefers_variant_that_can_open(fake_sd):
    """解析设备名时应挑支持服务采样率的通路，避免「选了 A、实际跑 B」。"""
    idx = audio_mod.resolve_device_id("耳机 (HECATE GM400)", kind="input")
    # #8 = MME（支持 44100）；#9 = WASAPI（只支持 48000，打开必失败）
    assert idx == 8


def test_all_noise_items_still_recoverable(monkeypatch):
    """极端情况：若所有设备都被判为噪声项，整理层会清空，但全量模式仍能取回。

    server 侧有兜底——精简结果为空时回退全量，保证用户永远有的选。
    """
    class _AllVirtual(FakeSd):
        def query_devices(self, device=None, kind=None):
            out = [{"name": "Microsoft 声音映射器 - Input", "hostapi": 0,
                    "max_input_channels": 2, "max_output_channels": 0,
                    "default_samplerate": 44100.0}]
            return out if device is None else out[device]

    monkeypatch.setattr(audio_mod, "sd", _AllVirtual())
    assert audio_mod.enriched_device_list("input") == []
    assert len(audio_mod.enriched_device_list("input", include_all=True)) == 1
