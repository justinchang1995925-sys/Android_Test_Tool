#!/usr/bin/env python3
"""Android smart-device test tool with a small web UI.

The app intentionally uses only Python's standard library so it can run on a
test bench without installing extra Python packages. It shells out to `adb`,
which must be available in PATH.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
import zipfile
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4
from xml.sax.saxutils import escape as xml_escape


if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR)).resolve()
else:
    APP_DIR = Path(__file__).resolve().parent
    RESOURCE_DIR = APP_DIR

DEFAULT_CAPTURE_DIR = APP_DIR / "captures"
DEFAULT_STATS_DIR = APP_DIR / "stats_exports"
DEFAULT_CHART_DIR = APP_DIR / "chart_exports"
DEFAULT_MONKEY_DIR = APP_DIR / "monkey_logs"
ADB_CONFIG_FILE = APP_DIR / "android_tool.json"
ADB_TIMEOUT_SECONDS = 120
RECORDING_LOCK = threading.Lock()
RECORDING_SESSIONS: dict[str, dict[str, Any]] = {}
INSTALL_LOCK = threading.Lock()
INSTALL_SESSIONS: dict[str, dict[str, Any]] = {}
FILL_LOCK = threading.Lock()
FILL_SESSIONS: dict[str, dict[str, Any]] = {}
MONKEY_LOCK = threading.Lock()
MONKEY_SESSIONS: dict[str, dict[str, Any]] = {}
_GLOBAL_ADB_LOCK = threading.Lock()
_SERIAL_ADB_LOCKS: dict[str, threading.Lock] = {}


def run_command(args: list[str], timeout: int = ADB_TIMEOUT_SECONDS, stdin: bytes | None = None) -> dict[str, Any]:
    """Run a command and return a JSON-friendly result."""
    try:
        completed = subprocess.run(
            args,
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return {
            "ok": False,
            "returncode": 127,
            "stdout": "",
            "stderr": "未找到 adb，请确认 Android platform-tools 已加入 PATH。",
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "returncode": -1,
            "stdout": decode_output(exc.stdout or b""),
            "stderr": f"命令执行超时（{timeout}s）。",
        }

    stdout = decode_output(completed.stdout)
    stderr = decode_output(completed.stderr)
    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": stdout,
        "stderr": stderr,
    }


def decode_output(data: bytes) -> str:
    for encoding in ("utf-8", "gbk", sys.getdefaultencoding()):
        try:
            return data.decode(encoding, errors="replace")
        except LookupError:
            continue
    return data.decode(errors="replace")


def load_adb_path_from_config() -> str:
    if not ADB_CONFIG_FILE.exists():
        return ""
    try:
        data = json.loads(ADB_CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("adbPath") or data.get("adb") or "").strip()


def normalize_adb_path(raw_path: str) -> str:
    path = Path(raw_path.strip()).expanduser()
    if not path.is_absolute():
        path = (APP_DIR / path).resolve()
    else:
        path = path.resolve()
    return str(path)


def find_adb_on_path() -> str:
    """Return the first adb on PATH, matching Windows `where adb` order."""
    path_env = os.environ.get("PATH", "")
    names = ("adb.exe", "adb") if os.name == "nt" else ("adb",)
    for directory in path_env.split(os.pathsep):
        if not directory.strip():
            continue
        base = Path(directory.strip())
        for name in names:
            candidate = base / name
            if candidate.is_file():
                return str(candidate.resolve())
    return ""


def resolve_adb_executable() -> tuple[str, str]:
    """Resolve adb once at startup. Priority: system PATH > ANDROID_ADB > android_tool.json."""
    found = find_adb_on_path() or shutil.which("adb") or ""
    if found:
        return str(Path(found).resolve()), "PATH"

    env_path = os.environ.get("ANDROID_ADB", "").strip()
    if env_path:
        return normalize_adb_path(env_path), "ANDROID_ADB"

    config_path = load_adb_path_from_config()
    if config_path:
        return normalize_adb_path(config_path), "android_tool.json"

    return "adb", "PATH"


ADB_EXECUTABLE, ADB_SOURCE = resolve_adb_executable()


def adb_args(serial: str | None = None) -> list[str]:
    args = [ADB_EXECUTABLE]
    if serial:
        args.extend(["-s", serial])
    return args


def adb_device_lock(serial: str | None) -> threading.Lock:
    if not serial:
        return _GLOBAL_ADB_LOCK
    with _GLOBAL_ADB_LOCK:
        if serial not in _SERIAL_ADB_LOCKS:
            _SERIAL_ADB_LOCKS[serial] = threading.Lock()
        return _SERIAL_ADB_LOCKS[serial]


def adb(serial: str | None, *args: str, timeout: int = ADB_TIMEOUT_SECONDS) -> dict[str, Any]:
    with adb_device_lock(serial):
        return run_command([*adb_args(serial), *args], timeout=timeout)


def get_adb_status() -> dict[str, Any]:
    adb_path = Path(ADB_EXECUTABLE)
    if adb_path.name != "adb" and not adb_path.is_file():
        return {
            "ok": False,
            "available": False,
            "adbPath": ADB_EXECUTABLE,
            "adbSource": ADB_SOURCE,
            "version": "",
            "readyDeviceCount": 0,
            "message": f"ADB 路径不存在：{ADB_EXECUTABLE}（来源 {ADB_SOURCE}）。",
        }

    version = run_command([ADB_EXECUTABLE, "version"], timeout=15)
    if not version["ok"]:
        hint = (
            "请确认 platform-tools 已加入 PATH，"
            f"或在 {ADB_CONFIG_FILE.name} / 环境变量 ANDROID_ADB 中指定 adbPath。"
        )
        return {
            "ok": False,
            "available": False,
            "adbPath": ADB_EXECUTABLE,
            "adbSource": ADB_SOURCE,
            "message": version["stderr"] or f"未找到可用的 adb。{hint}",
            "version": "",
            "readyDeviceCount": 0,
        }
    version_line = version["stdout"].splitlines()[0] if version["stdout"] else "adb"
    devices_result = adb(None, "devices", "-l", timeout=15)
    ready_count = 0
    if devices_result["ok"]:
        for line in devices_result["stdout"].splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                ready_count += 1
    source_hint = {
        "ANDROID_ADB": "环境变量 ANDROID_ADB",
        "android_tool.json": f"配置文件 {ADB_CONFIG_FILE.name}",
        "PATH": "系统 PATH",
    }.get(ADB_SOURCE, ADB_SOURCE)
    return {
        "ok": True,
        "available": True,
        "adbPath": ADB_EXECUTABLE,
        "adbSource": ADB_SOURCE,
        "version": version_line,
        "readyDeviceCount": ready_count,
        "message": f"{version_line}（{ADB_EXECUTABLE}，{source_hint}），已连接可用设备 {ready_count} 台。",
    }


def require_text(data: dict[str, Any], key: str, label: str) -> str:
    value = str(data.get(key, "")).strip()
    if not value:
        raise ValueError(f"请填写{label}。")
    return value


def parse_selected_devices(data: dict[str, Any]) -> list[str]:
    devices = data.get("devices")
    if not isinstance(devices, list) or not devices:
        raise ValueError("请至少选择一台设备。")
    serials = [str(device).strip() for device in devices if str(device).strip()]
    if not serials:
        raise ValueError("请至少选择一台设备。")
    return serials


def filter_lines(text: str, keyword: str) -> str:
    keyword_lower = keyword.lower()
    lines = [line for line in text.splitlines() if keyword_lower in line.lower()]
    return "\n".join(lines) if lines else f"未找到包含 {keyword!r} 的结果。"


def parse_memory_mb(text: str, keyword: str | None = None) -> float | None:
    """Extract memory usage in MB from dumpsys meminfo output."""
    if keyword:
        for line in text.splitlines():
            if keyword.lower() not in line.lower():
                continue
            match = re.search(r"([\d,]+)\s*K\b", line, flags=re.IGNORECASE)
            if match:
                return int(match.group(1).replace(",", "")) / 1024

    patterns = [
        r"^\s*TOTAL\s+([\d,]+)\b",
        r"^\s*TOTAL\s+PSS:\s*([\d,]+)\b",
    ]
    for pattern in patterns:
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
        pid_matches = tokens[0] in pid_set
        package_matches = package_name.lower() in stripped.lower()
        if not pid_matches and not package_matches:
            continue

        if cpu_index is not None and cpu_index < len(tokens):
            value = parse_number_token(tokens[cpu_index])
            if value is None and cpu_index + 1 < len(tokens):
                value = parse_number_token(tokens[cpu_index + 1])
            if value is not None:
                values.append(value)
                continue

        # Fallback for top variants without a recognizable header.
        for token in tokens[1:]:
            value = parse_number_token(token)
            if value is not None:
                values.append(value)
                break
    return sum(values) if values else None


def parse_total_cpu_percent(text: str) -> float | None:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        capacity_match = re.search(r"([\d.]+)%\s*cpu\b", stripped, flags=re.IGNORECASE)
        idle_match = re.search(r"([\d.]+)%\s*(?:idle|id)\b", stripped, flags=re.IGNORECASE)
        if capacity_match and idle_match:
            capacity = float(capacity_match.group(1))
            idle = float(idle_match.group(1))
            if capacity > 100:
                return max(0.0, min(100.0, (capacity - idle) * 100 / capacity))
            return max(0.0, min(100.0, capacity - idle))

        cpu_line = re.search(r"%?cpu(?:\(s\))?\s*[:：]\s*(.+)", stripped, flags=re.IGNORECASE)
        if cpu_line:
            detail = cpu_line.group(1)
            idle_match = re.search(r"([\d.]+)\s*(?:id|idle)", detail, flags=re.IGNORECASE)
            if idle_match:
                return max(0.0, min(100.0, 100.0 - float(idle_match.group(1))))

            values = [
                float(value)
                for value, label in re.findall(r"([\d.]+)%?\s*([A-Za-z]+)", detail)
                if label.lower() in {"usr", "user", "sys", "system", "nice", "irq", "sirq", "softirq", "iow", "iowait"}
            ]
            if values:
                return max(0.0, min(100.0, sum(values)))
    return None


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
                return value, "CPU 来源：top\n"
        messages.append("top 未解析到 CPU，已回退到 dumpsys cpuinfo。\n")

    cpu_result = adb(serial, "shell", "dumpsys", "cpuinfo", timeout=60)
    if cpu_result["ok"]:
        value = parse_cpu_percent(cpu_result["stdout"], package_name)
        if value is not None:
            return value, "CPU 来源：dumpsys cpuinfo\n"
    else:
        messages.append(cpu_result["stderr"] or "CPU 采样失败。")

    if pids:
        messages.append("进程存在但未输出 CPU 占用，按 0.0% 记录。\n")
        return 0.0, "".join(messages)
    messages.append(f"APP 未运行或未出现在 CPU 统计中：{package_name}\n")
    return None, "".join(messages)


def get_total_cpu_percent(serial: str) -> tuple[float | None, str]:
    for command in (("top", "-b", "-n", "1"), ("top", "-n", "1")):
        result = adb(serial, "shell", *command, timeout=30)
        if not result["ok"]:
            continue
        value = parse_total_cpu_percent(result["stdout"])
        if value is not None:
            return value, "CPU 总占用来源：top\n"
    return None, "未解析到 CPU 总占用率。\n"


def safe_remote_file(path: str, filename: str) -> str:
    remote_dir = path.strip().replace("\\", "/") or "/sdcard"
    if not remote_dir.startswith("/"):
        remote_dir = "/" + remote_dir
    name = PurePosixPath(filename.strip()).name
    if not name:
        raise ValueError("请填写文件名称。")
    return posixpath.join(remote_dir.rstrip("/"), name)


def ensure_local_dir(path_text: str | None) -> Path:
    path = Path(path_text).expanduser() if path_text else DEFAULT_CAPTURE_DIR
    if not path.is_absolute():
        path = (APP_DIR / path).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def open_local_dir(path: Path) -> None:
    """Open a local folder without failing the main operation."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as exc:  # noqa: BLE001 - opening a folder is best-effort.
        print(f"打开目录失败：{path} ({exc})")


def sanitize_filename(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_") or "device"


def timestamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


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
    escaped = xml_escape(str(value))
    return f'<c r="{reference}" t="inlineStr"><is><t>{escaped}</t></is></c>'


def write_xlsx(path: Path, rows: list[list[Any]]) -> None:
    sheet_rows = []
    for row_index, row in enumerate(rows, start=1):
        cells = "".join(xlsx_cell(row_index, column_index, value) for column_index, value in enumerate(row, start=1))
        sheet_rows.append(f'<row r="{row_index}">{cells}</row>')

    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData>'
        f'{"".join(sheet_rows)}'
        '</sheetData>'
        '</worksheet>'
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Stats" sheetId="1" r:id="rId1"/></sheets>'
        '</workbook>'
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
        'Target="xl/workbook.xml"/>'
        '</Relationships>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        '</Relationships>'
    )

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as workbook:
        workbook.writestr("[Content_Types].xml", content_types)
        workbook.writestr("_rels/.rels", root_rels)
        workbook.writestr("xl/workbook.xml", workbook_xml)
        workbook.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        workbook.writestr("xl/worksheets/sheet1.xml", sheet_xml)


def list_devices() -> dict[str, Any]:
    result = adb(None, "devices", "-l")
    devices: list[dict[str, str]] = []
    if result["ok"]:
        for line in result["stdout"].splitlines()[1:]:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            serial = parts[0]
            state = parts[1] if len(parts) > 1 else "unknown"
            details = " ".join(parts[2:])
            devices.append({"serial": serial, "state": state, "details": details})
    return {"devices": devices, "raw": result}


def connect_ip(ip: str) -> dict[str, Any]:
    target = ip.strip()
    if ":" not in target:
        target = f"{target}:5555"
    result = adb(None, "connect", target, timeout=20)
    return {"target": target, "result": result, "devices": list_devices().get("devices", [])}


def for_each_device(data: dict[str, Any], worker) -> dict[str, Any]:
    results = []
    for serial in parse_selected_devices(data):
        try:
            item = worker(serial)
        except ValueError as exc:
            item = {"ok": False, "stdout": "", "stderr": str(exc)}
        results.append({"device": serial, **item})
    return {"results": results}


def get_process_memory(data: dict[str, Any]) -> dict[str, Any]:
    process = require_text(data, "process", "系统进程名称")

    def worker(serial: str) -> dict[str, Any]:
        result = adb(serial, "shell", "dumpsys", "meminfo", timeout=60)
        if result["ok"]:
            result["stdout"] = filter_lines(result["stdout"], process)
        return result

    return for_each_device(data, worker)


def get_app_memory(data: dict[str, Any]) -> dict[str, Any]:
    package_name = require_text(data, "package", "APP 包名")

    def worker(serial: str) -> dict[str, Any]:
        return adb(serial, "shell", "dumpsys", "meminfo", package_name, timeout=60)

    return for_each_device(data, worker)


def get_app_cpu(data: dict[str, Any]) -> dict[str, Any]:
    package_name = require_text(data, "package", "APP 包名")

    def worker(serial: str) -> dict[str, Any]:
        value, message = get_app_cpu_percent(serial, package_name)
        if value is None:
            return {"ok": False, "returncode": 1, "stdout": "", "stderr": message}
        return {
            "ok": True,
            "returncode": 0,
            "stdout": f"{package_name} CPU 占用率：{value:.2f}%\n{message}",
            "stderr": "",
        }

    return for_each_device(data, worker)


def get_total_cpu(data: dict[str, Any]) -> dict[str, Any]:
    def worker(serial: str) -> dict[str, Any]:
        value, message = get_total_cpu_percent(serial)
        if value is None:
            return {"ok": False, "returncode": 1, "stdout": "", "stderr": message}
        return {
            "ok": True,
            "returncode": 0,
            "stdout": f"设备 CPU 总占用率：{value:.2f}%\n{message}",
            "stderr": "",
        }

    return for_each_device(data, worker)


def get_app_and_total_cpu(data: dict[str, Any]) -> dict[str, Any]:
    package_name = require_text(data, "package", "APP 包名")

    def worker(serial: str) -> dict[str, Any]:
        app_value, app_message = get_app_cpu_percent(serial, package_name)
        total_value, total_message = get_total_cpu_percent(serial)
        ok = app_value is not None and total_value is not None
        stdout_parts = []
        stderr_parts = []
        if app_value is None:
            stderr_parts.append(app_message)
        else:
            stdout_parts.append(f"{package_name} CPU 占用率：{app_value:.2f}%")
        if total_value is None:
            stderr_parts.append(total_message)
        else:
            stdout_parts.append(f"设备 CPU 总占用率：{total_value:.2f}%")
        return {
            "ok": ok,
            "returncode": 0 if ok else 1,
            "stdout": "\n".join(stdout_parts),
            "stderr": "\n".join(part for part in stderr_parts if part),
        }

    return for_each_device(data, worker)


def get_stats_sample(data: dict[str, Any]) -> dict[str, Any]:
    memory_mode = str(data.get("memoryMode", "app")).strip()
    memory_target = str(data.get("memoryTarget", "")).strip()
    cpu_package = str(data.get("cpuPackage", "")).strip()
    cpu_mode = str(data.get("cpuMode", "app" if cpu_package else "none")).strip()
    if cpu_mode not in {"none", "app", "total", "both"}:
        raise ValueError("CPU 统计类型只能是 none、app、total 或 both。")
    if not memory_target and not cpu_package and cpu_mode != "total":
        raise ValueError("请至少填写内存统计目标或 CPU 包名。")
    if cpu_mode in {"app", "both"} and not cpu_package:
        raise ValueError("请选择 APP CPU 时必须填写 APP 包名。")
    if memory_mode not in {"app", "process"}:
        raise ValueError("内存统计类型只能是 app 或 process。")

    def worker(serial: str) -> dict[str, Any]:
        sample: dict[str, Any] = {
            "ok": True,
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "timestamp": int(time.time() * 1000),
            "memoryMb": None,
            "cpuPercent": None,
            "totalCpuPercent": None,
        }

        if memory_target:
            if memory_mode == "process":
                mem_result = adb(serial, "shell", "dumpsys", "meminfo", timeout=60)
                if mem_result["ok"]:
                    sample["memoryMb"] = parse_memory_mb(mem_result["stdout"], memory_target)
                else:
                    sample["ok"] = False
                    sample["stderr"] += mem_result["stderr"] or "内存采样失败。"
            else:
                mem_result = adb(serial, "shell", "dumpsys", "meminfo", memory_target, timeout=60)
                if mem_result["ok"]:
                    sample["memoryMb"] = parse_memory_mb(mem_result["stdout"])
                else:
                    sample["ok"] = False
                    sample["stderr"] += mem_result["stderr"] or "内存采样失败。"
            if sample["memoryMb"] is None:
                sample["ok"] = False
                sample["stderr"] += f"未解析到内存数据：{memory_target}\n"

        if cpu_mode in {"app", "both"}:
            sample["cpuPercent"], cpu_message = get_app_cpu_percent(serial, cpu_package)
            if cpu_message and not cpu_message.startswith("CPU 来源"):
                sample["stderr"] += cpu_message
            if sample["cpuPercent"] is None:
                sample["ok"] = False
                sample["stderr"] += f"未解析到 CPU 数据：{cpu_package}\n"

        if cpu_mode in {"total", "both"}:
            sample["totalCpuPercent"], total_cpu_message = get_total_cpu_percent(serial)
            if total_cpu_message and not total_cpu_message.startswith("CPU 总占用来源"):
                sample["stderr"] += total_cpu_message
            if sample["totalCpuPercent"] is None:
                sample["ok"] = False
                sample["stderr"] += "未解析到 CPU 总占用率\n"

        memory_text = "-" if sample["memoryMb"] is None else f"{sample['memoryMb']:.2f} MB"
        cpu_text = "-" if sample["cpuPercent"] is None else f"{sample['cpuPercent']:.2f}%"
        total_cpu_text = "-" if sample["totalCpuPercent"] is None else f"{sample['totalCpuPercent']:.2f}%"
        sample["stdout"] = f"memory={memory_text}, app_cpu={cpu_text}, total_cpu={total_cpu_text}"
        return sample

    return for_each_device(data, worker)


def save_stats_excel(data: dict[str, Any]) -> dict[str, Any]:
    rows = data.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("没有可保存的采样数据。")

    output_dir = ensure_local_dir(str(data.get("outputDir") or "").strip() or str(DEFAULT_STATS_DIR))
    filename = str(data.get("filename") or f"android_stats_{timestamp()}.xlsx").strip()
    safe_name = sanitize_filename(filename)
    if not safe_name.lower().endswith(".xlsx"):
        safe_name += ".xlsx"
    output_file = output_dir / safe_name

    headers = [
        "采样时间",
        "设备",
        "内存类型",
        "内存目标",
        "内存(MB)",
        "CPU包名",
        "CPU占用率(%)",
        "CPU总占用率(%)",
        "内存阈值(MB)",
        "CPU阈值(%)",
        "抓取间隔(秒)",
        "抓取总时长(秒)",
        "采样状态",
        "错误信息",
    ]
    table: list[list[Any]] = [headers]
    for row in rows:
        if not isinstance(row, dict):
            continue
        table.append(
            [
                row.get("time", ""),
                row.get("device", ""),
                row.get("memoryMode", ""),
                row.get("memoryTarget", ""),
                row.get("memoryMb"),
                row.get("cpuPackage", ""),
                row.get("cpuPercent"),
                row.get("totalCpuPercent"),
                row.get("memoryThreshold"),
                row.get("cpuThreshold"),
                row.get("intervalSec"),
                row.get("durationSec"),
                "成功" if row.get("ok") else "失败",
                row.get("stderr", ""),
            ]
        )

    if len(table) == 1:
        raise ValueError("没有有效的采样数据可保存。")
    write_xlsx(output_file, table)
    open_local_dir(output_dir)
    return {
        "path": str(output_file),
        "rows": len(table) - 1,
        "message": f"Excel 已保存：{output_file}",
    }


def save_stats_charts(data: dict[str, Any]) -> dict[str, Any]:
    charts = data.get("charts")
    if not isinstance(charts, list) or not charts:
        raise ValueError("没有可导出的曲线图。")

    output_dir = ensure_local_dir(str(data.get("outputDir") or "").strip() or str(DEFAULT_CHART_DIR))
    prefix = sanitize_filename(str(data.get("prefix") or f"android_charts_{timestamp()}"))
    saved_files: list[str] = []

    for chart in charts:
        if not isinstance(chart, dict):
            continue
        name = sanitize_filename(str(chart.get("name") or "chart"))
        html_content = str(chart.get("html") or "")
        if html_content:
            output_file = output_dir / f"{prefix}_{name}.html"
            output_file.write_text(html_content, encoding="utf-8")
            saved_files.append(str(output_file))
            continue
        data_url = str(chart.get("dataUrl") or "")
        marker = "base64,"
        if data_url.startswith("data:image/png;") and marker in data_url:
            raw = base64.b64decode(data_url.split(marker, 1)[1])
            output_file = output_dir / f"{prefix}_{name}.png"
            output_file.write_bytes(raw)
            saved_files.append(str(output_file))

    if not saved_files:
        raise ValueError("没有有效的曲线图数据可保存。")
    open_local_dir(output_dir)
    return {
        "paths": saved_files,
        "count": len(saved_files),
        "message": f"交互式曲线 HTML 已保存到：{output_dir}",
    }


def fill_memory(data: dict[str, Any]) -> dict[str, Any]:
    remote_path = require_text(data, "remotePath", "设备填充路径")
    filename = require_text(data, "filename", "填充文件名称")
    size_mb = int(data.get("sizeMb") or 0)
    if size_mb <= 0:
        raise ValueError("填充大小必须大于 0 MB。")
    remote_file = safe_remote_file(remote_path, filename)
    command = f"dd if=/dev/zero of={shlex.quote(remote_file)} bs=1048576 count={size_mb}"

    def worker(serial: str) -> dict[str, Any]:
        return adb(serial, "shell", command, timeout=max(120, size_mb * 2))

    return for_each_device(data, worker)


def prepare_fill_memory(data: dict[str, Any]) -> tuple[list[str], str, str, int, str]:
    devices = parse_selected_devices(data)
    remote_path = require_text(data, "remotePath", "设备填充路径")
    filename = require_text(data, "filename", "填充文件名称")
    size_mb = int(data.get("sizeMb") or 0)
    if size_mb <= 0:
        raise ValueError("填充大小必须大于 0 MB。")
    remote_file = safe_remote_file(remote_path, filename)
    command = f"dd if=/dev/zero of={shlex.quote(remote_file)} bs=1048576 count={size_mb}"
    return devices, remote_file, command, size_mb, filename


def session_is_cancelled(session: dict[str, Any]) -> bool:
    return bool(session.get("cancelRequested"))


def public_session(session: dict[str, Any]) -> dict[str, Any]:
    data = dict(session)
    data.pop("process", None)
    return data


def cancel_session(store: dict[str, dict[str, Any]], lock: threading.Lock, session_id: str, label: str) -> dict[str, Any]:
    monkey_process = None
    with lock:
        session = store.get(session_id)
        if not session:
            raise ValueError(f"{label}任务不存在或已过期。")
        if not session.get("running"):
            raise ValueError(f"{label}任务已结束，无法取消。")
        session["cancelRequested"] = True
        session["message"] = f"正在取消{label}..."
        monkey_process = session.get("process")
    if label == "Monkey" and monkey_process is not None:
        try:
            monkey_process.terminate()
        except OSError:
            pass
    with lock:
        return {"session": public_session(store[session_id])}


def fill_memory_session_worker(session_id: str, devices: list[str], remote_file: str, command: str, size_mb: int) -> None:
    total = len(devices)
    ok = True
    cancelled = False
    with FILL_LOCK:
        session = FILL_SESSIONS[session_id]
        session.update({"running": True, "total": total, "done": 0, "percent": 0, "message": "开始内存填充...", "cancelRequested": False})

    for serial in devices:
        with FILL_LOCK:
            session = FILL_SESSIONS[session_id]
            if session_is_cancelled(session):
                cancelled = True
                break
            session["currentDevice"] = serial
            session["message"] = f"正在填充 {serial}：{remote_file} ({size_mb} MB)"
            session["logs"].append(f"[{time.strftime('%H:%M:%S')}] 开始：{serial} -> {remote_file} ({size_mb} MB)")

        if cancelled:
            break

        result = adb(serial, "shell", command, timeout=max(120, size_mb * 2))
        ok = ok and result["ok"]
        text = (result["stdout"] + result["stderr"]).strip() or "(无输出)"
        with FILL_LOCK:
            session = FILL_SESSIONS[session_id]
            session["done"] += 1
            session["percent"] = round(session["done"] * 100 / total, 1)
            state = "成功" if result["ok"] else "失败"
            session["logs"].append(f"[{time.strftime('%H:%M:%S')}] {state}：{serial} -> {remote_file}\n{text}")

    with FILL_LOCK:
        session = FILL_SESSIONS[session_id]
        session["running"] = False
        session["ok"] = ok and not cancelled
        session["currentDevice"] = ""
        if cancelled:
            session["message"] = "内存填充已取消。"
        else:
            session["message"] = "内存填充完成。" if ok else "内存填充完成，但存在失败项。"
        session["completedAt"] = time.time()


def cancel_fill_memory(data: dict[str, Any]) -> dict[str, Any]:
    session_id = require_text(data, "sessionId", "内存填充任务 ID")
    return cancel_session(FILL_SESSIONS, FILL_LOCK, session_id, "内存填充")


def start_fill_memory(data: dict[str, Any]) -> dict[str, Any]:
    devices, remote_file, command, size_mb, filename = prepare_fill_memory(data)
    session_id = uuid4().hex
    session = {
        "sessionId": session_id,
        "running": False,
        "ok": True,
        "devices": devices,
        "remoteFile": remote_file,
        "filename": filename,
        "sizeMb": size_mb,
        "total": len(devices),
        "done": 0,
        "percent": 0,
        "currentDevice": "",
        "message": "等待内存填充任务启动...",
        "logs": [],
        "cancelRequested": False,
        "createdAt": time.time(),
        "completedAt": None,
    }
    with FILL_LOCK:
        FILL_SESSIONS[session_id] = session
    thread = threading.Thread(
        target=fill_memory_session_worker,
        args=(session_id, devices, remote_file, command, size_mb),
        daemon=True,
    )
    thread.start()
    return {"session": public_session(session)}


def get_fill_memory_status(data: dict[str, Any]) -> dict[str, Any]:
    session_id = require_text(data, "sessionId", "内存填充任务 ID")
    with FILL_LOCK:
        session = FILL_SESSIONS.get(session_id)
        if not session:
            raise ValueError("内存填充任务不存在或已过期。")
        return {"session": public_session(session)}


def int_option(data: dict[str, Any], key: str, default: int, minimum: int, label: str) -> int:
    value = int(data.get(key) or default)
    if value < minimum:
        raise ValueError(f"{label}必须大于等于 {minimum}。")
    return value


ADB_INSTALL_TIMEOUT_SECONDS = 600


def sort_apks_for_install(apks: list[Path]) -> list[Path]:
    """Match install_apks.sh: install non-launcher APKs first, launcher APKs last."""
    non_launcher = sorted([apk for apk in apks if "launcher" not in apk.name.lower()])
    launcher = sorted([apk for apk in apks if "launcher" in apk.name.lower()])
    return non_launcher + launcher


def get_device_connection_target(serial: str) -> tuple[str, str]:
    """Pick adb target that avoids broken Push Install on some vendor adb builds.

    PUDU adb fails ``adb -s <serial> install`` with ``unknown host service`` but succeeds with
    ``adb install`` (single device) or ``adb -t <transport_id> install``.
    """
    ready_devices = [
        device
        for device in list_devices().get("devices", [])
        if device.get("state") == "device"
    ]
    selected = next((device for device in ready_devices if device.get("serial") == serial), None)
    if selected:
        match = re.search(r"transport_id:(\d+)", selected.get("details", ""))
        if match:
            return "transport", match.group(1)
        if len(ready_devices) == 1:
            return "default", ""
    return "serial", serial


def adb_install(serial: str, *args: str, timeout: int = ADB_INSTALL_TIMEOUT_SECONDS) -> dict[str, Any]:
    target_type, target_value = get_device_connection_target(serial)
    with adb_device_lock(serial):
        if target_type == "transport":
            cmd = [ADB_EXECUTABLE, "-t", target_value, *args]
            target_label = f"-t {target_value}"
        elif target_type == "default":
            cmd = [ADB_EXECUTABLE, *args]
            target_label = "default device"
        else:
            cmd = [ADB_EXECUTABLE, "-s", serial, *args]
            target_label = f"-s {serial}"
        result = run_command(cmd, timeout=timeout)
        result["adbTarget"] = target_label
        return result


def parse_install_error(output: str) -> str:
    text = output or ""
    markers = {
        "INSTALL_FAILED_ALREADY_EXISTS": "应用已存在，可尝试 adb install -r",
        "INSTALL_FAILED_INVALID_APK": "无效的 APK 文件",
        "INSTALL_FAILED_INSUFFICIENT_STORAGE": "设备存储空间不足",
        "INSTALL_FAILED_UPDATE_INCOMPATIBLE": "版本不兼容，无法更新",
        "INSTALL_PARSE_FAILED": "APK 解析失败",
        "INSTALL_FAILED_VERSION_DOWNGRADE": "版本降级不被允许",
        "no devices/emulators found": "未找到连接的设备",
        "device not found": "设备未找到",
        "Permission denied": "权限被拒绝",
        "unknown host service": "ADB 与设备通信异常（unknown host service）",
    }
    for key, message in markers.items():
        if key in text:
            return message
    for line in text.splitlines():
        if re.search(r"(?i)(error|failed|failure)", line):
            return line.strip()
    return text.strip() or "安装失败"


def install_apk_on_device(serial: str, apk_path: Path) -> dict[str, Any]:
    """Install APK using the same flags as install_apks.sh: adb install -r -d."""
    result = adb_install(serial, "install", "-r", "-d", str(apk_path))
    target = result.pop("adbTarget", "")
    result["installMethod"] = f"install -r -d ({target})" if target else "install -r -d"
    if not result["ok"]:
        combined = (result["stdout"] + result["stderr"]).strip()
        result["errorSummary"] = parse_install_error(combined)
    return result


def build_monkey_args(data: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    whitelist_file = "/sdcard/whitelist.txt"
    blacklist_file = "/sdcard/blacklist.txt"
    test_packages_raw = str(data.get("testPackages") or "").strip()
    # Backward-compatible: older UI used "whitelistPackages" as tested package list.
    if not test_packages_raw:
        test_packages_raw = str(data.get("whitelistPackages") or "").strip()
    exclude_packages_raw = str(data.get("excludePackages") or "").strip()
    if not exclude_packages_raw:
        exclude_packages_raw = str(data.get("blacklistPackages") or "").strip()

    test_packages = [
        package.strip()
        for package in re.split(r"[\s,;，；]+", test_packages_raw)
        if package.strip()
    ]
    exclude_packages = [
        package.strip()
        for package in re.split(r"[\s,;，；]+", exclude_packages_raw)
        if package.strip()
    ]
    if not test_packages:
        raise ValueError("请至少填写一个 Monkey 被测 APP 包名。")
    event_count = int_option(data, "eventCount", 10000, 1, "Monkey 事件数")
    throttle = int_option(data, "throttle", 300, 0, "事件间隔")
    pct_syskeys = int_option(data, "pctSyskeys", 0, 0, "系统按键事件占比")
    pct_touch = int_option(data, "pctTouch", 90, 0, "触摸事件占比")
    seed = str(data.get("seed") or "").strip()
    selected_options = data.get("selectedOptions")
    if not isinstance(selected_options, list):
        selected_options = []
    selected_options = [str(option).strip() for option in selected_options if str(option).strip()]
    extra_args = shlex.split(str(data.get("extraArgs") or "").strip())

    if pct_syskeys > 100 or pct_touch > 100:
        raise ValueError("事件占比不能超过 100。")

    args = [
        "shell",
        "monkey",
        "--pkg-whitelist-file",
        whitelist_file,
    ]
    if exclude_packages:
        args.extend(["--pkg-blacklist-file", blacklist_file])
    args.extend([
        *selected_options,
        "--pct-syskeys",
        str(pct_syskeys),
        "--pct-touch",
        str(pct_touch),
        "--throttle",
        str(throttle),
    ])
    if seed:
        args.extend(["-s", seed])
    args.extend(extra_args)
    args.append(str(event_count))
    return args, {
        "whitelistFile": whitelist_file,
        "blacklistFile": blacklist_file,
        "testPackages": test_packages,
        "excludePackages": exclude_packages,
        "eventCount": event_count,
        "throttle": throttle,
        "pctSyskeys": pct_syskeys,
        "pctTouch": pct_touch,
        "seed": seed,
        "selectedOptions": selected_options,
        "extraArgs": extra_args,
    }


def classify_monkey_anomaly(line: str) -> str | None:
    upper = line.upper()
    if "CRASH:" in upper or "NATIVE CRASH" in upper or "// CRASH" in upper:
        return "CRASH"
    if "ANR" in upper or "NOT RESPONDING" in upper:
        return "ANR"
    return None


def monkey_session_worker(
    session_id: str,
    devices: list[str],
    monkey_args: list[str],
    output_dir: Path,
    test_packages: list[str],
    exclude_packages: list[str],
) -> None:
    ok = True
    cancelled = False
    with MONKEY_LOCK:
        session = MONKEY_SESSIONS[session_id]
        session.update({
            "running": True,
            "total": len(devices),
            "done": 0,
            "percent": 0,
            "message": "开始 Monkey 测试...",
            "cancelRequested": False,
            "process": None,
        })

    for serial in devices:
        with MONKEY_LOCK:
            session = MONKEY_SESSIONS[session_id]
            if session_is_cancelled(session):
                cancelled = True
                break
        log_file = output_dir / f"monkey_{sanitize_filename(serial)}_{timestamp()}.log"
        whitelist_local_file = output_dir / f"whitelist_{sanitize_filename(serial)}_{timestamp()}.txt"
        whitelist_local_file.write_text("\n".join(test_packages) + "\n", encoding="utf-8")
        push_result = adb(serial, "push", str(whitelist_local_file), "/sdcard/whitelist.txt", timeout=30)
        if not push_result["ok"]:
            ok = False
            with MONKEY_LOCK:
                session = MONKEY_SESSIONS[session_id]
                session["done"] += 1
                session["percent"] = round(session["done"] * 100 / session["total"], 1)
                session["logs"].append(
                    f"[{time.strftime('%H:%M:%S')}] {serial}: 白名单 push 失败\n"
                    f"{push_result['stdout']}{push_result['stderr']}"
                )
            continue
        if exclude_packages:
            blacklist_local_file = output_dir / f"blacklist_{sanitize_filename(serial)}_{timestamp()}.txt"
            blacklist_local_file.write_text("\n".join(exclude_packages) + "\n", encoding="utf-8")
            push_black = adb(serial, "push", str(blacklist_local_file), "/sdcard/blacklist.txt", timeout=30)
            if not push_black["ok"]:
                ok = False
                with MONKEY_LOCK:
                    session = MONKEY_SESSIONS[session_id]
                    session["done"] += 1
                    session["percent"] = round(session["done"] * 100 / session["total"], 1)
                    session["logs"].append(
                        f"[{time.strftime('%H:%M:%S')}] {serial}: 黑名单 push 失败\n"
                        f"{push_black['stdout']}{push_black['stderr']}"
                    )
                continue
        command = [*adb_args(serial), *monkey_args]
        with MONKEY_LOCK:
            session = MONKEY_SESSIONS[session_id]
            session["currentDevice"] = serial
            session["message"] = f"正在执行 Monkey：{serial}"
            session["logs"].append(f"[{time.strftime('%H:%M:%S')}] 已生成并推送白名单：{serial} -> /sdcard/whitelist.txt")
            if exclude_packages:
                session["logs"].append(f"[{time.strftime('%H:%M:%S')}] 已生成并推送黑名单：{serial} -> /sdcard/blacklist.txt")
            session["logs"].append(f"[{time.strftime('%H:%M:%S')}] 开始：{serial}")
            session["logFiles"].append(str(log_file))

        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            with MONKEY_LOCK:
                MONKEY_SESSIONS[session_id]["process"] = process
        except FileNotFoundError:
            ok = False
            with MONKEY_LOCK:
                session = MONKEY_SESSIONS[session_id]
                session["logs"].append("未找到 adb，请确认 Android platform-tools 已加入 PATH。")
            continue

        with log_file.open("w", encoding="utf-8", errors="replace") as log:
            log.write(f"Command: {' '.join(command)}\n")
            log.write(f"Start: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            assert process.stdout is not None
            for raw_line in iter(process.stdout.readline, b""):
                with MONKEY_LOCK:
                    if session_is_cancelled(MONKEY_SESSIONS[session_id]):
                        cancelled = True
                        process.terminate()
                        break
                line = decode_output(raw_line).rstrip()
                if not line:
                    continue
                now = time.strftime("%Y-%m-%d %H:%M:%S")
                log.write(f"[{now}] {line}\n")
                anomaly_type = classify_monkey_anomaly(line)
                with MONKEY_LOCK:
                    session = MONKEY_SESSIONS[session_id]
                    session["lastLine"] = line
                    if len(session["logs"]) < 300:
                        session["logs"].append(f"[{time.strftime('%H:%M:%S')}] {serial}: {line}")
                    if anomaly_type:
                        anomaly = {"time": now, "device": serial, "type": anomaly_type, "line": line}
                        session["anomalies"].append(anomaly)
                        session["crashCount"] += 1 if anomaly_type == "CRASH" else 0
                        session["anrCount"] += 1 if anomaly_type == "ANR" else 0

            if not cancelled:
                return_code = process.wait()
                log.write(f"\nEnd: {time.strftime('%Y-%m-%d %H:%M:%S')}\nReturn code: {return_code}\n")
                if return_code != 0:
                    ok = False

        with MONKEY_LOCK:
            session = MONKEY_SESSIONS[session_id]
            session["process"] = None
            session["done"] += 1
            session["percent"] = round(session["done"] * 100 / session["total"], 1)
            if cancelled:
                session["logs"].append(f"[{time.strftime('%H:%M:%S')}] 已取消：{serial}")
            else:
                session["logs"].append(f"[{time.strftime('%H:%M:%S')}] 完成：{serial}，日志：{log_file}")

        if cancelled:
            break

    with MONKEY_LOCK:
        session = MONKEY_SESSIONS[session_id]
        session["running"] = False
        session["process"] = None
        if cancelled:
            session["ok"] = False
            session["message"] = "Monkey 测试已取消。"
        else:
            session["ok"] = ok and session["crashCount"] == 0 and session["anrCount"] == 0
            session["message"] = (
                "Monkey 测试完成，未发现 Crash/ANR。"
                if session["ok"]
                else f"Monkey 测试完成，Crash {session['crashCount']} 次，ANR {session['anrCount']} 次。"
            )
        session["currentDevice"] = ""
        session["completedAt"] = time.time()
    if not cancelled:
        open_local_dir(output_dir)


def start_monkey_test(data: dict[str, Any]) -> dict[str, Any]:
    devices = parse_selected_devices(data)
    output_dir = ensure_local_dir(str(data.get("outputDir") or "").strip() or str(DEFAULT_MONKEY_DIR))
    monkey_args, config = build_monkey_args(data)
    session_id = uuid4().hex
    session = {
        "sessionId": session_id,
        "running": False,
        "ok": True,
        "devices": devices,
        "total": len(devices),
        "done": 0,
        "percent": 0,
        "currentDevice": "",
        "message": "等待 Monkey 任务启动...",
        "config": config,
        "logs": [],
        "logFiles": [],
        "lastLine": "",
        "crashCount": 0,
        "anrCount": 0,
        "anomalies": [],
        "cancelRequested": False,
        "process": None,
        "createdAt": time.time(),
        "completedAt": None,
    }
    with MONKEY_LOCK:
        MONKEY_SESSIONS[session_id] = session
    thread = threading.Thread(
        target=monkey_session_worker,
        args=(session_id, devices, monkey_args, output_dir, config["testPackages"], config["excludePackages"]),
        daemon=True,
    )
    thread.start()
    return {"session": public_session(session)}


def get_monkey_status(data: dict[str, Any]) -> dict[str, Any]:
    session_id = require_text(data, "sessionId", "Monkey 任务 ID")
    with MONKEY_LOCK:
        session = MONKEY_SESSIONS.get(session_id)
        if not session:
            raise ValueError("Monkey 任务不存在或已过期。")
        return {"session": public_session(session)}


def cancel_monkey_test(data: dict[str, Any]) -> dict[str, Any]:
    session_id = require_text(data, "sessionId", "Monkey 任务 ID")
    return cancel_session(MONKEY_SESSIONS, MONKEY_LOCK, session_id, "Monkey")


def install_apks(data: dict[str, Any]) -> dict[str, Any]:
    apk_dir = Path(require_text(data, "apkDir", "APK 存放路径")).expanduser()
    if not apk_dir.is_absolute():
        apk_dir = (APP_DIR / apk_dir).resolve()
    if not apk_dir.exists() or not apk_dir.is_dir():
        raise ValueError(f"APK 路径不存在或不是目录：{apk_dir}")
    apks = sort_apks_for_install(sorted(apk_dir.glob("*.apk")))
    if not apks:
        raise ValueError(f"目录下没有找到 APK 文件：{apk_dir}")

    def worker(serial: str) -> dict[str, Any]:
        outputs = []
        ok = True
        for apk_path in apks:
            result = install_apk_on_device(serial, apk_path)
            ok = ok and result["ok"]
            method = result.get("installMethod", "install")
            summary = result.get("errorSummary", "")
            outputs.append(
                f"===== {apk_path.name} ({method}) =====\n"
                f"returncode: {result['returncode']}\n"
                + (f"错误摘要: {summary}\n" if summary else "")
                + f"{result['stdout']}{result['stderr']}"
            )
        return {"ok": ok, "returncode": 0 if ok else 1, "stdout": "\n".join(outputs), "stderr": ""}

    return {**for_each_device(data, worker), "apks": [str(apk) for apk in apks]}


def prepare_apk_install(data: dict[str, Any]) -> tuple[list[str], Path, list[Path]]:
    devices = parse_selected_devices(data)
    apk_dir = Path(require_text(data, "apkDir", "APK 存放路径")).expanduser()
    if not apk_dir.is_absolute():
        apk_dir = (APP_DIR / apk_dir).resolve()
    if not apk_dir.exists() or not apk_dir.is_dir():
        raise ValueError(f"APK 路径不存在或不是目录：{apk_dir}")
    apks = sort_apks_for_install(sorted(apk_dir.glob("*.apk")))
    if not apks:
        raise ValueError(f"目录下没有找到 APK 文件：{apk_dir}")
    return devices, apk_dir, apks


def install_apk_session_worker(session_id: str, devices: list[str], apks: list[Path]) -> None:
    total = len(devices) * len(apks)
    ok = True
    success_count = 0
    cancelled = False
    with INSTALL_LOCK:
        session = INSTALL_SESSIONS[session_id]
        session.update({"running": True, "total": total, "done": 0, "percent": 0, "message": "开始安装 APK...", "cancelRequested": False})

    for serial in devices:
        for apk_path in apks:
            with INSTALL_LOCK:
                session = INSTALL_SESSIONS[session_id]
                if session_is_cancelled(session):
                    cancelled = True
                    break
                session["currentDevice"] = serial
                session["currentApk"] = apk_path.name
                phase = "启动器" if "launcher" in apk_path.name.lower() else "应用"
                session["message"] = f"正在安装{phase} {apk_path.name} 到 {serial}"
                session["logs"].append(f"[{time.strftime('%H:%M:%S')}] 开始：{serial} -> {apk_path.name}")

            result = install_apk_on_device(serial, apk_path)
            ok = ok and result["ok"]
            if result["ok"]:
                success_count += 1
            text = (result["stdout"] + result["stderr"]).strip() or "(无输出)"
            method = result.get("installMethod", "install")
            summary = result.get("errorSummary", "")
            with INSTALL_LOCK:
                session = INSTALL_SESSIONS[session_id]
                session["done"] += 1
                session["percent"] = round(session["done"] * 100 / total, 1)
                state = "成功" if result["ok"] else "失败"
                log_lines = [f"[{time.strftime('%H:%M:%S')}] {state}：{serial} -> {apk_path.name}（{method}）"]
                if summary:
                    log_lines.append(f"错误摘要: {summary}")
                log_lines.append(text)
                session["logs"].append("\n".join(log_lines))
            if cancelled:
                break
        if cancelled:
            break

    with INSTALL_LOCK:
        session = INSTALL_SESSIONS[session_id]
        session["running"] = False
        session["ok"] = ok and not cancelled
        session["currentDevice"] = ""
        session["currentApk"] = ""
        if cancelled:
            session["message"] = "APK 安装已取消。"
        elif ok:
            session["message"] = f"APK 安装完成，全部成功（{success_count}/{total}）。"
        else:
            session["message"] = (
                f"APK 安装完成，但存在失败项（成功 {success_count}/{total}，失败 {total - success_count}）。"
            )
        session["completedAt"] = time.time()


def cancel_install_apks(data: dict[str, Any]) -> dict[str, Any]:
    session_id = require_text(data, "sessionId", "安装任务 ID")
    return cancel_session(INSTALL_SESSIONS, INSTALL_LOCK, session_id, "APK 安装")


def start_install_apks(data: dict[str, Any]) -> dict[str, Any]:
    devices, apk_dir, apks = prepare_apk_install(data)
    session_id = uuid4().hex
    session = {
        "sessionId": session_id,
        "running": False,
        "ok": True,
        "apkDir": str(apk_dir),
        "apks": [str(apk) for apk in apks],
        "devices": devices,
        "total": len(devices) * len(apks),
        "done": 0,
        "percent": 0,
        "currentDevice": "",
        "currentApk": "",
        "message": "等待安装任务启动...",
        "logs": [],
        "cancelRequested": False,
        "createdAt": time.time(),
        "completedAt": None,
    }
    with INSTALL_LOCK:
        INSTALL_SESSIONS[session_id] = session
    thread = threading.Thread(target=install_apk_session_worker, args=(session_id, devices, apks), daemon=True)
    thread.start()
    return {"session": public_session(session)}


def get_install_apks_status(data: dict[str, Any]) -> dict[str, Any]:
    session_id = require_text(data, "sessionId", "安装任务 ID")
    with INSTALL_LOCK:
        session = INSTALL_SESSIONS.get(session_id)
        if not session:
            raise ValueError("安装任务不存在或已过期。")
        return {"session": public_session(session)}


def get_screen_size(data: dict[str, Any]) -> dict[str, Any]:
    def worker(serial: str) -> dict[str, Any]:
        return adb(serial, "shell", "wm", "size", timeout=20)

    return for_each_device(data, worker)


def take_screenshot(data: dict[str, Any]) -> dict[str, Any]:
    output_dir = ensure_local_dir(str(data.get("outputDir") or "").strip() or None)

    def worker(serial: str) -> dict[str, Any]:
        filename = f"screenshot_{sanitize_filename(serial)}_{timestamp()}.png"
        output_file = output_dir / filename
        try:
            binary = subprocess.run(
                [*adb_args(serial), "exec-out", "screencap", "-p"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
        except FileNotFoundError:
            return {
                "ok": False,
                "returncode": 127,
                "stdout": "",
                "stderr": "未找到 adb，请确认 Android platform-tools 已加入 PATH。",
            }
        except subprocess.TimeoutExpired:
            return {"ok": False, "returncode": -1, "stdout": "", "stderr": "截图命令执行超时。"}
        if binary.returncode != 0:
            return {
                "ok": False,
                "returncode": binary.returncode,
                "stdout": "",
                "stderr": decode_output(binary.stderr),
            }
        output_file.write_bytes(binary.stdout)
        return {"ok": True, "returncode": 0, "stdout": f"截图已保存：{output_file}", "stderr": ""}

    response = for_each_device(data, worker)
    if any(item.get("ok") for item in response["results"]):
        open_local_dir(output_dir)
    return response


def record_screen(data: dict[str, Any]) -> dict[str, Any]:
    output_dir = ensure_local_dir(str(data.get("outputDir") or "").strip() or None)
    duration = int(data.get("duration") or 10)
    if duration <= 0 or duration > 180:
        raise ValueError("录屏时长必须在 1 到 180 秒之间。")

    def worker(serial: str) -> dict[str, Any]:
        remote_file = f"/sdcard/screenrecord_{sanitize_filename(serial)}_{timestamp()}.mp4"
        record = adb(serial, "shell", "screenrecord", "--time-limit", str(duration), remote_file, timeout=duration + 20)
        if not record["ok"]:
            return record
        output_file = output_dir / PurePosixPath(remote_file).name
        pull = adb(serial, "pull", remote_file, str(output_file), timeout=120)
        adb(serial, "shell", "rm", "-f", remote_file, timeout=20)
        if pull["ok"]:
            pull["stdout"] = f"录屏已保存：{output_file}\n{pull['stdout']}"
        return pull

    response = for_each_device(data, worker)
    if any(item.get("ok") for item in response["results"]):
        open_local_dir(output_dir)
    return response


def start_recording(data: dict[str, Any]) -> dict[str, Any]:
    output_dir = ensure_local_dir(str(data.get("outputDir") or "").strip() or None)

    def worker(serial: str) -> dict[str, Any]:
        with RECORDING_LOCK:
            session = RECORDING_SESSIONS.get(serial)
            if session and session["process"].poll() is None:
                return {"ok": False, "returncode": 1, "stdout": "", "stderr": "该设备已有录屏正在进行。"}

            remote_file = f"/sdcard/screenrecord_{sanitize_filename(serial)}_{timestamp()}.mp4"
            try:
                process = subprocess.Popen(  # noqa: S603 - local test tool intentionally invokes adb.
                    [*adb_args(serial), "shell", "screenrecord", remote_file],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            except FileNotFoundError:
                return {
                    "ok": False,
                    "returncode": 127,
                    "stdout": "",
                    "stderr": "未找到 adb，请确认 Android platform-tools 已加入 PATH。",
                }

            time.sleep(0.5)
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                return {
                    "ok": False,
                    "returncode": process.returncode,
                    "stdout": decode_output(stdout),
                    "stderr": decode_output(stderr),
                }

            RECORDING_SESSIONS[serial] = {
                "process": process,
                "remote_file": remote_file,
                "output_dir": output_dir,
                "started_at": time.time(),
            }
            return {"ok": True, "returncode": 0, "stdout": f"录屏已开始：{remote_file}", "stderr": ""}

    return for_each_device(data, worker)


def stop_recording(data: dict[str, Any]) -> dict[str, Any]:
    opened_dirs: set[Path] = set()

    def worker(serial: str) -> dict[str, Any]:
        with RECORDING_LOCK:
            session = RECORDING_SESSIONS.pop(serial, None)
        if not session:
            return {"ok": False, "returncode": 1, "stdout": "", "stderr": "该设备没有正在进行的录屏。"}

        process: subprocess.Popen[bytes] = session["process"]
        remote_file = str(session["remote_file"])
        output_dir: Path = session["output_dir"]
        elapsed = max(0, int(time.time() - float(session["started_at"])))

        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        stdout, stderr = process.communicate()

        output_file = output_dir / PurePosixPath(remote_file).name
        pull = adb(serial, "pull", remote_file, str(output_file), timeout=120)
        adb(serial, "shell", "rm", "-f", remote_file, timeout=20)
        if pull["ok"]:
            opened_dirs.add(output_dir)
            pull["stdout"] = (
                f"录屏已停止，时长约 {elapsed} 秒。\n"
                f"录屏已保存：{output_file}\n"
                f"{pull['stdout']}"
            )
            if stdout or stderr:
                pull["stdout"] += f"\n录屏命令输出：\n{decode_output(stdout)}{decode_output(stderr)}"
        return pull

    response = for_each_device(data, worker)
    for output_dir in opened_dirs:
        open_local_dir(output_dir)
    return response


def get_current_package(data: dict[str, Any]) -> dict[str, Any]:
    package_pattern = re.compile(r"([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)")

    def worker(serial: str) -> dict[str, Any]:
        candidates = [
            adb(serial, "shell", "dumpsys", "window", "windows", timeout=30),
            adb(serial, "shell", "dumpsys", "activity", "top", timeout=30),
        ]
        for result in candidates:
            if not result["ok"]:
                continue
            focused_lines = [
                line.strip()
                for line in result["stdout"].splitlines()
                if any(key in line for key in ("mCurrentFocus", "mFocusedApp", "ACTIVITY"))
            ]
            for line in focused_lines:
                match = package_pattern.search(line)
                if match:
                    result["stdout"] = f"当前应用包名：{match.group(1)}\n来源：{line}"
                    return result
        return {"ok": False, "returncode": 1, "stdout": "", "stderr": "未能解析当前应用包名。"}

    return for_each_device(data, worker)


API_ROUTES = {
    "/api/devices": lambda data: list_devices(),
    "/api/connect-ip": lambda data: connect_ip(require_text(data, "ip", "设备 IP")),
    "/api/process-memory": get_process_memory,
    "/api/app-memory": get_app_memory,
    "/api/app-cpu": get_app_cpu,
    "/api/total-cpu": get_total_cpu,
    "/api/app-total-cpu": get_app_and_total_cpu,
    "/api/stats-sample": get_stats_sample,
    "/api/stats-save-excel": save_stats_excel,
    "/api/stats-save-charts": save_stats_charts,
    "/api/fill-memory": fill_memory,
    "/api/fill-memory-start": start_fill_memory,
    "/api/fill-memory-status": get_fill_memory_status,
    "/api/fill-memory-cancel": cancel_fill_memory,
    "/api/monkey-start": start_monkey_test,
    "/api/monkey-status": get_monkey_status,
    "/api/monkey-cancel": cancel_monkey_test,
    "/api/install-apks": install_apks,
    "/api/install-apks-start": start_install_apks,
    "/api/install-apks-status": get_install_apks_status,
    "/api/install-apks-cancel": cancel_install_apks,
    "/api/screen-size": get_screen_size,
    "/api/screenshot": take_screenshot,
    "/api/record": record_screen,
    "/api/record-start": start_recording,
    "/api/record-stop": stop_recording,
    "/api/current-package": get_current_package,
}


class RequestHandler(SimpleHTTPRequestHandler):
    server_version = "AndroidTestTool/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.serve_file(RESOURCE_DIR / "index.html")
            return
        if parsed.path == "/api/devices":
            self.write_json(list_devices())
            return
        if parsed.path == "/api/adb-status":
            self.write_json(get_adb_status())
            return
        file_path = (RESOURCE_DIR / parsed.path.lstrip("/")).resolve()
        if RESOURCE_DIR not in file_path.parents and file_path != RESOURCE_DIR:
            self.send_error(HTTPStatus.FORBIDDEN, "Forbidden")
            return
        self.serve_file(file_path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        handler = API_ROUTES.get(parsed.path)
        if handler is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Unknown API")
            return
        try:
            data = self.read_json()
            response = handler(data)
            self.write_json({"ok": True, **response})
        except ValueError as exc:
            self.write_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001 - expose controlled errors to the local UI.
            self.write_json({"ok": False, "error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def write_json(self, data: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def serve_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        content = path.read_bytes()
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")


def main() -> None:
    host = os.environ.get("ANDROID_TOOL_HOST", "127.0.0.1")
    port = int(os.environ.get("ANDROID_TOOL_PORT", "8000"))
    DEFAULT_CAPTURE_DIR.mkdir(exist_ok=True)
    DEFAULT_STATS_DIR.mkdir(exist_ok=True)
    DEFAULT_CHART_DIR.mkdir(exist_ok=True)
    DEFAULT_MONKEY_DIR.mkdir(exist_ok=True)
    with ThreadingHTTPServer((host, port), RequestHandler) as server:
        url = f"http://{host}:{port}"
        print(f"Android 测试工具已启动：{url}")
        print(f"ADB：{ADB_EXECUTABLE}（来源：{ADB_SOURCE}）")
        print("请确保 adb 可用，并已在 Android 设备上开启 USB 调试。")
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        server.serve_forever()


if __name__ == "__main__":
    main()
