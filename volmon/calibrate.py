"""校准命令：实测值 -> 软件当前读数 -> 计算并写入**该设备**的校准偏移。

用法：``python main.py calibrate 65.0``（还可加 ``--window 10`` / ``--global`` / ``--clear``）

为什么按设备存：不同麦克风的灵敏度/内含增益差十几 dB，共用同一个偏移必然串味
（用过 3.5mm 耳麦校准出的偏移套到 USB 台式麦上，读数会虚高十几 dB）。
"""
import asyncio

import aiohttp

DEFAULT_WINDOW_SEC = 5.0


async def _call(method, url, **kw):
    async with aiohttp.ClientSession() as s:
        async with getattr(s, method)(url, **kw) as r:
            return r.status, await r.json(content_type=None)


async def calibrate(real_db, host="127.0.0.1", port=8000, window=DEFAULT_WINDOW_SEC,
                    global_scope=False):
    """按当前正在采集的设备写入校准偏移，返回 (base_avg, offset, payload)。"""
    base = f"http://{host}:{port}"
    payload = {"real_db": float(real_db), "window": float(window)}
    if global_scope:
        payload["scope"] = "global"
    status, j = await _call("post", f"{base}/api/calib", json=payload)
    if status != 200 or not j.get("ok"):
        raise RuntimeError(j.get("error") or f"校准失败（HTTP {status}）")
    return j["base_avg"], j["applied"], j


async def clear_calib(host="127.0.0.1", port=8000):
    """清除当前设备的校准记录（回到未校准估算）。"""
    status, j = await _call("post", f"http://{host}:{port}/api/calib", json={"clear": True})
    if status != 200 or not j.get("ok"):
        raise RuntimeError(j.get("error") or f"清除失败（HTTP {status}）")
    return j


async def show_calib(host="127.0.0.1", port=8000, window=DEFAULT_WINDOW_SEC):
    status, j = await _call("get", f"http://{host}:{port}/api/calib?window={float(window)}")
    if status != 200:
        raise RuntimeError(f"读取校准状态失败（HTTP {status}）")
    return j


def run_calibrate(real_db, host, port, window=DEFAULT_WINDOW_SEC, global_scope=False):
    base_avg, offset, j = asyncio.run(
        calibrate(real_db, host, port, window=window, global_scope=global_scope))
    src = {"device": "该设备专属（推荐）", "global": "全局兜底值"}.get(
        j.get("source"), j.get("source"))
    print("校准完成：")
    print(f"  采集设备            = {j.get('device')}　（键 {j.get('device_key')}）")
    print(f"  取样窗口            = {j.get('window_sec')} 秒（能量域 RMS）")
    print(f"  专业设备实测值      = {real_db:.1f} dB SPL")
    print(f"  软件估算值(校准前)  = {base_avg:.2f} dB")
    print(f"  校准修正            = {offset:+.2f} dB（已保存并立即生效）")
    print(f"  保存位置            = {src}")
    print("  提示：换麦克风（插拔耳机/换 USB 麦）后在网页校准面板重新校一次即可，两份偏移互不干扰。")
