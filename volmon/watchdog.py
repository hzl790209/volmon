"""volmon 看门狗：监控 Web 服务健康，崩溃 / 卡死时自动重启。

设计要点
--------
- 看门狗以**长驻进程**方式运行（``loop`` 子命令），由 ``launcher.start`` 在拉起 serve
  的同时一并拉起（DETACHED_PROCESS，脱离任务计划进程树独立存活）。因此「谁来监控监控器」
  由 launcher 在每次启动时兜底，无需第二个任务计划。
- 单次检查逻辑（``check`` / ``run_once``）：
  1. 请求 `/api/health`；成功即视为健康。
  2. 失败时再确认一遍 ``main.py serve`` 进程是否还活着：
     - 进程已死      -> 硬崩溃，立即重启；
     - 进程活着但无响应 -> 卡死（死锁 / 端口占用），也重启；
     - 连续失败达到阈值（默认 2 次）也重启，避免瞬时抖动误重启。
  3. 重启通过 Task Scheduler COM 重新运行「VolmonMonitor」任务实现（与手动启动完全一致，
     launcher.start 会先清残留孤儿进程再各拉起一个 serve / watchdog），并带**冷却窗口**
     （PROC_SETTLE_SEC）防止服务刚重启还在预热就被误判再次重启。
- 唯一重启权威是 Windows 任务计划程序「VolmonMonitor」任务；服务内重启按钮、CLI 重启、
  以及看门狗自动重启全部走同一条 COM 路径，因此绝不会产生「进程内 fork」或「嵌套 launcher」
  这类会叠加出 2+2 重复实例的退路。
- 子命令：
  loop     长驻循环（**部署模式**，由 launcher 拉起）
  check    单次检查（可选：也可由任务计划每分钟调用一次，异常则自动重启）
  restart  强制重启一次
  status   输出当前健康 / 状态
"""
import argparse
import json
import os
import sys
import time

# Windows 下 stdout 可能被重定向到文件/管道，且默认按 gbk 编码；一旦 log() 打印
# 「⚠」等字符就会抛 UnicodeEncodeError，进而让看门狗主循环在第一次 log() 就崩溃，
# 彻底丧失「检测 + 重启」能力。统一改成 utf-8（失败则忽略，不影响文件日志）。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # volmon/ 的上一级

# 数据目录：打包后必须落用户目录（%APPDATA%/volmon-win/data），不能写进只读程序目录。
from volmon import paths as _paths  # noqa: E402

if _paths.is_frozen():
    DATA = os.path.join(_paths.data_root(), "data")
else:
    DATA = os.path.join(PROJ, "data")
STATE_FILE = os.path.join(DATA, "watchdog.json")
TASK_NAME = "VolmonMonitor"          # 被监控 / 被重启的任务
PORT_DEFAULT = 8000
HEALTH_TIMEOUT = 3.0
FAIL_THRESHOLD = 2                   # 连续失败几次才重启（避免瞬时抖动误重启）
COOLDOWN_SEC = 120                   # 两次重启之间最小间隔，防风暴
PROC_SETTLE_SEC = 90                 # 重启后给服务多少秒预热（期间不判失败，防止刚重启就被误重启）
STOP_GUARD = os.path.join(DATA, ".volmon.stopped")  # 用户显式停止后，看门狗不再自动拉起


def set_stop_guard():
    """写「停止保护」标记：用户显式停止服务后，看门狗不应把它重新拉起。"""
    try:
        with open(STOP_GUARD, "w", encoding="utf-8") as f:
            f.write("stopped at %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
    except Exception:
        pass


def clear_stop_guard():
    try:
        os.remove(STOP_GUARD)
    except OSError:
        pass


def stop_guarded():
    return os.path.exists(STOP_GUARD)


def load_config_port():
    try:
        with open(os.path.join(DATA, "config.json"), encoding="utf-8") as f:
            return int(json.load(f).get("port", PORT_DEFAULT))
    except Exception:
        return PORT_DEFAULT


def base():
    return "http://127.0.0.1:%d" % load_config_port()


def ping_health():
    """返回 (ok, version_display)。"""
    try:
        import urllib.request
        with urllib.request.urlopen(base() + "/api/health", timeout=HEALTH_TIMEOUT) as r:
            if r.status == 200:
                try:
                    d = json.loads(r.read())
                    return True, d.get("version_display") or d.get("version")
                except Exception:
                    return True, None
    except Exception:
        pass
    return False, None


def serve_process_alive():
    """serve 进程是否还活着：True=存活 / False=未运行 / None=无法判断（缺 psutil 等）。"""
    try:
        import psutil
    except Exception:
        return None
    for p in psutil.process_iter(["pid", "cmdline", "name"]):
        try:
            if p.info["name"] and p.info["name"].lower() in ("python.exe", "pythonw.exe"):
                cmd = " ".join(p.info["cmdline"] or [])
                c = cmd.lower()
                # 源码：python main.py serve；冻结：volmon-win.exe serve
                if ("serve" in c) and ("_watchdog" not in c):
                    return True
        except Exception:
            pass
    return False


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(s):
    try:
        os.makedirs(DATA, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = "[%s] %s" % (ts, msg)
    try:
        print(line, flush=True)
    except Exception:
        # 兜底：直接写 buffer，避免 stdout 编码问题再次抛出导致主循环崩溃
        try:
            sys.stdout.buffer.write((line + "\n").encode("utf-8", "replace"))
        except Exception:
            pass
    try:
        os.makedirs(DATA, exist_ok=True)
        with open(os.path.join(DATA, "watchdog.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def restart_service():
    """通过 Task Scheduler COM 重新运行 VolmonMonitor 任务（与手动启动完全一致）。

    这是**唯一的**重启权威：launcher.start 会先清掉所有残留的 serve / watchdog 孤儿
    进程，再各拉起一个，保证最终只有 1 个服务 + 1 个看门狗。不再使用「进程内 fork 自重启」
    或「嵌套 launcher」等会产生重复进程的退路。COM 不可用时直接返回失败，由看门狗下一轮
    重试，而不是自己再拉一份（那样会叠加成 2+2）。

    非 Windows（macOS / Linux）没有任务计划，回退到 volmon.restart 的进程内接替进程方案。
    """
    clear_stop_guard()  # 重启即代表希望服务运行，撤销「停止保护」
    if os.name != "nt":
        try:
            from volmon import restart as restart_mod
            keep = 0
            try:
                from volmon import config as _cfg
                keep = int(_cfg.DEFAULTS.get("log_keep_days", 0))
            except Exception:
                pass
            return restart_mod.spawn_restart(
                PROJ, os.path.join(PROJ, "data", "restart.log"), keep_days=keep)
        except Exception as e:
            return False, "spawn_restart failed: %s" % e
    try:
        import win32com.client
        scheduler = win32com.client.Dispatch("Schedule.Service")
        scheduler.Connect()
        folder = scheduler.GetFolder("\\")
        task = folder.GetTask(TASK_NAME)
        try:
            task.Stop()  # 先停掉可能残留的任务实例，确保接下来的 Run 真正执行 launcher
        except Exception:
            pass
        time.sleep(1)    # 等任务实例完全退出，避免 Run 与上一实例冲突
        task.Run("")
        return True, "triggered task %s" % TASK_NAME
    except Exception as e:
        return False, "com failed: %s" % e


def port_owner_pid():
    """返回当前监听配置端口的进程 PID（取不到返回 None）。"""
    try:
        import psutil
        port = load_config_port()
        for c in psutil.net_connections(kind="tcp"):
            try:
                if c.laddr.port == port and c.status == "LISTEN":
                    return c.pid
            except Exception:
                pass
    except Exception:
        pass
    return None


def prune_duplicates():
    """兜底清理：保证最多只有 1 个看门狗长驻进程。

    **注意：看门狗不再插手 serve 进程的去重。** serve 的「唯一性」完全由 launcher 在每次
    启动时负责（先杀掉所有旧 serve 再拉起一个），看门狗若在这里按命令行/端口去杀 serve，
    极容易在「旧 serve 还占着端口、新 serve 刚拉起但没绑上」的竞态里**误杀健康实例**，
    进而误判「进程已死」又触发重启，形成重启风暴。因此这里只清理看门狗自身的极端重复情况。
    """
    try:
        import psutil
    except Exception:
        return
    me = os.getpid()
    wds = []
    for p in psutil.process_iter(["pid", "cmdline", "name"]):
        try:
            info = p.info
            pid = info["pid"]
            if pid == me:
                continue
            if (info["name"] or "").lower() not in ("python.exe", "pythonw.exe"):
                continue
            cmd = " ".join(info["cmdline"] or [])
            c = cmd.lower()
            # 源码：python -m volmon.watchdog loop；冻结：volmon-win.exe _watchdog loop
            if ("watchdog" in c) and ("loop" in c):
                wds.append(pid)
        except Exception:
            pass
    if len(wds) > 1:
        wds.sort()
        for pid in wds[1:]:
            try:
                psutil.Process(pid).kill()
                log("清理重复 watchdog 进程 %d（保留 %d）" % (pid, wds[0]))
            except Exception:
                pass


def run_once(force=False):
    if stop_guarded():
        # 用户显式停止（网页/CLI `volmon stop`）：看门狗不应把它重新拉起来
        st = load_state()
        if not st.get("guard_noted"):
            log("检测到「停止保护」标记，暂停自动重启（执行启动/重启后可恢复）")
            st["guard_noted"] = True
            save_state(st)
        return 0
    # 兜底：先清理任何重复进程（防止历史 fork / 级联重启残留叠加成 2+2）
    prune_duplicates()
    ok, ver = ping_health()
    st = load_state()
    fail_streak = int(st.get("fail_streak", 0))
    last_restart = float(st.get("last_restart", 0) or 0)
    now = time.time()

    if ok:
        if fail_streak != 0:
            log("健康恢复（版本 %s），清空失败计数" % (ver or "?"))
        st["fail_streak"] = 0
        st["last_ok"] = now
        save_state(st)
        return 0

    # 不健康
    fail_streak += 1
    since_restart = now - last_restart

    # 重启后冷却期（服务正在起来），不判失败、不重启
    if since_restart < PROC_SETTLE_SEC:
        log("健康检查失败（连续 %d 次），但距上次重启仅 %.0f 秒，等待服务启动中…"
            % (fail_streak, since_restart))
        st["fail_streak"] = fail_streak
        save_state(st)
        return 0

    alive = serve_process_alive()
    hard_crash = (alive is False)

    if not force:
        # 硬崩溃（进程已死）：1 次即重启
        if hard_crash and fail_streak >= 1:
            pass
        elif fail_streak < FAIL_THRESHOLD:
            log("健康检查失败（连续 %d/%d 次），暂不重启（进程%s）"
                % (fail_streak, FAIL_THRESHOLD,
                   {True: "存活(可能卡死)", False: "已死", None: "状态未知"}[alive]))
            st["fail_streak"] = fail_streak
            save_state(st)
            return 1
        elif since_restart < COOLDOWN_SEC:
            log("健康检查失败，但距上次重启仅 %.0f 秒（冷却中），跳过" % since_restart)
            st["fail_streak"] = fail_streak
            save_state(st)
            return 1

    # 触发重启
    reason = ("force" if force else
              ("进程已死(硬崩溃)" if hard_crash else
               ("进程存活但无响应(卡死)" if alive is True else "health 连续失败")))
    log("⚠ 需要重启：%s（连续失败 %d 次）。正在重启任务 %s…" % (reason, fail_streak, TASK_NAME))
    ok2, detail = restart_service()
    if ok2:
        log("✔ 已触发重启：%s" % detail)
        st["fail_streak"] = 0
        st["last_restart"] = now
        st["restart_count"] = int(st.get("restart_count", 0)) + 1
        st["last_restart_reason"] = reason
    else:
        log("✖ 重启失败：%s" % detail)
        st["fail_streak"] = fail_streak
    save_state(st)
    return 2 if ok2 else 3


def run_loop(interval=15):
    log("看门狗长驻循环启动（间隔 %ds）" % interval)
    while True:
        try:
            run_once()
        except Exception as e:
            log("检查异常：%s" % e)
        time.sleep(interval)


def cmd_status():
    ok, ver = ping_health()
    alive = serve_process_alive()
    st = load_state()
    alive_txt = {True: "存活", False: "未运行", None: "未知"}.get(alive, "未知")
    print("健康   :", "✔ 正常" if ok else "✖ 异常")
    print("版本   :", ver or "-")
    print("serve  :", alive_txt)
    print("失败计数:", st.get("fail_streak", 0))
    print("累计重启:", st.get("restart_count", 0))
    lr = st.get("last_restart")
    print("上次重启:", (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(lr)) if lr else "-"))
    print("重启原因:", st.get("last_restart_reason", "-"))


def main():
    ap = argparse.ArgumentParser(description="volmon 看门狗")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("check", help="单次健康检查，异常则自动重启")
    p_loop = sub.add_parser("loop", help="长驻循环（不推荐，优先用任务计划每分钟 check）")
    p_loop.add_argument("--interval", type=int, default=30)
    sub.add_parser("restart", help="强制重启一次")
    sub.add_parser("status", help="输出当前健康 / 状态")
    args = ap.parse_args()
    cmd = args.cmd or "check"
    if cmd == "check":
        sys.exit(run_once())
    if cmd == "loop":
        run_loop(args.interval)
    if cmd == "restart":
        ok, d = restart_service()
        print(("✔ " if ok else "✖ ") + d)
        sys.exit(0 if ok else 1)
    if cmd == "status":
        cmd_status()
        sys.exit(0)


if __name__ == "__main__":
    main()
