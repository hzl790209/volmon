"""把 `heat.exe` 采集出来的 appfiles.wxs 后处理成可直接参与编译的形式。

用法：
    python packaging/postprocess_appfiles.py dist8

做三件事（都是 WiX 打包必需的）：
  1. 把 `Source="$(DistSrc)\\..."` 换成相对项目根目录的 `Source="dist8\\volmon-win\\..."`。
     —— heat 的 `-var` 变量只有在 light 阶段也传 `-d` 才生效，容易踩坑；写成相对路径最稳。
  2. 给每个 `<Component>` 加 `Win64="yes"`（x64 包要求）。
  3. 把两个入口 exe 的 File Id 改成稳定名 `filVolmonExe` / `filVolmonCli`，
     供 volmon.wxs 里的 Type-34 自定义操作与快捷方式用 `[#filVolmonExe]` 引用。
"""
import os
import re
import sys

EXES = (("volmon-win.exe", "filVolmonExe"), ("volmon-cli.exe", "filVolmonCli"))


def main():
    dist = (sys.argv[1] if len(sys.argv) > 1 else "dist8").strip().rstrip("/\\")
    proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(proj, "appfiles.wxs")
    s = open(path, encoding="utf-8").read()

    # 1) 变量引用 -> 相对路径（注意文件里是单个反斜杠）
    s = s.replace('Source="$(DistSrc)\\', 'Source="%s\\volmon-win\\' % dist)

    # 2) 64 位组件
    s = s.replace("<Component Id=", '<Component Win64="yes" Id=')

    # 3) 入口 exe 的稳定 File Id
    for exe, fid in EXES:
        pat = r'<File Id="([^"]+)" KeyPath="yes" Source="' + re.escape(dist) + r'\\volmon-win\\' + re.escape(exe) + r'"'
        m = re.search(pat, s)
        if not m:
            print("  !! 未找到 %s 的 File 行" % exe)
            continue
        s = s.replace(m.group(0), m.group(0).replace('Id="%s"' % m.group(1), 'Id="%s"' % fid), 1)
        print("  %-16s => %s" % (exe, fid))

    open(path, "w", encoding="utf-8").write(s)
    print("Components: %d | Win64: %d | filVolmonExe: %s | filVolmonCli: %s | 残留变量: %s"
          % (s.count("<Component "), s.count('Win64="yes"'),
             "filVolmonExe" in s, "filVolmonCli" in s, "$(DistSrc)" in s))


if __name__ == "__main__":
    main()
