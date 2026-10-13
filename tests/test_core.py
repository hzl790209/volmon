"""volmon 核心逻辑测试（L0/L1/L2 分级，符合《开发规范》§4）。

运行： pytest tests/ -q
- L0 语法：test_py_compile
- L1 核心：config / storage / alerting（仅标准库，任意环境可跑）
- L2 集成：依赖 sounddevice/aiohttp 的用例用 importorskip 保护，缺依赖自动跳过
"""
import os
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _enter_tmp(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.chdir(d)
    return d


def test_py_compile():
    """L0：全量语法校验，必须零错误。"""
    files = ["main.py"] + [
        os.path.join("volmon", f)
        for f in os.listdir(os.path.join(ROOT, "volmon"))
        if f.endswith(".py")
    ]
    r = subprocess.run(
        [sys.executable, "-m", "py_compile"] + files,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr


def test_config_defaults_and_persist(monkeypatch):
    """L1：配置默认值 + set 持久化 + 重载生效。"""
    _enter_tmp(monkeypatch)
    from volmon import config as C

    cfg = C.Config("data/config.json")
    assert cfg.get("threshold_db") == 70.0
    assert cfg.get("calib_offset_db") == 0.0
    cfg.set("threshold_db", 60)
    assert cfg.get("threshold_db") == 60
    cfg2 = C.Config("data/config.json")
    assert cfg2.get("threshold_db") == 60


def test_storage_samples_and_csv(monkeypatch):
    """L1：采样写入 + 查询 + CSV 导出。"""
    _enter_tmp(monkeypatch)
    from volmon import storage as S

    st = S.Storage("data/volmon.db")
    st.add_sample(1_000, -40.2, 65.0, "default")
    rows = st.get_samples(0)
    assert len(rows) == 1 and rows[0]["db"] == 65.0
    n = st.export_csv("data/x.csv", 0)
    assert n == 1
    assert os.path.exists("data/x.csv")


def test_alerting_sustain_and_log(monkeypatch):
    """L1：阈值 + 持续时长判定，超量片段结束后落库。"""
    _enter_tmp(monkeypatch)
    from volmon import storage as S, alerting as A

    st = S.Storage("data/volmon.db")
    alm = A.AlertManager(st, threshold=60, sustain_sec=1.0)

    assert alm.check(50, 1000)["alert"] is False          # 未超阈
    assert alm.check(70, 1200)["alert"] is False          # 超阈但未持续
    r = alm.check(70, 2500)
    assert r["alert"] is True and r["peak"] == 70.0       # 持续超阈 -> 告警
    alm.check(50, 3000)                                  # 回到阈值下 -> 结束并记日志
    alerts = st.get_alerts(0)
    assert len(alerts) == 1
    assert alerts[0]["peak_db"] == 70.0
    assert alerts[0]["duration_s"] is not None


def test_lazy_imports_skipped_without_deps():
    """L2 保护：缺第三方依赖时相关模块用例自动跳过。"""
    pytest.importorskip("sounddevice")
    pytest.importorskip("aiohttp")
    from volmon import audio, server  # noqa: F401
