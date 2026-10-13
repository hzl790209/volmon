"""L2 集成测试：后端服务（aiohttp）路由 + WebSocket 广播 + Web 页面。

对应《开发规范》§4.2 L2。使用 FakeEngine 替代真实麦克风，避免依赖音频设备/PortAudio。
aiohttp 未安装时整模块自动跳过（importorskip）。
运行： pytest tests/test_server.py -v
"""
import asyncio
import os
import subprocess
import sys
import tempfile
import time

import pytest

pytest.importorskip("aiohttp")

from aiohttp.test_utils import TestServer, TestClient  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from volmon import __version__  # noqa: E402
from volmon import config as C, storage as S, alerting as A, server as server_mod  # noqa: E402


def _tmp(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.chdir(d)
    return d


class FakeEngine:
    """替代真实麦克风引擎，返回固定原始值。"""
    def __init__(self, raw=-40.0):
        self._raw = raw
        self.device = None
        self.switched_to = None
        self.rescanned = False

    def raw_db(self):
        return self._raw

    def recent_raw_avg(self, n=100):
        return self._raw

    def list_input_devices(self):
        return [(0, "默认设备", 1), (1, "USB 麦克风", 1)]

    def list_output_devices(self):
        return [(1, "USB 耳机", 2), (3, "本机扬声器", 2)]

    def rescan(self):
        """模拟热插拔后重新枚举（不触碰真实 PortAudio）。"""
        self.rescanned = True
        return self.list_input_devices()

    def active_device_info(self):
        return {"configured": self.device, "index": 0,
                "name": self.device or "default", "samplerate": 44100, "running": True}

    def switch_device(self, device="default"):
        """记录热切换，供断言"改配置后采集流确实切过去了"。"""
        self.device = None if device in (None, "", "default") else device
        self.switched_to = device
        return self.active_device_info()


async def _make_server():
    cfg = C.Config("data/config.json")
    cfg.set("sample_interval_ms", 50)
    store = S.Storage("data/volmon.db")
    alm = A.AlertManager(store, threshold=70.0, sustain_sec=3.0)
    eng = FakeEngine(raw=-40.0)
    srv = server_mod.MonitorServer(eng, store, cfg, alm)
    server = TestServer(srv.app)
    client = TestClient(server)
    await client.start_server()
    loop_task = asyncio.create_task(srv.loop())
    return client, loop_task, cfg, store, eng


def test_rest_endpoints_and_ws(monkeypatch):
    _tmp(monkeypatch)

    async def _run():
        client, loop_task, cfg, store, eng = await _make_server()
        try:
            r = await client.get("/api/health")
            body = await r.json()
            assert body["ok"] is True
            # 版本号随心跳暴露，用于确认线上跑的是哪一份代码
            assert body["version"] == __version__

            r = await client.get("/api/config")
            assert (await r.json())["threshold_db"] == 70.0

            r = await client.post(
                "/api/config",
                json={"threshold_db": 55.0, "calib_offset_db": 5.0},
            )
            assert (await r.json())["ok"] is True
            assert cfg.get("threshold_db") == 55.0
            assert cfg.get("calib_offset_db") == 5.0

            r = await client.get("/api/devices")
            devs = await r.json()
            assert any(x["name"] == "USB 麦克风" for x in devs)

            r = await client.get("/api/raw_avg")
            assert (await r.json())["raw_avg"] == -40.0

            r = await client.get("/api/samples?hours=1")
            assert isinstance(await r.json(), list)

            r = await client.get("/")
            assert r.status == 200
            html = await r.text()
            assert "音量" in html and "实时" in html

            ws = await client.ws_connect("/ws")
            msg = await asyncio.wait_for(ws.receive(), timeout=3.0)
            data = msg.json()
            assert {"t", "raw_db", "db", "threshold", "over", "alert"}.issubset(data.keys())
            # db = dBFS原始值(-40) + dBFS→dB SPL 参考偏移(90) + 校准偏移(5) = 55
            assert data["db"] == 55.0
            assert data["threshold"] == 55.0
            await ws.close()
        finally:
            loop_task.cancel()
            await client.close()

    asyncio.run(_run())


def test_quiet_endpoints(monkeypatch):
    """报警静音时段：设置（分钟）/ 查询 / 取消 / 非法参数。"""
    _tmp(monkeypatch)

    async def _run():
        client, loop_task, cfg, store, eng = await _make_server()
        try:
            r = await client.get("/api/quiet")
            j = await r.json()
            assert j["quiet_active"] is False, "默认不应处于静音"

            r = await client.post("/api/quiet", json={"minutes": 30})
            j = await r.json()
            assert j["ok"] is True
            assert j["quiet_active"] is True
            assert 29 * 60 <= j["quiet_remaining_sec"] <= 30 * 60
            assert j["until_text"], "应返回可读的截止时间"

            r = await client.get("/api/quiet")
            assert (await r.json())["quiet_active"] is True

            # 广播里也要带上，供 Web/CLI 显示倒计时
            ws = await client.ws_connect("/ws")
            data = (await asyncio.wait_for(ws.receive(), timeout=3.0)).json()
            assert data["quiet_active"] is True
            assert data["quiet_remaining_sec"] > 0
            await ws.close()

            r = await client.post("/api/quiet", json={"cancel": True})
            j = await r.json()
            assert j["ok"] is True and j["quiet_active"] is False

            assert (await client.post("/api/quiet", json={"minutes": -1})).status == 400
            assert (await client.post("/api/quiet", json={})).status == 400
        finally:
            loop_task.cancel()
            await client.close()

    asyncio.run(_run())


def test_web_page_shows_version_badge(monkeypatch):
    """《开发规范》§1.5：每个版本都必须在页面上显示版本号（禁止硬编码，来自 __version__）。"""
    _tmp(monkeypatch)

    async def _run():
        client, loop_task, cfg, store, eng = await _make_server()
        try:
            r = await client.get("/")
            html = await r.text()
            assert 'id="ver"' in html, "页面必须有版本号展示元素"
            assert "showVersion" in html, "版本号应由 JS 从后端填入，而非硬编码"
            # 页面里不得把某个具体版本号写死（唯一来源是 volmon/__init__.py）
            import re

            hardcoded = re.search(r'id="ver"[^>]*>\s*v\d+\.\d+\.\d+', html)
            assert hardcoded is None, "版本号禁止在页面中硬编码"
        finally:
            loop_task.cancel()
            await client.close()

    asyncio.run(_run())


def test_output_devices_and_rescan(monkeypatch):
    """热插拔后能重新枚举设备；报警声输出设备可枚举可选。"""
    _tmp(monkeypatch)

    async def _run():
        client, loop_task, cfg, store, eng = await _make_server()
        try:
            r = await client.get("/api/output-devices")
            outs = await r.json()
            assert any(x["name"] == "USB 耳机" for x in outs), "应列出输出设备"
            assert all(x["channels"] > 0 for x in outs)

            r = await client.post("/api/devices/rescan")
            j = await r.json()
            assert j["ok"] is True
            assert getattr(eng, "rescanned", False), "应触发引擎重新扫描"
            assert any(x["name"] == "USB 麦克风" for x in j["devices"])
            assert any(x["name"] == "本机扬声器" for x in j["output_devices"])

            # 报警输出设备可写
            r = await client.post("/api/config", json={"alarm_output_device": "USB 耳机"})
            assert (await r.json())["ok"] is True
            assert cfg.get("alarm_output_device") == "USB 耳机"
        finally:
            loop_task.cancel()
            await client.close()

    asyncio.run(_run())


def test_input_device_hot_switch(monkeypatch):
    """改了输入设备必须**立刻切换采集流**，不能等重启服务。

    回归背景：InputStream 在服务启动时打开，此前只改配置不生效，
    用户会遇到"选了 USB 耳机但拾音还是内置麦克风"。
    """
    _tmp(monkeypatch)

    async def _run():
        client, loop_task, cfg, store, eng = await _make_server()
        try:
            cfg.set("device", "default")
            await asyncio.sleep(0.15)          # 首轮：只记录，不做无谓重启
            assert eng.switched_to is None, "首次不应重启采集流"

            cfg.set("device", "USB 麦克风")
            await asyncio.sleep(0.25)
            assert eng.switched_to == "USB 麦克风", "改配置后应热切换采集设备"

            r = await client.get("/api/status")
            j = await r.json()
            assert j["configured_input"] == "USB 麦克风"
            assert j["input_device"]["name"] == "USB 麦克风"
            assert j["version"] == __version__

            # 广播里也要带上，供页面显示"实际在用哪个麦克风"
            ws = await client.ws_connect("/ws")
            d = (await asyncio.wait_for(ws.receive(), timeout=3.0)).json()
            assert d["input_device"]["name"] == "USB 麦克风"
            assert "alarm_output_device" in d
            await ws.close()
        finally:
            loop_task.cancel()
            await client.close()

    asyncio.run(_run())


def _free_port():
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_live_serve_fake_deployment(monkeypatch):
    """部署级测试：真正启动 `main.py serve --fake`，验证 Web+WS+REST 全栈可用。"""
    pytest.importorskip("websockets")
    _tmp(monkeypatch)
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "main.py"), "serve", "--fake",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=os.getcwd(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    ws_url = f"ws://127.0.0.1:{port}/ws"
    try:
        import urllib.request

        ok = False
        for _ in range(50):
            try:
                with urllib.request.urlopen(base + "/api/health", timeout=1) as r:
                    if r.status == 200:
                        ok = True
                        break
            except Exception:
                time.sleep(0.2)
        assert ok, "服务未能启动"

        with urllib.request.urlopen(base + "/api/config", timeout=2) as r:
            cfg = __import__("json").loads(r.read())
        assert cfg["threshold_db"] == 70.0

        with urllib.request.urlopen(base + "/", timeout=2) as r:
            html = r.read().decode("utf-8", "ignore")
        assert "音量" in html

        async def _ws():
            import websockets

            async with websockets.connect(ws_url, open_timeout=3, ping_interval=5) as ws:
                return __import__("json").loads(await asyncio.wait_for(ws.recv(), timeout=5))

        data = asyncio.new_event_loop().run_until_complete(_ws())
        assert "db" in data and "threshold" in data
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def test_module_importable():
    assert server_mod.MonitorServer is not None
