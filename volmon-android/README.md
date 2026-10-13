# volmon-android

volmon 的**原生安卓端**（Kotlin + Jetpack Compose）。把桌面版 volmon 的核心能力——实时麦克风分贝监测、超阈值报警声、触发录音、校准、后台持续运行——移植到手机上。

> **关于鸿蒙 4.2**：HarmonyOS 4.2 带有 AOSP 兼容层，**可直接安装并运行安卓 APK**，因此本工程编译出的一个安卓包即可同时覆盖安卓与鸿蒙 4.2，无需单独做鸿蒙版。（仅 HarmonyOS NEXT / 5.x 才去掉 AOSP、需要原生 `.hap`，那不在本工程范围内。）

---

## 1. 用 Android Studio 打开

1. 本目录即是 Gradle 工程根（`settings.gradle.kts` 在根）。
2. Android Studio → `Open` → 选择本目录。
3. 首次打开会：
   - 下载 Gradle 8.9（见 `gradle/wrapper/gradle-wrapper.properties`）；
   - 生成 `gradle-wrapper.jar`（仓库里不提交，由 IDE 自动生成）；
   - 同步依赖（需要联网，依赖来自 `google()` + `mavenCentral()`）。
4. 同步完成后即可 `Build → Make Project`。

> 工程未提交 `gradle-wrapper.jar`、`*.iml`、`local.properties`、`build/`、`~/.gradle`，这些是本地生成物，按 `.gitignore` 规则忽略。

## 2. 运行 / 安装

- **模拟器**：新建一个带麦克风的 AVD（Pixel 系列 + API 26+），`Run` 即可。注意模拟器麦克风默认指向宿主机麦克风，需要授权。
- **真机（安卓）**：`Run` 选 USB 设备；或 `Build → Build Bundle(s) / APK(s) → Build APK`，把 `app/build/outputs/apk/debug/app-debug.apk` 拷到手机安装。
- **真机（鸿蒙 4.2）**：把同一个 `app-debug.apk` 拷到鸿蒙设备，用「文件管理」或浏览器打开安装即可（系统会把它当 AOSP 应用跑）。若提示「风险应用」，在设置里允许「外部来源应用安装」后重试。

## 3. 权限与运行期注意

| 权限 | 用途 | 说明 |
|---|---|---|
| `RECORD_AUDIO` | 采集环境音 | 首次点「开始监测」弹窗请求；模拟器/真机都需授权 |
| `FOREGROUND_SERVICE` + `FOREGROUND_SERVICE_MICROPHONE` | 后台持续监测 | Android 14+ 必须，已声明 |
| `POST_NOTIFICATIONS` | 前台服务通知 | Android 13+ 建议在系统设置里允许本应用通知，否则通知栏可能不显示 |
| `WAKE_LOCK` | 息屏后尽量保持采集线程 | 已在 Manifest 声明 |

**后台保活**：本应用用前台服务 + 通知实现持续监测，比普通后台服务持久得多。但系统（尤其国产 ROM / 鸿蒙）仍可能锁屏后节流甚至回收，**如要保证长时间后台监测，请把本应用加入「电池优化白名单 / 不受限制」**。

**校准的前提**：校准页读取的是「当前麦克风实时读数」。只有在「开始监测」服务运行期间，读到的才是真实值；如果没启动监测就去校准，得到的是地板值（-100 dBFS），校准会失真。请先开始监测，再进校准页输入专业声级计读数。

## 4. 与桌面版功能对照

| 桌面版（Python） | 安卓端实现 |
|---|---|
| `audio.py` RMS→dBFS | `audio/AudioCapture.kt`（`AudioRecord` 16-bit PCM） |
| `config.py` 阈值/持续/报警/录音参数 | `data/Preferences.kt`（DataStore 持久化） |
| `alarm.py` 蜂鸣 | `audio/AlarmSound.kt`（AudioTrack 合成正弦） |
| `recorder.py` 触发录音 + 预录 | `service/MonitoringService.kt` + `audio/RingPcm.kt`（环形预录缓冲） |
| `calibrate` 校准 | `data/AudioLevel.kt` `computeCalibration` + 校准页 |
| Web 实时曲线 | `ui/components/LevelChart.kt`（纯 Canvas 绘制） |
| 前台常驻服务 | `service/MonitoringService.kt`（foreground service, microphone 类型） |
| 录音回放 | `ui/screens/RecordingsScreen.kt`（MediaPlayer 播放 / 删除 WAV） |
| 录音数量 / 磁盘限额 | `data/RecordingStore.kt`（`makeRoom` 落盘前腾空间 + `prune` 落盘后裁剪）；超限策略可选「覆盖最旧」或「停止存储」 |
| 录音一键清空 / 多选转发 | `ui/screens/RecordingsScreen.kt`（长按或「选择」进多选 → 全选 → `ACTION_SEND_MULTIPLE` 转发；`FileProvider` 见 `res/xml/file_paths.xml`） |
| 报警记录+趋势 | `data/AlarmStore.kt`（JSONL 持久化）+ `ui/screens/AlarmLogScreen.kt`（今日/累计/峰值/均值统计 + 近 7 天趋势图 + 明细） |

**运行时权限**：首次点「开始监测」会在界面内请求 `RECORD_AUDIO`（Android 14 的麦克风前台服务必须先拿到该权限，否则直接崩溃）；Android 13+ 还会顺带请求 `POST_NOTIFICATIONS`。

算法与桌面版完全一致：`dBFS = 20·log10(RMS)`（静音钳到 -100），`dB SPL ≈ dBFS + 90 + calibOffset`，校准偏移 `= 实测 − (dBFS + 90)`。关键常量见 `data/AudioLevel.kt`。

## 5. 已知限制 / 待办

- **已在本机命令行编译通过**：工具链为 `JDK 17 (Corretto) + Gradle 8.9 + AGP 8.5.2 + Android SDK 34`，产物 `app/build/outputs/apk/debug/app-debug.apk`。依赖走 `maven.aliyun.com` + `dl.google.com` 镜像（见 `settings.gradle.kts`），因为 `mavenCentral()` / `github.com` 在部分网络下不可达。
- **签名**：`release` 构建暂用 `debug` 签名（`app/build.gradle.kts` 里有注释）。要上架应用市场，请自备 `release` 签名并在 `signingConfigs` 配置。
- **后台节流**：见第 3 节，长时后台监测依赖系统白名单。
- **录音文件位置**：`Android/data/com.volmon.app/files/recordings/`（App 私有目录）。「录音回放」页可直接试听、长按多选、全选后转发（走 `FileProvider` 生成 `content://` URI），或一键清空。
- **录音限额**：条数与占用空间可分别设上限（0 = 不限制），超限策略二选一 —— 「覆盖最旧」自动删最早一条再存新的，「停止存储」则不再保存新录音。
- **未做**：安静时段（桌面 `quiet`）、邮件日报、Web 远程查看——这些桌面版特性暂未移植，可按需在 `data/Settings` 扩展。

## 6. 工程结构

```
volmon-android/
├── settings.gradle.kts
├── build.gradle.kts                 # 根：AGP 8.5.2 + Kotlin 2.0.21
├── gradle/wrapper/gradle-wrapper.properties
└── app/
    ├── build.gradle.kts             # 依赖 / Compose / minSdk26 targetSdk34
    └── src/main/
        ├── AndroidManifest.xml
        ├── res/values/{strings,themes}.xml
        ├── res/xml/file_paths.xml     # FileProvider 白名单（录音转发用）
        ├── res/mipmap-*/ic_launcher.png   # 辐射4 Pip-Boy 风格图标
        └── java/com/volmon/app/
            ├── VolmonApplication.kt
            ├── data/        AudioLevel / MonitorState / Preferences
            │                AlarmStore / SampleStore / RecordingStore
            ├── audio/       AudioCapture / RingPcm / AlarmSound
            ├── service/     MonitoringService / WavWriter
            └── ui/          MainActivity / MonitorViewModel
                ├── theme/Theme.kt               # 黑客绿 / PIP-Boy 双主题
                ├── components/LevelChart / HistoryTrendChart / ScanlineOverlay
                └── screens/ HomeScreen / SettingsScreen / CalibrationScreen
                             RecordingsScreen / AlarmLogScreen
```

## 7. 最低/目标版本

- `minSdk = 26`（Android 8.0）
- `compileSdk = 34`，`targetSdk = 34`
- Kotlin 2.0.21，Compose Compiler 随 Kotlin 2.0 自带，`compose-bom 2024.09.00`
