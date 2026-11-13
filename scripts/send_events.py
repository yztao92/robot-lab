#!/usr/bin/env python3
"""Feed raw input-event JSONL to the local simulator, always marked as replay.

This sends at a fixed interval; it does not wait for simulated or motor arrival.
Exported UI traces also contain receipts and cannot be replayed as raw commands.
"""
import argparse
import importlib.util
import json
import math
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("robot_simulator_server", ROOT / "server.py")
SERVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVER)


def load_events(path):
    """Validate the entire batch before sending its first event."""
    events = []
    if path.stat().st_size > 10 * SERVER.MAX_BODY:
        raise ValueError("JSONL 文件不能超过 10 MiB")
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            if len(line.encode()) > SERVER.MAX_BODY:
                raise ValueError("单个事件不能超过 1 MiB")
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("事件必须是 JSON 对象")
            event["source"] = "replay"
            events.append((number, SERVER.validate_event(event)))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"第 {number} 行无效：{exc}。仅支持原始输入事件，不支持含模拟回执的导出时间线。") from None
    if not events:
        raise ValueError("文件中没有输入事件")
    return events


def call_json(url, event=None):
    request = Request(url)
    if event is not None:
        request = Request(url, data=json.dumps(event).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=5) as response:
            return json.load(response)
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("error", str(exc))
        except ValueError:
            detail = str(exc)
        raise RuntimeError(f"服务拒绝请求（HTTP {exc.code}）：{detail}") from None
    except (URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError(f"无法访问本地模拟服务：{exc}") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path, nargs="?", default=ROOT / "examples/events.jsonl")
    parser.add_argument("--interval", type=float, default=2.0, help="事件间隔秒数（默认 2 秒），不等待动作回执")
    parser.add_argument("--url", default="http://127.0.0.1:8769/api/events", help="本地模拟器的 POST 地址")
    parser.add_argument("--dry-run", action="store_true", help="只检查文件，不连接服务")
    args = parser.parse_args()
    parsed = urlsplit(args.url)
    try:
        if not math.isfinite(args.interval) or args.interval < 0:
            raise ValueError("interval 必须是有限的非负秒数")
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
                or parsed.path != "/api/events" or parsed.query or parsed.fragment or parsed.username or parsed.password):
            raise ValueError("url 必须是本机 http://127.0.0.1:<端口>/api/events 或 localhost 同类地址")
        events = load_events(args.file)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(f"已校验 {len(events)} 个输入事件；source 统一为 replay。", flush=True)
    if args.dry_run:
        return 0
    sent = 0
    try:
        health = call_json(args.url.removesuffix("/api/events") + "/api/health")
        if not health.get("ok"):
            raise RuntimeError("服务健康检查失败")
        print(f"当前 SSE 接收端：{health.get('subscribers', '?')}；定时间隔 {args.interval:g} 秒，不等待动作回执。", flush=True)
        if health.get("subscribers", 0) == 0:
            raise RuntimeError("没有打开的预览接收端。请先在浏览器打开模拟器；服务不会补播历史事件。")
        for number, event in events:
            if sent:
                time.sleep(args.interval)
            result = call_json(args.url, event)
            if not result.get("ok") or not isinstance(result.get("event"), dict):
                raise RuntimeError("服务未返回有效接收确认")
            sent += 1
            print(f"[{sent}/{len(events)}] 第 {number} 行 {event['type']} → 已接收 #{result['event'].get('seq', '?')}", flush=True)
    except (RuntimeError, KeyboardInterrupt) as exc:
        detail = "用户中断" if isinstance(exc, KeyboardInterrupt) else str(exc)
        print(f"已停止：{detail}；此前服务已接收 {sent} 个事件。", file=sys.stderr)
        return 1
    print("输入发送完成；执行状态请查看预览时间线。", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
