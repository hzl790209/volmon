"""把 MSI + 文档 + WiX 源码打包到桌面（交付用 zip）。

用法：
    python packaging/make_zip.py

为什么单独写脚本：直接在命令行里写含 Windows 路径的字符串，反斜杠会被
shell / heredoc 的转义链吃掉（例如 "\\volmon-win" 变成垂直制表符），
所以 README 正文一律放在 packaging/README-install.txt 里，脚本只做打包。
"""
import os
import zipfile

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DST = os.path.join(os.path.expanduser("~"), "Desktop", "volmon-win-x64.zip")

# (仓库内相对路径, zip 内路径)
ITEMS = [
    ("volmon-win-x64.msi", "volmon-win-x64.msi"),
    ("WINDOWS.md", "WINDOWS.md"),
    ("CHANGELOG.md", "CHANGELOG.md"),
    ("volmon.wxs", "volmon.wxs"),
    ("appfiles.wxs", "appfiles.wxs"),
    ("volmon.zh-CN.wxl", "volmon.zh-CN.wxl"),
    ("packaging/README-install.txt", "README.txt"),
    ("packaging/volmon-cli.cmd", "volmon-cli.cmd"),
    ("packaging/postprocess_appfiles.py", "postprocess_appfiles.py"),
]


def main():
    missing = [a for a, _ in ITEMS if not os.path.exists(os.path.join(PROJ, a))]
    if missing:
        print("缺少文件:", missing)
        return 1
    with zipfile.ZipFile(DST, "w", zipfile.ZIP_DEFLATED) as z:
        for a, b in ITEMS:
            z.write(os.path.join(PROJ, a), b)
    print("OK:", DST, os.path.getsize(DST), "bytes")
    print("entries:", zipfile.ZipFile(DST).namelist())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
