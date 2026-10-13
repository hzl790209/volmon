"""volmon 音量监控软件包。

版本说明：
  - `__product__` + `__version__` 组成对外的**产品版本**（macOS=volmon-mac、Windows=volmon-win）。
  - `__version__` 仍是唯一版本源（页面徽章 / CLI 状态条 / 接口都取它）。
  - 定版后如还需改动，按 1.0.x 递增（1.0.1、1.0.2 …）。
"""
import sys

# 产品名随平台变化：macOS=volmon-mac，Windows=volmon-win（页面/接口展示用）
__product__ = "volmon-win" if sys.platform == "win32" else "volmon-mac"
__version__ = "1.0.3"

# 对外展示用的完整版本串（如页面徽章 "volmon-mac 1.0"）
__display__ = f"{__product__} {__version__}"
