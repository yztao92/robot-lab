#!/usr/bin/env python3
"""Manage Robot Lab with launchd: it starts at login and comes back if it exits."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

from preflight import check

ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.yztao92.robot-lab"
DOMAIN = f"gui/{os.getuid()}"
TARGET = f"{DOMAIN}/{LABEL}"
# In LaunchAgents, so launchd starts it again at every login.
PLIST = Path.home() / f"Library/LaunchAgents/{LABEL}.plist"
LOGS = Path.home() / "Library/Logs/RobotLab"
URL = "http://127.0.0.1:8769"


def launchctl(*args):
    return subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True)


def loaded():
    return launchctl("print", TARGET).returncode == 0


def health():
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(URL + "/api/health", timeout=1) as response:
            return json.load(response).get("ok") is True
    except (OSError, ValueError):
        return False


def port_in_use():
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", 8769)) == 0


def stop():
    if not loaded():
        print("后台服务未运行。")
        return
    result = launchctl("bootout", TARGET)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not loaded() and not port_in_use():
            print("Robot Lab 已停止；下次登录时会自动启动。")
            return
        time.sleep(0.2)
    raise RuntimeError("停止尚未完成，请检查服务状态与日志。")


def start(no_board=False):
    newly_loaded = False
    if not loaded():
        if port_in_use():
            raise RuntimeError("8769 端口已有其他进程，请先关闭原来的前台服务，再启动后台服务。")
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        LOGS.mkdir(parents=True, exist_ok=True)
        # The stable path, not sys.executable: a Homebrew upgrade removes the versioned one.
        args = [os.environ.get("ROBOTLAB_PYTHON") or shutil.which("python3") or sys.executable,
                "-u", str(ROOT / "server.py"), "--port", "8769"]
        if no_board:
            args.append("--no-board")
        definition = {
            "Label": LABEL,
            "ProgramArguments": args,
            "WorkingDirectory": str(ROOT),
            "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                                     "PYTHONUNBUFFERED": "1"},
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 5,
            "ExitTimeOut": 5,
            "StandardOutPath": str(LOGS / "stdout.log"),
            "StandardErrorPath": str(LOGS / "stderr.log"),
        }
        PLIST.write_bytes(plistlib.dumps(definition))
        PLIST.chmod(0o600)
        result = launchctl("bootstrap", DOMAIN, str(PLIST))
        if result.returncode:
            raise RuntimeError(f"后台启动失败：{result.stderr.strip()}")
        newly_loaded = True
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if health():
            print(f"Robot Lab 已运行：{URL}/")
            print(f"日志：{LOGS}")
            print("关闭终端不影响运行，登录时自动启动；需要停止时双击「停止模拟器.command」。")
            return
        time.sleep(0.25)
    # Only clean up this invocation's failed startup; never stop an existing service.
    if newly_loaded:
        launchctl("bootout", TARGET)
        raise RuntimeError(f"15 秒内未能启动，已取消后台运行。请检查 {LOGS / 'stderr.log'}")
    raise RuntimeError(f"现有后台服务暂未响应，已保留运行。请检查 {LOGS / 'stderr.log'}，需要时使用 restart。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "stop", "restart", "status"))
    parser.add_argument("--no-board", action="store_true", help="启动时不自动连接上次主板，可在页面选择主板")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("后台托管仅支持 macOS；其他平台请直接运行 server.py。")
    try:
        if args.command in ("start", "restart"):
            check()
            if loaded() and PLIST.exists():
                definition = plistlib.loads(PLIST.read_bytes())
                if Path(definition.get("WorkingDirectory", "")).resolve() != ROOT:
                    raise RuntimeError("已有另一个目录的 Robot Lab 在运行，请先从原目录停止，再启动这一份。")
        if args.command in ("stop", "restart"):
            stop()
        if args.command in ("start", "restart"):
            start(args.no_board)
        elif args.command == "status":
            print(f"后台托管：{'已加载' if loaded() else '未加载'}")
            print(f"本地 HTTP：{'正常' if health() else '不可用'}")
            print(f"日志：{LOGS}")
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
