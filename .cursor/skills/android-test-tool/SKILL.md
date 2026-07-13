---
name: android-test-tool
description: Build, maintain, or extend the Python web-based Android smart device test tool in this repository. Use when the user asks about app.py, Android device testing, ADB web tools, memory/CPU charts, APK install automation, screenshots, screen recording, Excel export, or packaging the tool as a Windows executable.
---

# Android Test Tool

## Purpose

Maintain a Python local Web test tool for Android smart devices. The tool uses a browser UI and shells out to `adb` for all device operations.

Use the existing architecture unless the user explicitly asks for a redesign:

- Backend: `app.py`, Python standard library HTTP server.
- Frontend: `index.html` for the main tool page.
- Chart page: `stats.html` for memory and CPU curves.
- Shared theme: `theme.css` and `theme.js` for dark/light mode across pages.
- Packaging: `build_exe.bat` + PyInstaller one-file exe.

## Core Requirements

The tool must support:

- Web UI as the primary carrier.
- USB device discovery through `adb devices -l`.
- IP device connection through `adb connect`, with browser-side IP history/autocomplete.
- Multiple selected devices for supported actions.
- One-shot system process memory lookup by process name.
- One-shot APP memory lookup by package name.
- One-shot APP CPU lookup by package name, one-shot total CPU lookup, and combined APP CPU plus total CPU lookup.
- Device storage filling through `dd if=/dev/zero` with progress display and completion notice.
- Batch APK install from a local directory, installing all `.apk` files for each selected device with progress display and completion notice.
- Monkey testing where users enter whitelist package names and the tool generates/pushes `/sdcard/whitelist.txt`; common ignore options are selected from a multi-select list with visible selected-option feedback, with customizable event count, random seed, event percentages, throttle, log directory, and Crash/ANR summary.
- Screen resolution lookup through `adb shell wm size`.
- Screenshot capture through `adb exec-out screencap -p`, then open the local save directory.
- Manual screen recording start/stop, saving MP4 locally, then open the local save directory.
- Current foreground app package lookup.
- Memory and CPU curve display on a separate Web page through one combined chart entry; CPU charts can show APP process CPU, total CPU, or both, and the chart page should only show curves selected for capture.
- Custom threshold lines for memory and CPU curves.
- Custom sampling interval, total sampling duration, and max retained sample points (default 7200 for continuous capture, up to 50000).
- Saving sampled memory/CPU data as local `.xlsx` files, then opening the save directory.
- Exporting current memory/CPU curves as local interactive `.html` files (hover tooltips for time and values), including a combined memory+CPU tabbed HTML when both charts are active, then opening the save directory.
- ADB status banner on the main page (`GET /api/adb-status`).
- Cancel in-flight fill-memory, batch APK install, and Monkey sessions via cancel APIs.
- Per-device ADB command locks to avoid concurrent `adb` races.
- Chart page: threshold exceed alerts, display downsampling above ~2000 points (export keeps full data), auto Excel backup every 5 minutes during sampling.
- Windows executable packaging so users can double-click to start.

## Implementation Rules

- Prefer Python standard library only. Avoid adding runtime dependencies unless the user approves.
- Keep `app.py`, `index.html`, `stats.html`, `theme.css`, `theme.js`, and `chart-export.js` compatible with PyInstaller resource loading.
- When running from source, static files are loaded from the project directory.
- When running as an exe, static files are loaded from PyInstaller `_MEIPASS`; output files are written next to the exe.
- Default output directories:
  - Screenshots and recordings: `captures`
  - Excel exports: `stats_exports`
  - Chart image exports: `chart_exports`
  - Monkey logs: `monkey_logs`
- Preserve multi-device behavior: API handlers should return `{"results": [...]}` with one result per device.
- Surface `adb` failures clearly in `stderr` or JSON `error`.
- Resolve adb once at startup via `ADB_EXECUTABLE`: priority **PATH** (first `adb` on PATH, same order as `where adb`) > `ANDROID_ADB` env > `android_tool.json` (`adbPath`) next to app/exe. Expose path in `/api/adb-status` (`adbPath`, `adbSource`).
- After backend route changes, remind the user to restart `python app.py` or rebuild/restart the exe.

## ADB Command Reference

Use these command patterns:

- List devices: `adb devices -l`
- Connect IP: `adb connect <ip-or-ip:port>`
- System process memory: `adb shell dumpsys meminfo`, then filter by process name.
- APP memory: `adb shell dumpsys meminfo <package>`
- APP CPU: prefer `pidof <package>` plus `top` by PID; fall back to `adb shell dumpsys cpuinfo` filtered by package name. Total CPU is parsed from `top`.
- Fill storage: `adb shell dd if=/dev/zero of=<remote-file> bs=1048576 count=<size-mb>`
- Install APK: `adb -s <serial> install -r <apk-path>`
- Monkey test: `adb shell monkey --pkg-whitelist-file /sdcard/whitelist.txt --ignore-crashes --ignore-native-crashes --ignore-timeouts --pct-syskeys 0 --ignore-security-exceptions --pct-touch 90 --throttle 300 10000`
- Screen size: `adb shell wm size`
- Screenshot: `adb exec-out screencap -p`
- Screen record: `adb shell screenrecord <remote-mp4>`
- Current app: parse `dumpsys window windows` or `dumpsys activity top`.

## Memory and CPU Charts

The chart workflow is:

1. Main page collects selected devices, target package/process, thresholds, sampling interval, total duration, and Excel output path.
2. Main page opens `stats.html` with query parameters.
3. `stats.html` polls `/api/stats-sample`.
4. `stats.html` draws memory and CPU curves with Canvas.
5. `stats.html` stores sampled rows in memory.
6. User clicks "保存 Excel" or "导出曲线 HTML"; auto-save also posts to `/api/stats-save-excel` every 5 minutes when new rows exist.
7. `stats.html` posts rows to `/api/stats-save-excel` or interactive chart HTML to `/api/stats-save-charts` (uses `chart-export.js`, supports `CHART_CONFIGS` for combined export).
8. Backend writes local `.xlsx` or `.html` files and opens the save directory.

Long-running main-page tasks use session APIs with cancel endpoints:

- `/api/fill-memory-start` + `/api/fill-memory-status` + `/api/fill-memory-cancel`
- `/api/install-apks-start` + `/api/install-apks-status` + `/api/install-apks-cancel`
- `/api/monkey-start` + `/api/monkey-status` + `/api/monkey-cancel`

Memory values should be normalized to MB. CPU values should be percentages. Excel export should remain dependency-free by writing a minimal `.xlsx` ZIP/XML workbook.

## Packaging

Use `build_exe.bat` or this PyInstaller command:

```powershell
python -m PyInstaller --clean --onefile --name AndroidTestTool --add-data "index.html;." --add-data "stats.html;." --add-data "theme.css;." --add-data "theme.js;." --add-data "chart-export.js;." app.py
```

The expected output is:

```text
dist/AndroidTestTool.exe
```

The exe should:

- Start the local HTTP server.
- Open the default browser automatically.
- Require only that the target machine has `adb` available in `PATH`.
- Create `captures` and `stats_exports` next to the exe as needed.

## Validation

After code changes:

```powershell
python -m py_compile app.py
```

Also check linter diagnostics for changed files. For packaging-related changes, rebuild the exe and confirm `dist/AndroidTestTool.exe` is generated.
