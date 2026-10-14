"""**子进程**音频设备探针。

存在的理由：PortAudio 在同一进程里做 ``Terminate()/Initialize()``（过去"热插拔后重新
扫描设备"用的就是这个办法）会破坏内部各 host API 的索引映射。实测本机：
  * 重初始化之后，MME 设备 ``麦克风阵列 #3`` 打开报
    ``A device ID has been used that is out of range for your system``；
  * 被迫回退到 DirectSound ``#11``，而那条通路实测**完全静音**（-100 dBFS）；
  * 同一设备同一索引，重初始化前后读数相差 5~18 dB，之前在校准值顿时失效。

改用**独立子进程**枚举：子进程自带干净的 PortAudio，能看到刚插入的设备；
主进程的 PortAudio 一动不动，正在跑的采集流不受污染，读数也不会漂。

用法：``python -m volmon.probe`` → stdout 输出 JSON。
"""
import json
import sys

try:
    from volmon.audio import device_group_key, enriched_device_list
except ImportError:  # 未安装到 sys.path 时补上项目根目录
    import os

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from volmon.audio import device_group_key, enriched_device_list


def snapshot(aliases=None, include_all=False):
    """返回整理后的输入/输出设备清单（结构与 ``/api/devices`` 一致）。"""
    aliases = aliases or {}
    out = {}
    try:
        for kind, key in (("input", "devices"), ("output", "output_devices")):
            items = enriched_device_list(kind, aliases, include_all=include_all)
            out[key] = items
            out[f"hidden_{kind}"] = sum(1 for x in items if x.get("hidden"))
    except Exception as e:
        return {"ok": False, "error": str(e), "devices": [], "output_devices": []}
    # 附带组键：便于调用方对比"主进程此刻有哪些设备"
    out["groups"] = sorted({
        device_group_key(x.get("name") or "")
        for x in (out.get("devices") or []) + (out.get("output_devices") or [])
    })
    out["ok"] = True
    return out


def main():
    json.dump(snapshot(), sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
