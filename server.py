#!/usr/bin/env python3
"""Robot Lab server: assets, the event hub, and the link to the selected RK3576 board.

Run: python3 server.py --port 8769 [--board-ip 192.168.0.7]
POST /api/events accepts one documented event. GET /api/events is SSE.
GET /api/boards[/scan] lists (or finds) boards; POST /api/board/select switches, with an optional
password when key auth is refused. Board writes: `systemctl start|stop|restart voiced`, the
backlight, the fan's PWM duty, the amplifier's attenuation, and — only while the camera window is open —
a read of the ISP's self path beside visiond (read-only). Never servo commands.
POST /api/camera {"cmd":"start"|"stop"} opens or closes it; GET /api/camera/frame?after=N long-polls
for the next raw NV12 frame (dimensions in X-Frame-Width / X-Frame-Height).
The source label is supplied by the producer, not proof of a board connection.
"""
import argparse
import datetime as dt
import json
import math
import mimetypes
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
import board_link  # noqa: E402
MEDIA = ROOT / "media"
PROTOCOL = ROOT / "data" / "ServoActionList.json"
MAX_BODY = 1024 * 1024
BOARD_SERVICES = ("robotd", "voiced", "visiond", "waked", "translated", "mediad")
import board_bridge  # noqa: E402  (scripts/ is on the path above)
# Both amplifier instances (5-0034, 5-0037) carry the same setting.
VOLUME_SET = "amixer -q -c 0 cset name=aw_dev_0_rx_volume {raw}; amixer -q -c 0 cset name=aw_dev_1_rx_volume {raw}"
# The pwm-fan's hwmon index (hwmon6 on .8) depends on probe order, so it is globbed. pwm1 is
# inverted on this board (pwm-active-low): see board_bridge.fan_pwm.
FAN_PWM = "/sys/devices/platform/pwm-fan/hwmon/hwmon*/pwm1"
FAN_SET = f"f=$(ls {FAN_PWM} 2>/dev/null | head -1); [ -n \"$f\" ] && echo {{pwm}} > $f"
# One `key=value` per line, so a missing figure never shifts the others.
STATUS = ("for s in {services}; do echo svc_$s=$(systemctl is-active $s); done; "
          "echo up=$(cut -d. -f1 /proc/uptime); echo load=$(cut -d' ' -f1 /proc/loadavg); "
          # CPU busy share over 0.5 s, all cores: /proc/stat's cpu line twice; idle + iowait are idle.
          # (STATUS goes through str.format, hence the doubled braces.)
          "echo cpu=$({{ head -1 /proc/stat; sleep 0.5; head -1 /proc/stat; }} | awk '{{t=0; for (i = 2; i <= 9; i++) t += $i; "
          "d = $5 + $6; if (NR == 1) {{ t0 = t; d0 = d }} else if (t > t0) printf \"%d\", 100 * (1 - (d - d0) / (t - t0)) + 0.5 }}'); "
          "echo mem=$(awk '/^MemTotal:/ {{t = $2}} /^MemAvailable:/ {{a = $2}} END {{if (t) printf \"%d\", 100 * (t - a) / t + 0.5}}' /proc/meminfo); "
          "echo temp=$(cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null | sort -n | tail -1); "
          "echo vol=$(amixer -c 0 cget name=aw_dev_0_rx_volume | sed -n 's/.*: values=//p'); "
          "b=$(ls -d /sys/class/backlight/* | head -1); echo bl=$(cat $b/brightness) bl_max=$(cat $b/max_brightness); "
          f"echo fan=$(cat {FAN_PWM} 2>/dev/null | head -1)")
BRIGHTNESS_SET = "b=$(ls -d /sys/class/backlight/* | head -1); echo $(( $(cat $b/max_brightness) * {percent} / 100 )) > $b/brightness"
POSE_AXES = {"head_yaw", "head_pitch_down", "head_roll", "arm_l_out", "arm_l_forward", "arm_r_out", "arm_r_forward"}
SOURCES = {"injected", "replay", "board", "cloud", "pc"}
COMMON = {"type", "source", "timestamp", "emotion", "turn_id", "detail"}
FIELDS = {
    "action": {"action_id"}, "expression": {"expression_id", "mode"},
    "state": {"state_id"}, "doa": {"angle_deg", "frame"},
    "reset": set(), "stop": set(),
    "caption": {"kind", "text"},
    "board_status": set(),
    "board_log": {"message"},
    "pose": {"joints"},
    "faces": {"faces"},
    "metric": {"turn_id", "metrics"},
}
METRIC_KEYS = {"e2e_ms", "end_ms", "server_first_ms", "play_ms", "intent_ms", "reply_s", "doa_ms"}
OPTIONAL_FIELDS = {
    "caption": {"is_final", "action_id"},
    "expression": {"action_id"},
    "board_status": {
        "board_online", "board_ip", "services", "voice_active", "uptime_s", "turn_phase",
        "cpu_temp_c", "cpu_pct", "mem_pct", "npu_pct", "cpu_mhz", "cpu_mhz_full", "load_1m", "people", "service_cpu",
        "auth_required", "board_error", "volume_raw", "volume_pct", "brightness_pct", "fan_pct",
    },
    "board_log": {"level", "logger"},
    "metric": {"note", "woken"},
}


def validate_event(event):
    if not isinstance(event, dict):
        raise ValueError("Event must be a JSON object")
    kind = event.get("type")
    if not isinstance(kind, str) or kind not in FIELDS:
        raise ValueError("Unsupported event type")
    source = event.get("source")
    if not isinstance(source, str) or source not in SOURCES:
        raise ValueError("source must be injected, replay, board, cloud, or pc")
    allowed = COMMON | FIELDS[kind] | OPTIONAL_FIELDS.get(kind, set())
    if set(event) - allowed:
        raise ValueError("Unknown event fields")
    if FIELDS[kind] - set(event):
        raise ValueError("Missing event fields")
    if "timestamp" in event:
        value = event["timestamp"]
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError("timestamp must be an ISO 8601 string with a timezone")
        try:
            stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError()
        except ValueError:
            raise ValueError("timestamp must be an ISO 8601 string with a timezone") from None
    if kind == "action" and event["action_id"] not in {f"A{i:02}" for i in range(1, 22)}:
        raise ValueError("Unknown action_id")
    if kind == "expression":
        if event["expression_id"] not in {f"F{i:02}" for i in range(1, 22)} | {"D01", "D02"}:
            raise ValueError("Unknown expression_id")
        if event["mode"] not in ("full", "loop", "once"):
            raise ValueError("mode must be full, loop or once")
        if "action_id" in event and (not event["expression_id"].startswith("F")
                                     or event["action_id"] != "A" + event["expression_id"][1:]):
            raise ValueError("action_id must be the expression's paired action")
    if kind == "state" and event["state_id"] not in {f"R{i:02}" for i in range(8)} | {f"T{i:02}" for i in range(1, 9)}:
        raise ValueError("Unknown state_id")
    if kind == "doa":
        angle = event["angle_deg"]
        if type(angle) not in (int, float) or not math.isfinite(angle) or not 0 <= angle < 360:
            raise ValueError("angle_deg must be finite and in [0, 360)")
        if event["frame"] != "world":
            raise ValueError("frame must be world; adapt board coordinates before publishing")
    if kind == "metric":
        metrics = event["metrics"]
        if not isinstance(event["turn_id"], str) or not 0 < len(event["turn_id"]) <= 32:
            raise ValueError("turn_id must be a short string")
        if not isinstance(metrics, dict) or set(metrics) - METRIC_KEYS:
            raise ValueError("metrics must use known keys")
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in metrics.values()):
            raise ValueError("metric values must be finite numbers")
        if "note" in event and (not isinstance(event["note"], str) or len(event["note"]) > 60):
            raise ValueError("note must be a short string")
        if "woken" in event and not isinstance(event["woken"], bool):
            raise ValueError("woken must be a boolean")
    if kind == "faces":
        validate_faces(event["faces"])
    if kind == "pose":
        joints = event["joints"]
        if not isinstance(joints, dict) or not joints or set(joints) - POSE_AXES:
            raise ValueError("joints must map known axes to angles")
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in joints.values()):
            raise ValueError("joint angles must be finite numbers")
    return dict(event)


class EventHub:
    def __init__(self, queue_size=256):
        self.lock = threading.Lock()
        self.clients = set()
        self.seq = 0
        self.queue_size = queue_size
        self.dropped_clients = 0
        self.last_event_at = None

    def subscribe(self):
        channel = queue.Queue(maxsize=self.queue_size)
        with self.lock:
            self.clients.add(channel)
        return channel

    def unsubscribe(self, channel):
        with self.lock:
            self.clients.discard(channel)

    def publish(self, event):
        with self.lock:
            self.seq += 1
            event = dict(event, seq=self.seq,
                         received_at=dt.datetime.now(dt.timezone.utc).isoformat())
            self.last_event_at = event["received_at"]
            for channel in tuple(self.clients):
                try:
                    channel.put_nowait(event)
                except queue.Full:
                    self.clients.discard(channel)
                    self.dropped_clients += 1
                    # Never silently continue after losing control events.
                    while not channel.empty():
                        try:
                            channel.get_nowait()
                        except queue.Empty:
                            break
                    channel.put_nowait(None)
            return event

    def health(self):
        with self.lock:
            return {"ok": True, "transport": "local_http_sse", "hardware_verified": False,
                    "subscribers": len(self.clients), "event_count": self.seq,
                    "last_event_at": self.last_event_at, "dropped_clients": self.dropped_clients}


class SimulatorServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, root=ROOT, media=MEDIA, protocol=PROTOCOL, link=None):
        self.root, self.media, self.protocol = Path(root).resolve(), Path(media).resolve(), Path(protocol).resolve()
        self.hub = EventHub()
        self.link = link
        super().__init__(address, Handler)


def validate_faces(faces) -> None:
    if not isinstance(faces, list) or len(faces) > 32:
        raise ValueError("faces must be a short list")

    def fractions(values, count) -> bool:
        return (isinstance(values, list) and len(values) == count
                and all(type(v) in (int, float) and math.isfinite(v) for v in values))

    for face in faces:
        if not isinstance(face, dict) or set(face) - {"id", "name", "talking", "bbox", "landmarks"}:
            raise ValueError("a face uses known keys")
        if "bbox" in face and not fractions(face["bbox"], 4):
            raise ValueError("bbox must be four numbers")
        points = face.get("landmarks", [])
        if not isinstance(points, list) or len(points) > 98 or not all(fractions(p, 2) for p in points):
            raise ValueError("landmarks must be up to 98 number pairs")


def query_board_status(link):
    host = link.current
    if not host:
        return {"ok": True, "board_online": False, "board_ip": "", "services": {}, "voice_active": False}
    try:
        # is-active exits non-zero when any unit is down; the output is still the answer.
        done = link.run(host, STATUS.format(services=" ".join(BOARD_SERVICES)), timeout=4)
        kv = dict(pair.split("=", 1) for pair in done.stdout.decode("utf-8", errors="replace").split() if "=" in pair)
        if "up" not in kv:
            raise board_link.BoardError("unexpected status output")
        services = {s: kv.get(f"svc_{s}", "unknown") for s in BOARD_SERVICES}
        status = {"ok": True, "board_online": True, "board_ip": host, "services": services,
                  "voice_active": services["voiced"] == "active", "uptime_s": int(kv["up"])}
        if kv.get("temp", "").isdigit():
            status["cpu_temp_c"] = round(int(kv["temp"]) / 1000, 1)
        if kv.get("cpu", "").isdigit():
            status["cpu_pct"] = min(100, int(kv["cpu"]))
        if kv.get("mem", "").isdigit():
            status["mem_pct"] = min(100, int(kv["mem"]))
        try:
            status["load_1m"] = float(kv["load"])
        except (KeyError, ValueError):
            pass
        if kv.get("vol", "").isdigit():
            status["volume_raw"] = int(kv["vol"])
            status["volume_pct"] = board_bridge.volume_percent(int(kv["vol"]))
        if kv.get("fan", "").isdigit():
            status["fan_pct"] = board_bridge.fan_percent(int(kv["fan"]))
        if kv.get("bl", "").isdigit() and kv.get("bl_max", "").isdigit():
            status["brightness_pct"] = board_bridge.brightness_percent(int(kv["bl"]), int(kv["bl_max"]))
        return status
    except board_link.AuthRequired as exc:
        return {"ok": True, "board_online": True, "board_ip": host, "services": {}, "voice_active": False,
                "auth_required": True, "board_error": str(exc)}
    except (board_link.BoardError, subprocess.TimeoutExpired, ValueError) as exc:
        return {"ok": True, "board_online": False, "board_ip": host, "services": {}, "voice_active": False,
                "board_error": str(exc)[:200]}


def run_board_control(link, payload):
    action = payload.get("cmd")
    if action in ("start", "stop", "restart"):
        remote = f"systemctl {action} voiced"
    elif action in ("brightness", "fan", "volume"):
        percent = payload.get("percent")
        if type(percent) is not int or not 0 <= percent <= 100:
            raise ValueError("percent must be an integer 0..100")
        if action == "volume":
            remote = VOLUME_SET.format(raw=board_bridge.volume_raw(percent))
        elif action == "fan":
            remote = FAN_SET.format(pwm=board_bridge.fan_pwm(percent))
        else:
            remote = BRIGHTNESS_SET.format(percent=max(percent, 5))  # never fully dark from here
    else:
        raise ValueError("Unsupported control cmd")
    try:
        link.run(link.current, remote)
    except subprocess.TimeoutExpired:
        # The remote command may still finish; report fresh status plus the timeout.
        return dict(query_board_status(link), ok=False, error=f"板端命令 {action} 超时（25s）")
    except board_link.BoardError as exc:
        return dict(query_board_status(link), ok=False, error=str(exc))
    return query_board_status(link)


class Handler(BaseHTTPRequestHandler):
    server_version = "RobotSimulator/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        # GET /api/health polling and camera frames should not dominate a terminal session.
        if self.path not in ("/api/health", "/api/board/status") and not self.path.startswith("/api/camera/frame"):
            super().log_message(format, *args)

    def allowed_host(self):
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in {f"localhost:{port}", f"127.0.0.1:{port}"}

    def reply(self, status, obj, head=False):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def do_POST(self):
        if not self.allowed_host():
            self.close_connection = True
            return self.reply(403, {"ok": False, "error": "Local host only"})
        req_path = urlsplit(self.path).path
        if req_path not in ("/api/events", "/api/board/control", "/api/board/select", "/api/camera"):
            self.close_connection = True
            return self.reply(404, {"ok": False, "error": "Unknown endpoint"})
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://127.0.0.1:{self.server.server_address[1]}",
                                     f"http://localhost:{self.server.server_address[1]}"}:
            self.close_connection = True
            return self.reply(403, {"ok": False, "error": "Cross-origin event writes are disabled"})
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            self.close_connection = True
            return self.reply(415, {"ok": False, "error": "Use application/json"})
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if not 0 < length <= MAX_BODY or self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            return self.reply(413, {"ok": False, "error": "Send a body with Content-Length between 1 and 1048576"})
        self.connection.settimeout(10)
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("Incomplete request body")
            payload = json.loads(raw)
            if req_path != "/api/events" and not isinstance(payload, dict):
                raise ValueError("Board payload must be a JSON object")
            if req_path == "/api/board/select":
                password = payload.get("password")
                if password is not None and (not isinstance(password, str) or not 0 < len(password) <= 256):
                    raise ValueError("password must be a non-empty string")
                try:
                    unreachable = self.server.link.select(str(payload.get("host", "")).strip(), password)
                except board_link.AuthRequired as exc:
                    return self.reply(401, {"ok": False, "auth_required": True, "board_ip": payload.get("host"),
                                            "error": str(exc)})
                status_obj = query_board_status(self.server.link) if unreachable is None else {
                    "ok": True, "board_online": False, "board_ip": self.server.link.current, "services": {},
                    "voice_active": False, "board_error": unreachable}
                return self.reply(200, dict(status_obj, board=self.server.link.listing()))
            if req_path == "/api/camera":
                return self.camera_control(payload)
            if req_path == "/api/board/control":
                status_obj = run_board_control(self.server.link, payload)
                self.server.hub.publish(dict(
                    {k: v for k, v in status_obj.items() if k in OPTIONAL_FIELDS["board_status"]},
                    type="board_status", source="board"))
                return self.reply(200 if status_obj["ok"] else 504, status_obj)
            event = validate_event(payload)
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            return self.reply(400, {"ok": False, "error": str(exc)})
        except TimeoutError:
            self.close_connection = True
            return self.reply(408, {"ok": False, "error": "Request body timeout"})
        finally:
            self.connection.settimeout(None)
        event = self.server.hub.publish(event)
        self.reply(200, {"ok": True, "event": event})

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head=False):
        if not self.allowed_host():
            return self.reply(403, {"ok": False, "error": "Local host only"}, head)
        path = unquote(urlsplit(self.path).path)
        if "\x00" in path:
            return self.reply(400, {"ok": False, "error": "Invalid path"}, head)
        if path == "/api/health":
            return self.reply(200, self.server.hub.health(), head)
        if path == "/api/board/status":
            return self.reply(200, query_board_status(self.server.link), head)
        if path == "/api/boards":
            return self.reply(200, self.server.link.listing(), head)
        if path == "/api/boards/scan":
            return self.reply(200, self.server.link.scan(), head)
        if path == "/api/camera/status":
            return self.reply(200, self.server.link.camera.status(), head)
        if path == "/api/camera/frame":
            return self.camera_frame(head)
        if path == "/api/events":
            if head:
                return self.reply(405, {"ok": False, "error": "Use GET for SSE"}, head)
            return self.stream_events()
        if path.startswith("/api/"):
            return self.reply(404, {"ok": False, "error": "Unknown endpoint"}, head)
        if path.startswith("/media/"):
            name = path[len("/media/"):]
            file = (self.server.media / name).resolve()
            allowed = file.parent == self.server.media and file.suffix.lower() == ".mp4"
        elif path == "/data/ServoActionList.json":
            file, allowed = self.server.protocol, True
        else:
            file = (self.server.root / (path.lstrip("/") or "index.html")).resolve()
            allowed = (file.is_relative_to(self.server.root)
                       and not any(part.startswith(".") for part in Path(path).parts if part != "/"))
        if not allowed:
            return self.reply(403, {"ok": False, "error": "Path outside asset directory"}, head)
        if not file.is_file():
            return self.reply(404, {"ok": False, "error": "Asset not found"}, head)
        self.serve_file(file, head)

    def camera_control(self, payload):
        cmd, camera = payload.get("cmd"), self.server.link.camera
        if cmd == "stop":
            return self.reply(200, camera.stop())
        if cmd != "start":
            raise ValueError("cmd must be start or stop")
        scale, fps = payload.get("scale", 1), payload.get("fps", 4)
        if scale not in (1, 2, 4) or type(scale) is not int:
            raise ValueError("scale must be 1, 2 or 4")
        if type(fps) not in (int, float) or not 0.5 <= fps <= 60:
            raise ValueError("fps must be between 0.5 and 60")
        try:
            return self.reply(200, camera.start(scale, float(fps)))
        except board_link.AuthRequired as exc:
            return self.reply(401, {"ok": False, "auth_required": True, "board_ip": self.server.link.current,
                                    "error": str(exc)})
        except board_link.BoardError as exc:
            return self.reply(502, dict(camera.status(), ok=False, error=str(exc)[:200]))

    def camera_frame(self, head):
        query = parse_qs(urlsplit(self.path).query)
        try:
            after = int(query.get("after", ["0"])[0])
        except ValueError:
            return self.reply(400, {"ok": False, "error": "after must be an integer"}, head)
        frame = None if head else self.server.link.camera.wait_frame(after)
        if frame is None:
            self.send_response(204)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        seq, width, height, body = frame
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Seq", str(seq))
        self.send_header("X-Frame-Width", str(width))
        self.send_header("X-Frame-Height", str(height))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def serve_file(self, file, head):
        size = file.stat().st_size
        start, end, status = 0, size - 1, 200
        requested = self.headers.get("Range")
        if requested:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
            valid = bool(match and any(match.groups()))
            if valid:
                left, right = match.groups()
                if left:
                    start = int(left)
                    end = min(int(right), size - 1) if right else size - 1
                else:
                    start = max(0, size - int(right))
                valid = size > 0 and start <= end and start < size
            if not valid:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        self.send_response(status)
        content_type = mimetypes.guess_type(str(file))[0] or "application/octet-stream"
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(max(0, end - start + 1)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head:
            return
        try:
            with file.open("rb") as stream:
                stream.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = stream.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def stream_events(self):
        channel = self.server.hub.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(b'event: ready\ndata: {"replay":false}\n\n')
            self.wfile.flush()
            while True:
                try:
                    event = channel.get(timeout=10)
                except queue.Empty:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    continue
                if event is None:
                    self.wfile.write(b'event: gap\ndata: {"error":"subscriber_overflow","resume":"resend current state"}\n\n')
                    self.wfile.flush()
                    return
                payload = json.dumps(event, ensure_ascii=False)
                self.wfile.write(f"id: {event['seq']}\ndata: {payload}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        finally:
            self.server.hub.unsubscribe(channel)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8769)
    parser.add_argument("--board-ip", help="启动时连接的主板；省略则用上次选中的（页面左上角可切换）")
    parser.add_argument("--no-board", action="store_true", help="启动时不自动连接主板；之后可在页面选择")
    args = parser.parse_args()
    if args.no_board and args.board_ip:
        parser.error("--no-board 不能与 --board-ip 同时使用")
    server = SimulatorServer(("127.0.0.1", args.port))
    server.link = board_link.BoardLink(server.hub.publish, initial=args.board_ip)
    if args.no_board:
        server.link.current = None  # keep the saved selection on disk intact
    server.link.start_bridge()
    print(f"Robot simulator: http://127.0.0.1:{args.port}  board={server.link.current or '未选择'}", flush=True)
    print("Board events come over ssh; source labels do not verify hardware. Ctrl-C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.link.stop()
        server.server_close()


if __name__ == "__main__":
    main()
