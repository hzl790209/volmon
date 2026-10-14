"""路径处理：区分"程序资源"与"用户数据"，让打包成 .app 后也能正常工作。

打包后（PyInstaller `sys.frozen`）有两个坑：
  1. **程序资源**（web/index.html 等）在 `sys._MEIPASS` 临时目录里，不能用 `__file__` 推导；
  2. **用户数据**（config/db/日志/录音）绝对不能写到 .app 包内部——那地方是只读的，
     且签名后会因内容变化而失效。必须写到 `~/Library/Application Support/volmon-mac`。

未打包（源码运行）时行为完全不变：资源与数据都在项目根目录。
"""
import os
import sys

# 应用名随平台变化：macOS=volmon-mac，Windows=volmon-win
# （决定打包后的数据目录名与可执行文件名）
APP_NAME = "volmon-win" if sys.platform == "win32" else "volmon-mac"


def is_frozen():
    """是否运行在打包后的 .app / 单文件里。"""
    return bool(getattr(sys, "frozen", False))


def project_root():
    """源码根目录（volmon/ 的上一级）。打包时指向包内资源目录。"""
    if is_frozen():
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resource_path(*parts):
    """取**程序内置资源**的绝对路径（web 页面等只读文件）。"""
    return os.path.join(project_root(), *parts)


def app_support_dir():
    """用户数据目录（打包后存放 config/db/日志/录音，与程序解耦、可写）。

    - Windows: %APPDATA%/volmon-win
    - macOS:   ~/Library/Application Support/volmon-mac
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~/AppData/Roaming")
        return os.path.join(base, APP_NAME)
    base = os.path.expanduser("~/Library/Application Support")
    return os.path.join(base, APP_NAME)


def data_root():
    """数据/日志根目录。

    - 打包后：~/Library/Application Support/volmon-mac（可写、与 app 解耦）
    - 源码运行：项目根目录（保持原有行为）
    """
    if is_frozen():
        return app_support_dir()
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resolve_user_path(p):
    """把用户在配置里填的路径解析成**绝对路径**。

    网页上的「存储路径」允许用户填绝对路径（如 ``D:\\volmon\\recordings``）或
    相对路径（如 ``data/recordings``）。这里统一归一化，让下游（日志 handler、
    数据库、录音写盘）拿到的都是可直接使用的绝对路径。

    规则：
      - 空值返回 None（表示"该项未设置，用调用方的默认值"）
      - 先展开环境变量（``%APPDATA%`` / ``$HOME``）与 ``~``
      - 已是绝对路径：原样返回
      - 相对路径：以 **data_root()** 为基准解析，而不是依赖进程当前工作目录；
        这样无论服务从哪个目录被拉起（计划任务、桌面版双击），相对路径都落在同一处。
    """
    if not p:
        return None
    s = os.path.expandvars(os.path.expanduser(str(p).strip()))
    if not os.path.isabs(s):
        s = os.path.join(data_root(), s)
    return os.path.normpath(s)


def setup_runtime():
    """打包后把工作目录切到用户数据目录，让所有相对路径（data/...）自然落对用户目录。

    必须在任何配置/存储模块被导入之前调用。
    """
    if not is_frozen():
        return None
    d = app_support_dir()
    try:
        os.makedirs(d, exist_ok=True)
        # 把常用的 data/ 子目录也一并建好：launcher 锁文件、配置、日志都在这里，
        # 否则冻结态首次运行各模块要各自兜底（真机 MSI 首装实测踩坑）。
        os.makedirs(os.path.join(d, "data"), exist_ok=True)
        os.chdir(d)
    except Exception:
        pass
    return d
