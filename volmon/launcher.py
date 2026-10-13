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

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # volmon/ 的上一级
PID_FILE = os.path.join(PROJ, ".volmon.pid")
IS_WIN = os.name == "nt"


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
        with open(os.path.join(PROJ, "data", "config.json"), encoding="utf-8") as f:
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
    if IS_WIN:
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid],
                             capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        import signal
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _start_background():
    py = find_python()
    log_dir = os.path.join(PROJ, "data")
    os.makedirs(log_dir, exist_ok=True)
    log = os.path.join(log_dir, "restart.log")
    logf = open(log, "ab")
    if IS_WIN:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE：后台服务不弹窗口
        p = subprocess.Popen([py, "main.py", "serve"], cwd=PROJ,
                             stdout=logf, stderr=subprocess.STDOUT,
                             startupinfo=si,
                             creationflags=0x00000008)  # DETACHED_PROCESS
    else:
        p = subprocess.Popen([py, "main.py", "serve"], cwd=PROJ,
                             stdout=logf, stderr=subprocess.STDOUT,
                             start_new_session=True)
    with open(PID_FILE, "w") as f:
        f.write(str(p.pid))
    return p.pid


def ensure_service():
    if svc_up():
        return True
    print("▶ 后端服务未运行，正在后台启动（端口 %d）…" % port())
    _start_background()
    for _ in range(40):
        time.sleep(0.5)
        if svc_up():
            print("✔ 服务已就绪：" + base())
            return True
    print("✖ 服务启动超时，data/restart.log 末尾：")
    try:
        with open(os.path.join(PROJ, "data", "restart.log"), "rb") as f:
            lines = f.read().decode("utf-8", "ignore").splitlines()
        for ln in lines[-15:]:
            print("  " + ln)
    except Exception:
        pass
    return False


def stop_service():
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


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else ""
    py = find_python()
    if cmd in ("", "up", "start"):
        if not ensure_service():
            return 1
        if cmd == "start":
            print("面板地址：" + base() + "/")
            return 0
        # 默认 / up：拉起后再进入终端实时波形
        return subprocess.run([py, "main.py", "cli"]).returncode
    if cmd == "serve":
        # 前台运行（Ctrl-C 停止），便于看日志
        return subprocess.run([py, "main.py", "serve"]).returncode
    if cmd == "web":
        if not ensure_service():
            return 1
        webbrowser.open(base() + "/")
        return 0
    if cmd == "cli":
        if not ensure_service():
            return 1
        return subprocess.run([py, "main.py", "cli"]).returncode
    if cmd == "status":
        return cmd_status()
    if cmd == "stop":
        stop_service()
        return 0
    if cmd == "restart":
        stop_service()
        ensure_service()
        return 0
    # 其余原样透传给 main.py（quiet / policy / schedule / clear / logs / mail / config …）
    return subprocess.run([py, "main.py"] + args).returncode


if __name__ == "__main__":
    sys.exit(main())
