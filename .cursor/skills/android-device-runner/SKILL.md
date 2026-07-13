---
name: android-device-runner
description: Execute Android smart-device test tasks through ADB from an Agent conversation. Use when the user asks to batch install APKs, connect USB/IP Android devices, collect app or process memory, collect app CPU, export sampled data to Excel, capture screenshots, record screens, fill storage, query screen size, or get the foreground package without opening the web UI.
---

# Android Device Runner

## What This Skill Does

Runs Android device test tasks directly from an Agent conversation using `adb` and the bundled Python script `scripts/android_device_runner.py`.

Use this skill when the user says things like:

- "帮我批量安装 D:/apk 目录下的 APK"
- "每 2 秒抓取一次 com.xxx 的内存，抓取 3 小时"
- "先安装 APK，再采集 com.xxx 的内存和 CPU 并导出 Excel"
- "连接 192.168.1.10，然后截图"

## Requirements

- Python 3.10+
- Android platform-tools installed
- `adb` available in `PATH`
- Android device USB debugging enabled
- For IP devices, `adb connect <ip>:5555` must be possible

The script uses only Python standard library modules.

## Execution Rules

1. Parse the user's request into concrete parameters:
   - Device selection: all connected devices, explicit serials, or IP targets.
   - APK directory, if installation is requested.
   - Memory target: app package or system process name.
   - CPU target: app package name.
   - Sampling interval and total duration.
   - Output directory.
2. Use the bundled script instead of writing new ADB command loops.
3. Prefer `--all-devices` when the user does not name a specific device.
4. For long sampling jobs, run the command with enough timeout and report the output file path.
5. Summarize results with:
   - Devices used
   - APK install success/failure counts
   - Sampling interval and duration
   - Excel file path
   - Any ADB errors

## Common Commands

List connected devices:

```powershell
python .cursor/skills/android-device-runner/scripts/android_device_runner.py devices
```

Connect an IP device:

```powershell
python .cursor/skills/android-device-runner/scripts/android_device_runner.py connect --ip 192.168.1.10
```

Batch install all APKs in a directory to all connected devices:

```powershell
python .cursor/skills/android-device-runner/scripts/android_device_runner.py install-apks --apk-dir D:/apk --all-devices
```

Sample APP memory every 2 seconds for 1 hour and export Excel:

```powershell
python .cursor/skills/android-device-runner/scripts/android_device_runner.py sample --all-devices --memory-app com.xxx.xxx --interval 2 --duration 3600 --output-dir reports
```

Install APKs, then sample memory and CPU:

```powershell
python .cursor/skills/android-device-runner/scripts/android_device_runner.py run-plan --all-devices --apk-dir D:/apk --memory-app com.xxx.xxx --cpu-app com.xxx.xxx --interval 2 --duration 3600 --output-dir reports
```

Screenshot all devices:

```powershell
python .cursor/skills/android-device-runner/scripts/android_device_runner.py screenshot --all-devices --output-dir captures
```

## Duration Parsing

Convert natural language before invoking the script:

- `2S`, `2秒`, `2 seconds` -> `--interval 2`
- `1小时`, `1h` -> `--duration 3600`
- `30分钟`, `30m` -> `--duration 1800`

If the user omits duration for sampling, ask for it before running long collection.

## Supported Operations

The script supports:

- `devices`: list connected devices
- `connect`: connect by IP
- `install-apks`: batch install `.apk` files
- `sample`: sample memory and/or CPU and export `.xlsx`
- `run-plan`: install APKs first, then sample memory and/or CPU
- `screen-size`: query screen resolution
- `screenshot`: save screenshots
- `current-package`: get foreground package
- `fill-storage`: create a zero-filled file on device storage

## Output

Default output directory is `android_runner_output` under the current working directory.

Sampling output is an `.xlsx` file with columns:

- Time
- Device
- Memory Target Type
- Memory Target
- Memory MB
- CPU Package
- CPU Percent
- Status
- Error

CPU sampling prefers `pidof <package>` plus `top` by PID. If `top` cannot be parsed on the target Android version, the script falls back to `dumpsys cpuinfo`.

## Safety Notes

- `fill-storage` consumes real device storage. Confirm path and size before running.
- `install-apks` installs all `.apk` files in the chosen directory.
- Sampling for hours is long-running; use a foreground timeout long enough for the requested duration.
