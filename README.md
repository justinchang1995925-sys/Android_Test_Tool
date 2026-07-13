# Android 智能设备测试工具

这是一个基于 Python 标准库的本地 Web 测试工具，用于通过 `adb` 管理和测试 Android 智能设备。

## 环境要求

- Windows / macOS / Linux 均可运行，当前项目按 Windows 路径使用也没有额外依赖。
- Python 3.10 或更高版本。
- Android platform-tools 已安装。默认使用系统 `PATH` 中的 `adb`；若电脑存在多个 adb 版本，建议指定固定路径（见下方「指定 ADB 路径」）。
- Android 设备已开启 USB 调试；IP 连接前通常需要设备和电脑在同一网络，并确保设备端已允许调试连接。

## 启动

```powershell
cd d:\Android_tool
python app.py
```

启动后在浏览器打开：

```text
http://127.0.0.1:8000
```

如需修改监听地址或端口：

```powershell
$env:ANDROID_TOOL_HOST="0.0.0.0"
$env:ANDROID_TOOL_PORT="8080"
python app.py
```

### 指定 ADB 路径（避免多版本 adb 冲突）

当系统 `PATH` 中存在多个 adb（例如 `C:\Windows\adb.exe` 与新版 platform-tools 混用）时，批量安装可能出现 `server version doesn't match this client`。

优先级（高 → 低）：

1. 系统 **PATH** 中第一个 `adb`（与 PowerShell 里 `where adb` 第一条一致）
2. 环境变量 `ANDROID_ADB`（仅当 PATH 未找到 adb 时使用）
3. 项目目录（或 exe 同目录）下的 `android_tool.json`

示例（PowerShell，临时生效）：

```powershell
$env:ANDROID_ADB="D:\adb_new_for_android12\adb.exe"
python app.py
```

示例（配置文件 `android_tool.json`，与 `app.py` 或 exe 同目录）：

```json
{
  "adbPath": "D:\\adb_new_for_android12\\adb.exe"
}
```

修改后需重启 `python app.py`；主页 ADB 状态条会显示当前使用的 adb 路径。

## 打包 EXE

在 Windows 上双击或运行：

```powershell
.\build_exe.bat
```

脚本会检查语法、安装 PyInstaller，并生成：

```text
dist\AndroidTestTool.exe
```

把 `AndroidTestTool.exe` 发给其他 Windows 电脑后，对方双击即可启动 Web 工具，程序会自动打开浏览器。对方电脑仍需要安装 Android platform-tools，并确保 `adb` 已加入系统 `PATH`。

## 功能

- 支持 USB 设备列表刷新和多设备选择。
- 支持 IP 连接，IP 输入框会记忆历史输入，并在下次输入前缀时自动补全。
- 支持按系统进程名单次查询内存占用。
- 支持按 APP 包名单次查询内存占用。
- 支持按 APP 包名单次查询 CPU 占用率、设备 CPU 总占用率，以及 APP CPU + CPU 总占用同时查询。
- 支持通过独立“内存 / CPU 曲线”区域进行连续采样，可选择 APP 单进程 CPU、CPU 总占用率或两者同时记录；曲线页只显示已选择的曲线，内存、CPU 各占一行显示。
- 支持将内存和 CPU 采样数据保存为本地 `.xlsx` Excel 文件，可自定义保存目录，保存成功后自动打开目录；曲线页在采样过程中每 5 分钟自动备份 Excel（有新增数据时）。
- 支持将当前内存和 CPU 曲线导出为本地交互式 `.html` 文件，用浏览器打开后鼠标悬停可查看时间与数值；同时导出单图与「内存+CPU 合集」标签页版本。
- 曲线页在采样值超过阈值时顶部告警（同设备同指标 60 秒内不重复提示）；点数超过 2000 时绘制自动降采样，导出仍保留全量数据。
- 主页面顶部显示 ADB 状态（版本与可用设备数），每 30 秒自动刷新。
- 内存填充、批量安装 APK、Monkey 测试支持进行中取消。
- 支持自定义设备路径、文件名和大小进行内存填充，填充时显示进度，完成后弹窗提示。
- 支持指定本地 APK 目录并批量安装目录下所有 `.apk`，安装时显示进度，完成后弹窗提示。
- 支持 Monkey 测试，用户输入一个或多个白名单包名后工具自动生成并推送 `/sdcard/whitelist.txt`；常用忽略参数通过下拉多选配置并显示已选项，支持单击选中、双击取消，并可自定义事件数、随机数种子、事件占比、事件间隔和日志保存路径，自动统计 Crash / ANR 发生时间点和次数。
- 支持查看屏幕分辨率。
- 支持截图和手动开始/停止录屏，可使用默认 `captures` 目录或自定义保存路径，保存完成后自动打开存放目录。
- 支持查看当前前台应用包名。

## 注意事项

- 多设备操作会按选择的设备逐台执行，并在页面结果区显示每台设备的输出。
- 曲线页面会定时轮询采样接口；修改后端代码后需要重启 `python app.py` 并刷新页面。
- Excel 文件默认保存到项目下的 `stats_exports` 目录；自定义路径为空时使用默认目录。
- 曲线图默认保存到项目下的 `chart_exports` 目录；自定义路径为空时使用默认目录。
- Monkey 日志默认保存到项目下的 `monkey_logs` 目录；自定义路径为空时使用默认目录。
- EXE 运行时，`captures`、`stats_exports`、`chart_exports` 和 `monkey_logs` 默认创建在 EXE 所在目录。
- 内存填充会真实占用设备存储空间，请确认路径和大小后再执行。
- 录屏使用 Android 原生 `screenrecord`，开始后点击停止会自动拉取 MP4 并清理设备端临时文件。
