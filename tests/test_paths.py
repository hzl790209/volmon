"""路径模块测试：保证"打包后写用户目录、源码运行行为不变"这两条底线。

这是 DMG 打包正确性的关键——`.app` 内部只读，一旦数据路径指错，
打包后的程序要么写不进去、要么破坏签名。
"""
import os
import shutil
import sys
import tempfile

from volmon import paths


def _tmpdir():
    """自建临时目录。

    不用 pytest 的 `tmp_path` 夹具：某些环境下 pytest 的 basetemp 目录已存在且属主不同，
    `mkdir` 会抛 EEXIST（被代理层转成 PermissionError），导致用例在夹具阶段就报错。
    """
    return tempfile.mkdtemp(prefix="volmon-paths-")


def test_source_mode_is_not_frozen():
    """测试进程本身是源码运行。"""
    assert paths.is_frozen() is False


def test_source_mode_data_root_is_project_root():
    """源码运行时数据根目录 = 项目根目录（与打包前行为完全一致）。"""
    root = paths.data_root()
    assert os.path.isdir(os.path.join(root, "volmon"))
    assert os.path.isfile(os.path.join(root, "main.py"))


def test_resource_path_points_to_bundled_web_page():
    """包内资源（web/index.html）必须能取到。"""
    p = paths.resource_path("web", "index.html")
    assert os.path.isfile(p), p


def test_app_support_dir_name():
    assert paths.app_support_dir().endswith("volmon-mac")
    assert paths.app_support_dir().startswith(os.path.expanduser("~"))


def test_setup_runtime_is_noop_when_not_frozen(monkeypatch):
    """源码运行时 setup_runtime 不能改动工作目录，否则会破坏既有相对路径约定。"""
    cwd = os.getcwd()
    monkeypatch.setattr(paths, "is_frozen", lambda: False)
    assert paths.setup_runtime() is None
    assert os.getcwd() == cwd


def test_frozen_mode_data_root_is_user_dir(monkeypatch):
    """打包后（sys.frozen=True）数据根目录必须落在用户目录，绝不能是 .app 内部。"""
    d = _tmpdir()
    try:
        monkeypatch.setattr(paths, "is_frozen", lambda: True)
        monkeypatch.setattr(paths, "app_support_dir", lambda: os.path.join(d, "asdir"))
        assert paths.data_root() == os.path.join(d, "asdir")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_frozen_mode_resource_path_uses_meipass(monkeypatch):
    """打包后资源从 sys._MEIPASS 取，而不是 __file__ 推导。"""
    d = _tmpdir()
    try:
        os.makedirs(os.path.join(d, "web"))
        with open(os.path.join(d, "web", "index.html"), "w", encoding="utf-8") as f:
            f.write("x")
        monkeypatch.setattr(paths, "is_frozen", lambda: True)
        monkeypatch.setattr(sys, "_MEIPASS", d, raising=False)
        assert paths.resource_path("web", "index.html") == os.path.join(d, "web", "index.html")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_setup_runtime_creates_and_chdirs_when_frozen(monkeypatch):
    """打包后 setup_runtime 要建目录并切过去，让 data/... 这类相对路径自然落对用户目录。"""
    d = _tmpdir()
    target = os.path.join(d, "asdir")
    cwd = os.getcwd()
    monkeypatch.setattr(paths, "is_frozen", lambda: True)
    monkeypatch.setattr(paths, "app_support_dir", lambda: target)
    try:
        got = paths.setup_runtime()
        assert got == target
        assert os.path.isdir(target)
        assert os.path.realpath(os.getcwd()) == os.path.realpath(target)
    finally:
        os.chdir(cwd)  # 必须还原，避免影响同批次其它用例
        shutil.rmtree(d, ignore_errors=True)
