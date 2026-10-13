# L3 真机 E2E 检查清单（发布前必跑）

> 对应《开发规范》§4.4。每次发布前在 **Windows + 麦克风** 环境逐项走查，并在下方「执行记录」登记结果。
> 任一项失败即**不得发布**，回到对应模块修复后重测。

## 环境准备
- [ ] `python -m venv venv && venv\Scripts\activate`
- [ ] `pip install -r requirements.txt`
- [ ] 系统/浏览器已授权麦克风权限

## 功能走查
- [ ] `python main.py serve` 启动无报错，监听 `http://0.0.0.0:8000`
- [ ] 浏览器打开 `http://127.0.0.1:8000`，显示实时波形与当前 dB
- [ ] 制造噪声（拍手/说话），波形与「当前 dB」实时变化
- [ ] 红色虚线（阈值）显示在波形上
- [ ] 另开终端 `python main.py cli`，终端出现滚动波形与当前 dB
- [ ] `python main.py calibrate 65.0`（先用专业噪音仪测得真实值）后，软件读数与该真实值接近
- [ ] Web 校准面板输入实测值并「一键校准」，偏移生效
- [ ] 在设置面板把阈值临时调低使其超标：Web 出现黄色「超阈值」、持续后变红色「超量告警」横幅
- [ ] CLI 端同步出现超量横幅
- [ ] 超量结束后，「告警日志」列表新增一条（峰值/阈值/持续时长）
- [ ] `data/volmon.log` 出现 `超量片段结束` 记录
- [ ] `python main.py logs --hours 24` 列出超量事件
- [ ] `python main.py logs --hours 24 --export data/export.csv` 产出 CSV，Excel 可打开
- [ ] 关闭再启动 `serve`，历史趋势图能显示此前采样（SQLite 持久化）

## 回归
- [ ] `python -m py_compile main.py volmon/*.py` 通过
- [ ] `pytest tests/ -q` 通过（L1）

## 执行记录
| 日期 | 版本 | 结果 | 执行人 | 备注 |
|------|------|------|--------|------|
|      |      |      |        |      |
