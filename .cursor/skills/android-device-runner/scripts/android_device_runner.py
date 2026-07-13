#!/usr/bin/env python3
"""Direct ADB runner for Android device testing.

This script is intentionally dependency-free so it can be bundled inside a
Cursor Skill and copied between machines.
"""

from __future__ import annotations

import argparse
import os
import posixpath
import re
import shlex
import subprocess
import sys
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from xml.sax.saxutils import escape as xml_escape


DEFAULT_OUTPUT_DIR = Path.cwd() / "android_runner_output"


def decode_output(data: bytes) -> str:
    for encoding in ("utf-8", "gbk", sys.getdefaultencoding()):
        try:
            return data.decode(encoding, errors="replace")
        except LookupError:
            continue
    return data.decode(errors="replace")


def run_command(args: list[str], timeout: int | None = None) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return {"ok": False, "returncode": 127, "stdout": "", "stderr": "adb not found in PATH."}
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "returncode": -1,
            "stdout": decode_output(exc.stdout or b""),
            "stderr": f"Command timed out after {timeout}s.",
        }
    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": decode_output(completed.stdout),
        "stderr": decode_output(completed.stderr),
    }


def adb(serial: str | None, *args: str, timeout: int | None = None) -> dict[str, Any]:
    cmd = ["adb"]
    if serial:
        cmd.extend(["-s", serial])
    cmd.extend(args)
    return run_command(cmd, timeout=timeout)


def list_devices() -> list[str]:
    result = adb(None, "devices", "-l", timeout=20)
    if not result["ok"]:
        raise RuntimeError(result["stderr"] or "Failed to list devices.")
    devices = []
    for line in result["stdout"].splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
    return devices


def resolve_devices(args: argparse.Namespace) -> list[str]:
    if getattr(args, "devices", None):
        return args.devices
    if getattr(args, "all_devices", False):
        devices = list_devices()
        if not devices:
            raise RuntimeError("No connected Android devices found.")
        return devices
    devices = list_devices()
    if len(devices) == 1:
        return devices
    if not devices:
        raise RuntimeError("No connected Android devices found.")
    raise RuntimeError("Multiple devices found. Use --all-devices or --devices SERIAL [SERIAL...]")


def ensure_dir(path_text: str | None) -> Path:
    path = Path(path_text).expanduser() if path_text else DEFAULT_OUTPUT_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def sanitize_filename(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_") or "device"


def timestamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def parse_memory_mb(text: str, keyword: str | None = None) -> float | None:
    if keyword:
        for line in text.splitlines():
            if keyword.lower() not in line.lower():
                continue
            match = re.search(r"([\d,]+)\s*K\b", line, flags=re.IGNORECASE)
            if match:
                return int(match.group(1).replace(",", "")) / 1024
    for pattern in (r"^\s*TOTAL\s+([\d,]+)\b", r"^\s*TOTAL\s+PSS:\s*([\d,]+)\b"):
        match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
        if match:
            return int(match.group(1).replace(",", "")) / 1024
    return None


def parse_cpu_percent(text: str, keyword: str) -> float | None:
    values = []
    for line in text.splitlines():
        if keyword.lower() not in line.lower():
            continue
        match = re.match(r"\s*[+-]?([\d.]+)%", line)
        if match:
            values.append(float(match.group(1)))
    return max(values) if values else None


def parse_number_token(token: str) -> float | None:
    match = re.match(r"^[+-]?([\d.]+)%?$", token.strip())
    if not match:
        return None
    return float(match.group(1))


def parse_top_cpu_percent(text: str, package_name: str, pids: list[str]) -> float | None:
    pid_set = set(pids)
    cpu_index: int | None = None
    values = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        tokens = stripped.split()
        upper_tokens = [token.upper() for token in tokens]
        if "PID" in upper_tokens and any("CPU" in token for token in upper_tokens):
            for index, token in enumerate(upper_tokens):
                if token in {"%CPU", "CPU%", "CPU"}:
                    cpu_index = index
                    break
                if "CPU" in token:
                    cpu_index = index + 1 if "[" in token and index + 1 < len(tokens) else index
                    break
            continue
        if not tokens:
            continue
        if tokens[0] not in pid_set and package_name.lower() not in stripped.lower():
            continue
        if cpu_index is not None and cpu_index < len(tokens):
            value = parse_number_token(tokens[cpu_index])
            if value is None and cpu_index + 1 < len(tokens):
                value = parse_number_token(tokens[cpu_index + 1])
            if value is not None:
                values.append(value)
                continue
        for token in tokens[1:]:
            value = parse_number_token(token)
            if value is not None:
                values.append(value)
                break
    return sum(values) if values else None


def get_app_cpu_percent(serial: str, package_name: str) -> tuple[float | None, str]:
    messages = []
    pid_result = adb(serial, "shell", "pidof", package_name, timeout=20)
    pids = re.findall(r"\b\d+\b", pid_result["stdout"]) if pid_result["ok"] else []
    if pids:
        pid_arg = ",".join(pids)
        top_commands = [
            ("top", "-b", "-n", "1", "-p", pid_arg),
            ("top", "-n", "1", "-p", pid_arg),
            ("top", "-b", "-n", "1"),
            ("top", "-n", "1"),
        ]
        for command in top_commands:
            result = adb(serial, "shell", *command, timeout=30)
            if not result["ok"]:
                continue
            value = parse_top_cpu_percent(result["stdout"], package_name, pids)
            if value is not None:
                return value, "top"
        messages.append("top parse failed; fell back to dumpsys cpuinfo. ")
    cpu_result = adb(serial, "shell", "dumpsys", "cpuinfo", timeout=60)
    if cpu_result["ok"]:
        value = parse_cpu_percent(cpu_result["stdout"], package_name)
        if value is not None:
            return value, "dumpsys cpuinfo"
    else:
        messages.append(cpu_result["stderr"])
    if pids:
        messages.append("process exists but CPU was not reported; recorded 0.0%. ")
        return 0.0, "".join(messages)
    messages.append(f"app is not running or not reported: {package_name}. ")
    return None, "".join(messages)


def column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def xlsx_cell(row_index: int, column_index: int, value: Any) -> str:
    reference = f"{column_name(column_index)}{row_index}"
    if value is None:
        return f'<c r="{reference}"/>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{reference}"><v>{value}</v></c>'
    return f'<c r="{reference}" t="inlineStr"><is><t>{xml_escape(str(value))}</t></is></c>'


def write_xlsx(path: Path, rows: list[list[Any]]) -> None:
    sheet_rows = []
    for row_index, row in enumerate(rows, start=1):
        cells = "".join(xlsx_cell(row_index, col, value) for col, value in enumerate(row, start=1))
        sheet_rows.append(f'<row r="{row_index}">{cells}</row>')
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="AndroidStats" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '</Types>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/></Relationships>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/></Relationships>'
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as workbook:
        workbook.writestr("[Content_Types].xml", content_types)
        workbook.writestr("_rels/.rels", root_rels)
        workbook.writestr("xl/workbook.xml", workbook_xml)
        workbook.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        workbook.writestr("xl/worksheets/sheet1.xml", sheet_xml)


def command_devices(_: argparse.Namespace) -> int:
    devices = list_devices()
    print("Connected devices:")
    for serial in devices:
        print(f"- {serial}")
    if not devices:
        print("(none)")
    return 0


def command_connect(args: argparse.Namespace) -> int:
    target = args.ip if ":" in args.ip else f"{args.ip}:5555"
    result = adb(None, "connect", target, timeout=30)
    print(result["stdout"] or result["stderr"])
    return 0 if result["ok"] else 1


def install_apks(devices: list[str], apk_dir: Path) -> bool:
    apks = sorted(apk_dir.glob("*.apk"))
    if not apks:
        raise RuntimeError(f"No APK files found in {apk_dir}")
    ok = True
    total = len(devices) * len(apks)
    done = 0
    for serial in devices:
        for apk_path in apks:
            done += 1
            print(f"[{done}/{total}] Installing {apk_path.name} on {serial}...")
            result = adb(serial, "install", "-r", str(apk_path), timeout=300)
            print((result["stdout"] + result["stderr"]).strip() or "(no output)")
            ok = ok and result["ok"]
    print("APK install complete." if ok else "APK install complete with failures.")
    return ok


def command_install_apks(args: argparse.Namespace) -> int:
    devices = resolve_devices(args)
    apk_dir = Path(args.apk_dir).expanduser().resolve()
    ok = install_apks(devices, apk_dir)
    return 0 if ok else 1


def collect_sample(serial: str, args: argparse.Namespace) -> dict[str, Any]:
    row: dict[str, Any] = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "device": serial,
        "memory_type": "",
        "memory_target": "",
        "memory_mb": None,
        "cpu_package": args.cpu_app or "",
        "cpu_percent": None,
        "ok": True,
        "error": "",
    }
    if args.memory_app:
        row["memory_type"] = "app"
        row["memory_target"] = args.memory_app
        result = adb(serial, "shell", "dumpsys", "meminfo", args.memory_app, timeout=60)
        if result["ok"]:
            row["memory_mb"] = parse_memory_mb(result["stdout"])
        else:
            row["ok"] = False
            row["error"] += result["stderr"]
    if args.memory_process:
        row["memory_type"] = "process"
        row["memory_target"] = args.memory_process
        result = adb(serial, "shell", "dumpsys", "meminfo", timeout=60)
        if result["ok"]:
            row["memory_mb"] = parse_memory_mb(result["stdout"], args.memory_process)
        else:
            row["ok"] = False
            row["error"] += result["stderr"]
    if args.cpu_app:
        row["cpu_percent"], cpu_message = get_app_cpu_percent(serial, args.cpu_app)
        if cpu_message and cpu_message not in {"top", "dumpsys cpuinfo"}:
            row["error"] += f"CPU source/status: {cpu_message}. "
    if row["memory_target"] and row["memory_mb"] is None:
        row["ok"] = False
        row["error"] += f"Memory data not found for {row['memory_target']}. "
    if args.cpu_app and row["cpu_percent"] is None:
        row["ok"] = False
        row["error"] += f"CPU data not found for {args.cpu_app}. "
    return row


def sample_stats(devices: list[str], args: argparse.Namespace) -> Path:
    if not args.memory_app and not args.memory_process and not args.cpu_app:
        raise RuntimeError("Specify --memory-app, --memory-process, or --cpu-app.")
    if args.memory_app and args.memory_process:
        raise RuntimeError("Use only one of --memory-app or --memory-process.")
    output_dir = ensure_dir(args.output_dir)
    output_file = output_dir / f"android_stats_{timestamp()}.xlsx"
    rows = [[
        "Time",
        "Device",
        "Memory Target Type",
        "Memory Target",
        "Memory MB",
        "CPU Package",
        "CPU Percent",
        "Status",
        "Error",
    ]]
    start = time.monotonic()
    sample_index = 0
    while True:
        sample_index += 1
        for serial in devices:
            row = collect_sample(serial, args)
            rows.append([
                row["time"],
                row["device"],
                row["memory_type"],
                row["memory_target"],
                row["memory_mb"],
                row["cpu_package"],
                row["cpu_percent"],
                "OK" if row["ok"] else "FAILED",
                row["error"],
            ])
            memory_text = "-" if row["memory_mb"] is None else f"{row['memory_mb']:.2f} MB"
            cpu_text = "-" if row["cpu_percent"] is None else f"{row['cpu_percent']:.2f}%"
            print(f"[sample {sample_index}] {serial}: memory={memory_text}, cpu={cpu_text}")
        if time.monotonic() - start >= args.duration:
            break
        time.sleep(args.interval)
    write_xlsx(output_file, rows)
    print(f"Saved Excel: {output_file}")
    return output_file


def command_sample(args: argparse.Namespace) -> int:
    devices = resolve_devices(args)
    sample_stats(devices, args)
    return 0


def command_run_plan(args: argparse.Namespace) -> int:
    devices = resolve_devices(args)
    ok = True
    if args.apk_dir:
        ok = install_apks(devices, Path(args.apk_dir).expanduser().resolve())
    sample_stats(devices, args)
    return 0 if ok else 1


def command_screen_size(args: argparse.Namespace) -> int:
    devices = resolve_devices(args)
    ok = True
    for serial in devices:
        result = adb(serial, "shell", "wm", "size", timeout=20)
        ok = ok and result["ok"]
        print(f"[{serial}] {(result['stdout'] + result['stderr']).strip()}")
    return 0 if ok else 1


def command_screenshot(args: argparse.Namespace) -> int:
    devices = resolve_devices(args)
    output_dir = ensure_dir(args.output_dir)
    ok = True
    for serial in devices:
        output_file = output_dir / f"screenshot_{sanitize_filename(serial)}_{timestamp()}.png"
        try:
            completed = subprocess.run(
                ["adb", "-s", serial, "exec-out", "screencap", "-p"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
        except FileNotFoundError:
            print("adb not found in PATH.")
            return 1
        if completed.returncode == 0:
            output_file.write_bytes(completed.stdout)
            print(f"[{serial}] saved {output_file}")
        else:
            ok = False
            print(f"[{serial}] {decode_output(completed.stderr)}")
    return 0 if ok else 1


def command_current_package(args: argparse.Namespace) -> int:
    devices = resolve_devices(args)
    pattern = re.compile(r"([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)")
    ok = True
    for serial in devices:
        result = adb(serial, "shell", "dumpsys", "window", "windows", timeout=30)
        found = ""
        if result["ok"]:
            for line in result["stdout"].splitlines():
                if "mCurrentFocus" in line or "mFocusedApp" in line:
                    match = pattern.search(line)
                    if match:
                        found = match.group(1)
                        break
        if found:
            print(f"[{serial}] {found}")
        else:
            ok = False
            print(f"[{serial}] package not found")
    return 0 if ok else 1


def safe_remote_file(path: str, filename: str) -> str:
    remote_dir = path.strip().replace("\\", "/") or "/sdcard"
    if not remote_dir.startswith("/"):
        remote_dir = "/" + remote_dir
    return posixpath.join(remote_dir.rstrip("/"), PurePosixPath(filename).name)


def command_fill_storage(args: argparse.Namespace) -> int:
    devices = resolve_devices(args)
    remote_file = safe_remote_file(args.remote_path, args.filename)
    command = f"dd if=/dev/zero of={shlex.quote(remote_file)} bs=1048576 count={args.size_mb}"
    ok = True
    for index, serial in enumerate(devices, start=1):
        print(f"[{index}/{len(devices)}] Filling {serial}: {remote_file} ({args.size_mb} MB)")
        result = adb(serial, "shell", command, timeout=max(120, args.size_mb * 2))
        ok = ok and result["ok"]
        print((result["stdout"] + result["stderr"]).strip() or "(no output)")
    return 0 if ok else 1


def add_device_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--all-devices", action="store_true", help="Use all connected devices.")
    group.add_argument("--devices", nargs="+", help="Explicit adb serials.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Android ADB test runner")
    subparsers = parser.add_subparsers(dest="command", required=True)

    devices_parser = subparsers.add_parser("devices")
    devices_parser.set_defaults(func=command_devices)

    connect_parser = subparsers.add_parser("connect")
    connect_parser.add_argument("--ip", required=True)
    connect_parser.set_defaults(func=command_connect)

    install_parser = subparsers.add_parser("install-apks")
    add_device_args(install_parser)
    install_parser.add_argument("--apk-dir", required=True)
    install_parser.set_defaults(func=command_install_apks)

    sample_parser = subparsers.add_parser("sample")
    add_device_args(sample_parser)
    sample_parser.add_argument("--memory-app")
    sample_parser.add_argument("--memory-process")
    sample_parser.add_argument("--cpu-app")
    sample_parser.add_argument("--interval", type=float, required=True)
    sample_parser.add_argument("--duration", type=int, required=True)
    sample_parser.add_argument("--output-dir")
    sample_parser.set_defaults(func=command_sample)

    plan_parser = subparsers.add_parser("run-plan")
    add_device_args(plan_parser)
    plan_parser.add_argument("--apk-dir")
    plan_parser.add_argument("--memory-app")
    plan_parser.add_argument("--memory-process")
    plan_parser.add_argument("--cpu-app")
    plan_parser.add_argument("--interval", type=float, required=True)
    plan_parser.add_argument("--duration", type=int, required=True)
    plan_parser.add_argument("--output-dir")
    plan_parser.set_defaults(func=command_run_plan)

    size_parser = subparsers.add_parser("screen-size")
    add_device_args(size_parser)
    size_parser.set_defaults(func=command_screen_size)

    screenshot_parser = subparsers.add_parser("screenshot")
    add_device_args(screenshot_parser)
    screenshot_parser.add_argument("--output-dir")
    screenshot_parser.set_defaults(func=command_screenshot)

    package_parser = subparsers.add_parser("current-package")
    add_device_args(package_parser)
    package_parser.set_defaults(func=command_current_package)

    fill_parser = subparsers.add_parser("fill-storage")
    add_device_args(fill_parser)
    fill_parser.add_argument("--remote-path", default="/sdcard")
    fill_parser.add_argument("--filename", required=True)
    fill_parser.add_argument("--size-mb", type=int, required=True)
    fill_parser.set_defaults(func=command_fill_storage)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("Interrupted.")
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI should report clear errors.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
