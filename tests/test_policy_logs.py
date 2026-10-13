"""生效策略明细 / 日志清空 / 告警记录策略快照。

对应需求：
  1. 有地方显示当前生效策略且要有策略明细     -> GET /api/policy
  2. 一键清空 + 按时间段清空日志              -> POST /api/logs/clear + Storage.clear()
  3. 日志里要包含当前策略、是否启用声音报警及持续时间 -> alerts 表与 CSV 增加策略列
"""
import asyncio
import csv
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, ".")

import pytest  # noqa: E402

from volmon import alerting as alerting_mod  # noqa: E402
from volmon import config as config_mod  # noqa: E402
from volmon import storage as storage_mod  # noqa: E402


def _mk_store(tmp=None, csv_name="alerts.csv"):
    tmp = tmp or tempfile.mkdtemp()
    return storage_mod.Storage(os.path.join(tmp, "v.db"),
                               alerts_csv=os.path.join(tmp, csv_name))


# ---------------- 需求 3：告警记录里带上策略快照 ----------------
def test_alert_stores_policy_context_and_csv_columns():
    tmp = tempfile.mkdtemp()
    store = _mk_store(tmp)
    ctx = {
        "period": "晚间",
        "schedule_applied": True,
        "alarm_enabled": True,
        "alarm_duration_sec": 10.0,
        "alarm_cooldown_sec": 3.0,
    }
    now = int(time.time() * 1000)
    store.add_alert(now, now + 4000, 62.5, 55.0, context=ctx)

    rows = store.get_alerts(0)
    assert len(rows) == 1
    a = rows[0]
    assert a["period"] == "晚间"
    assert a["schedule_applied"] is True
    assert a["alarm_enabled"] is True
    assert a["alarm_duration_sec"] == 10.0
    assert a["alarm_cooldown_sec"] == 3.0

    # 本地 CSV 也应包含策略列，便于用 Excel 直接查看
    with open(os.path.join(tmp, "alerts.csv"), encoding="utf-8-sig") as f:
        rd = list(csv.reader(f))
    header = rd[0]
    for col in ("period", "alarm_enabled", "alarm_duration_s", "alarm_cooldown_s"):
        assert col in header, f"CSV 缺少策略列 {col}：{header}"
    row = dict(zip(header, rd[1]))
    assert row["period"] == "晚间"
    assert row["alarm_enabled"] == "是"
    assert row["alarm_duration_s"] == "10"


def test_alert_without_context_still_works():
    """老调用方式（不带 context）不能报错，策略列为空。"""
    tmp = tempfile.mkdtemp()
    store = _mk_store(tmp)
    now = int(time.time() * 1000)
    store.add_alert(now, now + 1000, 71.0, 70.0)
    a = store.get_alerts(0)[0]
    assert a["period"] == ""
    assert a["alarm_enabled"] is False


def test_alerting_passes_context_to_storage():
    """告警器必须把"当前生效策略"传给存储层。"""
    captured = {}

    class SpyStore:
        def add_alert(self, *a, **k):
            captured["args"] = a
            captured["kwargs"] = k

    alm = alerting_mod.AlertManager(SpyStore(), threshold=50.0, sustain_sec=0.0)
    alm.set_context({"period": "白天", "schedule_applied": False,
                     "alarm_enabled": False, "alarm_duration_sec": 5, "alarm_cooldown_sec": 1})
    alm.check(80.0, time.time() * 1000)      # 进入超标
    alm.check(10.0, time.time() * 1000 + 500)  # 结束 -> 落库
    assert captured["kwargs"].get("context", {}).get("period") == "白天"


def test_migration_adds_policy_columns_to_old_db():
    """老库升级不能丢数据：应自动补上策略列。"""
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "old.db")
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE alerts (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " start_ts INTEGER, end_ts INTEGER, peak_db REAL, threshold_db REAL)"
    )
    con.execute("INSERT INTO alerts (start_ts, end_ts, peak_db, threshold_db) VALUES (1,2,70,55)")
    con.commit()
    con.close()

    store = storage_mod.Storage(path)
    cols = {r[1] for r in store.conn.execute("PRAGMA table_info(alerts)").fetchall()}
    for c in ("period", "schedule_applied", "alarm_enabled",
              "alarm_duration_sec", "alarm_cooldown_sec"):
        assert c in cols, f"迁移后缺少列 {c}"
    assert store.get_alerts(0)[0]["peak_db"] == 70, "历史数据应保留"


# ---------------- 需求 2：清空日志 ----------------
def test_clear_all_and_by_scope():
    tmp = tempfile.mkdtemp()
    store = _mk_store(tmp)
    t0 = int(time.time() * 1000)
    for i in range(10):
        store.add_sample(t0 + i * 1000, -40.0, 50.0, "dev")
    for i in range(4):
        store.add_alert(t0 + i * 1000, t0 + i * 1000 + 500, 60.0, 55.0)

    assert store.counts() == {"samples": 10, "alerts": 4}

    # 只清采样
    d = store.clear(scope="samples")
    assert d["samples"] == 10 and d["alerts"] == 0
    assert store.counts() == {"samples": 0, "alerts": 4}

    # 只清告警
    d = store.clear(scope="alerts")
    assert d["alerts"] == 4
    assert store.counts()["alerts"] == 0

    # 全清
    for i in range(3):
        store.add_sample(t0 + i * 1000, -40.0, 50.0, "dev")
    d = store.clear(scope="all")
    assert store.counts() == {"samples": 0, "alerts": 0}


def test_clear_by_time_window():
    """按时间段清空：只删时间窗内的，窗外的必须保留。"""
    tmp = tempfile.mkdtemp()
    store = _mk_store(tmp)
    base = int(time.time() * 1000) - 10 * 3600 * 1000  # 10 小时前
    for h in range(10):                       # 10 个小时各一条
        ts = base + h * 3600 * 1000
        store.add_sample(ts, -40.0, 50.0, "dev")
        store.add_alert(ts, ts + 500, 60.0, 55.0)

    since = base + 7 * 3600 * 1000            # 从 7 小时前起
    d = store.clear(scope="all", since_ts=since)
    assert d["samples"] == 3, f"应只删最近 3 条采样，实际 {d['samples']}"
    assert store.counts()["samples"] == 7

    # 只删最早的 2 小时
    d2 = store.clear(scope="all", until_ts=base + 1 * 3600 * 1000)
    assert d2["samples"] == 2
    assert store.counts()["samples"] == 5


def test_clear_rebuilds_local_csv():
    """清空告警后本地 CSV 必须同步重写，不能"库里没了、CSV 里还在"。"""
    tmp = tempfile.mkdtemp()
    store = _mk_store(tmp)
    t0 = int(time.time() * 1000)
    for i in range(5):
        store.add_alert(t0 + i * 1000, t0 + i * 1000 + 500, 60.0, 55.0,
                        context={"period": "白天", "alarm_enabled": False})
    csv_path = os.path.join(tmp, "alerts.csv")
    with open(csv_path, encoding="utf-8-sig") as f:
        assert len([l for l in f.read().splitlines() if l.strip()]) == 6  # 表头 + 5

    store.clear(scope="alerts")
    with open(csv_path, encoding="utf-8-sig") as f:
        lines = [l for l in f.read().splitlines() if l.strip()]
    assert len(lines) == 1, f"清空后 CSV 应只剩表头，实际 {len(lines)} 行"


def test_clear_rejects_unknown_scope():
    tmp = tempfile.mkdtemp()
    store = _mk_store(tmp)
    with pytest.raises(ValueError):
        store.clear(scope="whatever")


# ---------------- 需求 1 + 2：服务端接口 ----------------
def test_policy_and_clear_endpoints(monkeypatch):
    pytest.importorskip("aiohttp")
    from volmon import alerting as A
    from volmon import server as server_mod
    from volmon import sim as sim_mod

    tmp = tempfile.mkdtemp()
    monkeypatch.chdir(tmp)
    cfg = config_mod.Config(path=os.path.join(tmp, "config.json"))
    cfg.set("sample_interval_ms", 50)
    cfg.set("alarm_output_device", "USB 耳机")
    store = _mk_store(tmp)
    alm = A.AlertManager(store, threshold=cfg.get("threshold_db"), sustain_sec=3.0)
    srv = server_mod.MonitorServer(sim_mod.SimEngine(), store, cfg, alm)

    t0 = int(time.time() * 1000)
    # 注意用**过去**的时间戳：若写成 t0 + i*1000 会落到"未来"，按小时清空时会筛不掉
    for i in range(3):
        store.add_alert(t0 - i * 1000, t0 - i * 1000 + 500, 60.0, 55.0,
                        context={"period": "白天", "alarm_enabled": False})

    async def _run():
        from aiohttp.test_utils import TestClient, TestServer

        client = TestClient(TestServer(srv.app))
        await client.start_server()
        task = asyncio.create_task(srv.loop())
        try:
            r = await client.get("/api/policy")
            j = await r.json()
            assert j["ok"] is True
            for k in ("source", "period_label", "threshold_db", "sustain_sec",
                      "alarm_enabled", "alarm_duration_sec", "alarm_cooldown_sec",
                      "alarm_output_device", "windows", "counts", "input_device"):
                assert k in j, f"策略明细缺少 {k}"
            assert j["alarm_output_device"] == "USB 耳机"
            assert j["counts"]["alerts"] == 3

            # 按小时清空
            r = await client.post("/api/logs/clear", json={"scope": "alerts", "hours": 1})
            j = await r.json()
            assert j["ok"] is True
            assert j["deleted"]["alerts"] == 3
            assert j["remaining"]["alerts"] == 0

            # 一键全清
            store.add_sample(t0, -40.0, 50.0, "dev")
            r = await client.post("/api/logs/clear", json={"all": True})
            j = await r.json()
            assert j["ok"] is True
            assert j["remaining"]["samples"] == 0

            # 非法参数
            assert (await client.post("/api/logs/clear", json={"hours": -1})).status == 400
            assert (await client.post("/api/logs/clear", json={"scope": "x"})).status == 400
            assert (await client.post(
                "/api/logs/clear", json={"start_ts": 200, "end_ts": 100})).status == 400
        finally:
            task.cancel()
            await client.close()

    asyncio.run(_run())
