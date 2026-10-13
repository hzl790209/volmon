"""校准命令：实测值 -> 软件原始均值 -> 计算并应用校准偏移。"""
import asyncio

import aiohttp


async def calibrate(real_db, host="127.0.0.1", port=8000):
    base = f"http://{host}:{port}"
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{base}/api/raw_avg?n=150") as r:
            if r.status != 200:
                raise RuntimeError("无法获取原始均值，请确认后端已启动")
            j = await r.json()
        # base_avg = 原始 dBFS 均值 + dBFS→dB SPL 参考偏移，即未校准时界面显示的估算声压级。
        # 用实测值减去它，得到的就是对估算值的修正量。
        base_avg = j.get("base_avg", j["raw_avg"] + j.get("ref_offset_db", 0.0))
        offset = round(real_db - base_avg, 2)
        async with s.post(f"{base}/api/config", json={"calib_offset_db": offset}) as r:
            await r.json()
    return raw_avg, offset


def run_calibrate(real_db, host, port):
    raw_avg, offset = asyncio.run(calibrate(real_db, host, port))
    print("校准完成：")
    print(f"  专业设备实测值     = {real_db:.1f} dB SPL")
    print(f"  软件估算值(校准前) = {base_avg:.2f} dB")
    print(f"  校准修正           = {offset:+.2f} dB（已保存并立即生效）")
