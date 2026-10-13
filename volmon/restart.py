"""服务自重启：供 Web 页面「重启服务」按钮调用。

思路（避免在端口仍被占用时启动新进程）：
  1. 当前进程派生一个**脱离会话的接替进程**；
  2. 接替进程等待当前进程退出（端口释放）后，再用**相同参数** exec 启动 `main.py serve`；
  3. 当前进程在响应客户端之后自行退出。

这样改代码/换设备后无需手工杀进程，点一下按钮即可生效。
"""
import os
import subprocess
import sys

# 接替进程脚本：argv[1]=父进程PID，argv[2:]=原启动参数
_WAIT_AND_EXEC = r"""
import os, sys, time
parent = int(sys.argv[1])
args = sys.argv[2:]
deadline = time.time() + 30
while time.time() < deadline:
    try:
        os.kill(parent, 0)
    except OSError:
        break
    time.sleep(0.2)
time.sleep(0.5)
os.execv(sys.executable, [sys.executable] + args)
"""


def spawn_restart(root, log_path="data/restart.log", keep_days=None):
    """派生接替进程；返回 (ok, msg)。

    重启日志同样**按日切割**：写入前若发现 log_path 不是今天的，先归档为
    restart_YYYY-MM-DD.log；keep_days>0 时顺带清理过期归档（0 = 只切不删）。
    """
    main_py = os.path.join(root, "main.py")
    # 打包成 .app 后没有源码入口，改为重新 exec 自己
    if not os.path.exists(main_py):
        if not getattr(sys, "frozen", False):
            return False, f"未找到入口文件 {main_py}"
        args = [sys.executable] + [a for a in sys.argv[1:]]
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        try:
            from volmon import logrot

            logrot.rotate_file_if_stale(
                log_path, logrot.DEFAULT_KEEP_DAYS if keep_days is None else keep_days
            )
        except Exception:
            pass
        try:
            logf = open(log_path, "a", encoding="utf-8")
        except Exception:
            logf = subprocess.DEVNULL
        try:
            subprocess.Popen(
                args,
                cwd=os.path.dirname(log_path) or ".",
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=logf,
                stderr=subprocess.STDOUT,
            )
        except Exception as e:
            return False, f"重启失败: {e}"
        return True, "接替进程已派生，原进程即将退出"
    # 沿用当前启动参数（如 serve / --fake / --port ...）
    args = [main_py] + sys.argv[1:]
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    try:
        from volmon import logrot

        logrot.rotate_file_if_stale(
            log_path, logrot.DEFAULT_KEEP_DAYS if keep_days is None else keep_days
        )
    except Exception:
        pass
    try:
        logf = open(log_path, "a", encoding="utf-8")
    except Exception:
        logf = subprocess.DEVNULL
    try:
        subprocess.Popen(
            [sys.executable, "-c", _WAIT_AND_EXEC, str(os.getpid())] + args,
            cwd=root,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=subprocess.STDOUT,
        )
    except Exception as e:
        return False, f"派生接替进程失败: {e}"
    return True, "接替进程已派生，原进程即将退出"
