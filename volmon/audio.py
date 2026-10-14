"""音频采集引擎：从麦克风读取音频流，计算 RMS -> dBFS，维护滚动缓冲用于校准。"""
import collections
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time

import numpy as np
import sounddevice as sd

logger = logging.getLogger("volmon.audio")

# dBFS 下限钳制，避免静音时 log10(0) = -inf
DBFS_FLOOR = -100.0

# 线性 RMS 环形缓冲容量：以 blocksize=2048 / 44100Hz 计约 21.5 块每秒，
# 2048 条 ≈ 95 秒，足够覆盖校准所需的取样窗口。
_POWER_BUF_LEN = 2048


def _device_base_name(full):
    """PortAudio 在 Windows 上会把**同一物理设备**按 host API 各列一份，名字相同、仅后缀不同，
    例如 ``耳机式麦克风 (HECATE GM400), Windows WASAPI``。取逗号前的基础名用于匹配。"""
    if "," in full:
        return full.rsplit(",", 1)[0].strip()
    return full.strip()


def _hostapi_name_of(index):
    """返回某设备所属 host API 的名字（如 WASAPI / MME）。

    PortAudio 的 host API 标识是 ``query_hostapis()`` 元组里的**位置序号**，
    ``query_devices(i)['hostapi']`` 返回的就是该序号（不是 'index' 键），据此取名字。
    """
    try:
        hap = sd.query_hostapis()
        ha = sd.query_devices(index).get("hostapi")
        for hap_index, h in enumerate(hap):
            if hap_index == ha:
                return h.get("name", "")
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# 设备清单整理：去重 + 剔除日常用不到的噪声项
#
# Windows 上 PortAudio 会把同一物理设备按 host API 各列一份（MME / DirectSound /
# WASAPI / WDM-KS），再加上驱动底层端点、虚拟映射器、回环混音设备，下拉里动辄十几项
# （实测本机输入 10 项 / 输出 12 项），用户根本分不清该选哪个。这里统一整理：
#   1) 同一物理设备合并成**一项**，自动挑一个能用的通路作为代表；
#   2) 虚拟映射器 / 回环混音 / WDM-KS 底层端点默认不展示，可用「显示全部设备」还原。
# ---------------------------------------------------------------------------

TARGET_SR = 44100  # 服务默认采样率：挑代表通路时必须挑支持它的，否则每次开流都会先失败再回退

# 挑选代表通路的偏好（数值越大越优先），叠加"是否支持 TARGET_SR"后生效
_HOSTAPI_PREF = {"Windows WASAPI": 30, "MME": 20, "Windows DirectSound": 10, "Windows WDM-KS": 0}

# 只是"系统默认设备"的别名：选它就等于选下拉里的「默认设备」，没必要单独列出
_VIRTUAL_ALIAS_RE = re.compile(
    r"(microsoft sound mapper|声音映射器|primary sound (capture )?driver|主声音)", re.I)

# 回环/混音类**输入**设备：抓的是电脑内部播放的声音，不是麦克风（噪声监测用不到）
_LOOPBACK_RE = re.compile(
    r"(stereo\s*mix|立体声混音|混音|what\s+u\s+hear|loopback|回环|wave\s*out"
    r"|(扬声器|speakers?|耳机|headphones?).*output|output\s+with)", re.I)

# 设备全名里括号内的标注出现这些词时，说明括号前的部分才是"物理设备名"，
# 括号内只是驱动/厂商的技术后缀。据此把跨 host API 的不同全名合并成同一台硬件。
_DRIVER_DETAIL_TOKENS = ("realtek", "conexant", "creative", "c-media", "via", "idt",
                         "sigmatel", "hd audio", "nvidia", "amd", "intel", "usb audio",
                         "bluetooth", "nahimic", "high definition")


def hostapi_slug(name):
    """把 host API 名压成稳定的短标识（用于把校准值绑定到"某条具体通路"）。

    同一台麦克风走 MME 与走 DirectSound 时**电平可以差十几 dB**（实测本机麦克风阵列：
    MME #3 正常有声，DirectSound #11 完全静音 -100 dBFS），所以校准必须能区分通路。
    """
    s = (name or "").lower()
    for token, slug in (("wasapi", "wasapi"), ("wdm-ks", "wdm-ks"), ("wdm", "wdm-ks"),
                        ("directsound", "directsound"), ("mme", "mme")):
        if token in s:
            return slug
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "unknown"


# PortAudio **内部状态失效**的特征（区别于"设备不支持某采样率"这类正常错误）。
# 典型：某次 Terminate/Initialize（热插拔重扫）之后或设备被拔出后，索引仍在
# query_devices() 里、但打开时报 "A device ID has been used that is out of range"。
_PORT_STATE_ERR = (
    "out of range", "unanticipated host error", "invalid device",
    "device unavailable", "internal portaudio error", "hostapi is not initialized",
)


def is_port_state_error(err):
    msg = str(err or "").lower()
    return any(t in msg for t in _PORT_STATE_ERR)


def _device_group_key(full):
    """返回设备的"物理身份键"：同一硬件的不同 host API / 驱动全名应得到同一个键。    例：``麦克风阵列 (Realtek(R) Audio)``（MME）与 ``麦克风阵列 (Realtek HD Audio
    Mic Array input)``（WDM-KS）都归并到 ``麦克风阵列``。
    括号内不含厂商/驱动标识时（如 ``耳机 (HECATE GM400)``）不做归并，避免误合并两台设备。
    """
    base = _device_base_name(full)
    # 注意用贪婪的内层 ``(.*)``：设备名里常出现嵌套括号（``Realtek(R) Audio``），
    # 若限定括号内不能含括号就会匹配失败、跨名归并失效。
    m = re.match(r"^(.*?)\s*\((.*)\)\s*$", base)
    if m:
        prefix, detail = m.group(1).strip(), m.group(2).lower()
        if prefix and any(t in detail for t in _DRIVER_DETAIL_TOKENS):
            return prefix.lower()
    return base.lower()


# 对外用名：校准偏移按"物理设备组键"存储，跨 host API / 驱动全名保持稳定。
device_group_key = _device_group_key


def _supports_samplerate(index, kind, samplerate=TARGET_SR, channels=1):
    """该设备能否以给定采样率打开（不真正开流，只是查询能力）。"""
    try:
        if kind == "input":
            sd.check_input_settings(device=index, samplerate=samplerate, channels=channels)
        else:
            sd.check_output_settings(device=index, samplerate=samplerate, channels=channels)
        return True
    except Exception:
        return False


def _variant_rank(index, kind, samplerate=TARGET_SR):
    """给某个 host API 通路打分，用于挑"最可能直接可用"的那一个。

    先保证**能开**（支持服务采样率，WASAPI 端点常只支持 48000 而 MME 支持 44100），
    再按 host API 偏好排序；这样代表通路基本就是引擎实际会用的那个，不会出现
    "选了 A 通路、实际跑在 B 通路"的错位。
    """
    rank = _HOSTAPI_PREF.get(_hostapi_name_of(index), 5)
    if _supports_samplerate(index, kind, samplerate):
        rank += 100
    return rank


def device_hide_reason(name, hostapi, kind):
    """判断设备是否属于"日常用不到"的噪声项。返回 (代号, 说明)；应保留则返回 (None, None)。"""
    if _VIRTUAL_ALIAS_RE.search(name or ""):
        return "virtual", "系统虚拟设备（等同「默认设备」，无需单独选择）"
    if kind == "input" and _LOOPBACK_RE.search(name or ""):
        return "loopback", "回环录音/立体声混音（抓的是电脑内部声音，不是麦克风）"
    if "WDM-KS" in (hostapi or ""):
        return "raw", "系统底层端点（WDM-KS 内核流），一般无需直接选择"
    return None, None


def enriched_device_list(kind, aliases=None, include_all=False, target_sr=TARGET_SR):
    """返回**整理过**的设备列表（每项一个物理设备），供面板下拉与状态展示使用。

    处理两件事：

    1. **去重**：Windows 下同一物理设备按 MME / DirectSound / WASAPI / WDM-KS 各列一份，
       甚至驱动全名都不同（``麦克风阵列 (Realtek(R) Audio)`` vs
       ``麦克风阵列 (Realtek HD Audio Mic Array input)``）。这里按 ``_device_group_key``
       合并成一项，并挑一个"能开且最合适"的通路作为代表，其余放进 ``variants``。
    2. **去噪**：虚拟映射器（等同"默认设备"）、回环混音、WDM-KS 底层端点默认剔除，
       它们选了多半也没用（详见 ``device_hide_reason``）。``include_all=True`` 时全部返回，
       并在每项上标出 ``hidden`` / ``hidden_reason``，供"显示全部设备"用。

    ``display`` 优先取 config 里的友好别名（如把 3.5mm 麦命名为"3.5mm耳麦"），便于辨认。
    """
    aliases = aliases or {}
    want = "max_input_channels" if kind == "input" else "max_output_channels"
    try:
        raw = list(enumerate(sd.query_devices()))
    except Exception:
        raw = []

    groups, order = {}, []
    for i, d in raw:
        if d.get(want, 0) <= 0:
            continue
        name = d["name"]
        key = _device_group_key(name)
        ha = _hostapi_name_of(i)
        hid, reason = device_hide_reason(name, ha, kind)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append({
            "index": i,
            "name": name,
            "channels": int(d.get(want, 0)),
            "hostapi": ha,
            "samplerate": d.get("default_samplerate"),
            "hidden": bool(hid),
            "hidden_kind": hid or "",
            "hidden_reason": reason or "",
        })

    out = []
    for key in order:
        members = groups[key]
        pool = members if include_all else [m for m in members if not m["hidden"]]
        if not pool:
            continue                       # 全部变体都被判为噪声项 -> 整组隐藏
        # 先挑"没被判为噪声"的成员，再在其中按可用性打分；这样同一硬件即便开了
        # 「显示全部」，代表项也仍然是那条最推荐的通路。
        pool = sorted(pool, key=lambda m: (m["hidden"], -_variant_rank(
            m["index"], kind, target_sr)))
        rep = pool[0]
        base_name = _device_base_name(rep["name"])
        # 别名键优先级：组键（"麦克风阵列"）> 基础全名（"麦克风阵列 (Realtek(R) Audio)"）> 全名。
        # 组键优先是为了让"给这台硬件起个名"一次覆盖它所有 host API 通路；
        # 后两者保证旧配置里按全名存的别 names 仍然有效。
        alias = aliases.get(key) or aliases.get(base_name) or aliases.get(rep["name"]) or ""
        out.append({
            "index": rep["index"],
            "name": rep["name"],
            "channels": rep["channels"],
            "hostapi": rep["hostapi"],
            "display": alias or rep["name"],
            "alias": alias,
            # 该设备是否存在多个 host API 通路（面板据此提示"已自动合并"）
            "is_dup": len(members) > 1,
            "group": key,
            "variant_count": len(members),
            "alt_hostapis": [m["hostapi"] for m in pool[1:]],
            "variants": [
                {"index": m["index"], "name": m["name"], "hostapi": m["hostapi"],
                 "channels": m["channels"], "samplerate": m["samplerate"],
                 "hidden": m["hidden"], "hidden_reason": m["hidden_reason"]}
                for m in members
            ],
            "hidden": rep["hidden"],
            "hidden_reason": rep["hidden_reason"],
        })
    return out


def resolve_device_id(device, kind="input"):
    """把配置中的 device（None / 'default' / int / 名字字符串）解析成 sounddevice 可用的单值。

    返回 ``None`` 表示使用系统默认；否则返回 **int 设备索引**，保证传入 ``InputStream`` /
    ``OutputStream`` 时不会出现 "Multiple X devices found" 歧义错误。

    背景：Windows 下 PortAudio 会把同一物理设备按 MME / DirectSound / WASAPI / WDM-KS
    各列一份、基础名完全相同。用户把设备设成 ``耳机式麦克风 (HECATE GM400)`` 这种基础名后，
    sounddevice 按名匹配会命中 4 个，直接抛 ``ValueError: Multiple input devices found``，
    导致引擎在 aiohttp 启动阶段崩溃、服务起不来。

    消解策略（同名多份时）：挑**能开且最合适**的那个——先要求支持 TARGET_SR
    （WASAPI 端点常只支持 48000，用 44100 打开必失败），再按 host API 偏好
    （WASAPI > MME > DirectSound > WDM-KS）排序。这样 UI 选中的通路与引擎实际
    使用的通路基本一致，不会出现"每次开流都先失败再回落"的抖动。
    """
    if device is None or isinstance(device, bool):
        return None
    if isinstance(device, int):
        return device
    try:
        return int(str(device).strip())  # "12" 形式的索引
    except (TypeError, ValueError):
        pass
    name = str(device).strip()
    if not name or name.lower() == "default":
        return None
    try:
        devs = sd.query_devices()
    except Exception:
        logger.warning("枚举音频设备失败，回退系统默认")
        return None
    want = "max_input_channels" if kind == "input" else "max_output_channels"
    matches = []
    for i, d in enumerate(devs):
        if d.get(want, 0) <= 0:
            continue
        base = _device_base_name(d["name"])
        if d["name"] == name or base == name or name in d["name"]:
            matches.append((i, d))
    if not matches:
        # 退一步：配置名本身可能就是基础名，匹配所有同名（跨 host API）
        for i, d in enumerate(devs):
            if d.get(want, 0) <= 0:
                continue
            if _device_base_name(d["name"]) == name:
                matches.append((i, d))
    if not matches:
        logger.warning("未找到%s设备 %r，回退系统默认", "输入" if kind == "input" else "输出", name)
        return None
    if len(matches) == 1:
        return matches[0][0]
    # 多匹配：按下述优先级排序取第一（稳定的排序接口，等于不破坏原顺序）
    best = max(matches, key=lambda it: _variant_rank(it[0], kind, TARGET_SR))[0]
    logger.info("设备 %r 存在 %d 个同名通路，选用 #%d（%s）",
                name, len(matches), best, _hostapi_name_of(best))
    return best


def list_matching_indices(name, kind="input"):
    """返回与设备名匹配的全部设备索引（同名多 host API 变体都算）。

    用于「首选变体打开失败（如 WASAPI 不支持该采样率）时，换同设备的其它
    host API 变体重试」，尽量尊重用户在面板里选的设备，而不是直接退回系统默认。
    """
    if name is None or (isinstance(name, str) and name.strip().lower() == "default"):
        return []
    name = str(name).strip()
    try:
        devs = sd.query_devices()
    except Exception:
        return []
    want = "max_input_channels" if kind == "input" else "max_output_channels"
    out = []
    for i, d in enumerate(devs):
        if d.get(want, 0) <= 0:
            continue
        base = _device_base_name(d["name"])
        if d["name"] == name or base == name or name in d["name"]:
            out.append(i)
    if not out:
        for i, d in enumerate(devs):
            if d.get(want, 0) <= 0:
                continue
            if _device_base_name(d["name"]) == name:
                out.append(i)
    return out


# 常见音频芯片/接口厂商标识，用于判断"两个不同全名的设备是否同一物理硬件"。
_VENDOR_HINTS = ("realtek", "conexant", "creative", "alc", "c-media",
                "usb", "bluetooth", "via", "idt", "sigmatel")
# 输入类关键词：命中才认为是麦克风/录入设备，避免误配到扬声器。
_MIC_TOKENS = {"mic", "microphone", "麦克风", "input", "headset", "headphone", "阵列", "array"}
# 这些词出现时说明是**其它物理设备**（内置阵列麦、立体声混音回环等），
# 绝不能当作"同一 3.5mm 插孔"的备用变体。
_EXCLUDE_TOKENS = {"阵列", "array", "混音", "mix", "立体声", "stereo"}


def _norm_tokens(s):
    """把设备名切成小写 token 集合，供厂商/类型匹配。

    注意：括号/空格/标点都作为分隔符，**不**吃进 token，
    否则 ``(Realtek`` 会被误判为带括号的厂商名而匹配失败。
    """
    s = (s or "").lower()
    return set(re.findall(r"[a-z0-9一-鿿]+", s))


def _sibling_input_indices(name):
    """返回与给定输入设备名'很可能是同一物理设备'的其它输入设备索引。

    用途：消解 Windows 下同一物理麦克风被不同 host API 以**不同全名**列出的情况，
    例如 WDM-KS 的 ``麦克风 (Realtek HD Audio Mic input)`` 与 MME 的
    ``麦克风 (Realtek(R) Audio)`` 其实是同一个 3.5mm 插孔，但基础名不同。

    当选中的精确名打不开时（如 WDM-KS 独占模式失败），自动改用同硬件的其它变体，
    避免静默回退到系统默认、并消除"配置与实际不一致"的虚假告警。
    会排除阵列麦/立体声混音等明显不同的物理设备，避免误配。
    """
    if not name:
        return []
    base = _norm_tokens(name)
    if not (base & _MIC_TOKENS):
        return []
    out = []
    try:
        devs = sd.query_devices()
    except Exception:
        return []
    for i, d in enumerate(devs):
        if d.get("max_input_channels", 0) <= 0:
            continue
        dn = d["name"]
        if dn == name:
            continue
        t2 = _norm_tokens(dn)
        # 必须是录入类设备，且不是阵列麦/立体声混音等其它物理设备
        if not (t2 & _MIC_TOKENS):
            continue
        # 关键：**两台设备在"是不是阵列麦 / 是不是立体声混音"上必须一致**。
        # 否则本机就会出现「麦克风阵列 (Realtek(R) Audio)」打不开时，因为共享 realtek
        # 而被当成同硬件变体、静默回退到「麦克风 (Realtek(R) Audio)」——那是另一个
        # 物理插孔（3.5mm 耳麦），读数与校准值完全不可比。
        if (base & _EXCLUDE_TOKENS) != (t2 & _EXCLUDE_TOKENS):
            continue
        # 共享厂商标识 -> 判定为同一物理设备的不同 host API 变体
        if base & t2 & set(_VENDOR_HINTS):
            if i not in out:
                out.append(i)
    return out


class AudioEngine:
    def __init__(self, device="default", sample_rate=44100, blocksize=2048, buffer_len=200):
        self.sample_rate = sample_rate
        self.blocksize = blocksize
        self.device = None if device in (None, "default") else device
        self._lock = threading.Lock()
        self._latest_raw = DBFS_FLOOR
        self._buf = collections.deque(maxlen=buffer_len)
        # 线性 RMS 历史（monotonic 时间戳, 该块 RMS）：校准与"读数平滑"都在**能量域**聚合，
        # 不能拿 _buf 里的 dB 值做算术平均（那是对数域平均，见 rms_db_over 的说明）。
        self._pow_buf = collections.deque(maxlen=_POWER_BUF_LEN)
        self._stream = None
        self._sink = None           # 原始音频帧出口（录音器用），在 PortAudio 线程里被调用
        # 实际打开通路的相关状态（用于"换 host API 通路后读数会变"的识别与告警）
        self._stream_meta = {}
        self._last_index = None     # 上次成功打开的通路，优先复用，减少切换抖动
        self._preferred_index = None
        self._requested_name = None

    def set_frame_sink(self, fn):
        """注册原始音频帧回调 fn(ndarray[frames, ch])。

        注意：fn 运行在 **PortAudio 回调线程**里，必须极快、不能做 I/O 或加锁等耗时操作，
        否则会造成音频断续（underflow）。录音器内部只是往环形缓冲 memcpy，符合要求。
        """
        self._sink = fn

    @staticmethod
    def list_input_devices():
        """返回可用输入设备列表 [(index, name, max_input_channels), ...]"""
        devs = []
        try:
            for i, d in enumerate(sd.query_devices()):
                if d.get("max_input_channels", 0) > 0:
                    devs.append((i, d["name"], int(d["max_input_channels"])))
        except Exception as e:
            logger.warning("列举输入设备失败: %s", e)
        return devs

    @staticmethod
    def list_output_devices():
        """返回可用输出（播放）设备列表 [(index, name, max_output_channels), ...]。

        用于报警声选择从哪路输出：3.5mm 耳机 / USB 耳机 / 本机扬声器等。
        """
        devs = []
        try:
            for i, d in enumerate(sd.query_devices()):
                if d.get("max_output_channels", 0) > 0:
                    devs.append((i, d["name"], int(d["max_output_channels"])))
        except Exception as e:
            logger.warning("列举输出设备失败: %s", e)
        return devs

    @staticmethod
    def device_list(kind="input", aliases=None, include_all=False):
        """返回整理后的设备字典列表（每项一个物理设备，已去重/去噪）。

        ``kind``: "input"（麦克风）/ "output"（播放）。``aliases`` 为 config 的 device_aliases。
        ``include_all=True`` 时连虚拟/回环/底层端点一并返回（并标 ``hidden_reason``）。
        """
        return enriched_device_list(kind, aliases, include_all=include_all)

    @staticmethod
    def rescan_devices():
        """强制 PortAudio 重新枚举设备——**热插拔后无需重启服务**。

        PortAudio 在进程内只在初始化时枚举一次音频设备，插入 USB 声卡/耳机后
        若不做这一步，`query_devices()` 仍返回旧列表（表现为"新设备不出现"）。

        ⚠️ 副作用：这是把双刃剑。实测它会打乱内部 host API 索引映射，之后
        MME 设备可能报 ``device ID out of range``，被迫改走 DirectSound（而那条
        通路电平完全不同，甚至静音）→ 读数对该设备之前做的校准失效。
        因此**只在万不得已时使用**（如确证读到的是过期设备表），日常"刷新设备列表"
        请走 `probe_subprocess()` —— 用子进程枚举，不动本进程 PortAudio。
        """
        try:
            sd._terminate()
        except Exception as e:
            logger.debug("PortAudio terminate: %s", e)
        try:
            sd._initialize()
        except Exception as e:
            logger.debug("PortAudio initialize: %s", e)

    @staticmethod
    def probe_subprocess(timeout=10.0):
        """在**独立子进程**里枚举音频设备，返回整理好的清单。

        子进程自带全新的 PortAudio，因此能看到刚插入的设备；主进程的 PortAudio
        完全不动，不会出现上面 `rescan_devices()` 提到的索引错乱 / 读数漂移。
        失败（超时 / 打包环境不支持 `-m`）时返回 ``{"ok": False, ...}``，调用方回退即可。
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ)
        env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "volmon.probe"],
                capture_output=True, timeout=timeout, env=env, cwd=root,
            )
            if proc.returncode != 0:
                return {"ok": False, "error": proc.stderr.decode("utf-8", "ignore")[-200:]}
            return json.loads(proc.stdout.decode("utf-8", "ignore"))
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def active_device_info(self, aliases=None):
        """返回采集流**实际**在用的设备信息。

        用于回答"到底在用哪个麦克风"——配置里选的设备与流真正打开的设备可能不一致
        （例如改了配置但没重启服务，或指定设备被拔出后回退到默认设备）。

        ``aliases``：可选的设备友好别名表（config 的 device_aliases），命中则一并返回 ``display``，
        便于面板用"3.5mm耳麦"这类易认的名字展示当前采集设备。
        """
        info = {
            "configured": self.device or "default",
            "index": None,
            "name": None,
            "display": None,
            "samplerate": None,
            "running": self._stream is not None,
            # 实际走的哪条 host API 通路（MME / DirectSound / WASAPI …）+ 是否并非首选。
            # 通路一变电平就可能变，是"换个设备读数不准"最常见的隐藏原因。
            "hostapi": None,
            "hostapi_slug": "",
            "fallback": False,
            "fallback_reason": "",
        }
        meta = self.stream_path()
        info["hostapi"] = meta.get("hostapi")
        info["hostapi_slug"] = meta.get("hostapi_slug") or ""
        info["fallback"] = bool(meta.get("fallback"))
        info["fallback_reason"] = meta.get("fallback_reason") or ""
        try:
            if self._stream is not None:
                d = self._stream.device
                # 单方向流返回 int；双方向返回 (in, out)
                idx = d[0] if isinstance(d, (tuple, list)) else d
                info["index"] = idx if isinstance(idx, int) else None
                try:
                    nm = sd.query_devices(idx)["name"]
                    info["name"] = nm
                    base = _device_base_name(nm)
                    info["display"] = (aliases or {}).get(base) or (aliases or {}).get(nm) or nm
                except Exception:
                    pass
                info["samplerate"] = getattr(self._stream, "samplerate", None)
        except Exception as e:
            logger.debug("读取采集设备信息失败: %s", e)
        return info

    def switch_device(self, device="default"):
        """热切换输入设备（**无需重启服务**）。失败时自动回滚到原设备。

        背景：InputStream 在服务启动时打开，单纯改配置不会生效，
        用户会以为"选了 USB 耳机但拾音还是内置麦克风"。
        """
        old = self.device
        was_running = self._stream is not None
        if was_running:
            self.stop()
        self.device = None if device in (None, "", "default") else device
        # 换了设备：上次成功的那条通路不再优先（它是另一台麦克风的）
        self._last_index = None
        try:
            if was_running:
                self.start()
        except Exception as e:
            logger.warning("切换输入设备到 %r 失败，回滚到 %r: %s", device, old or "default", e)
            self.device = old
            try:
                if was_running:
                    self.start()
            except Exception as e2:
                logger.warning("回滚后重启采集流失败: %s", e2)
            raise
        logger.info("输入设备已切换：%s -> %s", old or "default", self.device or "default")
        return self.active_device_info()

    def rescan(self):
        """停止采集 → 重初始化 PortAudio → 重新枚举 → 恢复采集。返回最新输入设备列表。"""
        was_running = self._stream is not None
        if was_running:
            self.stop()
        AudioEngine.rescan_devices()
        if was_running:
            try:
                self.start()
            except Exception as e:
                logger.warning("重新扫描后恢复采集失败: %s", e)
        return self.list_input_devices()

    def _callback(self, indata, frames, time_info, status):
        if status:
            logger.warning("音频流状态: %s", status)
        try:
            x = indata[:, 0] if indata.ndim > 1 else indata
            x = np.asarray(x, dtype=np.float32)
            if x.size == 0:
                return
            rms = float(np.sqrt(np.mean(x ** 2)))
            # 必须转成 Python 原生 float：np.float64 参与比较会产出 np.bool_，
            # 而 json 无法序列化 np.bool_（会导致 WebSocket 广播异常）
            db = float(20.0 * np.log10(rms)) if rms > 0 else DBFS_FLOOR
            db = float(max(db, DBFS_FLOOR))
            with self._lock:
                self._latest_raw = db
                self._buf.append(db)
                self._pow_buf.append((time.monotonic(), rms))
            # 原始帧交给录音器（先算完 dB 再做，且必须 copy：indata 是 PortAudio 的复用缓冲）
            if self._sink is not None:
                try:
                    self._sink(indata.copy())
                except Exception as e:
                    logger.debug("音频帧出口异常: %s", e)
        except Exception as e:
            logger.debug("回调计算异常: %s", e)

    def _candidate_indices(self):
        """打开输入流时的候选索引顺序（从最贴合用户选择到最兜底）。

        1. 上次**实际成功**过的通路（浮动最小，最优先）
        2. 按名字解析出的首选通路
        3. 同名设备的其它 host API 变体
        4. 同物理设备、不同全名的变体（WDM-KS 名 ↔ MME 名）
        5. 系统默认设备
        """
        out = []
        if getattr(self, "_last_index", None) is not None:
            out.append(self._last_index)
        preferred = resolve_device_id(self.device, kind="input")
        self._preferred_index = preferred
        if preferred is not None:
            out.append(preferred)
            for i in list_matching_indices(self.device, kind="input"):
                if i != preferred:
                    out.append(i)
        for i in _sibling_input_indices(self.device):
            if i not in out:
                out.append(i)
        out.append(None)  # 系统默认
        # 去重保序
        seen, uniq = set(), []
        for i in out:
            key = "none" if i is None else i
            if key in seen:
                continue
            seen.add(key)
            uniq.append(i)
        return uniq

    def start(self):
        """打开采集流。

        **不再悄悄换通路**：Windows 上同一麦克风走 MME / DirectSound / WASAPI 的电平可能
        差十几 dB（甚至 DirectSound 完全静音），一旦首选通路打开失败而回退到别处，
        读数就会和用户之前校准的值对不上——这正是"换个输入设备后就不准"的来源。
        因此这里会：
          * 遇到 PortAudio **内部状态失效**（"device ID out of range" 等）时重初始化一次并重试；
          * 若最终用的不是首选通路，把 `index / hostapi / fallback_reason` 记进
            `stream_path()`，由上层在面板与日志里**明确告知**，而不是无声无息地换路。
        """
        if self._stream is not None:
            return
        self._requested_name = self.device
        last_err = None
        healed = False
        candidates = self._candidate_indices()
        i = 0
        while i < len(candidates):
            c = candidates[i]
            i += 1
            try:
                self._open_stream(c)
                self._note_opened(c)
                return
            except Exception as e:
                last_err = e
                logger.warning("输入设备 #%s 打开失败，尝试下一个候选: %s", c, e)
                # PortAudio 内部设备表坏了（常见于热插拔/重扫之后）：重初始化一次再 resolvesi。
                # 这时**所有**索引都可能已经失效，所以整条候选链重算、重新走一遍。
                if not healed and is_port_state_error(e):
                    healed = True
                    logger.warning(
                        "PortAudio 内部设备表疑似失效（%s）：重初始化后重新解析设备并重试整条候选链", e)
                    AudioEngine.rescan_devices()
                    candidates = self._candidate_indices()
                    i = 0
        raise last_err

    def _note_opened(self, dev):
        """记录本次实际打开的通路；与"用户选的/首选的"不一致时明确标记出来。"""
        meta = {
            "index": dev,
            "name": None,
            "hostapi": None,
            "group": None,
            "requested": getattr(self, "_requested_name", None),
            "requested_group": None,
            "preferred_index": getattr(self, "_preferred_index", None),
            "fallback": False,
            "fallback_reason": "",
        }
        requested_name = getattr(self, "_requested_name", None)
        if requested_name:
            meta["requested_group"] = _device_group_key(requested_name)
        if dev is not None:
            try:
                nm = sd.query_devices(dev)["name"]
                meta["name"] = nm
                meta["hostapi"] = _hostapi_name_of(dev)
                meta["group"] = _device_group_key(nm)
                # 用实际打开的设备全名回填 self.device：消除"同名/同硬件不同 host API"
                # 造成的"配置与实际不一致"虚假告警；若最终只能用系统默认（说明所选设备
                # 已不存在），保留原配置以便上层正确告警。
                self.device = nm
            except Exception:
                pass
            self._last_index = dev
        pref = meta["preferred_index"]
        if meta["group"] and meta["requested_group"] and meta["group"] != meta["requested_group"]:
            meta["fallback"] = True
            meta["fallback_reason"] = (
                f"所选设备 {meta['requested']} 打不开，实际落在 {meta['name']}（不同设备，读数不可比）")
        elif dev is not None and pref is not None and dev != pref:
            meta["fallback"] = True
            meta["fallback_reason"] = (
                f"首选通路 #{pref}（{_hostapi_name_of(pref)}）不可用，改走 "
                f"#{dev}（{meta['hostapi']}）——同一设备不同通路电平可能不同，建议重新校准")
        self._stream_meta = meta
        if meta["fallback"]:
            logger.warning("采集通路已发生改变：%s", meta["fallback_reason"])

    def stream_path(self):
        """当前流**实际**在用的通路信息（含是否并非首选、原因）。

        上层据此在面板/接口里暴露"现在到底走的哪条通路"，并让校准值能按通路区分保存。
        """
        meta = dict(getattr(self, "_stream_meta", None) or {})
        meta.setdefault("index", None)
        meta.setdefault("fallback", False)
        meta.setdefault("fallback_reason", "")
        meta["hostapi_slug"] = hostapi_slug(meta.get("hostapi"))
        meta["running"] = self._stream is not None
        return meta

    def _open_stream(self, dev):
        self._stream = sd.InputStream(
            device=dev,
            channels=1,
            samplerate=self.sample_rate,
            blocksize=self.blocksize,
            callback=self._callback,
        )
        self._stream.start()
        shown = "default"
        if dev is not None:
            try:
                shown = sd.query_devices(dev)["name"]
            except Exception:
                shown = str(dev)
        logger.info("音频采集已启动 (device=%s, sr=%d)", shown, self.sample_rate)

    def stop(self):
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as e:
                logger.warning("停止音频流异常: %s", e)
            self._stream = None
        # 换麦克风后旧设备的 RMS 量纲不能继续参与平均（会造成切换后几秒读数虚高/虚低）
        with self._lock:
            self._pow_buf.clear()
            self._buf.clear()

    def is_silent(self):
        """麦克风是否持续输出"数字静音"。

        常见于：macOS 未授予麦克风权限（流能打开但被静音成全零）、系统输入被静音、
        或选错了输入设备。此时读数会恒为 DBFS_FLOOR，波形呈一条直线，容易被误判为"采集停止"。
        """
        with self._lock:
            vals = list(self._buf)[-50:]
        if not vals:
            return True  # 回调从未产出数据，同样视为无有效输入
        return all(v <= DBFS_FLOOR + 1e-6 for v in vals)

    def raw_db(self):
        with self._lock:
            return self._latest_raw

    def rms_db_over(self, seconds=5.0):
        """返回最近 ``seconds`` 秒的**能量域** RMS（dBFS）。

        为什么不能直接用 ``recent_raw_avg``：那个接口对每个数据块先取 20·log10(RMS)
        再做算术平均，等于在**对数域**求均值（几何平均）。声音有起伏时几何平均系统性
        小于真实能量均值（Jensen 不等式），实测安静房间能差 3~6 dB——而这正是"照着软件
        给的平均值校准、校准完读数还是对不上"的根源。

        专业声级计读的是整段时间的**能量 RMS**（各块功率先线性平均再开方），这里同样
        在线性域聚合，校准基准与声级计口径一致。
        """
        try:
            sec = float(seconds)
        except (TypeError, ValueError):
            sec = 0.0
        if sec <= 0:
            return self.raw_db()
        now = time.monotonic()
        cutoff = now - sec
        with self._lock:
            vals = [r for t, r in self._pow_buf if t >= cutoff]
        if not vals:
            with self._lock:
                vals = [r for _t, r in list(self._pow_buf)[-1:]]
            if not vals:
                return self.raw_db()
        mean_power = float(np.mean(np.square(np.asarray(vals, dtype=np.float64))))
        if mean_power <= 0.0:
            return DBFS_FLOOR
        return float(max(20.0 * np.log10(np.sqrt(mean_power)), DBFS_FLOOR))

    def recent_raw_avg(self, n=100):
        with self._lock:
            vals = list(self._buf)[-n:]
        if not vals:
            return DBFS_FLOOR
        return float(np.mean(vals))
