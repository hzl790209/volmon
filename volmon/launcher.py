"""跨平台一键启动器（被 volmon.bat / 未来 Linux 脚本调用）。

提供与 macOS ~/bin/volmon 等价的体验：
  start / stop / restart / status / web / cli / 默认(=start+cli) / 其余透传 main.py

Windows 上用 taskkill + .volmon.pid 管理后台服务；
macOS/Linux 上用 os.kill + .volmon.pid。
未打包（源码开发）模式下，数据写到项目内 data/（与 macOS 开发行为一致）。
"""
import os
import sys
import json
import time
import subprocess
import urllib.request
import webbrowser

# Windows 下 stdout 常按 gbk 编码，打印「✔/⚠」等字符会抛 UnicodeEncodeError，
# 可能中断启动流程（例如还没拉起看门狗就崩了）。统一改成 utf-8。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # volmon/ 的上一级
IS_WIN = os.name == "nt"

# 打包后（PyInstaller）数据必须写到用户目录，不能写进只读的程序目录。
# 未打包（源码运行）时数据仍落在项目根目录，保持原有行为。
from volmon import paths as _paths  # noqa: E402

IS_FROZEN = _paths.is_frozen()
_DATA_ROOT = _paths.data_root() if IS_FROZEN else PROJ
TASK_NAME = "VolmonMonitor"  # 与 watchdog.py 保持一致的计划任务名（开机自启 + 崩溃自愈）

PID_FILE = os.path.join(_DATA_ROOT, ".volmon.pid")


def _is_serve_cmd(cmd):
    """判断某进程命令行是否为「服务进程」。

    源码模式：``python main.py serve``；冻结模式：``volmon-win.exe serve``。
    看门狗命令行含 ``_watchdog``，必须排除，否则会把看门狗误当成服务。
    """
    c = (cmd or "").lower()
    if "serve" not in c or "_watchdog" in c:
        return False
    return ("main.py" in c) or ("volmon" in c) or (".exe" in c)


def _is_watchdog_cmd(cmd):
    """判断某进程命令行是否为「看门狗进程」。"""
    c = (cmd or "").lower()
    return ("watchdog" in c) and ("loop" in c)


def find_python():
    if IS_WIN:
        cand = os.path.join(PROJ, "venv", "Scripts", "python.exe")
    else:
        cand = os.path.join(PROJ, "venv", "bin", "python")
    if os.path.isfile(cand):
        return cand
    return sys.executable


def port():
    try:
        with open(os.path.join(_DATA_ROOT, "data", "config.json"), encoding="utf-8") as f:
            return int(json.load(f).get("port", 8000))
    except Exception:
        return 8000


def base():
    return "http://127.0.0.1:%d" % port()


def svc_up():
    try:
        return urllib.request.urlopen(base() + "/api/health", timeout=2).status == 200
    except Exception:
        return False


def read_pid():
    try:
        with open(PID_FILE) as f:
            return int(f.read().strip())
    except Exception:
        return None


def proc_alive(pid):
    if pid is None:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    # 优先用 psutil（跨平台、不依赖命令行编码）
    try:
        import psutil
        return psutil.pid_exists(pid)
    except Exception:
        pass
    if IS_WIN:
        # tasklist 在中文系统输出 GBK，必须用 gbk 解码，否则会抛 UnicodeDecodeError
        try:
            out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid],
                                 capture_output=True, encoding="gbk",
                                 errors="ignore").stdout
            return str(pid) in (out or "")
        except Exception:
            return False
    try:
        import signal
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _acquire_lock(lock_file, timeout=30, stale_sec=120):
    """用独占文件锁把「拉起关键区」串行化，避免多次 launcher 并发各拉一份。

    返回 True 表示拿到锁（用完务必 _release_lock）；超时返回 False（此时应复用已有实例）。
    stale_sec 内的旧锁视为上一次崩溃遗留，直接抢断。
    """
    deadline = time.time() + timeout
    # 锁文件可能位于 data/ 子目录；冻结态首次运行时该目录还不存在，
    # 必须先建目录，否则 os.open 直接 FileNotFoundError（真机 MSI 首装实测踩坑）。
    try:
        _d = os.path.dirname(lock_file)
        if _d:
            os.makedirs(_d, exist_ok=True)
    except Exception:
        pass
    while time.time() < deadline:
        try:
            fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock_file) > stale_sec:
                    os.remove(lock_file)
                    continue
            except OSError:
                pass
            time.sleep(0.5)
    return False


def _release_lock(lock_file):
    try:
        os.remove(lock_file)
    except OSError:
        pass


SERVE_LOCK = os.path.join(_DATA_ROOT, "data", ".volmon.serve.lock")
WATCHDOG_LOCK = os.path.join(_DATA_ROOT, "data", ".volmon.watchdog.lock")


def _dbg(msg):
    try:
        with open(os.path.join(_DATA_ROOT, "data", "launcher.debug.log"), "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def _port_owner_pid(target_port=None):
    """返回当前监听指定端口的进程 PID（取不到返回 None）。"""
    try:
        import psutil
    except Exception:
        return None
    if target_port is None:
        target_port = port()   # 注意：这里调用的是模块级 port() 函数，不要与上方形参同名
    try:
        for c in psutil.net_connections(kind="tcp"):
            try:
                if c.laddr.port == target_port and c.status == "LISTEN":
                    return c.pid
            except Exception:
                pass
    except Exception:
        pass
    return None


def _ensure_port_free(target_port=None, tries=12):
    """拉起新 serve 前，确保目标端口没有被任何（含卡死/僵尸）进程占用。

    必要性：若旧 serve 进程卡死但仍占着端口，新 serve 会因「地址已被占用」绑定失败、
    变成监听不了端口的僵尸，进而被看门狗误判为崩溃→重启→再卡死，形成风暴。
    这里在杀掉 stale serve 之后、spawn 之前，轮询端口占用者并强制结束，直到端口空闲。
    """
    if target_port is None:
        target_port = port()
    import time as _t
    for _ in range(tries):
        owner = _port_owner_pid(target_port)
        if owner is None:
            return True
        try:
            import psutil
            psutil.Process(owner).kill()
            _dbg("port %d still held by pid %d, killed" % (target_port, owner))
        except Exception:
            pass
        _t.sleep(0.5)
    return _port_owner_pid(target_port) is None


def _start_background():
    _kill_stale_serve_procs()   # 先清掉任何残留的 serve，避免叠加出多个实例抢占端口
    _ensure_port_free()          # 再确认端口已释放（杀掉仍占端口的卡死/僵尸进程），否则新 serve 绑定会失败
    _dbg("enter _start_background svc_up=%s" % svc_up())
    if svc_up():                 # 已有 serve 在监听，绝不重复拉起
        _dbg("skip _start_background: serve already up")
        return None
    py = find_python()
    log_dir = os.path.join(_DATA_ROOT, "data")
    os.makedirs(log_dir, exist_ok=True)
    log = os.path.join(log_dir, "restart.log")
    logf = open(log, "ab")
    if IS_FROZEN:
        # 冻结模式：直接以 exe 本身启动子进程，无需 main.py
        cmd = [py, "serve"]
        cwd = os.path.dirname(py)
    else:
        cmd = [py, "main.py", "serve"]
        cwd = PROJ
    if IS_WIN:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE：后台服务不弹窗口
        p = subprocess.Popen(cmd, cwd=cwd,
                             stdout=logf, stderr=subprocess.STDOUT,
                             startupinfo=si,
                             creationflags=0x00000008)  # DETACHED_PROCESS
    else:
        p = subprocess.Popen(cmd, cwd=cwd,
                             stdout=logf, stderr=subprocess.STDOUT,
                             start_new_session=True)
    with open(PID_FILE, "w") as f:
        f.write(str(p.pid))
    _dbg("SPAWN serve pid=%s" % p.pid)
    return p.pid


def ensure_service():
    # 任何「启动」意图都撤销「停止保护」，使看门狗恢复自动重启能力
    try:
        from volmon import watchdog as wd
        wd.clear_stop_guard()
    except Exception:
        pass
    _dbg("ensure_service called (svc_up=%s)" % svc_up())
    if svc_up():
        # 服务在跑也要保证看门狗在跑（进程起来但看门狗可能没被拉起过）
        _ensure_watchdog()
        return True
    print("▶ 后端服务未运行，正在后台启动（端口 %d）…" % port())
    if not _acquire_lock(SERVE_LOCK):
        # 另一个 launcher 正在拉起，复用它的实例即可
        for _ in range(40):
            time.sleep(0.5)
            if svc_up():
                _ensure_watchdog()
                return True
        return False
    try:
        _start_background()
        for _ in range(40):
            time.sleep(0.5)
            if svc_up():
                _ensure_watchdog()   # 先确保看门狗拉起，再打印（打印异常不影响看门狗）
                print("✔ 服务已就绪：" + base())
                return True
    finally:
        _release_lock(SERVE_LOCK)
    print("✖ 服务启动超时，data/restart.log 末尾：")
    try:
        with open(os.path.join(PROJ, "data", "restart.log"), "rb") as f:
            lines = f.read().decode("utf-8", "ignore").splitlines()
        for ln in lines[-15:]:
            print("  " + ln)
    except Exception:
        pass
    return False


def _kill_stale_serve_procs():
    """结束所有残留的 `main.py serve` 进程（不依赖 .volmon.pid，直接按命令行匹配）。

    必要性：服务由计划任务以 Highest 权限拉起、且 main.py serve 以 DETACHED_PROCESS
    启动，是「孤儿进程」，普通（非提升）shell 的 taskkill 会因拒绝访问而杀不掉，
    且 .volmon.pid 可能指向的不是真正在服务的那个进程。本函数借助 psutil 按命令行
    匹配并终止，进程本身以提升权限运行时（处于计划任务上下文内）即可成功结束。
    """
    try:
        import psutil
    except Exception:
        return
    me = os.getpid()
    for p in psutil.process_iter(["pid", "cmdline", "name"]):
        try:
            if p.info["pid"] == me:
                continue
            if (p.info["name"] or "").lower() not in ("python.exe", "pythonw.exe"):
                continue
            cmd = " ".join(p.info["cmdline"] or [])
            if _is_serve_cmd(cmd):
                p.kill()
        except Exception:
            pass


def _kill_stale_watchdog_procs():
    """结束所有残留的看门狗进程（``volmon.watchdog loop``）。

    看门狗由本进程（处于计划任务 Highest 上下文）拉起，因此以同样权限即可终止。
    重启服务前先清掉旧看门狗，避免拉起新服务时叠加出多个看门狗。
    """
    try:
        import psutil
    except Exception:
        return
    me = os.getpid()
    for p in psutil.process_iter(["pid", "cmdline", "name"]):
        try:
            if p.info["pid"] == me:
                continue
            if (p.info["name"] or "").lower() not in ("python.exe", "pythonw.exe"):
                continue
            cmd = " ".join(p.info["cmdline"] or [])
            if "volmon.watchdog" in cmd and "loop" in cmd:
                p.kill()
        except Exception:
            pass


def _start_watchdog():
    """后台拉起看门狗长驻循环（与 serve 同上下文、DETACHED，进程退出后继续存活）。"""
    py = find_python()
    log_dir = os.path.join(_DATA_ROOT, "data")
    os.makedirs(log_dir, exist_ok=True)
    log = os.path.join(log_dir, "watchdog.out.log")
    logf = open(log, "ab")
    if IS_FROZEN:
        cmd = [py, "_watchdog", "loop"]
        cwd = os.path.dirname(py)
    else:
        cmd = [py, "-m", "volmon.watchdog", "loop"]
        cwd = PROJ
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    subprocess.Popen(cmd, cwd=cwd,
                     stdout=logf, stderr=subprocess.STDOUT,
                     startupinfo=si, creationflags=0x00000008)  # DETACHED_PROCESS
    _dbg("SPAWN watchdog loop")


def _ensure_watchdog():
    """幂等地确保只有一个看门狗在跑：清掉多余实例（保留最老），没有才拉起。"""
    _dbg("ensure_watchdog called (svc_up=%s)" % svc_up())
    if not _acquire_lock(WATCHDOG_LOCK):
        return
    try:
        try:
            import psutil
            me = os.getpid()
            wds = []
            for p in psutil.process_iter(["pid", "cmdline", "name"]):
                try:
                    if p.info["pid"] == me:
                        continue
                    if (p.info["name"] or "").lower() not in ("python.exe", "pythonw.exe"):
                        continue
                    cmd = " ".join(p.info["cmdline"] or [])
                    if _is_watchdog_cmd(cmd):
                        wds.append(p.info["pid"])
                except Exception:
                    pass
        except Exception:
            wds = []
        if wds:
            wds.sort()
            for pid in wds[1:]:
                try:
                    import psutil
                    psutil.Process(pid).kill()
                    _dbg("kill extra watchdog %d" % pid)
                except Exception:
                    pass
            return  # 已有一个（最老）看门狗在跑，不再拉起
        _start_watchdog()
    finally:
        _release_lock(WATCHDOG_LOCK)


def stop_service():
    # 显式停止：置「停止保护」，看门狗不应在用户刚停下时立刻重新拉起
    try:
        from volmon import watchdog as wd
        wd.set_stop_guard()
    except Exception:
        pass
    pid = read_pid()
    if pid and proc_alive(pid):
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True)
        else:
            import signal
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
        for _ in range(20):
            time.sleep(0.5)
            if not svc_up():
                print("✔ 后端服务已停止")
                return
        print("⚠ 服务未响应，可强制结束进程")
    else:
        print("· 后端服务本来就没在跑")
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
    # 兜底：清理任何残留的 main.py serve 孤儿进程
    _kill_stale_serve_procs()
    # 同时停掉看门狗（volmon stop 应完全停止）
    _kill_stale_watchdog_procs()


def cmd_status():
    if not svc_up():
        print("· 服务未运行（执行 volmon 即可一键启动）")
        return 0
    try:
        j = json.loads(urllib.request.urlopen(base() + "/api/status", timeout=3).read())
    except Exception as e:
        print("  （无法解析状态）" + str(e))
        return 1
    d = j.get("input_device") or {}
    print("✔ 服务运行中（" + base() + "）")
    print("  版本     :", j.get("version_display") or j.get("version"))
    print("  采集设备 :", (d.get("name") or "未知"), "(索引", d.get("index"), ")")
    print("  报警输出 :", j.get("alarm_output_device") or "系统默认输出")
    print("  配置设备 :", j.get("configured_input"))
    return 0


def _run(*sub):
    """以「子命令」方式运行当前 exe / 解释器（冻结模式直接用 exe，源码模式加 main.py）。"""
    py = find_python()
    if IS_FROZEN:
        return subprocess.run([py] + list(sub))
    return subprocess.run([py, "main.py"] + list(sub))


def _cli_inproc(extra_args=None):
    """在当前进程内直接进入终端实时波形界面。

    为什么不能像以前那样 `_run("cli")`：冻结后 ``_run`` 会执行
    ``volmon-win.exe cli``，而 ``cli`` 又落回 launcher 的 cli 分支，
    于是**无限递归 spawn**（用户看到的现象就是"敲了命令没有任何反应"，
    实际在疯狂起进程）。这里改为直接在本进程内调用 main.py 的 cli 子命令。
    """
    from main import main as _main

    old_argv = sys.argv
    try:
        sys.argv = [sys.argv[0], "cli"] + list(extra_args or [])
        _main()
    except KeyboardInterrupt:
        pass
    finally:
        sys.argv = old_argv
    return 0


def install_scheduled_task():
    """注册 Windows 计划任务（登录时启动 + 崩溃自愈）。

    任务动作 = ``volmon-win.exe start``，由 launcher 先清理残留再各拉起 1 个服务 + 1 个看门狗；
    看门狗检测到崩溃后会通过 COM 重新运行本任务实现自愈。无需管理员权限即可创建
    （以交互式登录用户身份运行），但「以最高权限运行」可避免部分系统策略限制。
    """
    try:
        import win32com.client
        exe = sys.executable
        scheduler = win32com.client.Dispatch("Schedule.Service")
        scheduler.Connect()
        root = scheduler.GetFolder("\\")
        try:
            root.DeleteTask(TASK_NAME, 0)
        except Exception:
            pass
        task = scheduler.NewTask(0)
        p = task.Principal
        p.LogonType = 3                      # TASK_LOGON_INTERACTIVE_TOKEN：以登录用户身份运行
        p.RunLevel = 1                       # TASK_RUNLEVEL_HIGHEST：最高权限
        p.DisplayName = "Volmon 音量监控"
        trig = task.Triggers.Create(9)       # TASK_TRIGGER_LOGON：登录时触发
        trig.Enabled = True
        act = task.Actions.Create(0)         # TASK_ACTION_EXEC
        act.Path = exe
        act.Arguments = "start"
        act.WorkingDirectory = os.path.dirname(exe)
        s = task.Settings
        s.Enabled = True
        s.StartWhenAvailable = True
        s.DisallowStartIfOnBatteries = False
        s.ExecutionTimeLimit = ""            # 不限执行时长
        s.MultipleInstances = 0              # IgnoreNew：避免重复拉起
        s.RestartCount = 3
        s.RestartInterval = "PT1M"
        s.Hidden = False
        # 6=TASK_CREATE_OR_UPDATE, 3=TASK_LOGON_INTERACTIVE_TOKEN
        root.RegisterTaskDefinition(TASK_NAME, task, 6, "", "", 3)
        return True, "已创建开机自启任务 %s（登录时自动启动服务）" % TASK_NAME
    except Exception as e:
        return False, "创建计划任务失败：%s" % e


def uninstall_scheduled_task():
    """删除 VolmonMonitor 计划任务（卸载 / 重置时用）。"""
    try:
        import win32com.client
        scheduler = win32com.client.Dispatch("Schedule.Service")
        scheduler.Connect()
        root = scheduler.GetFolder("\\")
        try:
            root.DeleteTask(TASK_NAME, 0)
        except Exception:
            pass
        return True, "已删除计划任务 %s" % TASK_NAME
    except Exception as e:
        return False, "删除计划任务失败：%s" % e


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else ""
    py = find_python()
    if cmd in ("", "up", "start"):
        # 先清理可能残留的旧服务进程（提升权限下可成功终止孤儿进程），
        # 再启动全新进程——保证加载到最新代码、不与原进程争用端口。
        stop_service()
        if not ensure_service():
            return 1
        if cmd == "start":
            print("面板地址：" + base() + "/")
            return 0
        # 默认 / up：拉起后再进入终端实时波形
        return _cli_inproc(args[1:])
    if cmd == "serve":
        # 前台运行（Ctrl-C 停止），便于看日志
        return _run("serve").returncode
    if cmd == "web":
        if not ensure_service():
            return 1
        webbrowser.open(base() + "/")
        return 0
    if cmd == "cli":
        if not ensure_service():
            return 1
        return _cli_inproc(args[1:])
    if cmd == "status":
        return cmd_status()
    if cmd == "stop":
        stop_service()
        return 0
    if cmd == "restart":
        stop_service()
        ensure_service()
        return 0
    if cmd == "install":
        ok, msg = install_scheduled_task()
        print(("✔ " if ok else "✖ ") + msg)
        if ok:
            try:
                import win32com.client
                scheduler = win32com.client.Dispatch("Schedule.Service")
                scheduler.Connect()
                scheduler.GetFolder("\\").GetTask(TASK_NAME).Run("")
                print("✔ 已立即启动服务（稍后浏览器会自动打开面板）")
            except Exception as e:
                print("（任务已创建，但无法立即启动：%s；下次登录会自动启动）" % e)
        return 0 if ok else 1
    if cmd == "uninstall":
        ok, msg = uninstall_scheduled_task()
        print(("✔ " if ok else "✖ ") + msg)
        return 0 if ok else 1
    # 其余原样透传给 main.py（quiet / policy / schedule / clear / logs / mail / config …）
    return _run(*args).returncode


if __name__ == "__main__":
    sys.exit(main())
